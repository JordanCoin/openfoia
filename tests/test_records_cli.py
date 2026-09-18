"""Regression tests for public-records CLI output and documentation."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.records import list_sources
from openfoia.records.base import RecordEntity, SearchResult


def test_records_search_raw_is_parseable_json_without_terminal_output(monkeypatch):
    """Raw mode must be pipe-safe even when upstream data has control characters."""

    class Adapter:
        async def search(self, query, **kwargs):
            return SearchResult(
                source="sec",
                query=query,
                total_results=1,
                entities=[
                    RecordEntity(
                        entity_type="ORGANIZATION",
                        name="Uranium\x1fEnergy",
                        source="sec",
                        extra_data={"snippet": "filing\x00text"},
                    )
                ],
            )

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "Uranium Energy", "--source", "sec", "--raw"],
    )

    assert result.exit_code == 0
    assert "\x1f" not in result.stdout
    assert "\x00" not in result.stdout
    assert json.loads(result.stdout) == [
        {
            "entity_type": "ORGANIZATION",
            "name": "Uranium\x1fEnergy",
            "source": "sec",
            "source_url": None,
            "jurisdiction": None,
            "status": None,
            "identifiers": {},
            "extra_data": {"snippet": "filing\x00text"},
        }
    ]


def test_records_search_help_lists_every_registered_source():
    """CLI help must not hide supported public-records sources."""

    result = CliRunner().invoke(app, ["records", "search", "--help"])

    assert result.exit_code == 0
    for source in list_sources():
        assert source in result.output


def test_records_search_raw_reports_source_errors_instead_of_empty_json(monkeypatch):
    """A failed search must not look like a successful empty result to a script."""

    class Adapter:
        async def search(self, query, **kwargs):
            return SearchResult(
                source="sec",
                query=query,
                total_results=0,
                entities=[],
                error="AdapterRequestError: HTTP 503",
            )

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "Acme Corp", "--source", "sec", "--raw"],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == ""
