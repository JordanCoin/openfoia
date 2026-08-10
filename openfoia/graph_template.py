"""Graph visualization HTML template.

Standalone HTML with inline CSS/JS for the entity relationship graph,
document reader, and entity highlighting. Embedded as a Python string
so the output HTML has zero external dependencies — works offline.
"""

from __future__ import annotations

from pathlib import Path


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


def render(graph_json: str, output_path: Path) -> None:
    """Render the graph template with embedded data and write to file."""
    html = _TEMPLATE.replace("__GRAPH_DATA__", escape_json_for_script(graph_json))
    output_path.write_text(html)


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenFOIA Entity Graph</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #e0e0e0; overflow: hidden; }
  #graph { width: 100vw; height: 100vh; cursor: grab; }
  #graph.grabbing { cursor: grabbing; }

  #info-panel {
    position: fixed; top: 16px; right: 16px; width: 380px;
    background: rgba(15, 15, 20, 0.97); border: 1px solid #333;
    border-radius: 10px; padding: 20px; display: none;
    max-height: 85vh; overflow-y: auto; z-index: 10;
    backdrop-filter: blur(12px); box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  #info-panel h2 { font-size: 18px; margin-bottom: 4px; color: #fff; font-weight: 600; }
  #info-panel .entity-type-badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
    background: rgba(255,255,255,0.08); color: #aaa; margin-bottom: 12px;
  }
  #info-panel .label { color: #666; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 14px; margin-bottom: 4px; }
  #info-panel .value { font-size: 14px; margin-bottom: 4px; }
  #info-panel .doc-card {
    background: #111827; border: 1px solid #1e293b; border-radius: 8px;
    padding: 12px; margin: 6px 0; cursor: pointer; transition: all 0.15s;
  }
  #info-panel .doc-card:hover { background: #1e293b; border-color: #3b82f6; }
  #info-panel .doc-card .doc-name { font-size: 13px; color: #93c5fd; font-weight: 500; }
  #info-panel .doc-card .doc-meta { font-size: 11px; color: #64748b; margin-top: 2px; }
  #info-panel .doc-card .doc-context { font-size: 12px; color: #94a3b8; margin-top: 6px; font-style: italic; line-height: 1.4; }
  #info-panel .close { position: absolute; top: 12px; right: 14px; cursor: pointer; color: #666; font-size: 20px; width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; border-radius: 6px; }
  #info-panel .close:hover { background: rgba(255,255,255,0.1); color: #fff; }
  #info-panel .connected-label { font-size: 12px; color: #64748b; margin-top: 2px; }
  .connected-entity { display: inline-block; padding: 2px 8px; margin: 2px; border-radius: 4px; font-size: 11px; cursor: pointer; border: 1px solid #333; }
  .connected-entity:hover { border-color: #666; background: rgba(255,255,255,0.05); }

  #doc-reader {
    position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
    background: rgba(5, 5, 10, 0.98); z-index: 100; display: none;
    backdrop-filter: blur(16px);
  }
  #doc-reader .reader-header {
    height: 56px; display: flex; align-items: center; justify-content: space-between;
    padding: 0 24px; border-bottom: 1px solid #1e293b; background: #0f172a;
  }
  #doc-reader .reader-header h2 { font-size: 15px; color: #e2e8f0; font-weight: 500; flex: 1; }
  #doc-reader .reader-header a.source-link {
    color: #3b82f6; font-size: 13px; text-decoration: none; padding: 6px 12px;
    border: 1px solid #1e3a5f; border-radius: 6px; margin-right: 12px; white-space: nowrap;
  }
  #doc-reader .reader-header a.source-link:hover { background: rgba(59,130,246,0.1); border-color: #3b82f6; }
  #doc-reader .reader-close { cursor: pointer; color: #64748b; font-size: 22px; width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; border-radius: 8px; }
  #doc-reader .reader-close:hover { background: rgba(255,255,255,0.08); color: #fff; }
  #doc-reader .reader-body { display: flex; height: calc(100vh - 56px); }
  #doc-reader .reader-sidebar {
    width: 280px; border-right: 1px solid #1e293b; overflow-y: auto;
    padding: 16px; background: #0f172a; flex-shrink: 0;
  }
  #doc-reader .reader-sidebar h3 { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
  .sidebar-entity {
    padding: 8px 10px; border-radius: 6px; margin: 2px 0; cursor: pointer;
    font-size: 13px; display: flex; align-items: center; gap: 8px; transition: background 0.1s;
  }
  .sidebar-entity:hover { background: rgba(255,255,255,0.05); }
  .sidebar-entity.active { background: rgba(59, 130, 246, 0.15); }
  .sidebar-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .sidebar-entity-name { color: #e2e8f0; }
  .sidebar-entity-count { color: #64748b; font-size: 11px; margin-left: auto; }
  #doc-reader .reader-text {
    flex: 1; overflow-y: auto; padding: 32px 48px; font-size: 14px;
    line-height: 1.8; color: #cbd5e1; white-space: pre-wrap;
    font-family: 'Georgia', 'Times New Roman', serif;
  }
  #doc-reader .reader-text .entity-highlight {
    background: rgba(59, 130, 246, 0.2); border-bottom: 2px solid #3b82f6;
    padding: 1px 2px; border-radius: 2px; cursor: pointer;
  }
  #doc-reader .reader-text .entity-highlight.active {
    background: rgba(250, 204, 21, 0.3); border-bottom-color: #facc15;
  }

  #legend {
    position: fixed; bottom: 16px; left: 16px;
    background: rgba(15, 15, 20, 0.95); border: 1px solid #333;
    border-radius: 8px; padding: 12px 16px; z-index: 10;
  }
  #legend h3 { font-size: 12px; margin-bottom: 8px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }
  .legend-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; color: #94a3b8; cursor: pointer; user-select: none; }
  .legend-item:hover { color: #e2e8f0; }
  .legend-item.hidden { opacity: 0.3; text-decoration: line-through; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }
  #title {
    position: fixed; top: 16px; left: 16px;
    background: rgba(15, 15, 20, 0.95); border: 1px solid #333;
    border-radius: 8px; padding: 12px 16px; z-index: 10;
  }
  #title h1 { font-size: 16px; color: #fff; font-weight: 600; }
  #title p { font-size: 12px; color: #64748b; margin-top: 4px; }
  #title .hint { font-size: 11px; color: #475569; margin-top: 6px; }

  #search-box {
    position: fixed; top: 16px; left: 50%; transform: translateX(-50%);
    z-index: 10; display: flex; gap: 0;
  }
  #search-box input {
    background: rgba(15, 15, 20, 0.95); border: 1px solid #333; border-radius: 8px;
    padding: 8px 14px; color: #e2e8f0; font-size: 13px; width: 280px; outline: none;
  }
  #search-box input:focus { border-color: #3b82f6; }
  #search-box .search-results {
    position: absolute; top: 40px; left: 0; width: 100%; max-height: 240px;
    overflow-y: auto; background: rgba(15, 15, 20, 0.97); border: 1px solid #333;
    border-radius: 8px; display: none;
  }
  #search-box .search-result {
    padding: 8px 14px; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 8px;
  }
  #search-box .search-result:hover { background: rgba(255,255,255,0.05); }
  #search-box .search-result .sr-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }

  #focus-bar {
    position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
    background: rgba(15, 15, 20, 0.95); border: 1px solid #333;
    border-radius: 8px; padding: 10px 20px; z-index: 10; display: none;
    align-items: center; gap: 16px; font-size: 13px;
  }
  #focus-bar .focus-label { color: #94a3b8; }
  #focus-bar .focus-depth { color: #e2e8f0; font-weight: 600; }
  #focus-bar input[type=range] { width: 120px; accent-color: #3b82f6; }
  #focus-bar button {
    background: #1e293b; border: 1px solid #334155; color: #e2e8f0;
    padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px;
  }
  #focus-bar button:hover { background: #334155; }
