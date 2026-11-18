"""`openfoia search`: one query, every registered source, provenance on every row.

All offline: adapters are fakes swapped into the records registry, and the
real adapters used in the credential tests are only constructed, never asked
to search (conftest blocks sockets either way).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from openfoia import net
from openfoia import search as search_mod
from openfoia.agent import OpenFOIAAgent
from openfoia.cli import app
from openfoia.net import EgressMode, EgressPolicy, TorUnavailableError
from openfoia.records.base import AdapterRequestError, RecordEntity, SearchResult

runner = CliRunner()
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HOSTILE = 'Evil \x1b[2J\x1b]8;;http://x\x07\x9b31m‮TXT\x00 "q" \\ [red]x[/red]\n\ud800 end'


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    for var in (
        "OPENFOIA_TOR",
        "OPENFOIA_TOR_HOST",
        "OPENFOIA_TOR_PORT",
        "OPENFOIA_NARA_API_KEY",
        "CATALOG_API_KEY",
        "OPENFOIA_MUCKROCK_TOKEN",
        "OPENFOIA_API_DATA_GOV_KEY",
        "OPENFOIA_GOVINFO_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _entity(source, i, *, title=None, date=None, snippet="a snippet"):
    return RecordEntity(
        entity_type="DOCUMENT",
        name=title if title is not None else f"{source} doc {i}",
        source=source,
        source_url=f"https://{source}.example/doc/{i}",
        identifiers={"id": f"{source}-{i}"},
        extra_data={"description": snippet, **({"date": date} if date else {})},
    )


class Fake:
    """A records adapter stand-in; records every search it is asked to run."""

    calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, name, egress=None, *, behaviour="ok", entities=None, total=None):
        self.name = name
        self.egress = egress
        self.behaviour = behaviour
        self.entities = entities if entities is not None else [_entity(name, i) for i in range(2)]
        self.total = total if total is not None else len(self.entities)
        self.credentials_withheld = False

    async def search(self, query, **kwargs):
        Fake.calls.append((self.name, query))
        if self.behaviour == "raise":
            raise AdapterRequestError("ConnectError: boom")
        if self.behaviour == "api_error":
            return SearchResult(self.name, query, 0, [], error="HTTP 503 from upstream")
        if self.behaviour == "hang":
            await asyncio.sleep(60)
        if self.behaviour == "empty":
            return SearchResult(self.name, query, 0, [])
        return SearchResult(self.name, query, self.total, list(self.entities))


@pytest.fixture
def registry(monkeypatch):
    """Install fakes: registry(name=behaviour or Fake kwargs) -> dict of built adapters."""
    Fake.calls = []
    built: dict[str, Fake] = {}

    def install(**spec):
        def get_adapter(name, *, egress=None, api_data_gov_key=None):
            kw = spec[name] if isinstance(spec[name], dict) else {"behaviour": spec[name]}
            built[name] = Fake(name, egress, **kw)
            return built[name]

        monkeypatch.setattr("openfoia.records.list_sources", lambda: sorted(spec))
        monkeypatch.setattr("openfoia.records.get_adapter", get_adapter)
        return built

    return install


def _tor_up(monkeypatch, *, reachable=True):
    """Pretend socksio is installed and the SOCKS port answers (or not)."""

    class Client:
        async def aclose(self):
            return None

    async def check_tor(policy, timeout=5.0):
        return reachable

    monkeypatch.setattr(net, "egress_client", lambda *a, **k: Client())
    monkeypatch.setattr(net, "check_tor", check_tor)


# --- JSON shape and provenance ------------------------------------------------


def test_json_rows_carry_provenance(registry):
    registry(documentcloud="ok", sec="ok")
    result = runner.invoke(app, ["search", "Acme Corp", "--json", "--yes"])

    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["query"] == "Acme Corp"
    assert data["egress"] == "direct"
    assert [s["source"] for s in data["sources"]] == ["documentcloud", "sec"]
    assert {s["status"] for s in data["sources"]} == {"ok"}
    assert len(data["results"]) == 4
    for row in data["results"]:
        assert {"source", "title", "url", "snippet", "retrieved_at", "egress"} <= set(row)
        assert ISO_UTC.match(row["retrieved_at"]), row["retrieved_at"]
        assert row["egress"] == "direct"
        assert row["url"].startswith(f"https://{row['source']}.example/")
        assert "confidence" not in row  # no source gives a real one; none is invented
    assert data["results"][0]["id"] == "documentcloud-0"


def test_limit_is_per_source(registry):
    registry(documentcloud={"entities": [_entity("documentcloud", i) for i in range(30)]})
    result = runner.invoke(app, ["search", "x", "--json", "--yes", "--limit", "3"])
    data = json.loads(result.stdout)
    assert len(data["results"]) == 3
    assert data["sources"][0]["total"] == 30
    assert data["sources"][0]["returned"] == 3


# --- warning and consent ---------------------------------------------------------


def test_json_mode_warning_goes_to_stderr_and_stdout_is_clean_json(registry):
    registry(documentcloud="ok", sec="ok")
    result = runner.invoke(app, ["search", "Acme Corp", "--json", "--yes"])

    assert "WARNING: this sends the query 'Acme Corp' to 2" in result.stderr
    assert "documentcloud, sec" in result.stderr
    assert "WILL see your real IP" in result.stderr
    assert "WARNING" not in result.stdout
    json.loads(result.stdout)  # nothing but the JSON document


def test_prompt_is_on_stderr_and_yes_answer_runs_the_search(registry):
    registry(documentcloud="ok")
    result = runner.invoke(app, ["search", "Acme", "--json"], input="y\n")

    assert result.exit_code == 0, result.stderr
    assert "Send this query to the sources listed above?" in result.stderr
    assert Fake.calls == [("documentcloud", "Acme")]
    # CliRunner echoes the typed answer onto stdout; a real terminal does not.
    assert result.stdout.startswith(" y\n")
    json.loads(result.stdout[len(" y\n") :])


def test_yes_flag_skips_the_prompt(registry):
    registry(documentcloud="ok")
    result = runner.invoke(app, ["search", "Acme", "--yes"], input="")

    assert result.exit_code == 0, result.stderr
    assert "Send this query" not in result.stderr + result.stdout
    assert Fake.calls == [("documentcloud", "Acme")]


def test_decline_aborts_before_any_request(registry, monkeypatch):
    registry(documentcloud="ok", sec="ok")
    logged = []
    monkeypatch.setattr("openfoia.cli._log_case_queries", lambda rows: logged.append(rows))

    result = runner.invoke(app, ["search", "Acme", "--json"], input="n\n")

    assert result.exit_code == 0
    assert "Aborted. Nothing left your machine." in result.stderr
    assert Fake.calls == []
    assert logged == []
    assert result.stdout == " n\n"  # only CliRunner's echo of the typed answer


def test_unknown_source_and_bad_since_fail_before_any_request(registry):
    registry(documentcloud="ok")
    bad_source = runner.invoke(app, ["search", "x", "-s", "nope", "--yes"])
    bad_since = runner.invoke(app, ["search", "x", "--since", "2020/01/01", "--yes"])

    assert bad_source.exit_code == 1
    assert "Unknown source(s): nope" in bad_source.stderr
    assert bad_since.exit_code == 1
    assert "YYYY-MM-DD" in bad_since.stderr
    assert Fake.calls == []


def test_source_flag_repeats_and_comma_separates(registry):
    registry(a="ok", b="ok", c="ok")
    result = runner.invoke(app, ["search", "x", "-s", "a,c", "--source", "c", "--json", "-y"])
    assert [s["source"] for s in json.loads(result.stdout)["sources"]] == ["a", "c"]
    assert sorted(Fake.calls) == [("a", "x"), ("c", "x")]


# --- Tor: fail closed, never fall back ---------------------------------------------


def test_egress_tor_without_socksio_fails_closed(registry, monkeypatch):
    import httpx

    def no_socks(*args, **kwargs):
        raise ImportError("socksio not installed")

    registry(documentcloud="ok")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", no_socks)

    result = runner.invoke(app, ["search", "Acme", "--egress", "tor", "--yes"])

    assert result.exit_code == 1
    assert "install-extras tor" in result.stderr
    assert "WARNING" not in result.stderr  # failed before warning, prompt or request
    assert Fake.calls == []


def test_egress_tor_with_tor_down_fails_closed(registry, monkeypatch):
    registry(documentcloud="ok")
    _tor_up(monkeypatch, reachable=False)

    result = runner.invoke(app, ["search", "Acme", "--egress", "tor", "--yes", "--json"])

    assert result.exit_code == 1
    assert "not reachable" in result.stderr
    assert result.stdout == ""
    assert Fake.calls == []


def test_configured_tor_is_the_default_and_fails_closed(registry, monkeypatch):
    registry(documentcloud="ok")
    monkeypatch.setenv("OPENFOIA_TOR", "true")
    _tor_up(monkeypatch, reachable=False)

    result = runner.invoke(app, ["search", "Acme", "--yes"])

    assert result.exit_code == 1
    assert "not reachable" in result.stderr
    assert Fake.calls == []


def test_egress_tor_routes_every_adapter_and_tags_rows(registry, monkeypatch):
    built = registry(documentcloud="ok", sec="ok")
    _tor_up(monkeypatch)

    result = runner.invoke(app, ["search", "Acme", "--egress", "tor", "--yes", "--json"])

    assert result.exit_code == 0, result.stderr
    assert all(a.egress.is_tor for a in built.values())
    data = json.loads(result.stdout)
    assert data["egress"] == "tor"
    assert {r["egress"] for r in data["results"]} == {"tor"}
    assert "will NOT see your real IP" in result.stderr
    assert "Tor hides who is asking, not what is asked" in result.stderr


def test_egress_direct_overrides_configured_tor_for_this_run(registry, monkeypatch):
    built = registry(documentcloud="ok")
    monkeypatch.setenv("OPENFOIA_TOR", "true")

    result = runner.invoke(app, ["search", "Acme", "--egress", "direct", "--yes", "--json"])

    assert result.exit_code == 0, result.stderr
    assert not built["documentcloud"].egress.is_tor
    assert "WILL see your real IP" in result.stderr


def test_run_search_itself_fails_closed_for_every_caller(registry, monkeypatch):
    """The gate is in the shared function, not only in the CLI (agent parity)."""
    registry(documentcloud="ok")
    _tor_up(monkeypatch, reachable=False)
    plan = search_mod.plan_search("Acme", policy=EgressPolicy(mode=EgressMode.TOR))

    with pytest.raises(TorUnavailableError):
        asyncio.run(search_mod.run_search(plan, allow_network=True))
    assert Fake.calls == []


def test_run_search_refuses_without_consent(registry):
    registry(documentcloud="ok")
    plan = search_mod.plan_search("Acme", policy=EgressPolicy())
    with pytest.raises(PermissionError):
        asyncio.run(search_mod.run_search(plan))
    assert Fake.calls == []


# --- credentials withheld over Tor ---------------------------------------------------


def test_sources_whose_personal_key_is_withheld_over_tor_are_skipped(monkeypatch):
    """Real adapters: NARA and MuckRock cannot run without the user's key."""
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "personal-nara-key")
    monkeypatch.setenv("OPENFOIA_MUCKROCK_TOKEN", "personal-muckrock-token")

    plan = search_mod.plan_search(
        "Acme",
        ["nara", "muckrock", "fec", "documentcloud"],
        policy=EgressPolicy(mode=EgressMode.TOR),
        api_data_gov_key="personal-data-gov-key",
    )

    assert plan.skipped == {
        "nara": "personal API key not sent over Tor",
        "muckrock": "personal API token not sent over Tor",
    }
    assert plan.contacted == ["fec", "documentcloud"]
    assert plan.withheld == ["fec"]  # runs on DEMO_KEY instead
    warning = "\n".join(plan.warning_lines())
    assert "Personal credentials withheld over Tor (these run without them): fec" in warning
    assert "nara (personal API key not sent over Tor)" in warning


