"""DocumentCloud adapter — search 10M+ public documents, pull text locally.

DocumentCloud is the document platform used by newsrooms worldwide. This
adapter enables three workflows:

1. SEARCH: Find documents by keyword across 10M+ public records
2. PULL: Fetch full text into the local pipeline (DC already extracted it)
3. CROSSREF: Check local entities against DocumentCloud documents

Uses the REST API directly (not the python-documentcloud library) for
speed and control. No auth needed for public documents.

API docs: https://api.www.documentcloud.org/api/
Rate limit: 10 req/sec, 500 req/day (anonymous). Free account removes daily limit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..net import EgressPolicy, egress_client
from .base import RecordAdapter, RecordEntity, SearchResult, validate_download_url

logger = logging.getLogger(__name__)

API_BASE = "https://api.www.documentcloud.org/api"
ASSET_BASE = "https://s3.documentcloud.org"

#: Parallel per-page text requests. DocumentCloud allows 10 req/sec.
PAGE_FETCH_CONCURRENCY = 4
#: Beyond this many pages, keep the full text as one page instead of
#: issuing one request per page.
MAX_PAGE_FETCH = 2000


async def _fetch_page_texts(
    client: httpx.AsyncClient, base: str, page_count: int
) -> list[str] | None:
    """Fetch ``{base}-p{n}.txt`` for every page; None if any page fails."""
    if not 0 < page_count <= MAX_PAGE_FETCH:
        return None
    sem = asyncio.Semaphore(PAGE_FETCH_CONCURRENCY)

    async def one(n: int) -> str:
        url = f"{base}-p{n}.txt"
        validate_download_url(url)
        async with sem:
            resp = await client.get(url, timeout=15)
        if resp.status_code in (429, 503):
            raise RuntimeError(f"DocumentCloud rate limit on page {n}")
        resp.raise_for_status()
        return resp.text

    try:
        # TaskGroup cancels the remaining requests as soon as one page fails.
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(one(n)) for n in range(1, page_count + 1)]
    except Exception as e:  # ExceptionGroup from the TaskGroup
        first = e.exceptions[0] if isinstance(e, ExceptionGroup) else e
        logger.warning("Per-page text unavailable, keeping full text as one page: %s", first)
        return None
    return [t.result() for t in tasks]


class DocumentCloudAdapter(RecordAdapter):
    """Search and pull documents from DocumentCloud's 10M+ public archive.

    Uses the REST API directly for speed. The python-documentcloud library
    adds significant overhead per result — httpx is faster for our use case.

    Usage::

        adapter = DocumentCloudAdapter()
        results = await adapter.search("FBI surveillance")
        entity = await adapter.fetch("2090186")
        doc_id, text = await adapter.pull_text("2090186")
    """

    source_name = "documentcloud"

    def __init__(self, auth_token: str | None = None, *, egress: EgressPolicy | None = None):
        """Initialize. Auth token optional — public docs are freely searchable.

        For higher rate limits, register at https://accounts.muckrock.com/
        and pass the JWT access token.
        """
        super().__init__(egress=egress)
        self._headers: dict[str, str] = {}
        if auth_token:
            self._headers["Authorization"] = f"Bearer {auth_token}"

    async def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 25,
        **kwargs: Any,
    ) -> SearchResult:
        """Search DocumentCloud for public documents.

        Returns results with highlights showing where the search term appears.
        The API supports Solr query syntax: title:budget AND data_year:2024
        """
        params: dict[str, Any] = {
            "q": query,
            "per_page": min(page_size, 100),
            "page": page,
            "hl": "true",
        }

        try:
            async with egress_client(self._egress, timeout=15) as client:
                resp = await client.get(
                    f"{API_BASE}/documents/search/",
                    params=params,
                    headers=self._headers,
                )
                if resp.status_code == 429:
                    msg = (
                        "DocumentCloud rate limit hit (500/day anonymous). "
                        "Register at https://accounts.muckrock.com/ for unlimited access."
                    )
                    logger.warning(msg)
                    return SearchResult(
                        source="documentcloud",
                        query=query,
                        total_results=0,
                        entities=[],
                        page=page,
                        per_page=page_size,
                        error=msg,
                    )
                if resp.status_code == 503:
                    msg = "DocumentCloud rate limit (10/sec) — slow down."
                    logger.warning(msg)
                    return SearchResult(
                        source="documentcloud",
                        query=query,
                        total_results=0,
                        entities=[],
                        page=page,
                        per_page=page_size,
                        error=msg,
                    )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            msg = f"DocumentCloud search timed out for: {query}"
            logger.warning(msg)
            return SearchResult(
                source="documentcloud",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )
        except Exception as e:
            msg = f"DocumentCloud search failed: {e}"
            logger.warning(msg)
            return SearchResult(
                source="documentcloud",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )

        total = data.get("count", 0)
        entities = []

        for doc in data.get("results", []):
            # Extract highlights (where the search term appears in the doc)
            # API returns highlights per page: {"page_no_1": ["snippet..."], "page_no_5": [...]}
            highlights = []
            hl_data = doc.get("highlights")
            if hl_data and isinstance(hl_data, dict):
                for page_key in sorted(hl_data.keys()):
                    for text_snippet in hl_data[page_key]:
                        clean = text_snippet.replace("<em>", "").replace("</em>", "").strip()
                        if clean:
                            highlights.append(clean)
                            if len(highlights) >= 3:
                                break
                    if len(highlights) >= 3:
                        break

            entities.append(
                RecordEntity(
                    entity_type="DOCUMENT",
                    name=doc.get("title", "Untitled"),
                    source="documentcloud",
                    source_url=doc.get("canonical_url"),
                    identifiers={
                        "documentcloud_id": str(doc.get("id", "")),
                        "slug": doc.get("slug", ""),
                    },
                    extra_data={
                        "pages": doc.get("page_count"),
                        "access": doc.get("access"),
                        "description": doc.get("description"),
                        "source": doc.get("source"),
                        "language": doc.get("language"),
                        "created_at": doc.get("created_at"),
                        "pdf_url": doc.get("file_url"),
                        "highlights": highlights,
                        "highlights_count": len(highlights),
                    },
                )
            )

        return SearchResult(
            source="documentcloud",
            query=query,
            total_results=total,
            entities=entities,
            page=page,
            per_page=page_size,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Fetch a specific document by ID, including full extracted text.

        DocumentCloud already extracted the text — no need for local OCR.
        """
        try:
            async with egress_client(self._egress, timeout=30) as client:
                # Get document metadata
                resp = await client.get(
                    f"{API_BASE}/documents/{identifier}/",
                    headers=self._headers,
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                doc = resp.json()

                # Get full text (DC already extracted it)
                full_text = None
                page_texts = None
                text_url = doc.get("asset_url")
                slug = doc.get("slug", "")
                doc_id = doc.get("id", identifier)
                if text_url:
                    txt_url = f"{text_url}documents/{doc_id}/{slug}.txt"
                    try:
                        # asset_url comes from the API response, so it is
                        # attacker-influenced if the upstream is compromised.
                        validate_download_url(txt_url)
                        txt_resp = await client.get(txt_url, timeout=15)
                        if txt_resp.status_code == 200:
                            full_text = txt_resp.text
                    except Exception as e:
                        logger.debug("Could not fetch text: %s", e)
                    page_texts = await _fetch_page_texts(
                        client,
                        f"{text_url}documents/{doc_id}/pages/{slug}",
                        doc.get("page_count") or 0,
                    )
                    if full_text is None and page_texts:
                        from ..doctext import join_pages

                        full_text = join_pages(page_texts)

        except Exception as e:
            logger.warning("DocumentCloud fetch failed for %s: %s", identifier, e)
            return None

        return RecordEntity(
            entity_type="DOCUMENT",
            name=doc.get("title", "Untitled"),
            source="documentcloud",
            source_url=doc.get("canonical_url"),
            identifiers={
                "documentcloud_id": str(doc.get("id", "")),
                "slug": doc.get("slug", ""),
            },
            extra_data={
                "pages": doc.get("page_count"),
                "access": doc.get("access"),
                "description": doc.get("description"),
                "source": doc.get("source"),
                "language": doc.get("language"),
                "created_at": doc.get("created_at"),
                "pdf_url": doc.get("file_url"),
                "full_text": full_text,
                "page_texts": page_texts,
                "text_length": len(full_text) if full_text else 0,
            },
        )

    async def pull_text(
        self, identifier: str, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        """Pull a document's text into the local OpenFOIA database.

        Fetches from DocumentCloud, saves a Document row with extracted_text
        already populated. Ready for entity extraction immediately.

        *case_id* attaches the document's request to that investigation.

        Returns (document_id, full_text) or (None, None) on failure.
        """
        from .store import store_text_document

        entity = await self.fetch(identifier)
        if not entity:
            return None, None
        dc_id = entity.identifiers.get("documentcloud_id", identifier)
        return store_text_document(
            entity,
            request_number=f"DC-{dc_id}",
            filename=f"documentcloud-{dc_id}.txt",
            body=f"Pulled from DocumentCloud: {entity.source_url or dc_id}",
            case_id=case_id,
        )
