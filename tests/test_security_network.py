"""Security regression tests: data leaving the machine.

Principle 1: data never leaves the machine unless the user explicitly
chooses. No silent cloud calls. These tests pin the places that broke it.
"""

from __future__ import annotations

import re

import pytest


# ---------------------------------------------------------------------------
# The local web UI must not phone home
# ---------------------------------------------------------------------------


def _served_html() -> str:
    from openfoia import server

    src = server.__file__
    with open(src) as f:
        return f.read()


def test_web_ui_loads_no_external_resources():
    """A CDN <script> tells Cloudflare (and any observer) when you work."""
    html = _served_html()

    external = re.findall(r"""(?:src|href)\s*=\s*["'](https?://[^"']+)["']""", html)
    # Links the user deliberately clicks are fine; loaded subresources are not.
    loaded = [u for u in external if not u.startswith("https://github.com/")]

    assert loaded == [], f"web UI loads external resources: {loaded}"


def test_web_ui_has_no_cdn_script_tag():
    assert "cdn.tailwindcss.com" not in _served_html()


def _loopback_client(tmp_path):
    from fastapi.testclient import TestClient

    from openfoia.server import create_app

    app = create_app(token="t", data_dir=tmp_path)
    return TestClient(app, base_url="http://127.0.0.1")


def test_server_sends_a_restrictive_csp(tmp_path):
    """Defence in depth: structurally block off-machine subresources."""
    resp = _loopback_client(tmp_path).get("/api/health")

    csp = resp.headers.get("content-security-policy", "")
    assert csp, "no Content-Security-Policy header"
    assert "default-src 'self'" in csp
    assert "connect-src 'self'" in csp


def test_server_rejects_non_loopback_host_header(tmp_path):
    """DNS rebinding: a public name resolving to 127.0.0.1 must not work."""
    from fastapi.testclient import TestClient

    from openfoia.server import create_app

    client = TestClient(create_app(token="t", data_dir=tmp_path), base_url="http://evil.example")
    resp = client.get("/api/health")

    assert resp.status_code == 400


def test_server_does_not_leak_token_via_referrer(tmp_path):
    """The token rides in the query string; keep it out of Referer/caches."""
    resp = _loopback_client(tmp_path).get("/api/health")

    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert "no-store" in resp.headers.get("cache-control", "")


# ---------------------------------------------------------------------------
# Web archive must not persist live trackers
# ---------------------------------------------------------------------------


def test_archive_strips_trackers_from_saved_html(tmp_path):
    """Opening an archived page later must not fire beacons."""
    import asyncio

    from openfoia.pipeline import web

    raw = (
        "<html><head>"
        '<script src="https://www.google-analytics.com/analytics.js"></script>'
        "</head><body><article>Real content here</article>"
        '<img src="https://facebook.com/tr?id=1">'
        "</body></html>"
    )

    class _Resp:
        text = raw
        status_code = 200

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    import httpx

    orig = httpx.AsyncClient
    httpx.AsyncClient = _Client
    try:
        result = asyncio.run(web.archive_url("https://example.test/page", tmp_path))
    finally:
        httpx.AsyncClient = orig

    saved = (tmp_path / result.html_path).read_text() if False else open(result.html_path).read()

    assert "google-analytics.com" not in saved
    assert "facebook.com/tr" not in saved
    assert "Real content here" in saved


# ---------------------------------------------------------------------------
# Crossref must gate network access at the library layer, not just the CLI
# ---------------------------------------------------------------------------


def test_crossref_refuses_network_without_explicit_optin():
    """An agent or script caller must not silently send subject names out."""
    import asyncio

    from openfoia.crossref import crossref_entities

    with pytest.raises(PermissionError, match="network"):
        asyncio.run(crossref_entities(["Jane Doe"], allow_network=False))


def test_crossref_defaults_to_no_network():
    """Default must be safe: opt-in, not opt-out."""
    import asyncio
    import inspect

    from openfoia.crossref import crossref_entities

    sig = inspect.signature(crossref_entities)
    assert sig.parameters["allow_network"].default is False

    with pytest.raises(PermissionError):
        asyncio.run(crossref_entities(["Jane Doe"]))
