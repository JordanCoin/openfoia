"""Where exports and downloads go, and how purge finds the ones it must remove.

Exports (graphs, FollowTheMoney JSON, reports, CSVs) and downloads embed
document text and the names under investigation, in plaintext. They used to
default to the current directory, where ``openfoia purge`` never looked.

Now every default lands under ``<data dir>/exports/`` (portable mode and
``OPENFOIA_DATA_DIR`` respected), which purge removes with the rest of the data
directory. When the user names an output path outside the data directory, it
is written to a manifest inside the data directory, and purge lists and
removes those files too. The manifest itself is a plaintext list of file
paths; it is removed by purge along with everything else.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MANIFEST_NAME = "exports_manifest.json"


def _data_dir() -> Path:
    from .db import get_data_dir

    return get_data_dir()


def exports_dir(*parts: str) -> Path:
    """``<data dir>/exports[/parts...]``, created owner-only."""
    from .db import _ensure_private_dir

    path = _ensure_private_dir(_data_dir() / "exports")
    for part in parts:
        path = _ensure_private_dir(path / part)
    return path


def default_export_path(filename: str) -> Path:
    """Where an export goes when the user did not name a path."""
    return exports_dir() / filename


def _manifest_path() -> Path:
    return _data_dir() / MANIFEST_NAME


def _is_inside_data_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(_data_dir().resolve())
    except ValueError:
        return False
    return True


def recorded_exports() -> list[Path]:
    """Files written outside the data directory, as recorded by :func:`record_export`."""
    try:
        data = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Path(p) for p in data.get("files", []) if isinstance(p, str)]


def record_export(path: Path | str) -> None:
    """Remember a file written outside the data directory so purge can remove it.

    Paths inside the data directory need no record: purge removes the whole
    directory. Never raises: failing to record must not lose the export.
    """
    path = Path(path)
    if str(path) == "-":
        return
    try:
        if _is_inside_data_dir(path):
            return
        resolved = str(path.resolve())
        files = [str(p) for p in recorded_exports()]
        if resolved in files:
            return
        files.append(resolved)
        manifest = _manifest_path()
        tmp = manifest.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"files": files}, f, indent=2)
        os.replace(tmp, manifest)
    except OSError:
        return


def resolve_output(output: Path | str | None, default_name: str) -> Path:
    """The path to write: *output* if given (recorded for purge), else the default."""
    if output is None:
        return default_export_path(default_name)
    path = Path(output)
    record_export(path)
    return path
