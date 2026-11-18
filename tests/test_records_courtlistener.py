"""CourtListener adapter: parsing, token handling, local budget, 429 backoff, untrusted data.

All HTTP goes through a mocked transport behind the real egress_client; nothing
touches the network.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.net import EgressMode, EgressPolicy, egress_client
from openfoia.records import courtlistener as cl
from openfoia.records import get_adapter
from openfoia.records.courtlistener import CourtListenerAdapter

runner = CliRunner()
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "courtlistener.json").read_text())
API = "https://www.courtlistener.com/api/rest/v4"


def fx(name):
    return copy.deepcopy(FIXTURES[name])


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(cl.KEY_ENV, raising=False)
    monkeypatch.delenv(cl.BUDGET_ENV, raising=False)
    monkeypatch.setattr(cl, "_budget_warned", False)
    sleeps: list[float] = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(cl, "_sleep", record_sleep)
    return sleeps


@pytest.fixture
def sleeps(env):
    return env


def serve(monkeypatch, responder):
    """Route every egress_client in the adapter to *responder*; returns the requests sent."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return responder(request)

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, **kw):
        return egress_client(None, timeout=timeout, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(cl, "egress_client", fake_egress_client)
    return sent


def routes(table):
    """Responder from {path: json body or httpx.Response}."""

    def responder(request):
        item = table.get(request.url.path)
        if item is None:
            return httpx.Response(404, json={"detail": "Not found."})
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    return responder


DOCKET_ROUTES = {
    "/api/rest/v4/dockets/4214664/": FIXTURES["docket"],
    "/api/rest/v4/docket-entries/": FIXTURES["docket_entries"],
    "/api/rest/v4/parties/": FIXTURES["parties"],
    "/api/rest/v4/attorneys/": FIXTURES["attorneys"],
}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_parses_dockets_and_sends_token(monkeypatch):
    sent = serve(monkeypatch, routes({"/api/rest/v4/search/": fx("search_r")}))
    result = asyncio.run(CourtListenerAdapter(api_key="k-120").search("veterans"))

    assert result.error is None and result.total_results == 2
    req = sent[0]
    assert str(req.url).startswith(f"{API}/search/")
    assert dict(req.url.params) == {"q": "veterans", "type": "r"}
    assert req.headers["authorization"] == "Token k-120"
    e = result.entities[0]
    assert e.entity_type == "COURT_CASE" and e.source == "courtlistener"
    assert next(iter(e.identifiers.values())) == "4214664"
    assert e.name == "National Veterans Legal Services Program v. United States"
    assert e.source_url == (
        "https://www.courtlistener.com/docket/4214664/"
        "national-veterans-legal-services-program-v-united-states/"
    )
    assert e.jurisdiction == "dcd" and e.extra_data["date"] == "2016-04-21"
    assert e.extra_data["documents"][0]["id"] == "rd:2974077"
    assert e.extra_data["more_documents"] is True
    assert "<mark>" not in e.extra_data["snippet"] and "PACER access" in e.extra_data["snippet"]
    assert cl.budget_used()["day"] == 1


def test_search_filings_and_opinions_and_court_filter(monkeypatch):
    sent = serve(
        monkeypatch,
        lambda r: httpx.Response(
            200, json=fx("search_rd") if r.url.params["type"] == "rd" else fx("search_o")
        ),
    )
    adapter = CourtListenerAdapter(api_key="k")
    rd = asyncio.run(adapter.search("pacer fees", filing_type="rd", jurisdiction="dcd"))
    assert sent[0].url.params["court"] == "dcd"
    assert next(iter(rd.entities[0].identifiers.values())) == "rd:2974077"
    assert (
        rd.entities[0].name
        == "National Veterans Legal Services Program v. United States: Complaint"
    )
    op = asyncio.run(adapter.search("foo", search_type="o"))
    assert op.entities[0].identifiers["opinion"] == "op:6489975"
    assert op.entities[0].extra_data["citations"] == ["101 Haw. 235", "65 P.3d 182"]


def test_unknown_search_type_is_an_error_and_sends_nothing(monkeypatch):
    sent = serve(monkeypatch, routes({}))
    result = asyncio.run(CourtListenerAdapter(api_key="k").search("x", filing_type="10-K"))
    assert result.error and "Unknown CourtListener search type" in result.error
    assert sent == []


def test_api_error_is_not_empty_results(monkeypatch):
    serve(monkeypatch, lambda r: httpx.Response(500, json={"detail": "boom"}))
    failed = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert failed.error and "HTTP 500" in failed.error and failed.entities == []

    sent = serve(
        monkeypatch,
        lambda r: httpx.Response(302, headers={"Location": "https://evil.example/steal"}),
    )
    redirected = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert "redirect (HTTP 302); not followed" in redirected.error
    assert [r.url.host for r in sent] == ["www.courtlistener.com"]

    serve(
        monkeypatch, lambda r: httpx.Response(200, json={"count": 0, "next": None, "results": []})
    )
    empty = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert empty.error is None and empty.total_results == 0 and empty.entities == []

    serve(monkeypatch, lambda r: httpx.Response(401, json={"detail": "Invalid token."}))
    rejected = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert "rejected the API token" in rejected.error and cl.KEY_ENV in rejected.error


def test_cli_distinguishes_api_error_from_no_results(monkeypatch):
    monkeypatch.setenv(cl.KEY_ENV, "k")
    serve(monkeypatch, lambda r: httpx.Response(503, json={"detail": "down"}))
    r = runner.invoke(app, ["records", "search", "x", "-s", "courtlistener"])
    assert r.exit_code == 1 and "HTTP 503" in r.output and "No results found" not in r.output

    serve(monkeypatch, lambda r: httpx.Response(200, json={"count": 0, "results": []}))
    r = runner.invoke(app, ["records", "search", "x", "-s", "courtlistener"])
    assert r.exit_code == 0 and "No results found" in r.output
    assert "query logging" in r.output  # the disclosure notice is printed before sending


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


def test_missing_key_explains_how_to_get_one_and_sends_nothing(monkeypatch):
    sent = serve(monkeypatch, routes({}))
    result = asyncio.run(CourtListenerAdapter().search("x"))
    assert result.error and cl.TOKEN_URL in result.error and cl.KEY_ENV in result.error
    assert sent == []
    with pytest.raises(cl.AdapterRequestError, match=cl.KEY_ENV):
        asyncio.run(CourtListenerAdapter().fetch("4214664"))

    r = runner.invoke(app, ["records", "search", "x", "-s", "courtlistener"])
    assert r.exit_code == 1 and cl.KEY_ENV in r.output and "Sending" not in r.output
    r = runner.invoke(app, ["records", "fetch", "4214664", "-s", "courtlistener"])
    assert r.exit_code == 1 and cl.KEY_ENV in r.output
    assert sent == []


def test_key_comes_from_env_only(monkeypatch):
    monkeypatch.setenv(cl.KEY_ENV, "from-env")
    assert get_adapter("courtlistener").has_key
    assert get_adapter("courtlistener", api_data_gov_key="data-gov")._api_key == "from-env"


@pytest.mark.parametrize("tor", [True, False])
def test_key_withheld_over_tor(monkeypatch, tor):
    monkeypatch.setenv(cl.KEY_ENV, "cl-131")
    sent = serve(monkeypatch, routes({"/api/rest/v4/search/": fx("search_r")}))
    policy = EgressPolicy(mode=EgressMode.TOR) if tor else EgressPolicy()
    adapter = CourtListenerAdapter(egress=policy)
    result = asyncio.run(adapter.search("x"))
    assert adapter.credentials_withheld is tor and adapter.has_key is not tor
    if tor:
        assert sent == [] and "not used over Tor" in result.error
    else:
        assert sent and sent[0].headers["authorization"] == "Token cl-131"


def test_token_is_redacted_from_transport_errors(monkeypatch):
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.headers['authorization']}")

    serve(monkeypatch, boom)
    result = asyncio.run(CourtListenerAdapter(api_key="secret-tok").search("x"))
    assert "secret-tok" not in result.error and "<redacted>" in result.error


