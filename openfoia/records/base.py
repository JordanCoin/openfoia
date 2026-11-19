"""Base class for public records adapters."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import posixpath
import re
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..net import EgressPolicy, egress_client, public_addresses_only

#: Names that only ever mean this machine or the local network.
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".localdomain")
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
#: NAT64 (RFC 6052): 64:ff9b::a.b.c.d reaches the embedded IPv4 address.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _inet_aton(host: str) -> ipaddress.IPv4Address | None:
    """Parse *host* the way inet_aton (and so glibc's getaddrinfo) does, or None.

    Accepts 1-4 dot-separated parts, each decimal, 0x-hex or 0-prefixed octal:
    ``2130706433``, ``127.1``, ``0x7f000001`` and ``0177.0.0.1`` are all
    127.0.0.1. The last part fills the remaining bytes.
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values = []
    for part in parts:
        if not part:
            return None
        try:
            if part[:2] == "0x":
                value = int(part[2:] or "0", 16)
            elif len(part) > 1 and part[0] == "0":
                value = int(part[1:], 8)
            else:
                value = int(part, 10)
        except ValueError:
            return None
        values.append(value)
    *head, last = values
    if any(v > 0xFF for v in head) or last >= 1 << (8 * (4 - len(head))):
        return None
    number = 0
    for v in head:
        number = number << 8 | v
    number = number << (8 * (4 - len(head))) | last
    return ipaddress.IPv4Address(number)


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable unicast, looking through IPv4-mapped and NAT64 addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def normalize_host(host: str) -> str:
    """Lowercase, no trailing dot, IDNA (ASCII) form; ValueError if it has none."""
    host = host.strip().rstrip(".").lower()
    if not host:
        raise ValueError("empty host")
    if ":" in host:  # an IPv6 literal (urlparse already removed the brackets)
        return host
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"host {host!r} is not a valid domain name") from exc


def validate_download_url(url: str) -> str:
    """Return *url* if it is safe to fetch, else raise ValueError.

    File URLs in third-party API responses (MuckRock ``ffile``,
    DocumentCloud ``asset_url``) are attacker-influenced: a compromised or
    MITM'd upstream can point them at ``file:///etc/passwd`` or cloud
    metadata endpoints, and the body is written straight to disk.

    This is a check of the string only, with no DNS lookup. The host is
    normalised first (case, trailing dot, IDNA), numeric hosts are read the
    way inet_aton reads them (``https://2130706433/`` and ``https://127.1/``
    are 127.0.0.1), and an IP literal must be globally routable. A hostname
    that resolves to an internal address is caught at download time instead:
    see :func:`check_download_target`.
    """
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise ValueError(f"Refusing to download over {parsed.scheme or 'missing'} scheme: {url!r}")

    try:
        raw_host = parsed.hostname
    except ValueError:
        raw_host = None
    if not raw_host:
        raise ValueError(f"Refusing to download from a URL with no host: {url!r}")
    try:
        host = normalize_host(raw_host)
    except ValueError as exc:
        raise ValueError(f"Refusing to download from {url!r}: {exc}") from None

    if host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
        raise ValueError(f"Refusing to download from a local-network host: {url!r}")

    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        ip = _inet_aton(host)
        if ip is None and _NUMERIC_LABEL.match(host.rsplit(".", 1)[-1]):
            # A numeric last label that is not a valid address: resolvers
            # disagree on what it means, so do not guess.
            raise ValueError(
                f"Refusing to download from an ambiguous numeric host: {url!r}"
            ) from None
    if ip is not None and not _is_public_ip(ip):
        raise ValueError(f"Refusing to download from a non-public address: {url!r}")

    return url


#: getaddrinfo, behind a name so tests can fake DNS without touching the network.
_getaddrinfo = socket.getaddrinfo


def public_addresses(host: str) -> list[str]:
    """Resolve *host* and return its addresses, or raise ValueError unless all are public.

    Refused when the lookup fails or returns nothing, or when any address is
    internal (loopback, private, link-local, carrier-grade NAT, reserved,
    multicast, or an IPv4-mapped/NAT64 form of one). An IP literal is checked
    without a lookup. This is the resolver the download connect path uses
    (see :func:`openfoia.net.public_addresses_only`), so the addresses it
    returns are the ones the socket is opened to.
    """
    name = normalize_host(host.strip("[]"))
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise ValueError(f"{host} is not a public address")
        return [str(literal)]
    try:
        infos = _getaddrinfo(name, None)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{host} does not resolve ({exc})") from None
    if not infos:
        raise ValueError(f"{host} does not resolve")
    addresses: list[str] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise ValueError(f"{host} resolved to an unreadable address {raw!r}") from None
        if not _is_public_ip(ip):
            raise ValueError(f"{host} resolves to a non-public address ({ip})")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    return addresses


