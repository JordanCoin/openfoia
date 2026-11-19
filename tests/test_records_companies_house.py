"""UK Companies House adapter: parsing, Basic-auth key, budget, 429s, FtM import, hostile data.

All HTTP goes through a mocked transport behind the real egress_client; nothing
touches the network. Fixtures are written from Companies House's published
schemas (tests/fixtures/companies_house.json); the 401 body is the live one.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.net import EgressMode, EgressPolicy, egress_client
from openfoia.records import companies_house as ch
from openfoia.records import get_adapter, keyed_api
from openfoia.records.companies_house import CompaniesHouseAdapter

runner = CliRunner()
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "companies_house.json").read_text())
TOR = EgressPolicy(mode=EgressMode.TOR)
OFFICER = "AbCdEf123456_-xyz"


def fx(name):
    return copy.deepcopy(FIXTURES[name])


def basic(key):
    return "Basic " + base64.b64encode(f"{key}:".encode()).decode()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(ch.KEY_ENV, raising=False)
    monkeypatch.delenv(ch.BUDGET_ENV, raising=False)
    sleeps: list[float] = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(keyed_api, "_sleep", record_sleep)
    return sleeps


@pytest.fixture
def sleeps(env):
    return env


def serve(monkeypatch, responder):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return responder(request)

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, **kw):
        # Same policy (retry count included), DIRECT so the mock transport is used.
        direct = replace(policy, mode=EgressMode.DIRECT) if policy else None
        return egress_client(direct, timeout=timeout, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(keyed_api, "egress_client", fake_egress_client)
    return sent


def routes(table):
    def responder(request):
        item = table.get(request.url.path)
        if item is None:
            return httpx.Response(404, json={"errors": [{"error": "company-profile-not-found"}]})
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    return responder


COMPANY_ROUTES = {
    "/company/01234567": FIXTURES["company_profile"],
    "/company/01234567/officers": FIXTURES["officers"],
    "/company/01234567/persons-with-significant-control": FIXTURES["pscs"],
    "/company/01234567/filing-history": FIXTURES["filing_history"],
    f"/officers/{OFFICER}/appointments": FIXTURES["appointments"],
}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_company_search_parses_and_sends_basic_auth(monkeypatch):
    sent = serve(monkeypatch, routes({"/search/companies": fx("search_companies")}))
    result = asyncio.run(CompaniesHouseAdapter(api_key="ch-55").search("acme", page_size=10))

    assert result.error is None and result.total_results == 2
    req = sent[0]
    assert str(req.url).startswith(
        "https://api.company-information.service.gov.uk/search/companies?"
    )
    assert dict(req.url.params) == {"q": "acme", "items_per_page": "10", "start_index": "0"}
    assert req.headers["authorization"] == basic("ch-55")
    acme, scot = result.entities
    assert acme.source == "companieshouse" and acme.entity_type == "ORGANIZATION"
    assert next(iter(acme.identifiers.values())) == "01234567"
    assert acme.status == "active" and acme.jurisdiction == "gb"
    assert acme.source_url == f"{ch.SITE}/company/01234567"
    assert acme.extra_data["date"] == "2009-03-02" and acme.extra_data["company_type"] == "ltd"
    assert scot.extra_data["date_of_cessation"] == "2019-02-05"
    assert ch.BUDGET.used()["5 minutes"] == 1


def test_officer_search_and_page_mapping(monkeypatch):
    sent = serve(monkeypatch, routes({"/search/officers": fx("search_officers")}))
    result = asyncio.run(
        CompaniesHouseAdapter(api_key="k").search(
            "jane smith", page=3, page_size=20, filing_type="officer"
        )
    )
    assert sent[0].url.path == "/search/officers" and sent[0].url.params["start_index"] == "40"
    e = result.entities[0]
    assert next(iter(e.identifiers.values())) == f"officer:{OFFICER}"
    assert e.extra_data["date_of_birth"] == "1968-04" and e.extra_data["appointment_count"] == 3


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/officers/AbCdEf123456/appointments",
        "/officers/../../company/x/appointments",
        "/officers/AbCdEf123456/appointments?x=@evil.example",
        "//evil.example/officers/AbCdEf123456/appointments",
    ],
)
def test_officer_hits_with_hostile_links_are_dropped(monkeypatch, link):
    data = fx("search_officers")
    data["items"][0]["links"]["self"] = link
    serve(monkeypatch, routes({"/search/officers": data}))
    result = asyncio.run(CompaniesHouseAdapter(api_key="k").search("x", filing_type="officers"))
    assert result.error is None and result.entities == []


def test_unknown_search_type_sends_nothing(monkeypatch):
    sent = serve(monkeypatch, routes({}))
    result = asyncio.run(CompaniesHouseAdapter(api_key="k").search("x", filing_type="10-K"))
    assert "Unknown Companies House search type" in result.error and sent == []


def test_api_error_is_not_empty_results(monkeypatch):
    serve(
        monkeypatch, lambda r: httpx.Response(500, json={"error": "Internal", "type": "ch:service"})
    )
    failed = asyncio.run(CompaniesHouseAdapter(api_key="k").search("x"))
    assert "HTTP 500" in failed.error and "Internal" in failed.error and failed.entities == []

    sent = serve(
        monkeypatch, lambda r: httpx.Response(302, headers={"Location": "https://evil.example/"})
    )
    redirected = asyncio.run(CompaniesHouseAdapter(api_key="k").search("x"))
    assert "redirect (HTTP 302); not followed" in redirected.error
    assert [r.url.host for r in sent] == [ch.API_HOST]

    serve(monkeypatch, lambda r: httpx.Response(200, json={"total_results": 0, "items": []}))
    empty = asyncio.run(CompaniesHouseAdapter(api_key="k").search("x"))
    assert empty.error is None and empty.total_results == 0 and empty.entities == []

    serve(monkeypatch, lambda r: httpx.Response(401, json=fx("unauthenticated_401")))
    rejected = asyncio.run(CompaniesHouseAdapter(api_key="k").search("x"))
    assert "rejected the API key (HTTP 401)" in rejected.error and ch.KEY_ENV in rejected.error
    assert "Empty Authorization header" in rejected.error


def test_cli_distinguishes_api_error_from_no_results(monkeypatch):
    monkeypatch.setenv(ch.KEY_ENV, "k")
    serve(monkeypatch, lambda r: httpx.Response(503, json={"error": "down"}))
    r = runner.invoke(app, ["records", "search", "x", "-s", "companieshouse"])
    assert r.exit_code == 1 and "HTTP 503" in r.output and "No results found" not in r.output

    serve(monkeypatch, lambda r: httpx.Response(200, json={"total_results": 0, "items": []}))
    r = runner.invoke(app, ["records", "search", "x", "-s", "companieshouse"])
    assert r.exit_code == 0 and "No results found" in r.output
    assert "identifies you to Companies House" in r.output


def test_cli_limit_pages_the_search(monkeypatch):
    monkeypatch.setenv(ch.KEY_ENV, "k")
    sent = serve(monkeypatch, routes({"/search/companies": fx("search_companies")}))
    r = runner.invoke(app, ["records", "search", "acme", "-s", "companieshouse", "-n", "5"])
    assert r.exit_code == 0, r.output
    assert sent[0].url.params["items_per_page"] == "5"
    assert "01234567" in r.output, r.output
    assert "SC123456" in r.output and "HOLDINGS" in r.output


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------


def test_missing_key_explains_and_sends_nothing(monkeypatch):
    sent = serve(monkeypatch, routes({}))
    result = asyncio.run(CompaniesHouseAdapter().search("x"))
    assert ch.KEY_ENV in result.error and ch.KEY_URL in result.error
    with pytest.raises(ch.AdapterRequestError, match=ch.KEY_ENV):
        asyncio.run(CompaniesHouseAdapter().fetch("01234567"))
    r = runner.invoke(app, ["records", "search", "x", "-s", "companieshouse"])
    assert r.exit_code == 1 and ch.KEY_ENV in r.output and "Sending" not in r.output
    r = runner.invoke(app, ["records", "fetch", "01234567", "-s", "companieshouse"])
    assert r.exit_code == 1 and ch.KEY_ENV in r.output
    assert sent == []


def test_key_comes_from_env_only(monkeypatch):
    monkeypatch.setenv(ch.KEY_ENV, "from-env")
    assert get_adapter("companieshouse").has_key
    assert get_adapter("companieshouse", api_data_gov_key="x")._api_key == "from-env"


@pytest.mark.parametrize("tor", [True, False])
def test_key_withheld_over_tor(monkeypatch, tor):
    monkeypatch.setenv(ch.KEY_ENV, "ch-55")
    sent = serve(monkeypatch, routes({"/search/companies": fx("search_companies")}))
    adapter = CompaniesHouseAdapter(egress=TOR if tor else EgressPolicy())
    result = asyncio.run(adapter.search("x"))
    assert adapter.credentials_withheld is tor and adapter.has_key is not tor
    if tor:
        assert sent == [] and "not used over Tor" in result.error
    else:
        assert sent and sent[0].headers["authorization"] == basic("ch-55")


def test_openfoia_search_skips_without_a_usable_key(monkeypatch):
    from openfoia.search import plan_search, run_search

    plan = plan_search("Acme", ["companieshouse"], policy=EgressPolicy())
    assert plan.adapters == {} and ch.KEY_ENV in plan.skipped["companieshouse"]

    monkeypatch.setenv(ch.KEY_ENV, "secret")
    plan = plan_search("Acme", ["companieshouse"], policy=TOR)
    assert plan.skipped == {"companieshouse": "personal API key not sent over Tor"}

    sent = serve(monkeypatch, routes({"/search/companies": fx("search_companies")}))
    plan = plan_search("Acme", ["companieshouse"], policy=EgressPolicy())
    assert any("identifies you to Companies House" in line for line in plan.warning_lines())
    report = asyncio.run(run_search(plan, limit=2, allow_network=True, log_to_case=False))
    assert report.statuses[0].status == "ok" and report.rows[0]["id"] == "01234567"
    assert sent[0].url.params["items_per_page"] == "2"


def test_key_and_basic_header_are_redacted_from_errors(monkeypatch):
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.headers['authorization']} secret-key")

    serve(monkeypatch, boom)
    result = asyncio.run(CompaniesHouseAdapter(api_key="secret-key").search("x"))
    assert "secret-key" not in result.error and basic("secret-key") not in result.error
    assert "<redacted>" in result.error


# ---------------------------------------------------------------------------
# Fetch and store
# ---------------------------------------------------------------------------


def test_fetch_company_reads_officers_owners_and_filings(monkeypatch):
    sent = serve(monkeypatch, routes(COMPANY_ROUTES))
    entity = asyncio.run(CompaniesHouseAdapter(api_key="k").fetch("1234567"))
    assert [r.url.path for r in sent] == [
        "/company/01234567",
        "/company/01234567/officers",
        "/company/01234567/persons-with-significant-control",
        "/company/01234567/filing-history",
    ]
    assert len(sent) == ch.COMPANY_FETCH_REQUESTS
    assert dict(sent[2].url.params) == {
        "items_per_page": "100",
        "start_index": "0",
        "register_view": "false",
    }
    x = entity.extra_data
    assert x["errors"] == []
    assert [o["id"] for o in x["officers"]] == [OFFICER, "QrStUv987654", "MnOpQr111222"]
    assert x["pscs"][0]["natures_of_control"][0] == "ownership of shares 75 to 100 percent"
    assert x["filings"][0]["document_metadata"].endswith("/document/AbC123dEf456")
    text = x["full_text"]
    assert "Previous name: ACME TRADING LIMITED (2009-03-02 to 2012-07-01)" in text
    assert "Flags: confirmation statement overdue, has charges" in text
    assert "- SMITH, Jane Mary (director; appointed 2015-01-12)" in text
    assert "- Mr Ivan PETROV (individual person with significant control" in text
    assert "CS01  confirmation statement: confirmation statement with no updates" in text
    schemas = [e["schema"] for e in x["ftm_entities"]]
    assert schemas.count("Directorship") == 3 and schemas.count("Ownership") == 2


def test_company_kept_when_a_list_fails(monkeypatch):
    table = dict(COMPANY_ROUTES)
    table["/company/01234567/officers"] = httpx.Response(500, json={"error": "boom"})
    del table["/company/01234567/persons-with-significant-control"]  # 404: none filed
    serve(monkeypatch, routes(table))
    entity = asyncio.run(CompaniesHouseAdapter(api_key="k").fetch("01234567"))
    assert entity.extra_data["officers"] == [] and entity.extra_data["pscs"] == []
    assert "officers not retrieved" in entity.extra_data["errors"][0]
    assert "NOTE: officers not retrieved" in entity.extra_data["full_text"]


def test_unknown_company_is_none(monkeypatch):
    serve(monkeypatch, routes({}))
    assert asyncio.run(CompaniesHouseAdapter(api_key="k").fetch("99999999")) is None


def test_hostile_document_links_are_refused(monkeypatch):
    table = dict(COMPANY_ROUTES)
    filings = fx("filing_history")
    filings["items"][0]["links"]["document_metadata"] = "https://evil.example/document/x"
    filings["items"][1]["links"]["document_metadata"] = (
        "https://document-api.company-information.service.gov.uk/document/../../x"
    )
    table["/company/01234567/filing-history"] = filings
    serve(monkeypatch, routes(table))
    entity = asyncio.run(CompaniesHouseAdapter(api_key="k").fetch("01234567"))
    assert all(f["document_metadata"] is None for f in entity.extra_data["filings"])
    assert "2 filing document link(s) refused" in entity.extra_data["errors"][0]


def test_fetch_officer_appointments(monkeypatch):
    sent = serve(monkeypatch, routes(COMPANY_ROUTES))
    entity = asyncio.run(
        CompaniesHouseAdapter(api_key="k").fetch(f"{ch.SITE}/officers/{OFFICER}/appointments")
    )
    assert [r.url.path for r in sent] == [f"/officers/{OFFICER}/appointments"]
    assert entity.entity_type == "PERSON" and entity.name == "Jane Mary SMITH"
    numbers = [a["company_number"] for a in entity.extra_data["appointments"]]
    assert numbers == ["01234567", "11223344"]
    assert "WIDGET SHELL LTD (11223344, dissolved): director" in entity.extra_data["full_text"]


def test_identifier_forms():
    assert ch.canonical_id("6") == "00000006"
    assert ch.canonical_id(" sc123456 ") == "SC123456"
    assert ch.canonical_id(f"{ch.SITE}/company/SC123456/officers") == "SC123456"
    assert ch.canonical_id(f"officer:{OFFICER}") == f"officer:{OFFICER}"
    for bad in (
        "",
        "123456789",
        "../x",
        "SC12/456",
        "officer:../../x",
        "officer:a",
        "rd:123",
        "https://evil.example/company/00000006",
        "http://find-and-update.company-information.service.gov.uk/company/00000006",
        "١٢٣",
    ):
        with pytest.raises(ValueError):
            ch.canonical_id(bad)


def test_cli_fetch_stores_company_and_imports_graph(monkeypatch):
    from openfoia.db import get_session
    from openfoia.models import Entity, entity_links

    monkeypatch.setenv(ch.KEY_ENV, "k")
    sent = serve(monkeypatch, routes(COMPANY_ROUTES))
    assert runner.invoke(app, ["case", "new", "Shells"]).exit_code == 0
    r = runner.invoke(
        app, ["records", "fetch", "01234567", f"officer:{OFFICER}", "-s", "companieshouse"]
    )
    assert r.exit_code == 0, r.output
    assert r.output.count("saved") == 2 and "Attached to the active case" in r.output

    n = len(sent)
    r = runner.invoke(app, ["records", "fetch", "1234567", "-s", "companieshouse"])
    assert "already stored (--refresh" in r.output and len(sent) == n

    r = runner.invoke(app, ["docs", "grep", "WIDGET SHELL"])
    assert "WIDGET SHELL" in r.output
    with get_session() as session:
        names = {e.normalized_text for e in session.query(Entity)}
        links = session.query(entity_links).count()
    assert {
        "ACME HOLDINGS LIMITED",
        "SMITH, Jane Mary",
        "Mr Ivan PETROV",
        "WIDGET SHELL LTD",
    } <= names
    assert links >= 5


# ---------------------------------------------------------------------------
# Budget and rate limits
# ---------------------------------------------------------------------------


def _write_budget(times, blocked_until=None):
    path = ch.BUDGET.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sent": times, "blocked_until": blocked_until}))


def test_budget_exhausted_refuses_without_sending(monkeypatch):
    sent = serve(monkeypatch, routes({"/search/companies": fx("search_companies")}))
    now = time.time()
    _write_budget([now - 100 + i * 0.01 for i in range(ch.WINDOW_LIMIT)])
    result = asyncio.run(CompaniesHouseAdapter(api_key="k").search("x"))
    assert sent == [] and "budget used up: 600 of 600 requests in 5 minutes" in result.error
    assert "Nothing was sent" in result.error and "frees up at" in result.error


def test_budget_nearly_free_waits_instead(monkeypatch, sleeps):
    sent = serve(monkeypatch, routes({"/search/companies": fx("search_companies")}))
    now = time.time()
    _write_budget([now - 280 + i * 0.01 for i in range(ch.WINDOW_LIMIT)])
    assert asyncio.run(CompaniesHouseAdapter(api_key="k").search("x")).error is None
    assert len(sent) == 1 and len(sleeps) == 1 and 15 <= sleeps[0] <= 21


def test_429_without_retry_after_blocks_the_rest_of_the_window(monkeypatch, sleeps):
    sent = serve(monkeypatch, lambda r: httpx.Response(429, json={"error": "too many"}))
    adapter = CompaniesHouseAdapter(api_key="k")
    result = asyncio.run(adapter.search("x"))
    assert "rate limit reached (HTTP 429)" in result.error and "600 requests per 5" in result.error
    assert sleeps == [] and len(sent) == 1
    again = asyncio.run(adapter.search("y"))
    assert len(sent) == 1 and "Nothing was sent" in again.error


def test_429_short_retry_after_is_waited_then_retried(monkeypatch, sleeps):
    responses = [
        httpx.Response(429, headers={"Retry-After": "10"}, json={"error": "slow"}),
        httpx.Response(200, json=fx("search_companies")),
    ]
    sent = serve(monkeypatch, lambda r: responses.pop(0))
    assert asyncio.run(CompaniesHouseAdapter(api_key="k").search("x")).error is None
    assert len(sent) == 2 and sleeps == [10.0]


# ---------------------------------------------------------------------------
# Untrusted strings and sizes
# ---------------------------------------------------------------------------


def test_hostile_strings_are_cleaned(monkeypatch):
    data = fx("search_companies")
    data["items"][0]["title"] = "Evil \x1b]0;pwned\x07\x1b[2J [link=https://x.example]Verify[/link]‮"
    data["items"][0]["address_snippet"] = "A" * 100_000
    data["items"][1]["company_number"] = "../../x"
    monkeypatch.setenv(ch.KEY_ENV, "k")
    serve(monkeypatch, routes({"/search/companies": data}))

    result = asyncio.run(CompaniesHouseAdapter().search("x"))
    assert len(result.entities) == 1
    e = result.entities[0]
    assert "\x1b" not in e.name and "‮" not in e.name
    assert len(e.extra_data["address"]) <= 300

    r = runner.invoke(app, ["records", "search", "x", "-s", "companieshouse"])
    assert r.exit_code == 0, r.output
    assert "\x1b" not in r.output and "[link=" in r.output


def test_error_detail_cannot_break_rich_markup(monkeypatch):
    monkeypatch.setenv(ch.KEY_ENV, "k")
    serve(monkeypatch, lambda r: httpx.Response(500, json={"error": "[/bold] \x1b[2J gone"}))
    r = runner.invoke(app, ["records", "search", "x", "-s", "companieshouse"])
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit), r.output
    assert "(/bold)" in r.output and "\x1b" not in r.output
    r = runner.invoke(app, ["records", "fetch", "01234567", "-s", "companieshouse"])
    assert "(/bold)" in r.output and isinstance(r.exception, SystemExit)


def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.setattr(ch, "MAX_RESPONSE_BYTES", 1000)
    serve(monkeypatch, lambda r: httpx.Response(200, content=b'{"items": [' + b" " * 5000 + b"]}"))
    result = asyncio.run(CompaniesHouseAdapter(api_key="k").search("x"))
    assert result.error and "refusing to read it" in result.error


def test_every_try_is_counted_so_transport_retries_are_off(monkeypatch):
    sent = serve(monkeypatch, lambda r: httpx.Response(503, json={"error": "busy"}))
    result = asyncio.run(CompaniesHouseAdapter(api_key="k").search("x"))
    assert "HTTP 503" in result.error
    assert len(sent) == 1 and ch.BUDGET.used()["5 minutes"] == 1
