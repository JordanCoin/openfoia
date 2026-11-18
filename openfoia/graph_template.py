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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenFOIA Entity Graph</title>
<style>
  :root {
    --surface: #1a1a19; --panel: rgba(32, 32, 30, 0.96); --panel-2: #2a2a27; --border: #3f3e3a;
    --text: #f0efec; --text-2: #c3c2b7; --text-3: #8f8e86; --accent: #6da7ec;
    --font: system-ui, -apple-system, 'Segoe UI', Roboto, 'Noto Sans', 'Noto Sans Arabic', 'Noto Sans Hebrew', 'Noto Sans CJK SC', 'DejaVu Sans', sans-serif;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: var(--font); background: var(--surface); color: var(--text); overflow: hidden; }
  button, input, select { font-family: inherit; }
  #graph { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; cursor: grab; display: block; }
  #graph.grabbing { cursor: grabbing; }
  .swatch { width: 16px; height: 16px; flex-shrink: 0; }

  #sidebar {
    position: fixed; top: 12px; left: 12px; width: 232px; max-height: calc(100vh - 24px); overflow-y: auto;
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; z-index: 10;
  }
  #sidebar h1 { font-size: 15px; font-weight: 600; }
  #stats { font-size: 12px; color: var(--text-2); margin-top: 4px; line-height: 1.5; }
  #sidebar h2 { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.5px; margin: 14px 0 6px; font-weight: 600; }
  .legend-item {
    display: flex; align-items: center; gap: 8px; width: 100%; padding: 3px 4px; font-size: 13px; color: var(--text);
    background: none; border: 0; border-radius: 4px; cursor: pointer; text-align: left;
  }
  .legend-item:hover { background: rgba(255,255,255,0.07); }
  .legend-item .legend-count { margin-left: auto; color: var(--text-3); font-size: 12px; font-variant-numeric: tabular-nums; }
  .legend-item[aria-pressed="false"] { opacity: 0.4; }
  .legend-item[aria-pressed="false"] .legend-name { text-decoration: line-through; }
  .control-row { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-2); }
  .control-row output { margin-left: auto; color: var(--text); font-variant-numeric: tabular-nums; }
  #conf-range { width: 100%; margin-top: 6px; accent-color: var(--accent); }
  #filter-count { font-size: 12px; color: var(--text-3); margin-top: 4px; }
  #size-by { margin-left: auto; background: var(--panel-2); color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 2px 4px; font-size: 12px; }
  .btn-row { display: flex; gap: 6px; margin-top: 14px; }
  .btn {
    background: var(--panel-2); border: 1px solid var(--border); color: var(--text); padding: 5px 10px;
    border-radius: 6px; cursor: pointer; font-size: 12px;
  }
  .btn:hover { border-color: var(--text-3); }
  .hint { font-size: 11px; color: var(--text-3); margin-top: 10px; line-height: 1.5; }

  #search-box { position: fixed; top: 12px; left: 50%; transform: translateX(-50%); z-index: 11; }
  #search-box input {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 14px; color: var(--text); font-size: 13px; width: 300px; outline: none;
  }
  #search-box input:focus { border-color: var(--accent); }
  #search-results {
    position: absolute; top: 40px; left: 0; width: 100%; max-height: 300px; overflow-y: auto;
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px; display: none;
  }
  .search-result { padding: 7px 12px; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 8px; }
  .search-result:hover, .search-result.first { background: rgba(255,255,255,0.07); }
  .search-result .sr-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .search-result .sr-note { margin-left: auto; color: var(--text-3); font-size: 11px; white-space: nowrap; }

  #tooltip {
    position: fixed; z-index: 20; display: none; pointer-events: none; max-width: 320px;
    background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
    font-size: 12px; color: var(--text-2); line-height: 1.5; box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  }
  #tooltip .tt-title { color: var(--text); font-size: 13px; font-weight: 600; }

  #info-panel {
    position: fixed; top: 12px; right: 12px; width: 360px; max-width: calc(100vw - 24px);
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; display: none;
    max-height: calc(100vh - 24px); overflow-y: auto; z-index: 12; box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  #info-panel h2 { font-size: 17px; margin: 0 28px 6px 0; font-weight: 600; overflow-wrap: anywhere; }
  .type-badge { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-2); margin-bottom: 4px; }
  .facts { font-size: 12px; color: var(--text-2); margin-bottom: 10px; line-height: 1.5; }
  #info-panel .label { color: var(--text-3); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 14px; margin-bottom: 4px; font-weight: 600; }
  #info-close { position: absolute; top: 10px; right: 10px; cursor: pointer; color: var(--text-3); font-size: 20px; width: 28px; height: 28px; border-radius: 6px; background: none; border: 0; }
  #info-close:hover { background: rgba(255,255,255,0.1); color: var(--text); }
  .neighbour {
    display: flex; align-items: center; gap: 8px; width: 100%; padding: 4px; font-size: 13px; color: var(--text);
    background: none; border: 0; border-radius: 4px; cursor: pointer; text-align: left;
  }
  .neighbour:hover { background: rgba(255,255,255,0.07); }
  .neighbour .nb-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .neighbour .nb-rel { margin-left: auto; color: var(--text-3); font-size: 11px; white-space: nowrap; }
  .neighbour.filtered { opacity: 0.45; }
  .doc-card { background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin: 6px 0; }
  .doc-card .doc-name { font-size: 13px; color: var(--text); font-weight: 600; overflow-wrap: anywhere; }
  .doc-card .doc-meta { font-size: 11px; color: var(--text-3); margin-top: 2px; }
  .mention { font-size: 12px; color: var(--text-2); margin-top: 6px; line-height: 1.4; }
  .mention .m-where { color: var(--text); font-variant-numeric: tabular-nums; }
  .mention .m-context { font-style: italic; overflow-wrap: anywhere; }
  .doc-card a, .doc-card .open-reader { font-size: 12px; color: var(--accent); text-decoration: none; display: inline-block; margin-top: 6px; background: none; border: 0; cursor: pointer; padding: 0; }
  .doc-card .no-text { font-size: 11px; color: var(--text-3); margin-top: 6px; }

  #doc-reader { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: var(--surface); z-index: 100; display: none; }
  #doc-reader .reader-header {
    height: 52px; display: flex; align-items: center; gap: 12px;
    padding: 0 20px; border-bottom: 1px solid var(--border); background: var(--panel-2);
  }
  #doc-reader .reader-header h2 { font-size: 15px; font-weight: 600; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #doc-reader a.source-link { color: var(--accent); font-size: 13px; text-decoration: none; padding: 5px 10px; border: 1px solid var(--border); border-radius: 6px; white-space: nowrap; }
  #reader-close { cursor: pointer; color: var(--text-3); font-size: 22px; width: 36px; height: 36px; border-radius: 8px; background: none; border: 0; }
  #reader-close:hover { background: rgba(255,255,255,0.08); color: var(--text); }
  #doc-reader .reader-body { display: flex; height: calc(100vh - 52px); }
  #reader-sidebar { width: 280px; border-right: 1px solid var(--border); overflow-y: auto; padding: 14px; flex-shrink: 0; }
  #reader-sidebar h3 { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
  .sidebar-entity { padding: 6px 8px; border-radius: 6px; margin: 2px 0; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 8px; }
  .sidebar-entity:hover { background: rgba(255,255,255,0.05); }
  .sidebar-entity.active { background: rgba(109, 167, 236, 0.18); }
  .sidebar-entity-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sidebar-entity-count { color: var(--text-3); font-size: 11px; margin-left: auto; }
  #reader-text { flex: 1; overflow-y: auto; padding: 28px 44px; font-size: 14px; line-height: 1.8; color: var(--text-2); white-space: pre-wrap; overflow-wrap: anywhere; font-family: Georgia, 'Times New Roman', 'Noto Serif', serif; }
  .entity-highlight { background: rgba(255,255,255,0.08); border-bottom: 2px solid var(--text-3); padding: 1px 2px; border-radius: 2px; cursor: pointer; color: var(--text); }
  .entity-highlight.active { background: rgba(250, 204, 21, 0.3); }

  #focus-bar {
    position: fixed; bottom: 12px; left: 50%; transform: translateX(-50%);
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 16px; z-index: 10; display: none;
    align-items: center; gap: 12px; font-size: 13px; color: var(--text-2);
  }
  #focus-bar .focus-depth { color: var(--text); font-weight: 600; }
  #focus-bar input[type=range] { width: 110px; accent-color: var(--accent); }
  #empty { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); color: var(--text-3); font-size: 14px; display: none; }
