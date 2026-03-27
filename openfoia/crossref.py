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

# Minimum confidence to crossref (skip low-confidence regex guesses)
_MIN_CONFIDENCE = 0.5

# Rate limits per source (seconds between requests) — based on documented API limits
_SOURCE_DELAYS: dict[str, float] = {
    "muckrock": 1.0,  # 1 req/sec average, 20 burst
    "documentcloud": 0.5,  # 10 req/sec, but 500/day anonymous
    "opencorporates": 1.0,  # ~1 req/sec free tier
    "sec": 0.5,  # 10 req/sec with User-Agent
    "usaspending": 0.2,  # no documented limit
    "nonprofits": 0.5,  # not documented, be conservative
    "govinfo": 1.0,  # DEMO_KEY shared pool — be conservative
    "fec": 1.0,  # DEMO_KEY shared pool — be conservative
    "regulations": 1.0,  # DEMO_KEY shared pool — be conservative
    "opensanctions": 0.5,  # ~5 req/sec free
    "icij": 0.0,  # local CSV, no API
}
_DEFAULT_DELAY = 0.5


class _RateLimited(BaseException):
    """Raised when a source hits its rate limit.

    Inherits from BaseException (not Exception) so generic except clauses
    in individual checkers don't swallow it — the crossref loop catches it.
    """

    pass


def _check_rate_limit(result: Any) -> None:
    """Raise _RateLimited if the search result indicates a rate limit error."""
    err = getattr(result, "error", None)
    if err and ("rate limit" in err.lower() or "429" in err.lower()):
        raise _RateLimited(err)


def _deduplicate_entities(entities: list[Any]) -> list[Any]:
    """Deduplicate entities by normalized name (case-insensitive).

    Keeps the highest-confidence version of each name.
    Also merges near-duplicates like 'Clearview AI' and 'Clearview Ai Inc.'
    """
    seen: dict[str, Any] = {}
    for e in entities:
        key = e.normalized_text.lower().strip().rstrip(".")
        # Strip common suffixes for dedup: Inc, LLC, Corp, Ltd
        base_key = key
        for suffix in [" inc", " llc", " corp", " ltd", " co", " lp"]:
            if base_key.endswith(suffix):
                base_key = base_key[: -len(suffix)].rstrip(",. ")
                break

        # Use base_key for dedup, but keep the full name
        if base_key not in seen or e.confidence > seen[base_key].confidence:
            seen[base_key] = e

    return list(seen.values())


