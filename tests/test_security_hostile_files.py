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


# ---------------------------------------------------------------------------
# Images (A11): bounded memory, and every metadata block goes
# ---------------------------------------------------------------------------

SOURCE = "Jane Source"


@pytest.fixture
def pil():
    return pytest.importorskip("PIL.Image")


def test_huge_canvas_is_refused_before_any_pixel_is_decoded(pil, tmp_path, monkeypatch):
    from openfoia.pipeline import metadata

    path = tmp_path / "canvas.png"
    pil.new("RGB", (300, 200), (255, 255, 255)).save(path)
    before = path.read_bytes()
    monkeypatch.setattr(metadata, "MAX_IMAGE_PIXELS", 300 * 200 - 1)

    def no_decode(self, *a, **k):
        raise AssertionError("decoded an image over the pixel cap")

    monkeypatch.setattr(pil.Image, "load", no_decode)
    monkeypatch.setattr(pil.Image, "copy", no_decode)

    result = metadata.strip_metadata(path)

    assert result["status"] == "failed"
    assert result["error"] == (
        "image too large to clean safely (300x200 pixels, over 0 megapixels)"
    )
    assert metadata.describe(result)[0] is False
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_pillow_decompression_bomb_is_a_failure(pil, tmp_path, monkeypatch):
    from openfoia.pipeline.metadata import describe, strip_metadata

    path = tmp_path / "bomb.png"
    pil.new("L", (100, 100)).save(path)
    monkeypatch.setattr(pil, "MAX_IMAGE_PIXELS", 1000)  # 2x this raises at open

    result = strip_metadata(path)

    assert result["status"] == "failed"
    assert describe(result) == (False, "metadata NOT stripped: image too large to clean safely")


def test_image_at_the_cap_is_still_cleaned(pil, tmp_path, monkeypatch):
    from openfoia.pipeline import metadata

    path = tmp_path / "ok.png"
    pil.new("RGB", (300, 200)).save(path)
    monkeypatch.setattr(metadata, "MAX_IMAGE_PIXELS", 300 * 200)

    assert metadata.strip_metadata(path)["status"] == "stripped"


def _jpeg_with_every_block(pil, path: Path) -> Path:
    from PIL import ImageCms

    exif = pil.Exif()
    exif[0x013B] = SOURCE  # Artist
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    pil.radial_gradient("L").convert("RGB").save(
        path,
        "JPEG",
        exif=exif.tobytes(),
        comment=f"scanned by {SOURCE}".encode(),
        xmp=f"<x:xmpmeta><dc:creator>{SOURCE}</dc:creator></x:xmpmeta>".encode(),
        icc_profile=icc,
    )
    reopened = pil.open(path)
    assert {"exif", "comment", "xmp", "icc_profile"} <= set(reopened.info)
    return path


def test_jpeg_comment_xmp_and_icc_are_removed_and_listed(pil, tmp_path):
    from openfoia.pipeline.metadata import strip_metadata

    path = _jpeg_with_every_block(pil, tmp_path / "photo.jpg")

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert {"Artist", "comment", "xmp", "icc_profile"} <= set(result["stripped"])
    assert SOURCE.encode() not in path.read_bytes()
    assert not {"exif", "comment", "xmp", "icc_profile"} & set(pil.open(path).info)


def test_png_text_chunks_and_exif_are_removed(pil, tmp_path):
    from PIL.PngImagePlugin import PngInfo

    from openfoia.pipeline.metadata import strip_metadata

    path = tmp_path / "shot.png"
    text = PngInfo()
    text.add_text("Author", SOURCE)
    text.add_itxt("Description", f"phone of {SOURCE}")
    exif = pil.Exif()
    exif[0x013B] = SOURCE
    pil.new("RGB", (40, 40)).save(path, pnginfo=text, exif=exif.tobytes())
    assert path.read_bytes().count(SOURCE.encode()) >= 2

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert {"Author", "Description"} <= set(result["stripped"])
    assert SOURCE.encode() not in path.read_bytes()


