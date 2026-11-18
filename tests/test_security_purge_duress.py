"""Purge, duress and `db encrypt`: what is actually removed, encrypted or refused.

These back the November 2026 audit fixes (A1, A2, A6, A15, A18, and the
`db encrypt` FTS failure) with tests that go past the minimal reproductions in
test_audit_repros.py.
"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from openfoia.db import has_sqlcipher

runner = CliRunner()
needs_sqlcipher = pytest.mark.skipif(not has_sqlcipher(), reason="no SQLCipher driver installed")
SQLITE_HEADER = b"SQLite format 3\x00"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(d))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return d


def _open_encrypted(path, password):
    from openfoia.db import get_sqlcipher_driver, sqlcipher_key_pragma

    conn = get_sqlcipher_driver().connect(str(path))
    conn.execute(sqlcipher_key_pragma(password))
    conn.execute("PRAGMA cipher_compatibility = 4")
    return conn


def _add_document(session, text: str, pages: list[str] | None = None) -> str:
    from openfoia.doctext import replace_pages
    from openfoia.models import (
        Agency,
        DeliveryMethod,
        Document,
        DocumentType,
        Entity,
        EntityType,
        Request,
        User,
    )

    user = User(id=str(uuid4()), email=f"{uuid4().hex[:6]}@example.org", name="L")
    session.add(user)
    session.flush()
    req = Request(
        id=str(uuid4()),
        request_number=f"R-{uuid4().hex[:6]}",
        requester_id=user.id,
        agency_id=session.query(Agency).first().id,
        subject="s",
        body="",
        delivery_method=DeliveryMethod.EMAIL,
    )
    session.add(req)
    session.flush()
    doc = Document(
        id=str(uuid4()),
        request_id=req.id,
        doc_type=DocumentType.FULL_RESPONSE,
        filename="memo.txt",
        file_path="",
        file_size=0,
        mime_type="text/plain",
        extracted_text=text,
    )
    session.add(doc)
    session.flush()
    replace_pages(session, doc.id, pages or [text])
    session.add(
        Entity(
            id=str(uuid4()),
            document_id=doc.id,
            entity_type=EntityType.PERSON,
            raw_text="Jane Doe",
            normalized_text="jane doe",
            context=text[:200],
        )
    )
    return doc.id


# ---------------------------------------------------------------------------
# `openfoia db encrypt` on a database with stored pages (FTS5)
# ---------------------------------------------------------------------------


def _plaintext_db_with_pages(data_dir) -> Path:
    from openfoia.db import get_db_path, get_engine, get_session, init_db

    init_db()
    with get_session() as s:
        _add_document(s, "Minister Alvarez signed the contract.", ["Minister Alvarez", "signed"])
    db = get_db_path()
    # Tables, triggers and views a later feature might add (extraction runs,
    # page vectors): the copy must carry whatever is there, not a fixed list.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE future_feature (id INTEGER PRIMARY KEY, payload BLOB NOT NULL);
        INSERT INTO future_feature (payload) VALUES (x'00ff10');
        CREATE INDEX ix_future_payload ON future_feature(payload);
        CREATE VIEW future_view AS SELECT count(*) AS n FROM future_feature;
        CREATE TRIGGER future_ad AFTER DELETE ON document_pages
            BEGIN DELETE FROM future_feature; END;
        """
    )
    conn.commit()
    conn.close()
    get_engine().dispose()
    return db


