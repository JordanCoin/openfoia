"""Jurisdiction data files, their loader, the CLI, and local public bodies."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.jurisdictions import _DIR, BODY_TYPES, get, load_all, search

runner = CliRunner()

KEYS = {
    "id": str,
    "name": str,
    "level": str,
    "statute": {"name", "citation", "url"},
    "response": {
        "days",
        "unit",
        "rule_text",
        "extension_days",
        "extension_unit",
        "silence_is_denial",
    },
    "fees": {
        "search_allowed",
        "labor_rule",
        "copy_rule",
        "waiver_rule",
        "estimate_required",
        "late_penalty",
    },
    "residency": {"restricted", "rule_text"},
    "appeal": {"body", "type", "deadline_days", "deadline_unit", "rule_text"},
    "exemptions_citation": (str, type(None)),
    "format_rule": (str, type(None)),
    "covers": {"governor", "legislature", "universities_public", "school_districts"},
    "sources": list,
    "verified": bool,
    "verified_at": (str, type(None)),
    "notes": (str, type(None)),
}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENFOIA_AI_PROVIDER", "none")
    return tmp_path


def _ok(result):
    assert result.exit_code == 0, result.output
    return result.output


def test_every_file_matches_schema_and_filename():
    files = sorted(Path(_DIR).glob("*.json"))
    assert len(files) == 52  # federal + 50 states + DC
    for path in files:
        j = json.loads(path.read_text())
        assert j["id"] == path.stem, path
        assert set(j) == set(KEYS), (path, set(j) ^ set(KEYS))
        for key, spec in KEYS.items():
            if isinstance(spec, set):
                assert set(j[key]) == spec, (path, key)
            else:
                assert isinstance(j[key], spec), (path, key)
        assert j["level"] in ("federal", "state")
        assert j["response"]["unit"] in (None, "business", "calendar", "none")
        assert j["appeal"]["type"] in (
            None,
            "court",
            "commission",
            "attorney_general",
            "agency_head",
            "ombudsman_advisory",
        )
        if j["verified"]:
            assert j["sources"] and j["verified_at"], path
        assert all(u.startswith("https://") for u in j["sources"]), path


def test_loader_get_and_search():
    data = load_all()
    assert next(iter(data)) == "us"  # federal first
    assert get("US-MI")["response"]["days"] == 5
    assert get("nope") is None
    assert [j["id"] for j in search("michigan")] == ["us-mi"]
    assert "us-tx" in {j["id"] for j in search("Public Information")}


def test_verified_entries_are_the_researched_ones():
    verified = {j["id"] for j in load_all().values() if j["verified"]}
    assert verified == {"us", "us-mi", "us-il", "us-tx", "us-ny", "us-wa", "us-fl"}
    assert get("us")["response"] == {
        "days": 20,
        "unit": "business",
        "rule_text": get("us")["response"]["rule_text"],
        "extension_days": 10,
        "extension_unit": "business",
        "silence_is_denial": None,
    }
    assert get("us-mi")["covers"]["governor"] is False


def test_jurisdiction_cli(data_dir):
    out = _ok(runner.invoke(app, ["jurisdiction", "list"]))
    assert "us-mi" in out and "Michigan" in out
    out = _ok(runner.invoke(app, ["jurisdiction", "list", "--unverified"]))
    assert "us-mi" not in out and "us-ca" in out
    out = _ok(runner.invoke(app, ["jurisdiction", "show", "us-mi"]))
    assert "Verified 2026-10-14" in out and "MCL 15.243" in out and "rcfp.org" in out
    out = _ok(runner.invoke(app, ["jurisdiction", "show", "us-ca"]))
    assert "UNVERIFIED" in out and "unknown" in out
    assert (
        json.loads(_ok(runner.invoke(app, ["jurisdiction", "show", "us", "--json"])))["id"] == "us"
    )
    assert runner.invoke(app, ["jurisdiction", "show", "mars"]).exit_code == 1
    out = _ok(runner.invoke(app, ["jurisdiction", "search", "texas"]))
    assert "us-tx" in out
    assert runner.invoke(app, ["jurisdiction", "search", "zzz"]).exit_code == 1


def test_agency_add_list_info_and_migration_head(data_dir):
    from openfoia import db as dbmod

    _ok(runner.invoke(app, ["init"]))
    version = "select version_num from alembic_version"
    assert sqlite3.connect(dbmod.get_db_path()).execute(version).fetchall() == [("007",)]

    out = _ok(
        runner.invoke(
            app,
            [
                "agency",
                "add",
                "--name",
                "Detroit Public Schools Community District",
                "-j",
                "us-mi",
                "-l",
                "school_district",
                "--email",
                "foia.request@detroitk12.org",
                "--custodian-title",
                "FOIA Coordinator",
            ],
        )
    )
    assert "Michigan Freedom of Information Act" in out and "5 response days" in out
    # Stub jurisdiction warns, unknown ones and bad levels are refused.
    out = _ok(
        runner.invoke(
            app, ["agency", "add", "--name", "Oakland USD", "-j", "us-ca", "-l", "school_district"]
        )
    )
    assert "unverified stub" in out
    assert (
        runner.invoke(app, ["agency", "add", "--name", "X", "-j", "us-zz", "-l", "city"]).exit_code
        == 2
    )
    assert (
        runner.invoke(
            app, ["agency", "add", "--name", "X", "-j", "us-mi", "-l", "planet"]
        ).exit_code
        == 2
    )
    assert (
        runner.invoke(app, ["agency", "add", "--name", "X", "-j", "us", "-l", "city"]).exit_code
        == 2
    )
    assert (
        runner.invoke(
            app, ["agency", "add", "--name", "Oakland USD", "-j", "us-ca", "-l", "city"]
        ).exit_code
        == 1
    )

    out = _ok(
        runner.invoke(
            app, ["agency", "list", "-j", "us-mi", "-l", "school_district"], env={"COLUMNS": "200"}
        )
    )
    assert "Detroit" in out and "Oakland" not in out
    out = _ok(runner.invoke(app, ["agency", "list", "-l", "federal", "-n", "5"]))
    assert "Detroit" not in out
    out = _ok(runner.invoke(app, ["agency", "info", "Detroit Public Schools"]))
    assert "school_district" in out and "us-mi" in out and "FOIA Coordinator" in out
    out = _ok(runner.invoke(app, ["agency", "info", "CIA"]))
    assert "us: Freedom of Information Act" in out
    assert all(b in BODY_TYPES for b in ("school_district", "university"))


def test_auto_upgrade_from_005_marks_seeded_agencies_federal(data_dir, monkeypatch):
    from alembic import command

    from openfoia import db as dbmod

    cfg = dbmod._alembic_config()
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{dbmod.get_db_path()}")
    command.upgrade(cfg, "005")
    con = sqlite3.connect(dbmod.get_db_path())
    con.execute(
        "insert into agencies (id, name, level, preferred_method, typical_response_days) "
        "values ('a1', 'Old Federal', 'FEDERAL', 'EMAIL', 20)"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(dbmod, "_schema_checked", False)
    with dbmod.get_session() as s:
        row = (
            s.connection()
            .exec_driver_sql("select jurisdiction_id, body_type from agencies where id='a1'")
            .fetchone()
        )
    assert tuple(row) == ("us", "federal")


STUB_ALLOWED = {("id",), ("name",), ("level",), ("statute", "name"), ("sources",), ("verified",)}


def _leaves(obj, prefix=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, (*prefix, k))
    else:
        yield prefix, obj


def test_stubs_are_null_everywhere_except_identity():
    """A stub may not carry a value nobody sourced."""
    for j in load_all().values():
        if j["verified"]:
            continue
        for path, value in _leaves(j):
            if path in STUB_ALLOWED:
                continue
            assert value is None, (j["id"], path, value)


def test_verified_entries_never_store_a_clock_without_a_day_count():
    for i in ("us-tx", "us-wa", "us-ny", "us-fl"):
        assert get(i)["response"]["days"] is None, i
    assert get("us-fl")["response"]["unit"] == "none"


def test_malformed_file_names_itself(tmp_path, monkeypatch):
    import openfoia.jurisdictions as mod

    (tmp_path / "us-xx.json").write_text("{not json")
    monkeypatch.setattr(mod, "_DIR", tmp_path)
    mod.load_all.cache_clear()
    try:
        with pytest.raises(ValueError, match=r"us-xx\.json"):
            mod.load_all()
        (tmp_path / "us-xx.json").write_text(json.dumps({"id": "us-yy"}))
        mod.load_all.cache_clear()
        with pytest.raises(ValueError, match="does not match filename"):
            mod.load_all()
    finally:
        mod.load_all.cache_clear()


def test_unknown_clock_means_no_due_date(data_dir, monkeypatch):
    from datetime import UTC, datetime

    from openfoia.db import get_session
    from openfoia.gateways.base import DeliveryResult, DeliveryStatus
    from openfoia.models import Agency, Request

    _ok(runner.invoke(app, ["init"]))
    out = _ok(
        runner.invoke(
            app,
            [
                "agency",
                "add",
                "--name",
                "Austin ISD",
                "-j",
                "us-tx",
                "-l",
                "school_district",
                "--email",
                "records@austinisd.example",
            ],
        )
    )
    assert "unknown (no statutory day count)" in " ".join(out.split())
    with get_session() as s:
        assert (
            s.query(Agency).filter(Agency.name == "Austin ISD").one().typical_response_days is None
        )
        assert (
            s.query(Agency).filter(Agency.abbreviation == "CIA").one().typical_response_days == 45
        )
    out = _ok(runner.invoke(app, ["agency", "info", "Austin ISD"], env={"COLUMNS": "200"}))
    assert "unknown (no statutory day count)" in " ".join(out.split())

    class FakeGateway:
        def __init__(self, **kw):
            pass

        async def send(self, payload):
            return DeliveryResult(
                status=DeliveryStatus.SENT,
                reference_id="ref-1",
                sent_at=datetime(2026, 1, 5, tzinfo=UTC),
            )

    monkeypatch.setattr("openfoia.gateways.email.EmailGateway", FakeGateway)
    monkeypatch.setenv("OPENFOIA_SMTP_USER", "u")
    monkeypatch.setenv("OPENFOIA_SMTP_PASSWORD", "p")
    args = ["-a", "Austin ISD", "-s", "Gifted program 1990s", "-n", "Jo", "-e", "jo@example.com"]
    _ok(runner.invoke(app, ["request", "new", *args, "-b", "Please send the records."]))
    out = _ok(runner.invoke(app, ["request", "send", *args, "-b", "Please send the records."]))
    assert "no statutory deadline known for this body" in out
    with get_session() as s:
        req = s.query(Request).one()
        assert req.sent_at is not None and req.due_date is None
    out = _ok(runner.invoke(app, ["deadlines", "list"]))
    assert "no statutory deadline known for this body" in out
    assert runner.invoke(app, ["deadlines", "check"]).exit_code == 0