def test_all_sources_skips_courtlistener_without_key(monkeypatch):
    sent = serve(monkeypatch, routes({}))
    monkeypatch.setattr("openfoia.records.list_sources", lambda: ["courtlistener"])
    r = runner.invoke(app, ["records", "search", "x", "--all-sources"])
    assert "skipped (no OPENFOIA_COURTLISTENER_API_KEY)" in r.output
    assert sent == []


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def test_fetch_docket_with_entries_parties_and_attorneys(monkeypatch):
    sent = serve(monkeypatch, routes(DOCKET_ROUTES))
    entity = asyncio.run(CourtListenerAdapter(api_key="k").fetch("4214664"))

    assert [r.url.path.removeprefix("/api/rest/v4") for r in sent] == [
        "/dockets/4214664/",
        "/docket-entries/",
        "/parties/",
        "/attorneys/",
    ]
    assert len(sent) == cl.DOCKET_FETCH_REQUESTS
    assert sent[1].url.params["docket"] == "4214664"
    assert sent[1].url.params["omit"] == "recap_documents__plain_text"
    assert all(r.headers["authorization"] == "Token k" for r in sent)
    x = entity.extra_data
    assert x["errors"] == [] and x["docket_number"] == "1:16-cv-00745"
    assert [d["id"] for d in x["entries"][0]["documents"]] == ["rd:2974077", "rd:2974078"]
    assert x["attorneys"] == [
        {"name": "Ann Example", "represents": ["National Veterans Legal Services Program"]}
    ]
    text = x["full_text"]
    assert "Docket number: 1:16-cv-00745" in text and "Judge: Paul L. Friedman" in text
    assert "- United States (Defendant)" in text
    assert "rd:2974077  Complaint (PDF on CourtListener, 48 pages)" in text
    assert "rd:2974078  Civil Cover Sheet (not in RECAP" in text
    assert cl.budget_used()["day"] == 4


