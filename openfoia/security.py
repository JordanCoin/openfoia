"""Forensically sound data destruction for OpenFOIA.

Provides multi-pass overwrite deletion, shell history scrubbing,
and free-space filling. Designed for situations where standard
rm/shutil.rmtree is insufficient.

WARNING: On SSDs with TRIM, the drive firmware may discard overwritten
blocks before the data is physically zeroed. These routines provide
best-effort overwrite but cannot guarantee unrecoverability on flash
storage. Full-disk encryption (FileVault / LUKS) is the only reliable
protection on modern SSDs.
"""

from __future__ import annotations

import os
import platform
import tempfile
from pathlib import Path

from rich import print as rprint


# ---------------------------------------------------------------------------
# Secure file deletion
# ---------------------------------------------------------------------------

OVERWRITE_PASSES = 3


def secure_delete(path: Path | str) -> None:
    """Overwrite a single file with random data (3 passes), then unlink.

    Each pass writes os.urandom bytes equal to the file size, followed
    by an explicit flush + fsync to push data to the storage layer.
    """
    path = Path(path)
    if not path.is_file():
        return

    size = path.stat().st_size
    if size == 0:
        path.unlink()
        return

    # Overwrite in place
    for _ in range(OVERWRITE_PASSES):
        with open(path, "r+b") as f:
            remaining = size
            while remaining > 0:
                chunk = min(remaining, 1024 * 1024)  # 1 MiB at a time
                f.write(os.urandom(chunk))
                remaining -= chunk
            f.flush()
            os.fsync(f.fileno())

    path.unlink()


def secure_delete_dir(path: Path | str) -> int:
    """Recursively secure-delete every file under *path*, then remove dirs.

    Returns the number of files securely deleted.
    """
    path = Path(path)
    if not path.exists():
        return 0

    count = 0
    # Files first (bottom-up so directories are empty when we reach them)
    for item in sorted(path.rglob("*"), reverse=True):
        if item.is_file() or item.is_symlink():
            if item.is_symlink():
                item.unlink()
            else:
                secure_delete(item)
            count += 1
        elif item.is_dir():
            try:
                item.rmdir()
            except OSError:
                pass  # non-empty dir — will retry after children removed

    # Remove the root directory itself
    try:
        path.rmdir()
    except OSError:
        pass

    return count


# ---------------------------------------------------------------------------
# Shell history scrubbing
# ---------------------------------------------------------------------------

_HISTORY_FILES = [
    ".bash_history",
    ".zsh_history",
    ".sh_history",
]


def clear_shell_history() -> list[str]:
    """Remove lines containing 'openfoia' from common shell history files.

    Returns a list of history files that were modified.
    """
    modified: list[str] = []
    home = Path.home()

    for name in _HISTORY_FILES:
        hist = home / name
        if not hist.is_file():
            continue

        try:
            lines = hist.read_text(errors="replace").splitlines(keepends=True)
            filtered = [ln for ln in lines if "openfoia" not in ln.lower()]
            if len(filtered) != len(lines):
                hist.write_text("".join(filtered))
                modified.append(str(hist))
        except OSError:
            continue

    return modified


# ---------------------------------------------------------------------------
# Free-space fill (slow, thorough)
# ---------------------------------------------------------------------------


def fill_free_space(path: Path | str, chunk_size_mb: int = 100) -> None:
    """Write random data to fill free disk space, then delete.

    Creates temporary files of *chunk_size_mb* MiB in the directory
    at *path* until the disk is full (ENOSPC), then removes them.

    This helps overwrite blocks that were freed by earlier deletions
    but may still contain recoverable data on spinning disks.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)

    chunk_bytes = chunk_size_mb * 1024 * 1024
    fill_files: list[Path] = []

    try:
        while True:
            fd, tmp = tempfile.mkstemp(dir=path, prefix=".openfoia_fill_")
            tmp_path = Path(tmp)
            fill_files.append(tmp_path)

            try:
                written = 0
                while written < chunk_bytes:
                    block = min(1024 * 1024, chunk_bytes - written)
                    data = os.urandom(block)
                    os.write(fd, data)
                    written += len(data)
                os.fsync(fd)
            except OSError:
                # Disk full — expected exit
                break
            finally:
                os.close(fd)
    except OSError:
        # Could not even create the file — disk full
        pass

    # Clean up fill files
    for fp in fill_files:
        try:
            fp.unlink()
        except OSError:
            pass

    # Try to remove the directory if we created it
    try:
        path.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# SSD warning
# ---------------------------------------------------------------------------

_SSD_WARNING = """
[bold yellow]SSD / Flash Storage Warning[/bold yellow]
[yellow]Modern SSDs use wear-leveling and TRIM. Overwritten data may not be
physically erased from the flash cells — the drive firmware decides when
to actually zero blocks. Secure overwrite is effective on spinning
(HDD) drives, but on SSDs the only reliable protection is full-disk
encryption (FileVault on macOS, LUKS on Linux).[/yellow]
"""


def print_ssd_warning() -> None:
    """Print a warning about SSD TRIM limitations."""
    rprint(_SSD_WARNING)
