"""Benchmark all extraction backends head-to-head on the same documents.

Produces:
1. A comparison table of entity recall and relationship recall
2. An HTML graph visualization of the best result
3. Timing for each backend

Note: The graph HTML uses innerHTML for the info panel. This is safe because
all content comes from the local extraction pipeline (not user input), and
the graph is a local file opened in the browser for inspection only.
"""

import asyncio
import json
import time
from pathlib import Path

from openfoia.pipeline.extract import (
    EntityExtractor, ExtractionResult,
    _gliner_available, _spacy_available, _llm_available,
)
from openfoia.models import EntityType
from openfoia.config import load_config

# Richer test doc with more entities and relationships
DOC = """
Department of Justice
Office of Information Policy
441 G Street, NW, 6th Floor
Washington, DC 20530

March 15, 2026

RE: FOIA Request No. DOJ-2026-001234

Dear Ms. Sarah Chen,

This letter responds to your Freedom of Information Act request dated January 8, 2026,
in which you requested records pertaining to the contract between Meridian Defense Systems
and the Department of Defense for Project ATLAS, awarded on September 12, 2024.

After a thorough search, we located 247 pages of responsive records. We are releasing
182 pages in full and 65 pages with redactions pursuant to FOIA Exemptions (b)(5) and
(b)(7)(A).

The contract was valued at $14,750,000 and was overseen by Deputy Assistant Secretary
James R. Whitfield at the Pentagon. Payments were routed through Consolidated Federal
Services LLC, a subsidiary of Meridian Defense Systems, to account ending in 4891 at
First National Bank of Virginia.

Additional correspondence was found between Whitfield and Meridian CEO Robert Garrison
regarding modifications to the contract scope. Garrison had previously served as Deputy
Director at the Defense Intelligence Agency from 2018 to 2021.

A separate set of emails between Department of Defense procurement officer Lisa Park and
Whitfield discuss a potential $3,200,000 extension to the contract for Phase 2 of
Project ATLAS. Park raised concerns about sole-source justification in an email dated
February 22, 2025.

Senator Maria Gonzalez of the Armed Services Committee also corresponded with Whitfield
regarding the contract timeline. Her chief of staff, Daniel Okoro, requested a briefing
on March 1, 2025.

If you have questions, contact FOIA analyst Michael Torres at (202) 514-3642 or
michael.torres@usdoj.gov.

Sincerely,
Director, Office of Information Policy
"""

EXPECTED_PERSONS = {
    "Sarah Chen", "James R. Whitfield", "Robert Garrison", "Lisa Park",
    "Maria Gonzalez", "Daniel Okoro", "Michael Torres",
}

EXPECTED_ORGS = {
    "Department of Justice", "Department of Defense", "Meridian Defense Systems",
    "Consolidated Federal Services LLC", "First National Bank of Virginia",
    "Defense Intelligence Agency", "Armed Services Committee",
    "Office of Information Policy",
}

EXPECTED_MONEY = {"$14,750,000", "$3,200,000"}

EXPECTED_RELATIONSHIPS = {
    ("James R. Whitfield", "Department of Defense"),
    ("James R. Whitfield", "Pentagon"),
    ("Robert Garrison", "Meridian Defense Systems"),
    ("Robert Garrison", "Defense Intelligence Agency"),
    ("Lisa Park", "Department of Defense"),
    ("Meridian Defense Systems", "Department of Defense"),
    ("Maria Gonzalez", "Armed Services Committee"),
    ("Daniel Okoro", "Maria Gonzalez"),
    ("Daniel Okoro", "James R. Whitfield"),
    ("Michael Torres", "Department of Justice"),
    ("Consolidated Federal Services LLC", "Meridian Defense Systems"),
}


def check_recall(result, expected, entity_type):
    found_texts = set()
    for ent in result.entities:
        if ent.entity_type.value == entity_type:
            found_texts.add(ent.normalized_text.lower())

    found = 0
    missed = set()
    for exp in expected:
        exp_lower = exp.lower()
        if any(exp_lower in f or f in exp_lower for f in found_texts):
            found += 1
        else:
            missed.add(exp)
    return found, len(expected), missed


def check_relationship_recall(result, expected):
    found_pairs = set()
    for rel in result.relationships:
        src = (rel.get("source") or "").lower()
        tgt = (rel.get("target") or "").lower()
        found_pairs.add((src, tgt))
        found_pairs.add((tgt, src))

    found = 0
    missed = set()
    for exp_src, exp_tgt in expected:
        src_l = exp_src.lower()
        tgt_l = exp_tgt.lower()
        matched = any(
            (src_l in fs and tgt_l in ft) or (tgt_l in fs and src_l in ft)
            for fs, ft in found_pairs
        )
        if matched:
            found += 1
        else:
            missed.add((exp_src, exp_tgt))
    return found, len(expected), missed


