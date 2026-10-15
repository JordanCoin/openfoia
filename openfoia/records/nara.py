"""NARA National Archives Catalog adapter (catalog.archives.gov API v2).

Needs a free key: email Catalog_API@nara.gov (there is no signup form) and set
OPENFOIA_NARA_API_KEY (or CATALOG_API_KEY, NARA's own name for it). The default key allows 10,000 queries a month; this
adapter counts them locally and warns at 80 percent. Requests are throttled
to one per second. API help: https://www.archives.gov/research/catalog/help/api

NARA's terms ask API users not to cache or scrape; only records the user
explicitly fetches are stored, and the required attribution notice is printed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

from .base import AdapterRequestError, RecordAdapter, RecordEntity, SearchResult

logger = logging.getLogger(__name__)

API_BASE_DEFAULT = "https://catalog.archives.gov/api/v2"
KEY_ENV = "OPENFOIA_NARA_API_KEY"
KEY_ENV_ALIAS = "CATALOG_API_KEY"  # the name NARA's own bulk-download scripts read
QUOTA_ENV = "OPENFOIA_NARA_IGNORE_QUOTA"
MONTHLY_QUOTA = 10_000
QUOTA_FILE = "nara_quota.json"
ATTRIBUTION = (
    "This product uses the National Archives Catalog API but is not endorsed or "
    "certified by the National Archives and Records Administration."
)
DESCRIPTION_MARKER = "NARA catalog description (no OCR text)"
NO_KEY_MESSAGE = (
    f"NARA Catalog API requires a key. Email Catalog_API@nara.gov for a free one "
    f"(there is no signup form) and set {KEY_ENV} (or {KEY_ENV_ALIAS})."
)
PAGE_MAX = 100
# Digital objects (scans, PDFs) are served from NARA's S3 buckets, not the API host.
OBJECT_HOST = "nara-media-001.s3.amazonaws.com"
OBJECT_PATH_STYLE = ("s3.amazonaws.com", "/NARAprodstorage/")
MAX_OBJECT_BYTES = 500 * 1024 * 1024

_last_call = 0.0
_quota_warned = False


def api_base() -> str:
    """The API base, honouring OPENFOIA_NARA_API_BASE only for NARA's host or a local test server.

    The key goes in a header on every request, so an arbitrary base would send
    it to an arbitrary host.
    """
    from urllib.parse import urlsplit

    base = os.environ.get("OPENFOIA_NARA_API_BASE") or API_BASE_DEFAULT
    parts = urlsplit(base)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if not ((parts.scheme == "https" and parts.hostname == "catalog.archives.gov") or local):
        raise AdapterRequestError(
            "OPENFOIA_NARA_API_BASE must be https://catalog.archives.gov/... or a localhost "
            f"test server; refusing to send the API key to {base!r}."
        )
    return base.rstrip("/")


def object_url_allowed(url: str) -> bool:
    """True only for https URLs on NARA's object storage; anything else is not downloaded."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host, prefix = OBJECT_PATH_STYLE
    return parts.hostname == OBJECT_HOST or (
        parts.hostname == host and parts.path.startswith(prefix)
    )


async def _throttle() -> None:
    global _last_call
    wait = 1.0 - (time.monotonic() - _last_call)  # ponytail: 1 req/s, process-wide
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call = time.monotonic()


def quota_path():
    from ..db import get_data_dir

    return get_data_dir() / QUOTA_FILE


def quota_count(now: datetime | None = None) -> int:
    """Requests sent this calendar month, from the local counter."""
    month = (now or datetime.now(UTC)).strftime("%Y-%m")
    try:
        data = json.loads(quota_path().read_text())
    except (OSError, ValueError):
        return 0
    return int(data.get("count", 0)) if data.get("month") == month else 0


def _record_request() -> int:
    """Count one request against this month's quota; returns the new count."""
    now = datetime.now(UTC)
    count = quota_count(now) + 1
    path = quota_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"month": now.strftime("%Y-%m"), "count": count}))
    except OSError as exc:  # the counter is advisory; never block on it
        logger.debug("Could not write NARA quota file: %s", exc)
    return count


def _check_quota() -> None:
    used = quota_count()
    if used >= MONTHLY_QUOTA and not os.environ.get(QUOTA_ENV):
        raise AdapterRequestError(
            f"NARA monthly quota reached ({used}/{MONTHLY_QUOTA} queries this month by the local "
            f"counter). Wait for next month, ask NARA for a higher tier, or set {QUOTA_ENV}=1."
        )
    global _quota_warned
    if used >= MONTHLY_QUOTA * 0.8 and not _quota_warned:
        _quota_warned = True  # once per process; the number only goes up
        logger.warning("NARA quota: %d of %d queries used this month.", used, MONTHLY_QUOTA)