def test_tiff_tags_are_not_copied_into_the_clean_file(pil, tmp_path):
    """Pillow re-writes an opened TIFF's XMP/IPTC/Photoshop tags on save."""
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    from openfoia.pipeline.metadata import strip_metadata

    path = tmp_path / "scan.tif"
    ifd = ImageFileDirectory_v2()
    ifd[315] = SOURCE  # Artist
    ifd[700] = f"<dc:creator>{SOURCE}</dc:creator>".encode()  # XMP
    pil.new("RGB", (40, 40)).save(path, tiffinfo=ifd)
    assert SOURCE.encode() in path.read_bytes()

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert SOURCE.encode() not in path.read_bytes()


def test_palette_image_keeps_its_colours(pil, tmp_path):
    from openfoia.pipeline.metadata import strip_metadata

    path = tmp_path / "chart.png"
    img = pil.new("P", (4, 1))
    img.putpalette([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9] + [0] * (256 * 3 - 12))
    img.putdata([0, 1, 2, 3])
    img.save(path)

    assert strip_metadata(path)["status"] == "stripped"
    clean = pil.open(path).convert("RGB")
    assert [clean.getpixel((x, 0)) for x in range(4)] == [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (9, 9, 9),
    ]


# ---------------------------------------------------------------------------
# PDF (A4): nothing that names a person survives, or the result says partial
# ---------------------------------------------------------------------------


def _pdf_page_writer():
    from pypdf import PdfWriter

    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    return writer, page


def _write_pdf(writer, path: Path) -> Path:
    with open(path, "wb") as f:
        writer.write(f)
    return path


def _rect(*values):
    from pypdf.generic import ArrayObject, FloatObject

    return ArrayObject([FloatObject(v) for v in values])


def _pdf_dict(**entries):
    """DictionaryObject from keyword names (``Type="/Annot"`` -> /Type /Annot)."""
    from pypdf.generic import DictionaryObject, NameObject, PdfObject, TextStringObject

    out = DictionaryObject()
    for key, value in entries.items():
        if isinstance(value, str):
            value = NameObject(value) if value.startswith("/") else TextStringObject(value)
        assert isinstance(value, PdfObject)
        out[NameObject(f"/{key}")] = value
    return out


def _jpeg_with_gps(pil) -> bytes:
    import io

    exif = pil.Exif()
    exif[0x013B] = SOURCE  # Artist
    exif[0x8825] = {1: "N", 2: (41.0, 0.0, 51.0)}  # GPS IFD: latitude
    buf = io.BytesIO()
    pil.new("RGB", (16, 16), (200, 0, 0)).save(
        buf,
        "JPEG",
        exif=exif.tobytes(),
        comment=f"by {SOURCE}".encode(),
        xmp=f"<x:xmpmeta><dc:creator>{SOURCE}</dc:creator></x:xmpmeta>".encode(),
    )
    data = buf.getvalue()
    assert SOURCE.encode() in data and b"Exif" in data
    return data


def _image_xobject(writer, page, data: bytes, filters, width=16, height=16):
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        StreamObject,
    )

    img = StreamObject()
    img._data = data
    img.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(width),
            NameObject("/Height"): NumberObject(height),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Filter"): (
                ArrayObject([NameObject(f) for f in filters])
                if len(filters) > 1
                else NameObject(filters[0])
            ),
        }
    )
    xmp = DecodedStreamObject()
    xmp.set_data(f"<x:xmpmeta><dc:creator>{SOURCE} (image XMP)</dc:creator></x:xmpmeta>".encode())
    img[NameObject("/Metadata")] = writer._add_object(xmp)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(img)})}
    )
    content = DecodedStreamObject()
    content.set_data(b"q 100 0 0 100 0 0 cm /Im0 Do Q")
    page[NameObject("/Contents")] = writer._add_object(content)


