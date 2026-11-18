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

import calendar
import contextlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from rich import print as rprint

from .models import utcnow as _utcnow

# ---------------------------------------------------------------------------
# Secure file deletion
# ---------------------------------------------------------------------------

OVERWRITE_PASSES = 3


def secure_delete(path: Path | str) -> None:
    """Overwrite a single file with random data (3 passes), then unlink.

    Each pass writes os.urandom bytes equal to the file size, followed
    by an explicit flush + fsync to push data to the storage layer.

    A read-only file (an ingested copy of a ``chmod 444`` original, say) is
    made owner-writable first: purge is the owner destroying their own data,
    and refusing to overwrite would leave the plaintext behind. A symlink is
    removed, never followed. Raises OSError if the file cannot be overwritten
    or removed; :func:`secure_delete_dir` turns that into a per-file report.
    """
    path = Path(path)
    if path.is_symlink():
        path.unlink()
        return
    if not path.is_file():
        return

    st = path.stat()
    if not st.st_mode & stat.S_IWUSR:
        os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IWUSR)

    size = st.st_size
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


@dataclass
class ShredReport:
    """What :func:`secure_delete_dir` did to each file.

    ``unlinked_only`` files are gone from the directory but their contents
    were NOT overwritten, so they may be recoverable from the disk.
    ``survived`` files are still there.
    """

    shredded: list[str] = field(default_factory=list)
    unlinked_only: list[tuple[str, str]] = field(default_factory=list)
    survived: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failures(self) -> list[tuple[str, str]]:
        """Every file that was not overwritten: (path, reason)."""
        return [*self.unlinked_only, *self.survived]

    @property
    def ok(self) -> bool:
        return not self.unlinked_only and not self.survived

    def merge(self, other: ShredReport) -> None:
        self.shredded.extend(other.shredded)
        self.unlinked_only.extend(other.unlinked_only)
        self.survived.extend(other.survived)


