"""Regulations.gov adapter — follow the lobbying on federal rulemaking.

Every federal proposed rule gets public comments. Who's commenting tells
you who's trying to influence policy. Search documents, comments, and dockets.

Uses the Regulations.gov API v4. DEMO_KEY works, free key at api.data.gov.
API docs: https://open.gsa.gov/api/regulationsgov/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..net import EgressPolicy, egress_client
from .base import RecordAdapter, RecordEntity, SearchResult

logger = logging.getLogger(__name__)

API_BASE = "https://api.regulations.gov/v4"


class RegulationsGovAdapter(RecordAdapter):
    """Search federal rulemaking documents and public comments.

    Usage::

        adapter = RegulationsGovAdapter()
        results = await adapter.search("surveillance technology")
    """

    source_name = "regulations"

    def __init__(self, api_key: str | None = None, *, egress: EgressPolicy | None = None):
        super().__init__(egress=egress)
        self._api_key = api_key or "DEMO_KEY"

    async def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 25,
        **kwargs: Any,
    ) -> SearchResult:
        """Search federal rulemaking documents by keyword."""
        params: dict[str, Any] = {
            "filter[searchTerm]": query,
            "page[size]": max(5, min(page_size, 250)),
            "page[number]": page,
            "api_key": self._api_key,
        }

        doc_type = kwargs.get("doc_type")
        if doc_type:
            params["filter[documentType]"] = doc_type

        agency = kwargs.get("agency")
        if agency:
            params["filter[agencyId]"] = agency

        try:
            async with egress_client(self._egress, timeout=20) as client:
                resp = await client.get(
                    f"{API_BASE}/documents",
                    params=params,
                )
                if resp.status_code == 429:
                    msg = (
                        "Regulations.gov rate limit. Get a free key at https://api.data.gov/signup/"
                    )
                    logger.warning(msg)
                    return SearchResult(
                        source="regulations",
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
            msg = f"Regulations.gov search timed out for: {query}"
            logger.warning(msg)
            return SearchResult(
                source="regulations",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )
        except Exception as e:
            msg = f"Regulations.gov search failed: {e}"
            logger.warning(msg)
            return SearchResult(
                source="regulations",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )

        meta = data.get("meta", {})
        total = meta.get("totalElements", 0)

        entities = []
        for doc in data.get("data", []):
            attrs = doc.get("attributes", {})
            doc_id = doc.get("id", "")
            title = attrs.get("title", "Untitled")
            agency_id = attrs.get("agencyId", "")
            doc_type_val = attrs.get("documentType", "")
            posted = attrs.get("postedDate", "")

            entities.append(
                RecordEntity(
                    entity_type="REGULATION",
                    name=title,
                    source="regulations",
                    source_url=f"https://www.regulations.gov/document/{doc_id}" if doc_id else None,
                    identifiers={
                        "document_id": doc_id,
                        "docket_id": attrs.get("docketId") or "",
                    },
                    extra_data={
                        "agency": agency_id,
                        "document_type": doc_type_val,
                        "posted_date": posted[:10] if posted else None,
                        "comment_count": attrs.get("numberOfCommentsReceived"),
                    },
                )
            )

        return SearchResult(
            source="regulations",
            query=query,
            total_results=total,
            entities=entities,
            page=page,
            per_page=page_size,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Fetch a specific document by ID."""
        try:
            async with egress_client(self._egress, timeout=15) as client:
                resp = await client.get(
                    f"{API_BASE}/documents/{identifier}",
                    params={"api_key": self._api_key},
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("Regulations.gov fetch failed for %s: %s", identifier, e)
            return None

        doc = data.get("data", {})
        attrs = doc.get("attributes", {})
        doc_id = doc.get("id", identifier)

        return RecordEntity(
            entity_type="REGULATION",
            name=attrs.get("title", "Untitled"),
            source="regulations",
            source_url=f"https://www.regulations.gov/document/{doc_id}",
            identifiers={
                "document_id": doc_id,
                "docket_id": attrs.get("docketId") or "",
            },
            extra_data={
                "agency": attrs.get("agencyId"),
                "document_type": attrs.get("documentType"),
                "posted_date": (attrs.get("postedDate") or "")[:10],
                "comment_count": attrs.get("numberOfCommentsReceived"),
                "abstract": attrs.get("summary"),
            },
        )
