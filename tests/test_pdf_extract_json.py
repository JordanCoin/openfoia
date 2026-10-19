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

    r = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1, min_chars_per_page=1))
    assert r.pages == ["clean page one " * 5, "", "g�rbl�d"]
    assert r.verdicts == ["text_layer", "image_only", "garbled"]
    assert (r.page_count, r.char_count) == (3, 82)


def test_old_binary_without_json_falls_back_to_form_feeds(tmp_path, pdf):
    fake = _script(
        tmp_path,
        'case "$*" in *--json*) echo "error: unexpected argument \'--json\' found" >&2; exit 2;; esac\n'
        "printf 'page one text\\fpage two text\\f'\n",
    )
    r = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1, min_chars_per_page=1))
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
        asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1, min_chars_per_page=1))


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
    assert con.execute("select version_num from alembic_version").fetchall() == [("007",)]
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
    r = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1, min_chars_per_page=1))
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


def test_json_verdicts_bypass_whole_document_gates(tmp_path, pdf):
    # Per-page verdicts already route bad pages; a sparse text_layer page is kept.
    contract = {
        **CONTRACT,
        "pages": [{"n": 1, "text": "one", "verdict": "text_layer"}] * 2,
    }
    fake = _script(tmp_path, f"printf '%s' '{json.dumps(contract)}'\n")
    r = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1))
    assert r.pages == ["one", "one"]
    assert r.quality_reasons == ()


def test_symbol_heavy_plain_output_is_flagged_for_low_alpha_ratio(tmp_path, pdf):
    fake = _script(tmp_path, "printf '1234 !@#$ 5678 !@#$\\f1234 !@#$ 5678 !@#$'\n")
    r = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1, min_chars_per_page=1))
    assert r.quality_reasons == ("LOW_ALPHA_RATIO",)
    assert r.pages == ["1234 !@#$ 5678 !@#$", "1234 !@#$ 5678 !@#$"]


def test_whitespace_heavy_sparse_plain_output_is_flagged_by_default_gates(tmp_path, pdf):
    fake = _script(
        tmp_path,
        "printf 'A                                                \\fA                                                '\n",
    )
    assert asyncio.run(extract_text(pdf, binary_path=fake)) is None  # below min_chars


def test_quality_metrics_allow_text_heavy_output(tmp_path, pdf):
    fake = _script(tmp_path, "printf 'A useful page with enough words.\\fAnother useful page.'\n")
    result = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1))
    assert result is not None


def test_quality_rejection_log_contains_reason_codes_and_named_metrics(tmp_path, pdf, caplog):
    fake = _script(tmp_path, "printf '1234 !@#$ 5678 !@#$\\f1234 !@#$ 5678 !@#$'\n")
    with caplog.at_level("WARNING", logger="openfoia.pipeline.pdf_extract"):
        r = asyncio.run(extract_text(pdf, binary_path=fake, min_chars=1, min_chars_per_page=1))
    assert r.quality_reasons == ("LOW_ALPHA_RATIO",)
    message = caplog.records[-1].message
    assert "reason_codes=['LOW_ALPHA_RATIO']" in message
    assert "total_chars=32" in message
    assert "page_count=2" in message
    assert "chars_per_page=16.000" in message
    assert "alpha_ratio=0.000" in message


def test_pdf_quality_config_file_and_environment(tmp_path, monkeypatch):
    from openfoia.config import load_config

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "pdf_extract": {
                    "min_chars_threshold": 80,
                    "min_chars_per_page_threshold": 30,
                    "min_alpha_ratio_threshold": 0.4,
                }
            }
        )
    )
    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_MIN_CHARS", "90")
    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_MIN_CHARS_PER_PAGE", "40")
    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_MIN_ALPHA_RATIO", "0.6")
    config = load_config()
    assert config.pdf_extract.min_chars_threshold == 90
    assert config.pdf_extract.min_chars_per_page_threshold == 40
    assert config.pdf_extract.min_alpha_ratio_threshold == 0.6

    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_MIN_CHARS", "bad")
    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_MIN_CHARS_PER_PAGE", "bad")
    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_MIN_ALPHA_RATIO", "bad")
    config = load_config()
    assert config.pdf_extract.min_chars_threshold == 80
    assert config.pdf_extract.min_chars_per_page_threshold == 30
    assert config.pdf_extract.min_alpha_ratio_threshold == 0.4