def test_docket_keeps_metadata_when_entries_fail(monkeypatch):
    table = dict(DOCKET_ROUTES)
    table["/api/rest/v4/docket-entries/"] = httpx.Response(403, json={"detail": "No access."})
    serve(monkeypatch, routes(table))
    entity = asyncio.run(CourtListenerAdapter(api_key="k").fetch("4214664"))
    assert entity.extra_data["entries"] == []
    assert "docket entries not retrieved" in entity.extra_data["errors"][0]
    assert "NOTE: docket entries not retrieved" in entity.extra_data["full_text"]


def test_next_link_to_another_host_is_not_followed(monkeypatch):
    entries = fx("docket_entries")
    entries["next"] = "https://evil.example/api/rest/v4/docket-entries/?cursor=x"
    table = dict(DOCKET_ROUTES)
    table["/api/rest/v4/docket-entries/"] = entries
    sent = serve(monkeypatch, routes(table))
    entity = asyncio.run(CourtListenerAdapter(api_key="k").fetch("4214664", entry_pages=3))
    assert all(r.url.host == "www.courtlistener.com" for r in sent)
    assert any("outside" in e for e in entity.extra_data["errors"])


def test_cli_fetch_docket_and_filing_store_documents(monkeypatch):
    monkeypatch.setenv(cl.KEY_ENV, "k")
    table = dict(DOCKET_ROUTES)
    table["/api/rest/v4/recap-documents/2974077/"] = FIXTURES["recap_document"]
    sent = serve(monkeypatch, routes(table))

    assert runner.invoke(app, ["case", "new", "Fees"]).exit_code == 0
    r = runner.invoke(app, ["records", "fetch", "4214664", "rd:2974077", "-s", "courtlistener"])
    assert r.exit_code == 0, r.output
    assert r.output.count("saved") == 2 and "up to 5 requests" in r.output
    assert "Attached to the active case" in r.output

    n = len(sent)
    url = "https://www.courtlistener.com/docket/4214664/slug/"
    r = runner.invoke(app, ["records", "fetch", url, "RD:2974077", "-s", "courtlistener"])
    assert r.output.count("already stored (--refresh") == 2 and len(sent) == n

    r = runner.invoke(app, ["docs", "grep", "exceed the cost"])
    assert "exceed the cost" in r.output
    out = runner.invoke(app, ["case", "export", "-f", "json"]).output
    assert "https://www.courtlistener.com/docket/4214664/" in out


