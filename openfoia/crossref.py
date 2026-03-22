"""Cross-reference engine.

Takes extracted entities and checks them against every available source:
- MuckRock (other FOIA requests mentioning this person/org)
- OpenCorporates (company registrations, directors)
- SEC EDGAR (public company filings)
- ICIJ Offshore Leaks (Panama/Pandora/Paradise Papers — local CSV)
- OpenSanctions (sanctions lists, PEPs — local or API)

This is the free, local, offline-capable version of what Maltego charges
$999/year for.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .models import EntityType

logger = logging.getLogger(__name__)


@dataclass
class CrossRefHit:
    """A single match from a cross-reference source."""
    source: str
    entity_name: str
    match_type: str  # "exact", "partial", "fuzzy"
    details: str
    url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CrossRefResult:
    """Cross-reference results for a single entity."""
    entity_name: str
    entity_type: str
    hits: list[CrossRefHit]
    sources_checked: list[str]

    @property
    def flagged(self) -> bool:
        return len(self.hits) > 0


@dataclass
class CrossRefReport:
    """Full cross-reference report for all entities."""
    results: list[CrossRefResult]
    total_entities: int
    total_hits: int
    total_flagged: int
    sources_used: list[str]


# Entity types worth cross-referencing (skip dates, money, etc.)
_CROSSREF_TYPES = {EntityType.PERSON, EntityType.ORGANIZATION}


async def crossref_entities(
    entities: list[Any],
    sources: list[str] | None = None,
    icij_data_dir: str | None = None,
) -> CrossRefReport:
    """Cross-reference extracted entities against all available sources.

    Args:
        entities: ExtractedEntity objects from the extraction pipeline
        sources: List of sources to check (default: all available)
        icij_data_dir: Path to downloaded ICIJ CSV data (for offline search)

    Returns:
        CrossRefReport with all hits
    """
    # Filter to cross-referable entity types
    targets = [e for e in entities if e.entity_type in _CROSSREF_TYPES]

    available_sources = _get_available_sources(icij_data_dir)
    if sources:
        available_sources = {k: v for k, v in available_sources.items() if k in sources}

    results: list[CrossRefResult] = []

    for entity in targets:
        hits: list[CrossRefHit] = []
        sources_checked: list[str] = []

        for source_name, checker in available_sources.items():
            sources_checked.append(source_name)
            try:
                source_hits = await checker(entity.normalized_text, entity.entity_type)
                hits.extend(source_hits)
            except Exception as e:
                logger.warning("CrossRef %s failed for '%s': %s", source_name, entity.normalized_text, e)

        results.append(CrossRefResult(
            entity_name=entity.normalized_text,
            entity_type=entity.entity_type.value,
            hits=hits,
            sources_checked=sources_checked,
        ))

    flagged = [r for r in results if r.flagged]
    total_hits = sum(len(r.hits) for r in results)

    return CrossRefReport(
        results=results,
        total_entities=len(targets),
        total_hits=total_hits,
        total_flagged=len(flagged),
        sources_used=list(available_sources.keys()),
    )


def _get_available_sources(icij_data_dir: str | None = None) -> dict[str, Any]:
    """Discover which cross-reference sources are available."""
    sources: dict[str, Any] = {}

    # MuckRock — always available (public API, no key)
    sources["muckrock"] = _check_muckrock

    # OpenCorporates — always available (free tier)
    sources["opencorporates"] = _check_opencorporates

    # SEC EDGAR — always available (free)
    sources["sec"] = _check_sec

    # ICIJ Offshore Leaks — available if CSVs downloaded locally
    if icij_data_dir:
        from pathlib import Path
        if Path(icij_data_dir).exists():
            sources["icij"] = lambda name, etype: _check_icij(name, etype, icij_data_dir)

    # OpenSanctions — available if data downloaded or API key set
    sources["opensanctions"] = _check_opensanctions

    return sources


# ---------------------------------------------------------------------------
# Source checkers
# ---------------------------------------------------------------------------

async def _check_muckrock(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search MuckRock for FOIA requests mentioning this entity."""
    from .records.muckrock import MuckRockAdapter

    adapter = MuckRockAdapter()
    try:
        result = await adapter.search(name, page_size=5)
    except Exception:
        return []

    hits = []
    for req in result.entities:
        # Only count if the name actually appears in the title
        if name.lower() in req.name.lower():
            hits.append(CrossRefHit(
                source="muckrock",
                entity_name=name,
                match_type="partial",
                details=f"FOIA request: {req.name} (by {req.extra_data.get('username', '?')})",
                url=req.source_url,
                extra={"muckrock_id": req.identifiers.get("muckrock_id")},
            ))

    # Only report fuzzy matches if the result count is meaningfully small
    # (a broad search returning 46k results is not useful)
    if not hits and 0 < result.total_results <= 100:
        hits.append(CrossRefHit(
            source="muckrock",
            entity_name=name,
            match_type="fuzzy",
            details=f"{result.total_results} FOIA requests in related search",
            url=f"https://www.muckrock.com/foi/list/?q={name.replace(' ', '+')}",
        ))

    return hits


