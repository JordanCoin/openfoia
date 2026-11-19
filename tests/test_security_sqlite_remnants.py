"""Deleted text must not survive in the database file or its rollback journal.

SQLite leaves deleted rows in free pages, writes the original content of every
changed page to the rollback journal (so a delete copies the deleted text
there) and, by default, unlinks that journal after each transaction, leaving
it in the filesystem's free space. FTS5 keeps a deleted page's words, with
positions, in older index segments. These tests read the raw bytes of a
plaintext database after a delete.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from openfoia import db as dbmod
from openfoia import doctext

PHRASE = "Minister Xylobribe accepted the Quarnwick payment"
WORDS = ("Xylobribe", "Quarnwick")


def _needles() -> list[bytes]:
    """The phrase, each word as written, its FTS form, and word tails (prefix compression)."""
    out = [PHRASE.encode()]
    for w in WORDS:
        out += [w.encode(), w.lower().encode(), w.lower()[-6:].encode()]
    return out


def _leftovers(path: Path) -> list[bytes]:
    if not path.exists():
        return []
    data = path.read_bytes()
    return [n for n in _needles() if n in data]


@pytest.fixture
def plain_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    dbmod.init_db()
    path = dbmod.get_db_path()
    assert path.read_bytes()[:16] == b"SQLite format 3\x00"
    return path


def _filler(n: int) -> str:
    return " ".join(f"filler{n}w{i}" for i in range(300))


def _add_doc(pages: list[str]) -> str:
    from openfoia.models import Agency, DeliveryMethod, Document, DocumentType, Request, User

    with dbmod.get_session() as s:
        user = s.query(User).first() or User(id=str(uuid4()), email="a@b.c", name="L")
        s.add(user)
        s.flush()
        req = Request(
            id=str(uuid4()),
            request_number=f"R-{uuid4().hex[:6]}",
            requester_id=user.id,
            agency_id=s.query(Agency).first().id,
            subject="s",
            body="",
            delivery_method=DeliveryMethod.EMAIL,
        )
        s.add(req)
        s.flush()
        doc = Document(
            id=str(uuid4()),
            request_id=req.id,
            doc_type=DocumentType.FULL_RESPONSE,
            filename="x.txt",
            file_path="",
            file_size=0,
            mime_type="text/plain",
            extracted_text=doctext.join_pages(pages),
        )
        s.add(doc)
        doctext.replace_pages(s, doc.id, pages)
        return doc.id


def _delete_doc(doc_id: str) -> None:
    from openfoia.models import Document

    with dbmod.get_session() as s:
        s.delete(s.get(Document, doc_id))


def _seed_and_delete(plain_db) -> None:
    for n in range(3):
        _add_doc([_filler(n * 10 + p) for p in range(5)])
    secret = _add_doc([_filler(90), f"{_filler(91)} {PHRASE} {_filler(92)}", _filler(93)])
    _add_doc([_filler(70 + p) for p in range(5)])
    assert PHRASE.encode() in plain_db.read_bytes()  # the text really was stored
    _delete_doc(secret)


def _journal(path: Path) -> Path:
    return Path(str(path) + "-journal")


# --- the guarantee ----------------------------------------------------------


def test_deleted_document_text_is_not_in_the_database_file(plain_db):
    _seed_and_delete(plain_db)
    assert _leftovers(plain_db) == []
    assert _leftovers(_journal(plain_db)) == []
    assert not Path(str(plain_db) + "-wal").exists()
    with sqlite3.connect(plain_db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # The index still works for the documents that remain.
        assert conn.execute(
            "SELECT count(*) FROM document_pages_fts WHERE document_pages_fts MATCH 'filler70w1'"
        ).fetchone() == (1,)


def test_replaced_page_text_is_not_in_the_database_file(plain_db):
    """Re-OCR and re-fetch replace a document's pages: the old text goes too."""
    doc_id = _add_doc([_filler(1), f"{PHRASE} {_filler(2)}"])
    _add_doc([_filler(3)])
    from openfoia.models import Document

    new = [_filler(4), _filler(5)]
    with dbmod.get_session() as s:
        s.get(Document, doc_id).extracted_text = doctext.join_pages(new)
        doctext.replace_pages(s, doc_id, new)
    assert _leftovers(plain_db) == []
    assert _leftovers(_journal(plain_db)) == []


