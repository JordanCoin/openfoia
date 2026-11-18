"""Rendered tests for the `openfoia analyze graph` page.

The page is generated through the real CLI from the fixture in
``tests/graph_fixture.py`` and opened in headless Chromium through Playwright.
The fixture plants three strands with one main hub each and four bridges, so
the usefulness checks have a known right answer.

The browser tests skip when the ``playwright`` package or a Chromium binary is
missing. Chromium is taken from ``OPENFOIA_TEST_CHROMIUM``, or from
``/opt/pw-browsers/chromium`` when that file exists. Nothing is downloaded.
The ``--no-text`` test needs no browser and always runs.

"Default zoom" below means the view the page opens with: it fits the graph to
the window once the layout has settled. All measurements are taken at a
1366x768 window, device pixel ratio 1, so canvas pixels are CSS pixels.

Set ``OPENFOIA_GRAPH_METRICS_OUT`` to a file path to get every measured value
as JSON next to its threshold.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
import webbrowser
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from tests.graph_fixture import BODY_SENTINEL, NON_LATIN, Spec, build_spec, load_spec

DEFAULT_CHROMIUM = "/opt/pw-browsers/chromium"
VIEWPORT = {"width": 1366, "height": 768}

# --- Thresholds. Each is stated with the reason for the number. ---------------
# The cooling schedule is 300 ticks. On the development container the 260-entity
# page stops in about 0.25 s and the 520-entity page in about 0.6 s. The budgets
# leave room for a machine roughly ten times slower.
SETTLE_BUDGET_S = 5.0
LARGE_SETTLE_BUDGET_S = 10.0
# Marks are kept apart by the collision force, so the expected count is 0. One
# percent of the node count allows for a stray pair without letting a pile-up pass.
MAX_NODE_OVERLAP_FRACTION = 0.01
# Body text on the web is rarely set below 11 px; smaller than that is hard to
# read on a cheap 1366x768 laptop panel. The page uses 12 px.
MIN_LABEL_FONT_PX = 11
# Names are placed only where they do not cover another name, so 0 is expected.
MAX_LABEL_LABEL_OVERLAPS = 0
# A name may cover another entity's mark only for the few best-connected
# entities, whose names are forced. 5% of the names shown is the allowance.
MAX_LABELS_OVER_FOREIGN_MARKS_FRACTION = 0.05
# A graph with no overlapping names because it has almost no names is not
# readable either: at least 40 of the 260 entities must be named at default zoom.
MIN_LABELS_AT_DEFAULT_ZOOM = 40
# Planted strands: the mean distance between two entities of the same strand
# must be at most 60% of the mean distance between entities of different
# strands. A layout with no structure gives a ratio near 1.0.
MAX_INTRA_INTER_RATIO = 0.60
# ...and at least 90% of connected entities must be nearer to the centre of
# their own strand than to the centre of any other. Bridges and entities that
# sit between two hubs account for the rest.
MIN_NEAREST_CENTROID_FRACTION = 0.90
# Unconnected entities must not push the connected graph aside: its centre of
# mass stays within 15% of the free area's width and height from the middle,
# and it keeps at least 70% of the bounding box of everything drawn.
MAX_MAIN_COMPONENT_OFFSET_FRACTION = 0.15
MIN_MAIN_COMPONENT_AREA_FRACTION = 0.70
# De-emphasis is drawn at 18% opacity. A mark counts as dimmed below 35% and as
# fully drawn above 98%; nine in ten sampled marks must agree (names and lines
# cross some of the sample points).
DIMMED_ALPHA_MAX = 0.35 * 255
LIT_ALPHA_MIN = 0.98 * 255
MIN_SAMPLE_AGREEMENT = 0.90

METRICS: dict[str, object] = {}


def _measure(name: str, value, threshold) -> None:
    METRICS[name] = {"measured": value, "threshold": threshold}
    print(f"MEASURED {name}: {value} (threshold: {threshold})")


@pytest.fixture(scope="module", autouse=True)
def _write_metrics():
    yield
    out = os.environ.get("OPENFOIA_GRAPH_METRICS_OUT")
    if out and METRICS:
        Path(out).write_text(json.dumps(METRICS, indent=2, ensure_ascii=False), encoding="utf-8")


# --- Building the pages through the real CLI ----------------------------------


class Export:
    def __init__(self, spec: Spec, directory: Path):
        self.spec = spec
        self.dir = directory

    def path(self, name: str) -> Path:
        return self.dir / name


def _export(tmp: Path, n_entities: int, with_no_text: bool = False) -> Export:
    spec = build_spec(n_entities=n_entities)
    runner = CliRunner()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OPENFOIA_DATA_DIR", str(tmp / "data"))
        mp.delenv("OPENFOIA_DB_PASSWORD", raising=False)
        mp.setattr(webbrowser, "open", lambda *a, **k: True)
        load_spec(spec)
        variants = [("graph", [])]
        if with_no_text:
            variants.append(("notext", ["--no-text"]))
        for name, extra in variants:
            result = runner.invoke(
                app,
                ["analyze", "graph", "--all", "--view", "-o", str(tmp / f"{name}.json"), *extra],
            )
            assert result.exit_code == 0, result.output
    return Export(spec, tmp)


@pytest.fixture(scope="module")
def small(tmp_path_factory) -> Export:
    """260 distinct entities, 319 mentions, 315 relationships, 9 documents."""
    return _export(tmp_path_factory.mktemp("graph-small"), 260, with_no_text=True)


@pytest.fixture(scope="module")
def large(tmp_path_factory) -> Export:
    """520 distinct entities."""
    return _export(tmp_path_factory.mktemp("graph-large"), 520)


# --- Browser ------------------------------------------------------------------


def _chromium_path() -> str | None:
    configured = os.environ.get("OPENFOIA_TEST_CHROMIUM")
    candidate = configured or DEFAULT_CHROMIUM
    return candidate if Path(candidate).exists() else None


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip(
        "playwright.sync_api", reason="playwright is not installed (pip install playwright)"
    )
    path = _chromium_path()
    if path is None:
        pytest.skip(
            "No Chromium found at "
            f"{os.environ.get('OPENFOIA_TEST_CHROMIUM') or DEFAULT_CHROMIUM}. "
            "Set OPENFOIA_TEST_CHROMIUM to a Chromium/Chrome binary."
        )
    pw = sync_api.sync_playwright().start()
    try:
        instance = pw.chromium.launch(executable_path=path)
    except Exception as exc:  # a missing shared library, sandbox refusal, ...
        pw.stop()
        pytest.skip(f"Chromium at {path} could not start: {exc}")
    yield instance
    instance.close()
    pw.stop()


class Opened:
    def __init__(self, page, requests, problems, started):
        self.page = page
        self.requests = requests
        self.problems = problems
        self.started = started

    def snap(self) -> dict:
        return self.page.evaluate("() => window.openfoiaGraph.snapshot()")

    def wait_settled(self, budget_s: float = SETTLE_BUDGET_S) -> float:
        self.page.wait_for_function(
            "() => window.openfoiaGraph.snapshot().settled", timeout=budget_s * 1000
        )
        return time.monotonic() - self.started

    def alpha_at(self, x: float, y: float) -> int:
        """Canvas alpha at a point: 0 where nothing is drawn, 255 for a solid mark."""
        return self.page.evaluate(
            "([x, y]) => document.getElementById('graph').getContext('2d')"
            ".getImageData(Math.round(x), Math.round(y), 1, 1).data[3]",
            [x, y],
        )


@pytest.fixture
def open_graph(browser):
    contexts = []

    def _open(html: Path, viewport: dict | None = None, scheme: str = "dark") -> Opened:
        # offline=True: the browser itself has no network, and anything that is
        # not the file on disk is aborted and recorded. The page follows the
        # system colour scheme; the pixel checks are written for the dark one
        # unless a test asks for light.
        ctx = browser.new_context(
            viewport=viewport or VIEWPORT,
            device_scale_factor=1,
            offline=True,
            color_scheme=scheme,
        )
        contexts.append(ctx)
        ctx.route(lambda url: not url.startswith("file:"), lambda route: route.abort())
        page = ctx.new_page()
        requests: list[str] = []
        problems: list[str] = []
        page.on("request", lambda r: requests.append(r.url))
        page.on(
            "console",
            lambda m: (
                # The readback hint is caused by the tests' own getImageData calls.
                problems.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") and "willReadFrequently" not in m.text
                else None
            ),
        )
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        started = time.monotonic()
        page.goto(html.resolve().as_uri())
        return Opened(page, requests, problems, started)

    yield _open
    for ctx in contexts:
        ctx.close()


def _overlapping_pairs(nodes: list[dict]) -> int:
    vis = [n for n in nodes if n["visible"]]
    count = 0
    for i, a in enumerate(vis):
        for b in vis[i + 1 :]:
            if math.hypot(a["x"] - b["x"], a["y"] - b["y"]) < a["r"] + b["r"]:
                count += 1
    return count


def _rects_intersect(a: dict, b: dict) -> bool:
    return (
        a["x"] < b["x"] + b["w"]
        and a["x"] + a["w"] > b["x"]
        and a["y"] < b["y"] + b["h"]
        and a["y"] + a["h"] > b["y"]
    )


def _dom_rect(page, selector: str) -> dict:
    return page.evaluate(
        "(sel) => { const b = document.querySelector(sel).getBoundingClientRect();"
        " return {x: b.left, y: b.top, w: b.width, h: b.height}; }",
        selector,
    )


def _by_key(snapshot: dict) -> dict[tuple[str, str], dict]:
    return {(n["type"], n["label"]): n for n in snapshot["nodes"]}


def _top_hubs(spec: Spec, count: int = 5) -> list[tuple[str, str]]:
    nb = spec.neighbours()
    return sorted(nb, key=lambda k: (-len(nb[k]), k))[:count]


def _rgb(hex_colour: str) -> list[int]:
    return [int(hex_colour[i : i + 2], 16) for i in (1, 3, 5)]


def _luminance(hex_colour: str) -> float:
    """WCAG relative luminance."""
    channels = []
    for v in _rgb(hex_colour):
        c = v / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# --- Correctness --------------------------------------------------------------


def test_no_text_export_has_no_document_body(small):
    """--no-text keeps entities, links and context snippets but no document body."""
    with_text = small.path("graph.html").read_text(encoding="utf-8")
    assert BODY_SENTINEL in with_text  # control: the default export does embed bodies
    for name in ("notext.html", "notext.json"):
        content = small.path(name).read_text(encoding="utf-8")
        assert BODY_SENTINEL not in content
        assert "The committee reviewed the submitted bids" not in content
    data = json.loads(small.path("notext.json").read_text(encoding="utf-8"))
    assert data["documents"]
    assert all(doc["text"] == "" for doc in data["documents"].values())
    assert len(data["nodes"]) == len(small.spec.mentions)


def test_page_source_has_no_innerhtml_or_external_reference(small):
    html = small.path("graph.html").read_text(encoding="utf-8")
    page_code = (
        html[: html.index("var graphData =")]
        + html[html.index("\n", html.index("var graphData =")) :]
    )
    assert "innerHTML" not in page_code
    assert "outerHTML" not in page_code
    assert "insertAdjacentHTML" not in page_code
    assert "document.write" not in page_code
    assert not re.search(r"https?://", page_code)
    assert not re.search(r"<(?:link|img|iframe|object|embed)\b", page_code)
    assert not re.search(r"<script[^>]*\ssrc\s*=", page_code)
    assert "@import" not in page_code and "url(" not in page_code
    assert "Content-Security-Policy" in page_code and "default-src 'none'" in page_code


def test_loads_with_no_console_errors_and_no_network_requests(small, open_graph):
    html = small.path("graph.html")
    opened = open_graph(html)
    opened.wait_settled()
    # Exercise the interactive paths too; an error in a handler would show up here.
    hub = max(opened.snap()["nodes"], key=lambda n: n["degree"])
    opened.page.mouse.move(hub["x"], hub["y"])
    opened.page.mouse.click(hub["x"], hub["y"])
    opened.page.locator(".open-reader").first.click()
    opened.page.keyboard.press("Escape")
    opened.page.fill("#search-input", "a")
    opened.page.keyboard.press("Enter")
    opened.page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
    assert opened.problems == []
    assert opened.requests == [html.resolve().as_uri()]


def test_simulation_settles_and_stops(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    wall = opened.wait_settled(SETTLE_BUDGET_S)
    first = opened.snap()
    opened.page.wait_for_timeout(700)
    second = opened.snap()
    moved = max(
        max(abs(a["wx"] - b["wx"]), abs(a["wy"] - b["wy"]))
        for a, b in zip(first["nodes"], second["nodes"], strict=True)
    )
    _measure("settle_wall_seconds_260", round(wall, 2), f"<= {SETTLE_BUDGET_S}")
    _measure("settle_page_ms_260", round(first["settleMs"]), f"<= {SETTLE_BUDGET_S * 1000:.0f}")
    _measure("movement_after_settle_px", moved, "== 0")
    assert moved == 0
    assert second["ticks"] == first["ticks"]  # the loop is not still running
    assert second["running"] is False
    assert first["settleMs"] <= SETTLE_BUDGET_S * 1000


def test_layout_is_reproducible(small, open_graph):
    """No random start: opening the same export twice gives the same positions."""
    runs = []
    for _ in range(2):
        opened = open_graph(small.path("graph.html"))
        opened.wait_settled()
        runs.append([(n["label"], n["wx"], n["wy"]) for n in opened.snap()["nodes"]])
    assert runs[0] == runs[1]


def test_dragging_a_node_reheats_then_settles_again(small, open_graph):
    """#58/#125: a drag warms the layout, and it must cool and stop again."""
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    before = opened.snap()
    node = max(before["nodes"], key=lambda n: n["degree"])
    page = opened.page
    page.mouse.move(node["x"], node["y"])
    page.mouse.down()
    page.mouse.move(node["x"] + 60, node["y"] + 40, steps=6)
    assert opened.snap()["running"] is True
    page.mouse.up()
    page.wait_for_function(
        "() => window.openfoiaGraph.snapshot().settled", timeout=SETTLE_BUDGET_S * 1000
    )
    after = opened.snap()
    assert after["ticks"] > before["ticks"]
    page.wait_for_timeout(500)
    assert opened.snap()["ticks"] == after["ticks"]


