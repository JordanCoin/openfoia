"""Regression tests for the naive-UTC datetime convention.

`datetime.utcnow()` is deprecated (3.12+) and this project's CI targets 3.13,
so it has to go. But the ORM columns are `DateTime` **without** timezone, i.e.
naive UTC, and model helpers compare stored values against "now". Swapping in
an aware `datetime.now(timezone.utc)` would make those comparisons raise
`TypeError: can't subtract offset-naive and offset-aware datetimes`.

These tests pin the convention: keep storing naive UTC, but stop calling the
deprecated API. Changing to fully timezone-aware storage is a separate,
deliberate migration.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta


def test_utcnow_helper_returns_naive_datetime():
    """Storage stays naive so it stays comparable with existing rows."""
    from openfoia.models import utcnow

    now = utcnow()

    assert now.tzinfo is None, "helper returned an aware datetime; ORM columns are naive"


def test_utcnow_helper_is_actually_utc():
    from openfoia.models import utcnow

    delta = abs((utcnow() - datetime.now(UTC).replace(tzinfo=None)).total_seconds())

    assert delta < 5, "helper is not UTC-based"


def test_utcnow_helper_emits_no_deprecation_warning():
    """The whole point: no deprecated utcnow() under the hood."""
    from openfoia.models import utcnow

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        utcnow()


def test_days_pending_still_works_against_naive_storage():
    """This is what a blind aware-datetime conversion would break."""
    from openfoia.models import Request, utcnow

    req = Request()
    req.sent_at = utcnow() - timedelta(days=3)

    assert req.days_pending() == 3


def test_is_overdue_still_works_against_naive_storage():
    from openfoia.models import Request, utcnow

    req = Request()
    req.due_date = utcnow() - timedelta(days=1)
    assert req.is_overdue() is True

    req.due_date = utcnow() + timedelta(days=1)
    assert req.is_overdue() is False


def test_no_local_time_written_into_utc_columns():
    """`sent_at = datetime.now()` wrote LOCAL time into a UTC column.

    Every reader (`days_pending`, `is_overdue`) compares those values against
    UTC, so for a user in UTC+9 or UTC-8 the statutory FOIA deadline was off
    by up to a day. Timestamps destined for storage must use the helper.
    """
    import re
    from pathlib import Path

    import openfoia

    pkg = Path(openfoia.__file__).parent
    offenders = []
    # Assignments of a bare datetime.now() to a persisted timestamp column.
    pattern = re.compile(
        r"\.(sent_at|created_at|received_at|processed_at|acknowledged_at|"
        r"completed_at|occurred_at|due_date|ends_at)\s*=\s*datetime\.now\(\s*\)"
    )
    for path in pkg.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(pkg)}:{lineno}")

    assert offenders == [], f"local time written into a naive-UTC column: {offenders}"


def test_no_deprecated_utcnow_calls_remain_in_package():
    """`datetime.utcnow()` is deprecated and removed-in-future."""
    import re
    from pathlib import Path

    import openfoia

    pkg = Path(openfoia.__file__).parent
    offenders = []
    for path in pkg.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            # Attribute-style calls only: `datetime.utcnow()` and aliased
            # forms like `dt.utcnow()`. A bare `utcnow()` is our own helper.
            if re.search(r"\w\.utcnow\s*\(", line):
                offenders.append(f"{path.relative_to(pkg)}:{lineno}")

    assert offenders == [], f"deprecated datetime.utcnow() still called: {offenders}"
