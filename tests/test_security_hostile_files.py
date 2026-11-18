"""Hostile input files: archived web pages, documents and images.

A file the journalist did not write is attacker-controlled. An archived page
must not phone home when it is reopened, a stored copy must not be called
clean while it still names its author, and a small file must not be able to
exhaust the memory of a $300 laptop.

The browser tests open each archived page from disk in a real Chromium with
every request intercepted. They skip when Playwright or a Chromium build is
not available; the fast tests above them check the same pages statically.
"""

from __future__ import annotations

import os
from html.parser import HTMLParser
from pathlib import Path

import pytest

from openfoia.pipeline.web import ARCHIVE_CSP_META, _extract_content, _sanitize_for_archive

# ---------------------------------------------------------------------------
# Archived web pages (A3)
# ---------------------------------------------------------------------------

B = "https://beacon.example"

#: The seven bypasses the audit confirmed in Chromium against the regex
#: sanitizer. Opening the *unsanitized* page makes a request for every one of
#: these, which the browser test uses as its positive control.
AUDIT_PAYLOADS = {
    "svg_slash_onload": f"<svg/onload=\"fetch('{B}/svg')\"></svg>",
    "meta_refresh": f'<meta http-equiv="refresh" content="0;url={B}/r">',
    "css_import": f"<style>@import '{B}/i.css';</style>",
    "script_close_with_space": f"<script>fetch('{B}/s')</script >",
    "svg_image": f'<svg><image href="{B}/i.png" /></svg>',
    "input_image": f'<input type="image" src="{B}/in.png">',
    "body_background": f'<body background="{B}/bg.png"></body>',
}

#: Further constructs that load, navigate or run script in a browser.
MORE_PAYLOADS = {
    "img_onerror": f"<img src=x onerror=\"fetch('{B}/onerror')\">",
    "img_srcset": f'<img srcset="{B}/srcset.png 1x">',
    "picture_source": f'<picture><source srcset="{B}/pic.png"><img alt=x></picture>',
    "image_tag_outside_svg": f'<image src="{B}/image.png">',
    "link_stylesheet": f'<link rel="stylesheet" href="{B}/s.css">',
    "link_preload": f'<link rel="preload" as="image" href="{B}/pre.png">',
    "link_prefetch": f'<link rel="prefetch" href="{B}/prefetch">',
    "iframe_src": f'<iframe src="{B}/frame"></iframe>',
    "iframe_srcdoc": f"<iframe srcdoc=\"<img src='{B}/srcdoc.png'>\"></iframe>",
    "object_data": f'<object data="{B}/obj"></object>',
    "embed_src": f'<embed src="{B}/embed">',
    "video_poster": f'<video poster="{B}/poster.png"></video>',
    "audio_src": f'<audio src="{B}/a.mp3" autoplay></audio>',
    "video_source": f'<video autoplay><source src="{B}/v.mp4"></video>',
    "track": f'<video><track src="{B}/t.vtt" default></video>',
    "inline_style_url": f"<div style=\"background:url('{B}/style.png')\">x</div>",
    "inline_style_css_escape": f'<div style="background:\\75rl({B}/esc.png)">x</div>',
    "style_block_url": f"<style>body{{background:url({B}/block.png)}}</style>",
    "style_font_face": f"<style>@font-face{{font-family:f;src:url({B}/f.woff)}}"
    "p{font-family:f}</style><p>x</p>",
    "table_background": f'<table background="{B}/tb.png"><tr><td>x</td></tr></table>',
    "td_background": f'<table><tr><td background="{B}/td.png">x</td></tr></table>',
    "base_href": f'<base href="{B}/"><img src="relative.png">',
    "body_onload": f"<body onload=\"fetch('{B}/bodyonload')\"><p>x</p></body>",
    "details_ontoggle": f"<details open ontoggle=\"fetch('{B}/toggle')\">x</details>",
    "svg_use": f'<svg><use href="{B}/use.svg#a"/></svg>',
    "svg_xlink_image": f'<svg><image xlink:href="{B}/xlink.png"/></svg>',
    "svg_foreign_object": f'<svg><foreignObject><img src="{B}/fo.png"></foreignObject></svg>',
    "math_mtext_img": f'<math><mtext><img src="{B}/math.png"></mtext></math>',
    "noscript_img": f'<noscript><img src="{B}/noscript.png"></noscript>',
    "conditional_comment": f'<!--[if IE]><img src="{B}/ie.png"><![endif]-->',
    "upper_case_meta_refresh": f'<META HTTP-EQUIV="REFRESH" CONTENT="0;URL={B}/R">',
    "meta_refresh_unquoted": f"<meta http-equiv=refresh content=0;url={B}/unq>",
    "nested_script_split": f"<scr<script>x</script>ipt>fetch('{B}/split')</script>",
    "script_in_title": f"<title><script>fetch('{B}/title')</script></title>",
    "textarea_breakout": f"<textarea></textarea><img src=x onerror=\"fetch('{B}/ta')\">",
    "attribute_entity_js": '<a href="&#106;avascript:fetch(1)">link</a>',
    "form_autosubmit": f'<form action="{B}/form" id=f></form><button form=f autofocus>x</button>',
    "input_formaction": f'<input type=submit formaction="{B}/fa" autofocus>',
    "marquee_onstart": f"<marquee onstart=\"fetch('{B}/marquee')\">x</marquee>",
    "svg_animate_href": f'<svg><animate attributeName=href values="{B}/anim"/></svg>',
    "object_param": f'<object><param name=movie value="{B}/p.swf"></object>',
    "applet": f'<applet code=x codebase="{B}/applet"></applet>',
    "frameset": f'<frameset><frame src="{B}/frameset"></frameset>',
    "isindex": f'<isindex action="{B}/isindex">',
    "portal": f'<portal src="{B}/portal"></portal>',
    "meta_set_cookie": '<meta http-equiv="set-cookie" content="a=b">',
    "a_ping": f'<a href="{B}/a" ping="{B}/ping">x</a>',
    "uppercase_onload_no_space": f"<SVG/ONLOAD=\"fetch('{B}/upper')\"></SVG>",
    "slash_separated_attr": f'<img/src="{B}/slash.png">',
    "null_in_tag": f'<img\x00src="{B}/nul.png">',
}

