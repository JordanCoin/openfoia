"""Name normalization for entity dedup. Stdlib only, works offline."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import EntityType

# ponytail: vendored legal forms and honorifics with no transliteration, so
# Cyrillic and Latin spellings of one name ("OOO Romashka") do not merge,
# and forms are stripped, never expanded. Upgrade path: nomenklatura (phase 2).
# Ordered by token count, longest first, so "co ltd" wins over "ltd".
_LEGAL_FORMS = (
    "s a de c v",
    "sp z o o",
    "spol s r o",
    "s de r l",
    "sa de cv",
    "gmbh co kg",
    "public limited company",
    "limited liability company",
    "s p a",
    "s r l",
    "s r o",
    "l l c",
    "l l p",
    "d o o",
    "s a",
    "a s",
    "b v",
    "n v",
    "l p",
    "pte ltd",
    "pty ltd",
    "pvt ltd",
    "sdn bhd",
    "co ltd",
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "plc",
    "corp",
    "corporation",
    "co",
    "company",
    "lp",
    "llp",
    "gmbh",
    "ag",
    "kg",
    "se",
    "sa",
    "sas",
    "sarl",
    "srl",
    "spa",
    "bv",
    "nv",
    "oy",
    "oyj",
    "ab",
    "as",
    "asa",
    "aps",
    "kft",
    "sro",
    "doo",
    "ooo",
    "jsc",
    "pjsc",
    "ltda",
    "cia",
    "kk",
    "bhd",
)
_HONORIFICS = frozenset({"mr", "mrs", "ms", "dr", "prof", "sir", "hon"})
_PUNCT = re.compile(r"[^\w\s]")


def dedup_key(name: str, entity_type: Any = None) -> str:
    """Return the key under which two spellings of one entity collide."""
    tokens = _PUNCT.sub(" ", unicodedata.normalize("NFKC", name).casefold()).split()
    key = " ".join(tokens)
    if entity_type == EntityType.PERSON:
        while tokens and tokens[0] in _HONORIFICS:
            tokens = tokens[1:]
    else:
        for form in _LEGAL_FORMS:
            n = len(form.split())
            if len(tokens) > n and tokens[-n:] == form.split():
                tokens = tokens[:-n]
                break
    return " ".join(tokens) or key