@needs_sqlcipher
def test_db_encrypt_handles_stored_pages_and_extra_tables(data_dir):
    from openfoia.cli import app

    db = _plaintext_db_with_pages(data_dir)
    assert db.read_bytes()[:16] == SQLITE_HEADER
    before = {r[0] for r in sqlite3.connect(str(db)).execute("SELECT name FROM sqlite_master")}
    assert "document_pages_fts" in before

    result = runner.invoke(app, ["db", "encrypt"], input="it's-secret\nit's-secret\n")
    assert result.exit_code == 0, result.output

    raw = db.read_bytes()
    assert raw[:16] != SQLITE_HEADER
    assert b"Alvarez" not in raw
    assert not list(data_dir.glob(".encrypting_*"))

    conn = _open_encrypted(db, "it's-secret")
    after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert before <= after, sorted(before - after)
    # Full-text search works on the encrypted copy...
    hits = conn.execute(
        "SELECT rowid FROM document_pages_fts WHERE document_pages_fts MATCH 'alvarez'"
    ).fetchall()
    assert len(hits) == 1
    conn.execute("INSERT INTO document_pages_fts(document_pages_fts) VALUES ('integrity-check')")
    # ...and the triggers that keep it in step survived too.
    doc_id = conn.execute("SELECT document_id FROM document_pages LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO document_pages (document_id, page_number, text, char_count) "
        "VALUES (?, 99, ?, 25)",
        (doc_id, "Second contractor Okonkwo"),
    )
    assert conn.execute(
        "SELECT count(*) FROM document_pages_fts WHERE document_pages_fts MATCH 'okonkwo'"
    ).fetchone() == (1,)
    assert conn.execute("SELECT payload FROM future_feature").fetchone() == (b"\x00\xff\x10",)
    assert conn.execute("SELECT n FROM future_view").fetchone() == (1,)
    conn.execute("DELETE FROM document_pages")
    assert conn.execute("SELECT count(*) FROM future_feature").fetchone() == (0,)
    conn.rollback()
    conn.close()


@needs_sqlcipher
def test_db_encrypt_then_orm_search_works(data_dir):
    from openfoia.db import encrypt_database, get_engine, get_session
    from openfoia.doctext import search_pages

    _plaintext_db_with_pages(data_dir)
    encrypt_database("pw-one")
    get_engine().dispose()
    with get_session(password="pw-one") as s:
        hits, engine = search_pages(s, "Alvarez")
    assert engine.startswith("fts5")
    assert len(hits) == 1


@needs_sqlcipher
def test_db_encrypt_shreds_the_plaintext_wal_instead_of_letting_sqlite_unlink_it(
    data_dir, monkeypatch
):
    """A read-write connection checkpoints a -wal on close and plain-unlinks it.

    The plaintext must be attached read-only so the WAL is still there for the
    shredder, and every plaintext file must go through secure_delete.
    """
    import shutil

    from openfoia import security
    from openfoia.db import encrypt_database, get_db_path, get_engine, init_db

    init_db(seed=False)
    get_engine().dispose()
    db = get_db_path()
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE notes (t TEXT)")
    conn.execute("INSERT INTO notes VALUES ('WAL-ONLY-SECRET')")
    conn.commit()
    # Keep a copy of the files while the WAL is un-checkpointed (as after a crash).
    snap = data_dir.parent / "snap"
    snap.mkdir()
    for f in data_dir.glob("data.db*"):
        shutil.copy(f, snap / f.name)
    conn.close()
    for f in snap.iterdir():
        shutil.copy(f, data_dir / f.name)
    wal = Path(str(db) + "-wal")
    assert b"WAL-ONLY-SECRET" in wal.read_bytes()
    assert b"WAL-ONLY-SECRET" not in db.read_bytes()

    shredded: list[str] = []
    real = security.secure_delete

    def spy(path):
        shredded.append(Path(path).name)
        real(path)

    monkeypatch.setattr(security, "secure_delete", spy)
    encrypt_database("pw")

    assert {"data.db", "data.db-wal", "data.db-shm"} <= set(shredded), shredded
    assert sorted(p.name for p in data_dir.glob("data.db*")) == ["data.db"]
    c = _open_encrypted(db, "pw")
    assert c.execute("SELECT t FROM notes").fetchone() == ("WAL-ONLY-SECRET",)
    c.close()


@needs_sqlcipher
def test_db_encrypt_refuses_an_already_encrypted_database(data_dir):
    from openfoia.db import encrypt_database, get_db_path, init_db

    init_db(seed=False, password="first")
    before = get_db_path(password="first").read_bytes()
    with pytest.raises(RuntimeError, match="not a plaintext database"):
        encrypt_database("second")
    assert get_db_path(password="first").read_bytes() == before


# ---------------------------------------------------------------------------
# A1: duress mode never sits next to a plaintext real database
# ---------------------------------------------------------------------------


@needs_sqlcipher
def test_init_duress_without_real_passphrase_creates_nothing(data_dir):
    from openfoia.cli import app

    result = runner.invoke(app, ["init", "--duress-password", "decoy-pass"])
    assert result.exit_code == 1
    assert "Duress mode configured" not in result.output
    assert not list(data_dir.glob("*.db*"))


