"""GovInfo adapter — search across all three branches of the US government.

Congressional bills, court opinions, Federal Register rules, GAO reports,
congressional hearings, CFR, and more. 50+ collections, millions of documents.

Uses the GovInfo API v2. Requires a free API key from api.data.gov
(DEMO_KEY works for testing). Rate limit: 1,200 req/min.
API docs: https://api.govinfo.gov/docs/
GitHub:   https://github.com/usgpo/api
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..net import EgressPolicy, egress_client
from .base import RecordAdapter, RecordEntity, SearchResult

logger = logging.getLogger(__name__)

API_BASE = "https://api.govinfo.gov"

# Key collections for investigative use
COLLECTION_LABELS = {
    "BILLS": "Congressional Bill",
    "BILLSTATUS": "Bill Status",
    "CDOC": "Congressional Document",
    "CHRG": "Congressional Hearing",
    "CREC": "Congressional Record",
    "CRPT": "Congressional Report",
    "FR": "Federal Register",
    "CFR": "Code of Federal Regulations",
    "GAOREPORTS": "GAO Report",
    "GOVPUB": "Government Publication",
    "PLAW": "Public Law",
    "USCOURTS": "Court Opinion",
    "USCODE": "US Code",
    "ECFR": "Electronic CFR",
    "BUDGET": "US Budget",
    "EO": "Executive Order",
    "CRI": "Congressional Research Report",
}


class GovInfoAdapter(RecordAdapter):
    """Search documents from all three branches of the US government.

    Court opinions, congressional reports, Federal Register rules, GAO reports,
    bills, hearings, and more. 50+ collections.

    Usage::

        adapter = GovInfoAdapter()
        results = await adapter.search("surveillance technology")
        results = await adapter.search("Palantir", collection="USCOURTS")
    """

    source_name = "govinfo"

    def __init__(self, api_key: str | None = None, *, egress: EgressPolicy | None = None):
        """Initialize. Uses DEMO_KEY if no key provided.

        For higher rate limits, get a free key at https://api.data.gov/signup/
        """
        super().__init__(egress=egress)
        self._api_key = api_key or "DEMO_KEY"

    async def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 25,
        **kwargs: Any,
    ) -> SearchResult:
        """Search GovInfo across all collections or a specific one.

        Pass collection='USCOURTS' for court opinions, 'FR' for Federal Register,
        'CHRG' for congressional hearings, etc.
        """
        collection = kwargs.get("collection")

        payload: dict[str, Any] = {
            "query": query,
            "pageSize": min(page_size, 100),
            "offsetMark": "*",
            "sorts": [{"field": "relevancy", "sortOrder": "DESC"}],
        }
        if collection:
            payload["collection"] = collection

        try:
            async with egress_client(self._egress, timeout=20) as client:
                resp = await client.post(
                    f"{API_BASE}/search",
                    params={"api_key": self._api_key},
                    json=payload,
                )
                if resp.status_code == 429:
                    msg = "GovInfo rate limit hit. Get a free key at https://api.data.gov/signup/"
                    logger.warning(msg)
                    return SearchResult(
                        source="govinfo",
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
            msg = f"GovInfo search timed out for: {query}"
            logger.warning(msg)
            return SearchResult(
                source="govinfo",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )
        except Exception as e:
            msg = f"GovInfo search failed: {e}"
            logger.warning(msg)
            return SearchResult(
                source="govinfo",
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
            collection_code = doc.get("collectionCode", "")
            collection_label = COLLECTION_LABELS.get(collection_code, collection_code)
            package_id = doc.get("packageId", "")
            title = doc.get("title", "Untitled")
            authors = doc.get("governmentAuthor", [])
            author_str = ", ".join(authors) if authors else None

            # Build source URL
            source_url = f"https://www.govinfo.gov/app/details/{package_id}" if package_id else None

            entities.append(
                RecordEntity(
                    entity_type="GOVERNMENT_DOCUMENT",
                    name=title,
                    source="govinfo",
                    source_url=source_url,
                    identifiers={
                        "package_id": package_id,
                        "granule_id": doc.get("granuleId") or "",
                        "collection": collection_code,
                    },
                    extra_data={
                        "collection": collection_label,
                        "collection_code": collection_code,
                        "date_issued": doc.get("dateIssued"),
                        "government_author": author_str,
                        "last_modified": doc.get("lastModified"),
                    },
                )
            )

        return SearchResult(
            source="govinfo",
            query=query,
            total_results=total,
            entities=entities,
            page=page,
            per_page=page_size,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Fetch a specific document package by ID."""
        try:
            async with egress_client(self._egress, timeout=20) as client:
                resp = await client.get(
                    f"{API_BASE}/packages/{identifier}/summary",
                    params={"api_key": self._api_key},
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("GovInfo fetch failed for %s: %s", identifier, e)
            return None

        collection_code = data.get("collectionCode", "")

        return RecordEntity(
            entity_type="GOVERNMENT_DOCUMENT",
            name=data.get("title", "Untitled"),
            source="govinfo",
            source_url=f"https://www.govinfo.gov/app/details/{identifier}",
            identifiers={
                "package_id": identifier,
                "collection": collection_code,
            },
            extra_data={
                "collection": COLLECTION_LABELS.get(collection_code, collection_code),
                "collection_code": collection_code,
                "date_issued": data.get("dateIssued"),
                "government_author": ", ".join(data.get("governmentAuthor", [])),
                "pages": data.get("pageCount"),
                "category": data.get("category"),
            },
        )
