"""Security regression tests: the outbound-HTTP egress choke point.

Principle 1: data never leaves the machine unless the user explicitly
chooses. Principle 3: be honest about what we protect and what we don't.

`openfoia.net` is the single place every network-touching module is meant to
build its httpx client from. These tests pin its fail-closed behaviour (Tor
required but unavailable must never silently fall back to clearnet), its
stream-isolation contract, and the honesty of `describe_egress`.
"""

from __future__ import annotations

import asyncio

import pytest

from openfoia.net import (
    DEFAULT_USER_AGENT,
    EgressMode,
    EgressPolicy,
    TorUnavailableError,
    check_tor,
    describe_egress,
    egress_client,
    tor_proxy_url,
)

# ---------------------------------------------------------------------------
# EgressPolicy / EgressMode basics
# ---------------------------------------------------------------------------


def test_default_policy_is_direct():
    policy = EgressPolicy()
    assert policy.mode is EgressMode.DIRECT
    assert policy.is_tor is False


def test_tor_policy_is_tor():
    policy = EgressPolicy(mode=EgressMode.TOR)
    assert policy.is_tor is True


def test_policy_is_frozen():
    policy = EgressPolicy()
    with pytest.raises(Exception):  # noqa: B017 - dataclass frozen -> FrozenInstanceError
        policy.mode = EgressMode.TOR


# ---------------------------------------------------------------------------
# Non-identifying User-Agent
# ---------------------------------------------------------------------------


def test_default_user_agent_does_not_identify_openfoia():
    ua = DEFAULT_USER_AGENT.lower()
    assert "openfoia" not in ua
    assert "github" not in ua


def test_default_user_agent_looks_like_a_browser():
    assert "Mozilla" in DEFAULT_USER_AGENT


# ---------------------------------------------------------------------------
# tor_proxy_url: scheme + stream isolation
# ---------------------------------------------------------------------------


def test_tor_proxy_url_uses_socks5h_scheme():
    policy = EgressPolicy(mode=EgressMode.TOR, isolate_streams=False)
    url = tor_proxy_url(policy)
    assert url.startswith("socks5h://")


def test_tor_proxy_url_no_creds_when_isolation_disabled():
    policy = EgressPolicy(mode=EgressMode.TOR, isolate_streams=False)
    url = tor_proxy_url(policy)
    assert url == "socks5h://127.0.0.1:9050"
    assert "@" not in url


def test_tor_proxy_url_host_and_port_from_policy():
    policy = EgressPolicy(
        mode=EgressMode.TOR, tor_host="10.0.0.5", tor_port=9150, isolate_streams=False
    )
    url = tor_proxy_url(policy)
    assert url == "socks5h://10.0.0.5:9150"


def test_tor_proxy_url_random_isolation_differs_between_calls():
    """No token -> a fresh random credential each call -> a fresh circuit."""
    policy = EgressPolicy(mode=EgressMode.TOR, isolate_streams=True)
    url_a = tor_proxy_url(policy)
    url_b = tor_proxy_url(policy)
    assert url_a != url_b
    assert "@" in url_a
    assert "@" in url_b


def test_tor_proxy_url_same_token_yields_same_creds():
    """Same isolation_token -> same creds -> deliberately shared circuit."""
    policy = EgressPolicy(mode=EgressMode.TOR, isolate_streams=True)
    url_a = tor_proxy_url(policy, isolation_token="job-42")
    url_b = tor_proxy_url(policy, isolation_token="job-42")
    assert url_a == url_b


def test_tor_proxy_url_different_tokens_yield_different_creds():
    policy = EgressPolicy(mode=EgressMode.TOR, isolate_streams=True)
    url_a = tor_proxy_url(policy, isolation_token="job-42")
    url_b = tor_proxy_url(policy, isolation_token="job-43")
    assert url_a != url_b