@needs_sqlcipher
def test_init_duress_on_existing_plaintext_db_refuses(data_dir):
    from openfoia.cli import app
    from openfoia.db import get_engine, init_db

    init_db(seed=False)
    get_engine().dispose()
    result = runner.invoke(app, ["init", "--duress-password", "decoy-pass"])
    assert result.exit_code == 1, result.output
    assert "NOT configured" in result.output
    assert "not encrypted" in result.output
    assert sorted(p.name for p in data_dir.glob("*.db")) == ["data.db"]


@needs_sqlcipher
def test_setup_duress_mode_refuses_plaintext_and_missing_real_db(data_dir):
    from openfoia.db import get_engine, init_db
    from openfoia.security import DuressSetupError, setup_duress_mode

    with pytest.raises(DuressSetupError, match="no database"):
        setup_duress_mode("decoy")
    init_db(seed=False)
    get_engine().dispose()
    with pytest.raises(DuressSetupError, match="not encrypted"):
        setup_duress_mode("decoy")
    assert not (data_dir / "profile_1.db").exists()


@needs_sqlcipher
def test_duress_added_after_db_encrypt_works(data_dir):
    """The documented path for an existing plaintext DB: encrypt, then add the decoy."""
    from openfoia.db import encrypt_database, get_engine, init_db
    from openfoia.security import is_encrypted_db, setup_duress_mode

    init_db(seed=False)
    get_engine().dispose()
    encrypt_database("real-pass")
    decoy = setup_duress_mode("decoy-pass")
    assert is_encrypted_db(data_dir / "profile_0.db")
    assert is_encrypted_db(decoy)


@needs_sqlcipher
def test_init_duress_recreates_the_decoy_on_an_encrypted_setup(data_dir):
    """The CHANGELOG tells users to re-create an old decoy with `init --duress`."""
    from openfoia.cli import app
    from openfoia.security import _can_open_db

    assert (
        runner.invoke(app, ["init", "--password", "real", "--duress-password", "old"]).exit_code
        == 0
    )
    result = runner.invoke(app, ["init", "--duress"], input="new-decoy\nnew-decoy\n")
    assert result.exit_code == 0, result.output
    assert "Duress mode configured" in result.output
    assert _can_open_db(data_dir / "profile_1.db", "new-decoy")
    assert not _can_open_db(data_dir / "profile_1.db", "old")
    assert _can_open_db(data_dir / "profile_0.db", "real")


@needs_sqlcipher
def test_duress_passphrase_that_opens_the_real_db_is_refused(data_dir):
    from openfoia.db import init_db
    from openfoia.security import DuressSetupError, setup_duress_mode

    init_db(seed=False, password="same")
    with pytest.raises(DuressSetupError, match="opens the real database"):
        setup_duress_mode("same")


@needs_sqlcipher
def test_cli_duress_success_is_only_printed_after_checking_both_files(data_dir, monkeypatch):
    """If the decoy somehow came out plaintext, success must not be printed."""
    from openfoia import security
    from openfoia.cli import app

    real_seed = security.seed_decoy_db

    def plaintext_decoy(path, password=None):
        real_seed(path, password=password)
        Path(path).write_bytes(SQLITE_HEADER + b"\x00" * 4080)

    monkeypatch.setattr(security, "seed_decoy_db", plaintext_decoy)
    result = runner.invoke(app, ["init", "--password", "real", "--duress-password", "decoy"])
    assert result.exit_code == 1
    assert "Duress mode configured" not in result.output
    assert "profile_1.db is not encrypted" in " ".join(result.output.split())
    assert not (data_dir / "profile_1.db").exists()


# ---------------------------------------------------------------------------
# A6: the decoy is built like a real database
# ---------------------------------------------------------------------------


@needs_sqlcipher
def test_decoy_matches_real_db_in_mode_migrations_fts_and_agencies(data_dir):
    from openfoia.cli import app

    result = runner.invoke(app, ["init", "--password", "real", "--duress-password", "decoy"])
    assert result.exit_code == 0, result.output
    real, decoy = data_dir / "profile_0.db", data_dir / "profile_1.db"
    assert stat.S_IMODE(real.stat().st_mode) == stat.S_IMODE(decoy.stat().st_mode) == 0o600

    def facts(path, pw):
        c = _open_encrypted(path, pw)
        out = {
            "schema": sorted((t, n) for t, n in c.execute("SELECT type, name FROM sqlite_master")),
            "version": c.execute("SELECT version_num FROM alembic_version").fetchall(),
            "agencies": c.execute("SELECT count(*) FROM agencies").fetchone()[0],
        }
        c.close()
        return out

    r, d = facts(real, "real"), facts(decoy, "decoy")
    assert r["schema"] == d["schema"]
    assert r["version"] == d["version"]
    assert r["agencies"] == d["agencies"] > 10