def test_pdf_info_dictionary_goes_whole_custom_keys_included(tmp_path):
    from pypdf import PdfReader

    from openfoia.pipeline.metadata import describe, strip_metadata

    writer, _ = _pdf_page_writer()
    writer.add_metadata({"/Author": SOURCE, "/Company": "Leak Unit", "/SourcePhone": "+90 555"})
    path = _write_pdf(writer, tmp_path / "info.pdf")

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert {"/Author", "/Company", "/SourcePhone", "/Producer"} <= set(result["stripped"])
    assert not PdfReader(str(path)).metadata
    raw = path.read_bytes()
    assert b"/Info" not in raw and b"Leak Unit" not in raw and b"+90 555" not in raw
    assert describe(result)[0]


def test_pdf_annotation_and_popup_authors_and_dates_go_form_fields_stay(tmp_path):
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, NameObject

    from openfoia.pipeline.metadata import strip_metadata

    writer, page = _pdf_page_writer()
    note = _pdf_dict(
        Type="/Annot",
        Subtype="/Text",
        Rect=_rect(10, 10, 30, 30),
        T=SOURCE,
        M="D:20260101120000+03'00'",
        CreationDate="D:20260101110000+03'00'",
        Contents="reviewed",
    )
    popup = _pdf_dict(Type="/Annot", Subtype="/Popup", Rect=_rect(40, 40, 90, 90), T=SOURCE)
    widget = _pdf_dict(
        Type="/Annot", Subtype="/Widget", FT="/Tx", T="case_number", Rect=_rect(1, 1, 9, 9)
    )
    note_ref, popup_ref = writer._add_object(note), writer._add_object(popup)
    note[NameObject("/Popup")] = popup_ref
    page[NameObject("/Annots")] = ArrayObject([note_ref, popup_ref, writer._add_object(widget)])
    path = _write_pdf(writer, tmp_path / "annots.pdf")

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert {
        "annotation author (/T)",
        "annotation creation date",
        "annotation modification date",
    } <= set(result["stripped"])
    raw = path.read_bytes()
    assert SOURCE.encode() not in raw and b"+03" not in raw
    annots = [a.get_object() for a in PdfReader(str(path)).pages[0]["/Annots"]]
    assert annots[0]["/Contents"] == "reviewed"  # the comment itself is content
    assert annots[2]["/T"] == "case_number"  # a form field's /T is its name


def test_pdf_xmp_and_private_data_are_gone_from_the_file_not_just_unlinked(tmp_path):
    """pypdf writes every object it holds: removing the reference is not enough."""
    from pypdf.generic import NameObject

    from openfoia.pipeline.metadata import strip_metadata

    writer, page = _pdf_page_writer()
    page[NameObject("/PieceInfo")] = _pdf_dict(
        Illustrator=_pdf_dict(Private=f"{SOURCE} private", LastModified="D:2026")
    )
    page[NameObject("/LastModified")] = _pdf_dict(Private="D:20260101")
    xmp = _pdf_dict()
    page[NameObject("/Metadata")] = writer._add_object(xmp)
    xmp[NameObject("/Who")] = _pdf_dict(Name=f"{SOURCE} page XMP")
    path = _write_pdf(writer, tmp_path / "private.pdf")

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert SOURCE.encode() not in path.read_bytes()


