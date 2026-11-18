"""Web archive ingestion via HTTP/Tor.

Fetches web pages, strips trackers, extracts main content,
and archives locally for the document pipeline.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..net import EgressMode, EgressPolicy, egress_client

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
        elif tag == "div" and self._current_div_text is None:
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
        elif (
            tag == "div"
            and self._current_div_text is not None
            and len(self._tag_stack) <= self._div_depth
        ):
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


#: First element of every archived page. Defense in depth only: the tree
#: re-serialization below is what makes the page inert, but if a construct
#: ever slips past it, the browser still refuses to fetch anything.
ARCHIVE_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
)

#: Tags kept in an archived page. Nothing here loads a resource, runs code,
#: navigates, or submits anything. Every other tag is dropped and its text kept,
#: except _ARCHIVE_DROP_WITH_CONTENT, whose contents go too.
_ARCHIVE_ALLOWED_TAGS = frozenset(
    {
        "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "blockquote",
        "br", "caption", "center", "cite", "code", "col", "colgroup", "dd", "del",
        "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "i", "ins",
        "kbd", "li", "main", "mark", "nav", "ol", "p", "pre", "q", "rp", "rt", "ruby",
        "s", "samp", "section", "small", "span", "strong", "sub", "summary", "sup",
        "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u", "ul",
        "var", "wbr",
    }
)  # fmt: skip

#: Elements whose contents are code, styling or embedded documents rather than
#: page text. Dropped with everything inside them.
_ARCHIVE_DROP_WITH_CONTENT = frozenset(
    {
        "applet", "frameset", "iframe", "math", "noembed", "noframes", "object",
        "script", "select", "style", "svg", "template",
    }
)  # fmt: skip

_ARCHIVE_VOID_TAGS = frozenset({"br", "col", "hr", "wbr"})

#: Attributes kept on allowed tags. None of them holds a URL, a script or CSS.
#: Notably absent: src, href, srcset, style, on*, background, action, formaction,
#: xlink:href, poster, data, cite, ping, longdesc, manifest.
_ARCHIVE_ALLOWED_ATTRS = frozenset(
    {
        "abbr", "align", "colspan", "datetime", "dir", "headers", "id", "lang",
        "open", "reversed", "rowspan", "scope", "span", "start", "title", "value",
    }
)  # fmt: skip


class _ArchiveSanitizer(HTMLParser):
    """Rebuild a page from its parse events, keeping only inert markup.

    Nothing from the source is copied through as markup: every tag and
    attribute written out is one this class chose from an allowlist, and every
    piece of text is escaped. A construct the parser misreads (a mangled tag, an
    unusual end tag) can only lose content, never smuggle markup through: the
    browser parses our output, not the original page.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.body: list[str] = []
        self._open: list[str] = []
        self._drop: str | None = None
        self._drop_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=True)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        if self._drop is not None:
            if tag == self._drop and not self_closing:
                self._drop_depth += 1
            return
        if tag in _ARCHIVE_DROP_WITH_CONTENT:
            if not self_closing:
                self._drop, self._drop_depth = tag, 1
            return
        if tag == "title":
            self._in_title = not self_closing
            return
        if tag not in _ARCHIVE_ALLOWED_TAGS:
            return  # unwrap: drop the tag, keep the text inside it

        kept = [
            (name.lower(), value)
            for name, value in attrs
            if name.lower() in _ARCHIVE_ALLOWED_ATTRS and value is not None
        ]
        if tag == "a":
            # Keep where a link pointed as hover text, never as a live link.
            href = next((v for n, v in attrs if n.lower() == "href" and v), None)
            if href and not any(n == "title" for n, _ in kept):
                kept.append(("title", href))
        rendered = "".join(f' {n}="{escape(v, quote=True)}"' for n, v in kept)
        self.body.append(f"<{tag}{rendered}>")
        if tag in _ARCHIVE_VOID_TAGS:
            return
        if self_closing:
            self.body.append(f"</{tag}>")
        else:
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._drop is not None:
            if tag == self._drop:
                self._drop_depth -= 1
                if self._drop_depth == 0:
                    self._drop = None
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in self._open:
            # Close everything opened since, so the output stays well nested.
            while self._open:
                top = self._open.pop()
                self.body.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._drop is not None:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self.body.append(escape(data, quote=False))

    # Comments (IE conditional comments included), doctypes, processing
    # instructions and CDATA sections are not page text: drop them.
    def handle_comment(self, data: str) -> None:
        pass

    def handle_decl(self, decl: str) -> None:
        pass

    def handle_pi(self, data: str) -> None:
        pass

    def unknown_decl(self, data: str) -> None:
        pass

    def document(self) -> str:
        while self._open:
            self.body.append(f"</{self._open.pop()}>")
        title = " ".join("".join(self.title_parts).split())
        # The CSP is the first tag in the file: <html> and <head> are implied
        # by the HTML parser, so nothing can come before the policy.
        return (
            "<!DOCTYPE html>\n"
            f"{ARCHIVE_CSP_META}"
            '<meta charset="utf-8">'
            '<meta name="referrer" content="no-referrer">'
            f"<title>{escape(title, quote=False)}</title>\n"
            f"<body>{''.join(self.body)}</body>\n"
        )


