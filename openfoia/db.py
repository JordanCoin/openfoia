"""Database session management for OpenFOIA.

All data stored locally in ~/.openfoia/data.db
Supports optional AES-256 encryption at rest via SQLCipher.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Agency, AgencyLevel, DeliveryMethod

# Check for SQLCipher availability
_HAS_SQLCIPHER = False
try:
    import pysqlcipher3.dbapi2 as sqlcipher  # noqa: F401

    _HAS_SQLCIPHER = True
except ImportError:
    pass


def has_sqlcipher() -> bool:
    """Return True if pysqlcipher3 is installed and usable."""
    return _HAS_SQLCIPHER


def get_db_password() -> str | None:
    """Get the database password from env var or config.

    Priority: OPENFOIA_DB_PASSWORD env var > config file.
    Returns None if no password is configured.
    """
    # Check env var first
    pw = os.environ.get("OPENFOIA_DB_PASSWORD")
    if pw:
        return pw

    # Check config file
    from .config import load_config

    cfg = load_config()
    return cfg.encryption.password


#: Sidecar files SQLite writes next to the database. They hold recent writes
#: in plaintext, so they must be shredded whenever the main file is.
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def secure_delete_plaintext_db(db_path: Path) -> None:
    """Shred a plaintext database *in place*, along with its sidecar files.

    Overwriting the file's own blocks matters: renaming it away and deleting a
    copy (the previous behaviour) frees the original blocks without ever
    touching them, leaving the whole pre-encryption database recoverable by
    file carving. Best-effort on SSDs — see docs/THREAT_MODEL.md.
    """
    from .security import secure_delete

    db_path = Path(db_path)
    for candidate in (db_path, *(Path(str(db_path) + s) for s in _DB_SIDECAR_SUFFIXES)):
        if candidate.is_file():
            secure_delete(candidate)


def sqlcipher_key_literal(password: str) -> str:
    """Return *password* as a safely-quoted SQL string literal.

    SQLCipher's ``PRAGMA key`` cannot be parameterized through every driver
    path, so the passphrase has to be embedded in the statement text. Escaping
    is not optional: a passphrase containing an apostrophe (``it's ...``) used
    to terminate the literal early, turning the remainder into a SQL comment
    and silently reducing the effective key to the few characters before the
    quote — on both create and unlock, so nothing looked broken.
    """
    escaped = password.replace("'", "''")
    return f"'{escaped}'"


def sqlcipher_key_pragma(password: str) -> str:
    """Build the ``PRAGMA key`` statement for *password*."""
    return f"PRAGMA key = {sqlcipher_key_literal(password)}"


def _ensure_private_dir(path: Path) -> Path:
    """Create *path* if needed and make it owner-only (0700).

    The data directory holds the investigation database, ingested documents
    and config.json (which can carry SMTP/Twilio/Lob credentials). On a shared
    machine the default 0755 let any other local account read all of it.
    Directories created before this fix are tightened on next use.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        try:
            current = stat.S_IMODE(path.stat().st_mode)
            if current & 0o077:
                os.chmod(path, 0o700)
        except OSError:
            # Read-only media or a filesystem without POSIX modes — the caller
            # still gets a usable directory; permissions just cannot be fixed.
            pass
    return path


def get_data_dir() -> Path:
    """Get the OpenFOIA data directory, creating if needed.

    Resolution order:
    1. OPENFOIA_DATA_DIR env var (explicit override)
    2. Portable mode: if a .openfoia-portable file exists next to the
       installed package (or in the current directory), use a data/
       directory there — keeps everything on the USB stick
    3. Default: ~/.openfoia/

    For USB deployments: create a .openfoia-portable file on the USB
    and all data stays on the drive, never touching the host machine.
    """
    # 1. Explicit env var
    env_dir = os.environ.get("OPENFOIA_DATA_DIR")
    if env_dir:
        data_dir = Path(env_dir)
        return _ensure_private_dir(data_dir)

    # 2. Portable mode — check for marker file
    # Check next to this package
    package_dir = Path(__file__).parent.parent
    portable_marker = package_dir / ".openfoia-portable"
    if portable_marker.exists():
        data_dir = package_dir / "openfoia-data"
        return _ensure_private_dir(data_dir)

    # Also check current working directory
    cwd_marker = Path.cwd() / ".openfoia-portable"
    if cwd_marker.exists():
        data_dir = Path.cwd() / "openfoia-data"
        return _ensure_private_dir(data_dir)

    # 3. Default
    data_dir = Path.home() / ".openfoia"
    return _ensure_private_dir(data_dir)


def get_db_path(password: str | None = None) -> Path:
    """Get the database file path.

    If *password* matches the stored duress password, returns the decoy
    database path instead of the real one — transparently.
    """
    if password is None:
        password = get_db_password()

    if password:
        from .security import is_duress_password, get_decoy_db_path

        if is_duress_password(password):
            return get_decoy_db_path()

    # Once duress mode is configured the real database lives in an opaque
    # profile slot; before that it is the legacy data.db.
    from .security import real_profile_path

    real_slot = real_profile_path()
    if real_slot.exists():
        return real_slot

    return get_data_dir() / "data.db"


def get_engine(db_path: Path | None = None, password: str | None = None) -> Engine:
    """Create a SQLAlchemy engine.

    If *password* is provided (or discovered from config/env), the engine
    will use SQLCipher for AES-256 encryption at rest.  When no password is
    set, plain SQLite is used -- fully backwards-compatible.
    """
    if db_path is None:
        db_path = get_db_path(password=password)

    if password is None:
        password = get_db_password()

    if password and _HAS_SQLCIPHER:
        # Use pysqlcipher3 as the DBAPI driver via creator pattern
        def _sqlcipher_creator():
            conn = sqlcipher.connect(str(db_path))
            conn.execute(sqlcipher_key_pragma(password))
            conn.execute("PRAGMA cipher_compatibility = 4")
            # Keep key material and plaintext pages out of memory longer than
            # necessary (wiped on free rather than left for a core dump).
            conn.execute("PRAGMA cipher_memory_security = ON")
            return conn

        engine = create_engine(
            "sqlite+pysqlite:///",  # dummy URL; creator overrides
            creator=_sqlcipher_creator,
            echo=False,
        )
    elif password and not _HAS_SQLCIPHER:
        raise RuntimeError(
            "Database password is set but pysqlcipher3 is not installed. "
            "Your data would be stored UNENCRYPTED. Refusing to continue. "
            "Install encryption support: openfoia install-extras encryption"
        )
    else:
        url = f"sqlite:///{db_path}"
        engine = create_engine(url, echo=False)

    # Enable foreign keys for SQLite / SQLCipher
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def get_session_factory(engine: Engine | None = None) -> sessionmaker:
    """Create a session factory."""
    if engine is None:
        engine = get_engine()
    return sessionmaker(bind=engine)


@contextmanager
def get_session(password: str | None = None) -> Generator[Session, None, None]:
    """Get a database session with automatic commit/rollback."""
    engine = get_engine(password=password)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_migrations(password: str | None = None) -> None:
    """Run alembic migrations to bring the database schema up to date."""
    from alembic import command
    from alembic.config import Config

    db_path = get_db_path()

    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))

    if password is None:
        password = get_db_password()

    if password and _HAS_SQLCIPHER:
        # Alembic needs a real engine to run migrations against SQLCipher.
        # We pass the engine via the config attributes.
        engine = get_engine(db_path=db_path, password=password)
        alembic_cfg.attributes["connection"] = engine.connect()
        alembic_cfg.set_main_option("sqlalchemy.url", "sqlite:///")  # placeholder
        command.upgrade(alembic_cfg, "head")
        alembic_cfg.attributes["connection"].close()
    else:
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.upgrade(alembic_cfg, "head")