def _next_session() -> None:
    from openfoia.models import Agency

    with dbmod.get_session() as s:
        s.query(Agency).count()


def test_index_merge_skipped_by_a_kill_is_done_by_the_next_session(plain_db, monkeypatch):
    """The process dies after the delete commits, before the merge: the next session merges."""
    with monkeypatch.context() as m:
        m.setattr(dbmod, "_after_session", lambda engine: None)
        _seed_and_delete(plain_db)
    assert _leftovers(plain_db), "precondition: the index still holds the words"
    _next_session()
    assert _leftovers(plain_db) == []
    assert _leftovers(_journal(plain_db)) == []
    with sqlite3.connect(plain_db) as conn:
        assert conn.execute("SELECT count(*) FROM fts_merge_pending").fetchone()[0] == 0


def test_index_merge_blocked_by_a_busy_database_is_retried(plain_db, monkeypatch):
    """Another process holds the write lock during the merge: the next session merges."""
    real = dbmod._after_session

    def locked(engine):
        other = sqlite3.connect(plain_db, timeout=0.1)
        other.execute("BEGIN IMMEDIATE")
        try:
            real(engine)
        finally:
            other.rollback()
            other.close()

    with monkeypatch.context() as m:
        m.setattr(dbmod, "_after_session", locked)
        m.setattr(dbmod, "scrub_rollback_journal", lambda engine, **kw: False)
        _seed_and_delete(plain_db)
    assert _leftovers(plain_db), "precondition: the merge could not run"
    _next_session()
    assert _leftovers(plain_db) == []
    assert _leftovers(_journal(plain_db)) == []


def test_pending_merge_is_recorded_in_the_deleting_transaction(plain_db, monkeypatch):
    _next_session()  # clears the row migration 009 leaves for databases it upgrades
    with sqlite3.connect(plain_db) as conn:
        assert conn.execute("SELECT id FROM fts_merge_pending").fetchall() == []
    with monkeypatch.context() as m:
        m.setattr(dbmod, "_after_session", lambda engine: None)
        _seed_and_delete(plain_db)
    with sqlite3.connect(plain_db) as conn:
        assert conn.execute("SELECT id FROM fts_merge_pending").fetchall() == [(1,)]


# --- controls: what each part protects against ---------------------------------


def test_control_without_the_journal_scrub_the_journal_holds_deleted_text(plain_db, monkeypatch):
    """The journal really receives the deleted text; the scrub is what removes it."""
    monkeypatch.setattr(dbmod, "scrub_rollback_journal", lambda engine, **kw: True)
    # The index merge is a later transaction that reuses the journal from the start.
    monkeypatch.setattr(doctext, "merge_fulltext_index", lambda conn: False)
    _seed_and_delete(plain_db)
    assert PHRASE.encode() in _journal(plain_db).read_bytes()


def test_control_without_the_index_merge_fts_keeps_deleted_words(plain_db, monkeypatch):
    monkeypatch.setattr(doctext, "merge_fulltext_index", lambda conn: False)
    _seed_and_delete(plain_db)
    assert _leftovers(plain_db), "the FTS index should still hold the deleted words"


# --- pragmas on every connection ------------------------------------------------


def _pragmas(conn) -> tuple:
    return tuple(
        conn.execute(f"PRAGMA {p}").fetchone()[0]
        for p in ("secure_delete", "journal_mode", "temp_store", "foreign_keys")
    )


