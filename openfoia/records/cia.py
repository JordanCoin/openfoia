"""CIA FOIA Electronic Reading Room adapter (cia.gov/readingroom).

The reading room is a Drupal site, not an API. Searching means what a
browser does: load the landing page for the form token, POST the search
block form, follow the redirect to ``/readingroom/search/site/<query>``
and read the result list. Document pages live at
``/readingroom/document/<esdn>`` and PDFs at ``/readingroom/docs/<NAME>.pdf``.

URL patterns (confirmed 2026-10-13 with a neutral query):

- search: ``/readingroom/search/site/<query>?page=N`` (N is 0-based), result
  items ``<ol class="search-results"><li><h3 class="title"><a href>`` plus
  ``<p class="search-snippet">``; no dates or totals, only a pager.
- document: ``/readingroom/document/<esdn lowercased>`` with Drupal field
  divs ``field-name-field-document-number``, ``field-name-field-page-count``,
  ``field-name-field-pub-date`` and a ``/readingroom/docs/<NAME>.pdf`` link.

The site sits behind a bot filter: every path except the landing page
answers scripted clients with a ``302`` to ``/readingroom`` carrying an
"Access Denied" body. That is reported as *blocked*, never as "no results".
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from ..net import EgressPolicy, egress_client
from .base import (
    AdapterRequestError,
    RecordAdapter,
    RecordEntity,
    SearchResult,
    download_to_file,
    validate_download_url,
)

logger = logging.getLogger(__name__)

BASE = "https://www.cia.gov/readingroom"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MIN_INTERVAL = 1.0  # seconds between requests to cia.gov

_CREST_RE = re.compile(r"^CIA-RDP[A-Z0-9-]+-\d$", re.I)
_RESULT_RE = re.compile(
    r'<h3 class="title">\s*<a href="([^"]+)">(.*?)</a>\s*</h3>'
    r'(?:\s*<div class="search-snippet-info">\s*<p class="search-snippet">(.*?)</p>)?',
    re.S,
)
_FIELD_RE = re.compile(
    r'field-name-field-([a-z-]+)[^>]*>.*?<div class="field-item[^"]*">(.*?)</div>', re.S
)

_last_request = 0.0


async def _throttle() -> None:
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request = time.monotonic()


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def blocked_reason(resp: Any, url: str) -> str | None:
    """Why *resp* is a bot-filter or CAPTCHA page, or None if it is not."""
    body = (
        (resp.text or "")[:4000].lower() if "text" in resp.headers.get("content-type", "") else ""
    )
    if (
        resp.status_code in (403, 429)
        or "<title>access denied</title>" in body
        or "captcha" in body
    ):
        return f"cia.gov blocked the request (HTTP {resp.status_code}, bot filter). Open it in a browser: {url}"
    if resp.status_code in (301, 302, 303, 307, 308):
        target = urlparse(resp.headers.get("location", "")).path.rstrip("/")
        if target in ("/readingroom", ""):
            return f"cia.gov redirected to the landing page instead of serving {url} (bot filter). Open it in a browser."
    return None


def normalize_identifier(identifier: str) -> tuple[str, str, str | None]:
    """Return (kind, document_page_url, pdf_url_or_None) for an identifier.

    Accepts a CREST number (``CIA-RDP96-00789R002200230001-2``), a MORI
    number (bare digits, zero padded to ten), an ESDN or ``DOC_...`` PDF
    name, or any ``/readingroom/`` URL.
    """
    ident = identifier.strip()
    if "://" in ident:
        path = urlparse(ident).path
        m = re.search(r"/readingroom/document/([^/?#]+)", path)
        if m:
            return "url", f"{BASE}/document/{m.group(1)}", None
        m = re.search(r"/readingroom/docs/([^/?#]+)\.pdf$", path, re.I)
        if m:
            name = m.group(1)
            slug = re.sub(r"^DOC_", "", name).lower()
            return "url", f"{BASE}/document/{slug}", f"{BASE}/docs/{name}.pdf"
        raise ValueError(f"Not a cia.gov reading room document URL: {identifier!r}")
    if _CREST_RE.match(ident):
        up = ident.upper()
        return "crest", f"{BASE}/document/{up.lower()}", f"{BASE}/docs/{up}.pdf"
    if ident.isdigit():
        padded = ident.zfill(10)
        return "mori", f"{BASE}/document/{padded}", f"{BASE}/docs/DOC_{padded}.pdf"
    m = re.match(r"^(?:DOC_)?([A-Za-z0-9-]+?)(?:\.pdf)?$", ident)
    if m:
        esdn = m.group(1)
        return "esdn", f"{BASE}/document/{esdn.lower()}", f"{BASE}/docs/DOC_{esdn}.pdf"
    raise ValueError(f"Unrecognised CIA reading room identifier: {identifier!r}")


class CIAReadingRoomAdapter(RecordAdapter):
    """Search and fetch documents from the CIA FOIA Electronic Reading Room."""

    source_name = "cia"

    def __init__(self, *, egress: EgressPolicy | None = None):
        super().__init__(egress=egress)

    def _client(self, timeout: float = 20):
        # No redirects: each hop is validated and a redirect home means "blocked".
        return egress_client(self._egress, timeout=timeout, follow_redirects=False)

    async def _get(self, client: Any, url: str, **kwargs: Any) -> Any:
        await _throttle()
        resp = await client.get(url, **kwargs)
        reason = blocked_reason(resp, url)
        if reason:
            raise AdapterRequestError(reason)
        return resp

    async def _search_page(self, client: Any, query: str, page: int) -> str:
        """Establish the Drupal session then load one page of results."""
        await _throttle()
        home = await client.get(f"{BASE}/")
        if home.status_code != 200:
            raise AdapterRequestError(f"cia.gov landing page returned HTTP {home.status_code}")
        m = re.search(r'name="form_build_id" value="([^"]+)"', home.text)
        if not m:
            raise AdapterRequestError("cia.gov landing page has no search form (layout changed?)")
        await _throttle()
        posted = await client.post(
            f"{BASE}/",
            data={
                "search_block_form": query,
                "form_build_id": m.group(1),
                "form_id": "search_block_form",
                "op": "Search",
            },
        )
        location = posted.headers.get("location", "")
        if posted.status_code not in (302, 303) or "/readingroom/search/" not in location:
            reason = blocked_reason(posted, f"{BASE}/")
            raise AdapterRequestError(
                reason or f"cia.gov search form returned HTTP {posted.status_code}"
            )
        url = f"{BASE}/search/site/{quote(query, safe='')}"
        if page > 1:
            url += f"?page={page - 1}"
        resp = await self._get(client, url)
        if resp.status_code != 200:
            raise AdapterRequestError(f"cia.gov search returned HTTP {resp.status_code}")
        return resp.text

    @staticmethod
    def parse_search(page_html: str) -> tuple[list[RecordEntity], bool]:
        """Parse result items; returns (entities, has_next_page)."""
        entities = []
        for href, title, snippet in _RESULT_RE.findall(page_html):
            url = html.unescape(href)
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            entities.append(
                RecordEntity(
                    entity_type="DOCUMENT",
                    name=_text(title) or "Untitled",
                    source="cia",
                    source_url=url,
                    identifiers={"esdn": slug.upper()},
                    extra_data={
                        "snippet": _text(snippet) if snippet else None,
                        "pdf_url": f"{BASE}/docs/{slug.upper()}.pdf"
                        if slug.upper().startswith("CIA-")
                        else None,
                    },
                )
            )
        has_next = 'class="pager-next' in page_html or "pager-next" in page_html
        return entities, has_next

    async def search(self, query: str, page: int = 1, **kwargs: Any) -> SearchResult:
        try:
            async with self._client() as client:
                page_html = await self._search_page(client, query, page)
        except AdapterRequestError as e:
            return self._failed(query, str(e), page=page)
        except Exception as e:
            return self._failed(query, f"{type(e).__name__}: {e}", page=page)

        entities, has_next = self.parse_search(page_html)
        # The site shows no total, only a pager; the count is for this page.
        return SearchResult(
            source="cia",
            query=query,
            total_results=len(entities),
            entities=entities,
            page=page,
            per_page=20,
            raw_response={"has_next_page": has_next},
        )

    @staticmethod
    def parse_document(page_html: str, page_url: str) -> RecordEntity:
        fields = {k: _text(v) for k, v in _FIELD_RE.findall(page_html)}
        m = re.search(r'<h1 class="documentFirstHeading">(.*?)</h1>', page_html, re.S)
        title = _text(m.group(1)) if m else fields.get("document-number", "Untitled")
        pdf = re.search(
            r'href="(https://www\.cia\.gov/readingroom/docs/[^"]+\.pdf)"', page_html, re.I
        )
        pages = fields.get("page-count")
        return RecordEntity(
            entity_type="DOCUMENT",
            name=title,
            source="cia",
            source_url=page_url,
            identifiers={
                "esdn": fields.get("document-number", page_url.rsplit("/", 1)[-1].upper())
            },
            extra_data={
                "pages": int(pages) if pages and pages.isdigit() else None,
                "date": fields.get("pub-date"),
                "release_date": fields.get("release-date"),
                "collection": fields.get("collection"),
                "doc_type": fields.get("taxonomy-doc-type"),
                "pdf_url": html.unescape(pdf.group(1)) if pdf else None,
            },
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Resolve an identifier to its document page. Raises AdapterRequestError when blocked."""
        _, page_url, pdf_url = normalize_identifier(identifier)
        async with self._client() as client:
            resp = await self._get(client, page_url)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise AdapterRequestError(f"cia.gov returned HTTP {resp.status_code} for {page_url}")
        entity = self.parse_document(resp.text, page_url)
        entity.extra_data.setdefault("pdf_url", None)
        if not entity.extra_data["pdf_url"]:
            entity.extra_data["pdf_url"] = pdf_url
        return entity

    async def download(self, identifier: str, output_dir: Path | str) -> tuple[RecordEntity, Path]:
        """Fetch the document page, then stream its PDF into *output_dir*."""
        entity = await self.fetch(identifier)
        if entity is None:
            raise AdapterRequestError(f"cia.gov has no document page for {identifier!r} (HTTP 404)")
        url = entity.extra_data.get("pdf_url")
        if not url:
            raise AdapterRequestError(f"No PDF link on the document page for {identifier!r}")
        validate_download_url(url)
        dest = Path(output_dir) / Path(urlparse(url).path).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        await _throttle()
        try:
            async with self._client(timeout=60) as client:
                await download_to_file(client, url, dest, max_bytes=MAX_DOWNLOAD_BYTES)
        except httpx.HTTPStatusError as e:
            reason = blocked_reason(e.response, url)
            raise AdapterRequestError(
                reason or f"cia.gov returned HTTP {e.response.status_code} for {url}"
            ) from e
        with open(dest, "rb") as f:
            head = f.read(5)
        if head != b"%PDF-":
            dest.unlink()
            raise AdapterRequestError(
                f"cia.gov returned a non-PDF body for {url} (bot filter or HTML error page)"
            )
        return entity, dest