</style>
</head>
<body>
<div id="title">
  <h1>OpenFOIA Entity Graph</h1>
  <p id="stats"></p>
  <div class="hint">Click node to inspect &middot; Double-click to zoom &middot; Scroll to zoom &middot; Drag to pan</div>
</div>
<div id="search-box">
  <input type="text" placeholder="Search entities..." id="search-input" autocomplete="off">
  <div class="search-results" id="search-results"></div>
</div>
<div id="legend"></div>
<div id="focus-bar">
  <span class="focus-label">Focus mode:</span>
  <span class="focus-depth" id="focus-depth-label">1-hop</span>
  <input type="range" min="1" max="4" value="1" id="focus-depth">
  <button onclick="exitFocusMode()">Back to full graph</button>
</div>
<div id="info-panel">
  <span class="close" onclick="document.getElementById('info-panel').style.display='none'">&times;</span>
  <div id="info-content"></div>
</div>
<div id="doc-reader">
  <div class="reader-header">
    <h2 id="reader-title"></h2>
    <span class="reader-close" onclick="closeReader()">&times;</span>
  </div>
  <div class="reader-body">
    <div class="reader-sidebar" id="reader-sidebar"></div>
    <div class="reader-text" id="reader-text"></div>
  </div>
</div>
<canvas id="graph"></canvas>

