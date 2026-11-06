"""Junk-mention scoring (#57) and small-model prompts (#59)."""

import json

import pytest

from openfoia.config import EntityConfig
from openfoia.pipeline.extract import (
    EntityType,
    Mention,
    _build_extraction_prompt,
    _is_compact_model,
    _mention_score,
    _strip_think,
)

ORG, PERSON, LOC = EntityType.ORGANIZATION, EntityType.PERSON, EntityType.LOCATION


def _m(text: str, etype: EntityType, backend: str = "regex", score: float = 0.9) -> Mention:
    return Mention(etype, text, text, -1, -1, None, "", backend, score)


@pytest.mark.parametrize(
    "text,etype",
    [
        ("To:", ORG),
        ("Bill To:", ORG),
        ("APPROVED", ORG),
        ("EC .Orchestrating a or g-?te' 22", ORG),
        (
            "Justification of request: Clcorwcw will assist the department with "
            "identifying suspects through facial recognition.",
            PERSON,
        ),
        ("The officer filed the report. Then he left the building.", PERSON),
        ("Ab", ORG),
        ("x" * 81, ORG),
    ],
)
def test_junk_scores_zero(text, etype):
    for backend in ("regex", "spacy", "gliner", "llm"):
        assert _mention_score(_m(text, etype, backend)) == 0.0, (text, backend)


@pytest.mark.parametrize(
    "text,etype",
    [
        ("U.S. Department of Justice", ORG),
        ("AT&T", ORG),
        ("Meridian Defense Systems Inc.", ORG),
        ("Clearview AI", ORG),
        ("FBI", ORG),
        ("Sarah Chen", PERSON),
        ("Patrick O'Brien", PERSON),
        ("Dr. John A. Smith", PERSON),
        ("Mrs. Jane Smith-Jones", PERSON),
        ("José Müller", PERSON),
        ("Washington, DC", LOC),
    ],
)
def test_legitimate_names_kept(text, etype):
    for backend in ("regex", "spacy", "gliner"):
        assert _mention_score(_m(text, etype, backend)) > 0.3, (text, backend)


def test_long_person_penalized_not_dropped():
    long_name = _mention_score(_m("Sgt. Lakca Gaither For FMU Use Only", PERSON, "spacy"))
    assert 0.0 < long_name < _mention_score(_m("Lakca Gaither", PERSON, "spacy"))


@pytest.mark.parametrize(
    "model,compact",
    [
        ("qwen3:8b", True),
        ("llama3.1:8b", True),
        ("llama3.1:8b-instruct-q4_K_M", True),
        ("mistral:7b", True),
        ("qwen2.5:1.5b", True),
        ("llama3.2:3b", True),
        ("llama3.2", True),
        ("tinyllama", True),
        ("qwen2.5:14b", False),
        ("gemma3:27b", False),
        ("llama3.1:70b", False),
        ("mixtral:8x7b", False),
        ("claude-sonnet-4-5", False),
        ("gpt-4o", False),
        ("", False),
        (None, False),
    ],
)
def test_model_size_classification(model, compact):
    assert _is_compact_model(model) is compact


def test_compact_prompt_drops_relationships():
    cfg = EntityConfig(custom_types=[{"name": "badge", "description": "Badge numbers"}])
    full = _build_extraction_prompt("Some text.", None, 1, cfg)
    compact = _build_extraction_prompt("Some text.", None, 1, cfg, compact=True)
    assert "RELATIONSHIP" in full.upper()
    assert "relationship" not in compact.lower()
    assert "DOCUMENT_ID" not in compact and "BADGE" in compact
    assert len(compact) < len(full) / 2
    assert compact.isascii()


def test_strip_think_yields_json():
    raw = '<think>\nThe doc mentions {Acme}...\n</think>\n{"entities": []}'
    assert json.loads(_strip_think(raw)) == {"entities": []}
    assert _strip_think('{"entities": []}') == '{"entities": []}'
