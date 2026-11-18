"""The ensemble extraction path (#60): every NER backend runs, then a conservative merge.

The backends are stubbed so these run without GLiNER or spaCy installed: what is
under test is the orchestration and the merge, not the models.
"""

from __future__ import annotations

import asyncio

import pytest

from openfoia.config import OpenFOIAConfig
from openfoia.models import EntityType
from openfoia.pipeline import extract as extract_mod
from openfoia.pipeline.extract import EntityExtractor, Mention

TEXT = "Kirkland & Ellis LLP represented Clearview AI. Charlie Beck approved the contract."
ORG, PER = EntityType.ORGANIZATION, EntityType.PERSON


def _mention(entity_type, text, backend, score):
    start = TEXT.find(text) if text in TEXT else -1
    return Mention(
        entity_type,
        text,
        text,
        start,
        start + len(text) if start >= 0 else -1,
        None,
        TEXT,
        backend,
        score,
    )


GLINER = [(ORG, "Clearview AI", 0.9), (PER, "Charlie Beck", 0.8)]
SPACY = [(ORG, "Clearview AI", 0.85), (ORG, "Kirkland & Ellis LLP", 0.85)]


@pytest.fixture
def backends(monkeypatch):
    """Stub both NER backends; return the call log and a way to set availability."""
    calls: list[str] = []
    found = {"gliner": GLINER, "spacy": SPACY}

    def stub(name):
        def run(self, text, page_numbers):
            calls.append(name)
            return [_mention(t, s, name, score) for t, s, score in found[name]]

        return run

    monkeypatch.setattr(EntityExtractor, "_mentions_from_gliner", stub("gliner"))
    monkeypatch.setattr(EntityExtractor, "_mentions_from_spacy", stub("spacy"))
    monkeypatch.setattr(extract_mod, "_llm_available", lambda *a, **k: False)

    def available(gliner: bool = True, spacy: bool = True):
        monkeypatch.setattr(extract_mod, "_gliner_available", lambda: gliner)
        monkeypatch.setattr(extract_mod, "_spacy_available", lambda: spacy)

    available()
    return calls, found, available


def _extract(ensemble: bool):
    result = asyncio.run(EntityExtractor(config=OpenFOIAConfig()).extract(TEXT, ensemble=ensemble))
    by_name = {e.normalized_text: e for e in result.entities}
    return result, by_name


def test_ensemble_runs_every_available_backend(backends):
    calls, _, _ = backends

    result, _ = _extract(ensemble=True)

    assert sorted(calls) == ["gliner", "spacy"]
    assert {"gliner", "spacy"} <= set(result.metadata["backends"])
    assert result.metadata["ensemble"] is True
    assert result.metadata["llm_validated"] is False


def test_default_runs_one_backend_only(backends):
    """Single backend by default keeps peak RAM low on small machines."""
    calls, _, _ = backends

    _extract(ensemble=False)

    assert calls == ["gliner"]


def test_default_falls_back_to_the_next_backend_when_the_first_is_missing(backends):
    calls, _, available = backends
    available(gliner=False)

    _extract(ensemble=False)

    assert calls == ["spacy"]


def test_agreement_merges_into_one_entity_with_higher_confidence(backends):
    _, _, available = backends
    available(spacy=False)
    _, alone = _extract(ensemble=False)
    available()

    _, merged = _extract(ensemble=True)

    clearview = merged["Clearview AI"]
    assert sum(1 for n in merged if n == "Clearview AI") == 1
    assert clearview.metadata["backends"] == ["gliner", "spacy"]
    assert clearview.metadata["occurrence_count"] == 2
    assert clearview.confidence > alone["Clearview AI"].confidence


def test_two_backends_agreeing_outweighs_one_backend_repeating_itself(backends):
    """Cross-validation is the point of the ensemble, not just counting mentions."""
    _, found, available = backends
    found["gliner"] = [(ORG, "Clearview AI", 0.8), (ORG, "Clearview AI", 0.8)]
    available(spacy=False)
    _, repeated = _extract(ensemble=True)

    found["gliner"] = [(ORG, "Clearview AI", 0.8)]
    found["spacy"] = [(ORG, "Clearview AI", 0.8)]
    available()
    _, agreed = _extract(ensemble=True)

    assert repeated["Clearview AI"].metadata["occurrence_count"] == 2
    assert agreed["Clearview AI"].metadata["occurrence_count"] == 2
    assert agreed["Clearview AI"].confidence > repeated["Clearview AI"].confidence


def test_entity_from_one_backend_survives_the_merge(backends):
    _, merged = _extract(ensemble=True)

    assert merged["Charlie Beck"].metadata["backends"] == ["gliner"]
    assert merged["Kirkland & Ellis LLP"].metadata["backends"] == ["spacy"]


def test_merge_keeps_every_raw_spelling_as_evidence(backends):
    """OCR text is evidence: a merge must not silently rewrite the other spelling."""
    _, found, _ = backends
    found["spacy"] = [(ORG, "CLEARVIEW AI", 0.85)]

    _, merged = _extract(ensemble=True)

    clearview = next(e for e in merged.values() if e.normalized_text.lower() == "clearview ai")
    assert {"Clearview AI", "CLEARVIEW AI"} <= set(clearview.metadata["aliases"])


def test_ocr_misspelling_from_another_backend_joins_the_cluster_as_an_alias(backends):
    """OCR reads i as l: "Clearvlew Al" is the same company, and both spellings stay as evidence."""
    _, found, _ = backends
    found["spacy"] = [(ORG, "Clearvlew Al", 0.85)]

    _, merged = _extract(ensemble=True)

    clusters = [e for e in merged.values() if "Clearvlew Al" in e.metadata["aliases"]]
    assert len(clusters) == 1
    assert {"Clearview AI", "Clearvlew Al"} <= set(clusters[0].metadata["aliases"])
    assert clusters[0].metadata["backends"] == ["gliner", "spacy"]


def test_ensemble_without_any_ner_backend_still_returns_regex_results(backends):
    calls, _, available = backends
    available(gliner=False, spacy=False)

    result = asyncio.run(
        EntityExtractor(config=OpenFOIAConfig()).extract(
            "Contact foia@agency.gov before 2026-01-15.", ensemble=True
        )
    )

    assert calls == []
    assert result.metadata["backends"] == ["regex"]
    assert any(e.entity_type == EntityType.EMAIL for e in result.entities)
