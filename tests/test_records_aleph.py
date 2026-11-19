"""OCCRP Aleph adapter: parsing, key handling, filters, budget, 429s, FtM import, hostile data.

All HTTP goes through a mocked transport behind the real egress_client; nothing
touches the network. Search and entity fixtures follow the open-source Aleph
API (see tests/fixtures/aleph.json); the 401 bodies are the live ones.
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
from openfoia.records import aleph as al
from openfoia.records import get_adapter, keyed_api
from openfoia.records.aleph import AlephAdapter

runner = CliRunner()
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "aleph.json").read_text())
COMPANY_ID = FIXTURES["entity"]["id"]
TOR = EgressPolicy(mode=EgressMode.TOR)


def fx(name):
    return copy.deepcopy(FIXTURES[name])


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(al.KEY_ENV, raising=False)
    monkeypatch.delenv(al.BUDGET_ENV, raising=False)
    sleeps: list[float] = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(keyed_api, "_sleep", record_sleep)
    return sleeps


@pytest.fixture
def sleeps(env):
    return env


def serve(monkeypatch, responder):
    """Route the adapter's egress_client to *responder*; returns the requests sent."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return responder(request)

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, **kw):
        return egress_client(None, timeout=timeout, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(keyed_api, "egress_client", fake_egress_client)
    return sent


def routes(table):
    def responder(request):
        item = table.get(request.url.path)
        if item is None:
            return httpx.Response(404, json={"message": "Not found", "status": "error"})
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    return responder


ENTITY_ROUTES = {
    f"/api/2/entities/{COMPANY_ID}": FIXTURES["entity"],
    f"/api/2/entities/{COMPANY_ID}/expand": FIXTURES["expand"],
}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_parses_entities_and_sends_the_key(monkeypatch):
    sent = serve(monkeypatch, routes({"/api/2/entities": fx("search")}))
    result = asyncio.run(AlephAdapter(api_key="k-55").search("acme", page_size=10))

    assert result.error is None and result.total_results == 2
    req = sent[0]
    assert str(req.url).startswith("https://aleph.occrp.org/api/2/entities?")
    assert dict(req.url.params) == {"q": "acme", "limit": "10", "offset": "0"}
    assert req.headers["authorization"] == "ApiKey k-55"
    company, person = result.entities
    assert company.source == "aleph" and company.entity_type == "ORGANIZATION"
    assert next(iter(company.identifiers.values())) == COMPANY_ID
    assert company.identifiers["registration_number"] == "01234567"
    assert company.name == "ACME HOLDINGS LIMITED" and company.jurisdiction == "gb"
    assert company.source_url == f"https://aleph.occrp.org/entities/{COMPANY_ID}"
    assert company.extra_data["dataset"] == "UK Companies House"
    assert company.extra_data["date"] == "2009-03-02"
    assert company.extra_data["snippet"] == "ACME HOLDINGS LIMITED"  # highlight tags stripped
    assert person.entity_type == "PERSON" and person.extra_data["countries"] == ["ru"]
    assert al.BUDGET.used()["a day"] == 1


def test_filters_map_onto_params_and_are_checked_locally(monkeypatch):
    sent = serve(monkeypatch, routes({"/api/2/entities": fx("search")}))
    result = asyncio.run(
        AlephAdapter(api_key="k").search(
            "acme", page=2, page_size=10, filing_type="Company", jurisdiction="UK"
        )
    )
    params = dict(sent[0].url.params)
    assert params["schema"] == "Company" and params["filter:countries"] == "gb"
    assert params["offset"] == "10"
    # The Person from Russia does not match: an ignored filter must not look applied.
    assert [e.name for e in result.entities] == ["ACME HOLDINGS LIMITED"]
    assert result.raw_response["filtered_out_locally"] == 1

    people = asyncio.run(
        AlephAdapter(api_key="k").search("acme", filing_type="person", jurisdiction="ru")
    )
    assert [e.name for e in people.entities] == ["Ivan Petrov"]


@pytest.mark.parametrize(
    "kwargs, message",
    [({"filing_type": "10-K"}, "Unknown Aleph type"), ({"jurisdiction": "us_ca"}, "country code")],
)
def test_bad_filters_are_errors_and_send_nothing(monkeypatch, kwargs, message):
    sent = serve(monkeypatch, routes({}))
    result = asyncio.run(AlephAdapter(api_key="k").search("x", **kwargs))
    assert result.error and message in result.error and sent == []


def test_next_link_in_a_response_is_never_followed(monkeypatch):
    body = fx("search")
    body["next"] = "https://evil.example/api/2/entities?q=acme&offset=10"
    sent = serve(monkeypatch, routes({"/api/2/entities": body}))
    adapter = AlephAdapter(api_key="k")
    asyncio.run(adapter.search("acme", page=1, page_size=2))
    asyncio.run(adapter.search("acme", page=2, page_size=2))
    assert [r.url.host for r in sent] == ["aleph.occrp.org", "aleph.occrp.org"]
    assert sent[1].url.params["offset"] == "2"


def test_api_error_is_not_empty_results(monkeypatch):
    serve(monkeypatch, lambda r: httpx.Response(500, json={"message": "boom", "status": "error"}))
    failed = asyncio.run(AlephAdapter(api_key="k").search("x"))
    assert failed.error and "HTTP 500" in failed.error and "boom" in failed.error
    assert failed.entities == []

    sent = serve(
        monkeypatch, lambda r: httpx.Response(302, headers={"Location": "https://evil.example/x"})
    )
    redirected = asyncio.run(AlephAdapter(api_key="k").search("x"))
    assert "redirect (HTTP 302); not followed" in redirected.error
    assert [r.url.host for r in sent] == ["aleph.occrp.org"]

    serve(monkeypatch, lambda r: httpx.Response(200, json={"results": [], "total": 0}))
    empty = asyncio.run(AlephAdapter(api_key="k").search("x"))
    assert empty.error is None and empty.total_results == 0 and empty.entities == []

    serve(monkeypatch, lambda r: httpx.Response(200, json=[1, 2]))
    assert "unexpected JSON" in asyncio.run(AlephAdapter(api_key="k").search("x")).error

    serve(monkeypatch, lambda r: httpx.Response(200, json={"status": "ok"}))
    assert "no result list" in asyncio.run(AlephAdapter(api_key="k").search("x")).error


def test_live_401_bodies_read_as_key_errors(monkeypatch):
    serve(
        monkeypatch,
        lambda r: httpx.Response(
            401, text=FIXTURES["invalid_key_401"], headers={"content-type": "text/plain"}
        ),
    )
    result = asyncio.run(AlephAdapter(api_key="k").search("x"))
    assert "rejected the API key (HTTP 401)" in result.error and al.KEY_ENV in result.error
    assert "invalid credentials" in result.error and "<pre>" not in result.error

    serve(monkeypatch, lambda r: httpx.Response(401, json=fx("unauthenticated_401")))
    assert "not authorized" in asyncio.run(AlephAdapter(api_key="k").search("x")).error

    serve(monkeypatch, lambda r: httpx.Response(403, json={"message": "Forbidden"}))
    assert "extended access" in asyncio.run(AlephAdapter(api_key="k").search("x")).error


def test_cli_distinguishes_api_error_from_no_results(monkeypatch):
    monkeypatch.setenv(al.KEY_ENV, "k")
    serve(monkeypatch, lambda r: httpx.Response(503, json={"message": "down"}))
    r = runner.invoke(app, ["records", "search", "x", "-s", "aleph"])
    assert r.exit_code == 1 and "HTTP 503" in r.output and "No results found" not in r.output

    serve(monkeypatch, lambda r: httpx.Response(200, json={"results": [], "total": 0}))
    r = runner.invoke(app, ["records", "search", "x", "-s", "aleph"])
    assert r.exit_code == 0, r.output
    assert "No results found" in r.output and "identifies your OCCRP account" in r.output


def test_cli_search_table_shows_fetchable_ids(monkeypatch):
    monkeypatch.setenv(al.KEY_ENV, "k")
    serve(monkeypatch, routes({"/api/2/entities": fx("search")}))
    r = runner.invoke(app, ["records", "search", "acme", "-s", "aleph", "--json"])
    assert r.exit_code == 0, r.output
    rows = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
    assert rows[0]["id"] == COMPANY_ID and rows[0]["source"] == "aleph"


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------


def test_missing_key_explains_and_sends_nothing(monkeypatch):
    sent = serve(monkeypatch, routes({}))
    result = asyncio.run(AlephAdapter().search("x"))
    assert result.error and al.REGISTER_URL in result.error and al.KEY_ENV in result.error
    with pytest.raises(al.AdapterRequestError, match=al.KEY_ENV):
        asyncio.run(AlephAdapter().fetch(COMPANY_ID))

    r = runner.invoke(app, ["records", "search", "x", "-s", "aleph"])
    assert r.exit_code == 1 and al.KEY_ENV in r.output and "Sending" not in r.output
    r = runner.invoke(app, ["records", "fetch", COMPANY_ID, "-s", "aleph"])
    assert r.exit_code == 1 and al.KEY_ENV in r.output
    assert sent == []


def test_key_comes_from_env_only(monkeypatch):
    monkeypatch.setenv(al.KEY_ENV, "from-env")
    assert get_adapter("aleph").has_key
    assert get_adapter("aleph", api_data_gov_key="data-gov")._api_key == "from-env"


@pytest.mark.parametrize("tor", [True, False])
def test_key_withheld_over_tor(monkeypatch, tor):
    monkeypatch.setenv(al.KEY_ENV, "aleph-55")
    sent = serve(monkeypatch, routes({"/api/2/entities": fx("search")}))
    adapter = AlephAdapter(egress=TOR if tor else EgressPolicy())
    result = asyncio.run(adapter.search("x"))
    assert adapter.credentials_withheld is tor and adapter.has_key is not tor
    if tor:
        assert sent == [] and "not used over Tor" in result.error
        assert adapter.unavailable_reason() == "personal API key not sent over Tor"
    else:
        assert sent and sent[0].headers["authorization"] == "ApiKey aleph-55"


def test_openfoia_search_skips_aleph_without_a_usable_key(monkeypatch):
    from openfoia.search import plan_search

    plan = plan_search("Acme", ["aleph"], policy=EgressPolicy())
    assert plan.adapters == {} and al.KEY_ENV in plan.skipped["aleph"]

    monkeypatch.setenv(al.KEY_ENV, "secret")
    plan = plan_search("Acme", ["aleph"], policy=TOR)
    assert plan.skipped == {"aleph": "personal API key not sent over Tor"}
    assert "aleph" in plan.withheld_skipped

    plan = plan_search("Acme", ["aleph"], policy=EgressPolicy())
    assert list(plan.adapters) == ["aleph"]
    assert any("identifies your OCCRP account" in line for line in plan.warning_lines())


def test_all_sources_skips_aleph_without_key(monkeypatch):
    sent = serve(monkeypatch, routes({}))
    monkeypatch.setattr("openfoia.records.list_sources", lambda: ["aleph"])
    r = runner.invoke(app, ["records", "search", "x", "--all-sources"])
    assert f"skipped (no {al.KEY_ENV})" in r.output and sent == []


def test_key_is_redacted_from_transport_errors(monkeypatch):
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.headers['authorization']}")

    serve(monkeypatch, boom)
    result = asyncio.run(AlephAdapter(api_key="secret-key").search("x"))
    assert "secret-key" not in result.error and "<redacted>" in result.error


