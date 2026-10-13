#!/usr/bin/env python3
"""Draft jurisdiction JSON from saved Reporters Committee Open Government Guide pages.

The guide (https://www.rcfp.org/open-government-guide/) has one page per state
in a fixed outline. This script reads page text you saved yourself, finds the
sections that carry the public-records rules, and writes one draft
``us-<xx>.json`` per state with ``verified: false``. A person then reads each
draft against the page, fixes what the regexes got wrong, and flips
``verified``. Nothing here fetches: ``--fetch`` only prints the curl commands.

Usage:
    python scripts/rcfp_jurisdictions.py --fetch                 # print curl commands
    python scripts/rcfp_jurisdictions.py raw/ drafts/            # raw/<xx>.txt -> drafts/us-<xx>.json

Where the values live on a page (found by hand in the 2026-10-14 pass):
    the Open Records body runs from the second line reading exactly "Open
    Records" to the last line reading "Open Meetings"; I.A statute, I.A.1 who
    may request, I.B which bodies, I.D fees, II.A exemptions, IV.A.4 format,
    IV.B how long to wait, IV.B.3 delay as denial, IV.C administrative appeal,
    IV.E court action, IV.E.6 time limit for suit. Many sub-headings are empty
    and say "(This section is blank. See the point above.)"; the rule is then
    in the parent section's prose.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STATES = {
    "al": "Alabama",
    "ak": "Alaska",
    "az": "Arizona",
    "ar": "Arkansas",
    "ca": "California",
    "co": "Colorado",
    "ct": "Connecticut",
    "de": "Delaware",
    "dc": "District of Columbia",
    "fl": "Florida",
    "ga": "Georgia",
    "hi": "Hawaii",
    "id": "Idaho",
    "il": "Illinois",
    "in": "Indiana",
    "ia": "Iowa",
    "ks": "Kansas",
    "ky": "Kentucky",
    "la": "Louisiana",
    "me": "Maine",
    "md": "Maryland",
    "ma": "Massachusetts",
    "mi": "Michigan",
    "mn": "Minnesota",
    "ms": "Mississippi",
    "mo": "Missouri",
    "mt": "Montana",
    "ne": "Nebraska",
    "nv": "Nevada",
    "nh": "New Hampshire",
    "nj": "New Jersey",
    "nm": "New Mexico",
    "ny": "New York",
    "nc": "North Carolina",
    "nd": "North Dakota",
    "oh": "Ohio",
    "ok": "Oklahoma",
    "or": "Oregon",
    "pa": "Pennsylvania",
    "ri": "Rhode Island",
    "sc": "South Carolina",
    "sd": "South Dakota",
    "tn": "Tennessee",
    "tx": "Texas",
    "ut": "Utah",
    "vt": "Vermont",
    "va": "Virginia",
    "wa": "Washington",
    "wv": "West Virginia",
    "wi": "Wisconsin",
    "wy": "Wyoming",
}

# (field path, start heading regex, end heading regex)
SECTIONS = [
    ("statute", r"A\. Statute", r"B\. Whose records|1\. Status of requester"),
    ("residency", r"1\. Status of requester", r"2\. Purpose"),
    ("covers", r"B\. Whose records", r"C\. What records"),
    ("fees", r"D\. Fee provisions", r"E\. Who enforces"),
    ("exemptions", r"A\. Exemptions in the open", r"B\. Other statutory"),
    ("format", r"4\. Can the requester choose", r"5\. Availability"),
    ("response", r"B\. How long to wait", r"C\. Administrative appeal"),
    ("delay", r"3\. Delay in response", r"4\. |C\. Administrative appeal"),
    ("appeal", r"C\. Administrative appeal", r"E\. Court action"),
    ("suit", r"6\. Time limit for filing suit", r"7\. What court"),
]

DAY_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "ten": 10,
    "fourteen": 14,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "sixty": 60,
}
DAYS_RE = re.compile(
    r"\b(\d{1,3}|"
    + "|".join(DAY_WORDS)
    + r")\s*(?:\(\d+\)\s*)?(business|working|calendar)?\s*days?\b",
    re.I,
)
CITE_RE = re.compile(
    r"([A-Z][A-Za-z.]*\.?\s?(?:Code|Stat\.|Rev\.|Gen\.|Comp\.|Ann\.)[^;.]{0,60}?§+\s?[\d.\-A-Za-z()]+)"
)


def open_records_body(text: str) -> list[str]:
    """Lines of the Open Records half of the page, headings kept, filler dropped."""
    lines = [ln.strip() for ln in text.splitlines()]
    starts = [i for i, ln in enumerate(lines) if ln == "Open Records"]
    ends = [i for i, ln in enumerate(lines) if ln == "Open Meetings"]
    if not starts:
        return []
    s = starts[1] if len(starts) > 1 else starts[0]
    later = [i for i in ends if i > s]
    e = later[-1] if later else len(lines)
    return [
        ln
        for ln in lines[s:e]
        if ln and ln != "Compare" and not ln.startswith("(This section is blank")
    ]


def section(lines: list[str], start: str, end: str) -> str | None:
    idx = [i for i, ln in enumerate(lines) if re.match(start, ln)]
    if not idx:
        return None
    i = idx[0]
    stop = [k for k, ln in enumerate(lines) if k > i and re.match(end, ln)]
    j = stop[0] if stop else min(i + 60, len(lines))
    body = re.sub(r"\s+", " ", " ".join(lines[i + 1 : j])).strip()
    return body or None


def sentences(text: str | None, pattern: str, limit: int = 3) -> list[str]:
    if not text:
        return []
    out = [s for s in re.split(r"(?<=[.;])\s+", text) if re.search(pattern, s, re.I)]
    return out[:limit]


def day_count(text: str | None) -> tuple[int | None, str | None]:
    """A day count only when the section names exactly one, else None."""
    found = set()
    unit = None
    for m in DAYS_RE.finditer(text or ""):
        n = m.group(1).lower()
        found.add(int(n) if n.isdigit() else DAY_WORDS[n])
        if m.group(2):
            unit = "business" if m.group(2).lower() in ("business", "working") else "calendar"
    if len(found) != 1:
        return None, None
    return found.pop(), unit


def draft(code: str, text: str, source: str) -> dict:
    lines = open_records_body(text)
    sec = {name: section(lines, a, b) for name, a, b in SECTIONS}
    days, unit = day_count(sec["response"])
    cite = CITE_RE.search(sec["statute"] or "") or CITE_RE.search(" ".join(lines[:40]))
    covers = sec["covers"] or ""
    return {
        "id": f"us-{code}",
        "name": STATES.get(code, code.upper()),
        "level": "state",
        "statute": {"name": None, "citation": cite.group(1).strip() if cite else None, "url": None},
        "response": {
            "days": days,
            "unit": unit,
            "rule_text": " ".join(sentences(sec["response"], r"days?|promptly|reasonable", 2))
            or None,
            "extension_days": None,
            "extension_unit": None,
            "silence_is_denial": None,
        },
        "fees": {
            "search_allowed": None,
            "labor_rule": " ".join(sentences(sec["fees"], r"search|labor|staff|hour", 2)) or None,
            "copy_rule": " ".join(sentences(sec["fees"], r"per page|cents|copy", 2)) or None,
            "waiver_rule": " ".join(sentences(sec["fees"], r"waive|reduc|public interest", 2))
            or None,
            "estimate_required": None,
            "late_penalty": None,
        },
        "residency": {"restricted": None, "rule_text": sec["residency"]},
        "appeal": {
            "body": None,
            "type": None,
            "deadline_days": None,
            "deadline_unit": None,
            "rule_text": " ".join(filter(None, [sec["appeal"], sec["suit"]])) or None,
        },
        "exemptions_citation": None,
        "format_rule": sec["format"],
        "covers": {
            "governor": None,
            "legislature": None,
            "universities_public": None,
            "school_districts": None,
        },
        "sources": [source],
        "verified": False,
        "verified_at": None,
        "notes": (
            "DRAFT from scripts/rcfp_jurisdictions.py: every value must be read against the "
            "source before verified is set. Coverage text: "
            + (covers[:300] if covers else "not found")
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("raw_dir", nargs="?", help="directory of <xx>.txt page text files")
    ap.add_argument("out_dir", nargs="?", help="where to write us-<xx>.json drafts")
    ap.add_argument("--fetch", action="store_true", help="print curl commands and exit")
    args = ap.parse_args(argv)
    if args.fetch:
        for code, name in STATES.items():
            slug = name.lower().replace(" ", "-")
            print(
                f"curl -sL https://www.rcfp.org/open-government-guide/{slug}/ -o raw/{code}.html && sleep 3"
            )
        print(
            "# then strip the HTML to text (for example: python -c 'import html,re,sys; ...') into raw/<xx>.txt"
        )
        return 0
    if not args.raw_dir or not args.out_dir:
        ap.error("raw_dir and out_dir are required unless --fetch")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in sorted(Path(args.raw_dir).glob("*.txt")):
        code = path.stem.lower()
        if code not in STATES:
            print(f"skip {path.name}: not a state code", file=sys.stderr)
            continue
        slug = STATES[code].lower().replace(" ", "-")
        d = draft(
            code,
            path.read_text(errors="ignore"),
            f"https://www.rcfp.org/open-government-guide/{slug}/",
        )
        (out / f"us-{code}.json").write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
        n += 1
    print(f"wrote {n} draft(s) to {out}; all verified=false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
