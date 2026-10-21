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
import random
import re
from dataclasses import dataclass, field
from typing import Any

from .models import EntityType
from .net import EgressPolicy, egress_client

logger = logging.getLogger(__name__)


@dataclass
class CrossRefHit:
    """A single match from a cross-reference source."""

    source: str
    entity_name: str
    match_type: str  # "exact", "partial", "fuzzy", "alias"
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
    source_statuses: dict[str, str] = field(default_factory=dict)

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
    source_errors: dict[str, str] = field(default_factory=dict)


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
    "opensanctions-local": 0.0,  # local CSV, no API
}
_DEFAULT_DELAY = 0.5

OPENSANCTIONS_KEY_ENV = "OPENFOIA_OPENSANCTIONS_API_KEY"
OPENSANCTIONS_FILE = "targets.simple.csv"


class _RateLimited(BaseException):
    """Raised when a source hits its rate limit.

    Inherits from BaseException (not Exception) so generic except clauses
    in individual checkers don't swallow it — the crossref loop catches it.
    """


class _SourceCheckError(RuntimeError):
    """A checker failure that must be visible in the cross-reference report.

    Carries any hits the checker had already collected before it failed, so a
    partial failure reports the source as incomplete *without* discarding
    matches the user would otherwise never see.
    """

    def __init__(
        self,
        error: BaseException,
        partial_hits: list[CrossRefHit] | None = None,
        error_type: str | None = None,
        stop_source: str | None = None,
    ):
        self.error_type = error_type or type(error).__name__
        self.partial_hits = partial_hits or []
        # Status for later entities when the source must not be asked again.
        self.stop_source = stop_source
        super().__init__(self.error_type)


def _error_kind(message: str) -> str:
    """Name the failure behind an adapter error string, without quoting it.

    Adapters report failures as ``"<ExceptionName>: <detail>"``. The detail can
    echo the URL — and therefore the entity name being looked up — so only the
    leading exception name is carried into a report the user may share.
    """
    head = message.split(":", 1)[0].strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", head):
        return head
    return "SourceError"


def _check_rate_limit(result: Any) -> None:
    """Raise if the search result reports a rate limit or any other failure."""
    err = getattr(result, "error", None)
    if err:
        if "rate limit" in err.lower() or "429" in err.lower():
            raise _RateLimited(err)
        raise _SourceCheckError(RuntimeError(err), error_type=_error_kind(err))


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


#: Sources that run entirely against local data — safe with no network.
OFFLINE_SOURCES = frozenset({"icij", "opensanctions-local"})