async def _check_opencorporates(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search OpenCorporates for company registrations."""
    if entity_type == EntityType.PERSON:
        return []  # OpenCorporates is for companies

    from .records.opencorporates import OpenCorporatesAdapter

    adapter = OpenCorporatesAdapter()
    try:
        result = await adapter.search(name, page_size=5)
    except Exception:
        return []

    hits = []
    for ent in result.entities:
        name_lower = name.lower()
        ent_lower = ent.name.lower()
        if name_lower in ent_lower or ent_lower in name_lower:
            officers = ent.extra_data.get("officers", [])
            officer_names = ", ".join(o["name"] for o in officers[:3]) if officers else "none listed"
            hits.append(CrossRefHit(
                source="opencorporates",
                entity_name=name,
                match_type="exact" if name_lower == ent_lower else "partial",
                details=f"{ent.name} ({ent.jurisdiction or '?'}) — officers: {officer_names}",
                url=ent.source_url,
                extra={
                    "company_number": ent.identifiers.get("company_number"),
                    "jurisdiction": ent.jurisdiction,
                    "status": ent.status,
                },
            ))

    return hits


async def _check_sec(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search SEC EDGAR for filings."""
    if entity_type == EntityType.PERSON:
        return []  # SEC is mostly company filings

    from .records.sec_edgar import SECEdgarAdapter

    adapter = SECEdgarAdapter()
    try:
        result = await adapter.search(name, page_size=5)
    except Exception:
        return []

    hits = []
    seen_ciks: set[str] = set()
    for ent in result.entities:
        cik = ent.identifiers.get("cik", "")
        # One hit per company (CIK), not per filing
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        hits.append(CrossRefHit(
            source="sec",
            entity_name=name,
            match_type="partial",
            details=f"{ent.name} (CIK {cik}) — {ent.extra_data.get('filing_type', '?')} ({ent.extra_data.get('filing_date', '?')})",
            url=ent.source_url,
            extra={"cik": cik},
        ))

    return hits[:5]  # cap at 5 most relevant


async def _check_icij(name: str, entity_type: EntityType, data_dir: str) -> list[CrossRefHit]:
    """Search local ICIJ Offshore Leaks CSV data.

    Download from: https://offshoreleaks.icij.org/pages/database
    Extract to a directory and pass as icij_data_dir.
    """
    import csv
    from pathlib import Path

    hits = []
    data_path = Path(data_dir)
    name_lower = name.lower()

    # Search across all ICIJ CSV files
    for csv_file in data_path.glob("*.csv"):
        try:
            with open(csv_file, encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Check common name fields
                    for field_name in ["name", "entity_name", "officer_name", "intermediary_name"]:
                        val = row.get(field_name, "")
                        if val and name_lower in val.lower():
                            hits.append(CrossRefHit(
                                source="icij",
                                entity_name=name,
                                match_type="partial",
                                details=f"ICIJ {csv_file.stem}: {val}",
                                url=f"https://offshoreleaks.icij.org/search?q={name.replace(' ', '+')}",
                                extra={
                                    "jurisdiction": row.get("jurisdiction", ""),
                                    "source": csv_file.stem,
                                    "node_id": row.get("node_id", ""),
                                },
                            ))
                            break  # one hit per row is enough
        except Exception as e:
            logger.warning("Failed to search ICIJ file %s: %s", csv_file, e)

    return hits[:10]  # cap to avoid flooding


async def _check_opensanctions(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search OpenSanctions for sanctions/PEP matches.

    Uses the free API (rate limited, non-commercial use).
    """
    import httpx

    hits = []

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.opensanctions.org/search/default",
                params={"q": name, "limit": 5},
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        return []

    for result in data.get("results", []):
        score = result.get("score", 0)
        if score < 0.5:
            continue

        schema = result.get("schema", "")
        properties = result.get("properties", {})
        countries = properties.get("country", [])
        topics = result.get("datasets", [])

        is_pep = any("pep" in t.lower() for t in topics)
        is_sanctioned = any("sanction" in t.lower() for t in topics)

        detail_parts = []
        if is_pep:
            detail_parts.append("PEP (Politically Exposed Person)")
        if is_sanctioned:
            detail_parts.append("SANCTIONED")
        if countries:
            detail_parts.append(f"Countries: {', '.join(countries[:3])}")
        if not detail_parts:
            detail_parts.append(schema)

        result_name = properties.get("name", [name])[0] if properties.get("name") else name

        hits.append(CrossRefHit(
            source="opensanctions",
            entity_name=name,
            match_type="exact" if score > 0.9 else "fuzzy",
            details=f"{result_name} — {'; '.join(detail_parts)}",
            url=f"https://opensanctions.org/search/?q={name.replace(' ', '+')}",
            extra={
                "score": score,
                "is_pep": is_pep,
                "is_sanctioned": is_sanctioned,
                "datasets": topics,
            },
        ))

    return hits
