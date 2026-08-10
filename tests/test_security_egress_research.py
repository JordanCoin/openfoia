"""Security regression tests: crossref + web-archive route through egress.

Principle 1: data never leaves the machine unless the user explicitly
chooses. `openfoia.net.egress_client()` is the single place every
network-touching module should build its httpx client from.

Two modules were exceptions until now:
- `crossref.py` sent every source-checker's request through an adapter that
  built its own client (fine, already fixed elsewhere) EXCEPT
  `_check_opensanctions`, which opened a bare `httpx.AsyncClient` itself.
- `pipeline/web.py` hand-rolled a SOCKS transport for `use_tor` and, worse,
  sent a self-identifying User-Agent
  ("OpenFOIA/1.0; +https://github.com/JordanCoin/openfoia") on every
  request — a fingerprint that ties every fetched page straight back to
  this tool and its author, regardless of DIRECT vs TOR routing.

These tests pin:
1. No bare `httpx.AsyncClient(`/`AsyncHTTPTransport` construction remains in
   either module (source scan).
2. `pipeline/web.py` no longer sends the identifying UA, and a fetched
   request actually carries the generic `DEFAULT_USER_AGENT` on the wire.
3. `crossref_entities(..., egress=...)` threads that policy down to both an
   adapter-based source (MuckRock) and the module's own-client source
   (OpenSanctions).
4. The per-source rate-limit sleep is jittered but never sleeps less than
   the documented base delay.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from openfoia import crossref as crossref_mod
from openfoia.crossref import _SOURCE_DELAYS, crossref_entities
from openfoia.models import EntityType
from openfoia.net import DEFAULT_USER_AGENT, EgressMode, EgressPolicy
from openfoia.pipeline import web as web_mod
from openfoia.pipeline.web import archive_url, fetch_url

REPO_ROOT = Path(__file__).resolve().parent.parent
CROSSREF_PATH = REPO_ROOT / "openfoia" / "crossref.py"
WEB_PATH = REPO_ROOT / "openfoia" / "pipeline" / "web.py"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeEntity:
    """Stand-in for an ExtractedEntity — crossref only reads these attrs."""

    def __init__(
        self, name: str, entity_type: EntityType = EntityType.PERSON, confidence: float = 1.0
    ):
        self.normalized_text = name
        self.entity_type = entity_type
        self.confidence = confidence


class _FakeResponse:
    def __init__(self, json_data: dict, status_code: int = 200):
        self._json = json_data
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self) -> dict:
        return self._json


class _FakeAsyncClient:
    """Stands in for the object egress_client() would normally return."""

    def __init__(self, get_responses=None):
        self._get_responses = list(get_responses or [])
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if self._get_responses:
            return self._get_responses.pop(0)
        return _FakeResponse({})


# ---------------------------------------------------------------------------
# Source-scan anti-regression guards
# ---------------------------------------------------------------------------


def test_no_bare_httpx_asyncclient_in_crossref():
    src = CROSSREF_PATH.read_text()
    assert "httpx.AsyncClient(" not in src


def test_no_bare_httpx_asyncclient_or_transport_in_web_pipeline():
    src = WEB_PATH.read_text()
    assert "httpx.AsyncClient(" not in src
    assert "AsyncHTTPTransport" not in src


# ---------------------------------------------------------------------------
# Self-identifying User-Agent must be gone from pipeline/web.py
# ---------------------------------------------------------------------------


def test_web_pipeline_source_has_no_self_identifying_user_agent():
    src = WEB_PATH.read_text()
    assert "OpenFOIA/1.0" not in src
    assert "github.com/JordanCoin" not in src


def test_fetch_url_sends_generic_default_user_agent(monkeypatch):
    """Drive fetch_url end-to-end (real egress_client, fake transport) and
    capture the User-Agent that actually hits the wire."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["user_agent"] = request.headers.get("user-agent")
        return httpx.Response(200, text="<html><title>T</title><body>hi</body></html>")

    from openfoia import net as net_mod

    real_egress_client = net_mod.egress_client

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        return real_egress_client(
            policy,
            timeout=timeout,
            isolation_token=isolation_token,
            headers=headers,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(web_mod, "egress_client", fake_egress_client)

    result = asyncio.run(fetch_url("https://example.com/"))

    assert seen["user_agent"] == DEFAULT_USER_AGENT
    assert "openfoia" not in seen["user_agent"].lower()
    assert "github" not in seen["user_agent"].lower()
    assert result.used_tor is False


def test_fetch_url_use_tor_true_yields_tor_policy(monkeypatch):
    captured: dict = {}

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        captured["policy"] = policy
        fake = _FakeAsyncClient(
            get_responses=[
                _FakeResponse({}, status_code=200),
            ]
        )
        # fetch_url calls client.get(url) then reads response.text — patch
        # _FakeResponse to behave like an httpx.Response with .text
        return fake

    async def _get(self, url, **kwargs):
        resp = _FakeResponse({})
        resp.text = "<html><title>T</title><body>hi</body></html>"
        return resp

    monkeypatch.setattr(_FakeAsyncClient, "get", _get)
    monkeypatch.setattr(web_mod, "egress_client", fake_egress_client)

    result = asyncio.run(fetch_url("https://example.com/", use_tor=True))

    assert captured["policy"].mode is EgressMode.TOR
    assert result.used_tor is True


def test_fetch_url_egress_param_wins_over_use_tor(monkeypatch):
    """egress= takes precedence over use_tor= when both are given."""
    captured: dict = {}

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        captured["policy"] = policy
        return _FakeAsyncClient()

    async def _get(self, url, **kwargs):
        resp = _FakeResponse({})
        resp.text = "<html><title>T</title><body>hi</body></html>"
        return resp

    monkeypatch.setattr(_FakeAsyncClient, "get", _get)
    monkeypatch.setattr(web_mod, "egress_client", fake_egress_client)

    direct_policy = EgressPolicy()
    result = asyncio.run(fetch_url("https://example.com/", use_tor=True, egress=direct_policy))

    assert captured["policy"] is direct_policy
    assert result.used_tor is False


def test_archive_url_threads_egress_through_to_fetch_url(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        captured["policy"] = policy
        return _FakeAsyncClient()

    async def _get(self, url, **kwargs):
        resp = _FakeResponse({})
        resp.text = "<html><title>T</title><body>hi</body></html>"
        return resp

    monkeypatch.setattr(_FakeAsyncClient, "get", _get)
    monkeypatch.setattr(web_mod, "egress_client", fake_egress_client)

    tor_policy = EgressPolicy(mode=EgressMode.TOR)
    result = asyncio.run(archive_url("https://example.com/", tmp_path, egress=tor_policy))

    assert captured["policy"] is tor_policy
    assert result.metadata["used_tor"] is True


# ---------------------------------------------------------------------------
# crossref_entities(..., egress=...) threads the policy to checkers
# ---------------------------------------------------------------------------


def test_crossref_entities_threads_tor_policy_to_adapter_source(monkeypatch):
    """MuckRockAdapter (an adapter-based checker) must receive the policy."""
    captured: dict = {}

    class _FakeMuckRockAdapter:
        def __init__(self, *args, egress=None, **kwargs):
            captured["muckrock_egress"] = egress

        async def search(self, name, page_size=5):
            from openfoia.records.base import SearchResult

            return SearchResult(source="muckrock", query=name, total_results=0, entities=[])

    monkeypatch.setattr("openfoia.records.muckrock.MuckRockAdapter", _FakeMuckRockAdapter)

    # Restrict to a single remote source so the test is fast and targeted.
    tor_policy = EgressPolicy(mode=EgressMode.TOR)
    entities = [_FakeEntity("Acme Corp", EntityType.ORGANIZATION)]

    asyncio.run(
        crossref_entities(
            entities,
            sources=["muckrock"],
            allow_network=True,
            egress=tor_policy,
        )
    )

    assert captured["muckrock_egress"] is tor_policy
    assert captured["muckrock_egress"].mode is EgressMode.TOR


def test_crossref_entities_threads_tor_policy_to_own_client_source(monkeypatch):
    """OpenSanctions builds its own client via egress_client() — cover it too."""
    captured: dict = {}

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        captured["policy"] = policy
        return _FakeAsyncClient(get_responses=[_FakeResponse({"results": []})])

    monkeypatch.setattr(crossref_mod, "egress_client", fake_egress_client)

    tor_policy = EgressPolicy(mode=EgressMode.TOR)
    entities = [_FakeEntity("Acme Corp", EntityType.ORGANIZATION)]

    asyncio.run(
        crossref_entities(
            entities,
            sources=["opensanctions"],
            allow_network=True,
            egress=tor_policy,
        )
    )

    assert captured["policy"] is tor_policy
    assert captured["policy"].mode is EgressMode.TOR


def test_crossref_entities_defaults_to_direct_when_egress_unspecified(monkeypatch):
    """No behavior change for existing callers: default is a plain DIRECT policy."""
    captured: dict = {}

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        captured["policy"] = policy
        return _FakeAsyncClient(get_responses=[_FakeResponse({"results": []})])

    monkeypatch.setattr(crossref_mod, "egress_client", fake_egress_client)

    entities = [_FakeEntity("Acme Corp", EntityType.ORGANIZATION)]

    asyncio.run(
        crossref_entities(
            entities,
            sources=["opensanctions"],
            allow_network=True,
        )
    )

    assert captured["policy"] is None  # egress_client(None, ...) == DIRECT


def test_crossref_entities_allow_network_gate_unaffected_by_egress():
    """allow_network is orthogonal to egress and still gates remote sources."""
    entities = [_FakeEntity("Acme Corp", EntityType.ORGANIZATION)]

    with pytest.raises(PermissionError):
        asyncio.run(
            crossref_entities(
                entities,
                sources=["opensanctions"],
                egress=EgressPolicy(mode=EgressMode.TOR),
                # allow_network defaults to False
            )
        )


# ---------------------------------------------------------------------------
# Timing-jitter: never sleeps less than the documented base delay
# ---------------------------------------------------------------------------


def test_crossref_rate_limit_jitter_never_sleeps_below_base_delay(monkeypatch):
    """Monkeypatch random to its extremes and asyncio.sleep to record
    durations; every recorded sleep must be >= the documented base delay
    for that source, and the two extremes must differ (proof of jitter)."""
    sleeps: list[float] = []

    async def fake_sleep(duration):
        sleeps.append(duration)

    # crossref_entities does `import asyncio` locally, but that's the same
    # sys.modules object as the `asyncio` imported here, so patching it here
    # reaches the real call site.
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        return _FakeAsyncClient(get_responses=[_FakeResponse({"results": []})])

    monkeypatch.setattr(crossref_mod, "egress_client", fake_egress_client)

    entities = [_FakeEntity("Acme Corp", EntityType.ORGANIZATION)]
    base_delay = _SOURCE_DELAYS["opensanctions"]

    # random.random() == 0.0 -> minimum jitter factor (1.0x base delay)
    monkeypatch.setattr(crossref_mod.random, "random", lambda: 0.0)
    sleeps.clear()
    asyncio.run(crossref_entities(entities, sources=["opensanctions"], allow_network=True))
    assert sleeps[-1] == pytest.approx(base_delay * 1.0)

    # random.random() == 1.0 -> maximum jitter factor (~1.5x base delay)
    monkeypatch.setattr(crossref_mod.random, "random", lambda: 1.0)
    sleeps.clear()
    asyncio.run(crossref_entities(entities, sources=["opensanctions"], allow_network=True))
    assert sleeps[-1] == pytest.approx(base_delay * 1.5)

    # Across the whole documented range, jitter must never sleep less than
    # the floor — a lower jitter would violate the source's rate limit.
    for r in (0.0, 0.25, 0.5, 0.75, 1.0):
        monkeypatch.setattr(crossref_mod.random, "random", lambda r=r: r)
        sleeps.clear()
        asyncio.run(crossref_entities(entities, sources=["opensanctions"], allow_network=True))
        assert sleeps[-1] >= base_delay - 1e-9