# ---------------------------------------------------------------------------
# Fetch and store
# ---------------------------------------------------------------------------


def test_fetch_entity_with_neighbours(monkeypatch):
    sent = serve(monkeypatch, routes(ENTITY_ROUTES))
    entity = asyncio.run(
        AlephAdapter(api_key="k").fetch(f"https://aleph.occrp.org/entities/{COMPANY_ID}")
    )
    assert [r.url.path for r in sent] == [
        f"/api/2/entities/{COMPANY_ID}",
        f"/api/2/entities/{COMPANY_ID}/expand",
    ]
    x = entity.extra_data
    assert x["errors"] == []
    names = {r["name"] for r in x["related"]}
    assert {"Ivan Petrov", "Acme Offshore Ltd"} <= names
    schemas = {e["schema"] for e in x["ftm_entities"]}
    assert {"Company", "Directorship", "Ownership", "Person"} <= schemas
    directorship = next(e for e in x["ftm_entities"] if e["schema"] == "Directorship")
    assert directorship["properties"]["director"] == ["b7e4f1c2d3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8"]
    text = x["full_text"]
    assert "previousName: ACME TRADING LIMITED" in text and "Credit: According to data" in text
    assert "directorshipOrganization > director: Person Ivan Petrov" in text


def test_entity_kept_when_expand_fails(monkeypatch):
    table = dict(ENTITY_ROUTES)
    table[f"/api/2/entities/{COMPANY_ID}/expand"] = httpx.Response(500, json={"message": "x"})
    serve(monkeypatch, routes(table))
    entity = asyncio.run(AlephAdapter(api_key="k").fetch(COMPANY_ID))
    assert "related entities not retrieved" in entity.extra_data["errors"][0]
    assert "NOTE: related entities not retrieved" in entity.extra_data["full_text"]


