"""Statutory clocks for a request, computed from its jurisdiction's rule.

Pure functions, no dependencies beyond the jurisdiction JSON. Business days
skip weekends and the eleven US federal holidays (observed). State holidays
are out of scope: a state clock may run a day short of the statute around a
state-only holiday.

`request_clocks` is the one code path the CLI and the agent share: it reads a
request's sent, extension, toll, denial and appeal dates and returns every
clock. The stored `due_date` column is a cache of its response clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

# Federal FOIA constants that are not in us.json because they are not the
# response clock: appeal decision and expedited-processing decision.
FEDERAL_APPEAL_DECISION_BUSINESS_DAYS = 20
FEDERAL_EXPEDITE_DECISION_CALENDAR_DAYS = 10

NO_CLOCK = "no statutory deadline known for this body"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def federal_holidays(year: int) -> set[date]:
    """The eleven US federal holidays for a year, as observed (5 U.S.C. 6103)."""
    fixed = [(1, 1), (6, 19), (7, 4), (11, 11), (12, 25)]
    days = {_observed(date(year, m, d)) for m, d in fixed}
    days |= {
        _nth_weekday(year, 1, 0, 3),  # Birthday of Martin Luther King, Jr.
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _last_weekday(year, 5, 0),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    }
    # New Year's Day observed on the previous Friday lands in the prior year.
    days.add(_observed(date(year + 1, 1, 1)))
    return days


def _is_business_day(d: datetime, holidays: set[date]) -> bool:
    return d.weekday() < 5 and d.date() not in holidays


def _holidays_around(start: datetime) -> set[date]:
    return federal_holidays(start.year) | federal_holidays(start.year + 1)


def business_days_after(
    start: datetime | date, n: int, holidays: set[date] | None = None
) -> datetime:
    """The datetime n business days after start. Weekends and federal holidays do not count."""
    current = start if isinstance(start, datetime) else datetime(start.year, start.month, start.day)  # noqa: DTZ001
    if holidays is None:
        holidays = _holidays_around(current)
    added = 0
    while added < n:
        current += timedelta(days=1)
        if _is_business_day(current, holidays):
            added += 1
    return current


def business_days_between(a: datetime, b: datetime) -> int:
    """Business days in (a, b]; 0 when b is not after a."""
    if b <= a:
        return 0
    holidays = _holidays_around(a)
    current, n = a, 0
    while current.date() < b.date():
        current += timedelta(days=1)
        if _is_business_day(current, holidays):
            n += 1
    return n


def add_days(start: datetime, n: int | None, unit: str | None) -> datetime | None:
    """n days after start in the statute's unit; None when the rule is unknown or 'none'."""
    if n is None or unit in (None, "none"):
        return None
    if unit == "business":
        return business_days_after(start, n)
    return start + timedelta(days=n)


def due_date(jurisdiction: dict, filed_at: datetime, extended: bool = False) -> datetime | None:
    """Statutory response deadline, plus the one-time extension when taken."""
    r = jurisdiction["response"]
    due = add_days(filed_at, r["days"], r["unit"])
    if due is not None and extended:
        due = add_days(due, r["extension_days"], r["extension_unit"] or r["unit"]) or due
    return due


def response_due(
    jurisdiction: dict,
    filed_at: datetime,
    extended: bool = False,
    tolls: list[tuple[datetime, datetime]] = (),
) -> datetime | None:
    """Base rule, plus the extension, plus every toll in the clock's own unit."""
    due = due_date(jurisdiction, filed_at, extended)
    if due is None:
        return None
    unit = jurisdiction["response"]["unit"]
    for start, until in tolls:
        if until <= start:
            continue
        if unit == "business":
            due = business_days_after(due, business_days_between(start, until))
        else:
            due += timedelta(days=(until - start).days)
    return due


def appeal_deadline(jurisdiction: dict, denied_at: datetime) -> datetime | None:
    """Last day to appeal a denial, or None when the jurisdiction's rule is unknown."""
    a = jurisdiction["appeal"]
    return add_days(denied_at, a["deadline_days"], a["deadline_unit"])


def appeal_decision_due(jurisdiction: dict, appealed_at: datetime) -> datetime | None:
    """When the body must decide an appeal; only federal FOIA defines one here."""
    if jurisdiction["id"] == "us":
        return business_days_after(appealed_at, FEDERAL_APPEAL_DECISION_BUSINESS_DAYS)
    return None


def is_overdue(due: datetime | None, now: datetime) -> bool:
    """Overdue means the due date's day has passed, not that its midnight has."""
    return due is not None and now.date() > due.date()


def rule_text(jurisdiction: dict) -> str:
    """One line a status screen can print next to a deadline."""
    r = jurisdiction["response"]
    if r["unit"] in (None, "none") or r["days"] is None:
        return r["rule_text"] or NO_CLOCK
    return r["rule_text"] or f"{r['days']} {r['unit']} days"


def jurisdiction_for(jurisdiction_id: str | None) -> tuple[dict, bool]:
    """The law for an agency's jurisdiction id; federal FOIA, flagged, when there is none."""
    from .jurisdictions import get

    j = get(jurisdiction_id) if jurisdiction_id else None
    if j is None:
        return get("us"), True
    return j, False


def tolls_of(req) -> list[tuple[datetime, datetime]]:
    """Tolls recorded on a request, as (start, until) pairs, from its extra_data JSON."""
    raw = (req.extra_data or {}).get("tolls") or []
    return [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in raw]


@dataclass
class Clocks:
    law: dict
    fallback: bool  # agency had no jurisdiction; federal FOIA assumed
    response_due: datetime | None  # None: no clock running or none known
    response_kind: str  # "response", "appeal decision" or ""
    response_reason: str  # why response_due is None, for display
    appeal_due: datetime | None
    appeal_reason: str
    still_interested_due: datetime | None


def request_clocks(req) -> Clocks:
    """Every clock on a request, recomputed from the jurisdiction each time.

    The stored due_date is only a cache; callers may write the result back.
    """
    law, fallback = jurisdiction_for(req.agency.jurisdiction_id if req.agency else None)
    status = getattr(req.status, "value", req.status)
    response: datetime | None = None
    kind, reason = "", ""
    if req.sent_at and status not in ("closed", "complete"):
        if req.appealed_at:
            response = appeal_decision_due(law, req.appealed_at)
            kind = "appeal decision" if response else ""
            reason = "" if response else "no statutory appeal-decision deadline for this body"
        elif not req.denied_at:
            response = response_due(law, req.sent_at, req.extended_at is not None, tolls_of(req))
            kind = "response" if response else ""
            reason = "" if response else rule_text(law)
    appeal: datetime | None = None
    appeal_reason = ""
    if req.denied_at and not req.appealed_at and status != "closed":
        appeal = appeal_deadline(law, req.denied_at)
        appeal_reason = "" if appeal else "appeal deadline unknown for this jurisdiction"
    still = req.still_interested_due_at if status != "closed" else None
    return Clocks(law, fallback, response, kind, reason, appeal, appeal_reason, still)