<script>
(function() {
"use strict";
var graphData = __GRAPH_DATA__;
var docs = graphData.documents || {};

var TYPE_COLORS = {
  person: '#ef4444', organization: '#3b82f6', location: '#22c55e',
  date: '#f59e0b', money: '#a855f7', document_id: '#14b8a6',
  phone: '#f97316', email: '#ec4899', address: '#06b6d4'
};

var nodeCount = graphData.nodes.length;
var edgeCount = graphData.edges.length;
var typesSet = {};
graphData.nodes.forEach(function(n) { typesSet[n.type] = true; });
var types = Object.keys(typesSet).sort();
document.getElementById('stats').textContent = nodeCount + ' entities, ' + edgeCount + ' relationships';

// Type filter state
var hiddenTypes = {};
// Focus mode state
var focusMode = false;
var focusVisible = {};
var focusDepth = 1;

var legendEl = document.getElementById('legend');
function renderLegend() {
  // Build legend using DOM methods for safety (all data is from local DB)
  legendEl.textContent = '';
  var h3 = document.createElement('h3');
  h3.textContent = 'Entity Types';
  legendEl.appendChild(h3);
  types.forEach(function(t) {
    var item = document.createElement('div');
    item.className = 'legend-item' + (hiddenTypes[t] ? ' hidden' : '');
    item.setAttribute('data-type', t);
    var dot = document.createElement('div');
    dot.className = 'legend-dot';
    dot.style.background = TYPE_COLORS[t] || '#999';
    item.appendChild(dot);
    item.appendChild(document.createTextNode(t));
    item.onclick = function() {
      if (hiddenTypes[t]) delete hiddenTypes[t]; else hiddenTypes[t] = true;
      renderLegend();
    };
    legendEl.appendChild(item);
  });
}
renderLegend();

function isNodeVisible(n) {
  if (hiddenTypes[n.type]) return false;
  if (focusMode && !focusVisible[n.label]) return false;
  return true;
}

// Occurrence index: label -> [{document_id, context, page_number, ...}]
var occurrenceIndex = {};
graphData.nodes.forEach(function(n) {
  if (!occurrenceIndex[n.label]) occurrenceIndex[n.label] = [];
  occurrenceIndex[n.label].push(n);
});

// Entities by document for the reader sidebar
var entitiesByDoc = {};
graphData.nodes.forEach(function(n) {
  if (!entitiesByDoc[n.document_id]) entitiesByDoc[n.document_id] = [];
  entitiesByDoc[n.document_id].push(n);
});

// Deduplicate display nodes
var uniqueNodes = {};
graphData.nodes.forEach(function(n) {
  if (!uniqueNodes[n.label] || n.confidence > uniqueNodes[n.label].confidence) {
    var existing = uniqueNodes[n.label];
    uniqueNodes[n.label] = { id: n.id, label: n.label, type: n.type, confidence: n.confidence, count: (existing ? (existing.count||1) : 0) + 1 };
  } else { uniqueNodes[n.label].count = (uniqueNodes[n.label].count||1) + 1; }
});
var displayNodes = Object.keys(uniqueNodes).map(function(k) { return uniqueNodes[k]; });
var nodeIdToLabel = {};
graphData.nodes.forEach(function(n) { nodeIdToLabel[n.id] = n.label; });

// Canvas
var canvas = document.getElementById('graph');
var ctx = canvas.getContext('2d');
var W, H;
function resize() { W = canvas.width = window.innerWidth; H = canvas.height = window.innerHeight; }
resize();
window.addEventListener('resize', resize);

// Build adjacency for connected entities
var adjacency = {};
graphData.edges.forEach(function(e) {
  var s = nodeIdToLabel[e.source], t = nodeIdToLabel[e.target];
  if (s && t) {
    if (!adjacency[s]) adjacency[s] = [];
    if (!adjacency[t]) adjacency[t] = [];
    adjacency[s].push({label: t, type: e.type});
    adjacency[t].push({label: s, type: e.type});
  }
});

var SIM_NODES = displayNodes.map(function(n) {
  return {
    label: n.label, type: n.type, confidence: n.confidence, count: n.count||1,
    x: W/2 + (Math.random()-0.5)*W*0.6, y: H/2 + (Math.random()-0.5)*H*0.6,
    vx: 0, vy: 0, radius: Math.min(8 + (n.count||1)*2, 24)
  };
});

var labelToIdx = {};
SIM_NODES.forEach(function(n,i) { labelToIdx[n.label] = i; });
var SIM_EDGES = [];
graphData.edges.forEach(function(e) {
  var s = nodeIdToLabel[e.source], t = nodeIdToLabel[e.target];
  if (s && t && labelToIdx[s] !== undefined && labelToIdx[t] !== undefined)
    SIM_EDGES.push({source: labelToIdx[s], target: labelToIdx[t], type: e.type||''});
});

var nodeScale = Math.max(1, Math.sqrt(SIM_NODES.length / 15));
var REPULSION = 5000 * nodeScale, ATTRACTION = 0.003, DAMPING = 0.85, CENTER_GRAVITY = 0.005, LINK_DISTANCE = 180 * nodeScale;
var selectedNode = null;

function simulate() {
  var i,j,dx,dy,dist,force,fx,fy,a,b;
  for (i=0;i<SIM_NODES.length;i++) for (j=i+1;j<SIM_NODES.length;j++) {
    dx=SIM_NODES[j].x-SIM_NODES[i].x; dy=SIM_NODES[j].y-SIM_NODES[i].y;
    dist=Math.sqrt(dx*dx+dy*dy)||1; force=REPULSION/(dist*dist);
    fx=(dx/dist)*force; fy=(dy/dist)*force;
    SIM_NODES[i].vx-=fx; SIM_NODES[i].vy-=fy; SIM_NODES[j].vx+=fx; SIM_NODES[j].vy+=fy;
  }
  for (i=0;i<SIM_EDGES.length;i++) {
    a=SIM_NODES[SIM_EDGES[i].source]; b=SIM_NODES[SIM_EDGES[i].target];
    dx=b.x-a.x; dy=b.y-a.y; dist=Math.sqrt(dx*dx+dy*dy)||1;
    force=(dist-LINK_DISTANCE)*ATTRACTION; fx=(dx/dist)*force; fy=(dy/dist)*force;
    a.vx+=fx; a.vy+=fy; b.vx-=fx; b.vy-=fy;
  }
  for (i=0;i<SIM_NODES.length;i++) {
    SIM_NODES[i].vx+=(W/2-SIM_NODES[i].x)*CENTER_GRAVITY;
    SIM_NODES[i].vy+=(H/2-SIM_NODES[i].y)*CENTER_GRAVITY;
    SIM_NODES[i].vx*=DAMPING; SIM_NODES[i].vy*=DAMPING;
    SIM_NODES[i].x+=SIM_NODES[i].vx; SIM_NODES[i].y+=SIM_NODES[i].vy;
    SIM_NODES[i].x=Math.max(SIM_NODES[i].radius,Math.min(W-SIM_NODES[i].radius,SIM_NODES[i].x));
    SIM_NODES[i].y=Math.max(SIM_NODES[i].radius,Math.min(H-SIM_NODES[i].radius,SIM_NODES[i].y));
  }
}

// Pan/zoom
var offsetX=0, offsetY=0, scale=1, isPanning=false, lastMX=0, lastMY=0, dragNode=null;
var targetScale=1, targetOX=0, targetOY=0, animating=false;

function animateZoom() {
  var dx=targetOX-offsetX, dy=targetOY-offsetY, ds=targetScale-scale;
  if (Math.abs(dx)<0.5 && Math.abs(dy)<0.5 && Math.abs(ds)<0.001) {
    offsetX=targetOX; offsetY=targetOY; scale=targetScale; animating=false; return;
  }
  offsetX+=dx*0.12; offsetY+=dy*0.12; scale+=ds*0.12;
  animating=true; requestAnimationFrame(animateZoom);
}

function zoomToNode(node) {
  targetScale = 2.5;
  targetOX = W/2 - node.x * targetScale;
  targetOY = H/2 - node.y * targetScale;
  if (!animating) animateZoom();
}

// --- Search ---
var searchInput = document.getElementById('search-input');
var searchResults = document.getElementById('search-results');
searchInput.addEventListener('input', function() {
  var q = searchInput.value.toLowerCase().trim();
  searchResults.textContent = '';
  if (q.length < 2) { searchResults.style.display = 'none'; return; }
  var matches = SIM_NODES.filter(function(n) { return n.label.toLowerCase().indexOf(q) >= 0; }).slice(0, 8);
  if (matches.length === 0) { searchResults.style.display = 'none'; return; }
  matches.forEach(function(n) {
    var div = document.createElement('div');
    div.className = 'search-result';
    var dot = document.createElement('div');
    dot.className = 'sr-dot';
    dot.style.background = TYPE_COLORS[n.type] || '#999';
    div.appendChild(dot);
    div.appendChild(document.createTextNode(n.label));
    div.onclick = function() {
      selectedNode = n; showNodeInfo(n); zoomToNode(n);
      searchInput.value = ''; searchResults.style.display = 'none';
    };
    searchResults.appendChild(div);
  });
  searchResults.style.display = 'block';
});
searchInput.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') { searchInput.value = ''; searchResults.style.display = 'none'; searchInput.blur(); }
});
document.addEventListener('click', function(e) {
  if (!document.getElementById('search-box').contains(e.target)) searchResults.style.display = 'none';
});