def test_ingestion_forwards_pdf_quality_config(tmp_path, pdf, monkeypatch):
    from openfoia.pipeline import ingest as ingest_mod

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    config_path = tmp_path / "data" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "pdf_extract": {
                    "binary_path": "/tmp/configured-pdf-extract",
                    "min_chars_threshold": 61,
                    "min_chars_per_page_threshold": 31,
                    "min_alpha_ratio_threshold": 0.7,
                }
            }
        )
    )
    seen = {}

    async def fake_extract(path, **kwargs):
        seen.update(kwargs)
        return pdf_extract.PDFExtractResult(
            text="text-heavy page",
            page_count=1,
            char_count=16,
            backend="configured",
            pages=["text-heavy page"],
            verdicts=["text_layer"],
        )

    monkeypatch.setattr(pdf_extract, "extract_text", fake_extract)
    result = asyncio.run(ingest_mod.DocumentIngester(tmp_path / "store")._extract_text_pdf(pdf))
    assert result[0] == "text-heavy page"
    assert seen == {
        "binary_path": "/tmp/configured-pdf-extract",
        "min_chars": 61,
        "min_chars_per_page": 31,
        "min_alpha_ratio": 0.7,
    }


def test_ingestion_forces_ocr_after_configured_direct_rejection(tmp_path, pdf, monkeypatch):
    from openfoia.pipeline import ingest as ingest_mod
    from openfoia.pipeline.ocr import OCREngine, OCRResult

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    config_path = tmp_path / "data" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "pdf_extract": {
                    "binary_path": "/tmp/configured-pdf-extract",
                    "min_chars_threshold": 61,
                    "min_chars_per_page_threshold": 31,
                    "min_alpha_ratio_threshold": 0.7,
                }
            }
        )
    )
    direct_calls = []
    ocr_calls = []

    async def reject_direct(path, **kwargs):
        direct_calls.append((path, kwargs))
        return None

    async def fake_process(self, path, *, force_ocr=False):
        ocr_calls.append((path, force_ocr))
        return OCRResult(
            text="OCR fallback text " * 4,
            confidence=0.9,
            page_count=1,
            pages=[{"text": "OCR fallback text " * 4, "verdict": "ocr"}],
            metadata={},
        )

    monkeypatch.setattr(pdf_extract, "extract_text", reject_direct)
    monkeypatch.setattr(OCREngine, "process_pdf", fake_process)

    result = asyncio.run(ingest_mod.DocumentIngester(tmp_path / "store")._extract_text_pdf(pdf))

    assert result[0] == "OCR fallback text " * 4
    assert len(direct_calls) == 1
    assert ocr_calls == [(pdf, True)]


def test_pdf_quality_config_save_load_round_trip(tmp_path, monkeypatch):
    from openfoia.config import load_config, save_config

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    for name in (
        "OPENFOIA_PDF_EXTRACT_BINARY",
        "OPENFOIA_PDF_EXTRACT_MIN_CHARS",
        "OPENFOIA_PDF_EXTRACT_MIN_CHARS_PER_PAGE",
        "OPENFOIA_PDF_EXTRACT_MIN_ALPHA_RATIO",
    ):
        monkeypatch.delenv(name, raising=False)

    config_path = tmp_path / "config.json"
    config = load_config(config_path)
    config.pdf_extract.binary_path = "/custom/pdf-extract"
    config.pdf_extract.min_chars_threshold = 71
    config.pdf_extract.min_chars_per_page_threshold = 31
    config.pdf_extract.min_alpha_ratio_threshold = 0.7
    save_config(config, config_path)

    loaded = load_config(config_path)
    assert loaded.pdf_extract.binary_path == "/custom/pdf-extract"
    assert loaded.pdf_extract.min_chars_threshold == 71
    assert loaded.pdf_extract.min_chars_per_page_threshold == 31
    assert loaded.pdf_extract.min_alpha_ratio_threshold == 0.7


