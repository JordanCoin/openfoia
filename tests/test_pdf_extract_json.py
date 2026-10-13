"""glyph --json extraction: per-page verdicts, fallbacks, timeout, OCR routing."""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from openfoia.pipeline import pdf_extract
from openfoia.pipeline.pdf_extract import PDFExtractTimeout, extract_text

GLYPH = Path("/Users/jordan/Code/glyph-api/target/release/glyph-api")
ALICE = Path("/Users/jordan/Code/glyph-api/test-pdfs/generated/alice_sample.pdf")

CONTRACT = {
    "engine": "glyph-api",
    "version": "0.6.0",
    "profile": "large",
    "pages": [
        {"n": 2, "text": "", "chars": 0, "unresolved": 0, "verdict": "image_only", "hits": {}},
        {
            "n": 1,
            "text": "clean page one " * 5,
            "chars": 75,
            "unresolved": 0,
            "verdict": "text_layer",
            "hits": {},
        },
        {
            "n": 3,
            "text": "g�rbl�d",
            "chars": 7,
            "unresolved": 2,
            "verdict": "garbled",
            "hits": {"replacement_char": 2},
        },
    ],
    "summary": {},
}


def _script(tmp_path: Path, body: str) -> str:
    fake = tmp_path / "pdf-extract"
    fake.write_text("#!/bin/sh\n" + body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return str(fake)


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4")
    return p


def test_json_path_orders_pages_and_keeps_verdicts(tmp_path, pdf):
    (tmp_path / "out.json").write_text(json.dumps(CONTRACT))
    fake = _script(tmp_path, f'case "$*" in *--json*) cat {tmp_path}/out.json;; *) exit 9;; esac\n')

    r = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1))
    assert r.pages == ["clean page one " * 5, "", "g�rbl�d"]
    assert r.verdicts == ["text_layer", "image_only", "garbled"]
    assert (r.page_count, r.char_count) == (3, 82)


def test_old_binary_without_json_falls_back_to_form_feeds(tmp_path, pdf):
    fake = _script(
        tmp_path,
        'case "$*" in *--json*) echo "error: unexpected argument \'--json\' found" >&2; exit 2;; esac\n'
        "printf 'page one text\\fpage two text\\f'\n",
    )
    r = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1))
    assert r.pages == ["page one text", "page two text"]
    assert r.verdicts is None


def test_json_from_unknown_engine_is_not_trusted(tmp_path, pdf):
    fake = _script(tmp_path, 'echo \'{"engine": "other", "pages": []}\'\n')
    # Not glyph output and far below min_chars once treated as plain text.
    assert asyncio.run(extract_text(pdf, binary_path=fake, min_chars=50)) is None


def test_timeout_scales_with_size_and_names_the_file(tmp_path, pdf, monkeypatch):
    assert pdf_extract.timeout_for(pdf) == 30  # floor
    monkeypatch.setattr(pdf_extract, "timeout_for", lambda p: 1)
    fake = _script(tmp_path, "sleep 5\n")
    with pytest.raises(PDFExtractTimeout, match=r"a\.pdf.*within 1s"):
        asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1))


def test_timeout_cap():
    class Big:
        def stat(self):
            class S:
                st_size = 5000 * 1024 * 1024

            return S()

    assert pdf_extract.timeout_for(Big()) == 600


def test_migration_005_adds_verdict_column(tmp_path, monkeypatch):
    import sqlite3

    from openfoia import db as dbmod

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    dbmod.init_db(seed=False)
    con = sqlite3.connect(dbmod.get_db_path())
    assert con.execute("select version_num from alembic_version").fetchall() == [("005",)]
    cols = [r[1] for r in con.execute("pragma table_info(document_pages)")]
    assert "verdict" in cols


