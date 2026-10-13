"""GLiNER input windowing and forced extraction tiers. No model download needed."""

from __future__ import annotations

import asyncio
import re
from itertools import pairwise

import pytest

from openfoia.pipeline import extract as ex
from openfoia.pipeline.extract import (
    BackendUnavailableError,
    EntityExtractor,
    _gliner_available,
    _gliner_windows,
)


def _words(s: str) -> int:
    return len(ex._GLINER_WORD.findall(s))


def _long_text() -> str:
    filler = " ".join(f"Sentence {i} talks about the budget for the county." for i in range(120))
    return f"{filler} Mr. Hocking chaired the WISC review. {filler}"


def test_windows_are_bounded_exact_slices_with_overlap():
    text = _long_text()
    windows = _gliner_windows(text, max_words=100)
    assert len(windows) > 5
    for offset, window in windows:
        assert text[offset : offset + len(window)] == window
        assert _words(window) <= 100
    # consecutive windows overlap, so nothing falls between them
    for (o1, w1), (o2, _) in pairwise(windows):
        assert o2 < o1 + len(w1)
    assert windows[0][0] == 0
    assert windows[-1][0] + len(windows[-1][1]) == len(text)


def test_sentence_longer_than_budget_is_split_on_words():
    text = " ".join(["word"] * 250)
    windows = _gliner_windows(text, max_words=100)
    assert [_words(w) for _, w in windows] == [100, 100, 50]


def test_empty_text_has_no_windows():
    assert _gliner_windows("") == []


class _FakeGliner:
    """Finds 'Hocking' and 'WISC' and truncates like the real model would."""

    def __init__(self):
        self.calls = 0

    def predict_entities(self, text, labels, threshold=0.4):
        self.calls += 1
        assert _words(text) <= 384, "input would be truncated"
        out = []
        for name, label in (("Hocking", "person"), ("WISC", "organization")):
            for m in re.finditer(name, text):
                out.append(
                    {"text": name, "label": label, "score": 0.9, "start": m.start(), "end": m.end()}
                )
        return out


def test_gliner_mentions_use_document_offsets_and_dedupe(monkeypatch):
    text = _long_text()
    fake = _FakeGliner()
    monkeypatch.setattr(ex, "_get_gliner", lambda: fake)

    mentions = EntityExtractor(provider="none")._mentions_from_gliner(text, None)

    assert fake.calls > 1
    by_name = {m.raw_text: m for m in mentions}
    assert sorted(by_name) == ["Hocking", "WISC"]
    assert len(mentions) == 2  # overlap windows do not double count
    h = by_name["Hocking"]
    assert text[h.start_offset : h.end_offset] == "Hocking"
    assert "Hocking chaired" in h.context


def test_regex_tier_never_touches_llm_or_ner(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be called")

    monkeypatch.setattr(ex, "_llm_available", boom)
    monkeypatch.setattr(ex, "_get_gliner", boom)
    result = asyncio.run(
        EntityExtractor(tier="regex").extract("Call 555-123-4567 about $5,000 on 2024-01-02.")
    )
    assert result.metadata["backends"] == ["regex"]


def test_forced_tier_errors_when_not_installed(monkeypatch):
    monkeypatch.setattr(ex, "_gliner_available", lambda: False)
    with pytest.raises(BackendUnavailableError, match="install-extras ner"):
        asyncio.run(EntityExtractor(tier="gliner").extract("text"))
    with pytest.raises(ValueError):
        EntityExtractor(tier="bert")


def test_provider_none_disables_llm_even_with_key():
    assert ex._llm_available("none", "sk-test", None) is False


@pytest.mark.skipif(not _gliner_available(), reason="gliner not installed")
def test_real_gliner_finds_entity_past_384_words(monkeypatch):
    """Runs only when gliner and its cached model are present; never downloads."""
    import huggingface_hub.constants

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_OFFLINE", True)
    try:
        ex._get_gliner()
    except Exception as e:
        pytest.skip(f"GLiNER model not cached locally: {e}")

    text = _long_text()
    names = {m.raw_text for m in EntityExtractor(provider="none")._mentions_from_gliner(text, None)}
    assert any("Hocking" in n for n in names)
