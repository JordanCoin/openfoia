"""Tests for scripts/release.py, the release consistency check."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def _tree(tmp_path: Path, *, pyproject="4.3.0", package="4.3.0", changelog: str) -> Path:
    (tmp_path / "openfoia").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "openfoia"\nversion = "{pyproject}"\n'
    )
    (tmp_path / "openfoia" / "__init__.py").write_text(f'"""x"""\n\n__version__ = "{package}"\n')
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


CHANGELOG = """# Changelog

## [4.3.0] - 2026-10-01

### Added

- A thing.

## [4.2.0] - 2026-09-26

- Older thing.

[4.3.0]: https://example.invalid/compare/v4.2.0...v4.3.0
"""


@pytest.mark.parametrize(
    "branch",
    ["release/4.3.0", "release/v4.3.0", "release/release-4.3.0", "refs/heads/release/4.3.0"],
)
def test_accepted_branch_names(branch):
    assert release.version_from_branch(branch) == "4.3.0"


@pytest.mark.parametrize(
    "branch",
    [
        "release/release-4.54",  # two components: ambiguous, not X.Y.Z
        "release/4.3",
        "release/4.3.0-rc.1",
        "release/4.3.0; rm -rf /",
        "feature/4.3.0",
        "release/",
        "main",
    ],
)
def test_rejected_branch_names(branch):
    with pytest.raises(release.ReleaseError):
        release.version_from_branch(branch)


def test_consistent_release_returns_only_its_own_changelog_section(tmp_path):
    notes = release.check_release(_tree(tmp_path, changelog=CHANGELOG), "4.3.0")

    assert notes == "### Added\n\n- A thing.\n"


def test_last_section_excludes_link_reference_block(tmp_path):
    root = _tree(tmp_path, pyproject="4.2.0", package="4.2.0", changelog=CHANGELOG)

    assert release.check_release(root, "4.2.0") == "- Older thing.\n"


def test_every_mismatch_is_reported_at_once(tmp_path):
    root = _tree(tmp_path, pyproject="4.2.0", package="4.1.0", changelog="# Changelog\n")

    with pytest.raises(release.ReleaseError) as error:
        release.check_release(root, "4.3.0")

    problems = "\n".join(error.value.problems)
    assert "pyproject.toml version is '4.2.0'" in problems
    assert "__version__ is '4.1.0'" in problems
    assert "no '## [4.3.0]' section" in problems


def test_empty_changelog_section_is_rejected(tmp_path):
    changelog = "# Changelog\n\n## [4.3.0] - 2026-10-01\n\n## [4.2.0]\n\n- Older.\n"

    with pytest.raises(release.ReleaseError, match="section is empty"):
        release.check_release(_tree(tmp_path, changelog=changelog), "4.3.0")


def test_cli_writes_notes_and_prints_version(tmp_path, capsys):
    root = _tree(tmp_path, changelog=CHANGELOG)
    notes = tmp_path / "notes.md"

    code = release.main(
        ["--branch", "release/4.3.0", "--notes-out", str(notes), "--root", str(root)]
    )

    assert code == 0
    assert capsys.readouterr().out.strip() == "4.3.0"
    assert notes.read_text() == "### Added\n\n- A thing.\n"


def test_cli_failure_lists_problems_on_stderr(tmp_path, capsys):
    root = _tree(tmp_path, pyproject="4.2.0", changelog=CHANGELOG)

    code = release.main(["--version", "4.3.0", "--root", str(root)])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "pyproject.toml version is '4.2.0'" in captured.err


def test_repository_version_is_consistent_and_released_in_changelog():
    """pyproject.toml, __version__ and CHANGELOG.md must agree on every commit.

    This is the same check the release workflow runs, so drift between the
    two version strings is caught on any PR, not only on a release branch.
    """
    version = release.pyproject_version(ROOT)

    assert release.check_release(ROOT, version)
