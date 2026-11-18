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
    "Acme \x1b]52;c;cm0gLXJmIH4K\x1b\\ \x1b]0;pwned\x07 \x9b31m \u202eevil\u202c "
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
    return not any(ch in text for ch in "\x1b\x07\x9b\u202e\u202c")


# ---------------------------------------------------------------------------
# A8: the shared sanitizer
# ---------------------------------------------------------------------------


def test_safe_text_strips_controls_and_bidi_but_keeps_joiners():
    from openfoia.termsafe import safe_text

    persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"  # ZWNJ is part of the spelling
    family = "\U0001f469\u200d\U0001f467"  # ZWJ emoji sequence
    out = safe_text(f"{HOSTILE} {persian} {family} \ud800")
    assert _no_controls(out)
    assert "\ud800" not in out
    assert persian in out and family in out
    assert "\n" not in safe_text("a\nb\tc") and safe_text("a\nb\tc") == "a b c"


def test_safe_text_multiline_keeps_layout_only():
    from openfoia.termsafe import safe_text

    out = safe_text("line1\r\nline2\x1b[31m\tcol\u2028line3", multiline=True)
    assert out == "line1\nline2[31m\tcol\nline3"


def test_search_clean_is_the_shared_sanitizer():
    from openfoia import search, termsafe

    assert search.clean is termsafe.safe_text


def test_safe_markup_is_never_parsed_as_markup():
    from rich.console import Console

    from openfoia.termsafe import safe_markup

    console = Console(force_terminal=True, color_system=None, width=200)
    with console.capture() as cap:
        console.print(f"[bold]{safe_markup(HOSTILE)}[/bold]")
    out = cap.get()
    assert _no_controls(out)
    assert "[link=https://evil.example/x]Verify[/link]" in out
    assert "\x1b]8;" not in out  # no OSC 8 hyperlink


def test_safe_json_escapes_controls_and_round_trips():
    from openfoia.termsafe import safe_json

    value = {"name": HOSTILE + " محمد \u200d"}
    text = safe_json(value)
    assert _no_controls(text)
    assert "محمد" in text  # readable non-ASCII is kept
    assert json.loads(text) == value


def test_safe_console_backstop_strips_escapes_from_unescaped_strings():
    from openfoia.termsafe import SafeConsole

    console = SafeConsole(force_terminal=True, color_system=None, width=200)
    with console.capture() as cap:
        console.print("plain " + "\x1b]52;c;AAAA\x1b\\" + " end")
    assert "\x1b" not in cap.get()


@pytest.mark.parametrize(
    "source,ids",
    [
        ("muckrock", {"muckrock_id": "\x1b]0;x\x07 1"}),
        ("documentcloud", {"documentcloud_id": "1"}),
        ("sec", {"cik": "1"}),
        ("federalregister", {"document_number": "2026-1"}),
        ("futuresource", {}),
    ],
)
def test_records_tables_strip_escapes_and_markup_for_every_source(capsys, source, ids):
    from openfoia import cli
    from openfoia.records.base import RecordEntity, SearchResult

    entity = RecordEntity(
        entity_type="ORGANIZATION",
        name=HOSTILE,
        source=source,
        identifiers=ids,
        extra_data={"username": HOSTILE, "highlights": [HOSTILE], "filing_type": HOSTILE},
    )
    result = SearchResult(source=source, query=HOSTILE, total_results=1, entities=[entity])
    cli._print_search_result(result, source, HOSTILE, 5, False, False, None)
    out = capsys.readouterr().out
    assert _no_controls(out), repr(out)
    assert "\x1b]8;" not in out


