"""`openfoia search`: one query to every registered records source, provenance on every row.

The CLI command and the agent tool both call :func:`plan_search` and then
:func:`run_search`, so a human and an agent get the same sources, the same
egress route, the same consent gate and the same rows (CLAUDE.md principle 5).

There is no network code here. Each source is searched by its own
``RecordAdapter.search()``, through the same ``_search_one`` that
``openfoia records search`` uses, and every adapter request goes through
``openfoia.net.egress_client()``. Sources come from the records registry, so a
newly registered adapter is searched without any change to this module.

Two optional adapter hooks, read with ``getattr`` so other adapters need nothing:

* ``offline`` (bool): True when the adapter answers from data on this machine
  (OpenSanctions with its bulk file). It goes in ``SearchPlan.local``, not
  ``adapters``: it is not listed as contacted, needs no consent or Tor route,
  and its rows carry ``egress: "local"``.
* ``match_confidence(entity)``: a real match score in 0..1 for a row. Only
  then does the row get a ``confidence`` key; no other source has one, so no
  other row gets it.

Results are untrusted third-party text. :func:`clean` (an alias of
:func:`openfoia.termsafe.safe_text`) makes a value safe to print to a
terminal; JSON callers should dump with :func:`openfoia.termsafe.safe_json`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from . import net
from .net import EgressPolicy, TorUnavailableError
from .termsafe import safe_text

logger = logging.getLogger(__name__)

#: Sources searched at the same time. Each adapter still keeps its own pacing
#: (SEC, NARA, CIA) and net.py spaces requests to any one host.
MAX_CONCURRENT_SOURCES = 4
#: A source that has not answered by then is reported as an error.
SOURCE_TIMEOUT = 120.0
REASON_MAX = 200

OK, NO_RESULTS, ERROR, SKIPPED = "ok", "no_results", "error", "skipped"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
#: One implementation for every terminal-bound value: see openfoia.termsafe.
clean = safe_text


def utc_now() -> str:
    """Current UTC time as ISO-8601 with a Z suffix, to the second."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_since(since: str | None) -> date | None:
    """YYYY-MM-DD as a date; ValueError for anything else."""
    if not since:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise ValueError(f"--since must be a date in YYYY-MM-DD format, got {since!r}")
    return date.fromisoformat(since)


def _row_date(value: str | None) -> date | None:
    if not value or not _ISO_DATE.match(value):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _opt_str(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _short(text: str) -> str:
    text = clean(text).strip()
    return text if len(text) <= REASON_MAX else text[: REASON_MAX - 1] + "…"


@dataclass
class SourceStatus:
    """What happened at one source: ok, no_results, error or skipped, and why."""

    source: str
    status: str
    reason: str | None = None
    total: int | None = None  # total the source reported, when it answered
    returned: int = 0  # rows in this report
    older_than_since: int = 0  # rows dropped by --since
    credentials_withheld: bool = False  # ran without the user's personal key (Tor)
    retrieved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "reason": self.reason,
            "total": self.total,
            "returned": self.returned,
            "older_than_since": self.older_than_since,
            "credentials_withheld": self.credentials_withheld,
            "retrieved_at": self.retrieved_at,
        }


@dataclass
class SearchPlan:
    """Which sources a query would go to, decided before any request."""

    query: str
    policy: EgressPolicy
    order: list[str]
    adapters: dict[str, Any]  # sources that will be sent the query
    skipped: dict[str, str]  # source -> why it will not be
    withheld_skipped: set[str] = field(default_factory=set)  # skipped for a withheld key
    local: dict[str, Any] = field(default_factory=dict)  # answered on this machine, nothing sent

    @property
    def egress(self) -> str:
        return "tor" if self.policy.is_tor else "direct"

    @property
    def contacted(self) -> list[str]:
        return list(self.adapters)

    @property
    def withheld(self) -> list[str]:
        """Sources that will run without the user's personal key (withheld over Tor)."""
        return [s for s, a in self.adapters.items() if getattr(a, "credentials_withheld", False)]

    def warning_lines(self) -> list[str]:
        """The pre-flight warning, shared by the CLI prompt and the agent tool."""
        if not self.adapters and self.local:
            lines = ["Nothing will be sent over the network."]
        elif not self.adapters:
            lines = ["Nothing will be sent: every selected source is skipped."]
        else:
            lines = [
                f"WARNING: this sends the query {self.query!r} to {len(self.adapters)} "
                f"external source(s): {', '.join(self.contacted)}",
            ]
            if self.policy.is_tor:
                isolation = "on" if self.policy.isolate_streams else "off"
                lines.append(
                    "  Egress: Tor. These servers will NOT see your real IP "
                    f"(stream isolation: {isolation})."
                )
            else:
                lines.append("  Egress: direct. These servers WILL see your real IP.")
            lines.append(
                "  Either way the query itself reaches every source listed: Tor hides who "
                "is asking, not what is asked. A global adversary watching both ends of "
                "the connection can still correlate timing."
            )
            if self.withheld:
                lines.append(
                    "  Personal credentials withheld over Tor (these run without them): "
                    + ", ".join(self.withheld)
                )
            for source, adapter in self.adapters.items():
                notice = getattr(adapter, "search_notice", None)
                if isinstance(notice, str) and notice:
                    lines.append(f"  {source}: {notice}")
        if self.local:
            described = []
            for source, adapter in self.local.items():
                note = getattr(adapter, "local_notice", None)
                described.append(f"{source} ({note})" if isinstance(note, str) and note else source)
            lines.append("  Searched on this machine only, nothing sent: " + "; ".join(described))
        if self.skipped:
            lines.append(
                "  Not contacted: "
                + "; ".join(f"{s} ({reason})" for s, reason in self.skipped.items())
            )
        return lines

    def preview(self) -> dict[str, Any]:
        """Machine-readable plan: what would be sent where, before anything is."""
        return {
            "query": self.query,
            "egress": self.egress,
            "would_contact": self.contacted,
            "credentials_withheld": self.withheld,
            "searched_locally": list(self.local),
            "skipped": dict(self.skipped),
            "warning": self.warning_lines(),
        }


