"""Entity dedup keys with and without the optional rigour extra."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from openfoia.crossref import _deduplicate_entities
from openfoia.matching import dedup_key
from openfoia.models import EntityType

ORG = EntityType.ORGANIZATION
CLEARVIEW = ["Clearview AI Inc.", "Clearview AI, Ltd", "CLEARVIEW AI LIMITED"]


def _entities(names, entity_type=ORG):
    return [
        SimpleNamespace(normalized_text=n, entity_type=entity_type, confidence=0.5 + i / 100)
        for i, n in enumerate(names)
    ]


@pytest.fixture
def no_rigour(monkeypatch):
    monkeypatch.setitem(sys.modules, "rigour.names", None)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Clearview AI Inc.", "clearview ai"),
        ("Clearview AI, Ltd", "clearview ai"),
        ("CLEARVIEW AI LIMITED", "clearview ai limited"),
        ("Acme Co.", "acme"),
        ("Foo LLC, Inc", "foo llc"),
        ("  Jane Doe  ", "jane doe"),
        ("Inc.", "inc"),
    ],
)
def test_fallback_key_matches_legacy_suffix_strip(no_rigour, name, key):
    assert dedup_key(name, ORG) == key


def test_fallback_dedup_keeps_highest_confidence(no_rigour):
    kept = _deduplicate_entities(_entities(CLEARVIEW))
    assert [e.normalized_text for e in kept] == ["Clearview AI, Ltd", "CLEARVIEW AI LIMITED"]


def test_rigour_merges_company_forms_across_spellings():
    pytest.importorskip("rigour.names")
    kept = _deduplicate_entities(_entities(CLEARVIEW))
    assert [e.normalized_text for e in kept] == ["CLEARVIEW AI LIMITED"]
    assert dedup_key("Petrobras S.A.", ORG) == dedup_key("Petrobras SA", ORG)
    assert dedup_key("Acme Co.", ORG) == "acme"
    assert dedup_key("Inc.", ORG) == "inc"


def test_rigour_strips_person_honorifics():
    pytest.importorskip("rigour.names")
    assert dedup_key("Dr. Jane Smith", EntityType.PERSON) == dedup_key(
        "jane smith", EntityType.PERSON
    )
