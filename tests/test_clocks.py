"""Request clocks come from the jurisdiction's rule, not a hardcoded 20 business days."""

# ruff: noqa: DTZ001  (naive UTC is the storage convention, see test_datetime_semantics)

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.clocks import appeal_deadline, business_days_after, due_date, federal_holidays
from openfoia.jurisdictions import get
from openfoia.models import utcnow

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
    long_ago = utcnow() - timedelta(days=400)
    mi = _sent_request("Detroit Schools", long_ago, "REQ-MI")
    fed = _sent_request("Central Intelligence", utcnow() - timedelta(days=1), "REQ-US")

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
    denied_on = (utcnow() - timedelta(days=200)).strftime("%Y-%m-%d")
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
    soon = (utcnow() + timedelta(days=3)).strftime("%Y-%m-%d")
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
    num = _sent_request("Austin ISD", utcnow() - timedelta(days=30), "REQ-TX")
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


def _request_number(agency_name: str) -> str:
    from openfoia.db import get_session
    from openfoia.models import Agency, Request

    with get_session() as s:
        agency = s.query(Agency).filter(Agency.name.ilike(f"%{agency_name}%")).first()
        return s.query(Request).filter(Request.agency_id == agency.id).one().request_number


def test_agent_send_shares_the_cli_clock_path(data_dir):
    import asyncio

    from openfoia.agent import OpenFOIAAgent
    from openfoia.db import get_session
    from openfoia.models import Request

    _ok(runner.invoke(app, ["init"]))
    _ok(
        runner.invoke(
            app,
            ["agency", "add", "--name", "Lansing Schools", "-j", "us-mi", "-l", "school_district"],
        )
    )
    num = _sent_request("Lansing Schools", datetime(2026, 10, 14), "REQ-AGENT")
    with get_session() as s:
        req = s.query(Request).filter(Request.request_number == num).one()
        req.sent_at = None
        req.due_date = None
        req_id = req.id
    with get_session() as s:
        agent = OpenFOIAAgent(db_session=s, config={})
        out = asyncio.run(agent._send_request({"request_id": req_id}))
        sent = datetime.fromisoformat(out["sent_at"])
        assert out["due_date"] == business_days_after(sent, 5).isoformat()


def test_stale_due_date_under_a_no_clock_law_is_not_shown_or_overdue(data_dir, monkeypatch):
    from openfoia import jurisdictions
    from openfoia.db import get_session
    from openfoia.models import Request

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
    num = _sent_request("Austin ISD", utcnow() - timedelta(days=60), "REQ-STALE")
    with get_session() as s:  # a due date written by the old 20-day code
        s.query(Request).filter(Request.request_number == num).one().due_date = (
            utcnow() - timedelta(days=30)
        )
    out = _ok(runner.invoke(app, ["request", "status", num]))
    assert "no statutory deadline" in out and "days over" not in out
    assert runner.invoke(app, ["deadlines", "check"]).exit_code == 0
    with get_session() as s:
        assert s.query(Request).filter(Request.request_number == num).one().due_date is None


def test_appealed_retires_the_response_clock_everywhere(data_dir):
    _ok(runner.invoke(app, ["init"]))
    _ok(
        runner.invoke(
            app,
            ["agency", "add", "--name", "Flint Schools", "-j", "us-mi", "-l", "school_district"],
        )
    )
    mi = _sent_request("Flint Schools", utcnow() - timedelta(days=30), "REQ-MI2")
    fed = _sent_request("Central Intelligence", utcnow() - timedelta(days=30), "REQ-US2")
    # appealed needs a denial first, unless forced
    assert runner.invoke(app, ["request", "mark", mi, "appealed"]).exit_code == 1
    _ok(runner.invoke(app, ["request", "mark", mi, "denied"]))
    out = _ok(runner.invoke(app, ["request", "mark", mi, "appealed"]))
    assert "no statutory appeal-decision deadline" in out
    out = _ok(runner.invoke(app, ["request", "status", mi]))
    assert "Response due" not in out and "no statutory appeal-decision deadline" in out
    out = _ok(runner.invoke(app, ["request", "mark", fed, "appealed", "--force"]))
    assert "appeal decision due" in out
    out = _ok(runner.invoke(app, ["request", "status", fed]))
    assert "Appeal decision due" in out
    rows = runner.invoke(app, ["deadlines", "list"], env={"COLUMNS": "200"}).output
    assert "appeal decision" in rows and "REQ-US2" in rows


