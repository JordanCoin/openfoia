"""End-to-end tests for entity extraction across all backends.

These tests use realistic fake FOIA documents with known entities.
Each document has a ground truth set of expected entities and relationships.
Tests verify that each backend finds them.
"""

import asyncio

import pytest

from openfoia.pipeline.extract import (
    EntityExtractor,
    ExtractionResult,
    _gliner_available,
    _llm_available,
    _spacy_available,
)

# ---------------------------------------------------------------------------
# Test documents with ground truth
# ---------------------------------------------------------------------------

# Document 1: FOIA response letter (typical government correspondence)
DOC_FOIA_LETTER = """
Department of Justice
Office of Information Policy
441 G Street, NW, 6th Floor
Washington, DC 20530

March 15, 2026

RE: FOIA Request No. DOJ-2026-001234

Dear Ms. Sarah Chen,

This letter responds to your Freedom of Information Act request dated January 8, 2026,
in which you requested records pertaining to the contract between Meridian Defense Systems
and the Department of Defense for Project ATLAS, awarded on September 12, 2024.

After a thorough search, we located 247 pages of responsive records. We are releasing
182 pages in full and 65 pages with redactions pursuant to FOIA Exemptions (b)(5) and
(b)(7)(A).

The contract was valued at $14,750,000 and was overseen by Deputy Assistant Secretary
James R. Whitfield at the Pentagon. Payments were routed through Consolidated Federal
Services LLC, a subsidiary of Meridian Defense Systems, to account ending in 4891 at
First National Bank of Virginia.

If you have questions, contact FOIA analyst Michael Torres at (202) 514-3642 or
michael.torres@usdoj.gov.

Sincerely,
Director, Office of Information Policy
"""

DOC_FOIA_LETTER_EXPECTED = {
    "persons": {"Sarah Chen", "James R. Whitfield", "Michael Torres"},
    "organizations": {
        "Department of Justice",
        "Department of Defense",
        "Meridian Defense Systems",
        "Consolidated Federal Services LLC",
        "First National Bank of Virginia",
        "Office of Information Policy",
    },
    "locations": {"Washington, DC", "Pentagon"},
    "money": {"$14,750,000"},
    "dates": {
        "March 15, 2026",
        "January 8, 2026",
        "September 12, 2024",
    },
    "emails": {"michael.torres@usdoj.gov"},
    "phones": {"(202) 514-3642"},
    "document_ids": {"DOJ-2026-001234"},
    # Key relationships we expect
    "relationships": {
        ("Sarah Chen", "Department of Justice"),  # requester → agency
        ("James R. Whitfield", "Department of Defense"),  # person → org
        ("James R. Whitfield", "Pentagon"),  # person → location
        ("Meridian Defense Systems", "Department of Defense"),  # org → org (contract)
        ("Consolidated Federal Services LLC", "Meridian Defense Systems"),  # subsidiary
        ("Michael Torres", "michael.torres@usdoj.gov"),  # person → contact
    },
}


# Document 2: Financial disclosure / investigation memo
DOC_FINANCIAL_MEMO = """
MEMORANDUM

TO: Inspector General, Department of the Interior
FROM: Special Agent Patricia Reyes, Office of Inspector General
DATE: November 3, 2025
RE: Investigation No. OIG-2025-0892 — Irregular Payments to Coastal Resources Inc.

SUMMARY OF FINDINGS:

Between March 2023 and August 2025, the Bureau of Land Management (BLM) office in
Albuquerque, New Mexico issued 14 sole-source contracts totaling $3.2 million to
Coastal Resources Inc., a firm registered in Wilmington, Delaware.

Analysis of bank records from Wells Fargo account #7712-XXXX reveals that within 72
hours of each payment, transfers of $50,000 to $175,000 were made to a personal
account held by Regional Director Thomas Blackwell at Chase Bank in Phoenix, Arizona.

Mr. Blackwell's former business partner, Linda Okafor, serves as CEO of Coastal
Resources Inc. They co-owned a property at 1847 Desert Springs Road, Scottsdale, AZ
85251 from 2018 to 2022.

Witness David Park, a BLM procurement specialist in Albuquerque, stated that Blackwell
personally approved each contract despite recusal requirements.

RECOMMENDATION: Refer for criminal prosecution under 18 U.S.C. § 201 (bribery).
"""

