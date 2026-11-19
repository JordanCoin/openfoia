"""Entity extraction from documents.

Four extraction tiers, tried in order:
1. LLM (ollama / anthropic / openai) — highest quality, extracts relationships
2. GLiNER — local zero-shot NER model, custom entity types, runs on CPU, no API key
3. spaCy — fast local NER for people/orgs/locations + dependency parsing for relationships
4. Regex — zero-config fallback for dates, money, emails, phones, addresses

The first available backend is used. All backends produce the same output format.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import sys
import unicodedata
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

from ..config import EntityConfig, OpenFOIAConfig, load_config
from ..models import ConfidenceLevel, EntityType
from ..termsafe import safe_text

logger = logging.getLogger(__name__)

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

_MAX_MENTION_LEN = 80
# A period after 2+ lowercase letters ends a sentence unless the word is an abbreviation
_SENTENCE_PERIOD = re.compile(r"[a-z]{2}\.$")
_ABBREVIATIONS = _ORG_SUFFIXES | {
    "mr", "mrs", "ms", "dr", "rev", "jr", "sr", "st", "hon", "prof", "gen", "sgt", "lt", "col",
    "capt", "dept", "div", "assn", "bros", "intl", "natl", "govt", "ave", "blvd", "no", "vs",
}  # fmt: skip

# Function words inside names, ignored when judging prose vs. name
_CONNECTORS = frozenset(
    {"and", "of", "the", "de", "du", "da", "del", "la", "le", "van", "von", "y", "for"}
)
# One plausible name token in any script: "U.S.", "AT&T", "O'Brien", "Smith-Jones", "Müller"
_NAME_TOKEN = re.compile(r"^[^\W_]+(?:[.&'\u2019\-][^\W_]+)*\.?['\u2019]?$")

# The checks below measure names in any script, not only Latin. Three things differ:
# - Han, kana and hangul carry a syllable or a word in one character, so a two-character
#   Chinese or Korean name is a whole name. Those characters count as two.
# - Indic, Thai and Arabic write vowels and harakat as combining marks. They are part of
#   the word (`\w` does not match them), not punctuation.
# - Arabic, Hebrew, CJK, Indic and Thai have no capital letters, and Georgian is
#   written in lowercase Mkhedruli, so "no capital letter" means nothing there.
_ZERO_WIDTH_JOINERS = "\u200c\u200d"  # ZWNJ/ZWJ, inside Persian and Indic words


def _is_wide(c: str) -> bool:
    """Han, kana or hangul: one character is a syllable or a word."""
    return (
        "\u3040" <= c <= "\u30ff"  # hiragana, katakana
        or "\u3400" <= c <= "\u4dbf"  # CJK extension A
        or "\u4e00" <= c <= "\u9fff"  # CJK unified ideographs
        or "\uac00" <= c <= "\ud7af"  # hangul syllables
        or "\uf900" <= c <= "\ufaff"  # CJK compatibility ideographs
        or "\uff66" <= c <= "\uff9f"  # halfwidth katakana
        or "\U00020000" <= c <= "\U0003134f"  # CJK extensions B and later
    )


def _is_mark(c: str) -> bool:
    return unicodedata.category(c)[0] == "M" or c in _ZERO_WIDTH_JOINERS


def _width(s: str) -> int:
    """len(s), with wide characters counted twice."""
    return sum(2 if _is_wide(c) else 1 for c in s)


def _letter_count(s: str) -> int:
    """Letters and combining marks, with wide characters counted twice."""
    return sum(2 if _is_wide(c) else 1 for c in s if c.isalpha() or _is_mark(c))


def _strip_marks(w: str) -> str:
    return "".join(c for c in w if not _is_mark(c))


def _is_lowercase_letter(c: str) -> bool:
    """A lowercase letter in a script that has capitals (Georgian Mkhedruli does not count)."""
    return c.islower() and not "\u10d0" <= c <= "\u10ff"


def _lacks_capital(s: str) -> bool:
    """Written in a cased script but with no capital letter anywhere."""
    cased = [c for c in s if c.isupper() or _is_lowercase_letter(c)]
    return bool(cased) and not any(c.isupper() for c in cased)


def _punctuation_ratio(s: str) -> float:
    """Share of punctuation and symbols; combining marks are letters, not punctuation."""
    return sum(1 for c in s if unicodedata.category(c)[0] in "PS") / len(s)


def _surface_clean(text: str) -> str:
    """Normalize whitespace and strip trailing punctuation."""
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
    if m.entity_type in _NER_TYPES and (_width(s) < 3 or _letter_count(s) <= 2):
        return 0.0

    # Names are short; anything this long is a sentence or a run-on line
    if len(s) > _MAX_MENTION_LEN:
        return 0.0

    words = s.split()
    sentence_ends = [
        i
        for i, w in enumerate(words)
        if _SENTENCE_PERIOD.search(w) and w.rstrip(".").lower() not in _ABBREVIATIONS
    ]
    if len(sentence_ends) > 1 or (sentence_ends == [len(words) - 1] and len(words) >= 6):
        return 0.0
    # Six or more words that are mostly lowercase is prose, not a name, even
    # when the only period sits on an abbreviation ("... by Acme Inc.").
    if m.entity_type in _NER_TYPES and len(words) >= 6:
        content = [w for w in words if w.lower() not in _CONNECTORS and w != "&"]
        if content and sum(_is_lowercase_letter(w[:1]) for w in content) * 2 > len(content):
            return 0.0

    garbled = 0
    if m.entity_type in _NER_TYPES:
        garbled = sum(
            1 for w in words if w != "&" and not _NAME_TOKEN.match(_strip_marks(w.strip(',;:()"')))
        )
        if garbled >= 2:
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
    # No capital letter at all, in a script that has them, is suspicious
    if m.entity_type in _NER_TYPES and _lacks_capital(s):
        base -= 0.35
    # Single short word (non-acronym)
    if m.entity_type in _NER_TYPES and len(words) == 1 and _width(s) <= 3:
        base -= 0.45
    if garbled:
        base -= 0.30
    if m.entity_type in _NER_TYPES and _punctuation_ratio(s) > 0.25:
        base -= 0.50
    if (
        m.entity_type == EntityType.PERSON
        and sum(w.rstrip(".").lower() not in _ABBREVIATIONS for w in words) > 5
    ):
        base -= 0.40

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
_GLINER2_MODEL = None


@dataclass(frozen=True)
class NerModelSpec:
    """A tier-2 model the user can choose with ``entities.ner_model``.

    Sizes are measured, not taken from model cards: docs/benchmarks/gliner2.md.
    """

    key: str  # the entities.ner_model value
    model_id: str  # Hugging Face repo id, recorded on each extraction run
    package: str  # import name of the optional extra
    extra: str  # openfoia install-extras <extra>
    download: str  # first download, everything the loader fetches
    ram: str  # peak RSS of a process extracting with it


NER_MODELS = {
    "english": NerModelSpec(
        key="english",
        model_id="urchade/gliner_medium-v2.1",
        package="gliner",
        extra="ner",
        download="about 1.6 GB",
        ram="about 2.0 GB",
    ),
    "multilingual": NerModelSpec(
        key="multilingual",
        model_id="fastino/gliner2-multi-v1",
        package="gliner2",
        extra="ner-multilingual",
        download="about 1.25 GB",
        ram="about 3.0 GB",
    ),
}
DEFAULT_NER_MODEL = "english"
#: The default tier-2 model (kept for callers that predate the choice).
GLINER_MODEL_ID = NER_MODELS[DEFAULT_NER_MODEL].model_id


def _hf_cache_dir() -> str | None:
    """Keep Hugging Face downloads on the stick in portable mode.

    The CLI does this at startup; the agent, MCP and web entry points do not,
    so the loaders repeat it before importing huggingface_hub (which reads
    HF_HOME at import). Returns the hub cache to pass explicitly in portable
    mode, None otherwise (the library default).
    """
    from ..config import confine_caches_to_data_dir, portable_data_dir  # lazy: no cycle

    confine_caches_to_data_dir()
    if portable_data_dir() is None:
        return None
    return os.environ.get("HF_HUB_CACHE") or os.path.join(os.environ["HF_HOME"], "hub")


def _download_notice(spec: NerModelSpec) -> str:
    return (
        f"{spec.model_id} is not cached; downloading {spec.download} from huggingface.co "
        "(one time, not through Tor; your IP is visible to Hugging Face)"
    )


def _get_gliner():
    """Lazily load the English GLiNER model (downloads about 1.6 GB on first use).

    Loads from the local Hugging Face cache first so a cached model never
    contacts huggingface.co (principle 1). Only if the cache is missing do we
    warn and download.
    """
    global _GLINER_MODEL
    if _GLINER_MODEL is None:
        _hf_cache_dir()
        from gliner import GLiNER  # optional: ner
        from huggingface_hub import constants as hf_constants  # optional: ner

        spec = NER_MODELS["english"]
        # ponytail: gliner ignores local_files_only for its tokenizer backbone,
        # so flip the hub-wide offline flag instead of threading kwargs through.
        was_offline = hf_constants.HF_HUB_OFFLINE
        hf_constants.HF_HUB_OFFLINE = True
        try:
            _GLINER_MODEL = GLiNER.from_pretrained(spec.model_id, local_files_only=True)
        except Exception:
            if was_offline:
                raise
            logger.warning(_download_notice(spec))
            hf_constants.HF_HUB_OFFLINE = False
            _GLINER_MODEL = GLiNER.from_pretrained(spec.model_id)
        finally:
            hf_constants.HF_HUB_OFFLINE = was_offline
    return _GLINER_MODEL


class Gliner2Adapter:
    """A GLiNER2 model behind GLiNER's ``predict_entities``.

    The windowing, labels, threshold, label mapping, scoring and merge of the
    GLiNER tier then run unchanged. ``errors`` counts windows that raised
    (``_mentions_from_gliner`` skips them).
    """

    def __init__(self, model: Any):
        self.model, self.errors = model, 0

    def predict_entities(
        self, text: str, labels: list[str], threshold: float = 0.5
    ) -> list[dict[str, Any]]:
        try:
            out = self.model.extract_entities(
                text, labels, threshold=threshold, include_confidence=True, include_spans=True
            )
        except Exception:
            self.errors += 1
            raise
        return [
            {
                "text": e["text"],
                "start": e["start"],
                "end": e["end"],
                "label": label,
                "score": e["confidence"],
            }
            for label, ents in out["entities"].items()
            for e in ents
        ]


def _get_gliner2() -> Gliner2Adapter:
    """Lazily load the multilingual GLiNER2 model (about 1.25 GB on first use).

    Same contract as _get_gliner: the local cache first, and a download only
    after a warning when the cache is missing (never in HF offline mode).
    Only the local model is used; gliner2's hosted API client is not.
    """
    global _GLINER2_MODEL
    if _GLINER2_MODEL is None:
        cache_dir = _hf_cache_dir()
        from gliner2 import AutoExtractor  # optional: ner-multilingual
        from huggingface_hub import constants as hf_constants  # optional: ner-multilingual

        spec = NER_MODELS["multilingual"]
        kwargs: dict[str, Any] = {"map_location": "cpu"}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        # The tokenizer is loaded without local_files_only, as in _get_gliner.
        was_offline = hf_constants.HF_HUB_OFFLINE
        hf_constants.HF_HUB_OFFLINE = True
        try:
            model = AutoExtractor.from_pretrained(spec.model_id, local_files_only=True, **kwargs)
        except Exception:
            if was_offline:
                raise
            logger.warning(_download_notice(spec))
            hf_constants.HF_HUB_OFFLINE = False
            model = AutoExtractor.from_pretrained(spec.model_id, **kwargs)
        finally:
            hf_constants.HF_HUB_OFFLINE = was_offline
        _GLINER2_MODEL = Gliner2Adapter(model)
    return _GLINER2_MODEL


def _gliner_available() -> bool:
    """Check if GLiNER is installed."""
    try:
        import gliner  # noqa: F401 - availability probe for the optional extra

        return True
    except ImportError:
        return False


def _gliner2_available() -> bool:
    """Check if the local GLiNER2 runtime is installed (gliner2[local], with torch)."""
    try:
        import gliner2  # noqa: F401 - availability probe for the optional extra

        return True
    except Exception:  # ImportError, or gliner2 installed without its torch stack
        return False


def _ner_model_available(key: str) -> bool:
    """Is the package for tier-2 model *key* installed? Loads no model."""
    return _gliner2_available() if key == "multilingual" else _gliner_available()


def _get_ner_model(key: str) -> Any:
    """The loaded tier-2 model for *key* (see NER_MODELS)."""
    return _get_gliner2() if key == "multilingual" else _get_gliner()


# ---------------------------------------------------------------------------
# "This document is not in English" hint
# ---------------------------------------------------------------------------

#: Readable names for the first word of unicodedata.name() of a letter.
_SCRIPT_LABELS = {
    "CJK": "Han (Chinese/Japanese)",
    "HIRAGANA": "Japanese kana",
    "KATAKANA": "Japanese kana",
    "HANGUL": "Hangul (Korean)",
}
#: Frequent English function words, and frequent function words of languages
#: written in Latin script that the English model handles badly (Spanish,
#: Portuguese, French, Turkish, Swahili, Indonesian, German, Italian). Words
#: that are also common English words ("a", "in", "on", "per") are left out.
_ENGLISH_WORDS = frozenset({
    "the", "of", "and", "to", "is", "was", "for", "that", "with", "by", "from", "this",
    "are", "were", "has", "have", "had", "which", "be", "been", "it", "its", "an", "at",
    "as", "or", "not", "will", "would", "shall", "their", "they", "he", "she", "his", "her",
})  # fmt: skip
_OTHER_LATIN_WORDS = frozenset({
    # es / pt / fr
    "el", "los", "las", "del", "por", "con", "una", "para", "que", "se", "su", "al",
    "como", "más", "fue", "entre",
    "os", "das", "dos", "não", "uma", "com", "ao", "pela", "pelo", "foi", "em", "são", "à",
    "les", "des", "une", "est", "et", "du", "pour", "dans", "sur", "au", "aux", "qui",
    "pas", "été", "avec",
    # tr / sw / id
    "ve", "bir", "bu", "için", "ile", "olarak", "olan", "gibi", "daha", "çok",
    "tarafından",  # noqa: RUF001 - Turkish dotless i
    "na", "ya", "wa", "kwa", "za", "katika", "ni", "kuwa", "cha", "hiyo", "pia",
    "dan", "yang", "di", "ini", "itu", "dengan", "untuk", "dari", "dalam", "pada",
    "tidak", "akan", "oleh",
    # de / it
    "der", "die", "und", "nicht", "mit", "il", "della", "che", "non", "sono",
})  # fmt: skip
_WORD = re.compile(r"[^\W\d_]+")
#: Below this many letters (wide ones count twice) or words a text is too short to judge.
_HINT_MIN_LETTERS = 40
_HINT_MIN_WORDS = 30
_hint_shown = False


def _letter_script(c: str) -> str:
    if c < "\u0250":  # ASCII, Latin-1, Latin Extended-A/B
        return "LATIN"
    name = unicodedata.name(c, "")
    return name.split(" ", 1)[0] if name else "UNKNOWN"


def non_english_reason(text: str) -> str | None:
    """Why *text* looks like it is not in English, or None.

    Two cheap checks, no model and no network: at least 30% of the letters
    are in a non-Latin script (Han, kana and hangul count twice, as in
    _letter_count), or the text is in Latin script but its function words are
    those of another language rather than English. A heuristic for a hint,
    not a language identifier.
    """
    scripts: Counter[str] = Counter()
    for c in text:
        if c.isalpha():
            scripts[_letter_script(c)] += 2 if _is_wide(c) else 1
    total = sum(scripts.values())
    if total < _HINT_MIN_LETTERS:
        return None
    other = {k: v for k, v in scripts.items() if k != "LATIN"}
    if sum(other.values()) >= 0.3 * total:
        top = max(other, key=lambda k: other[k])
        return f"is mostly in {_SCRIPT_LABELS.get(top, top.title())} script"

    words = _WORD.findall(text.lower())
    if len(words) < _HINT_MIN_WORDS:
        return None
    english = sum(1 for w in words if w in _ENGLISH_WORDS)
    foreign = sum(1 for w in words if w in _OTHER_LATIN_WORDS)
    if foreign >= 0.08 * len(words) and english * 4 < foreign:
        return "looks like it is not in English"
    return None


def multilingual_hint(text: str, ner_model: str) -> str | None:
    """The suggestion to switch to the multilingual model, or None.

    Only for the English model, and only text analysis: it never loads,
    switches or downloads anything.
    """
    if ner_model != "english":
        return None
    reason = non_english_reason(text)
    if reason is None:
        return None
    english, multi = NER_MODELS["english"], NER_MODELS["multilingual"]
    return (
        f"This document {reason}. The English NER model ({english.model_id}) misses "
        f"many names outside English. The multilingual model ({multi.model_id}) finds "
        f"more; it downloads {multi.download} once from huggingface.co and uses "
        f"{multi.ram} of RAM while extracting ({english.ram} for the English one). "
        f"To use it: openfoia install-extras {multi.extra}, then pass --ner-model "
        'multilingual, set OPENFOIA_NER_MODEL=multilingual, or put "ner_model": '
        '"multilingual" under "entities" in config.json. Nothing was switched or '
        "downloaded. See docs/GUIDE.md, Documents not in English."
    )


#: GLiNER truncates input past 384 words (its own word splitter, below).
#: Windows stay well under that so nothing is silently dropped.
GLINER_MAX_WORDS = 300

_GLINER_WORD = re.compile(r"\w+(?:[-_]\w+)*|\S")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n\s*\n")


def _gliner_windows(
    text: str, max_words: int = GLINER_MAX_WORDS, overlap: int = 1
) -> list[tuple[int, str]]:
    """Split *text* into sentence-aligned windows of at most *max_words* words.

    Returns (offset, window) pairs where window == text[offset:offset+len(window)],
    so a model's character offsets map back by adding offset. Consecutive
    windows share *overlap* trailing sentences so an entity on a boundary is
    seen whole at least once. A single sentence longer than the budget is
    split on word boundaries.
    """
    # (start, end, words) per sentence, with over-long sentences pre-split
    pieces: list[tuple[int, int, int]] = []
    pos = 0
    for m in [*_SENTENCE_END.finditer(text), None]:
        end = m.start() if m else len(text)
        words = [w.span() for w in _GLINER_WORD.finditer(text, pos, end)]
        for i in range(0, len(words), max_words):
            chunk = words[i : i + max_words]
            pieces.append((chunk[0][0], chunk[-1][1], len(chunk)))
        pos = m.end() if m else len(text)

    windows: list[tuple[int, str]] = []
    i = 0
    while i < len(pieces):
        j, count = i, 0
        while j < len(pieces) and count + pieces[j][2] <= max_words:
            count += pieces[j][2]
            j += 1
        start, end = pieces[i][0], pieces[j - 1][1]
        windows.append((start, text[start:end]))
        if j >= len(pieces):
            break
        i = max(i + 1, j - overlap)
    return windows


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
        import spacy  # optional: ner-spacy

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


def spacy_model_name() -> str | None:
    """Name and version of the loaded spaCy pipeline, or None if none was loaded."""
    if _SPACY_NLP is None:
        return None
    meta = getattr(_SPACY_NLP, "meta", {}) or {}
    name = f"{meta.get('lang', '')}_{meta.get('name', '')}".strip("_") or "unknown"
    return f"{name} {meta['version']}" if meta.get("version") else name


def _spacy_available() -> bool:
    """Check if spaCy is installed with an English model."""
    try:
        import spacy  # optional: ner-spacy

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


# "8b", "1.5b", "270m"; the lookbehind skips MoE tags like "8x7b"
_PARAM_COUNT = re.compile(r"(?<![\dx.])(\d+(?:\.\d+)?)([bm])\b", re.IGNORECASE)
_KNOWN_SMALL_MODELS = ("tinyllama", "smollm", "phi3", "phi4-mini", "llama3.2")
# qwen3:8b runs away on the full prompt (truncated JSON even with think off and 8000 tokens).
# Matched anywhere in the name so "hf.co/unsloth/Qwen3-8B-GGUF" counts too.
_COMPACT_FAMILIES = ("qwen3",)
_COMPACT_TYPES = ("PERSON", "ORGANIZATION", "LOCATION", "DATE", "MONEY")


def _is_compact_model(model: str | None, setting: str | bool = "auto") -> bool:
    """Whether to use the compact prompt. setting is ai.compact_prompt: auto, true or false.

    auto: models under 8B parameters and the qwen3 family; unknown sizes get the full prompt.
    """
    choice = str(setting).lower()
    if choice in ("true", "false"):
        return choice == "true"
    name = (model or "").lower()
    if any(f in name for f in _COMPACT_FAMILIES):
        return True
    m = _PARAM_COUNT.search(name)
    if m:
        return m.group(2) == "m" or float(m.group(1)) < 8
    return any(tag in name for tag in _KNOWN_SMALL_MODELS)


def _strip_think(text: str) -> str:
    """Drop <think>...</think> blocks that reasoning models emit before their JSON."""
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def _build_extraction_prompt(
    text: str,
    context: str | None,
    page_number: int | None,
    entity_config: EntityConfig,
    compact: bool = False,
) -> str:
    """Build the structured prompt for LLM entity extraction.

    compact=True (models of 8B or fewer, see _is_compact_model) asks for the
    core entity types only, with no relationship section, because small
    models lose the JSON format under long instructions.
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

    builtin = entity_config.builtin_types
    if compact:
        builtin = [t for t in builtin if t in _COMPACT_TYPES]

    type_lines = []
    for t in builtin:
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

    if compact:
        return f"""Extract EVERY named entity from this FOIA document. Be thorough.

TEXT:
{text}

Types:
{types_block}

Return ONLY JSON:
{{"entities": [{{"raw_text": "...", "normalized": "...", "type": "PERSON", "confidence": 0.9, "context": "..."}}]}}{suffix}"""

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
    return _strip_think(body.get("response", ""))