</style>
</head>
<body>
<canvas id="graph"></canvas>
<div id="empty">No entities to show.</div>
<div id="sidebar">
  <h1>OpenFOIA Entity Graph</h1>
  <p id="stats"></p>
  <h2>Entity types</h2>
  <div id="legend"></div>
  <h2>Filter</h2>
  <label class="control-row" for="conf-range">Minimum confidence <output id="conf-value">0%</output></label>
  <input type="range" id="conf-range" min="0" max="100" step="5" value="0">
  <p id="filter-count"></p>
  <h2>Display</h2>
  <label class="control-row" for="size-by">Size nodes by
    <select id="size-by"><option value="degree">Connections</option><option value="mentions">Mentions</option></select>
  </label>
  <div class="btn-row">
    <button type="button" class="btn" id="fit-btn" title="Fit the visible graph to the window (F)">Fit to screen</button>
    <button type="button" class="btn" id="reset-btn">Reset filters</button>
  </div>
  <p class="hint">Hover a node for a summary. Click for sources and neighbours. Click a type to hide it. Scroll to zoom, drag to pan, double-click to zoom in. Names appear as space allows; zoom in for more.</p>
</div>
<div id="search-box">
  <input type="text" placeholder="Search entities (press /)" id="search-input" autocomplete="off" spellcheck="false" dir="auto" aria-label="Search entities">
  <div id="search-results"></div>
