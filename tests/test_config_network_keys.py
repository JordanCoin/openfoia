"""records.api_data_gov_key and network.max_attempts: loading and wiring (#56)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from typer.testing import CliRunner

from openfoia import net
from openfoia.cli import _egress_policy_from, app
from openfoia.config import load_config, redact_secrets

ENV_VARS = ("OPENFOIA_API_DATA_GOV_KEY", "OPENFOIA_NETWORK_MAX_ATTEMPTS")


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _write(data_dir, data):
    (data_dir / "config.json").write_text(json.dumps(data))


def test_defaults():
    cfg = load_config()
    assert cfg.records.api_data_gov_key is None
    assert cfg.network.max_attempts == 3


@pytest.mark.parametrize("key", ["api_data_gov_key", "_api_data_gov_key"])
def test_api_data_gov_key_from_file(data_dir, key):
    _write(data_dir, {"records": {key: "file-key"}})
    assert load_config().records.api_data_gov_key == "file-key"


def test_env_overrides_file(data_dir, monkeypatch):
    _write(data_dir, {"records": {"_api_data_gov_key": "file-key"}, "network": {"max_attempts": 5}})
    cfg = load_config()
    assert cfg.network.max_attempts == 5

    monkeypatch.setenv("OPENFOIA_API_DATA_GOV_KEY", "env-key")
    monkeypatch.setenv("OPENFOIA_NETWORK_MAX_ATTEMPTS", "1")
    cfg = load_config()
    assert cfg.records.api_data_gov_key == "env-key"
    assert cfg.network.max_attempts == 1

    monkeypatch.setenv("OPENFOIA_NETWORK_MAX_ATTEMPTS", "lots")
    assert load_config().network.max_attempts == 5


def test_key_redacted_in_config_show():
    shown = redact_secrets({"records": {"_api_data_gov_key": "real-key"}})
    assert "real-key" not in json.dumps(shown)


def test_records_search_sends_configured_key_and_redacts_it(data_dir, monkeypatch):
    _write(data_dir, {"records": {"_api_data_gov_key": "real-key"}})
    seen = []

    def handler(request):
        seen.append(request.url.params.get("api_key"))
        return httpx.Response(500, request=request)

    real_client = net.egress_client
    monkeypatch.setattr(
        "openfoia.records.fec.egress_client",
        lambda policy, **kw: real_client(policy, transport=httpx.MockTransport(handler), **kw),
    )

    result = CliRunner().invoke(app, ["records", "search", "Acme", "-s", "fec", "--json"])

    assert seen == ["real-key"]
    assert "real-key" not in result.output
    assert "api_key=<redacted>" in result.output


def test_max_attempts_one_sends_a_single_request_on_429(monkeypatch):
    cfg = load_config()
    cfg.network.max_attempts = 1
    policy = _egress_policy_from(cfg)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429)

    async def go():
        async with net.egress_client(policy, transport=httpx.MockTransport(handler)) as client:
            return await client.get("https://api.example.org/x")

    assert asyncio.run(go()).status_code == 429
    assert len(calls) == 1