def generate_graph_html(result, output_path):
    """Generate a force-directed graph visualization."""
    type_colors = {
        "person": "#e74c3c", "organization": "#3498db", "location": "#2ecc71",
        "money": "#f39c12", "date": "#9b59b6", "email": "#1abc9c",
        "phone": "#e67e22", "document_id": "#95a5a6", "address": "#34495e",
    }

    nodes = []
    node_ids = {}
    for i, ent in enumerate(result.entities):
        nid = f"n{i}"
        node_ids[ent.normalized_text.lower()] = nid
        nodes.append({
            "id": nid, "label": ent.normalized_text, "type": ent.entity_type.value,
            "color": type_colors.get(ent.entity_type.value, "#bdc3c7"),
            "confidence": ent.confidence,
            "occurrences": ent.metadata.get("occurrence_count", 1),
        })

    edges = []
    for rel in result.relationships:
        src = (rel.get("source") or "").lower()
        tgt = (rel.get("target") or "").lower()
        src_id = tgt_id = None
        for key, nid in node_ids.items():
            if src in key or key in src:
                src_id = nid
            if tgt in key or key in tgt:
                tgt_id = nid
        if src_id and tgt_id and src_id != tgt_id:
            edges.append({
                "source": src_id, "target": tgt_id,
                "label": rel.get("relation", ""), "confidence": rel.get("confidence", 0.5),
            })

    graph_data = json.dumps({"nodes": nodes, "edges": edges})

    # Write the HTML file with embedded graph data
    output_path.write_text(_GRAPH_HTML_TEMPLATE.replace("__GRAPH_DATA__", graph_data))
    print(f"\nGraph saved to {output_path}")