def resolves_to_public_address(host: str) -> bool:
    """DNS check that every address *host* resolves to is globally routable.

    False when the lookup fails or any address is internal (loopback, private,
    link-local, carrier-grade NAT, reserved, multicast). This is a network
    call (to the system resolver), so it is made at download time, never
    when only checking a URL string, and never in Tor mode.
    """
    try:
        public_addresses(host)
    except ValueError:
        return False
    return True


def public_only_connections(egress: EgressPolicy | None) -> contextlib.AbstractContextManager:
    """Pin DIRECT connections made in this block to vetted public addresses.

    In DIRECT mode the hostname is resolved once inside the connect path, every
    address must be public, and the socket goes to an address from that same
    answer (TLS still verifies the certificate against the hostname), so DNS
    rebinding between a check and the connection does not work. In Tor mode,
    or with *egress* None, this does nothing: over Tor the exit relay resolves
    the name. Covers clients built by egress_client() with its own transport.
    """
    if egress is None or egress.is_tor:
        return contextlib.nullcontext()
    return public_addresses_only(public_addresses)


async def check_download_target(url: str, egress: EgressPolicy | None) -> None:
    """Raise ValueError unless *url* may be downloaded on *egress*.

    Always runs :func:`validate_download_url`. In DIRECT mode it also
    resolves the hostname and requires every address to be public, which
    stops a public-looking name that points at 127.0.0.1 or the LAN.

    In Tor mode nothing is resolved here: a local lookup would itself leak the
    hostname outside Tor, and the name is resolved at the exit relay, whose
    exit policy refuses private and local addresses. Protection against a
    hostname that resolves to an internal address therefore relies on the Tor
    exit policy in that mode, and DNS rebinding at the exit is not something
    OpenFOIA can stop.

    This is a check before the request, not a pin: on its own it does not stop
    a resolver that answers differently a second later (DNS rebinding). In
    DIRECT mode the pin is :func:`public_only_connections`, which
    :func:`download_to_file` applies to the connection itself.
    """
    validate_download_url(url)
    if egress is None or egress.is_tor:
        return
    host = urlparse(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return  # an IP literal, already checked above
    except ValueError:
        pass
    if not await asyncio.to_thread(resolves_to_public_address, host):
        raise ValueError(
            f"Refusing to download {url!r}: {host} does not resolve, or resolves to a "
            "non-public address."
        )


async def download_to_file(
    client: Any, url: str, dest: Any, max_bytes: int, *, egress: EgressPolicy | None = None
) -> int:
    """Stream *url* into *dest*, aborting as soon as *max_bytes* is exceeded.

    Buffering via ``response.content`` first would let a compromised or
    MITM'd upstream force a huge allocation before any size check ran. This
    enforces the cap incrementally and removes the partial file on abort.

    *url* is checked with :func:`check_download_target` first: pass the
    request's *egress* policy so that in DIRECT mode the hostname must
    resolve to public addresses (None checks the URL string only). In DIRECT
    mode the connection is then made under :func:`public_only_connections`:
    the name is resolved again inside the connect path and the socket goes to
    an address from that vetted answer, so a DNS server that changes its answer
    after the check (rebinding to 127.0.0.1 or the LAN) is refused. That pin
    needs a client from egress_client() with its own transport; with an
    HTTP(S) proxy from the environment, the proxy resolves the name. Redirects
    are refused: each one would be a second, unchecked URL.

    Returns the number of bytes written.
    """
    await check_download_target(url, egress)
    if getattr(client, "follow_redirects", False):
        raise ValueError("Refusing to download with a client that follows redirects")

    dest = Path(dest)
    # Stream into a sibling .part file and rename when complete, so *dest* only
    # ever exists whole: even a killed process can't leave a truncated file
    # that a resume would mistake for a finished download.
    part = dest.with_name(dest.name + ".part")
    written = 0

    try:
        with public_only_connections(egress):
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with open(part, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError(
                                f"Refusing download over {max_bytes} bytes (cap exceeded): {url!r}"
                            )
                        f.write(chunk)
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    return written


def safe_download_filename(url: str, default: str = "download") -> str:
    """Derive a safe basename from *url*.

    ``url.split("/")[-1]`` accepted percent-encoded traversal, empty names and
    dotfiles straight into the output directory.
    """
    path = unquote(urlparse(url).path or "")
    name = posixpath.basename(posixpath.normpath(path))

    # normpath can still yield traversal markers for pathological input.
    name = name.replace("\\", "/").split("/")[-1]
    name = name.strip().lstrip(".")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)

    if not name or name in (".", ".."):
        return default
    return name[:255]


@dataclass
class RecordEntity:
    """A normalized entity returned by a records adapter.

    Compatible with the OpenFOIA Entity model for downstream ingestion.
    """

    entity_type: str  # "PERSON", "ORGANIZATION", etc.
    name: str
    source: str  # adapter source name (e.g. "opencorporates", "sec")
    source_url: str | None = None
    jurisdiction: str | None = None
    status: str | None = None
    identifiers: dict[str, str] = field(default_factory=dict)
    extra_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to plain dict for serialization."""
        return {
            "entity_type": self.entity_type,
            "name": self.name,
            "source": self.source,
            "source_url": self.source_url,
            "jurisdiction": self.jurisdiction,
            "status": self.status,
            "identifiers": self.identifiers,
            "extra_data": self.extra_data,
        }


@dataclass
class SearchResult:
    """Container for search results from a records adapter."""

    source: str
    query: str
    total_results: int
    entities: list[RecordEntity]
    page: int = 1
    per_page: int = 25
    raw_response: dict[str, Any] | None = None
    error: str | None = None  # non-None means API failed (vs genuine zero results)


class AdapterRequestError(RuntimeError):
    """A records API call failed at the transport or HTTP-status level.

    Distinct from "the source returned zero results" — conflating the two
    makes a failed sanctions or registry check look like a clean record.
    """


class RecordAdapter(ABC):
    """Base class for public records adapters.

    Each adapter wraps an external API and returns normalized
    RecordEntity objects that can feed into the OpenFOIA entity model.
    """

    source_name: str = ""
    credentials_withheld: bool = False

    def __init__(self, *, egress: EgressPolicy | None = None) -> None:
        self._egress = egress if egress is not None else EgressPolicy()

    def _personal_credential(self, value: str | None) -> str | None:
        """*value*, or None over Tor: a personal key or token names the user whatever the route."""
        if value and self._egress.is_tor:
            self.credentials_withheld = True
            return None
        return value

    def unavailable_reason(self) -> str | None:
        """Why search() cannot run as configured, or None when it can.

        Checked before any request (by ``openfoia search``), so a source that
        needs a key it does not have, or whose personal key was withheld over
        Tor, is reported as skipped instead of being sent the query only to
        refuse it. Override in adapters whose API refuses anonymous calls.
        """
        return None

    async def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        isolation_token: str | None = None,
    ) -> Any:
        """GET *url* and return decoded JSON, raising AdapterRequestError.

        Centralized so every adapter reports transport and HTTP failures the
        same way instead of letting them surface as empty result sets.
        """
        try:
            async with egress_client(
                self._egress, timeout=timeout, isolation_token=isolation_token
            ) as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            raise AdapterRequestError(f"{type(exc).__name__}: {exc}") from exc

    def _failed(self, query: str, error: str, **kwargs: Any) -> SearchResult:
        """Build a SearchResult that records a failure, not an empty result."""
        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=0,
            entities=[],
            error=error,
            **kwargs,
        )

    @abstractmethod
    async def search(self, query: str, **kwargs: Any) -> SearchResult:
        """Search for records matching the query.

        Args:
            query: Search term (company name, person name, etc.)
            **kwargs: Adapter-specific options (jurisdiction, page, etc.)

        Returns:
            SearchResult containing normalized entities.
        """
        ...

    @abstractmethod
    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Fetch a specific record by its identifier.

        Args:
            identifier: Unique identifier for the record (company number, CIK, etc.)
            **kwargs: Adapter-specific options.

        Returns:
            RecordEntity if found, None otherwise.
        """
        ...


DEMO_KEY = "DEMO_KEY"


class DataGovAdapter(RecordAdapter):
    """Base for api.data.gov sources (fec, govinfo, regulations), which share one key.

    Without an explicit *api_key* these use the shared, rate-limited DEMO_KEY. Get a
    free key at https://api.data.gov/signup/ and set OPENFOIA_API_DATA_GOV_KEY, or
    ``"records": {"_api_data_gov_key": "..."}`` in config.json. The key is tied to the
    email you signed up with, so it identifies you to api.data.gov even over Tor, and
    over Tor DEMO_KEY is used instead.
    """

    def __init__(self, api_key: str | None = None, *, egress: EgressPolicy | None = None):
        super().__init__(egress=egress)
        personal = api_key if api_key != DEMO_KEY else None
        self._api_key = self._personal_credential(personal) or DEMO_KEY

    def _redact(self, err: object) -> str:
        """Error text with the key removed, wherever httpx or a URL may have put it."""
        text = re.sub(r"api_key=[^&\s'\"]+", "api_key=<redacted>", str(err))
        if self._api_key != DEMO_KEY:
            text = text.replace(self._api_key, "<redacted>")
        return text
