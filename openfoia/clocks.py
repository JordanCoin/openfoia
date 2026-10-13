"""Statutory clocks for a request, computed from its jurisdiction's rule.

Pure functions, no dependencies. Business days skip weekends and the eleven
US federal holidays (observed). State holidays are out of scope: a state clock
may run a day short of the statute around a state-only holiday.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# Federal FOIA constants that are not in us.json because they are not the
# response clock: appeal decision and expedited-processing decision.
FEDERAL_APPEAL_DECISION_BUSINESS_DAYS = 20
FEDERAL_EXPEDITE_DECISION_CALENDAR_DAYS = 10


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


def business_days_after(
    start: datetime | date, n: int, holidays: set[date] | None = None
) -> datetime:
    """The datetime n business days after start. Weekends and federal holidays do not count."""
    if holidays is None:
        holidays = federal_holidays(start.year) | federal_holidays(start.year + 1)
    current = start if isinstance(start, datetime) else datetime(start.year, start.month, start.day)  # noqa: DTZ001
    added = 0
    while added < n:
        current += timedelta(days=1)
        if current.weekday() < 5 and current.date() not in holidays:
            added += 1
    return current


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


def appeal_deadline(jurisdiction: dict, denied_at: datetime) -> datetime | None:
    """Last day to appeal a denial, or None when the jurisdiction's rule is unknown."""
    a = jurisdiction["appeal"]
    return add_days(denied_at, a["deadline_days"], a["deadline_unit"])


def rule_text(jurisdiction: dict) -> str:
    """One line a status screen can print next to a deadline."""
    r = jurisdiction["response"]
    if r["unit"] in (None, "none") or r["days"] is None:
        return r["rule_text"] or "no statutory deadline"
    return r["rule_text"] or f"{r['days']} {r['unit']} days"
