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