# Issue #61 regressions: whole-document gates must not override per-page
# verdicts, discard text when OCR is unavailable, or ignore configuration.

FIXTURES = Path(__file__).parent / "fixtures" / "pdf_quality"
needs_extractor = pytest.mark.skipif(
    shutil.which("pdf-extract") is None, reason="pdf-extract not on PATH"
)
ROW_PAGE = "1001 $4,512.00 03/14/2024\\n1002 $9,870.25 03/15/2024\\n1003 $1,204.10 03/18/2024"


def _old_binary(tmp_path, page_text):
    """A pre---json extractor that prints two form-feed separated pages."""
    return _script(
        tmp_path,
        'case "$*" in *--json*) echo "error: unexpected argument \'--json\' found" >&2; exit 2;; esac\n'
        f"printf '{page_text}\\f{page_text}\\f'\n",
    )


def _ocr_result(text):
    from openfoia.pipeline.ocr import OCRResult

    pages = [{"page_number": 1, "text": text, "confidence": 0.9, "verdict": "ocr"}]
    return OCRResult(text, 0.9, 1, pages, {})


def _ingest_real(tmp_path, monkeypatch, pdf_path):
    """Ingest through the real extractor, recording OCR instead of running tesseract."""
    from openfoia.pipeline import ingest as ingest_mod
    from openfoia.pipeline.ocr import OCREngine

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    calls = {"pages": [], "whole": 0}

    async def ocr_pages(self, path, page_numbers):
        calls["pages"].extend(page_numbers)
        return {n: f"ocr text {n}" for n in page_numbers}

    async def whole(self, path, force_ocr=False):
        calls["whole"] += 1
        return _ocr_result("whole document OCR " * 5)

    monkeypatch.setattr(OCREngine, "ocr_pages", ocr_pages)
    monkeypatch.setattr(OCREngine, "process_pdf", whole)
    ingester = ingest_mod.DocumentIngester(tmp_path / "store")
    return asyncio.run(ingester._extract_text_pdf(pdf_path)), calls


@needs_extractor
@pytest.mark.parametrize(
    ("name", "exact"), [("numeric_table.pdf", "1,000,000"), ("foia_log.pdf", "2024-001000")]
)
def test_tabular_text_layer_keeps_exact_digits(tmp_path, monkeypatch, name, exact):
    (text, _, verdicts), calls = _ingest_real(tmp_path, monkeypatch, FIXTURES / name)
    assert calls == {"pages": [], "whole": 0}
    assert exact in text
    assert set(verdicts) == {"text_layer"}


@needs_extractor
def test_one_text_page_and_nine_scans_ocrs_only_the_scans(tmp_path, monkeypatch):
    (_, pages, verdicts), calls = _ingest_real(
        tmp_path, monkeypatch, FIXTURES / "one_text_nine_scans.pdf"
    )
    assert calls == {"pages": list(range(2, 11)), "whole": 0}
    assert pages[0].strip().startswith("The Department of Public Works")
    assert verdicts == ["text_layer"] + ["ocr:image_only"] * 9


@needs_extractor
def test_prose_fixture_passes_without_ocr(tmp_path, monkeypatch):
    (text, _, verdicts), calls = _ingest_real(tmp_path, monkeypatch, FIXTURES / "prose.pdf")
    assert calls == {"pages": [], "whole": 0}
    assert "exemption 5" in text and verdicts == ["text_layer"]


