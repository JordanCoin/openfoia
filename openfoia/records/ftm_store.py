"""Write fetched FollowTheMoney entities to a local file and import them.

Aleph returns FollowTheMoney entities, and the Companies House adapter maps a
company, its officers and its owners onto the same model. Both go through
:func:`openfoia.ftm_import.import_ftm_to_db`, the code path ``openfoia import``
uses, so fetched people, companies, directorships and ownerships land in the
entity graph like any other import, attached to the fetched record's document.

Every value is upstream data: schemas, ids and property values are checked and
cleaned here before anything is written, so the stored entities carry no
terminal escapes or bidi overrides. The JSON-lines file is kept under the data
directory (``downloads/ftm/``), where ``purge`` removes it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..db import get_data_dir
from .keyed_api import clean_line

_SCHEMA = re.compile(r"^[A-Z][A-Za-z]{1,40}$")
_PROP = re.compile(r"^[a-z][A-Za-z]{0,60}$")
_FILE_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
MAX_ENTITIES = 1000
MAX_VALUES = 20
MAX_VALUE = 1000


def clean_ftm(entity: Any) -> dict[str, Any] | None:
    """A cleaned ``{"id", "schema", "properties"}`` dict, or None if *entity* is malformed."""
    if not isinstance(entity, dict):
        return None
    ftm_id = clean_line(entity.get("id"), 200)
    schema = entity.get("schema")
    if not ftm_id or not isinstance(schema, str) or not _SCHEMA.match(schema):
        return None
    props: dict[str, list[str]] = {}
    raw = entity.get("properties")
    for key, values in (raw if isinstance(raw, dict) else {}).items():
        if not isinstance(key, str) or not _PROP.match(key):
            continue
        if not isinstance(values, list):
            values = [values]
        kept = []
        for value in values[:MAX_VALUES]:
            if isinstance(value, dict):  # an embedded entity: keep its id as the reference
                value = value.get("id")
            if isinstance(value, str | int | float) and not isinstance(value, bool):
                text = clean_line(value, MAX_VALUE)
                if text:
                    kept.append(text)
        if kept:
            props[key] = kept
    return {"id": ftm_id, "schema": schema, "properties": props}


def import_ftm_entities(
    entities: list[dict[str, Any]], *, name: str, document_id: str | None, tag: str
) -> tuple[int, int]:
    """Clean *entities*, write them to ``downloads/ftm/<name>.ftm.json`` and import them.

    Returns ``(entities_imported, relationships_imported)`` as
    :func:`import_ftm_to_db` counts them (entities already in the database by
    name and type are linked, not duplicated).
    """
    from ..ftm_import import import_ftm_to_db  # lazy: pulls in the ORM

    cleaned: dict[str, dict[str, Any]] = {}
    for entity in entities[:MAX_ENTITIES]:
        c = clean_ftm(entity)
        if c and c["id"] not in cleaned:
            cleaned[c["id"]] = c
    if not cleaned:
        return 0, 0
    out_dir = get_data_dir() / "downloads" / "ftm"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_FILE_SAFE.sub('_', name)[:120] or 'record'}.ftm.json"
    with open(path, "w", encoding="utf-8") as f:
        for c in cleaned.values():
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return import_ftm_to_db(path, document_id=document_id, tag=tag)
