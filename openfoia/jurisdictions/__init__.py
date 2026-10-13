"""Public-records jurisdictions: one JSON file per law, loaded lazily.

A null field means "unknown", never "none". Entries with verified=false are
stubs waiting for a sourced fill; the CLI says so loudly.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent

LEVELS = ("federal", "state")
BODY_TYPES = ("federal", "state", "county", "city", "school_district", "university", "other")


@lru_cache(maxsize=1)
def load_all() -> dict[str, dict]:
    """Every jurisdiction keyed by id, federal first then states by id."""
    data = {}
    for path in sorted(_DIR.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                j = json.load(fh)
            if j.get("id") != path.stem:
                raise ValueError(f"id {j.get('id')!r} does not match filename")
        except (ValueError, AttributeError) as e:  # json errors are ValueErrors
            raise ValueError(f"jurisdiction file {path.name}: {e}") from e
        data[j["id"]] = j
    return dict(sorted(data.items(), key=lambda kv: (kv[1]["level"] != "federal", kv[0])))


def get(jurisdiction_id: str) -> dict | None:
    return load_all().get(jurisdiction_id.strip().lower())


def search(text: str) -> list[dict]:
    """Jurisdictions whose id, name or statute name contains the text."""
    needle = text.strip().lower()
    return [
        j
        for j in load_all().values()
        if needle in j["id"]
        or needle in j["name"].lower()
        or needle in (j["statute"]["name"] or "").lower()
    ]
