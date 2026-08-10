"""Entity extraction from documents.

Four extraction tiers, tried in order:
1. LLM (ollama / anthropic / openai) — highest quality, extracts relationships
2. GLiNER — local zero-shot NER model, custom entity types, runs on CPU, no API key
3. spaCy — fast local NER for people/orgs/locations + dependency parsing for relationships
4. Regex — zero-config fallback for dates, money, emails, phones, addresses

The first available backend is used. All backends produce the same output format.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import EntityConfig, OpenFOIAConfig, load_config
from ..models import ConfidenceLevel, EntityType

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ExtractedEntity:
    """An entity extracted from text."""

    entity_type: EntityType
    raw_text: str
    normalized_text: str
    confidence: float
    context: str
    page_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedRelationship:
    """A relationship between two entities."""

    source: str  # normalized entity text
    target: str
    relation: str  # works_for, located_at, communicated_with, etc.
    confidence: float
    evidence: str  # the sentence or context where this was found


@dataclass
class ExtractionResult:
    """Result of entity extraction."""

    entities: list[ExtractedEntity]
    relationships: list[dict[str, Any]]
    summary: str
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Mention scoring and junk filtering (Phase 2 helpers)
# ---------------------------------------------------------------------------

# Entities that are obviously not named entities — document boilerplate
_HARD_BLOCK = {
    "to",
    "from",
    "cc",
    "bcc",
    "date",
    "subject",
    "subj",
    "re",
    "approved",
    "denied",
    "signature",
    "signed",
    "page",
    "pages",
    "bill to",
    "ship to",
    "sold to",
    "remit to",
    "invoice",
    "invoice no",
    "invoice number",
    "qty",
    "quantity",
    "subtotal",
    "total",
    "amount due",
    "description",
    "n/a",
    "none",
    "unknown",
    "other",
    "misc",
    "various",
}

# Generic roles/titles — penalize, don't hard-drop (might be real in some contexts)
_WEAK_GENERIC = {
    "seller",
    "buyer",
    "contractor",
    "subcontractor",
    "vendor",
    "supplier",
    "requester",
    "recipient",
    "witness",
    "agent",
    "special agent",
    "director",
    "manager",
    "officer",
    "ceo",
    "cfo",
    "counsel",
    "attorney",
    "office",
    "department",
    "bureau",
    "division",
    "committee",
    "board",
    "agency",
}

# Concepts that NER models sometimes tag as entities
_CONCEPTS = {
    "human trafficking",
    "trafficking",
    "narcotics",
    "bribery",
    "corruption",
    "fraud",
    "money laundering",
    "kickbacks",
    "child exploitation",
    "terrorism",
    "espionage",
}

# Short all-caps acronyms that MUST survive filtering
_KEEP_ACRONYMS = {
    "FBI",
    "CIA",
    "DOJ",
    "DHS",
    "DOD",
    "DOS",
    "EPA",
    "IRS",
    "DEA",
    "ICE",
    "CBP",
    "TSA",
    "SEC",
    "FTC",
    "FCC",
    "OIG",
    "NYC",
    "DC",
    "NYPD",
    "LAPD",
    "ATF",
    "NSA",
    "NEC",
    "APD",
    "FOIA",
    "OMB",
    "GAO",
    "NIST",
    "BIS",
    "LBPD",
    "LASD",
}

_ORG_SUFFIXES = {
    "inc",
    "llc",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "lp",
    "llp",
    "pllc",
    "plc",
}

_NER_TYPES = {EntityType.PERSON, EntityType.ORGANIZATION, EntityType.LOCATION}


def _surface_clean(text: str) -> str:
    """Normalize whitespace and strip trailing punctuation."""
    import unicodedata

    t = unicodedata.normalize("NFKC", text or "").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", t).strip(" \t\r\n,;:")


def _is_protected_acronym(raw: str) -> bool:
    """Check if this is a known government/org acronym."""
    s = _surface_clean(raw)
    return s in _KEEP_ACRONYMS or (s.isupper() and 2 <= len(s) <= 6 and s.isalpha())


def _mention_score(m: Mention) -> float:
    """Score a mention for quality. 0.0 = definitely junk, should be dropped."""
    s = _surface_clean(m.normalized_text)
    k = s.lower().rstrip(".")

    if not s:
        return 0.0

    # Protected acronyms always survive
    if _is_protected_acronym(m.raw_text):
        return max(0.7, m.backend_score)

    # Hard blocks
    if k in _HARD_BLOCK or s.endswith(":"):
        return 0.0

    # NER types that are just numbers
    if m.entity_type in _NER_TYPES and re.fullmatch(r"\d+(?:\.\d+)?", s):
        return 0.0

    # NER types with ≤2 letters (not an acronym)
    if m.entity_type in _NER_TYPES and len(re.sub(r"[^A-Za-z]", "", s)) <= 2:
        return 0.0

    # Calibrate backend scores
    if m.backend_name == "spacy":
        base = 0.55  # spaCy's fixed 0.85 is inflated
    elif m.backend_name == "gliner":
        base = 0.15 + 0.85 * m.backend_score
    elif m.backend_name == "regex":
        base = (
            0.92
            if m.entity_type in {EntityType.EMAIL, EntityType.PHONE, EntityType.DOCUMENT_ID}
            else 0.78
        )
    else:
        base = m.backend_score

    # Soft penalties
    if k in _WEAK_GENERIC:
        base -= 0.45
    if k in _CONCEPTS:
        base -= 0.60
    # No uppercase at all in a NER entity is suspicious
    if m.entity_type in _NER_TYPES and not re.search(r"[A-Z]", s):
        base -= 0.35
    # Single short word (non-acronym)
    if m.entity_type in _NER_TYPES and len(s.split()) == 1 and len(s) <= 3:
        base -= 0.45

    return max(0.0, min(0.99, base))


def _ocr_fold(text: str) -> str:
    """Fold OCR-common substitutions for fuzzy comparison."""
    t = _surface_clean(text).lower().replace("&", " and ")
    t = re.sub(r"\b8\b", " and ", t)  # OCR: '&' -> '8'
    t = t.translate(str.maketrans({"0": "o", "1": "i", "l": "i", "5": "s"}))
    return re.sub(r"[^a-z0-9 ]+", "", t)


def _core_tokens(text: str, etype: EntityType) -> list[str]:
    """Extract meaningful tokens, stripping org suffixes."""
    toks = re.findall(r"[a-z0-9]+", _ocr_fold(text))
    if etype == EntityType.ORGANIZATION:
        toks = [t for t in toks if t not in _ORG_SUFFIXES and t != "the"]
    return toks


# ---------------------------------------------------------------------------
# Internal pipeline types (not exported — used between extraction phases)
# ---------------------------------------------------------------------------


@dataclass
class Mention:
    """A single mention found by one backend. Internal only.

    Preserves positional info and provenance so the merge phase can
    cross-validate across backends without losing evidence.
    """

    entity_type: EntityType
    raw_text: str
    normalized_text: str
    start_offset: int  # char offset in chunk (-1 if unknown, e.g. LLM)
    end_offset: int  # char offset in chunk (-1 if unknown)
    page: int | None
    context: str  # surrounding sentence or window
    backend_name: str  # "regex", "gliner", "spacy", "llm"
    backend_score: float  # raw confidence from this backend


@dataclass
class AliasCluster:
    """A group of mentions referring to the same real-world entity.

    Internal only. Preserves all raw mentions for provenance — the merge
    phase creates these, and they get converted to ExtractedEntity for output.
    """

    entity_type: EntityType
    canonical_text: str  # best normalized_text (longest or most frequent)
    aliases: set[str] = field(default_factory=set)  # all raw_text variants
    mentions: list[Mention] = field(default_factory=list)
    merged_confidence: float = 0.0
    pages: list[int] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regex patterns for the zero-config fallback
# ---------------------------------------------------------------------------

_REGEX_PATTERNS: dict[EntityType, list[re.Pattern[str]]] = {
    EntityType.DATE: [
        re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
        re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b"),
        re.compile(
            r"\b((?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\.?\s+\d{1,2},?\s+\d{4})\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\.?,?\s+\d{4})\b",
            re.IGNORECASE,
        ),
    ],
    EntityType.MONEY: [
        re.compile(r"(\$[\d,]+(?:\.\d{1,2})?(?:\s*(?:million|billion|trillion|[MBTmbt]))?)\b"),
    ],
    EntityType.EMAIL: [
        re.compile(r"\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b"),
    ],
    EntityType.PHONE: [
        re.compile(r"(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b"),
    ],
    EntityType.DOCUMENT_ID: [
        re.compile(r"\b(\d{4}-[A-Z]-\d{3,6})\b"),
        re.compile(
            r"\b((?:FOIA|FOI|ATF|DOJ|FBI|CIA|DHS|DOD|DOS|EPA|HHS|USDA|OIG|SEC|FTC|FCC|IRS|DEA|ICE|CBP|TSA|NARA)-\d{4}-\d{3,8})\b",
            re.IGNORECASE,
        ),
        re.compile(r"\b(Case\s+(?:No\.?|Number|#)\s*:?\s*[\w-]{4,20})\b", re.IGNORECASE),
    ],
    EntityType.ADDRESS: [
        re.compile(
            r"\b(\d{1,6}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*"
            r"\s+(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|Lane|Ln|"
            r"Way|Court|Ct|Circle|Cir|Place|Pl|Terrace|Ter|Highway|Hwy)"
            r"\.?(?:\s*,?\s*(?:Suite|Ste|Apt|Unit|#)\s*\w+)?)\b",
            re.IGNORECASE,
        ),
    ],
}


def _get_context_window(text: str, start: int, end: int, window: int = 80) -> str:
    """Return surrounding text for a match."""
    ctx_start = max(0, start - window)
    ctx_end = min(len(text), end + window)
    ctx = text[ctx_start:ctx_end].replace("\n", " ").strip()
    if ctx_start > 0:
        ctx = "..." + ctx
    if ctx_end < len(text):
        ctx = ctx + "..."
    return ctx


def _get_sentence(text: str, pos: int) -> str:
    """Extract the sentence containing position `pos`."""
    # Walk backward to sentence start
    start = pos
    while start > 0 and text[start - 1] not in ".!?\n":
        start -= 1
    # Walk forward to sentence end
    end = pos
    while end < len(text) and text[end] not in ".!?\n":
        end += 1
    return text[start : end + 1].strip()


# ---------------------------------------------------------------------------
# GLiNER backend (local zero-shot NER)
# ---------------------------------------------------------------------------

_GLINER_MODEL = None


def _get_gliner():
    """Lazily load the GLiNER model (downloads ~300MB on first use)."""
    global _GLINER_MODEL
    if _GLINER_MODEL is None:
        from gliner import GLiNER

        _GLINER_MODEL = GLiNER.from_pretrained("urchade/gliner_medium-v2.1")
    return _GLINER_MODEL


def _gliner_available() -> bool:
    """Check if GLiNER is installed."""
    try:
        import gliner  # noqa: F401 - availability probe for the optional extra

        return True
    except ImportError:
        return False


def _gliner_type_to_entity_type(label: str) -> EntityType | None:
    """Map GLiNER label strings to our EntityType enum."""
    mapping = {
        "person": EntityType.PERSON,
        "people": EntityType.PERSON,
        "individual": EntityType.PERSON,
        "organization": EntityType.ORGANIZATION,
        "company": EntityType.ORGANIZATION,
        "government agency": EntityType.ORGANIZATION,
        "agency": EntityType.ORGANIZATION,
        "location": EntityType.LOCATION,
        "city": EntityType.LOCATION,
        "country": EntityType.LOCATION,
        "address": EntityType.ADDRESS,
        "date": EntityType.DATE,
        "money": EntityType.MONEY,
        "dollar amount": EntityType.MONEY,
        "email": EntityType.EMAIL,
        "email address": EntityType.EMAIL,
        "phone": EntityType.PHONE,
        "phone number": EntityType.PHONE,
        "document id": EntityType.DOCUMENT_ID,
        "case number": EntityType.DOCUMENT_ID,
        "tracking number": EntityType.DOCUMENT_ID,
    }
    return mapping.get(label.lower())


# ---------------------------------------------------------------------------
# spaCy backend (local statistical NER)
# ---------------------------------------------------------------------------

_SPACY_NLP = None


def _get_spacy():
    """Lazily load spaCy with the best available English model."""
    global _SPACY_NLP
    if _SPACY_NLP is None:
        import spacy

        # Try models from best to smallest
        for model_name in ["en_core_web_trf", "en_core_web_lg", "en_core_web_md", "en_core_web_sm"]:
            try:
                _SPACY_NLP = spacy.load(model_name)
                break
            except OSError:
                continue
        if _SPACY_NLP is None:
            raise ImportError(
                "No spaCy English model found. Install one with: "
                "python -m spacy download en_core_web_sm"
            )
    return _SPACY_NLP


def _spacy_available() -> bool:
    """Check if spaCy is installed with an English model."""
    try:
        import spacy

        for model_name in ["en_core_web_trf", "en_core_web_lg", "en_core_web_md", "en_core_web_sm"]:
            try:
                spacy.load(model_name)
                return True
            except OSError:
                continue
        return False
    except ImportError:
        return False


def _spacy_label_to_entity_type(label: str) -> EntityType | None:
    """Map spaCy NER labels to our EntityType enum."""
    mapping = {
        "PERSON": EntityType.PERSON,
        "PER": EntityType.PERSON,
        "ORG": EntityType.ORGANIZATION,
        "GPE": EntityType.LOCATION,  # Geopolitical entity (countries, cities, states)
        "LOC": EntityType.LOCATION,  # Non-GPE locations (mountains, bodies of water)
        "FAC": EntityType.LOCATION,  # Facilities (buildings, airports)
        "DATE": EntityType.DATE,
        "TIME": EntityType.DATE,
        "MONEY": EntityType.MONEY,
    }
    return mapping.get(label)


# ---------------------------------------------------------------------------
# Relationship extraction (works with GLiNER and spaCy outputs)
# ---------------------------------------------------------------------------


def _extract_cooccurrence_relationships(
    entities: list[ExtractedEntity],
    text: str,
    window_chars: int = 500,
) -> list[dict[str, Any]]:
    """Extract relationships based on entity co-occurrence in text.

    Two entities that appear within `window_chars` of each other are
    considered related. Closer entities get higher confidence.
    Also extracts syntactic relationships from sentence structure.
    """
    relationships: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    # Build entity position index
    entity_positions: list[tuple[ExtractedEntity, int]] = []
    for ent in entities:
        # Find all occurrences of this entity in the text
        search_text = ent.raw_text
        start = 0
        while True:
            idx = text.find(search_text, start)
            if idx == -1:
                break
            entity_positions.append((ent, idx))
            start = idx + 1

    # Sort by position
    entity_positions.sort(key=lambda x: x[1])

    # Find co-occurring pairs
    for i, (ent_a, pos_a) in enumerate(entity_positions):
        for j in range(i + 1, len(entity_positions)):
            ent_b, pos_b = entity_positions[j]

            # Stop looking if too far away
            distance = pos_b - pos_a
            if distance > window_chars:
                break

            # Skip self-relations and same-type low-value pairs
            if ent_a.normalized_text.lower() == ent_b.normalized_text.lower():
                continue

            # Determine relationship type from entity type pairs
            relation = _infer_relation_type(ent_a, ent_b)
            if not relation:
                continue

            # Dedup
            key = (
                min(ent_a.normalized_text, ent_b.normalized_text),
                max(ent_a.normalized_text, ent_b.normalized_text),
                relation,
            )
            if key in seen:
                continue
            seen.add(key)

            # Confidence based on proximity (closer = stronger)
            confidence = max(0.3, 1.0 - (distance / window_chars))

            # Get the sentence as evidence
            mid_pos = (pos_a + pos_b) // 2
            evidence = _get_sentence(text, mid_pos)

            relationships.append(
                {
                    "source": ent_a.normalized_text,
                    "target": ent_b.normalized_text,
                    "relation": relation,
                    "confidence": round(confidence, 2),
                    "evidence": evidence[:300],
                }
            )

    return relationships


def _infer_relation_type(a: ExtractedEntity, b: ExtractedEntity) -> str | None:
    """Infer a relationship type from two entity types."""
    pair = (a.entity_type, b.entity_type)
    reverse_pair = (b.entity_type, a.entity_type)

    relation_map: dict[tuple[EntityType, EntityType], str] = {
        (EntityType.PERSON, EntityType.ORGANIZATION): "affiliated_with",
        (EntityType.PERSON, EntityType.PERSON): "communicated_with",
        (EntityType.PERSON, EntityType.LOCATION): "located_at",
        (EntityType.PERSON, EntityType.MONEY): "associated_amount",
        (EntityType.PERSON, EntityType.DATE): "dated_event",
        (EntityType.ORGANIZATION, EntityType.LOCATION): "located_at",
        (EntityType.ORGANIZATION, EntityType.MONEY): "financial_transaction",
        (EntityType.ORGANIZATION, EntityType.ORGANIZATION): "related_to",
        (EntityType.ORGANIZATION, EntityType.DATE): "dated_event",
        (EntityType.MONEY, EntityType.DATE): "dated_transaction",
    }

    if pair in relation_map:
        return relation_map[pair]
    if reverse_pair in relation_map:
        return relation_map[reverse_pair]
    return None


def _extract_spacy_syntactic_relationships(
    doc: Any,  # spacy Doc
    entities: list[ExtractedEntity],
) -> list[dict[str, Any]]:
    """Extract relationships using spaCy's dependency parse.

    Finds subject-verb-object triples where subjects/objects match
    known entities.
    """
    relationships: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for sent in doc.sents:
        # Find root verb
        root = sent.root
        if root.pos_ != "VERB":
            continue

        # Find subject and object
        subjects = []
        objects = []
        for child in root.children:
            if child.dep_ in ("nsubj", "nsubjpass"):
                # Get the full noun phrase
                span_text = " ".join(t.text for t in child.subtree)
                subjects.append(span_text)
            elif child.dep_ in ("dobj", "pobj", "attr"):
                span_text = " ".join(t.text for t in child.subtree)
                objects.append(span_text)

        # Match to known entities
        for subj in subjects:
            subj_match = _find_entity_match(subj, entities)
            if not subj_match:
                continue
            for obj in objects:
                obj_match = _find_entity_match(obj, entities)
                if not obj_match:
                    continue

                key = (subj_match.normalized_text, obj_match.normalized_text, root.lemma_)
                if key in seen:
                    continue
                seen.add(key)

                relationships.append(
                    {
                        "source": subj_match.normalized_text,
                        "target": obj_match.normalized_text,
                        "relation": root.lemma_,  # the verb lemma (e.g., "work", "pay", "send")
                        "confidence": 0.7,
                        "evidence": sent.text.strip()[:300],
                    }
                )

    return relationships


def _find_entity_match(text: str, entities: list[ExtractedEntity]) -> ExtractedEntity | None:
    """Find an entity whose text matches (substring) the given text."""
    text_lower = text.lower()
    for ent in entities:
        if ent.normalized_text.lower() in text_lower or text_lower in ent.normalized_text.lower():
            return ent
    return None


# ---------------------------------------------------------------------------
# LLM prompt builder
# ---------------------------------------------------------------------------


def _build_extraction_prompt(
    text: str,
    context: str | None,
    page_number: int | None,
    entity_config: EntityConfig,
) -> str:
    """Build the structured prompt for LLM entity extraction.

    Uses a compact prompt for smaller models (≤8B) and a detailed prompt
    for larger models. Smaller models choke on long instructions with
    many entity types and relationship definitions.
    """

    type_descriptions = {
        "PERSON": "Names of individuals (include titles/roles if mentioned)",
        "ORGANIZATION": "Companies, agencies, departments, groups, committees",
        "LOCATION": "Cities, states, countries, facilities, addresses",
        "DATE": "Specific dates or date ranges",
        "MONEY": "Dollar amounts, budgets, costs, fees",
        "DOCUMENT_ID": "Case numbers, file numbers, reference IDs, tracking numbers",
        "PHONE": "Phone numbers",
        "EMAIL": "Email addresses",
        "ADDRESS": "Street addresses, mailing addresses",
    }

    type_lines = []
    for t in entity_config.builtin_types:
        desc = type_descriptions.get(t, t)
        type_lines.append(f"- {t}: {desc}")

    for ct in entity_config.custom_types:
        name = ct.get("name", "").upper()
        desc = ct.get("description", name)
        type_lines.append(f"- {name}: {desc}")

    types_block = "\n".join(type_lines)

    suffix = ""
    if entity_config.extraction_prompt_suffix:
        suffix = f"\n\nADDITIONAL INSTRUCTIONS:\n{entity_config.extraction_prompt_suffix}"

    return f"""Analyze this document and extract ALL entities and relationships.