def test_same_sources_run_direct_with_the_key(monkeypatch):
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "personal-nara-key")
    monkeypatch.setenv("OPENFOIA_MUCKROCK_TOKEN", "personal-muckrock-token")
    plan = search_mod.plan_search("Acme", ["nara", "muckrock"], policy=EgressPolicy())
    assert plan.skipped == {}
    assert "nara: sends your API key" in "\n".join(plan.warning_lines())


def test_missing_keys_are_skipped_not_sent(monkeypatch):
    plan = search_mod.plan_search("Acme", ["nara", "muckrock"], policy=EgressPolicy())
    assert plan.contacted == []
    assert plan.skipped["nara"] == "no OPENFOIA_NARA_API_KEY"
    assert plan.skipped["muckrock"].startswith("no OPENFOIA_MUCKROCK_TOKEN")


def test_cli_reports_withheld_sources_as_skipped(monkeypatch):
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "personal-nara-key")
    monkeypatch.setenv("OPENFOIA_MUCKROCK_TOKEN", "personal-muckrock-token")
    _tor_up(monkeypatch)

    result = runner.invoke(
        app, ["search", "Acme", "-s", "nara,muckrock", "--egress", "tor", "--json"], input=""
    )

    assert result.exit_code == 1  # no source answered
    data = json.loads(result.stdout)
    assert {s["source"]: (s["status"], s["reason"]) for s in data["sources"]} == {
        "nara": ("skipped", "personal API key not sent over Tor"),
        "muckrock": ("skipped", "personal API token not sent over Tor"),
    }
    assert all(s["credentials_withheld"] for s in data["sources"])
    assert "Send this query" not in result.stderr  # nothing to send, nothing to confirm
    assert "Nothing will be sent" in result.stderr


