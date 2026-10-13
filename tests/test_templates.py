"""Generated letters must not leak unrendered placeholders."""

from datetime import UTC, datetime

import pytest

from openfoia.templates import (
    RequestDetails,
    RequesterInfo,
    appeal_denial,
    records_about_self,
    standard_request,
)

REQUESTER = RequesterInfo(name="Jane Doe", email="jane@example.com", address="1 Main St")


@pytest.mark.parametrize(
    "letter",
    [
        standard_request(REQUESTER, "FBI", RequestDetails(subject="X", description="X")),
        standard_request(
            REQUESTER, "FBI", RequestDetails(subject="X", description="X"), fee_waiver=False
        ),
        records_about_self(REQUESTER, "FBI", record_type="my file"),
        appeal_denial(
            REQUESTER,
            "FBI",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 2, 1, tzinfo=UTC),
            "T-1",
            ["b(6)"],
            "why",
        ),
    ],
)
def test_signature_renders_requester_name(letter):
    assert "{requester" not in letter
    assert letter.rstrip().endswith("Jane Doe")


# --- jurisdiction-aware letters


from openfoia.jurisdictions import get  # noqa: E402
from openfoia.templates import (  # noqa: E402
    FEE_CAP_PLACEHOLDER,
    STATUTE_PLACEHOLDER,
    school_program_request,
    state_request,
)

DETAILS = RequestDetails(subject="gifted program", description="gifted program files")


def test_federal_letter_is_unchanged_by_us_or_no_jurisdiction():
    plain = standard_request(REQUESTER, "FBI", DETAILS)
    assert standard_request(REQUESTER, "FBI", DETAILS, jurisdiction=get("us")) == plain
    assert standard_request(REQUESTER, "FBI", DETAILS, jurisdiction=None) == plain
    assert "5 U.S.C. § 552" in plain and "20 business days" in plain


def test_michigan_letter_cites_the_act_and_its_clock():
    letter = standard_request(REQUESTER, "DPSCD", DETAILS, jurisdiction=get("us-mi"))
    assert (
        "Pursuant to the Michigan Freedom of Information Act, Mich. Comp. Laws §§ 15.231-15.246"
        in letter
    )
    assert "within 5 business days, with any extension of up to 10 business days" in letter
    assert "exemption under MCL 15.243" in letter
    assert "itemized estimate" in letter and FEE_CAP_PLACEHOLDER in letter
    assert "retention schedule" in letter and "destruction log" in letter
    assert "I reserve all rights of review provided by Mich. Comp. Laws §§ 15.231-15.246" in letter
    assert "appealed to" not in letter
    assert "Please apply any fee waiver or reduction the law provides: First $20 waived" in letter
    assert "because" not in letter.split("Please apply")[1].split("\n")[0]
    assert "5 U.S.C." not in letter and "20 business days" not in letter
    assert letter.rstrip().endswith("Jane Doe")


def test_stub_jurisdiction_leaves_placeholders_and_invents_nothing():
    stub = {**get("us-mi"), "id": "us-xx", "name": "Nowhere"}
    stub["statute"] = {"name": None, "citation": None, "url": None}
    stub["response"] = dict.fromkeys(get("us-mi")["response"])
    stub["fees"] = dict.fromkeys(get("us-mi")["fees"])
    stub["appeal"] = dict.fromkeys(get("us-mi")["appeal"])
    stub["exemptions_citation"] = None
    letter = state_request(REQUESTER, "Nowhere Schools", DETAILS, stub)
    assert f"your state's public records law {STATUTE_PLACEHOLDER}" in letter
    assert "look forward to your response within" not in letter
    assert "appealed to" not in letter
    assert "fee waiver or reduction" not in letter
    assert "I reserve all rights of review provided by your state's public records law." in letter
    assert "itemized estimate" in letter  # estimate_required unknown still asks
    assert "cite the specific exemption relied on" in letter


def test_days_null_omits_the_clock_even_with_rule_text():
    j = {**get("us-mi"), "response": {**get("us-mi")["response"], "days": None}}
    assert "look forward to your response within" not in state_request(REQUESTER, "X", DETAILS, j)


def test_school_program_letter_is_ferpa_aware():
    letter = school_program_request(REQUESTER, "DPSCD", "Gifted and Talented Program", get("us-mi"))
    assert "Records of the program known as Gifted and Talented Program" in letter
    assert "20 U.S.C. 1232g" in letter
    assert "gives no ground to deny this request as a whole" in letter
    assert "Aggregate counts only" in letter
    assert "[START DATE] through [END DATE]" in letter
    for banned in ("student roster", "test scores of", "each student's"):
        assert banned not in letter
    federal = school_program_request(REQUESTER, "Department of Education", "Javits grants")
    assert "5 U.S.C. § 552" in federal and "20 U.S.C. 1232g" in federal


