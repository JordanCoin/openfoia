"""End-to-end SQLCipher tests — the real encryption paths, not helpers.

The at-rest fixes (key escaping, in-place shredding, duress slots) are the
highest-stakes code in the project and were previously only exercised at the
helper level, because the encryption extra could not be installed. These run
against a real encrypted database.

Skipped when no SQLCipher driver is present; CI installs one so they always
run there.
"""

from __future__ import annotations

import pytest

from openfoia.db import has_sqlcipher

pytestmark = pytest.mark.skipif(
    not has_sqlcipher(), reason="no SQLCipher driver installed (pip install sqlcipher3-binary)"
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    return tmp_path


def _driver():
    from openfoia.db import get_sqlcipher_driver

    return get_sqlcipher_driver()


def _open(path, password):
    """Open an encrypted DB and return the connection, or raise."""
    from openfoia.db import sqlcipher_key_pragma

    conn = _driver().connect(str(path))
    conn.execute(sqlcipher_key_pragma(password))
    conn.execute("PRAGMA cipher_compatibility = 4")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return conn


def _make_encrypted(path, password, secret="TOP-SECRET-INVESTIGATION"):
    conn = _driver().connect(str(path))
    from openfoia.db import sqlcipher_key_pragma

    conn.execute(sqlcipher_key_pragma(password))
    conn.execute("PRAGMA cipher_compatibility = 4")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (secret,))
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# The key-escaping fix, verified against real SQLCipher
# ---------------------------------------------------------------------------

TRICKY_PASSPHRASES = [
    "it's a long passphrase with an apostrophe",
    "abc'--the-rest-of-my-very-long-passphrase",
    "quote'quote'quote",
    "trailing'",
    'double"quote and back\\slash',
]


@pytest.mark.parametrize("password", TRICKY_PASSPHRASES)
def test_passphrase_round_trips_through_real_sqlcipher(data_dir, password):
    """The full passphrase must be the key — not a truncated prefix."""
    db = _make_encrypted(data_dir / "t.db", password)

    conn = _open(db, password)
    assert conn.execute("SELECT x FROM t").fetchone()[0] == "TOP-SECRET-INVESTIGATION"
    conn.close()


@pytest.mark.parametrize("password", TRICKY_PASSPHRASES)
def test_truncated_prefix_does_not_unlock(data_dir, password):
    """The regression: `abc'--tail` must NOT be openable with just `abc`.

    Under the old f-string interpolation the effective key was the text
    before the apostrophe, so this prefix would have opened the database.
    """
    db = _make_encrypted(data_dir / "t.db", password)
    prefix = password.split("'")[0]
    if not prefix or prefix == password:
        pytest.skip("passphrase has no quote-truncation prefix to test")

    with pytest.raises(_driver().DatabaseError):
        _open(db, prefix)


def test_wrong_password_is_rejected(data_dir):
    db = _make_encrypted(data_dir / "t.db", "correct-horse")

    with pytest.raises(_driver().DatabaseError):
        _open(db, "wrong-horse")


def test_encrypted_file_contains_no_plaintext(data_dir):
    db = _make_encrypted(data_dir / "t.db", "hunter2")

    raw = db.read_bytes()
    assert b"TOP-SECRET-INVESTIGATION" not in raw
    assert not raw.startswith(b"SQLite format 3")


# ---------------------------------------------------------------------------
# init / engine / migrations against a real encrypted database
# ---------------------------------------------------------------------------


def test_init_db_with_password_produces_an_encrypted_db(data_dir):
    """Encrypted init was silently broken: alembic migrated a throwaway DB."""
    from openfoia.db import get_db_path, init_db

    init_db(seed=False, password="it's-encrypted")

    db = get_db_path(password="it's-encrypted")
    assert db.exists()

    raw = db.read_bytes()
    assert not raw.startswith(b"SQLite format 3"), "database is not encrypted"

    # The schema must actually be present in the ENCRYPTED database.
    conn = _open(db, "it's-encrypted")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "requests" in tables or "agencies" in tables, f"schema missing, got: {tables}"


def test_encrypt_database_leaves_no_plaintext_behind(data_dir):
    """The flagship fix: shred the original in place, keep no .bak."""
    import sqlite3

    from openfoia.db import encrypt_database, get_db_path

    db = get_db_path()
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('PLAINTEXT-EVIDENCE')")
    conn.commit()
    conn.close()
    assert b"PLAINTEXT-EVIDENCE" in db.read_bytes()

    encrypt_database("it's-a-secret")

    assert not (data_dir / "data.db.bak").exists(), "plaintext backup left on disk"
    raw = db.read_bytes()
    assert b"PLAINTEXT-EVIDENCE" not in raw
    assert not raw.startswith(b"SQLite format 3")

    conn = _open(db, "it's-a-secret")
    assert conn.execute("SELECT x FROM t").fetchone()[0] == "PLAINTEXT-EVIDENCE"
    conn.close()


def test_get_session_round_trips_through_encrypted_db(data_dir):
    """The ORM path must work against SQLCipher, with a quoted passphrase."""
    from openfoia.db import get_session, init_db
    from openfoia.models import Agency

    password = "it's-encrypted"
    init_db(seed=True, password=password)

    with get_session(password=password) as session:
        assert session.query(Agency).count() > 0


# ---------------------------------------------------------------------------
# Duress mode, end to end
# ---------------------------------------------------------------------------


def test_duress_password_opens_the_decoy_not_the_real_db(data_dir):
    from openfoia.db import get_db_path, init_db
    from openfoia.security import setup_duress_mode

    real_pw = "the-real-passphrase"
    duress_pw = "it's-the-duress-one"

    init_db(seed=False, password=real_pw)
    decoy = setup_duress_mode(duress_pw)

    assert decoy.exists()
    # The duress password resolves to the decoy...
    assert get_db_path(password=duress_pw) == decoy
    # ...and the real one does not.
    assert get_db_path(password=real_pw) != decoy


def test_duress_migration_moves_real_db_into_a_slot(data_dir):
    from openfoia.db import init_db
    from openfoia.security import real_profile_path, setup_duress_mode

    init_db(seed=False, password="real-pw")
    setup_duress_mode("duress-pw")

    assert real_profile_path().exists(), "real DB was not migrated into slot 0"
    assert list(data_dir.glob("data.db*")) == [], "legacy data.db* left behind"


def test_decoy_is_encrypted_not_plaintext(data_dir):
    from openfoia.db import init_db
    from openfoia.security import setup_duress_mode

    init_db(seed=False, password="real-pw")
    decoy = setup_duress_mode("duress-pw")

    raw = decoy.read_bytes()
    assert not raw.startswith(b"SQLite format 3"), "decoy is plaintext"


def test_decoy_opens_with_duress_password(data_dir):
    from openfoia.db import init_db
    from openfoia.security import setup_duress_mode

    init_db(seed=False, password="real-pw")
    decoy = setup_duress_mode("it's-duress")

    conn = _open(decoy, "it's-duress")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables, "decoy has no schema"
