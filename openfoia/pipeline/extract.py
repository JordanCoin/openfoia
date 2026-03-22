"""Entity extraction from documents using LLMs with regex fallback.

Supports three backends:
- ollama  (local, default) - zero-cost, private, works offline
- anthropic - high quality, requires API key
- openai - high quality, requires API key

When no AI provider is reachable, falls back to regex-based extraction
for common patterns (dates, money, emails, phone numbers, addresses).
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
        # ISO dates: 2024-01-15
        re.compile(r'\b(\d{4}-\d{2}-\d{2})\b'),
        # US dates: 01/15/2024, 1/15/24
        re.compile(r'\b(\d{1,2}/\d{1,2}/\d{2,4})\b'),
        # Written dates: January 15, 2024 / Jan 15, 2024 / 15 January 2024
        re.compile(
            r'\b((?:January|February|March|April|May|June|July|August|September|'
            r'October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
            r'\.?\s+\d{1,2},?\s+\d{4})\b',
            re.IGNORECASE,
        ),
        re.compile(
            r'\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|'
            r'September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|'
            r'Oct|Nov|Dec)\.?,?\s+\d{4})\b',
            re.IGNORECASE,
        ),
    ],
    EntityType.MONEY: [
        # $1,234.56  or $1234  or $1.2M  or $1.2 million
        re.compile(r'(\$[\d,]+(?:\.\d{1,2})?(?:\s*(?:million|billion|trillion|[MBTmbt]))?)\b'),
    ],
    EntityType.EMAIL: [
        re.compile(r'\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b'),
    ],
    EntityType.PHONE: [
        # (555) 123-4567, 555-123-4567, +1-555-123-4567, 555.123.4567
        re.compile(r'(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b'),
    ],
    EntityType.DOCUMENT_ID: [
        # Common FOIA tracking patterns: 2024-F-00123, FOIA-2024-001, DOJ-2024-001234
        re.compile(r'\b(\d{4}-[A-Z]-\d{3,6})\b'),
        re.compile(r'\b((?:FOIA|FOI|ATF|DOJ|FBI|CIA|DHS|DOD|DOS|EPA|HHS|USDA)-\d{4}-\d{3,8})\b', re.IGNORECASE),
        # Case numbers
        re.compile(r'\b(Case\s+(?:No\.?|Number|#)\s*:?\s*[\w-]{4,20})\b', re.IGNORECASE),
    ],
    EntityType.ADDRESS: [
        # US street addresses: 123 Main St, 456 Oak Ave Suite 100
        re.compile(
            r'\b(\d{1,6}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*'
            r'\s+(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|Lane|Ln|'
            r'Way|Court|Ct|Circle|Cir|Place|Pl|Terrace|Ter|Highway|Hwy)'
            r'\.?(?:\s*,?\s*(?:Suite|Ste|Apt|Unit|#)\s*\w+)?)\b',
            re.IGNORECASE,
        ),
    ],
}


def _get_context_window(text: str, start: int, end: int, window: int = 80) -> str:
    """Return surrounding text for a match."""
    ctx_start = max(0, start - window)
    ctx_end = min(len(text), end + window)
    ctx = text[ctx_start:ctx_end].replace('\n', ' ').strip()
    if ctx_start > 0:
        ctx = '...' + ctx
    if ctx_end < len(text):
        ctx = ctx + '...'
    return ctx


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

    # Built-in type descriptions
    type_descriptions = {
        "PERSON": "Names of individuals (include titles/roles if mentioned)",
        "ORGANIZATION": "Companies, agencies, departments, groups",
        "LOCATION": "Cities, states, countries, facilities",
        "DATE": "Specific dates or date ranges",
        "MONEY": "Dollar amounts, budgets, costs",
        "DOCUMENT_ID": "Case numbers, file numbers, reference IDs",
        "PHONE": "Phone numbers",
        "EMAIL": "Email addresses",
        "ADDRESS": "Street addresses, mailing addresses",
    }

    type_lines = []
    for t in entity_config.builtin_types:
        desc = type_descriptions.get(t, t)
        type_lines.append(f"- {t}: {desc}")

    # Custom types from config
    for ct in entity_config.custom_types:
        name = ct.get("name", "").upper()
        desc = ct.get("description", name)
        type_lines.append(f"- {name}: {desc}")

    types_block = "\n".join(type_lines)

    suffix = ""
    if entity_config.extraction_prompt_suffix:
        suffix = f"\n\nADDITIONAL INSTRUCTIONS:\n{entity_config.extraction_prompt_suffix}"

    return f"""Analyze this FOIA document excerpt and extract all entities and relationships.

CONTEXT: {context or 'FOIA response document'}
PAGE: {page_number or 'Unknown'}

DOCUMENT TEXT:
{text}

Extract the following entity types:
{types_block}