def test_unknown_entity_is_none_not_an_error(monkeypatch):
    serve(monkeypatch, routes({}))
    assert asyncio.run(AlephAdapter(api_key="k").fetch("deadbeef")) is None


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../../roles/me",
        "abc/def",
        "a..b",
        "abc?x=1",
        "https://evil.example/entities/abc",
        "http://aleph.occrp.org/entities/abc",
        "https://aleph.occrp.org/api/2/roles/me",
        "abc‮",
    ],
)
def test_malformed_ids_are_refused_before_any_request(monkeypatch, bad):
    sent = serve(monkeypatch, routes({}))
    with pytest.raises(ValueError):
        asyncio.run(AlephAdapter(api_key="k").fetch(bad))
    assert sent == []


def test_cli_fetch_stores_document_and_imports_ftm_graph(monkeypatch):
    from openfoia.db import get_session
    from openfoia.models import Entity, entity_links

    monkeypatch.setenv(al.KEY_ENV, "k")
    sent = serve(monkeypatch, routes(ENTITY_ROUTES))
    assert runner.invoke(app, ["case", "new", "Offshore"]).exit_code == 0
    r = runner.invoke(app, ["records", "fetch", COMPANY_ID, "-s", "aleph"])
    assert r.exit_code == 0, r.output
    assert "saved" in r.output and "Attached to the active case" in r.output
    assert "identifies your OCCRP account" in r.output

    n = len(sent)
    r = runner.invoke(app, ["records", "fetch", f"aleph:{COMPANY_ID}", "-s", "aleph"])
    assert "already stored (--refresh" in r.output and len(sent) == n

    r = runner.invoke(app, ["docs", "grep", "ACME TRADING"])
    assert "ACME TRADING" in r.output
    with get_session() as session:
        names = {e.normalized_text for e in session.query(Entity)}
        links = session.query(entity_links).count()
    assert {"ACME HOLDINGS LIMITED", "Ivan Petrov", "Acme Offshore Ltd"} <= names
    assert links == 2  # Directorship and Ownership
    assert list((Path(str(al.BUDGET.path().parent)) / "downloads" / "ftm").glob("aleph-*.ftm.json"))


