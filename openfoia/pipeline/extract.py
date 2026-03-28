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

from ..config import load_config, OpenFOIAConfig, EntityConfig
from ..models import EntityType, ConfidenceLevel


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
        import gliner  # noqa: F401

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
    """Build the structured prompt for LLM entity extraction."""

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
2. normalized: Cleaned/standardized (e.g., "Dr. John A. Smith" → "John A. Smith")
3. type: From the list above
4. confidence: 0.0-1.0
5. context: The surrounding sentence

Also identify RELATIONSHIPS between entities:
- "affiliated_with" (person → organization)
- "located_at" (entity → location)
- "communicated_with" (person ↔ person)
- "financial_transaction" (entity → money)
- "dated_event" (entity → date)
- "supervised_by" (person → person)
- "contracted_with" (org → org)
- "paid" (entity → money + recipient)
- "sent_to" (document → person/org)

Return ONLY valid JSON:
{{
  "entities": [
    {{"raw_text": "...", "normalized": "...", "type": "PERSON", "confidence": 0.95, "context": "..."}}
  ],
  "relationships": [
    {{"source": "John Smith", "target": "Acme Corp", "relation": "affiliated_with", "confidence": 0.9, "evidence": "..."}}
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
    if provider == "ollama":
        import urllib.request

        url = (base_url or "http://localhost:11434").rstrip("/")
        try:
            req = urllib.request.Request(f"{url}/api/tags", method="GET")
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
        # Cloud providers (anthropic, openai) only used if explicitly configured —
        # we don't silently send document text to external APIs.
        if _llm_available(self.provider, self.api_key, self.base_url):
            if self.provider in ("anthropic", "openai"):
                import sys

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
    ) -> ExtractionResult:
        """Extract entities and relationships from text."""
        import asyncio

        backend = self._resolve_backend()

        if backend == "llm":
            return await self._extract_with_llm(text, context, page_numbers)
        elif backend == "gliner":
            return await asyncio.to_thread(self._extract_with_gliner, text, page_numbers)
        elif backend == "spacy":
            return await asyncio.to_thread(self._extract_with_spacy, text, page_numbers)
        else:
            return self._extract_with_regex(text, page_numbers)

    # ------------------------------------------------------------------
    # LLM path (highest quality)
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
            if normalized in can_norm or can_norm in normalized:
                if len(normalized) > 3 and len(can_norm) > 3:
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