def test_pdf_embedded_jpeg_loses_exif_gps_and_xmp_and_still_decodes(pil, tmp_path):
    import io

    from pypdf import PdfReader

    from openfoia.pipeline.metadata import strip_metadata

    writer, page = _pdf_page_writer()
    _image_xobject(writer, page, _jpeg_with_gps(pil), ["/DCTDecode"])
    path = _write_pdf(writer, tmp_path / "scan.pdf")

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert "EXIF/XMP in embedded JPEG images" in result["stripped"]
    assert "XMP" in result["stripped"]
    raw = path.read_bytes()
    assert SOURCE.encode() not in raw and b"Exif" not in raw
    image = PdfReader(str(path)).pages[0]["/Resources"]["/XObject"]["/Im0"]
    decoded = pil.open(io.BytesIO(image._data))
    assert decoded.size == (16, 16) and "exif" not in decoded.info
    red, green, blue = decoded.convert("RGB").getpixel((8, 8))
    assert red > 180 and green < 30 and blue < 30


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (["/JPXDecode"], "embedded JPEG 2000 image (metadata boxes not checked)"),
        (["/FlateDecode", "/DCTDecode"], "embedded JPEG image under a second filter (not checked)"),
    ],
)
def test_pdf_image_that_cannot_be_checked_makes_it_partial(tmp_path, filters, expected):
    from openfoia.pipeline.metadata import describe, strip_metadata

    writer, page = _pdf_page_writer()
    _image_xobject(writer, page, b"\x00\x00\x00\x0cjP  \r\n\x87\n", filters)
    path = _write_pdf(writer, tmp_path / "jpx.pdf")

    result = strip_metadata(path)

    assert result["status"] == "partial"
    assert result["remaining"] == [expected]
    assert describe(result)[0] is False


def test_pdf_jpeg_that_cannot_be_parsed_makes_it_partial(tmp_path):
    from openfoia.pipeline.metadata import strip_metadata

    writer, page = _pdf_page_writer()
    _image_xobject(writer, page, b"\xff\xd8\xff\xe1\x00\x10Exif truncated", ["/DCTDecode"])
    path = _write_pdf(writer, tmp_path / "cut.pdf")

    result = strip_metadata(path)

    assert result["status"] == "partial"
    assert result["remaining"] == ["embedded JPEG image (could not be checked)"]


def test_pdf_file_attachment_annotation_makes_it_partial(tmp_path):
    from pypdf.generic import ArrayObject, DecodedStreamObject, NameObject

    from openfoia.pipeline.metadata import describe, strip_metadata

    writer, page = _pdf_page_writer()
    embedded = DecodedStreamObject()
    embedded.set_data(f"spreadsheet written by {SOURCE}".encode())
    embedded[NameObject("/Type")] = NameObject("/EmbeddedFile")
    spec = _pdf_dict(Type="/Filespec", F="sheet.xlsx")
    spec[NameObject("/EF")] = _pdf_dict(F=writer._add_object(embedded))
    annot = _pdf_dict(Type="/Annot", Subtype="/FileAttachment", Rect=_rect(1, 1, 9, 9))
    annot[NameObject("/FS")] = spec
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(annot)])
    path = _write_pdf(writer, tmp_path / "attach.pdf")

    result = strip_metadata(path)

    assert result["status"] == "partial"
    assert "attached file (not cleaned)" in result["remaining"]
    clean, note = describe(result)
    assert not clean and "attached file" in note


def test_pdf_earlier_revision_is_not_left_in_the_file(tmp_path):
    """An incremental save keeps the old /Info in the file. The old code saw no
    sensitive key in the latest revision and returned without rewriting."""
    from pypdf import PdfReader, PdfWriter

    from openfoia.pipeline.metadata import strip_metadata

    writer, _ = _pdf_page_writer()
    writer.add_metadata({"/Author": SOURCE})
    path = _write_pdf(writer, tmp_path / "revised.pdf")
    update = PdfWriter(str(path), incremental=True)
    update.metadata = {"/Title": "Report"}
    with open(path, "ab") as f:
        update.write(f)
    assert SOURCE.encode() in path.read_bytes()

    strip_metadata(path)

    assert SOURCE.encode() not in path.read_bytes()
    assert len(PdfReader(str(path)).pages) == 1


