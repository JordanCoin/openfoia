#!/usr/bin/env python3
"""Validate a release and extract its notes.

Used by ``.github/workflows/release.yml`` both on a release pull request (to
fail before merge) and after the merge (to publish). Stdlib only, so it runs
before the project is installed.

A release is consistent when all of these name the same ``X.Y.Z``:

- the branch name: ``release/X.Y.Z`` (``release/vX.Y.Z`` and
  ``release/release-X.Y.Z`` are accepted too)
- ``version`` in ``pyproject.toml``
- ``__version__`` in ``openfoia/__init__.py``
- a ``## [X.Y.Z]`` section in ``CHANGELOG.md`` with a non-empty body, which
  becomes the GitHub release notes

Usage::

    python scripts/release.py --branch release/4.3.0 --notes-out notes.md
    python scripts/release.py --version 4.3.0 --notes-out notes.md

On success it prints the version and exits 0. On failure it lists every
problem at once and exits 1, so one CI run shows everything to fix.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Plain SemVer core only. Pre-release suffixes are deliberately unsupported:
#: SemVer writes ``4.3.0-rc.1`` where PEP 440 wants ``4.3.0rc1``, and a
#: silent mismatch between the tag and the installed version is worse than
#: not having release candidates.
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_BRANCH_RE = re.compile(r"release/(?:release-|v)?(?P<version>.+)")


class ReleaseError(Exception):
    """One or more reasons the release is not consistent."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def version_from_branch(ref: str) -> str:
    """Return ``X.Y.Z`` from a release branch name, or raise ReleaseError."""
    ref = ref.removeprefix("refs/heads/")
    match = _BRANCH_RE.fullmatch(ref)
    if not match or not VERSION_RE.fullmatch(match.group("version")):
        raise ReleaseError(
            [
                f"branch '{ref}' is not a release branch: expected release/X.Y.Z "
                "(for example release/4.3.0; release/v4.3.0 and "
                "release/release-4.3.0 also work)"
            ]
        )
    return match.group("version")


def pyproject_version(root: Path) -> str | None:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data.get("project", {}).get("version")


def package_version(root: Path) -> str | None:
    text = (root / "openfoia" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return match.group(1) if match else None


def changelog_section(text: str, version: str) -> str | None:
    """Return the body of ``## [version]`` up to the next ``## [`` heading."""
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.MULTILINE)
    match = heading.search(text)
    if not match:
        return None
    rest = text[match.end() :]
    following = re.search(r"^## \[", rest, re.MULTILINE)
    body = rest[: following.start()] if following else rest
    # Drop the trailing link-reference block if this is the last section.
    body = re.split(r"^\[[^\]]+\]: ", body, maxsplit=1, flags=re.MULTILINE)[0]
    return body.strip()


def check_release(root: Path, version: str) -> str:
    """Validate *version* against the tree at *root*; return the release notes."""
    problems: list[str] = []
    if not VERSION_RE.fullmatch(version):
        problems.append(f"'{version}' is not a plain X.Y.Z version")

    found = pyproject_version(root)
    if found != version:
        problems.append(f"pyproject.toml version is {found!r}, expected '{version}'")

    found = package_version(root)
    if found != version:
        problems.append(f"openfoia/__init__.py __version__ is {found!r}, expected '{version}'")

    notes = changelog_section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    if notes is None:
        problems.append(f"CHANGELOG.md has no '## [{version}]' section")
    elif not notes:
        problems.append(f"CHANGELOG.md '## [{version}]' section is empty")

    if problems:
        raise ReleaseError(problems)
    assert notes is not None
    return notes + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--branch", help="release branch name, e.g. release/4.3.0")
    source.add_argument("--version", help="version to check, e.g. 4.3.0")
    parser.add_argument("--notes-out", type=Path, help="write release notes here")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        version = version_from_branch(args.branch) if args.branch else args.version
        notes = check_release(args.root, version)
    except ReleaseError as exc:
        print("Release check failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    if args.notes_out:
        args.notes_out.write_text(notes, encoding="utf-8")
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
