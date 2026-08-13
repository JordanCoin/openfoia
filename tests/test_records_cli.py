"""Regression tests for public-records CLI documentation."""

from __future__ import annotations

from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.records import list_sources


def test_records_search_help_lists_every_registered_source():
    """CLI help must not hide supported public-records sources."""

    result = CliRunner().invoke(app, ["records", "search", "--help"])

    assert result.exit_code == 0
    for source in list_sources():
        assert source in result.output