def test_password_protected_pdf_is_a_failure(tmp_path):
    from openfoia.pipeline.metadata import describe, strip_metadata

    writer, _ = _pdf_page_writer()
    writer.add_metadata({"/Author": SOURCE})
    writer.encrypt(user_password="secret", owner_password="owner")
    path = _write_pdf(writer, tmp_path / "locked.pdf")
    before = path.read_bytes()

    result = strip_metadata(path)

    assert result["status"] == "failed"
    assert describe(result) == (False, "metadata NOT stripped: the PDF is password protected")
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# DOCX (A4): every part Word puts names, dates and ids in
# ---------------------------------------------------------------------------

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _docx_members(path: Path) -> dict[str, bytes]:
    import zipfile

    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def _write_docx(path: Path, members: dict[str, bytes]) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _add_part(members, name, data, content_type=None, rel_type=None, owner="word/document.xml"):
    """Add a part with its content-type override and a relationship from *owner*."""
    import posixpath

    members[name] = data if isinstance(data, bytes) else data.encode()
    if content_type:
        ct = members["[Content_Types].xml"].decode()
        override = f'<Override PartName="/{name}" ContentType="{content_type}"/>'
        members["[Content_Types].xml"] = ct.replace("</Types>", override + "</Types>").encode()
    if rel_type:
        folder, base = posixpath.split(owner)
        rels_name = posixpath.join(folder, "_rels", base + ".rels")
        rels = members.get(rels_name, b"").decode() or (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            "</Relationships>"
        )
        target = posixpath.relpath(name, folder or ".")
        rel = f'<Relationship Id="rIdT{len(rels)}" Type="{REL}/{rel_type}" Target="{target}"/>'
        members[rels_name] = rels.replace("</Relationships>", rel + "</Relationships>").encode()


def _hostile_docx(path: Path) -> Path:
    """A .docx carrying the reviewer's name, account, initials, dates and paths
    in every place Word puts them."""
    from docx import Document

    doc = Document()
    doc.add_paragraph("memo body")
    doc.core_properties.author = SOURCE
    doc.core_properties.last_modified_by = SOURCE
    doc.save(str(path))
    members = _docx_members(path)

    body = (
        f'<w:p w:rsidR="00AB12CD" w:rsidRDefault="00AB12CD">'
        f'<w:ins w:id="1" w:author="{SOURCE}" w:date="2026-01-01T09:00:00Z" '
        f'xmlns:w16du="http://schemas.microsoft.com/office/word/2023/wordml/word16du" '
        f'w16du:dateUtc="2026-01-01T06:00:00Z"><w:r><w:t>inserted</w:t></w:r></w:ins>'
        f'<w:del w:id="2" w:author="{SOURCE}" w:date="2026-01-02T09:00:00Z">'
        f"<w:r><w:delText>removed</w:delText></w:r></w:del>"
        f'<w:commentRangeStart w:id="0"/><w:r><w:t>commented</w:t></w:r>'
        f'<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:p>'
        # A hostile file may bind any prefix to the WordprocessingML namespace.
        f'<x:p xmlns:x="{W}"><x:ins x:id="3" x:author="{SOURCE}"><x:r><x:t>also</x:t>'
        f"</x:r></x:ins></x:p>"
    )
    document = members["word/document.xml"].decode()
    members["word/document.xml"] = document.replace("<w:sectPr", body + "<w:sectPr", 1).encode()

    _add_part(
        members,
        "word/comments.xml",
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:comments xmlns:w="{W}">'
        f'<w:comment w:id="0" w:author="{SOURCE}" w:initials="JS" '
        f'w:date="2026-01-03T09:00:00Z"><w:p><w:r><w:t>check this</w:t></w:r></w:p>'
        f"</w:comment></w:comments>",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
        "comments",
    )
    _add_part(
        members,
        "word/people.xml",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
        f'<w15:person w15:author="{SOURCE}"><w15:presenceInfo w15:providerId="AD" '
        'w15:userId="S::jsource@interior.example::0b1c"/></w15:person></w15:people>',
        "application/vnd.openxmlformats-officedocument.wordprocessingml.people+xml",
        "../../schemas.microsoft.com/office/2011/relationships/people",
    )
    settings = members["word/settings.xml"].decode()
    settings = settings.replace(
        "</w:settings>",
        '<w:attachedTemplate r:id="rIdTpl"/><w:docVars><w:docVar w:name="reviewer" '
        f'w:val="{SOURCE}"/></w:docVars></w:settings>',
    )
    if "xmlns:r=" not in settings.split(">", 2)[1]:
        settings = settings.replace("<w:settings ", f'<w:settings xmlns:r="{REL}" ', 1)
    members["word/settings.xml"] = settings.encode()
    members["word/_rels/settings.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rIdTpl" Type="{REL}/attachedTemplate" '
        'Target="file:///C:\\Users\\jsource\\AppData\\Roaming\\Microsoft\\Templates\\Leak.dotm" '
        'TargetMode="External"/></Relationships>'
    ).encode()
    _add_part(
        members,
        "docProps/custom.xml",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns='
        '"http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="Desk">'
        f"<vt:lpwstr>{SOURCE}</vt:lpwstr></property></Properties>",
        "application/vnd.openxmlformats-officedocument.custom-properties+xml",
        "custom-properties",
        owner="",
    )
    members["customXml/item2.xml"] = f"<sp><Owner>{SOURCE}</Owner></sp>".encode()
    app = members["docProps/app.xml"].decode()
    app = app.replace("<Company/>", "<Company>Ministry of Interior</Company>")
    members["docProps/app.xml"] = app.replace("<Manager/>", "<Manager>J. Source</Manager>").encode()
    _write_docx(path, members)
    blob = b"".join(members.values())
    assert blob.count(SOURCE.encode()) >= 8 and b"jsource" in blob
    return path