async def crossref_entities(
    entities: list[Any],
    sources: list[str] | None = None,
    icij_data_dir: str | None = None,
    on_progress: Any | None = None,
    allow_network: bool = False,
    egress: EgressPolicy | None = None,
    opensanctions_data_dir: str | None = None,
) -> CrossRefReport:
    """Cross-reference extracted entities against all available sources.

    Filters to PERSON and ORGANIZATION types, deduplicates by name,
    skips low-confidence entities, and rate-limits API calls.

    Args:
        entities: ExtractedEntity objects from the extraction pipeline
        sources: List of sources to check (default: all available)
        icij_data_dir: Path to downloaded ICIJ CSV data (for offline search)
        opensanctions_data_dir: Directory holding OpenSanctions targets.simple.csv
            (for offline search)
        allow_network: Must be explicitly True to contact remote sources.
            Cross-referencing sends the *names of the people and organizations
            under investigation* to third-party APIs, which is the single most
            sensitive thing this toolkit holds. The gate lives here rather than
            in the CLI so that agent, server and script callers cannot reach
            the network without the same explicit opt-in (Principle 5).
        egress: How to route the network calls (DIRECT/TOR). Orthogonal to
            allow_network — allow_network gates WHETHER to contact remote
            sources at all; egress governs HOW (direct vs Tor). Defaults to
            a plain DIRECT policy.

    Returns:
        CrossRefReport with all hits

    Raises:
        PermissionError: if remote sources are requested without opting in.
    """
    import asyncio

    available_sources = _get_available_sources(icij_data_dir, egress, opensanctions_data_dir)
    if sources:
        available_sources = {k: v for k, v in available_sources.items() if k in sources}

    # Check permission BEFORE touching the entities, so the refusal is about
    # consent and cannot be reached only on some input shapes.
    if not allow_network:
        remote = sorted(set(available_sources) - OFFLINE_SOURCES)
        if remote:
            raise PermissionError(
                "Cross-referencing would send entity names over the network to: "
                f"{', '.join(remote)}. Pass allow_network=True to opt in "
                "(the CLI does this after showing its warning), or restrict to "
                f"offline sources: {', '.join(sorted(OFFLINE_SOURCES))}."
            )

    # Filter to cross-referable entity types and confidence threshold
    targets = [
        e for e in entities if e.entity_type in _CROSSREF_TYPES and e.confidence >= _MIN_CONFIDENCE
    ]

    # Deduplicate — "Clearview AI" and "Clearview Ai Inc." become one lookup
    targets = _deduplicate_entities(targets)

    if on_progress:
        on_progress(
            "start",
            f"Checking {len(targets)} entities (filtered from {len(entities)}, "
            f"deduped, ≥{_MIN_CONFIDENCE:.0%} confidence) across {len(available_sources)} sources",
        )

    results: list[CrossRefResult] = []
    source_errors: dict[str, str] = {}
    # Track sources that hit rate limits — skip them for remaining entities
    exhausted_sources: dict[str, str] = {}

    for idx, entity in enumerate(targets):
        hits: list[CrossRefHit] = []
        sources_checked: list[str] = []
        source_statuses: dict[str, str] = {}

        if on_progress:
            on_progress(
                "entity",
                f"[{idx + 1}/{len(targets)}] {entity.normalized_text}",
            )

        for source_name, checker in available_sources.items():
            if source_name in exhausted_sources:
                source_statuses[source_name] = exhausted_sources[source_name]
                continue
            sources_checked.append(source_name)
            try:
                source_hits = await checker(entity.normalized_text, entity.entity_type)
                hits.extend(source_hits)
                source_statuses[source_name] = "matched" if source_hits else "checked"
            except _RateLimited:
                logger.warning(
                    "CrossRef %s rate limited — skipping for remaining entities",
                    source_name,
                )
                exhausted_sources[source_name] = "skipped(rate-limited)"
                source_statuses[source_name] = "ERRORED(RateLimited)"
                source_errors.setdefault(source_name, "RateLimited")
            except _SourceCheckError as exc:
                logger.warning(
                    "CrossRef %s failed: %s",
                    source_name,
                    exc.error_type,
                )
                # A partially failed source still keeps whatever it did find:
                # the status marks it incomplete, but real matches survive.
                hits.extend(exc.partial_hits)
                if exc.stop_source:
                    exhausted_sources[source_name] = exc.stop_source
                source_statuses[source_name] = f"ERRORED({exc.error_type})"
                source_errors.setdefault(source_name, exc.error_type)
            except Exception as e:
                logger.warning(
                    "CrossRef %s failed: %s",
                    source_name,
                    type(e).__name__,
                )
                error_type = type(e).__name__
                source_statuses[source_name] = f"ERRORED({error_type})"
                source_errors.setdefault(source_name, error_type)

            # Rate limit: respect each API's documented limits. Sleep the base
            # delay times a randomized ~[1.0, 1.5) jitter factor rather than
            # the exact duration every time — a perfectly regular gap between
            # requests is itself a timing fingerprint (trivially regular
            # signatures are easy to correlate, especially over Tor where a
            # metronomic request cadence can help link a stream back to a
            # user). The base delay is a floor, never a ceiling: jitter only
            # ever adds time, so the documented rate limit is never violated.
            base_delay = _SOURCE_DELAYS.get(source_name, _DEFAULT_DELAY)
            jitter_factor = 1.0 + random.random() * 0.5
            await asyncio.sleep(base_delay * jitter_factor)

        results.append(
            CrossRefResult(
                entity_name=entity.normalized_text,
                entity_type=entity.entity_type.value,
                hits=hits,
                sources_checked=sources_checked,
                source_statuses=source_statuses,
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
        source_errors=source_errors,
    )


def _get_available_sources(
    icij_data_dir: str | None = None,
    egress: EgressPolicy | None = None,
    opensanctions_data_dir: str | None = None,
) -> dict[str, Any]:
    """Discover which cross-reference sources are available.

    *egress* is bound into each checker closure so every source-checking
    call ends up routed through the same DIRECT-vs-TOR policy — the caller
    of crossref_entities() picks the policy once, not each `_check_<source>`.
    """
    sources: dict[str, Any] = {}

    # MuckRock — always available (public API, no key)
    sources["muckrock"] = lambda name, etype: _check_muckrock(name, etype, egress)

    # OpenCorporates — always available (free tier)
    sources["opencorporates"] = lambda name, etype: _check_opencorporates(name, etype, egress)

    # SEC EDGAR — always available (free)
    sources["sec"] = lambda name, etype: _check_sec(name, etype, egress)

    # ICIJ Offshore Leaks — available if CSVs downloaded locally
    if icij_data_dir:
        from pathlib import Path

        if Path(icij_data_dir).exists():
            sources["icij"] = lambda name, etype: _check_icij(name, etype, icij_data_dir)

    # DocumentCloud — always available (public API, no key)
    sources["documentcloud"] = lambda name, etype: _check_documentcloud(name, etype, egress)

    # USAspending — always available (no key, no rate limit)
    sources["usaspending"] = lambda name, etype: _check_usaspending(name, etype, egress)

    # ProPublica Nonprofit — always available (no key)
    sources["nonprofits"] = lambda name, etype: _check_nonprofits(name, etype, egress)

    # GovInfo — court opinions, congressional reports, Federal Register (DEMO_KEY)
    sources["govinfo"] = lambda name, etype: _check_govinfo(name, etype, egress)

    # FEC — campaign finance contributions (DEMO_KEY)
    sources["fec"] = lambda name, etype: _check_fec(name, etype, egress)

    # Regulations.gov — federal rulemaking documents (DEMO_KEY)
    sources["regulations"] = lambda name, etype: _check_regulations(name, etype, egress)

    # OpenSanctions API — keyed by OPENFOIA_OPENSANCTIONS_API_KEY when set
    sources["opensanctions"] = lambda name, etype: _check_opensanctions(name, etype, egress)

    # OpenSanctions bulk data — available if targets.simple.csv downloaded locally
    if opensanctions_data_dir:
        from pathlib import Path

        if Path(opensanctions_data_dir).exists():
            sources["opensanctions-local"] = lambda name, etype: _check_opensanctions_local(
                name, etype, opensanctions_data_dir
            )

    return sources


# ---------------------------------------------------------------------------
# Source checkers
# ---------------------------------------------------------------------------


async def _check_muckrock(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search MuckRock for FOIA requests mentioning this entity."""
    from .records.muckrock import MuckRockAdapter

    adapter = MuckRockAdapter(egress=egress)
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

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


async def _check_opencorporates(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search OpenCorporates for company registrations."""
    if entity_type == EntityType.PERSON:
        return []  # OpenCorporates is for companies

    from .records.opencorporates import OpenCorporatesAdapter

    adapter = OpenCorporatesAdapter(egress=egress)
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

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


async def _check_sec(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search SEC EDGAR for filings."""
    if entity_type == EntityType.PERSON:
        return []  # SEC is mostly company filings

    from .records.sec_edgar import SECEdgarAdapter

    adapter = SECEdgarAdapter(egress=egress)
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

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

    # Search across all ICIJ CSV files.  A local data read failure makes this
    # source incomplete, so surface it to the report rather than treating it
    # as an uneventful no-match -- but keep reading the files that do open, and
    # hand the hits found so far to the caller instead of dropping real matches.
    first_error: Exception | None = None

    try:
        csv_files = sorted(data_path.glob("*.csv"))
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

    if not csv_files:
        # A directory with no CSVs means the download or extraction did not
        # land where the user thinks it did. Reporting "checked, no hits"
        # would claim we searched the leaks when we read nothing at all.
        raise _SourceCheckError(
            FileNotFoundError("no ICIJ CSV files in data directory"),
            error_type="NoICIJData",
        )

    for csv_file in csv_files:
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
        except Exception as exc:
            # Log the file only, never the searched name or the row contents.
            logger.warning("Failed to search ICIJ file %s: %s", csv_file.name, type(exc).__name__)
            if first_error is None:
                first_error = exc

    if first_error is not None:
        raise _SourceCheckError(first_error, partial_hits=hits[:10]) from first_error

    return hits[:10]  # cap to avoid flooding


async def _check_fec(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search FEC for campaign finance contributions involving this entity."""
    from .records.fec import FECAdapter

    adapter = FECAdapter(egress=egress)
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

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


async def _check_regulations(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search Regulations.gov for federal rulemaking mentioning this entity."""
    from .records.regulations import RegulationsGovAdapter

    adapter = RegulationsGovAdapter(egress=egress)
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

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


async def _check_govinfo(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search GovInfo for court opinions, congressional reports, and federal rules."""
    from .records.govinfo import GovInfoAdapter

    adapter = GovInfoAdapter(egress=egress)
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

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


async def _check_nonprofits(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search ProPublica for nonprofit organizations matching this entity."""
    if entity_type == EntityType.PERSON:
        return []

    from .records.propublica_nonprofit import ProPublicaNonprofitAdapter

    adapter = ProPublicaNonprofitAdapter(egress=egress)
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

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


async def _check_usaspending(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search USAspending for federal contracts and grants involving this entity."""
    if entity_type == EntityType.PERSON:
        return []  # USAspending tracks organizations, not individuals

    from .records.usaspending import USASpendingAdapter

    adapter = USASpendingAdapter(egress=egress)
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

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


async def _check_documentcloud(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search DocumentCloud's 10M+ public document archive."""
    from .records.documentcloud import DocumentCloudAdapter

    adapter = DocumentCloudAdapter(egress=egress)
    try:
        result = await adapter.search(name, page_size=5)
        _check_rate_limit(result)
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

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


async def _check_opensanctions(
    name: str, entity_type: EntityType, egress: EgressPolicy | None = None
) -> list[CrossRefHit]:
    """Search OpenSanctions for sanctions/PEP matches.

    Sends ``OPENFOIA_OPENSANCTIONS_API_KEY`` (environment only) in the
    documented ``Authorization: ApiKey`` header when it is set.
    """
    import os

    hits = []
    headers = {"Accept": "application/json"}
    api_key = os.environ.get(OPENSANCTIONS_KEY_ENV)
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"

    try:
        async with egress_client(egress, timeout=15) as client:
            resp = await client.get(
                "https://api.opensanctions.org/search/default",
                params={"q": name, "limit": 5},
                headers=headers,
            )
            if resp.status_code == 429:
                raise _RateLimited("OpenSanctions rate limit")
            if resp.status_code in (401, 403):
                # Without a key every request fails the same way; stop sending
                # names for the rest of the run.
                raise _SourceCheckError(
                    PermissionError(f"HTTP {resp.status_code}"),
                    error_type=f"API key required: set {OPENSANCTIONS_KEY_ENV}",
                    stop_source="skipped(api-key-required)",
                )
            if resp.status_code != 200:
                raise _SourceCheckError(RuntimeError(f"HTTP {resp.status_code}"))
            data = resp.json()
    except _RateLimited:
        raise
    except _SourceCheckError:
        raise
    except Exception as exc:
        raise _SourceCheckError(exc) from exc

    for result in data.get("results", []):
        score = result.get("score", 0)
        if score < 0.5:
            continue

        schema = result.get("schema", "")
        properties = result.get("properties", {})
        countries = properties.get("country", [])
        topics = result.get("datasets", [])

        is_pep, is_sanctioned, detail_parts = _classify_datasets(topics, countries, schema)

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


_PEP_RE = re.compile(r"(?<![a-z])peps?(?![a-z])|politically exposed", re.IGNORECASE)


def _classify_datasets(
    datasets: list[str], countries: list[str], schema: str
) -> tuple[bool, bool, list[str]]:
    """Return (is_pep, is_sanctioned, detail parts) from dataset titles or names.

    Only a list whose title says "sanction" counts: OpenSanctions also carries
    procurement debarments, health-care exclusions and wanted notices, and its
    ``sanctions`` column is filled for those too.
    """
    is_pep = any(_PEP_RE.search(d) for d in datasets)
    # ponytail: title keywords; "US OFAC Specially Designated Nationals (SDN) List"
    # lacks the word. The full dataset's per-entity topics are the exact signal.
    is_sanctioned = any("sanction" in d.lower() or "ofac" in d.lower() for d in datasets)
    parts = []
    if is_pep:
        parts.append("PEP (Politically Exposed Person)")
    if is_sanctioned:
        parts.append("SANCTIONED")
    if countries:
        parts.append(f"Countries: {', '.join(countries[:3])}")
    if datasets:
        parts.append(f"Lists: {'; '.join(datasets[:2])}")
    if not parts:
        parts.append(schema)
    return is_pep, is_sanctioned, parts


def _normalize_name(text: str) -> str:
    """Lowercase, turn punctuation into spaces, collapse whitespace."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


async def _check_opensanctions_local(
    name: str, entity_type: EntityType, data_dir: str
) -> list[CrossRefHit]:
    """Screen a name against a downloaded OpenSanctions targets.simple.csv.

    Download from https://www.opensanctions.org/datasets/default/ (CC BY-NC 4.0).
    The file is streamed row by row; nothing leaves the machine.
    """
    import csv
    from pathlib import Path

    csv_path = Path(data_dir) / OPENSANCTIONS_FILE
    if not csv_path.is_file():
        # Same reasoning as NoICIJData: "checked, no hits" would claim a
        # screening that never read a single row.
        raise _SourceCheckError(
            FileNotFoundError(f"no {OPENSANCTIONS_FILE} in data directory"),
            error_type="NoOpenSanctionsData",
        )

    target = _normalize_name(name)
    if not target:
        return []

    hits: list[CrossRefHit] = []
    # ponytail: one full pass over the file per entity; build a name index if
    # screening hundreds of entities against the 449 MB file gets too slow.
    try:
        with open(csv_path, encoding="utf-8", errors="ignore", newline="") as f:
            for row in csv.DictReader(f):
                primary = row.get("name") or ""
                if _normalize_name(primary) == target:
                    match_type, score = "exact", 1.0
                else:
                    aliases = next(csv.reader([row.get("aliases") or ""], delimiter=";"), [])
                    if not any(_normalize_name(a) == target for a in aliases):
                        continue
                    match_type, score = "alias", 0.8

                datasets = [d for d in (row.get("dataset") or "").split(";") if d]
                countries = [c for c in (row.get("countries") or "").split(";") if c]
                is_pep, is_sanctioned, detail_parts = _classify_datasets(
                    datasets, countries, row.get("schema") or ""
                )

                entity_id = row.get("id") or ""
                hits.append(
                    CrossRefHit(
                        source="opensanctions-local",
                        entity_name=name,
                        match_type=match_type,
                        details=f"{primary or name} — {'; '.join(detail_parts)}",
                        url=f"https://www.opensanctions.org/entities/{entity_id}/"
                        if entity_id
                        else None,
                        extra={
                            "score": score,
                            "is_pep": is_pep,
                            "is_sanctioned": is_sanctioned,
                            "datasets": datasets,
                        },
                    )
                )
                if len(hits) >= 10:
                    break
    except Exception as exc:
        # Log the file only, never the searched name or the row contents.
        logger.warning(
            "Failed to read OpenSanctions file %s: %s", csv_path.name, type(exc).__name__
        )
        raise _SourceCheckError(exc, partial_hits=hits) from exc

    return hits