def test_ingest_routes_only_bad_pages_to_ocr(tmp_path, pdf, monkeypatch):
    from openfoia.pipeline import ingest as ingest_mod
    from openfoia.pipeline.ocr import OCREngine

    (tmp_path / "out.json").write_text(json.dumps(CONTRACT))
    fake = _script(tmp_path, f"cat {tmp_path}/out.json\n")
    monkeypatch.setattr(pdf_extract, "find_binary", lambda configured=None: fake)
    monkeypatch.setattr(pdf_extract, "timeout_for", lambda p: 10)
    seen = {}

    async def fake_ocr(self, path, page_numbers):
        seen["pages"] = page_numbers
        return {n: f"ocr text {n}" for n in page_numbers}

    monkeypatch.setattr(OCREngine, "ocr_pages", fake_ocr)
    _, pages, verdicts = asyncio.run(
        ingest_mod.DocumentIngester(tmp_path / "store")._extract_text_pdf(pdf)
    )

    assert seen["pages"] == [2, 3]
    assert pages[1:] == ["ocr text 2", "ocr text 3"]
    assert pages[0].startswith("clean page one")
    assert verdicts == ["text_layer", "ocr:image_only", "ocr:garbled"]


def test_ingest_keeps_text_when_tesseract_missing(tmp_path, pdf, monkeypatch, capsys):
    from openfoia.pipeline import ingest as ingest_mod
    from openfoia.pipeline.ocr import OCREngine

    (tmp_path / "out.json").write_text(json.dumps(CONTRACT))
    fake = _script(tmp_path, f"cat {tmp_path}/out.json\n")
    monkeypatch.setattr(pdf_extract, "find_binary", lambda configured=None: fake)

    async def missing(self, path, page_numbers):
        return None

    monkeypatch.setattr(OCREngine, "ocr_pages", missing)
    monkeypatch.setattr(ingest_mod, "_ocr_hint_shown", False)
    ingester = ingest_mod.DocumentIngester(tmp_path / "store")
    _, pages, verdicts = asyncio.run(ingester._extract_text_pdf(pdf))
    asyncio.run(ingester._extract_text_pdf(pdf))  # hint printed once, not twice

    assert verdicts == ["text_layer", "image_only", "garbled"]
    assert pages[2] == "g�rbl�d"
    assert capsys.readouterr().err.count("install-extras ocr") == 1


def test_replace_pages_stores_verdicts(tmp_path, monkeypatch):
    from uuid import uuid4

    from openfoia import doctext
    from openfoia.db import get_session, init_db
    from openfoia.models import (
        Agency,
        DeliveryMethod,
        Document,
        DocumentPage,
        DocumentType,
        Request,
        User,
    )

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    init_db()
    with get_session() as s:
        user = User(id=str(uuid4()), email="a@b.c", name="L")
        s.add(user)
        s.flush()
        req = Request(
            id=str(uuid4()),
            request_number="X-1",
            requester_id=user.id,
            agency_id=s.query(Agency).first().id,
            subject="t",
            body="",
            delivery_method=DeliveryMethod.EMAIL,
        )
        s.add(req)
        s.flush()
        doc = Document(
            id=str(uuid4()),
            request_id=req.id,
            doc_type=DocumentType.FULL_RESPONSE,
            filename="x.pdf",
            file_path="",
            file_size=0,
            mime_type="application/pdf",
            extracted_text="a\fb",
        )
        s.add(doc)
        doctext.replace_pages(s, doc.id, ["a", "b"], ["text_layer", "ocr:image_only"])
        doc_id = doc.id
    with get_session() as s:
        rows = s.query(DocumentPage).filter_by(document_id=doc_id).order_by("page_number").all()
        assert [(p.page_number, p.verdict) for p in rows] == [
            (1, "text_layer"),
            (2, "ocr:image_only"),
        ]


def test_json_unknown_major_version_falls_back(tmp_path, pdf, capsys):
    (tmp_path / "out.json").write_text(json.dumps({**CONTRACT, "version": "9.0.0"}))
    fake = _script(
        tmp_path,
        f"case \"$*\" in *--json*) cat {tmp_path}/out.json;; *) printf 'plain one\\fplain two\\f';; esac\n",
    )
    r = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1))
    assert r.pages == ["plain one", "plain two"]
    assert r.verdicts is None
    assert "contract version '9.0.0'" in capsys.readouterr().err