def test_docx_revisions_comments_reviewers_and_properties_are_cleaned(tmp_path):
    from docx import Document

    from openfoia.pipeline.metadata import describe, strip_metadata

    path = _hostile_docx(tmp_path / "memo.docx")

    result = strip_metadata(path)

    assert result["status"] == "stripped", result
    assert {
        "author",
        "last_modified_by",
        "company",
        "manager",
        "custom_properties",
        "custom XML data (customXml)",
        "reviewer accounts (people.xml)",
        "thumbnail",
        "revision and comment authors",
        "revision and comment dates",
        "revision and comment initials",
        "rsid",
        "attached template path",
        "document variables",
        "document id",
    } <= set(result["stripped"])
    members = _docx_members(path)
    for name, data in members.items():
        for needle in (SOURCE.encode(), b"jsource", b"J. Source", b"Ministry", b"2026-01-0"):
            assert needle not in data, (name, needle)
        assert b"rsidR" not in data, name
    assert not any(n.lower().startswith(("customxml/", "docprops/thumbnail")) for n in members)
    assert "word/people.xml" not in members and "docProps/custom.xml" not in members
    blob = b"".join(members.values())
    assert b"people.xml" not in blob and b"custom.xml" not in blob and b"customXml" not in blob
    # Content stays: the tracked insertion, the deletion and the comment text.
    assert b"inserted" in blob and b"removed" in blob and b"check this" in blob
    assert any("memo body" in p.text for p in Document(str(path)).paragraphs)
    assert describe(result)[0]