def test_cli_uses_the_agency_jurisdiction_unless_told_otherwise(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from openfoia.cli import app

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
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
        ],
    )
    assert r.exit_code == 0, r.output
    common = ["-s", "Gifted program", "-n", "Jane Doe", "-e", "jane@example.com"]
    r = runner.invoke(
        app, ["template", "generate", "standard", "-a", "Detroit Public Schools", *common]
    )
    assert r.exit_code == 0, r.output
    assert "15.231-15.246" in r.output and "5 U.S.C." not in r.output
    r = runner.invoke(
        app,
        [
            "template",
            "generate",
            "standard",
            "-a",
            "Detroit Public Schools",
            *common,
            "--jurisdiction",
            "us",
        ],
    )
    assert "5 U.S.C." in r.output
    r = runner.invoke(app, ["template", "generate", "standard", "-a", "FBI", *common])
    assert "5 U.S.C." in r.output and "MCL" not in r.output
    r = runner.invoke(
        app, ["template", "generate", "standard", "-a", "FBI", *common, "--jurisdiction", "us-zz"]
    )
    assert r.exit_code == 2
    r = runner.invoke(app, ["template", "show", "school-program", "--jurisdiction", "us-mi"])
    assert r.exit_code == 0 and "[PROGRAM NAME]" in r.output and "MCL 15.243" in r.output
    r = runner.invoke(app, ["template", "list"])
    assert "school-program" in r.output


def test_texas_letter_names_no_appeal_body_and_no_clock():
    letter = state_request(REQUESTER, "Austin ISD", DETAILS, get("us-tx"))
    assert "appealed to" not in letter
    assert "look forward to your response within" not in letter
    assert "I reserve all rights of review provided by" in letter


def test_appeal_window_only_for_agency_head_or_commission_with_known_unit():
    base = get("us-mi")
    j = {
        **base,
        "appeal": {
            **base["appeal"],
            "type": "agency_head",
            "deadline_days": 30,
            "deadline_unit": "calendar",
        },
    }
    assert "including an appeal within 30 calendar days" in state_request(
        REQUESTER, "X", DETAILS, j
    )
    j["appeal"]["deadline_unit"] = None
    assert "including an appeal" not in state_request(REQUESTER, "X", DETAILS, j)
    j["appeal"]["deadline_unit"] = "calendar"
    j["appeal"]["type"] = "court"
    assert "including an appeal" not in state_request(REQUESTER, "X", DETAILS, j)


def test_extension_clause_needs_its_own_unit():
    base = get("us-mi")
    j = {**base, "response": {**base["response"], "extension_unit": None}}
    letter = state_request(REQUESTER, "X", DETAILS, j)
    assert "within 5 business days, as the statute provides" in letter
    assert "extension" not in letter.split("Sincerely")[0].split("look forward")[1]


def test_school_program_asks_for_retention_on_both_paths():
    for j in (get("us-mi"), None):
        letter = school_program_request(REQUESTER, "X", "Program", j)
        assert "retention schedule series" in letter and "destruction log" in letter


def test_cli_refuses_self_under_state_law_and_warns_on_expedited(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from openfoia.cli import app

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    common = [
        "-a",
        "FBI",
        "-s",
        "x",
        "-n",
        "Jane Doe",
        "-e",
        "jane@example.com",
        "--jurisdiction",
        "us-mi",
    ]
    r = runner.invoke(app, ["template", "generate", "self", *common])
    assert r.exit_code == 2 and "federal FOIA and Privacy Act" in r.output
    r = runner.invoke(app, ["template", "generate", "standard", *common, "--expedited"])
    assert r.exit_code == 0 and "Expedited processing is a federal FOIA provision" in r.output
    assert "Expedited" not in r.output.split("Dear Records Custodian")[1]


def test_cli_ambiguous_agency_substring_exits_2(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from openfoia.cli import app

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    for name in ("Detroit Public Schools Community District", "Detroit Public Library"):
        assert (
            runner.invoke(
                app, ["agency", "add", "--name", name, "-j", "us-mi", "-l", "city"]
            ).exit_code
            == 0
        )
    common = ["-s", "x", "-n", "Jane Doe", "-e", "jane@example.com"]
    r = runner.invoke(app, ["template", "generate", "standard", "-a", "Detroit Public", *common])
    assert r.exit_code == 2 and "Detroit Public Library" in r.output
    r = runner.invoke(
        app, ["template", "generate", "standard", "-a", "Detroit Public Library", *common]
    )
    assert r.exit_code == 0 and "15.231-15.246" in r.output


def test_reservation_sentence_never_doubles_the_period():
    letter = standard_request(REQUESTER, "Austin ISD", DETAILS, jurisdiction=get("us-tx"))
    assert "et seq.." not in letter
    assert "I reserve all rights of review provided by Tex. Gov't Code § 552.001 et seq." in letter
