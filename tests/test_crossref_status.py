"""Regression tests for honest cross-reference source statuses."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openfoia import crossref as crossref_mod
from openfoia.crossref import CrossRefHit, _check_icij, crossref_entities
from openfoia.models import EntityType


def test_crossref_reports_a_failed_source_without_hiding_successful_hits(monkeypatch, caplog):
    """A broken source must not look like a clean no-match result."""

    async def failing_checker(name, entity_type):
        raise RuntimeError("upstream response included the query")

    async def matching_checker(name, entity_type):
        return [
            CrossRefHit(
                source="working",
                entity_name=name,
                match_type="exact",
                details="match",
            )
        ]

    async def no_sleep(duration):
        return None

    monkeypatch.setattr(
        crossref_mod,
        "_get_available_sources",
        lambda icij_data_dir, egress: {"broken": failing_checker, "working": matching_checker},
    )
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    entity = SimpleNamespace(
        entity_type=EntityType.ORGANIZATION,
        normalized_text="Acme Corp",
        confidence=1.0,
    )
    report = asyncio.run(crossref_entities([entity], allow_network=True))

    assert report.total_hits == 1
    assert report.source_errors == {"broken": "RuntimeError"}
    assert report.results[0].source_statuses == {
        "broken": "ERRORED(RuntimeError)",
        "working": "matched",
    }
    assert "Acme Corp" not in caplog.text
    assert "upstream response included the query" not in caplog.text


def test_crossref_reports_rate_limit_as_an_incomplete_source(monkeypatch):
    """Rate-limited searches must not be represented as clean no-matches."""

    async def rate_limited_checker(name, entity_type):
        raise crossref_mod._RateLimited("source limit")

    async def working_checker(name, entity_type):
        return []

    async def no_sleep(duration):
        return None

    monkeypatch.setattr(
        crossref_mod,
        "_get_available_sources",
        lambda icij_data_dir, egress: {"limited": rate_limited_checker, "working": working_checker},
    )
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    entities = [
        SimpleNamespace(
            entity_type=EntityType.ORGANIZATION,
            normalized_text=name,
            confidence=1.0,
        )
        for name in ("Acme Corp", "Globex Corp")
    ]

    report = asyncio.run(crossref_entities(entities, allow_network=True))

    assert report.source_errors == {"limited": "RateLimited"}
    assert report.results[0].source_statuses["limited"] == "ERRORED(RateLimited)"
    assert report.results[1].source_statuses["limited"] == "skipped(rate-limited)"


def test_icij_read_failure_propagates_as_a_redacted_source_error(monkeypatch, tmp_path):
    """Unreadable local ICIJ data must reach the report error boundary."""

    csv_file = tmp_path / "offshore.csv"
    csv_file.write_text("name\nAcme Corp\n")
    original_open = open

    def fail_open(path, *args, **kwargs):
        if path == csv_file:
            raise OSError("sensitive local path details")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_open)

    with pytest.raises(crossref_mod._SourceCheckError) as error:
        asyncio.run(_check_icij("Acme Corp", EntityType.ORGANIZATION, str(tmp_path)))

    assert error.value.error_type == "OSError"


def test_icij_partial_failure_keeps_hits_from_readable_files(monkeypatch, tmp_path):
    """One unreadable CSV must not erase matches found in the files that opened."""

    good = tmp_path / "a_readable.csv"
    good.write_text("name\nAcme Corp\n")
    bad = tmp_path / "b_unreadable.csv"
    bad.write_text("name\nAcme Corp\n")
    original_open = open

    def fail_open(path, *args, **kwargs):
        if path == bad:
            raise OSError("sensitive local path details")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_open)

    with pytest.raises(crossref_mod._SourceCheckError) as error:
        asyncio.run(_check_icij("Acme Corp", EntityType.ORGANIZATION, str(tmp_path)))

    assert error.value.error_type == "OSError"
    assert [hit.entity_name for hit in error.value.partial_hits] == ["Acme Corp"]


def test_partially_failed_source_reports_error_and_keeps_its_hits(monkeypatch):
    """A source that fails mid-check stays flagged ERRORED but keeps real matches."""

    async def partially_failing_checker(name, entity_type):
        raise crossref_mod._SourceCheckError(
            OSError("unreadable shard"),
            partial_hits=[
                CrossRefHit(
                    source="icij",
                    entity_name=name,
                    match_type="partial",
                    details="found before the failure",
                )
            ],
        )

    async def no_sleep(duration):
        return None

    monkeypatch.setattr(
        crossref_mod,
        "_get_available_sources",
        lambda icij_data_dir, egress: {"icij": partially_failing_checker},
    )
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    entity = SimpleNamespace(
        entity_type=EntityType.ORGANIZATION,
        normalized_text="Acme Corp",
        confidence=1.0,
    )
    report = asyncio.run(crossref_entities([entity], allow_network=True))

    assert report.total_hits == 1
    assert report.total_flagged == 1
    assert report.source_errors == {"icij": "OSError"}
    assert report.results[0].source_statuses == {"icij": "ERRORED(OSError)"}


def test_adapter_error_reports_its_own_failure_kind(monkeypatch):
    """Every remote failure must not collapse into one opaque 'RuntimeError'."""

    kinds = []

    for message, expected in (
        ("HTTPStatusError: Server error '503' for url ...", "HTTPStatusError"),
        ("ConnectError: [Errno -3] Temporary failure in name resolution", "ConnectError"),
        ("something went wrong", "SourceError"),
    ):
        with pytest.raises(crossref_mod._SourceCheckError) as error:
            crossref_mod._check_rate_limit(SimpleNamespace(error=message))
        kinds.append(error.value.error_type)
        # The detail can echo the looked-up name via the URL; only the kind travels.
        assert message not in str(error.value)
        assert error.value.error_type == expected

    assert kinds == ["HTTPStatusError", "ConnectError", "SourceError"]


def test_empty_icij_directory_is_reported_rather_than_called_clean(tmp_path):
    """A data directory holding no CSVs means nothing was searched."""

    with pytest.raises(crossref_mod._SourceCheckError) as error:
        asyncio.run(_check_icij("Acme Corp", EntityType.ORGANIZATION, str(tmp_path)))

    assert error.value.error_type == "NoICIJData"
    assert error.value.partial_hits == []
