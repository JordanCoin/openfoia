"""Security regression tests: exposing Tor egress via config + CLI, honestly.

Principle 1 (data never leaves the machine unless the user explicitly
chooses) and Principle 3 (be honest about what we protect and what we
don't) both apply here:

- `network.tor` defaults OFF — Tor routing is opt-in, not opt-out.
- CLI commands that build an `EgressPolicy` in TOR mode must fail closed:
  if the SOCKS proxy isn't actually reachable, abort rather than silently
  falling through to a clearnet request.
- The leak report shown before crossref sends subject names out must not
  overstate what Tor protects, and docs/THREAT_MODEL.md must not either.

Everything here is offline: `openfoia.crossref.crossref_entities` and
`openfoia.pipeline.web.archive_url` are mocked, never called for real, so
this file has no dependency on the parallel work landing in those modules.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from openfoia.cli import _egress_policy_from, app
from openfoia.config import NetworkConfig, load_config
from openfoia.net import EgressMode, EgressPolicy, TorUnavailableError

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own ~/.openfoia so config.json never leaks state."""
    data_dir = tmp_path / "openfoia-data"
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(data_dir))
    for var in ("OPENFOIA_TOR", "OPENFOIA_TOR_HOST", "OPENFOIA_TOR_PORT"):
        monkeypatch.delenv(var, raising=False)
    return data_dir


# ---------------------------------------------------------------------------
# NetworkConfig defaults + env overrides
# ---------------------------------------------------------------------------


def test_network_config_defaults_are_off_and_local():
    net = NetworkConfig()
    assert net.tor is False
    assert net.tor_host == "127.0.0.1"
    assert net.tor_port == 9050
    assert net.isolate_streams is True


def test_load_config_network_defaults(isolated_data_dir):
    cfg = load_config()
    assert cfg.network.tor is False
    assert cfg.network.tor_host == "127.0.0.1"
    assert cfg.network.tor_port == 9050
    assert cfg.network.isolate_streams is True


def test_env_override_tor_on(monkeypatch, isolated_data_dir):
    monkeypatch.setenv("OPENFOIA_TOR", "true")
    cfg = load_config()
    assert cfg.network.tor is True


def test_env_override_tor_off_explicit(monkeypatch, isolated_data_dir):
    monkeypatch.setenv("OPENFOIA_TOR", "0")
    cfg = load_config()
    assert cfg.network.tor is False


def test_env_override_tor_host_and_port(monkeypatch, isolated_data_dir):
    monkeypatch.setenv("OPENFOIA_TOR_HOST", "10.0.0.9")
    monkeypatch.setenv("OPENFOIA_TOR_PORT", "9150")
    cfg = load_config()
    assert cfg.network.tor_host == "10.0.0.9"
    assert cfg.network.tor_port == 9150


def test_env_override_bad_port_does_not_crash(monkeypatch, isolated_data_dir, capsys):
    monkeypatch.setenv("OPENFOIA_TOR_PORT", "not-a-port")
    cfg = load_config()  # must not raise
    assert cfg.network.tor_port == 9050  # left at default
    assert "not-a-port" in capsys.readouterr().out


def test_config_file_network_section_round_trips(isolated_data_dir):
    from openfoia.config import OpenFOIAConfig, save_config

    cfg = OpenFOIAConfig()
    cfg.network.tor = True
    cfg.network.tor_host = "127.0.0.1"
    cfg.network.tor_port = 9150
    cfg.network.isolate_streams = False
    save_config(cfg)

    reloaded = load_config()
    assert reloaded.network.tor is True
    assert reloaded.network.tor_port == 9150
    assert reloaded.network.isolate_streams is False


# ---------------------------------------------------------------------------
# _egress_policy_from: config default vs. CLI override
# ---------------------------------------------------------------------------


def _cfg(*, tor=False, host="127.0.0.1", port=9050, isolate=True):
    return SimpleNamespace(
        network=NetworkConfig(tor=tor, tor_host=host, tor_port=port, isolate_streams=isolate)
    )


def test_egress_policy_from_uses_config_default_when_tor_is_none():
    policy = _egress_policy_from(_cfg(tor=False), tor=None)
    assert policy.mode is EgressMode.DIRECT

    policy = _egress_policy_from(_cfg(tor=True), tor=None)
    assert policy.mode is EgressMode.TOR


def test_egress_policy_from_cli_true_overrides_config_off():
    policy = _egress_policy_from(_cfg(tor=False), tor=True)
    assert policy.mode is EgressMode.TOR


def test_egress_policy_from_cli_false_overrides_config_on():
    policy = _egress_policy_from(_cfg(tor=True), tor=False)
    assert policy.mode is EgressMode.DIRECT


def test_egress_policy_from_carries_host_port_isolation():
    policy = _egress_policy_from(
        _cfg(tor=True, host="10.1.1.1", port=9999, isolate=False), tor=None
    )
    assert policy.tor_host == "10.1.1.1"
    assert policy.tor_port == 9999
    assert policy.isolate_streams is False


