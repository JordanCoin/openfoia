"""Offline semantic search (docs grep --semantic, issue #118).

A tiny deterministic embedder stands in for the model, so nothing here
downloads anything. The purge, missing-extra, download and chunking tests run
on a core install; search tests need numpy or sqlite-vec and skip without
them; the SQLCipher tests need sqlcipher3 (and sqlite-vec for the load test).
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sqlite3
import sys
import zlib

import httpx
import pytest
from typer.testing import CliRunner

from openfoia import doctext, semantic

runner = CliRunner()

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
HAS_SQLITE_VEC = importlib.util.find_spec("sqlite_vec") is not None

# "Meaning" for the fake model: words in one group land in the same bucket.
_SYNONYMS = {
    "sibling": "brother",
    "funds": "payments",
    "transferred": "payments",
    "paid": "payments",
    "executive": "mayor",
    "patrolman": "officer",
    "dismissed": "fired",
}


class FakeEmbedder:
    """Bag of words hashed into 32 buckets, synonyms folded. Plain lists, no numpy."""

    name = "fake-test"
    dim = 32

    def encode(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"\w+", t.lower()):
                v[zlib.crc32(_SYNONYMS.get(w, w).encode()) % self.dim] += 1.0
            out.append(v)
        return out


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    monkeypatch.delenv("OPENFOIA_SEMANTIC_BACKEND", raising=False)
    from openfoia.db import init_db

    init_db()
    return tmp_path / "data"


def _add_doc(pages: list[str], title: str = "Minutes") -> str:
    from uuid import uuid4

    from openfoia.db import get_session
    from openfoia.models import Agency, DeliveryMethod, Document, DocumentType, Request, User

    with get_session() as s:
        user = s.query(User).first() or User(id=str(uuid4()), email="a@b.c", name="L")
        s.add(user)
        s.flush()
        req = Request(
            id=str(uuid4()),
            request_number=f"DC-{uuid4().hex[:6]}",
            requester_id=user.id,
            agency_id=s.query(Agency).first().id,
            subject=title,
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


def _embed_all() -> int:
    from openfoia.db import get_session

    with get_session() as s:
        return semantic.embed_missing(s, FakeEmbedder())


def _vector_rows(document_id: str | None = None) -> int:
    from openfoia.db import get_db_path

    conn = sqlite3.connect(get_db_path())
    try:
        if document_id is None:
            return conn.execute(f"SELECT count(*) FROM {semantic.VECTOR_TABLE}").fetchone()[0]
        return conn.execute(
            f"SELECT count(*) FROM {semantic.VECTOR_TABLE} WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
    finally:
        conn.close()


# --- purge: embeddings never outlive their text (no extras needed) ---------


def test_deleting_a_document_purges_its_vectors(db):
    keep = _add_doc(["the council approved the parks budget"])
    gone = _add_doc(["funds transferred to a sibling of the city executive", "page two"])
    assert _embed_all() == 3
    assert _vector_rows(gone) == 2

    from openfoia.db import get_session
    from openfoia.models import Document

    with get_session() as s:
        s.delete(s.get(Document, gone))

    assert _vector_rows(gone) == 0
    assert _vector_rows(keep) == 1


def test_vectors_purged_even_without_foreign_keys(db):
    """Triggers do the purge; they do not depend on PRAGMA foreign_keys."""
    doc = _add_doc(["one page of text", "another page"])
    _embed_all()
    from openfoia.db import get_db_path

    conn = sqlite3.connect(get_db_path())
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    conn.execute("DELETE FROM document_pages WHERE document_id = ? AND page_number = 1", (doc,))
    conn.commit()
    assert _vector_rows(doc) == 1
    conn.execute("DELETE FROM documents WHERE id = ?", (doc,))
    conn.commit()
    conn.close()
    assert _vector_rows(doc) == 0


def test_replaced_or_edited_pages_lose_stale_vectors(db):
    doc = _add_doc(["old text about payments", "second page"])
    _embed_all()
    from openfoia.db import get_db_path, get_session

    with get_session() as s:
        doctext.replace_pages(s, doc, ["new OCR text"])
    assert _vector_rows(doc) == 0

    _embed_all()
    conn = sqlite3.connect(get_db_path())
    conn.execute("UPDATE document_pages SET text = 'corrected' WHERE document_id = ?", (doc,))
    conn.commit()
    conn.close()
    assert _vector_rows(doc) == 0


def test_lost_triggers_are_restored_and_orphans_swept(db):
    """A batch migration that rebuilds document_pages drops its triggers."""
    doc = _add_doc(["a page", "b page"])
    _embed_all()
    from openfoia.db import get_db_path, get_session

    conn = sqlite3.connect(get_db_path())
    for t in ("document_pages_vec_ad", "document_pages_vec_au", "documents_vec_ad"):
        conn.execute(f"DROP TRIGGER {t}")
    conn.execute("DELETE FROM document_pages WHERE document_id = ? AND page_number = 2", (doc,))
    conn.commit()
    conn.close()
    assert _vector_rows(doc) == 2  # orphaned: no trigger, no FK enforcement

    with get_session() as s:
        semantic.ensure_vector_table(s.connection())
    assert _vector_rows(doc) == 1


def test_embed_drop_cli_removes_every_vector(db):
    from openfoia.cli import app

    _add_doc(["x y z"])
    _embed_all()
    result = runner.invoke(app, ["docs", "embed", "--drop", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Deleted 1" in result.output
    assert _vector_rows() == 0


def test_purge_removes_the_vectors_and_the_model(db):
    """`purge` deletes the data dir: database (with its vectors) and models/."""
    from openfoia.cli import app

    _add_doc(["text"])
    _embed_all()
    model_dir = semantic.model_path("potion-base-8M")
    model_dir.mkdir(parents=True)
    (model_dir / "model.safetensors").write_bytes(b"x" * 10)

    result = runner.invoke(app, ["purge", "--yes"])
    assert result.exit_code == 0, result.output
    assert not db.exists()


def test_secure_purge_shreds_the_database_holding_vectors(db, monkeypatch, tmp_path):
    from openfoia.cli import app

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    _add_doc(["text"])
    _embed_all()
    result = runner.invoke(app, ["purge", "--secure", "--yes"])
    assert result.exit_code == 0, result.output
    assert not db.exists()


def test_decoy_profile_carries_no_vectors(tmp_path, monkeypatch):
    """The duress decoy is a fresh database: no copy of the real embeddings."""
    from openfoia.db import get_sqlcipher_driver, has_sqlcipher, sqlcipher_key_pragma
    from openfoia.security import seed_decoy_db

    if not has_sqlcipher():
        pytest.skip("no SQLCipher driver installed (the decoy is always encrypted)")
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    decoy = tmp_path / "profile_1.db"
    seed_decoy_db(decoy, password="decoy-passphrase")
    conn = get_sqlcipher_driver().connect(str(decoy))
    try:
        conn.execute(sqlcipher_key_pragma("decoy-passphrase"))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert "documents" in tables  # opened with the key: this is the real schema
    assert "document_page_vectors" not in tables


# --- missing extra / model: a hint, never a crash (no extras needed) -------


def test_missing_extra_gives_install_hint(db, monkeypatch):
    from openfoia.cli import app

    monkeypatch.setitem(sys.modules, "model2vec", None)  # import model2vec -> ImportError
    _add_doc(["anything"])
    result = runner.invoke(app, ["docs", "grep", "--semantic", "payments"])
    assert result.exit_code == 1
    assert "openfoia install-extras semantic" in result.output

    # Keyword grep and the status report keep working without the extra.
    assert runner.invoke(app, ["docs", "grep", "anything"]).exit_code == 0
    status = runner.invoke(app, ["docs", "embed", "--status"])
    assert status.exit_code == 0, status.output
    assert "Pages with text: 1" in status.output


def test_core_modules_do_not_import_the_extra():
    import subprocess

    code = (
        "import sys; import openfoia.cli, openfoia.semantic, openfoia.doctext; "
        "bad = [m for m in ('model2vec', 'sqlite_vec', 'numpy', 'huggingface_hub') "
        "if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.skipif(importlib.util.find_spec("model2vec") is None, reason="model2vec not installed")
def test_real_loader_never_reaches_the_hub_for_a_missing_model(db):
    """No model downloaded: a clear error, and no socket opened (conftest blocks them)."""
    with pytest.raises(semantic.SemanticUnavailable, match="not downloaded"):
        semantic.load_embedder("potion-base-8M")


def test_missing_model_names_the_download_command(db, monkeypatch):
    monkeypatch.setattr(semantic, "_import_model2vec", lambda: object)
    with pytest.raises(semantic.SemanticUnavailable, match="openfoia docs embed-model"):
        semantic.load_embedder()


# --- model download: explicit, pinned, verified, under the data dir --------

_BLOB = b"tiny model bytes"


@pytest.fixture
def tiny_spec(monkeypatch):
    spec = semantic.ModelSpec(
        name="tiny",
        repo="example/tiny",
        revision="0" * 40,
        files={"model.safetensors": (len(_BLOB), hashlib.sha256(_BLOB).hexdigest())},
        languages="none",
        about="test",
    )
    monkeypatch.setitem(semantic.MODELS, "tiny", spec)
    return spec


def test_download_verifies_and_stores_under_data_dir(db, tiny_spec):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=_BLOB)

    folder = semantic.download_model("tiny", transport=httpx.MockTransport(handler))
    assert folder == db / "models" / "tiny"
    assert (folder / "model.safetensors").read_bytes() == _BLOB
    assert semantic.model_installed("tiny")
    assert semantic.verify_model("tiny") == []
    assert seen == [f"https://huggingface.co/example/tiny/resolve/{'0' * 40}/model.safetensors"]


def test_download_with_wrong_hash_installs_nothing(db, tiny_spec):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"tampered bytes!!"))
    with pytest.raises(RuntimeError, match="SHA-256"):
        semantic.download_model("tiny", transport=transport)
    folder = db / "models" / "tiny"
    assert not (folder / "model.safetensors").exists()
    assert not (folder / "model.safetensors.part").exists()
    assert not semantic.model_installed("tiny")


def test_embed_model_cli_asks_first_and_cancel_sends_nothing(db):
    from openfoia.cli import app

    result = runner.invoke(app, ["docs", "embed-model"], input="n\n")
    assert result.exit_code == 1
    assert "huggingface.co" in result.output
    assert "Nothing from your documents" in result.output
    assert "Nothing was sent" in result.output
    assert runner.invoke(app, ["docs", "embed-model", "--list"]).exit_code == 0
    assert not (db / "models").exists()  # looking leaves no trace in the data dir


# --- chunking ---------------------------------------------------------------


def test_chunks_overlap_and_cover_the_page():
    text = " ".join(f"w{i}" for i in range(100))
    spans = semantic.chunk_spans(text, words=40, overlap=10)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert text[spans[1][0] : spans[1][1]].split()[0] == "w30"
    assert semantic.chunk_spans("   ") == []


def test_text_without_spaces_is_still_chunked():
    cjk = "市长的兄弟收到了资金" * 20  # 200 characters, no spaces
    spans = semantic.chunk_spans(cjk, words=40, overlap=10)
    assert len(spans) > 1
    assert all(e - s <= 80 for s, e in spans)


def test_vectors_round_trip_as_float32_little_endian():
    blob = semantic.pack([1.0, -2.5, 0.0])
    assert len(blob) == 12
    assert semantic.unpack(blob) == [1.0, -2.5, 0.0]


# --- search (needs numpy or sqlite-vec) -------------------------------------

BACKENDS = [
    pytest.param("numpy", marks=pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")),
    pytest.param(
        "sqlite-vec",
        marks=pytest.mark.skipif(not HAS_SQLITE_VEC, reason="sqlite-vec not installed"),
    ),
]


@pytest.mark.parametrize("backend", BACKENDS)
def test_search_ranks_the_paraphrase_first(db, backend):
    from openfoia.db import get_session

    if backend == "sqlite-vec":
        with get_session() as s:
            ok, why = semantic.load_sqlite_vec(s.connection())
        if not ok:
            pytest.skip(why)
    _add_doc(["The council approved the parks budget for the summer."], "Parks")
    target = _add_doc(
        ["Weather was mild.", "Funds were transferred to a sibling of the city executive."],
        "Ledger",
    )
    _embed_all()
    with get_session() as s:
        hits, used, _ = semantic.semantic_search(
            s, "payments to the mayor's brother", FakeEmbedder(), limit=3, backend=backend
        )
    assert used == backend
    assert (hits[0].document_id, hits[0].page) == (target, 2)
    assert hits[0].title == "Ledger"
    assert "sibling" in hits[0].passage
    assert hits[0].score > hits[-1].score


@pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")
def test_falls_back_to_numpy_and_says_why(db, monkeypatch):
    from openfoia.db import get_session

    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    _add_doc(["funds transferred to a sibling"])
    _embed_all()
    with get_session() as s:
        hits, used, why = semantic.semantic_search(s, "payments brother", FakeEmbedder())
    assert used == "numpy"
    assert "not installed" in why
    assert len(hits) == 1


def test_python_without_extension_loading_is_reported(monkeypatch):
    """macOS system Python's sqlite3 has no enable_load_extension."""

    class _NoLoad:
        def execute(self, *_):
            raise sqlite3.OperationalError("no such function: vec_version")

    class _Conn:
        class connection:
            dbapi_connection = _NoLoad()

    monkeypatch.setitem(sys.modules, "sqlite_vec", type(sys)("sqlite_vec"))
    ok, why = semantic.load_sqlite_vec(_Conn())
    assert not ok
    assert "cannot load extensions" in why


@pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")
def test_search_respects_document_filter(db):
    from openfoia.db import get_session

    a = _add_doc(["payments to the brother of the mayor"])
    b = _add_doc(["payments to the brother"])
    _embed_all()
    with get_session() as s:
        hits, _, _ = semantic.semantic_search(
            s, "payments brother mayor", FakeEmbedder(), document_ids=[b], backend="numpy"
        )
    assert {h.document_id for h in hits} == {b}
    assert a not in {h.document_id for h in hits}


@pytest.mark.skipif(not (HAS_NUMPY or HAS_SQLITE_VEC), reason="no search backend installed")
def test_cli_semantic_grep_embeds_new_pages_and_says_which_path(db, monkeypatch):
    from openfoia.cli import app

    monkeypatch.setattr(semantic, "load_embedder", lambda name=None: FakeEmbedder())
    doc = _add_doc(["Funds were transferred to a sibling of the city executive."])
    result = runner.invoke(app, ["docs", "grep", "--semantic", "payments to the mayor's brother"])
    assert result.exit_code == 0, result.output
    assert "Embedding 1 page(s)" in result.output
    assert "Semantic search:" in result.output
    assert doc[:8] in result.output
    assert "score" in result.output
    assert _vector_rows(doc) == 1


# --- SQLCipher ---------------------------------------------------------------