@dataclass
class SearchReport:
    query: str
    egress: str
    since: str | None
    limit: int
    statuses: list[SourceStatus]
    rows: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0: every source answered or was skipped; 2: some errored; 1: none answered."""
        answered = [s for s in self.statuses if s.status in (OK, NO_RESULTS)]
        if not answered:
            return 1
        if any(s.status == ERROR for s in self.statuses):
            return 2
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "egress": self.egress,
            "since": self.since,
            "limit_per_source": self.limit,
            "sources": [s.to_dict() for s in self.statuses],
            "results": self.rows,
            "notes": self.notes,
        }


def egress_policy(egress: str | None = None, config: Any = None) -> EgressPolicy:
    """The configured egress policy (config.network), or *egress* ('tor'/'direct') for this run."""
    from .cli import _egress_policy_from  # lazy: circular import
    from .config import load_config  # lazy: monkeypatched in tests

    if egress not in (None, "tor", "direct"):
        raise ValueError(f"egress must be 'tor' or 'direct', got {egress!r}")
    cfg = config if config is not None else load_config()
    return _egress_policy_from(cfg, tor=None if egress is None else egress == "tor")


def plan_search(
    query: str,
    sources: list[str] | None = None,
    *,
    policy: EgressPolicy,
    api_data_gov_key: str | None = None,
) -> SearchPlan:
    """Pick the sources for *query*. No network.

    *sources* None means every registered source. An unknown name raises
    ValueError. A source whose adapter reports ``unavailable_reason()`` (a
    required key missing, or a personal key withheld over Tor) is skipped
    with that reason rather than sent the query. A source whose adapter is
    ``offline`` goes in ``SearchPlan.local``: it is searched, but sent nothing.
    """
    from .records import get_adapter, list_sources  # lazy: monkeypatched in tests

    query = (query or "").strip()
    if not query:
        raise ValueError("Give a search term.")
    available = list_sources()
    if sources:
        wanted = list(dict.fromkeys(s.strip().lower() for s in sources if s.strip()))
        unknown = [s for s in wanted if s not in available]
        if unknown:
            raise ValueError(
                f"Unknown source(s): {', '.join(unknown)}. Available: {', '.join(available)}"
            )
    else:
        wanted = available

    adapters: dict[str, Any] = {}
    local: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    withheld_skipped: set[str] = set()
    for source in wanted:
        adapter = get_adapter(source, egress=policy, api_data_gov_key=api_data_gov_key)
        check = getattr(adapter, "unavailable_reason", None)
        reason = check() if callable(check) else None
        if reason:
            skipped[source] = _short(reason)
            if getattr(adapter, "credentials_withheld", False):
                withheld_skipped.add(source)
        elif getattr(adapter, "offline", False) is True:
            local[source] = adapter
        else:
            adapters[source] = adapter
    return SearchPlan(query, policy, wanted, adapters, skipped, withheld_skipped, local)


async def ensure_route(policy: EgressPolicy) -> None:
    """Fail closed: raise TorUnavailableError unless Tor egress can be used right now.

    No-op for DIRECT. For TOR, checks the SOCKS stack (socksio) is installed
    and the proxy port accepts connections, before any query is sent.
    """
    if not policy.is_tor:
        return

    client = net.egress_client(policy)  # TorUnavailableError without socksio
    await client.aclose()
    if not await net.check_tor(policy):
        raise TorUnavailableError(
            f"Tor egress requested but the SOCKS proxy at {policy.tor_host}:{policy.tor_port} "
            "is not reachable. Start Tor (e.g. `tor` / `sudo systemctl start tor`). "
            "Nothing was sent."
        )


def _row(
    entity: Any, source: str, egress: str, retrieved_at: str, adapter: Any = None
) -> dict[str, Any]:
    from .cli import _compact_result  # lazy: circular import

    c = _compact_result(entity)
    row = {
        "source": source,
        "title": "" if c["title"] is None else str(c["title"]),
        "url": _opt_str(c["url"]),
        "snippet": "" if c["snippet"] is None else str(c["snippet"]),
        "retrieved_at": retrieved_at,
        "egress": egress,
        "id": _opt_str(c["id"]),
        "date": _opt_str(c["created"]),
        "type": _opt_str(getattr(entity, "entity_type", None)),
    }
    score = getattr(adapter, "match_confidence", None)
    value = score(entity) if callable(score) else None
    if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1:
        row["confidence"] = float(value)
    return row


async def run_search(
    plan: SearchPlan,
    *,
    limit: int = 10,
    since: str | None = None,
    allow_network: bool = False,
    log_to_case: bool = True,
) -> SearchReport:
    """Send the query to every source in *plan* and collect rows and statuses.

    *allow_network* must be True when the plan contacts any source: the caller
    shows ``plan.warning_lines()`` and gets consent first, the same gate as
    ``crossref_entities``. Raises TorUnavailableError (nothing sent) when the
    plan is Tor and Tor is not usable. One source failing never stops the
    others; each gets its own status.
    """
    from .cli import _search_one  # lazy: circular import

    since_date = parse_since(since)
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if plan.adapters and not allow_network:
        raise PermissionError(
            f"Searching would send {plan.query!r} to: {', '.join(plan.contacted)}. "
            "Pass allow_network=True after showing the warning and getting consent."
        )
    if plan.adapters:
        await ensure_route(plan.policy)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SOURCES)

    async def one(source: str, adapter: Any):
        async with semaphore:
            try:
                result = await asyncio.wait_for(
                    _search_one(adapter, source, plan.query, limit, {}), SOURCE_TIMEOUT
                )
            except TimeoutError:
                return source, None, f"no answer within {SOURCE_TIMEOUT:.0f}s", utc_now()
            except Exception as exc:
                return source, None, str(exc) or type(exc).__name__, utc_now()
            return source, result, None, utc_now()

    searched = {**plan.adapters, **plan.local}
    outcomes = await asyncio.gather(*(one(s, a) for s, a in searched.items()))
    by_source = {o[0]: o for o in outcomes}

    statuses: list[SourceStatus] = []
    rows: list[dict[str, Any]] = []
    for source in plan.order:
        if source in plan.skipped:
            statuses.append(
                SourceStatus(
                    source,
                    SKIPPED,
                    reason=plan.skipped[source],
                    credentials_withheld=source in plan.withheld_skipped,
                )
            )
            continue
        _, result, failure, retrieved_at = by_source[source]
        status = SourceStatus(
            source,
            ERROR,
            retrieved_at=retrieved_at,
            credentials_withheld=bool(getattr(searched[source], "credentials_withheld", 0)),
        )
        statuses.append(status)
        if result is None:
            status.reason = _short(failure or "failed")
            continue
        kept = []
        for entity in list(result.entities)[:limit]:
            egress = "local" if source in plan.local else plan.egress
            row = _row(entity, source, egress, retrieved_at, searched[source])
            row_date = _row_date(row["date"])
            if since_date and row_date and row_date < since_date:
                status.older_than_since += 1
                continue
            kept.append(row)
        rows.extend(kept)
        status.returned = len(kept)
        if result.error:
            status.reason = _short(result.error)
            continue
        status.total = result.total_results
        status.status = OK if kept else NO_RESULTS
        if not kept and status.older_than_since:
            status.reason = f"{status.older_than_since} result(s) older than --since"
        elif not kept:
            status.reason = " ".join(empty_result_caveats(source, result)) or None

    report = SearchReport(plan.query, plan.egress, since, limit, statuses, rows)
    if log_to_case and searched:
        _log(report)
    return report


def empty_result_caveats(source: str, result: Any) -> list[str]:
    """Why an empty result is not a clean "nothing there" for this source."""
    raw = result.raw_response or {}
    notes = []
    dropped = raw.get("filtered_out_locally")
    if source == "aleph" and dropped:
        notes.append(
            f"Aleph returned {dropped} result(s), but none matched --type/--jurisdiction when "
            "re-checked here, so all were dropped. Search again without those options to see them."
        )
    if source == "opensanctions" and raw.get("mode") == "local":
        notes.append(
            "Offline screening matches whole names and aliases exactly (word order and middle "
            "names matter), so no result is not proof the name is not listed. The API "
            "(OPENFOIA_OPENSANCTIONS_API_KEY) does fuzzy matching."
        )
    return notes


def _log(report: SearchReport) -> None:
    """Record the query against the active case, as `records search` does."""
    from .cli import _log_case_queries  # lazy: circular import

    entries = [
        (s.source, report.query, s.total if s.status in (OK, NO_RESULTS) else None)
        for s in report.statuses
        if s.status != SKIPPED
    ]
    try:
        _log_case_queries(entries)
    except Exception as exc:  # the search already happened; still return its results
        logger.warning("Could not log search to the active case: %s", type(exc).__name__)
        report.notes.append(f"Not logged to the active case ({type(exc).__name__}).")
