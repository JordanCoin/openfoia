"""Federal Register adapter: federal rules, proposed rules, notices and presidential documents.

Uses the Federal Register API v1 (https://www.federalregister.gov/api/v1). No
API key, so nothing sent identifies the user beyond the query and the route.
GovInfo carries the same documents in its FR collection, but needs an
api.data.gov key and has no agency, document-type, docket or comment-deadline
filters; this API has all four.

Verified against the live API on 2026-11-17:

- ``GET /documents.json`` takes ``conditions[term]``, ``conditions[agencies][]``
  (agency slugs), ``conditions[type][]`` (RULE, PRORULE, NOTICE, PRESDOCU),
  ``conditions[publication_date][gte|lte]`` (YYYY-MM-DD),
  ``conditions[docket_id]``, ``fields[]``, ``per_page``, ``page``, ``order``.
- No results is HTTP 200 with ``{"count": 0}`` and no ``results`` key.
- An unknown agency slug or a bad date is HTTP 400 with ``{"errors": {...}}``;
  paging past 10,000 items is HTTP 400 with ``{"message": ...}``.
- An unknown document type is NOT an error: HTTP 200 with count 0. Types are
  therefore checked here, so a typo never reads as "no such documents".
- ``count`` is capped at 10,000.
- ``GET /documents/<number>.json`` is one document; unknown numbers are a 404
  HTML page.
- The website itself (documentation, html_url, raw_text_url, body_html_url)
  answers scripted clients with a 302 to unblock.federalregister.gov, a
  CAPTCHA. Only the API is used, and the full text comes from the PDF on
  www.govinfo.gov (the printed edition), which was reachable.
- No rate-limit headers were seen and no limit is documented on the API's
  responses. A 429 is retried by egress_client (net.RetryTransport, honouring
  Retry-After); one that persists is reported as an error.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..net import egress_client
from .base import (
    AdapterRequestError,
    RecordAdapter,
    RecordEntity,
    SearchResult,
    download_to_file,
    validate_download_url,
)

logger = logging.getLogger(__name__)
_sleep = asyncio.sleep  # patched in tests

API_BASE = "https://www.federalregister.gov/api/v1"
SITE_HOST = "www.federalregister.gov"
BLOCK_HOST = "unblock.federalregister.gov"
# Hosts the API's PDF links point at (seen 2026-11-17). Anything else is refused.
PDF_HOSTS = frozenset({"www.govinfo.gov", "govinfo.gov", "public-inspection.federalregister.gov"})
MAX_PDF_BYTES = 200 * 1024 * 1024
# www.govinfo.gov answered a PDF's first request with 502 twice on 2026-11-17 and
# served it on the next one; egress_client retries only 429 and 503.
PDF_RETRY_STATUSES = frozenset({502, 504})
PDF_ATTEMPTS = 3
PAGE_MAX = 1000  # the API accepts more, but stops paging at 10,000 items
AGENCIES_HELP = (
    "Use the agency's slug as listed at https://www.federalregister.gov/agencies "
    "(for example environmental-protection-agency), its name, or a short name such as EPA."
)

#: User spellings -> API type codes.
DOC_TYPES = {
    "rule": "RULE",
    "final-rule": "RULE",
    "proposed-rule": "PRORULE",
    "prorule": "PRORULE",
    "notice": "NOTICE",
    "presidential": "PRESDOCU",
    "presidential-document": "PRESDOCU",
    "presdocu": "PRESDOCU",
}
DOC_TYPE_CHOICES = "rule, proposed-rule, notice, presidential"

#: Short names -> slugs, from /api/v1/agencies.json (short_name, slug) on 2026-11-17.
#: Other agencies: pass the slug or the full name.
AGENCY_ALIASES = {
    "dhs": "homeland-security-department",
    "dod": "defense-department",
    "doe": "energy-department",
    "doj": "justice-department",
    "epa": "environmental-protection-agency",
    "fbi": "federal-bureau-of-investigation",
    "fcc": "federal-communications-commission",
    "fda": "food-and-drug-administration",
    "ftc": "federal-trade-commission",
    "ice": "u-s-immigration-and-customs-enforcement",
    "irs": "internal-revenue-service",
    "osha": "occupational-safety-and-health-administration",
    "sec": "securities-and-exchange-commission",
}

SEARCH_FIELDS = [
    "document_number",
    "title",
    "type",
    "subtype",
    "action",
    "abstract",
    "excerpts",
    "publication_date",
    "effective_on",
    "comments_close_on",
    "comment_url",
    "agencies",
    "docket_ids",
    "regulation_id_numbers",
    "citation",
    "significant",
    "html_url",
    "pdf_url",
]

_DOC_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")
_DOC_URL_RE = re.compile(r"^/(?:documents/\d{4}/\d{2}/\d{2}|d)/([A-Za-z0-9-]+)(?:/|$)")
_TAG_RE = re.compile(r"<[^>]*>")


def clean(value: Any) -> str | None:
    """An API string made safe to print: tags stripped, entities decoded, control chars gone.

    Titles and abstracts carry markup (``<span class="match">``, ``<INF>``) and
    are upstream data, so an ``&#27;`` entity must not become a live terminal
    escape. Unescaping happens before the control-character pass for that reason.
    """
    if value is None:
        return None
    text = html.unescape(_TAG_RE.sub("", str(value)))
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split()) or None


def normalize_document_number(identifier: str) -> str:
    """A Federal Register document number from a number or a federalregister.gov URL.

    Raises ValueError for anything else, so an identifier never reaches the
    API path unchecked.
    """
    ident = (identifier or "").strip()
    if ident.lower().startswith(("https://", "http://")):
        parts = urlsplit(ident)
        match = _DOC_URL_RE.match(parts.path) if parts.hostname == SITE_HOST else None
        if not match:
            raise ValueError(
                f"Not a Federal Register document URL: {ident!r}. "
                "Give the document number, e.g. 2026-21024."
            )
        ident = match.group(1)
    if not _DOC_NUMBER_RE.match(ident):
        raise ValueError(
            f"Not a Federal Register document number: {ident!r} (expected e.g. 2026-21024)."
        )
    return ident


def doc_type_codes(value: str | None) -> list[str]:
    """API type codes for a comma-separated list of user spellings; ValueError on unknown ones."""
    codes: list[str] = []
    for part in (value or "").split(","):
        key = re.sub(r"[\s_]+", "-", part.strip().lower())
        if not key:
            continue
        code = DOC_TYPES.get(key)
        if code is None:
            raise ValueError(
                f"Unknown Federal Register document type {part.strip()!r}. "
                f"Use one of: {DOC_TYPE_CHOICES} (comma-separated for several)."
            )
        if code not in codes:
            codes.append(code)
    return codes


def agency_slugs(value: str | None) -> list[str]:
    """Agency slugs for a comma-separated list of slugs, names or short names."""
    slugs: list[str] = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        slug = AGENCY_ALIASES.get(part.lower()) or re.sub(r"[^a-z0-9]+", "-", part.lower()).strip(
            "-"
        )
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def _iso_date(value: Any, flag: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError(f"{flag} must be a date as YYYY-MM-DD, got {value!r}.") from None


def build_params(query: str, page: int, per_page: int, **kwargs: Any) -> dict[str, Any]:
    """Query parameters for /documents.json. Raises ValueError on a filter the API would misread.

    Filters: ``agency`` (slugs, names or short names, comma-separated),
    ``doc_type`` (or ``filing_type``, which the CLI's --type arrives as),
    ``since`` / ``until`` (publication date, YYYY-MM-DD) and ``docket``.
    """
    params: dict[str, Any] = {
        "per_page": max(1, min(int(per_page), PAGE_MAX)),
        "page": max(1, int(page)),
        "order": kwargs.get("order") or ("relevance" if query else "newest"),
        "fields[]": SEARCH_FIELDS,
    }
    if query:
        params["conditions[term]"] = query
    agencies = agency_slugs(kwargs.get("agency"))
    if agencies:
        params["conditions[agencies][]"] = agencies
    types = doc_type_codes(kwargs.get("doc_type") or kwargs.get("filing_type"))
    if types:
        params["conditions[type][]"] = types
    since = _iso_date(kwargs["since"], "--since") if kwargs.get("since") else None
    until = _iso_date(kwargs["until"], "--until") if kwargs.get("until") else None
    if since and until and since > until:
        raise ValueError(f"--since {since} is after --until {until}.")
    if since:
        params["conditions[publication_date][gte]"] = since
    if until:
        params["conditions[publication_date][lte]"] = until
    docket = (kwargs.get("docket") or "").strip()
    if docket:
        params["conditions[docket_id]"] = docket
    return params


def _api_error(resp: httpx.Response) -> str:
    """Why the API refused a request, in words a reporter can act on."""
    status = resp.status_code
    if status == 429:
        wait = resp.headers.get("Retry-After")
        return (
            "Federal Register rate limit (HTTP 429) after retrying"
            + (f"; it asks to wait {clean(wait)} seconds" if wait else "")
            + ". Wait a few minutes and search again."
        )
    if 300 <= status < 400:
        location = resp.headers.get("location") or ""
        if urlsplit(location).hostname == BLOCK_HOST:
            return (
                "federalregister.gov answered with its bot filter (a CAPTCHA at "
                f"{BLOCK_HOST}) instead of data. This is blocked, not an empty result."
            )
        return f"Federal Register answered HTTP {status} (redirect to {clean(location)!r})."
    detail = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, dict) and errors:
            detail = "; ".join(f"{clean(k)}: {clean(v)}" for k, v in errors.items())
            if "agencies" in errors:
                detail += ". " + AGENCIES_HELP
        elif body.get("message"):
            detail = clean(body["message"]) or ""
    if status == 400:
        return f"Federal Register rejected the query: {detail or 'HTTP 400'}"
    return f"Federal Register answered HTTP {status}" + (f": {detail}" if detail else ".")


class FederalRegisterAdapter(RecordAdapter):
    """Search and fetch Federal Register documents (1994 to today).

    Usage::

        adapter = FederalRegisterAdapter()
        results = await adapter.search("facial recognition", agency="DHS", doc_type="rule")
        results = await adapter.search("", docket="EPA-HQ-OAR-2021-0317")
        entity, pdf = await adapter.download("2026-21024", "/tmp/fr")
    """

    source_name = "federalregister"

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET an API path. Returns decoded JSON, None on 404; raises AdapterRequestError."""
        url = f"{API_BASE}/{path}"
        try:
            async with egress_client(self._egress, timeout=30) as client:
                resp = await client.get(url, params=params)
        except httpx.TimeoutException:
            raise AdapterRequestError("Federal Register timed out.") from None
        except httpx.HTTPError as exc:
            raise AdapterRequestError(f"Federal Register request failed: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise AdapterRequestError(_api_error(resp))
        try:
            return resp.json()
        except ValueError:
            raise AdapterRequestError(
                "Federal Register returned something other than JSON "
                f"({clean(resp.headers.get('content-type')) or 'no content type'})."
            ) from None

    async def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 25,
        **kwargs: Any,
    ) -> SearchResult:
        """Search by keyword and/or filters (see build_params). Errors come back in ``error``."""
        try:
            params = build_params(query, page, page_size, **kwargs)
        except ValueError as exc:
            return self._failed(query, str(exc), page=page, per_page=page_size)
        per_page = params["per_page"]
        try:
            data = await self._get_json("documents.json", params)
        except AdapterRequestError as exc:
            return self._failed(query, str(exc), page=page, per_page=per_page)
        count = data.get("count") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("results", []), list)
            or not isinstance(count, int)
        ):
            return self._failed(
                query, "Federal Register returned an unexpected response.", page=page
            )

        entities = [e for e in (_entity(doc) for doc in data.get("results") or []) if e]
        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=count,
            entities=entities,
            page=page,
            per_page=per_page,
            raw_response=data,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """One document's full metadata. None if the number does not exist.

        Raises AdapterRequestError for an invalid number or a failed request,
        so a failure never reads as "no such document".
        """
        try:
            number = normalize_document_number(identifier)
        except ValueError as exc:
            raise AdapterRequestError(str(exc)) from None
        data = await self._get_json(f"documents/{quote(number, safe='')}.json")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise AdapterRequestError("Federal Register returned an unexpected response.")
        entity = _entity(data)
        if entity is None:
            return None
        x = entity.extra_data
        x["dates"] = clean(data.get("dates"))
        x["pages"] = data.get("page_length")
        x["start_page"] = data.get("start_page")
        x["end_page"] = data.get("end_page")
        x["topics"] = [t for t in (clean(t) for t in data.get("topics") or []) if t]
        x["cfr_references"] = [
            f"{clean(r.get('title'))} CFR {clean(r.get('part'))}"
            for r in data.get("cfr_references") or []
            if isinstance(r, dict) and r.get("title") and r.get("part")
        ]
        x["public_inspection_pdf_url"] = data.get("public_inspection_pdf_url")
        x["signing_date"] = clean(data.get("signing_date"))
        x["executive_order_number"] = clean(data.get("executive_order_number"))
        x["regulations_dockets"] = [
            {
                "id": clean(d.get("id")),
                "title": clean(d.get("title")),
                "agency": clean(d.get("agency_name")),
            }
            for d in data.get("dockets") or []
            if isinstance(d, dict) and d.get("id")
        ]
        return entity

    async def download(self, identifier: str, output_dir: Path | str) -> tuple[RecordEntity, Path]:
        """Fetch a document's metadata, then stream its PDF into *output_dir*.

        The PDF is the printed edition on www.govinfo.gov (or, before printing,
        the public-inspection copy). Its URL comes from the API response, so it
        is checked by validate_download_url and against PDF_HOSTS first.
        """
        entity = await self.fetch(identifier)
        if entity is None:
            raise AdapterRequestError(
                f"Federal Register has no document {identifier!r} (HTTP 404)."
            )
        url = entity.extra_data.get("pdf_url") or entity.extra_data.get("public_inspection_pdf_url")
        if not url:
            raise AdapterRequestError(f"Federal Register lists no PDF for {identifier!r}.")
        try:
            validate_download_url(url)
        except ValueError as exc:
            raise AdapterRequestError(str(exc)) from None
        host = urlsplit(url).hostname
        if host not in PDF_HOSTS:
            raise AdapterRequestError(
                f"Refusing to download {url!r}: {host} is not GovInfo or the Federal "
                "Register's public-inspection host."
            )
        number = entity.identifiers["document_number"]
        dest = Path(output_dir) / f"federalregister-{number}.pdf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shown = clean(url)
        for attempt in range(1, PDF_ATTEMPTS + 1):
            try:
                async with egress_client(self._egress, timeout=120) as client:
                    await download_to_file(
                        client, url, dest, max_bytes=MAX_PDF_BYTES, egress=self._egress
                    )
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in PDF_RETRY_STATUSES and attempt < PDF_ATTEMPTS:
                    logger.info("%s answered HTTP %d; retrying the PDF", host, status)
                    await _sleep(2.0 * attempt)
                    continue
                raise AdapterRequestError(f"{host} answered HTTP {status} for {shown}") from None
            except (httpx.HTTPError, ValueError) as exc:
                raise AdapterRequestError(f"PDF download failed: {exc}") from exc
        with open(dest, "rb") as f:
            head = f.read(5)
        if head != b"%PDF-":
            dest.unlink()
            raise AdapterRequestError(f"{host} returned something other than a PDF for {shown}")
        entity.extra_data["pdf_path"] = str(dest)
        return entity, dest