def test_engine_connections_get_the_pragmas(plain_db):
    engine = dbmod.get_engine(db_path=plain_db)
    raw = engine.raw_connection()
    try:
        assert _pragmas(raw.driver_connection) == (1, "persist", 2, 1)
    finally:
        raw.close()
        engine.dispose()


def test_migration_connections_get_the_pragmas(tmp_path, monkeypatch):
    """The plaintext alembic path builds its own engine; it must get them too."""
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    real = dbmod._set_connection_pragmas
    from_alembic = []

    def spy(dbapi_connection, connection_record=None):
        real(dbapi_connection, connection_record)
        if any(Path(f.filename).name == "env.py" for f in inspect.stack()):
            from_alembic.append(_pragmas(dbapi_connection))

    monkeypatch.setattr(dbmod, "_set_connection_pragmas", spy)
    dbmod.init_db()
    assert from_alembic and set(from_alembic) == {(1, "persist", 2, 1)}


def test_sqlcipher_connections_get_the_pragmas(tmp_path):
    if not dbmod.has_sqlcipher():
        pytest.skip("SQLCipher driver not installed")
    path = tmp_path / "enc.db"
    engine = dbmod.get_engine(db_path=path, password="correct horse")
    raw = engine.raw_connection()
    try:
        conn = raw.driver_connection
        conn.execute("CREATE TABLE t (x)")
        assert _pragmas(conn) == (1, "persist", 2, 1)
    finally:
        raw.close()
        engine.dispose()
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"


# --- the scrub itself -----------------------------------------------------------


def test_scrub_overwrites_with_zeros_before_truncating(plain_db, monkeypatch):
    """Truncating alone would free the blocks with the old content still in them."""
    _add_doc([f"{PHRASE} {_filler(1)}"])
    monkeypatch.setattr(dbmod, "scrub_rollback_journal", lambda engine, **kw: True)
    _add_doc([_filler(2)])  # a write transaction: the journal holds pre-images
    journal = _journal(plain_db)
    size = journal.stat().st_size
    assert size > 0
    monkeypatch.undo()

    seen = []
    real_fsync = os.fsync

    def spy(fd):
        real_fsync(fd)
        if not seen:
            seen.append(journal.read_bytes())

    monkeypatch.setattr(dbmod.os, "fsync", spy)
    engine = dbmod.get_engine(db_path=plain_db)
    try:
        assert dbmod.scrub_rollback_journal(engine) is True
    finally:
        engine.dispose()
    assert len(seen[0]) == size and seen[0] == bytes(size)
    assert journal.stat().st_size == 0


def test_scrub_skips_while_another_connection_holds_a_write_lock(plain_db, monkeypatch):
    monkeypatch.setattr(dbmod, "scrub_rollback_journal", lambda engine, **kw: True)
    _add_doc([_filler(1)])
    monkeypatch.undo()
    journal = _journal(plain_db)
    before = journal.read_bytes()
    assert before

    other = sqlite3.connect(plain_db, isolation_level=None)
    other.execute("PRAGMA journal_mode=PERSIST")
    other.execute("BEGIN IMMEDIATE")
    other.execute(
        "INSERT INTO users (id, email, name, is_journalist, created_at) "
        "VALUES ('u2', 'x@y.z', 'X', 0, '2026-01-01')"
    )
    engine = dbmod.get_engine(db_path=plain_db)
    try:
        assert dbmod.scrub_rollback_journal(engine, busy_ms=50) is False
    finally:
        engine.dispose()
    other.execute("ROLLBACK")  # needs its journal: it must not have been zeroed
    other.close()
    with sqlite3.connect(plain_db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM users WHERE id='u2'").fetchone() == (0,)


def test_session_failure_in_cleanup_does_not_break_the_session(plain_db, monkeypatch, caplog):
    def boom(engine, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(dbmod, "scrub_rollback_journal", boom)
    doc_id = _add_doc([_filler(1)])
    with dbmod.get_session() as s:
        from openfoia.models import Document

        assert s.get(Document, doc_id) is not None
    assert "Could not clean up after the session" in caplog.text
