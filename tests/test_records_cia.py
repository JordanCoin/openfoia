"""CIA FOIA reading room adapter: parsing, identifiers, download, blocked pages.

All transport is httpx.MockTransport; no request leaves the process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from openfoia.records import cia
from openfoia.records.base import AdapterRequestError
from openfoia.records.cia import BASE, CIAReadingRoomAdapter, normalize_identifier

FIXTURES = Path(__file__).parent / "fixtures"
HOME = (FIXTURES / "cia_home.html").read_text()
SEARCH = (FIXTURES / "cia_search.html").read_text()
DOCUMENT = (FIXTURES / "cia_document.html").read_text()
BLOCKED = (FIXTURES / "cia_blocked.html").read_text()
PDF = b"%PDF-1.4\n%fake\n"
DOC_PATH = "/readingroom/document/cia-rdp74-00390r000300140008-2"
PDF_PATH = "/readingroom/docs/CIA-RDP74-00390R000300140008-2.pdf"


def _denied(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        302, headers={"location": "https://www.cia.gov/readingroom"}, text=BLOCKED, request=request
    )


def _site(request: httpx.Request) -> httpx.Response:
    """A reading room that lets us in."""
    path = request.url.path
    if request.method == "POST" and path == "/readingroom/":
        assert b"search_block_form=annual+report" in request.content
        assert b"form_build_id=form-TESTBUILDID" in request.content
        return httpx.Response(
            302, headers={"location": f"{BASE}/search/site/annual%20report"}, request=request
        )
    if path == "/readingroom/":
        return httpx.Response(200, text=HOME, request=request)
    if path == "/readingroom/search/site/annual report":
        return httpx.Response(200, text=SEARCH, request=request)
    if path == DOC_PATH:
        return httpx.Response(200, text=DOCUMENT, request=request)
    if path == PDF_PATH:
        return httpx.Response(200, content=PDF, request=request)
    return httpx.Response(404, request=request)


@pytest.fixture(autouse=True)
def _fast_and_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(cia, "MIN_INTERVAL", 0)
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))


def _mock(monkeypatch, handler):
    calls: list[httpx.Request] = []

    def _client(policy=None, **kwargs):
        def record(request):
            calls.append(request)
            return handler(request)

        kwargs.pop("isolation_token", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(cia, "egress_client", _client)
    return calls


# --- search ---------------------------------------------------------------


def test_search_parses_title_url_esdn_snippet(monkeypatch):
    calls = _mock(monkeypatch, _site)
    result = asyncio.run(CIAReadingRoomAdapter().search("annual report"))

    assert result.error is None
    assert [r.method for r in calls] == ["GET", "POST", "GET"]
    assert result.total_results == 2
    assert result.raw_response == {"has_next_page": True}
    first, second = result.entities
    assert first.name == "INTERIM REPORT ON RAB REPORTS INVENTORY"
    assert first.source_url == f"{BASE}/document/cia-rdp74-00390r000300140008-2"
    assert first.identifiers == {"esdn": "CIA-RDP74-00390R000300140008-2"}
    assert first.extra_data["snippet"].startswith("MEMORANDUM FOR: Acting Chief")
    assert "<strong>" not in first.extra_data["snippet"]
    assert first.extra_data["pdf_url"] == f"{BASE}/docs/CIA-RDP74-00390R000300140008-2.pdf"
    assert second.name == "INPUT TO THE DCI'S ANNUAL REPORT (U)"
    assert second.identifiers == {"esdn": "0000012345"}
    assert second.extra_data["pdf_url"] is None


def test_search_page_two_uses_zero_based_pager(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "page=" in str(request.url):
            return httpx.Response(200, text="<html></html>", request=request)
        return _site(request)

    _mock(monkeypatch, handler)
    result = asyncio.run(CIAReadingRoomAdapter().search("annual report", page=2))
    assert result.error is None
    assert seen[-1] == f"{BASE}/search/site/annual%20report?page=1"
    assert result.entities == []


def test_search_blocked_is_an_error_not_zero_results(monkeypatch):
    def handler(request):
        if request.url.path == "/readingroom/" and request.method == "GET":
            return httpx.Response(200, text=HOME, request=request)
        if request.method == "POST":
            return _site(request)
        return _denied(request)

    _mock(monkeypatch, handler)
    result = asyncio.run(CIAReadingRoomAdapter().search("annual report"))
    assert result.error is not None
    assert "blocked" in result.error or "bot filter" in result.error
    assert result.entities == []


def test_search_captcha_body_is_blocked(monkeypatch):
    def handler(request):
        if request.method == "GET" and request.url.path == "/readingroom/":
            return httpx.Response(200, text=HOME, request=request)
        if request.method == "POST":
            return _site(request)
        return httpx.Response(200, text="<html><body>Please solve this CAPTCHA</body></html>")

    _mock(monkeypatch, handler)
    result = asyncio.run(CIAReadingRoomAdapter().search("annual report"))
    assert result.error and "bot filter" in result.error


def test_search_timeout_is_an_error(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    _mock(monkeypatch, handler)
    result = asyncio.run(CIAReadingRoomAdapter().search("annual report"))
    assert result.error and "ReadTimeout" in result.error


# --- identifiers ------------------------------------------------------------


@pytest.mark.parametrize(
    "ident, kind, page, pdf",
    [
        (
            "CIA-RDP96-00789R002200230001-2",
            "crest",
            f"{BASE}/document/cia-rdp96-00789r002200230001-2",
            f"{BASE}/docs/CIA-RDP96-00789R002200230001-2.pdf",
        ),
        (
            "cia-rdp96-00789r002200230001-2",
            "crest",
            f"{BASE}/document/cia-rdp96-00789r002200230001-2",
            f"{BASE}/docs/CIA-RDP96-00789R002200230001-2.pdf",
        ),
        ("144765", "mori", f"{BASE}/document/0000144765", f"{BASE}/docs/DOC_0000144765.pdf"),
        (
            "DOC_0000144765.pdf",
            "esdn",
            f"{BASE}/document/0000144765",
            f"{BASE}/docs/DOC_0000144765.pdf",
        ),
        (
            "0000144765",
            "mori",
            f"{BASE}/document/0000144765",
            f"{BASE}/docs/DOC_0000144765.pdf",
        ),
        (
            "https://www.cia.gov/readingroom/document/cia-rdp74-00390r000300140008-2",
            "url",
            f"{BASE}/document/cia-rdp74-00390r000300140008-2",
            None,
        ),
        (
            "https://www.cia.gov/readingroom/docs/DOC_0000144765.pdf",
            "url",
            f"{BASE}/document/0000144765",
            f"{BASE}/docs/DOC_0000144765.pdf",
        ),
    ],
)
def test_normalize_identifier(ident, kind, page, pdf):
    assert normalize_identifier(ident) == (kind, page, pdf)


@pytest.mark.parametrize("bad", ["https://example.com/x.pdf", "", "a b c", "../etc/passwd"])
def test_normalize_identifier_rejects_garbage(bad):
    with pytest.raises(ValueError):
        normalize_identifier(bad)


# --- fetch / download ---------------------------------------------------------


def test_fetch_parses_document_page(monkeypatch):
    _mock(monkeypatch, _site)
    entity = asyncio.run(CIAReadingRoomAdapter().fetch("CIA-RDP74-00390R000300140008-2"))
    assert entity.name == "INTERIM REPORT ON RAB REPORTS INVENTORY"
    assert entity.identifiers == {"esdn": "CIA-RDP74-00390R000300140008-2"}
    assert entity.extra_data["pages"] == 8
    assert entity.extra_data["date"] == "September 29, 1970"
    assert entity.extra_data["release_date"] == "September 25, 2001"
    assert entity.extra_data["collection"] == "General CIA Records"
    assert entity.extra_data["pdf_url"] == f"{BASE}/docs/CIA-RDP74-00390R000300140008-2.pdf"


def test_fetch_404_returns_none(monkeypatch):
    _mock(monkeypatch, _site)
    assert asyncio.run(CIAReadingRoomAdapter().fetch("999")) is None


def test_fetch_blocked_raises(monkeypatch):
    _mock(monkeypatch, _denied)
    with pytest.raises(AdapterRequestError, match="bot filter"):
        asyncio.run(CIAReadingRoomAdapter().fetch("CIA-RDP74-00390R000300140008-2"))


def test_download_writes_pdf_under_data_dir(monkeypatch, tmp_path):
    from openfoia.db import get_data_dir

    calls = _mock(monkeypatch, _site)
    dest_dir = get_data_dir() / "downloads" / "cia"
    entity, path = asyncio.run(
        CIAReadingRoomAdapter().download("CIA-RDP74-00390R000300140008-2", dest_dir)
    )
    assert path == dest_dir / "CIA-RDP74-00390R000300140008-2.pdf"
    assert path.read_bytes() == PDF
    assert str(path).startswith(str(tmp_path))
    assert entity.extra_data["pages"] == 8
    assert all(r.url.host == "www.cia.gov" for r in calls)
    assert all("openfoia" not in r.headers["user-agent"].lower() for r in calls)


def test_download_rejects_html_body(monkeypatch, tmp_path):
    def handler(request):
        if request.url.path == PDF_PATH:
            return httpx.Response(200, text="<html>nope</html>", request=request)
        return _site(request)

    _mock(monkeypatch, handler)
    with pytest.raises(AdapterRequestError, match="non-PDF"):
        asyncio.run(CIAReadingRoomAdapter().download("CIA-RDP74-00390R000300140008-2", tmp_path))
    assert list(tmp_path.glob("*.pdf")) == []


def test_download_blocked_pdf_reports_bot_filter(monkeypatch, tmp_path):
    def handler(request):
        if request.url.path == PDF_PATH:
            return _denied(request)
        return _site(request)

    _mock(monkeypatch, handler)
    with pytest.raises(AdapterRequestError, match="bot filter"):
        asyncio.run(CIAReadingRoomAdapter().download("CIA-RDP74-00390R000300140008-2", tmp_path))


# --- CLI --------------------------------------------------------------------


def test_cli_search_prints_pre_contact_notice_and_rows(monkeypatch):
    from openfoia.cli import app

    _mock(monkeypatch, _site)
    result = CliRunner().invoke(
        app, ["records", "search", "annual report", "--source", "cia", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert "cia.gov" in result.output  # notice (stderr) names the destination
    rows = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert len(rows) == 2
    assert '"id": "CIA-RDP74-00390R000300140008-2"' in rows[0]


def test_cli_fetch_cia_downloads_ingests_and_attaches_to_case(monkeypatch, tmp_path):
    from openfoia.cli import app
    from openfoia.db import ensure_db, get_session
    from openfoia.models import Document, Request
    from openfoia.pipeline.ingest import IngestResult

    _mock(monkeypatch, _site)

    async def fake_ingest(self, file_path, **kwargs):
        assert Path(file_path).read_bytes() == PDF
        return IngestResult(
            document_id="doc-cia-1",
            filename=Path(file_path).name,
            file_path=str(file_path),
            file_size=len(PDF),
            mime_type="application/pdf",
            page_count=1,
            extracted_text="page one",
            checksum="x",
            metadata={},
            pages=["page one"],
        )

    monkeypatch.setattr("openfoia.pipeline.ingest.DocumentIngester.ingest_file", fake_ingest)

    runner = CliRunner()
    ensure_db()
    assert runner.invoke(app, ["case", "new", "rab"]).exit_code == 0
    result = runner.invoke(
        app, ["records", "fetch", "CIA-RDP74-00390R000300140008-2", "--source", "cia"]
    )
    assert result.exit_code == 0, result.output
    assert "cia.gov" in result.output
    assert "Attached to the active case" in result.output

    with get_session() as session:
        req = session.query(Request).filter(Request.request_number.like("CIA-%")).one()
        assert req.request_number == "CIA-CIA-RDP74-00390R000300140008-2"
        assert req.case_id is not None
        doc = session.query(Document).filter(Document.request_id == req.id).one()
        assert doc.id == "doc-cia-1"
        assert doc.extracted_text == "page one"