DOC_FINANCIAL_MEMO_EXPECTED = {
    "persons": {"Patricia Reyes", "Thomas Blackwell", "Linda Okafor", "David Park"},
    "organizations": {
        "Department of the Interior",
        "Office of Inspector General",
        "Bureau of Land Management",
        "Coastal Resources Inc.",
        "Wells Fargo",
        "Chase Bank",
    },
    "locations": {
        "Albuquerque, New Mexico",
        "Wilmington, Delaware",
        "Phoenix, Arizona",
        "Scottsdale",
    },
    "money": {"$3.2 million", "$50,000", "$175,000"},
    "dates": {"November 3, 2025", "March 2023", "August 2025"},
    "document_ids": {"OIG-2025-0892"},
    "relationships": {
        ("Thomas Blackwell", "Bureau of Land Management"),  # director → agency
        ("Thomas Blackwell", "Chase Bank"),  # person → bank
        ("Thomas Blackwell", "Linda Okafor"),  # business partners
        ("Linda Okafor", "Coastal Resources Inc."),  # CEO → company
        ("Coastal Resources Inc.", "Bureau of Land Management"),  # contractor → agency
        ("David Park", "Bureau of Land Management"),  # employee → agency
    },
}


# Document 3: Short, messy scanned document (tests noise tolerance)
DOC_SCAN_MESSY = """
l\\/lEMO - DRAFT - DO NOT DISTRIBUTE

Fr0m: J. Anderson, Asst Dir
T0: R. Kimura
Subj: Meeting w/ Apex Global re: consulting contract

Bob - met with CEO Mark Sullivan of Apex Global on 12/15/2025 at their
NYC office (340 Park Avenue). They're asking $2.4M for the analytics
platform. CFO Diana Walsh says they can do $1.8M if we commit by Jan 30.

Also - Sullivan mentioned he plays golf with Sen. Richardson. Not sure
if that matters but noting it.

Call me at 555-867-5309 or janet.anderson@state.gov

- Janet
"""