def test_node_and_edge_counts_match_the_database(small, open_graph):
    spec = small.spec
    exported = json.loads(small.path("graph.json").read_text(encoding="utf-8"))
    assert len(exported["nodes"]) == len(spec.mentions)
    assert len(exported["edges"]) == len(spec.links)

    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    snap = opened.snap()
    # One node per entity. An entity mentioned in three documents is three rows
    # in the database and one node here; the mention count is reported too.
    assert snap["entityCount"] == len(spec.entity_keys) == len(snap["nodes"])
    assert set(_by_key(snap)) == spec.entity_keys
    assert snap["mentionCount"] == len(spec.mentions)
    # Every relationship row is counted. Rows between the same two entities are
    # drawn as one line whose weight is the number of rows.
    assert snap["relationshipCount"] == len(spec.links)
    assert snap["linkWeightTotal"] + snap["selfLoops"] == len(spec.links)
    pairs = {frozenset(p) for k, v in spec.neighbours().items() for p in [(k, o) for o in v]}
    assert snap["linkCount"] == len(pairs)
    stats = opened.page.inner_text("#stats")
    assert f"{len(spec.entity_keys)} entities" in stats
    assert f"{len(spec.mentions)} mentions" in stats
    assert f"{len(spec.links)} relationships" in stats
    _measure("nodes_drawn_vs_entities", snap["entityCount"], f"== {len(spec.entity_keys)}")
    _measure("relationships_vs_rows", snap["relationshipCount"], f"== {len(spec.links)}")