# ---------------------------------------------------------------------------
# Budget and rate limits
# ---------------------------------------------------------------------------


def _write_budget(times, blocked_until=None):
    path = al.BUDGET.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sent": times, "blocked_until": blocked_until}))


def test_daily_budget_exhausted_refuses_without_sending(monkeypatch):
    sent = serve(monkeypatch, routes({"/api/2/entities": fx("search")}))
    now = time.time()
    _write_budget([now - 80_000 + i for i in range(al.DAY_LIMIT)])
    result = asyncio.run(AlephAdapter(api_key="k").search("x"))
    assert sent == [] and "budget used up: 500 of 500" in result.error
    assert "Nothing was sent" in result.error and al.BUDGET_ENV in result.error

    monkeypatch.setenv(al.BUDGET_ENV, "1")
    assert asyncio.run(AlephAdapter(api_key="k").search("x")).error is None
    assert len(sent) == 1


def test_minute_window_waits(monkeypatch, sleeps):
    sent = serve(monkeypatch, routes({"/api/2/entities": fx("search")}))
    now = time.time()
    _write_budget([now - 50 + i * 0.1 for i in range(30)])
    assert asyncio.run(AlephAdapter(api_key="k").search("x")).error is None
    assert len(sent) == 1 and len(sleeps) == 1 and 5 <= sleeps[0] <= 11


