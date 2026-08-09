"""MuckRock API adapter — search 150k+ completed FOIA requests.

MuckRock is a nonprofit that files and tracks FOIA requests. Their API
exposes 46k+ completed requests with full communications and attached
documents. No API key needed for public data.

API docs: https://www.muckrock.com/api/
Rate limit: 1 req/sec average, 20 burst
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import httpx

from .base import (
    RecordAdapter,
    RecordEntity,
    SearchResult,
    download_to_file,
    safe_download_filename,
    validate_download_url,
)

#: Cap on a single downloaded response file (100 MiB), matching ingest.
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

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

        # MuckRock's API has no full-text search. Multi-layer strategy:
        # 1. Tags (best for topics: "surveillance", "foia")
        # 2. Agency name → agency ID → FOIA requests (best for "FBI", "EPA")
        # 3. User filter (for requester names)
        # Each layer tried in order; first with results wins.

        data: dict = {"count": 0, "results": []}

        async with httpx.AsyncClient(timeout=30) as client:
            # Layer 1: Tags
            tag_params = {**params, "tags": query.lower().strip().replace(" ", "-")}
            resp = await client.get(f"{API_BASE}/foia/", params=tag_params, headers=self._headers)
            if resp.status_code == 200:
                data = resp.json()

            # Layer 2: Agency name lookup → filter by agency ID
            if data.get("count", 0) == 0:
                agency_id = await self._resolve_agency_id(client, query)
                if agency_id:
                    agency_params = {**params, "agency": agency_id}
                    resp2 = await client.get(
                        f"{API_BASE}/foia/",
                        params=agency_params,
                        headers=self._headers,
                    )
                    if resp2.status_code == 200:
                        agency_data = resp2.json()
                        if agency_data.get("count", 0) > 0:
                            data = agency_data

            # Layer 3: User filter
            if data.get("count", 0) == 0:
                user_params = {**params, "user": query}
                resp3 = await client.get(
                    f"{API_BASE}/foia/",
                    params=user_params,
                    headers=self._headers,
                )
                if resp3.status_code == 200:
                    user_data = resp3.json()
                    if user_data.get("count", 0) > 0:
                        data = user_data

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

    # Common abbreviation → full agency name mapping
    _AGENCY_NAMES: ClassVar[dict[str, str]] = {
        "fbi": "Federal Bureau of Investigation",
        "cia": "Central Intelligence Agency",
        "nsa": "National Security Agency",
        "epa": "Environmental Protection Agency",
        "doj": "Department of Justice",
        "dod": "Department of Defense",
        "dhs": "Department of Homeland Security",
        "ice": "U.S. Immigration and Customs Enforcement",
        "atf": "Bureau of Alcohol, Tobacco, Firearms and Explosives",
        "dea": "Drug Enforcement Administration",
        "sec": "Securities and Exchange Commission",
        "fda": "Food and Drug Administration",
        "cdc": "Centers for Disease Control and Prevention",
        "irs": "Internal Revenue Service",
        "usda": "Department of Agriculture",
        "hud": "Department of Housing and Urban Development",
        "dot": "Department of Transportation",
        "faa": "Federal Aviation Administration",
        "nasa": "National Aeronautics and Space Administration",
        "usps": "U.S. Postal Service",
    }

    async def _resolve_agency_id(self, client: httpx.AsyncClient, query: str) -> int | None:
        """Resolve a query to a MuckRock agency ID.

        Tries abbreviation lookup first, then exact name match on the API.
        """
        q = query.strip().lower()

        # Try abbreviation map
        full_name = self._AGENCY_NAMES.get(q)
        if not full_name:
            # Try the query as a full name directly
            full_name = query.strip()

        try:
            resp = await client.get(
                f"{API_BASE}/agency/",
                params={"format": "json", "name": full_name, "page_size": 1},
                headers=self._headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("count", 0) > 0:
                    return data["results"][0]["id"]
        except Exception:
            pass

        return None

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

        # follow_redirects is off: a redirect is a second, unvalidated URL and
        # would bypass the scheme/host checks below.
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            for f in files:
                url = f.get("url")
                if not url:
                    continue

                try:
                    validate_download_url(url)

                    filename = safe_download_filename(url)
                    dest = output_path / filename

                    # Streamed with an incremental cap: buffering the whole
                    # body first would let a compromised upstream force a
                    # large allocation before the size check ran.
                    written = await download_to_file(
                        client, url, dest, max_bytes=MAX_DOWNLOAD_BYTES
                    )
                    downloaded.append(str(dest))
                    logger.info("Downloaded %s (%d bytes)", filename, written)
                except Exception as e:
                    logger.warning("Failed to download %s: %s", url, e)

        return downloaded