# ---------------------------------------------------------------------------
# _check_tor_or_exit: the fail-closed gate, tested directly
# ---------------------------------------------------------------------------


def test_check_tor_or_exit_noop_for_direct_policy(monkeypatch):
    from openfoia.cli import _check_tor_or_exit

    calls = []
    monkeypatch.setattr("openfoia.net.check_tor", lambda *a, **k: calls.append(1))

    _check_tor_or_exit(EgressPolicy(mode=EgressMode.DIRECT))  # must not raise
    assert calls == []  # check_tor never even invoked for DIRECT


def test_check_tor_or_exit_raises_when_tor_unreachable(monkeypatch):
    import typer

    from openfoia.cli import _check_tor_or_exit

    async def _unreachable(*a, **k):
        return False

    monkeypatch.setattr("openfoia.net.check_tor", _unreachable)

    with pytest.raises(typer.Exit):
        _check_tor_or_exit(EgressPolicy(mode=EgressMode.TOR))


def test_check_tor_or_exit_passes_when_tor_reachable(monkeypatch):
    from openfoia.cli import _check_tor_or_exit

    async def _reachable(*a, **k):
        return True

    monkeypatch.setattr("openfoia.net.check_tor", _reachable)

    _check_tor_or_exit(EgressPolicy(mode=EgressMode.TOR))  # must not raise


# ---------------------------------------------------------------------------
# `openfoia ingest` — fail-closed end to end via CliRunner
# ---------------------------------------------------------------------------


def test_ingest_aborts_when_tor_requested_and_unreachable(monkeypatch, isolated_data_dir):
    async def _unreachable(*a, **k):
        return False

    monkeypatch.setattr("openfoia.net.check_tor", _unreachable)

    called = []

    async def _archive_url(*a, **k):
        called.append((a, k))
        raise AssertionError("archive_url must not be called when Tor is unreachable")

    monkeypatch.setattr("openfoia.pipeline.web.archive_url", _archive_url)

    result = runner.invoke(app, ["ingest", "--url", "https://example.test/doc", "--tor"])

    assert result.exit_code != 0
    assert called == []
    assert "not reachable" in result.output.lower()


def test_ingest_no_tor_flag_never_checks_tor(monkeypatch, isolated_data_dir):
    """No --tor and config default is off -> DIRECT, no Tor probe at all."""
    probed = []
    monkeypatch.setattr("openfoia.net.check_tor", lambda *a, **k: probed.append(1))

    async def _archive_url(url, storage_path, use_tor=False, egress=None):
        assert use_tor is False
        assert egress is not None and egress.mode is EgressMode.DIRECT
        return SimpleNamespace(
            title="t",
            url=url,
            document_id="doc-id-123456789",
            file_size=10,
            text="hello",
            html_path="h.html",
            text_path="t.txt",
            checksum="abcdef1234567890",
        )

    monkeypatch.setattr("openfoia.pipeline.web.archive_url", _archive_url)

    result = runner.invoke(app, ["ingest", "--url", "https://example.test/doc"])

    assert result.exit_code == 0, result.output
    assert probed == []


def test_ingest_tor_unavailable_error_tells_user_to_install_extras(monkeypatch, isolated_data_dir):
    async def _reachable(*a, **k):
        return True

    monkeypatch.setattr("openfoia.net.check_tor", _reachable)

    async def _archive_url(*a, **k):
        raise TorUnavailableError("socksio missing")

    monkeypatch.setattr("openfoia.pipeline.web.archive_url", _archive_url)

    result = runner.invoke(app, ["ingest", "--url", "https://example.test/doc", "--tor"])

    assert result.exit_code != 0
    assert "install-extras tor" in result.output