class NARAAdapter(RecordAdapter):
    """Search and fetch descriptions (and OCR text where present) from the NARA Catalog."""

    source_name = "nara"

    def __init__(self, api_key: str | None = None, *, egress=None):
        super().__init__(egress=egress)
        self._api_key = api_key or os.environ.get(KEY_ENV) or os.environ.get(KEY_ENV_ALIAS)

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    async def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise AdapterRequestError(NO_KEY_MESSAGE)
        base = api_base()  # refuses to send the key anywhere but NARA or localhost
        _check_quota()
        await _throttle()
        _record_request()
        return await self._request(
            f"{base}/records/search",
            params=params,
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            timeout=30,
        )

    async def search(self, query: str, **kwargs: Any) -> SearchResult:
        """One page of results; ``all_pages=True`` walks every page with searchAfter
        up to ``max_hits``, one quota-counted request per page (NARA's AllChildRecords)."""
        page = int(kwargs.get("page") or 1)
        per_page = min(int(kwargs.get("page_size") or 25), PAGE_MAX)
        params: dict[str, Any] = {"q": query or "*", "limit": per_page, "page": page}
        if kwargs.get("ancestor"):
            params["ancestorNaId"] = str(kwargs["ancestor"])
        if kwargs.get("available_online"):
            params["availableOnline"] = "true"
        if kwargs.get("type_of_materials"):
            params["typeOfMaterials"] = kwargs["type_of_materials"]
        if kwargs.get("all_pages"):
            return await self._walk(query, params, int(kwargs.get("max_hits") or 1000))
        try:
            data = await self._get(params)
        except AdapterRequestError as exc:
            return self._failed(query, str(exc), page=page, per_page=per_page)
        hits = (data.get("body") or {}).get("hits") or {}
        total = (hits.get("total") or {}).get("value") or 0
        entities = [_entity(h.get("_source") or {}) for h in hits.get("hits") or []]
        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=int(total),
            entities=[e for e in entities if e],
            page=page,
            per_page=per_page,
            raw_response=data,
        )

    async def _walk(self, query: str, params: dict[str, Any], max_hits: int) -> SearchResult:
        # searchAfter is incompatible with page: first value "*", then the last hit's sort[0].
        params = {k: v for k, v in params.items() if k != "page"}
        params["limit"] = PAGE_MAX
        params["searchAfter"] = "*"
        entities: list[RecordEntity] = []
        total = 0
        while len(entities) < max_hits:
            try:
                data = await self._get(params)
            except AdapterRequestError as exc:
                failed = self._failed(query, str(exc), per_page=PAGE_MAX)
                failed.entities = entities  # keep what arrived before the failure
                return failed
            hits = (data.get("body") or {}).get("hits") or {}
            total = int((hits.get("total") or {}).get("value") or total)
            page_hits = hits.get("hits") or []
            entities += [e for e in (_entity(h.get("_source") or {}) for h in page_hits) if e]
            sort = page_hits[-1].get("sort") if page_hits else None
            if len(page_hits) < PAGE_MAX or not sort:
                break
            params["searchAfter"] = sort[0]
        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=total,
            entities=entities[:max_hits],
            page=1,
            per_page=PAGE_MAX,
        )

    async def download_objects(
        self, entity: RecordEntity, dest_dir
    ) -> list[tuple[dict[str, Any], Any, str]]:
        """Download *entity*'s digital objects into *dest_dir* (NARA's DownloadObjects).

        Returns ``(object, path, status)`` per object, status one of downloaded,
        present (already on disk at the expected size), refused (not NARA's object
        host) or ``failed: ...``. Object downloads are not catalog queries, so they
        are not counted against the quota, but they keep the 1 req/s throttle.
        """
        from pathlib import Path

        from ..net import egress_client
        from .base import download_to_file, safe_download_filename

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = []
        for i, obj in enumerate(entity.extra_data.get("objects") or [], 1):
            url = obj.get("url") or ""
            dest = dest_dir / (
                _object_filename(obj.get("filename"))
                or safe_download_filename(url, default=f"object-{i}")
            )
            size = obj.get("size")
            if dest.exists() and (not size or dest.stat().st_size == size):
                out.append((obj, dest, "present"))
                continue
            if not object_url_allowed(url):
                out.append((obj, dest, "refused"))
                continue
            await _throttle()
            try:
                async with egress_client(self._egress, timeout=120) as client:
                    await download_to_file(client, url, dest, max_bytes=MAX_OBJECT_BYTES)
            except Exception as exc:
                out.append((obj, dest, f"failed: {type(exc).__name__}: {exc}"))
                continue
            out.append((obj, dest, "downloaded"))
        return out

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        naid = str(identifier).strip().removeprefix("NARA-")
        data = await self._get({"naId": naid, "includeExtractedText": "true"})
        hits = ((data.get("body") or {}).get("hits") or {}).get("hits") or []
        if not hits:
            return None
        entity = _entity(hits[0].get("_source") or {})
        if entity is None:
            return None
        record = (hits[0].get("_source") or {}).get("record") or {}
        entity.extra_data["objects"] = [
            {
                "type": obj.get("objectType"),
                "url": obj.get("objectUrl"),
                "filename": obj.get("objectFilename"),
                "size": obj.get("objectFileSize"),
            }
            for obj in record.get("digitalObjects") or []
        ]
        # Keep blank pages as "" so positions hold: page 3 of the scan stays p.3.
        page_texts = [
            (obj.get("extractedText") or "").strip() for obj in record.get("digitalObjects") or []
        ]
        if any(page_texts):
            from ..doctext import join_pages

            entity.extra_data["page_texts"] = page_texts
            entity.extra_data["full_text"] = join_pages(page_texts)
            entity.extra_data["text_kind"] = "ocr"
        else:
            # No OCR text on this record: store the catalog description so the
            # citation exists locally, with a marker so it never reads as the record.
            desc = _description_text(record)
            entity.extra_data["full_text"] = f"{DESCRIPTION_MARKER}\n\n{desc}" if desc else None
            entity.extra_data["text_kind"] = "description"
        entity.extra_data["text_length"] = len(entity.extra_data.get("full_text") or "")
        return entity

    async def pull_text(
        self, identifier: str, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        """Fetch a NAID and store its text as a local Document (see store.py)."""
        entity = await self.fetch(identifier)
        if not entity:
            return None, None
        return self.store(entity, identifier, case_id)

    def store(
        self, entity: RecordEntity, identifier: str, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        from .store import store_text_document

        naid = entity.identifiers.get("naid", identifier)
        described = entity.extra_data.get("text_kind") == "description"
        return store_text_document(
            entity,
            request_number=f"NARA-{naid}",
            filename=f"nara-{naid}{'-description' if described else ''}.txt",
            body=(
                f"{DESCRIPTION_MARKER}: {entity.source_url}"
                if described
                else f"Pulled from the National Archives Catalog: {entity.source_url}"
            ),
            case_id=case_id,
        )


def _object_filename(name: str | None) -> str:
    """Bare objectFilename made safe without urlparse, so '#', '?' and ';' survive as '_'."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")[:255]


def _description_text(record: dict[str, Any]) -> str:
    parts = [record.get("title") or ""]
    for key in ("scopeAndContentNote", "generalNotes"):
        val = record.get(key)
        if isinstance(val, list):
            val = "\n".join(str(v) for v in val)
        if val:
            parts.append(str(val))
    return "\n\n".join(p for p in parts if p).strip()


def _entity(source: dict[str, Any]) -> RecordEntity | None:
    record = source.get("record") or {}
    naid = record.get("naId")
    if naid is None:
        return None
    naid = str(naid)
    ancestors = record.get("ancestors") or []
    record_group = next(
        (
            str(a.get("recordGroupNumber"))
            for a in ancestors
            if a.get("recordGroupNumber") is not None
        ),
        None,
    )
    collection = next((a.get("title") for a in ancestors if a.get("title")), None)
    dates = record.get("productionDates") or []
    date = None
    if dates and isinstance(dates[0], dict):
        date = dates[0].get("logicalDate") or dates[0].get("year")
    objects = record.get("digitalObjects") or []
    snippet = record.get("scopeAndContentNote")
    if isinstance(snippet, list):
        snippet = " ".join(str(s) for s in snippet)
    identifiers = {"naid": naid}
    if record_group:
        identifiers["record_group"] = record_group
    return RecordEntity(
        entity_type="DOCUMENT",
        name=record.get("title") or f"NAID {naid}",
        source="nara",
        source_url=f"https://catalog.archives.gov/id/{naid}",
        identifiers=identifiers,
        extra_data={
            "pages": len(objects) or None,
            "level": record.get("levelOfDescription"),
            "date": str(date) if date else None,
            "collection": collection,
            "snippet": snippet,
            "description": snippet,
            "access": (record.get("accessRestriction") or {}).get("status"),
            "online": bool(objects),
        },
    )