def test_tor_proxy_url_isolation_creds_are_embedded_as_userinfo():
    policy = EgressPolicy(mode=EgressMode.TOR, isolate_streams=True)
    url = tor_proxy_url(policy, isolation_token="fixed-token")
    # socks5h://user:pass@host:port
    assert url.startswith("socks5h://")
    userinfo, _, hostport = url.removeprefix("socks5h://").partition("@")
    assert hostport == "127.0.0.1:9050"
    assert ":" in userinfo  # user:pass


# ---------------------------------------------------------------------------
# egress_client: construction, UA handling, fail-closed
# ---------------------------------------------------------------------------


def test_egress_client_none_policy_is_direct():
    client = egress_client(None)
    try:
        assert client.headers["user-agent"] == DEFAULT_USER_AGENT
    finally:
        asyncio.run(client.aclose())


def test_egress_client_direct_sets_default_user_agent():
    policy = EgressPolicy(mode=EgressMode.DIRECT)
    client = egress_client(policy)
    try:
        assert client.headers["user-agent"] == DEFAULT_USER_AGENT
    finally:
        asyncio.run(client.aclose())


def test_egress_client_explicit_user_agent_overrides_default():
    """SEC EDGAR etc. are required to send a descriptive UA — respect it."""
    policy = EgressPolicy(mode=EgressMode.DIRECT)
    custom_ua = "openfoia-research contact@example.org"
    client = egress_client(policy, headers={"User-Agent": custom_ua})
    try:
        assert client.headers["user-agent"] == custom_ua
    finally:
        asyncio.run(client.aclose())


def test_egress_client_direct_has_no_socks_transport():
    policy = EgressPolicy(mode=EgressMode.DIRECT)
    client = egress_client(policy)
    try:
        assert "SOCKS" not in type(client._transport).__name__.upper()
    finally:
        asyncio.run(client.aclose())


def test_egress_client_tor_mode_builds_socks_client(monkeypatch):
    """With socksio importable, TOR mode should succeed and use a socks5h proxy."""
    policy = EgressPolicy(mode=EgressMode.TOR, isolate_streams=False)
    pytest.importorskip("socksio")

    client = egress_client(policy)
    try:
        assert client.headers["user-agent"] == DEFAULT_USER_AGENT
    finally:
        asyncio.run(client.aclose())


def test_egress_client_tor_mode_fails_closed_when_socksio_missing(monkeypatch):
    """The whole point: never silently return a clearnet client in TOR mode."""
    import httpx

    def _boom(*args, **kwargs):
        if kwargs.get("proxy") or (args and "socks5" in str(args)):
            raise ImportError("socksio not installed")
        raise ImportError("socksio not installed")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)

    policy = EgressPolicy(mode=EgressMode.TOR)
    with pytest.raises(TorUnavailableError, match="install-extras tor"):
        egress_client(policy)


def test_egress_client_tor_mode_never_falls_back_to_direct_client(monkeypatch):
    """Belt-and-suspenders: assert AsyncClient is never called without a proxy
    kwarg when TOR mode's primary construction fails."""
    import httpx

    calls = []
    real_async_client = httpx.AsyncClient

    def _tracking(*args, **kwargs):
        calls.append(kwargs.get("proxy"))
        raise ImportError("socksio not installed")

    monkeypatch.setattr(httpx, "AsyncClient", _tracking)

    policy = EgressPolicy(mode=EgressMode.TOR)
    with pytest.raises(TorUnavailableError):
        egress_client(policy)

    # Every attempted construction must have been with a proxy set (never None).
    assert calls, "egress_client never attempted to construct a client"
    assert all(c is not None for c in calls)

    monkeypatch.setattr(httpx, "AsyncClient", real_async_client)


def test_egress_client_passes_through_timeout_and_kwargs():
    policy = EgressPolicy(mode=EgressMode.DIRECT)
    client = egress_client(policy, timeout=42.0, follow_redirects=True)
    try:
        assert client.timeout.connect == 42.0
        assert client.follow_redirects is True
    finally:
        asyncio.run(client.aclose())