# ---------------------------------------------------------------------------
# `openfoia crossref` — fail-closed end to end via CliRunner
#
# crossref queries the DB through openfoia.db.get_session/get_db_path; both
# are looked up as module attributes at call time (`from .db import ...`
# inside the command), so patching the attributes on openfoia.db before
# invoking intercepts them cleanly without a real database.
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, items):
        self._items = items

    def filter(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def all(self):
        return self._items


class _FakeSession:
    def __init__(self, items):
        self._items = items

    def query(self, _model):
        return _FakeQuery(self._items)


@contextmanager
def _fake_get_session(items):
    yield _FakeSession(items)


@pytest.fixture
def fake_entities_db(monkeypatch, isolated_data_dir):
    """Stub the DB layer crossref reads from with one PERSON entity."""
    db_file = isolated_data_dir / "data.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db_file.write_text("")  # just needs to exist

    entity = SimpleNamespace(
        entity_type="PERSON",
        raw_text="Jane Doe",
        normalized_text="jane doe",
        confidence=0.95,
        context="",
    )

    monkeypatch.setattr("openfoia.db.get_db_path", lambda *a, **k: db_file)
    monkeypatch.setattr("openfoia.db.get_session", lambda *a, **k: _fake_get_session([entity]))
    return db_file


def test_crossref_aborts_when_tor_requested_and_unreachable(monkeypatch, fake_entities_db):
    async def _unreachable(*a, **k):
        return False

    monkeypatch.setattr("openfoia.net.check_tor", _unreachable)

    called = []

    async def _crossref_entities(*a, **k):
        called.append((a, k))
        raise AssertionError("crossref_entities must not be called when Tor is unreachable")

    monkeypatch.setattr("openfoia.crossref.crossref_entities", _crossref_entities)

    result = runner.invoke(app, ["crossref", "--tor", "--yes"])

    assert result.exit_code != 0
    assert called == []
    assert "not reachable" in result.output.lower()


def test_crossref_direct_mode_never_probes_tor(monkeypatch, fake_entities_db):
    probed = []
    monkeypatch.setattr("openfoia.net.check_tor", lambda *a, **k: probed.append(1))

    captured = {}

    async def _crossref_entities(entities, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            total_entities=1,
            sources_used=["muckrock"],
            total_hits=0,
            total_flagged=0,
            results=[],
        )

    monkeypatch.setattr("openfoia.crossref.crossref_entities", _crossref_entities)

    result = runner.invoke(app, ["crossref", "--no-tor", "--yes"])

    assert result.exit_code == 0, result.output
    assert probed == []
    assert captured["allow_network"] is True
    assert captured["egress"].mode is EgressMode.DIRECT


def test_crossref_tor_mode_passes_egress_policy_through(monkeypatch, fake_entities_db):
    async def _reachable(*a, **k):
        return True

    monkeypatch.setattr("openfoia.net.check_tor", _reachable)

    captured = {}

    async def _crossref_entities(entities, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            total_entities=1,
            sources_used=["muckrock"],
            total_hits=0,
            total_flagged=0,
            results=[],
        )

    monkeypatch.setattr("openfoia.crossref.crossref_entities", _crossref_entities)

    result = runner.invoke(app, ["crossref", "--tor", "--yes"])

    assert result.exit_code == 0, result.output
    assert captured["egress"].mode is EgressMode.TOR
    # The honest leak report must have been shown, not a bare "trust me".
    assert "will not see your real ip" in result.output.lower()
    assert "not protected" not in result.output.lower() or "query" in result.output.lower()


def test_crossref_tor_unavailable_error_tells_user_to_install_extras(monkeypatch, fake_entities_db):
    async def _reachable(*a, **k):
        return True

    monkeypatch.setattr("openfoia.net.check_tor", _reachable)

    async def _crossref_entities(*a, **k):
        raise TorUnavailableError("socksio missing")

    monkeypatch.setattr("openfoia.crossref.crossref_entities", _crossref_entities)

    result = runner.invoke(app, ["crossref", "--tor", "--yes"])

    assert result.exit_code != 0
    assert "install-extras tor" in result.output


def test_crossref_without_yes_shows_honest_report_before_confirm(monkeypatch, fake_entities_db):
    """The leak warning must mention the subject names, not just 'be careful'."""

    async def _reachable(*a, **k):
        return True

    monkeypatch.setattr("openfoia.net.check_tor", _reachable)

    called = []

    async def _crossref_entities(*a, **k):
        called.append(1)
        return SimpleNamespace(
            total_entities=1, sources_used=[], total_hits=0, total_flagged=0, results=[]
        )

    monkeypatch.setattr("openfoia.crossref.crossref_entities", _crossref_entities)

    # Answer "n" to the confirmation prompt -> must abort before any network call.
    result = runner.invoke(app, ["crossref"], input="n\n")

    assert result.exit_code == 0  # aborts cleanly (typer.Exit(0))
    assert called == []
    assert "names of the people and organizations" in result.output.lower()


# ---------------------------------------------------------------------------
# docs/THREAT_MODEL.md — the honesty check
# ---------------------------------------------------------------------------


def _threat_model_text() -> str:
    path = Path(__file__).resolve().parent.parent / "docs" / "THREAT_MODEL.md"
    return path.read_text()


def test_threat_model_has_no_forbidden_overclaims():
    text = _threat_model_text().lower()
    assert "no traces" not in text
    assert "untraceable" not in text
    assert "anonymous" not in text  # "anonymity" appears only in a negation


def test_threat_model_discloses_crossref_sends_subject_names():
    text = _threat_model_text().lower()
    assert "crossref" in text
    assert "subject names" in text or "names of the people" in text


def test_threat_model_names_what_tor_does_not_protect():
    text = _threat_model_text().lower()
    assert "who is asking, not what is asked" in text or "hides who is asking" in text
    assert "correlate timing" in text or "timing" in text
