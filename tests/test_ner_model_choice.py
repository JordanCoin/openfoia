"""Choosing the tier-2 NER model: english (default) or multilingual.

The model itself never loads here: every test stubs it, so this runs without
torch or any optional extra (and the file and test names avoid the words CI
deselects). See docs/benchmarks/gliner2.md for why the multilingual model is
fastino/gliner2-multi-v1 and why it is not the default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.config import _CACHE_ENV, OpenFOIAConfig, load_config, save_config
from openfoia.pipeline import extract as ex

runner = CliRunner()

MULTI_ID = "fastino/gliner2-multi-v1"
ENGLISH_ID = "urchade/gliner_medium-v2.1"

# Russian, news register: "The Accounts Chamber found violations in a contract
# that StroyInvest signed with the Novosibirsk mayor's office..."
RUSSIAN = (
    "Счётная палата выявила нарушения в контракте, который компания «СтройИнвест» "
    "заключила с мэрией Новосибирска в прошлом году. Директор департамента Ирина "  # noqa: RUF001
    "Смирнова подписала договор с банком в Москве."  # noqa: RUF001
)
# Portuguese (Latin script, no non-Latin letters at all).
PORTUGUESE = (
    "O Tribunal de Contas da União abriu uma auditoria sobre os contratos assinados por "
    "Carlos Almeida em Brasília. Segundo documentos obtidos pela reportagem, a empresa "
    "pagou R$ 12 milhões à construtora em 2023, e o contrato foi renovado pela prefeitura "
    "sem licitação, com valores que não foram publicados no portal da transparência."
)
ENGLISH = (
    "The Department of Justice released 182 pages in full and 65 pages with redactions. "
    "The contract was valued at $14,750,000 and was overseen by Deputy Assistant Secretary "
    "James R. Whitfield at the Pentagon. Payments were routed through a subsidiary of "
    "Meridian Defense Systems to an account at the bank, and José García of "
    "Construcciones del Norte S.A. was named in the emails."
)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    from openfoia import db as dbmod

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    monkeypatch.delenv("OPENFOIA_NER_MODEL", raising=False)
    monkeypatch.setenv("OPENFOIA_AI_PROVIDER", "none")
    monkeypatch.setattr(dbmod, "_schema_checked", False)
    monkeypatch.setattr(ex, "_hint_shown", False)
    return tmp_path / "data"


def _no_model_loads(monkeypatch):
    """Any attempt to load (and so maybe download) a tier-2 model fails the test."""

    def boom(*a, **k):
        raise AssertionError("a tier-2 model was loaded")

    for name in ("_get_gliner", "_get_gliner2", "_get_ner_model", "_hf_cache_dir"):
        monkeypatch.setattr(ex, name, boom)


def _only_regex(monkeypatch):
    _no_model_loads(monkeypatch)
    monkeypatch.setattr(ex, "_gliner_available", lambda: False)
    monkeypatch.setattr(ex, "_gliner2_available", lambda: False)
    monkeypatch.setattr(ex, "_spacy_available", lambda: False)


class _FakeTier2:
    """predict_entities like GLiNER; tags each listed name with its label."""

    def __init__(self, names):
        self.names = names

    def predict_entities(self, text, labels, threshold=0.4):
        out = []
        for name, label in self.names:
            i = text.find(name)
            if i >= 0:
                out.append(
                    {"text": name, "label": label, "score": 0.9, "start": i, "end": i + len(name)}
                )
        return out


def _use_fake_multilingual(monkeypatch, names=(("Ирина Смирнова", "person"),)):
    fake = _FakeTier2(names)
    loaded = []
    monkeypatch.setattr(ex, "_gliner2_available", lambda: True)
    monkeypatch.setattr(ex, "_spacy_available", lambda: False)
    monkeypatch.setattr(ex, "_get_gliner2", lambda: loaded.append("multilingual") or fake)
    monkeypatch.setattr(ex, "_get_gliner", lambda: loaded.append("english") or fake)
    return loaded


# --- selecting the model: config, env, flag -------------------------------------------


def test_default_is_the_english_model(data_dir):
    assert load_config().entities.ner_model == "english"
    extractor = ex.EntityExtractor(provider="none")
    assert extractor.ner_model == "english"
    assert extractor.ner_model_id == ENGLISH_ID == ex.GLINER_MODEL_ID


def test_config_file_env_and_argument_select_the_model(data_dir, monkeypatch):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(json.dumps({"entities": {"ner_model": "multilingual"}}))
    assert load_config().entities.ner_model == "multilingual"
    assert ex.EntityExtractor(provider="none").ner_model_id == MULTI_ID

    monkeypatch.setenv("OPENFOIA_NER_MODEL", "English")  # env beats the file, any case
    assert load_config().entities.ner_model == "english"

    # An explicit argument (the CLI flag) beats both.
    assert ex.EntityExtractor(provider="none", ner_model="multilingual").ner_model == "multilingual"
    with pytest.raises(ValueError, match="multilingual"):
        ex.EntityExtractor(provider="none", ner_model="klingon")


def test_unknown_config_value_warns_and_keeps_english(data_dir, monkeypatch, capsys):
    monkeypatch.setenv("OPENFOIA_NER_MODEL", "gpt")
    assert load_config().entities.ner_model == "english"
    err = capsys.readouterr().err
    assert "OPENFOIA_NER_MODEL='gpt'" in err and "english, multilingual" in err


def test_saved_config_keeps_the_choice(tmp_path):
    cfg = OpenFOIAConfig()
    cfg.entities.ner_model = "multilingual"
    path = tmp_path / "config.json"
    save_config(cfg, path)
    assert json.loads(path.read_text())["entities"]["ner_model"] == "multilingual"
    assert load_config(path).entities.ner_model == "multilingual"


def test_choices_match_the_registry():
    from openfoia.config import NER_MODEL_CHOICES

    assert tuple(ex.NER_MODELS) == NER_MODEL_CHOICES
    assert ex.NER_MODELS["multilingual"].extra == "ner-multilingual"
    assert ex.NER_MODELS["multilingual"].package == "gliner2"


def _document(text: str) -> str:
    from openfoia.db import get_session, init_db
    from openfoia.models import Agency, DeliveryMethod, Document, DocumentType, Request, User

    init_db()
    with get_session() as s:
        user = User(email="ner@example.invalid", name="NER")
        s.add(user)
        s.flush()
        req = Request(
            request_number="REQ-NER-1",
            requester_id=user.id,
            agency_id=s.query(Agency).first().id,
            subject="Tender",
            body="",
            delivery_method=DeliveryMethod.EMAIL,
        )
        s.add(req)
        s.flush()
        doc = Document(
            request_id=req.id,
            doc_type=DocumentType.FULL_RESPONSE,
            filename="ru.pdf",
            file_path="",
            file_size=len(text),
            mime_type="application/pdf",
            extracted_text=text,
        )
        s.add(doc)
        s.flush()
        return doc.id


def test_cli_flag_selects_the_model_and_the_run_records_its_id(monkeypatch):
    loaded = _use_fake_multilingual(monkeypatch)
    doc_id = _document(RUSSIAN)

    result = runner.invoke(app, ["analyze", "extract", doc_id, "--ner-model", "multilingual"])
    assert result.exit_code == 0, result.output
    assert "Ирина Смирнова" in result.output
    assert loaded == ["multilingual"]

    listing = runner.invoke(app, ["analyze", "runs", doc_id, "--csv", "-"])
    assert listing.exit_code == 0, listing.output
    assert f",gliner,{MULTI_ID}," in listing.output

    bad = runner.invoke(app, ["analyze", "extract", doc_id, "--force", "--ner-model", "xx"])
    assert bad.exit_code == 1 and "Unknown --ner-model" in bad.output


def test_english_run_records_the_english_id(monkeypatch):
    _use_fake_multilingual(monkeypatch)
    monkeypatch.setattr(ex, "_gliner_available", lambda: True)
    doc_id = _document(RUSSIAN)
    assert runner.invoke(app, ["analyze", "extract", doc_id]).exit_code == 0
    listing = runner.invoke(app, ["analyze", "runs", doc_id, "--csv", "-"]).output
    assert f",gliner,{ENGLISH_ID}," in listing and MULTI_ID not in listing


# --- run metadata ------------------------------------------------------------------


def test_metadata_and_run_description_name_the_model_that_ran(monkeypatch):
    from openfoia.extraction_runs import describe_extraction

    _use_fake_multilingual(monkeypatch)
    extractor = ex.EntityExtractor(provider="none", ner_model="multilingual")
    result = asyncio.run(extractor.extract(RUSSIAN))
    assert result.metadata["ner_model"] == MULTI_ID
    described = describe_extraction(extractor, result.metadata, False)
    assert described["backend"] == "gliner" and described["model"] == MULTI_ID

    ensemble = asyncio.run(extractor.extract(RUSSIAN, ensemble=True))
    assert MULTI_ID in describe_extraction(extractor, ensemble.metadata, True)["model"]

    # The configured model did not run: nothing is attributed to it.
    regex = asyncio.run(ex.EntityExtractor(tier="regex", ner_model="multilingual").extract(RUSSIAN))
    assert regex.metadata["ner_model"] is None
    assert describe_extraction(extractor, regex.metadata, False)["model"] is None


# --- the multilingual extra is missing ----------------------------------------------


def test_missing_multilingual_extra_falls_back_with_one_message(monkeypatch, capsys):
    _only_regex(monkeypatch)
    extractor = ex.EntityExtractor(provider="none", ner_model="multilingual")
    assert extractor._resolve_backend() == "regex"
    result = asyncio.run(extractor.extract("Payment of $5,000 on 2024-01-02."))
    asyncio.run(extractor.extract("Payment of $7,000 on 2024-02-03."))

    assert result.metadata["backends"] == ["regex"]
    err = capsys.readouterr().err
    assert err.count("openfoia install-extras ner-multilingual") == 1
    assert "gliner2" in err and "next tier" in err

    with pytest.raises(ex.BackendUnavailableError, match="install-extras ner-multilingual"):
        asyncio.run(ex.EntityExtractor(tier="gliner", ner_model="multilingual").extract("x"))


def test_missing_multilingual_extra_uses_the_next_tier_not_the_english_model(monkeypatch):
    """Falling back to the English model could start an unasked 1.6 GB download."""
    _only_regex(monkeypatch)
    monkeypatch.setattr(ex, "_gliner_available", lambda: True)  # english installed
    extractor = ex.EntityExtractor(provider="none", ner_model="multilingual")
    result = asyncio.run(extractor.extract("Payment of $5,000 on 2024-01-02."))
    assert result.metadata["backends"] == ["regex"]


def test_cli_points_at_the_multilingual_extra_when_it_is_missing(monkeypatch):
    _only_regex(monkeypatch)
    doc_id = _document(RUSSIAN)
    result = runner.invoke(app, ["analyze", "extract", doc_id, "--ner-model", "multilingual"])
    assert result.exit_code == 0, result.output
    assert "install-extras ner-multilingual" in result.output


def test_probe_is_false_for_a_broken_or_absent_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "gliner2", None)  # import gliner2 -> ImportError
    assert ex._gliner2_available() is False
    assert ex._ner_model_available("multilingual") is False


def test_install_extras_knows_ner_multilingual(monkeypatch):
    seen = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )
    result = runner.invoke(app, ["install-extras", "ner-multilingual"])
    assert result.exit_code == 0, result.output
    assert seen[0][-1] == "openfoia[ner-multilingual]"
    assert "--ner-model multilingual" in result.output and "1.25 GB" in result.output


# --- the "not in English" hint ---------------------------------------------------------


def test_hint_detects_non_latin_and_non_english_text():
    assert "Cyrillic" in ex.non_english_reason(RUSSIAN)
    assert ex.non_english_reason(PORTUGUESE) == "looks like it is not in English"
    assert "Han" in ex.non_english_reason(
        "审计署发现华信建设公司在年获得了上海市政府的三个项目" * 2
    )
    assert "Arabic" in ex.non_english_reason(
        "قال ديوان المحاسبة إن شركة النور للمقاولات حصلت على عقد بقيمة خمسة ملايين دينار في عمّان."
    )
    assert ex.non_english_reason(ENGLISH) is None
    assert ex.non_english_reason("Газпром") is None  # too short to judge


def test_hint_fires_once_and_never_loads_or_downloads(monkeypatch, capsys):
    _only_regex(monkeypatch)
    first = asyncio.run(ex.EntityExtractor(provider="none").extract(RUSSIAN))
    second = asyncio.run(ex.EntityExtractor(provider="none").extract(PORTUGUESE))

    err = capsys.readouterr().err
    assert err.count("multilingual model") == 1, err
    hint = first.metadata["ner_hint"]
    assert "Cyrillic" in hint and "Nothing was switched or downloaded" in hint
    assert "install-extras ner-multilingual" in hint and "1.25 GB" in hint
    assert "OPENFOIA_NER_MODEL=multilingual" in hint and "--ner-model multilingual" in hint
    assert "ner_hint" in second.metadata  # still reported to callers, just not reprinted


def test_no_hint_for_english_text_or_the_multilingual_model(monkeypatch, capsys):
    _only_regex(monkeypatch)
    english = asyncio.run(ex.EntityExtractor(provider="none").extract(ENGLISH))
    assert "ner_hint" not in english.metadata
    multi = asyncio.run(
        ex.EntityExtractor(provider="none", ner_model="multilingual").extract(RUSSIAN)
    )
    assert "ner_hint" not in multi.metadata
    assert "multilingual model" not in capsys.readouterr().err


def test_cli_prints_the_hint(monkeypatch):
    _only_regex(monkeypatch)
    doc_id = _document(RUSSIAN)
    result = runner.invoke(app, ["analyze", "extract", doc_id])
    assert result.exit_code == 0, result.output
    assert result.output.count("Nothing was switched or downloaded") == 1


def test_agent_tool_returns_the_hint(monkeypatch):
    from openfoia.agent import OpenFOIAAgent
    from openfoia.db import get_session

    _only_regex(monkeypatch)
    doc_id = _document(RUSSIAN)
    with get_session() as s:
        out = asyncio.run(
            OpenFOIAAgent(db_session=s, config={})._extract_entities({"document_id": doc_id})
        )
    assert "Cyrillic" in out["hint"] and "multilingual" in out["hint"]
    assert out["ner_model"] is None  # regex only: no tier-2 model ran


# --- loading: download notice, offline mode, portable cache -----------------------------


def _fake_hub(monkeypatch, offline=False):
    hub = types.ModuleType("huggingface_hub")
    hub.constants = SimpleNamespace(HF_HUB_OFFLINE=offline)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    return hub.constants


def _fake_gliner2(monkeypatch, cached: bool, constants):
    calls = []

    class AutoExtractor:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls.append((model_id, kwargs, constants.HF_HUB_OFFLINE, os.environ.get("HF_HOME")))
            if kwargs.get("local_files_only") and not cached:
                raise OSError("not in the local cache")
            return SimpleNamespace(extract_entities=lambda *a, **k: {"entities": {}})

    mod = types.ModuleType("gliner2")
    mod.AutoExtractor = AutoExtractor
    monkeypatch.setitem(sys.modules, "gliner2", mod)
    monkeypatch.setattr(ex, "_GLINER2_MODEL", None)
    return calls


@pytest.fixture
def portable_stick(tmp_path, monkeypatch):
    stick = tmp_path / "stick"
    stick.mkdir()
    (stick / ".openfoia-portable").touch()
    monkeypatch.chdir(stick)
    monkeypatch.delenv("OPENFOIA_DATA_DIR", raising=False)
    for var in (*_CACHE_ENV, "HF_HUB_CACHE"):
        monkeypatch.delenv(var, raising=False)
    return stick


def test_multilingual_model_cache_stays_on_the_stick(portable_stick, monkeypatch):
    constants = _fake_hub(monkeypatch)
    calls = _fake_gliner2(monkeypatch, cached=True, constants=constants)

    ex._get_gliner2()

    hf_home = str(portable_stick / "openfoia-data" / "cache" / "huggingface")
    (model_id, kwargs, offline, env_home) = calls[0]
    assert model_id == MULTI_ID and len(calls) == 1
    assert env_home == hf_home, "HF_HOME must point at the stick before the library loads"
    assert kwargs["cache_dir"] == os.path.join(hf_home, "hub")
    assert kwargs["local_files_only"] is True and offline is True
    assert constants.HF_HUB_OFFLINE is False, "the offline flag was not restored"


def test_english_model_cache_stays_on_the_stick(portable_stick, monkeypatch):
    seen = []

    class GLiNER:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            seen.append(os.environ.get("HF_HOME"))
            return object()

    mod = types.ModuleType("gliner")
    mod.GLiNER = GLiNER
    monkeypatch.setitem(sys.modules, "gliner", mod)
    _fake_hub(monkeypatch)
    monkeypatch.setattr(ex, "_GLINER_MODEL", None)
    ex._get_gliner()
    assert seen == [str(portable_stick / "openfoia-data" / "cache" / "huggingface")]


def test_no_cache_dir_outside_portable_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENFOIA_DATA_DIR", raising=False)
    constants = _fake_hub(monkeypatch)
    calls = _fake_gliner2(monkeypatch, cached=True, constants=constants)
    ex._get_gliner2()
    assert "cache_dir" not in calls[0][1]


def test_uncached_model_warns_with_the_measured_size_then_downloads(monkeypatch, caplog):
    monkeypatch.chdir(os.path.dirname(__file__))
    monkeypatch.delenv("OPENFOIA_DATA_DIR", raising=False)
    constants = _fake_hub(monkeypatch)
    calls = _fake_gliner2(monkeypatch, cached=False, constants=constants)

    with caplog.at_level(logging.WARNING, logger=ex.__name__):
        model = ex._get_gliner2()

    assert isinstance(model, ex.Gliner2Adapter)
    assert [c[1].get("local_files_only") for c in calls] == [True, None]
    assert calls[1][2] is False  # the download ran with the offline flag off
    notice = caplog.text
    assert MULTI_ID in notice and "about 1.25 GB" in notice and "huggingface.co" in notice
    assert constants.HF_HUB_OFFLINE is False


def test_offline_mode_never_downloads(monkeypatch):
    monkeypatch.chdir(os.path.dirname(__file__))
    monkeypatch.delenv("OPENFOIA_DATA_DIR", raising=False)
    constants = _fake_hub(monkeypatch, offline=True)
    calls = _fake_gliner2(monkeypatch, cached=False, constants=constants)
    with pytest.raises(OSError):
        ex._get_gliner2()
    assert len(calls) == 1 and constants.HF_HUB_OFFLINE is True


def test_adapter_turns_v2_output_into_predictions():
    out = {
        "entities": {"person": [{"text": "Ayşe Kaya", "start": 4, "end": 13, "confidence": 0.8}]}
    }
    model = SimpleNamespace(extract_entities=lambda *a, **k: out)
    preds = ex.Gliner2Adapter(model).predict_entities("x", ["person"], threshold=0.4)
    assert preds == [{"text": "Ayşe Kaya", "start": 4, "end": 13, "label": "person", "score": 0.8}]