For each entity, provide:
1. raw_text: Exactly as it appears in the document
2. normalized: Cleaned/standardized version
3. type: Entity type from the list above
4. confidence: 0.0-1.0 based on clarity
5. context: The surrounding sentence for verification

Also identify RELATIONSHIPS between entities:
- "works_for" (person -> organization)
- "located_at" (entity -> location)
- "communicated_with" (person <-> person)
- "mentioned_in" (entity -> document)
- "dated" (event -> date)
- "cost" (item -> money)

Return ONLY valid JSON (no markdown fences, no commentary):
{{
  "entities": [
    {{"raw_text": "...", "normalized": "...", "type": "PERSON", "confidence": 0.9, "context": "..."}}
  ],
  "relationships": [
    {{"source": "...", "target": "...", "relation": "works_for", "evidence": "..."}}
  ]
}}{suffix}"""


# ---------------------------------------------------------------------------
# LLM client helpers
# ---------------------------------------------------------------------------

def _call_ollama(
    prompt: str,
    model: str,
    base_url: str | None,
    temperature: float,
    max_tokens: int,
) -> str:
    """Call Ollama's /api/generate endpoint."""
    import urllib.request
    import urllib.error

    url = (base_url or "http://localhost:11434").rstrip("/")
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }).encode()

    req = urllib.request.Request(
        f"{url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    return body.get("response", "")


def _call_anthropic(
    prompt: str,
    model: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """Call Anthropic Messages API."""
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
    prompt: str,
    model: str,
    api_key: str,
    base_url: str | None,
    temperature: float,
    max_tokens: int,
) -> str:
    """Call OpenAI Chat Completions API."""
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
    """Quick check whether the configured LLM backend is reachable."""
    if provider == "ollama":
        # Probe the Ollama server
        import urllib.request
        import urllib.error
        url = (base_url or "http://localhost:11434").rstrip("/")
        try:
            req = urllib.request.Request(f"{url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception:
            return False
    # Cloud providers need an API key
    return bool(api_key)


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------

class EntityExtractor:
    """Extract entities from document text.

    Uses an LLM (ollama / anthropic / openai) when available, otherwise
    falls back to regex-based extraction so the tool works out of the box
    with zero configuration.
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
        self._use_llm: bool | None = None  # resolved lazily

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract(
        self,
        text: str,
        context: str | None = None,
        page_numbers: list[int] | None = None,
    ) -> ExtractionResult:
        """Extract entities and relationships from *text*."""
        import asyncio

        if self._use_llm is None:
            self._use_llm = _llm_available(self.provider, self.api_key, self.base_url)

        if self._use_llm:
            return await self._extract_with_llm(text, context, page_numbers)
        return self._extract_with_regex(text, page_numbers)

    # ------------------------------------------------------------------
    # LLM path
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
            result = await asyncio.to_thread(
                self._extract_chunk_llm, chunk, context, page_num,
            )
            all_entities.extend(result["entities"])
            all_relationships.extend(result["relationships"])

        merged = self._merge_entities(all_entities)
        summary = self._generate_summary(merged, all_relationships)

        return ExtractionResult(
            entities=merged,
            relationships=all_relationships,
            summary=summary,
            metadata={
                "chunks_processed": len(chunks),
                "total_chars": len(text),
                "model": self.model,
                "provider": self.provider,
                "backend": "llm",
            },
        )

    def _extract_chunk_llm(
        self,
        text: str,
        context: str | None,
        page_number: int | None,
    ) -> dict[str, Any]:
        """Send a single chunk to the LLM and parse the response."""
        prompt = _build_extraction_prompt(text, context, page_number, self.entity_config)

        # Dispatch to the correct backend
        if self.provider == "ollama":
            content = _call_ollama(
                prompt, self.model, self.base_url,
                self.temperature, self.max_tokens,
            )
        elif self.provider == "anthropic":
            content = _call_anthropic(
                prompt, self.model, self.api_key or "",
                self.temperature, self.max_tokens,
            )
        elif self.provider == "openai":
            content = _call_openai(
                prompt, self.model, self.api_key or "",
                self.base_url, self.temperature, self.max_tokens,
            )
        else:
            raise ValueError(f"Unknown AI provider: {self.provider}")

        return self._parse_llm_response(content, page_number)

    def _parse_llm_response(
        self, content: str, page_number: int | None,
    ) -> dict[str, Any]:
        """Parse LLM JSON response into entities + relationships."""
        try:
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = {"entities": [], "relationships": []}
        except json.JSONDecodeError:
            data = {"entities": [], "relationships": []}

        # Build a lookup of valid types (builtins + custom)
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
                # Custom type — store as closest builtin or skip
                continue

            entities.append(ExtractedEntity(
                entity_type=entity_type,
                raw_text=e.get("raw_text", ""),
                normalized_text=e.get("normalized", e.get("raw_text", "")),
                confidence=float(e.get("confidence", 0.5)),
                context=e.get("context", ""),
                page_number=page_number,
            ))

        return {"entities": entities, "relationships": data.get("relationships", [])}

    # ------------------------------------------------------------------
    # Regex fallback path
    # ------------------------------------------------------------------

    def _extract_with_regex(
        self,
        text: str,
        page_numbers: list[int] | None,
    ) -> ExtractionResult:
        """Extract entities using regex patterns — no AI required."""
        entities: list[ExtractedEntity] = []
        seen: set[tuple[str, str]] = set()  # (type, normalized) dedup

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

                    entities.append(ExtractedEntity(
                        entity_type=entity_type,
                        raw_text=raw,
                        normalized_text=normalized,
                        confidence=0.6,  # regex matches are decent but not perfect
                        context=ctx,
                        page_number=page_num,
                    ))

        summary = self._generate_summary(entities, [])

        return ExtractionResult(
            entities=entities,
            relationships=[],
            summary=summary,
            metadata={
                "total_chars": len(text),
                "backend": "regex",
                "note": "No AI provider available; used regex fallback.",
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
        paragraphs = text.split('\n\n')
        current_chunk: list[str] = []
        current_len = 0

        for para in paragraphs:
            para_len = len(para) + 2
            if current_len + para_len > max_chars and current_chunk:
                chunks.append('\n\n'.join(current_chunk))
                current_chunk = []
                current_len = 0
            current_chunk.append(para)
            current_len += para_len

        if current_chunk:
            chunks.append('\n\n'.join(current_chunk))

        return chunks

    def _merge_entities(
        self, entities: list[ExtractedEntity],
    ) -> list[ExtractedEntity]:
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
        """Build a human-readable summary of what was extracted."""
        by_type: dict[EntityType, list[ExtractedEntity]] = {}
        for entity in entities:
            by_type.setdefault(entity.entity_type, []).append(entity)

        lines = ["## Entity Extraction Summary\n"]

        for entity_type, type_entities in by_type.items():
            lines.append(f"### {entity_type.value.title()}s ({len(type_entities)})")
            for e in sorted(type_entities, key=lambda x: -x.confidence)[:10]:
                lines.append(f"- {e.normalized_text} [{e.confidence:.0%}]")
            if len(type_entities) > 10:
                lines.append(f"  ... and {len(type_entities) - 10} more")
            lines.append("")

        if relationships:
            lines.append(f"### Relationships ({len(relationships)})")
            for rel in relationships[:20]:
                lines.append(
                    f"- {rel.get('source')} -> {rel.get('relation')} -> {rel.get('target')}"
                )

        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Entity linker (unchanged from scaffold)
# ---------------------------------------------------------------------------

class EntityLinker:
    """Link entities across documents to build a knowledge graph."""

    def __init__(self) -> None:
        self.canonical_entities: dict[str, dict[str, Any]] = {}
        self.links: list[dict[str, Any]] = []

    def add_entities(self, entities: list[ExtractedEntity], source_doc_id: str) -> None:
        """Add entities from a document to the graph."""
        for entity in entities:
            canonical_id = self._find_or_create_canonical(entity)
            entity.metadata['canonical_id'] = canonical_id
            entity.metadata['source_doc'] = source_doc_id

    def _find_or_create_canonical(self, entity: ExtractedEntity) -> str:
        """Find or create a canonical entity."""
        normalized = entity.normalized_text.lower().strip()

        for can_id, canonical in self.canonical_entities.items():
            if canonical['type'] != entity.entity_type:
                continue

            if canonical['normalized'].lower() == normalized:
                canonical['aliases'].add(entity.raw_text)
                canonical['confidence'] = max(canonical['confidence'], entity.confidence)
                return can_id

            can_norm = canonical['normalized'].lower()
            if normalized in can_norm or can_norm in normalized:
                if len(normalized) > 3 and len(can_norm) > 3:
                    canonical['aliases'].add(entity.raw_text)
                    return can_id

        import uuid
        can_id = str(uuid.uuid4())
        self.canonical_entities[can_id] = {
            'id': can_id,
            'type': entity.entity_type,
            'normalized': entity.normalized_text,
            'aliases': {entity.raw_text},
            'confidence': entity.confidence,
            'first_seen': entity.metadata.get('source_doc'),
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
        """Create a link between two canonical entities."""
        self.links.append({
            'source': source_id,
            'target': target_id,
            'relation': relation,
            'confidence': confidence,
            'evidence': evidence,
        })

    def export_graph(self) -> dict[str, Any]:
        """Export the entity graph."""
        return {
            'entities': [
                {
                    'id': e['id'],
                    'type': e['type'].value,
                    'name': e['normalized'],
                    'aliases': list(e['aliases']),
                    'confidence': e['confidence'],
                }
                for e in self.canonical_entities.values()
            ],
            'links': self.links,
        }
