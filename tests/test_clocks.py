"""Request clocks come from the jurisdiction's rule, not a hardcoded 20 business days."""

# ruff: noqa: DTZ001, DTZ005  (naive UTC is the storage convention, see test_datetime_semantics)

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.clocks import appeal_deadline, business_days_after, due_date, federal_holidays
from openfoia.jurisdictions import get

runner = CliRunner()


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    return tmp_path / "data"


def _ok(result):
    assert result.exit_code == 0, result.output
    return result.output


def test_business_days_skip_weekends_and_federal_holidays():
    # 2026-07-04 is a Saturday, observed Friday 2026-07-03.
    assert date(2026, 7, 3) in federal_holidays(2026)
    assert date(2026, 11, 26) in federal_holidays(2026)  # Thanksgiving
    # Thursday 2026-07-02 + 1 business day skips the observed holiday and the weekend.
    assert business_days_after(datetime(2026, 7, 2), 1) == datetime(2026, 7, 6)
    # A Friday + 1 business day is Monday.
    assert business_days_after(datetime(2026, 10, 16), 1) == datetime(2026, 10, 19)


def test_due_dates_follow_the_jurisdiction():
    filed = datetime(2026, 10, 14)  # Wednesday
    mi = get("us-mi")
    assert due_date(mi, filed) == datetime(2026, 10, 21)  # 5 business days
    assert due_date(mi, filed, extended=True) == datetime(2026, 11, 4)  # + 10 business days
    us = get("us")
    assert due_date(us, filed) == business_days_after(filed, 20)
    assert appeal_deadline(us, filed) == filed + timedelta(days=90)
    none = {
        "response": {"days": None, "unit": "none", "extension_days": None, "extension_unit": None}
    }
    assert due_date(none, filed) is None
    unknown = {"appeal": {"deadline_days": None, "deadline_unit": None}}
    assert appeal_deadline(unknown, filed) is None


def _sent_request(agency_name: str, sent_at: datetime, number: str) -> str:
    from openfoia.db import get_session
    from openfoia.models import Agency, DeliveryMethod, Request, RequestStatus, User

    with get_session() as s:
        user = s.query(User).first() or User(id=str(uuid4()), email="u@x.test", name="U")
        s.add(user)
        agency = s.query(Agency).filter(Agency.name.ilike(f"%{agency_name}%")).first()
        req = Request(
            id=str(uuid4()),
            request_number=number,
            requester_id=user.id,
            agency_id=agency.id,
            subject=f"records from {agency_name}",
            body="x",
            delivery_method=DeliveryMethod.EMAIL,
            status=RequestStatus.SENT,
            sent_at=sent_at,
        )
        s.add(req)
        return number


def test_mark_events_and_deadlines(data_dir):
    _ok(runner.invoke(app, ["init"]))
    _ok(
        runner.invoke(
            app,
            ["agency", "add", "--name", "Detroit Schools", "-j", "us-mi", "-l", "school_district"],
        )
    )
    long_ago = datetime.now() - timedelta(days=400)
    mi = _sent_request("Detroit Schools", long_ago, "REQ-MI")
    fed = _sent_request("Central Intelligence", datetime.now() - timedelta(days=1), "REQ-US")

    out = _ok(runner.invoke(app, ["request", "status", mi]))
    assert "us-mi: Michigan Freedom of Information Act" in out and "Response due" in out
    out = _ok(runner.invoke(app, ["request", "status", fed]))
    assert "us: Freedom of Information Act" in out

    # Extension pushes the Michigan clock by 10 business days from the 5-day due date.
    out = _ok(
        runner.invoke(
            app, ["request", "mark", mi, "extended", "--on", long_ago.strftime("%Y-%m-%d")]
        )
    )
    assert "response due" in out
    expected = due_date(get("us-mi"), long_ago, extended=True)
    assert expected.strftime("%Y-%m-%d") in out

    # Denial sets the appeal window from the jurisdiction (Michigan: 180 calendar days).
    denied_on = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    out = _ok(runner.invoke(app, ["request", "mark", mi, "denied", "--on", denied_on]))
    assert "appeal due" in out
    out = _ok(runner.invoke(app, ["request", "status", mi]))
    assert "Appeal due" in out and "days over" in out

    # deadlines check flags the overdue appeal and exits 1; list groups it under OVERDUE.
    r = runner.invoke(app, ["deadlines", "check"])
    assert r.exit_code == 1 and "OVERDUE appeal: REQ-MI" in r.output
    out = runner.invoke(app, ["deadlines", "list"], env={"COLUMNS": "200"}).output
    assert "OVERDUE" in out and "appeal" in out and "REQ-US" in out and "UPCOMING" in out

    # Still-interested and tolling need their dates; appealed clears the appeal clock.
    assert runner.invoke(app, ["request", "mark", fed, "tolled"]).exit_code == 2
    assert runner.invoke(app, ["request", "mark", fed, "still-interested"]).exit_code == 2
    assert runner.invoke(app, ["request", "mark", fed, "launched"]).exit_code == 2
    soon = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    _ok(runner.invoke(app, ["request", "mark", fed, "still-interested", "--due", soon]))
    out = _ok(runner.invoke(app, ["request", "status", fed]))
    assert "Still interested" in out and "reply due" in out
    _ok(runner.invoke(app, ["request", "mark", mi, "appealed"]))
    r = runner.invoke(app, ["deadlines", "check"])
    assert "OVERDUE appeal:" not in r.output


def test_no_statutory_deadline_is_said_not_invented(data_dir, monkeypatch):
    from openfoia import jurisdictions

    _ok(runner.invoke(app, ["init"]))
    _ok(
        runner.invoke(
            app, ["agency", "add", "--name", "Austin ISD", "-j", "us-tx", "-l", "school_district"]
        )
    )
    tx = dict(jurisdictions.get("us-tx"))
    tx["response"] = {**tx["response"], "days": None, "unit": "none", "rule_text": "promptly"}
    monkeypatch.setattr(
        jurisdictions,
        "get",
        lambda jid: tx if jid == "us-tx" else jurisdictions.load_all().get(jid),
    )
    num = _sent_request("Austin ISD", datetime.now() - timedelta(days=30), "REQ-TX")
    out = _ok(runner.invoke(app, ["request", "status", num]))
    assert "no statutory deadline" in out and "promptly" in out
    out = _ok(runner.invoke(app, ["deadlines", "list"], env={"COLUMNS": "200"}))
    assert "NO STATUTORY DEADLINE (1)" in out and "REQ-TX" in out
    assert runner.invoke(app, ["deadlines", "check"]).exit_code == 0


def test_migration_head_007_and_auto_upgrade_from_006(data_dir, monkeypatch):
    from alembic import command

    from openfoia import db as dbmod

    dbmod.init_db(seed=False)
    version = "select version_num from alembic_version"
    assert sqlite3.connect(dbmod.get_db_path()).execute(version).fetchall() == [("007",)]

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(data_dir.parent / "old"))
    cfg = dbmod._alembic_config()
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{dbmod.get_db_path()}")
    command.upgrade(cfg, "006")
    monkeypatch.setattr(dbmod, "_schema_checked", False)
    with dbmod.get_session() as s:
        s.connection().exec_driver_sql("select appeal_due_at from requests").fetchall()
    assert sqlite3.connect(dbmod.get_db_path()).execute(version).fetchall() == [("007",)]
