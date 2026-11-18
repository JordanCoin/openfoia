"""Security regression tests: terminal output and what leaves the machine.

Covers audit findings A8 (terminal escapes / Rich markup from upstream text),
A9 (document text sent to an LLM off this machine), A12 (SEC User-Agent),
A13 (download URL checks) and A14 (SMTP EHLO hostname, Tor and email).
The strict repros live in test_audit_repros.py; these pin the details.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
from typer.testing import CliRunner

from openfoia.net import DEFAULT_USER_AGENT, EgressMode, EgressPolicy

runner = CliRunner()

TOR = EgressPolicy(mode=EgressMode.TOR)
DIRECT = EgressPolicy()

HOSTILE = (
    "Acme \x1b]52;c;cm0gLXJmIH4K\x1b\\ \x1b]0;pwned\x07 \x9b31m ‮evil‬ "
    "\x1b[2J [link=https://evil.example/x]Verify[/link] Corp"
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(d))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    for var in ("OPENFOIA_TOR", "OPENFOIA_AI_ALLOW_REMOTE", "OPENFOIA_SEC_USER_AGENT"):
        monkeypatch.delenv(var, raising=False)
    return d


def _no_controls(text: str) -> bool:
    return not any(ch in text for ch in "\x1b\x07\x9b‮‬")


# ---------------------------------------------------------------------------
# A13: download URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://0177.0.0.1/",
        "https://0x7f.1/",
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://[64:ff9b::a00:1]/",  # NAT64 of 10.0.0.1
        "https://169.254.169.254/latest/meta-data/",
        "https://10.1/",
        "https://192.168.1.1/",
        "https://0.0.0.0/",
        "https://LOCALHOST/",
        "https://printer.local/",
        "https://metadata.google.internal/",
        "https://1.2.3.999/",  # ambiguous numeric host
        "http://example.org/",
        "file:///etc/passwd",
    ],
)
def test_download_url_rejects_more_internal_spellings(url):
    from openfoia.records.base import validate_download_url

    with pytest.raises(ValueError):
        validate_download_url(url)


@pytest.mark.parametrize(
    "url", ["https://example.org/a.pdf", "https://8.8.8.8/x", "https://bücher.example/x"]
)
def test_download_url_accepts_public_hosts(url):
    from openfoia.records.base import validate_download_url

    assert validate_download_url(url) == url


def _fake_dns(monkeypatch, address):
    from openfoia.records import base

    calls = []

    def resolve(host, *a, **k):
        calls.append(host)
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(base, "_getaddrinfo", resolve)
    return calls


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "100.64.0.1", "::1", "fe80::1"])
def test_direct_download_refuses_a_name_that_resolves_internal(monkeypatch, address):
    from openfoia.records.base import check_download_target

    calls = _fake_dns(monkeypatch, address)
    with pytest.raises(ValueError, match="non-public"):
        asyncio.run(check_download_target("https://files.example.org/a.pdf", DIRECT))
    assert calls == ["files.example.org"]


def test_direct_download_allows_a_name_that_resolves_public(monkeypatch):
    from openfoia.records.base import check_download_target

    _fake_dns(monkeypatch, "93.184.215.14")
    asyncio.run(check_download_target("https://files.example.org/a.pdf", DIRECT))


def test_tor_download_does_not_resolve_locally(monkeypatch):
    from openfoia.records.base import check_download_target

    calls = _fake_dns(monkeypatch, "127.0.0.1")
    asyncio.run(check_download_target("https://files.example.org/a.pdf", TOR))
    assert calls == []  # a local lookup would leak the name outside Tor


def test_download_to_file_checks_dns_and_refuses_redirecting_clients(monkeypatch, tmp_path):
    from openfoia.records.base import download_to_file

    class Client:
        follow_redirects = False

        def stream(self, *a, **k):
            raise AssertionError("request made")

    _fake_dns(monkeypatch, "192.168.0.10")
    with pytest.raises(ValueError):
        asyncio.run(
            download_to_file(Client(), "https://x.example/a", tmp_path / "a", 10, egress=DIRECT)
        )
    _fake_dns(monkeypatch, "93.184.215.14")
    redirecting = Client()
    redirecting.follow_redirects = True
    with pytest.raises(ValueError, match="redirect"):
        asyncio.run(
            download_to_file(redirecting, "https://x.example/a", tmp_path / "a", 10, egress=DIRECT)
        )
    assert not (tmp_path / "a").exists()
