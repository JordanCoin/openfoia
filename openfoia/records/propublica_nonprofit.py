"""ProPublica Nonprofit Explorer adapter — IRS 990 data on every US tax-exempt org.

Search 1.8M+ nonprofits. See revenue, expenses, assets, officer compensation,
and download original Form 990 filings. Data goes back to 2001.

Uses the Nonprofit Explorer API v2. No auth needed. GET only.
API docs: https://projects.propublica.org/nonprofits/api
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import RecordAdapter, RecordEntity, SearchResult

logger = logging.getLogger(__name__)

API_BASE = "https://projects.propublica.org/nonprofits/api/v2"


class ProPublicaNonprofitAdapter(RecordAdapter):
    """Search IRS 990 data for every US tax-exempt organization.

    Revenue, expenses, assets, liabilities, and links to original filings.
    Covers 1.8M+ organizations back to 2001.

    Usage::

        adapter = ProPublicaNonprofitAdapter()
        results = await adapter.search("Palantir Foundation")
        org = await adapter.fetch("142007220")  # by EIN
    """

    source_name = "propublica_nonprofit"

    async def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 25,
        **kwargs: Any,
    ) -> SearchResult:
        """Search nonprofits by name, city, or keyword."""
        params: dict[str, Any] = {
            "q": query,
            "page": page - 1,  # API is zero-indexed
        }
        state = kwargs.get("state")
        if state:
            params["state[id]"] = state.upper()

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{API_BASE}/search.json", params=params)
                if resp.status_code == 404:
                    # ProPublica returns 404 for some queries with special chars
                    return SearchResult(
                        source="propublica_nonprofit",
                        query=query,
                        total_results=0,
                        entities=[],
                        page=page,
                        per_page=page_size,
                    )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            msg = f"ProPublica Nonprofit search timed out for: {query}"
            logger.warning(msg)
            return SearchResult(
                source="propublica_nonprofit",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )
        except Exception as e:
            msg = f"ProPublica Nonprofit search failed: {e}"
            logger.warning(msg)
            return SearchResult(
                source="propublica_nonprofit",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )

        total = data.get("total_results", 0)
        entities = []

        for org in data.get("organizations", []):
            ein = str(org.get("ein", ""))
            name = org.get("name", "Unknown")
            city = org.get("city", "")
            state_code = org.get("state", "")
            location = f"{city}, {state_code}" if city else state_code

            entities.append(
                RecordEntity(
                    entity_type="NONPROFIT",
                    name=name,
                    source="propublica_nonprofit",
                    source_url=f"https://projects.propublica.org/nonprofits/organizations/{ein}",
                    jurisdiction=state_code or None,
                    identifiers={
                        "ein": ein,
                        "strein": org.get("strein", ""),
                    },
                    extra_data={
                        "address": org.get("address"),
                        "city": city,
                        "state": state_code,
                        "zipcode": org.get("zipcode"),
                        "subsection": _subsection_label(org.get("subseccd")),
                        "ntee_code": org.get("ntee_code"),
                        "location": location,
                    },
                )
            )

        return SearchResult(
            source="propublica_nonprofit",
            query=query,
            total_results=total,
            entities=entities,
            page=page,
            per_page=page_size,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Fetch detailed org data by EIN, including financial filings."""
        ein = identifier.replace("-", "")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{API_BASE}/organizations/{ein}.json")
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("ProPublica Nonprofit fetch failed for %s: %s", identifier, e)
            return None

        org = data.get("organization", {})
        filings = data.get("filings_with_data", [])

        # Get the most recent filing for financial summary
        latest = filings[0] if filings else {}
        revenue = latest.get("totrevenue")
        expenses = latest.get("totfuncexpns")
        assets = latest.get("totassetsend")
        liabilities = latest.get("totliabend")
        tax_year = latest.get("tax_prd_yr")
        pdf_url = latest.get("pdf_url")

        name = org.get("name", "Unknown")
        city = org.get("city", "")
        state_code = org.get("state", "")

        return RecordEntity(
            entity_type="NONPROFIT",
            name=name,
            source="propublica_nonprofit",
            source_url=f"https://projects.propublica.org/nonprofits/organizations/{ein}",
            jurisdiction=state_code or None,
            identifiers={
                "ein": ein,
                "strein": org.get("strein", ""),
            },
            extra_data={
                "address": org.get("address"),
                "city": city,
                "state": state_code,
                "zipcode": org.get("zipcode"),
                "subsection": _subsection_label(org.get("subseccd")),
                "ntee_code": org.get("ntee_code"),
                "tax_year": tax_year,
                "revenue": revenue,
                "expenses": expenses,
                "assets": assets,
                "liabilities": liabilities,
                "revenue_formatted": _format_dollars(revenue) if revenue else None,
                "expenses_formatted": _format_dollars(expenses) if expenses else None,
                "assets_formatted": _format_dollars(assets) if assets else None,
                "pdf_url": pdf_url,
                "filing_count": len(filings),
            },
        )


def _format_dollars(amount: float) -> str:
    if abs(amount) >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B"
    if abs(amount) >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if abs(amount) >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:,.0f}"


def _subsection_label(code: int | None) -> str:
    if code is None:
        return ""
    labels = {
        3: "501(c)(3)",
        4: "501(c)(4)",
        5: "501(c)(5)",
        6: "501(c)(6)",
        7: "501(c)(7)",
    }
    return labels.get(code, f"501(c)({code})")
