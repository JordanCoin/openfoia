"""Tests for the custom entity type CLI commands.

Tests the full lifecycle: add, list, import (CSV), export, test, remove.
"""

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openfoia.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Use a temp directory for ~/.openfoia so tests don't touch real config."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Also patch Path.home() for config.py
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    yield fake_home


def _get_config(home: Path) -> dict:
    config_path = home / ".openfoia" / "config.json"
    if config_path.exists():
        return json.loads(config_path.read_text())
    return {}


class TestEntitiesAdd:
    def test_add_basic(self, isolated_config):
        result = runner.invoke(
            app,
            [
                "entities",
                "add",
                "-n",
                "CONTRACT_NUMBER",
                "-p",
                r"\b[A-Z]{2,4}-\d{4,}-\d{4,}\b",
                "-d",
                "Federal contract numbers",
            ],
        )
        assert result.exit_code == 0
        assert "Added entity type 'CONTRACT_NUMBER'" in result.output

        config = _get_config(isolated_config)
        types = config["entities"]["custom_types"]
        assert len(types) == 1
        assert types[0]["name"] == "CONTRACT_NUMBER"
        assert types[0]["description"] == "Federal contract numbers"

    def test_add_normalizes_name(self, isolated_config):
        result = runner.invoke(
            app,
            [
                "entities",
                "add",
                "-n",
                "case number",
                "-p",
                r"\b\d{2}-cv-\d{4,}\b",
            ],
        )
        assert result.exit_code == 0

        config = _get_config(isolated_config)
        assert config["entities"]["custom_types"][0]["name"] == "CASE_NUMBER"

    def test_add_rejects_invalid_regex(self, isolated_config):
        result = runner.invoke(
            app,
            [
                "entities",
                "add",
                "-n",
                "BAD",
                "-p",
                "[invalid",
            ],
        )
        assert result.exit_code == 1
        assert "Invalid regex" in result.output

    def test_add_rejects_duplicate(self, isolated_config):
        runner.invoke(app, ["entities", "add", "-n", "TEST", "-p", r"\btest\b"])
        result = runner.invoke(app, ["entities", "add", "-n", "TEST", "-p", r"\btest2\b"])
        assert result.exit_code == 1
        assert "already exists" in result.output


class TestEntitiesList:
    def test_list_empty(self, isolated_config):
        result = runner.invoke(app, ["entities", "list"])
        assert result.exit_code == 0
        assert "No custom entity types" in result.output

    def test_list_with_types(self, isolated_config):
        runner.invoke(app, ["entities", "add", "-n", "FOO", "-p", r"\bfoo\b", "-d", "Test foo"])
        runner.invoke(app, ["entities", "add", "-n", "BAR", "-p", r"\bbar\b", "-d", "Test bar"])

        result = runner.invoke(app, ["entities", "list"])
        assert result.exit_code == 0
        assert "FOO" in result.output
        assert "BAR" in result.output


