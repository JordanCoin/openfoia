"""OpenSanctions records adapter: API matching, offline bulk file, search plan, fetch, download.

All HTTP goes through a mocked transport behind the adapter's egress_client,
and conftest blocks real sockets: nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from openfoia import search as search_mod
from openfoia.cli import app
from openfoia.net import EgressMode, EgressPolicy, egress_client
from openfoia.records import get_adapter, list_sources
from openfoia.records import opensanctions as osx
from openfoia.records.base import RecordEntity, SearchResult
from openfoia.records.opensanctions import OpenSanctionsAdapter

runner = CliRunner()
FIXTURE_CSV = Path(__file__).parent / "fixtures" / "opensanctions" / "targets.simple.csv"
KEY = "os-secret-key-0123456789"
TOR = EgressPolicy(mode=EgressMode.TOR)
HOSTILE = "Evil \x1b]52;c;cHduZWQ=\x07\x1b[2J‮TXT\x9b31m [red]x[/red]"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(osx.KEY_ENV, raising=False)
    for var in ("OPENFOIA_TOR", "OPENFOIA_TOR_HOST", "OPENFOIA_TOR_PORT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def local_file(tmp_path):
    """The fixture CSV installed where `records opensanctions-data --download` puts it."""
    folder = tmp_path / "data" / "opensanctions"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURE_CSV, folder / osx.DATA_FILE)
    return folder / osx.DATA_FILE


@pytest.fixture
def no_http(monkeypatch):
    """Any attempt to build an HTTP client fails the test."""

    def refuse(*a, **k):
        pytest.fail("an offline OpenSanctions search tried to build an HTTP client")

    monkeypatch.setattr(osx, "egress_client", refuse)
    monkeypatch.setattr("openfoia.net.egress_client", refuse)


def serve(monkeypatch, responder):
    """Route the adapter's egress_client to *responder*; returns (requests, policies)."""
    sent: list[httpx.Request] = []
    policies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return responder(request)

    def fake_egress_client(policy=None, **kw):
        policies.append(policy)
        kw.pop("transport", None)
        return egress_client(None, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(osx, "egress_client", fake_egress_client)
    return sent, policies


def match_body(*results):
    return {
        "responses": {
            "q": {"results": list(results), "total": {"value": len(results)}, "query": {}}
        },
        "limit": 10,
    }


def scored(ident, caption, score, *, topics=(), datasets=(), countries=(), **props):
    properties = {"topics": list(topics), "country": list(countries), **props}
    return {
        "id": ident,
        "caption": caption,
        "schema": "Person",
        "properties": properties,
        "datasets": list(datasets),
        "referents": [],
        "target": True,
        "score": score,
        "match": score >= 0.7,
        "explanations": {},
    }


def ok(data):
    return lambda request: httpx.Response(200, json=data)


# ---------------------------------------------------------------------------
# Registry, modes, key handling
# ---------------------------------------------------------------------------


def test_registered_and_mode_follows_the_local_file(local_file):
    assert "opensanctions" in list_sources()
    adapter = get_adapter("opensanctions")
    assert adapter.offline is True and adapter.mode == "local"
    local_file.unlink()
    adapter = get_adapter("opensanctions")
    assert adapter.offline is False and adapter.mode == "api"


def test_key_from_env_only_and_withheld_over_tor(monkeypatch):
    assert OpenSanctionsAdapter().has_key is False
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    assert OpenSanctionsAdapter().has_key is True

    over_tor = OpenSanctionsAdapter(egress=TOR)
    assert over_tor.has_key is False and over_tor.credentials_withheld is True
    assert "Tor" in over_tor.unavailable_reason()


def test_tor_with_a_key_sends_nothing(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    sent, _ = serve(monkeypatch, ok(match_body()))
    result = asyncio.run(OpenSanctionsAdapter(egress=TOR).search("Ana Fixture"))
    assert sent == []
    assert result.error and "not used over Tor" in result.error
    assert KEY not in result.error


def test_no_key_and_no_file_sends_nothing_and_says_how_to_fix(monkeypatch):
    sent, _ = serve(monkeypatch, ok(match_body()))
    adapter = OpenSanctionsAdapter()
    assert osx.KEY_ENV in adapter.unavailable_reason()
    result = asyncio.run(adapter.search("Ana Fixture"))
    assert sent == []
    assert "opensanctions-data --download" in result.error and osx.KEY_ENV in result.error


# ---------------------------------------------------------------------------
# API search
# ---------------------------------------------------------------------------


def test_api_search_parses_match_results(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    data = match_body(
        scored(
            "NK-low",
            "Ivar Testerova",
            0.61,
            topics=["role.pep"],
            datasets=["wd_peps"],
            countries=["ru"],
        ),
        scored(
            "NK-top",
            "Ivar Testerov",
            0.95,
            topics=["sanction", "crime"],
            datasets=["us_ofac_sdn", "eu_fsf"],
            countries=["ru", "cy"],
            birthDate=["1961-03-04"],
            alias=["Иван Тестеров"],
        ),
    )
    sent, _ = serve(monkeypatch, ok(data))

    result = asyncio.run(OpenSanctionsAdapter().search("Ivar Testerov", schema="Person"))

    assert result.error is None and result.total_results == 2
    (req,) = sent
    assert req.method == "POST" and req.url.path == "/match/default"
    assert dict(req.url.params) == {"limit": "10", "cutoff": "0.5"}
    assert req.headers["authorization"] == f"ApiKey {KEY}"
    assert "Ivar" not in str(req.url)  # the name is in the body: OpenSanctions logs GET URLs
    assert json.loads(req.content) == {
        "queries": {"q": {"schema": "Person", "properties": {"name": ["Ivar Testerov"]}}}
    }

    top, low = result.entities  # best score first
    assert top.name == "Ivar Testerov" and top.entity_type == "PERSON"
    assert top.identifiers == {"opensanctions_id": "NK-top"}
    assert top.source_url == "https://www.opensanctions.org/entities/NK-top/"
    x = top.extra_data
    assert x["datasets"] == ["us_ofac_sdn", "eu_fsf"]
    assert x["topics"] == ["sanction", "crime"] and x["topics_inferred"] is False
    assert x["countries"] == ["ru", "cy"]
    assert x["match_score"] == 0.95 and x["match"] is True and x["match_type"] == "exact"
    assert x["is_sanctioned"] is True and x["is_pep"] is False
    assert x["ftm"]["properties"]["birthDate"] == ["1961-03-04"]
    assert low.extra_data["is_pep"] is True and low.extra_data["match_type"] == "fuzzy"
    assert OpenSanctionsAdapter().match_confidence(top) == 0.95


def test_api_error_is_not_empty_results(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    serve(monkeypatch, lambda r: httpx.Response(500, json={"detail": "index down"}))
    failed = asyncio.run(OpenSanctionsAdapter().search("Ana Fixture"))
    assert failed.error and "HTTP 500" in failed.error and "index down" in failed.error

    serve(monkeypatch, ok(match_body()))
    empty = asyncio.run(OpenSanctionsAdapter().search("Ana Fixture"))
    assert empty.error is None and empty.total_results == 0 and empty.entities == []

    serve(monkeypatch, ok({"unexpected": True}))
    odd = asyncio.run(OpenSanctionsAdapter().search("Ana Fixture"))
    assert odd.error and "no result list" in odd.error


def test_429_is_reported_once_with_retry_after(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    sent, _ = serve(
        monkeypatch,
        lambda r: httpx.Response(429, headers={"Retry-After": "120"}, json={"detail": "quota"}),
    )
    result = asyncio.run(OpenSanctionsAdapter().search("Ana Fixture"))
    assert len(sent) == 1  # POST is not retried; the quota will not reset in seconds
    assert "HTTP 429" in result.error and "120s" in result.error


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_names_the_env_var_and_is_redacted(monkeypatch, status):
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    serve(monkeypatch, lambda r: httpx.Response(status, json={"detail": f"bad key {KEY}"}))
    result = asyncio.run(OpenSanctionsAdapter().search("Ana Fixture"))
    assert f"HTTP {status}" in result.error and osx.KEY_ENV in result.error
    assert KEY not in result.error


def test_transport_error_is_redacted(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)

    def boom(request):
        raise httpx.ConnectError(f"failed with {KEY}")

    serve(monkeypatch, boom)
    result = asyncio.run(OpenSanctionsAdapter().search("Ana Fixture"))
    assert "ConnectError" in result.error and KEY not in result.error


def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    monkeypatch.setattr(osx, "MAX_RESPONSE_BYTES", 1000)
    serve(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 5000))
    result = asyncio.run(OpenSanctionsAdapter().search("Ana Fixture"))
    assert "refusing to read it" in result.error


def test_hostile_api_strings_are_cleaned_and_bad_ids_dropped(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    data = match_body(
        scored("../../etc/passwd", "Traversal", 0.9),
        scored("NK-ok", HOSTILE, 0.8, topics=[HOSTILE], datasets=[HOSTILE], countries=[HOSTILE]),
        {"id": "NK-weird", "caption": "Weird", "schema": "Person", "properties": [], "score": 7},
    )
    data["responses"]["q"]["results"].append("not a dict")
    serve(monkeypatch, ok(data))

    result = asyncio.run(OpenSanctionsAdapter().search("x"))

    ids = [e.identifiers["opensanctions_id"] for e in result.entities]
    assert ids == ["NK-ok", "NK-weird"]
    blob = json.dumps([e.to_dict() for e in result.entities])
    for bad in ("\x1b", "\x07", "‮", "\x9b"):
        assert bad not in blob
    weird = result.entities[1]
    assert weird.extra_data["match_score"] is None  # 7 is not a score
    assert OpenSanctionsAdapter().match_confidence(weird) is None


# ---------------------------------------------------------------------------
# Offline: the bulk file, with no network at all
# ---------------------------------------------------------------------------


def test_local_search_needs_no_network(local_file, no_http):
    adapter = get_adapter("opensanctions", egress=TOR)  # Tor configured, Tor not running
    assert adapter.offline and adapter.unavailable_reason() is None
    assert adapter.search_notice is None

    result = asyncio.run(adapter.search("ivar q. TESTEROV"))

    assert result.error is None and result.raw_response["mode"] == "local"
    (hit,) = result.entities
    x = hit.extra_data
    assert hit.identifiers == {"opensanctions_id": "NK-fx001"}
    assert x["match_score"] == 1.0 and x["match_type"] == "exact" and x["mode"] == "local"
    assert x["datasets"] == [
        "EU Financial Sanctions Files (FSF)",
        "US OFAC Specially Designated Nationals (SDN) List",
    ]
    assert x["topics"] == ["sanction"] and x["topics_inferred"] is True
    assert x["countries"] == ["ru"] and x["program_ids"] == ["EU-UKR"]

    alias = asyncio.run(adapter.search("Marta Ruiz")).entities[0]
    assert alias.extra_data["match_score"] == 0.8 and alias.extra_data["topics"] == ["role.pep"]
    assert asyncio.run(adapter.search("Testerov")).entities == []  # whole names only


def test_local_missing_file_is_an_error_not_a_clean_result(tmp_path, no_http):
    adapter = OpenSanctionsAdapter(mode="local", data_dir=tmp_path)
    assert "opensanctions-data --download" in adapter.unavailable_reason()
    result = asyncio.run(adapter.search("Ana Fixture"))
    assert result.error and "No targets.simple.csv" in result.error


def test_local_hostile_rows_are_cleaned(tmp_path, no_http):
    header = FIXTURE_CSV.read_text().splitlines()[0]
    row = [f'"{v}"' for v in ("NK-evil", "Person", HOSTILE, "", "", "", "", "", "", "", "", "")]
    row += ['"Evil List \x1b[31m"', '""', '""', '""']
    (tmp_path / osx.DATA_FILE).write_text(header + "\n" + ",".join(row) + "\n")
    adapter = OpenSanctionsAdapter(mode="local", data_dir=tmp_path)

    (hit,) = asyncio.run(adapter.search(HOSTILE)).entities

    blob = json.dumps(hit.to_dict()) + osx.entity_text(hit)
    for bad in ("\x1b", "\x07", "‮", "\x9b"):
        assert bad not in blob


# ---------------------------------------------------------------------------
# `openfoia search`: local mode is not "contacted"; confidence only here
# ---------------------------------------------------------------------------


def test_plan_lists_local_source_as_searched_not_contacted(local_file, no_http):
    plan = search_mod.plan_search("Ivar Testerov", ["opensanctions"], policy=EgressPolicy())
    assert plan.contacted == [] and list(plan.local) == ["opensanctions"]
    lines = plan.warning_lines()
    assert lines[0] == "Nothing will be sent over the network."
    assert not any("WARNING" in line for line in lines)
    assert any("on this machine only" in line and "opensanctions" in line for line in lines)
    preview = plan.preview()
    assert preview["would_contact"] == [] and preview["searched_locally"] == ["opensanctions"]

    # No consent needed: nothing is sent.
    report = asyncio.run(search_mod.run_search(plan, allow_network=False, log_to_case=False))
    (row,) = report.rows
    assert row["source"] == "opensanctions" and row["egress"] == "local"
    assert row["confidence"] == 0.8 and row["id"] == "NK-fx001"  # an alias match
    assert report.statuses[0].status == "ok"


def test_local_miss_says_exact_matching_is_not_proof(local_file, no_http):
    """A sanctions-screening miss offline must not read as a clean result."""
    r = runner.invoke(app, ["records", "search", "Nobody Here Atall", "-s", "opensanctions"])
    out = " ".join(r.output.split())
    assert "No results found" in out
    assert "no result is not proof the name is not listed" in out

    plan = search_mod.plan_search("Nobody Here Atall", ["opensanctions"], policy=EgressPolicy())
    report = asyncio.run(search_mod.run_search(plan, allow_network=False, log_to_case=False))
    (status,) = report.statuses
    assert status.status == "no_results" and "not proof" in (status.reason or "")


def test_api_mode_is_contacted_with_its_notice(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    plan = search_mod.plan_search("Acme", ["opensanctions"], policy=EgressPolicy())
    assert plan.contacted == ["opensanctions"] and plan.local == {}
    text = "\n".join(plan.warning_lines())
    assert "WARNING" in text and "POST body" in text and "credit" in text
    assert KEY not in text


def test_missing_key_and_data_is_skipped_with_reason(monkeypatch):
    plan = search_mod.plan_search("Acme", ["opensanctions"], policy=EgressPolicy())
    assert plan.adapters == {} and plan.local == {}
    assert osx.KEY_ENV in plan.skipped["opensanctions"]

    monkeypatch.setenv(osx.KEY_ENV, KEY)
    plan = search_mod.plan_search("Acme", ["opensanctions"], policy=TOR)
    assert "not sent over Tor" in plan.skipped["opensanctions"]
    assert "opensanctions" in plan.withheld_skipped


class _Other:
    """Another source whose entities even carry a 'match_score': it must not become confidence."""

    credentials_withheld = False

    def __init__(self, egress=None, **kw):
        pass

    async def search(self, query, **kwargs):
        entity = RecordEntity(
            entity_type="DOCUMENT",
            name="Other doc",
            source="other",
            identifiers={"id": "o-1"},
            extra_data={"match_score": 0.99, "score": 3},
        )
        return SearchResult("other", query, 1, [entity])


def test_confidence_only_on_opensanctions_rows(monkeypatch, local_file, no_http):
    from openfoia import records

    def fake_get_adapter(name, *, egress=None, api_data_gov_key=None):
        return _Other() if name == "other" else OpenSanctionsAdapter(egress=egress)

    monkeypatch.setattr(records, "list_sources", lambda: ["opensanctions", "other"])
    monkeypatch.setattr(records, "get_adapter", fake_get_adapter)
    plan = search_mod.plan_search("Fictional Shipping Ltd", None, policy=EgressPolicy())
    report = asyncio.run(search_mod.run_search(plan, allow_network=True, log_to_case=False))

    rows = {r["source"]: r for r in report.rows}
    assert rows["opensanctions"]["confidence"] == 1.0
    assert "confidence" not in rows["other"]
    assert rows["other"]["egress"] == "direct"


def test_cli_search_local_sends_nothing_and_asks_nothing(local_file, no_http):
    result = runner.invoke(app, ["search", "Olu Sample", "-s", "opensanctions", "--json"])
    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output and "Send this query" not in result.output
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    (row,) = payload["results"]
    assert row["confidence"] == 1.0 and row["egress"] == "local"
    assert row["type"] == "PERSON"


def test_agent_tool_local_needs_no_network_permission(local_file, no_http, monkeypatch):
    from openfoia.agent import search_records

    out = asyncio.run(
        search_records({"query": "Olu Sample", "sources": ["opensanctions"]}, allow_network=False)
    )
    assert "error" not in out, out
    assert out["results"][0]["confidence"] == 1.0


# ---------------------------------------------------------------------------
# records search / fetch / store
# ---------------------------------------------------------------------------


def test_records_search_local_table(local_file, no_http):
    r = runner.invoke(app, ["records", "search", "Fictional Shipping Ltd", "-s", "opensanctions"])
    assert r.exit_code == 0, r.output
    assert "nothing is sent" in r.output and "NK-fx003" in r.output
    assert "1.00" in r.output and "topics inferred" in " ".join(r.output.split())


def test_records_search_without_key_or_file_exits_before_sending(monkeypatch):
    sent, _ = serve(monkeypatch, ok(match_body()))
    r = runner.invoke(app, ["records", "search", "Ana Fixture", "-s", "opensanctions"])
    assert r.exit_code == 1 and sent == []
    assert "opensanctions-data --download" in " ".join(r.output.split())


def nested_entity():
    return {
        "id": "NK-top",
        "caption": "Ivar Testerov",
        "schema": "Person",
        "datasets": ["us_ofac_sdn"],
        "properties": {
            "name": ["Ivar Testerov"],
            "topics": ["sanction"],
            "nationality": ["ru"],
            "sanctions": [
                {
                    "id": "ofac-san-1",
                    "schema": "Sanction",
                    "caption": "Sanction",
                    "properties": {
                        "authority": ["Office of Foreign Assets Control"],
                        "program": ["UKRAINE-EO13661"],
                        "startDate": ["2014-03-20"],
                    },
                }
            ],
            "familyPerson": [{"id": "NK-kin", "schema": "Family", "caption": "Kin"}],
        },
    }


def test_fetch_api_follows_one_same_host_redirect_and_parses_nested(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)

    def responder(request):
        if request.url.path == "/entities/NK-old":
            return httpx.Response(308, headers={"Location": "/entities/NK-top"})
        if request.url.path == "/entities/NK-top":
            return httpx.Response(200, json=nested_entity())
        return httpx.Response(404, json={"detail": "nope"})

    sent, _ = serve(monkeypatch, responder)
    entity = asyncio.run(OpenSanctionsAdapter().fetch("NK-old"))

    assert [r.url.path for r in sent] == ["/entities/NK-old", "/entities/NK-top"]
    assert all(r.headers["authorization"] == f"ApiKey {KEY}" for r in sent)
    assert sent[0].url.params["nested"] == "true"
    x = entity.extra_data
    assert x["countries"] == ["ru"] and x["is_sanctioned"] is True
    assert x["sanctions_details"] == [
        "Office of Foreign Assets Control · UKRAINE-EO13661 · 2014-03-20"
    ]
    assert x["ftm"]["properties"]["sanctions"] == ["ofac-san-1"]  # FtM reference by id
    assert {r["id"] for r in x["related"]} == {"ofac-san-1", "NK-kin"}
    assert asyncio.run(OpenSanctionsAdapter().fetch("NK-missing")) is None


def test_fetch_refuses_a_redirect_off_the_api_host(monkeypatch):
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    sent, _ = serve(
        monkeypatch,
        lambda r: httpx.Response(308, headers={"Location": "https://evil.example/entities/NK-x"}),
    )
    with pytest.raises(osx.OpenSanctionsError, match="Refusing to follow"):
        asyncio.run(OpenSanctionsAdapter().fetch("NK-old"))
    assert len(sent) == 1  # the key never went to evil.example


@pytest.mark.parametrize("bad", ["../etc", "NK x", "a/b", "", "NK-1?x=1", "-lead"])
def test_fetch_rejects_ids_that_are_not_opensanctions_ids(bad, monkeypatch):
    sent, _ = serve(monkeypatch, ok({}))
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    with pytest.raises(ValueError):
        asyncio.run(OpenSanctionsAdapter().fetch(bad))
    assert sent == []


def test_cli_fetch_local_stores_document_and_ftm_entity_in_the_case(local_file, no_http):
    assert runner.invoke(app, ["case", "new", "Shipping"]).exit_code == 0
    r = runner.invoke(app, ["records", "fetch", "NK-fx003", "-s", "opensanctions"])
    assert r.exit_code == 0, r.output
    assert "nothing is sent" in r.output and "saved" in r.output
    assert "Attached to the active case" in r.output

    again = runner.invoke(app, ["records", "fetch", "NK-fx003", "-s", "opensanctions"])
    assert "already stored" in again.output

    from openfoia.db import get_session
    from openfoia.models import Document, Entity, Request

    with get_session() as session:
        req = session.query(Request).filter(Request.request_number == "OS-NK-fx003").one()
        assert req.case_id is not None
        doc = session.query(Document).filter(Document.request_id == req.id).one()
        assert "Fictional Shipping Ltd" in doc.extracted_text
        assert '"schema": "Company"' in doc.extracted_text  # the FtM entity, as JSON
        assert "CC BY-NC 4.0" in doc.extracted_text
        (ent,) = session.query(Entity).filter(Entity.document_id == doc.id).all()
        assert ent.entity_type.value == "organization"
        assert ent.extra_data["ftm_id"] == "NK-fx003"
        assert ent.extra_data["ftm_schema"] == "Company"
        assert ent.extra_data["topics"] == ["sanction"]


def test_cli_fetch_rejects_bad_id_before_anything(local_file, no_http):
    r = runner.invoke(app, ["records", "fetch", "../x", "-s", "opensanctions"])
    assert r.exit_code == 2 and "Not an OpenSanctions id" in r.output


# ---------------------------------------------------------------------------
# Crossref goes through the adapter
# ---------------------------------------------------------------------------


def test_crossref_uses_the_adapter_for_both_sources(monkeypatch, local_file):
    from types import SimpleNamespace

    from openfoia.crossref import crossref_entities
    from openfoia.models import EntityType

    built: list[str] = []
    real = osx.OpenSanctionsAdapter

    def spy(*args, **kwargs):
        built.append(kwargs.get("mode"))
        return real(*args, **kwargs)

    monkeypatch.setattr(osx, "OpenSanctionsAdapter", spy)
    monkeypatch.setenv(osx.KEY_ENV, KEY)
    hit = scored("NK-api", "Fictional Shipping Ltd", 0.97, topics=["sanction"])
    hit["schema"] = "Company"
    sent, _ = serve(monkeypatch, ok(match_body(hit)))
    entity = SimpleNamespace(
        entity_type=EntityType.ORGANIZATION,
        normalized_text="Fictional Shipping Ltd",
        confidence=0.9,
    )

    report = asyncio.run(
        crossref_entities(
            [entity],
            sources=["opensanctions", "opensanctions-local"],
            opensanctions_data_dir=str(local_file.parent),
            allow_network=True,
        )
    )

    assert sorted(built) == ["api", "local"]
    assert json.loads(sent[0].content)["queries"]["q"]["schema"] == "Organization"
    by_source = {h.source: h for h in report.results[0].hits}
    assert by_source["opensanctions"].extra["score"] == 0.97
    assert by_source["opensanctions-local"].extra["score"] == 1.0
    assert report.source_errors == {}


class _Query:
    def __init__(self, items):
        self._items = items

    def filter(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def all(self):
        return self._items


def test_cli_crossref_does_not_list_opensanctions_without_a_usable_key(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from openfoia.models import EntityType

    entity = SimpleNamespace(
        entity_type=EntityType.PERSON,
        raw_text="Olu Sample",
        normalized_text="Olu Sample",
        confidence=0.95,
        context="",
    )

    @contextmanager
    def fake_session(*a, **k):
        yield SimpleNamespace(query=lambda _model: _Query([entity]))

    db_file = tmp_path / "data.db"
    db_file.write_text("")
    monkeypatch.setattr("openfoia.db.get_db_path", lambda *a, **k: db_file)
    monkeypatch.setattr("openfoia.db.get_session", fake_session)
    sent, _ = serve(monkeypatch, ok(match_body()))

    r = runner.invoke(app, ["crossref", "--sources", "opensanctions"])

    assert sent == []
    text = " ".join(r.output.split())
    assert "opensanctions: not contacted" in text
    assert "Send these names" not in text  # nothing is left to warn about
    assert f"API key required: set {osx.KEY_ENV}" in text  # reported, never "clean"


# ---------------------------------------------------------------------------
# Explicit bulk download
# ---------------------------------------------------------------------------

CSV_BYTES = FIXTURE_CSV.read_bytes()
VERSIONED = "https://data.opensanctions.org/artifacts/default/20261119-x/index.json"
FILE_URL = "https://data.opensanctions.org/artifacts/default/20261119-x/targets.simple.csv"


def index_doc(**over):
    entry = {
        "name": "targets.simple.csv",
        "url": FILE_URL,
        "size": len(CSV_BYTES),
        "checksum": hashlib.sha1(CSV_BYTES).hexdigest(),
        **over,
    }
    return {"version": "20261119-x", "resources": [{"name": "names.txt"}, entry]}


def download_routes(index, body=CSV_BYTES):
    def responder(request):
        url = str(request.url)
        if url == osx.INDEX_URL:
            return httpx.Response(307, headers={"Location": VERSIONED})
        if url == VERSIONED:
            return httpx.Response(200, json=index)
        if url == FILE_URL:
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    return responder


def test_status_without_download_sends_nothing(no_http):
    r = runner.invoke(app, ["records", "opensanctions-data"])
    assert r.exit_code == 0 and "No OpenSanctions bulk file" in r.output


def test_download_asks_first_and_aborting_sends_nothing(monkeypatch):
    sent, _ = serve(monkeypatch, download_routes(index_doc()))
    r = runner.invoke(app, ["records", "opensanctions-data", "--download"], input="n\n")
    assert r.exit_code == 0 and sent == []
    text = " ".join(r.output.split())
    assert "WARNING" in text and "data.opensanctions.org" in text and "480 MB" in text
    assert "Nothing left your machine" in text


def test_download_verifies_size_and_checksum_then_searches_offline(monkeypatch, tmp_path):
    sent, _ = serve(monkeypatch, download_routes(index_doc()))
    r = runner.invoke(app, ["records", "opensanctions-data", "--download", "--yes"])
    assert r.exit_code == 0, r.output
    assert [str(q.url) for q in sent] == [osx.INDEX_URL, VERSIONED, FILE_URL]
    assert all("authorization" not in q.headers for q in sent)  # no key on bulk downloads
    assert f"{len(CSV_BYTES):,} bytes" in r.output and "verified" in r.output

    status = osx.local_status()
    assert status["present"] and status["meta"]["version"] == "20261119-x"
    assert get_adapter("opensanctions").offline is True

    r = runner.invoke(app, ["records", "opensanctions-data", "--download", "--yes"])
    assert "Already up to date" in r.output and len(sent) == 5  # index only, no file


def test_checksum_mismatch_keeps_the_old_file(monkeypatch, local_file):
    old = local_file.read_bytes()
    serve(monkeypatch, download_routes(index_doc(checksum="0" * 40)))
    r = runner.invoke(app, ["records", "opensanctions-data", "--download", "--yes"])
    assert r.exit_code == 1 and "Checksum mismatch" in r.output
    assert local_file.read_bytes() == old
    assert not list(local_file.parent.glob("*.download*"))


def test_download_larger_than_the_index_says_is_cut_off(monkeypatch, local_file):
    old = local_file.read_bytes()
    serve(monkeypatch, download_routes(index_doc(), body=CSV_BYTES + b"extra"))
    r = runner.invoke(app, ["records", "opensanctions-data", "--download", "--yes"])
    assert r.exit_code == 1 and "cap exceeded" in " ".join(r.output.split())
    assert local_file.read_bytes() == old


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/targets.simple.csv",
        "http://data.opensanctions.org/x.csv",
        "https://data.opensanctions.org@evil.example/x.csv",
    ],
)
def test_index_pointing_off_the_data_host_is_refused(monkeypatch, url):
    sent, _ = serve(monkeypatch, download_routes(index_doc(url=url)))
    with pytest.raises(osx.OpenSanctionsError):
        asyncio.run(osx.read_index())
    assert all(q.url.host == osx.DATA_HOST for q in sent)


@pytest.mark.parametrize("over", [{"size": -1}, {"size": "big"}, {"checksum": "abc"}])
def test_index_with_implausible_size_or_checksum_is_refused(monkeypatch, over):
    serve(monkeypatch, download_routes(index_doc(**over)))
    with pytest.raises(osx.OpenSanctionsError):
        asyncio.run(osx.read_index())


def test_download_goes_over_the_configured_egress(monkeypatch):
    _, policies = serve(monkeypatch, download_routes(index_doc()))
    asyncio.run(osx.read_index(TOR))
    assert policies and all(p is TOR for p in policies)
