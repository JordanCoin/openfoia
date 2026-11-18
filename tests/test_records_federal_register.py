"""Federal Register adapter: parsing, filters, honest errors, 429 backoff, PDF download.

HTTP is mocked with httpx.MockTransport behind the real egress_client, so the
retry transport (429 backoff) runs as it does in production. Fixtures under
tests/fixtures/federal_register/ are real API responses from 2026-11-17, trimmed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from openfoia import net
from openfoia.cli import app
from openfoia.net import EgressMode, EgressPolicy
from openfoia.records import federal_register as fr
from openfoia.records import get_adapter, list_sources
from openfoia.records.base import AdapterRequestError, RecordEntity, SearchResult
from openfoia.records.federal_register import FederalRegisterAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "federal_register"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
runner = CliRunner()


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    sleeps: list[float] = []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(net, "_sleep", no_sleep)
    monkeypatch.setattr(net, "MIN_HOST_INTERVAL", 0.0)
    monkeypatch.setattr(fr, "_sleep", no_sleep)
    return sleeps


@pytest.fixture
def sleeps(_isolated):
    return _isolated


def _route(monkeypatch, *responses):
    """Serve *responses* in order through the real egress_client; returns the requests seen."""
    seen: list[httpx.Request] = []
    real = net.egress_client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = responses[len(seen) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    def fake(policy=None, **kwargs):
        kwargs.pop("transport", None)
        return real(policy, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(fr, "egress_client", fake)
    return seen


def _search(query="biometric", **kwargs):
    return asyncio.run(FederalRegisterAdapter().search(query, **kwargs))


# --- registration ---------------------------------------------------------


def test_registered_without_a_key():
    assert "federalregister" in list_sources()
    adapter = get_adapter("federalregister", api_data_gov_key="personal-key")
    assert isinstance(adapter, FederalRegisterAdapter)
    assert not hasattr(adapter, "_api_key")


# --- search parsing --------------------------------------------------------


def test_search_parses_real_response(monkeypatch):
    seen = _route(monkeypatch, httpx.Response(200, json=_fixture("search.json")))
    result = _search()

    assert result.error is None
    assert result.total_results == 258
    assert [e.identifiers["document_number"] for e in result.entities] == [
        "2026-16231",
        "2025-19747",
    ]
    rule, proposed = result.entities
    assert rule.name == "9-11 Response and Biometric Entry-Exit Fee for H-1B and L-1 Visas"
    assert rule.source == "federalregister"
    assert rule.source_url.startswith("https://www.federalregister.gov/documents/2026/08/10/")
    assert rule.extra_data["document_type"] == "Rule"
    assert rule.extra_data["publication_date"] == "2026-08-10"
    assert rule.extra_data["comments_close_on"] is None
    assert "Homeland Security Department" in rule.extra_data["agencies"]
    assert rule.extra_data["docket_ids"] == ["Docket No. USCBP-2024-0009", "CBP Dec. No. 26-11"]
    assert proposed.extra_data["document_type"] == "Proposed Rule"
    assert proposed.extra_data["comments_close_on"] == "2026-01-02"
    # Excerpts carry <span class="match"> highlighting; it must not reach the terminal.
    assert "<span" not in proposed.extra_data["snippet"]
    assert "Biometric" in proposed.extra_data["snippet"]
    assert len(seen) == 1 and seen[0].url.host == "www.federalregister.gov"
    assert seen[0].url.path == "/api/v1/documents.json"


def test_clean_defuses_terminal_escapes_and_markup():
    # Control characters arrive as entities (html.unescape voids those) or raw in
    # the JSON, including the 8-bit CSI \x9b; all must be gone.
    hostile = "&#27;]0;owned&#7;Rule <b>on</b>\x1b[31m\x07 water\x9b\n"
    cleaned = fr.clean(hostile)
    assert not any(ch in cleaned for ch in "\x1b\x07\x9b\n")
    assert "<b>" not in cleaned
    assert cleaned == "]0;ownedRule on [31m water"


def test_hostile_titles_are_cleaned_in_entities(monkeypatch):
    data = _fixture("search.json")
    data["results"][0]["title"] = "&#27;[2J[red]Spoofed[/red]"
    data["results"][0]["html_url"] = "javascript:alert(1)"
    _route(monkeypatch, httpx.Response(200, json=data))
    entity = _search().entities[0]
    assert "\x1b" not in entity.name
    assert entity.source_url is None  # only https links on federalregister.gov are kept


# --- filters -> query parameters -------------------------------------------


def test_filters_become_api_conditions(monkeypatch):
    seen = _route(monkeypatch, httpx.Response(200, json=_fixture("empty.json")))
    _search(
        "facial recognition",
        agency="DHS, Environmental Protection Agency",
        filing_type="rule, Proposed Rule",
        since="2025-01-01",
        until="2026-06-30",
        docket="USCIS-2025-0205",
        page=2,
        page_size=50,
    )
    params = seen[0].url.params
    assert params["conditions[term]"] == "facial recognition"
    assert params.get_list("conditions[agencies][]") == [
        "homeland-security-department",
        "environmental-protection-agency",
    ]
    assert params.get_list("conditions[type][]") == ["RULE", "PRORULE"]
    assert params["conditions[publication_date][gte]"] == "2025-01-01"
    assert params["conditions[publication_date][lte]"] == "2026-06-30"
    assert params["conditions[docket_id]"] == "USCIS-2025-0205"
    assert params["page"] == "2"
    assert params["per_page"] == "50"
    assert params["order"] == "relevance"
    assert "comments_close_on" in params.get_list("fields[]")


def test_no_query_sorts_newest_and_sends_no_term(monkeypatch):
    seen = _route(monkeypatch, httpx.Response(200, json=_fixture("empty.json")))
    _search("", docket="EPA-HQ-OAR-2021-0317", doc_type="presidential")
    params = seen[0].url.params
    assert "conditions[term]" not in params
    assert params["order"] == "newest"
    assert params.get_list("conditions[type][]") == ["PRESDOCU"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"doc_type": "memo"}, "Unknown Federal Register document type 'memo'"),
        ({"since": "last week"}, "--since must be a date"),
        ({"since": "2026-02-01", "until": "2026-01-01"}, "is after --until"),
    ],
)
def test_bad_filters_fail_locally_without_a_request(monkeypatch, kwargs, message):
    # The API answers an unknown type with HTTP 200 and count 0: checking here is
    # what stops a typo from reading as "no such documents".
    seen = _route(monkeypatch)
    result = _search(**kwargs)
    assert result.error and message in result.error
    assert seen == []


def test_page_size_is_capped():
    params = fr.build_params("x", 1, 50_000)
    assert params["per_page"] == fr.PAGE_MAX


# --- no results vs API error -----------------------------------------------


def test_empty_result_is_not_an_error(monkeypatch):
    _route(monkeypatch, httpx.Response(200, json=_fixture("empty.json")))
    result = _search("zzqqxxnonexistentterm")
    assert result.error is None
    assert result.total_results == 0
    assert result.entities == []


def test_rejected_agency_is_an_error_with_a_hint(monkeypatch):
    _route(monkeypatch, httpx.Response(400, json=_fixture("error_agency.json")))
    result = _search(agency="Department of Nowhere")
    assert result.error.startswith("Federal Register rejected the query: agencies: invalid value")
    assert "short name such as EPA" in result.error
    assert result.total_results == 0


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(500, text="oops"), "HTTP 500"),
        (
            httpx.Response(
                400, json={"status": 400, "message": "Pagination limit exceeded.  No more"}
            ),
            "Pagination limit exceeded",
        ),
        (httpx.Response(200, text="<html>maintenance</html>"), "other than JSON"),
        (httpx.Response(200, json=["not", "a", "dict"]), "unexpected response"),
        (
            httpx.Response(302, headers={"location": "https://unblock.federalregister.gov/"}),
            "bot filter",
        ),
        (httpx.ConnectError("refused"), "request failed"),
        (httpx.ReadTimeout("slow"), "timed out"),
    ],
)
def test_api_failures_are_errors_not_empty_results(monkeypatch, response, message):
    # Transport errors are retried by egress_client, so serve the failure every time.
    _route(monkeypatch, *[response] * net.MAX_ATTEMPTS)
    result = _search()
    assert result.error is not None and message in result.error
    assert result.entities == []


# --- 429 backoff -----------------------------------------------------------


def test_429_is_retried_with_backoff_then_succeeds(monkeypatch, sleeps):
    seen = _route(
        monkeypatch,
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json=_fixture("search.json")),
    )
    result = _search()
    assert result.error is None and len(result.entities) == 2
    assert len(seen) == 2
    assert sleeps == [2.0]


def test_persistent_429_is_reported_as_rate_limit(monkeypatch):
    seen = _route(monkeypatch, *[httpx.Response(429)] * net.MAX_ATTEMPTS)
    result = _search()
    assert len(seen) == net.MAX_ATTEMPTS
    assert "rate limit (HTTP 429)" in result.error


def test_long_retry_after_is_passed_back_not_slept(monkeypatch, sleeps):
    seen = _route(monkeypatch, httpx.Response(429, headers={"Retry-After": "600"}))
    result = _search()
    assert len(seen) == 1 and sleeps == []
    assert "wait 600 seconds" in result.error


# --- fetch -----------------------------------------------------------------


def test_fetch_document_metadata(monkeypatch):
    seen = _route(monkeypatch, httpx.Response(200, json=_fixture("document.json")))
    entity = asyncio.run(FederalRegisterAdapter().fetch("2026-21024"))

    assert seen[0].url.path == "/api/v1/documents/2026-21024.json"
    assert entity.identifiers["document_number"] == "2026-21024"
    assert entity.identifiers["citation"] == "91 FR 65390"
    x = entity.extra_data
    assert x["pages"] == 43
    assert x["pdf_url"] == "https://www.govinfo.gov/content/pkg/FR-2026-10-14/pdf/2026-21024.pdf"
    assert "8 CFR 281" in x["cfr_references"]
    assert "Immigration" in x["topics"]
    assert {"id": "ICEB-2025-0034", "agency": "ICEB"}.items() <= x["regulations_dockets"][1].items()


def test_fetch_accepts_a_federalregister_url(monkeypatch):
    seen = _route(monkeypatch, httpx.Response(200, json=_fixture("document.json")))
    url = (
        "https://www.federalregister.gov/documents/2026/10/14/2026-21024/imposition-and-collection"
    )
    asyncio.run(FederalRegisterAdapter().fetch(url))
    assert seen[0].url.path == "/api/v1/documents/2026-21024.json"


def test_fetch_unknown_number_is_none(monkeypatch):
    _route(monkeypatch, httpx.Response(404, text="<html>404 Not Found</html>"))
    assert asyncio.run(FederalRegisterAdapter().fetch("0000-00000")) is None


def test_fetch_failure_raises_instead_of_none(monkeypatch):
    _route(monkeypatch, *[httpx.Response(503)] * net.MAX_ATTEMPTS)
    with pytest.raises(AdapterRequestError, match="HTTP 503"):
        asyncio.run(FederalRegisterAdapter().fetch("2026-21024"))


@pytest.mark.parametrize(
    "identifier",
    ["../../agencies", "2026-21024/../x", "a b", "", "https://evil.example/documents/2026/1/1/x/"],
)
def test_fetch_rejects_bad_identifiers_without_a_request(monkeypatch, identifier):
    seen = _route(monkeypatch)
    with pytest.raises(AdapterRequestError):
        asyncio.run(FederalRegisterAdapter().fetch(identifier))
    assert seen == []


# --- download --------------------------------------------------------------


def _doc_with_pdf(url):
    doc = _fixture("document.json")
    doc["pdf_url"] = url
    doc["public_inspection_pdf_url"] = None
    return doc


def test_download_streams_the_govinfo_pdf(monkeypatch, tmp_path):
    seen = _route(
        monkeypatch,
        httpx.Response(200, json=_fixture("document.json")),
        httpx.Response(200, content=PDF_BYTES),
    )
    entity, path = asyncio.run(FederalRegisterAdapter().download("2026-21024", tmp_path / "out"))
    assert path == tmp_path / "out" / "federalregister-2026-21024.pdf"
    assert path.read_bytes() == PDF_BYTES
    assert [r.url.host for r in seen] == ["www.federalregister.gov", "www.govinfo.gov"]
    assert entity.extra_data["pdf_path"] == str(path)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://www.govinfo.gov/content/pkg/FR-2026-10-14/pdf/2026-21024.pdf",
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost/x.pdf",
        "https://evil.example.com/content/pkg/FR-2026-10-14/pdf/2026-21024.pdf",
        "https://www.govinfo.gov.evil.example/x.pdf",
    ],
)
def test_malicious_download_url_is_refused(monkeypatch, tmp_path, url):
    seen = _route(monkeypatch, httpx.Response(200, json=_doc_with_pdf(url)))
    with pytest.raises(AdapterRequestError, match="Refusing to download"):
        asyncio.run(FederalRegisterAdapter().download("2026-21024", tmp_path))
    assert len(seen) == 1  # metadata only; the PDF URL was never requested
    assert list(tmp_path.iterdir()) == []


def test_download_retries_govinfo_502(monkeypatch, tmp_path, sleeps):
    seen = _route(
        monkeypatch,
        httpx.Response(200, json=_fixture("document.json")),
        httpx.Response(502),
        httpx.Response(200, content=PDF_BYTES),
    )
    _, path = asyncio.run(FederalRegisterAdapter().download("2026-21024", tmp_path))
    assert path.read_bytes() == PDF_BYTES
    assert len(seen) == 3 and sleeps == [2.0]


def test_download_gives_up_after_repeated_502(monkeypatch, tmp_path):
    _route(
        monkeypatch,
        httpx.Response(200, json=_fixture("document.json")),
        *[httpx.Response(502)] * fr.PDF_ATTEMPTS,
    )
    with pytest.raises(AdapterRequestError, match="HTTP 502"):
        asyncio.run(FederalRegisterAdapter().download("2026-21024", tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_a_non_pdf_body(monkeypatch, tmp_path):
    _route(
        monkeypatch,
        httpx.Response(200, json=_fixture("document.json")),
        httpx.Response(200, content=b"<html>Request Access</html>"),
    )
    with pytest.raises(AdapterRequestError, match="other than a PDF"):
        asyncio.run(FederalRegisterAdapter().download("2026-21024", tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_egress_policy_reaches_the_client(monkeypatch):
    captured = []
    real = net.egress_client

    def fake(policy=None, **kwargs):
        captured.append(policy)
        return real(
            EgressPolicy(),
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"count": 0})),
        )

    monkeypatch.setattr(fr, "egress_client", fake)
    policy = EgressPolicy(mode=EgressMode.TOR)
    asyncio.run(FederalRegisterAdapter(egress=policy).search("x"))
    assert captured == [policy]


# --- CLI -------------------------------------------------------------------


class _CapturingAdapter:
    credentials_withheld = False

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    async def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.result or SearchResult(
            source="federalregister", query=query, total_results=0, entities=[]
        )


def test_cli_maps_options_onto_adapter_filters(monkeypatch):
    adapter = _CapturingAdapter()
    monkeypatch.setattr("openfoia.records.get_adapter", lambda source, **_: adapter)
    r = runner.invoke(
        app,
        [
            "records",
            "search",
            "-s",
            "federalregister",
            "--agency",
            "EPA",
            "-t",
            "proposed-rule",
            "--since",
            "2026-01-01",
            "--until",
            "2026-03-31",
            "--docket",
            "EPA-HQ-OAR-2021-0317",
            "-n",
            "40",
        ],
    )
    assert r.exit_code == 0, r.output
    query, kwargs = adapter.calls[0]
    assert query == ""
    assert kwargs["agency"] == "EPA"
    assert kwargs["filing_type"] == "proposed-rule"
    assert kwargs["since"] == "2026-01-01" and kwargs["until"] == "2026-03-31"
    assert kwargs["docket"] == "EPA-HQ-OAR-2021-0317"
    assert kwargs["page_size"] == 40
    # The warning names everything that leaves the machine, filters included.
    assert "docket=EPA-HQ-OAR-2021-0317" in r.output
    assert "www.federalregister.gov" in r.output


def test_cli_refuses_federalregister_filters_on_other_sources():
    r = runner.invoke(app, ["records", "search", "acme", "-s", "sec", "--docket", "X-1"])
    assert r.exit_code == 1
    assert "only supported with --source federalregister" in " ".join(r.output.split())


def test_cli_table_prints_markup_in_titles_literally(monkeypatch):
    entity = RecordEntity(
        entity_type="GOVERNMENT_DOCUMENT",
        name="[bold red]Spoofed[/bold red] rule",
        source="federalregister",
        identifiers={"document_number": "2026-00001"},
        extra_data={"document_type": "Rule", "agencies": ["[link=x]EPA"]},
    )
    adapter = _CapturingAdapter(
        SearchResult(source="federalregister", query="x", total_results=1, entities=[entity])
    )
    monkeypatch.setattr("openfoia.records.get_adapter", lambda source, **_: adapter)
    monkeypatch.setenv("COLUMNS", "200")
    r = runner.invoke(app, ["records", "search", "x", "-s", "federalregister"])
    assert r.exit_code == 0, r.output
    assert "[bold red]Spoofed[/bold red]" in r.output
    assert "[link=x]EPA" in r.output


def test_all_sources_skips_others_for_a_docket_filter(monkeypatch):
    adapters = {"federalregister": _CapturingAdapter(), "sec": _CapturingAdapter()}
    monkeypatch.setattr("openfoia.records.list_sources", lambda: sorted(adapters))
    monkeypatch.setattr("openfoia.records.get_adapter", lambda source, **_: adapters[source])
    r = runner.invoke(app, ["records", "search", "--all-sources", "--docket", "X-1"])
    assert r.exit_code == 0, r.output
    assert "sec             skipped (docket filter not supported)" in r.output
    assert adapters["federalregister"].calls and not adapters["sec"].calls


def _fake_ingest(text):
    from openfoia.pipeline.ingest import IngestResult

    async def ingest_file(self, path, **kwargs):
        stored = Path(self.storage_path) / "copy.pdf"
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(Path(path).read_bytes())
        return IngestResult(
            document_id="d0c00000-0000-0000-0000-000000000001",
            filename=Path(path).name,
            file_path=str(stored),
            file_size=len(PDF_BYTES),
            mime_type="application/pdf",
            page_count=1,
            extracted_text=text,
            checksum="0",
            metadata={},
            pages=[text] if text else None,
        )

    return ingest_file


def test_cli_fetch_downloads_ingests_and_skips_on_rerun(monkeypatch, tmp_path):
    from openfoia.db import get_session
    from openfoia.models import Document, Request

    monkeypatch.setattr(
        "openfoia.pipeline.ingest.DocumentIngester.ingest_file", _fake_ingest("x" * 80)
    )
    _route(
        monkeypatch,
        httpx.Response(200, json=_fixture("document.json")),
        httpx.Response(200, content=PDF_BYTES),
    )
    url = "https://www.federalregister.gov/documents/2026/10/14/2026-21024/slug"
    r = runner.invoke(app, ["records", "fetch", url, "-s", "federalregister"])
    assert r.exit_code == 0, r.output
    assert "Fetched 1" in r.output
    assert "comments close" not in r.output  # this rule had no comment period
    assert "www.govinfo.gov" in r.output
    with get_session() as session:
        req = session.query(Request).filter(Request.request_number == "FR-2026-21024").one()
        assert "https://www.federalregister.gov/documents/2026/10/14/2026-21024/" in req.body
        assert session.query(Document).filter(Document.request_id == req.id).count() == 1

    r = runner.invoke(app, ["records", "fetch", "2026-21024", "-s", "federalregister"])
    assert r.exit_code == 0, r.output
    assert "already stored" in r.output


def test_cli_fetch_without_text_stores_nothing(monkeypatch):
    from openfoia.db import get_session
    from openfoia.models import Request

    monkeypatch.setattr("openfoia.pipeline.ingest.DocumentIngester.ingest_file", _fake_ingest(None))
    _route(
        monkeypatch,
        httpx.Response(200, json=_fixture("document.json")),
        httpx.Response(200, content=PDF_BYTES),
    )
    r = runner.invoke(app, ["records", "fetch", "2026-21024", "-s", "federalregister"])
    assert r.exit_code == 1, r.output
    assert "not stored" in r.output
    with get_session() as session:
        assert session.query(Request).filter(Request.request_number == "FR-2026-21024").count() == 0