@pytest.mark.parametrize("raw,json_lines", [(True, False), (False, True)])
def test_records_json_output_has_no_raw_controls(capsys, raw, json_lines):
    from openfoia import cli
    from openfoia.records.base import RecordEntity, SearchResult

    entity = RecordEntity(entity_type="ORGANIZATION", name=HOSTILE, source="opencorporates")
    result = SearchResult(source="opencorporates", query="q", total_results=1, entities=[entity])
    cli._print_search_result(result, "opencorporates", "q", 5, raw, json_lines, None)
    out = capsys.readouterr().out
    assert _no_controls(out), repr(out)
    # The value is preserved, only escaped: it decodes back to the original.
    if raw:
        assert json.loads(out)[0]["name"] == HOSTILE
    else:
        assert json.loads(out.splitlines()[0])["title"] == HOSTILE


def test_crossref_style_markup_strings_escape_upstream_values(capsys):
    """rprint (the module console) + _safe: the pattern every CLI site uses."""
    from openfoia import cli

    cli.rprint(f"[red]FLAGGED: {cli._safe(HOSTILE)}[/red]")
    out = capsys.readouterr().out
    assert _no_controls(out)
    assert "[link=https://evil.example/x]" in out


def test_echo_keeps_document_layout_but_drops_escapes(capsys):
    from openfoia import cli

    cli._echo(f"--- page 1 ---\n{HOSTILE}\n\tindented")
    out = capsys.readouterr().out
    assert _no_controls(out)
    assert "--- page 1 ---\n" in out and "\n\tindented" in out


# ---------------------------------------------------------------------------
# A9: document text and LLMs off this machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,local",
    [
        ("http://localhost:11434", True),
        ("http://LOCALHOST.:11434", True),
        ("http://127.0.0.1:8080/v1", True),
        ("http://127.9.9.9/v1", True),
        ("http://[::1]:11434", True),
        ("https://localhost.attacker.example/v1", False),
        ("https://attacker.example/localhost/v1", False),
        ("https://127.0.0.1.attacker.example/v1", False),
        ("http://gpu-box.example.net:11434", False),
        ("http://10.0.0.5:11434", False),  # the LAN is not this machine
        ("http://0.0.0.0:11434", False),
        ("", False),
        (None, False),
        ("not a url", False),
    ],
)
def test_is_loopback_url(url, local):
    from openfoia.pipeline.extract import is_loopback_url

    assert is_loopback_url(url) is local


def _no_llm_calls(monkeypatch):
    from openfoia.pipeline import extract

    def boom(*a, **k):
        raise AssertionError("document text was sent to the LLM")

    for name in ("_call_ollama", "_call_openai", "_call_anthropic"):
        monkeypatch.setattr(extract, name, boom)
    # Keep these tests on the regex tier: no NER models loaded.
    monkeypatch.setattr(extract, "_gliner_available", lambda: False)
    monkeypatch.setattr(extract, "_spacy_available", lambda: False)
    probes = []
    monkeypatch.setattr(extract, "_llm_available", lambda *a: probes.append(a) or True)
    return probes


def test_remote_ollama_without_consent_is_refused_and_never_probed(data_dir, monkeypatch, capsys):
    from openfoia.pipeline import extract

    probes = _no_llm_calls(monkeypatch)
    ex = extract.EntityExtractor(provider="ollama", base_url="http://gpu-box.example.net:11434")
    assert ex._resolve_backend() != "llm"
    err = capsys.readouterr().err
    assert "WARNING" in err and "gpu-box.example.net" in err and "allow_remote" in err
    assert probes == [], "the remote host was contacted before consent"
    result = asyncio.run(ex.extract("Jane Doe met John Smith at Acme Corp in Springfield."))
    assert result.entities is not None  # local extraction still works


def test_forced_llm_tier_without_consent_raises(data_dir, monkeypatch):
    from openfoia.pipeline import extract

    _no_llm_calls(monkeypatch)
    ex = extract.EntityExtractor(
        provider="openai", api_key="k", base_url="https://api.example.com/v1", tier="llm"
    )
    with pytest.raises(extract.RemoteLLMNotAllowedError):
        asyncio.run(ex.extract("Jane Doe met John Smith."))


