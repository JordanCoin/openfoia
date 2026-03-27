"""USAspending adapter — search federal contracts, grants, and loans.

USAspending tracks every dollar the federal government spends: contracts,
grants, loans, and other financial assistance. Data goes back to FY2001.
This is how you follow the money.

Uses the v2 REST API. No auth needed. No rate limit documented.
API docs: https://api.usaspending.gov/
GitHub:   https://github.com/fedspendingtransparency/usaspending-api
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import RecordAdapter, RecordEntity, SearchResult

logger = logging.getLogger(__name__)

API_BASE = "https://api.usaspending.gov/api/v2"

# Award type codes
CONTRACTS = ["A", "B", "C", "D"]
GRANTS = ["02", "03", "04", "05"]
DIRECT_PAYMENTS = ["06", "10"]
LOANS = ["07", "08"]
OTHER_ASSIST = ["09", "11"]
ALL_AWARDS = CONTRACTS + GRANTS + DIRECT_PAYMENTS + LOANS + OTHER_ASSIST


class USASpendingAdapter(RecordAdapter):
    """Search federal spending data — contracts, grants, and loans.

    Every federal dollar is tracked here. Search by keyword (searches award
    descriptions) or by recipient name (who got the money).

    Usage::

        adapter = USASpendingAdapter()
        results = await adapter.search("Palantir")
        recipient = await adapter.fetch("Palantir Technologies")
    """

    source_name = "usaspending"

    async def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 25,
        **kwargs: Any,
    ) -> SearchResult:
        """Search federal awards by keyword or recipient name.

        Keywords search award descriptions. Use award_type to filter:
        'contracts', 'grants', 'loans', or 'all' (default).
        """
        award_type = kwargs.get("award_type", "contracts")
        type_codes = {
            "contracts": CONTRACTS,
            "grants": GRANTS,
            "loans": LOANS,
        }.get(award_type, CONTRACTS)

        payload: dict[str, Any] = {
            "filters": {
                "keywords": [query],
                "award_type_codes": type_codes,
            },
            "fields": [
                "Award ID",
                "Recipient Name",
                "Award Amount",
                "Total Outlays",
                "Description",
                "Start Date",
                "End Date",
                "Awarding Agency",
                "Award Type",
                "recipient_id",
            ],
            "page": page,
            "limit": min(page_size, 100),
            "sort": "Award Amount",
            "order": "desc",
            "subawards": False,
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{API_BASE}/search/spending_by_award/",
                    json=payload,
                )
                if resp.status_code == 422:
                    msg = f"USAspending rejected query: {resp.text[:200]}"
                    logger.warning(msg)
                    return SearchResult(
                        source="usaspending",
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
            msg = f"USAspending search timed out for: {query}"
            logger.warning(msg)
            return SearchResult(
                source="usaspending",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )
        except Exception as e:
            msg = f"USAspending search failed: {e}"
            logger.warning(msg)
            return SearchResult(
                source="usaspending",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )

        results = data.get("results", [])
        page_meta = data.get("page_metadata", {})
        # USAspending doesn't return total count directly in spending_by_award
        # Estimate from hasNext
        has_next = page_meta.get("hasNext", False)
        estimated_total = page * page_size + (1 if has_next else 0)

        entities = []
        for award in results:
            amount = award.get("Award Amount") or 0
            outlays = award.get("Total Outlays") or 0
            award_id = award.get("Award ID", "")
            recipient = award.get("Recipient Name", "Unknown")

            entities.append(
                RecordEntity(
                    entity_type="AWARD",
                    name=recipient,
                    source="usaspending",
                    source_url=f"https://www.usaspending.gov/award/{award_id}"
                    if award_id
                    else None,
                    identifiers={
                        "award_id": str(award_id),
                        "recipient_id": award.get("recipient_id") or "",
                    },
                    extra_data={
                        "award_amount": amount,
                        "total_outlays": outlays,
                        "description": award.get("Description"),
                        "start_date": award.get("Start Date"),
                        "end_date": award.get("End Date"),
                        "awarding_agency": award.get("Awarding Agency"),
                        "award_type": award.get("Award Type") or award_type.title(),
                        "amount_formatted": _format_dollars(amount),
                    },
                )
            )

        return SearchResult(
            source="usaspending",
            query=query,
            total_results=estimated_total,
            entities=entities,
            page=page,
            per_page=page_size,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Search for a specific recipient by name and return their spending summary."""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{API_BASE}/recipient/",
                    json={
                        "keyword": identifier,
                        "limit": 10,
                        "page": 1,
                        "sort": "amount",
                        "order": "desc",
                    },
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
        except Exception as e:
            logger.warning("USAspending recipient lookup failed for %s: %s", identifier, e)
            return None

        results = data.get("results", [])
        if not results:
            return None

        # Return top match
        r = results[0]
        return RecordEntity(
            entity_type="RECIPIENT",
            name=r.get("name", identifier),
            source="usaspending",
            source_url=f"https://www.usaspending.gov/recipient/{r.get('id', '')}/latest"
            if r.get("id")
            else None,
            identifiers={
                "recipient_id": r.get("id") or "",
                "uei": r.get("uei") or "",
                "duns": r.get("duns") or "",
            },
            extra_data={
                "amount": r.get("amount"),
                "amount_formatted": _format_dollars(r.get("amount") or 0),
                "recipient_level": r.get("recipient_level"),
            },
        )


def _format_dollars(amount: float) -> str:
    """Format a dollar amount for display."""
    if abs(amount) >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B"
    if abs(amount) >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if abs(amount) >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:,.0f}"