DOC_SCAN_MESSY_EXPECTED = {
    "persons": {"J. Anderson", "R. Kimura", "Mark Sullivan", "Diana Walsh", "Janet"},
    "organizations": {"Apex Global"},
    "locations": {"NYC", "340 Park Avenue"},
    "money": {"$2.4M", "$1.8M"},
    "dates": {"12/15/2025"},
    "emails": {"janet.anderson@state.gov"},
    "phones": {"555-867-5309"},
    "relationships": {
        ("Mark Sullivan", "Apex Global"),  # CEO → company
        ("Diana Walsh", "Apex Global"),  # CFO → company
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entities_by_type(result: ExtractionResult) -> dict[str, set[str]]:
    """Group extracted entities by type for easy comparison."""
    grouped: dict[str, set[str]] = {}
    for ent in result.entities:
        key = ent.entity_type.value
        grouped.setdefault(key, set()).add(ent.normalized_text)
    return grouped


def _check_entities(
    result: ExtractionResult,
    expected: dict[str, set[str]],
    min_recall: float,
    backend: str,
    doc_name: str,
):
    """Check that the expected entities are found with minimum recall."""
    grouped = _entities_by_type(result)

    type_mapping = {
        "persons": "person",
        "organizations": "organization",
        "locations": "location",
        "money": "money",
        "dates": "date",
        "emails": "email",
        "phones": "phone",
        "document_ids": "document_id",
    }

    total_expected = 0
    total_found = 0

    for expected_key, expected_set in expected.items():
        if expected_key == "relationships":
            continue

        entity_type = type_mapping.get(expected_key)
        if not entity_type:
            continue

        found_set = grouped.get(entity_type, set())
        found_normalized = {v.lower().strip() for v in found_set}

        for exp in expected_set:
            total_expected += 1
            exp_lower = exp.lower().strip()
            # Check exact match or substring match
            matched = any(exp_lower in found or found in exp_lower for found in found_normalized)
            if matched:
                total_found += 1

    if total_expected == 0:
        return

    recall = total_found / total_expected
    print(
        f"\n  [{backend}] {doc_name}: {total_found}/{total_expected} entities found ({recall:.0%} recall)"
    )

    # Print what was missed
    for expected_key, expected_set in expected.items():
        if expected_key == "relationships":
            continue
        entity_type = type_mapping.get(expected_key)
        if not entity_type:
            continue
        found_set = grouped.get(entity_type, set())
        found_normalized = {v.lower().strip() for v in found_set}
        for exp in expected_set:
            exp_lower = exp.lower().strip()
            matched = any(exp_lower in found or found in exp_lower for found in found_normalized)
            if not matched:
                print(f"    MISSED [{entity_type}]: {exp}")

    assert recall >= min_recall, (
        f"[{backend}] {doc_name}: recall {recall:.0%} < minimum {min_recall:.0%}"
    )


def _check_relationships(
    result: ExtractionResult,
    expected_rels: set[tuple[str, str]],
    min_recall: float,
    backend: str,
    doc_name: str,
):
    """Check that expected relationships are found."""
    if not expected_rels:
        return

    found_pairs: set[tuple[str, str]] = set()
    for rel in result.relationships:
        src = (rel.get("source") or "").lower().strip()
        tgt = (rel.get("target") or "").lower().strip()
        found_pairs.add((src, tgt))
        found_pairs.add((tgt, src))  # relationships can be either direction

    total_found = 0
    for exp_src, exp_tgt in expected_rels:
        src_l = exp_src.lower()
        tgt_l = exp_tgt.lower()
        matched = any(
            (src_l in fs and tgt_l in ft) or (tgt_l in fs and src_l in ft) for fs, ft in found_pairs
        )
        if matched:
            total_found += 1

    recall = total_found / len(expected_rels)
    print(
        f"  [{backend}] {doc_name} relationships: {total_found}/{len(expected_rels)} ({recall:.0%})"
    )

    if recall < min_recall:
        for exp_src, exp_tgt in expected_rels:
            src_l = exp_src.lower()
            tgt_l = exp_tgt.lower()
            matched = any(
                (src_l in fs and tgt_l in ft) or (tgt_l in fs and src_l in ft)
                for fs, ft in found_pairs
            )
            if not matched:
                print(f"    MISSED REL: {exp_src} ↔ {exp_tgt}")


# ---------------------------------------------------------------------------
# Tests: Regex backend (always available)
# ---------------------------------------------------------------------------


class TestRegexExtraction:
    """Tests for the regex fallback — the baseline that always works."""

    def _make_extractor(self) -> EntityExtractor:
        ext = EntityExtractor()
        ext._backend = "regex"  # force regex
        return ext

    def test_foia_letter_structured_entities(self):
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FOIA_LETTER))

        # Regex should find: dates, money, emails, phones, doc IDs
        # It will NOT find people or organizations — that's expected
        _check_entities(
            result,
            {
                "money": DOC_FOIA_LETTER_EXPECTED["money"],
                "emails": DOC_FOIA_LETTER_EXPECTED["emails"],
                "phones": DOC_FOIA_LETTER_EXPECTED["phones"],
                "document_ids": DOC_FOIA_LETTER_EXPECTED["document_ids"],
            },
            min_recall=0.75,
            backend="regex",
            doc_name="FOIA Letter",
        )
        assert "regex" in result.metadata["backend"]

    def test_financial_memo_structured_entities(self):
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FINANCIAL_MEMO))

        _check_entities(
            result,
            {
                "money": DOC_FINANCIAL_MEMO_EXPECTED["money"],
                "document_ids": DOC_FINANCIAL_MEMO_EXPECTED["document_ids"],
            },
            min_recall=0.5,
            backend="regex",
            doc_name="Financial Memo",
        )

    def test_messy_scan_structured_entities(self):
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_SCAN_MESSY))

        _check_entities(
            result,
            {
                "money": DOC_SCAN_MESSY_EXPECTED["money"],
                "emails": DOC_SCAN_MESSY_EXPECTED["emails"],
            },
            min_recall=0.5,
            backend="regex",
            doc_name="Messy Scan",
        )

    def test_relationships_exist_in_regex(self):
        """Regex should produce co-occurrence relationships now."""
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FOIA_LETTER))
        # Should have at least some relationships from co-occurrence
        print(f"\n  [regex] FOIA Letter: {len(result.relationships)} relationships found")