# --- per-source failures ---------------------------------------------------------------


def test_one_source_failing_does_not_stop_the_others(registry, monkeypatch):
    monkeypatch.setattr(search_mod, "SOURCE_TIMEOUT", 0.05)
    registry(a="raise", b="api_error", c="empty", d="ok", e="hang")

    result = runner.invoke(app, ["search", "Acme", "--json", "--yes"])

    assert result.exit_code == 2  # partial failure
    data = json.loads(result.stdout)
    by = {s["source"]: s for s in data["sources"]}
    assert (by["a"]["status"], by["a"]["reason"]) == ("error", "ConnectError: boom")
    assert (by["b"]["status"], by["b"]["reason"]) == ("error", "HTTP 503 from upstream")
    assert (by["c"]["status"], by["c"]["total"]) == ("no_results", 0)
    assert (by["d"]["status"], by["d"]["returned"]) == ("ok", 2)
    assert by["e"]["status"] == "error" and "no answer within" in by["e"]["reason"]
    assert {r["source"] for r in data["results"]} == {"d"}
    assert "a               error: ConnectError: boom" in result.stderr
    assert "c               no results" in result.stderr


def test_every_source_failing_exits_1(registry):
    registry(a="raise", b="api_error")
    result = runner.invoke(app, ["search", "Acme", "--yes"])
    assert result.exit_code == 1
    assert "error: ConnectError: boom" in result.stdout


