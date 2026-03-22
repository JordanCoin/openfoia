"""FollowTheMoney (FtM) export for interoperability.

Converts OpenFOIA entities to the FollowTheMoney data model used by
Aleph, OpenAleph, OpenSanctions, and ICIJ tools. This makes OpenFOIA
data pluggable into the broader investigative journalism ecosystem.

FtM spec: https://followthemoney.tech
"""

from __future__ import annotations

import json
from typing import Any
from pathlib import Path

from .models import EntityType


# Map OpenFOIA entity types to FtM schema types
_ENTITY_TYPE_TO_FTM_SCHEMA: dict[str, str] = {
    "person": "Person",
    "organization": "Organization",
    "location": "Address",
    "money": "Payment",
    "date": "Event",
    "email": "Email",
    "phone": "Phone",
    "document_id": "Document",
    "address": "Address",
}

# Map OpenFOIA relationship types to FtM interstitial schemas
_RELATION_TO_FTM: dict[str, dict[str, str]] = {
    "affiliated_with": {"schema": "Membership", "source_prop": "member", "target_prop": "organization"},
    "works_for": {"schema": "Membership", "source_prop": "member", "target_prop": "organization"},
    "located_at": {"schema": "Address", "source_prop": "entity", "target_prop": "full"},
    "communicated_with": {"schema": "Email", "source_prop": "emitters", "target_prop": "recipients"},
    "financial_transaction": {"schema": "Payment", "source_prop": "payer", "target_prop": "beneficiary"},
    "associated_amount": {"schema": "Payment", "source_prop": "payer", "target_prop": "amountUsd"},
    "related_to": {"schema": "UnknownLink", "source_prop": "subject", "target_prop": "object"},
    "dated_event": {"schema": "Event", "source_prop": "involved", "target_prop": "date"},
    "supervised_by": {"schema": "Directorship", "source_prop": "director", "target_prop": "organization"},
    "contracted_with": {"schema": "Contract", "source_prop": "authority", "target_prop": "contractor"},
}


def _try_ftm_available() -> bool:
    """Check if followthemoney library is installed."""
    try:
        import followthemoney  # noqa: F401
        return True
    except ImportError:
        return False


def entities_to_ftm_native(entities: list[Any], relationships: list[dict]) -> list[Any]:
    """Convert OpenFOIA entities to native FtM EntityProxy objects.

    Requires: pip install followthemoney
    """
    from followthemoney import model

    ftm_entities = []

    for ent in entities:
        schema_name = _ENTITY_TYPE_TO_FTM_SCHEMA.get(ent.entity_type.value, "LegalEntity")

        try:
            proxy = model.make_entity(schema_name)
        except Exception:
            proxy = model.make_entity("LegalEntity")

        proxy.make_id(ent.normalized_text, ent.entity_type.value)
        proxy.add("name", ent.normalized_text)

        # Add type-specific properties
        if ent.entity_type == EntityType.PERSON:
            proxy.add("name", ent.normalized_text)
        elif ent.entity_type == EntityType.EMAIL:
            try:
                proxy = model.make_entity("Email")
                proxy.make_id(ent.normalized_text)
                proxy.add("subject", ent.normalized_text)
            except Exception:
                pass
        elif ent.entity_type == EntityType.PHONE:
            try:
                proxy = model.make_entity("Phone")
                proxy.make_id(ent.normalized_text)
                proxy.add("number", ent.normalized_text)
            except Exception:
                pass

        if ent.context:
            proxy.add("notes", ent.context[:500])

        ftm_entities.append(proxy)

    return ftm_entities


def entities_to_ftm_json(entities: list[Any], relationships: list[dict]) -> list[dict]:
    """Convert OpenFOIA entities to FtM JSON format (no library needed).

    This produces the same JSON-lines format that Aleph, OpenAleph, and
    OpenSanctions use, without requiring the followthemoney package.
    """
    ftm_lines: list[dict] = []
    entity_id_map: dict[str, str] = {}  # normalized_text -> ftm_id

    for ent in entities:
        schema = _ENTITY_TYPE_TO_FTM_SCHEMA.get(ent.entity_type.value, "LegalEntity")

        # Generate a stable ID from entity text + type
        import hashlib
        ftm_id = hashlib.sha256(
            f"{ent.entity_type.value}:{ent.normalized_text}".encode()
        ).hexdigest()[:32]

        entity_id_map[ent.normalized_text.lower()] = ftm_id

        properties: dict[str, list[str]] = {
            "name": [ent.normalized_text],
        }

        if ent.entity_type == EntityType.PERSON:
            properties["name"] = [ent.normalized_text]
        elif ent.entity_type == EntityType.EMAIL:
            properties["bodyText"] = [ent.normalized_text]
        elif ent.entity_type == EntityType.PHONE:
            properties["number"] = [ent.normalized_text]
        elif ent.entity_type == EntityType.MONEY:
            properties["amountUsd"] = [ent.normalized_text.replace("$", "").replace(",", "")]
        elif ent.entity_type == EntityType.ADDRESS:
            properties["full"] = [ent.normalized_text]

        if ent.context:
            properties["notes"] = [ent.context[:500]]

        ftm_lines.append({
            "id": ftm_id,
            "schema": schema,
            "properties": properties,
        })

    # Convert relationships to FtM interstitial entities
    for rel in relationships:
        source_text = (rel.get("source") or "").lower()
        target_text = (rel.get("target") or "").lower()
        relation = rel.get("relation", "related_to")

        source_id = entity_id_map.get(source_text)
        target_id = entity_id_map.get(target_text)
        if not source_id or not target_id:
            continue

        ftm_rel = _RELATION_TO_FTM.get(relation, _RELATION_TO_FTM["related_to"])

        import hashlib
        rel_id = hashlib.sha256(
            f"{source_id}:{target_id}:{relation}".encode()
        ).hexdigest()[:32]

        properties = {
            ftm_rel["source_prop"]: [source_id],
            ftm_rel["target_prop"]: [target_id],
        }

        if rel.get("evidence"):
            properties["summary"] = [rel["evidence"][:500]]

        ftm_lines.append({
            "id": rel_id,
            "schema": ftm_rel["schema"],
            "properties": properties,
        })

    return ftm_lines


def export_ftm(
    entities: list[Any],
    relationships: list[dict],
    output_path: Path | str,
    use_native: bool = False,
) -> int:
    """Export entities to FollowTheMoney JSON-lines format.

    This is the standard interchange format for investigative data tools.
    Each line is a JSON object representing one entity.

    Compatible with:
    - Aleph / OpenAleph (import via alephclient)
    - OpenSanctions
    - ICIJ tools
    - Maltego (via OCCRP transforms)

    Args:
        entities: OpenFOIA ExtractedEntity objects
        relationships: Relationship dicts from extraction
        output_path: Where to write the .ftm.json file
        use_native: If True and followthemoney is installed, use native library

    Returns:
        Number of FtM entities written
    """
    output_path = Path(output_path)

    if use_native and _try_ftm_available():
        ftm_entities = entities_to_ftm_native(entities, relationships)
        with open(output_path, "w") as f:
            for proxy in ftm_entities:
                f.write(json.dumps(proxy.to_dict()) + "\n")
        return len(ftm_entities)
    else:
        ftm_lines = entities_to_ftm_json(entities, relationships)
        with open(output_path, "w") as f:
            for line in ftm_lines:
                f.write(json.dumps(line) + "\n")
        return len(ftm_lines)
