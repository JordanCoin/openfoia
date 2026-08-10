"""Security regression tests: records adapters route through the egress choke point.

Principle 1: data never leaves the machine unless the user explicitly
chooses. `openfoia.net.egress_client()` is the single place every
network-touching module should build its httpx client from — a records
adapter that constructs `httpx.AsyncClient` directly bypasses DIRECT-vs-TOR
routing, Tor stream isolation, and the non-identifying default User-Agent.

These tests pin two things:
1. No adapter module constructs `httpx.AsyncClient` directly anymore (source
   scan, same style as tests/test_security_network.py).
2. An `EgressPolicy` passed into an adapter's constructor actually reaches
   the client that performs the HTTP call — for both the shared
   `RecordAdapter._request` path (SEC EDGAR) and an adapter that opens its
   own client (MuckRock) — and that SEC EDGAR's legally-required descriptive
   User-Agent still overrides the generic default.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from openfoia.net import DEFAULT_USER_AGENT, EgressMode, EgressPolicy, egress_client
from openfoia.records.documentcloud import DocumentCloudAdapter
from openfoia.records.fec import FECAdapter
from openfoia.records.govinfo import GovInfoAdapter
from openfoia.records.muckrock import MuckRockAdapter
from openfoia.records.opencorporates import OpenCorporatesAdapter
from openfoia.records.propublica_nonprofit import ProPublicaNonprofitAdapter
from openfoia.records.regulations import RegulationsGovAdapter
from openfoia.records.sec_edgar import DEFAULT_USER_AGENT as SEC_DEFAULT_USER_AGENT
from openfoia.records.sec_edgar import SECEdgarAdapter
from openfoia.records.usaspending import USASpendingAdapter

RECORDS_DIR = Path(__file__).resolve().parent.parent / "openfoia" / "records"

# ---------------------------------------------------------------------------
# Fakes shared across the behavior tests below
# ---------------------------------------------------------------------------


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
    """Stands in for the object egress_client() would normally return.

    Supports the same `async with ... as client:` usage and records every
    call made against it so tests can assert on what was sent.
    """

    def __init__(self, get_responses=None, post_responses=None):
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])
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

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if self._post_responses:
            return self._post_responses.pop(0)
        return _FakeResponse({})


# ---------------------------------------------------------------------------
# Source-scan anti-regression guard
# ---------------------------------------------------------------------------


def test_no_bare_httpx_asyncclient_in_records_adapters():
    """Every records adapter must build its client via egress_client()."""
    offenders = []
    for path in sorted(RECORDS_DIR.glob("*.py")):
        if "httpx.AsyncClient(" in path.read_text():
            offenders.append(path.name)

    assert offenders == [], (
        f"records modules still construct httpx.AsyncClient directly: {offenders}"
    )


# ---------------------------------------------------------------------------
# Constructor contract: every adapter accepts egress=EgressPolicy(...)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_cls",
    [
        MuckRockAdapter,
        OpenCorporatesAdapter,
        SECEdgarAdapter,
        FECAdapter,
        RegulationsGovAdapter,
        GovInfoAdapter,
        ProPublicaNonprofitAdapter,
        USASpendingAdapter,
        DocumentCloudAdapter,
    ],
)
def test_adapter_accepts_egress_policy_kwarg(adapter_cls):
    policy = EgressPolicy(mode=EgressMode.TOR)
    adapter = adapter_cls(egress=policy)
    assert adapter._egress is policy


@pytest.mark.parametrize(
    "adapter_cls",
    [
        MuckRockAdapter,
        OpenCorporatesAdapter,
        SECEdgarAdapter,
        FECAdapter,
        RegulationsGovAdapter,
        GovInfoAdapter,
        ProPublicaNonprofitAdapter,
        USASpendingAdapter,
        DocumentCloudAdapter,
    ],
)
def test_adapter_defaults_to_direct_policy_when_unspecified(adapter_cls):
    """No behavior change for existing callers: default is a plain DIRECT policy."""
    adapter = adapter_cls()
    assert adapter._egress == EgressPolicy()
    assert adapter._egress.mode is EgressMode.DIRECT


# ---------------------------------------------------------------------------
# Behavior: the policy passed to the adapter reaches egress_client()
# ---------------------------------------------------------------------------


def test_muckrock_search_threads_policy_to_own_client(monkeypatch):
    """MuckRock builds its own client (not via _request) — cover that path."""
    import openfoia.records.muckrock as muckrock_mod

    captured: dict = {}
    fake_client = _FakeAsyncClient(
        get_responses=[
            _FakeResponse(
                {
                    "count": 1,
                    "results": [
                        {
                            "id": 1,
                            "title": "Test FOIA request",
                            "slug": "test-foia-request",
                            "status": "done",
                        }
                    ],
                }
            )
        ]
    )

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        captured["policy"] = policy
        return fake_client

    monkeypatch.setattr(muckrock_mod, "egress_client", fake_egress_client)

    policy = EgressPolicy(mode=EgressMode.TOR)
    adapter = MuckRockAdapter(egress=policy)
    result = asyncio.run(adapter.search("surveillance"))

    assert captured["policy"] is policy
    assert captured["policy"].mode is EgressMode.TOR
    assert result.total_results == 1
    assert result.entities[0].name == "Test FOIA request"


def test_muckrock_search_agencies_threads_policy(monkeypatch):
    """The agency-search branch opens its own client too — separate call site."""
    import openfoia.records.muckrock as muckrock_mod

    captured: dict = {}
    fake_client = _FakeAsyncClient(get_responses=[_FakeResponse({"count": 0, "results": []})])

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        captured["policy"] = policy
        return fake_client

    monkeypatch.setattr(muckrock_mod, "egress_client", fake_egress_client)

    policy = EgressPolicy(mode=EgressMode.TOR)
    adapter = MuckRockAdapter(egress=policy)
    asyncio.run(adapter.search("FBI", agency=True))

    assert captured["policy"] is policy


def test_sec_edgar_search_threads_policy_via_shared_request(monkeypatch):
    """SEC EDGAR only ever goes through RecordAdapter._request — cover that path."""
    import openfoia.records.base as base_mod

    captured: dict = {}
    fake_client = _FakeAsyncClient(
        get_responses=[_FakeResponse({"hits": {"total": 0, "hits": []}})]
    )

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        captured["policy"] = policy
        captured["headers"] = headers
        return fake_client

    monkeypatch.setattr(base_mod, "egress_client", fake_egress_client)

    policy = EgressPolicy(mode=EgressMode.TOR)
    adapter = SECEdgarAdapter(egress=policy)
    result = asyncio.run(adapter.search("Palantir"))

    assert captured["policy"] is policy
    assert captured["policy"].mode is EgressMode.TOR
    assert result.error is None
    assert result.total_results == 0


# ---------------------------------------------------------------------------
# SEC EDGAR's descriptive User-Agent must survive egress routing
# ---------------------------------------------------------------------------


def test_sec_edgar_sends_descriptive_user_agent_not_generic_default(monkeypatch):
    """_request() passes _edgar_headers() as per-call headers to client.get();
    those must reach the wire even though egress_client's own default is the
    generic browser UA."""
    import openfoia.records.base as base_mod

    fake_client = _FakeAsyncClient(
        get_responses=[_FakeResponse({"hits": {"total": 0, "hits": []}})]
    )

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        return fake_client

    monkeypatch.setattr(base_mod, "egress_client", fake_egress_client)

    adapter = SECEdgarAdapter()
    asyncio.run(adapter.search("Palantir"))

    method, url, kwargs = fake_client.calls[0]
    sent_headers = kwargs.get("headers") or {}
    assert sent_headers.get("User-Agent") == SEC_DEFAULT_USER_AGENT
    assert sent_headers.get("User-Agent") != DEFAULT_USER_AGENT


def test_sec_edgar_ua_wins_through_the_real_egress_client_header_merge(monkeypatch):
    """End-to-end proof using the REAL net.egress_client() header-merge logic
    (only the transport is swapped for an in-memory one — no sockets)."""
    import openfoia.records.base as base_mod

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["user_agent"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"hits": {"total": 0, "hits": []}})

    def fake_egress_client(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        return egress_client(
            policy,
            timeout=timeout,
            isolation_token=isolation_token,
            headers=headers,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(base_mod, "egress_client", fake_egress_client)

    adapter = SECEdgarAdapter()
    asyncio.run(adapter.search("Palantir"))

    assert seen["user_agent"] == SEC_DEFAULT_USER_AGENT
    assert seen["user_agent"] != DEFAULT_USER_AGENT
