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

import enum
import secrets
import socket
from dataclasses import dataclass
from urllib.parse import quote

# A common, non-identifying browser UA. This must NEVER mention "openfoia"
# or a project URL — that would tell every server we talk to (and anyone
# logging their traffic) that OpenFOIA made the request.
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0"


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
    **kwargs,
) -> "httpx.AsyncClient":  # noqa: F821 - httpx is lazy-imported below
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
    """
    import httpx

    policy = policy or EgressPolicy()

    merged_headers = {"User-Agent": policy.user_agent}
    if headers:
        merged_headers.update(headers)

    if not policy.is_tor:
        return httpx.AsyncClient(timeout=timeout, headers=merged_headers, **kwargs)

    proxy_url = tor_proxy_url(policy, isolation_token=isolation_token)
    try:
        return httpx.AsyncClient(proxy=proxy_url, timeout=timeout, headers=merged_headers, **kwargs)
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


def describe_egress(policy: EgressPolicy) -> dict:
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