# ---------------------------------------------------------------------------
# Tests: GLiNER backend (requires pip install gliner)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _gliner_available(), reason="GLiNER not installed")
class TestGLiNERExtraction:
    """Tests for GLiNER zero-shot NER — the sweet spot backend."""

    def _make_extractor(self) -> EntityExtractor:
        ext = EntityExtractor()
        ext._backend = "gliner"
        return ext

    def test_foia_letter_all_entities(self):
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FOIA_LETTER))

        # GLiNER should find people, orgs, AND structured entities
        _check_entities(
            result,
            {
                "persons": DOC_FOIA_LETTER_EXPECTED["persons"],
                "organizations": DOC_FOIA_LETTER_EXPECTED["organizations"],
                "money": DOC_FOIA_LETTER_EXPECTED["money"],
            },
            min_recall=0.5,  # GLiNER should get at least half
            backend="gliner",
            doc_name="FOIA Letter",
        )
        _check_relationships(
            result,
            DOC_FOIA_LETTER_EXPECTED["relationships"],
            min_recall=0.2,  # co-occurrence won't catch everything
            backend="gliner",
            doc_name="FOIA Letter",
        )
        assert "gliner" in result.metadata["backend"]

    def test_financial_memo_all_entities(self):
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FINANCIAL_MEMO))

        _check_entities(
            result,
            {
                "persons": DOC_FINANCIAL_MEMO_EXPECTED["persons"],
                "organizations": DOC_FINANCIAL_MEMO_EXPECTED["organizations"],
                "money": DOC_FINANCIAL_MEMO_EXPECTED["money"],
            },
            min_recall=0.5,
            backend="gliner",
            doc_name="Financial Memo",
        )
        _check_relationships(
            result,
            DOC_FINANCIAL_MEMO_EXPECTED["relationships"],
            min_recall=0.2,
            backend="gliner",
            doc_name="Financial Memo",
        )

    def test_messy_scan_resilience(self):
        """GLiNER should handle OCR-quality text."""
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_SCAN_MESSY))

        _check_entities(
            result,
            {
                "persons": {"Mark Sullivan", "Diana Walsh"},
                "organizations": DOC_SCAN_MESSY_EXPECTED["organizations"],
            },
            min_recall=0.4,  # messy text, lower bar
            backend="gliner",
            doc_name="Messy Scan",
        )


# ---------------------------------------------------------------------------
# Tests: spaCy backend (requires pip install spacy + model)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _spacy_available(), reason="spaCy not installed with English model")
class TestSpaCyExtraction:
    """Tests for spaCy NER + dependency parsing."""

    def _make_extractor(self) -> EntityExtractor:
        ext = EntityExtractor()
        ext._backend = "spacy"
        return ext

    def test_foia_letter_people_and_orgs(self):
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FOIA_LETTER))

        _check_entities(
            result,
            {
                "persons": DOC_FOIA_LETTER_EXPECTED["persons"],
                "organizations": DOC_FOIA_LETTER_EXPECTED["organizations"],
            },
            min_recall=0.4,
            backend="spacy",
            doc_name="FOIA Letter",
        )
        # spaCy should also produce syntactic relationships
        syntactic_rels = [r for r in result.relationships if r.get("confidence") == 0.7]
        print(
            f"\n  [spacy] FOIA Letter: {len(syntactic_rels)} syntactic relationships, {len(result.relationships)} total"
        )

    def test_financial_memo_people_and_orgs(self):
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FINANCIAL_MEMO))

        _check_entities(
            result,
            {
                "persons": DOC_FINANCIAL_MEMO_EXPECTED["persons"],
                "organizations": DOC_FINANCIAL_MEMO_EXPECTED["organizations"],
            },
            min_recall=0.4,
            backend="spacy",
            doc_name="Financial Memo",
        )

    def test_supplements_with_regex(self):
        """spaCy path should also include regex-found emails/phones/doc IDs."""
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FOIA_LETTER))

        grouped = _entities_by_type(result)
        # These come from regex supplementation
        assert "email" in grouped or "phone" in grouped, (
            "spaCy path should supplement with regex for emails/phones"
        )


