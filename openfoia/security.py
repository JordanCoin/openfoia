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


def get_profile_paths() -> list[Path]:
    """Return paths for both profile slots."""
    from .db import get_data_dir

    data_dir = get_data_dir()
    return [data_dir / name for name in _PROFILE_SLOTS]


def setup_duress_mode(duress_password: str) -> Path:
    """Create and seed a decoy profile encrypted with the duress password.

    Returns the path to the decoy database. No password hash is stored
    anywhere — the password is verified by attempting to open the DB.
    """
    from .db import get_data_dir

    # Use the second slot for the decoy
    decoy_path = get_data_dir() / _PROFILE_SLOTS[1]

    # Create and seed with encryption
    seed_decoy_db(decoy_path, password=duress_password)

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
    try:
        import pysqlcipher3.dbapi2 as sqlcipher
    except ImportError as exc:
        # Encryption support missing entirely — a configuration error, not a
        # wrong password. Swallowing this would make the duress password
        # silently never match, so the decoy would never open under coercion.
        raise RuntimeError(
            "Cannot verify the database password: pysqlcipher3 is not installed. "
            "Install encryption support: openfoia install-extras encryption"
        ) from exc

    from .db import sqlcipher_key_pragma

    try:
        conn = sqlcipher.connect(str(db_path))
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


