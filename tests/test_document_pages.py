"""Per-page document text, the local FTS5 index, and docs grep/show."""

from __future__ import annotations

import asyncio
import json
import stat

import httpx
import pytest
from typer.testing import CliRunner

from openfoia import doctext

runner = CliRunner()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    from openfoia.db import init_db

    init_db()


def _add_doc(pages: list[str], title: str = "Board minutes", page_count: int | None = None) -> str:
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
            page_count=page_count,
            extracted_text=doctext.join_pages(pages),
        )
        s.add(doc)
        doctext.replace_pages(s, doc.id, pages)
        return doc.id


def _pages(doc_id: str) -> list[tuple[int, str]]:
    from openfoia.db import get_session
    from openfoia.models import DocumentPage

    with get_session() as s:
        rows = s.query(DocumentPage).filter_by(document_id=doc_id).order_by("page_number")
        return [(p.page_number, p.text) for p in rows]


# --- model + storage -------------------------------------------------------


def test_replace_pages_numbers_from_one_and_replaces(db):
    doc_id = _add_doc(["first", "second"])
    assert _pages(doc_id) == [(1, "first"), (2, "second")]

    from openfoia.db import get_session

    with get_session() as s:
        doctext.replace_pages(s, doc_id, ["only"])
    assert _pages(doc_id) == [(1, "only")]


def test_split_pages_on_form_feed():
    assert doctext.split_pages("one\ftwo\f") == ["one", "two"]
    assert doctext.split_pages("one\f\ftwo") == ["one", "", "two"]
    assert doctext.split_pages("no breaks") == ["no breaks"]


def test_pdf_extract_keeps_pages(tmp_path):
    from openfoia.pipeline.pdf_extract import extract_text

    fake = tmp_path / "pdf-extract"
    fake.write_text("#!/bin/sh\nprintf 'page one text\\fpage two text\\f'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    result = asyncio.run(extract_text(pdf, binary_path=str(fake), min_chars=1))
    assert result.pages == ["page one text", "page two text"]
    assert result.page_count == 2


# --- DocumentCloud page fetch (mocked transport, no network) ---------------


def _dc_transport(fail_page: int | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/documents/42/":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "slug": "memo",
                    "title": "Memo",
                    "page_count": 3,
                    "asset_url": "https://s3.documentcloud.org/",
                },
            )
        if path == "/documents/42/memo.txt":
            return httpx.Response(200, text="Full text p1 p2 p3")
        if path.startswith("/documents/42/pages/memo-p"):
            n = int(path.removeprefix("/documents/42/pages/memo-p").removesuffix(".txt"))
            if n == fail_page:
                return httpx.Response(429)
            return httpx.Response(200, text=f"Text of page {n}")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "fail_page, expected",
    [
        (None, [(1, "Text of page 1"), (2, "Text of page 2"), (3, "Text of page 3")]),
        (2, [(1, "Full text p1 p2 p3")]),  # any page failure keeps the full text as one page
    ],
)
def test_documentcloud_pull_stores_pages(db, monkeypatch, fail_page, expected):
    from openfoia.records import documentcloud

    monkeypatch.setattr(
        documentcloud,
        "egress_client",
        lambda policy=None, **kw: httpx.AsyncClient(transport=_dc_transport(fail_page)),
    )
    doc_id, text = asyncio.run(documentcloud.DocumentCloudAdapter().pull_text("42"))
    assert text == "Full text p1 p2 p3"
    assert _pages(doc_id) == expected


def test_documentcloud_refetch_keeps_real_pages_when_pages_fail(db, monkeypatch):
    from openfoia.records import documentcloud

    transport = {"t": _dc_transport(None)}
    monkeypatch.setattr(
        documentcloud,
        "egress_client",
        lambda policy=None, **kw: httpx.AsyncClient(transport=transport["t"]),
    )
    adapter = documentcloud.DocumentCloudAdapter()
    doc_id, _ = asyncio.run(adapter.pull_text("42"))
    transport["t"] = _dc_transport(fail_page=2)
    again, _ = asyncio.run(adapter.pull_text("42"))
    assert again == doc_id
    assert [n for n, _ in _pages(doc_id)] == [1, 2, 3]


def test_migrations_reach_head_fresh_and_from_001(tmp_path, monkeypatch):
    import sqlite3

    from alembic import command

    from openfoia import db as dbmod

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "fresh"))
    dbmod.init_db(seed=False)
    version = "select version_num from alembic_version"
    assert sqlite3.connect(dbmod.get_db_path()).execute(version).fetchall() == [("007",)]

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "old"))
    cfg = dbmod._alembic_config()
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{dbmod.get_db_path()}")
    command.upgrade(cfg, "001")
    monkeypatch.setattr(dbmod, "_schema_checked", False)
    with dbmod.get_session() as s:
        s.connection().exec_driver_sql("select count(*) from document_pages").scalar()
    assert sqlite3.connect(dbmod.get_db_path()).execute(version).fetchall() == [("007",)]