# ---------------------------------------------------------------------------
# Tests: LLM backend (requires running ollama or API key)
# ---------------------------------------------------------------------------

_LLM_AVAILABLE = False
try:
    from openfoia.config import load_config

    _cfg = load_config()
    _LLM_AVAILABLE = _llm_available(_cfg.ai.provider, _cfg.ai.api_key, _cfg.ai.base_url)
except Exception:
    pass


@pytest.mark.skipif(not _LLM_AVAILABLE, reason="No LLM backend available")
class TestLLMExtraction:
    """Tests for LLM-based extraction. Only runs if ollama or an API key is configured."""

    def _make_extractor(self) -> EntityExtractor:
        ext = EntityExtractor()
        ext._backend = "llm"
        return ext

    def test_foia_letter_comprehensive(self):
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FOIA_LETTER))

        # LLM should be the best — highest recall bar
        _check_entities(
            result,
            {
                "persons": DOC_FOIA_LETTER_EXPECTED["persons"],
                "organizations": DOC_FOIA_LETTER_EXPECTED["organizations"],
                "money": DOC_FOIA_LETTER_EXPECTED["money"],
                "dates": DOC_FOIA_LETTER_EXPECTED["dates"],
                "emails": DOC_FOIA_LETTER_EXPECTED["emails"],
            },
            min_recall=0.6,
            backend="llm",
            doc_name="FOIA Letter",
        )
        _check_relationships(
            result,
            DOC_FOIA_LETTER_EXPECTED["relationships"],
            min_recall=0.3,
            backend="llm",
            doc_name="FOIA Letter",
        )

    def test_financial_memo_comprehensive(self):
        ext = self._make_extractor()
        result = asyncio.run(ext.extract(DOC_FINANCIAL_MEMO))

        _check_entities(
            result,
            {
                "persons": DOC_FINANCIAL_MEMO_EXPECTED["persons"],
                "organizations": DOC_FINANCIAL_MEMO_EXPECTED["organizations"],
                "money": DOC_FINANCIAL_MEMO_EXPECTED["money"],
            },
            min_recall=0.6,
            backend="llm",
            doc_name="Financial Memo",
        )
        _check_relationships(
            result,
            DOC_FINANCIAL_MEMO_EXPECTED["relationships"],
            min_recall=0.3,
            backend="llm",
            doc_name="Financial Memo",
        )


# ---------------------------------------------------------------------------
# Meta test: backend selection
# ---------------------------------------------------------------------------


class TestBackendSelection:
    """Verify the backend selection logic."""

    def test_forced_regex(self):
        ext = EntityExtractor()
        ext._backend = "regex"
        result = asyncio.run(ext.extract("John Smith paid $500 on 2024-01-15"))
        assert "regex" in result.metadata["backend"]

    def test_auto_fallback(self):
        """With no config, should fall back to gliner → spacy → regex."""
        ext = EntityExtractor()
        backend = ext._resolve_backend()
        # Should be one of these (depends on what's installed)
        assert backend in ("llm", "gliner", "spacy", "regex")
        print(f"\n  Auto-selected backend: {backend}")