async def crossref_entities(
    entities: list[Any],
    sources: list[str] | None = None,
    icij_data_dir: str | None = None,
    on_progress: Any | None = None,
) -> CrossRefReport:
    """Cross-reference extracted entities against all available sources.

    Filters to PERSON and ORGANIZATION types, deduplicates by name,
    skips low-confidence entities, and rate-limits API calls.

    Args:
        entities: ExtractedEntity objects from the extraction pipeline
        sources: List of sources to check (default: all available)
        icij_data_dir: Path to downloaded ICIJ CSV data (for offline search)

    Returns:
        CrossRefReport with all hits
    """
    import asyncio

    # Filter to cross-referable entity types and confidence threshold
    targets = [
        e for e in entities if e.entity_type in _CROSSREF_TYPES and e.confidence >= _MIN_CONFIDENCE
    ]

    # Deduplicate — "Clearview AI" and "Clearview Ai Inc." become one lookup
    targets = _deduplicate_entities(targets)

    available_sources = _get_available_sources(icij_data_dir)
    if sources:
        available_sources = {k: v for k, v in available_sources.items() if k in sources}

    if on_progress:
        on_progress(
            "start",
            f"Checking {len(targets)} entities (filtered from {len(entities)}, "
            f"deduped, ≥{_MIN_CONFIDENCE:.0%} confidence) across {len(available_sources)} sources",
        )

    results: list[CrossRefResult] = []
    # Track sources that hit rate limits — skip them for remaining entities
    exhausted_sources: set[str] = set()

    for idx, entity in enumerate(targets):
        hits: list[CrossRefHit] = []
        sources_checked: list[str] = []

        if on_progress:
            on_progress(
                "entity",
                f"[{idx + 1}/{len(targets)}] {entity.normalized_text}",
            )

        for source_name, checker in available_sources.items():
            if source_name in exhausted_sources:
                continue
            sources_checked.append(source_name)
            try:
                source_hits = await checker(entity.normalized_text, entity.entity_type)
                hits.extend(source_hits)
            except _RateLimited:
                logger.warning(
                    "CrossRef %s rate limited — skipping for remaining entities",
                    source_name,
                )
                exhausted_sources.add(source_name)
            except Exception as e:
                logger.warning(
                    "CrossRef %s failed for '%s': %s",
                    source_name,
                    entity.normalized_text,
                    e,
                )

            # Rate limit: respect each API's documented limits
            await asyncio.sleep(_SOURCE_DELAYS.get(source_name, _DEFAULT_DELAY))

        results.append(
            CrossRefResult(
                entity_name=entity.normalized_text,
                entity_type=entity.entity_type.value,
                hits=hits,
                sources_checked=sources_checked,
            )
        )

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

    # DocumentCloud — always available (public API, no key)
    sources["documentcloud"] = _check_documentcloud

    # USAspending — always available (no key, no rate limit)
    sources["usaspending"] = _check_usaspending

    # ProPublica Nonprofit — always available (no key)
    sources["nonprofits"] = _check_nonprofits

    # GovInfo — court opinions, congressional reports, Federal Register (DEMO_KEY)
    sources["govinfo"] = _check_govinfo

    # FEC — campaign finance contributions (DEMO_KEY)
    sources["fec"] = _check_fec

    # Regulations.gov — federal rulemaking documents (DEMO_KEY)
    sources["regulations"] = _check_regulations

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
        _check_rate_limit(result)
    except Exception:
        return []

    hits = []
    for req in result.entities:
        # Only count if the name actually appears in the title
        if name.lower() in req.name.lower():
            hits.append(
                CrossRefHit(
                    source="muckrock",
                    entity_name=name,
                    match_type="partial",
                    details=f"FOIA request: {req.name} (by {req.extra_data.get('username', '?')})",
                    url=req.source_url,
                    extra={"muckrock_id": req.identifiers.get("muckrock_id")},
                )
            )

    # Only report fuzzy matches if the result count is meaningfully small
    # (a broad search returning 46k results is not useful)
    if not hits and 0 < result.total_results <= 100:
        hits.append(
            CrossRefHit(
                source="muckrock",
                entity_name=name,
                match_type="fuzzy",
                details=f"{result.total_results} FOIA requests in related search",
                url=f"https://www.muckrock.com/foi/list/?q={name.replace(' ', '+')}",
            )
        )

    return hits


