"""Entity scoring must not discard names written in non-Latin scripts.

_mention_score used to count only A-Z letters and look for an A-Z capital, so every
name in Cyrillic, Greek, Arabic, Hebrew, Indic, Thai, CJK, Georgian, Armenian or
Ethiopic script scored 0 and was dropped by every backend, the LLM tier included.
"""

from __future__ import annotations

import pytest

from openfoia.models import EntityType
from openfoia.pipeline.extract import Mention, _mention_score

PER, ORG, LOC = EntityType.PERSON, EntityType.ORGANIZATION, EntityType.LOCATION

NAMES = [
    (ORG, "Газпром"),
    (PER, "Алексей Навальный"),
    (PER, "Володимир Зеленський"),
    (PER, "Κυριάκος Μητσοτάκης"),
    (ORG, "وزارة الداخلية"),
    (PER, "مُحَمَّد"),  # harakat are combining marks
    (PER, "بنیامین‌پور"),  # Persian zero-width non-joiner inside the word
    (PER, "בנימין נתניהו"),
    (LOC, "تهران"),
    (PER, "नरेन्द्र मोदी"),  # Devanagari vowel signs and virama
    (LOC, "ঢাকা"),  # two base letters, two vowel signs
    (PER, "ประยุทธ์ จันทร์โอชา"),
    (LOC, "东京"),  # two characters is a whole name in Han script
    (LOC, "東京都"),
    (PER, "王伟"),
    (ORG, "トヨタ自動車"),
    (PER, "김정은"),
    (LOC, "თბილისი"),  # Georgian Mkhedruli is lowercase by design
    (LOC, "Երևան"),
    (LOC, "አዲስ አበባ"),
]

BACKENDS = [("spacy", 0.85), ("gliner", 0.9), ("regex", 0.5), ("llm", 0.9)]


def _score(entity_type: EntityType, text: str, backend: str, raw: float) -> float:
    return _mention_score(Mention(entity_type, text, text, -1, -1, None, "", backend, raw))


@pytest.mark.parametrize("backend, raw", BACKENDS)
@pytest.mark.parametrize("entity_type, text", NAMES)
def test_non_latin_name_scores_like_a_latin_name(entity_type, text, backend, raw):
    """Kept, and at the same confidence as a well-formed Latin name from that backend.

    Crossref skips entities under 0.5, so a name that merely survives at 0.2 is
    still never checked against sanctions lists or company registries.
    """
    latin = _score(entity_type, "Kirkland Ellis", backend, raw)
    assert _score(entity_type, text, backend, raw) == pytest.approx(latin)


@pytest.mark.parametrize(
    "entity_type, text",
    [
        (PER, "王"),  # one Han character
        (PER, "к"),  # one Cyrillic letter
        (PER, "To"),
        (ORG, "EC .Orchestrating a or g-?te' 22"),
        (ORG, "?? !! ::"),
    ],
)
def test_junk_is_still_dropped_in_any_script(entity_type, text):
    assert all(_score(entity_type, text, b, r) == 0.0 for b, r in BACKENDS)


def test_lowercase_is_penalized_the_same_in_cyrillic_as_in_latin():
    """A cased script without a capital is suspicious, whatever the script."""
    for backend, raw in BACKENDS:
        latin = _score(ORG, "acme corp", backend, raw)
        cyrillic = _score(ORG, "газпром нефть", backend, raw)
        assert cyrillic == pytest.approx(latin)
        assert latin < _score(ORG, "Acme Corp", backend, raw)


@pytest.mark.parametrize("backend, raw", BACKENDS)
def test_cyrillic_acronym_is_protected_like_a_latin_one(backend, raw):
    assert _score(ORG, "ФСБ", backend, raw) == pytest.approx(_score(ORG, "FBI", backend, raw))
