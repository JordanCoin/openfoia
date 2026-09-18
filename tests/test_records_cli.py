"""Regression tests for public-records CLI output."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from openfoia.cli import app
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