def test_force_ocr_skips_extraction_and_keeps_all_pages(tmp_path, pdf, monkeypatch):
    from openfoia.pipeline.ocr import OCREngine, OCRResult

    def boom(*a, **k):
        raise AssertionError("extract_text must not run under force_ocr")

    monkeypatch.setattr(pdf_extract, "extract_text", boom)
    monkeypatch.setattr("openfoia.pipeline.pdf_extract.extract_text", boom)

    async def tess(self, path):
        pages = [
            {"page_number": n, "text": f"ocr {n}", "confidence": 0.9, "verdict": "ocr"}
            for n in (1, 2, 3)
        ]
        return OCRResult("\n\n".join(p["text"] for p in pages), 0.9, 3, pages, {})

    monkeypatch.setattr(OCREngine, "_process_tesseract", tess)
    r = asyncio.run(OCREngine().process_pdf(pdf, force_ocr=True))
    assert [p["text"] for p in r.pages] == ["ocr 1", "ocr 2", "ocr 3"]


def test_docs_ingest_ocr_never_loses_ocr_text(tmp_path, monkeypatch):
    """Mixed PDF: clean page + image_only pages. --ocr must end with OCR text on every bad page."""
    from typer.testing import CliRunner

    from openfoia.cli import app
    from openfoia.db import get_session, init_db
    from openfoia.models import DocumentPage
    from openfoia.pipeline.ocr import OCREngine, OCRResult

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    init_db()
    from pypdf import PdfWriter

    real_pdf = tmp_path / "mixed.pdf"
    w = PdfWriter()
    for _ in range(3):
        w.add_blank_page(72, 72)
    with real_pdf.open("wb") as fh:
        w.write(fh)

    (tmp_path / "out.json").write_text(json.dumps(CONTRACT))
    fake = _script(tmp_path, f"cat {tmp_path}/out.json\n")
    monkeypatch.setattr(pdf_extract, "find_binary", lambda configured=None: fake)

    async def ocr_pages(self, path, page_numbers):
        return {n: f"ocr text {n}" for n in page_numbers}

    async def tess(self, path):
        pages = [
            {"page_number": n, "text": f"ocr text {n}", "confidence": 0.9, "verdict": "ocr"}
            for n in (1, 2, 3)
        ]
        return OCRResult("\n\n".join(p["text"] for p in pages), 0.9, 3, pages, {})

    monkeypatch.setattr(OCREngine, "ocr_pages", ocr_pages)
    monkeypatch.setattr(OCREngine, "_process_tesseract", tess)

    res = CliRunner().invoke(app, ["docs", "ingest", str(real_pdf), "--ocr"])
    assert res.exit_code == 0, res.output
    with get_session() as s:
        rows = s.query(DocumentPage).order_by(DocumentPage.page_number).all()
        got = [(p.page_number, p.text, p.verdict) for p in rows]
    # pages 2 and 3 were image_only/garbled: their OCR text survives --ocr; page 1 is OCR'd too
    assert [t for _, t, _ in got] == ["ocr text 1", "ocr text 2", "ocr text 3"]
    assert all(v == "ocr" for _, _, v in got)


def _glyph_supports_json() -> bool:
    if not (GLYPH.is_file() and ALICE.is_file()):
        return False
    probe = subprocess.run(
        [str(GLYPH), str(ALICE), "--json"], capture_output=True, text=True, timeout=60
    )
    return probe.returncode == 0 and "argument" not in probe.stderr.lower()


@pytest.mark.skipif(not _glyph_supports_json(), reason="local glyph build with --json not present")
def test_real_glyph_binary_json(tmp_path):
    pdf = tmp_path / "alice.pdf"
    shutil.copy(ALICE, pdf)
    r = asyncio.run(extract_text(pdf, binary_path=str(GLYPH)))
    assert r is not None and r.verdicts is not None
    assert len(r.pages) == len(r.verdicts) >= 1
    assert set(r.verdicts) <= {"text_layer", "garbled", "image_only"}
    assert "Alice" in r.text
    plain = subprocess.run([str(GLYPH), str(pdf)], capture_output=True, text=True, timeout=60)
    assert r.pages[0].strip() == plain.stdout.split("\f")[0].strip()