// --- Focus Mode ---
function enterFocusMode(centerLabel, depth) {
  focusMode = true;
  focusDepth = depth || 1;
  focusVisible = {};
  // BFS from center node
  var queue = [centerLabel];
  var visited = {};
  visited[centerLabel] = 0;
  while (queue.length > 0) {
    var current = queue.shift();
    var d = visited[current];
    focusVisible[current] = true;
    if (d < focusDepth) {
      (adjacency[current] || []).forEach(function(c) {
        if (visited[c.label] === undefined) {
          visited[c.label] = d + 1;
          queue.push(c.label);
        }
      });
    }
  }
  document.getElementById('focus-bar').style.display = 'flex';
  document.getElementById('focus-depth').value = focusDepth;
  document.getElementById('focus-depth-label').textContent = focusDepth + '-hop';
}

window.exitFocusMode = function() {
  focusMode = false; focusVisible = {};
  document.getElementById('focus-bar').style.display = 'none';
};

document.getElementById('focus-depth').addEventListener('input', function() {
  var val = parseInt(this.value);
  if (selectedNode && focusMode) {
    enterFocusMode(selectedNode.label, val);
  }
});

canvas.addEventListener('wheel', function(e) {
  e.preventDefault();
  var f = e.deltaY>0?0.92:1.08;
  offsetX = e.clientX-(e.clientX-offsetX)*f;
  offsetY = e.clientY-(e.clientY-offsetY)*f;
  scale*=f; targetScale=scale; targetOX=offsetX; targetOY=offsetY;
});