def test_docx_jpeg_and_png_media_lose_exif_gps_and_text(pil, tmp_path):
    import io

    from PIL.PngImagePlugin import PngInfo

    from openfoia.pipeline.metadata import strip_metadata

    path = tmp_path / "photos.docx"
    from docx import Document

    Document().save(str(path))
    members = _docx_members(path)
    text = PngInfo()
    text.add_text("Author", SOURCE)
    png = io.BytesIO()
    pil.new("RGB", (8, 8), (0, 0, 255)).save(png, "PNG", pnginfo=text)
    _add_part(members, "word/media/image1.jpeg", _jpeg_with_gps(pil), rel_type="image")
    _add_part(members, "word/media/image2.png", png.getvalue(), "image/png", rel_type="image")
    _write_docx(path, members)

    result = strip_metadata(path)

    assert result["status"] == "stripped", result
    assert {
        "image metadata in word/media/image1.jpeg",
        "image metadata in word/media/image2.png",
    } <= set(result["stripped"])
    members = _docx_members(path)
    for name in ("word/media/image1.jpeg", "word/media/image2.png"):
        data = members[name]
        assert SOURCE.encode() not in data and b"Exif" not in data, name
        image = pil.open(io.BytesIO(data))
        image.load()
        assert image.size in {(16, 16), (8, 8)} and "exif" not in image.info


def test_docx_media_that_cannot_be_checked_makes_it_partial(tmp_path):
    from openfoia.pipeline.metadata import describe, strip_metadata

    path = _hostile_docx(tmp_path / "chart.docx")
    members = _docx_members(path)
    _add_part(members, "word/media/image1.emf", f"EMF by {SOURCE}".encode(), rel_type="image")
    _add_part(members, "word/embeddings/sheet.xlsx", b"PK\x03\x04", rel_type="package")
    _write_docx(path, members)

    result = strip_metadata(path)

    assert result["status"] == "partial"
    assert result["remaining"] == [
        "embedded object word/embeddings/sheet.xlsx (not checked)",
        "word/media/image1.emf (not checked)",
    ] or set(result["remaining"]) == {
        "embedded object word/embeddings/sheet.xlsx (not checked)",
        "word/media/image1.emf (not checked)",
    }
    clean, note = describe(result)
    assert not clean and "image1.emf" in note
    # What could be cleaned still was.
    assert SOURCE.encode() not in _docx_members(path)["word/document.xml"]


def test_docx_picture_source_path_is_removed(tmp_path):
    from openfoia.pipeline.metadata import strip_metadata

    path = tmp_path / "pic.docx"
    from docx import Document

    Document().save(str(path))
    members = _docx_members(path)
    wp = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    pic = (
        f'<w:p><w:r><w:drawing><wp:inline xmlns:wp="{wp}"><wp:docPr id="1" name="Picture 1" '
        'descr="C:\\Users\\jsource\\Pictures\\IMG_2034.JPG"/></wp:inline></w:drawing></w:r></w:p>'
    )
    doc = members["word/document.xml"].decode()
    members["word/document.xml"] = doc.replace("<w:sectPr", pic + "<w:sectPr", 1).encode()
    _write_docx(path, members)

    result = strip_metadata(path)

    assert "local file paths of inserted pictures" in result["stripped"]
    data = _docx_members(path)["word/document.xml"]
    assert b"jsource" not in data and b'name="Picture 1"' in data