async def _check_opencorporates(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search OpenCorporates for company registrations."""
    if entity_type == EntityType.PERSON:
        return []  # OpenCorporates is for companies

    from .records.opencorporates import OpenCorporatesAdapter

    adapter = OpenCorporatesAdapter()
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except Exception:
        return []

    hits = []
    for ent in result.entities:
        name_lower = name.lower()
        ent_lower = ent.name.lower()
        if name_lower in ent_lower or ent_lower in name_lower:
            officers = ent.extra_data.get("officers", [])
            officer_names = (
                ", ".join(o["name"] for o in officers[:3]) if officers else "none listed"
            )
            hits.append(
                CrossRefHit(
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
                )
            )

    return hits


async def _check_sec(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search SEC EDGAR for filings."""
    if entity_type == EntityType.PERSON:
        return []  # SEC is mostly company filings

    from .records.sec_edgar import SECEdgarAdapter

    adapter = SECEdgarAdapter()
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
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
        hits.append(
            CrossRefHit(
                source="sec",
                entity_name=name,
                match_type="partial",
                details=f"{ent.name} (CIK {cik}) — {ent.extra_data.get('filing_type', '?')} ({ent.extra_data.get('filing_date', '?')})",
                url=ent.source_url,
                extra={"cik": cik},
            )
        )

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
                            hits.append(
                                CrossRefHit(
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
                                )
                            )
                            break  # one hit per row is enough
        except Exception as e:
            logger.warning("Failed to search ICIJ file %s: %s", csv_file, e)

    return hits[:10]  # cap to avoid flooding


async def _check_fec(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search FEC for campaign finance contributions involving this entity."""
    from .records.fec import FECAdapter

    adapter = FECAdapter()
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except Exception:
        return []

    hits = []
    for contrib in result.entities:
        amount = contrib.extra_data.get("amount_formatted", "")
        committee = contrib.extra_data.get("committee_name", "")
        detail = f"{amount} to {committee}" if committee else amount

        hits.append(
            CrossRefHit(
                source="fec",
                entity_name=name,
                match_type="partial",
                details=detail,
                url=contrib.source_url,
                extra={"amount": contrib.extra_data.get("amount")},
            )
        )

    return hits[:5]


async def _check_regulations(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search Regulations.gov for federal rulemaking mentioning this entity."""
    from .records.regulations import RegulationsGovAdapter

    adapter = RegulationsGovAdapter()
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except Exception:
        return []

    hits = []
    for doc in result.entities:
        if name.lower() not in doc.name.lower():
            continue
        agency = doc.extra_data.get("agency", "")
        doc_type = doc.extra_data.get("document_type", "")
        detail = f"{agency} {doc_type}: {doc.name[:60]}"

        hits.append(
            CrossRefHit(
                source="regulations",
                entity_name=name,
                match_type="partial",
                details=detail,
                url=doc.source_url,
                extra={"document_id": doc.identifiers.get("document_id")},
            )
        )

    return hits


async def _check_govinfo(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search GovInfo for court opinions, congressional reports, and federal rules."""
    from .records.govinfo import GovInfoAdapter

    adapter = GovInfoAdapter()
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except Exception:
        return []

    hits = []
    for doc in result.entities:
        if name.lower() not in doc.name.lower():
            continue
        collection = doc.extra_data.get("collection", "")
        author = doc.extra_data.get("government_author", "")
        detail = f"{collection}: {doc.name[:80]}"
        if author:
            detail += f" — {author}"

        hits.append(
            CrossRefHit(
                source="govinfo",
                entity_name=name,
                match_type="partial",
                details=detail,
                url=doc.source_url,
                extra={"package_id": doc.identifiers.get("package_id")},
            )
        )

    return hits


async def _check_nonprofits(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search ProPublica for nonprofit organizations matching this entity."""
    if entity_type == EntityType.PERSON:
        return []

    from .records.propublica_nonprofit import ProPublicaNonprofitAdapter

    adapter = ProPublicaNonprofitAdapter()
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except Exception:
        return []

    hits = []
    for org in result.entities:
        if name.lower() not in org.name.lower():
            continue
        detail = f"Nonprofit: {org.name}"
        subsection = org.extra_data.get("subsection")
        if subsection:
            detail += f" ({subsection})"
        location = org.extra_data.get("location")
        if location:
            detail += f" — {location}"

        hits.append(
            CrossRefHit(
                source="nonprofits",
                entity_name=name,
                match_type="partial",
                details=detail,
                url=org.source_url,
                extra={"ein": org.identifiers.get("ein")},
            )
        )

    return hits


async def _check_usaspending(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search USAspending for federal contracts and grants involving this entity."""
    if entity_type == EntityType.PERSON:
        return []  # USAspending tracks organizations, not individuals

    from .records.usaspending import USASpendingAdapter

    adapter = USASpendingAdapter()
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except Exception:
        return []

    hits = []
    for award in result.entities:
        # Only count if name appears in recipient
        if name.lower() not in award.name.lower():
            continue

        amount = award.extra_data.get("amount_formatted", "")
        agency = award.extra_data.get("awarding_agency", "")
        award_type = award.extra_data.get("award_type", "")
        detail = f"{award_type}: {amount} from {agency}" if agency else f"{award_type}: {amount}"

        hits.append(
            CrossRefHit(
                source="usaspending",
                entity_name=name,
                match_type="partial",
                details=detail,
                url=award.source_url,
                extra={
                    "award_id": award.identifiers.get("award_id"),
                    "amount": award.extra_data.get("award_amount"),
                },
            )
        )

    return hits


async def _check_documentcloud(name: str, entity_type: EntityType) -> list[CrossRefHit]:
    """Search DocumentCloud's 10M+ public document archive."""
    from .records.documentcloud import DocumentCloudAdapter

    adapter = DocumentCloudAdapter()
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except Exception:
        return []

    hits = []
    for doc in result.entities:
        # Only count if the name actually appears in the title or highlights
        title_match = name.lower() in doc.name.lower()
        highlights = doc.extra_data.get("highlights", [])
        highlight_match = any(name.lower() in h.lower() for h in highlights)

        if title_match or highlight_match:
            detail = f"Document: {doc.name}"
            pages = doc.extra_data.get("pages")
            if pages:
                detail += f" ({pages} pages)"
            source_org = doc.extra_data.get("source")
            if source_org:
                detail += f" — {source_org}"

            hits.append(
                CrossRefHit(
                    source="documentcloud",
                    entity_name=name,
                    match_type="partial",
                    details=detail,
                    url=doc.source_url,
                    extra={"documentcloud_id": doc.identifiers.get("documentcloud_id")},
                )
            )

    return hits


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

        hits.append(
            CrossRefHit(
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
            )
        )

    return hits