def test_llm_chunk_call_checks_consent_itself(data_dir, monkeypatch):
    """Defense in depth: even a caller that skips _resolve_backend cannot send."""
    from openfoia.pipeline import extract

    _no_llm_calls(monkeypatch)
    ex = extract.EntityExtractor(provider="anthropic", api_key="k")
    with pytest.raises(extract.RemoteLLMNotAllowedError):
        ex._extract_chunk_llm("Jane Doe", None, 1)


def test_consent_in_config_env_allows_and_still_warns(data_dir, monkeypatch, capsys):
    from openfoia.pipeline import extract

    _no_llm_calls(monkeypatch)
    monkeypatch.setenv("OPENFOIA_AI_ALLOW_REMOTE", "1")
    ex = extract.EntityExtractor(provider="openai", api_key="k")
    assert ex._resolve_backend() == "llm"
    err = capsys.readouterr().err
    assert "WARNING" in err and "api.openai.com" in err


def test_tor_mode_refuses_remote_llm_even_with_consent(data_dir, monkeypatch, capsys):
    from openfoia.pipeline import extract

    probes = _no_llm_calls(monkeypatch)
    monkeypatch.setenv("OPENFOIA_TOR", "1")
    ex = extract.EntityExtractor(provider="openai", api_key="k", allow_remote=True)
    assert ex._resolve_backend() != "llm"
    err = capsys.readouterr().err
    assert "Tor" in err and "not sending" in err
    assert probes == []


def test_tor_mode_keeps_local_ollama(data_dir, monkeypatch, capsys):
    from openfoia.pipeline import extract

    _no_llm_calls(monkeypatch)
    monkeypatch.setenv("OPENFOIA_TOR", "1")
    ex = extract.EntityExtractor(provider="ollama", base_url="http://127.0.0.1:11434")
    assert ex._resolve_backend() == "llm"
    assert "WARNING" not in capsys.readouterr().err


def test_agent_path_uses_the_same_consent_rule(data_dir, monkeypatch):
    """agent.py builds EntityExtractor() from config: no consent, no remote send."""
    from openfoia.pipeline import extract

    _no_llm_calls(monkeypatch)
    monkeypatch.setenv("OPENFOIA_AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENFOIA_OPENAI_API_KEY", "k")
    ex = extract.EntityExtractor()
    assert ex.provider == "openai"
    assert ex._resolve_backend() != "llm"


def test_allow_remote_config_requires_a_real_true(tmp_path):
    from openfoia.config import OpenFOIAConfig, _merge_config

    assert _merge_config(OpenFOIAConfig(), {"ai": {"allow_remote": "yes"}}).ai.allow_remote is False
    assert _merge_config(OpenFOIAConfig(), {"ai": {"allow_remote": True}}).ai.allow_remote is True


def test_cli_consent_prompt_is_not_assumed_without_a_terminal(data_dir, monkeypatch):
    from openfoia import cli

    monkeypatch.setenv("OPENFOIA_AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENFOIA_OPENAI_API_KEY", "k")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    assert cli._remote_ai_consent() is None  # falls back to config (off)


def test_entities_import_helpers_do_not_send_without_consent(data_dir, monkeypatch, capsys):
    from openfoia import cli

    _no_llm_calls(monkeypatch)
    monkeypatch.setenv("OPENFOIA_AI_PROVIDER", "ollama")
    monkeypatch.setenv("OPENFOIA_AI_BASE_URL", "http://gpu-box.example.net:11434")
    assert cli._llm_generate_regex("a US phone number") is None
    assert cli._llm_map_columns(["a", "b"], [["x", "y"]]) is None
    assert "WARNING" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# A12: SEC User-Agent
# ---------------------------------------------------------------------------


def test_sec_user_agent_over_tor_ignores_the_declared_one(monkeypatch):
    from openfoia.records import sec_edgar

    monkeypatch.setenv("OPENFOIA_SEC_USER_AGENT", "Jane Doe jane@example.org")
    assert sec_edgar._edgar_headers(TOR) == {"User-Agent": DEFAULT_USER_AGENT}