This is a government/legal document obtained through FOIA. Be thorough.

CONTEXT: {context or "FOIA response document"}
PAGE: {page_number or "Unknown"}

DOCUMENT TEXT:
{text}

Extract these entity types:
{types_block}

For each entity provide:
1. raw_text: Exactly as it appears
2. normalized: Cleaned/standardized (e.g., "Dr. John A. Smith" -> "John A. Smith")
3. type: From the list above
4. confidence: 0.0-1.0
5. context: The surrounding sentence

Also identify RELATIONSHIPS between entities:
- "affiliated_with" (person-organization)
- "located_at" (entity-location)
- "communicated_with" (person-person)
- "financial_transaction" (entity-money)
- "dated_event" (entity-date)
- "contracted_with" (org-org)

Return ONLY valid JSON:
{{
  "entities": [
    {{"raw_text": "...", "normalized": "...", "type": "PERSON", "confidence": 0.95, "context": "..."}}
  ],
  "relationships": [
    {{"source": "John Smith", "target": "Acme Corp", "relation": "affiliated_with", "confidence": 0.9}}
  ]
}}{suffix}"""


# ---------------------------------------------------------------------------
# LLM client helpers
# ---------------------------------------------------------------------------


def _call_ollama(
    prompt: str, model: str, base_url: str | None, temperature: float, max_tokens: int
) -> str:
    import urllib.request

    url = (base_url or "http://localhost:11434").rstrip("/")
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
    ).encode()
    req = urllib.request.Request(
        f"{url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read())
    return body.get("response", "")


def _call_anthropic(
    prompt: str, model: str, api_key: str, temperature: float, max_tokens: int
) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _call_openai(
    prompt: str, model: str, api_key: str, base_url: str | None, temperature: float, max_tokens: int
) -> str:
    import openai

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = openai.OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""


def _llm_available(provider: str, api_key: str | None, base_url: str | None) -> bool:
    import urllib.request

    if provider == "ollama":
        url = (base_url or "http://localhost:11434").rstrip("/")
        try:
            req = urllib.request.Request(f"{url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception:
            return False

    if provider == "openai" and base_url:
        # Local OpenAI-compatible server — check if it's actually reachable
        is_local = "localhost" in base_url or "127.0.0.1" in base_url
        if is_local:
            try:
                url = base_url.rstrip("/")
                req = urllib.request.Request(f"{url}/models", method="GET")
                with urllib.request.urlopen(req, timeout=3):
                    return True
            except Exception:
                return False

    return bool(api_key)


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------


class EntityExtractor:
    """Extract entities and relationships from document text.

    Tries backends in order: LLM → GLiNER → spaCy → Regex.
    The first available backend is used.
    """

    def __init__(
        self,
        config: OpenFOIAConfig | None = None,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        cfg = config or load_config()
        self.provider = provider or cfg.ai.provider
        self.model = model or cfg.ai.model
        self.api_key = api_key or cfg.ai.api_key
        self.base_url = base_url or cfg.ai.base_url
        self.temperature = cfg.ai.extraction_temperature
        self.max_tokens = cfg.ai.extraction_max_tokens
        self.entity_config = cfg.entities
        self._backend: str | None = None  # resolved lazily

    def _resolve_backend(self) -> str:
        """Determine which extraction backend to use."""
        if self._backend is not None:
            return self._backend

        # Try in order of quality.
        if _llm_available(self.provider, self.api_key, self.base_url):
            if self.provider in ("anthropic", "openai"):
                import sys

                is_local = self.base_url and (
                    "localhost" in self.base_url
                    or "127.0.0.1" in self.base_url
                    or "0.0.0.0" in self.base_url
                )
                if is_local:
                    print(
                        f"Using local AI provider ({self.model} via {self.base_url})",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"WARNING: Using cloud AI provider '{self.provider}'. "
                        "Document text will be sent to external servers.",
                        file=sys.stderr,
                    )
            self._backend = "llm"
        elif _gliner_available():
            self._backend = "gliner"
        elif _spacy_available():
            self._backend = "spacy"
        else:
            self._backend = "regex"

        return self._backend

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract(
        self,
        text: str,
        context: str | None = None,
        page_numbers: list[int] | None = None,
        ensemble: bool = False,
    ) -> ExtractionResult:
        """Extract entities and relationships from text.

        Pipeline:
        1. Fast extraction: regex (always) + NER backends (GLiNER/spaCy)
        2. Merge: deduplicate, boost multi-backend agreement
        3. LLM cleanup: if available, validate and clean the merged list
        4. Relationships: co-occurrence from validated entities

        Default: regex + best NER. --ensemble: all NER backends.
        LLM always runs as step 3 validator if available (not as extractor).
        """
        import asyncio

        backend = self._resolve_backend()

        # --- Phase 1: Fast extraction (NER + regex) ---
        all_mentions: list[Mention] = []

        # Regex always runs (instant, zero cost)
        all_mentions.extend(self._mentions_from_regex(text, page_numbers))

        if ensemble:
            # Run all available NER backends
            if _gliner_available():
                all_mentions.extend(
                    await asyncio.to_thread(self._mentions_from_gliner, text, page_numbers)
                )
            if _spacy_available():
                all_mentions.extend(
                    await asyncio.to_thread(self._mentions_from_spacy, text, page_numbers)
                )
        else:
            # Run best available NER backend alongside regex
            # LLM is NOT here — it runs in Phase 3 as validator
            if backend in ("gliner", "llm") and _gliner_available():
                all_mentions.extend(
                    await asyncio.to_thread(self._mentions_from_gliner, text, page_numbers)
                )
            elif backend in ("spacy", "llm") and _spacy_available():
                all_mentions.extend(
                    await asyncio.to_thread(self._mentions_from_spacy, text, page_numbers)
                )

        # --- Phase 2: Merge + deduplicate ---
        clusters = self._merge_mentions(all_mentions)
        entities = self._clusters_to_entities(clusters)

        # --- Phase 3: LLM validates the merged entity list ---
        # Only runs when NER backends produced entities worth validating.
        # Skip if only regex ran (no NER entities to validate) or backend was forced.
        has_ner_mentions = any(m.backend_name != "regex" for m in all_mentions)
        llm_available = has_ner_mentions and _llm_available(
            self.provider, self.api_key, self.base_url
        )
        if llm_available and entities:
            entities = await self._llm_validate_entities(entities, text)

        # --- Phase 4: Relationships from validated entities ---
        relationships = _extract_cooccurrence_relationships(entities, text) if entities else []

        backends_used = sorted({m.backend_name for m in all_mentions})
        if llm_available:
            backends_used.append("llm-validator")

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            summary=self._generate_summary(entities, relationships),
            metadata={
                "backend": "+".join(backends_used) if backends_used else backend,
                "backends": backends_used,
                "total_chars": len(text),
                "mentions_raw": len(all_mentions),
                "entities_merged": len(entities),
                "ensemble": ensemble,
                "llm_validated": llm_available,
            },
        )

    # ------------------------------------------------------------------
    # LLM validation (Phase 3 — reviews entity list, not document)
    # ------------------------------------------------------------------

    async def _llm_validate_entities(
        self, entities: list[ExtractedEntity], source_text: str
    ) -> list[ExtractedEntity]:
        """LLM reviews the merged entity list. Fast because input is ~2K, not 78K."""
        import asyncio

        # Build compact entity list for LLM
        entity_lines = []
        for e in entities:
            entity_lines.append(
                f'- {e.entity_type.value}: "{e.raw_text}" (confidence: {e.confidence:.0%})'
            )
        entity_list = "\n".join(entity_lines[:200])  # cap to keep prompt manageable

        prompt = f"""Review this entity list extracted from a government document.