def _reason(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def _shred_into(path: Path, report: ShredReport) -> None:
    """Shred one file; on failure fall back to unlink, and record what happened."""
    try:
        secure_delete(path)
    except OSError as exc:
        why = _reason(exc)
        try:
            path.unlink()
        except FileNotFoundError:
            report.unlinked_only.append((str(path), why))
        except OSError as exc2:
            report.survived.append((str(path), f"{why}; could not remove: {_reason(exc2)}"))
        else:
            report.unlinked_only.append((str(path), why))
    else:
        report.shredded.append(str(path))


def secure_delete_file(path: Path | str) -> ShredReport:
    """:func:`secure_delete` for one file, reporting instead of raising."""
    report = ShredReport()
    path = Path(path)
    if path.exists() or path.is_symlink():
        _shred_into(path, report)
    return report


def _make_traversable(directory: Path) -> None:
    """Give the owner rwx on *directory* so its entries can be listed and removed."""
    with contextlib.suppress(OSError):
        if directory.is_symlink() or not directory.is_dir():
            return
        mode = stat.S_IMODE(directory.stat().st_mode)
        if mode & stat.S_IRWXU != stat.S_IRWXU:
            os.chmod(directory, mode | stat.S_IRWXU)


def secure_delete_dir(path: Path | str) -> ShredReport:
    """Recursively secure-delete every file under *path*, then remove dirs.

    One file that cannot be overwritten (wrong owner, read-only medium, I/O
    error) never stops the rest: it is unlinked without overwriting if
    possible, and every such file is listed in the returned report, so the
    caller can tell the user exactly what survived.
    """
    path = Path(path)
    report = ShredReport()
    if not path.exists() and not path.is_symlink():
        return report
    if path.is_symlink() or not path.is_dir():
        _shred_into(path, report)
        return report

    errors: list[OSError] = []
    _make_traversable(path)
    for root, dirs, _files in os.walk(path, onerror=errors.append):
        for d in dirs:
            _make_traversable(Path(root) / d)

    # Files first, bottom-up, so directories are empty when we reach them.
    for root, dirs, files in os.walk(path, topdown=False, onerror=errors.append):
        for name in sorted(files, reverse=True):
            _shred_into(Path(root) / name, report)
        for name in dirs:
            sub = Path(root) / name
            if sub.is_symlink():
                _shred_into(sub, report)  # unlinks the link, never its target
            else:
                with contextlib.suppress(OSError):
                    sub.rmdir()

    with contextlib.suppress(OSError):
        path.rmdir()

    if path.exists():
        recorded = {p for p, _ in report.survived}
        for root, _dirs, files in os.walk(path):
            for name in files:
                p = str(Path(root) / name)
                if p not in recorded:
                    report.survived.append((p, "still present after purge"))
                    recorded.add(p)
        for err in errors:
            if err.filename and Path(err.filename).exists() and err.filename not in recorded:
                report.survived.append((str(err.filename), f"could not list: {_reason(err)}"))

    return report


@contextlib.contextmanager
def shredding_tempdir(parent: Path | str) -> Iterator[Path]:
    """A temporary directory under *parent* whose files are shredded on exit.

    ``tempfile.TemporaryDirectory`` removes its files with a plain unlink,
    which leaves e.g. rasterized page images of a sensitive PDF recoverable
    from the freed blocks. Callers must be done reading the files (images
    fully loaded, not lazily backed by the file) before the block exits.
    """
    scratch = Path(tempfile.mkdtemp(dir=parent))
    try:
        yield scratch
    finally:
        secure_delete_dir(scratch)


# ---------------------------------------------------------------------------
# Shell history scrubbing
# ---------------------------------------------------------------------------

_HISTORY_FILES = [
    ".bash_history",
    ".zsh_history",
    ".sh_history",
    ".ksh_history",
    ".mksh_history",
    ".history",
]

#: A history line that ran OpenFOIA: the name anywhere (``openfoia ...``,
#: ``python -m openfoia``, ``OPENFOIA_DB_PASSWORD=...``) or the ``run.sh``
#: wrapper docs/USB.md tells portable users to create.
_OPENFOIA_LINE = re.compile(r"openfoia|(?:^|[\s/;&|(`'\"=])run\.sh\b", re.IGNORECASE)


def _history_files(home: Path) -> list[Path]:
    files = [home / name for name in _HISTORY_FILES]
    xdg = os.environ.get("XDG_DATA_HOME")
    files.append(home / ".local" / "share" / "fish" / "fish_history")
    if xdg:
        files.append(Path(xdg) / "fish" / "fish_history")
    histfile = os.environ.get("HISTFILE")
    if histfile:
        files.append(Path(histfile).expanduser())
    seen: set[Path] = set()
    unique = []
    for f in files:
        key = f.resolve() if f.exists() else f
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def _filter_line_history(lines: list[str]) -> list[str]:
    """bash/zsh/sh: drop matching lines and their backslash continuations."""
    kept: list[str] = []
    dropping = False
    for ln in lines:
        body = ln.rstrip("\r\n")
        if dropping or _OPENFOIA_LINE.search(body):
            dropping = body.endswith("\\")
            continue
        kept.append(ln)
    return kept


def _filter_fish_history(lines: list[str]) -> list[str]:
    """fish: drop whole entries (``- cmd: ...`` plus its indented lines)."""
    kept: list[str] = []
    dropping = False
    for ln in lines:
        if ln.startswith("- cmd:"):
            dropping = bool(_OPENFOIA_LINE.search(ln))
        elif not ln.startswith((" ", "\t")):
            dropping = False
        if not dropping:
            kept.append(ln)
    return kept


def _rewrite_in_place(path: Path, text: str) -> None:
    """Replace *path*'s contents, overwriting the old bytes rather than freeing them."""
    data = text.encode("utf-8", errors="surrogateescape")
    old_size = path.stat().st_size
    with open(path, "r+b") as f:
        f.write(data)
        tail = old_size - len(data)
        if tail > 0:
            f.write(os.urandom(tail))
        f.flush()
        os.fsync(f.fileno())
        f.truncate(len(data))
        f.flush()
        os.fsync(f.fileno())


def clear_shell_history() -> list[str]:
    """Remove OpenFOIA commands from shell history files.

    Covers bash, zsh, sh/ksh, fish (``~/.local/share/fish/fish_history``) and
    whatever ``$HISTFILE`` points at. A line is removed when it mentions
    ``openfoia`` or runs the ``run.sh`` wrapper from docs/USB.md.

    It cannot reach the history of the shell this command runs in, which is
    held in memory and written back when that shell exits; see
    :data:`LIVE_SHELL_HISTORY_NOTE`. Returns the history files that were
    modified.
    """
    modified: list[str] = []

    for hist in _history_files(Path.home()):
        if not hist.is_file():
            continue

        try:
            raw = hist.read_bytes().decode("utf-8", errors="surrogateescape")
            lines = raw.splitlines(keepends=True)
            if hist.name == "fish_history":
                filtered = _filter_fish_history(lines)
            else:
                filtered = _filter_line_history(lines)
            if len(filtered) != len(lines):
                _rewrite_in_place(hist, "".join(filtered))
                modified.append(str(hist))
        except OSError:
            continue

    return modified


#: What purge tells the user about the shell it is running in.
LIVE_SHELL_HISTORY_NOTE = (
    "Your current shell still holds this session's commands in memory and "
    "writes them back to its history file when it exits (some setups also "
    "append after every command). Before closing this terminal run "
    "`unset HISTFILE` (bash, zsh), or close it with `kill -9 $$`. "
    "fish saves each command as it runs; start it as `fish --private` next time."
)


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
        with contextlib.suppress(OSError):
            fp.unlink()

    # Try to remove the directory if we created it
    with contextlib.suppress(OSError):
        path.rmdir()


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


# ---------------------------------------------------------------------------
# Duress mode — decoy profile
# ---------------------------------------------------------------------------
#
# IMPORTANT: This is a "decoy profile" feature, NOT true plausible
# deniability. A forensic examiner can determine that two encrypted
# databases exist. For actual coercion resistance, use a VeraCrypt
# hidden volume. See docs/THREAT_MODEL.md.
#
# Design (per codex review):
# - No password hash stored anywhere — SQLCipher itself is the verifier
# - Both real and decoy DBs are encrypted (no plaintext decoy)
# - Opaque filenames (profile_0.db, profile_1.db) — not "decoy.db"
# - Try each profile with the entered password; whichever opens is used
# ---------------------------------------------------------------------------

# Profile slot filenames — intentionally opaque
_PROFILE_SLOTS = ["profile_0.db", "profile_1.db"]


def _has_sqlcipher() -> bool:
    """Return True if SQLCipher support is importable."""
    from .db import has_sqlcipher

    return has_sqlcipher()


def get_profile_paths() -> list[Path]:
    """Return paths for both profile slots."""
    from .db import get_data_dir

    data_dir = get_data_dir()
    return [data_dir / name for name in _PROFILE_SLOTS]


def real_profile_path() -> Path:
    """Path of the slot holding the real database."""
    return get_profile_paths()[0]


def migrate_db_to_profile_slot() -> Path:
    """Move a legacy ``data.db`` (and its sidecars) into the real profile slot.

    The sidecars must travel with it. A ``data.db-wal`` left behind holds
    recent writes in plaintext *and* re-labels the layout — an examiner seeing
    it next to profile_0/profile_1 learns which slot is real, which is exactly
    what moving into an opaque slot was meant to prevent.

    Idempotent, and never clobbers an existing slot.
    """
    from .db import _DB_SIDECAR_SUFFIXES, get_data_dir

    data_dir = get_data_dir()
    legacy = data_dir / "data.db"
    real = real_profile_path()

    if real.exists():
        # Already migrated. Remove any stale legacy leftovers so nothing in
        # the directory listing points at which slot is real.
        for suffix in ("", *_DB_SIDECAR_SUFFIXES):
            stale = Path(str(legacy) + suffix)
            if stale.is_file():
                secure_delete_file(stale)
        return real

    if not legacy.exists():
        return real

    shutil.move(str(legacy), str(real))
    for suffix in _DB_SIDECAR_SUFFIXES:
        sidecar = Path(str(legacy) + suffix)
        if sidecar.is_file():
            shutil.move(str(sidecar), str(real) + suffix)

    return real


def duress_mode_active() -> bool:
    """True once the real database has been migrated into a profile slot."""
    return real_profile_path().exists()


#: First 16 bytes of every plaintext SQLite database. A SQLCipher file starts
#: with a random salt instead.
SQLITE_PLAINTEXT_HEADER = b"SQLite format 3\x00"


class DuressSetupError(RuntimeError):
    """Duress mode was NOT configured; the message says why and what to do."""


def is_encrypted_db(path: Path | str) -> bool:
    """True when *path* holds a database that is not readable as plain SQLite.

    An empty or missing file is not encrypted: SQLite would happily create a
    plaintext database in it on the next write.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(len(SQLITE_PLAINTEXT_HEADER))
    except OSError:
        return False
    return len(head) == len(SQLITE_PLAINTEXT_HEADER) and head != SQLITE_PLAINTEXT_HEADER


def _current_real_db() -> Path | None:
    """The real database as it is on disk now: slot 0, else the legacy data.db."""
    from .db import get_data_dir

    for candidate in (real_profile_path(), get_data_dir() / "data.db"):
        if candidate.is_file():
            return candidate
    return None


def _shred_db_files(db_path: Path) -> None:
    from .db import _DB_SIDECAR_SUFFIXES

    for suffix in ("", *_DB_SIDECAR_SUFFIXES):
        secure_delete_file(Path(str(db_path) + suffix))


def setup_duress_mode(duress_password: str) -> Path:
    """Create and seed a decoy profile encrypted with the duress password.

    Returns the path to the decoy database. No password hash is stored
    anywhere — the password is verified by attempting to open the DB.

    Refuses (``DuressSetupError``) unless the real database already exists
    and is encrypted: an encrypted decoy next to a plaintext real database
    points an examiner straight at the readable, real investigation. Success
    is only returned after both slots have been checked on disk.

    The real database is moved into slot 0 at the same time. Leaving it as
    ``data.db`` next to a file named ``profile_1.db`` would tell any examiner
    both that duress mode is configured and which file is the decoy — the
    opposite of the "opaque filenames" the design promises.
    """
    from .db import _restrict_db_permissions, get_data_dir

    if not _has_sqlcipher():
        # A plaintext decoy contradicts the guarantee. Fail closed.
        raise RuntimeError(
            "Duress mode requires database encryption, but no SQLCipher driver is "
            "installed. A plaintext decoy would provide no protection. "
            "Install encryption support: openfoia install-extras encryption"
        )
    if not duress_password:
        raise DuressSetupError("Duress mode needs a non-empty duress passphrase.")

    real = _current_real_db()
    if real is None:
        raise DuressSetupError(
            "There is no database to protect yet. Create an encrypted one with the "
            "decoy in a single step: openfoia init --encrypt --duress"
        )
    if not is_encrypted_db(real):
        raise DuressSetupError(
            f"The real database ({real.name}) is not encrypted. A decoy next to a "
            "readable real database would point straight at it. Encrypt it first "
            "(openfoia db encrypt), then run: openfoia init --duress"
        )
    if _can_open_db(real, duress_password):
        raise DuressSetupError(
            "The duress passphrase opens the real database. Choose a different one."
        )

    # Migrate the real database (with its sidecars) into slot 0 so both slots
    # look alike on disk.
    real = migrate_db_to_profile_slot()

    # Use the second slot for the decoy
    decoy_path = get_data_dir() / _PROFILE_SLOTS[1]

    # Build it exactly like a real database, under the decoy key.
    seed_decoy_db(decoy_path, password=duress_password)
    _restrict_db_permissions(real)

    # Verify on disk before anyone is told duress mode is configured.
    problems = []
    if not is_encrypted_db(real):
        problems.append(f"{real.name} is not encrypted")
    if not is_encrypted_db(decoy_path):
        problems.append(f"{decoy_path.name} is not encrypted")
    elif not _can_open_db(decoy_path, duress_password):
        problems.append(f"{decoy_path.name} does not open with the duress passphrase")
    if problems:
        _shred_db_files(decoy_path)
        raise DuressSetupError("; ".join(problems) + ".")

    return decoy_path


def resolve_db_with_password(password: str | None = None) -> tuple[Path, bool]:
    """Determine which database to open based on the password.

    Tries to open each profile slot with the given password. The first
    one that opens successfully is used. Returns (path, is_decoy).

    This is the single entry point that all commands should use.
    SQLCipher itself acts as the password verifier — no hash stored.
    """
    from .db import get_db_path

    if not password:
        return get_db_path(), False

    # Try each profile slot — whichever opens with this password wins
    profiles = get_profile_paths()
    for i, profile_path in enumerate(profiles):
        if not profile_path.exists():
            continue
        if _can_open_db(profile_path, password):
            return profile_path, (i == 1)  # slot 1 = decoy

    # No profile matched — fall back to default (real) DB
    return get_db_path(), False


def _can_open_db(db_path: Path, password: str) -> bool:
    """Try to open an encrypted SQLite database with the given password.

    Returns True if the password works (SQLCipher can read the schema).
    """
    # Encryption support missing entirely is a configuration error, not a
    # wrong password. Swallowing it would make the duress password silently
    # never match, so the decoy would never open under coercion.
    from .db import get_sqlcipher_driver, sqlcipher_key_pragma

    sqlcipher = get_sqlcipher_driver()

    try:
        conn = sqlcipher.connect(str(db_path))
        # A wrong key is the expected outcome for one of the slots; keep
        # SQLCipher from logging "hmac check failed" to the terminal.
        with contextlib.suppress(Exception):
            conn.execute("PRAGMA cipher_log_level = NONE")
        conn.execute(sqlcipher_key_pragma(password))
        conn.execute("PRAGMA cipher_compatibility = 4")
        conn.execute("SELECT count(*) FROM sqlite_master")
        conn.close()
        return True
    except Exception:
        return False


# Keep backward compat aliases
def is_duress_password(password: str) -> bool:
    """Check if this password opens the decoy profile."""
    profiles = get_profile_paths()
    if len(profiles) > 1 and profiles[1].exists():
        return _can_open_db(profiles[1], password)
    return False


def get_decoy_db_path() -> Path:
    """Return the path to the decoy profile (slot 1)."""
    from .db import get_data_dir

    return get_data_dir() / _PROFILE_SLOTS[1]


def resolve_db_path(password: str | None = None) -> Path:
    """Return the appropriate database path based on the password."""
    path, _ = resolve_db_with_password(password)
    return path


#: Bland request topics for the decoy: (agency abbreviation, subject, body).
#: The generator is public source code, so this content can never by itself
#: prove a profile is a decoy or not; see docs/THREAT_MODEL.md.
_DECOY_TOPICS = [
    (
        "NOAA",
        "Monthly climate summaries for {city} ({year})",
        "I am requesting copies of the monthly climate summary reports for the {city} "
        "area for calendar year {year}, in CSV or PDF format.",
    ),
    (
        "DOI",
        "Visitor statistics for {park} ({year})",
        "I request monthly visitor counts for {park} for {year}, and any summary "
        "reports on parking and trail use prepared that year.",
    ),
    (
        "GSA",
        "Public meeting minutes, GSA regional office, Q{quarter} {year}",
        "Under the Freedom of Information Act, I request copies of the recorded "
        "minutes of public meetings held by the GSA regional office serving {city} "
        "during Q{quarter} {year}.",
    ),
    (
        "DOT",
        "Traffic count data for {road} near {city} ({year})",
        "I request annual average daily traffic counts and any published traffic "
        "studies for {road} near {city} for {year}.",
    ),
    (
        "USDA",
        "Farmers market promotion grants awarded in {state} ({year})",
        "I request a list of Farmers Market Promotion Program grants awarded to "
        "recipients in {state} in fiscal year {year}, with award amounts.",
    ),
    (
        "EPA",
        "Air quality monitoring summaries for {city} ({year})",
        "I request the annual air quality monitoring summaries (PM2.5 and ozone) "
        "for monitoring stations in the {city} area for {year}.",
    ),
    (
        "FAA",
        "Noise complaint statistics for {airport} ({year})",
        "I request monthly totals of noise complaints received about operations at "
        "{airport} during {year}.",
    ),
    (
        "USPS",
        "Renovation schedule for the main post office in {city}",
        "I request any public notices or schedules for planned renovation work on "
        "the main post office building in {city} during {year}.",
    ),
    (
        "NASA",
        "Public outreach event calendar ({year})",
        "I request the calendar of public outreach and education events NASA held "
        "or sponsored in {state} during {year}.",
    ),
    (
        "NARA",
        "Finding aids for {city} federal building records",
        "I request copies of existing finding aids describing records about the "
        "construction of federal buildings in {city}.",
    ),
    (
        "ED",
        "Library services grants to public libraries in {state} ({year})",
        "I request a list of federal grants for library services awarded to public "
        "libraries in {state} in {year}.",
    ),
    (
        "SSA",
        "Field office wait-time statistics for {state} ({year})",
        "I request average visitor wait-time statistics for Social Security field "
        "offices in {state} for {year}.",
    ),
]

_DECOY_PLACES = [
    ("Portland", "Oregon", "Crater Lake National Park", "Interstate 5", "PDX"),
    ("Denver", "Colorado", "Rocky Mountain National Park", "Interstate 25", "DEN"),
    ("Asheville", "North Carolina", "Great Smoky Mountains", "Interstate 40", "AVL"),
    ("Tucson", "Arizona", "Saguaro National Park", "Interstate 10", "TUS"),
    ("Burlington", "Vermont", "Marsh-Billings-Rockefeller Park", "Interstate 89", "BTV"),
    ("Madison", "Wisconsin", "Apostle Islands", "US Route 151", "MSN"),
    ("Boise", "Idaho", "Craters of the Moon", "Interstate 84", "BOI"),
    ("Albuquerque", "New Mexico", "Petroglyph National Monument", "Interstate 25", "ABQ"),
    ("Sacramento", "California", "Lassen Volcanic National Park", "Interstate 80", "SMF"),
    ("Duluth", "Minnesota", "Voyageurs National Park", "Interstate 35", "DLH"),
]

_DECOY_FIRST = ["Alex", "Jordan", "Sam", "Pat", "Casey", "Morgan", "Taylor", "Jamie", "Robin"]
_DECOY_FIRST += ["Chris", "Dana", "Lee", "Kim", "Avery", "Riley", "Drew", "Jesse", "Quinn"]
_DECOY_LAST = ["Chen", "Park", "Davis", "Wilson", "Thomas", "Garcia", "Lee", "Brown", "Nguyen"]
_DECOY_LAST += ["Miller", "Lopez", "Clark", "Walker", "Young", "Hall", "Allen", "Wright", "King"]
_DECOY_DOMAINS = ["gmail.com", "outlook.com", "proton.me", "yahoo.com", "fastmail.com"]


def _decoy_response_text(rng, topic_subject: str, agency_name: str, city: str, year: int) -> str:
    months = ["January", "February", "March", "April", "May", "June"]
    months += ["July", "August", "September", "October", "November", "December"]
    lines = [agency_name, f"Response: {topic_subject}", ""]
    for month in rng.sample(months, rng.randint(3, 6)):
        lines.append(f"{month} {year}: {rng.randint(40, 9800):,} ({city} area)")
    lines += ["", "This information is released in full. No fees were charged."]
    return "\n".join(lines)


def seed_decoy_db(db_path: Path, password: str | None = None) -> None:
    """Build a decoy profile: a real database with innocent-looking FOIA data.

    The schema comes from the same path as ``openfoia init`` (alembic
    migrations, the full agency seed), under the duress key, so the decoy has
    the same tables, migration version and agencies as a real database. The
    requests, documents and entities on top are drawn at random from bland
    topics and spread over the past months, and each document gets a stored
    file under ``docs/`` like an ingested one. The file is made owner-only
    (0600) like the real profile.

    *password* is required: a plaintext decoy offers no protection.
    """
    import random
    from datetime import timedelta
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from .db import _restrict_db_permissions, get_engine, init_db
    from .doctext import replace_pages
    from .models import (
        Agency,
        DeliveryMethod,
        Document,
        DocumentType,
        Entity,
        EntityType,
        Request,
        RequestStatus,
        TimelineEvent,
        User,
    )
    from .pipeline.ingest import private_dest_dir

    if not password:
        raise ValueError("The decoy profile must be encrypted: pass the duress password.")
    try:
        from .db import get_sqlcipher_driver

        get_sqlcipher_driver()
    except RuntimeError as exc:
        # Fail closed. Falling back to plain SQLite here would write an
        # UNENCRYPTED decoy database while telling the user duress mode
        # is configured — the opposite of the promised guarantee.
        raise RuntimeError(
            "Cannot create the decoy profile: no SQLCipher driver is installed. "
            "A plaintext decoy would contradict the duress-mode guarantee. "
            "Install encryption support: openfoia install-extras encryption"
        ) from exc

    db_path = Path(db_path)
    # Remove an existing decoy (and its sidecars) if present
    _shred_db_files(db_path)

    init_db(seed=True, password=password, db_path=db_path)

    rng = random.SystemRandom()
    now = _utcnow()
    engine = get_engine(db_path=db_path, password=password)
    session = sessionmaker(bind=engine)()
    docs_dir = db_path.parent / "docs"
    try:
        agencies = {a.abbreviation: a for a in session.query(Agency).all()}

        first, last = rng.choice(_DECOY_FIRST), rng.choice(_DECOY_LAST)
        span_days = rng.randint(90, 540)
        user = User(
            id=str(uuid4()),
            email=f"{first.lower()}.{last.lower()}{rng.choice(['', rng.randint(1, 99)])}"
            f"@{rng.choice(_DECOY_DOMAINS)}",
            name=f"{first} {last}",
            organization=rng.choice([None, None, "Neighborhood association", "Local history club"]),
            is_journalist=False,
            created_at=now - timedelta(days=span_days, minutes=rng.randint(0, 1440)),
        )
        session.add(user)
        session.flush()

        topics = [t for t in _DECOY_TOPICS if t[0] in agencies]
        chosen = rng.sample(topics, min(len(topics), rng.randint(3, 8)))
        ages = sorted((rng.uniform(1, span_days - 1) for _ in chosen), reverse=True)

        for (abbr, subject_t, body_t), age in zip(chosen, ages, strict=True):
            city, state, park, road, airport = rng.choice(_DECOY_PLACES)
            created = now - timedelta(days=age)
            year = created.year - rng.choice([0, 1, 1, 2])
            fields = {
                "city": city,
                "state": state,
                "park": park,
                "road": road,
                "airport": f"{city} airport ({airport})",
                "year": year,
                "quarter": rng.randint(1, 4),
            }
            agency = agencies[abbr]
            req = Request(
                id=str(uuid4()),
                request_number=f"REQ-{created.strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}",
                requester_id=user.id,
                agency_id=agency.id,
                subject=subject_t.format(**fields),
                body=body_t.format(**fields),
                delivery_method=agency.preferred_method or DeliveryMethod.EMAIL,
                status=RequestStatus.DRAFT,
                fee_waiver_requested=rng.random() < 0.7,
                created_at=created,
            )
            session.add(req)
            events: list[tuple[str, object]] = []
            if age > 1.5 or rng.random() < 0.5:
                req.sent_at = created + timedelta(hours=rng.uniform(0.2, 30))
                req.status = RequestStatus.SENT
                elapsed = (now - req.sent_at).days
                if elapsed > 6 and rng.random() < 0.8:
                    req.acknowledged_at = req.sent_at + timedelta(days=rng.uniform(1, 6))
                    req.status = RequestStatus.ACKNOWLEDGED
                    events.append(("acknowledged", req.acknowledged_at))
                if elapsed > 25 and rng.random() < 0.85:
                    received = req.sent_at + timedelta(days=rng.uniform(18, min(elapsed, 90)))
                    req.completed_at = received
                    req.status = RequestStatus.COMPLETE
                    events.append(("received", received))
            session.flush()
            for event, when in events:
                session.add(
                    TimelineEvent(
                        request_id=req.id,
                        event_type=event,
                        description=event,
                        occurred_at=when,
                        extra_data={"jurisdiction": agency.jurisdiction_id or "us"},
                    )
                )
            try:
                from .clocks import request_clocks

                req.due_date = request_clocks(req).response_due
            except Exception:  # a missing clock is not a decoy failure
                req.due_date = None

            if req.status != RequestStatus.COMPLETE:
                continue

            text = _decoy_response_text(rng, req.subject, agency.name, city, year)
            doc_id = str(uuid4())
            dest_dir = private_dest_dir(docs_dir, doc_id)
            dest = dest_dir / f"{doc_id}.txt"
            dest.write_text(text, encoding="utf-8")
            os.chmod(dest, 0o600)
            stamp = calendar.timegm(req.completed_at.timetuple())
            with contextlib.suppress(OSError):
                os.utime(dest, (stamp, stamp))
            doc = Document(
                id=doc_id,
                request_id=req.id,
                doc_type=DocumentType.FULL_RESPONSE,
                filename=f"{abbr.lower()}_response_{req.request_number[-6:].lower()}.txt",
                file_path=str(dest),
                file_size=dest.stat().st_size,
                mime_type="text/plain",
                extracted_text=text,
                ocr_completed=True,
                received_at=req.completed_at,
            )
            session.add(doc)
            session.flush()
            replace_pages(session, doc_id, [text])
            for etype, raw in (
                (EntityType.ORGANIZATION, agency.name),
                (EntityType.LOCATION, city),
                (EntityType.DATE, str(year)),
            ):
                session.add(
                    Entity(
                        id=str(uuid4()),
                        document_id=doc_id,
                        entity_type=etype,
                        raw_text=raw,
                        normalized_text=raw.lower(),
                        context=text[:200],
                    )
                )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    _restrict_db_permissions(db_path)
