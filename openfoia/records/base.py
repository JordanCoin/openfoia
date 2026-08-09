"""Base class for public records adapters."""

from __future__ import annotations

import ipaddress
import posixpath
import re
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse


def validate_download_url(url: str) -> str:
    """Return *url* if it is safe to fetch, else raise ValueError.

    File URLs in third-party API responses (MuckRock ``ffile``,
    DocumentCloud ``asset_url``) are attacker-influenced: a compromised or
    MITM'd upstream can point them at ``file:///etc/passwd`` or cloud
    metadata endpoints, and the body is written straight to disk.
    """
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise ValueError(f"Refusing to download over {parsed.scheme or 'missing'} scheme: {url!r}")

    host = parsed.hostname
    if not host:
        raise ValueError(f"Refusing to download from a URL with no host: {url!r}")

    # Block obvious internal targets without doing a network lookup.
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise ValueError(f"Refusing to download from a loopback host: {url!r}")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    ):
        raise ValueError(f"Refusing to download from a non-public address: {url!r}")

    return url


def resolves_to_public_address(host: str) -> bool:
    """Best-effort DNS check that *host* is not an internal address.

    Separate from :func:`validate_download_url` so callers can opt in; DNS
    resolution is itself a network call.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


async def download_to_file(client: Any, url: str, dest: Any, max_bytes: int) -> int:
    """Stream *url* into *dest*, aborting as soon as *max_bytes* is exceeded.

    Buffering via ``response.content`` first would let a compromised or
    MITM'd upstream force a huge allocation before any size check ran. This
    enforces the cap incrementally and removes the partial file on abort.

    Returns the number of bytes written.
    """
    from pathlib import Path

    dest = Path(dest)
    written = 0

    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in response.aiter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(
                            f"Refusing download over {max_bytes} bytes (cap exceeded): {url!r}"
                        )
                    f.write(chunk)
    except BaseException:
        if dest.exists():
            dest.unlink()
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

    async def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> Any:
        """GET *url* and return decoded JSON, raising AdapterRequestError.

        Centralized so every adapter reports transport and HTTP failures the
        same way instead of letting them surface as empty result sets.
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
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
