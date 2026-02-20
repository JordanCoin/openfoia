"""Entity extraction from documents using LLMs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import EntityType, ConfidenceLevel


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


class EntityExtractor:
    """Extract entities from document text using LLMs.
    
    Uses Claude or GPT for high-quality extraction with:
    - Named entity recognition
    - Relationship extraction
    - Coreference resolution
    - Evidence chain building
    """

    def __init__(
        self,
        provider: str = "anthropic",
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
    ):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            if self.provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            elif self.provider == "openai":
                import openai
                self._client = openai.OpenAI(api_key=self.api_key)
            else:
                raise ValueError(f"Unknown provider: {self.provider}")
        return self._client

    async def extract(
        self,
        text: str,
        context: str | None = None,
        page_numbers: list[int] | None = None,
    ) -> ExtractionResult:
        """Extract entities and relationships from text."""
        import asyncio
        
        # Chunk text if too long
        chunks = self._chunk_text(text, max_chars=8000)
        
        all_entities = []
        all_relationships = []
        
        for i, chunk in enumerate(chunks):
            page_num = page_numbers[i] if page_numbers and i < len(page_numbers) else None
            result = await asyncio.to_thread(
                self._extract_chunk,
                chunk,
                context,
                page_num,
            )
            all_entities.extend(result['entities'])
            all_relationships.extend(result['relationships'])
        
        # Deduplicate and merge entities
        merged_entities = self._merge_entities(all_entities)
        
        # Generate summary
        summary = await asyncio.to_thread(
            self._generate_summary,
            merged_entities,
            all_relationships,
        )
        
        return ExtractionResult(
            entities=merged_entities,
            relationships=all_relationships,
            summary=summary,
            metadata={
                "chunks_processed": len(chunks),
                "total_chars": len(text),
                "model": self.model,
            },
        )

    def _extract_chunk(
        self,
        text: str,
        context: str | None,
        page_number: int | None,
    ) -> dict[str, Any]:
        """Extract entities from a single chunk."""
        prompt = f"""Analyze this FOIA document excerpt and extract all entities and relationships.

CONTEXT: {context or 'FOIA response document'}
PAGE: {page_number or 'Unknown'}

DOCUMENT TEXT:
{text}

Extract the following entity types:
- PERSON: Names of individuals (include titles/roles if mentioned)
- ORGANIZATION: Companies, agencies, departments, groups
- LOCATION: Addresses, cities, countries, facilities
- DATE: Specific dates or date ranges
- MONEY: Dollar amounts, budgets, costs
- DOCUMENT_ID: Case numbers, file numbers, reference IDs
- PHONE: Phone numbers
- EMAIL: Email addresses

For each entity, provide:
1. raw_text: Exactly as it appears
2. normalized: Cleaned/standardized version
3. type: Entity type from above
4. confidence: 0.0-1.0 based on clarity
5. context: Surrounding sentence for verification

Also identify RELATIONSHIPS between entities:
- "works_for" (person → organization)
- "located_at" (entity → location)
- "communicated_with" (person ↔ person)
- "mentioned_in" (entity → document)
- "dated" (event → date)
- "cost" (item → money)

