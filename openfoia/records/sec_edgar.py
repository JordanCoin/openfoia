"""SEC EDGAR adapter for public company filings.

Uses the EDGAR full-text search API (EFTS) which requires no API key.
https://efts.sec.gov/LATEST/search-index?q=<query>
"""

from __future__ import annotations

from typing import Any

from .base import AdapterRequestError, RecordAdapter, RecordEntity, SearchResult

EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"
EDGAR_FILING_BASE = "https://www.sec.gov/Archives/edgar/data"

#: SEC requires a descriptive User-Agent. Keep it generic and overridable —
#: announcing "OpenFOIA" tells a government endpoint that the requester is
#: using an adversarial-journalism tool.
DEFAULT_USER_AGENT = "Research Client/1.0 (contact: research@example.org)"


def _edgar_headers() -> dict[str, str]:
    """Headers for EDGAR requests, honouring OPENFOIA_SEC_USER_AGENT."""
    import os

    return {"User-Agent": os.environ.get("OPENFOIA_SEC_USER_AGENT", DEFAULT_USER_AGENT)}


class SECEdgarAdapter(RecordAdapter):
    """Adapter for SEC EDGAR full-text search."""

    source_name = "sec"

    async def search(self, query: str, **kwargs: Any) -> SearchResult:
        """Search SEC EDGAR filings by keyword.

        Args:
            query: Search term (company name, keyword, etc.)
            filing_type: Optional filing type filter (e.g. "10-K", "10-Q", "8-K").
            page: Page number starting from 1 (default 1).

        Returns:
            SearchResult with filing entities.
        """
        params: dict[str, Any] = {"q": query}

        filing_type = kwargs.get("filing_type")
        if filing_type:
            params["forms"] = filing_type

        page = kwargs.get("page", 1)
        per_page = 25
        params["from"] = (page - 1) * per_page
        params["size"] = per_page

        try:
            data = await self._request(EFTS_BASE, params=params, headers=_edgar_headers())
        except AdapterRequestError as exc:
            return self._failed(query, str(exc), page=page, per_page=per_page)

        hits = data.get("hits", {})
        total = (
            hits.get("total", {}).get("value", 0)
            if isinstance(hits.get("total"), dict)
            else hits.get("total", 0)
        )
        hit_list = hits.get("hits", [])

        entities: list[RecordEntity] = []
        for hit in hit_list:
            entity = self._parse_filing(hit)
            if entity:
                entities.append(entity)

        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=total,
            entities=entities,
            page=page,
            per_page=per_page,
            raw_response=data,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Fetch a specific filing by accession number.

        Args:
            identifier: SEC accession number (e.g. "0001193125-21-123456").

        Returns:
            RecordEntity if found, None otherwise.
        """
        # Search by accession number
        params = {"q": f'"{identifier}"', "size": 1}

        try:
            data = await self._request(EFTS_BASE, params=params, headers=_edgar_headers())
        except AdapterRequestError:
            return None

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            return None

        return self._parse_filing(hits[0])

    def _parse_filing(self, hit: dict[str, Any]) -> RecordEntity | None:
        """Parse an EFTS hit into a RecordEntity."""
        source = hit.get("_source", {})

        display_names = source.get("display_names", [])
        company_name = display_names[0] if display_names else source.get("entity_name", "")

        if not company_name:
            return None

        form_type = source.get("form_type", source.get("file_type", ""))
        filing_date = source.get("file_date", source.get("period_of_report", ""))
        file_number = source.get("file_num", "")

        # Build filing URL
        accession = source.get("accession_no", "")
        cik = source.get("entity_id", "")
        filing_url = ""
        if accession and cik:
            clean_accession = accession.replace("-", "")
            filing_url = f"{EDGAR_FILING_BASE}/{cik}/{clean_accession}/{accession}-index.htm"

        identifiers: dict[str, str] = {}
        if accession:
            identifiers["accession_number"] = accession
        if cik:
            identifiers["cik"] = str(cik)
        if file_number:
            identifiers["file_number"] = file_number

        return RecordEntity(
            entity_type="ORGANIZATION",
            name=company_name,
            source=self.source_name,
            source_url=filing_url or None,
            jurisdiction="us_federal",
            status=None,
            identifiers=identifiers,
            extra_data={
                "filing_type": form_type,
                "filing_date": filing_date,
                "display_names": display_names,
            },
        )
