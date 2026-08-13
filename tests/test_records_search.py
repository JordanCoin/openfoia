"""Regression tests for public-records search output."""

from __future__ import annotations

from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.records.base import SearchResult


def test_typed_sec_empty_search_reports_scope_and_coverage_limit(monkeypatch):
    """A zero typed SEC result must not look like proof that no filing exists."""

    class Adapter:
        async def search(self, query, **kwargs):
            assert query == "Webull Corp"
            assert kwargs == {"filing_type": "F-3"}
            return SearchResult(source="sec", query=query, total_results=0, entities=[])

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "Webull Corp", "--source", "sec", "--type", "F-3"],
    )

    assert result.exit_code == 0
    output = " ".join(result.output.lower().split())
    assert "sec returned 0 total results for 'webull corp'" in output
    assert "showing 0 of 0 results" in output
    assert "applied sec filing type filter: f-3" in output
    assert "0 results is not proof that no filing exists" in output


def test_non_sec_empty_search_keeps_existing_message(monkeypatch):
    """The typed SEC warning must not change other sources' output."""

    class Adapter:
        async def search(self, query, **kwargs):
            return SearchResult(source="opencorporates", query=query, total_results=0, entities=[])

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "No Such Company", "--source", "opencorporates"],
    )

    assert result.exit_code == 0
    assert "No results found for 'No Such Company' on opencorporates." in result.output