_GRAPH_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OpenFOIA Entity Graph</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a2e;color:#fff;font-family:system-ui;overflow:hidden}
canvas{display:block}
#info{position:fixed;top:20px;left:20px;background:rgba(0,0,0,.8);padding:15px;border-radius:8px;max-width:300px;font-size:13px}
#legend{position:fixed;bottom:20px;left:20px;background:rgba(0,0,0,.8);padding:12px;border-radius:8px;font-size:12px}
.li{display:flex;align-items:center;gap:6px;margin:3px 0}
.ld{width:10px;height:10px;border-radius:50%}
#sel{position:fixed;top:20px;right:20px;background:rgba(0,0,0,.9);padding:15px;border-radius:8px;max-width:400px;display:none;font-size:13px}
</style></head><body>
<canvas id="c"></canvas>
<div id="info"><h3 style="margin-bottom:8px">OpenFOIA Entity Graph</h3><p id="stats"></p><p style="color:#888;margin-top:6px">Click node for details. Drag to rearrange.</p></div>
<div id="legend">
<div class="li"><div class="ld" style="background:#e74c3c"></div>Person</div>
<div class="li"><div class="ld" style="background:#3498db"></div>Organization</div>
<div class="li"><div class="ld" style="background:#2ecc71"></div>Location</div>
<div class="li"><div class="ld" style="background:#f39c12"></div>Money</div>
<div class="li"><div class="ld" style="background:#9b59b6"></div>Date</div>
</div>
<div id="sel"></div>
<script>
const D=JSON.parse('__GRAPH_DATA__');
const N=D.nodes,E=D.edges;
const cv=document.getElementById('c'),cx=cv.getContext('2d');
let W,H;
function rsz(){W=cv.width=innerWidth;H=cv.height=innerHeight}
addEventListener('resize',rsz);rsz();
document.getElementById('stats').textContent=N.length+' entities, '+E.length+' relationships';
N.forEach((n,i)=>{const a=(i/N.length)*Math.PI*2,r=Math.min(W,H)*.3;
n.x=W/2+Math.cos(a)*r+(Math.random()-.5)*100;
n.y=H/2+Math.sin(a)*r+(Math.random()-.5)*100;
n.vx=0;n.vy=0;n.r=Math.max(8,Math.min(20,8+(n.occurrences||1)*3))});
const byId={};N.forEach(n=>byId[n.id]=n);
let drag=null,sel=null;
cv.onmousedown=e=>{const r=cv.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
for(const n of N)if(Math.hypot(n.x-mx,n.y-my)<n.r+5){drag=n;sel=n;showSel(n);break}};
cv.onmousemove=e=>{if(!drag)return;const r=cv.getBoundingClientRect();drag.x=e.clientX-r.left;drag.y=e.clientY-r.top;drag.vx=0;drag.vy=0};
cv.onmouseup=()=>{drag=null};
function showSel(n){const p=document.getElementById('sel');
const rels=E.filter(e=>e.source===n.id||e.target===n.id);
const el=document.createElement('div');
const h=document.createElement('h3');h.textContent=n.label;el.appendChild(h);
const sub=document.createElement('p');sub.style.color='#888';
sub.textContent=n.type+' | confidence: '+(n.confidence*100).toFixed(0)+'%';el.appendChild(sub);
if(rels.length){const b=document.createElement('p');b.style.marginTop='8px';
b.innerHTML='<b>Connections:</b>';el.appendChild(b);
rels.forEach(r=>{const other=r.source===n.id?byId[r.target]:byId[r.source];
if(other){const c=document.createElement('p');c.style.color='#ccc';
c.textContent='\u2192 '+r.label+' \u2192 '+other.label;el.appendChild(c)}})}
p.replaceChildren(el);p.style.display='block'}
function sim(){for(let i=0;i<N.length;i++)for(let j=i+1;j<N.length;j++){
const a=N[i],b=N[j];let dx=b.x-a.x,dy=b.y-a.y,d=Math.max(1,Math.hypot(dx,dy)),f=800/(d*d);
a.vx-=dx/d*f;a.vy-=dy/d*f;b.vx+=dx/d*f;b.vy+=dy/d*f}
E.forEach(e=>{const a=byId[e.source],b=byId[e.target];if(!a||!b)return;
let dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy),f=(d-150)*.01;
a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f});
N.forEach(n=>{n.vx+=(W/2-n.x)*.001;n.vy+=(H/2-n.y)*.001});
N.forEach(n=>{if(n===drag)return;n.vx*=.9;n.vy*=.9;n.x+=n.vx;n.y+=n.vy;
n.x=Math.max(n.r,Math.min(W-n.r,n.x));n.y=Math.max(n.r,Math.min(H-n.r,n.y))})}
function draw(){cx.clearRect(0,0,W,H);
E.forEach(e=>{const a=byId[e.source],b=byId[e.target];if(!a||!b)return;
cx.beginPath();cx.moveTo(a.x,a.y);cx.lineTo(b.x,b.y);
const hi=sel&&(e.source===sel.id||e.target===sel.id);
cx.strokeStyle=hi?'rgba(255,255,255,.6)':'rgba(255,255,255,.15)';cx.lineWidth=hi?2:1;cx.stroke();
if(e.label&&hi){cx.fillStyle='rgba(255,255,255,.7)';cx.font='10px system-ui';cx.textAlign='center';
cx.fillText(e.label,(a.x+b.x)/2,(a.y+b.y)/2-5)}});
N.forEach(n=>{cx.beginPath();cx.arc(n.x,n.y,n.r,0,Math.PI*2);cx.fillStyle=n.color;
if(sel&&n.id===sel.id){cx.shadowColor=n.color;cx.shadowBlur=20}cx.fill();cx.shadowBlur=0;
cx.fillStyle='#fff';cx.font=(n.r>12?'12':'10')+'px system-ui';cx.textAlign='center';
cx.fillText(n.label,n.x,n.y+n.r+14)});
sim();requestAnimationFrame(draw)}
draw();
</script></body></html>"""


def main():
    cfg = load_config()
    backends = [("regex", None)]
    if _gliner_available():
        backends.append(("gliner", None))
    if _llm_available(cfg.ai.provider, cfg.ai.api_key, cfg.ai.base_url):
        backends.append(("llm", cfg))

    print("=" * 70)
    print("OpenFOIA Entity Extraction Benchmark")
    print("=" * 70)
    print(f"\nDocument: {len(DOC)} chars")
    print(f"Expected: {len(EXPECTED_PERSONS)} persons, {len(EXPECTED_ORGS)} orgs, "
          f"{len(EXPECTED_MONEY)} money, {len(EXPECTED_RELATIONSHIPS)} relationships")
    print(f"Backends: {', '.join(b[0] for b in backends)}\n")

    best_result = None
    best_score = 0

    for backend_name, cfg_override in backends:
        ext = EntityExtractor(config=cfg_override)
        ext._backend = backend_name

        print(f"--- {backend_name.upper()} ---")
        start = time.time()
        result = asyncio.run(ext.extract(DOC))
        elapsed = time.time() - start

        p_found, p_total, p_missed = check_recall(result, EXPECTED_PERSONS, "person")
        o_found, o_total, o_missed = check_recall(result, EXPECTED_ORGS, "organization")
        m_found, m_total, m_missed = check_recall(result, EXPECTED_MONEY, "money")
        r_found, r_total, r_missed = check_relationship_recall(result, EXPECTED_RELATIONSHIPS)

        pr = p_found/p_total if p_total else 0
        orr = o_found/o_total if o_total else 0
        mr = m_found/m_total if m_total else 0
        rr = r_found/r_total if r_total else 0
        overall = (pr + orr + mr + rr) / 4

        print(f"  Time:          {elapsed:.1f}s")
        print(f"  Entities:      {len(result.entities)} total")
        print(f"  Persons:       {p_found}/{p_total} ({pr:.0%})")
        for m in p_missed: print(f"    MISSED: {m}")
        print(f"  Organizations: {o_found}/{o_total} ({orr:.0%})")
        for m in o_missed: print(f"    MISSED: {m}")
        print(f"  Money:         {m_found}/{m_total} ({mr:.0%})")
        print(f"  Relationships: {r_found}/{r_total} ({rr:.0%}) [{len(result.relationships)} total]")
        for s, t in list(r_missed)[:5]: print(f"    MISSED: {s} <-> {t}")
        print(f"  OVERALL:       {overall:.0%}\n")

        if overall > best_score:
            best_score = overall
            best_result = result

    if best_result:
        out = Path("test_graph.html")
        generate_graph_html(best_result, out)
        print(f"Open {out} in browser to see the graph")


if __name__ == "__main__":
    main()
