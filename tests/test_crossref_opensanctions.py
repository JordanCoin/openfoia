"""OpenSanctions: offline screening from targets.simple.csv, and API key handling."""

from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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


def test_cli_requested_offline_source_without_data_fails():
    result = CliRunner().invoke(app, ["crossref", "--sources", "opensanctions-local"])
    assert result.exit_code != 0


def test_cli_data_flag_pointing_at_the_csv_asks_for_the_directory():
    csv_file = str(FIXTURE_DIR / "targets.simple.csv")
    result = CliRunner().invoke(app, ["crossref", "--opensanctions-data", csv_file])
    assert result.exit_code != 0
    assert "pass the directory containing targets.simple.csv" in result.output


class _Resp:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


class _Client:
    def __init__(self, resp, seen):
        self._resp = resp
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self._seen.append(headers)
        return self._resp


def _patch_client(monkeypatch, resp):
    seen: list[dict] = []
    monkeypatch.setattr(crossref_mod, "egress_client", lambda *a, **k: _Client(resp, seen))
    return seen


def test_api_key_header_sent_only_when_env_set(monkeypatch, caplog, capsys):
    seen = _patch_client(monkeypatch, _Resp(200, {"results": []}))

    monkeypatch.delenv(OPENSANCTIONS_KEY_ENV, raising=False)
    asyncio.run(_check_opensanctions("Ana Fixture", EntityType.PERSON))
    assert "Authorization" not in seen[-1]

    monkeypatch.setenv(OPENSANCTIONS_KEY_ENV, FAKE_KEY)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(_check_opensanctions("Ana Fixture", EntityType.PERSON))
    assert seen[-1]["Authorization"] == f"ApiKey {FAKE_KEY}"
    out = capsys.readouterr()
    assert FAKE_KEY not in caplog.text + out.out + out.err


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_reports_key_required_and_names_env_var(monkeypatch, status):
    monkeypatch.setenv(OPENSANCTIONS_KEY_ENV, FAKE_KEY)
    _patch_client(monkeypatch, _Resp(status))

    with pytest.raises(_SourceCheckError) as exc:
        asyncio.run(_check_opensanctions("Ana Fixture", EntityType.PERSON))

    message = str(exc.value)
    assert "API key required" in message
    assert OPENSANCTIONS_KEY_ENV in message
    assert FAKE_KEY not in message


def test_keyless_401_stops_sending_names_for_the_rest_of_the_run(monkeypatch):
    monkeypatch.delenv(OPENSANCTIONS_KEY_ENV, raising=False)
    monkeypatch.setitem(crossref_mod._SOURCE_DELAYS, "opensanctions", 0.0)
    seen = _patch_client(monkeypatch, _Resp(401))
    entities = [
        SimpleNamespace(entity_type=EntityType.PERSON, normalized_text=n, confidence=0.95)
        for n in ("Ana Fixture", "Olu Sample")
    ]

    report = asyncio.run(crossref_entities(entities, sources=["opensanctions"], allow_network=True))

    assert len(seen) == 1
    assert report.source_errors == {
        "opensanctions": f"API key required: set {OPENSANCTIONS_KEY_ENV}"
    }
    assert report.results[1].source_statuses["opensanctions"] == "skipped(api-key-required)"