def test_tolling_pauses_in_the_clock_unit_and_survives_extension(data_dir):
    from openfoia.db import get_session
    from openfoia.models import Request

    _ok(runner.invoke(app, ["init"]))
    _ok(
        runner.invoke(
            app,
            [
                "agency",
                "add",
                "--name",
                "Ann Arbor Schools",
                "-j",
                "us-mi",
                "-l",
                "school_district",
            ],
        )
    )
    sent = datetime(2026, 10, 14)  # Wednesday; 5 business days -> Oct 21
    num = _sent_request("Ann Arbor Schools", sent, "REQ-TOLL")
    assert (
        runner.invoke(
            app, ["request", "mark", num, "tolled", "--on", "2026-10-16", "--until", "2026-10-16"]
        ).exit_code
        == 2
    )
    assert (
        runner.invoke(
            app, ["request", "mark", num, "tolled", "--on", "2026-10-16", "--until", "2026-10-10"]
        ).exit_code
        == 2
    )
    # Fri Oct 16 to Mon Oct 19 is one business day of pause: Oct 21 -> Oct 22.
    out = _ok(
        runner.invoke(
            app, ["request", "mark", num, "tolled", "--on", "2026-10-16", "--until", "2026-10-19"]
        )
    )
    assert "response due 2026-10-22" in out
    # The extension stacks on top: +10 business days from Oct 21 is Nov 4, plus the one tolled day.
    out = _ok(runner.invoke(app, ["request", "mark", num, "extended", "--on", "2026-10-20"]))
    assert "response due 2026-11-05" in out
    with get_session() as s:
        req = s.query(Request).filter(Request.request_number == num).one()
        assert req.due_date == datetime(2026, 11, 5)
        assert len(req.extra_data["tolls"]) == 1
    # Federal calendar-day clocks: a toll recorded before any due date exists still applies.
    fed = _sent_request("Central Intelligence", sent, "REQ-TOLL-US")
    with get_session() as s:
        s.query(Request).filter(Request.request_number == fed).one().due_date = None
    out = _ok(
        runner.invoke(
            app, ["request", "mark", fed, "tolled", "--on", "2026-10-15", "--until", "2026-10-22"]
        )
    )
    assert "response due" in out


def test_overdue_grammar_replied_and_no_duplicate_timeline_rows(data_dir):
    from openfoia.db import get_session
    from openfoia.models import Request, TimelineEvent

    _ok(runner.invoke(app, ["init"]))
    num = _sent_request("Central Intelligence", utcnow() - timedelta(days=3), "REQ-GR")
    yesterday = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = utcnow().strftime("%Y-%m-%d")
    _ok(runner.invoke(app, ["request", "mark", num, "still-interested", "--due", yesterday]))
    out = _ok(runner.invoke(app, ["request", "status", num]))
    assert "1 day over" in out and "1 days" not in out
    # Due today is not overdue until the day has passed.
    _ok(runner.invoke(app, ["request", "mark", num, "still-interested", "--due", today]))
    assert "OVERDUE still interested" not in runner.invoke(app, ["deadlines", "check"]).output
    with get_session() as s:
        req = s.query(Request).filter(Request.request_number == num).one()
        events = (
            s.query(TimelineEvent)
            .filter(
                TimelineEvent.request_id == req.id, TimelineEvent.event_type == "still-interested"
            )
            .count()
        )
        assert events == 1
    _ok(runner.invoke(app, ["request", "mark", num, "replied"]))
    out = _ok(runner.invoke(app, ["request", "status", num]))
    assert "Still interested" not in out
    # Closed refuses the lifecycle events that would reopen it.
    _ok(runner.invoke(app, ["request", "mark", num, "closed"]))
    for ev in ("denied", "appealed", "received"):
        assert runner.invoke(app, ["request", "mark", num, ev]).exit_code == 1
