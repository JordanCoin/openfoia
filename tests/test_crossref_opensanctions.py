"""OpenSanctions: offline screening from targets.simple.csv, and API key handling."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from openfoia import crossref as crossref_mod
from openfoia.cli import app
from openfoia.crossref import (
    OPENSANCTIONS_KEY_ENV,
    _check_opensanctions,
    _check_opensanctions_local,
    _SourceCheckError,
    crossref_entities,
)
from openfoia.models import EntityType
from openfoia.net import egress_client
from openfoia.records import opensanctions as os_mod

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "opensanctions"
FAKE_KEY = "os-test-key-0123456789abcdef"


def _local(name: str, data_dir: Path = FIXTURE_DIR):
    return asyncio.run(_check_opensanctions_local(name, EntityType.PERSON, str(data_dir)))


def test_local_exact_name_match_scores_one_and_flags_sanctions():
    (hit,) = _local("ivar q testerov")  # punctuation and case normalized away
    assert hit.source == "opensanctions-local"
    assert hit.match_type == "exact"
    assert hit.extra == {
        "score": 1.0,
        "is_pep": False,
        "is_sanctioned": True,
        "datasets": [
            "EU Financial Sanctions Files (FSF)",
            "US OFAC Specially Designated Nationals (SDN) List",
        ],
    }
    assert "Lists: EU Financial Sanctions Files (FSF); US OFAC" in hit.details
    assert hit.url == "https://www.opensanctions.org/entities/NK-fx001/"


def test_local_exclusion_with_sanctions_cell_is_not_sanctioned():
    (hit,) = _local("Dana Excluded")
    assert hit.extra["is_sanctioned"] is False
    assert "SANCTIONED" not in hit.details
    assert "US HHS Office of Inspector General Exclusions" in hit.details


def test_local_pep_needs_the_word_not_a_substring():
    assert _local("Olu Sample")[0].extra["is_pep"] is True  # "PEP position annotations"
    assert _local("Acme Offshore")[0].extra["is_pep"] is False  # "Peppercorn ..."


def test_local_alias_match_scores_point_eight_and_flags_pep():
    (hit,) = _local("Marta   Ruiz")
    assert hit.match_type == "alias"
    assert hit.extra["score"] == 0.8
    assert hit.extra["is_pep"] is True
    assert hit.extra["is_sanctioned"] is False


def test_local_non_latin_alias_and_no_substring_matches():
    assert _local("Иван Тестеров")[0].extra["score"] == 0.8
    assert _local("Testerov") == []  # whole-name match only, no partial hits


def test_local_missing_file_is_an_error_not_a_clean_result(tmp_path):
    with pytest.raises(_SourceCheckError) as exc:
        _local("Ana Fixture", tmp_path)
    assert exc.value.error_type == "NoOpenSanctionsData"


def test_local_read_error_surfaces_and_never_logs_the_name(tmp_path, monkeypatch, caplog):
    shutil.copy(FIXTURE_DIR / "targets.simple.csv", tmp_path / "targets.simple.csv")

    def _boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr("builtins.open", _boom)
    with caplog.at_level(logging.WARNING), pytest.raises(_SourceCheckError):
        _local("Ana Fixture", tmp_path)
    assert "Ana Fixture" not in caplog.text


def test_offline_source_runs_without_network_opt_in():
    """No allow_network: only offline sources may run, and this one does."""
    entities = [
        SimpleNamespace(
            entity_type=EntityType.ORGANIZATION,
            normalized_text="Fictional Shipping Limited",
            confidence=0.9,
        )
    ]
    report = asyncio.run(
        crossref_entities(
            entities,
            sources=["opensanctions-local"],
            opensanctions_data_dir=str(FIXTURE_DIR),
        )
    )
    assert report.sources_used == ["opensanctions-local"]
    assert report.results[0].hits[0].extra["is_sanctioned"] is True


class _FakeQuery:
    def __init__(self, items):
        self._items = items

    def filter(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def all(self):
        return self._items


@contextmanager
def _fake_session(items):
    yield SimpleNamespace(query=lambda _model: _FakeQuery(items))


def test_cli_offline_screening_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    db_file = tmp_path / "data.db"
    db_file.write_text("")
    entity = SimpleNamespace(
        entity_type=EntityType.PERSON,
        raw_text="Olu Sample",
        normalized_text="Olu Sample",
        confidence=0.95,
        context="",
    )
    monkeypatch.setattr("openfoia.db.get_db_path", lambda *a, **k: db_file)
    monkeypatch.setattr("openfoia.db.get_session", lambda *a, **k: _fake_session([entity]))

    result = CliRunner().invoke(
        app,
        ["crossref", "--sources", "opensanctions-local", "--opensanctions-data", str(FIXTURE_DIR)],
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output  # no network warning, no prompt
    assert "FLAGGED: Olu Sample" in result.output


def test_cli_requested_offline_source_without_data_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))  # no database: flags fail first
    result = CliRunner().invoke(app, ["crossref", "--sources", "opensanctions-local"])
    assert result.exit_code != 0
    assert "needs --opensanctions-data" in result.output


def test_cli_data_flag_pointing_at_the_csv_asks_for_the_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    csv_file = str(FIXTURE_DIR / "targets.simple.csv")
    result = CliRunner().invoke(app, ["crossref", "--opensanctions-data", csv_file])
    assert result.exit_code != 0
    assert "pass the directory containing targets.simple.csv" in result.output


MATCH_PATH = "/match/default"


def _patch_client(monkeypatch, status, data=None):
    """Serve every OpenSanctions adapter request from a mock; returns the requests sent.

    The API code lives in openfoia.records.opensanctions now (crossref goes
    through the adapter), so that is where the client is replaced.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=data if data is not None else {"detail": "x"})

    def fake_egress_client(policy=None, **kw):
        kw.pop("transport", None)
        return egress_client(None, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(os_mod, "egress_client", fake_egress_client)
    return seen


EMPTY = {"responses": {"q": {"results": [], "total": {"value": 0}, "query": {}}}, "limit": 5}


def test_api_key_header_sent_only_when_env_set(monkeypatch, caplog, capsys):
    seen = _patch_client(monkeypatch, 200, EMPTY)

    monkeypatch.delenv(OPENSANCTIONS_KEY_ENV, raising=False)
    with pytest.raises(_SourceCheckError):
        asyncio.run(_check_opensanctions("Ana Fixture", EntityType.PERSON))
    assert seen == []  # no key: the name is not sent to a server that would refuse it

    monkeypatch.setenv(OPENSANCTIONS_KEY_ENV, FAKE_KEY)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(_check_opensanctions("Ana Fixture", EntityType.PERSON))
    assert seen[-1].headers["Authorization"] == f"ApiKey {FAKE_KEY}"
    assert seen[-1].method == "POST" and seen[-1].url.path == MATCH_PATH
    assert "Ana Fixture" not in str(seen[-1].url)  # the name is in the body, not the URL
    out = capsys.readouterr()
    assert FAKE_KEY not in caplog.text + out.out + out.err


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_reports_key_required_and_names_env_var(monkeypatch, status):
    monkeypatch.setenv(OPENSANCTIONS_KEY_ENV, FAKE_KEY)
    _patch_client(monkeypatch, status)

    with pytest.raises(_SourceCheckError) as exc:
        asyncio.run(_check_opensanctions("Ana Fixture", EntityType.PERSON))

    message = str(exc.value)
    assert "API key required" in message
    assert OPENSANCTIONS_KEY_ENV in message
    assert FAKE_KEY not in message


def test_keyless_run_sends_no_names_and_stops_for_the_rest_of_the_run(monkeypatch):
    monkeypatch.delenv(OPENSANCTIONS_KEY_ENV, raising=False)
    monkeypatch.setitem(crossref_mod._SOURCE_DELAYS, "opensanctions", 0.0)
    seen = _patch_client(monkeypatch, 401)
    entities = [
        SimpleNamespace(entity_type=EntityType.PERSON, normalized_text=n, confidence=0.95)
        for n in ("Ana Fixture", "Olu Sample")
    ]

    report = asyncio.run(crossref_entities(entities, sources=["opensanctions"], allow_network=True))

    assert seen == []  # was one doomed request before the adapter checked the key first
    assert report.source_errors == {
        "opensanctions": f"API key required: set {OPENSANCTIONS_KEY_ENV}"
    }
    assert report.results[1].source_statuses["opensanctions"] == "skipped(api-key-required)"


def test_api_key_is_withheld_over_tor(monkeypatch):
    from openfoia.net import EgressMode, EgressPolicy

    monkeypatch.setenv(OPENSANCTIONS_KEY_ENV, FAKE_KEY)
    tor = EgressPolicy(mode=EgressMode.TOR)

    seen = _patch_client(monkeypatch, 200, EMPTY)
    with pytest.raises(crossref_mod._SourceCheckError) as exc:
        asyncio.run(_check_opensanctions("Ana Fixture", EntityType.PERSON, tor))
    assert seen == []  # neither the key nor the name goes out over Tor
    assert "withheld over Tor" in exc.value.error_type
    assert FAKE_KEY not in str(exc.value)


def test_api_hits_carry_score_topics_and_entity_url(monkeypatch):
    monkeypatch.setenv(OPENSANCTIONS_KEY_ENV, FAKE_KEY)
    data = {
        "responses": {
            "q": {
                "results": [
                    {
                        "id": "NK-api001",
                        "caption": "Ivar Testerov",
                        "schema": "Person",
                        "properties": {"topics": ["sanction"], "country": ["ru"]},
                        "datasets": ["us_ofac_sdn"],
                        "score": 0.93,
                        "match": True,
                        "explanations": {},
                    },
                    {
                        "id": "NK-api002",
                        "caption": "Ivar Testerova",
                        "schema": "Person",
                        "properties": {"topics": ["role.pep"]},
                        "datasets": ["wd_peps"],
                        "score": 0.62,
                        "match": False,
                        "explanations": {},
                    },
                ],
                "total": {"value": 2},
                "query": {},
            }
        },
        "limit": 5,
    }
    seen = _patch_client(monkeypatch, 200, data)

    hits = asyncio.run(_check_opensanctions("Ivar Testerov", EntityType.PERSON))

    body = json.loads(seen[0].content)
    assert body["queries"]["q"] == {"schema": "Person", "properties": {"name": ["Ivar Testerov"]}}
    assert [h.match_type for h in hits] == ["exact", "fuzzy"]
    assert hits[0].extra["score"] == 0.93 and hits[0].extra["is_sanctioned"] is True
    assert hits[1].extra["is_pep"] is True and hits[1].extra["topics"] == ["role.pep"]
    assert hits[0].url == "https://www.opensanctions.org/entities/NK-api001/"
    assert "SANCTIONED" in hits[0].details


def test_api_rate_limit_stops_the_source_for_the_run(monkeypatch):
    monkeypatch.setenv(OPENSANCTIONS_KEY_ENV, FAKE_KEY)
    monkeypatch.setitem(crossref_mod._SOURCE_DELAYS, "opensanctions", 0.0)
    seen = _patch_client(monkeypatch, 429)
    entities = [
        SimpleNamespace(entity_type=EntityType.PERSON, normalized_text=n, confidence=0.95)
        for n in ("Ana Fixture", "Olu Sample")
    ]

    report = asyncio.run(crossref_entities(entities, sources=["opensanctions"], allow_network=True))

    assert len(seen) == 1  # POST is never retried by the transport, and the run stops
    assert report.source_errors == {"opensanctions": "RateLimited"}
    assert report.results[1].source_statuses["opensanctions"] == "skipped(rate-limited)"