</div>
<div id="tooltip"></div>
<div id="focus-bar">
  <span>Focus:</span>
  <span class="focus-depth" id="focus-depth-label">1-hop</span>
  <input type="range" min="1" max="4" value="1" id="focus-depth" aria-label="Focus depth">
  <button type="button" class="btn" id="focus-exit">Back to full graph</button>
</div>
<div id="info-panel">
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

var SURFACE = '#1a1a19', TEXT = '#f0efec', TEXT_2 = '#c3c2b7', TEXT_3 = '#8f8e86';
var FONT_STACK = "system-ui, -apple-system, 'Segoe UI', Roboto, 'Noto Sans', 'Noto Sans Arabic', 'Noto Sans Hebrew', 'Noto Sans CJK SC', 'DejaVu Sans', sans-serif";
// Colour and shape both carry the type, so the graph still reads without colour vision.
var TYPE_STYLE = new Map([
  ['person', {color: '#3987e5', shape: 'circle', name: 'Person'}],
  ['organization', {color: '#d95926', shape: 'square', name: 'Organization'}],
  ['location', {color: '#199e70', shape: 'diamond', name: 'Location'}],
  ['money', {color: '#c98500', shape: 'hexagon', name: 'Money'}],
  ['date', {color: '#d55181', shape: 'triangle', name: 'Date'}],
  ['document_id', {color: '#0ca30c', shape: 'pentagon', name: 'Document ID'}],
  ['email', {color: '#9085e9', shape: 'star', name: 'Email'}],
  ['phone', {color: '#e66767', shape: 'triangle-down', name: 'Phone'}],
  ['address', {color: '#8f8e86', shape: 'cross', name: 'Address'}]
]);
function styleOf(type) { return TYPE_STYLE.get(type) || {color: '#8f8e86', shape: 'circle', name: type}; }

