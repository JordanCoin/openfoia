"""OpenCorporates adapter for company records.

Uses the free tier of the OpenCorporates API (no key needed for basic search).
API docs: https://api.opencorporates.com/documentation/API-Reference
"""

from __future__ import annotations

from typing import Any

from ..net import egress_client
from .base import AdapterRequestError, RecordAdapter, RecordEntity, SearchResult

API_BASE = "https://api.opencorporates.com/v0.4"


class OpenCorporatesAdapter(RecordAdapter):
    """Adapter for OpenCorporates company search."""

    source_name = "opencorporates"

    async def search(self, query: str, **kwargs: Any) -> SearchResult:
        """Search companies by name.

        Args:
            query: Company name to search for.
            jurisdiction: Optional jurisdiction code (e.g. "us_ca", "gb").
            page: Page number (default 1).

        Returns:
            SearchResult with company entities.
        """
        params: dict[str, Any] = {"q": query}

        jurisdiction = kwargs.get("jurisdiction")
        if jurisdiction:
            params["jurisdiction_code"] = jurisdiction

        page = kwargs.get("page", 1)
        params["page"] = page

        try:
            data = await self._request(f"{API_BASE}/companies/search", params=params)
        except AdapterRequestError as exc:
            # A failed lookup must not read as "this company does not exist".
            return self._failed(query, str(exc), page=page)

        results = data.get("results", {})
        companies = results.get("companies", [])
        total = results.get("total_count", 0)
        per_page = results.get("per_page", 25)

        entities: list[RecordEntity] = []
        for item in companies:
            company = item.get("company", {})
            entity = self._parse_company(company)
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
        """Fetch a specific company by jurisdiction and company number.

        Args:
            identifier: In format "jurisdiction_code/company_number"
                        e.g. "us_ca/C1234567" or "gb/12345678"

        Returns:
            RecordEntity if found, None otherwise.
        """
        async with egress_client(self._egress, timeout=15.0) as client:
            response = await client.get(f"{API_BASE}/companies/{identifier}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()

        company = data.get("results", {}).get("company", {})
        return self._parse_company(company)

    def _parse_company(self, company: dict[str, Any]) -> RecordEntity | None:
        """Parse a company JSON object into a RecordEntity."""
        name = company.get("name")
        if not name:
            return None

        jurisdiction = company.get("jurisdiction_code", "")
        company_number = company.get("company_number", "")
        status = company.get("current_status") or company.get("status", "")
        opencorporates_url = company.get("opencorporates_url", "")

        # Collect directors/officers if present
        officers = []
        for officer_item in company.get("officers", []) or []:
            officer = officer_item.get("officer", {})
            officer_name = officer.get("name")
            if officer_name:
                officers.append(
                    {
                        "name": officer_name,
                        "position": officer.get("position", ""),
                        "start_date": officer.get("start_date", ""),
                        "end_date": officer.get("end_date"),
                    }
                )

        # Collect addresses
        registered_address = company.get("registered_address") or {}
        address_str = ""
        if registered_address:
            parts = [
                registered_address.get("street_address", ""),
                registered_address.get("locality", ""),
                registered_address.get("region", ""),
                registered_address.get("postal_code", ""),
                registered_address.get("country", ""),
            ]
            address_str = ", ".join(p for p in parts if p)

        identifiers: dict[str, str] = {}
        if company_number:
            identifiers["company_number"] = company_number
        if jurisdiction:
            identifiers["jurisdiction"] = jurisdiction

        return RecordEntity(
            entity_type="ORGANIZATION",
            name=name,
            source=self.source_name,
            source_url=opencorporates_url or None,
            jurisdiction=jurisdiction or None,
            status=status or None,
            identifiers=identifiers,
            extra_data={
                "company_type": company.get("company_type", ""),
                "incorporation_date": company.get("incorporation_date", ""),
                "dissolution_date": company.get("dissolution_date"),
                "registered_address": address_str,
                "officers": officers,
                "industry_codes": company.get("industry_codes", []),
            },
        )
