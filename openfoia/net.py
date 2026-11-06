"""Outbound-HTTP egress choke point.

Every network-touching module in OpenFOIA should build its httpx client
through `egress_client()` rather than constructing `httpx.AsyncClient`
directly. That's what makes DIRECT-vs-TOR routing, Tor stream isolation, and
a non-identifying User-Agent a property of the whole app instead of
something every caller has to remember to get right.

Principle 1 (data never leaves the machine unless the user explicitly
chooses) shows up here as fail-closed Tor mode: if the SOCKS stack isn't
available, `egress_client()` raises `TorUnavailableError` rather than
silently handing back a clearnet client — the classic deanonymization leak
is exactly that kind of silent fallback.

Principle 3 (be honest about what we protect and what we don't) shows up in
`describe_egress()`: Tor hides who is asking, not what is being asked, and
this module says so rather than claiming "anonymous."
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import secrets
import socket
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
from httpx._utils import get_environment_proxies

# A common, non-identifying browser UA. This must NEVER mention "openfoia"
# or a project URL — that would tell every server we talk to (and anyone
# logging their traffic) that OpenFOIA made the request.
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0"


MAX_ATTEMPTS = 3
RETRY_STATUSES = frozenset({429, 503})
RETRY_METHODS = frozenset({"GET", "HEAD"})
MAX_RETRY_AFTER = 30.0  # longer server-requested waits go back to the adapter, not slept
MIN_HOST_INTERVAL = 0.1  # 10 req/sec, DocumentCloud's documented limit

logger = logging.getLogger(__name__)
_sleep = asyncio.sleep
# ponytail: per-host slots but one shared interval; per-API limits when an API needs slower
_next_slot: dict[str, float] = {}


async def _throttle(host: str) -> None:
    # The slot is reserved before any await, so concurrent tasks cannot race without a lock.
    now = time.monotonic()
    slot = max(now, _next_slot.get(host, 0.0))
    _next_slot[host] = slot + MIN_HOST_INTERVAL
    if slot > now:
        await _sleep(slot - now)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int) -> float:
    return 0.5 * 2 ** (attempt - 1) + random.uniform(0, 0.5)


class RetryTransport(httpx.AsyncBaseTransport):
    """Per-host spacing for every request; retries for idempotent ones.

    GET/HEAD are retried on 429/503 and on ConnectError/ReadTimeout, up to
    MAX_ATTEMPTS, honoring Retry-After. Other methods are sent once.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, max_attempts: int = MAX_ATTEMPTS):
        self._inner = inner
        self._max_attempts = max_attempts

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        attempts = self._max_attempts if request.method in RETRY_METHODS else 1
        for attempt in range(1, attempts + 1):
            await _throttle(request.url.host)
            try:
                response = await self._inner.handle_async_request(request)
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                if attempt == attempts:
                    raise
                reason = type(exc).__name__
                delay = _backoff(attempt)
            else:
                if response.status_code not in RETRY_STATUSES or attempt == attempts:
                    return response
                reason = f"HTTP {response.status_code}"
                delay = _retry_after(response)
                if delay is None:
                    delay = _backoff(attempt)
                elif delay > MAX_RETRY_AFTER:
                    return response
                await response.aclose()
            logger.info(
                "Retrying %s after %s (attempt %d of %d) in %.1fs",
                request.url.host,
                reason,
                attempt,
                attempts,
                delay,
            )
            await _sleep(delay)
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def aclose(self) -> None:
        await self._inner.aclose()


class EgressMode(str, enum.Enum):
    DIRECT = "direct"  # clearnet, no proxy (backward-compatible default)
    TOR = "tor"  # route via Tor SOCKS5; FAIL-CLOSED if unavailable


class TorUnavailableError(RuntimeError):
    """Tor egress was required but the SOCKS proxy/socksio is unavailable.

    Raised instead of silently falling back to clearnet (that fallback is
    the classic deanonymization leak).
    """


@dataclass(frozen=True)
class EgressPolicy:
    mode: EgressMode = EgressMode.DIRECT
    tor_host: str = "127.0.0.1"
    tor_port: int = 9050
    isolate_streams: bool = True  # unique SOCKS creds -> separate Tor circuit
    user_agent: str = DEFAULT_USER_AGENT

    @property
    def is_tor(self) -> bool:
        return self.mode is EgressMode.TOR