def test_fetch_filing_without_text_offers_only_a_storage_pdf(monkeypatch):
    doc = fx("recap_document")
    doc["plain_text"] = ""
    serve(monkeypatch, routes({"/api/rest/v4/recap-documents/2974077/": doc}))
    entity = asyncio.run(CourtListenerAdapter(api_key="k").fetch("rd:2974077"))
    assert entity.extra_data["full_text"] is None
    assert entity.extra_data["pdf_url"] == (
        "https://storage.courtlistener.com/recap/gov.uscourts.dcd.178502/"
        "gov.uscourts.dcd.178502.1.0_48.pdf"
    )


def test_pdf_download_goes_to_storage_without_the_token(monkeypatch, tmp_path):
    serve(monkeypatch, routes({"/api/rest/v4/recap-documents/2974077/": fx("recap_document")}))
    adapter = CourtListenerAdapter(api_key="k")
    entity = asyncio.run(adapter.fetch("rd:2974077"))
    sent = serve(monkeypatch, lambda r: httpx.Response(200, content=b"%PDF-1.4 test"))
    path = asyncio.run(adapter.download_pdf(entity, tmp_path))
    assert path.read_bytes() == b"%PDF-1.4 test"
    assert sent[0].url.host == "storage.courtlistener.com"
    assert "authorization" not in sent[0].headers
    assert cl.budget_used()["day"] == 1  # the API call, not the download


@pytest.mark.parametrize(
    "filepath",
    [
        "../../etc/passwd",
        "recap/../../x.pdf",
        "https://evil.example/x.pdf",
        "recap/x.pdf?u=@evil.example",
        "recap/x\u202e.pdf",
    ],
)
def test_malicious_download_path_is_refused(monkeypatch, filepath):
    doc = fx("recap_document")
    doc["plain_text"] = ""
    doc["filepath_local"] = filepath
    serve(monkeypatch, routes({"/api/rest/v4/recap-documents/2974077/": doc}))
    entity = asyncio.run(CourtListenerAdapter(api_key="k").fetch("rd:2974077"))
    assert entity.extra_data["pdf_url"] is None and entity.extra_data["pdf_refused"] is True


