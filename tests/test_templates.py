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