def test_429_short_retry_after_is_waited_then_retried(monkeypatch, sleeps):
    responses = [
        httpx.Response(429, headers={"Retry-After": "40"}, json={"message": "slow down"}),
        httpx.Response(200, json=fx("search")),
    ]
    sent = serve(monkeypatch, lambda r: responses.pop(0))
    result = asyncio.run(AlephAdapter(api_key="k").search("x"))
    assert result.error is None and len(sent) == 2 and sleeps == [40.0]


def test_429_long_wait_fails_and_blocks_later_calls(monkeypatch, sleeps):
    sent = serve(
        monkeypatch,
        lambda r: httpx.Response(429, headers={"Retry-After": "3600"}, json={"message": "limit"}),
    )
    adapter = AlephAdapter(api_key="k")
    result = asyncio.run(adapter.search("x"))
    assert "rate limit reached (HTTP 429)" in result.error and "Try again after" in result.error
    assert sleeps == [] and len(sent) == 1
    again = asyncio.run(adapter.search("x"))
    assert len(sent) == 1 and "Nothing was sent" in again.error


# ---------------------------------------------------------------------------
# Untrusted strings and sizes
# ---------------------------------------------------------------------------


def test_hostile_strings_are_cleaned_and_bad_ids_dropped(monkeypatch):
    data = fx("search")
    hit = data["results"][0]
    hit["properties"]["name"] = ["Evil \x1b]52;c;cm0gLXJm\x07\x1b[2J [bold red]Markup[/bold red]‮"]
    hit["properties"]["registrationNumber"] = ["9" * 100_000]
    data["results"][1]["id"] = "../../roles/me"
    monkeypatch.setenv(al.KEY_ENV, "k")
    serve(monkeypatch, routes({"/api/2/entities": data}))

    result = asyncio.run(AlephAdapter().search("x"))
    assert len(result.entities) == 1
    e = result.entities[0]
    assert "\x1b" not in e.name and "\x07" not in e.name and "‮" not in e.name
    assert len(e.identifiers["registration_number"]) <= 100

    r = runner.invoke(app, ["records", "search", "x", "-s", "aleph"])
    assert r.exit_code == 0, r.output
    assert "\x1b" not in r.output and "[bold" in r.output  # shown as text, not applied


def test_error_detail_cannot_break_rich_markup(monkeypatch):
    monkeypatch.setenv(al.KEY_ENV, "k")
    serve(monkeypatch, lambda r: httpx.Response(500, json={"message": "[/bold] \x1b[2J gone"}))
    r = runner.invoke(app, ["records", "search", "x", "-s", "aleph"])
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit), r.output
    assert "(/bold)" in r.output and "\x1b" not in r.output
    r = runner.invoke(app, ["records", "fetch", COMPANY_ID, "-s", "aleph"])
    assert "(/bold)" in r.output and isinstance(r.exception, SystemExit)


def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.setattr(al, "MAX_RESPONSE_BYTES", 1000)
    serve(
        monkeypatch, lambda r: httpx.Response(200, content=b'{"results": [' + b" " * 5000 + b"]}")
    )
    result = asyncio.run(AlephAdapter(api_key="k").search("x"))
    assert result.error and "refusing to read it" in result.error


def test_stored_ftm_values_are_cleaned(monkeypatch):
    from openfoia.records.ftm_store import clean_ftm

    cleaned = clean_ftm(
        {
            "id": "x1",
            "schema": "Person",
            "properties": {
                "name": ["A\x1b[31mB‮"],
                "Bad Key": ["dropped"],
                "nationality": "ru",
                "notes": [True, None, {"id": "ref-1"}],
            },
        }
    )
    assert cleaned == {
        "id": "x1",
        "schema": "Person",
        "properties": {"name": ["A[31mB"], "nationality": ["ru"], "notes": ["ref-1"]},
    }
    assert clean_ftm({"id": "x", "schema": "evil; drop"}) is None