def test_fan_out_is_concurrent_but_bounded(registry, monkeypatch):
    running, peak = 0, 0

    async def slow_search(self, query, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return SearchResult(self.name, query, 0, [])

    monkeypatch.setattr(Fake, "search", slow_search)
    registry(**{f"s{i}": "ok" for i in range(10)})

    result = runner.invoke(app, ["search", "Acme", "--yes", "--json"])

    assert result.exit_code == 0, result.stderr
    assert 1 < peak <= search_mod.MAX_CONCURRENT_SOURCES


def test_since_drops_older_rows_and_keeps_undated(registry):
    registry(
        a={
            "entities": [
                _entity("a", 0, date="2019-12-31"),
                _entity("a", 1, date="2020-01-01T10:00:00"),
                _entity("a", 2),
            ]
        },
        b={"entities": [_entity("b", 0, date="2001-01-01")]},
    )
    result = runner.invoke(app, ["search", "x", "--since", "2020-01-01", "--json", "-y"])

    data = json.loads(result.stdout)
    assert [r["id"] for r in data["results"]] == ["a-1", "a-2"]
    by = {s["source"]: s for s in data["sources"]}
    assert by["a"]["older_than_since"] == 1
    assert by["b"]["status"] == "no_results"
    assert by["b"]["reason"] == "1 result(s) older than --since"


def test_newly_registered_adapter_is_searched_with_no_other_change(monkeypatch):
    from openfoia import records
    from openfoia.records.base import RecordAdapter

    class NewSource(RecordAdapter):
        source_name = "newsource"

        async def search(self, query, **kwargs):
            return SearchResult("newsource", query, 1, [_entity("newsource", 0)])

        async def fetch(self, identifier, **kwargs):
            return None

    monkeypatch.setattr(records, "_REGISTRY", {"newsource": NewSource})
    result = runner.invoke(app, ["search", "x", "--json", "-y"])
    data = json.loads(result.stdout)
    assert data["sources"][0]["status"] == "ok"
    assert data["results"][0]["source"] == "newsource"


# --- hostile upstream text ---------------------------------------------------------------


def test_hostile_strings_stay_valid_ascii_json(registry):
    registry(a={"entities": [_entity("a", 0, title=HOSTILE, snippet=HOSTILE)]})
    result = runner.invoke(app, ["search", "x", "--json", "--yes"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.isascii()  # no raw ESC, C1 or bidi bytes reach the terminal
    row = json.loads(result.stdout)["results"][0]
    assert row["title"] == HOSTILE  # escaped, not lost


def test_hostile_strings_are_stripped_in_terminal_output(registry):
    registry(
        a={"entities": [_entity("a", 0, title=HOSTILE, snippet=HOSTILE)]},
        b="ok",
    )

    result = runner.invoke(app, ["search", "x", "--yes"])

    out = result.stdout + result.stderr
    for bad in ("\x1b", "\x9b", "\x07", "\x00", "‮", "\ud800"):
        assert bad not in out, repr(bad)
    assert "Evil" in result.stdout and "[red]x[/red]" in result.stdout  # printed, not interpreted


def test_hostile_error_text_is_stripped(registry, monkeypatch):
    async def bad(self, query, **kwargs):
        raise AdapterRequestError(HOSTILE)

    monkeypatch.setattr(Fake, "search", bad)
    registry(a="ok")
    result = runner.invoke(app, ["search", "x", "--yes"])
    assert "\x1b" not in result.stdout and "‮" not in result.stdout
    assert "error: Evil" in result.stdout


# --- agent parity ----------------------------------------------------------------------


def _agent(**config):
    return OpenFOIAAgent(db_session=None, config=config)


def test_agent_tool_is_registered_with_the_live_source_list(registry):
    registry(documentcloud="ok", sec="ok")
    tool = next(t for t in _agent().get_tools() if t["name"] == "search_records")
    assert tool["parameters"]["properties"]["sources"]["items"]["enum"] == ["documentcloud", "sec"]


def test_agent_needs_operator_opt_in(registry):
    registry(documentcloud="ok")
    out = asyncio.run(_agent().execute_tool("search_records", {"query": "Acme", "confirmed": True}))
    assert "turned off" in out["error"]
    assert out["would_contact"] == ["documentcloud"]
    assert Fake.calls == []


def test_agent_gets_the_warning_before_anything_is_sent(registry):
    registry(documentcloud="ok", sec="ok")
    out = asyncio.run(_agent(allow_network=True).execute_tool("search_records", {"query": "Acme"}))

    assert out["status"] == "confirmation_required"
    plan = search_mod.plan_search("Acme", policy=EgressPolicy())
    assert out["warning"] == plan.warning_lines()  # the CLI's warning, word for word
    assert Fake.calls == []


def test_agent_and_cli_return_the_same_report(registry):
    registry(documentcloud="ok", sec="api_error")
    agent_out = asyncio.run(
        _agent(allow_network=True).execute_tool(
            "search_records", {"query": "Acme", "confirmed": True, "limit": 1}
        )
    )
    cli_out = json.loads(runner.invoke(app, ["search", "Acme", "--json", "-y", "-n", "1"]).stdout)

    def strip_times(d):
        d = json.loads(json.dumps(d))
        for item in d["sources"] + d["results"]:
            item.pop("retrieved_at")
        return d

    assert strip_times(agent_out) == strip_times(cli_out)


def test_agent_cannot_take_the_user_off_tor(registry, monkeypatch):
    registry(documentcloud="ok")
    monkeypatch.setenv("OPENFOIA_TOR", "true")
    out = asyncio.run(
        _agent(allow_network=True).execute_tool(
            "search_records", {"query": "Acme", "egress": "direct", "confirmed": True}
        )
    )
    assert "cannot switch a search to direct" in out["error"]
    assert Fake.calls == []


def test_agent_tor_unavailable_is_reported_and_nothing_sent(registry, monkeypatch):
    registry(documentcloud="ok")
    _tor_up(monkeypatch, reachable=False)
    out = asyncio.run(
        _agent(allow_network=True).execute_tool(
            "search_records", {"query": "Acme", "egress": "tor", "confirmed": True}
        )
    )
    assert "not reachable" in out["error"]
    assert Fake.calls == []


def test_clean_keeps_zero_width_joiners_but_drops_bidi_overrides():
    """A Persian surname keeps its ZWNJ; an RTL override that reorders text does not survive."""
    from openfoia.search import clean

    assert clean("بنیامین\u200cپور") == "بنیامین\u200cپور"
    assert clean("invoice\u202efdp.exe") == "invoicefdp.exe"