def test_sec_user_agent_direct_uses_the_declared_one(monkeypatch):
    from openfoia.records import sec_edgar

    monkeypatch.setenv("OPENFOIA_SEC_USER_AGENT", "Jane Doe jane@example.org")
    assert sec_edgar._edgar_headers(DIRECT) == {"User-Agent": "Jane Doe jane@example.org"}


def test_sec_user_agent_default_is_common_and_notice_is_shown_once(monkeypatch, capsys):
    from openfoia.records import sec_edgar

    monkeypatch.delenv("OPENFOIA_SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(sec_edgar, "_ua_notice_shown", False)
    assert sec_edgar._edgar_headers(DIRECT) == {"User-Agent": DEFAULT_USER_AGENT}
    assert sec_edgar._edgar_headers(DIRECT) == {"User-Agent": DEFAULT_USER_AGENT}
    err = capsys.readouterr().err
    assert err.count("fair-access") == 1 and "OPENFOIA_SEC_USER_AGENT" in err
    assert "research@example.org" not in sec_edgar.DEFAULT_USER_AGENT


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


@pytest.mark.parametrize(
    "host,expected",
    [
        ("2130706433", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("10.1", "10.0.0.1"),
        ("192.168.257", "192.168.1.1"),
        ("134744072", "8.8.8.8"),
        ("256.1.1.1", None),
        ("1.2.3.4.5", None),
        ("example", None),
    ],
)
def test_numeric_hosts_are_read_like_inet_aton(host, expected):
    """The same reading glibc's resolver gives, so the check sees the real target."""
    from openfoia.records.base import _inet_aton, validate_download_url

    got = _inet_aton(host)
    assert (str(got) if got else None) == expected
    if expected == "8.8.8.8":
        validate_download_url(f"https://{host}/")  # a public address in another spelling


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


# ---------------------------------------------------------------------------
# A14: SMTP
# ---------------------------------------------------------------------------


def test_smtp_ehlo_name_is_not_the_hostname(monkeypatch):
    from openfoia.gateways import email as email_mod
    from openfoia.gateways.base import DeliveryPayload

    seen = {}

    class SMTP:
        def __init__(self, host, port, local_hostname=None):
            seen["local_hostname"] = local_hostname

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg, to_addrs=None):
            pass

    monkeypatch.setattr(email_mod.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(socket, "getfqdn", lambda *a: "janes-macbook-pro.local")
    gw = email_mod.EmailGateway(smtp_host="smtp.example.org", smtp_user="u", smtp_password="p")
    asyncio.run(gw.send(DeliveryPayload("FOIA", "foia@agency.example", "subj", "body")))
    assert seen["local_hostname"] == email_mod.EHLO_NAME
    assert "janes" not in email_mod.EHLO_NAME


@pytest.mark.parametrize("tor", [True, False])
def test_request_send_warns_that_email_skips_tor(data_dir, monkeypatch, tor):
    from openfoia.cli import app
    from openfoia.gateways import email as email_mod
    from openfoia.gateways.base import DeliveryResult, DeliveryStatus

    sent = []

    async def fake_send(self, payload):
        sent.append(payload)
        return DeliveryResult(success=False, status=DeliveryStatus.FAILED, error_message="x")

    monkeypatch.setattr(email_mod.EmailGateway, "send", fake_send)
    monkeypatch.setenv("OPENFOIA_SMTP_USER", "u")
    monkeypatch.setenv("OPENFOIA_SMTP_PASSWORD", "p")
    if tor:
        monkeypatch.setenv("OPENFOIA_TOR", "1")
    r = runner.invoke(
        app,
        [
            "request",
            "send",
            "-a",
            "FBI",
            "-s",
            "Records",
            "-b",
            "Please send records.",
            "-n",
            "Jane",
            "-e",
            "jane@example.org",
            "--to",
            "foia@agency.example",
        ],
    )
    assert sent, r.output  # non-interactive: warned, then sent
    out = " ".join(r.output.split())
    assert ("not through Tor" in out) is tor
