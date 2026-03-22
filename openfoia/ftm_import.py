"""Import FollowTheMoney JSON-lines into OpenFOIA.

Reads .ftm.json files (from Aleph, OpenAleph, OpenSanctions, ICIJ tools)
and ingests them as local entities in the OpenFOIA database.

This means you can:
1. Export from Aleph → import into OpenFOIA → work offline
2. Download OpenSanctions data → import → crossref locally
3. Receive FtM data from a colleague → import → purge when done
4. Pull from any FtM-compatible tool → analyze locally → unplug the USB
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import EntityType

logger = logging.getLogger(__name__)

# Map FtM schema types back to OpenFOIA entity types
_FTM_SCHEMA_TO_ENTITY_TYPE: dict[str, EntityType] = {
    "Person": EntityType.PERSON,
    "LegalEntity": EntityType.ORGANIZATION,
    "Organization": EntityType.ORGANIZATION,
    "Company": EntityType.ORGANIZATION,
    "PublicBody": EntityType.ORGANIZATION,
    "Address": EntityType.ADDRESS,
    "Email": EntityType.EMAIL,
    "Phone": EntityType.PHONE,
    "Payment": EntityType.MONEY,
    "Document": EntityType.DOCUMENT_ID,
    "Event": EntityType.DATE,
    "Vessel": EntityType.ORGANIZATION,
    "Vehicle": EntityType.ORGANIZATION,
    "Airplane": EntityType.ORGANIZATION,
    "RealEstate": EntityType.LOCATION,
}

# FtM schemas that represent relationships (interstitial entities)
_FTM_RELATIONSHIP_SCHEMAS = {
    "Membership", "Directorship", "Ownership", "Employment",
    "UnknownLink", "Family", "Associate", "Representation",
    "Payment", "Contract", "Succession",
}

# Which FtM properties contain the entity name
_NAME_PROPERTIES = ["name", "title", "summary", "subject", "number", "full"]


def _extract_name(properties: dict[str, list[str]]) -> str:
    """Extract the best name from FtM properties."""
    for prop in _NAME_PROPERTIES:
        values = properties.get(prop, [])
        if values and values[0]:
            return values[0]
    # Fallback: first non-empty property value
    for values in properties.values():
        if values and values[0]:
            return values[0]
    return "Unknown"


def _extract_context(properties: dict[str, list[str]]) -> str:
    """Extract context/notes from FtM properties."""
    for prop in ["notes", "summary", "description", "bodyText"]:
        values = properties.get(prop, [])
        if values and values[0]:
            return values[0][:500]
    return ""


def parse_ftm_file(file_path: Path | str) -> tuple[list[dict], list[dict]]:
    """Parse a FtM JSON-lines file into entities and relationships.

    Returns:
        (entities, relationships) where each is a list of dicts
    """
    file_path = Path(file_path)
    entities: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []

    with open(file_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipped invalid JSON on line %d", line_num)
                continue

            schema = data.get("schema", "")
            properties = data.get("properties", {})
            ftm_id = data.get("id", "")

            if schema in _FTM_RELATIONSHIP_SCHEMAS:
                # This is a relationship entity — extract source/target refs
                source_ref = None
                target_ref = None

                # Different schemas use different property names for endpoints
                for src_prop in ["member", "director", "owner", "payer", "subject", "authority", "emitters"]:
                    vals = properties.get(src_prop, [])
                    if vals:
                        source_ref = vals[0]
                        break

                for tgt_prop in ["organization", "asset", "beneficiary", "object", "contractor", "recipients"]:
                    vals = properties.get(tgt_prop, [])
                    if vals:
                        target_ref = vals[0]
                        break

                if source_ref and target_ref:
                    relationships.append({
                        "ftm_id": ftm_id,
                        "schema": schema,
                        "source_ref": source_ref,
                        "target_ref": target_ref,
                        "properties": properties,
                    })
            else:
                entity_type = _FTM_SCHEMA_TO_ENTITY_TYPE.get(schema)
                if entity_type is None:
                    # Unknown schema — default to organization for legal entities
                    entity_type = EntityType.ORGANIZATION

                name = _extract_name(properties)
                context = _extract_context(properties)

                entities.append({
                    "ftm_id": ftm_id,
                    "schema": schema,
                    "entity_type": entity_type,
                    "name": name,
                    "context": context,
                    "properties": properties,
                })

    return entities, relationships


def import_ftm_to_db(
    file_path: Path | str,
    document_id: str | None = None,
    tag: str | None = None,
) -> tuple[int, int]:
    """Import FtM entities into the OpenFOIA database.

    Creates Entity records for each FtM entity, optionally linked to
    a document. Deduplicates against existing entities by name + type.

    Args:
        file_path: Path to .ftm.json file
        document_id: Optional document ID to associate entities with
        tag: Optional tag for tracking import batches

    Returns:
        (entities_imported, relationships_imported)
    """
    from .db import get_session, get_db_path, init_db
    from .models import Entity

    db_path = get_db_path()
    if not db_path.exists():
        init_db()

    parsed_entities, parsed_relationships = parse_ftm_file(file_path)

    entities_added = 0
    rels_added = 0

    with get_session() as session:
        # Get existing entity names for dedup
        existing = set()
        for row in session.query(Entity.entity_type, Entity.normalized_text).all():
            existing.add((row[0].value, row[1].lower()))

        # Map FtM IDs to our entity IDs for relationship resolution
        ftm_to_local: dict[str, str] = {}

        for ent_data in parsed_entities:
            name = ent_data["name"]
            etype = ent_data["entity_type"]
            key = (etype.value, name.lower())

            if key in existing:
                # Already have this entity — find its ID for relationship mapping
                existing_ent = session.query(Entity).filter(
                    Entity.entity_type == etype,
                    Entity.normalized_text == name,
                ).first()
                if existing_ent:
                    ftm_to_local[ent_data["ftm_id"]] = existing_ent.id
                continue

            entity_id = str(uuid4())
            ftm_to_local[ent_data["ftm_id"]] = entity_id

            entity = Entity(
                id=entity_id,
                document_id=document_id or _get_or_create_import_doc(session, tag),
                entity_type=etype,
                raw_text=name,
                normalized_text=name,
                confidence=0.8,  # imported data, not extracted
                context=ent_data["context"],
                extra_data={
                    "source": "ftm_import",
                    "ftm_schema": ent_data["schema"],
                    "ftm_id": ent_data["ftm_id"],
                    "import_tag": tag,
                },
            )
            session.add(entity)
            existing.add(key)
            entities_added += 1

        # Import relationships as entity_links
        from .models import entity_links
        for rel_data in parsed_relationships:
            source_id = ftm_to_local.get(rel_data["source_ref"])
            target_id = ftm_to_local.get(rel_data["target_ref"])
            if source_id and target_id and source_id != target_id:
                # Check if link already exists
                existing_link = session.query(entity_links).filter(
                    entity_links.c.source_id == source_id,
                    entity_links.c.target_id == target_id,
                ).first()
                if not existing_link:
                    session.execute(entity_links.insert().values(
                        source_id=source_id,
                        target_id=target_id,
                        link_type=rel_data["schema"].lower(),
                    ))
                    rels_added += 1

    return entities_added, rels_added


_IMPORT_DOC_ID: str | None = None


def _get_or_create_import_doc(session: Any, tag: str | None = None) -> str:
    """Get or create a placeholder document for FtM imports."""
    global _IMPORT_DOC_ID

    if _IMPORT_DOC_ID:
        return _IMPORT_DOC_ID

    from .models import Document, DocumentType, Request, User, Agency, RequestStatus, DeliveryMethod

    # Need a request to hang the document on
    user = session.query(User).first()
    if not user:
        user = User(id=str(uuid4()), email="import@openfoia.local", name="FtM Import")
        session.add(user)
        session.flush()

    agency = session.query(Agency).first()

    req = Request(
        id=str(uuid4()),
        request_number=f"IMPORT-{tag or 'FTM'}",
        requester_id=user.id,
        agency_id=agency.id if agency else user.id,
        subject=f"FtM Import{f' ({tag})' if tag else ''}",
        body="Imported from FollowTheMoney data",
        delivery_method=DeliveryMethod.EMAIL,
        status=RequestStatus.DRAFT,
    )
    session.add(req)
    session.flush()

    doc = Document(
        id=str(uuid4()),
        request_id=req.id,
        doc_type=DocumentType.CORRESPONDENCE,
        filename=f"ftm-import{f'-{tag}' if tag else ''}.json",
        file_path="",
        file_size=0,
        mime_type="application/json",
        entities_extracted=True,
    )
    session.add(doc)
    session.flush()

    _IMPORT_DOC_ID = doc.id
    return doc.id