function screenToWorld(sx,sy) { return [(sx-offsetX)/scale,(sy-offsetY)/scale]; }
function findNodeAt(sx,sy) {
  var wc=screenToWorld(sx,sy), wx=wc[0], wy=wc[1];
  for (var i=SIM_NODES.length-1;i>=0;i--) {
    var n=SIM_NODES[i];
    if (!isNodeVisible(n)) continue;
    var dx=wx-n.x, dy=wy-n.y;
    if (dx*dx+dy*dy < n.radius*n.radius*1.5) return n;
  }
  return null;
}

canvas.addEventListener('mousedown', function(e) {
  var n = findNodeAt(e.clientX,e.clientY);
  if (n) { dragNode=n; return; }
  isPanning=true; lastMX=e.clientX; lastMY=e.clientY;
  canvas.classList.add('grabbing');
});
canvas.addEventListener('mousemove', function(e) {
  if (dragNode) { var wc=screenToWorld(e.clientX,e.clientY); dragNode.x=wc[0]; dragNode.y=wc[1]; dragNode.vx=0; dragNode.vy=0; }
  else if (isPanning) { offsetX+=e.clientX-lastMX; offsetY+=e.clientY-lastMY; targetOX=offsetX; targetOY=offsetY; lastMX=e.clientX; lastMY=e.clientY; }
  else { canvas.style.cursor = findNodeAt(e.clientX,e.clientY) ? 'pointer' : 'grab'; }
});
canvas.addEventListener('mouseup', function() { isPanning=false; dragNode=null; canvas.classList.remove('grabbing'); });

canvas.addEventListener('click', function(e) {
  var n = findNodeAt(e.clientX,e.clientY);
  if (n) { selectedNode=n; showNodeInfo(n); }
});
canvas.addEventListener('dblclick', function(e) {
  e.preventDefault();
  var n = findNodeAt(e.clientX,e.clientY);
  if (n) zoomToNode(n);
});

// Document reader
window.closeReader = function() { document.getElementById('doc-reader').style.display='none'; };