def test_docx_zip_timestamps_and_extra_fields_are_reset(tmp_path):
    import zipfile

    from openfoia.pipeline.metadata import strip_metadata

    path = tmp_path / "stamped.docx"
    from docx import Document

    Document().save(str(path))
    with zipfile.ZipFile(path) as z:
        assert {i.date_time for i in z.infolist()} != {(1980, 1, 1, 0, 0, 0)}

    strip_metadata(path)

    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
    assert {i.date_time for i in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert all(i.extra == b"" and i.create_system == 0 for i in infos)
    assert infos[0].filename == "[Content_Types].xml"


def test_zip_that_is_not_a_word_document_is_a_failure(tmp_path):
    from openfoia.pipeline.metadata import strip_metadata

    path = _write_docx(tmp_path / "other.docx", {"notes.txt": f"by {SOURCE}".encode()})

    result = strip_metadata(path)

    assert result["status"] == "failed"
    assert result["error"] == "could not open the file as a DOCX"


# ---------------------------------------------------------------------------
# DOCX zip bombs: refused from the central directory, before decompression
# ---------------------------------------------------------------------------


def _docx_with_big_media(path: Path, size: int) -> Path:
    """A .docx whose document relationships reference a large, highly
    compressible image part (zeros): the part is read by anything that loads
    the package, not only by code that walks every member."""
    import zipfile

    from docx import Document

    Document().save(str(path))
    members = _docx_members(path)
    _add_part(members, "word/media/image1.png", b"", rel_type="image")
    ct = members["[Content_Types].xml"].decode()
    members["[Content_Types].xml"] = ct.replace(
        "<Default ", '<Default Extension="png" ContentType="image/png"/><Default ', 1
    ).encode()
    del members["word/media/image1.png"]
    _write_docx(path, members)
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("word/media/image1.png")
        info.compress_type = zipfile.ZIP_DEFLATED
        with z.open(info, "w", force_zip64=True) as f:
            f.write(_PNG_SIG)
            chunk = b"\0" * 2**20
            for _ in range(size // 2**20):
                f.write(chunk)
    return path


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _no_member_reads(monkeypatch):
    import zipfile

    def refuse(self, *args, **kwargs):
        raise AssertionError("a member was decompressed")

    monkeypatch.setattr(zipfile.ZipFile, "open", refuse)


def test_docx_with_a_referenced_zip_bomb_part_is_refused_before_reading(tmp_path, monkeypatch):
    from openfoia.pipeline.metadata import describe, strip_metadata

    path = _docx_with_big_media(tmp_path / "bomb.docx", 64 * 2**20)  # 64 MiB of zeros
    assert path.stat().st_size < 2**20
    before = path.read_bytes()
    _no_member_reads(monkeypatch)

    result = strip_metadata(path)

    assert result["status"] == "failed"
    assert result["error"] == (
        "too large to clean safely: archive member expands too much (zip bomb)"
    )
    assert describe(result)[0] is False
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("limit", "value", "error"),
    [
        ("MAX_ZIP_MEMBER_BYTES", 1000, "archive member expands to 0 MiB"),
        ("MAX_ZIP_TOTAL_BYTES", 10_000, "archive expands to 0 MiB"),
        ("MAX_ZIP_MEMBERS", 3, "archive has too many members"),
    ],
)
def test_docx_over_a_directory_limit_is_refused(tmp_path, monkeypatch, limit, value, error):
    from openfoia.pipeline import metadata

    path = _hostile_docx(tmp_path / "big.docx")
    monkeypatch.setattr(metadata, limit, value)
    _no_member_reads(monkeypatch)

    result = metadata.strip_metadata(path)

    assert result["status"] == "failed"
    assert result["error"].startswith(f"too large to clean safely: {error}")


def test_docx_with_two_members_of_the_same_name_is_refused(tmp_path):
    import warnings
    import zipfile

    from openfoia.pipeline.metadata import strip_metadata

    path = _hostile_docx(tmp_path / "dup.docx")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "a") as z:
            z.writestr("Word/Document.xml", b"<w:document/>")

    result = strip_metadata(path)

    assert result["status"] == "failed"
    assert "same name" in result["error"]


def test_large_ordinary_docx_part_is_still_cleaned(tmp_path):
    """The ratio check only applies past 1 MiB; ordinary XML is well under it."""
    from openfoia.pipeline.metadata import strip_metadata

    path = _hostile_docx(tmp_path / "long.docx")
    members = _docx_members(path)
    paragraphs = "".join(
        f"<w:p><w:r><w:t>Paragraph {i} of the long annex, item {i * 7919 % 10007}.</w:t></w:r></w:p>"
        for i in range(40_000)
    )
    doc = members["word/document.xml"].decode()
    members["word/document.xml"] = doc.replace("<w:sectPr", paragraphs + "<w:sectPr", 1).encode()
    assert len(members["word/document.xml"]) > 2 * 2**20
    _write_docx(path, members)

    assert strip_metadata(path)["status"] == "stripped"