def _call_anthropic(
    prompt: str, model: str, api_key: str, temperature: float, max_tokens: int
) -> str:
    import anthropic  # optional: cloud-ai

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
    import openai  # optional: cloud-ai

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
    return _strip_think(response.choices[0].message.content or "")


def _llm_available(provider: str, api_key: str | None, base_url: str | None) -> bool:
    if not provider or provider == "none":
        return False

    if provider == "ollama":
        url = (base_url or "http://localhost:11434").rstrip("/")
        try:
            req = urllib.request.Request(f"{url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception:
            return False

    if provider == "openai" and base_url and is_loopback_url(base_url):
        # Local OpenAI-compatible server — check if it's actually reachable
        try:
            url = base_url.rstrip("/")
            req = urllib.request.Request(f"{url}/models", method="GET")
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception:
            return False

    return bool(api_key)


# ---------------------------------------------------------------------------
# Where document text goes: this machine, or somewhere else
# ---------------------------------------------------------------------------


def is_loopback_url(url: str | None) -> bool:
    """True only when *url*'s host is this machine.

    That is the name ``localhost`` (exactly, trailing dot allowed) or a loopback
    IP literal (127.0.0.0/8, ``[::1]``). A substring test is not enough:
    ``https://localhost.attacker.example/`` is somebody else's server.
    Anything unparseable counts as not local.
    """
    if not url:
        return False
    try:
        host = urlparse(url.strip()).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def llm_endpoint(provider: str | None, base_url: str | None) -> str | None:
    """The URL *provider* sends prompts to, or None when no LLM is configured."""
    if not provider or provider == "none":
        return None
    if provider == "ollama":
        return base_url or "http://localhost:11434"
    if provider == "anthropic":
        # _call_anthropic does not pass base_url; the SDK reads ANTHROPIC_BASE_URL.
        return os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    if provider == "openai":
        return base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    return base_url


def remote_llm_host(provider: str | None, base_url: str | None) -> str | None:
    """The host off this machine that document text would be sent to, or None if local."""
    url = llm_endpoint(provider, base_url)
    if url is None or is_loopback_url(url):
        return None
    try:
        return urlparse(url.strip()).hostname or url
    except ValueError:
        return url


def remote_llm_refusal(
    provider: str | None, base_url: str | None, *, allow_remote: bool, tor: bool
) -> str | None:
    """Why text must not go to the configured LLM, or None when it may (or it is local).

    The one consent rule for every caller (CLI, agent tool, web UI): an LLM
    that is not on this machine is used only with ``ai.allow_remote`` (config
    or OPENFOIA_AI_ALLOW_REMOTE) or the user's confirmation for this run,
    and never in Tor mode, because these client libraries cannot be routed
    through Tor and the request would leave from the user's real IP.
    """
    host = remote_llm_host(provider, base_url)
    if host is None:
        return None
    if tor:
        return (
            f"WARNING: AI provider '{provider}' at {host} is not on this machine, and Tor "
            "mode is on. OpenFOIA cannot route AI requests through Tor, so it is not "
            f"sending any document text to {host}. Use a model on this machine (Ollama at "
            "localhost), or turn Tor off for this run (OPENFOIA_TOR=0)."
        )
    if not allow_remote:
        return (
            f"WARNING: AI provider '{provider}' at {host} is not on this machine. "
            f"Not sending document text to {host} without your consent: confirm when "
            'the CLI asks, or set "ai": {"allow_remote": true} in config.json '
            "(or OPENFOIA_AI_ALLOW_REMOTE=1). Using local extraction instead."
        )
    return None


def remote_llm_warning(provider: str | None, base_url: str | None) -> str | None:
    """The warning printed before text goes to an LLM off this machine, or None if local."""
    host = remote_llm_host(provider, base_url)
    if host is None:
        return None
    return (
        f"WARNING: AI provider '{provider}' at {host} is not on this machine. Text from "
        f"your documents will be sent to {host}, from your real IP address, and you "
        "cannot control how long it is kept there."
    )


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------

#: Prompt the LLM validator sends; its SHA-256 is recorded on LLM extraction runs.
LLM_VALIDATION_PROMPT = """Review this entity list extracted from a government document.

TASKS:
1. Remove junk entries (boilerplate like "To:", "APPROVED", job titles, sentence fragments)
2. Flag OCR errors in names (e.g. "KIRKLAND 8. ELLIS" should be "Kirkland & Ellis")
3. Mark low-quality entries with confidence 0.1
4. Keep all legitimate people, organizations, locations, dates, money amounts

ENTITIES:
{entity_list}

Return JSON: {{"keep": [{{"raw_text": "...", "confidence": 0.95, "corrected": "..."}}], "remove": ["junk entry 1", "junk entry 2"]}}"""

TIERS = ("gliner", "llm", "spacy", "regex")


class BackendUnavailableError(RuntimeError):
    """A forced extraction tier is not installed or not reachable."""


class RemoteLLMNotAllowedError(BackendUnavailableError):
    """The configured LLM is off this machine and sending to it was not allowed."""


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
        tier: str | None = None,
        allow_remote: bool | None = None,
        ner_model: str | None = None,
    ):
        """*tier* forces one backend (see TIERS) instead of the best available.

        regex/spacy/gliner never call an LLM; llm runs regex + the best local
        NER, then the LLM validator, and fails if no LLM is reachable.

        *allow_remote* is consent to send text to an LLM that is not on this
        machine (None: config ``ai.allow_remote``). It has no effect in Tor
        mode, where a remote LLM is never used; see remote_llm_refusal.

        *ner_model* picks the tier-2 model, a key of NER_MODELS (None: config
        ``entities.ner_model``, default "english").
        """
        if tier is not None and tier not in TIERS:
            raise ValueError(f"Unknown tier {tier!r}; choose from {', '.join(TIERS)}")
        if ner_model is not None and ner_model not in NER_MODELS:
            raise ValueError(
                f"Unknown NER model {ner_model!r}; choose from {', '.join(NER_MODELS)}"
            )
        self.tier = tier
        cfg = config or load_config()
        self.ner_model = ner_model or getattr(cfg.entities, "ner_model", DEFAULT_NER_MODEL)
        if self.ner_model not in NER_MODELS:
            self.ner_model = DEFAULT_NER_MODEL
        self._ner_fallback_shown = False
        self._tier2_ran = False
        self.provider = provider or cfg.ai.provider
        self.model = model or cfg.ai.model
        self.api_key = api_key or cfg.ai.api_key
        self.base_url = base_url or cfg.ai.base_url
        self.temperature = cfg.ai.extraction_temperature
        self.max_tokens = cfg.ai.extraction_max_tokens
        self.compact_prompt = cfg.ai.compact_prompt
        self.entity_config = cfg.entities
        self.allow_remote = cfg.ai.allow_remote if allow_remote is None else allow_remote
        self.tor = cfg.network.tor
        self._backend: str | None = None  # resolved lazily
        self._refusal: str | None = None
        self._consent_checked = False

    def _llm_send_allowed(self) -> bool:
        """May text go to the configured LLM? Warns (once) whenever it is off this machine."""
        if not self._consent_checked:
            self._consent_checked = True
            self._refusal = remote_llm_refusal(
                self.provider, self.base_url, allow_remote=self.allow_remote, tor=self.tor
            )
            message = self._refusal or remote_llm_warning(self.provider, self.base_url)
            if message:
                print(message, file=sys.stderr)
        return self._refusal is None

    def _require_llm_consent(self) -> None:
        """Raise before any request that would carry text to an LLM it may not reach."""
        if not self._llm_send_allowed():
            raise RemoteLLMNotAllowedError(self._refusal or "remote AI provider not allowed")

    def _llm_ready(self) -> bool:
        """Allowed and reachable. Never probes a remote host before consent."""
        return self._llm_send_allowed() and _llm_available(
            self.provider, self.api_key, self.base_url
        )

    @property
    def ner_spec(self) -> NerModelSpec:
        return NER_MODELS[self.ner_model]

    @property
    def ner_model_id(self) -> str:
        """The Hugging Face id of the configured tier-2 model."""
        return self.ner_spec.model_id

    def _ner_unavailable_message(self) -> str:
        spec = self.ner_spec
        if spec.key == "english":
            return f"GLiNER is not installed: openfoia install-extras {spec.extra}"
        return (
            f"The {spec.key} NER model ({spec.model_id}) is selected, but its package "
            f"({spec.package}) is not installed: openfoia install-extras {spec.extra}"
        )

    def _tier2_available(self) -> bool:
        """Is the configured tier-2 model's package installed? Loads no model.

        A missing multilingual extra is reported once per extractor, and the
        next tier (spaCy, then regex) is used. The English model stays as
        before: no message here (the CLI explains the regex-only fallback).
        """
        if _ner_model_available(self.ner_model):
            return True
        if self.ner_model != DEFAULT_NER_MODEL and not self._ner_fallback_shown:
            self._ner_fallback_shown = True
            print(
                f"{self._ner_unavailable_message()}. Using the next tier (spaCy or "
                "regex) instead, which finds fewer names outside English.",
                file=sys.stderr,
            )
        return False

    def _resolve_backend(self) -> str:
        """Determine which extraction backend to use."""
        if self._backend is not None:
            return self._backend

        # Try in order of quality.
        if self._llm_ready():
            # Off-machine providers were warned about in _llm_send_allowed.
            local = remote_llm_host(self.provider, self.base_url) is None
            if self.provider in ("anthropic", "openai") and local:
                print(
                    f"Using local AI provider ({self.model} via {self.base_url})",
                    file=sys.stderr,
                )
            self._backend = "llm"
        elif self._tier2_available():
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
        global _hint_shown
        tier = self.tier
        self._tier2_ran = False
        if tier == "gliner" and not _ner_model_available(self.ner_model):
            raise BackendUnavailableError(self._ner_unavailable_message())
        if tier == "spacy" and not _spacy_available():
            raise BackendUnavailableError(
                "spaCy or its English model is not installed: openfoia install-extras ner-spacy"
            )
        if tier == "llm" and not self._llm_send_allowed():
            raise RemoteLLMNotAllowedError(self._refusal or "remote AI provider not allowed")
        if tier == "llm" and not _llm_available(self.provider, self.api_key, self.base_url):
            raise BackendUnavailableError(
                f"No LLM reachable for provider '{self.provider}'. Start Ollama "
                "(ollama serve) or configure a provider: openfoia config --init"
            )
        backend = self._resolve_backend() if tier in (None, "llm") else tier

        # --- Phase 1: Fast extraction (NER + regex) ---
        all_mentions: list[Mention] = []

        # Regex always runs (instant, zero cost)
        all_mentions.extend(self._mentions_from_regex(text, page_numbers))

        if ensemble:
            # Run all available NER backends
            if self._tier2_available():
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
            if backend in ("gliner", "llm") and self._tier2_available():
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
        llm_available = (
            tier in (None, "llm") and (has_ner_mentions or tier == "llm") and self._llm_ready()
        )
        if llm_available and entities:
            entities = await self._llm_validate_entities(entities, text)

        # --- Phase 4: Relationships from validated entities ---
        relationships = _extract_cooccurrence_relationships(entities, text) if entities else []

        backends_used = sorted({m.backend_name for m in all_mentions})
        if llm_available:
            backends_used.append("llm-validator")

        metadata: dict[str, Any] = {
            "backend": "+".join(backends_used) if backends_used else backend,
            "backends": backends_used,
            "total_chars": len(text),
            "mentions_raw": len(all_mentions),
            "entities_merged": len(entities),
            "ensemble": ensemble,
            "llm_validated": llm_available,
            # The tier-2 model that actually ran (None when it did not).
            "ner_model": self.ner_model_id if self._tier2_ran else None,
        }
        # A suggestion, not a switch: text analysis only, printed once per process.
        hint = multilingual_hint(text, self.ner_model) if tier != "regex" else None
        if hint:
            metadata["ner_hint"] = hint
            if not _hint_shown:
                _hint_shown = True
                print(hint, file=sys.stderr)

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            summary=self._generate_summary(entities, relationships),
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # LLM validation (Phase 3 — reviews entity list, not document)
    # ------------------------------------------------------------------

    async def _llm_validate_entities(
        self, entities: list[ExtractedEntity], source_text: str
    ) -> list[ExtractedEntity]:
        """LLM reviews the merged entity list. Fast because input is ~2K, not 78K."""
        # Build compact entity list for LLM
        entity_lines = []
        for e in entities:
            entity_lines.append(
                f'- {e.entity_type.value}: "{e.raw_text}" (confidence: {e.confidence:.0%})'
            )
        entity_list = "\n".join(entity_lines[:200])  # cap to keep prompt manageable

        prompt = LLM_VALIDATION_PROMPT.format(entity_list=entity_list)

        try:
            self._require_llm_consent()
            if self.provider == "openai":
                raw = await asyncio.to_thread(
                    _call_openai, prompt, self.model, self.api_key or "", self.base_url, 0.1, 4000
                )
            else:
                raw = await asyncio.to_thread(
                    _call_ollama, prompt, self.model, self.base_url, 0.1, 4000
                )
        except Exception as exc:
            # The error can quote the remote server's response.
            print(
                safe_text(f"LLM validation skipped: {type(exc).__name__}: {exc}"),
                file=sys.stderr,
            )
            return entities  # LLM failed, return unmodified

        # Parse LLM response
        try:
            json_match = re.search(r"\{[\s\S]*\}", raw)
            if not json_match:
                print(
                    f"LLM validation: no JSON found in response ({len(raw)} chars)", file=sys.stderr
                )
                return entities
            data = json.loads(json_match.group())
        except (json.JSONDecodeError, AttributeError) as exc:
            print(f"LLM validation: JSON parse failed: {exc}", file=sys.stderr)
            return entities

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
        self._require_llm_consent()
        prompt = _build_extraction_prompt(
            text,
            context,
            page_number,
            self.entity_config,
            compact=_is_compact_model(self.model, self.compact_prompt),
        )

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
        """Extract mentions with the configured tier-2 model (GLiNER or GLiNER2)."""
        model = _get_ner_model(self.ner_model)
        if model is None:
            return []
        self._tier2_ran = True

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

        page = page_numbers[0] if page_numbers else None
        best: dict[tuple[int, int, EntityType], Mention] = {}

        for offset, window in _gliner_windows(text):
            try:
                predictions = model.predict_entities(window, labels, threshold=0.4)
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
                start = offset + pred.get("start", window.find(raw))
                end = offset + pred.get("end", window.find(raw) + len(raw))
                mention = Mention(
                    entity_type=etype,
                    raw_text=raw,
                    normalized_text=raw.strip(),
                    start_offset=start,
                    end_offset=end,
                    page=page,
                    context=_get_context_window(text, start, end),
                    backend_name="gliner",
                    backend_score=round(pred.get("score", 0.5), 3),
                )
                key = (start, end, etype)
                if key not in best or mention.backend_score > best[key].backend_score:
                    best[key] = mention

        return list(best.values())

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