function openReader(docId, highlightEntity) {
  var doc = docs[docId];
  if (!doc || !doc.text) return;
  var reader = document.getElementById('doc-reader');
  document.getElementById('reader-title').textContent = doc.filename;
  // Source link
  var existingLink = document.getElementById('reader-source-link');
  if (existingLink) existingLink.remove();
  if (doc.source_url) {
    var link = document.createElement('a');
    link.id = 'reader-source-link';
    link.className = 'source-link';
    link.href = doc.source_url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = '\u2197 View original source';
    document.querySelector('.reader-header').insertBefore(link, document.querySelector('.reader-close'));
  }

  // Sidebar: all entities in this document
  var sidebar = document.getElementById('reader-sidebar');
  sidebar.textContent = '';
  var h3 = document.createElement('h3');
  h3.textContent = 'Entities Found';
  sidebar.appendChild(h3);

  var docEntities = entitiesByDoc[docId] || [];
  // Group by label, count occurrences
  var entityCounts = {};
  docEntities.forEach(function(e) {
    if (!entityCounts[e.label]) entityCounts[e.label] = {type: e.type, count: 0};
    entityCounts[e.label].count++;
  });
  var sorted = Object.keys(entityCounts).sort(function(a,b) { return entityCounts[b].count - entityCounts[a].count; });

  sorted.forEach(function(label) {
    var info = entityCounts[label];
    var div = document.createElement('div');
    div.className = 'sidebar-entity' + (label === highlightEntity ? ' active' : '');
    var dot = document.createElement('div');
    dot.className = 'sidebar-dot';
    dot.style.background = TYPE_COLORS[info.type] || '#999';
    div.appendChild(dot);
    var name = document.createElement('span');
    name.className = 'sidebar-entity-name';
    name.textContent = label;
    div.appendChild(name);
    var count = document.createElement('span');
    count.className = 'sidebar-entity-count';
    count.textContent = info.count + 'x';
    div.appendChild(count);
    div.onclick = function() {
      // Highlight this entity in text and scroll to first occurrence
      renderDocText(docId, label);
      sidebar.querySelectorAll('.sidebar-entity').forEach(function(el) { el.classList.remove('active'); });
      div.classList.add('active');
    };
    sidebar.appendChild(div);
  });

  renderDocText(docId, highlightEntity);
  reader.style.display = 'block';
}