function polygon(c, x, y, r, sides, rot) {
  for (var i = 0; i < sides; i++) {
    var a = rot + i * 2 * Math.PI / sides;
    if (i === 0) c.moveTo(x + r * Math.cos(a), y + r * Math.sin(a)); else c.lineTo(x + r * Math.cos(a), y + r * Math.sin(a));
  }
  c.closePath();
}
// Shapes are scaled to cover roughly the area of a circle of radius r.
function shapePath(c, shape, x, y, r) {
  var i, a, rr, w;
  c.beginPath();
  switch (shape) {
    case 'square': rr = r * 0.89; c.rect(x - rr, y - rr, rr * 2, rr * 2); break;
    case 'diamond': polygon(c, x, y, r * 1.25, 4, 0); break;
    case 'hexagon': polygon(c, x, y, r * 1.1, 6, 0); break;
    case 'triangle': polygon(c, x, y + r * 0.2, r * 1.4, 3, -Math.PI / 2); break;
    case 'triangle-down': polygon(c, x, y - r * 0.2, r * 1.4, 3, Math.PI / 2); break;
    case 'pentagon': polygon(c, x, y, r * 1.15, 5, -Math.PI / 2); break;
    case 'star':
      for (i = 0; i < 10; i++) {
        a = -Math.PI / 2 + i * Math.PI / 5; rr = (i % 2 === 0) ? r * 1.45 : r * 0.65;
        if (i === 0) c.moveTo(x + rr * Math.cos(a), y + rr * Math.sin(a)); else c.lineTo(x + rr * Math.cos(a), y + rr * Math.sin(a));
      }
      c.closePath(); break;
    case 'cross':
      w = r * 0.45; rr = r * 1.15;
      c.moveTo(x - w, y - rr); c.lineTo(x + w, y - rr); c.lineTo(x + w, y - w); c.lineTo(x + rr, y - w);
      c.lineTo(x + rr, y + w); c.lineTo(x + w, y + w); c.lineTo(x + w, y + rr); c.lineTo(x - w, y + rr);
      c.lineTo(x - w, y + w); c.lineTo(x - rr, y + w); c.lineTo(x - rr, y - w); c.lineTo(x - w, y - w);
      c.closePath(); break;
    default: c.arc(x, y, r, 0, Math.PI * 2);
  }
}

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
// Text from documents: let the browser pick the direction from the text itself.
function docText(tag, cls, text) { var e = el(tag, cls, text); e.dir = 'auto'; return e; }
function swatch(type) {
  var c = document.createElement('canvas');
  c.className = 'swatch'; c.width = 32; c.height = 32;
  var g = c.getContext('2d'), st = styleOf(type);
  g.fillStyle = st.color; shapePath(g, st.shape, 16, 16, 10); g.fill();
  return c;
}
function pct(x) { return Math.round(x * 100) + '%'; }
function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
var RTL_RE = /[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]/;
function safeUrl(u) { return (typeof u === 'string' && /^https?:\/\//i.test(u)) ? u : null; }

// --- Model: one node per entity (same type and label), however many mentions ---
var NODES = [], nodeByKey = new Map(), nodeById = new Map(), mentionCount = 0;
rawNodes.forEach(function(m) {
  if (!m || typeof m !== 'object') return;
  var label = String(m.label === undefined || m.label === null ? '' : m.label);
  var type = String(m.type === undefined || m.type === null ? 'unknown' : m.type);
  var key = type + '\u0000' + label;
  var n = nodeByKey.get(key);
  if (!n) {
    n = {key: key, label: label, type: type, mentions: [], conf: 0, links: [], degree: 0, idx: NODES.length,
         x: 0, y: 0, vx: 0, vy: 0, r: 5, q: 0, sx: 0, sy: 0, sr: 0, rtl: RTL_RE.test(label), short: null, lw: 0};
    var cps = Array.from(label);
    n.short = cps.length > 30 ? cps.slice(0, 29).join('') + '\u2026' : label;
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
NODES.forEach(function(n, i) { n.idx = i; });

// Relationships between the same two entities are drawn as one line. Every
// relationship row is still counted and listed in the details panel.
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
    l = {s: a, t: b, weight: 0, rels: [], dist: 0, str: 0, bias: 0};
    linkByPair.set(pk, l); LINKS.push(l);
    a.links.push({node: b, link: l}); b.links.push({node: a, link: l});
  }
  l.weight++;
  var rtype = (typeof e.type === 'string' && e.type) ? e.type : 'related';
  if (!l.rels.some(function(r) { return r.type === rtype && r.from === s; })) l.rels.push({type: rtype, from: s});
});
LINKS.sort(function(a, b) { return a.s.idx - b.s.idx || a.t.idx - b.t.idx; });
NODES.forEach(function(n) {
  n.links.sort(function(a, b) { return a.node.idx - b.node.idx; });
  n.degree = n.links.length;
});

var docIds = new Set();
NODES.forEach(function(n) { n.mentions.forEach(function(m) { if (m.docId) docIds.add(m.docId); }); });
document.getElementById('stats').textContent =
  plural(NODES.length, 'entity', 'entities') + ' \u00b7 ' + plural(mentionCount, 'mention', 'mentions') + ' \u00b7 ' +
  plural(relationshipCount, 'relationship', 'relationships') + ' \u00b7 ' + plural(docIds.size, 'document', 'documents');
if (NODES.length === 0) document.getElementById('empty').style.display = 'block';

var typeCounts = new Map();
NODES.forEach(function(n) { typeCounts.set(n.type, (typeCounts.get(n.type) || 0) + 1); });
var types = Array.from(typeCounts.keys()).sort(function(a, b) {
  return typeCounts.get(b) - typeCounts.get(a) || (a < b ? -1 : 1);
});

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
types.forEach(function(t) {
  var item = el('button', 'legend-item');
  item.type = 'button';
  item.setAttribute('data-type', t);
  item.setAttribute('aria-pressed', 'true');
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
  NODES.forEach(function(n) { n.r = 5.5 + 17.5 * Math.sqrt(Math.max(0, val(n)) / top); });
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
  DPR = Math.max(1, Math.min(window.devicePixelRatio || 1, 2));
  W = window.innerWidth; H = window.innerHeight;
  canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
}
resize();

// Zoomed out, marks are drawn up to MARK_BOOST times larger than true scale so their shapes
// stay readable. Collision keeps the same extra distance, so they still do not touch.
var MARK_BOOST = 1.3;
function markRadius(r, k) { return Math.max(r * k * (k < 1 ? Math.min(1 / Math.sqrt(k), MARK_BOOST) : 1), 2.5); }
var LABEL_FONT = "12px " + FONT_STACK, LABEL_FONT_BOLD = "600 12px " + FONT_STACK, LABEL_H = 15;
var labelOrder = NODES.slice().sort(function(a, b) {
  return b.degree - a.degree || b.mentions.length - a.mentions.length || a.idx - b.idx;
});
// The best-connected entities are always named, with the name written across
// the mark. The layout keeps a lane the size of that name clear of other marks.
var MUST_COUNT = Math.max(5, Math.ceil(NODES.length * 0.02));
var MUST = labelOrder.slice(0, MUST_COUNT).filter(function(n) { return n.degree > 1; });
ctx.font = LABEL_FONT_BOLD;
MUST.forEach(function(n) { n.must = true; n.lw = ctx.measureText(n.short).width; });
var CHARGE = 70, CHARGE_RANGE2 = 900 * 900, LINK_DISTANCE = 30, COLLIDE_PAD = 6, COLLIDE = 0.6, GRAVITY = 0.06, DAMPING = 0.6, MAX_SPEED = 80;
// d3-style cooling (#58): forces scale with alpha, which decays to ALPHA_MIN in ~300 ticks, then the loop stops.
var alpha = 1, ALPHA_MIN = 0.001, ALPHA_DECAY = 1 - Math.pow(ALPHA_MIN, 1/300), ALPHA_REHEAT = 0.3;
var ticks = 0, startedAt = performance.now(), settleMs = null;

// Deterministic start: best-connected entities in the middle, on a sunflower spiral.
// No Math.random, so the same export always opens with the same layout.
NODES.filter(function(n) { return n.degree > 0; }).sort(function(a, b) { return b.degree - a.degree || a.idx - b.idx; }).forEach(function(n, i) {
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
var SIM = NODES.filter(function(n) { return n.degree > 0; });
var ISOLATES = NODES.filter(function(n) { return n.degree === 0; }).sort(function(a, b) {
  return (a.type < b.type ? -1 : a.type > b.type ? 1 : 0) || a.idx - b.idx;
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
    x0 = (minX + maxX) / 2 - (cols - 1) * TRAY_CELL / 2; y0 = maxY + 90;
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
  // Keep the name lane of each must-name entity clear. The name is a fixed size
  // on screen, so its size in layout units depends on the current zoom.
  // While the view follows the layout, use the zoom it is heading for, worked out
  // per tick so the result does not depend on how many ticks fit in a frame.
  var fit = autoFit ? fitView() : null;
  var kk = Math.max(fit ? fit.k : view.k, 0.05), hw, hh, ov;
  for (i = 0; i < MUST.length; i++) {
    a = MUST[i];
    hw = (a.lw / 2 + 10) / kk; hh = (LABEL_H / 2 + 4) / kk;
    for (j = 0; j < n; j++) {
      b = SIM[j];
      if (b === a) continue;
      dx = b.x - a.x; dy = b.y - a.y; min = b.r * MARK_BOOST;
      if (Math.abs(dx) < hw + min && Math.abs(dy) < hh + min) {
        ov = hh + min - Math.abs(dy);
        b.vy += (dy >= 0 ? 1 : -1) * ov * 0.35;
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
}
function simActive() { return alpha >= ALPHA_MIN || dragNode !== null; }
function reheat() { prepareForces(); alpha = Math.max(alpha, ALPHA_REHEAT); requestDraw(); }

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
  var left = W >= 760 ? sidebarEl.getBoundingClientRect().right + 16 : 16;
  var top = 56, right = 24, bottom = 30;
  var availW = Math.max(W - left - right, 50), availH = Math.max(H - top - bottom, 50);
  var k = Math.min(availW / Math.max(maxX - minX, 1), availH / Math.max(maxY - minY, 1), 2);
  k = Math.max(k, 0.02);
  return {k: k, x: left + availW / 2 - (minX + maxX) / 2 * k, y: top + availH / 2 - (minY + maxY) / 2 * k};
}
function fitToScreen(animate) {
  var f = fitView();
  if (!f) return;
  autoFit = true;
  if (animate && !simActive()) { target = f; } else { view = f; target = null; }
  requestDraw();
}
function zoomToNode(n) {
  autoFit = false;
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
var lastLabels = [], trayCaption = null;

function render() {
  var i, n, l, st, k = view.k, vx = view.x, vy = view.y;
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
    n.sx = n.x * k + vx; n.sy = n.y * k + vy; n.sr = markRadius(n.r, k);
  }

  // Edges: one path for the background, one for the heavier links, one for the highlighted node's links.
  ctx.lineCap = 'round';
  var pass, alphaBase = focus ? 0.05 : (LINKS.length > 1500 ? 0.12 : 0.2);
  for (pass = 0; pass < 2; pass++) {
    ctx.beginPath();
    for (i = 0; i < LINKS.length; i++) {
      l = LINKS[i];
      if (!l.s.vis || !l.t.vis) continue;
      if ((l.weight > 1) !== (pass === 1)) continue;
      ctx.moveTo(l.s.sx, l.s.sy); ctx.lineTo(l.t.sx, l.t.sy);
    }
    ctx.strokeStyle = 'rgba(240,239,236,' + alphaBase + ')';
    ctx.lineWidth = pass === 1 ? 2 : 1;
    ctx.stroke();
  }
  if (focus) {
    ctx.beginPath();
    focus.links.forEach(function(x) {
      if (!x.node.vis) return;
      ctx.moveTo(focus.sx, focus.sy); ctx.lineTo(x.node.sx, x.node.sy);
    });
    ctx.strokeStyle = 'rgba(240,239,236,0.75)'; ctx.lineWidth = 1.5; ctx.stroke();
  }

  // Nodes: dimmed ones first, so highlighted ones sit on top.
  for (pass = 0; pass < 2; pass++) {
    for (i = 0; i < NODES.length; i++) {
      n = NODES[i];
      if (!n.vis) continue;
      var lit = !hl || hl.has(n);
      if (lit !== (pass === 1)) continue;
      if (n.sx < -40 || n.sx > W + 40 || n.sy < -40 || n.sy > H + 40) continue;
      st = styleOf(n.type);
      ctx.globalAlpha = lit ? 1 : 0.18;
      shapePath(ctx, st.shape, n.sx, n.sy, n.sr);
      ctx.fillStyle = st.color; ctx.fill();
      // A ring in the background colour keeps touching marks apart.
      if (n.sr > 3.5) { ctx.strokeStyle = SURFACE; ctx.lineWidth = 1.5; ctx.stroke(); }
      if (n === selectedNode || n === hoverNode) {
        shapePath(ctx, st.shape, n.sx, n.sy, n.sr + 3);
        ctx.strokeStyle = TEXT; ctx.lineWidth = 2; ctx.stroke();
      }
    }
  }
  ctx.globalAlpha = 1;
  if (tray && ISOLATES.some(function(x) { return x.vis; })) {
    ctx.font = "11px " + FONT_STACK; ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic'; ctx.direction = 'ltr';
    ctx.fillStyle = TEXT_3;
    var cap = 'NO RECORDED RELATIONSHIP (' + ISOLATES.length + ')';
    trayCaption = {x: tray.x * k + vx - 4, y: tray.y * k + vy - 2, w: ctx.measureText(cap).width + 8, h: 18, owner: null, label: true};
    ctx.fillText(cap, trayCaption.x + 4, trayCaption.y + 14);
  } else { trayCaption = null; }
  drawLabels(focus, hl);
}

// Labels are placed greedily, most connected first, and only where they do not
// cover a node or another label. Zooming in makes room for more of them.
function drawLabels(focus, hl) {
  var CELL = 64, grid = new Map(), i, n;
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
  for (i = 0; i < NODES.length; i++) {
    n = NODES[i];
    if (!n.vis || n.sx < -30 || n.sx > W + 30 || n.sy < -30 || n.sy > H + 30) continue;
    var e = n.sr * 1.15;
    add({x: n.sx - e, y: n.sy - e, w: e * 2, h: e * 2, owner: n});
  }
  // Names stay clear of the panels that sit over the canvas.
  ['sidebar', 'search-box'].forEach(function(id) {
    var b = document.getElementById(id).getBoundingClientRect();
    if (b.width && b.height) add({x: b.left - 4, y: b.top - 4, w: b.width + 8, h: b.height + 8, owner: null, label: true});
  });
  if (trayCaption) add(trayCaption);
  ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
  lastLabels = [];
  var order = labelOrder;
  if (focus) {
    order = [focus].concat(focus.links.map(function(x) { return x.node; }).sort(function(a, b) { return b.degree - a.degree || a.idx - b.idx; }));
  }
  for (i = 0; i < order.length; i++) {
    n = order[i];
    if (!n.vis || !n.short) continue;
    if (n.sx < 0 || n.sx > W || n.sy < 0 || n.sy > H) continue;
    var strong = (n === focus);
    if (!n.lw) { ctx.font = LABEL_FONT_BOLD; n.lw = ctx.measureText(n.short).width; }
    ctx.font = (strong || (!focus && n.must)) ? LABEL_FONT_BOLD : LABEL_FONT;
    var w = n.lw + 12, gap = n.sr * 1.2 + 3;
    var spots = [
      {x: n.sx - w / 2, y: n.sy + gap, align: 'center'},
      {x: n.sx + gap, y: n.sy - LABEL_H / 2, align: 'left'},
      {x: n.sx - w / 2, y: n.sy - gap - LABEL_H, align: 'center'},
      {x: n.sx - gap - w, y: n.sy - LABEL_H / 2, align: 'right'}
    ];
    var spot = null;
    if (n.must && !focus) {
      var mid = {x: n.sx - w / 2, y: n.sy - LABEL_H / 2, align: 'center'};
      if (mid.x >= 2 && mid.x + w <= W - 2 && mid.y >= 2 && mid.y + LABEL_H <= H - 2 && free(mid.x, mid.y, w, LABEL_H, n)) spot = mid;
    }
    for (var s = 0; !spot && s < spots.length; s++) {
      var c = spots[s];
      if (c.x < 2 || c.x + w > W - 2 || c.y < 2 || c.y + LABEL_H > H - 2) continue;
      if (free(c.x, c.y, w, LABEL_H, n)) { spot = c; break; }
    }
    // The best-connected entities must be named. With no clear spot beside the
    // mark, look a little further out and across the entity's own mark, and take
    // the place that covers the fewest other marks. Names never cover names.
    if (!spot && (strong || (n.must && !focus))) {
      var far = gap + LABEL_H + 6, fewest = Infinity;
      spots.push(
        {x: n.sx - w / 2, y: n.sy - LABEL_H / 2, align: 'center'},
        {x: n.sx - w / 2, y: n.sy + far, align: 'center'},
        {x: n.sx - w / 2, y: n.sy - far - LABEL_H, align: 'center'},
        {x: n.sx + gap, y: n.sy + gap, align: 'left'},
        {x: n.sx - gap - w, y: n.sy + gap, align: 'right'},
        {x: n.sx + gap, y: n.sy - gap - LABEL_H, align: 'left'},
        {x: n.sx - gap - w, y: n.sy - gap - LABEL_H, align: 'right'}
      );
      for (s = 0; s < spots.length; s++) {
        c = spots[s];
        if (c.x < 2 || c.x + w > W - 2 || c.y < 2 || c.y + LABEL_H > H - 2) continue;
        if (!free(c.x, c.y, w, LABEL_H, n, true)) continue;
        var hits = covered(c.x, c.y, w, LABEL_H, n);
        if (hits < fewest) { fewest = hits; spot = c; }
      }
      if (!spot && strong) spot = spots[4];
    }
    if (!spot) continue;
    var rect = {x: spot.x, y: spot.y, w: w, h: LABEL_H, owner: n, label: true};
    add(rect); lastLabels.push(rect);
    ctx.direction = n.rtl ? 'rtl' : 'ltr';
    ctx.textAlign = spot.align;
    var tx = spot.align === 'center' ? spot.x + w / 2 : (spot.align === 'left' ? spot.x + 3 : spot.x + w - 3);
    var ty = spot.y + LABEL_H / 2 + 0.5;
    ctx.strokeStyle = SURFACE; ctx.lineWidth = 3.5; ctx.strokeText(n.short, tx, ty);
    ctx.fillStyle = (hl && !strong) ? TEXT_2 : TEXT; ctx.fillText(n.short, tx, ty);
  }
  ctx.direction = 'ltr';
}

// --- Pointer ---
var isPanning = false, lastMX = 0, lastMY = 0, dragMoved = false;
function findNodeAt(px, py) {
  var best = null, bestD = Infinity;
  for (var i = 0; i < NODES.length; i++) {
    var n = NODES[i];
    if (!isNodeVisible(n)) continue;
    var dx = px - n.sx, dy = py - n.sy, d = Math.sqrt(dx * dx + dy * dy), reach = Math.max(n.sr * 1.2, 7) + 3;
    if (d <= reach && d < bestD) { best = n; bestD = d; }
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
      var nd = new Set(); n.mentions.forEach(function(m) { nd.add(m.docId); });
      tooltip.appendChild(el('div', '', styleOf(n.type).name + ' \u00b7 ' + plural(n.degree, 'connection', 'connections') + ' \u00b7 ' +
        plural(n.mentions.length, 'mention', 'mentions') + ' in ' + plural(nd.size, 'document', 'documents')));
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
    dragNode.vx = 0; dragNode.vy = 0;
    requestDraw();
  } else if (isPanning) {
    var dx = e.clientX - lastMX, dy = e.clientY - lastMY;
    if (dx || dy) { dragMoved = true; autoFit = false; target = null; view = {x: view.x + dx, y: view.y + dy, k: view.k}; }
    lastMX = e.clientX; lastMY = e.clientY;
    requestDraw();
  } else if (e.target === canvas) {
    setHover(findNodeAt(e.clientX, e.clientY), e.clientX, e.clientY);
  } else if (hoverNode) {
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
  autoFit = false; target = null;
  view = {x: e.clientX - (e.clientX - view.x) * f, y: e.clientY - (e.clientY - view.y) * f, k: k};
  setHover(null);
  requestDraw();
}, {passive: false});

function select(n) { selectedNode = n; showNodeInfo(n); requestDraw(); }
function deselect() {
  selectedNode = null;
  document.getElementById('info-panel').style.display = 'none';
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
function closeSearch() { searchResults.style.display = 'none'; searchResults.textContent = ''; searchMatches = []; }
searchInput.addEventListener('input', function() {
  var q = searchInput.value.normalize('NFKC').toLowerCase().trim();
  closeSearch();
  if (!q) return;
  searchMatches = labelOrder.filter(function(n) { return n.label.normalize('NFKC').toLowerCase().indexOf(q) >= 0; }).slice(0, 12);
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
  if (e.key === 'Escape') { searchInput.value = ''; closeSearch(); searchInput.blur(); e.stopPropagation(); }
  else if (e.key === 'Enter' && searchMatches.length) { var n = searchMatches[0]; searchInput.value = ''; closeSearch(); reveal(n); }
});
document.addEventListener('click', function(e) {
  if (!document.getElementById('search-box').contains(e.target)) closeSearch();
});

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
      span.style.borderBottomColor = styleOf(node.type).color;
      span.addEventListener('click', setActive.bind(null, node));
      spans.push({el: span, node: node});
      textEl.appendChild(span);
      pos = m.index + m[0].length;
    }
  }
  if (pos < text.length) textEl.appendChild(document.createTextNode(text.slice(pos)));

  var sidebar = document.getElementById('reader-sidebar');
  sidebar.textContent = '';
  sidebar.appendChild(el('h3', '', 'Entities found'));
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

    content.appendChild(el('div', 'label', 'Connected to (' + node.degree + ')'));
    node.links.slice().sort(function(a, b) { return b.link.weight - a.link.weight || b.node.degree - a.node.degree || a.node.idx - b.node.idx; })
      .forEach(function(x) {
        var row = el('button', 'neighbour' + (isNodeVisible(x.node) ? '' : ' filtered'));
        row.type = 'button';
        row.setAttribute('data-type', x.node.type);
        row.appendChild(swatch(x.node.type));
        row.appendChild(docText('span', 'nb-label', x.node.label));
        // Arrow shows which way the relationship was recorded.
        var rels = x.link.rels.map(function(r) { return (r.from === node ? '\u2192 ' : '\u2190 ') + r.type; }).join(', ');
        var rel = docText('span', 'nb-rel', rels.length > 34 ? rels.slice(0, 33) + '\u2026' : rels);
        rel.title = rels + (isNodeVisible(x.node) ? '' : ' (hidden by filter)');
        row.appendChild(rel);
        row.addEventListener('click', function() { reveal(x.node); });
        content.appendChild(row);
      });
  }

  content.appendChild(el('div', 'label', 'Sources (' + plural(byDoc.size, 'document', 'documents') + ')'));
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
      if (m.context) {
        var cps = Array.from(m.context);
        line.appendChild(docText('div', 'm-context', '\u201c' + (cps.length > 160 ? cps.slice(0, 160).join('') + '\u2026' : m.context) + '\u201d'));
      }
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

  panel.style.display = 'block';
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
window.openfoiaGraph = {
  snapshot: function() {
    return {
      entityCount: NODES.length, mentionCount: mentionCount, relationshipCount: relationshipCount,
      linkCount: LINKS.length, linkWeightTotal: LINKS.reduce(function(a, l) { return a + l.weight; }, 0), selfLoops: selfLoops,
      settled: !running && !simActive(), running: running, alpha: alpha, ticks: ticks, settleMs: settleMs,
      view: {x: view.x, y: view.y, k: view.k}, autoFit: autoFit, width: W, height: H, labelFont: LABEL_FONT,
      nodes: NODES.map(function(n) {
        return {label: n.label, type: n.type, visible: isNodeVisible(n), selected: n === selectedNode, degree: n.degree, mentions: n.mentions.length,
                confidence: n.conf, x: n.x * view.k + view.x, y: n.y * view.k + view.y, r: markRadius(n.r, view.k), wx: n.x, wy: n.y};
      }),
      labels: lastLabels.map(function(r) { return {label: r.owner.label, x: r.x, y: r.y, w: r.w, h: r.h}; })
    };
  }
};

layoutIsolates();
filtersChanged();
})();
</script>
</body>
</html>"""