def _sanitize_for_archive(html: str) -> str:
    """Rebuild *html* as an inert page for the local archive.

    An archived page is reopened later, from disk, and from the journalist's
    real IP even when it was fetched over Tor. Anything in it that loads a
    resource, runs script, refreshes or submits tells the page's author when,
    and from where, it was re-read. Regex filtering of the source cannot be made
    complete (the November 2026 audit found seven bypasses), so the page is
    parsed and rebuilt from an allowlist of tags and attributes, with a
    Content-Security-Policy that forbids every fetch as its first element.

    The archive keeps the page's text and basic structure (headings,
    paragraphs, lists, tables, emphasis). Images, styling, forms, embedded
    media, SVG and scripts are not kept. Links keep their text; where they
    pointed is kept as hover text (title), not as a clickable link.
    """
    sanitizer = _ArchiveSanitizer()
    # Best-effort parsing: malformed markup must not abort the archive. Whatever
    # was rebuilt before a parser error is still inert.
    with contextlib.suppress(Exception):
        sanitizer.feed(html)
        sanitizer.close()
    return sanitizer.document()


def _extract_content(html: str) -> tuple[str, str]:
    """Extract main text content and title from HTML.

    Returns (text_content, page_title).
    """
    extractor = _ContentExtractor()
    # Best-effort parsing: malformed markup must not abort the archive.
    with contextlib.suppress(Exception):
        extractor.feed(html)
    return extractor.get_content(), extractor.title.strip()


async def fetch_url(
    url: str, use_tor: bool = False, *, egress: EgressPolicy | None = None
) -> WebFetchResult:
    """Fetch a web page and extract its content.

    Args:
        url: The URL to fetch.
        use_tor: If True (and *egress* is not given), route through Tor via
            the egress choke point. Kept for backward compatibility.
        egress: How to route the request (DIRECT/TOR). Takes precedence over
            *use_tor* when both are given. Defaults to a plain DIRECT policy,
            or a TOR policy when use_tor=True.

    Returns:
        WebFetchResult with extracted text, title, and raw HTML.
    """
    policy = (
        egress
        if egress is not None
        else (EgressPolicy(mode=EgressMode.TOR) if use_tor else EgressPolicy())
    )

    async with egress_client(policy, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()

    raw_html = response.text
    # Strip tracking/analytics scripts
    cleaned_html = _strip_trackers(raw_html)
    # Extract main content
    text, title = _extract_content(cleaned_html)

    fetched_at = datetime.now(UTC).isoformat()

    return WebFetchResult(
        url=url,
        title=title or url,
        text=text,
        raw_html=raw_html,
        fetched_at=fetched_at,
        content_length=len(raw_html),
        used_tor=policy.is_tor,
    )


async def archive_url(
    url: str,
    storage_path: str | Path,
    use_tor: bool = False,
    *,
    egress: EgressPolicy | None = None,
) -> WebArchiveResult:
    """Fetch a URL and archive it locally.

    Saves raw HTML and extracted text to storage_path,
    returns an IngestResult-compatible dict.

    Args:
        url: The URL to fetch and archive.
        storage_path: Base directory for storing archived content.
        use_tor: If True (and *egress* is not given), route through Tor via
            the egress choke point. Kept for backward compatibility.
        egress: How to route the request (DIRECT/TOR). Takes precedence over
            *use_tor* when both are given.

    Returns:
        WebArchiveResult with paths to saved files and metadata.
    """
    storage = Path(storage_path)
    storage.mkdir(parents=True, exist_ok=True)

    result = await fetch_url(url, use_tor=use_tor, egress=egress)

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