function renderDocText(docId, highlightEntity) {
  var doc = docs[docId];
  if (!doc) return;
  var textEl = document.getElementById('reader-text');
  var text = doc.text;

  // Find all entity labels in this document
  var docEntities = entitiesByDoc[docId] || [];
  var labels = [];
  var seen = {};
  docEntities.forEach(function(e) {
    if (!seen[e.label] && e.label.length > 1) { labels.push(e.label); seen[e.label] = e.type; }
  });
  // Sort longest first for greedy matching
  labels.sort(function(a,b) { return b.length - a.length; });

  // Escape and highlight
  var escaped = '';
  var i = 0;
  while (i < text.length) {
    var matched = false;
    for (var li = 0; li < labels.length; li++) {
      var lbl = labels[li];
      var chunk = text.substring(i, i + lbl.length);
      if (chunk.toLowerCase() === lbl.toLowerCase()) {
        var isActive = (lbl === highlightEntity) ? ' active' : '';
        var eType = seen[lbl] || 'organization';
        var col = TYPE_COLORS[eType] || '#999';
        escaped += '<span class="entity-highlight' + isActive + '" data-entity="' + lbl.replace(/"/g,'&quot;') + '" style="border-bottom-color:' + col + '">';
        // escape the actual matched text
        var safe = document.createElement('span');
        safe.textContent = chunk;
        escaped += safe.innerHTML;
        escaped += '</span>';
        i += lbl.length;
        matched = true;
        break;
      }
    }
    if (!matched) {
      var ch = text[i];
      if (ch === '<') escaped += '&lt;';
      else if (ch === '>') escaped += '&gt;';
      else if (ch === '&') escaped += '&amp;';
      else escaped += ch;
      i++;
    }
  }

  textEl.innerHTML = escaped;

  // Scroll to first highlighted entity
  if (highlightEntity) {
    setTimeout(function() {
      var first = textEl.querySelector('.entity-highlight.active');
      if (first) first.scrollIntoView({behavior:'smooth', block:'center'});
    }, 100);
  }

  // Click entity highlights to select them
  textEl.querySelectorAll('.entity-highlight').forEach(function(el) {
    el.onclick = function() {
      var ent = el.getAttribute('data-entity');
      renderDocText(docId, ent);
      // Update sidebar
      document.getElementById('reader-sidebar').querySelectorAll('.sidebar-entity').forEach(function(se) {
        se.classList.toggle('active', se.querySelector('.sidebar-entity-name').textContent === ent);
      });
    };
  });
}

window.openReader = openReader;

function showNodeInfo(node) {
  var panel = document.getElementById('info-panel');
  var content = document.getElementById('info-content');
  var color = TYPE_COLORS[node.type] || '#999';
  var occurrences = occurrenceIndex[node.label] || [];
  content.textContent = '';

  var h2 = document.createElement('h2');
  h2.style.color = color;
  h2.textContent = node.label;
  content.appendChild(h2);

  var badge = document.createElement('div');
  badge.className = 'entity-type-badge';
  badge.textContent = node.type + '  \u00b7  ' + (node.confidence*100).toFixed(0) + '% confidence';
  content.appendChild(badge);

  // Focus mode button
  var focusBtn = document.createElement('button');
  focusBtn.style.cssText = 'background:#1e293b;border:1px solid #334155;color:#93c5fd;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:11px;margin-bottom:8px;';
  focusBtn.textContent = focusMode ? '\u2715 Exit focus' : '\u26b2 Focus on this entity';
  focusBtn.onclick = function() {
    if (focusMode) { window.exitFocusMode(); } else { enterFocusMode(node.label, 1); }
    showNodeInfo(node);
  };
  content.appendChild(focusBtn);

  // Connected entities
  var connected = adjacency[node.label] || [];
  if (connected.length > 0) {
    var connLabel = document.createElement('div');
    connLabel.className = 'label';
    connLabel.textContent = 'Connected (' + connected.length + ')';
    content.appendChild(connLabel);
    var connDiv = document.createElement('div');
    connDiv.style.marginBottom = '8px';
    var connSeen = {};
    connected.forEach(function(c) {
      if (connSeen[c.label]) return;
      connSeen[c.label] = true;
      var tag = document.createElement('span');
      tag.className = 'connected-entity';
      var cType = (uniqueNodes[c.label]||{}).type || 'organization';
      tag.style.color = TYPE_COLORS[cType] || '#999';
      tag.textContent = c.label;
      tag.title = c.type || 'related';
      tag.onclick = function() {
        var idx = labelToIdx[c.label];
        if (idx !== undefined) { selectedNode = SIM_NODES[idx]; showNodeInfo(SIM_NODES[idx]); zoomToNode(SIM_NODES[idx]); }
      };
      connDiv.appendChild(tag);
    });
    content.appendChild(connDiv);
  }

  // Document occurrences
  var docGroups = {};
  occurrences.forEach(function(occ) {
    if (!docGroups[occ.document_id]) docGroups[occ.document_id] = [];
    docGroups[occ.document_id].push(occ);
  });

  var docLabel = document.createElement('div');
  docLabel.className = 'label';
  docLabel.textContent = 'Documents (' + Object.keys(docGroups).length + ')';
  content.appendChild(docLabel);

  Object.keys(docGroups).forEach(function(docId) {
    var occs = docGroups[docId];
    var doc = docs[docId];
    var card = document.createElement('div');
    card.className = 'doc-card';

    var docName = document.createElement('div');
    docName.className = 'doc-name';
    docName.textContent = doc ? doc.filename : ('Document ' + docId.substring(0,8));
    card.appendChild(docName);

    var meta = document.createElement('div');
    meta.className = 'doc-meta';
    var metaParts = [];
    if (doc && doc.page_count) metaParts.push(doc.page_count + ' pages');
    metaParts.push(occs.length + ' mention' + (occs.length>1?'s':''));
    meta.textContent = metaParts.join(' \u00b7 ');
    card.appendChild(meta);

    if (occs[0] && occs[0].context) {
      var ctx = document.createElement('div');
      ctx.className = 'doc-context';
      ctx.textContent = '\u201c' + occs[0].context.substring(0,150) + (occs[0].context.length>150?'\u2026':'') + '\u201d';
      card.appendChild(ctx);
    }

    if (doc && doc.source_url) {
      var srcLink = document.createElement('a');
      srcLink.style.cssText = 'font-size:11px;color:#64748b;text-decoration:none;display:block;margin-top:4px;';
      srcLink.href = doc.source_url;
      srcLink.target = '_blank';
      srcLink.rel = 'noopener';
      srcLink.textContent = '\u2197 ' + doc.source_url.replace(/https?:\/\/(www\.)?/, '').substring(0,50);
      srcLink.onclick = function(e) { e.stopPropagation(); };
      card.appendChild(srcLink);
    }
    if (doc && doc.text) {
      var viewHint = document.createElement('div');
      viewHint.style.cssText = 'font-size:11px;color:#3b82f6;margin-top:4px;';
      viewHint.textContent = '\u2192 Open document reader';
      card.appendChild(viewHint);
      card.onclick = function() { openReader(docId, node.label); };
    } else {
      card.style.opacity = '0.6';
      card.style.cursor = 'default';
      var noText = document.createElement('div');
      noText.style.cssText = 'font-size:11px;color:#475569;margin-top:4px;';
      noText.textContent = 'No extracted text available';
      card.appendChild(noText);
    }
    content.appendChild(card);
  });

  panel.style.display = 'block';
}

function draw() {
  simulate();
  ctx.clearRect(0,0,W,H);
  ctx.save();
  ctx.translate(offsetX,offsetY);
  ctx.scale(scale,scale);

  // Connected set for highlighting
  var connectedLabels = {};
  if (selectedNode) {
    connectedLabels[selectedNode.label] = true;
    (adjacency[selectedNode.label]||[]).forEach(function(c) { connectedLabels[c.label] = true; });
  }

  // Edges — curved to avoid overlap, respect visibility filters
  for (var i=0; i<SIM_EDGES.length; i++) {
    var e=SIM_EDGES[i], a=SIM_NODES[e.source], b=SIM_NODES[e.target];
    if (!isNodeVisible(a) || !isNodeVisible(b)) continue;
    var edgeHighlight = selectedNode && (connectedLabels[a.label] && connectedLabels[b.label]);
    var directEdge = selectedNode && (a.label===selectedNode.label || b.label===selectedNode.label);
    ctx.strokeStyle = directEdge ? 'rgba(255,255,255,0.45)' : (edgeHighlight ? 'rgba(255,255,255,0.2)' : (selectedNode ? 'rgba(255,255,255,0.03)' : 'rgba(255,255,255,0.1)'));
    ctx.lineWidth = directEdge ? 1.5 : 1;
    // Curve edges to reduce overlap
    var dx=b.x-a.x, dy=b.y-a.y, dist=Math.sqrt(dx*dx+dy*dy)||1;
    var curve = dist * 0.15 * (i%2===0 ? 1 : -1);
    var nx=-dy/dist, ny=dx/dist;
    var cpx=(a.x+b.x)/2+nx*curve, cpy=(a.y+b.y)/2+ny*curve;
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.quadraticCurveTo(cpx,cpy,b.x,b.y); ctx.stroke();
  }

  // Nodes — skip hidden types and focus-filtered
  for (var i=0; i<SIM_NODES.length; i++) {
    var n=SIM_NODES[i], color=TYPE_COLORS[n.type]||'#999';
    if (!isNodeVisible(n)) continue;
    var isSelected = selectedNode && n.label === selectedNode.label;
    var isConnected = selectedNode && connectedLabels[n.label];
    var dimmed = selectedNode && !isConnected;

    ctx.beginPath(); ctx.arc(n.x,n.y,isSelected?n.radius+3:n.radius,0,Math.PI*2);
    ctx.fillStyle = color;
    ctx.globalAlpha = dimmed ? 0.15 : 0.85;
    ctx.fill();
    ctx.globalAlpha = 1;

    if (isSelected) {
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 2.5; ctx.stroke();
    } else if (isConnected) {
      ctx.strokeStyle = 'rgba(255,255,255,0.5)'; ctx.lineWidth = 1.5; ctx.stroke();
    } else {
      ctx.strokeStyle = dimmed ? 'rgba(255,255,255,0.05)' : 'rgba(255,255,255,0.2)';
      ctx.lineWidth = 1; ctx.stroke();
    }

    if (!dimmed || isConnected) {
      ctx.fillStyle = dimmed ? 'rgba(255,255,255,0.3)' : '#fff';
      ctx.font = (isSelected ? 'bold 12px' : '11px') + ' sans-serif';
      ctx.textAlign = 'center';
      var lbl = n.label.length>22 ? n.label.substring(0,20)+'\u2026' : n.label;
      ctx.fillText(lbl, n.x, n.y+n.radius+14);
    }
  }

  ctx.restore();
  requestAnimationFrame(draw);
}

// Click background to deselect
canvas.addEventListener('click', function(e) {
  if (!findNodeAt(e.clientX,e.clientY)) { selectedNode=null; document.getElementById('info-panel').style.display='none'; }
});

// Escape to close reader
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    var reader = document.getElementById('doc-reader');
    if (reader.style.display === 'block') { reader.style.display = 'none'; return; }
    selectedNode = null;
    document.getElementById('info-panel').style.display = 'none';
  }
});

draw();
})();
</script>
</body>
</html>"""