Return JSON:
{{
  "entities": [
    {{"raw_text": "...", "normalized": "...", "type": "PERSON", "confidence": 0.9, "context": "..."}}
  ],
  "relationships": [
    {{"source": "...", "target": "...", "relation": "works_for", "evidence": "..."}}
  ]
}}
"""
        
        if self.provider == "anthropic":
            response = self._get_client().messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            content = response.content[0].text
        else:  # openai
            response = self._get_client().chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
        
        # Parse JSON from response
        try:
            # Find JSON in response
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = {"entities": [], "relationships": []}
        except json.JSONDecodeError:
            data = {"entities": [], "relationships": []}
        
        # Convert to ExtractedEntity objects
        entities = []
        for e in data.get('entities', []):
            try:
                entity_type = EntityType(e['type'].upper())
            except ValueError:
                continue
            
            entities.append(ExtractedEntity(
                entity_type=entity_type,
                raw_text=e.get('raw_text', ''),
                normalized_text=e.get('normalized', e.get('raw_text', '')),
                confidence=float(e.get('confidence', 0.5)),
                context=e.get('context', ''),
                page_number=page_number,
            ))
        
        return {
            'entities': entities,
            'relationships': data.get('relationships', []),
        }

    def _chunk_text(self, text: str, max_chars: int = 8000) -> list[str]:
        """Split text into chunks for processing."""
        if len(text) <= max_chars:
            return [text]
        
        chunks = []
        paragraphs = text.split('\n\n')
        current_chunk = []
        current_len = 0
        
        for para in paragraphs:
            para_len = len(para) + 2  # +2 for \n\n
            if current_len + para_len > max_chars and current_chunk:
                chunks.append('\n\n'.join(current_chunk))
                current_chunk = []
                current_len = 0
            current_chunk.append(para)
            current_len += para_len
        
        if current_chunk:
            chunks.append('\n\n'.join(current_chunk))
        
        return chunks

    def _merge_entities(self, entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
        """Merge duplicate entities."""
        # Group by normalized text
        by_normalized: dict[str, list[ExtractedEntity]] = {}
        for entity in entities:
            key = (entity.entity_type, entity.normalized_text.lower())
            if key not in by_normalized:
                by_normalized[key] = []
            by_normalized[key].append(entity)
        
        merged = []
        for key, group in by_normalized.items():
            # Take the one with highest confidence
            best = max(group, key=lambda e: e.confidence)
            # Aggregate metadata
            best.metadata['occurrence_count'] = len(group)
            best.metadata['pages'] = list(set(
                e.page_number for e in group if e.page_number
            ))
            merged.append(best)
        
        return merged

    def _generate_summary(
        self,
        entities: list[ExtractedEntity],
        relationships: list[dict[str, Any]],
    ) -> str:
        """Generate a summary of extracted entities."""
        # Group entities by type
        by_type: dict[EntityType, list[ExtractedEntity]] = {}
        for entity in entities:
            if entity.entity_type not in by_type:
                by_type[entity.entity_type] = []
            by_type[entity.entity_type].append(entity)
        
        lines = ["## Entity Extraction Summary\n"]
        
        for entity_type, type_entities in by_type.items():
            lines.append(f"### {entity_type.value.title()}s ({len(type_entities)})")
            for e in sorted(type_entities, key=lambda x: -x.confidence)[:10]:
                conf_str = f"[{e.confidence:.0%}]"
                lines.append(f"- {e.normalized_text} {conf_str}")
            if len(type_entities) > 10:
                lines.append(f"  ... and {len(type_entities) - 10} more")
            lines.append("")
        
        if relationships:
            lines.append(f"### Relationships ({len(relationships)})")
            for rel in relationships[:20]:
                lines.append(
                    f"- {rel.get('source')} → {rel.get('relation')} → {rel.get('target')}"
                )
        
        return '\n'.join(lines)


class EntityLinker:
    """Link entities across documents to build a knowledge graph."""

    def __init__(self):
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
        # Simple fuzzy matching - in production use better algorithms
        normalized = entity.normalized_text.lower().strip()
        
        # Check existing canonicals
        for can_id, canonical in self.canonical_entities.items():
            if canonical['type'] != entity.entity_type:
                continue
            
            # Exact match
            if canonical['normalized'].lower() == normalized:
                canonical['aliases'].add(entity.raw_text)
                canonical['confidence'] = max(canonical['confidence'], entity.confidence)
                return can_id
            
            # Fuzzy match (simple containment check)
            can_norm = canonical['normalized'].lower()
            if normalized in can_norm or can_norm in normalized:
                if len(normalized) > 3 and len(can_norm) > 3:  # Avoid short matches
                    canonical['aliases'].add(entity.raw_text)
                    return can_id
        
        # Create new canonical
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