# ---------------------------------------------------------------------------
# check_tor: best-effort port probe, never raises, never opens real sockets
# in this offline suite
# ---------------------------------------------------------------------------


def _patch_inet_stream_socket(monkeypatch, factory):
    """Replace socket.socket only for AF_INET/SOCK_STREAM construction.

    asyncio.run() itself builds an event loop that opens an AF_UNIX
    self-pipe via socket.socket() under the hood; a blanket monkeypatch of
    socket.socket breaks THAT, not just the code under test. So delegate
    anything that isn't the (AF_INET, SOCK_STREAM) call check_tor makes back
    to the real socket.socket.
    """
    import socket as socket_mod

    real_socket = socket_mod.socket

    def _dispatch(family=-1, type=-1, *a, **k):
        if family == socket_mod.AF_INET and type == socket_mod.SOCK_STREAM:
            return factory()
        return real_socket(family, type, *a, **k)

    monkeypatch.setattr(socket_mod, "socket", _dispatch)


def test_check_tor_true_when_port_accepts(monkeypatch):
    class _FakeSocket:
        def settimeout(self, t):
            pass

        def connect(self, addr):
            return None

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    _patch_inet_stream_socket(monkeypatch, _FakeSocket)

    policy = EgressPolicy(mode=EgressMode.TOR)
    result = asyncio.run(check_tor(policy))
    assert result is True


def test_check_tor_false_when_port_refuses(monkeypatch):
    class _FakeSocket:
        def settimeout(self, t):
            pass

        def connect(self, addr):
            raise ConnectionRefusedError("nope")

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    _patch_inet_stream_socket(monkeypatch, _FakeSocket)

    policy = EgressPolicy(mode=EgressMode.TOR)
    result = asyncio.run(check_tor(policy))
    assert result is False


def test_check_tor_false_on_any_exception_never_raises(monkeypatch):
    def _boom():
        raise OSError("network unreachable")

    _patch_inet_stream_socket(monkeypatch, _boom)

    policy = EgressPolicy(mode=EgressMode.TOR)
    result = asyncio.run(check_tor(policy))
    assert result is False


# ---------------------------------------------------------------------------
# describe_egress: the honesty check
# ---------------------------------------------------------------------------


def test_describe_egress_direct_sees_real_ip():
    policy = EgressPolicy(mode=EgressMode.DIRECT)
    report = describe_egress(policy)

    assert report["mode"] == "direct"
    assert report["sees_real_ip"] is True


def test_describe_egress_tor_hides_real_ip_but_not_content():
    policy = EgressPolicy(mode=EgressMode.TOR, isolate_streams=True)
    report = describe_egress(policy)

    assert report["mode"] == "tor"
    assert report["sees_real_ip"] is False
    # Tor hides WHO is asking, not WHAT is being asked.
    assert report["endpoint_sees_destination"] is True
    assert report["endpoint_sees_query"] is True
    assert report["stream_isolation"] is True


def test_describe_egress_tor_names_what_is_not_protected():
    policy = EgressPolicy(mode=EgressMode.TOR)
    report = describe_egress(policy)

    not_protected = " ".join(report["not_protected"]).lower()
    assert "traffic" in not_protected or "timing" in not_protected
    assert "query" in not_protected or "content" in not_protected or "subject" in not_protected


def test_describe_egress_never_claims_anonymity_word_without_qualification():
    """Refuse to overpromise: the report must not just say 'anonymous'."""
    policy = EgressPolicy(mode=EgressMode.TOR)
    report = describe_egress(policy)

    assert report["not_protected"], "describe_egress must be honest about limits, not just claims"


def test_describe_egress_stream_isolation_reflects_policy():
    policy = EgressPolicy(mode=EgressMode.TOR, isolate_streams=False)
    report = describe_egress(policy)
    assert report["stream_isolation"] is False