@needs_sqlcipher
def test_decoy_content_is_randomized_and_spread_over_time(tmp_path, monkeypatch):
    from openfoia.security import seed_decoy_db

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "d"))

    def content(path):
        c = _open_encrypted(path, "decoy")
        subjects = [r[0] for r in c.execute("SELECT subject FROM requests")]
        created = [r[0] for r in c.execute("SELECT created_at FROM requests")]
        c.close()
        return subjects, created

    runs = []
    for name in ("a.db", "b.db", "c.db"):
        seed_decoy_db(tmp_path / "d" / name, password="decoy")
        runs.append(content(tmp_path / "d" / name))
    assert all(subjects for subjects, _ in runs)
    # Same generator, different databases: the chosen topics, places and years differ.
    assert len({tuple(subjects) for subjects, _ in runs}) > 1
    ca = runs[0][1]
    assert len({c[:10] for c in ca}) > 1, "all decoy requests created on one day"


def test_threat_model_says_the_decoy_generator_is_public():
    text = (Path(__file__).resolve().parent.parent / "docs" / "THREAT_MODEL.md").read_text()
    assert "decoy" in text.lower()
    assert "public" in text.lower() and "seed_decoy_db" in text


# ---------------------------------------------------------------------------
# A2: one unwritable file never stops secure purge
# ---------------------------------------------------------------------------


def test_secure_delete_dir_reports_and_continues_past_a_failing_file(tmp_path, monkeypatch):
    from openfoia import security

    root = tmp_path / "d"
    (root / "sub").mkdir(parents=True)
    for name in ("a.txt", "sub/b.txt", "sub/stuck.txt", "z.txt"):
        (root / name).write_text("secret " + name)

    real_open = open

    def flaky_open(path, mode="r", *a, **k):
        if Path(path).name == "stuck.txt" and "+" in mode:
            raise OSError(5, "Input/output error", str(path))
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(security, "open", flaky_open, raising=False)
    report = security.secure_delete_dir(root)

    assert not root.exists()
    assert len(report.shredded) == 3
    assert [Path(p).name for p, _ in report.unlinked_only] == ["stuck.txt"]
    assert "Input/output error" in report.unlinked_only[0][1]
    assert report.survived == []
    assert not report.ok


def test_secure_delete_makes_a_read_only_file_writable_and_overwrites_it(tmp_path, monkeypatch):
    from openfoia import security

    f = tmp_path / "ro.txt"
    f.write_bytes(b"SOURCE-NAME" * 100)
    f.chmod(0o444)
    writes: list[bytes] = []
    real_open = open

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def write(self, b):
            writes.append(b)
            return self.fh.write(b)

        def __getattr__(self, name):
            return getattr(self.fh, name)

    def spy_open(path, mode="r", *a, **k):
        if "+" in mode and not Path(path).stat().st_mode & 0o200:
            raise PermissionError(13, "Permission denied", str(path))
        return Spy(real_open(path, mode, *a, **k))

    monkeypatch.setattr(security, "open", spy_open, raising=False)
    security.secure_delete(f)
    assert not f.exists()
    assert sum(len(w) for w in writes) == 3 * 1100


def test_secure_delete_dir_does_not_follow_symlinks(tmp_path):
    from openfoia.security import secure_delete_dir

    outside = tmp_path / "outside.txt"
    outside.write_text("not ours")
    root = tmp_path / "d"
    root.mkdir()
    (root / "link").symlink_to(outside)
    (root / "dirlink").symlink_to(tmp_path)
    secure_delete_dir(root)
    assert outside.read_text() == "not ours"
    assert not root.exists()


def test_ingested_copies_are_owner_only(tmp_path):
    import asyncio

    from openfoia.pipeline.ingest import DocumentIngester

    original = tmp_path / "leak.txt"
    original.write_text("text")
    original.chmod(0o444)
    result = asyncio.run(DocumentIngester(tmp_path / "docs").ingest_file(original))
    assert stat.S_IMODE(Path(result.file_path).stat().st_mode) == 0o600
    content = asyncio.run(DocumentIngester(tmp_path / "docs").ingest_bytes(b"x", "b.txt"))
    assert stat.S_IMODE(Path(content.file_path).stat().st_mode) == 0o600


