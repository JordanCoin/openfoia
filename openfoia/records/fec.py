"""FEC OpenFEC adapter — campaign finance data for every US federal election.

Who donated to whom. PAC spending. Candidate financials. Contribution
records going back decades. This is how you follow the political money.

Uses the OpenFEC API v1. DEMO_KEY works, free key at api.data.gov for higher limits.
API docs: https://api.open.fec.gov/developers/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..net import EgressPolicy, egress_client
from .base import RecordAdapter, RecordEntity, SearchResult

logger = logging.getLogger(__name__)

API_BASE = "https://api.open.fec.gov/v1"


class FECAdapter(RecordAdapter):
    """Search FEC campaign finance data — candidates, committees, contributions.

    Usage::

        adapter = FECAdapter()
        results = await adapter.search("Palantir")  # contribution search
        candidate = await adapter.fetch("Biden")     # candidate search
    """

    source_name = "fec"

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
        """Search FEC contributions by contributor name.

        Returns individual and PAC contributions sorted by amount.
        """
        params: dict[str, Any] = {
            "contributor_name": query,
            "per_page": min(page_size, 100),
            "page": page,
            "sort": "-contribution_receipt_amount",
            "api_key": self._api_key,
        }

        try:
            async with egress_client(self._egress, timeout=20) as client:
                resp = await client.get(
                    f"{API_BASE}/schedules/schedule_a/",
                    params=params,
                )
                if resp.status_code == 429:
                    msg = "FEC rate limit hit. Get a free key at https://api.data.gov/signup/"
                    logger.warning(msg)
                    return SearchResult(
                        source="fec",
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
            msg = f"FEC search timed out for: {query}"
            logger.warning(msg)
            return SearchResult(
                source="fec",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )
        except Exception as e:
            msg = f"FEC search failed: {e}"
            logger.warning(msg)
            return SearchResult(
                source="fec",
                query=query,
                total_results=0,
                entities=[],
                page=page,
                per_page=page_size,
                error=msg,
            )

        pagination = data.get("pagination", {})
        total = pagination.get("count", 0)

        entities = []
        for contrib in data.get("results", []):
            amount = contrib.get("contribution_receipt_amount") or 0
            contributor = contrib.get("contributor_name", "Unknown")
            committee = contrib.get("committee", {})
            committee_name = committee.get("name", "Unknown")
            date = contrib.get("contribution_receipt_date")

            entities.append(
                RecordEntity(
                    entity_type="CONTRIBUTION",
                    name=contributor,
                    source="fec",
                    source_url=f"https://www.fec.gov/data/receipts/?contributor_name={query}",
                    identifiers={
                        "committee_id": committee.get("committee_id") or "",
                        "transaction_id": contrib.get("transaction_id") or "",
                    },
                    extra_data={
                        "amount": amount,
                        "amount_formatted": _format_dollars(amount),
                        "committee_name": committee_name,
                        "date": date,
                        "contributor_employer": contrib.get("contributor_employer"),
                        "contributor_occupation": contrib.get("contributor_occupation"),
                        "contributor_city": contrib.get("contributor_city"),
                        "contributor_state": contrib.get("contributor_state"),
                    },
                )
            )

        return SearchResult(
            source="fec",
            query=query,
            total_results=total,
            entities=entities,
            page=page,
            per_page=page_size,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Search for a candidate by name."""
        try:
            async with egress_client(self._egress, timeout=15) as client:
                resp = await client.get(
                    f"{API_BASE}/candidates/search/",
                    params={
                        "q": identifier,
                        "per_page": 5,
                        "sort": "-election_years",
                        "api_key": self._api_key,
                    },
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
        except Exception as e:
            logger.warning("FEC candidate search failed for %s: %s", identifier, e)
            return None

        results = data.get("results", [])
        if not results:
            return None

        c = results[0]
        return RecordEntity(
            entity_type="CANDIDATE",
            name=c.get("name", identifier),
            source="fec",
            source_url=f"https://www.fec.gov/data/candidate/{c.get('candidate_id', '')}/",
            identifiers={
                "candidate_id": c.get("candidate_id") or "",
            },
            extra_data={
                "office": c.get("office_full"),
                "party": c.get("party_full"),
                "state": c.get("state"),
                "district": c.get("district"),
                "election_years": c.get("election_years"),
                "active": c.get("active_through"),
            },
        )


def _format_dollars(amount: float) -> str:
    if abs(amount) >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if abs(amount) >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:,.0f}"