def test_protocol_relative_path_stays_on_storage_host():
    assert (
        cl.storage_url("//evil.example/x.pdf")
        == "https://storage.courtlistener.com/evil.example/x.pdf"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://storage.courtlistener.com/recap/x.pdf",
        "https://evil.example/recap/x.pdf",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
def test_download_pdf_refuses_urls_off_courtlistener_storage(monkeypatch, tmp_path, url):
    sent = serve(monkeypatch, lambda r: httpx.Response(200, content=b"x"))
    entity = cl.RecordEntity(
        entity_type="DOCUMENT",
        name="x",
        source="courtlistener",
        identifiers={"recap_document": "rd:1"},
        extra_data={"pdf_url": url},
    )
    with pytest.raises(ValueError, match="Refusing"):
        asyncio.run(CourtListenerAdapter(api_key="k").download_pdf(entity, tmp_path))
    assert sent == [] and list(tmp_path.iterdir()) == []


def test_identifier_forms():
    assert cl.canonical_id("4214664") == "4214664"
    assert cl.canonical_id("docket:4214664") == "4214664"
    assert cl.canonical_id("https://www.courtlistener.com/docket/4214664/x/") == "4214664"
    assert cl.canonical_id("rd:2974077") == "rd:2974077"
    assert cl.canonical_id("op:6489975") == "op:6489975"
    for bad in (
        "",
        "abc",
        "rd:",
        "rd:1/../2",
        "https://evil.example/docket/1/",
        "x:1",
        "rd:\u00b2",
    ):
        with pytest.raises(ValueError):
            cl.canonical_id(bad)


# ---------------------------------------------------------------------------
# Budget and rate limits
# ---------------------------------------------------------------------------


def _write_budget(times, blocked_until=None):
    path = cl.budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sent": times, "blocked_until": blocked_until}))


def test_daily_budget_exhausted_refuses_without_sending(monkeypatch):
    sent = serve(monkeypatch, routes({"/api/rest/v4/search/": fx("search_r")}))
    now = time.time()
    _write_budget([now - 80_000 + i for i in range(125)])
    result = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert sent == []
    assert "daily budget used up: 125 of 125" in result.error
    assert "Nothing was sent" in result.error and "frees up at" in result.error
    assert cl.USAGE_URL in result.error

    monkeypatch.setenv(cl.BUDGET_ENV, "1")
    assert asyncio.run(CourtListenerAdapter(api_key="k").search("x")).error is None
    assert len(sent) == 1


def test_hourly_budget_exhausted_refuses(monkeypatch):
    sent = serve(monkeypatch, routes({"/api/rest/v4/search/": fx("search_r")}))
    _write_budget([time.time() - 3000 + i for i in range(50)])
    result = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert sent == [] and "hourly budget used up: 50 of 50" in result.error


def test_minute_window_waits_instead_of_failing(monkeypatch, sleeps):
    sent = serve(monkeypatch, routes({"/api/rest/v4/search/": fx("search_r")}))
    now = time.time()
    _write_budget([now - 30 + i for i in range(5)])
    result = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert result.error is None and len(sent) == 1
    assert len(sleeps) == 1 and 25 <= sleeps[0] <= 31


def test_stale_and_corrupt_budget_files_reset():
    _write_budget([time.time() - 90_000] * 200)
    assert cl.budget_used()["day"] == 0
    cl.budget_path().write_text("not json")
    assert cl.budget_used() == {"minute": 0, "hour": 0, "day": 0}


def test_429_short_retry_after_is_waited_then_retried(monkeypatch, sleeps):
    responses = [
        httpx.Response(429, headers={"Retry-After": "40"}, json={"detail": "Rate limit 5/min."}),
        httpx.Response(200, json=fx("search_r")),
    ]
    sent = serve(monkeypatch, lambda r: responses.pop(0))
    result = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert result.error is None and len(sent) == 2
    assert sleeps == [40.0]


def test_429_long_retry_after_fails_and_blocks_later_calls(monkeypatch, sleeps):
    sent = serve(
        monkeypatch,
        lambda r: httpx.Response(
            429,
            headers={"Retry-After": "3600"},
            json={"detail": "Request was throttled. Rate limit exceeded: 125/day."},
        ),
    )
    adapter = CourtListenerAdapter(api_key="k")
    result = asyncio.run(adapter.search("x"))
    assert "rate limit reached (HTTP 429)" in result.error and "125/day" in result.error
    assert "Try again after" in result.error and sleeps == []
    assert len(sent) == 1

    again = asyncio.run(adapter.search("x"))
    assert len(sent) == 1 and "Nothing was sent" in again.error


# ---------------------------------------------------------------------------
# Untrusted strings
# ---------------------------------------------------------------------------


def test_terminal_escapes_and_huge_fields_are_cleaned(monkeypatch):
    data = fx("search_r")
    hit = data["results"][0]
    hit["caseName"] = "Evil \x1b]0;pwned\x07\x1b[2J v. [bold red]Markup[/bold red]\u202e"
    hit["docketNumber"] = "9" * 100_000
    hit["docket_absolute_url"] = "//evil.example/phish"
    hit["recap_documents"][0]["id"] = "1/../../admin"
    monkeypatch.setenv(cl.KEY_ENV, "k")
    serve(monkeypatch, routes({"/api/rest/v4/search/": data}))

    e = asyncio.run(CourtListenerAdapter().search("x")).entities[0]
    assert "\x1b" not in e.name and "\x07" not in e.name and "\u202e" not in e.name
    assert len(e.extra_data["docket_number"]) <= 100
    assert e.source_url == "https://www.courtlistener.com/docket/4214664/"
    assert e.extra_data["documents"] == []

    r = runner.invoke(app, ["records", "search", "x", "-s", "courtlistener", "-n", "1"])
    assert r.exit_code == 0, r.output
    i = r.output.find("\x1b")
    assert i == -1, repr(r.output[max(0, i - 80) : i + 40])
    assert "[bold" in r.output  # shown as text, not applied as Rich markup


def test_stored_text_drops_control_characters(monkeypatch):
    doc = fx("recap_document")
    doc["plain_text"] = "page one\x1b[31m red\x00\fpage two\r\n"
    serve(monkeypatch, routes({"/api/rest/v4/recap-documents/2974077/": doc}))
    entity = asyncio.run(CourtListenerAdapter(api_key="k").fetch("rd:2974077"))
    assert entity.extra_data["full_text"] == "page one[31m red\fpage two\n"


def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.setattr(cl, "MAX_RESPONSE_BYTES", 1000)
    serve(
        monkeypatch, lambda r: httpx.Response(200, content=b'{"results": [' + b" " * 5000 + b"]}")
    )
    result = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert result.error and "refusing to read it" in result.error


def test_error_detail_cannot_break_rich_markup(monkeypatch):
    monkeypatch.setenv(cl.KEY_ENV, "k")
    serve(monkeypatch, lambda r: httpx.Response(500, json={"detail": "[/bold] \x1b[2J gone"}))
    r = runner.invoke(app, ["records", "search", "x", "-s", "courtlistener"])
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit), r.output
    assert "(/bold)" in r.output and "\x1b" not in r.output
    r = runner.invoke(app, ["records", "fetch", "4214664", "-s", "courtlistener"])
    assert "(/bold)" in r.output and isinstance(r.exception, SystemExit)