@pytest.mark.parametrize("ocr", ["missing", "empty"])
def test_gate_reject_keeps_direct_text_when_ocr_gives_nothing(
    tmp_path, pdf, monkeypatch, capsys, ocr
):
    from openfoia.pipeline import ingest as ingest_mod
    from openfoia.pipeline.ocr import OCREngine

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_BINARY", _old_binary(tmp_path, ROW_PAGE))

    async def tesseract(self, path):
        if ocr == "missing":
            raise ImportError("No module named 'pytesseract'")
        return _ocr_result("")

    monkeypatch.setattr(OCREngine, "_process_tesseract", tesseract)
    monkeypatch.setattr(ingest_mod, "_ocr_hint_shown", False)
    ingester = ingest_mod.DocumentIngester(tmp_path / "store")
    text, pages, _ = asyncio.run(ingester._extract_text_pdf(pdf))
    asyncio.run(ingester._extract_text_pdf(pdf))

    assert len(pages) == 2 and "$4,512.00" in pages[0]
    assert "$9,870.25" in text
    assert capsys.readouterr().err.count("install-extras ocr") == (ocr == "missing")


def test_garbage_plain_text_without_verdicts_still_falls_back_to_ocr(tmp_path, pdf, monkeypatch):
    from openfoia.pipeline import ingest as ingest_mod
    from openfoia.pipeline.ocr import OCREngine

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_BINARY", _old_binary(tmp_path, "#$%% *+/<> =~^| " * 6))

    async def tesseract(self, path):
        return _ocr_result("Scanned memo recovered by OCR. " * 3)

    monkeypatch.setattr(OCREngine, "_process_tesseract", tesseract)
    text, _, verdicts = asyncio.run(
        ingest_mod.DocumentIngester(tmp_path / "store")._extract_text_pdf(pdf)
    )
    assert text.startswith("Scanned memo recovered by OCR.")
    assert verdicts == ["ocr"]


def test_docs_ocr_uses_configured_thresholds(tmp_path, pdf, monkeypatch):
    from typer.testing import CliRunner

    from openfoia.cli import app
    from openfoia.pipeline.ocr import OCREngine, RedactionDetector

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_BINARY", _old_binary(tmp_path, ROW_PAGE))
    monkeypatch.setenv("OPENFOIA_PDF_EXTRACT_MIN_ALPHA_RATIO", "0")

    async def tesseract(self, path):
        return _ocr_result("OCR replaced the direct text " * 3)

    async def no_boxes(self, path):
        return 0

    monkeypatch.setattr(OCREngine, "_process_tesseract", tesseract)
    monkeypatch.setattr(RedactionDetector, "_count_visual_redactions", no_boxes)
    out = tmp_path / "out.txt"
    res = CliRunner().invoke(app, ["docs", "ocr", str(pdf), "-o", str(out)])
    assert res.exit_code == 0, res.output
    assert "$4,512.00" in out.read_text()


def test_font_pack_env_points_engine_at_pack_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("GLYPH_API_FONTS_DIR", raising=False)
    assert "GLYPH_API_FONTS_DIR" not in pdf_extract.font_pack_env()  # no pack yet
    pack = tmp_path / "glyph-api" / "fonts"
    pack.mkdir(parents=True)
    assert "GLYPH_API_FONTS_DIR" not in pdf_extract.font_pack_env()  # empty dir is not a pack
    (pack / "Lato-Regular.ttf").write_bytes(b"\0")
    assert pdf_extract.font_pack_env()["GLYPH_API_FONTS_DIR"] == str(pack)
    monkeypatch.setenv("GLYPH_API_FONTS_DIR", "/elsewhere")
    assert pdf_extract.font_pack_env()["GLYPH_API_FONTS_DIR"] == "/elsewhere"  # explicit wins


def test_install_scripts_parse_and_know_the_font_pack():
    root = Path(__file__).resolve().parents[1]
    for script in ("install.sh", "uninstall.sh"):
        subprocess.run(["bash", "-n", str(root / script)], check=True)
        assert (
            "glyph-api/fonts" in (root / script).read_text()
            or "fonts" in (root / script).read_text()
        )
    text = (root / "install.sh").read_text()
    assert "--no-fonts" in text and "fonts.tar.gz" in text and "--strip-components=1" in text