def tor_proxy_url(policy: EgressPolicy, *, isolation_token: str | None = None) -> str:
    """Return 'socks5h://[user:pass@]host:port' for *policy*.

    The scheme is socks5h to make remote-DNS intent explicit. Note this is
    not a behavioral fix over socks5 in httpx: httpcore never resolves
    hostnames locally for a SOCKS proxy either way (socksio always sends a
    DOMAIN_NAME request), so socks5 already resolves remotely here too.

    When policy.isolate_streams is True, a SOCKS username/password derived
    from *isolation_token* (or a fresh random token when None) is embedded
    in the URL, so Tor opens a distinct circuit per credential pair — this
    is Tor's stream isolation (IsolateSOCKSAuth). Passing the same token
    across calls deliberately reuses one circuit; a different token (or
    None) gets a different one.
    """
    if not policy.isolate_streams:
        return f"socks5h://{policy.tor_host}:{policy.tor_port}"

    token = isolation_token if isolation_token is not None else secrets.token_hex(8)
    creds = quote(token, safe="")
    return f"socks5h://{creds}:{creds}@{policy.tor_host}:{policy.tor_port}"


def egress_client(
    policy: EgressPolicy | None = None,
    *,
    timeout: float = 15.0,
    isolation_token: str | None = None,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Build a configured httpx.AsyncClient for outbound requests.

    - policy=None behaves like EgressPolicy() (DIRECT).
    - DIRECT: plain client, no proxy.
    - TOR: proxied through tor_proxy_url(...). If the SOCKS stack (socksio)
      is missing, client construction raises ImportError, which is
      translated here into TorUnavailableError — TOR mode never falls back
      to a proxy-less client.
    - The User-Agent is set to policy.user_agent unless the caller passes an
      explicit "User-Agent" in *headers* (some callers, e.g. SEC EDGAR, are
      required to send a descriptive UA identifying themselves).
    - Every request goes through RetryTransport (per-host spacing, retries
      for idempotent requests). A caller-supplied *transport* is wrapped too.
    """
    policy = policy or EgressPolicy()

    merged_headers = {"User-Agent": policy.user_agent}
    if headers:
        merged_headers.update(headers)

    if not policy.is_tor:
        caller_transport = kwargs.pop("transport", None)
        mounts: dict[str, httpx.AsyncBaseTransport] = {}
        # Passing transport= turns off httpx's env-proxy handling, so the
        # HTTP(S)_PROXY / ALL_PROXY / NO_PROXY mounts are rebuilt here, wrapped.
        if caller_transport is None and kwargs.get("trust_env", True):
            for pattern, url in get_environment_proxies().items():
                inner = httpx.AsyncHTTPTransport(proxy=url) if url else httpx.AsyncHTTPTransport()
                mounts[pattern] = RetryTransport(inner)
        default = RetryTransport(caller_transport or httpx.AsyncHTTPTransport())
        return httpx.AsyncClient(
            transport=default, mounts=mounts, timeout=timeout, headers=merged_headers, **kwargs
        )

    proxy_url = tor_proxy_url(policy, isolation_token=isolation_token)
    try:
        # proxy= keeps env proxies from mounting; the "all://" mount then
        # replaces the transport httpx built from it with the same proxy, wrapped.
        proxied = RetryTransport(httpx.AsyncHTTPTransport(proxy=proxy_url))
        return httpx.AsyncClient(
            proxy=proxy_url,
            mounts={"all://": proxied},
            timeout=timeout,
            headers=merged_headers,
            **kwargs,
        )
    except ImportError as exc:
        raise TorUnavailableError(
            "Tor egress requires the 'socksio' package, which is not installed. "
            "Run: openfoia install-extras tor"
        ) from exc


async def check_tor(policy: EgressPolicy, *, timeout: float = 5.0) -> bool:
    """Best-effort: is the Tor SOCKS port accepting connections?

    Never raises — used to fail closed early and to report status honestly,
    not to prove Tor is actually working end to end.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((policy.tor_host, policy.tor_port))
        return True
    except OSError:
        return False


def describe_egress(policy: EgressPolicy) -> dict[str, Any]:
    """Honest, machine-readable summary of what this policy protects.

    Tor hides WHO is asking (the source IP), never WHAT is being asked (the
    destination host and the query contents reach the endpoint either way).
    This never claims blanket "anonymity" — it names the specific things
    that remain exposed.
    """
    if policy.is_tor:
        return {
            "mode": "tor",
            "sees_real_ip": False,
            "endpoint_sees_destination": True,
            "endpoint_sees_query": True,
            "stream_isolation": policy.isolate_streams,
            "not_protected": [
                "Global passive traffic-analysis / timing correlation "
                "between your connection into Tor and the exit traffic can "
                "still link a request back to you.",
                "The query content itself (e.g. subject names you search "
                "for) is still sent in the clear to the destination "
                "endpoint — Tor hides who is asking, not what is asked.",
            ],
        }
    return {
        "mode": "direct",
        "sees_real_ip": True,
        "endpoint_sees_destination": True,
        "endpoint_sees_query": True,
        "stream_isolation": False,
        "not_protected": [
            "The destination server sees your real IP address directly.",
            "The query content itself (e.g. subject names you search for) "
            "is sent in the clear to the destination endpoint.",
        ],
    }