# --- grep: FTS5 and the LIKE fallback --------------------------------------


@pytest.fixture
def corpus(db):
    a = _add_doc(
        ["Cover sheet.", "Mr. Hocking met the WISC board.\nThe school board voted no."],
        title="Board minutes",
    )
    b = _add_doc(["Nothing about it here.", "", "Hocking County contract audit."], title="Audit")
    return a, b


def _search(query, **kw):
    from openfoia.db import get_session

    with get_session() as s:
        return doctext.search_pages(s, query, **kw)


def test_grep_fts5_reports_page_and_passage(corpus):
    a, b = corpus
    hits, engine = _search("hocking", context=20)
    assert engine == "fts5"
    found = {(h.document_id, h.page) for h in hits}
    assert found == {(a, 2), (b, 3)}
    for h in hits:
        assert h.passage[h.match_start : h.match_end].lower() == "hocking"


def test_grep_fts5_phrase_near_and_doc_filter(corpus):
    a, b = corpus
    assert [h.page for h in _search('"school board"')[0]] == [2]
    assert {h.document_id for h in _search("NEAR(hocking audit, 5)")[0]} == {b}
    assert {h.document_id for h in _search("hocking", document_ids=[a])[0]} == {a}


def test_grep_invalid_fts_syntax_searches_literal(corpus):
    _, b = corpus
    hits, engine = _search("contract-audit")
    assert engine == "fts5-literal"
    assert [h.document_id for h in hits] == [b]


def test_grep_like_fallback_without_fts5(corpus, monkeypatch):
    a, _ = corpus
    monkeypatch.setattr(doctext, "ensure_fts", lambda conn: False)
    hits, engine = _search("wisc board")
    assert engine == "like"
    assert [(h.document_id, h.page) for h in hits] == [(a, 2)]
    assert hits[0].passage[hits[0].match_start : hits[0].match_end] == "WISC board"


def test_grep_finds_documents_written_without_pages(db):
    """Rows that only have extracted_text (older data) are backfilled as page 1."""
    from openfoia.db import get_session
    from openfoia.models import DocumentPage

    doc_id = _add_doc(["legacy text mentions Hocking"])
    with get_session() as s:
        s.query(DocumentPage).delete()
    hits, _ = _search("hocking")
    assert [(h.document_id, h.page) for h in hits] == [(doc_id, 1)]


def test_grep_flags_single_page_documents(db):
    from openfoia.cli import app

    doc_id = _add_doc(["all 40 pages of Hocking text in one blob"], page_count=40)
    res = runner.invoke(app, ["docs", "grep", "hocking", "--json"])
    (hit,) = json.loads(res.stdout)["hits"]
    assert (hit["page_count"], hit["stored_pages"], hit["pages_are_real"]) == (40, 1, False)

    res = runner.invoke(app, ["docs", "grep", "hocking"])
    assert f"{doc_id[:8]}  Board minutes  p.1*" in res.stdout
    assert "page numbers here are not the source's page numbers" in res.stdout


def test_like_offsets_survive_length_changing_case_fold(db, monkeypatch):
    monkeypatch.setattr(doctext, "ensure_fts", lambda conn: False)
    _add_doc(["İİİ Straße Hocking"])  # "İ".lower() is two characters
    (hit,), _ = _search("hocking")
    assert hit.passage[hit.match_start : hit.match_end] == "Hocking"


def test_cli_grep_json_and_show(corpus):
    from openfoia.cli import app

    a, _ = corpus
    res = runner.invoke(app, ["docs", "grep", '"school board"', "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["engine"] == "fts5"
    assert [(h["document_id"], h["page"], h["title"]) for h in out["hits"]] == [
        (a, 2, "Board minutes")
    ]

    res = runner.invoke(app, ["docs", "show", a[:8], "--page", "2", "--json"])
    assert res.exit_code == 0, res.output
    shown = json.loads(res.stdout)
    assert [p["page"] for p in shown["page_map"]] == [1, 2]
    assert shown["pages"] == [
        {"page": 2, "text": "Mr. Hocking met the WISC board.\nThe school board voted no."}
    ]

    res = runner.invoke(app, ["docs", "list", "--json"])
    assert {d["id"]: d["stored_pages"] for d in json.loads(res.stdout)}[a] == 2