def test_purge_secure_lists_survivors_still_scrubs_history_and_exits_nonzero(data_dir, monkeypatch):
    from openfoia import security
    from openfoia.cli import app

    (data_dir / "docs").mkdir(parents=True)
    (data_dir / "docs" / "ok.txt").write_text("fine")
    stuck = data_dir / "docs" / "stuck.txt"
    stuck.write_text("secret")
    hist = Path.home() / ".bash_history"
    hist.write_text("ls\nopenfoia request new --subject 'Minister X'\n")

    def fail_one(path, report):
        path = Path(path)
        if path.name == "stuck.txt":
            report.survived.append((str(path), "Operation not permitted"))
            return
        real_shred(path, report)

    real_shred = security._shred_into
    monkeypatch.setattr(security, "_shred_into", fail_one)
    monkeypatch.setattr(security, "print_ssd_warning", lambda: None)

    result = runner.invoke(app, ["purge", "--secure", "--yes"])
    assert result.exit_code == 1, result.output
    out = " ".join(result.output.split())
    assert "could NOT be removed" in out
    assert "stuck.txt" in out
    assert "Purge incomplete" in out
    assert "Minister X" not in hist.read_text()
    assert stuck.exists()


def test_purge_secure_survives_an_exception_and_still_scrubs_history(data_dir, monkeypatch):
    from openfoia import security
    from openfoia.cli import app

    data_dir.mkdir(parents=True)
    (data_dir / "x.txt").write_text("x")
    hist = Path.home() / ".bash_history"
    hist.write_text("openfoia docs ingest leak.pdf\n")

    def boom(_path):
        raise RuntimeError("disk vanished")

    monkeypatch.setattr(security, "secure_delete_dir", boom)
    monkeypatch.setattr(security, "print_ssd_warning", lambda: None)
    result = runner.invoke(app, ["purge", "--secure", "--yes"])
    assert result.exit_code != 0
    assert hist.read_text() == ""


# ---------------------------------------------------------------------------
# A15: history scrub
# ---------------------------------------------------------------------------


def test_history_scrub_covers_shells_wrappers_and_histfile(tmp_path, monkeypatch):
    from openfoia.security import clear_shell_history

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    custom = tmp_path / "elsewhere" / "my_history"
    custom.parent.mkdir()
    monkeypatch.setenv("HISTFILE", str(custom))

    (tmp_path / ".bash_history").write_text(
        "ls\n"
        "python -m openfoia docs ingest leak.pdf\n"
        "OPENFOIA_DB_PASSWORD=x ls\n"
        "./run.sh request new \\\n"
        "  --subject 'Minister X'\n"
        "/media/usb/run.sh search Minister\n"
        "cat notes.txt\n"
        "sh rerun.sh\n"
    )
    (tmp_path / ".zsh_history").write_text(
        ": 1700000000:0;openfoia search Minister\n: 1700000001:0;git status\n"
    )
    custom.write_text("openfoia init --encrypt\npwd\n")
    fish = tmp_path / ".local" / "share" / "fish"
    fish.mkdir(parents=True)
    (fish / "fish_history").write_text(
        "- cmd: openfoia request new --subject Minister\n"
        "  when: 1700000000\n"
        "  paths:\n"
        "    - leak.pdf\n"
        "- cmd: ls\n"
        "  when: 1700000001\n"
    )

    modified = clear_shell_history()
    assert len(modified) == 4
    assert (tmp_path / ".bash_history").read_text() == "ls\ncat notes.txt\nsh rerun.sh\n"
    assert (tmp_path / ".zsh_history").read_text() == ": 1700000001:0;git status\n"
    assert custom.read_text() == "pwd\n"
    assert (fish / "fish_history").read_text() == "- cmd: ls\n  when: 1700000001\n"