def init_db(seed: bool = True, password: str | None = None) -> None:
    """Initialize the database, running migrations and optionally seeding data."""
    # Ensure data directory exists
    get_data_dir()

    # Run alembic migrations (creates/updates tables)
    run_migrations(password=password)

    if seed:
        engine = get_engine(password=password)
        seed_agencies(engine)


def encrypt_database(password: str) -> None:
    """Encrypt an existing plaintext SQLite database with SQLCipher.

    Reads the current plaintext database, creates an encrypted copy,
    then swaps it in place. Raises RuntimeError if SQLCipher is not available.
    """
    if not _HAS_SQLCIPHER:
        raise RuntimeError(
            "pysqlcipher3 is not installed. Install with: pip install 'openfoia[encryption]'"
        )

    db_path = get_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    # Create encrypted copy in a temp file next to the original
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", dir=db_path.parent, prefix=".encrypting_")
    os.close(tmp_fd)

    try:
        # Open plaintext database with regular sqlite3
        plain_conn = sqlite3.connect(str(db_path))

        # Open new encrypted database with SQLCipher
        enc_conn = sqlcipher.connect(tmp_path)
        enc_conn.execute(sqlcipher_key_pragma(password))
        enc_conn.execute("PRAGMA cipher_compatibility = 4")

        # Dump plaintext and replay into encrypted DB
        for line in plain_conn.iterdump():
            enc_conn.execute(line)

        enc_conn.commit()
        enc_conn.close()
        plain_conn.close()

        # Shred the plaintext original IN PLACE before swapping the encrypted
        # file in. Renaming it away first would free its blocks untouched and
        # leave the entire unencrypted database recoverable.
        secure_delete_plaintext_db(db_path)
        shutil.move(tmp_path, db_path)
        os.chmod(db_path, 0o600)
    except Exception:
        # Clean up temp file on failure
        if Path(tmp_path).exists():
            os.unlink(tmp_path)
        raise


