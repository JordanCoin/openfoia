"""Name normalization for entity dedup.

Uses rigour (``openfoia install-extras match``) when installed: casefolding,
punctuation-insensitive tokens, legal forms across jurisdictions (Ltd/Limited,
S.A./SA, GmbH, OOO) and person honorifics. Without it, a lowercase plus
six-suffix strip that always works.
"""

from __future__ import annotations

from typing import Any

from .models import EntityType

_SUFFIXES = (" inc", " llc", " corp", " ltd", " co", " lp")


def _strip_suffix(key: str) -> str:
    for suffix in _SUFFIXES:
        if key.endswith(suffix):
            return key[: -len(suffix)].rstrip(",. ")
    return key


def dedup_key(name: str, entity_type: Any = None) -> str:
    """Return the key under which two spellings of one entity collide."""
    fallback = _strip_suffix(name.lower().strip().rstrip("."))
    try:
        from rigour.names import normalize_name, remove_org_types, remove_person_prefixes
    except ImportError:
        return fallback

    if entity_type == EntityType.PERSON:
        key = normalize_name(remove_person_prefixes(name)) or ""
    else:
        key = " ".join(remove_org_types(normalize_name(name) or "").split())
    # rigour keeps "co" (it is also a word), so the legacy strip still runs;
    # a name that is only a legal form ("Inc.") keeps the fallback key.
    return _strip_suffix(key) or fallback