class TestEntitiesRemove:
    def test_remove(self, isolated_config):
        runner.invoke(app, ["entities", "add", "-n", "TEMP", "-p", r"\btemp\b"])
        result = runner.invoke(app, ["entities", "remove", "TEMP"])
        assert result.exit_code == 0
        assert "Removed" in result.output

        config = _get_config(isolated_config)
        assert len(config["entities"]["custom_types"]) == 0

    def test_remove_nonexistent(self, isolated_config):
        result = runner.invoke(app, ["entities", "remove", "NOPE"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestEntitiesImport:
    def test_import_standard_csv(self, isolated_config, tmp_path):
        csv_file = tmp_path / "types.csv"
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "pattern", "description"])
            writer.writerow(["CONTRACT", r"\bCON-\d+\b", "Contract numbers"])
            writer.writerow(["GRANT", r"\bGR-\d+\b", "Grant numbers"])
            writer.writerow(["INVOICE", r"\bINV-\d+\b", "Invoice numbers"])

        result = runner.invoke(app, ["entities", "import", str(csv_file), "--no-smart"])
        assert result.exit_code == 0
        assert "Imported 3 entity type(s)" in result.output

        config = _get_config(isolated_config)
        assert len(config["entities"]["custom_types"]) == 3

    def test_import_fuzzy_columns(self, isolated_config, tmp_path):
        """Columns named differently should still map correctly."""
        csv_file = tmp_path / "types.csv"
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["entity_type", "regex", "notes"])
            writer.writerow(["BADGE_NUMBER", r"\bBDG-\d{6}\b", "Employee badge IDs"])

        result = runner.invoke(app, ["entities", "import", str(csv_file), "--no-smart"])
        assert result.exit_code == 0
        assert "Imported 1 entity type(s)" in result.output

    def test_import_skips_duplicates(self, isolated_config, tmp_path):
        runner.invoke(app, ["entities", "add", "-n", "EXISTING", "-p", r"\bEX\b"])

        csv_file = tmp_path / "types.csv"
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "pattern", "description"])
            writer.writerow(["EXISTING", r"\bEX\b", "dupe"])
            writer.writerow(["NEW_ONE", r"\bNEW\b", "fresh"])

        result = runner.invoke(app, ["entities", "import", str(csv_file), "--no-smart"])
        assert result.exit_code == 0
        assert "Imported 1" in result.output
        assert "Skipped 1" in result.output

    def test_import_rejects_bad_regex(self, isolated_config, tmp_path):
        csv_file = tmp_path / "types.csv"
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "pattern", "description"])
            writer.writerow(["GOOD", r"\bgood\b", "ok"])
            writer.writerow(["BAD", "[invalid", "broken regex"])

        result = runner.invoke(app, ["entities", "import", str(csv_file), "--no-smart"])
        assert result.exit_code == 0
        assert "Imported 1" in result.output
        assert "1 error" in result.output

    def test_import_file_not_found(self, isolated_config):
        result = runner.invoke(app, ["entities", "import", "/nonexistent.csv", "--no-smart"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestEntitiesExport:
    def test_export(self, isolated_config, tmp_path):
        runner.invoke(app, ["entities", "add", "-n", "A", "-p", r"\ba\b", "-d", "first"])
        runner.invoke(app, ["entities", "add", "-n", "B", "-p", r"\bb\b", "-d", "second"])

        out_file = tmp_path / "out.csv"
        result = runner.invoke(app, ["entities", "export", "-o", str(out_file)])
        assert result.exit_code == 0
        assert "Exported 2" in result.output

        with open(out_file) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["name"] == "A"
        assert rows[1]["name"] == "B"

    def test_export_empty(self, isolated_config):
        result = runner.invoke(app, ["entities", "export"])
        assert result.exit_code == 0
        assert "No custom entity types" in result.output


class TestEntitiesTest:
    def test_pattern_matching(self, isolated_config):
        runner.invoke(
            app, ["entities", "add", "-n", "SSN", "-p", r"\b\d{3}-\d{2}-\d{4}\b", "-d", "SSNs"]
        )
        runner.invoke(
            app, ["entities", "add", "-n", "ZIP", "-p", r"\b\d{5}(?:-\d{4})?\b", "-d", "ZIP codes"]
        )

        result = runner.invoke(
            app,
            [
                "entities",
                "test",
                "-t",
                "John's SSN is 123-45-6789 and he lives in 20530-1234",
            ],
        )
        assert result.exit_code == 0
        assert "123-45-6789" in result.output
        assert "20530-1234" in result.output

    def test_no_types_configured(self, isolated_config):
        result = runner.invoke(app, ["entities", "test", "-t", "hello world"])
        assert result.exit_code == 1
        assert "No custom entity types" in result.output


class TestRoundTrip:
    """Test the full add → export → import → test cycle."""

    def test_full_lifecycle(self, isolated_config, tmp_path):
        # Add some types
        runner.invoke(
            app,
            [
                "entities",
                "add",
                "-n",
                "FOIA_TRACKING",
                "-p",
                r"\bFOIA-\d{4}-\d+\b",
                "-d",
                "FOIA tracking",
            ],
        )
        runner.invoke(
            app,
            [
                "entities",
                "add",
                "-n",
                "CASE_REF",
                "-p",
                r"\b\d{2}-cv-\d{4,}\b",
                "-d",
                "Court cases",
            ],
        )

        # Export
        csv_file = tmp_path / "exported.csv"
        runner.invoke(app, ["entities", "export", "-o", str(csv_file)])

        # Remove all
        runner.invoke(app, ["entities", "remove", "FOIA_TRACKING"])
        runner.invoke(app, ["entities", "remove", "CASE_REF"])

        # Verify empty
        result = runner.invoke(app, ["entities", "list"])
        assert "No custom entity types" in result.output

        # Re-import
        result = runner.invoke(app, ["entities", "import", str(csv_file), "--no-smart"])
        assert "Imported 2" in result.output

        # Test against sample text
        result = runner.invoke(
            app,
            [
                "entities",
                "test",
                "-t",
                "Request FOIA-2026-001234 relates to case 24-cv-0892",
            ],
        )
        assert "FOIA-2026-001234" in result.output
        assert "24-cv-0892" in result.output
