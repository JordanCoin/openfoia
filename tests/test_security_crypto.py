"""Security regression tests: SQLCipher key handling.

The passphrase was interpolated into `PRAGMA key='{password}'` with an
f-string. A passphrase containing an apostrophe silently truncated the
effective key (everything after `'--` became a SQL comment), so a long
passphrase could be reduced to a couple of characters -- while still
appearing to work, because create and unlock truncated identically.
"""

from __future__ import annotations

import sqlite3

import pytest

NASTY_PASSPHRASES = [
    "it's a secret",
    "abc'--rest-of-my-very-long-passphrase",
    "quote'quote'quote",
    "trailing'",
    "'leading",
    "back\\slash",
    'double"quote',
    "semi;colon",
    "unicode-éè-passphrase",
    "normal-passphrase-no-quotes",
]


def _key_literal(password: str) -> str:
    from openfoia.db import sqlcipher_key_literal

    return sqlcipher_key_literal(password)


@pytest.mark.parametrize("password", NASTY_PASSPHRASES)
def test_key_literal_round_trips_through_sqlite_parser(password):
    """The escaped literal must decode back to the EXACT passphrase.

    This is the real regression: we hand the literal to SQLite's own parser
    and require the full passphrase back. Truncation shows up immediately.
    """
    literal = _key_literal(password)

    conn = sqlite3.connect(":memory:")
    try:
        (value,) = conn.execute(f"SELECT {literal}").fetchone()
    finally:
        conn.close()

    assert value == password


def test_key_literal_doubles_single_quotes():
    assert _key_literal("it's") == "'it''s'"


def test_key_literal_is_not_truncated_by_comment_injection():
    """`abc'--tail` must NOT reduce to `abc`."""
    password = "abc'--tail"
    literal = _key_literal(password)

    conn = sqlite3.connect(":memory:")
    try:
        (value,) = conn.execute(f"SELECT {literal}").fetchone()
    finally:
        conn.close()

    assert value != "abc"
    assert value == password


@pytest.mark.parametrize("password", NASTY_PASSPHRASES)
def test_key_pragma_statement_is_single_statement(password):
    """The built PRAGMA must not be splittable into extra statements."""
    from openfoia.db import sqlcipher_key_pragma

    stmt = sqlcipher_key_pragma(password)

    assert stmt.startswith("PRAGMA key = '")
    assert stmt.endswith("'")
    # sqlite3.complete_statement only returns True for one finished statement.
    assert sqlite3.complete_statement(stmt + ";")


def test_no_passphrase_interpolated_inside_a_quoted_pragma_literal():
    """No `PRAGMA key='{password}'` may remain anywhere in the package.

    The dangerous shape is interpolation *inside* the quotes, which is what
    lets an apostrophe close the literal early. The helper's own
    `PRAGMA key = {literal}` (quotes supplied by the escaper) is fine.
    """
    import re
    from pathlib import Path

    import openfoia

    pkg = Path(openfoia.__file__).parent
    offenders = []
    for path in pkg.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"""PRAGMA\s+key\s*=?\s*['"]\{""", line):
                offenders.append(f"{path.relative_to(pkg)}:{lineno}")

    assert offenders == [], f"f-string PRAGMA key interpolation remains: {offenders}"