TASKS:
1. Remove junk entries (boilerplate like "To:", "APPROVED", job titles, sentence fragments)
2. Flag OCR errors in names (e.g. "KIRKLAND 8. ELLIS" should be "Kirkland & Ellis")
3. Mark low-quality entries with confidence 0.1
4. Keep all legitimate people, organizations, locations, dates, money amounts

ENTITIES:
{entity_list}

Return JSON: {{"keep": [{{"raw_text": "...", "confidence": 0.95, "corrected": "..."}}], "remove": ["junk entry 1", "junk entry 2"]}}"""

        try:
            if self.provider == "openai":
                raw = await asyncio.to_thread(
                    _call_openai, prompt, self.model, self.api_key or "", self.base_url, 0.1, 4000
                )
            else:
                raw = await asyncio.to_thread(
                    _call_ollama, prompt, self.model, self.base_url, 0.1, 4000
                )
        except Exception as exc:
            import sys

            print(
                f"LLM validation skipped: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return entities  # LLM failed, return unmodified

        # Parse LLM response
        try:
            json_match = re.search(r"\{[\s\S]*\}", raw)
            if not json_match:
                import sys

                print(
                    f"LLM validation: no JSON found in response ({len(raw)} chars)", file=sys.stderr
                )
                return entities
            data = json.loads(json_match.group())
        except (json.JSONDecodeError, AttributeError) as exc:
            import sys

            print(f"LLM validation: JSON parse failed: {exc}", file=sys.stderr)
            return entities

        import sys

        keep_count = len(data.get("keep", []))
        remove_count = len(data.get("remove", []))
        print(
            f"LLM validation: keep={keep_count}, remove={remove_count} "
            f"(from {len(entities)} entities)",
            file=sys.stderr,
        )

        # Apply LLM decisions — handle both strings and dicts in remove list
        remove_set: set[str] = set()
        for r in data.get("remove", []):
            if isinstance(r, str):
                remove_set.add(r.lower().strip())
            elif isinstance(r, dict):
                remove_set.add(r.get("raw_text", "").lower().strip())
        corrections = {
            k.get("raw_text", "").lower(): k for k in data.get("keep", []) if isinstance(k, dict)
        }

        validated = []
        for ent in entities:
            raw_lower = ent.raw_text.lower().strip()
            if raw_lower in remove_set:
                continue  # LLM said remove
            if raw_lower in corrections:
                corr = corrections[raw_lower]
                # Apply correction
                if corr.get("corrected"):
                    ent.normalized_text = corr["corrected"]
                if corr.get("confidence") is not None:
                    ent.confidence = float(corr["confidence"])
                ent.metadata["llm_validated"] = True
            validated.append(ent)

        return validated

    # ------------------------------------------------------------------
    # LLM extraction (legacy, used by _mentions_from_llm)
    # ------------------------------------------------------------------

    async def _extract_with_llm(
        self,
        text: str,
        context: str | None,
        page_numbers: list[int] | None,
    ) -> ExtractionResult:
        import asyncio

        chunks = self._chunk_text(text, max_chars=8000)
        all_entities: list[ExtractedEntity] = []
        all_relationships: list[dict[str, Any]] = []

        for i, chunk in enumerate(chunks):
            page_num = page_numbers[i] if page_numbers and i < len(page_numbers) else None
            result = await asyncio.to_thread(self._extract_chunk_llm, chunk, context, page_num)
            all_entities.extend(result["entities"])
            all_relationships.extend(result["relationships"])

        merged = self._merge_entities(all_entities)

        # Validate: remove entities whose raw_text doesn't appear in the source.
        # This mitigates prompt injection from adversarial documents that try to
        # fabricate entities via instructions embedded in the text.
        text_lower = text.lower()
        validated = []
        for ent in merged:
            if ent.raw_text.lower() in text_lower:
                validated.append(ent)
            else:
                # Entity text not found in source — likely hallucinated or injected
                ent.confidence *= 0.3  # heavily penalize but don't discard
                ent.metadata["validation"] = "not_found_in_source"
                if ent.confidence >= 0.2:
                    validated.append(ent)
        merged = validated

        # Supplement with co-occurrence relationships
        cooccurrence_rels = _extract_cooccurrence_relationships(merged, text)
        # Merge, preferring LLM relationships (higher quality)
        existing_keys = {
            (r.get("source", "").lower(), r.get("target", "").lower()) for r in all_relationships
        }
        for rel in cooccurrence_rels:
            key = (rel["source"].lower(), rel["target"].lower())
            if key not in existing_keys:
                all_relationships.append(rel)

        return ExtractionResult(
            entities=merged,
            relationships=all_relationships,
            summary=self._generate_summary(merged, all_relationships),
            metadata={
                "backend": "llm",
                "model": self.model,
                "provider": self.provider,
                "chunks": len(chunks),
                "total_chars": len(text),
            },
        )

    def _extract_chunk_llm(
        self, text: str, context: str | None, page_number: int | None
    ) -> dict[str, Any]:
        prompt = _build_extraction_prompt(text, context, page_number, self.entity_config)

        if self.provider == "ollama":
            content = _call_ollama(
                prompt, self.model, self.base_url, self.temperature, self.max_tokens
            )
        elif self.provider == "anthropic":
            content = _call_anthropic(
                prompt, self.model, self.api_key or "", self.temperature, self.max_tokens
            )
        elif self.provider == "openai":
            content = _call_openai(
                prompt,
                self.model,
                self.api_key or "",
                self.base_url,
                self.temperature,
                self.max_tokens,
            )
        else:
            raise ValueError(f"Unknown AI provider: {self.provider}")

        return self._parse_llm_response(content, page_number)

    def _parse_llm_response(self, content: str, page_number: int | None) -> dict[str, Any]:
        try:
            json_match = re.search(r"\{[\s\S]*\}", content)
            data = (
                json.loads(json_match.group())
                if json_match
                else {"entities": [], "relationships": []}
            )
        except json.JSONDecodeError:
            data = {"entities": [], "relationships": []}

        valid_types: set[str] = set(self.entity_config.builtin_types)
        for ct in self.entity_config.custom_types:
            name = ct.get("name", "").upper()
            if name:
                valid_types.add(name)

        entities: list[ExtractedEntity] = []
        for e in data.get("entities", []):
            type_str = (e.get("type") or "").upper()
            if type_str not in valid_types:
                continue
            try:
                entity_type = EntityType(type_str.lower())
            except ValueError:
                # Custom type — store as DOCUMENT_ID with original type in metadata
                entity_type = EntityType.DOCUMENT_ID

            raw = e.get("raw_text") or e.get("text") or e.get("name", "")
            if not raw:
                continue
            entities.append(
                ExtractedEntity(
                    entity_type=entity_type,
                    raw_text=raw,
                    normalized_text=e.get("normalized") or raw,
                    confidence=float(e.get("confidence", 0.5)),
                    context=e.get("context", ""),
                    page_number=page_number,
                    metadata={"custom_type": type_str}
                    if type_str not in {t.value.upper() for t in EntityType}
                    else {},
                )
            )

        return {"entities": entities, "relationships": data.get("relationships", [])}

    # ------------------------------------------------------------------
    # GLiNER path (local zero-shot NER, custom entity types)
    # ------------------------------------------------------------------

    def _extract_with_gliner(
        self,
        text: str,
        page_numbers: list[int] | None,
    ) -> ExtractionResult:
        model = _get_gliner()

        # Define entity labels — including custom types from config
        labels = [
            "person",
            "organization",
            "government agency",
            "location",
            "date",
            "money",
            "dollar amount",
            "email address",
            "phone number",
            "address",
            "case number",
            "document id",
            "tracking number",
        ]

        # Add custom types from config
        for ct in self.entity_config.custom_types:
            label = ct.get("name", "").lower().replace("_", " ")
            if label and label not in labels:
                labels.append(label)

        # GLiNER works best on chunks (max ~1500 tokens)
        chunks = self._chunk_text(text, max_chars=4000)
        all_entities: list[ExtractedEntity] = []

        for i, chunk in enumerate(chunks):
            page_num = page_numbers[i] if page_numbers and i < len(page_numbers) else None

            predictions = model.predict_entities(chunk, labels, threshold=0.4)

            for pred in predictions:
                entity_type = _gliner_type_to_entity_type(pred["label"])
                if entity_type is None:
                    continue

                raw = pred["text"]
                # Skip very short or very long matches (noise)
                if len(raw.strip()) < 2 or len(raw) > 200:
                    continue

                ctx_start = max(0, chunk.find(raw) - 80)
                ctx_end = min(len(chunk), chunk.find(raw) + len(raw) + 80)
                context = chunk[ctx_start:ctx_end].replace("\n", " ").strip()

                all_entities.append(
                    ExtractedEntity(
                        entity_type=entity_type,
                        raw_text=raw,
                        normalized_text=raw.strip(),
                        confidence=round(pred["score"], 3),
                        context=context,
                        page_number=page_num,
                    )
                )

        merged = self._merge_entities(all_entities)

        # Extract relationships via co-occurrence
        relationships = _extract_cooccurrence_relationships(merged, text)

        return ExtractionResult(
            entities=merged,
            relationships=relationships,
            summary=self._generate_summary(merged, relationships),
            metadata={
                "backend": "gliner",
                "model": "gliner_medium-v2.1",
                "chunks": len(chunks),
                "total_chars": len(text),
            },
        )

    # ------------------------------------------------------------------
    # spaCy path (local statistical NER + dependency parsing)
    # ------------------------------------------------------------------

    def _extract_with_spacy(
        self,
        text: str,
        page_numbers: list[int] | None,
    ) -> ExtractionResult:
        nlp = _get_spacy()

        # spaCy has a max length — process in chunks if needed
        max_len = nlp.max_length
        chunks = self._chunk_text(text, max_chars=min(max_len - 100, 100000))

        all_entities: list[ExtractedEntity] = []
        all_relationships: list[dict[str, Any]] = []

        for i, chunk in enumerate(chunks):
            page_num = page_numbers[i] if page_numbers and i < len(page_numbers) else None
            doc = nlp(chunk)

            # Extract named entities
            for ent in doc.ents:
                entity_type = _spacy_label_to_entity_type(ent.label_)
                if entity_type is None:
                    continue

                # Skip very short matches
                if len(ent.text.strip()) < 2:
                    continue

                context = _get_context_window(chunk, ent.start_char, ent.end_char)

                all_entities.append(
                    ExtractedEntity(
                        entity_type=entity_type,
                        raw_text=ent.text,
                        normalized_text=ent.text.strip(),
                        confidence=0.85,  # spaCy doesn't provide per-entity scores
                        context=context,
                        page_number=page_num,
                    )
                )

            # Extract syntactic relationships
            chunk_entities = [
                e for e in all_entities if e.page_number == page_num or page_num is None
            ]
            syntactic_rels = _extract_spacy_syntactic_relationships(doc, chunk_entities)
            all_relationships.extend(syntactic_rels)

        merged = self._merge_entities(all_entities)

        # Also add co-occurrence relationships
        cooccurrence_rels = _extract_cooccurrence_relationships(merged, text)
        existing_keys = {
            (r.get("source", "").lower(), r.get("target", "").lower()) for r in all_relationships
        }
        for rel in cooccurrence_rels:
            key = (rel["source"].lower(), rel["target"].lower())
            if key not in existing_keys:
                all_relationships.append(rel)

        # Also run regex for structured patterns spaCy misses (emails, phones, doc IDs)
        regex_result = self._extract_with_regex(text, page_numbers)
        for ent in regex_result.entities:
            if ent.entity_type in (
                EntityType.EMAIL,
                EntityType.PHONE,
                EntityType.DOCUMENT_ID,
                EntityType.ADDRESS,
            ):
                # Only add if not already found
                key = (ent.entity_type, ent.normalized_text.lower())
                existing = {(e.entity_type, e.normalized_text.lower()) for e in merged}
                if key not in existing:
                    merged.append(ent)

        return ExtractionResult(
            entities=merged,
            relationships=all_relationships,
            summary=self._generate_summary(merged, all_relationships),
            metadata={
                "backend": "spacy",
                "model": _SPACY_NLP.meta["name"] if _SPACY_NLP else "unknown",
                "chunks": len(chunks),
                "total_chars": len(text),
            },
        )

    # ------------------------------------------------------------------
    # Regex fallback (zero config, always available)
    # ------------------------------------------------------------------

    def _extract_with_regex(
        self,
        text: str,
        page_numbers: list[int] | None,
    ) -> ExtractionResult:
        entities: list[ExtractedEntity] = []
        seen: set[tuple[str, str]] = set()

        for entity_type, patterns in _REGEX_PATTERNS.items():
            for pattern in patterns:
                for match in pattern.finditer(text):
                    raw = match.group(1) if match.lastindex else match.group(0)
                    normalized = raw.strip()
                    key = (entity_type.value, normalized.lower())
                    if key in seen:
                        continue
                    seen.add(key)

                    ctx = _get_context_window(text, match.start(), match.end())
                    page_num = page_numbers[0] if page_numbers else None

                    entities.append(
                        ExtractedEntity(
                            entity_type=entity_type,
                            raw_text=raw,
                            normalized_text=normalized,
                            confidence=0.6,
                            context=ctx,
                            page_number=page_num,
                        )
                    )

        # Custom patterns from config (so openfoia entities add actually works in regex mode)
        for ct in self.entity_config.custom_types:
            pattern_str = ct.get("pattern", "")
            type_name = ct.get("name", "CUSTOM").upper()
            if not pattern_str:
                continue
            try:
                compiled = re.compile(pattern_str)
            except re.error:
                continue
            try:
                etype = EntityType(type_name.lower())
            except ValueError:
                etype = EntityType.DOCUMENT_ID
            for match in compiled.finditer(text):
                raw = match.group(0)
                normalized = raw.strip()
                key = (type_name.lower(), normalized.lower())
                if key in seen:
                    continue
                seen.add(key)
                ctx = _get_context_window(text, match.start(), match.end())
                entities.append(
                    ExtractedEntity(
                        entity_type=etype,
                        raw_text=raw,
                        normalized_text=normalized,
                        confidence=0.6,
                        context=ctx,
                        page_number=page_numbers[0] if page_numbers else None,
                        metadata={"custom_type": type_name},
                    )
                )

        # Co-occurrence relationships even in regex mode
        relationships = _extract_cooccurrence_relationships(entities, text) if entities else []

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            summary=self._generate_summary(entities, relationships),
            metadata={
                "backend": "regex",
                "total_chars": len(text),
                "note": "No AI/NER model available. Install gliner or spacy for people/org extraction.",
            },
        )

    # ------------------------------------------------------------------
    # Mention extractors (Phase 1 of new pipeline)
    # ------------------------------------------------------------------

    def _mentions_from_regex(self, text: str, page_numbers: list[int] | None) -> list[Mention]:
        """Extract mentions using regex patterns. Always runs, instant."""
        mentions: list[Mention] = []
        seen: set[tuple[str, str]] = set()
        page_num = page_numbers[0] if page_numbers else None

        for entity_type, patterns in _REGEX_PATTERNS.items():
            for pattern in patterns:
                for match in pattern.finditer(text):
                    raw = match.group(1) if match.lastindex else match.group(0)
                    normalized = raw.strip()
                    key = (entity_type.value, normalized.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    ctx = _get_context_window(text, match.start(), match.end())
                    mentions.append(
                        Mention(
                            entity_type=entity_type,
                            raw_text=raw,
                            normalized_text=normalized,
                            start_offset=match.start(),
                            end_offset=match.end(),
                            page=page_num,
                            context=ctx,
                            backend_name="regex",
                            backend_score=0.6,
                        )
                    )

        # Custom patterns from config
        for ct in self.entity_config.custom_types:
            pattern_str = ct.get("pattern", "")
            type_name = ct.get("name", "CUSTOM").upper()
            if not pattern_str:
                continue
            try:
                compiled = re.compile(pattern_str)
            except re.error:
                continue
            try:
                etype = EntityType(type_name.lower())
            except ValueError:
                etype = EntityType.DOCUMENT_ID
            for match in compiled.finditer(text):
                raw = match.group(0)
                normalized = raw.strip()
                key = (type_name.lower(), normalized.lower())
                if key in seen:
                    continue
                seen.add(key)
                ctx = _get_context_window(text, match.start(), match.end())
                mentions.append(
                    Mention(
                        entity_type=etype,
                        raw_text=raw,
                        normalized_text=normalized,
                        start_offset=match.start(),
                        end_offset=match.end(),
                        page=page_num,
                        context=ctx,
                        backend_name="regex",
                        backend_score=0.6,
                    )
                )

        return mentions

    def _mentions_from_gliner(self, text: str, page_numbers: list[int] | None) -> list[Mention]:
        """Extract mentions using GLiNER zero-shot NER."""
        model = _get_gliner()
        if model is None:
            return []

        labels = [
            "person",
            "organization",
            "government agency",
            "location",
            "date",
            "money",
            "dollar amount",
            "email address",
            "phone number",
            "address",
            "case number",
            "document id",
            "tracking number",
        ]
        for ct in self.entity_config.custom_types:
            ct_name = ct.get("name", "").lower().replace("_", " ")
            if ct_name:
                labels.append(ct_name)

        mentions: list[Mention] = []
        chunks = self._chunk_text(text, max_chars=4000)

        for ci, chunk in enumerate(chunks):
            page = page_numbers[ci] if page_numbers and ci < len(page_numbers) else None
            try:
                predictions = model.predict_entities(chunk, labels, threshold=0.4)
            except Exception:
                continue

            for pred in predictions:
                raw = pred.get("text", "")
                if not raw or len(raw) < 2 or len(raw) > 200:
                    continue
                label = pred.get("label", "").lower()
                etype = _gliner_type_to_entity_type(label)
                if etype is None:
                    continue
                score = round(pred.get("score", 0.5), 3)
                start = pred.get("start", -1)
                end = pred.get("end", -1)
                ctx_start = max(0, chunk.find(raw) - 80)
                ctx_end = min(len(chunk), chunk.find(raw) + len(raw) + 80)
                ctx = chunk[ctx_start:ctx_end].replace("\n", " ").strip()

                mentions.append(
                    Mention(
                        entity_type=etype,
                        raw_text=raw,
                        normalized_text=raw.strip(),
                        start_offset=start,
                        end_offset=end,
                        page=page,
                        context=ctx,
                        backend_name="gliner",
                        backend_score=score,
                    )
                )

        return mentions

    def _mentions_from_spacy(self, text: str, page_numbers: list[int] | None) -> list[Mention]:
        """Extract mentions using spaCy NLP pipeline."""
        nlp = _get_spacy()
        if nlp is None:
            return []

        mentions: list[Mention] = []
        max_len = nlp.max_length
        chunks = self._chunk_text(text, max_chars=min(max_len - 100, 100000))

        for ci, chunk in enumerate(chunks):
            page = page_numbers[ci] if page_numbers and ci < len(page_numbers) else None
            try:
                doc = nlp(chunk)
            except Exception:
                continue

            for ent in doc.ents:
                etype = _spacy_label_to_entity_type(ent.label_)
                if etype is None:
                    continue
                raw = ent.text.strip()
                if not raw:
                    continue
                ctx = _get_context_window(chunk, ent.start_char, ent.end_char)
                mentions.append(
                    Mention(
                        entity_type=etype,
                        raw_text=raw,
                        normalized_text=raw,
                        start_offset=ent.start_char,
                        end_offset=ent.end_char,
                        page=page,
                        context=ctx,
                        backend_name="spacy",
                        backend_score=0.85,
                    )
                )

        return mentions

    def _mentions_from_llm(
        self,
        text: str,
        context: str | None,
        page_numbers: list[int] | None,
    ) -> list[Mention]:
        """Extract mentions using LLM. Slowest but highest quality."""
        mentions: list[Mention] = []
        chunks = self._chunk_text(text, max_chars=8000)

        for ci, chunk in enumerate(chunks):
            page = page_numbers[ci] if page_numbers and ci < len(page_numbers) else None
            try:
                chunk_result = self._extract_chunk_llm(chunk, context, page)
            except Exception:
                continue

            for ent in chunk_result.get("entities", []):
                raw = ent.raw_text
                if not raw:
                    continue
                # Hallucination check: penalize if not in source
                score = ent.confidence
                if raw.lower() not in chunk.lower():
                    score *= 0.3
                mentions.append(
                    Mention(
                        entity_type=ent.entity_type,
                        raw_text=raw,
                        normalized_text=ent.normalized_text,
                        start_offset=-1,
                        end_offset=-1,
                        page=page,
                        context=ent.context,
                        backend_name="llm",
                        backend_score=score,
                    )
                )

        return mentions

    # ------------------------------------------------------------------
    # Merge + Cluster (Phase 2 of new pipeline)
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_mentions(mentions: list[Mention]) -> list[AliasCluster]:
        """Merge mentions from multiple backends into alias clusters.

        Three phases:
        1. Score and filter: drop obvious junk, recalibrate confidence
        2. Exact grouping: same type + normalized text
        3. Fuzzy merge: substring match, OCR fold, token overlap
        """
        from difflib import SequenceMatcher

        # --- Phase 1: Score and filter junk ---
        scored: list[Mention] = []
        for m in mentions:
            score = _mention_score(m)
            if score <= 0.0:
                continue  # hard drop
            m.backend_score = score  # recalibrated
            scored.append(m)

        # --- Phase 2: Exact grouping ---
        # For orgs, strip legal suffixes from the key
        def _group_key(m: Mention) -> tuple[str, str]:
            norm = m.normalized_text.lower().strip().rstrip(".")
            if m.entity_type == EntityType.ORGANIZATION:
                for suffix in _ORG_SUFFIXES:
                    if norm.endswith(f" {suffix}"):
                        norm = norm[: -(len(suffix) + 1)].rstrip(",. ")
                        break
                    if norm.endswith(f" {suffix}."):
                        norm = norm[: -(len(suffix) + 2)].rstrip(",. ")
                        break
            return (m.entity_type.value, norm)

        groups: dict[tuple[str, str], list[Mention]] = {}
        for m in scored:
            key = _group_key(m)
            if key not in groups:
                groups[key] = []
            groups[key].append(m)

        clusters: list[AliasCluster] = []
        for (type_val, _norm_key), group in groups.items():
            try:
                etype = EntityType(type_val)
            except ValueError:
                continue

            best = max(group, key=lambda m: (len(m.normalized_text), m.backend_score))
            all_raw = {m.raw_text for m in group}
            all_pages = sorted({m.page for m in group if m.page is not None})
            all_contexts = [m.context for m in group if m.context][:5]

            backends = {m.backend_name for m in group}
            max_score = max(m.backend_score for m in group)
            boosted = min(
                0.99,
                max_score
                + 0.10 * (len(backends) - 1)
                + 0.05 * min(3, len(group) - 1)
                + (0.05 if len(all_pages) > 1 else 0),
            )

            clusters.append(
                AliasCluster(
                    entity_type=etype,
                    canonical_text=best.normalized_text,
                    aliases=all_raw,
                    mentions=group,
                    merged_confidence=boosted,
                    pages=all_pages,
                    contexts=all_contexts,
                )
            )

        # --- Phase 3: Fuzzy merge ---
        merged: list[AliasCluster] = []
        used: set[int] = set()
        sorted_clusters = sorted(clusters, key=lambda c: len(c.mentions), reverse=True)

        for i, c1 in enumerate(sorted_clusters):
            if i in used:
                continue
            for j, c2 in enumerate(sorted_clusters):
                if j <= i or j in used or c1.entity_type != c2.entity_type:
                    continue

                should_merge = False
                a, b = c1.canonical_text, c2.canonical_text

                # Substring match
                if (
                    a.lower() in b.lower()
                    or b.lower() in a.lower()
                    or SequenceMatcher(None, _ocr_fold(a), _ocr_fold(b)).ratio() >= 0.90
                ):
                    should_merge = True
                # Token Jaccard for orgs
                elif c1.entity_type == EntityType.ORGANIZATION:
                    ta = set(_core_tokens(a, c1.entity_type))
                    tb = set(_core_tokens(b, c2.entity_type))
                    if ta and tb and len(ta & tb) / len(ta | tb) >= 0.80:
                        should_merge = True
                # Person: same last name + first initial
                elif c1.entity_type == EntityType.PERSON:
                    ta = _core_tokens(a, c1.entity_type)
                    tb = _core_tokens(b, c2.entity_type)
                    if (
                        len(ta) >= 2
                        and len(tb) >= 2
                        and ta[-1] == tb[-1]
                        and ta[0][:1] == tb[0][:1]
                    ):
                        should_merge = True

                if should_merge:
                    c1.aliases.update(c2.aliases)
                    c1.mentions.extend(c2.mentions)
                    c1.pages = sorted(set(c1.pages + c2.pages))
                    c1.contexts.extend(c2.contexts[:3])
                    # Pick canonical: most mentions + highest score
                    if len(c2.mentions) > len(c1.mentions):
                        c1.canonical_text = c2.canonical_text
                    backends = {m.backend_name for m in c1.mentions}
                    max_score = max(m.backend_score for m in c1.mentions)
                    c1.merged_confidence = min(
                        0.99,
                        max_score
                        + 0.10 * (len(backends) - 1)
                        + 0.05 * min(3, len(c1.mentions) - 1),
                    )
                    used.add(j)
            merged.append(c1)

        return merged

    @staticmethod
    def _clusters_to_entities(clusters: list[AliasCluster]) -> list[ExtractedEntity]:
        """Convert alias clusters to ExtractedEntity for backward compat."""
        entities: list[ExtractedEntity] = []
        for cluster in clusters:
            # Most frequent raw_text
            from collections import Counter

            raw_counts = Counter(m.raw_text for m in cluster.mentions)
            best_raw = raw_counts.most_common(1)[0][0] if raw_counts else cluster.canonical_text

            entities.append(
                ExtractedEntity(
                    entity_type=cluster.entity_type,
                    raw_text=best_raw,
                    normalized_text=cluster.canonical_text,
                    confidence=cluster.merged_confidence,
                    context=cluster.contexts[0] if cluster.contexts else "",
                    page_number=cluster.pages[0] if cluster.pages else None,
                    metadata={
                        "occurrence_count": len(cluster.mentions),
                        "pages": cluster.pages,
                        "aliases": sorted(cluster.aliases),
                        "backends": sorted({m.backend_name for m in cluster.mentions}),
                    },
                )
            )

        return entities

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _chunk_text(self, text: str, max_chars: int = 8000) -> list[str]:
        """Split text into chunks on paragraph boundaries."""
        if len(text) <= max_chars:
            return [text]

        chunks: list[str] = []
        paragraphs = text.split("\n\n")
        current_chunk: list[str] = []
        current_len = 0

        for para in paragraphs:
            para_len = len(para) + 2
            if current_len + para_len > max_chars and current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_len = 0
            current_chunk.append(para)
            current_len += para_len

        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

        return chunks

    def _merge_entities(self, entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
        """Merge duplicate entities, keeping the highest-confidence one."""
        by_key: dict[tuple[EntityType, str], list[ExtractedEntity]] = {}
        for entity in entities:
            key = (entity.entity_type, entity.normalized_text.lower())
            by_key.setdefault(key, []).append(entity)

        merged: list[ExtractedEntity] = []
        for group in by_key.values():
            best = max(group, key=lambda e: e.confidence)
            best.metadata["occurrence_count"] = len(group)
            best.metadata["pages"] = sorted(
                {e.page_number for e in group if e.page_number is not None}
            )
            merged.append(best)

        return merged

    def _generate_summary(
        self,
        entities: list[ExtractedEntity],
        relationships: list[dict[str, Any]],
    ) -> str:
        by_type: dict[EntityType, list[ExtractedEntity]] = {}
        for entity in entities:
            by_type.setdefault(entity.entity_type, []).append(entity)

        lines = ["## Entity Extraction Summary\n"]

        for entity_type, type_entities in by_type.items():
            lines.append(f"### {entity_type.value.title()}s ({len(type_entities)})")
            for e in sorted(type_entities, key=lambda x: -x.confidence)[:10]:
                count = e.metadata.get("occurrence_count", 1)
                suffix = f" ({count}x)" if count > 1 else ""
                lines.append(f"- {e.normalized_text} [{e.confidence:.0%}]{suffix}")
            if len(type_entities) > 10:
                lines.append(f"  ... and {len(type_entities) - 10} more")
            lines.append("")

        if relationships:
            lines.append(f"### Relationships ({len(relationships)})")
            for rel in relationships[:20]:
                lines.append(
                    f"- {rel.get('source')} —[{rel.get('relation')}]→ {rel.get('target')} "
                    f"[{rel.get('confidence', 0):.0%}]"
                )
            if len(relationships) > 20:
                lines.append(f"  ... and {len(relationships) - 20} more")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entity linker (cross-document dedup)
# ---------------------------------------------------------------------------


class EntityLinker:
    """Link entities across documents to build a knowledge graph."""

    def __init__(self) -> None:
        self.canonical_entities: dict[str, dict[str, Any]] = {}
        self.links: list[dict[str, Any]] = []

    def add_entities(self, entities: list[ExtractedEntity], source_doc_id: str) -> None:
        for entity in entities:
            canonical_id = self._find_or_create_canonical(entity)
            entity.metadata["canonical_id"] = canonical_id
            entity.metadata["source_doc"] = source_doc_id

    def _find_or_create_canonical(self, entity: ExtractedEntity) -> str:
        normalized = entity.normalized_text.lower().strip()

        for can_id, canonical in self.canonical_entities.items():
            if canonical["type"] != entity.entity_type:
                continue

            if canonical["normalized"].lower() == normalized:
                canonical["aliases"].add(entity.raw_text)
                canonical["confidence"] = max(canonical["confidence"], entity.confidence)
                return can_id

            can_norm = canonical["normalized"].lower()
            if (normalized in can_norm or can_norm in normalized) and (
                len(normalized) > 3 and len(can_norm) > 3
            ):
                canonical["aliases"].add(entity.raw_text)
                return can_id

        import uuid

        can_id = str(uuid.uuid4())
        self.canonical_entities[can_id] = {
            "id": can_id,
            "type": entity.entity_type,
            "normalized": entity.normalized_text,
            "aliases": {entity.raw_text},
            "confidence": entity.confidence,
            "first_seen": entity.metadata.get("source_doc"),
        }
        return can_id

    def link_entities(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        confidence: ConfidenceLevel,
        evidence: str,
    ) -> None:
        self.links.append(
            {
                "source": source_id,
                "target": target_id,
                "relation": relation,
                "confidence": confidence,
                "evidence": evidence,
            }
        )

    def export_graph(self) -> dict[str, Any]:
        return {
            "entities": [
                {
                    "id": e["id"],
                    "type": e["type"].value,
                    "name": e["normalized"],
                    "aliases": list(e["aliases"]),
                    "confidence": e["confidence"],
                }
                for e in self.canonical_entities.values()
            ],
            "links": self.links,
        }