def test_real_unauthenticated_response_reads_as_a_token_error(monkeypatch):
    serve(monkeypatch, lambda r: httpx.Response(401, json=fx("unauthenticated_401")))
    result = asyncio.run(CourtListenerAdapter(api_key="k").search("x"))
    assert "HTTP 401" in result.error and "Authentication credentials" in result.error


def test_storage_url_checks():
    # storage_url refuses odd characters; storage_url_allowed refuses traversal and other hosts.
    assert not cl.storage_url_allowed("https://storage.courtlistener.com/a/../b.pdf")
    assert not cl.storage_url_allowed("https://storage.courtlistener.com.evil.example/b.pdf")
    assert cl.storage_url_allowed("https://storage.courtlistener.com/recap/b.pdf")
    assert cl.storage_url("recap/../../b.pdf") is None
    assert cl.storage_url("recap/b.pdf?x=1") is None


def test_cli_fetch_filing_without_text_downloads_and_ingests_pdf(monkeypatch):
    from uuid import uuid4

    from openfoia.pipeline.ingest import DocumentIngester, IngestResult

    monkeypatch.setenv(cl.KEY_ENV, "k")
    doc = fx("recap_document")
    doc["plain_text"] = ""
    api = routes({"/api/rest/v4/recap-documents/2974077/": doc})

    def responder(request):
        if request.url.host == "storage.courtlistener.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"%PDF-1.4 scanned")
        return api(request)

    sent = serve(monkeypatch, responder)
    ingested = []

    async def fake_ingest(self, path, *args, **kwargs):
        ingested.append(path)
        return IngestResult(
            document_id=str(uuid4()),
            filename=path.name,
            file_path=str(path),
            file_size=path.stat().st_size,
            mime_type="application/pdf",
            page_count=1,
            extracted_text="OCR text of the complaint",
            checksum="x",
            metadata={},
        )

    monkeypatch.setattr(DocumentIngester, "ingest_file", fake_ingest)
    r = runner.invoke(app, ["records", "fetch", "rd:2974077", "-s", "courtlistener"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.output.split())  # Rich wraps long lines
    assert "downloading the PDF from storage.courtlistener.com" in out
    assert "API token not sent" in out and "saved" in out
    assert [q.url.host for q in sent] == ["www.courtlistener.com", "storage.courtlistener.com"]
    assert ingested and ingested[0].name == "courtlistener-rd-2974077.pdf"

    r = runner.invoke(app, ["records", "fetch", "rd:2974077", "-s", "courtlistener"])
    assert "already stored (--refresh" in r.output and len(sent) == 2


def test_openfoia_search_skips_courtlistener_without_a_usable_token(monkeypatch):
    """`openfoia search` must not send the query only to get a 401 back."""
    from openfoia.net import EgressMode, EgressPolicy
    from openfoia.search import plan_search

    monkeypatch.delenv("OPENFOIA_COURTLISTENER_API_KEY", raising=False)
    plan = plan_search("Acme", ["courtlistener"], policy=EgressPolicy())
    assert plan.adapters == {}
    assert "OPENFOIA_COURTLISTENER_API_KEY" in plan.skipped["courtlistener"]

    monkeypatch.setenv("OPENFOIA_COURTLISTENER_API_KEY", "secret-token")
    plan = plan_search("Acme", ["courtlistener"], policy=EgressPolicy(mode=EgressMode.TOR))
    assert plan.skipped == {"courtlistener": "personal API token not sent over Tor"}
    assert "courtlistener" in plan.withheld_skipped

    plan = plan_search("Acme", ["courtlistener"], policy=EgressPolicy())
    assert list(plan.adapters) == ["courtlistener"]
    assert any("identifies you" in line for line in plan.warning_lines())


def test_clean_line_keeps_zero_width_joiners_but_drops_controls():
    from openfoia.records.courtlistener import clean_line

    assert clean_line("بنیامین\u200cپور") == "بنیامین\u200cپور"
    assert clean_line("a\x1b]52;c;cm0gLXJmIH4=\x07b\u202e") == "a]52;c;cm0gLXJmIH4=b"


def test_storage_pdf_download_passes_the_egress_policy(monkeypatch, tmp_path):
    """The A13 DNS check needs the route: in direct mode the host must resolve to public addresses."""
    import asyncio

    from openfoia.net import EgressPolicy
    from openfoia.records import courtlistener as cl

    seen = {}

    async def fake(client, url, dest, max_bytes, *, egress=None):
        seen["egress"] = egress

    monkeypatch.setattr(cl, "download_to_file", fake)
    policy = EgressPolicy()
    adapter = cl.CourtListenerAdapter(api_key="k", egress=policy)
    entity = cl.RecordEntity(
        source="courtlistener",
        entity_type="DOCUMENT",
        name="x",
        identifiers={"recap_document": "rd:7"},
        extra_data={"pdf_url": "https://storage.courtlistener.com/recap/gov.uscourts.x/1.pdf"},
    )
    asyncio.run(adapter.download_pdf(entity, tmp_path))
    assert seen["egress"] is policy
