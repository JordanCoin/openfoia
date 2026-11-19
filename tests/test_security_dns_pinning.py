"""DNS rebinding on downloads: the connection goes to the address that was vetted.

check_download_target resolves a download's hostname and requires public
addresses, but that is a check before the request. A rebinding DNS server can
answer a public address to the check and 127.0.0.1 to the connection. In DIRECT
mode download_to_file therefore resolves inside the connect path and opens the
socket to an address from that same, vetted answer. No real network: the
resolver is faked and the network backend under the vetting one is httpcore's
mock backend, which records where it was asked to connect.
"""

from __future__ import annotations

import asyncio
import socket
import ssl

import httpcore
import pytest

from openfoia import net
from openfoia.net import EgressMode, EgressPolicy, UnsafeAddressError, egress_client
from openfoia.records import base

DIRECT = EgressPolicy(mode=EgressMode.DIRECT, max_attempts=1)
PUBLIC = "93.184.215.14"
URL = "https://files.example.org/a.pdf"
BODY = b"%PDF-1.4 fake"


def _answer(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


class _RecordingBackend(httpcore.AsyncMockBackend):
    """httpcore's mock backend, recording connect targets and TLS server names."""

    def __init__(self):
        super().__init__(
            [b"HTTP/1.1 200 OK\r\n", f"Content-Length: {len(BODY)}\r\n\r\n".encode(), BODY]
        )
        self.connects: list[tuple[str, int]] = []
        self.server_names: list[str | None] = []
        self.ssl_contexts: list[ssl.SSLContext] = []
        backend = self

        class _Stream(httpcore.AsyncMockStream):
            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                backend.server_names.append(server_hostname)
                backend.ssl_contexts.append(ssl_context)
                return _Stream(list(self._buffer), self._http2)

        self._stream_cls = _Stream

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects.append((host, port))
        return self._stream_cls(list(self._buffer), self._http2)


@pytest.fixture
def backend(monkeypatch):
    rec = _RecordingBackend()
    monkeypatch.setattr(net, "_inner_backend_factory", lambda: rec)
    return rec


def _download(tmp_path, egress=DIRECT):
    async def go():
        async with egress_client(egress, follow_redirects=False, trust_env=False) as client:
            return await base.download_to_file(
                client, URL, tmp_path / "a.pdf", max_bytes=1 << 20, egress=egress
            )

    return asyncio.run(go())


def test_rebinding_resolver_cannot_redirect_the_download(monkeypatch, tmp_path, backend):
    """Public to the check, 127.0.0.1 to the connection: refused, nothing connects."""
    answers = iter([PUBLIC, "127.0.0.1", "127.0.0.1"])
    calls = []

    def rebinding(host, *a, **k):
        calls.append(host)
        return _answer(next(answers))

    monkeypatch.setattr(base, "_getaddrinfo", rebinding)
    with pytest.raises(UnsafeAddressError, match="non-public"):
        _download(tmp_path)
    assert calls == ["files.example.org", "files.example.org"]  # check, then connect path
    assert backend.connects == []
    assert not (tmp_path / "a.pdf").exists()


@pytest.mark.parametrize("https_proxy", [None, "http://127.0.0.1:9"])
def test_no_proxy_hosts_are_pinned_too(monkeypatch, tmp_path, backend, https_proxy):
    """A NO_PROXY match connects directly, so it gets the same vetted connect path."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "files.example.org")
    if https_proxy:
        monkeypatch.setenv("HTTPS_PROXY", https_proxy)
    answers = iter([PUBLIC, "127.0.0.1"])
    monkeypatch.setattr(base, "_getaddrinfo", lambda h, *a, **k: _answer(next(answers)))

    async def go():
        async with egress_client(DIRECT, follow_redirects=False) as client:  # trust_env on
            return await base.download_to_file(
                client, URL, tmp_path / "a.pdf", max_bytes=1 << 20, egress=DIRECT
            )

    with pytest.raises(UnsafeAddressError):
        asyncio.run(go())
    assert backend.connects == []


@pytest.mark.parametrize("internal", ["10.0.0.5", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_connect_path_refuses_internal_answers(monkeypatch, tmp_path, backend, internal):
    answers = iter([PUBLIC, internal])
    monkeypatch.setattr(base, "_getaddrinfo", lambda h, *a, **k: _answer(next(answers)))
    with pytest.raises(UnsafeAddressError):
        _download(tmp_path)
    assert backend.connects == []


def test_connection_goes_to_the_vetted_address_with_tls_for_the_hostname(
    monkeypatch, tmp_path, backend
):
    """The socket is opened to the vetted IP; SNI and verification use the hostname."""
    seen = []

    def resolver(host, *a, **k):
        seen.append(host)
        return _answer(PUBLIC)

    monkeypatch.setattr(base, "_getaddrinfo", resolver)
    assert _download(tmp_path) == len(BODY)
    assert (tmp_path / "a.pdf").read_bytes() == BODY
    assert backend.connects == [(PUBLIC, 443)]
    assert backend.server_names == ["files.example.org"]
    (ctx,) = backend.ssl_contexts
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_connect_path_uses_the_answer_it_checked_not_a_later_one(monkeypatch, tmp_path, backend):
    """Connect-time answer differs from the check's (both public): the vetted one is used."""
    answers = iter([PUBLIC, "93.184.215.99"])
    monkeypatch.setattr(base, "_getaddrinfo", lambda h, *a, **k: _answer(next(answers)))
    _download(tmp_path)
    assert backend.connects == [("93.184.215.99", 443)]


def test_plain_requests_are_not_vetted(monkeypatch, backend):
    """Outside a download (e.g. an API call), the transport does not resolve or vet."""

    def must_not_resolve(*a, **k):
        raise AssertionError("resolved outside a public-only block")

    monkeypatch.setattr(base, "_getaddrinfo", must_not_resolve)
    transport = net._DirectTransport()
    assert net._public_only.get() is None
    with base.public_only_connections(DIRECT):
        assert net._public_only.get() is base.public_addresses
    assert net._public_only.get() is None
    asyncio.run(transport.aclose())


def test_tor_mode_does_not_pin_or_resolve(monkeypatch):
    """Tor: the exit resolves the name; nothing is looked up locally."""
    tor = EgressPolicy(mode=EgressMode.TOR)
    with base.public_only_connections(tor):
        assert net._public_only.get() is None
    with base.public_only_connections(None):
        assert net._public_only.get() is None


def test_vetted_transport_uses_the_vetting_backend():
    """Guards the httpx internals the pin relies on (the pool's network backend)."""
    transport = net._vetted_transport(base.public_addresses)
    assert isinstance(transport._pool._network_backend, net._VettingBackend)
    ctx = transport._pool._ssl_context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    asyncio.run(transport.aclose())


@pytest.mark.parametrize(
    ("answers", "ok"),
    [
        ([PUBLIC], True),
        ([PUBLIC, "2606:2800:21f:cb07:6820:80da:af6b:8b2c"], True),
        ([PUBLIC, "192.168.1.10"], False),
        (["64:ff9b::7f00:1"], False),
        ([], False),
    ],
)
def test_public_addresses_rule(monkeypatch, answers, ok):
    monkeypatch.setattr(
        base, "_getaddrinfo", lambda h, *a, **k: [x for ip in answers for x in _answer(ip)]
    )
    if ok:
        assert base.public_addresses("files.example.org") == answers
    else:
        with pytest.raises(ValueError):
            base.public_addresses("files.example.org")


def test_documentcloud_text_fetch_is_pinned_api_call_is_not(monkeypatch):
    """The asset-host text fetch (URL from the API response) runs under the pin."""
    import httpx

    from openfoia.records import documentcloud as dc

    pinned: dict[str, bool] = {}

    def handler(request):
        pinned[request.url.host + request.url.path] = net._public_only.get() is not None
        if request.url.host == "api.www.documentcloud.org":
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "slug": "memo",
                    "title": "Memo",
                    "page_count": 1,
                    "asset_url": "https://s3.documentcloud.org/",
                },
            )
        return httpx.Response(200, text="page text")

    def fake_client(policy=None, **kw):
        return net.egress_client(policy, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(dc, "egress_client", fake_client)
    asyncio.run(dc.DocumentCloudAdapter(egress=DIRECT).fetch("7"))
    assert pinned["api.www.documentcloud.org/api/documents/7/"] is False
    assert pinned["s3.documentcloud.org/documents/7/memo.txt"] is True
    assert pinned["s3.documentcloud.org/documents/7/pages/memo-p1.txt"] is True