def test_purge_tells_the_user_about_the_live_shell(data_dir, monkeypatch):
    from openfoia import security
    from openfoia.cli import app

    data_dir.mkdir(parents=True)
    monkeypatch.setattr(security, "print_ssd_warning", lambda: None)
    result = runner.invoke(app, ["purge", "--secure", "--yes"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "unset HISTFILE" in out


# ---------------------------------------------------------------------------
# A18: exports land in the data directory or are recorded for purge
# ---------------------------------------------------------------------------


def test_default_exports_go_to_the_data_dir(data_dir, tmp_path, monkeypatch):
    from openfoia.cli import app
    from openfoia.db import get_session, init_db

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    init_db()
    with get_session() as s:
        _add_document(s, "Jane Doe met the minister.")
    assert runner.invoke(app, ["analyze", "graph", "--all"]).exit_code == 0
    assert runner.invoke(app, ["analyze", "export"]).exit_code == 0
    assert list(work.iterdir()) == []
    exports = data_dir / "exports"
    assert (exports / "graph.json").exists()
    assert (exports / "entities.ftm.json").exists()
    assert stat.S_IMODE(exports.stat().st_mode) == 0o700


def test_explicit_export_outside_is_listed_and_removed_by_purge(data_dir, tmp_path, monkeypatch):
    from openfoia.cli import app
    from openfoia.db import get_session, init_db

    init_db()
    with get_session() as s:
        _add_document(s, "Jane Doe met the minister.")
    out = tmp_path / "desk" / "g.json"
    out.parent.mkdir()
    assert runner.invoke(app, ["analyze", "graph", "--all", "-o", str(out)]).exit_code == 0
    assert out.exists()
    keep = tmp_path / "desk" / "unrelated.txt"
    keep.write_text("mine")

    prompt = runner.invoke(app, ["purge"], input="n\n")
    assert str(out) in " ".join(prompt.output.split())
    assert out.exists()

    monkeypatch.setattr("openfoia.security.print_ssd_warning", lambda: None)
    result = runner.invoke(app, ["purge", "--secure", "--yes"])
    assert result.exit_code == 0, result.output
    assert not out.exists()
    assert keep.read_text() == "mine"
    assert not data_dir.exists()


def test_purge_prompt_names_what_it_does_not_remove(data_dir):
    from openfoia.cli import app

    data_dir.mkdir(parents=True)
    result = runner.invoke(app, ["purge"], input="n\n")
    out = " ".join(result.output.split())
    assert "Not removed" in out
    assert "original files you ingested" in out
    assert "not overwritten" in out  # fast mode is honest about recoverability


# ---------------------------------------------------------------------------
# init --force and OCR scratch files
# ---------------------------------------------------------------------------


def test_init_force_shreds_the_db_and_its_sidecars(data_dir, monkeypatch):
    from openfoia import security
    from openfoia.cli import app
    from openfoia.db import get_engine, init_db

    init_db(seed=False)
    get_engine().dispose()
    for suffix in ("-wal", "-shm"):
        (data_dir / f"data.db{suffix}").write_bytes(b"PLAINTEXT" * 100)
    shredded: list[str] = []
    real = security.secure_delete

    def spy(path):
        shredded.append(Path(path).name)
        real(path)

    monkeypatch.setattr(security, "secure_delete", spy)
    result = runner.invoke(app, ["init", "--force", "--no-seed"])
    assert result.exit_code == 0, result.output
    assert {"data.db", "data.db-wal", "data.db-shm"} <= set(shredded)
    assert not (data_dir / "data.db-wal").exists()
    assert "not removed" in " ".join(result.output.split())


@needs_sqlcipher
def test_init_force_removes_both_profiles(data_dir):
    from openfoia.cli import app

    assert runner.invoke(app, ["init", "--password", "r", "--duress-password", "d"]).exit_code == 0
    assert (data_dir / "profile_1.db").exists()
    result = runner.invoke(app, ["init", "--force", "--no-seed"])
    assert result.exit_code == 0, result.output
    assert not list(data_dir.glob("profile_*"))


def test_shredding_tempdir_overwrites_its_files(tmp_path, monkeypatch):
    from openfoia import security

    shredded: list[str] = []
    real = security.secure_delete

    def spy(path):
        shredded.append(Path(path).name)
        real(path)

    monkeypatch.setattr(security, "secure_delete", spy)
    with security.shredding_tempdir(tmp_path) as scratch:
        (scratch / "page-1.ppm").write_bytes(b"P6 image of a leaked page")
    assert shredded == ["page-1.ppm"]
    assert list(tmp_path.iterdir()) == []


def test_ocr_uses_shredding_tempdir_not_temporarydirectory():
    import inspect

    from openfoia.pipeline import ocr

    src = inspect.getsource(ocr)
    assert "TemporaryDirectory" not in src
    assert src.count("shredding_tempdir(get_ocr_temp_dir())") >= 3
