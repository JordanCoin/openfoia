"""CourtListener adapter: federal dockets and filings (the RECAP archive) and case law.

CourtListener is run by Free Law Project. Its REST API v4 needs a free personal
token for every data endpoint (anonymous calls get HTTP 401, checked
2026-11-18): sign in at https://www.courtlistener.com/sign-in/, copy the token
from https://www.courtlistener.com/profile/api-token/ and set
OPENFOIA_COURTLISTENER_API_KEY. The token is personal, so it is never read from
config.json and never sent over Tor.

Default limits for a token are 5 requests a minute, 50 an hour and 125 a day,
each a rolling window (https://www.courtlistener.com/help/api/rest/v4/, read
2026-11-18). This adapter counts its own requests in the data directory,
waits out the per-minute window, and refuses once the hourly or daily budget
is used, so a batch never runs into a wall of HTTP 429s. CourtListener logs
API search queries against the account unless query logging is turned off in
the profile.

API docs: https://www.courtlistener.com/help/api/rest/v4/
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..db import get_data_dir
from ..net import _retry_after, egress_client
from ..termsafe import safe_text
from .base import (
    AdapterRequestError,
    RecordAdapter,
    RecordEntity,
    SearchResult,
    download_to_file,
    validate_download_url,
)
from .store import store_text_document

logger = logging.getLogger(__name__)

SITE = "https://www.courtlistener.com"
API_HOST = "www.courtlistener.com"
API_PATH = "/api/rest/v4/"
API_BASE = SITE + API_PATH.rstrip("/")
STORAGE_HOST = "storage.courtlistener.com"
STORAGE_BASE = f"https://{STORAGE_HOST}/"

KEY_ENV = "OPENFOIA_COURTLISTENER_API_KEY"
BUDGET_ENV = "OPENFOIA_COURTLISTENER_IGNORE_BUDGET"
BUDGET_FILE = "courtlistener_budget.json"
#: (window seconds, requests, name): CourtListener's documented defaults for a token.
LIMITS = ((60, 5, "minute"), (3600, 50, "hour"), (86400, 125, "day"))
DAY_LIMIT = LIMITS[-1][1]
#: Longest pause taken for the per-minute window or a 429's Retry-After before giving up.
MAX_WAIT = 65.0
USAGE_URL = f"{SITE}/profile/api-usage/"
TOKEN_URL = f"{SITE}/profile/api-token/"

NO_KEY_MESSAGE = (
    "CourtListener needs a free API token. Create an account at "
    f"{SITE}/sign-in/, copy the token from {TOKEN_URL} and set {KEY_ENV}. "
    "The token is personal: it identifies you to Free Law Project, which logs API "
    "searches against your account unless query logging is turned off in your profile."
)
TOR_KEY_MESSAGE = (
    "CourtListener needs your personal API token, which would identify you, so "
    "CourtListener is not used over Tor (it does not answer without a token). "
    "Turn off network.tor to query it directly."
)

#: Search types this adapter maps: dockets with up to three nested filings, filings,
#: dockets only, case law. https://www.courtlistener.com/help/api/rest/v4/search/
SEARCH_TYPES = {"r": "dockets", "rd": "filings", "d": "dockets", "o": "opinions"}
DEFAULT_SEARCH_TYPE = "r"

#: Pages of docket entries read per docket fetch (each page is one counted request).
ENTRY_PAGES = 1
#: Requests one docket fetch can use: docket, entry pages, parties, attorneys.
DOCKET_FETCH_REQUESTS = 1 + ENTRY_PAGES + 2

MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_FIELD = 300  # one-line fields: names, case names, docket numbers
MAX_DESCRIPTION = 2000  # docket entry descriptions
MAX_TEXT = 5_000_000  # document and opinion text
MAX_LIST = 500  # parties, attorneys, entries kept per docket

DOCKET_FIELDS = ",".join(
    (
        "id",
        "court_id",
        "case_name",
        "case_name_full",
        "docket_number",
        "date_filed",
        "date_terminated",
        "date_last_filing",
        "assigned_to_str",
        "referred_to_str",
        "nature_of_suit",
        "cause",
        "jury_demand",
        "jurisdiction_type",
        "pacer_case_id",
        "absolute_url",
    )
)
RECAP_DOCUMENT_FIELDS = ",".join(
    (
        "id",
        "description",
        "document_number",
        "attachment_number",
        "page_count",
        "plain_text",
        "ocr_status",
        "is_available",
        "is_sealed",
        "filepath_local",
        "absolute_url",
        "pacer_doc_id",
    )
)

_sleep = asyncio.sleep
_budget_warned = False

# C0/C1 controls (terminal escapes), DEL, and bidi overrides that reorder what a
# terminal shows. Tab, newline and form feed (page breaks) survive in long text.
_TEXT_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0d-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")
_TAG = re.compile(r"<[^<>]{0,200}>")


# ---------------------------------------------------------------------------
# Untrusted strings
# ---------------------------------------------------------------------------


def clean_line(value: Any, limit: int = MAX_FIELD) -> str:
    """One printable line from an API value: no control characters, capped at *limit*."""
    if value is None:
        return ""
    # The shared sanitizer keeps U+200C/U+200D, which Persian and Indic names need.
    text = safe_text(" ".join(str(value).split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    """Long API text with terminal escapes and bidi overrides removed, capped at *limit*."""
    if not value:
        return ""
    text = _TEXT_UNSAFE.sub("", str(value).replace("\r\n", "\n"))
    if len(text) > limit:
        text = text[:limit] + f"\n[truncated by openfoia at {limit:,} characters]"
    return text


def _strip_html(value: Any, limit: int = MAX_TEXT) -> str:
    return clean_text(html.unescape(_TAG.sub("", str(value or ""))), limit)


def _snippet(value: Any) -> str:
    return clean_line(html.unescape(_TAG.sub("", str(value or ""))), 500)


def _int_id(value: Any) -> str | None:
    """*value* as a decimal id string, or None: ids are built into URLs."""
    text = str(value).strip() if value is not None else ""
    # ASCII only: str.isdigit() also accepts superscript and Arabic-Indic digits.
    return text if re.fullmatch(r"[0-9]{1,20}", text) else None


def site_url(path: Any) -> str | None:
    """A courtlistener.com page URL from an API ``absolute_url`` path, or None.

    The path is attacker-influenced; only a plain absolute path is accepted, so
    it can never name another host (``//evil``) or smuggle whitespace.
    """
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return None
    if len(path) > 500 or any(not ch.isprintable() or ch.isspace() for ch in path):
        return None
    return SITE + path


def storage_url(filepath_local: Any) -> str | None:
    """The download URL for a ``filepath_local`` value, or None if it is unsafe.

    CourtListener documents that file paths are joined to
    https://storage.courtlistener.com/. The path comes from the API response,
    so only plain path characters are accepted (no scheme, query, ``@`` or
    invisible characters), and the result must still pass
    :func:`storage_url_allowed` (no traversal, the storage host only).
    """
    if not isinstance(filepath_local, str) or not filepath_local:
        return None
    path = filepath_local.lstrip("/")
    if len(path) > 500 or not re.fullmatch(r"[A-Za-z0-9._/-]+", path):
        return None
    url = STORAGE_BASE + path
    return url if storage_url_allowed(url) else None


def storage_url_allowed(url: str) -> bool:
    """True only for an https URL on CourtListener's file storage that validate_download_url accepts."""
    try:
        validate_download_url(url)
    except ValueError:
        return False
    parts = urlsplit(url)
    return parts.hostname == STORAGE_HOST and ".." not in parts.path.split("/")


def _api_url(path_or_url: str) -> str:
    """Absolute API URL for a path, or a ``next`` URL from a response after checking it.

    A ``next`` link comes from the response body; following one to any other
    host would send the token there.
    """
    if path_or_url.startswith("/"):
        return API_BASE + path_or_url
    parts = urlsplit(path_or_url)
    if (
        parts.scheme != "https"
        or parts.hostname != API_HOST
        or parts.port not in (None, 443)
        or parts.username
        or not parts.path.startswith(API_PATH)
    ):
        raise AdapterRequestError(
            f"Refusing to follow a CourtListener link outside {API_BASE}/: {clean_line(path_or_url)!r}"
        )
    return path_or_url


def parse_identifier(identifier: str) -> tuple[str, str]:
    """``(kind, id)`` for a fetch identifier.

    ``4214664``, ``docket:4214664`` or a courtlistener.com ``/docket/4214664/...``
    URL is a docket; ``rd:2974081`` a RECAP document (one filing); ``op:6489975``
    an opinion. Raises ValueError for anything else.
    """
    raw = str(identifier).strip()
    m = re.match(r"https://www\.courtlistener\.com/docket/(\d{1,20})(?:/|$)", raw)
    if m:
        return "docket", m.group(1)
    kind, sep, num = raw.partition(":")
    if not sep:
        kind, num = "docket", raw
    kind = {"docket": "docket", "d": "docket", "rd": "rd", "recap": "rd", "op": "op"}.get(
        kind.lower(), ""
    )
    if not kind or not _int_id(num):
        raise ValueError(
            f"Not a CourtListener id: {clean_line(raw)!r}. Use a docket id (4214664), "
            "rd:<id> for a filing, or op:<id> for an opinion."
        )
    return kind, num.strip()


def canonical_id(identifier: str) -> str:
    """The id stored in a request number: ``4214664``, ``rd:2974081`` or ``op:6489975``."""
    kind, num = parse_identifier(identifier)
    return num if kind == "docket" else f"{kind}:{num}"


# ---------------------------------------------------------------------------
# Local request budget (rolling windows, like CourtListener's own throttles)
# ---------------------------------------------------------------------------


def budget_path():
    return get_data_dir() / BUDGET_FILE


def _load_budget(now: float) -> dict[str, Any]:
    try:
        data = json.loads(budget_path().read_text())
    except (OSError, ValueError):
        data = {}
    sent = data.get("sent") if isinstance(data, dict) else None
    sent = [t for t in (sent or []) if isinstance(t, int | float) and now - t < LIMITS[-1][0]]
    blocked = data.get("blocked_until") if isinstance(data, dict) else None
    return {
        "sent": sorted(sent),
        "blocked_until": blocked if isinstance(blocked, int | float) and blocked > now else None,
    }


def _save_budget(state: dict[str, Any]) -> None:
    path = budget_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError as exc:  # the counter is advisory; never block on it
        logger.debug("Could not write CourtListener budget file: %s", exc)


def budget_used(now: float | None = None) -> dict[str, int]:
    """Requests sent from this machine in each rolling window: {"minute": n, ...}."""
    now = time.time() if now is None else now
    sent = _load_budget(now)["sent"]
    return {name: sum(now - t < window for t in sent) for window, _, name in LIMITS}


def _when(ts: float, now: float) -> str:
    at = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")
    minutes = max(1, round((ts - now) / 60))
    span = f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"
    return f"{at} (in {span})"


async def _reserve_request() -> None:
    """Count one request, waiting out the minute window; raise once hour or day is used up."""
    now = time.time()
    state = _load_budget(now)
    if os.environ.get(BUDGET_ENV):
        state["sent"].append(now)
        _save_budget(state)
        return
    if state["blocked_until"]:
        raise AdapterRequestError(
            "CourtListener rate limit: the server asked us to wait until "
            f"{_when(state['blocked_until'], now)} (HTTP 429). Nothing was sent. "
            f"Your usage: {USAGE_URL}"
        )
    sent = state["sent"]
    for window, limit, name in reversed(LIMITS):
        recent = [t for t in sent if now - t < window]
        if len(recent) < limit:
            continue
        frees = recent[0] + window
        if name != "minute":
            label = "daily" if name == "day" else "hourly"
            raise AdapterRequestError(
                f"CourtListener {label} budget used up: {len(recent)} of {limit} requests in "
                f"the last {name} (counted on this machine). Nothing was sent. The next "
                f"request frees up at {_when(frees, now)}. CourtListener's own count can be "
                f"higher if this token is used elsewhere: {USAGE_URL}. With a Free Law "
                f"Project membership's higher limits, set {BUDGET_ENV}=1."
            )
        wait = max(0.0, frees - now)
        logger.warning("CourtListener allows %d requests a minute; waiting %.0fs.", limit, wait)
        await _sleep(wait)
        now = time.time()
    global _budget_warned
    day = sum(now - t < LIMITS[-1][0] for t in sent)
    if day + 1 >= DAY_LIMIT * 0.8 and not _budget_warned:
        _budget_warned = True  # once per process
        logger.warning("CourtListener: %d of %d daily requests used.", day + 1, DAY_LIMIT)
    sent.append(now)
    _save_budget(state)


def _block_until(ts: float) -> None:
    state = _load_budget(time.time())
    state["blocked_until"] = ts
    _save_budget(state)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CourtListenerAdapter(RecordAdapter):
    """Search CourtListener and fetch dockets, filings and opinions."""

    source_name = "courtlistener"

    def __init__(self, api_key: str | None = None, *, egress=None):
        super().__init__(egress=egress)
        # Read from the environment only: a personal token never goes in config.json.
        self._api_key = self._personal_credential(api_key or os.environ.get(KEY_ENV))

    #: Shown by ``openfoia search`` before the query is sent.
    search_notice = (
        "sends your API token, which identifies you; CourtListener logs API searches "
        "against your account unless query logging is off in your profile. Counts against "
        f"the {DAY_LIMIT} requests a day."
    )

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def unavailable_reason(self) -> str | None:
        # CourtListener answers every anonymous API call with 401.
        if self._api_key:
            return None
        if self.credentials_withheld:
            return "personal API token not sent over Tor"
        return f"no {KEY_ENV} (CourtListener refuses anonymous API calls)"

    def _redact(self, text: object) -> str:
        """Error text for the terminal: one line, no token, no brackets Rich would parse as markup."""
        out = clean_line(text, 500).replace("[", "(").replace("]", ")")
        return out.replace(self._api_key, "<redacted>") if self._api_key else out

    async def _get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict | None:
        """GET one API resource as JSON; None on 404. Every call is budget-counted."""
        if not self._api_key:
            raise AdapterRequestError(
                TOR_KEY_MESSAGE if self.credentials_withheld else NO_KEY_MESSAGE
            )
        url = _api_url(path_or_url)
        headers = {"Authorization": f"Token {self._api_key}", "Accept": "application/json"}
        waited = False
        while True:
            await _reserve_request()
            try:
                status, body, retry = await self._send(url, params, headers)
            except AdapterRequestError:
                raise
            except Exception as exc:
                raise AdapterRequestError(
                    f"CourtListener request failed: {type(exc).__name__}: {self._redact(exc)}"
                ) from exc
            if status != 429:
                break
            # The egress transport already retried waits up to 30s; one more for the
            # per-minute window, then stop and remember when the server said to come back.
            if retry is not None and retry <= MAX_WAIT and not waited:
                waited = True
                logger.warning("CourtListener rate limit (HTTP 429); waiting %.0fs.", retry)
                await _sleep(retry)
                continue
            detail = self._detail(body)
            when = ""
            if retry is not None:
                _block_until(time.time() + retry)
                when = f" Try again after {_when(time.time() + retry, time.time())}."
            raise AdapterRequestError(
                f"CourtListener rate limit reached (HTTP 429){detail}.{when} Your usage: {USAGE_URL}"
            )
        if status == 404:
            return None
        if status == 401:
            raise AdapterRequestError(
                f"CourtListener rejected the API token (HTTP 401){self._detail(body)}. Check "
                f"{KEY_ENV}; the token is shown at {TOKEN_URL}."
            )
        if status == 403:
            raise AdapterRequestError(
                f"CourtListener refused access (HTTP 403){self._detail(body)}. Some endpoints "
                "are open only to some accounts."
            )
        if status >= 400:
            raise AdapterRequestError(
                f"CourtListener API error (HTTP {status}){self._detail(body)}."
            )
        if status >= 300:
            raise AdapterRequestError(
                f"CourtListener answered with a redirect (HTTP {status}); not followed, so the "
                "token is only ever sent to the API host."
            )
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise AdapterRequestError("CourtListener returned something that is not JSON.") from exc
        if not isinstance(data, dict):
            raise AdapterRequestError("CourtListener returned an unexpected JSON shape.")
        return data

    async def _send(
        self, url: str, params: dict[str, Any] | None, headers: dict[str, str]
    ) -> tuple[int, bytes, float | None]:
        """Status, body (capped) and Retry-After seconds for one GET."""
        async with (
            egress_client(self._egress, timeout=30) as client,
            client.stream("GET", url, params=params, headers=headers) as resp,
        ):
            cap = MAX_RESPONSE_BYTES if resp.status_code < 400 else 4096
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > cap:
                    if resp.status_code >= 400:
                        break
                    raise AdapterRequestError(
                        f"CourtListener response over {MAX_RESPONSE_BYTES // (1024 * 1024)} "
                        "MB; refusing to read it."
                    )
            return resp.status_code, bytes(body), _retry_after(resp)

    def _detail(self, body: bytes) -> str:
        try:
            detail = json.loads(body).get("detail")
        except (ValueError, AttributeError):
            return ""
        return f": {self._redact(clean_line(detail, 200))}" if detail else ""

    # -- search ------------------------------------------------------------

    async def search(self, query: str, **kwargs: Any) -> SearchResult:
        """One page of CourtListener search results (one counted request).

        ``search_type`` (or ``filing_type``, the CLI's ``--type``) picks r (dockets
        with up to three matching filings, the default), rd (filings), d (dockets)
        or o (case law). ``court`` takes CourtListener court ids, space-separated.
        """
        stype = kwargs.get("search_type") or kwargs.get("filing_type") or DEFAULT_SEARCH_TYPE
        stype = str(stype).lower()
        if stype not in SEARCH_TYPES:
            return self._failed(
                query,
                f"Unknown CourtListener search type {clean_line(stype)!r}; use one of "
                + ", ".join(f"{k} ({v})" for k, v in SEARCH_TYPES.items()),
            )
        params: dict[str, Any] = {"q": query, "type": stype}
        court = kwargs.get("court") or kwargs.get("jurisdiction")
        if court:
            params["court"] = clean_line(court, 200)
        try:
            data = await self._get("/search/", params)
        except AdapterRequestError as exc:
            return self._failed(query, str(exc))
        if data is None:
            return self._failed(query, "CourtListener search endpoint not found (HTTP 404).")
        results = data.get("results")
        if not isinstance(results, list):
            return self._failed(query, "CourtListener search returned no result list.")
        build = {"r": _docket_hit, "d": _docket_hit, "rd": _filing_hit, "o": _opinion_hit}[stype]
        entities = [e for e in (build(r) for r in results if isinstance(r, dict)) if e]
        count = data.get("count")
        total = count if isinstance(count, int) else len(entities)
        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=total,
            entities=entities,
            per_page=len(results),
            raw_response={"count": total, "type": stype, "has_more": bool(data.get("next"))},
        )

    # -- fetch -------------------------------------------------------------

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """A docket (with entries, parties and attorneys), a filing, or an opinion.

        Raises ValueError for an unrecognized id and AdapterRequestError when the
        docket itself cannot be read. A docket whose entries, parties or attorneys
        fail still returns, with the failures listed in ``extra_data["errors"]``
        and in the stored text.
        """
        kind, num = parse_identifier(identifier)
        if kind == "rd":
            return await self._fetch_filing(num)
        if kind == "op":
            return await self._fetch_opinion(num)
        return await self._fetch_docket(num, int(kwargs.get("entry_pages") or ENTRY_PAGES))

    async def _fetch_docket(self, num: str, entry_pages: int) -> RecordEntity | None:
        docket = await self._get(f"/dockets/{num}/", {"fields": DOCKET_FIELDS})
        if docket is None:
            return None
        errors: list[str] = []

        entries: list[dict[str, Any]] = []
        more_entries = False
        url: str | None = "/docket-entries/"
        params: dict[str, Any] | None = {
            "docket": num,
            "order_by": "entry_number",
            "omit": "recap_documents__plain_text",
        }
        try:
            for _ in range(max(1, entry_pages)):
                page = await self._get(url, params) or {}
                entries += [_entry(e) for e in page.get("results") or [] if isinstance(e, dict)]
                url, params = page.get("next"), None
                if not isinstance(url, str) or not url:
                    break
            more_entries = isinstance(url, str) and bool(url)
        except AdapterRequestError as exc:
            errors.append(f"docket entries not retrieved: {exc}")

        parties: list[dict[str, Any]] = []
        try:
            page = await self._get(
                "/parties/", {"docket": num, "filter_nested_results": "True", "omit": "attorneys"}
            )
            parties = [_party(p) for p in (page or {}).get("results") or [] if isinstance(p, dict)]
            if (page or {}).get("next"):
                errors.append("more parties on CourtListener than the first page shown here")
        except AdapterRequestError as exc:
            errors.append(f"parties not retrieved: {exc}")

        attorneys: list[dict[str, Any]] = []
        try:
            page = await self._get(
                "/attorneys/",
                {
                    "docket": num,
                    "filter_nested_results": "True",
                    "fields": "id,name,parties_represented",
                },
            )
            attorneys = [
                _attorney(a) for a in (page or {}).get("results") or [] if isinstance(a, dict)
            ]
        except AdapterRequestError as exc:
            errors.append(f"attorneys not retrieved: {exc}")

        names = {p["id"]: p["name"] for p in parties if p.get("id")}
        for a in attorneys:
            a["represents"] = [names[i] for i in a.pop("party_ids") if i in names]

        entity = _docket_entity(docket, num)
        entity.extra_data.update(
            entries=entries[:MAX_LIST],
            more_entries=more_entries or len(entries) > MAX_LIST,
            parties=parties[:MAX_LIST],
            attorneys=attorneys[:MAX_LIST],
            errors=errors,
            text_kind="docket",
        )
        entity.extra_data["full_text"] = _docket_text(entity)
        entity.extra_data["text_length"] = len(entity.extra_data["full_text"])
        return entity

    async def _fetch_filing(self, num: str) -> RecordEntity | None:
        doc = await self._get(f"/recap-documents/{num}/", {"fields": RECAP_DOCUMENT_FIELDS})
        if doc is None:
            return None
        page_url = site_url(doc.get("absolute_url"))
        docket_id = None
        if page_url:
            m = re.match(r"/docket/(\d+)/", doc.get("absolute_url") or "")
            docket_id = m.group(1) if m else None
        text = clean_text(doc.get("plain_text"))
        pdf = storage_url(doc.get("filepath_local")) if doc.get("is_available") else None
        description = clean_line(doc.get("description")) or f"RECAP document {num}"
        identifiers = {"recap_document": f"rd:{num}"}
        if docket_id:
            identifiers["docket_id"] = docket_id
        return RecordEntity(
            entity_type="DOCUMENT",
            name=description,
            source="courtlistener",
            source_url=page_url or f"{API_BASE}/recap-documents/{num}/",
            identifiers=identifiers,
            extra_data={
                "document_number": clean_line(doc.get("document_number"), 20) or None,
                "attachment_number": doc.get("attachment_number"),
                "pages": doc.get("page_count") if isinstance(doc.get("page_count"), int) else None,
                "is_available": bool(doc.get("is_available")),
                "sealed": bool(doc.get("is_sealed")),
                "ocr_status": doc.get("ocr_status"),
                "pdf_url": pdf,
                "pdf_refused": bool(
                    doc.get("is_available") and doc.get("filepath_local") and not pdf
                ),
                "full_text": text or None,
                "text_kind": "recap",
                "text_length": len(text),
            },
        )

    async def _fetch_opinion(self, num: str) -> RecordEntity | None:
        op = await self._get(
            f"/opinions/{num}/",
            {"fields": "id,type,author_str,plain_text,html_with_citations,absolute_url"},
        )
        if op is None:
            return None
        text = clean_text(op.get("plain_text")) or _strip_html(op.get("html_with_citations"))
        author = clean_line(op.get("author_str"))
        return RecordEntity(
            entity_type="DOCUMENT",
            name=f"Opinion {num}" + (f" by {author}" if author else ""),
            source="courtlistener",
            source_url=site_url(op.get("absolute_url")) or f"{API_BASE}/opinions/{num}/",
            identifiers={"opinion": f"op:{num}"},
            extra_data={
                "opinion_type": clean_line(op.get("type"), 40) or None,
                "author": author or None,
                "full_text": text or None,
                "text_kind": "opinion",
                "text_length": len(text),
            },
        )

    # -- PDFs and storage ----------------------------------------------------

    async def download_pdf(self, entity: RecordEntity, dest_dir) -> Any:
        """Download a filing's PDF from storage.courtlistener.com into *dest_dir*.

        The API token is not sent (the storage host does not need it) and the
        download is not an API request, so it is not budget-counted. Raises
        ValueError for a URL that is not CourtListener's file storage.
        """
        url = entity.extra_data.get("pdf_url") or ""
        if not storage_url_allowed(url):
            raise ValueError(f"Refusing to download {clean_line(url)!r}: not {STORAGE_BASE}")
        validate_download_url(url)
        ident = entity.identifiers.get("recap_document", "rd:0").removeprefix("rd:")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"courtlistener-rd-{_int_id(ident) or 'unknown'}.pdf"
        if dest.exists():
            return dest
        # follow_redirects stays off: a redirect is a second, unvalidated URL.
        async with egress_client(self._egress, timeout=120, follow_redirects=False) as client:
            await download_to_file(client, url, dest, max_bytes=MAX_PDF_BYTES, egress=self._egress)
        return dest

    def store(
        self, entity: RecordEntity, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        """Save the entity's text as a local Document under request number CL-<id>."""
        ident = next(iter(entity.identifiers.values()))
        return store_text_document(
            entity,
            request_number=f"CL-{ident}",
            filename=f"courtlistener-{ident.replace(':', '-')}.txt",
            body=f"Pulled from CourtListener: {entity.source_url}",
            case_id=case_id,
        )

    async def pull_text(
        self, identifier: str, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        entity = await self.fetch(identifier)
        if not entity:
            return None, None
        return self.store(entity, case_id)


# ---------------------------------------------------------------------------
# Response mapping (every string cleaned; ids checked before they reach a URL)
# ---------------------------------------------------------------------------


def _date(value: Any) -> str | None:
    text = clean_line(value, 40)
    return text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", text) else None


def _hit_documents(hit: dict[str, Any]) -> list[dict[str, Any]]:
    docs = []
    for d in hit.get("recap_documents") or []:
        if not isinstance(d, dict) or not _int_id(d.get("id")):
            continue
        docs.append(
            {
                "id": f"rd:{_int_id(d.get('id'))}",
                "entry_number": d.get("entry_number")
                if isinstance(d.get("entry_number"), int)
                else None,
                "description": clean_line(d.get("short_description") or d.get("description")),
                "pages": d.get("page_count") if isinstance(d.get("page_count"), int) else None,
                "is_available": bool(d.get("is_available")),
                "snippet": _snippet(d.get("snippet")),
            }
        )
    return docs


def _docket_hit(hit: dict[str, Any]) -> RecordEntity | None:
    docket_id = _int_id(hit.get("docket_id"))
    if not docket_id:
        return None
    docs = _hit_documents(hit)
    meta = hit.get("meta") if isinstance(hit.get("meta"), dict) else {}
    parties = [clean_line(p) for p in (hit.get("party") or [])[:20] if p]
    snippet = next((d["snippet"] for d in docs if d["snippet"]), "") or _snippet(hit.get("snippet"))
    return RecordEntity(
        entity_type="COURT_CASE",
        name=clean_line(hit.get("caseName")) or f"Docket {docket_id}",
        source="courtlistener",
        source_url=site_url(hit.get("docket_absolute_url")) or f"{SITE}/docket/{docket_id}/",
        jurisdiction=clean_line(hit.get("court_id"), 20) or None,
        identifiers={
            "docket_id": docket_id,
            "docket_number": clean_line(hit.get("docketNumber"), 100),
        },
        extra_data={
            "court": clean_line(hit.get("court")) or None,
            "docket_number": clean_line(hit.get("docketNumber"), 100) or None,
            "date": _date(hit.get("dateFiled")),
            "date_terminated": _date(hit.get("dateTerminated")),
            "nature_of_suit": clean_line(hit.get("suitNature")) or None,
            "cause": clean_line(hit.get("cause")) or None,
            "assigned_to": clean_line(hit.get("assignedTo")) or None,
            "parties": parties,
            "documents": docs,
            "more_documents": bool(meta.get("more_docs")),
            "snippet": snippet,
        },
    )


def _filing_hit(hit: dict[str, Any]) -> RecordEntity | None:
    rd_id = _int_id(hit.get("id"))
    if not rd_id:
        return None
    case = clean_line(hit.get("caseName"))
    desc = clean_line(hit.get("short_description") or hit.get("description"))
    identifiers = {"recap_document": f"rd:{rd_id}"}
    if _int_id(hit.get("docket_id")):
        identifiers["docket_id"] = _int_id(hit.get("docket_id"))
    return RecordEntity(
        entity_type="DOCUMENT",
        name=clean_line(f"{case}: {desc}" if case and desc else case or desc or f"Filing {rd_id}"),
        source="courtlistener",
        source_url=site_url(hit.get("absolute_url")) or f"{API_BASE}/recap-documents/{rd_id}/",
        jurisdiction=clean_line(hit.get("court_id"), 20) or None,
        identifiers=identifiers,
        extra_data={
            "court": clean_line(hit.get("court")) or None,
            "docket_number": clean_line(hit.get("docketNumber"), 100) or None,
            "entry_number": hit.get("entry_number")
            if isinstance(hit.get("entry_number"), int)
            else None,
            "date": _date(hit.get("entry_date_filed") or hit.get("dateFiled")),
            "pages": hit.get("page_count") if isinstance(hit.get("page_count"), int) else None,
            "is_available": bool(hit.get("is_available")),
            "snippet": _snippet(hit.get("snippet")),
        },
    )


def _opinion_hit(hit: dict[str, Any]) -> RecordEntity | None:
    opinions = [
        o for o in hit.get("opinions") or [] if isinstance(o, dict) and _int_id(o.get("id"))
    ]
    cluster = _int_id(hit.get("cluster_id"))
    if not opinions and not cluster:
        return None
    identifiers: dict[str, str] = {}
    if opinions:
        identifiers["opinion"] = f"op:{_int_id(opinions[0]['id'])}"
    if cluster:
        identifiers["cluster_id"] = cluster
    if _int_id(hit.get("docket_id")):
        identifiers["docket_id"] = _int_id(hit.get("docket_id"))
    citations = [clean_line(c, 60) for c in (hit.get("citation") or [])[:5] if c]
    return RecordEntity(
        entity_type="DOCUMENT",
        name=clean_line(hit.get("caseName")) or f"Opinion cluster {cluster}",
        source="courtlistener",
        source_url=site_url(hit.get("absolute_url")),
        jurisdiction=clean_line(hit.get("court_id"), 20) or None,
        identifiers=identifiers,
        extra_data={
            "court": clean_line(hit.get("court")) or None,
            "docket_number": clean_line(hit.get("docketNumber"), 100) or None,
            "date": _date(hit.get("dateFiled")),
            "citations": citations,
            "status": clean_line(hit.get("status"), 40) or None,
            "snippet": _snippet(opinions[0].get("snippet") if opinions else hit.get("snippet")),
        },
    )


def _docket_entity(docket: dict[str, Any], num: str) -> RecordEntity:
    return RecordEntity(
        entity_type="COURT_CASE",
        name=clean_line(docket.get("case_name") or docket.get("case_name_full")) or f"Docket {num}",
        source="courtlistener",
        source_url=site_url(docket.get("absolute_url")) or f"{SITE}/docket/{num}/",
        jurisdiction=clean_line(docket.get("court_id"), 20) or None,
        identifiers={
            "docket_id": num,
            "docket_number": clean_line(docket.get("docket_number"), 100),
        },
        extra_data={
            "court": clean_line(docket.get("court_id"), 20) or None,
            "docket_number": clean_line(docket.get("docket_number"), 100) or None,
            "case_name_full": clean_line(docket.get("case_name_full"), 1000) or None,
            "date": _date(docket.get("date_filed")),
            "date_terminated": _date(docket.get("date_terminated")),
            "date_last_filing": _date(docket.get("date_last_filing")),
            "assigned_to": clean_line(docket.get("assigned_to_str")) or None,
            "referred_to": clean_line(docket.get("referred_to_str")) or None,
            "nature_of_suit": clean_line(docket.get("nature_of_suit")) or None,
            "cause": clean_line(docket.get("cause")) or None,
            "jury_demand": clean_line(docket.get("jury_demand")) or None,
            "jurisdiction_type": clean_line(docket.get("jurisdiction_type")) or None,
            "pacer_case_id": clean_line(docket.get("pacer_case_id"), 40) or None,
        },
    )


def _entry(e: dict[str, Any]) -> dict[str, Any]:
    docs = []
    for d in e.get("recap_documents") or []:
        if not isinstance(d, dict) or not _int_id(d.get("id")):
            continue
        docs.append(
            {
                "id": f"rd:{_int_id(d.get('id'))}",
                "description": clean_line(d.get("description")),
                "document_number": clean_line(d.get("document_number"), 20) or None,
                "attachment_number": d.get("attachment_number")
                if isinstance(d.get("attachment_number"), int)
                else None,
                "pages": d.get("page_count") if isinstance(d.get("page_count"), int) else None,
                "is_available": bool(d.get("is_available")),
            }
        )
    return {
        "entry_number": e.get("entry_number") if isinstance(e.get("entry_number"), int) else None,
        "date_filed": _date(e.get("date_filed")),
        "description": clean_line(e.get("description"), MAX_DESCRIPTION),
        "documents": docs,
    }


def _party(p: dict[str, Any]) -> dict[str, Any]:
    types = [
        clean_line(t.get("name"), 80)
        for t in p.get("party_types") or []
        if isinstance(t, dict) and t.get("name")
    ]
    return {"id": _int_id(p.get("id")), "name": clean_line(p.get("name")), "types": types}


def _attorney(a: dict[str, Any]) -> dict[str, Any]:
    party_ids = []
    for rep in a.get("parties_represented") or []:
        m = (
            re.search(r"/parties/(\d+)/", str(rep.get("party") or ""))
            if isinstance(rep, dict)
            else None
        )
        if m:
            party_ids.append(m.group(1))
    return {"name": clean_line(a.get("name")), "party_ids": party_ids}


def _docket_text(entity: RecordEntity) -> str:
    """A plain-text docket sheet: what is stored locally and searched with docs grep."""
    x = entity.extra_data
    lines = [
        f"CourtListener docket {entity.identifiers['docket_id']}",
        f"Case: {entity.name}",
    ]
    for label, key in (
        ("Full name", "case_name_full"),
        ("Court", "court"),
        ("Docket number", "docket_number"),
        ("Filed", "date"),
        ("Terminated", "date_terminated"),
        ("Last filing", "date_last_filing"),
        ("Judge", "assigned_to"),
        ("Referred to", "referred_to"),
        ("Nature of suit", "nature_of_suit"),
        ("Cause", "cause"),
        ("Jury demand", "jury_demand"),
        ("Jurisdiction", "jurisdiction_type"),
    ):
        if x.get(key):
            lines.append(f"{label}: {x[key]}")
    lines.append(f"Source: {entity.source_url}")
    for err in x.get("errors") or []:
        lines.append(f"NOTE: {err}")

    lines += ["", "Parties"]
    lines += [
        f"- {p['name']}" + (f" ({', '.join(p['types'])})" if p["types"] else "")
        for p in x.get("parties") or []
    ] or ["- none listed"]
    lines += ["", "Attorneys"]
    lines += [
        f"- {a['name']}" + (f" (for {', '.join(a['represents'])})" if a.get("represents") else "")
        for a in x.get("attorneys") or []
    ] or ["- none listed"]

    entries = x.get("entries") or []
    more = " (more on CourtListener)" if x.get("more_entries") else ""
    lines += ["", f"Docket entries: {len(entries)} shown{more}"]
    for e in entries:
        num = f"#{e['entry_number']}" if e["entry_number"] is not None else "#-"
        lines.append(f"{num}  {e['date_filed'] or '-'}  {e['description']}")
        for d in e["documents"]:
            avail = "PDF on CourtListener" if d["is_available"] else "not in RECAP"
            pages = f", {d['pages']} pages" if d["pages"] else ""
            lines.append(f"    {d['id']}  {d['description'] or 'document'} ({avail}{pages})")
    return "\n".join(lines) + "\n"
