"""Web archive ingestion via HTTP/Tor.

Fetches web pages, strips trackers, extracts main content,
and archives locally for the document pipeline.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from uuid import uuid4

# Known tracker/analytics domains and script patterns to strip
TRACKER_PATTERNS: list[str] = [
    r"google-analytics\.com",
    r"googletagmanager\.com",
    r"facebook\.net",
    r"facebook\.com/tr",
    r"doubleclick\.net",
    r"hotjar\.com",
    r"segment\.com",
    r"mixpanel\.com",
    r"quantserve\.com",
    r"scorecardresearch\.com",
    r"chartbeat\.com",
    r"optimizely\.com",
    r"newrelic\.com",
    r"nr-data\.net",
    r"amplitude\.com",
    r"plausible\.io",
    r"matomo",
    r"piwik",
]

# Tags that are definitely not main content
NAV_TAGS = {"nav", "header", "footer", "aside", "menu", "sidebar"}


@dataclass
class WebFetchResult:
    """Result of fetching a web page."""

    url: str
    title: str
    text: str
    raw_html: str
    fetched_at: str
    content_length: int
    used_tor: bool


@dataclass
class WebArchiveResult:
    """Result of archiving a web page locally."""

    document_id: str
    url: str
    title: str
    text: str
    html_path: str
    text_path: str
    fetched_at: str
    file_size: int
    checksum: str
    metadata: dict[str, Any]


class _ContentExtractor(HTMLParser):
    """Simple readability-like content extractor.

    Strategy:
    1. Look for <article> or <main> tags and grab their text.
    2. Fall back to the largest <div> by text length.
    3. Strip nav/header/footer/aside content.
    """

    def __init__(self) -> None:
        super().__init__()
        self.title: str = ""
        self._in_title = False
        self._tag_stack: list[str] = []
        self._current_tag: str = ""
        self._skip_depth: int = 0
        self._in_script_or_style: bool = False

        # Collect text blocks keyed by container type
        self._article_text: list[str] = []
        self._main_text: list[str] = []
        self._div_blocks: list[list[str]] = []
        self._current_div_text: list[str] | None = None
        self._div_depth: int = 0
        self._body_text: list[str] = []

        self._in_article: int = 0
        self._in_main: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._tag_stack.append(tag)
        self._current_tag = tag

        if tag in ("script", "style"):
            self._in_script_or_style = True
            return

        if tag in NAV_TAGS:
            self._skip_depth += 1
            return

        if tag == "title":
            self._in_title = True

        if tag == "article":
            self._in_article += 1
        elif tag == "main":
            self._in_main += 1
        elif tag == "div":
            if self._current_div_text is None:
                self._current_div_text = []
                self._div_depth = len(self._tag_stack)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in ("script", "style"):
            self._in_script_or_style = False

        if tag in NAV_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

        if tag == "title":
            self._in_title = False

        if tag == "article" and self._in_article > 0:
            self._in_article -= 1
        elif tag == "main" and self._in_main > 0:
            self._in_main -= 1
        elif tag == "div" and self._current_div_text is not None:
            if len(self._tag_stack) <= self._div_depth:
                self._div_blocks.append(self._current_div_text)
                self._current_div_text = None

        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_script_or_style:
            return

        if self._in_title:
            self.title += data
            return

        if self._skip_depth > 0:
            return

        text = data.strip()
        if not text:
            return

        self._body_text.append(text)

        if self._in_article > 0:
            self._article_text.append(text)
        if self._in_main > 0:
            self._main_text.append(text)
        if self._current_div_text is not None:
            self._current_div_text.append(text)

    def get_content(self) -> str:
        """Return the best extracted content."""
        # Prefer <article> content
        if self._article_text:
            return "\n\n".join(self._article_text)

        # Then <main> content
        if self._main_text:
            return "\n\n".join(self._main_text)

        # Fall back to largest <div>
        if self._div_blocks:
            largest = max(self._div_blocks, key=lambda b: sum(len(t) for t in b))
            joined = "\n\n".join(largest)
            if len(joined) > 100:
                return joined

        # Last resort: all body text
        return "\n\n".join(self._body_text)


def _strip_trackers(html: str) -> str:
    """Remove script tags from known tracking/analytics domains."""
    combined_pattern = "|".join(TRACKER_PATTERNS)
    # Remove <script> tags whose src matches tracker patterns
    html = re.sub(
        rf'<script[^>]*(?:src=["\'][^"\']*(?:{combined_pattern})[^"\']*["\'])[^>]*>.*?</script>',
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove inline scripts containing tracker references
    html = re.sub(
        rf"<script[^>]*>(?:[^<]*(?:{combined_pattern})[^<]*)</script>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove tracking pixels (1x1 images from tracker domains)
    html = re.sub(
        rf'<img[^>]*(?:src=["\'][^"\']*(?:{combined_pattern})[^"\']*["\'])[^>]*/?>',
        "",
        html,
        flags=re.IGNORECASE,
    )
    return html


def _sanitize_for_archive(html: str) -> str:
    """Strip every remote-loading construct from HTML before archiving it.

    Tracker-domain filtering alone is not enough for a file that will be
    reopened later: any surviving <script>, <img>, <iframe> or stylesheet
    <link> re-contacts its origin. An archived page must be inert.
    """
    # Drop scripts (and their contents) outright.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<noscript\b[^>]*>.*?</noscript>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Drop remote subresource elements.
    html = re.sub(
        r"<(?:img|iframe|frame|embed|object|video|audio|source|track)\b[^>]*/?>",
        "",
        html,
        flags=re.IGNORECASE,
    )
    # Drop <link> elements that fetch (stylesheets, preloads, prefetch...).
    html = re.sub(r"<link\b[^>]*>", "", html, flags=re.IGNORECASE)
    # Drop inline event handlers (onload=, onerror=, ...).
    html = re.sub(r"\son[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html, flags=re.IGNORECASE)
    # Neutralise any remaining external url() references in inline styles.
    html = re.sub(r"url\(\s*['\"]?https?://[^)]*\)", "url(about:blank)", html, flags=re.IGNORECASE)
    return html


def _extract_content(html: str) -> tuple[str, str]:
    """Extract main text content and title from HTML.

    Returns (text_content, page_title).
    """
    extractor = _ContentExtractor()
    try:
        extractor.feed(html)
    except Exception:
        pass  # Best-effort parsing
    return extractor.get_content(), extractor.title.strip()


async def fetch_url(url: str, use_tor: bool = False) -> WebFetchResult:
    """Fetch a web page and extract its content.

    Args:
        url: The URL to fetch.
        use_tor: If True, route through Tor SOCKS5 proxy at localhost:9050.

    Returns:
        WebFetchResult with extracted text, title, and raw HTML.
    """
    import httpx

    transport = None
    if use_tor:
        transport = httpx.AsyncHTTPTransport(proxy="socks5://127.0.0.1:9050")

    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; OpenFOIA/1.0; +https://github.com/JordanCoin/openfoia)",
        },
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

    raw_html = response.text
    # Strip tracking/analytics scripts
    cleaned_html = _strip_trackers(raw_html)
    # Extract main content
    text, title = _extract_content(cleaned_html)

    fetched_at = datetime.now(timezone.utc).isoformat()

    return WebFetchResult(
        url=url,
        title=title or url,
        text=text,
        raw_html=raw_html,
        fetched_at=fetched_at,
        content_length=len(raw_html),
        used_tor=use_tor,
    )


async def archive_url(
    url: str,
    storage_path: str | Path,
    use_tor: bool = False,
) -> WebArchiveResult:
    """Fetch a URL and archive it locally.

    Saves raw HTML and extracted text to storage_path,
    returns an IngestResult-compatible dict.

    Args:
        url: The URL to fetch and archive.
        storage_path: Base directory for storing archived content.
        use_tor: Route through Tor SOCKS5 proxy.

    Returns:
        WebArchiveResult with paths to saved files and metadata.
    """
    storage = Path(storage_path)
    storage.mkdir(parents=True, exist_ok=True)

    result = await fetch_url(url, use_tor=use_tor)

    doc_id = str(uuid4())
    dest_dir = storage / doc_id[:2] / doc_id[2:4]
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Save the SANITIZED HTML, never the raw page. Persisting raw HTML kept
    # analytics scripts and tracking pixels live in the archive: opening the
    # saved page later would fire those beacons and tell the tracker (and the
    # journalist's ISP) that the page was re-read, and from where.
    safe_html = _sanitize_for_archive(result.raw_html)
    html_path = dest_dir / f"{doc_id}.html"
    html_path.write_text(safe_html, encoding="utf-8")

    # Save extracted text
    text_path = dest_dir / f"{doc_id}.txt"
    text_path.write_text(result.text, encoding="utf-8")

    checksum = hashlib.sha256(result.raw_html.encode("utf-8")).hexdigest()

    return WebArchiveResult(
        document_id=doc_id,
        url=url,
        title=result.title,
        text=result.text,
        html_path=str(html_path),
        text_path=str(text_path),
        fetched_at=result.fetched_at,
        file_size=len(result.raw_html),
        checksum=checksum,
        metadata={
            "source": "web",
            "url": url,
            "title": result.title,
            "fetched_at": result.fetched_at,
            "content_length": result.content_length,
            "used_tor": result.used_tor,
            "mime_type": "text/html",
        },
    )
