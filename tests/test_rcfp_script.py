"""scripts/rcfp_jurisdictions.py drafts a jurisdiction file from a saved RCFP page."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rcfp_jurisdictions as rcfp

PAGE = """
Open Records
	I. STATUTE
	Open Meetings
	I. STATUTE
Open Records
	I. STATUTE
	A. Statute
	The Example Public Records Act, Ex. Code § 1-2-3, opens records to the public.
	1. Status of requester
	Any person may request records; residency is not required.
	2. Purpose of request
	Compare
	B. Whose records are and are not subject to the Act
	The Act covers state agencies and school districts; the Legislature is exempt.
	C. What records are and are not subject to the act
	D. Fee provisions
	Copies cost 10 cents per page. Search time may not be charged. Fees may be waived in the public interest.
	E. Who enforces the Act?
	IV. PROCEDURE FOR OBTAINING RECORDS
	B. How long to wait
	The custodian must respond within seven business days of receipt.
	1. Statutory, regulatory or court-set time limits for agency response
	(This section is blank. See the point above.)
	C. Administrative appeal
	A denial may be appealed to the head of the agency.
	E. Court action
	6. Time limit for filing suit
	Suit must be filed within one year.
	7. What court
Open Meetings
"""


def test_draft_from_synthetic_page():
    d = rcfp.draft("ex", PAGE, "https://www.rcfp.org/open-government-guide/example/")
    assert d["id"] == "us-ex" and d["verified"] is False and d["verified_at"] is None
    assert d["statute"]["citation"] == "Ex. Code § 1-2-3"
    assert d["response"]["days"] == 7 and d["response"]["unit"] == "business"
    assert "seven business days" in d["response"]["rule_text"]
    assert "residency is not required" in d["residency"]["rule_text"]
    assert "10 cents" in d["fees"]["copy_rule"] and "waived" in d["fees"]["waiver_rule"]
    assert "head of the agency" in d["appeal"]["rule_text"]
    assert "within one year" in d["appeal"]["rule_text"]
    assert "school districts" in d["notes"]
    assert d["appeal"]["type"] is None and d["covers"]["legislature"] is None
    json.dumps(d)


def test_two_day_counts_means_no_number():
    assert rcfp.day_count("respond in 5 days, or 10 days with notice") == (None, None)
    assert rcfp.day_count("within ten business days") == (10, "business")


def test_fetch_prints_commands_and_writes_nothing(tmp_path, capsys):
    assert rcfp.main(["--fetch"]) == 0
    out = capsys.readouterr().out
    assert "curl -sL https://www.rcfp.org/open-government-guide/michigan/" in out
    assert "sleep 3" in out
    assert not list(tmp_path.iterdir())


def test_cli_writes_drafts(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ex.txt").write_text(PAGE)
    (raw / "mi.txt").write_text(PAGE)
    assert rcfp.main([str(raw), str(tmp_path / "out")]) == 0
    files = sorted(p.name for p in (tmp_path / "out").glob("*.json"))
    assert files == ["us-mi.json"]  # "ex" is not a state code and is skipped
    assert json.loads((tmp_path / "out" / "us-mi.json").read_text())["name"] == "Michigan"