def _site_url(value: Any) -> str | None:
    """*value* if it is an https link on federalregister.gov, else None (it is shown, not fetched)."""
    if isinstance(value, str):
        parts = urlsplit(value)
        if parts.scheme == "https" and parts.hostname == SITE_HOST:
            return clean(value)
    return None


def _entity(doc: dict[str, Any]) -> RecordEntity | None:
    if not isinstance(doc, dict):
        return None
    try:
        number = normalize_document_number(str(doc.get("document_number") or ""))
    except ValueError:
        return None
    agencies = [a for a in doc.get("agencies") or [] if isinstance(a, dict)]
    names = [n for n in (clean(a.get("name") or a.get("raw_name")) for a in agencies) if n]
    slugs = [s for s in (clean(a.get("slug")) for a in agencies) if s]
    dockets = [d for d in (clean(d) for d in doc.get("docket_ids") or []) if d]
    rins = [r for r in (clean(r) for r in doc.get("regulation_id_numbers") or []) if r]
    identifiers = {"document_number": number}
    citation = clean(doc.get("citation"))
    if citation:
        identifiers["citation"] = citation
    if dockets:
        identifiers["docket_ids"] = ", ".join(dockets)
    if rins:
        identifiers["regulation_id_numbers"] = ", ".join(rins)
    abstract = clean(doc.get("abstract"))
    pdf_url = doc.get("pdf_url") if isinstance(doc.get("pdf_url"), str) else None
    return RecordEntity(
        entity_type="GOVERNMENT_DOCUMENT",
        name=clean(doc.get("title")) or f"FR Doc. {number}",
        source="federalregister",
        source_url=_site_url(doc.get("html_url")),
        jurisdiction="us",
        identifiers=identifiers,
        extra_data={
            "document_type": clean(doc.get("type")),
            "subtype": clean(doc.get("subtype")),
            "action": clean(doc.get("action")),
            "agencies": names,
            "agency_slugs": slugs,
            "publication_date": clean(doc.get("publication_date")),
            "effective_on": clean(doc.get("effective_on")),
            "comments_close_on": clean(doc.get("comments_close_on")),
            "comment_url": clean(doc.get("comment_url")),
            "docket_ids": dockets,
            "citation": citation,
            "significant": doc.get("significant"),
            "description": abstract,
            "snippet": clean(doc.get("excerpts")) or abstract,
            "pdf_url": pdf_url,
        },
    )