def seed_agencies(engine: Engine) -> int:
    """Seed the database with federal agencies. Returns count of agencies added."""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    # Check if agencies already exist
    existing = session.query(Agency).count()
    if existing > 0:
        session.close()
        return 0

    agencies_data = get_federal_agencies()

    for data in agencies_data:
        agency = Agency(
            name=data["name"],
            abbreviation=data.get("abbreviation"),
            level=AgencyLevel.FEDERAL,
            foia_email=data.get("foia_email"),
            foia_fax=data.get("foia_fax"),
            foia_address=data.get("foia_address"),
            foia_portal_url=data.get("foia_portal_url"),
            preferred_method=DeliveryMethod(data.get("preferred_method", "email")),
            typical_response_days=data.get("typical_response_days", 20),
            fee_waiver_criteria=data.get("fee_waiver_criteria"),
        )
        session.add(agency)

    session.commit()
    count = len(agencies_data)
    session.close()
    return count


def get_federal_agencies() -> list[dict]:
    """Return federal agency seed data.

    Sources:
    - https://www.foia.gov/agency-search.html
    - Individual agency FOIA pages
    """
    return [
        # Intelligence & Security
        {
            "name": "Central Intelligence Agency",
            "abbreviation": "CIA",
            "foia_email": "foiacoordinator@ucia.gov",
            "foia_fax": "+1-703-613-3007",
            "foia_address": "Central Intelligence Agency\nInformation and Privacy Coordinator\nWashington, DC 20505",
            "foia_portal_url": "https://www.cia.gov/readingroom/",
            "preferred_method": "mail",
            "typical_response_days": 45,
            "fee_waiver_criteria": "Requester must demonstrate they are a representative of the news media or the information will contribute significantly to public understanding.",
        },
        {
            "name": "Federal Bureau of Investigation",
            "abbreviation": "FBI",
            "foia_email": "foiparequest@fbi.gov",
            "foia_fax": "+1-540-868-4391",
            "foia_address": "Federal Bureau of Investigation\nRecord/Information Dissemination Section\n170 Marcel Drive\nWinchester, VA 22602-4843",
            "foia_portal_url": "https://vault.fbi.gov/",
            "preferred_method": "email",
            "typical_response_days": 60,
            "fee_waiver_criteria": "News media representatives and educational/scientific institutions may qualify for reduced fees.",
        },
        {
            "name": "National Security Agency",
            "abbreviation": "NSA",
            "foia_email": "nsafoia@nsa.gov",
            "foia_address": "National Security Agency\nFOIA/PA Office (DJ4)\n9800 Savage Road, Suite 6932\nFt. George G. Meade, MD 20755-6932",
            "foia_portal_url": "https://www.nsa.gov/resources/everyone/foia/",
            "preferred_method": "email",
            "typical_response_days": 90,
        },
        {
            "name": "Department of Homeland Security",
            "abbreviation": "DHS",
            "foia_email": "foia@hq.dhs.gov",
            "foia_address": "Department of Homeland Security\nPrivacy Office, Mail Stop 0655\n2707 Martin Luther King Jr Ave SE\nWashington, DC 20528-0655",
            "foia_portal_url": "https://www.dhs.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        # Justice
        {
            "name": "Department of Justice",
            "abbreviation": "DOJ",
            "foia_email": "MRUFOIA.Requests@usdoj.gov",
            "foia_address": "Director, Office of Information Policy\nU.S. Department of Justice\n441 G Street, NW, 6th Floor\nWashington, DC 20530",
            "foia_portal_url": "https://www.justice.gov/oip/submit-and-track-request-or-appeal",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Bureau of Alcohol, Tobacco, Firearms and Explosives",
            "abbreviation": "ATF",
            "foia_email": "FOIAMail@atf.gov",
            "foia_address": "ATF FOIA Office\n244 Needy Road, Suite 1600\nMartinsburg, WV 25405",
            "foia_portal_url": "https://www.atf.gov/resource-center/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Drug Enforcement Administration",
            "abbreviation": "DEA",
            "foia_email": "DEA.FOIA@usdoj.gov",
            "foia_address": "Drug Enforcement Administration\nFOIA/PA Section\n8701 Morrissette Drive\nSpringfield, VA 22152",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "U.S. Marshals Service",
            "abbreviation": "USMS",
            "foia_email": "usms.foia@usdoj.gov",
            "foia_address": "U.S. Marshals Service\nFOIA/PA Officer\nCS-3, 10th Floor\nWashington, DC 20530",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        # Defense
        {
            "name": "Department of Defense",
            "abbreviation": "DOD",
            "foia_email": "osd.foia@mail.mil",
            "foia_address": "Office of the Secretary of Defense/Joint Staff FOIA Requester Service Center\n1155 Defense Pentagon\nWashington, DC 20301-1155",
            "foia_portal_url": "https://www.esd.whs.mil/FOIA/",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "Department of the Army",
            "abbreviation": "Army",
            "foia_email": "usarmy.pentagon.hqda-oaa.mbx.rmda-foia@mail.mil",
            "foia_address": "Department of the Army\nFOIA/PA Office\nRoom 1146, Pentagon\nWashington, DC 20310",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "Department of the Navy",
            "abbreviation": "Navy",
            "foia_email": "DONFOIA-PA@navy.mil",
            "foia_address": "Department of the Navy\nChief of Naval Operations (DNS-36)\n2000 Navy Pentagon\nWashington, DC 20350-2000",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "Department of the Air Force",
            "abbreviation": "USAF",
            "foia_email": "usaf.pentagon.saf-aa.mbx.haf-foia-workflow@mail.mil",
            "foia_address": "Secretary of the Air Force\nHAF FOIA Office (SAF/AAII(F))\n1000 Air Force Pentagon\nWashington, DC 20330-1000",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        # Regulatory
        {
            "name": "Environmental Protection Agency",
            "abbreviation": "EPA",
            "foia_email": "hq.foia@epa.gov",
            "foia_address": "National FOIA Office\nU.S. EPA, MC 2822T\n1200 Pennsylvania Avenue, NW\nWashington, DC 20460",
            "foia_portal_url": "https://www.epa.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Communications Commission",
            "abbreviation": "FCC",
            "foia_email": "foia@fcc.gov",
            "foia_address": "Federal Communications Commission\nFOIA Control Office\n445 12th Street SW\nWashington, DC 20554",
            "foia_portal_url": "https://www.fcc.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Trade Commission",
            "abbreviation": "FTC",
            "foia_email": "foia@ftc.gov",
            "foia_address": "Office of General Counsel\nFederal Trade Commission\n600 Pennsylvania Avenue, NW\nWashington, DC 20580",
            "foia_portal_url": "https://www.ftc.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Securities and Exchange Commission",
            "abbreviation": "SEC",
            "foia_email": "foiapa@sec.gov",
            "foia_address": "FOIA/PA Branch, Office of FOIA Services\nSecurities and Exchange Commission\n100 F Street NE\nWashington, DC 20549",
            "foia_portal_url": "https://www.sec.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Election Commission",
            "abbreviation": "FEC",
            "foia_email": "FOIA@fec.gov",
            "foia_address": "Federal Election Commission\nFOIA Requester Service Center\n1050 First Street NE\nWashington, DC 20463",
            "foia_portal_url": "https://www.fec.gov/freedom-information-act/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Energy Regulatory Commission",
            "abbreviation": "FERC",
            "foia_email": "foia-ceii@ferc.gov",
            "foia_address": "FOIA Officer\nFederal Energy Regulatory Commission\n888 First Street NE, Room 9A-01\nWashington, DC 20426",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        # Executive
        {
            "name": "White House Office",
            "abbreviation": "EOP",
            "foia_address": "Executive Office of the President\nOffice of Administration\nFOIA/Privacy Act Officer\n725 17th Street NW\nWashington, DC 20503",
            "preferred_method": "mail",
            "typical_response_days": 45,
        },
        {
            "name": "Office of Management and Budget",
            "abbreviation": "OMB",
            "foia_email": "ombfoia@omb.eop.gov",
            "foia_address": "Office of Management and Budget\nFOIA Officer\n725 17th Street NW\nWashington, DC 20503",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        # Health & Human Services
        {
            "name": "Department of Health and Human Services",
            "abbreviation": "HHS",
            "foia_email": "osfoia@hhs.gov",
            "foia_address": "U.S. Department of Health and Human Services\nOffice of the Secretary\nFOIA Office\n330 C Street SW, Room L-110\nWashington, DC 20201",
            "foia_portal_url": "https://www.hhs.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Food and Drug Administration",
            "abbreviation": "FDA",
            "foia_email": "FDAFOIA@fda.hhs.gov",
            "foia_address": "Food and Drug Administration\nDivision of Freedom of Information\n5630 Fishers Lane, Room 1035\nRockville, MD 20857",
            "foia_portal_url": "https://www.fda.gov/regulatory-information/freedom-information",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Centers for Disease Control and Prevention",
            "abbreviation": "CDC",
            "foia_email": "cdcfoia@cdc.gov",
            "foia_address": "CDC/ATSDR\nFOIA Office, MS D-54\n1600 Clifton Road NE\nAtlanta, GA 30329-4018",
            "foia_portal_url": "https://www.cdc.gov/od/foia/",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "National Institutes of Health",
            "abbreviation": "NIH",
            "foia_email": "nihfoia@nih.gov",
            "foia_address": "National Institutes of Health\nFOIA Office\n9000 Rockville Pike, Building 31, Room 5B-35\nBethesda, MD 20892",
            "foia_portal_url": "https://www.nih.gov/institutes-nih/nih-office-director/office-communications-public-liaison/freedom-information-act-office",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Centers for Medicare & Medicaid Services",
            "abbreviation": "CMS",
            "foia_email": "cmsfoia@cms.hhs.gov",
            "foia_address": "CMS FOIA Group\n7500 Security Boulevard, N2-20-16\nBaltimore, MD 21244",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        # Treasury & Finance
        {
            "name": "Department of the Treasury",
            "abbreviation": "Treasury",
            "foia_email": "FOIA@treasury.gov",
            "foia_address": "Department of the Treasury\nFOIA and Transparency\n1500 Pennsylvania Avenue NW\nWashington, DC 20220",
            "foia_portal_url": "https://home.treasury.gov/footer/freedom-of-information-act",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Internal Revenue Service",
            "abbreviation": "IRS",
            "foia_email": "FOIA.Request@irs.gov",
            "foia_address": "IRS Headquarters FOIA\nStop 211\nPO Box 621506\nAtlanta, GA 30362-3006",
            "foia_portal_url": "https://www.irs.gov/privacy-disclosure/irs-freedom-of-information",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "Federal Reserve System",
            "abbreviation": "FRS",
            "foia_email": "FOIA@frb.gov",
            "foia_address": "Board of Governors of the Federal Reserve System\nFOIA Office\n20th Street & Constitution Avenue NW\nWashington, DC 20551",
            "foia_portal_url": "https://www.federalreserve.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Consumer Financial Protection Bureau",
            "abbreviation": "CFPB",
            "foia_email": "FOIA@consumerfinance.gov",
            "foia_address": "CFPB FOIA Office\n1700 G Street NW\nWashington, DC 20552",
            "foia_portal_url": "https://www.consumerfinance.gov/foia-requests/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        # State & International
        {
            "name": "Department of State",
            "abbreviation": "State",
            "foia_email": "FOIA@state.gov",
            "foia_address": "U.S. Department of State\nOffice of Information Programs and Services\nA/GIS/IPS/RL\nSA-2, Suite 8100\nWashington, DC 20522-0208",
            "foia_portal_url": "https://foia.state.gov/",
            "preferred_method": "email",
            "typical_response_days": 35,
        },
        {
            "name": "U.S. Agency for International Development",
            "abbreviation": "USAID",
            "foia_email": "foia@usaid.gov",
            "foia_address": "USAID FOIA Office\n1300 Pennsylvania Avenue NW, Room 2.07C\nWashington, DC 20523",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        # Labor & Commerce
        {
            "name": "Department of Labor",
            "abbreviation": "DOL",
            "foia_email": "foiacoordinator@dol.gov",
            "foia_address": "U.S. Department of Labor\nOffice of the Solicitor\nFOIA/FACA Division\n200 Constitution Avenue NW, Room N-2428\nWashington, DC 20210",
            "foia_portal_url": "https://www.dol.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of Commerce",
            "abbreviation": "Commerce",
            "foia_email": "eFOIA@doc.gov",
            "foia_address": "Department of Commerce\nFOIA Officer\n1401 Constitution Avenue NW, Room 4513\nWashington, DC 20230",
            "foia_portal_url": "https://www.commerce.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "U.S. Patent and Trademark Office",
            "abbreviation": "USPTO",
            "foia_email": "foia@uspto.gov",
            "foia_address": "USPTO FOIA Office\nP.O. Box 1450\nAlexandria, VA 22313-1450",
            "foia_portal_url": "https://www.uspto.gov/learning-and-resources/ip-policy/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "National Oceanic and Atmospheric Administration",
            "abbreviation": "NOAA",
            "foia_email": "foia@noaa.gov",
            "foia_address": "NOAA FOIA Officer\n1315 East-West Highway, SSMC3, Room 3627\nSilver Spring, MD 20910",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        # Transportation
        {
            "name": "Department of Transportation",
            "abbreviation": "DOT",
            "foia_email": "foia@dot.gov",
            "foia_address": "Office of the General Counsel\nFOIA Office\n1200 New Jersey Avenue SE\nWashington, DC 20590",
            "foia_portal_url": "https://www.transportation.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Aviation Administration",
            "abbreviation": "FAA",
            "foia_email": "9-AWA-ARC-FOIA@faa.gov",
            "foia_address": "FAA FOIA Office\n800 Independence Avenue SW, Room 305\nWashington, DC 20591",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "National Highway Traffic Safety Administration",
            "abbreviation": "NHTSA",
            "foia_email": "NHTSA.FOIA@dot.gov",
            "foia_address": "NHTSA FOIA Office\n1200 New Jersey Avenue SE, West Building\nWashington, DC 20590",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        # Other Major Agencies
        {
            "name": "National Aeronautics and Space Administration",
            "abbreviation": "NASA",
            "foia_email": "hq-foia@nasa.gov",
            "foia_address": "NASA Headquarters\nFOIA Office\n300 E Street SW, Room 5K39\nWashington, DC 20546",
            "foia_portal_url": "https://www.nasa.gov/FOIA/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Social Security Administration",
            "abbreviation": "SSA",
            "foia_email": "foia.pa.officers@ssa.gov",
            "foia_address": "Social Security Administration\nOffice of Privacy and Disclosure\nFOIA Workgroup\n617 Altmeyer Building\n6401 Security Boulevard\nBaltimore, MD 21235",
            "foia_portal_url": "https://www.ssa.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Department of Education",
            "abbreviation": "ED",
            "foia_email": "EDFOIAManager@ed.gov",
            "foia_address": "U.S. Department of Education\nFOIA Service Center\n400 Maryland Avenue SW, LBJ 7W106A\nWashington, DC 20202",
            "foia_portal_url": "https://www2.ed.gov/policy/gen/leg/foia/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of Energy",
            "abbreviation": "DOE",
            "foia_email": "FOIA-Central@hq.doe.gov",
            "foia_address": "FOIA Officer\nU.S. Department of Energy\n1000 Independence Avenue SW\nWashington, DC 20585",
            "foia_portal_url": "https://www.energy.gov/management/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of Veterans Affairs",
            "abbreviation": "VA",
            "foia_email": "vaborefoia@va.gov",
            "foia_address": "Department of Veterans Affairs\nFOIA Service (005R1C)\n810 Vermont Avenue NW\nWashington, DC 20420",
            "foia_portal_url": "https://www.va.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Department of Housing and Urban Development",
            "abbreviation": "HUD",
            "foia_email": "HUD_FOIA@hud.gov",
            "foia_address": "HUD FOIA Office\n451 7th Street SW, Room 10139\nWashington, DC 20410",
            "foia_portal_url": "https://www.hud.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of the Interior",
            "abbreviation": "DOI",
            "foia_email": "os_foia@ios.doi.gov",
            "foia_address": "Department of the Interior\nFOIA Officer\n1849 C Street NW, MS-7328-MIB\nWashington, DC 20240",
            "foia_portal_url": "https://www.doi.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of Agriculture",
            "abbreviation": "USDA",
            "foia_email": "APHIS.FOIA.Officer@usda.gov",
            "foia_address": "USDA FOIA Service Center\n1400 Independence Avenue SW, Room 4037A\nWashington, DC 20250",
            "foia_portal_url": "https://www.usda.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        # Independent Agencies
        {
            "name": "General Services Administration",
            "abbreviation": "GSA",
            "foia_email": "gsa.foia@gsa.gov",
            "foia_address": "GSA FOIA Requester Service Center (H1F)\n1800 F Street NW\nWashington, DC 20405",
            "foia_portal_url": "https://www.gsa.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Office of Personnel Management",
            "abbreviation": "OPM",
            "foia_email": "foia@opm.gov",
            "foia_address": "U.S. Office of Personnel Management\nFOIA Requester Service Center\n1900 E Street NW, Room 5415\nWashington, DC 20415-0001",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "National Archives and Records Administration",
            "abbreviation": "NARA",
            "foia_email": "foia@nara.gov",
            "foia_address": "National Archives and Records Administration\nFOIA Office\n8601 Adelphi Road, Room 3110\nCollege Park, MD 20740-6001",
            "foia_portal_url": "https://www.archives.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Small Business Administration",
            "abbreviation": "SBA",
            "foia_email": "foia@sba.gov",
            "foia_address": "SBA FOIA Office\n409 3rd Street SW\nWashington, DC 20416",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "U.S. Postal Service",
            "abbreviation": "USPS",
            "foia_email": "foia@usps.gov",
            "foia_address": "Records Office\nFOIA Requester Service Center\nU.S. Postal Service\n475 L'Enfant Plaza SW, Room 1P830\nWashington, DC 20260-1101",
            "foia_portal_url": "https://about.usps.com/who/legal/foia/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "National Labor Relations Board",
            "abbreviation": "NLRB",
            "foia_email": "foia@nlrb.gov",
            "foia_address": "National Labor Relations Board\nFOIA Officer\n1015 Half Street SE\nWashington, DC 20570",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Equal Employment Opportunity Commission",
            "abbreviation": "EEOC",
            "foia_email": "foia@eeoc.gov",
            "foia_address": "Equal Employment Opportunity Commission\nFOIA Programs\n131 M Street NE\nWashington, DC 20507",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
    ]
