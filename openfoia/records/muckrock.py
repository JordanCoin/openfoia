"""MuckRock API adapter — search 150k+ completed FOIA requests.

MuckRock is a nonprofit that files and tracks FOIA requests. Their API
exposes 46k+ completed requests with full communications and attached
documents. No API key needed for public data.

API docs: https://www.muckrock.com/api/
Rate limit: 1 req/sec average, 20 burst
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import RecordAdapter, RecordEntity, SearchResult

logger = logging.getLogger(__name__)

API_BASE = "https://www.muckrock.com/api_v1"


class MuckRockAdapter(RecordAdapter):
    """Search MuckRock's database of completed FOIA requests.

    Returns FOIA request metadata as entities: the requesting user,
    the target agency, the subject, response documents, and tracking info.

    Usage::

        adapter = MuckRockAdapter()
        results = await adapter.search("EPA water contamination")

        # Or search by agency
        results = await adapter.search("FBI", agency=True)

        # Fetch a specific request with all communications and files
        request = await adapter.fetch("68490")
    """

    source_name = "muckrock"

    def __init__(self, api_key: str | None = None):
        """Initialize. API key is optional — public requests are freely accessible."""
        self.api_key = api_key
        self._headers: dict[str, str] = {"content-type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Token {api_key}"

    async def search(
        self,
        query: str,
        status: str = "done",
        page: int = 1,
        page_size: int = 25,
        agency: bool = False,
        **kwargs: Any,
    ) -> SearchResult:
        """Search MuckRock FOIA requests.

        Args:
            query: Search term (matched against title and communications)
            status: Filter by status (done, ack, processed, appealing, etc.)
            page: Page number for pagination
            page_size: Results per page (max 100)
            agency: If True, search agencies instead of requests
        """
        if agency:
            return await self._search_agencies(query, page, page_size)

        params: dict[str, Any] = {
            "format": "json",
            "page": page,
            "page_size": min(page_size, 100),
        }
        if status:
            params["status"] = status

        # MuckRock's API has no full-text search. Available filters:
        # title (exact), user, agency, jurisdiction, tags, embargo_status, status.
        # Tags work best for topic-based search (e.g., "fbi", "epa", "surveillance").
        # We use tags as primary search and fall back to client-side title filtering.
        params["tags"] = query.lower().strip().replace(" ", "-")

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{API_BASE}/foia/",
                params=params,
                headers=self._headers,
            )
            resp.raise_for_status()
            data = resp.json()

            # If tags returned 0, try user filter (people often search by requester)
            if data.get("count", 0) == 0:
                fallback_params = {
                    "format": "json",
                    "page": page,
                    "page_size": min(page_size, 100),
                    "user": query,
                }
                if status:
                    fallback_params["status"] = status
                resp2 = await client.get(
                    f"{API_BASE}/foia/",
                    params=fallback_params,
                    headers=self._headers,
                )
                if resp2.status_code == 200:
                    fallback_data = resp2.json()
                    if fallback_data.get("count", 0) > 0:
                        data = fallback_data

        entities = []
        for req in data.get("results", []):
            # Each FOIA request becomes an entity with rich metadata
            entities.append(
                RecordEntity(
                    entity_type="FOIA_REQUEST",
                    name=req.get("title", "Untitled"),
                    source="muckrock",
                    source_url=f"https://www.muckrock.com/foi/{req.get('slug', '')}-{req.get('id', '')}/",
                    jurisdiction=None,
                    status=req.get("status"),
                    identifiers={
                        "muckrock_id": str(req.get("id", "")),
                        "tracking_id": req.get("tracking_id", ""),
                    },
                    extra_data={
                        "username": req.get("username", ""),
                        "agency_id": req.get("agency"),
                        "submitted": req.get("datetime_submitted"),
                        "completed": req.get("datetime_done"),
                        "tags": req.get("tags", []),
                        "price": req.get("price"),
                        "communications_count": len(req.get("communications", [])),
                        "files_count": sum(
                            len(c.get("files", [])) for c in req.get("communications", [])
                        ),
                        "file_types": list(
                            {
                                f.get("ffile", "").rsplit(".", 1)[-1].lower()
                                for c in req.get("communications", [])
                                for f in c.get("files", [])
                                if "." in f.get("ffile", "")
                            }
                        ),
                    },
                )
            )

        return SearchResult(
            source="muckrock",
            query=query,
            total_results=data.get("count", 0),
            entities=entities,
            page=page,
            per_page=page_size,
        )

    async def _search_agencies(
        self,
        query: str,
        page: int,
        page_size: int,
    ) -> SearchResult:
        """Search MuckRock's agency database."""
        params: dict[str, Any] = {
            "format": "json",
            "page": page,
            "page_size": min(page_size, 100),
            "search": query,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{API_BASE}/agency/",
                params=params,
                headers=self._headers,
            )
            resp.raise_for_status()
            data = resp.json()

        entities = []
        for agency in data.get("results", []):
            entities.append(
                RecordEntity(
                    entity_type="ORGANIZATION",
                    name=agency.get("name", ""),
                    source="muckrock",
                    source_url=f"https://www.muckrock.com/agency/{agency.get('slug', '')}-{agency.get('id', '')}/",
                    jurisdiction=str(agency.get("jurisdiction")),
                    status=agency.get("status"),
                    identifiers={
                        "muckrock_agency_id": str(agency.get("id", "")),
                    },
                    extra_data={
                        "types": agency.get("types", []),
                        "requires_proxy": agency.get("requires_proxy", False),
                        "average_response_time": agency.get("average_response_time"),
                        "success_rate": agency.get("success_rate"),
                        "absolute_url": agency.get("absolute_url", ""),
                    },
                )
            )

        return SearchResult(
            source="muckrock",
            query=query,
            total_results=data.get("count", 0),
            entities=entities,
            page=page,
            per_page=page_size,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Fetch a specific FOIA request by MuckRock ID.

        Returns the full request with all communications and file URLs.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{API_BASE}/foia/{identifier}/",
                params={"format": "json"},
                headers=self._headers,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            req = resp.json()

        # Extract all file URLs from communications
        files = []
        communications = []
        for comm in req.get("communications", []):
            comm_data = {
                "date": comm.get("datetime"),
                "from_user": comm.get("from_user"),
                "subject": comm.get("subject"),
                "is_response": comm.get("response", False),
                "status": comm.get("status"),
            }
            for f in comm.get("files", []):
                files.append(
                    {
                        "id": f.get("id"),
                        "url": f.get("ffile"),
                        "date": comm.get("datetime"),
                    }
                )
            communications.append(comm_data)

        return RecordEntity(
            entity_type="FOIA_REQUEST",
            name=req.get("title", "Untitled"),
            source="muckrock",
            source_url=f"https://www.muckrock.com/foi/{req.get('slug', '')}-{req.get('id', '')}/",
            status=req.get("status"),
            identifiers={
                "muckrock_id": str(req.get("id", "")),
                "tracking_id": req.get("tracking_id", ""),
            },
            extra_data={
                "username": req.get("username", ""),
                "agency_id": req.get("agency"),
                "submitted": req.get("datetime_submitted"),
                "completed": req.get("datetime_done"),
                "tags": req.get("tags", []),
                "price": req.get("price"),
                "communications": communications,
                "files": files,
                "files_count": len(files),
            },
        )

    async def download_files(
        self,
        identifier: str,
        output_dir: str = ".",
    ) -> list[str]:
        """Download all response documents for a FOIA request.

        Returns list of downloaded file paths.
        """
        from pathlib import Path

        entity = await self.fetch(identifier)
        if not entity:
            return []

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        downloaded = []

        files = entity.extra_data.get("files", [])
        if not files:
            return []

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for f in files:
                url = f.get("url")
                if not url:
                    continue

                try:
                    resp = await client.get(url)
                    resp.raise_for_status()

                    # Extract filename from URL
                    filename = url.split("/")[-1]
                    dest = output_path / filename

                    dest.write_bytes(resp.content)
                    downloaded.append(str(dest))
                    logger.info("Downloaded %s (%d bytes)", filename, len(resp.content))
                except Exception as e:
                    logger.warning("Failed to download %s: %s", url, e)

        return downloaded