def test_legend_lists_every_type_present(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    shown = opened.page.eval_on_selector_all(
        "#legend .legend-item", "els => els.map(e => e.getAttribute('data-type'))"
    )
    assert set(shown) == small.spec.types
    assert len(shown) == len(small.spec.types)
    counts = opened.page.eval_on_selector_all(
        "#legend .legend-item",
        "els => els.map(e => [e.getAttribute('data-type'), "
        "Number(e.querySelector('.legend-count').textContent)])",
    )
    expected = {t: sum(1 for k in small.spec.entity_keys if k[0] == t) for t in small.spec.types}
    assert dict(counts) == expected
    # Each legend row has a drawn shape swatch, not only a colour name.
    assert opened.page.locator("#legend .legend-item canvas.swatch").count() == len(shown)


def test_type_filter_hides_nodes(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    people = [n for n in opened.snap()["nodes"] if n["type"] == "person"]

    def person_blue(n) -> bool:
        """Is the canvas at this node's centre the solid colour of a person mark?"""
        r, g, b, a = opened.page.evaluate(
            "([x, y]) => Array.from(document.getElementById('graph').getContext('2d')"
            ".getImageData(Math.round(x), Math.round(y), 1, 1).data)",
            [n["x"], n["y"]],
        )
        return a > 200 and math.dist((r, g, b), (0x39, 0x87, 0xE5)) < 40

    drawn_before = sum(person_blue(n) for n in people)
    assert drawn_before >= 0.9 * len(people)

    opened.page.click('#legend .legend-item[data-type="person"]')
    opened.page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
    after = opened.snap()
    assert all(not n["visible"] for n in after["nodes"] if n["type"] == "person")
    assert all(n["visible"] for n in after["nodes"] if n["type"] != "person")
    assert all(lbl["label"] not in {p["label"] for p in people} for lbl in after["labels"])
    # And on the canvas itself: where the person marks were, there is no mark now.
    by_label = {n["label"]: n for n in after["nodes"] if n["type"] == "person"}
    drawn_after = sum(person_blue(n) for n in by_label.values())
    _measure(
        "person_marks_still_drawn_after_hiding",
        f"{drawn_after}/{len(people)} (before: {drawn_before})",
        "== 0",
    )
    assert drawn_after == 0
    assert "Showing" in opened.page.inner_text("#filter-count")

    opened.page.click('#legend .legend-item[data-type="person"]')
    assert all(n["visible"] for n in opened.snap()["nodes"])


def test_minimum_confidence_filter_hides_low_confidence_entities(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    opened.page.fill("#conf-range", "80")
    opened.page.dispatch_event("#conf-range", "input")
    best: dict[tuple[str, str], float] = {}
    for m in small.spec.mentions:
        best[(m.type, m.label)] = max(best.get((m.type, m.label), 0.0), m.confidence)
    expected = {k for k, c in best.items() if c >= 0.8}
    assert 0 < len(expected) < len(best)
    visible = {k for k, n in _by_key(opened.snap()).items() if n["visible"]}
    assert visible == expected


def test_few_node_pairs_overlap_at_default_zoom(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    snap = opened.snap()
    pairs = _overlapping_pairs(snap["nodes"])
    allowed = int(MAX_NODE_OVERLAP_FRACTION * len(snap["nodes"]))
    _measure("overlapping_node_pairs_260", pairs, f"<= {allowed} (1% of {len(snap['nodes'])})")
    assert pairs <= allowed


def _platform_fonts(page, selector: str) -> list[str]:
    """Font families Chromium actually used to draw the element's text."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    cdp.send("CSS.enable")
    root = cdp.send("DOM.getDocument")["root"]["nodeId"]
    node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
    fonts = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]
    cdp.detach()
    return sorted(f["familyName"] for f in fonts)


_GLYPH_PROBE = """(labels) => {
  const family = window.openfoiaGraph.snapshot().labelFont.replace(/^\\S+\\s/, '');
  const c = document.createElement('canvas'); c.width = 48; c.height = 48;
  const g = c.getContext('2d', {willReadFrequently: true});
  g.font = '28px ' + family; g.textBaseline = 'middle'; g.fillStyle = '#fff';
  function bitmap(ch) {
    g.clearRect(0, 0, 48, 48); g.fillText(ch, 6, 24);
    const d = g.getImageData(0, 0, 48, 48).data; let ink = 0, sig = '';
    for (let i = 3; i < d.length; i += 4) { if (d[i] > 40) { ink++; sig += (i >> 2) + ','; } }
    return {ink, sig};
  }
  // Two code points that are not assigned to any character. A font without a
  // glyph draws the same "missing" box for both, and for every missing letter.
  const m1 = bitmap('\\u0378'), m2 = bitmap('\\u0379');
  const out = {};
  for (const label of labels) {
    const chars = Array.from(new Set(Array.from(label).filter(ch => /\\p{L}/u.test(ch))));
    const maps = chars.map(bitmap);
    out[label] = {
      chars: chars.length,
      blank: maps.filter(m => m.ink === 0).length,
      missingBox: maps.filter(m => m.sig === m1.sig || m.sig === m2.sig).length,
      distinct: new Set(maps.map(m => m.sig)).size,
    };
  }
  return out;
}"""


def test_non_latin_labels_render_as_real_characters(small, open_graph):
    """Cyrillic, Arabic, Hebrew and Chinese names are drawn with glyphs, not empty boxes.

    This depends on the machine having a font for each script; the page ships
    none. If one is missing the test fails and names the labels, which is the
    true state of that machine rather than a fault in the page.
    """
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    page = opened.page
    labels = [label for _, label in NON_LATIN]
    probe = page.evaluate(_GLYPH_PROBE, labels)
    tofu = {
        label: r
        for label, r in probe.items()
        if r["blank"] or r["missingBox"] or r["distinct"] < r["chars"]
    }
    assert not tofu, f"no installed font has glyphs for: {tofu}"

    fonts_used = {}
    for kind, label in NON_LATIN:
        # The name reaches the DOM unchanged (search result, details panel)...
        page.fill("#search-input", label)
        assert page.locator("#search-results .sr-label").first.inner_text() == label
        fonts_used[label] = _platform_fonts(page, "#search-results .sr-label")
        page.keyboard.press("Enter")
        assert page.inner_text("#info-content h2") == label
        assert page.get_attribute("#info-content h2", "dir") == "auto"
        page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
        # ...and is drawn on the canvas: the selected node always gets its name,
        # and there is light text ink inside the box the page reports for it.
        snap = opened.snap()
        node = _by_key(snap)[(kind, label)]
        assert node["selected"]
        rect = next(r for r in snap["labels"] if r["label"] == label)
        ink = page.evaluate(
            "(r) => { const d = document.getElementById('graph').getContext('2d')"
            ".getImageData(Math.round(r.x), Math.round(r.y), Math.round(r.w), Math.round(r.h)).data;"
            " let n = 0; for (let i = 0; i < d.length; i += 4)"
            " if (d[i + 3] > 200 && d[i] > 200 && d[i + 1] > 200 && d[i + 2] > 190) n++; return n; }",
            rect,
        )
        assert ink >= 20, f"no text drawn for {label!r}"
        page.keyboard.press("Escape")
    assert all(fonts_used.values())
    assert not any("LastResort" in f for fams in fonts_used.values() for f in fams)
    _measure(
        "fonts_used_for_non_latin_names",
        dict(fonts_used),
        "a real font family for every name; no missing-glyph boxes",
    )
    # Right-to-left names are shown in right-to-left order in the page, not reversed.
    arabic = next(label for _, label in NON_LATIN if "؀" <= label[0] <= "ۿ")
    page.fill("#search-input", arabic)
    assert (
        page.evaluate(
            "() => getComputedStyle(document.querySelector('#search-results .sr-label')).direction"
        )
        == "rtl"
    )


def test_500_node_graph_renders_and_settles_within_budget(large, open_graph):
    spec = large.spec
    assert len(spec.entity_keys) >= 500
    opened = open_graph(large.path("graph.html"))
    wall = opened.wait_settled(LARGE_SETTLE_BUDGET_S)
    snap = opened.snap()
    _measure("settle_wall_seconds_520", round(wall, 2), f"<= {LARGE_SETTLE_BUDGET_S}")
    _measure(
        "settle_page_ms_520", round(snap["settleMs"]), f"<= {LARGE_SETTLE_BUDGET_S * 1000:.0f}"
    )
    assert snap["settleMs"] <= LARGE_SETTLE_BUDGET_S * 1000
    assert snap["entityCount"] == len(spec.entity_keys)
    assert snap["relationshipCount"] == len(spec.links)
    assert opened.problems == []
    pairs = _overlapping_pairs(snap["nodes"])
    allowed = int(MAX_NODE_OVERLAP_FRACTION * len(snap["nodes"]))
    _measure("overlapping_node_pairs_520", pairs, f"<= {allowed} (1% of {len(snap['nodes'])})")
    assert pairs <= allowed
    w, h = snap["width"], snap["height"]
    assert all(0 <= n["x"] <= w and 0 <= n["y"] <= h for n in snap["nodes"])
    outlines = [c for c in snap["communities"] if c["hull"]]
    _measure("community_outlines_drawn_520", len(outlines), ">= 3 (budget measured with them on)")
    assert len(outlines) >= 3
    # Still responsive afterwards: a hover is answered and the loop stops again.
    hub = max(snap["nodes"], key=lambda n: n["degree"])
    t0 = time.monotonic()
    opened.page.mouse.move(hub["x"], hub["y"])
    opened.page.wait_for_selector("#tooltip", state="visible")
    opened.page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
    _measure("hover_response_seconds_520", round(time.monotonic() - t0, 2), "<= 1.0")
    assert time.monotonic() - t0 <= 1.0


def test_reader_marks_entities_without_changing_the_text(small, open_graph):
    spec = small.spec
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    page = opened.page
    hub_key = _top_hubs(spec, 1)[0]
    hub = _by_key(opened.snap())[hub_key]
    page.mouse.click(hub["x"], hub["y"])
    first_card = page.locator("#info-content .doc-card").first
    filename = first_card.locator(".doc-name").inner_text()
    first_card.locator(".open-reader").click()
    doc = next(d for d in spec.documents if d["filename"] == filename)
    # The reader shows the document text exactly; marks are spans around parts of it.
    assert page.evaluate("() => document.getElementById('reader-text').textContent") == doc["text"]
    active = page.eval_on_selector_all(
        "#reader-text .entity-highlight.active", "els => els.map(e => e.textContent)"
    )
    assert active and set(active) == {hub_key[1]}
    assert page.locator("#reader-text .entity-highlight").count() > len(active)
    assert opened.problems == []


HOSTILE_LABEL = '<img src=x onerror="window.PWNED=1">'
HOSTILE_TEXT = (
    f'Memo. {HOSTILE_LABEL} met "><svg onload=window.PWNED=1> today.\n'
    "</script><script>window.PWNED=1</script><iframe src=https://example.invalid/></iframe>"
)


def test_hostile_document_values_stay_text(tmp_path, open_graph):
    """Every value from the database is untrusted: none of it may become markup or run."""
    from openfoia.graph_template import render

    graph = {
        "nodes": [
            {
                "id": "a",
                "label": HOSTILE_LABEL,
                "type": "person",
                "confidence": 0.9,
                "document_id": "d1",
                "page_number": 1,
                "context": "<script>window.PWNED=1</script>",
            },
            {
                "id": "b",
                "label": '"><svg onload=window.PWNED=1>',
                "type": "organization<b>",
                "confidence": 0.8,
                "document_id": "d1",
                "page_number": 2,
                "context": "<b>bold</b>",
            },
            {"id": "c", "label": "__proto__", "type": "constructor", "document_id": "__proto__"},
        ],
        "edges": [
            {"source": "a", "target": "b", "type": "<i>rel</i>"},
            {"source": "a", "target": "c", "type": "toString"},
        ],
        "documents": {
            "d1": {
                "id": "d1",
                "filename": "<b>memo</b>.pdf",
                "page_count": 2,
                "text": HOSTILE_TEXT,
                "request_id": "r",
                "source_url": "javascript:window.PWNED=1",
            }
        },
    }
    out = tmp_path / "hostile.html"
    render(json.dumps(graph), out)
    opened = open_graph(out)
    opened.wait_settled()
    page = opened.page
    assert opened.snap()["entityCount"] == 3

    for node in opened.snap()["nodes"]:
        # Selecting zooms in on the neighbourhood; wait for the view to stop first.
        page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
        fresh = next(n for n in opened.snap()["nodes"] if n["label"] == node["label"])
        page.mouse.move(fresh["x"], fresh["y"])
        page.mouse.click(fresh["x"], fresh["y"])
        assert page.inner_text("#info-content h2") == node["label"]
        if page.locator("#info-content .open-reader").count():
            page.locator("#info-content .open-reader").first.click()
            assert (
                page.evaluate("() => document.getElementById('reader-text').textContent")
                == HOSTILE_TEXT
            )
            assert page.inner_text("#reader-title") == "<b>memo</b>.pdf"
            page.keyboard.press("Escape")
        page.keyboard.press("Escape")
    page.fill("#search-input", "<")
    assert page.locator("#search-results .sr-label").count() == 2

    assert page.evaluate("() => window.PWNED") is None
    assert page.evaluate("() => document.querySelectorAll('script').length") == 1
    assert page.evaluate("() => document.querySelectorAll('img, svg, iframe, b, i').length") == 0
    # A javascript: source URL is not turned into a link.
    assert page.evaluate("() => document.querySelectorAll('a').length") == 0
    assert opened.problems == []
    assert opened.requests == [out.resolve().as_uri()]


# --- Usefulness: can a reporter read the bigger picture? ----------------------


def test_hubs_stand_out(small, open_graph):
    """The five best-connected entities are among the largest marks and are named."""
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    snap = opened.snap()
    nodes = _by_key(snap)
    top = _top_hubs(small.spec)
    radii = sorted((n["r"] for n in snap["nodes"]), reverse=True)
    fifth_largest = radii[4]
    labels = {r["label"]: r for r in snap["labels"]}
    panels = [_dom_rect(opened.page, "#sidebar"), _dom_rect(opened.page, "#search-box")]
    report = {}
    for key in top:
        node = nodes[key]
        rank = 1 + sum(1 for r in radii if r > node["r"] + 1e-9)
        rect = labels.get(key[1])
        report[key[1]] = {
            "degree": node["degree"],
            "size_rank": rank,
            "radius_px": round(node["r"], 1),
            "labelled": rect is not None,
        }
        assert node["r"] >= fifth_largest - 1e-9, f"{key} is not among the 5 largest marks"
        assert rect is not None, f"hub {key} has no visible name"
        assert rect["x"] >= 0 and rect["x"] + rect["w"] <= snap["width"]
        assert rect["y"] >= 0 and rect["y"] + rect["h"] <= snap["height"]
        assert not any(_rects_intersect(rect, p) for p in panels), "name is under a panel"
        others = [r for r in snap["labels"] if r["label"] != key[1]]
        assert not any(_rects_intersect(rect, o) for o in others), "name is covered by another"
    # The fixture's planted main hubs must be the top three by degree.
    assert set(top[:3]) == set(small.spec.main_hubs)
    median_radius = statistics.median(radii)
    _measure("top5_hubs", report, "size_rank <= 5 and labelled, for each")
    _measure(
        "smallest_top5_radius_over_median_radius",
        round(min(nodes[k]["r"] for k in top) / median_radius, 2),
        "> 1 (reported for judgement, not asserted beyond rank)",
    )


def test_planted_clusters_are_visible(small, open_graph):
    """Entities of the same planted strand sit together, apart from the other strands."""
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    spec = small.spec
    connected = [
        (n, spec.cluster[key]) for key, n in _by_key(opened.snap()).items() if n["degree"] > 0
    ]
    intra, inter = [], []
    for i, (a, ca) in enumerate(connected):
        for b, cb in connected[i + 1 :]:
            (intra if ca == cb else inter).append(math.hypot(a["x"] - b["x"], a["y"] - b["y"]))
    ratio = statistics.mean(intra) / statistics.mean(inter)

    clusters = sorted({c for _, c in connected})
    centre = {
        c: (
            statistics.mean(n["x"] for n, k in connected if k == c),
            statistics.mean(n["y"] for n, k in connected if k == c),
        )
        for c in clusters
    }
    nearest_own = sum(
        min(clusters, key=lambda c: math.hypot(n["x"] - centre[c][0], n["y"] - centre[c][1])) == own
        for n, own in connected
    )
    fraction = nearest_own / len(connected)
    _measure(
        "mean_intra_cluster_px / mean_inter_cluster_px",
        f"{statistics.mean(intra):.0f} / {statistics.mean(inter):.0f} = {ratio:.2f}",
        f"<= {MAX_INTRA_INTER_RATIO}",
    )
    _measure(
        "entities_nearest_their_own_cluster_centre",
        f"{nearest_own}/{len(connected)} = {fraction:.3f}",
        f">= {MIN_NEAREST_CENTROID_FRACTION}",
    )
    assert spec.bridge_links == 4
    assert ratio <= MAX_INTRA_INTER_RATIO
    assert fraction >= MIN_NEAREST_CENTROID_FRACTION


@pytest.mark.parametrize("viewport", [VIEWPORT, {"width": 1024, "height": 600}])
def test_fit_to_screen_leaves_nothing_off_screen(small, open_graph, viewport):
    opened = open_graph(small.path("graph.html"), viewport=viewport)
    opened.wait_settled()
    page = opened.page
    w, h = viewport["width"], viewport["height"]

    def outside(snap):
        return [
            n["label"]
            for n in snap["nodes"]
            if n["x"] - n["r"] < 0
            or n["x"] + n["r"] > w
            or n["y"] - n["r"] < 0
            or n["y"] + n["r"] > h
        ]

    at_load = outside(opened.snap())
    # Zoom in and pan away so that most of the graph really is off-screen first.
    page.mouse.move(w * 0.7, h * 0.5)
    for _ in range(12):
        page.mouse.wheel(0, -300)
    page.mouse.down()
    page.mouse.move(w * 0.7 + 400, h * 0.5 + 300, steps=4)
    page.mouse.up()
    page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
    assert len(outside(opened.snap())) > 0.5 * len(small.spec.entity_keys)

    page.click("#fit-btn")
    page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
    after_fit = opened.snap()
    lost = outside(after_fit)
    sidebar = _dom_rect(page, "#sidebar")
    under_sidebar = [
        n["label"]
        for n in after_fit["nodes"]
        if n["x"] - n["r"] < sidebar["x"] + sidebar["w"]
        and n["y"] - n["r"] < sidebar["y"] + sidebar["h"]
    ]
    _measure(
        f"nodes_outside_viewport_after_fit_{w}x{h}",
        f"{len(lost)} (at load: {len(at_load)}); under the sidebar: {len(under_sidebar)}",
        "== 0",
    )
    assert at_load == []
    assert lost == []
    assert under_sidebar == []


def test_labels_are_legible_at_default_zoom(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    snap = opened.snap()
    labels = snap["labels"]

    # Declared size of the font the canvas text is drawn with, in CSS px.
    font_px = float(re.match(r"([\d.]+)px", snap["labelFont"]).group(1))
    # Measured from the pixels: height of the tallest run of text ink in a name
    # made of Latin capitals and lower case. A 12 px sans font has about 9 px of
    # ink from the top of a capital to the baseline, 11-12 px with descenders.
    # The sample is an ordinary name (not a hub's), drawn in the secondary ink. A
    # pixel counts as text ink when it is nearer to that ink than to the surface.
    sample = next(
        r for r in labels if not r["hub"] and re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", r["label"])
    )
    ink_rows = opened.page.evaluate(
        "([r, ink, surface]) => { const x = Math.round(r.x), y = Math.round(r.y) - 2, w = Math.round(r.w), h = Math.round(r.h) + 4;"
        " const d = document.getElementById('graph').getContext('2d').getImageData(x, y, w, h).data;"
        " const dist = (p, c) => Math.hypot(d[p] - c[0], d[p + 1] - c[1], d[p + 2] - c[2]);"
        " let rows = 0; for (let j = 0; j < h; j++) { let hit = false;"
        " for (let i = 0; i < w; i++) { const p = (j * w + i) * 4;"
        " if (d[p + 3] > 200 && dist(p, ink) < dist(p, surface)) { hit = true; break; } }"
        " if (hit) rows++; } return rows; }",
        [sample, _rgb(snap["theme"]["labelInk"]), _rgb(snap["theme"]["surface"])],
    )
    label_pairs = sum(_rects_intersect(a, b) for i, a in enumerate(labels) for b in labels[i + 1 :])
    over_marks = 0
    for rect in labels:
        for n in snap["nodes"]:
            if n["label"] == rect["label"] or not n["visible"]:
                continue
            box = {"x": n["x"] - n["r"], "y": n["y"] - n["r"], "w": 2 * n["r"], "h": 2 * n["r"]}
            if _rects_intersect(rect, box):
                over_marks += 1
                break
    _measure("label_font_css_px", font_px, f">= {MIN_LABEL_FONT_PX}")
    _measure("label_ink_height_px", f"{ink_rows} rows in {sample['label']!r}", ">= 8")
    _measure("labels_shown_at_default_zoom", len(labels), f">= {MIN_LABELS_AT_DEFAULT_ZOOM}")
    _measure("label_pairs_overlapping", label_pairs, f"<= {MAX_LABEL_LABEL_OVERLAPS}")
    _measure(
        "labels_covering_another_entity_mark",
        f"{over_marks}/{len(labels)}",
        f"<= {MAX_LABELS_OVER_FOREIGN_MARKS_FRACTION:.0%} of labels",
    )
    assert font_px >= MIN_LABEL_FONT_PX
    assert ink_rows >= 8
    assert len(labels) >= MIN_LABELS_AT_DEFAULT_ZOOM
    assert label_pairs <= MAX_LABEL_LABEL_OVERLAPS
    assert over_marks <= MAX_LABELS_OVER_FOREIGN_MARKS_FRACTION * len(labels)
    assert snap["view"]["k"] > 0  # sanity: this was measured at the fitted default view
    assert snap["autoFit"] is True


def test_selecting_a_hub_explains_it(small, open_graph):
    """Clicking a hub lists exactly its true neighbours and its sources, and dims the rest."""
    spec = small.spec
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    page = opened.page
    hub_key = _top_hubs(spec, 1)[0]
    truth = spec.neighbours()[hub_key]
    before = _by_key(opened.snap())
    hub = before[hub_key]
    others = [k for k in before if k != hub_key and k not in truth]
    alpha_before = {k: opened.alpha_at(before[k]["x"], before[k]["y"]) for k in others}

    page.mouse.click(hub["x"], hub["y"])
    page.mouse.move(snapshot_safe_x(opened), 4)  # move off the node: selection, not hover
    page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")

    assert page.inner_text("#info-content h2") == hub_key[1]
    shown = page.eval_on_selector_all(
        "#info-content .neighbour",
        "els => els.map(e => [e.getAttribute('data-type'), e.querySelector('.nb-label').textContent])",
    )
    shown_keys = {(t, label) for t, label in shown}
    assert len(shown) == len(shown_keys)
    assert shown_keys == truth
    facts = page.inner_text("#info-content .facts")
    assert f"{len(truth)} connections" in facts

    # Source document, page and confidence of every mention of the hub.
    cards = page.eval_on_selector_all(
        "#info-content .doc-card",
        "els => els.map(e => ({name: e.querySelector('.doc-name').textContent,"
        " where: Array.from(e.querySelectorAll('.m-where')).map(m => m.textContent)}))",
    )
    expected_sources = sorted(
        (
            spec.documents[m.doc]["filename"],
            f"p. {m.page} · {round(m.confidence * 100)}% confidence",
        )
        for m in spec.mentions_of(hub_key)
    )
    got_sources = sorted((c["name"], w) for c in cards for w in c["where"])
    assert got_sources == expected_sources

    # Everything that is not the hub or a neighbour is visibly dimmed on the canvas.
    # Selecting zooms in on the neighbourhood, and names may be written over
    # dimmed marks. So the sample is the non-neighbours that are still on screen
    # and have no name or relationship label drawn over their centre; the raw
    # figure, with those left in, is reported next to it.
    after_snap = opened.snap()
    after = _by_key(after_snap)
    assert after[hub_key]["selected"]
    text_boxes = after_snap["labels"] + after_snap["edgeLabels"]

    def clear_of_text(n):
        point = {"x": n["x"] - 2, "y": n["y"] - 2, "w": 4, "h": 4}
        return not any(_rects_intersect(point, b) for b in text_boxes)

    def on_screen(n):
        return 0 <= n["x"] < after_snap["width"] and 0 <= n["y"] < after_snap["height"]

    was_solid = [k for k in others if alpha_before[k] >= LIT_ALPHA_MIN]
    raw_dimmed = sum(
        opened.alpha_at(after[k]["x"], after[k]["y"]) <= DIMMED_ALPHA_MAX
        for k in was_solid
        if on_screen(after[k])
    )
    raw_total = sum(on_screen(after[k]) for k in was_solid)
    sampled = [k for k in was_solid if on_screen(after[k]) and clear_of_text(after[k])]
    dimmed = sum(opened.alpha_at(after[k]["x"], after[k]["y"]) <= DIMMED_ALPHA_MAX for k in sampled)
    _measure(
        "non_neighbours_dimmed_including_those_under_a_name",
        f"{raw_dimmed}/{raw_total}",
        "reported, not asserted",
    )
    lit = sum(opened.alpha_at(after[k]["x"], after[k]["y"]) >= LIT_ALPHA_MIN for k in truth)
    _measure(
        "hub_panel_neighbours",
        f"{len(shown_keys)} listed, {len(truth)} true, {len(shown_keys ^ truth)} differences",
        "0 differences",
    )
    _measure("hub_panel_sources", f"{len(got_sources)} of {len(expected_sources)} mentions", "all")
    _measure("non_neighbours_dimmed", f"{dimmed}/{len(sampled)}", f">= {MIN_SAMPLE_AGREEMENT:.0%}")
    _measure("neighbours_fully_drawn", f"{lit}/{len(truth)}", f">= {MIN_SAMPLE_AGREEMENT:.0%}")
    assert len(was_solid) >= 0.8 * len(others)
    assert len(sampled) >= 40
    assert dimmed >= MIN_SAMPLE_AGREEMENT * len(sampled)
    assert lit >= MIN_SAMPLE_AGREEMENT * len(truth)
    # Only the hub and its neighbours are named while it is selected.
    assert {r["label"] for r in opened.snap()["labels"]} <= {hub_key[1]} | {k[1] for k in truth}


def snapshot_safe_x(opened: Opened) -> float:
    """An x position on the top edge of the canvas with no node under it."""
    return opened.snap()["width"] * 0.45


def test_isolates_do_not_displace_the_connected_graph(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    snap = opened.snap()
    connected = [n for n in snap["nodes"] if n["degree"] > 0]
    isolates = [n for n in snap["nodes"] if n["degree"] == 0]
    assert len(isolates) >= 20  # the fixture has about one isolate in ten entities

    sidebar = _dom_rect(opened.page, "#sidebar")
    free_left = sidebar["x"] + sidebar["w"]
    free_w, free_h = snap["width"] - free_left, snap["height"]
    cx = statistics.mean(n["x"] for n in connected)
    cy = statistics.mean(n["y"] for n in connected)
    off_x = abs(cx - (free_left + free_w / 2)) / free_w
    off_y = abs(cy - free_h / 2) / free_h

    def bbox(nodes):
        x0 = min(n["x"] - n["r"] for n in nodes)
        x1 = max(n["x"] + n["r"] for n in nodes)
        y0 = min(n["y"] - n["r"] for n in nodes)
        y1 = max(n["y"] + n["r"] for n in nodes)
        return x0, y0, x1, y1

    c, a = bbox(connected), bbox(snap["nodes"])
    area_fraction = ((c[2] - c[0]) * (c[3] - c[1])) / ((a[2] - a[0]) * (a[3] - a[1]))
    inside = [n["label"] for n in isolates if c[0] <= n["x"] <= c[2] and c[1] <= n["y"] <= c[3]]
    _measure(
        "connected_graph_centre_offset",
        f"{off_x:.3f} of free width, {off_y:.3f} of height",
        f"<= {MAX_MAIN_COMPONENT_OFFSET_FRACTION} each",
    )
    _measure(
        "connected_graph_share_of_drawn_area",
        round(area_fraction, 3),
        f">= {MIN_MAIN_COMPONENT_AREA_FRACTION}",
    )
    _measure("isolates_inside_connected_graph_area", len(inside), "== 0")
    assert off_x <= MAX_MAIN_COMPONENT_OFFSET_FRACTION
    assert off_y <= MAX_MAIN_COMPONENT_OFFSET_FRACTION
    assert area_fraction >= MIN_MAIN_COMPONENT_AREA_FRACTION
    assert inside == []


# --- Design: marks, initials, attributes, relationship labels, groups, table, themes ---

# A plain mark has no ink of another colour inside it. Two bold initials at the
# smallest large size (13 px radius, about 10 px type) cover well over 12 pixels;
# the smallest icon (7 px radius) covers well over 4.
MIN_INITIALS_INK_PX = 12
MIN_ICON_INK_PX = 4
# With a 40-neighbour hub selected, at least a fifth of its relationships should
# be readable on the graph itself; the details panel lists all of them.
MIN_RELATIONSHIP_LABELS_FRACTION = 0.20
# Groups found by the page should match the planted strands for at least 90% of
# connected entities (bridges and two-entity fragments are the rest).
MIN_COMMUNITY_AGREEMENT = 0.90
# WCAG AA for body text.
MIN_TEXT_CONTRAST = 4.5
# Every mark must be reachable within 12 px of its centre (a 24 px target).
HIT_REACH_PX = 11

_PIXELS_JS = """([x, y, half, targets, tol]) => {
  const x0 = Math.round(x - half), y0 = Math.round(y - half), n = Math.max(1, Math.round(2 * half));
  const d = document.getElementById('graph').getContext('2d').getImageData(x0, y0, n, n).data;
  const counts = targets.map(() => 0); let total = 0;
  for (let p = 0; p < d.length; p += 4) {
    if (d[p + 3] < 200) continue;
    total++;
    targets.forEach((t, i) => { if (Math.hypot(d[p] - t[0], d[p + 1] - t[1], d[p + 2] - t[2]) < tol) counts[i]++; });
  }
  return {total, counts};
}"""

_INK_ROWS_JS = """([r, ink, surface]) => {
  const x = Math.round(r.x), y = Math.round(r.y) - 2, w = Math.round(r.w), h = Math.round(r.h) + 4;
  const d = document.getElementById('graph').getContext('2d').getImageData(x, y, w, h).data;
  const dist = (p, c) => Math.hypot(d[p] - c[0], d[p + 1] - c[1], d[p + 2] - c[2]);
  let rows = 0;
  for (let j = 0; j < h; j++) {
    let hit = false;
    for (let i = 0; i < w; i++) { const p = (j * w + i) * 4; if (d[p + 3] > 200 && dist(p, ink) < dist(p, surface)) { hit = true; break; } }
    if (hit) rows++;
  }
  return rows;
}"""


def _pixels(opened: Opened, node: dict, half: float, targets: list, tol: float = 45) -> dict:
    return opened.page.evaluate(_PIXELS_JS, [node["x"], node["y"], half, targets, tol])


def test_marks_show_initials_icons_or_nothing_by_size(small, open_graph):
    """Large marks carry initials, medium marks a type icon, small marks are plain."""
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    snap = opened.snap()
    theme = snap["theme"]
    white = [255, 255, 255]
    actors = [n for n in snap["nodes"] if not n["attribute"] and n["visible"]]
    large = [n for n in actors if n["tier"] == "large"]
    medium = [n for n in actors if n["tier"] == "medium"]
    # Judge each small mark by itself: leave out the few a name or caption is drawn over.
    boxes = snap["labels"] + [c["captionRect"] for c in snap["communities"] if c["captionRect"]]

    def under_text(n):
        me = {
            "x": n["x"] - n["r"] - 2,
            "y": n["y"] - n["r"] - 2,
            "w": 2 * n["r"] + 4,
            "h": 2 * n["r"] + 4,
        }
        return any(_rects_intersect(me, b) for b in boxes if b.get("label") != n["label"])

    plain = [n for n in actors if n["tier"] == "small" and n["degree"] > 0 and not under_text(n)]
    assert len(large) >= 3 and len(medium) >= 20 and len(plain) >= 20
    assert all(n["r"] >= 13 for n in large) and all(n["r"] < 7 for n in plain)

    ink_large = []
    for n in large:
        px = _pixels(opened, n, n["r"] * 0.6, [white, _rgb(theme[n["type"]])])
        # Initials in white on a tinted fill: white ink, and a fill that is neither
        # white nor the full-strength hue.
        ink_large.append(px["counts"][0])
        assert px["counts"][0] >= MIN_INITIALS_INK_PX, n["label"]
        assert px["total"] - px["counts"][0] - px["counts"][1] >= MIN_INITIALS_INK_PX, n["label"]
        assert n["initials"]
    ink_medium = []
    for n in medium:
        px = _pixels(opened, n, n["r"] * 0.6, [white, _rgb(theme[n["type"]])])
        ink_medium.append(px["counts"][0])
        assert px["counts"][0] >= MIN_ICON_INK_PX, n["label"]
        assert px["counts"][1] >= MIN_ICON_INK_PX, n["label"]
    stray = 0
    for n in plain:
        px = _pixels(opened, n, n["r"] * 0.45, [white, _rgb(theme[n["type"]])])
        stray += px["counts"][0]
        assert px["counts"][1] >= 0.9 * px["total"], n["label"]
    assert stray == 0
    _measure(
        "white_ink_px_inside_large_marks",
        f"min {min(ink_large)} over {len(large)} marks",
        f">= {MIN_INITIALS_INK_PX}",
    )
    _measure(
        "white_ink_px_inside_medium_marks",
        f"min {min(ink_medium)} over {len(medium)} marks",
        f">= {MIN_ICON_INK_PX}",
    )
    _measure("white_ink_px_inside_small_marks", f"{stray} over {len(plain)} marks", "== 0")


INITIALS_CASES = [
    ("ООО «Северный Мост Инвест»", "organization", "СМ"),  # noqa: RUF001  (Cyrillic, legal form)
    ("Grace Ferreira", "person", "GF"),
    ("华东建设集团有限公司", "organization", "华东"),  # CJK organization: two characters
    ("王建国", "person", "王"),  # CJK person: one character
    ("خالد المنصوري", "person", "خا"),  # Arabic: first letter of each word
    ("אופק השקעות בע״מ", "organization", "אה"),  # Hebrew, legal form
    ("Halcyon Shipping Co.", "organization", "HS"),
    ("Anatolia Construction A.Ş.", "organization", "AC"),
    ("Levant Trading LLC", "organization", "LT"),
    ("Tietê Consulting Ltda", "organization", "TC"),
    ("Atlantico Logistics S.A.", "organization", "AL"),
    ("Acme GmbH", "organization", "A"),
    ("grace o'brien", "person", "GO"),
    ("ЗАО «Альфа»", "organization", "А"),  # noqa: RUF001
    ("𝒜lpha 𝔅eta", "person", "𝒜𝔅"),  # noqa: RUF001  (outside the BMP: whole code points)
    ("LLC", "organization", "L"),  # nothing left after dropping legal forms: keep the word
    ("", "person", ""),
]


def test_initials(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    got = opened.page.evaluate(
        "(cases) => cases.map(c => window.openfoiaGraph.initials(c[0], c[1]))",
        [[label, kind] for label, kind, _ in INITIALS_CASES],
    )
    for (label, kind, expected), value in zip(INITIALS_CASES, got, strict=True):
        # A zero-width non-joiner keeps Arabic initials from joining; it is not a letter.
        assert value.replace("‌", "") == expected, (label, kind, value)
    # The planted hubs carry their initials in the page model.
    nodes = _by_key(opened.snap())
    for key in small.spec.main_hubs:
        assert nodes[key]["initials"] == opened.page.evaluate(
            "(c) => window.openfoiaGraph.initials(c[0], c[1])", [key[1], key[0]]
        )
        assert 1 <= len(nodes[key]["initials"].replace("‌", "")) <= 2


def test_money_and_dates_are_muted_and_unnamed_until_asked(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    snap = opened.snap()
    theme = snap["theme"]
    attrs = [n for n in snap["nodes"] if n["type"] in ("money", "date")]
    actors = [n for n in snap["nodes"] if not n["attribute"]]
    assert len(attrs) >= 50 and all(n["attribute"] for n in attrs)
    hues = [_rgb(theme[t]) for t in ("person", "organization", "location")]
    muted_px, hue_px = [], 0
    for n in attrs:
        px = _pixels(opened, n, n["r"] + 1.5, [_rgb(theme["muted"]), *hues], tol=40)
        muted_px.append(px["counts"][0])
        hue_px += sum(px["counts"][1:])
    with_muted = sum(1 for c in muted_px if c >= 3)
    named_at_default = [r["label"] for r in snap["labels"] if r["type"] in ("money", "date")]
    _measure("attribute_marks_drawn_in_muted_ink", f"{with_muted}/{len(attrs)}", ">= 95%")
    _measure("hue_px_inside_attribute_marks", hue_px, "== 0")
    _measure(
        "largest_attribute_radius_vs_smallest_actor_radius",
        f"{max(n['r'] for n in attrs):.1f} px vs {min(n['r'] for n in actors):.1f} px",
        "attribute < actor",
    )
    _measure("attribute_names_at_default_zoom", len(named_at_default), "== 0")
    assert with_muted >= 0.95 * len(attrs)
    assert hue_px == 0
    assert max(n["r"] for n in attrs) < min(n["r"] for n in actors)
    assert named_at_default == []

    # Select a hub: the amounts and dates tied to it are now named.
    hub_key = _top_hubs(small.spec, 1)[0]
    truth = small.spec.neighbours()[hub_key]
    hub = _by_key(snap)[hub_key]
    opened.page.mouse.click(hub["x"], hub["y"])
    opened.page.mouse.move(snapshot_safe_x(opened), 4)
    opened.page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
    named = {
        (r["type"], r["label"]) for r in opened.snap()["labels"] if r["type"] in ("money", "date")
    }
    attr_neighbours = {k for k in truth if k[0] in ("money", "date")}
    _measure("attribute_names_with_hub_selected", f"{len(named)} of {len(attr_neighbours)}", ">= 1")
    assert attr_neighbours
    assert named and named <= attr_neighbours


def test_selected_hub_shows_relationship_types_on_the_graph(small, open_graph):
    spec = small.spec
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    hub_key = _top_hubs(spec, 1)[0]
    truth = spec.neighbours()[hub_key]
    assert opened.snap()["edgeLabels"] == []  # nothing selected, no relationship labels
    hub = _by_key(opened.snap())[hub_key]
    opened.page.mouse.click(hub["x"], hub["y"])
    opened.page.mouse.move(snapshot_safe_x(opened), 4)
    opened.page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
    snap = opened.snap()
    edge_labels = snap["edgeLabels"]

    # What the fixture says the relationship is, with the arrow the page uses:
    # "→" when the selected entity is the source, "←" when it is the target.
    expected: dict[tuple[str, str], set[str]] = {}
    for a, b, rel in spec.links:
        ka = (spec.mentions[a].type, spec.mentions[a].label)
        kb = (spec.mentions[b].type, spec.mentions[b].label)
        if ka == hub_key:
            expected.setdefault(kb, set()).add("→ " + rel)
        elif kb == hub_key:
            expected.setdefault(ka, set()).add("← " + rel)
    assert set(expected) == truth
    for lab in edge_labels:
        key = (lab["toType"], lab["to"])
        assert key in truth
        if not lab["text"].endswith("…"):
            assert set(lab["text"].split(", ")) == expected[key], lab
    assert len({(lab["toType"], lab["to"]) for lab in edge_labels}) == len(edge_labels)

    overlaps = sum(
        _rects_intersect(a, b) for i, a in enumerate(edge_labels) for b in edge_labels[i + 1 :]
    )
    # A relationship label under a name is inside that name's own box; it must not
    # touch any other name, and a label on a line must not touch any name at all.
    over_names = 0
    for lab in edge_labels:
        for name in snap["labels"]:
            if lab["at"] == "name" and name["label"] == lab["to"] and name["type"] == lab["toType"]:
                continue
            over_names += _rects_intersect(lab, name)
    on_screen = sum(
        lab["x"] >= 0 and lab["x"] + lab["w"] <= snap["width"] and 0 <= lab["y"] <= snap["height"]
        for lab in edge_labels
    )
    need = math.ceil(MIN_RELATIONSHIP_LABELS_FRACTION * len(truth))
    _measure(
        "relationship_labels_with_hub_selected",
        f"{len(edge_labels)} of {len(truth)} ({sum(lab['at'] == 'edge' for lab in edge_labels)} on the"
        f" line, {sum(lab['at'] == 'name' for lab in edge_labels)} under the neighbour's name)",
        f">= {need} ({MIN_RELATIONSHIP_LABELS_FRACTION:.0%})",
    )
    _measure("relationship_label_pairs_overlapping", overlaps, "== 0")
    _measure("relationship_labels_over_names", over_names, "== 0")
    assert len(edge_labels) >= need
    assert on_screen == len(edge_labels)
    assert overlaps == 0
    assert over_names == 0
    assert opened.problems == []

    opened.page.keyboard.press("Escape")
    opened.page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
    after = opened.snap()
    assert after["edgeLabels"] == []
    # Closing the selection goes back to the whole graph.
    assert after["autoFit"] is True
    assert all(
        0 <= n["x"] <= after["width"] and 0 <= n["y"] <= after["height"] for n in after["nodes"]
    )


def _point_in_polygon(x: float, y: float, poly: list) -> bool:
    inside = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def test_community_outlines_name_the_planted_strands(small, open_graph):
    spec = small.spec
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    snap = opened.snap()
    nodes = _by_key(snap)
    drawn = [c for c in snap["communities"] if c["hull"]]
    assert len(drawn) == 3  # the fixture plants three strands

    # Each caption names the best-connected member of its own group, and its size.
    for c in drawn:
        members = [n for n in snap["nodes"] if n["community"] == c["id"]]
        top = max(members, key=lambda n: n["degree"])
        assert c["top"] == top["label"]
        assert c["size"] == len(members)
        assert c["caption"] is not None
        assert c["caption"].startswith(top["label"][:27])
        assert f"{len(members)} entities" in c["caption"]
    # Each planted hub is the named member of one group and sits inside that group's outline.
    assert {(c["topType"], c["top"]) for c in drawn} == set(spec.main_hubs)
    for key in spec.main_hubs:
        own = next(c for c in drawn if (c["topType"], c["top"]) == key)
        assert nodes[key]["community"] == own["id"]
        assert _point_in_polygon(nodes[key]["x"], nodes[key]["y"], own["hull"])
        assert all(
            _point_in_polygon(n["x"], n["y"], own["hull"])
            for n in snap["nodes"]
            if n["community"] == own["id"]
        )

    # The groups agree with the planted strands.
    hub_cluster = {nodes[k]["community"]: spec.cluster[k] for k in spec.main_hubs}
    connected = [(k, n) for k, n in nodes.items() if n["degree"] > 0]
    agree = sum(hub_cluster.get(n["community"]) == spec.cluster[k] for k, n in connected)
    captions = [c["captionRect"] for c in drawn]
    caption_overlaps = sum(
        _rects_intersect(a, b) for i, a in enumerate(captions) for b in captions[i + 1 :]
    ) + sum(_rects_intersect(a, b) for a in captions for b in snap["labels"])
    _measure("community_outlines_drawn", len(drawn), "== 3 planted strands")
    _measure(
        "entities_in_the_group_of_their_planted_strand",
        f"{agree}/{len(connected)} = {agree / len(connected):.3f}",
        f">= {MIN_COMMUNITY_AGREEMENT}",
    )
    _measure("captions_overlapping_names_or_each_other", caption_overlaps, "== 0")
    assert agree >= MIN_COMMUNITY_AGREEMENT * len(connected)
    assert caption_overlaps == 0
    # The outline is a faint wash, not a categorical colour: points inside an
    # outline but away from every mark and name are nearly transparent on the
    # canvas (a line may cross a few of them, hence the median).
    text = snap["labels"] + captions
    alphas = []
    for c in drawn:
        xs = [p[0] for p in c["hull"]]
        ys = [p[1] for p in c["hull"]]
        for gx in range(int(min(xs)), int(max(xs)), 9):
            for gy in range(int(min(ys)), int(max(ys)), 9):
                if not _point_in_polygon(gx, gy, c["hull"]):
                    continue
                if any(math.hypot(gx - n["x"], gy - n["y"]) < n["r"] + 8 for n in snap["nodes"]):
                    continue
                if any(
                    _rects_intersect({"x": gx - 4, "y": gy - 4, "w": 8, "h": 8}, t) for t in text
                ):
                    continue
                alphas.append(opened.alpha_at(gx, gy))
    assert len(alphas) >= 100
    wash = statistics.median(alphas) / 255
    _measure("community_outline_fill_opacity", round(wash, 3), "0 < opacity <= 0.06 (a wash)")
    assert 0 < wash <= 0.06


def _table_rows(page) -> list[dict]:
    return page.eval_on_selector_all(
        "#table-body tr",
        "els => els.map(e => ({type: e.getAttribute('data-type'),"
        " label: e.querySelector('.row-label').textContent,"
        " nums: Array.from(e.querySelectorAll('td.num')).map(t => Number(t.textContent)),"
        " rels: e.querySelector('td.rels').textContent}))",
    )


def test_table_view_lists_the_visible_entities(small, open_graph):
    spec = small.spec
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    page = opened.page
    nb = spec.neighbours()
    page.click("#view-table")
    assert opened.snap()["mode"] == "table"
    assert page.locator("#table-view").is_visible()

    rows = _table_rows(page)
    assert {(r["type"], r["label"]) for r in rows} == spec.entity_keys
    assert len(rows) == len(spec.entity_keys)
    # Default order: most connected first. Connections, mentions and documents match the fixture.
    conns = [r["nums"][0] for r in rows]
    assert conns == sorted(conns, reverse=True)
    assert (rows[0]["type"], rows[0]["label"]) == _top_hubs(spec, 1)[0]
    for r in rows:
        key = (r["type"], r["label"])
        ms = spec.mentions_of(key)
        assert r["nums"] == [len(nb[key]), len(ms), len({m.doc for m in ms})], key
    assert page.get_attribute('#table-head th[data-key="connections"]', "aria-sort") == "descending"
    hub_rels = {
        rel
        for a, b, rel in spec.links
        for i in (a, b)
        if (spec.mentions[i].type, spec.mentions[i].label) == (rows[0]["type"], rows[0]["label"])
    }
    assert rows[0]["rels"].split(" ")[0] in hub_rels

    # The table honours the type filter, the confidence filter and the search box.
    page.click('#legend .legend-item[data-type="person"]')
    rows = _table_rows(page)
    assert {(r["type"], r["label"]) for r in rows} == {
        k for k in spec.entity_keys if k[0] != "person"
    }
    page.fill("#conf-range", "80")
    page.dispatch_event("#conf-range", "input")
    visible = {k for k, n in _by_key(opened.snap()).items() if n["visible"]}
    assert 0 < len(visible) < len(spec.entity_keys)
    assert {(r["type"], r["label"]) for r in _table_rows(page)} == visible
    page.click("#reset-btn")
    page.fill("#search-input", "halcyon")
    rows = _table_rows(page)
    assert rows and {(r["type"], r["label"]) for r in rows} == {
        k for k in spec.entity_keys if "halcyon" in k[1].lower()
    }
    page.fill("#search-input", "")

    # Sorting by another column.
    page.click('#table-head th[data-key="name"] button')
    names = [r["label"] for r in _table_rows(page)]
    assert page.get_attribute('#table-head th[data-key="name"]', "aria-sort") == "ascending"
    assert names[0] == min(names, key=lambda s: s.lower()) or names == sorted(names, key=str.lower)
    page.click('#table-head th[data-key="connections"] button')

    # Keyboard focus on a row gives the same summary as hovering the mark.
    target_row = page.locator("#table-body tr").nth(3)
    label = target_row.locator(".row-label").inner_text()
    kind = target_row.get_attribute("data-type")
    target_row.focus()
    assert page.locator("#tooltip").is_visible()
    assert page.inner_text("#tooltip .tt-title") == label

    # Selecting a row goes back to the graph with that entity selected and in view.
    target_row.press("Enter")
    page.wait_for_function("() => window.openfoiaGraph.snapshot().settled")
    snap = opened.snap()
    assert snap["mode"] == "graph"
    assert not page.locator("#table-view").is_visible()
    node = _by_key(snap)[(kind, label)]
    assert node["selected"]
    assert 0 < node["x"] < snap["width"] and 0 < node["y"] < snap["height"]
    assert page.inner_text("#info-content h2") == label
    assert opened.problems == []
    _measure(
        "table_rows_vs_visible_entities", f"{len(spec.entity_keys)} rows", "== visible entities"
    )


def test_light_theme_has_dark_ink_on_a_light_surface(small, open_graph):
    opened = open_graph(small.path("graph.html"), scheme="light")
    opened.wait_settled()
    page = opened.page
    snap = opened.snap()
    theme = snap["theme"]
    assert theme["name"] == "light"
    label_contrast = _contrast(theme["labelInk"], theme["surface"])
    hub_contrast = _contrast(theme["ink"], theme["surface"])
    _measure("light_theme_label_contrast", round(label_contrast, 2), f">= {MIN_TEXT_CONTRAST}")
    _measure("light_theme_hub_label_contrast", round(hub_contrast, 2), f">= {MIN_TEXT_CONTRAST}")
    assert label_contrast >= MIN_TEXT_CONTRAST and hub_contrast >= MIN_TEXT_CONTRAST
    assert _luminance(theme["surface"]) > 0.9 > _luminance(theme["labelInk"])
    # What is actually on screen: the page background is the light surface, and
    # an ordinary name is drawn in the dark ink.
    body = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    assert body == "rgb({}, {}, {})".format(*_rgb(theme["surface"]))
    sample = next(
        r
        for r in snap["labels"]
        if not r["hub"] and re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", r["label"])
    )
    rows = page.evaluate(_INK_ROWS_JS, [sample, _rgb(theme["labelInk"]), _rgb(theme["surface"])])
    assert rows >= 8
    # Marks use the light theme's own steps of the three hues.
    person = next(n for n in snap["nodes"] if n["type"] == "person" and n["tier"] == "medium")
    px = _pixels(opened, person, person["r"] * 0.9, [_rgb("#2a78d6")], tol=30)
    assert px["counts"][0] >= 10
    assert opened.problems == []

    # The dark theme is the reverse, and the button switches between them.
    page.click("#theme-btn")
    dark = opened.snap()["theme"]
    assert dark["name"] == "dark"
    assert _luminance(dark["surface"]) < 0.05 < _luminance(dark["labelInk"])
    assert _contrast(dark["labelInk"], dark["surface"]) >= MIN_TEXT_CONTRAST
    assert (
        page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(26, 26, 25)"
    )
    _measure(
        "dark_theme_label_contrast",
        round(_contrast(dark["labelInk"], dark["surface"]), 2),
        f">= {MIN_TEXT_CONTRAST}",
    )


def test_small_marks_have_a_24px_hit_area(small, open_graph):
    opened = open_graph(small.path("graph.html"))
    opened.wait_settled()
    nodes = [n for n in opened.snap()["nodes"] if n["visible"]]

    def clearance(n):
        return min(
            math.hypot(n["x"] - o["x"], n["y"] - o["y"]) - o["r"] for o in nodes if o is not n
        )

    small_marks = [n for n in nodes if n["r"] < 6]
    target = max(small_marks, key=clearance)
    assert clearance(target) > 2 * HIT_REACH_PX
    page = opened.page
    for dx, dy in ((HIT_REACH_PX, 0), (0, -HIT_REACH_PX), (-8, 8)):
        page.mouse.move(target["x"] + dx, target["y"] + dy)
        page.wait_for_selector("#tooltip", state="visible")
        assert page.inner_text("#tooltip .tt-title") == target["label"]
        page.mouse.move(snapshot_safe_x(opened), 4)
        page.wait_for_selector("#tooltip", state="hidden")
    _measure(
        "hit_reach_for_a_small_mark",
        f"hover registers {HIT_REACH_PX} px from the centre of a {target['r']:.1f} px mark",
        f">= {HIT_REACH_PX} px (24 px target)",
    )