HAS_SQLCIPHER = importlib.util.find_spec("sqlcipher3") is not None


@pytest.mark.skipif(not (HAS_SQLCIPHER and HAS_SQLITE_VEC), reason="needs sqlcipher3 + sqlite-vec")
def test_sqlite_vec_loads_and_searches_under_sqlcipher(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENFOIA_DB_PASSWORD", "correct horse battery staple")
    from openfoia.db import get_db_path, get_session, init_db

    init_db()
    target = _add_doc(["Funds were transferred to a sibling of the city executive."])
    _add_doc(["The parks budget was approved."])
    _embed_all()
    with get_session() as s:
        assert type(s.connection().connection.dbapi_connection).__module__.startswith("sqlcipher3")
        hits, used, why = semantic.semantic_search(
            s, "payments to the mayor's brother", FakeEmbedder(), backend="sqlite-vec"
        )
    assert used == "sqlite-vec", why
    assert hits[0].document_id == target

    raw = get_db_path().read_bytes()
    assert not raw.startswith(b"SQLite format 3")
    assert b"sibling" not in raw
    vec = semantic.pack(FakeEmbedder().encode(["x"])[0])
    assert vec not in raw


@pytest.mark.skipif(not HAS_SQLCIPHER, reason="needs sqlcipher3")
def test_db_encrypt_carries_vectors_and_purge_triggers(tmp_path, monkeypatch):
    """`db encrypt` replays iterdump(); the plain vector table survives it."""
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    from openfoia.db import encrypt_database, get_session, init_db

    init_db()
    doc = _add_doc(["page one", "page two"])
    _embed_all()
    # Pre-existing, separate bug: the FTS5 virtual table does not survive the
    # iterdump() replay ("no such table: document_pages_fts"), so encrypt fails
    # on any database with pages. Take FTS out to test only the vector table.
    from openfoia.db import get_db_path

    conn = sqlite3.connect(get_db_path())
    for t in ("document_pages_ai", "document_pages_ad", "document_pages_au"):
        conn.execute(f"DROP TRIGGER {t}")
    conn.execute(f"DROP TABLE {doctext.FTS_TABLE}")
    conn.commit()
    conn.close()
    encrypt_database("pw")
    monkeypatch.setenv("OPENFOIA_DB_PASSWORD", "pw")
    with get_session() as s:
        conn = s.connection()
        n = conn.exec_driver_sql(f"SELECT count(*) FROM {semantic.VECTOR_TABLE}").scalar()
        assert n == 2
        conn.exec_driver_sql("DELETE FROM documents WHERE id = ?", (doc,))
        n = conn.exec_driver_sql(f"SELECT count(*) FROM {semantic.VECTOR_TABLE}").scalar()
        assert n == 0
