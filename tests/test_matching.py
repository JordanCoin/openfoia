"""Entity dedup keys: legal forms and honorifics."""

from __future__ import annotations

from types import SimpleNamespace

from openfoia.crossref import _deduplicate_entities
from openfoia.matching import _LEGAL_FORMS, dedup_key
from openfoia.models import EntityType

ORG = EntityType.ORGANIZATION
PERSON = EntityType.PERSON


def _entities(names, entity_type=ORG):
    return [
        SimpleNamespace(normalized_text=n, entity_type=entity_type, confidence=0.5 + i / 100)
        for i, n in enumerate(names)
    ]


def test_company_forms_merge_and_keep_highest_confidence():
    kept = _deduplicate_entities(
        _entities(["Clearview AI Inc.", "Clearview AI, Ltd", "CLEARVIEW AI LIMITED"])
    )
    assert [e.normalized_text for e in kept] == ["CLEARVIEW AI LIMITED"]


def test_dotted_and_bare_forms_merge():
    assert dedup_key("Petrobras S.A.", ORG) == dedup_key("Petrobras SA", ORG) == "petrobras"


def test_abbreviation_is_not_expanded():
    # Forms are stripped, never expanded: "Aktiengesellschaft" is not in the list.
    assert dedup_key("Siemens AG", ORG) != dedup_key("Siemens Aktiengesellschaft", ORG)


def test_person_honorific_stripped():
    assert dedup_key("Dr. Jane Doe", PERSON) == dedup_key("Jane Doe", PERSON)


def test_bare_legal_form_keeps_its_key():
    assert dedup_key("Inc.", ORG) == "inc"


def test_person_and_org_with_same_text_merge():
    # Dedup keys on text only, as before; entity type does not split them.
    kept = _deduplicate_entities(_entities(["Jane Doe"]) + _entities(["Jane Doe"], PERSON))
    assert len(kept) == 1


def test_multi_token_forms_come_first():
    sizes = [len(f.split()) for f in _LEGAL_FORMS]
    assert sizes == sorted(sizes, reverse=True)