ALL_PAYLOADS = {**AUDIT_PAYLOADS, **MORE_PAYLOADS}


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _tags(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _Tags()
    parser.feed(html)
    return parser.tags


@pytest.mark.parametrize("name", sorted(ALL_PAYLOADS))
def test_archived_payload_keeps_no_remote_reference(name):
    """Statically: only inert tags and attributes survive. (The URL may survive
    as escaped text, e.g. the body of a mangled script, which loads nothing.)"""
    from openfoia.pipeline.web import _ARCHIVE_ALLOWED_ATTRS, _ARCHIVE_ALLOWED_TAGS

    clean = _sanitize_for_archive("<p>report</p>" + ALL_PAYLOADS[name])

    assert "<p>report</p>" in clean
    tags = _tags(clean)
    assert [t for t, _ in tags[:4]] == ["meta", "meta", "meta", "title"]
    for tag, attrs in tags[4:]:
        assert tag == "body" or tag in _ARCHIVE_ALLOWED_TAGS, tag
        assert set(attrs) <= _ARCHIVE_ALLOWED_ATTRS, (tag, attrs)
        for attr, value in attrs.items():
            assert attr == "title" or "beacon" not in (value or ""), (tag, attr)


def test_archive_starts_with_the_content_security_policy():
    clean = _sanitize_for_archive("<html><head><title>T</title></head><body>x</body></html>")

    first = _tags(clean)[0]
    assert first == (
        "meta",
        {
            "http-equiv": "Content-Security-Policy",
            "content": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )
    assert clean.startswith("<!DOCTYPE html>\n" + ARCHIVE_CSP_META)
    # A page that brings its own head cannot put anything before the policy.
    hostile = _sanitize_for_archive('<meta http-equiv="Content-Security-Policy" content="x">')
    assert hostile.count("Content-Security-Policy") == 1


def test_archive_keeps_text_structure_and_title():
    page = (
        "<html><head><title>Contract  award\n list</title>"
        '<link rel="stylesheet" href="/s.css"></head><body>'
        '<nav><a href="/">Home</a></nav>'
        "<article><h1>Ministry &amp; contractors</h1>"
        "<p>Paid <b>4.2m</b> to <a href='https://example.org/acme'>Acme</a> &lt;Ltd&gt;</p>"
        "<table><tr><th>Year</th><td colspan=2>2026</td></tr></table>"
        "<ul><li>one<li>two</ul><pre>  spaced\n  text</pre></article>"
        "</body></html>"
    )

    clean = _sanitize_for_archive(page)

    assert "<title>Contract award list</title>" in clean
    assert "<h1>Ministry &amp; contractors</h1>" in clean
    assert "<b>4.2m</b>" in clean
    assert '<a title="https://example.org/acme">Acme</a> &lt;Ltd&gt;' in clean
    assert '<td colspan="2">2026</td>' in clean
    assert "<pre>  spaced\n  text</pre>" in clean
    assert "<li>one" in clean and "<li>two" in clean
    assert "href" not in clean and "stylesheet" not in clean


def test_archive_escapes_text_that_looks_like_markup():
    clean = _sanitize_for_archive(
        "<textarea><img src=x onerror=alert(1)></textarea><title><b>x</b></title>"
    )

    assert "<img" not in clean
    assert "&lt;img src=x onerror=alert(1)&gt;" in clean


def test_archive_drops_embedded_documents_with_their_contents():
    clean = _sanitize_for_archive(
        "<p>kept</p><svg><text>svg label</text></svg><math><mi>x</mi></math>"
        "<script>var a=1</script><style>p{}</style><template><p>tpl</p></template>"
        "<svg><svg></svg><text>nested</text></svg><p>after</p>"
    )

    assert "<body><p>kept</p><p>after</p></body>" in clean


def test_archive_of_unclosed_and_malformed_markup_is_well_nested():
    clean = _sanitize_for_archive("<div><p><b>bold<i>both</div>tail</p></span><svg><p>gone")

    assert "<body><div><p><b>bold<i>both</i></b></p></div>tail</body>" in clean


def test_extracted_text_is_unchanged_by_the_archive_sanitizer():
    page = "<html><body><article><p>Real content here</p></article></body></html>"

    text, _title = _extract_content(page)

    assert text == "Real content here"
    assert "Real content here" in _sanitize_for_archive(page)


# -- In a real browser ------------------------------------------------------


def _chromium_executable() -> str | None:
    """A Chromium build to drive, when Playwright's own build is not installed."""
    explicit = os.environ.get("OPENFOIA_TEST_CHROMIUM")
    if explicit:
        return explicit
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for pattern in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-linux/chrome"):
        found = sorted(root.glob(pattern))
        if found:
            return str(found[-1])
    return None


@pytest.fixture(scope="module")
def chromium():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    browser = None
    errors = []
    for executable in (None, _chromium_executable()):
        try:
            browser = pw.chromium.launch(headless=True, executable_path=executable)
            break
        except Exception as exc:  # any launch failure means "no browser here"
            errors.append(str(exc).splitlines()[0])
    if browser is None:
        pw.stop()
        pytest.skip(f"no Chromium for Playwright: {errors}")
    yield browser
    browser.close()
    pw.stop()


def _open_in_browser(browser, path: Path) -> tuple[list[str], dict[str, str | None]]:
    """Open *path* from disk; return (requests that reached the network stage,
    every request the page attempted -> its failure reason).

    Every request is intercepted and aborted, so nothing leaves the machine.
    A request the Content-Security-Policy refuses is still announced by
    Chromium (with failure "csp") but never reaches interception.
    """
    own = path.as_uri()
    reached: list[str] = []
    attempted: dict[str, str | None] = {}
    context = browser.new_context()

    def intercept(route):
        url = route.request.url
        if url == own:
            route.continue_()
        else:
            reached.append(url)
            route.abort()

    context.route("**/*", intercept)
    context.on("request", lambda req: req.url == own or attempted.setdefault(req.url, None))
    context.on("requestfailed", lambda req: attempted.__setitem__(req.url, req.failure))
    page = context.new_page()
    try:
        page.goto(own, wait_until="load", timeout=10_000)
        page.wait_for_timeout(400)
    except Exception as exc:  # a navigation away is a finding, not an error
        reached.append(f"navigation error: {str(exc).splitlines()[0]}")
    finally:
        context.close()
    return sorted(set(reached)), attempted


@pytest.mark.parametrize("name", sorted(AUDIT_PAYLOADS))
def test_unsanitized_audit_payload_does_reach_the_network(chromium, tmp_path, name):
    """Positive control: the harness does see these requests when they happen."""
    path = tmp_path / f"{name}.raw.html"
    path.write_text("<p>report</p>" + AUDIT_PAYLOADS[name], encoding="utf-8")

    reached, _ = _open_in_browser(chromium, path)
    assert any("beacon.example" in url for url in reached)


@pytest.mark.parametrize("name", sorted(ALL_PAYLOADS))
def test_archived_payload_makes_no_request_in_chromium(chromium, tmp_path, name):
    path = tmp_path / f"{name}.html"
    path.write_text(_sanitize_for_archive("<p>report</p>" + ALL_PAYLOADS[name]), encoding="utf-8")

    reached, attempted = _open_in_browser(chromium, path)
    # Not even an attempt for the CSP to refuse: the markup itself is inert.
    assert reached == []
    assert attempted == {}


def test_csp_alone_blocks_subresources_in_chromium(chromium, tmp_path):
    """Defense in depth: even markup that slipped through would be refused."""
    path = tmp_path / "csp.html"
    path.write_text(
        "<!DOCTYPE html>\n" + ARCHIVE_CSP_META + f'<img src="{B}/x.png">'
        f"<script>fetch('{B}/y')</script><link rel=stylesheet href='{B}/z.css'>",
        encoding="utf-8",
    )

    reached, attempted = _open_in_browser(chromium, path)
    assert reached == []
    assert attempted and set(attempted.values()) == {"csp"}, attempted
