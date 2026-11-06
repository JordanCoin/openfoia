"""RetryTransport: retries, Retry-After, method safety, per-host spacing."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from openfoia import net
from openfoia.net import EgressMode, EgressPolicy, egress_client


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []

    async def _record(delay):
        calls.append(delay)

    monkeypatch.setattr(net, "_sleep", _record)
    monkeypatch.setattr(net, "MIN_HOST_INTERVAL", 0.0)
    return calls


def _run(handler, method="GET", url="https://api.example.org/x"):
    async def go():
        async with egress_client(None, transport=httpx.MockTransport(handler)) as client:
            return await client.request(method, url)

    return asyncio.run(go())


def _sequence(*responses):
    calls = []

    def handler(request):
        calls.append(request)
        item = responses[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return handler, calls


def test_retries_429_then_succeeds(sleeps):
    handler, calls = _sequence(httpx.Response(429), httpx.Response(200, text="ok"))
    resp = _run(handler)
    assert resp.status_code == 200
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_gives_up_after_max_attempts(sleeps):
    handler, calls = _sequence(*[httpx.Response(503)] * net.MAX_ATTEMPTS)
    assert _run(handler).status_code == 503
    assert len(calls) == net.MAX_ATTEMPTS


def test_connect_error_retried_then_raised(sleeps):
    err = httpx.ConnectError("refused")
    handler, calls = _sequence(*[err] * net.MAX_ATTEMPTS)
    with pytest.raises(httpx.ConnectError):
        _run(handler)
    assert len(calls) == net.MAX_ATTEMPTS


def test_read_timeout_retried(sleeps):
    handler, calls = _sequence(httpx.ReadTimeout("slow"), httpx.Response(200))
    assert _run(handler).status_code == 200
    assert len(calls) == 2


def test_retry_after_honored(sleeps):
    handler, _ = _sequence(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200))
    _run(handler)
    assert sleeps == [7.0]


def test_long_retry_after_returned_to_caller(sleeps):
    handler, calls = _sequence(httpx.Response(429, headers={"Retry-After": "3600"}))
    assert _run(handler).status_code == 429
    assert len(calls) == 1
    assert sleeps == []


def test_post_not_retried(sleeps):
    handler, calls = _sequence(httpx.Response(503), httpx.Response(200))
    assert _run(handler, method="POST").status_code == 503
    assert len(calls) == 1


def test_404_not_retried(sleeps):
    handler, calls = _sequence(httpx.Response(404), httpx.Response(200))
    assert _run(handler).status_code == 404
    assert len(calls) == 1


def test_per_host_spacing(monkeypatch):
    sleeps: list[float] = []

    async def _sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(net.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(net, "_sleep", _sleep)

    async def go():
        for host in ("a.example", "a.example", "a.example", "b.example"):
            await net._throttle(host)

    asyncio.run(go())
    assert sleeps == pytest.approx([net.MIN_HOST_INTERVAL, 2 * net.MIN_HOST_INTERVAL])


def test_tor_mode_keeps_socks_proxy_under_retry():
    pytest.importorskip("socksio")
    client = egress_client(EgressPolicy(mode=EgressMode.TOR, isolate_streams=False))
    try:
        transport = client._transport_for_url(httpx.URL("https://api.example.org/"))
        assert isinstance(transport, net.RetryTransport)
        assert "socks" in type(transport._inner._pool).__name__.lower()
    finally:
        asyncio.run(client.aclose())
