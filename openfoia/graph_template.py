"""Graph visualization HTML template.

Standalone HTML with inline CSS/JS for the entity relationship graph,
document reader, and entity highlighting. Embedded as a Python string
so the output HTML has zero external dependencies — works offline.

The page is a plaintext export. It never loads anything: no script, font,
image or stylesheet comes from outside the file, and a Content-Security-Policy
meta tag tells the browser to refuse any such load and to run only the one
inline script whose hash is computed in ``render``.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

_SCRIPT_OPEN = "<script>"
_SCRIPT_CLOSE = "</script>"
_CSP_HASH_TOKEN = "__CSP_SCRIPT_SHA256__"


def get_template() -> str:
    """Return the graph HTML template string."""
    return _TEMPLATE


def escape_json_for_script(graph_json: str) -> str:
    """Make a JSON string safe to embed inside an inline <script> block.

    Document text and entity labels come from UNTRUSTED documents. ``json.dumps``
    does not escape ``<``, ``>`` or ``&``, so a document containing a literal
    ``</script>`` terminates the script element and everything after it is
    parsed as HTML — arbitrary JS execution in the reader's browser.

    These characters never appear in JSON *structure*, only inside string
    literals, so replacing them with their ``\\uXXXX`` escapes preserves the
    decoded value exactly while making breakout impossible. U+2028/U+2029 are
    valid in JSON but are line terminators in JavaScript, so they go too.
    """
    return (
        graph_json.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_html(graph_json: str) -> str:
    """Return the graph page with *graph_json* embedded.

    The data goes through ``escape_json_for_script``. The page's CSP then pins
    the inline script by SHA-256, so even a script element that somehow got
    into the page would not run. The hash token is only replaced in the part
    of the template before the script, never inside the embedded data.
    """
    head, _, rest = _TEMPLATE.partition(_SCRIPT_OPEN)
    body, _, tail = rest.rpartition(_SCRIPT_CLOSE)
    body = body.replace("__GRAPH_DATA__", escape_json_for_script(graph_json))
    digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
    head = head.replace(_CSP_HASH_TOKEN, digest)
    return head + _SCRIPT_OPEN + body + _SCRIPT_CLOSE + tail


def render(graph_json: str, output_path: Path) -> None:
    """Render the graph template with embedded data and write to file."""
    # UTF-8 and "\n" regardless of platform: the CSP hash covers the exact
    # script text, and non-Latin labels must not depend on the locale encoding.
    output_path.write_text(render_html(graph_json), encoding="utf-8", newline="\n")


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-__CSP_SCRIPT_SHA256__'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="light dark">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenFOIA Entity Graph</title>
<style>
  :root {
    color-scheme: light;
    --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --hairline: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10); --wash: rgba(11,11,11,0.05);
    --accent: #2a78d6; --shadow: 0 6px 24px rgba(11,11,11,0.10); --mark: rgba(237,161,0,0.30);
    --font: system-ui, -apple-system, 'Segoe UI', Roboto, 'Noto Sans', 'Noto Sans Arabic', 'Noto Sans Hebrew', 'Noto Sans CJK SC', sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface: #1a1a19; --page: #0d0d0d; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
      --hairline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10); --wash: rgba(255,255,255,0.06);
      --accent: #3987e5; --shadow: 0 6px 24px rgba(0,0,0,0.45); --mark: rgba(201,133,0,0.35);
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface: #1a1a19; --page: #0d0d0d; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --hairline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10); --wash: rgba(255,255,255,0.06);
    --accent: #3987e5; --shadow: 0 6px 24px rgba(0,0,0,0.45); --mark: rgba(201,133,0,0.35);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: var(--font); background: var(--surface); color: var(--ink); overflow: hidden; font-size: 13px; line-height: 1.45; }
  button, input, select { font: inherit; color: inherit; }
  button:focus-visible, input:focus-visible, select:focus-visible, tr:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  #graph { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; cursor: grab; display: block; }
  #graph.grabbing { cursor: grabbing; }
  .swatch { width: 18px; height: 18px; flex-shrink: 0; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow); }
  .eyebrow { font-size: 11px; color: var(--muted); letter-spacing: 0.04em; text-transform: uppercase; font-weight: 600; }

  #sidebar { position: fixed; top: 16px; left: 16px; width: 248px; max-height: calc(100vh - 32px); overflow-y: auto; padding: 18px 18px 16px; z-index: 10; }
  #sidebar h1 { font-size: 16px; font-weight: 600; letter-spacing: -0.01em; }
  #stats { font-size: 12px; color: var(--ink-2); margin-top: 4px; }
  #sidebar section { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--hairline); }
  #sidebar section > .eyebrow { display: block; margin-bottom: 8px; }
  .seg { display: flex; margin-top: 14px; padding: 2px; background: var(--wash); border-radius: 8px; }
  .seg button { flex: 1; padding: 5px 0; border: 0; background: none; border-radius: 6px; font-size: 12px; font-weight: 600; color: var(--ink-2); cursor: pointer; }
  .seg button[aria-selected="true"] { background: var(--surface); color: var(--ink); box-shadow: 0 0 0 1px var(--border); }
  .legend-group { font-size: 11px; color: var(--muted); margin: 10px 0 2px 6px; }
  .legend-group:first-child { margin-top: 0; }
  .legend-item {
    display: flex; align-items: center; gap: 10px; width: 100%; padding: 4px 6px; font-size: 13px;
    background: none; border: 0; border-radius: 6px; cursor: pointer; text-align: left;
  }
  .legend-item:hover { background: var(--wash); }
  .legend-item .legend-count { margin-left: auto; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
  .legend-item[aria-pressed="false"] { opacity: 0.4; }
  .legend-item[aria-pressed="false"] .legend-name { text-decoration: line-through; }
  .control-row { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--ink-2); }
  .control-row output { margin-left: auto; color: var(--ink); font-variant-numeric: tabular-nums; font-weight: 600; }
  #conf-range { width: 100%; margin-top: 8px; accent-color: var(--accent); }
  #filter-count { font-size: 12px; color: var(--muted); margin-top: 4px; }
  #size-by { margin-left: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 3px 6px; font-size: 12px; }
  .btn-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
  .btn { background: var(--surface); border: 1px solid var(--border); padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .btn:hover { background: var(--wash); }
  .hint { font-size: 11px; color: var(--muted); margin-top: 12px; line-height: 1.5; }

  #search-box { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 13; }
  #search-box input { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; font-size: 13px; width: 300px; outline: none; box-shadow: var(--shadow); }
  #search-box input:focus { border-color: var(--accent); }
  #search-box input::placeholder { color: var(--muted); }
  #search-results { position: absolute; top: 42px; left: 0; width: 100%; max-height: 300px; overflow-y: auto; display: none; padding: 4px; }
  .search-result { padding: 6px 8px; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 8px; border-radius: 6px; }
  .search-result:hover, .search-result.first { background: var(--wash); }
  .search-result .sr-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .search-result .sr-note { margin-left: auto; color: var(--muted); font-size: 11px; white-space: nowrap; }

  #tooltip { position: fixed; z-index: 20; display: none; pointer-events: none; max-width: 320px; padding: 8px 10px; font-size: 12px; color: var(--ink-2); line-height: 1.5; border-radius: 8px; }
  #tooltip .tt-title { color: var(--ink); font-size: 13px; font-weight: 600; }

  #info-panel { position: fixed; top: 16px; right: 16px; width: 360px; max-width: calc(100vw - 32px); padding: 18px; display: none; max-height: calc(100vh - 32px); overflow-y: auto; z-index: 14; }
  #info-panel h2 { font-size: 17px; margin: 0 28px 6px 0; font-weight: 600; overflow-wrap: anywhere; letter-spacing: -0.01em; }
  .type-badge { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-2); margin-bottom: 4px; }
  .facts { font-size: 12px; color: var(--ink-2); margin-bottom: 12px; }
  #info-panel .label { margin-top: 16px; margin-bottom: 6px; padding-top: 12px; border-top: 1px solid var(--hairline); }
  #info-close { position: absolute; top: 12px; right: 12px; cursor: pointer; color: var(--muted); font-size: 20px; line-height: 1; width: 28px; height: 28px; border-radius: 6px; background: none; border: 0; }
  #info-close:hover { background: var(--wash); color: var(--ink); }
  .neighbour { display: flex; align-items: center; gap: 8px; width: 100%; padding: 4px 6px; font-size: 13px; background: none; border: 0; border-radius: 6px; cursor: pointer; text-align: left; }
  .neighbour:hover { background: var(--wash); }
  .neighbour .nb-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .neighbour .nb-rel { margin-left: auto; color: var(--muted); font-size: 11px; white-space: nowrap; }
  .neighbour.filtered { opacity: 0.45; }
  .doc-card { border: 1px solid var(--hairline); border-radius: 8px; padding: 10px 12px; margin: 6px 0; }
  .doc-card .doc-name { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
  .doc-card .doc-meta { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .mention { font-size: 12px; color: var(--ink-2); margin-top: 6px; line-height: 1.4; }
  .mention .m-where { color: var(--ink); font-variant-numeric: tabular-nums; }
  .mention .m-context { font-style: italic; overflow-wrap: anywhere; }
  .doc-card a, .doc-card .open-reader { font-size: 12px; color: var(--accent); text-decoration: none; display: inline-block; margin-top: 6px; background: none; border: 0; cursor: pointer; padding: 0; }
  .doc-card .no-text { font-size: 11px; color: var(--muted); margin-top: 6px; }

  #table-view { position: fixed; top: 64px; left: 280px; right: 16px; bottom: 16px; z-index: 12; display: none; overflow: auto; }
  #entity-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  #entity-table caption { text-align: left; padding: 14px 16px 6px; font-size: 12px; color: var(--muted); }
  #entity-table th { position: sticky; top: 0; background: var(--surface); text-align: left; padding: 0; border-bottom: 1px solid var(--baseline); z-index: 1; }
  #entity-table th button { width: 100%; padding: 8px 16px; background: none; border: 0; cursor: pointer; text-align: inherit; font-size: 11px; font-weight: 600; color: var(--muted); letter-spacing: 0.04em; text-transform: uppercase; white-space: nowrap; }
  #entity-table th button:hover { color: var(--ink); }
  #entity-table th[aria-sort="ascending"] button::after { content: " \2191"; }
  #entity-table th[aria-sort="descending"] button::after { content: " \2193"; }
  #entity-table th[aria-sort] button { color: var(--ink); }
  #entity-table td { padding: 7px 16px; border-bottom: 1px solid var(--hairline); vertical-align: middle; }
  #entity-table th.num, #entity-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
  #entity-table tbody tr { cursor: pointer; }
  #entity-table tbody tr:hover, #entity-table tbody tr:focus { background: var(--wash); }
  #entity-table .cell-name { display: flex; align-items: center; gap: 10px; font-weight: 600; }
  #entity-table .cell-name span { overflow-wrap: anywhere; }
  #entity-table td.rels { color: var(--ink-2); font-size: 12px; }
  #entity-table td.type { color: var(--ink-2); white-space: nowrap; }
  #table-empty { padding: 24px 16px; color: var(--muted); display: none; }

  #doc-reader { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: var(--page); z-index: 100; display: none; }
  #doc-reader .reader-header { height: 52px; display: flex; align-items: center; gap: 12px; padding: 0 20px; border-bottom: 1px solid var(--hairline); background: var(--surface); }
  #doc-reader .reader-header h2 { font-size: 15px; font-weight: 600; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #doc-reader a.source-link { color: var(--accent); font-size: 13px; text-decoration: none; padding: 5px 10px; border: 1px solid var(--border); border-radius: 6px; white-space: nowrap; }
  #reader-close { cursor: pointer; color: var(--muted); font-size: 22px; line-height: 1; width: 36px; height: 36px; border-radius: 8px; background: none; border: 0; }
  #reader-close:hover { background: var(--wash); color: var(--ink); }
  #doc-reader .reader-body { display: flex; height: calc(100vh - 52px); }
  #reader-sidebar { width: 280px; border-right: 1px solid var(--hairline); overflow-y: auto; padding: 14px; flex-shrink: 0; background: var(--surface); }
  #reader-sidebar h3 { margin-bottom: 8px; }
  .sidebar-entity { padding: 5px 8px; border-radius: 6px; margin: 1px 0; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 8px; }
  .sidebar-entity:hover { background: var(--wash); }
  .sidebar-entity.active { background: var(--wash); box-shadow: inset 2px 0 0 var(--accent); }
  .sidebar-entity-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sidebar-entity-count { color: var(--muted); font-size: 11px; margin-left: auto; }
  #reader-text { flex: 1; overflow-y: auto; padding: 32px 48px; font-size: 14px; line-height: 1.8; color: var(--ink-2); white-space: pre-wrap; overflow-wrap: anywhere; }
  .entity-highlight { border-bottom: 2px solid var(--muted); padding: 1px 1px; border-radius: 2px; cursor: pointer; color: var(--ink); }
  .entity-highlight.active { background: var(--mark); }

  #focus-bar { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); padding: 8px 14px; z-index: 10; display: none; align-items: center; gap: 12px; font-size: 13px; color: var(--ink-2); }
  #focus-bar .focus-depth { color: var(--ink); font-weight: 600; }
  #focus-bar input[type=range] { width: 110px; accent-color: var(--accent); }
  #empty { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); color: var(--muted); font-size: 14px; display: none; }
</style>
</head>
<body>
<canvas id="graph"></canvas>
<div id="empty">No entities to show.</div>
<div id="sidebar" class="card">
  <h1>Entity graph</h1>
  <p id="stats"></p>
  <div class="seg" role="tablist" aria-label="View">
    <button type="button" role="tab" id="view-graph" aria-selected="true">Graph</button>
    <button type="button" role="tab" id="view-table" aria-selected="false">Table</button>
  </div>
  <section>
    <div id="legend"></div>
  </section>
  <section>
    <label class="control-row" for="conf-range">Minimum confidence <output id="conf-value">0%</output></label>
    <input type="range" id="conf-range" min="0" max="100" step="5" value="0">
    <p id="filter-count"></p>
  </section>
  <section>
    <label class="control-row" for="size-by">Size by
      <select id="size-by"><option value="degree">Connections</option><option value="mentions">Mentions</option></select>
    </label>
    <div class="btn-row">
      <button type="button" class="btn" id="fit-btn" title="Fit the visible graph to the window (F)">Fit to screen</button>
      <button type="button" class="btn" id="reset-btn">Reset filters</button>
      <button type="button" class="btn" id="theme-btn">Light theme</button>
    </div>
    <p class="hint">Hover for a summary, click for sources and neighbours. Scroll to zoom, drag to pan. More names appear as you zoom in.</p>
  </section>
</div>
<div id="search-box">
  <input type="text" placeholder="Search entities (press /)" id="search-input" autocomplete="off" spellcheck="false" dir="auto" aria-label="Search entities">
  <div id="search-results" class="card"></div>
</div>
<div id="table-view" class="card">
  <table id="entity-table">
    <caption id="table-caption"></caption>
    <thead><tr id="table-head"></tr></thead>
    <tbody id="table-body"></tbody>
  </table>
  <div id="table-empty">No entity matches the current filters.</div>
</div>
<div id="tooltip" class="card"></div>
<div id="focus-bar" class="card">
  <span>Neighbourhood:</span>
  <span class="focus-depth" id="focus-depth-label">1-hop</span>
  <input type="range" min="1" max="4" value="1" id="focus-depth" aria-label="Focus depth">
  <button type="button" class="btn" id="focus-exit">Back to full graph</button>
</div>
<div id="info-panel" class="card">
  <button type="button" id="info-close" aria-label="Close">&times;</button>
  <div id="info-content"></div>
</div>
<div id="doc-reader">
  <div class="reader-header">
    <h2 id="reader-title" dir="auto"></h2>
    <button type="button" id="reader-close" aria-label="Close reader">&times;</button>
  </div>
  <div class="reader-body">
    <div id="reader-sidebar"></div>
    <div id="reader-text" dir="auto"></div>
  </div>
</div>

<script>
(function() {
"use strict";
var graphData = __GRAPH_DATA__;

// Everything in graphData comes from documents the user did not write. It is
// only ever put on the page with textContent or drawn on the canvas.
if (!graphData || typeof graphData !== 'object') graphData = {};
var rawNodes = Array.isArray(graphData.nodes) ? graphData.nodes : [];
var rawEdges = Array.isArray(graphData.edges) ? graphData.edges : [];
var docs = new Map();
if (graphData.documents && typeof graphData.documents === 'object' && !Array.isArray(graphData.documents)) {
  Object.keys(graphData.documents).forEach(function(k) {
    var d = graphData.documents[k];
    if (d && typeof d === 'object') docs.set(k, d);
  });
}

// --- Theme: colours for the canvas, matching the CSS variables above ---
// People, organizations and places are the three hues that stay apart from each
// other for colour-blind readers when every pair is on screen at once. Everything
// else (money, dates, identifiers) is drawn in neutral ink.
var THEMES = {
  dark: {name: 'dark', surface: '#1a1a19', ink: '#f0efec', ink2: '#c3c2b7', muted: '#898781', hairline: '#2c2c2a', baseline: '#383835',
         hull: 'rgba(255,255,255,0.028)', tint: 0.42, onTint: '#ffffff',
         person: '#3987e5', organization: '#d95926', location: '#199e70'},
  light: {name: 'light', surface: '#fcfcfb', ink: '#0b0b0b', ink2: '#52514e', muted: '#898781', hairline: '#e1e0d9', baseline: '#c3c2b7',
          hull: 'rgba(11,11,11,0.028)', tint: 0.16, onTint: '#0b0b0b',
          person: '#2a78d6', organization: '#eb6834', location: '#1baf7a'}
};
var darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
function themeName() {
  var forced = document.documentElement.getAttribute('data-theme');
  if (forced === 'light' || forced === 'dark') return forced;
  return darkQuery && darkQuery.matches ? 'dark' : 'light';
}
var T = THEMES[themeName()];
var FONT_STACK = "system-ui, -apple-system, 'Segoe UI', Roboto, 'Noto Sans', 'Noto Sans Arabic', 'Noto Sans Hebrew', 'Noto Sans CJK SC', sans-serif";

function hexRgb(h) { return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]; }
// Opaque blend of colour a over colour b: t = 1 is all a.
function mix(a, b, t) {
  var x = hexRgb(a), y = hexRgb(b);
  return 'rgb(' + Math.round(x[0] * t + y[0] * (1 - t)) + ',' + Math.round(x[1] * t + y[1] * (1 - t)) + ',' + Math.round(x[2] * t + y[2] * (1 - t)) + ')';
}

// actor: one of the three coloured kinds. Others are attributes of a relationship
// (how much, when, which number) and are drawn small, hollow and neutral.
var TYPE_STYLE = new Map([
  ['person', {shape: 'disc', icon: 'person', name: 'Person', actor: true}],
  ['organization', {shape: 'square', icon: 'building', name: 'Organization', actor: true}],
  ['location', {shape: 'diamond', icon: 'pin', name: 'Location', actor: true}],
  ['money', {shape: 'disc', icon: 'banknote', name: 'Money', actor: false}],
  ['date', {shape: 'square', icon: 'calendar', name: 'Date', actor: false}],
  ['document_id', {shape: 'square', icon: null, name: 'Document ID', actor: false}],
  ['email', {shape: 'disc', icon: null, name: 'Email', actor: false}],
  ['phone', {shape: 'disc', icon: null, name: 'Phone', actor: false}],
  ['address', {shape: 'diamond', icon: null, name: 'Address', actor: false}]
]);
function styleOf(type) { return TYPE_STYLE.get(type) || {shape: 'disc', icon: null, name: type, actor: false}; }
function colorOf(type) { var st = TYPE_STYLE.get(type); return (st && st.actor) ? T[type] : T.muted; }

// --- Mark shapes and icons, all drawn with canvas paths ---
function roundedSquare(c, x, y, h, rad) {
  c.moveTo(x - h + rad, y - h);
  c.arcTo(x + h, y - h, x + h, y + h, rad); c.arcTo(x + h, y + h, x - h, y + h, rad);
  c.arcTo(x - h, y + h, x - h, y - h, rad); c.arcTo(x - h, y - h, x + h, y - h, rad);
  c.closePath();
}
function roundedDiamond(c, x, y, h, rad) {
  // A square turned 45 degrees: corner points on the axes, rounded.
  c.moveTo(x - h / 2, y - h / 2);
  c.arcTo(x, y - h, x + h, y, rad); c.arcTo(x + h, y, x, y + h, rad);
  c.arcTo(x, y + h, x - h, y, rad); c.arcTo(x - h, y, x, y - h, rad);
  c.closePath();
}
// Shapes cover roughly the area of a circle of radius r.
function shapePath(c, shape, x, y, r) {
  c.beginPath();
  if (shape === 'square') roundedSquare(c, x, y, r * 0.9, r * 0.3);
  else if (shape === 'diamond') roundedDiamond(c, x, y, r * 1.22, r * 0.28);
  else c.arc(x, y, r, 0, Math.PI * 2);
}
// Icons live in a box from -1 to 1 and are filled with the even-odd rule, so
// inner shapes (windows, the hole of a pin) show the mark's colour through.
var ICONS = {
  person: function(c) {
    c.moveTo(0.34, -0.42); c.arc(0, -0.42, 0.34, 0, Math.PI * 2);
    c.moveTo(-0.68, 0.8); c.bezierCurveTo(-0.68, 0.02, 0.68, 0.02, 0.68, 0.8); c.closePath();
  },
  building: function(c) {
    c.rect(-0.56, -0.74, 1.12, 1.48);
    c.rect(-0.34, -0.5, 0.24, 0.24); c.rect(0.1, -0.5, 0.24, 0.24);
    c.rect(-0.34, -0.1, 0.24, 0.24); c.rect(0.1, -0.1, 0.24, 0.24);
    c.rect(-0.14, 0.34, 0.28, 0.4);
  },
  pin: function(c) {
    c.moveTo(0, 0.84);
    c.bezierCurveTo(-0.2, 0.46, -0.56, 0.16, -0.56, -0.24);
    c.arc(0, -0.24, 0.56, Math.PI, 0);
    c.bezierCurveTo(0.56, 0.16, 0.2, 0.46, 0, 0.84); c.closePath();
    c.moveTo(0.22, -0.24); c.arc(0, -0.24, 0.22, 0, Math.PI * 2);
  },
  banknote: function(c) {
    c.rect(-0.82, -0.48, 1.64, 0.96);
    c.moveTo(0.24, 0); c.arc(0, 0, 0.24, 0, Math.PI * 2);
    c.rect(-0.66, -0.1, 0.16, 0.2); c.rect(0.5, -0.1, 0.16, 0.2);
  },
  calendar: function(c) {
    c.rect(-0.64, -0.52, 1.28, 1.18);
    c.rect(-0.46, -0.14, 0.92, 0.62);
    c.rect(-0.4, -0.78, 0.18, 0.4); c.rect(0.22, -0.78, 0.18, 0.4);
  }
};
function drawIcon(c, name, x, y, size, color) {
  var fn = ICONS[name];
  if (!fn) return;
  c.save(); c.translate(x, y); c.scale(size, size);
  c.beginPath(); fn(c); c.fillStyle = color; c.fill('evenodd');
  c.restore();
}

// --- Initials ---
var LEGAL_FORMS = new Set(['LLC', 'LTD', 'LTDA', 'INC', 'CO', 'CORP', 'CORPORATION', 'SA', 'SAS', 'A\u015e', 'GMBH', 'AG', 'PLC', 'LLP', 'LP',
  '\u041e\u041e\u041e', '\u041e\u0410\u041e', '\u0417\u0410\u041e', '\u041f\u0410\u041e', '\u0410\u041e']);
var CJK_RE = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]/;
var ARABIC_RE = /[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]/;
var RTL_RE = /[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]/;
// Up to two characters that stand for a name inside a large mark. Legal forms
// and punctuation are dropped first. Works on code points, never UTF-16 halves.
function initialsOf(label, type) {
  var s = String(label);
  s = s.replace(/\u05d1\u05e2[\u05f4"']\u05de/g, ' ');
  s = s.replace(/\u80a1\u4efd\u6709\u9650\u516c\u53f8|\u6709\u9650\u8d23\u4efb\u516c\u53f8|\u6709\u9650\u516c\u53f8|\u96c6\u56e2|\u516c\u53f8/g, ' ');
  s = s.replace(/[\u00ab\u00bb\u201c\u201d\u201e\u2039\u203a()\[\]{}]/g, ' ');
  var words = s.split(/[\s,;\/]+/).map(function(w) {
    return w.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '');
  }).filter(function(w) { return w.length > 0; });
  var kept = words.filter(function(w) { return !LEGAL_FORMS.has(w.replace(/\./g, '').toUpperCase()); });
  if (kept.length) words = kept;
  if (!words.length) return '';
  var first = Array.from(words[0])[0];
  if (CJK_RE.test(first)) return Array.from(words.join('')).slice(0, type === 'person' ? 1 : 2).join('');
  var a = Array.from(words[0].toUpperCase())[0];
  var b = words.length > 1 ? Array.from(words[1].toUpperCase())[0] : '';
  // Arabic letters join when adjacent; a zero-width non-joiner keeps the two initials apart.
  if (b && ARABIC_RE.test(a)) return a + '\u200c' + b;
  return a + b;
}

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
// Text from documents: let the browser pick the direction from the text itself.
function docText(tag, cls, text) { var e = el(tag, cls, text); e.dir = 'auto'; return e; }
function paintSwatch(c) {
  var type = c.getAttribute('data-type'), g = c.getContext('2d'), st = styleOf(type), col = colorOf(type);
  g.setTransform(2, 0, 0, 2, 0, 0); g.clearRect(0, 0, 18, 18);
  drawMark(g, st, col, 9, 9, st.actor ? 7.5 : 5, st.actor ? 'medium' : 'small', '', false);
}
function swatch(type) {
  var c = document.createElement('canvas');
  c.className = 'swatch'; c.width = 36; c.height = 36;
  c.setAttribute('data-type', type);
  paintSwatch(c);
  return c;
}
function pct(x) { return Math.round(x * 100) + '%'; }
function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
function safeUrl(u) { return (typeof u === 'string' && /^https?:\/\//i.test(u)) ? u : null; }
function clip(s, n) { var cps = Array.from(s); return cps.length > n ? cps.slice(0, n - 1).join('') + '\u2026' : s; }

// One mark. tier: 'large' (initials on a tinted fill with a type badge),
// 'medium' (type icon), 'small' (plain). Attributes are hollow and neutral.
function drawMark(c, st, col, x, y, r, tier, initials, rtl) {
  // Ring in the surface colour so touching marks and crossing lines stay apart.
  shapePath(c, st.shape, x, y, r + 2); c.fillStyle = T.surface; c.fill();
  if (!st.actor) {
    shapePath(c, st.shape, x, y, Math.max(r - 0.75, 1)); c.fillStyle = T.surface; c.fill();
    c.strokeStyle = col; c.lineWidth = 1.5; c.stroke();
    if (tier !== 'small' && st.icon) drawIcon(c, st.icon, x, y, r * 0.5, col);
    return;
  }
  if (tier === 'large') {
    shapePath(c, st.shape, x, y, r); c.fillStyle = mix(col, T.surface, T.tint); c.fill();
    shapePath(c, st.shape, x, y, r - 1); c.strokeStyle = col; c.lineWidth = 2; c.stroke();
    if (initials) {
      var n = Array.from(initials.replace('\u200c', '')).length;
      c.font = '600 ' + Math.round(r * (n > 1 ? 0.78 : 0.95)) + 'px ' + FONT_STACK;
      c.textAlign = 'center'; c.textBaseline = 'middle'; c.direction = rtl ? 'rtl' : 'ltr';
      c.fillStyle = T.onTint; c.fillText(initials, x, y + r * 0.04);
      c.direction = 'ltr';
    } else if (st.icon) {
      drawIcon(c, st.icon, x, y, r * 0.55, T.onTint);
    }
    // Type badge on the rim, so the kind is not carried by colour and outline alone.
    if (initials && st.icon) {
      var bx = x + r * 0.74, by = y - r * 0.74, br = Math.max(6, r * 0.34);
      c.beginPath(); c.arc(bx, by, br + 2, 0, Math.PI * 2); c.fillStyle = T.surface; c.fill();
      c.beginPath(); c.arc(bx, by, br, 0, Math.PI * 2); c.fillStyle = col; c.fill();
      drawIcon(c, st.icon, bx, by, br * 0.62, '#ffffff');
    }
    return;
  }
  shapePath(c, st.shape, x, y, r); c.fillStyle = col; c.fill();
  if (tier === 'medium' && st.icon) drawIcon(c, st.icon, x, y, r * 0.68, '#ffffff');
}

// --- Model: one node per entity (same type and label), however many mentions ---
var NODES = [], nodeByKey = new Map(), nodeById = new Map(), mentionCount = 0;
rawNodes.forEach(function(m) {
  if (!m || typeof m !== 'object') return;
  var label = String(m.label === undefined || m.label === null ? '' : m.label);
  var type = String(m.type === undefined || m.type === null ? 'unknown' : m.type);
  var key = type + '\u0000' + label;
  var n = nodeByKey.get(key);
  if (!n) {
    var st = styleOf(type);
    n = {key: key, label: label, type: type, st: st, attr: !st.actor, mentions: [], conf: 0, links: [], degree: 0, idx: 0,
         x: 0, y: 0, vx: 0, vy: 0, r: 5, q: 0, sx: 0, sy: 0, sr: 0, tier: 'small', vis: true, must: false, moved: false, com: -1,
         rtl: RTL_RE.test(label), short: clip(label, 30), initials: st.actor ? initialsOf(label, type) : '', lw: 0, lwb: 0, rels: null};
    nodeByKey.set(key, n); NODES.push(n);
  }
  var conf = (typeof m.confidence === 'number' && isFinite(m.confidence)) ? Math.max(0, Math.min(1, m.confidence)) : 1;
  n.mentions.push({
    docId: (m.document_id === undefined || m.document_id === null) ? '' : String(m.document_id),
    page: (typeof m.page_number === 'number' && isFinite(m.page_number)) ? m.page_number : null,
    conf: conf,
    context: typeof m.context === 'string' ? m.context : ''
  });
  if (conf > n.conf) n.conf = conf;
  mentionCount++;
  if (m.id !== undefined && m.id !== null) nodeById.set(String(m.id), n);
});
// Order by type and label, not by database row order, so the same case always
// produces the same layout.
NODES.sort(function(a, b) { return a.key < b.key ? -1 : (a.key > b.key ? 1 : 0); });
NODES.forEach(function(n, i) { n.idx = i; n.docCount = new Set(n.mentions.map(function(m) { return m.docId; })).size; });

// Relationships between the same two entities are drawn as one line. Every
// relationship row is still counted and listed in the details panel.
var MONEY_FLOW_RE = /paid|pay|received|receiv|transfer|fund|loan|grant|invoice/i;
var LINKS = [], linkByPair = new Map(), relationshipCount = 0, selfLoops = 0;
rawEdges.forEach(function(e) {
  if (!e || typeof e !== 'object') return;
  var s = nodeById.get(String(e.source)), t = nodeById.get(String(e.target));
  if (!s || !t) return;
  relationshipCount++;
  if (s === t) { selfLoops++; return; }
  var a = s.idx < t.idx ? s : t, b = (a === s) ? t : s, pk = a.idx + ':' + b.idx;
  var l = linkByPair.get(pk);
  if (!l) {
    l = {s: a, t: b, weight: 0, rels: [], dist: 0, str: 0, bias: 0, money: false, toS: false, toT: false};
    linkByPair.set(pk, l); LINKS.push(l);
    a.links.push({node: b, link: l}); b.links.push({node: a, link: l});
  }
  l.weight++;
  var rtype = (typeof e.type === 'string' && e.type) ? e.type : 'related';
  if (MONEY_FLOW_RE.test(rtype)) l.money = true;
  // Which end the relationship points at.
  if (t === l.t) l.toT = true; else l.toS = true;
  if (!l.rels.some(function(r) { return r.type === rtype && r.from === s; })) l.rels.push({type: rtype, from: s});
});
LINKS.sort(function(a, b) { return a.s.idx - b.s.idx || a.t.idx - b.t.idx; });
NODES.forEach(function(n) {
  n.links.sort(function(a, b) { return a.node.idx - b.node.idx; });
  n.degree = n.links.length;
});
// The relationship types an entity takes part in, most frequent first.
function topRels(n) {
  if (n.rels) return n.rels;
  var tally = new Map();
  n.links.forEach(function(x) { x.link.rels.forEach(function(r) { tally.set(r.type, (tally.get(r.type) || 0) + 1); }); });
  n.rels = Array.from(tally.entries()).sort(function(a, b) { return b[1] - a[1] || (a[0] < b[0] ? -1 : 1); });
  return n.rels;
}

var docIds = new Set();
NODES.forEach(function(n) { n.mentions.forEach(function(m) { if (m.docId) docIds.add(m.docId); }); });
document.getElementById('stats').textContent =
  plural(NODES.length, 'entity', 'entities') + ' \u00b7 ' + plural(mentionCount, 'mention', 'mentions') + ' \u00b7 ' +
  plural(relationshipCount, 'relationship', 'relationships') + ' \u00b7 ' + plural(docIds.size, 'document', 'documents');
if (NODES.length === 0) document.getElementById('empty').style.display = 'block';

var typeCounts = new Map();
NODES.forEach(function(n) { typeCounts.set(n.type, (typeCounts.get(n.type) || 0) + 1); });
var types = Array.from(typeCounts.keys()).sort(function(a, b) {
  return (styleOf(b).actor ? 1 : 0) - (styleOf(a).actor ? 1 : 0) || typeCounts.get(b) - typeCounts.get(a) || (a < b ? -1 : 1);
});

// --- Communities: which entities belong together ---
// Label propagation in a fixed order (best connected first, ties by content
// order), then groups joined by several relationships are merged. No randomness.
var SIM = NODES.filter(function(n) { return n.degree > 0; });
var COMMUNITIES = [];
(function detectCommunities() {
  var order = SIM.slice().sort(function(a, b) { return b.degree - a.degree || a.idx - b.idx; });
  order.forEach(function(n) { n.com = n.idx; });
  for (var round = 0; round < 30; round++) {
    var changed = false;
    order.forEach(function(n) {
      var tally = new Map(), best = -1, bestW = 0;
      n.links.forEach(function(x) { tally.set(x.node.com, (tally.get(x.node.com) || 0) + x.link.weight); });
      tally.forEach(function(w, c) { if (w > bestW || (w === bestW && (c === n.com || (best !== n.com && c < best)))) { best = c; bestW = w; } });
      if (best !== -1 && best !== n.com) { n.com = best; changed = true; }
    });
    if (!changed) break;
  }
  // Merge pass on the (small) graph of groups.
  var ids = Array.from(new Set(SIM.map(function(n) { return n.com; }))).sort(function(a, b) { return a - b; });
  var index = new Map(); ids.forEach(function(c, i) { index.set(c, i); });
  var k = ids.length, between = [], i;
  for (i = 0; i < k; i++) between.push(new Map());
  LINKS.forEach(function(l) {
    var a = index.get(l.s.com), b = index.get(l.t.com);
    if (a !== b) { between[a].set(b, (between[a].get(b) || 0) + l.weight); between[b].set(a, (between[b].get(a) || 0) + l.weight); }
  });
  // Two groups joined by several relationships, relative to the smaller one's
  // size, are one strand of the story; a couple of bridges do not join them.
  var parent = ids.map(function(_, q) { return q; }), alive = ids.map(function() { return true; });
  var count = new Array(k).fill(0);
  SIM.forEach(function(n) { count[index.get(n.com)]++; });
  for (var guard = 0; guard < k; guard++) {
    var bi = -1, bj = -1, bestRatio = 0;
    for (i = 0; i < k; i++) {
      if (!alive[i]) continue;
      between[i].forEach(function(w, jj) {
        if (jj <= i || !alive[jj] || w < 3) return;
        var ratio = w / Math.min(count[i], count[jj]);
        if (ratio >= 0.12 && ratio > bestRatio) { bestRatio = ratio; bi = i; bj = jj; }
      });
    }
    if (bi < 0) break;
    alive[bj] = false; parent[bj] = bi; count[bi] += count[bj];
    between[bj].forEach(function(w, o) {
      if (o === bi) return;
      between[bi].set(o, (between[bi].get(o) || 0) + w);
      between[o].set(bi, (between[o].get(bi) || 0) + w); between[o].delete(bj);
    });
    between[bi].delete(bj);
  }
  function root(q) { while (parent[q] !== q) q = parent[q]; return q; }
  var groups = new Map();
  SIM.forEach(function(n) {
    var g = root(index.get(n.com));
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(n);
  });
  var minSize = Math.max(5, Math.ceil(SIM.length * 0.02));
  groups.forEach(function(members) {
    var top = members.slice().sort(function(a, b) { return b.degree - a.degree || a.idx - b.idx; })[0];
    var c = {members: members, top: top, sizeable: members.length >= minSize, hull: null, caption: null};
    members.forEach(function(n) { n.com = COMMUNITIES.length; });
    COMMUNITIES.push(c);
  });
})();

// --- Filters ---
var hiddenTypes = new Set(), minConf = 0;
var focusMode = false, focusVisible = new Set(), focusDepth = 1;
var selectedNode = null, hoverNode = null, dragNode = null;

function isNodeVisible(n) {
  if (hiddenTypes.has(n.type)) return false;
  if (n.conf < minConf) return false;
  if (focusMode && !focusVisible.has(n)) return false;
  return true;
}

var legendEl = document.getElementById('legend');
var legendButtons = new Map();
var lastGroup = null;
types.forEach(function(t) {
  var group = styleOf(t).actor ? 'Who and where' : 'Details';
  if (group !== lastGroup) { legendEl.appendChild(el('div', 'legend-group', group)); lastGroup = group; }
  var item = el('button', 'legend-item');
  item.type = 'button';
  item.setAttribute('data-type', t);
  item.setAttribute('aria-pressed', 'true');
  item.title = 'Show or hide ' + styleOf(t).name;
  item.appendChild(swatch(t));
  item.appendChild(docText('span', 'legend-name', styleOf(t).name));
  item.appendChild(el('span', 'legend-count', String(typeCounts.get(t))));
  item.addEventListener('click', function() {
    if (hiddenTypes.has(t)) hiddenTypes.delete(t); else hiddenTypes.add(t);
    filtersChanged();
  });
  legendButtons.set(t, item);
  legendEl.appendChild(item);
});

var confRange = document.getElementById('conf-range');
confRange.addEventListener('input', function() {
  minConf = (parseInt(confRange.value, 10) || 0) / 100;
  filtersChanged();
});
document.getElementById('reset-btn').addEventListener('click', function() {
  hiddenTypes.clear(); minConf = 0; confRange.value = '0';
  if (focusMode) exitFocusMode(); else filtersChanged();
});

function filtersChanged() {
  var shown = 0;
  NODES.forEach(function(n) { if (isNodeVisible(n)) shown++; });
  legendButtons.forEach(function(btn, t) { btn.setAttribute('aria-pressed', hiddenTypes.has(t) ? 'false' : 'true'); });
  document.getElementById('conf-value').textContent = pct(minConf);
  document.getElementById('filter-count').textContent = 'Showing ' + shown + ' of ' + plural(NODES.length, 'entity', 'entities');
  if (hoverNode && !isNodeVisible(hoverNode)) setHover(null);
  if (selectedNode && !isNodeVisible(selectedNode)) deselect(); else if (selectedNode) showNodeInfo(selectedNode);
  hullsDirty = true;
  if (tableMode) renderTable();
  requestDraw();
}

// --- Node size ---
var sizeBy = 'degree';
function applySizes() {
  // Area grows with the value, relative to the largest in this graph, so the
  // best-connected entities are always the biggest marks on the page.
  function val(n) { return sizeBy === 'mentions' ? n.mentions.length - 1 : n.degree; }
  var top = 16;
  NODES.forEach(function(n) { if (val(n) > top) top = val(n); });
  NODES.forEach(function(n) {
    var share = Math.sqrt(Math.max(0, val(n)) / top);
    // Attributes stay small whatever they are attached to.
    n.r = n.attr ? 3.6 + 3 * share : 5.5 + 17.5 * share;
  });
}
applySizes();
document.getElementById('size-by').addEventListener('change', function() {
  sizeBy = this.value === 'mentions' ? 'mentions' : 'degree';
  applySizes(); reheat();
});

// --- Layout: force simulation in world coordinates, centred on (0, 0) ---
var canvas = document.getElementById('graph');
var ctx = canvas.getContext('2d');
var W = 0, H = 0, DPR = 1;
function resize() {
  DPR = Math.max(1, Math.min(window.devicePixelRatio || 1, 3));
  W = window.innerWidth; H = window.innerHeight;
  canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
}
resize();

var LABEL_FONT = "12px " + FONT_STACK, HUB_FONT = "600 13px " + FONT_STACK, EDGE_FONT = "11px " + FONT_STACK, LABEL_H = 16;
var labelOrder = NODES.slice().sort(function(a, b) {
  return (a.attr ? 1 : 0) - (b.attr ? 1 : 0) || b.degree - a.degree || b.mentions.length - a.mentions.length || a.idx - b.idx;
});
// The best-connected entities are always named, under their mark. The layout
// keeps a lane the size of that name clear of other marks.
var MUST_COUNT = Math.max(5, Math.ceil(NODES.length * 0.012));
var MUST = labelOrder.slice(0, MUST_COUNT).filter(function(n) { return n.degree > 1 && !n.attr; });
ctx.font = HUB_FONT;
MUST.forEach(function(n) { n.must = true; n.lwb = ctx.measureText(n.short).width; });
// Zoomed out, marks are drawn up to MARK_BOOST times larger than true scale so they
// stay readable. Collision keeps the same extra distance, so they still do not touch.
var MARK_BOOST = 1.3;
function markRadius(r, k) { return Math.max(r * k * (k < 1 ? Math.min(1 / Math.sqrt(k), MARK_BOOST) : 1), 2.5); }
// What a mark carries depends on how large it is drawn.
var TIER_LARGE = 13, TIER_MEDIUM = 7;
function tierOf(sr) { return sr >= TIER_LARGE ? 'large' : (sr >= TIER_MEDIUM ? 'medium' : 'small'); }
var CHARGE = 70, CHARGE_RANGE2 = 900 * 900, LINK_DISTANCE = 30, COLLIDE_PAD = 7, COLLIDE = 0.6, GRAVITY = 0.06, DAMPING = 0.6, MAX_SPEED = 80;
// d3-style cooling (#58): forces scale with alpha, which decays to ALPHA_MIN in ~300 ticks, then the loop stops.
var alpha = 1, ALPHA_MIN = 0.001, ALPHA_DECAY = 1 - Math.pow(ALPHA_MIN, 1/300), ALPHA_REHEAT = 0.3;
var ticks = 0, startedAt = performance.now(), settleMs = null;

// Deterministic start: best-connected entities in the middle, on a sunflower spiral.
// No Math.random, so the same export always opens with the same layout.
SIM.slice().sort(function(a, b) { return b.degree - a.degree || a.idx - b.idx; }).forEach(function(n, i) {
  var rad = 14 * Math.sqrt(0.5 + i), ang = i * 2.399963229728653;
  n.x = rad * Math.cos(ang); n.y = rad * Math.sin(ang);
});
LINKS.forEach(function(l) {
  var ds = l.s.degree, dt = l.t.degree;
  l.str = 1 / Math.min(ds, dt);
  l.bias = ds / (ds + dt);
});
function prepareForces() {
  NODES.forEach(function(n) { n.q = CHARGE * (1 + 0.25 * Math.sqrt(n.degree)); });
  // Links into a busy entity are longer, so its neighbours form a ring with room for its name.
  LINKS.forEach(function(l) { l.dist = LINK_DISTANCE + l.s.r + l.t.r + 7 * Math.sqrt(Math.max(l.s.degree, l.t.degree)); });
}
prepareForces();

// Entities with no relationship take no part in the simulation. They are set
// out in a block under the graph, so they cannot push the connected part
// around or stretch the view, and it is plain that nothing ties them in.
var ISOLATES = NODES.filter(function(n) { return n.degree === 0; }).sort(function(a, b) {
  return (a.attr ? 1 : 0) - (b.attr ? 1 : 0) || (a.type < b.type ? -1 : a.type > b.type ? 1 : 0) || a.idx - b.idx;
});
var TRAY_CELL = 26, tray = null;
function layoutIsolates() {
  tray = null;
  if (!ISOLATES.length) return;
  var minX = Infinity, maxX = -Infinity, maxY = -Infinity, i, n;
  for (i = 0; i < SIM.length; i++) {
    n = SIM[i];
    if (n.x - n.r < minX) minX = n.x - n.r;
    if (n.x + n.r > maxX) maxX = n.x + n.r;
    if (n.y + n.r > maxY) maxY = n.y + n.r;
  }
  var cols, x0, y0;
  if (SIM.length) {
    cols = Math.max(8, Math.min(ISOLATES.length, Math.floor((maxX - minX) / TRAY_CELL)));
    x0 = (minX + maxX) / 2 - (cols - 1) * TRAY_CELL / 2; y0 = maxY + 100;
  } else {
    cols = Math.max(1, Math.ceil(Math.sqrt(ISOLATES.length * W / Math.max(H, 1))));
    x0 = -(cols - 1) * TRAY_CELL / 2; y0 = -Math.ceil(ISOLATES.length / cols) * TRAY_CELL / 2;
  }
  for (i = 0; i < ISOLATES.length; i++) {
    n = ISOLATES[i];
    if (n.moved) continue;
    n.x = x0 + (i % cols) * TRAY_CELL; n.y = y0 + Math.floor(i / cols) * TRAY_CELL;
  }
  tray = {x: x0 - TRAY_CELL / 2, y: y0 - 44};
}

function tick() {
  var n = SIM.length, i, j, a, b, l, dx, dy, d2, d, f, min;
  // Wide windows get a wide layout: pull harder on the short axis.
  var aspect = Math.max(0.6, Math.min(1.8, Math.sqrt(W / Math.max(H, 1))));
  var gx = GRAVITY / aspect * alpha, gy = GRAVITY * aspect * alpha;
  for (i = 0; i < LINKS.length; i++) {
    l = LINKS[i]; a = l.s; b = l.t;
    dx = b.x + b.vx - a.x - a.vx; dy = b.y + b.vy - a.y - a.vy;
    d = Math.sqrt(dx * dx + dy * dy) || 0.001;
    f = (d - l.dist) / d * alpha * l.str; dx *= f; dy *= f;
    b.vx -= dx * l.bias; b.vy -= dy * l.bias;
    a.vx += dx * (1 - l.bias); a.vy += dy * (1 - l.bias);
  }
  for (i = 0; i < n; i++) {
    a = SIM[i];
    for (j = i + 1; j < n; j++) {
      b = SIM[j];
      dx = b.x - a.x; dy = b.y - a.y; d2 = dx * dx + dy * dy;
      if (d2 > CHARGE_RANGE2) continue;
      if (d2 < 0.01) { dx = 0.1 * ((i + j) % 2 ? 1 : -1); dy = 0.1; d2 = 0.02; }
      // Repulsion, scaled by alpha so it fades as the layout cools.
      f = alpha / Math.max(d2, 16);
      a.vx -= dx * f * b.q; a.vy -= dy * f * b.q;
      b.vx += dx * f * a.q; b.vy += dy * f * a.q;
      // Collision: not scaled by alpha, so overlaps are still pushed apart at the end.
      min = (a.r + b.r) * MARK_BOOST + COLLIDE_PAD;
      if (d2 < min * min) {
        d = Math.sqrt(d2); f = (min - d) / d * 0.5 * COLLIDE;
        a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
      }
    }
  }
  // Keep the lane under each must-name entity clear for its name. The name is a
  // fixed size on screen, so its size in layout units depends on the zoom. While
  // the view follows the layout, use the zoom it is heading for, worked out per
  // tick so the result does not depend on how many ticks fit in a frame.
  var fit = autoFit ? fitView() : null;
  var kk = Math.max(fit ? fit.k : view.k, 0.05), hw, hh, cy, ov;
  for (i = 0; i < MUST.length; i++) {
    a = MUST[i];
    hw = (a.lwb / 2 + 12) / kk; hh = (LABEL_H / 2 + 4) / kk;
    cy = a.y + (markRadius(a.r, kk) * 1.06 + 5 + LABEL_H / 2) / kk;
    for (j = 0; j < n; j++) {
      b = SIM[j];
      if (b === a) continue;
      dx = b.x - a.x; dy = b.y - cy; min = b.r * MARK_BOOST;
      if (Math.abs(dx) < hw + min && Math.abs(dy) < hh + min) {
        ov = hh + min - Math.abs(dy);
        b.vy += (dy >= 0 ? 1 : -1) * ov * 0.5;
      }
    }
  }
  for (i = 0; i < n; i++) {
    a = SIM[i];
    if (a === dragNode) { a.vx = 0; a.vy = 0; continue; }
    a.vx -= a.x * gx; a.vy -= a.y * gy;
    a.vx *= DAMPING; a.vy *= DAMPING;
    if (a.vx > MAX_SPEED) a.vx = MAX_SPEED; else if (a.vx < -MAX_SPEED) a.vx = -MAX_SPEED;
    if (a.vy > MAX_SPEED) a.vy = MAX_SPEED; else if (a.vy < -MAX_SPEED) a.vy = -MAX_SPEED;
    a.x += a.vx; a.y += a.vy;
  }
  alpha += ((dragNode ? ALPHA_REHEAT : 0) - alpha) * ALPHA_DECAY;
  ticks++;
  layoutIsolates();
  hullsDirty = true;
}
function simActive() { return alpha >= ALPHA_MIN || dragNode !== null; }
function reheat() { prepareForces(); alpha = Math.max(alpha, ALPHA_REHEAT); requestDraw(); }

// --- Community outlines: the convex hull of each group's marks, in layout units ---
var hullsDirty = true, HULL_PAD = 16;
function convexHull(pts) {
  pts.sort(function(a, b) { return a[0] - b[0] || a[1] - b[1]; });
  function cross(o, a, b) { return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]); }
  var lower = [], upper = [], i;
  for (i = 0; i < pts.length; i++) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], pts[i]) <= 0) lower.pop();
    lower.push(pts[i]);
  }
  for (i = pts.length - 1; i >= 0; i--) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], pts[i]) <= 0) upper.pop();
    upper.push(pts[i]);
  }
  lower.pop(); upper.pop();
  return lower.concat(upper);
}
function updateHulls() {
  hullsDirty = false;
  COMMUNITIES.forEach(function(c) {
    c.hull = null; c.shown = 0;
    if (!c.sizeable) return;
    var pts = [];
    c.members.forEach(function(n) {
      if (!isNodeVisible(n)) return;
      c.shown++;
      var rr = n.r * MARK_BOOST + HULL_PAD;
      for (var a = 0; a < 8; a++) pts.push([n.x + rr * Math.cos(a * Math.PI / 4), n.y + rr * Math.sin(a * Math.PI / 4)]);
    });
    if (c.shown >= 3 && isNodeVisible(c.top)) c.hull = convexHull(pts);
  });
}

// --- View: screen = world * k + (x, y) ---
var view = {x: 0, y: 0, k: 1}, target = null;
// Until the user pans or zooms, the view follows the layout so the whole graph stays on screen.
var autoFit = true;
var sidebarEl = document.getElementById('sidebar');

function fitView() {
  var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, any = false;
  for (var i = 0; i < NODES.length; i++) {
    var n = NODES[i];
    if (!isNodeVisible(n)) continue;
    any = true;
    if (n.x - n.r < minX) minX = n.x - n.r;
    if (n.x + n.r > maxX) maxX = n.x + n.r;
    if (n.y - n.r < minY) minY = n.y - n.r;
    if (n.y + n.r > maxY) maxY = n.y + n.r;
  }
  if (!any) return null;
  // Leave the sidebar and the search box clear when the window is wide enough to afford it.
  var left = W >= 760 ? sidebarEl.getBoundingClientRect().right + 28 : 16;
  var top = 78, right = 36, bottom = 34;
  var availW = Math.max(W - left - right, 50), availH = Math.max(H - top - bottom, 50);
  var k = Math.min(availW / Math.max(maxX - minX, 1), availH / Math.max(maxY - minY, 1), 2);
  k = Math.max(k, 0.02);
  return {k: k, x: left + availW / 2 - (minX + maxX) / 2 * k, y: top + availH / 2 - (minY + maxY) / 2 * k};
}
function fitToScreen(animate) {
  var f = fitView();
  if (!f) return;
  autoFit = true; framed = null;
  if (animate && !simActive()) { target = f; } else { view = f; target = null; }
  requestDraw();
}
function zoomToNode(n) {
  autoFit = false; framed = null;
  var k = Math.max(view.k, 1.5);
  var left = W >= 760 ? sidebarEl.getBoundingClientRect().right : 0;
  target = {k: k, x: left + (W - left) / 2 - n.x * k, y: H / 2 - n.y * k};
  requestDraw();
}
document.getElementById('fit-btn').addEventListener('click', function() { fitToScreen(true); });
window.addEventListener('resize', function() { resize(); requestDraw(); });

// --- Frame loop: stops once the layout has cooled and nothing is animating (#58) ---
var running = false;
function requestDraw() { if (!running) { running = true; requestAnimationFrame(frame); } }
function frame() {
  if (simActive()) {
    // Several ticks per frame while there is time, so a large graph settles in seconds, not minutes.
    var t0 = performance.now(), c = 0;
    do { tick(); c++; } while (alpha >= ALPHA_MIN && !dragNode && c < 30 && performance.now() - t0 < 10);
  }
  if (target) {
    var dx = target.x - view.x, dy = target.y - view.y, dk = target.k - view.k;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5 && Math.abs(dk) < 0.001) { view = target; target = null; }
    else { view = {x: view.x + dx * 0.2, y: view.y + dy * 0.2, k: view.k + dk * 0.2}; }
  } else if (autoFit) {
    var f = fitView();
    if (f) view = f;
  }
  render();
  if (simActive() || target) requestAnimationFrame(frame);
  else {
    running = false;
    if (settleMs === null) settleMs = performance.now() - startedAt;
  }
}

// --- Drawing ---
var lastLabels = [], lastEdgeLabels = [], lastCaptions = [], trayCaption = null;
// Attribute names (amounts, dates) show once the view is zoomed in this far.
var ATTR_LABEL_ZOOM = 1.5;

function render() {
  var i, n, l, k = view.k, vx = view.x, vy = view.y;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, W, H);

  var focus = hoverNode || selectedNode, hl = null;
  if (focus && !isNodeVisible(focus)) focus = null;
  if (focus) {
    hl = new Set([focus]);
    focus.links.forEach(function(x) { hl.add(x.node); });
  }
  for (i = 0; i < NODES.length; i++) {
    n = NODES[i];
    n.vis = isNodeVisible(n);
    n.sx = n.x * k + vx; n.sy = n.y * k + vy; n.sr = markRadius(n.r, k); n.tier = tierOf(n.sr);
  }

  // Community outlines, behind everything: a wash one step off the surface and a hairline.
  if (hullsDirty) updateHulls();
  ctx.lineJoin = 'round';
  COMMUNITIES.forEach(function(c) {
    if (!c.hull) return;
    ctx.beginPath();
    c.hull.forEach(function(p, q) { if (q === 0) ctx.moveTo(p[0] * k + vx, p[1] * k + vy); else ctx.lineTo(p[0] * k + vx, p[1] * k + vy); });
    ctx.closePath();
    ctx.fillStyle = T.hull; ctx.fill();
    ctx.strokeStyle = T.hairline; ctx.lineWidth = 1; ctx.stroke();
  });

  // Edges: hairlines. Links that carry money are a touch heavier; no extra hue.
  ctx.lineCap = 'round';
  var pass;
  ctx.globalAlpha = focus ? 0.12 : 1;
  for (pass = 0; pass < 2; pass++) {
    ctx.beginPath();
    for (i = 0; i < LINKS.length; i++) {
      l = LINKS[i];
      if (!l.s.vis || !l.t.vis) continue;
      if ((l.money || l.weight > 1) !== (pass === 1)) continue;
      ctx.moveTo(l.s.sx, l.s.sy); ctx.lineTo(l.t.sx, l.t.sy);
    }
    ctx.strokeStyle = T.baseline;
    ctx.lineWidth = pass === 1 ? 1.75 : 1;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  if (focus) drawFocusEdges(focus);

  // Marks: dimmed ones first, so highlighted ones sit on top; larger ones last.
  for (pass = 0; pass < 2; pass++) {
    for (i = 0; i < NODES.length; i++) {
      n = NODES[i];
      if (!n.vis) continue;
      var lit = !hl || hl.has(n);
      if (lit !== (pass === 1)) continue;
      if (n.sx < -40 || n.sx > W + 40 || n.sy < -40 || n.sy > H + 40) continue;
      if (!lit) {
        // De-emphasised: one flat shape at 18%, nothing inside it. Lines are cut
        // away under it first, the same job the surface ring does for a full mark.
        ctx.globalCompositeOperation = 'destination-out';
        shapePath(ctx, n.st.shape, n.sx, n.sy, n.sr + 1); ctx.fill();
        ctx.globalCompositeOperation = 'source-over';
        ctx.globalAlpha = 0.18;
        shapePath(ctx, n.st.shape, n.sx, n.sy, n.attr ? Math.max(n.sr - 0.75, 1) : n.sr);
        if (n.attr) { ctx.strokeStyle = T.muted; ctx.lineWidth = 1.5; ctx.stroke(); }
        else { ctx.fillStyle = colorOf(n.type); ctx.fill(); }
        ctx.globalAlpha = 1;
        continue;
      }
      drawMark(ctx, n.st, colorOf(n.type), n.sx, n.sy, n.sr, n.tier, n.initials, n.rtl);
      if (n === selectedNode || n === hoverNode) {
        shapePath(ctx, n.st.shape, n.sx, n.sy, n.sr + 4);
        ctx.strokeStyle = T.ink; ctx.lineWidth = 2; ctx.stroke();
      }
    }
  }
  ctx.globalAlpha = 1;
  if (tray && ISOLATES.some(function(x) { return x.vis; })) {
    ctx.font = "11px " + FONT_STACK; ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic'; ctx.direction = 'ltr';
    ctx.fillStyle = T.muted;
    var cap = 'No recorded relationship \u00b7 ' + ISOLATES.length;
    trayCaption = {x: tray.x * k + vx - 4, y: tray.y * k + vy - 2, w: ctx.measureText(cap).width + 8, h: 18, owner: {}, label: true};
    ctx.fillText(cap, trayCaption.x + 4, trayCaption.y + 14);
  } else { trayCaption = null; }
  drawLabels(focus, hl);
}

// The selected or hovered entity's links: full ink, with an arrowhead at the end each relationship points to.
function drawFocusEdges(focus) {
  ctx.strokeStyle = T.ink; ctx.fillStyle = T.ink;
  focus.links.forEach(function(x) {
    var o = x.node, l = x.link;
    if (!o.vis) return;
    ctx.lineWidth = l.money ? 2 : 1.25;
    ctx.beginPath(); ctx.moveTo(focus.sx, focus.sy); ctx.lineTo(o.sx, o.sy); ctx.stroke();
    var dx = o.sx - focus.sx, dy = o.sy - focus.sy, d = Math.sqrt(dx * dx + dy * dy) || 1;
    dx /= d; dy /= d;
    if (d < focus.sr + o.sr + 26) return;
    // One chevron per link, next to the neighbour, so forty of them do not pile up on the hub.
    var back = o.sr * (o.st.shape === 'diamond' ? 1.25 : 1.05) + 5;
    var toOther = (o === l.t) ? l.toT : l.toS, toFocus = (focus === l.t) ? l.toT : l.toS;
    function head(tx, ty, ux, uy) {
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(tx - ux * 7 - uy * 3.2, ty - uy * 7 + ux * 3.2);
      ctx.lineTo(tx - ux * 7 + uy * 3.2, ty - uy * 7 - ux * 3.2);
      ctx.closePath(); ctx.fill();
    }
    if (toOther) head(o.sx - dx * back, o.sy - dy * back, dx, dy);
    if (toFocus) head(o.sx - dx * (back + (toOther ? 16 : 7)), o.sy - dy * (back + (toOther ? 16 : 7)), -dx, -dy);
  });
}

// Names are placed greedily, most connected first, and only where they do not
// cover a mark or another name. Zooming in makes room for more of them.
function drawLabels(focus, hl) {
  var CELL = 64, grid = new Map(), i, n, s, c;
  function cells(x, y, w, h, fn) {
    var x0 = Math.floor(x / CELL), x1 = Math.floor((x + w) / CELL), y0 = Math.floor(y / CELL), y1 = Math.floor((y + h) / CELL);
    for (var cx = x0; cx <= x1; cx++) for (var cy = y0; cy <= y1; cy++) if (fn((cx + 512) * 4096 + cy + 512) === false) return false;
    return true;
  }
  function add(rect) { cells(rect.x, rect.y, rect.w, rect.h, function(key) { var a = grid.get(key); if (!a) { a = []; grid.set(key, a); } a.push(rect); }); }
  function free(x, y, w, h, owner, labelsOnly) {
    return cells(x, y, w, h, function(key) {
      var a = grid.get(key);
      if (!a) return true;
      for (var q = 0; q < a.length; q++) {
        var r = a[q];
        if (r.owner !== owner && (!labelsOnly || r.label) && x < r.x + r.w && x + w > r.x && y < r.y + r.h && y + h > r.y) return false;
      }
      return true;
    });
  }
  // How many other marks a box would cover.
  function covered(x, y, w, h, owner) {
    var seen = new Set();
    cells(x, y, w, h, function(key) {
      var a = grid.get(key);
      if (!a) return true;
      for (var q = 0; q < a.length; q++) {
        var r = a[q];
        if (r.owner !== owner && x < r.x + r.w && x + w > r.x && y < r.y + r.h && y + h > r.y) seen.add(r);
      }
      return true;
    });
    return seen.size;
  }
  function onScreen(x, y, w, h) { return x >= 2 && x + w <= W - 2 && y >= 2 && y + h <= H - 2; }
  function text(str, x, y, align, color, rtl) {
    ctx.direction = rtl ? 'rtl' : 'ltr'; ctx.textAlign = align;
    ctx.strokeStyle = T.surface; ctx.lineWidth = 3.5; ctx.strokeText(str, x, y);
    ctx.fillStyle = color; ctx.fillText(str, x, y);
  }

  for (i = 0; i < NODES.length; i++) {
    n = NODES[i];
    if (!n.vis || n.sx < -30 || n.sx > W + 30 || n.sy < -30 || n.sy > H + 30) continue;
    // With an entity in focus the rest is dimmed, and names may sit over dimmed marks.
    if (hl && !hl.has(n)) continue;
    var e = n.sr * (n.st.shape === 'diamond' ? 1.25 : 1.08) + 1;
    add({x: n.sx - e, y: n.sy - e, w: e * 2, h: e * 2, owner: n});
  }
  // Names stay clear of the panels that sit over the canvas.
  ['sidebar', 'search-box', 'info-panel'].forEach(function(id) {
    var b = document.getElementById(id).getBoundingClientRect();
    if (b.width && b.height) add({x: b.left - 4, y: b.top - 4, w: b.width + 8, h: b.height + 8, owner: {}, label: true});
  });
  if (trayCaption) add(trayCaption);
  ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
  lastLabels = []; lastEdgeLabels = []; lastCaptions = [];
  if (focus) focus.links.forEach(function(x) { x.node.relShown = false; });

  // Community captions: who the group is built around and how big it is.
  ctx.font = EDGE_FONT;
  var graphMidY = 0, hullCount = 0;
  COMMUNITIES.forEach(function(com) {
    if (!com.hull) return;
    var lo = Infinity, hi = -Infinity;
    com.hull.forEach(function(p) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; });
    graphMidY += ((lo + hi) / 2) * view.k + view.y; hullCount++;
  });
  graphMidY = hullCount ? graphMidY / hullCount : H / 2;
  COMMUNITIES.forEach(function(com) {
    if (!com.hull) return;
    if (focus && COMMUNITIES[focus.com] !== com) return;
    var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    com.hull.forEach(function(p) {
      var px = p[0] * view.k + view.x, py = p[1] * view.k + view.y;
      if (px < minX) minX = px; if (px > maxX) maxX = px; if (py < minY) minY = py; if (py > maxY) maxY = py;
    });
    var str = clip(com.top.label, 28) + ' \u00b7 ' + plural(com.shown, 'entity', 'entities');
    var w = ctx.measureText(str).width + 10, h = 16, mid = (minX + maxX) / 2;
    // Prefer the side of the outline that faces away from the middle of the
    // graph, so a caption does not sit between two groups.
    var above = [mid - w / 2, minY - h - 2], below = [mid - w / 2, maxY + 2];
    var outer = (minY + maxY) / 2 <= graphMidY ? [above, below] : [below, above];
    var spots = [outer[0], outer[1], [minX, minY - h - 2], [maxX - w, minY - h - 2], [mid - w / 2, minY + 4]];
    var spot = null, fewest = Infinity;
    for (s = 0; s < spots.length; s++) {
      if (!onScreen(spots[s][0], spots[s][1], w, h) || !free(spots[s][0], spots[s][1], w, h, com, true)) continue;
      var hits = covered(spots[s][0], spots[s][1], w, h, com);
      if (hits < fewest) { fewest = hits; spot = spots[s]; }
      if (hits === 0) break;
    }
    if (!spot) return;
    var rect = {x: spot[0], y: spot[1], w: w, h: h, owner: com, label: true, text: str, com: com};
    add(rect); lastCaptions.push(rect);
    text(str, rect.x + w / 2, rect.y + h / 2 + 0.5, 'center', T.muted, com.top.rtl);
  });

  var order = labelOrder;
  if (focus) {
    order = [focus].concat(focus.links.map(function(x) { return x.node; }).sort(function(a, b) {
      return (a.attr ? 1 : 0) - (b.attr ? 1 : 0) || b.degree - a.degree || a.idx - b.idx;
    }));
  }
  var zoomedIn = view.k >= ATTR_LABEL_ZOOM;
  for (i = 0; i < order.length; i++) {
    n = order[i];
    if (!n.vis || !n.short) continue;
    if (n.sx < 0 || n.sx > W || n.sy < 0 || n.sy > H) continue;
    // Amounts and dates are named only on demand: hover, a selected neighbour, or zoomed in.
    if (n.attr && !focus && !zoomedIn) continue;
    var strong = (n === focus), hub = strong || (n.must && !focus);
    if (hub) { if (!n.lwb) { ctx.font = HUB_FONT; n.lwb = ctx.measureText(n.short).width; } }
    else if (!n.lw) { ctx.font = LABEL_FONT; n.lw = ctx.measureText(n.short).width; }
    ctx.font = hub ? HUB_FONT : LABEL_FONT;
    var w = (hub ? n.lwb : n.lw) + 10, gap = n.sr * (n.st.shape === 'diamond' ? 1.25 : 1.06) + 4;
    // With an entity selected, each neighbour's name carries the relationship on a second line.
    var rel = null, bh = LABEL_H;
    if (selectedNode && focus === selectedNode && !strong) {
      var lk = null;
      for (s = 0; s < focus.links.length; s++) if (focus.links[s].node === n) { lk = focus.links[s].link; break; }
      if (lk) {
        rel = clip(lk.rels.map(function(r) { return (r.from === focus ? '\u2192 ' : '\u2190 ') + r.type; }).join(', '), 28);
        ctx.font = EDGE_FONT;
        var nameOnly = w;
        w = Math.max(w, ctx.measureText(rel).width + 10); bh = LABEL_H + 13;
        ctx.font = LABEL_FONT;
      }
    }
    var nameW = rel ? nameOnly : w, spots = null, spot = null;
    for (var attempt = 0; attempt < 2 && !spot; attempt++) {
      // Second attempt: the name alone, if name plus relationship found no room.
      if (attempt === 1) { if (!rel) break; rel = null; bh = LABEL_H; w = nameW; }
      spots = [
        {x: n.sx - w / 2, y: n.sy + gap, align: 'center'},
        {x: n.sx + gap, y: n.sy - bh / 2, align: 'left'},
        {x: n.sx - w / 2, y: n.sy - gap - bh, align: 'center'},
        {x: n.sx - gap - w, y: n.sy - bh / 2, align: 'right'}
      ];
      for (s = 0; s < spots.length; s++) {
        c = spots[s];
        if (!onScreen(c.x, c.y, w, bh)) continue;
        if (free(c.x, c.y, w, bh, n)) { spot = c; break; }
      }
    }
    // The best-connected entities must be named. With no clear spot beside the
    // mark, look a little further out and take the place that covers the fewest
    // other marks. Names never cover names.
    if (!spot && hub) {
      var far = gap + LABEL_H + 4, fewest = Infinity;
      spots.push(
        {x: n.sx - w / 2, y: n.sy + far, align: 'center'},
        {x: n.sx - w / 2, y: n.sy - far - LABEL_H, align: 'center'},
        {x: n.sx + gap * 0.8, y: n.sy + gap * 0.8, align: 'left'},
        {x: n.sx - gap * 0.8 - w, y: n.sy + gap * 0.8, align: 'right'},
        {x: n.sx + gap * 0.8, y: n.sy - gap * 0.8 - LABEL_H, align: 'left'},
        {x: n.sx - gap * 0.8 - w, y: n.sy - gap * 0.8 - LABEL_H, align: 'right'}
      );
      for (s = 0; s < spots.length; s++) {
        c = spots[s];
        if (!onScreen(c.x, c.y, w, LABEL_H)) continue;
        if (!free(c.x, c.y, w, LABEL_H, n, true)) continue;
        var hits = covered(c.x, c.y, w, LABEL_H, n);
        if (hits < fewest) { fewest = hits; spot = c; }
      }
      if (!spot && strong) spot = spots[0];
    }
    if (!spot) continue;
    var rect = {x: spot.x, y: spot.y, w: w, h: bh, owner: n, label: true, hub: hub};
    add(rect); lastLabels.push(rect);
    var tx = spot.align === 'center' ? spot.x + w / 2 : (spot.align === 'left' ? spot.x + 4 : spot.x + w - 4);
    text(n.short, tx, spot.y + LABEL_H / 2 + 0.5, spot.align, hub ? T.ink : T.ink2, n.rtl);
    if (rel) {
      ctx.font = EDGE_FONT;
      text(rel, tx, spot.y + LABEL_H + 6, spot.align, T.muted, false);
      lastEdgeLabels.push({x: spot.x, y: spot.y + LABEL_H, w: w, h: 13, text: rel, to: n, at: 'name'});
      n.relShown = true;
    } else { n.relShown = false; }
  }

  // Relationship types on the selected entity's links, where there is room.
  if (focus && focus === selectedNode) {
    ctx.font = EDGE_FONT;
    focus.links.slice().sort(function(a, b) { return b.node.degree - a.node.degree || a.node.idx - b.node.idx; }).forEach(function(x) {
      var o = x.node;
      if (!o.vis || o.relShown) return;
      var str = clip(x.link.rels.map(function(r) { return (r.from === focus ? '\u2192 ' : '\u2190 ') + r.type; }).join(', '), 28);
      var w = ctx.measureText(str).width + 8, h = 14, fr = [0.5, 0.64, 0.36, 0.78];
      for (var q = 0; q < fr.length; q++) {
        var mx = focus.sx + (o.sx - focus.sx) * fr[q] - w / 2, my = focus.sy + (o.sy - focus.sy) * fr[q] - h / 2;
        if (!onScreen(mx, my, w, h) || !free(mx, my, w, h, x)) continue;
        var rect = {x: mx, y: my, w: w, h: h, owner: x, label: true, text: str, from: focus, to: o, at: 'edge'};
        add(rect); lastEdgeLabels.push(rect);
        text(str, mx + w / 2, my + h / 2 + 0.5, 'center', T.ink2, RTL_RE.test(str));
        break;
      }
    });
  }
  ctx.direction = 'ltr';
}

// --- Pointer ---
var isPanning = false, lastMX = 0, lastMY = 0, dragMoved = false;
// Nearest mark within reach. Reach is at least 12 px, so every mark has a hit area of 24 px or more.
function findNodeAt(px, py) {
  var best = null, bestD = Infinity;
  for (var i = 0; i < NODES.length; i++) {
    var n = NODES[i];
    if (!isNodeVisible(n)) continue;
    var dx = px - n.sx, dy = py - n.sy, d = Math.sqrt(dx * dx + dy * dy), reach = Math.max(n.sr + 4, 12);
    if (d <= reach && d - n.sr < bestD) { best = n; bestD = d - n.sr; }
  }
  return best;
}

var tooltip = document.getElementById('tooltip');
function setHover(n, px, py) {
  if (n !== hoverNode) {
    hoverNode = n;
    tooltip.textContent = '';
    if (n) {
      tooltip.appendChild(docText('div', 'tt-title', n.label));
      tooltip.appendChild(el('div', '', styleOf(n.type).name + ' \u00b7 ' + plural(n.degree, 'connection', 'connections') + ' \u00b7 ' +
        plural(n.mentions.length, 'mention', 'mentions') + ' in ' + plural(n.docCount, 'document', 'documents')));
      var best = n.mentions.reduce(function(a, b) { return b.conf > a.conf ? b : a; });
      var d = docs.get(best.docId);
      var where = (d && typeof d.filename === 'string') ? d.filename : (best.docId ? 'Document ' + best.docId.slice(0, 8) : 'Unknown document');
      tooltip.appendChild(docText('div', '', 'Source: ' + where + (best.page !== null ? ', p. ' + best.page : '') + ' \u00b7 confidence ' + pct(best.conf)));
      tooltip.style.display = 'block';
    } else {
      tooltip.style.display = 'none';
    }
    canvas.style.cursor = n ? 'pointer' : '';
    requestDraw();
  }
  if (n && px !== undefined) {
    var tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
    tooltip.style.left = Math.max(4, Math.min(px + 14, W - tw - 4)) + 'px';
    tooltip.style.top = Math.max(4, (py + 16 + th > H ? py - th - 10 : py + 16)) + 'px';
  }
}

canvas.addEventListener('mousedown', function(e) {
  if (e.button !== 0) return;
  dragMoved = false; lastMX = e.clientX; lastMY = e.clientY;
  var n = findNodeAt(e.clientX, e.clientY);
  if (n) { dragNode = n; return; }
  isPanning = true; canvas.classList.add('grabbing');
});
window.addEventListener('mousemove', function(e) {
  if (dragNode) {
    if (!dragMoved && Math.abs(e.clientX - lastMX) + Math.abs(e.clientY - lastMY) < 4) return;
    // Dragging pins the node under the cursor and warms the layout so its neighbours follow.
    if (!dragMoved) { dragMoved = true; dragNode.moved = true; autoFit = false; target = null; setHover(null); reheat(); }
    dragNode.x = (e.clientX - view.x) / view.k; dragNode.y = (e.clientY - view.y) / view.k;
    dragNode.vx = 0; dragNode.vy = 0; hullsDirty = true;
    requestDraw();
  } else if (isPanning) {
    var dx = e.clientX - lastMX, dy = e.clientY - lastMY;
    if (dx || dy) { dragMoved = true; autoFit = false; target = null; framed = null; view = {x: view.x + dx, y: view.y + dy, k: view.k}; }
    lastMX = e.clientX; lastMY = e.clientY;
    requestDraw();
  } else if (e.target === canvas) {
    setHover(findNodeAt(e.clientX, e.clientY), e.clientX, e.clientY);
  } else if (hoverNode && !tableMode) {
    setHover(null);
  }
});
// Release on window, not canvas: a mouseup over a panel must still let go of the node.
window.addEventListener('mouseup', function() {
  isPanning = false; canvas.classList.remove('grabbing');
  if (dragNode) { dragNode = null; requestDraw(); }
});
canvas.addEventListener('mouseleave', function() { if (!dragNode) setHover(null); });
canvas.addEventListener('click', function(e) {
  if (dragMoved) return;
  var n = findNodeAt(e.clientX, e.clientY);
  if (n) select(n); else deselect();
});
canvas.addEventListener('dblclick', function(e) {
  e.preventDefault();
  var n = findNodeAt(e.clientX, e.clientY);
  if (n) zoomToNode(n);
});
canvas.addEventListener('wheel', function(e) {
  e.preventDefault();
  var f = e.deltaY > 0 ? 0.9 : 1 / 0.9, k = Math.max(0.02, Math.min(8, view.k * f));
  f = k / view.k;
  autoFit = false; target = null; framed = null;
  view = {x: e.clientX - (e.clientX - view.x) * f, y: e.clientY - (e.clientY - view.y) * f, k: k};
  setHover(null);
  requestDraw();
}, {passive: false});

// Selecting an entity zooms in on it and its neighbours, between the sidebar and
// the details panel, so their names and relationships have room. It only ever
// zooms in. Closing the selection goes back to the whole graph if that is where
// the view was and the user has not moved it since.
var framed = null;
function frameSelection(n) {
  var minX = n.x - n.r, maxX = n.x + n.r, minY = n.y - n.r, maxY = n.y + n.r;
  n.links.forEach(function(x) {
    var o = x.node;
    if (!isNodeVisible(o)) return;
    if (o.x - o.r < minX) minX = o.x - o.r;
    if (o.x + o.r > maxX) maxX = o.x + o.r;
    if (o.y - o.r < minY) minY = o.y - o.r;
    if (o.y + o.r > maxY) maxY = o.y + o.r;
  });
  var panel = document.getElementById('info-panel').getBoundingClientRect();
  var left = W >= 760 ? sidebarEl.getBoundingClientRect().right + 28 : 16;
  var right = (panel.width && W >= 1000) ? W - panel.left + 28 : 36;
  var top = 78, bottom = 60;
  var availW = Math.max(W - left - right, 50), availH = Math.max(H - top - bottom, 50);
  var k = Math.min(Math.max(availW - 180, 50) / Math.max(maxX - minX, 1), Math.max(availH - 110, 50) / Math.max(maxY - minY, 1), 1.6);
  if (k <= view.k * 1.15) return;
  if (!framed) framed = {wasAutoFit: autoFit};
  autoFit = false;
  target = {k: k, x: left + availW / 2 - (minX + maxX) / 2 * k, y: top + availH / 2 - (minY + maxY) / 2 * k};
}
function select(n) { selectedNode = n; showNodeInfo(n); frameSelection(n); requestDraw(); }
function deselect() {
  selectedNode = null;
  document.getElementById('info-panel').style.display = 'none';
  var back = framed && framed.wasAutoFit;
  framed = null;
  if (back) fitToScreen(true);
  requestDraw();
}
document.getElementById('info-close').addEventListener('click', deselect);

// Make a node visible even if the current filters hide it, then select it.
function reveal(n) {
  var changed = false;
  if (hiddenTypes.has(n.type)) { hiddenTypes.delete(n.type); changed = true; }
  if (n.conf < minConf) { minConf = Math.floor(n.conf * 20) / 20; confRange.value = String(Math.round(minConf * 100)); changed = true; }
  if (focusMode && !focusVisible.has(n)) { focusMode = false; focusVisible = new Set(); document.getElementById('focus-bar').style.display = 'none'; changed = true; }
  selectedNode = n;
  if (changed) filtersChanged(); else showNodeInfo(n);
  zoomToNode(n);
}

// --- Search ---
var searchInput = document.getElementById('search-input');
var searchResults = document.getElementById('search-results');
var searchMatches = [];
function norm(s) { return s.normalize('NFKC').toLowerCase(); }
function closeSearch() { searchResults.style.display = 'none'; searchResults.textContent = ''; searchMatches = []; }
searchInput.addEventListener('input', function() {
  var q = norm(searchInput.value).trim();
  closeSearch();
  // In the table view the search narrows the table instead of opening a list.
  if (tableMode) { renderTable(); return; }
  if (!q) return;
  searchMatches = labelOrder.filter(function(n) { return norm(n.label).indexOf(q) >= 0; })
    .sort(function(a, b) { return b.degree - a.degree || a.idx - b.idx; }).slice(0, 12);
  if (searchMatches.length === 0) {
    searchResults.appendChild(el('div', 'search-result', 'No entity matches'));
  }
  searchMatches.forEach(function(n, i) {
    var div = el('div', 'search-result' + (i === 0 ? ' first' : ''));
    div.appendChild(swatch(n.type));
    div.appendChild(docText('span', 'sr-label', n.label));
    div.appendChild(el('span', 'sr-note', isNodeVisible(n) ? styleOf(n.type).name : 'hidden by filter'));
    div.addEventListener('click', function() { searchInput.value = ''; closeSearch(); reveal(n); });
    searchResults.appendChild(div);
  });
  searchResults.style.display = 'block';
});
searchInput.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') { searchInput.value = ''; closeSearch(); if (tableMode) renderTable(); searchInput.blur(); e.stopPropagation(); }
  else if (e.key === 'Enter' && searchMatches.length) { var n = searchMatches[0]; searchInput.value = ''; closeSearch(); reveal(n); }
});
document.addEventListener('click', function(e) {
  if (!document.getElementById('search-box').contains(e.target)) closeSearch();
});

// --- Table view: the same entities as a sortable list ---
var tableMode = false, sortKey = 'connections', sortDir = -1;
var COLUMNS = [
  {key: 'name', title: 'Entity', num: false, get: function(n) { return norm(n.label); }},
  {key: 'type', title: 'Type', num: false, get: function(n) { return styleOf(n.type).name; }},
  {key: 'connections', title: 'Connections', num: true, get: function(n) { return n.degree; }},
  {key: 'mentions', title: 'Mentions', num: true, get: function(n) { return n.mentions.length; }},
  {key: 'documents', title: 'Documents', num: true, get: function(n) { return n.docCount; }},
  {key: 'rels', title: 'Top relationships', num: false, get: function(n) { var r = topRels(n); return r.length ? r[0][0] : '\uffff'; }}
];
var tableHead = document.getElementById('table-head');
COLUMNS.forEach(function(col) {
  var th = el('th', col.num ? 'num' : '');
  th.setAttribute('data-key', col.key);
  th.scope = 'col';
  var b = el('button', '', col.title);
  b.type = 'button';
  b.addEventListener('click', function() {
    if (sortKey === col.key) sortDir = -sortDir; else { sortKey = col.key; sortDir = col.num ? -1 : 1; }
    renderTable();
  });
  th.appendChild(b); tableHead.appendChild(th);
});
function renderTable() {
  var q = norm(searchInput.value).trim();
  var col = COLUMNS.filter(function(c) { return c.key === sortKey; })[0];
  var rows = NODES.filter(function(n) { return isNodeVisible(n) && (!q || norm(n.label).indexOf(q) >= 0); });
  rows.sort(function(a, b) {
    var x = col.get(a), y = col.get(b);
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir || b.degree - a.degree || a.idx - b.idx;
  });
  Array.prototype.forEach.call(tableHead.children, function(th) {
    if (th.getAttribute('data-key') === sortKey) th.setAttribute('aria-sort', sortDir > 0 ? 'ascending' : 'descending');
    else th.removeAttribute('aria-sort');
  });
  document.getElementById('table-caption').textContent = plural(rows.length, 'entity', 'entities') +
    (rows.length < NODES.length ? ' of ' + NODES.length + ' (filtered)' : '') + '. Select a row to find it in the graph.';
  document.getElementById('table-empty').style.display = rows.length ? 'none' : 'block';
  var body = document.getElementById('table-body');
  body.textContent = '';
  rows.forEach(function(n) {
    var tr = el('tr');
    tr.tabIndex = 0;
    tr.setAttribute('data-type', n.type);
    var name = el('td'), wrap = el('div', 'cell-name');
    wrap.appendChild(swatch(n.type)); wrap.appendChild(docText('span', 'row-label', n.label));
    name.appendChild(wrap); tr.appendChild(name);
    tr.appendChild(el('td', 'type', styleOf(n.type).name));
    tr.appendChild(el('td', 'num', String(n.degree)));
    tr.appendChild(el('td', 'num', String(n.mentions.length)));
    tr.appendChild(el('td', 'num', String(n.docCount)));
    tr.appendChild(docText('td', 'rels', topRels(n).slice(0, 3).map(function(r) { return r[0] + ' ' + r[1]; }).join(' \u00b7 ')));
    function go() { setHover(null); setView('graph'); reveal(n); }
    tr.addEventListener('click', go);
    tr.addEventListener('keydown', function(e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    // Keyboard focus on a row gives the same summary as hovering the mark.
    tr.addEventListener('focus', function() { var b = tr.getBoundingClientRect(); setHover(n, b.left + 220, b.bottom - 14); });
    tr.addEventListener('blur', function() { if (hoverNode === n) setHover(null); });
    body.appendChild(tr);
  });
}
function setView(mode) {
  tableMode = (mode === 'table');
  document.getElementById('view-graph').setAttribute('aria-selected', tableMode ? 'false' : 'true');
  document.getElementById('view-table').setAttribute('aria-selected', tableMode ? 'true' : 'false');
  var tv = document.getElementById('table-view');
  tv.style.left = (sidebarEl.getBoundingClientRect().right + 16) + 'px';
  tv.style.display = tableMode ? 'block' : 'none';
  canvas.style.visibility = tableMode ? 'hidden' : 'visible';
  searchInput.placeholder = tableMode ? 'Filter the table (press /)' : 'Search entities (press /)';
  closeSearch();
  if (tableMode) { document.getElementById('info-panel').style.display = 'none'; renderTable(); }
  else if (selectedNode) showNodeInfo(selectedNode);
  requestDraw();
}
document.getElementById('view-graph').addEventListener('click', function() { setView('graph'); });
document.getElementById('view-table').addEventListener('click', function() { setView('table'); });

// --- Focus mode: show only the neighbourhood of one entity ---
function enterFocusMode(center, depth) {
  focusMode = true;
  focusDepth = depth || 1;
  focusVisible = new Set([center]);
  var frontier = [center];
  for (var d = 0; d < focusDepth; d++) {
    var next = [];
    frontier.forEach(function(n) {
      n.links.forEach(function(x) { if (!focusVisible.has(x.node)) { focusVisible.add(x.node); next.push(x.node); } });
    });
    frontier = next;
  }
  document.getElementById('focus-bar').style.display = 'flex';
  document.getElementById('focus-depth').value = String(focusDepth);
  document.getElementById('focus-depth-label').textContent = focusDepth + '-hop';
  filtersChanged();
  fitToScreen(true);
}
function exitFocusMode() {
  focusMode = false; focusVisible = new Set();
  document.getElementById('focus-bar').style.display = 'none';
  filtersChanged();
  fitToScreen(true);
}
document.getElementById('focus-exit').addEventListener('click', exitFocusMode);
document.getElementById('focus-depth').addEventListener('input', function() {
  var val = parseInt(this.value, 10) || 1;
  if (selectedNode && focusMode) enterFocusMode(selectedNode, val);
});

// --- Document reader ---
var readerEl = document.getElementById('doc-reader');
function closeReader() { readerEl.style.display = 'none'; }
document.getElementById('reader-close').addEventListener('click', closeReader);
function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

function openReader(docId, highlight) {
  var doc = docs.get(docId);
  if (!doc || typeof doc.text !== 'string' || !doc.text) return;
  document.getElementById('reader-title').textContent = typeof doc.filename === 'string' ? doc.filename : 'Document';
  var old = document.getElementById('reader-source-link');
  if (old) old.remove();
  var url = safeUrl(doc.source_url);
  if (url) {
    var link = el('a', 'source-link', '\u2197 View original source');
    link.id = 'reader-source-link'; link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.title = 'Opens ' + url + ' in your browser. This contacts that website.';
    document.querySelector('.reader-header').insertBefore(link, document.getElementById('reader-close'));
  }

  // Entities mentioned in this document, most mentioned first.
  var inDoc = [];
  NODES.forEach(function(n) {
    var c = 0;
    n.mentions.forEach(function(m) { if (m.docId === docId) c++; });
    if (c) inDoc.push({node: n, count: c});
  });
  inDoc.sort(function(a, b) { return b.count - a.count || a.node.idx - b.node.idx; });

  var textEl = document.getElementById('reader-text');
  textEl.textContent = '';
  var spans = [], rows = new Map();
  function setActive(node) {
    spans.forEach(function(s) { s.el.classList.toggle('active', s.node === node); });
    rows.forEach(function(row, nd) { row.classList.toggle('active', nd === node); });
    for (var i = 0; i < spans.length; i++) {
      if (spans[i].node === node) { spans[i].el.scrollIntoView({block: 'center'}); break; }
    }
  }

  // Split the text on entity labels (longest first) and build it from text nodes and spans.
  var byLower = new Map();
  inDoc.forEach(function(x) { if (x.node.label.length > 1) byLower.set(x.node.label.toLowerCase(), x.node); });
  var labels = Array.from(byLower.keys()).sort(function(a, b) { return b.length - a.length; });
  var re = null;
  if (labels.length) { try { re = new RegExp(labels.map(escapeRe).join('|'), 'gi'); } catch (err) { re = null; } }
  var text = doc.text, pos = 0, m;
  if (re) {
    while ((m = re.exec(text)) !== null) {
      if (m[0].length === 0) { re.lastIndex++; continue; }
      var node = byLower.get(m[0].toLowerCase());
      if (!node) continue;
      if (m.index > pos) textEl.appendChild(document.createTextNode(text.slice(pos, m.index)));
      var span = el('span', 'entity-highlight', m[0]);
      span.style.borderBottomColor = colorOf(node.type);
      span.addEventListener('click', setActive.bind(null, node));
      spans.push({el: span, node: node});
      textEl.appendChild(span);
      pos = m.index + m[0].length;
    }
  }
  if (pos < text.length) textEl.appendChild(document.createTextNode(text.slice(pos)));

  var sidebar = document.getElementById('reader-sidebar');
  sidebar.textContent = '';
  sidebar.appendChild(el('h3', 'eyebrow', 'Entities found'));
  inDoc.forEach(function(x) {
    var row = el('div', 'sidebar-entity');
    row.appendChild(swatch(x.node.type));
    row.appendChild(docText('span', 'sidebar-entity-name', x.node.label));
    row.appendChild(el('span', 'sidebar-entity-count', x.count + '\u00d7'));
    row.addEventListener('click', setActive.bind(null, x.node));
    rows.set(x.node, row);
    sidebar.appendChild(row);
  });

  readerEl.style.display = 'block';
  if (highlight) setActive(highlight);
}

// --- Details panel ---
function showNodeInfo(node) {
  var panel = document.getElementById('info-panel');
  var content = document.getElementById('info-content');
  content.textContent = '';
  content.appendChild(docText('h2', '', node.label));

  var badge = el('div', 'type-badge');
  badge.appendChild(swatch(node.type));
  badge.appendChild(docText('span', '', styleOf(node.type).name));
  content.appendChild(badge);

  var byDoc = new Map(), lo = 1;
  node.mentions.forEach(function(m) {
    if (!byDoc.has(m.docId)) byDoc.set(m.docId, []);
    byDoc.get(m.docId).push(m);
    if (m.conf < lo) lo = m.conf;
  });
  var confText = node.mentions.length > 1 && lo < node.conf ? 'confidence ' + pct(lo) + '\u2013' + pct(node.conf) : 'confidence ' + pct(node.conf);
  content.appendChild(el('div', 'facts', plural(node.degree, 'connection', 'connections') + ' \u00b7 ' +
    plural(node.mentions.length, 'mention', 'mentions') + ' in ' + plural(byDoc.size, 'document', 'documents') + ' \u00b7 ' + confText));

  if (node.degree > 0) {
    var focusBtn = el('button', 'btn', focusMode ? 'Exit focus' : 'Show only this neighbourhood');
    focusBtn.type = 'button';
    focusBtn.addEventListener('click', function() {
      if (focusMode) exitFocusMode(); else enterFocusMode(node, 1);
    });
    content.appendChild(focusBtn);

    content.appendChild(el('div', 'label eyebrow', 'Connected to (' + node.degree + ')'));
    node.links.slice().sort(function(a, b) { return b.link.weight - a.link.weight || b.node.degree - a.node.degree || a.node.idx - b.node.idx; })
      .forEach(function(x) {
        var row = el('button', 'neighbour' + (isNodeVisible(x.node) ? '' : ' filtered'));
        row.type = 'button';
        row.setAttribute('data-type', x.node.type);
        row.appendChild(swatch(x.node.type));
        row.appendChild(docText('span', 'nb-label', x.node.label));
        // Arrow shows which way the relationship was recorded.
        var rels = x.link.rels.map(function(r) { return (r.from === node ? '\u2192 ' : '\u2190 ') + r.type; }).join(', ');
        var rel = docText('span', 'nb-rel', clip(rels, 34));
        rel.title = rels + (isNodeVisible(x.node) ? '' : ' (hidden by filter)');
        row.appendChild(rel);
        row.addEventListener('click', function() { reveal(x.node); });
        content.appendChild(row);
      });
  }

  content.appendChild(el('div', 'label eyebrow', 'Sources (' + plural(byDoc.size, 'document', 'documents') + ')'));
  byDoc.forEach(function(ms, docId) {
    var doc = docs.get(docId);
    var card = el('div', 'doc-card');
    card.appendChild(docText('div', 'doc-name', (doc && typeof doc.filename === 'string') ? doc.filename : (docId ? 'Document ' + docId.slice(0, 8) : 'Unknown document')));
    var meta = [];
    if (doc && typeof doc.page_count === 'number') meta.push(plural(doc.page_count, 'page', 'pages'));
    meta.push(plural(ms.length, 'mention', 'mentions'));
    card.appendChild(el('div', 'doc-meta', meta.join(' \u00b7 ')));

    ms.slice().sort(function(a, b) { return (a.page === null ? 1e9 : a.page) - (b.page === null ? 1e9 : b.page); }).slice(0, 6).forEach(function(m) {
      var line = el('div', 'mention');
      line.appendChild(el('span', 'm-where', (m.page !== null ? 'p. ' + m.page : 'page unknown') + ' \u00b7 ' + pct(m.conf) + ' confidence'));
      if (m.context) line.appendChild(docText('div', 'm-context', '\u201c' + clip(m.context, 160) + '\u201d'));
      card.appendChild(line);
    });
    if (ms.length > 6) card.appendChild(el('div', 'doc-meta', '+ ' + (ms.length - 6) + ' more'));

    if (doc && typeof doc.text === 'string' && doc.text) {
      var open = el('button', 'open-reader', '\u2192 Open document reader');
      open.type = 'button';
      open.addEventListener('click', function() { openReader(docId, node); });
      card.appendChild(open);
    } else {
      card.appendChild(el('div', 'no-text', 'Document text is not included in this export.'));
    }
    var url = doc ? safeUrl(doc.source_url) : null;
    if (url) {
      card.appendChild(document.createElement('br'));
      var src = el('a', '', '\u2197 ' + url.replace(/^https?:\/\/(www\.)?/i, '').slice(0, 50));
      src.href = url; src.target = '_blank'; src.rel = 'noopener noreferrer';
      src.title = 'Opens ' + url + ' in your browser. This contacts that website.';
      card.appendChild(src);
    }
    content.appendChild(card);
  });

  panel.style.display = tableMode ? 'none' : 'block';
}

// --- Theme switching ---
var themeBtn = document.getElementById('theme-btn');
function applyTheme() {
  T = THEMES[themeName()];
  themeBtn.textContent = T.name === 'dark' ? 'Light theme' : 'Dark theme';
  Array.prototype.forEach.call(document.querySelectorAll('canvas.swatch'), paintSwatch);
  requestDraw();
}
themeBtn.addEventListener('click', function() {
  document.documentElement.setAttribute('data-theme', T.name === 'dark' ? 'light' : 'dark');
  applyTheme();
});
if (darkQuery) {
  if (darkQuery.addEventListener) darkQuery.addEventListener('change', applyTheme);
  else if (darkQuery.addListener) darkQuery.addListener(applyTheme);
}

// --- Keyboard ---
document.addEventListener('keydown', function(e) {
  var typing = e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA');
  if (e.key === 'Escape') {
    if (readerEl.style.display === 'block') { closeReader(); return; }
    deselect();
  } else if (!typing && (e.key === 'f' || e.key === 'F') && !e.ctrlKey && !e.metaKey && !e.altKey) {
    fitToScreen(true);
  } else if (!typing && e.key === '/') {
    e.preventDefault(); searchInput.focus();
  }
});

// Read-only view of what is on screen, for the rendered tests and for debugging.
function screenRect(r) { return {x: r.x, y: r.y, w: r.w, h: r.h}; }
window.openfoiaGraph = {
  initials: function(label, type) { return initialsOf(label, type); },
  snapshot: function() {
    return {
      entityCount: NODES.length, mentionCount: mentionCount, relationshipCount: relationshipCount,
      linkCount: LINKS.length, linkWeightTotal: LINKS.reduce(function(a, l) { return a + l.weight; }, 0), selfLoops: selfLoops,
      settled: !running && !simActive(), running: running, alpha: alpha, ticks: ticks, settleMs: settleMs,
      view: {x: view.x, y: view.y, k: view.k}, autoFit: autoFit, width: W, height: H, labelFont: LABEL_FONT, hubFont: HUB_FONT,
      mode: tableMode ? 'table' : 'graph',
      theme: {name: T.name, surface: T.surface, ink: T.ink, labelInk: T.ink2, muted: T.muted,
              person: T.person, organization: T.organization, location: T.location},
      nodes: NODES.map(function(n) {
        var sr = markRadius(n.r, view.k);
        return {label: n.label, type: n.type, visible: isNodeVisible(n), selected: n === selectedNode, degree: n.degree, mentions: n.mentions.length,
                confidence: n.conf, x: n.x * view.k + view.x, y: n.y * view.k + view.y, r: sr, wx: n.x, wy: n.y,
                tier: tierOf(sr), attribute: n.attr, initials: n.initials, community: n.com};
      }),
      labels: lastLabels.map(function(r) { var o = screenRect(r); o.label = r.owner.label; o.type = r.owner.type; o.hub = r.hub; return o; }),
      edgeLabels: lastEdgeLabels.map(function(r) { var o = screenRect(r); o.text = r.text; o.to = r.to.label; o.toType = r.to.type; o.at = r.at; return o; }),
      communities: COMMUNITIES.map(function(c, i) {
        var cap = lastCaptions.filter(function(r) { return r.com === c; })[0];
        return {id: i, size: c.members.length, top: c.top.label, topType: c.top.type, sizeable: c.sizeable,
                hull: c.hull ? c.hull.map(function(p) { return [p[0] * view.k + view.x, p[1] * view.k + view.y]; }) : null,
                caption: cap ? cap.text : null, captionRect: cap ? screenRect(cap) : null};
      })
    };
  }
};

layoutIsolates();
applyTheme();
filtersChanged();
})();
</script>
</body>
</html>"""