def seed_decoy_db(db_path: Path, password: str | None = None) -> None:
    """Populate a decoy database with innocent-looking FOIA data.

    The decoy contains a handful of bland requests (weather data, public
    meeting minutes, park usage statistics) — nothing that would raise
    suspicion if examined.  Entities and documents are similarly mundane.

    If password is provided, the decoy is encrypted with SQLCipher.
    """
    import random
    from datetime import datetime, timedelta
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from .models import (
        Agency,
        AgencyLevel,
        Base,
        DeliveryMethod,
        Document,
        DocumentType,
        Entity,
        EntityType,
        Request,
        RequestStatus,
        User,
    )

    # Remove existing decoy if present
    if db_path.exists():
        db_path.unlink()

    if password:
        try:
            import pysqlcipher3.dbapi2 as sqlcipher

            from .db import sqlcipher_key_pragma

            def _creator():
                conn = sqlcipher.connect(str(db_path))
                conn.execute(sqlcipher_key_pragma(password))
                conn.execute("PRAGMA cipher_compatibility = 4")
                conn.execute("PRAGMA cipher_memory_security = ON")
                return conn

            engine = create_engine("sqlite+pysqlite:///", creator=_creator, echo=False)
        except ImportError as exc:
            # Fail closed. Falling back to plain SQLite here would write an
            # UNENCRYPTED decoy database while telling the user duress mode
            # is configured — the opposite of the promised guarantee.
            raise RuntimeError(
                "Cannot create the decoy profile: pysqlcipher3 is not installed. "
                "A plaintext decoy would contradict the duress-mode guarantee. "
                "Install encryption support: openfoia install-extras encryption"
            ) from exc
    else:
        engine = create_engine(f"sqlite:///{db_path}", echo=False)

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # --- Users (randomized to avoid fingerprinting) ---
    first_names = ["Alex", "Jordan", "Sam", "Pat", "Casey", "Morgan", "Taylor", "Jamie"]
    last_names = ["Chen", "Park", "Davis", "Wilson", "Thomas", "Garcia", "Lee", "Brown"]
    orgs = [
        "Local Civic Association",
        "Community Research Group",
        "Neighborhood Watch",
        "Public Data Project",
    ]
    decoy_first = random.choice(first_names)
    decoy_last = random.choice(last_names)
    decoy_name = f"{decoy_first} {decoy_last}"
    user = User(
        id=str(uuid4()),
        email=f"{decoy_first.lower()}.{decoy_last.lower()}@gmail.com",
        name=decoy_name,
        organization=random.choice(orgs),
        is_journalist=False,
    )
    session.add(user)
    session.flush()

    # --- Agencies (a small subset of common, benign agencies) ---
    agencies = [
        Agency(
            id=str(uuid4()),
            name="National Oceanic and Atmospheric Administration",
            abbreviation="NOAA",
            level=AgencyLevel.FEDERAL,
            foia_email="foia@noaa.gov",
            preferred_method=DeliveryMethod.EMAIL,
            typical_response_days=20,
        ),
        Agency(
            id=str(uuid4()),
            name="National Park Service",
            abbreviation="NPS",
            level=AgencyLevel.FEDERAL,
            foia_email="npsfoia@nps.gov",
            preferred_method=DeliveryMethod.EMAIL,
            typical_response_days=20,
        ),
        Agency(
            id=str(uuid4()),
            name="General Services Administration",
            abbreviation="GSA",
            level=AgencyLevel.FEDERAL,
            foia_email="gsa.foia@gsa.gov",
            preferred_method=DeliveryMethod.EMAIL,
            typical_response_days=20,
        ),
    ]
    for a in agencies:
        session.add(a)
    session.flush()

    # --- Requests (bland, non-sensitive) ---
    now = datetime.utcnow()
    requests_data = [
        {
            "subject": "Monthly weather data summaries for Portland, OR (2024)",
            "body": (
                "I am requesting copies of monthly weather data summary reports "
                "for the Portland, Oregon metropolitan area for the calendar year 2024. "
                "I am a hobbyist climate researcher and would appreciate any publicly "
                "available datasets in CSV or PDF format."
            ),
            "agency_idx": 0,
            "status": RequestStatus.COMPLETE,
            "sent_at": now - timedelta(days=45),
            "response_at": now - timedelta(days=12),
        },
        {
            "subject": "Public meeting minutes — GSA regional office, Q3 2024",
            "body": (
                "Under the Freedom of Information Act, I request copies of all "
                "publicly recorded meeting minutes from the GSA Pacific Rim Region "
                "office for July through September 2024."
            ),
            "agency_idx": 2,
            "status": RequestStatus.PROCESSING,
            "sent_at": now - timedelta(days=18),
            "response_at": None,
        },
        {
            "subject": "Visitor statistics for Crater Lake National Park (2023-2024)",
            "body": (
                "I request visitor count statistics and any available demographic "
                "breakdowns for Crater Lake National Park covering the period "
                "January 2023 through December 2024. This data is for a community "
                "blog post about park accessibility."
            ),
            "agency_idx": 1,
            "status": RequestStatus.SENT,
            "sent_at": now - timedelta(days=5),
            "response_at": None,
        },
    ]

    created_requests = []
    for i, rd in enumerate(requests_data):
        req = Request(
            id=str(uuid4()),
            request_number=f"REQ-{(now - timedelta(days=60 - i * 15)).strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}",
            requester_id=user.id,
            agency_id=agencies[rd["agency_idx"]].id,
            subject=rd["subject"],
            body=rd["body"],
            delivery_method=DeliveryMethod.EMAIL,
            status=rd["status"],
            fee_waiver_requested=True,
            sent_at=rd["sent_at"],
        )
        session.add(req)
        created_requests.append(req)
    session.flush()

    # --- Documents (one response for the completed request) ---
    weather_text = (
        "Monthly Climate Summary — Portland, OR\n"
        "National Weather Service\n\n"
        "January 2024: Average high 47F, average low 37F, total precip 5.2 in.\n"
        "February 2024: Average high 51F, average low 39F, total precip 3.8 in.\n"
    )
    doc = Document(
        id=str(uuid4()),
        request_id=created_requests[0].id,
        filename="noaa_portland_weather_2024.pdf",
        file_path="docs/noaa_portland_weather_2024.pdf",
        file_size=245_000,
        mime_type="application/pdf",
        doc_type=DocumentType.FULL_RESPONSE,
        page_count=12,
        extracted_text=weather_text,
        received_at=now - timedelta(days=12),
    )
    session.add(doc)
    session.flush()

    # --- Entities (innocuous) ---
    entities = [
        Entity(
            id=str(uuid4()),
            document_id=doc.id,
            entity_type=EntityType.ORGANIZATION,
            raw_text="National Weather Service",
            normalized_text="national weather service",
            context="Monthly Climate Summary — Portland, OR\nNational Weather Service",
        ),
        Entity(
            id=str(uuid4()),
            document_id=doc.id,
            entity_type=EntityType.LOCATION,
            raw_text="Portland, Oregon",
            normalized_text="portland, oregon",
            context="weather data summary reports for the Portland, Oregon metropolitan area",
        ),
        Entity(
            id=str(uuid4()),
            document_id=doc.id,
            entity_type=EntityType.DATE,
            raw_text="January 2024",
            normalized_text="january 2024",
            context="January 2024: Average high 47F, average low 37F",
        ),
    ]
    for e in entities:
        session.add(e)

    session.commit()
    session.close()
    engine.dispose()
