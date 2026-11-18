"""Benchmark all extraction backends head-to-head on the same documents.

Produces:
1. A comparison table of entity precision/recall/F1 and relationship recall
2. An HTML graph visualization of the best result
3. Timing for each backend, and the process's peak RSS

Tier-2 models other than the production one run through the same windowing,
label mapping, scoring and merge code: ``--model urchade/gliner_multi-v2.1``
loads with gliner, ``--model fastino/gliner2-multi-v1`` with gliner2 (#119).
``--multilingual`` also scores the non-Latin sentence set. Not collected by
pytest (file name) and excluded from CI's ``-k "not benchmark"`` selection.
Method and results: docs/benchmarks/gliner2.md.

The graph HTML builds its info panel with createElement/textContent only.
"""

import argparse
import asyncio
import json
import os
import resource
import statistics
import time
from pathlib import Path

from openfoia.config import load_config
from openfoia.graph_template import escape_json_for_script
from openfoia.pipeline.extract import (
    EntityExtractor,
    _gliner_available,
    _llm_available,
)

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
    "Sarah Chen",
    "James R. Whitfield",
    "Robert Garrison",
    "Lisa Park",
    "Maria Gonzalez",
    "Daniel Okoro",
    "Michael Torres",
}

EXPECTED_ORGS = {
    "Department of Justice",
    "Department of Defense",
    "Meridian Defense Systems",
    "Consolidated Federal Services LLC",
    "First National Bank of Virginia",
    "Defense Intelligence Agency",
    "Armed Services Committee",
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


# Every entity in DOC, for precision. (required, also_acceptable) per type:
# recall counts only required; a prediction is correct if it loosely matches
# (substring either way, as check_recall) a required or acceptable string.
GOLD = {
    "person": (EXPECTED_PERSONS, set()),
    "organization": (EXPECTED_ORGS, {"Pentagon"}),
    "location": ({"Washington, DC", "Pentagon"}, {"Virginia"}),
    "money": (EXPECTED_MONEY, set()),
    "date": (
        {
            "March 15, 2026",
            "January 8, 2026",
            "September 12, 2024",
            "February 22, 2025",
            "March 1, 2025",
        },
        {"2018", "2021"},
    ),
    "email": ({"michael.torres@usdoj.gov"}, set()),
    "phone": ({"(202) 514-3642"}, set()),
    "document_id": ({"DOJ-2026-001234"}, set()),
    "address": ({"441 G Street, NW, 6th Floor Washington, DC 20530"}, set()),
}
NER_TYPES = ("person", "organization", "location")

# The non-Latin names of tests/test_extraction_scripts.py, each in one short
# sentence in its language where it is the only named entity (gloss after #),
# plus Latin-script sentences for the mission's Istanbul/Nairobi/Sao Paulo.
# (language, sentence, [(type, gold), ...])
MULTILINGUAL = [
    # Gazprom signed a contract with the ministry last year.
    (
        "ru",
        "Газпром подписал контракт с министерством в прошлом году.",  # noqa: RUF001
        [("organization", "Газпром")],
    ),
    # A. N. published new documents about spending.
    (
        "ru",
        "Алексей Навальный опубликовал новые документы о расходах.",  # noqa: RUF001
        [("person", "Алексей Навальный")],
    ),
    # V. Z. signed a decree on Monday.
    ("uk", "Володимир Зеленський підписав указ у понеділок.", [("person", "Володимир Зеленський")]),  # noqa: RUF001
    # K. M. signed the contract yesterday.
    ("el", "Ο Κυριάκος Μητσοτάκης υπέγραψε τη σύμβαση χθες.", [("person", "Κυριάκος Μητσοτάκης")]),  # noqa: RUF001
    # The Interior Ministry announced a new contract yesterday.
    ("ar", "أعلنت وزارة الداخلية عن عقد جديد أمس.", [("organization", "وزارة الداخلية")]),
    # Muhammad signed the contract yesterday.
    ("ar", "وقّع مُحَمَّد العقد أمس.", [("person", "مُحَمَّد")]),
    # Mr. Benyaminpour signed the contract yesterday.
    ("fa", "آقای بنیامین‌پور قرارداد را دیروز امضا کرد.", [("person", "بنیامین‌پور")]),
    # B. N. signed the contract yesterday.
    ("he", "בנימין נתניהו חתם על החוזה אתמול.", [("person", "בנימין נתניהו")]),
    # The documents were sent to Tehran yesterday.
    ("fa", "اسناد دیروز به تهران فرستاده شد.", [("location", "تهران")]),
    # N. M. signed the contract yesterday.
    ("hi", "नरेन्द्र मोदी ने कल अनुबंध पर हस्ताक्षर किए।", [("person", "नरेन्द्र मोदी")]),
    # The documents were sent from Dhaka yesterday.
    ("bn", "নথিগুলো গতকাল ঢাকা থেকে পাঠানো হয়েছে।", [("location", "ঢাকা")]),
    # P. C. signed the contract yesterday.
    ("th", "ประยุทธ์ จันทร์โอชา ลงนามในสัญญาเมื่อวานนี้", [("person", "ประยุทธ์ จันทร์โอชา")]),
    # These documents were sent to Tokyo yesterday.
    ("zh", "这些文件昨天被寄往东京。", [("location", "东京")]),
    # The documents were sent to Tokyo yesterday.
    ("ja", "書類は昨日東京都に送られた。", [("location", "東京都")]),
    # Wang Wei signed the contract yesterday.
    ("zh", "王伟昨天签署了合同。", [("person", "王伟")]),
    # Toyota Motor signed the contract yesterday.
    ("ja", "トヨタ自動車は昨日契約に署名した。", [("organization", "トヨタ自動車")]),
    # Kim Jong Un signed the document yesterday.
    ("ko", "김정은은 어제 문서에 서명했다.", [("person", "김정은")]),
    # Tbilisi is a big city.
    ("ka", "თბილისი დიდი ქალაქია.", [("location", "თბილისი")]),
    # The documents were sent to Yerevan yesterday.
    ("hy", "Փաստաթղթերը երեկ ուղարկվել են Երևան։", [("location", "Երևան")]),  # noqa: RUF001
    # The documents were sent to Addis Ababa yesterday.
    ("am", "ሰነዶቹ ትናንት ወደ አዲስ አበባ ተልከዋል።", [("location", "አዲስ አበባ")]),
    # The Istanbul Metropolitan Municipality gave the tender to a contractor named A. Y.
    (
        "tr",
        "İstanbul Büyükşehir Belediyesi ihaleyi Ahmet Yılmaz adlı müteahhide verdi.",  # noqa: RUF001
        [("organization", "İstanbul Büyükşehir Belediyesi"), ("person", "Ahmet Yılmaz")],  # noqa: RUF001
    ),
    # The Finance Ministry paid J. M. five million shillings in Nairobi.
    (
        "sw",
        "Wizara ya Fedha ilimlipa John Mwangi shilingi milioni tano jijini Nairobi.",
        [("organization", "Wizara ya Fedha"), ("person", "John Mwangi"), ("location", "Nairobi")],
    ),
    # The Sao Paulo city government paid R$2 million to C. H. Ltda.
    (
        "pt",
        "A Prefeitura de São Paulo pagou R$ 2 milhões à Construtora Horizonte Ltda.",
        [
            ("organization", "Prefeitura de São Paulo"),
            ("organization", "Construtora Horizonte Ltda"),
        ],
    ),
]
# São Paulo inside the city government's name is also a fair location.
MULTILINGUAL_ACCEPTABLE = {"location": {"São Paulo", "İstanbul"}}


def _norm(s):
    return " ".join(s.lower().split()).strip(".,;:")


def _loose(a, b):
    return a in b or b in a


def _exact(a, b):
    return a == b


def prf(tp, fp, found, total):
    """Precision over predictions, recall over gold, and their F1."""
    p = tp / (tp + fp) if tp + fp else 0.0
    r = found / total if total else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def score_types(entities, gold, acceptable=None, strict=False):
    """{type: [tp, fp, found, total]} for every type in gold or predicted.

    Loose (default) is check_recall's substring-either-way rule, which also
    credits "Whitfield" for "James R. Whitfield" -- and a whole run-on CJK
    sentence for the name inside it. Strict wants the normalized text exactly.
    """
    acceptable = acceptable or {}
    match = _exact if strict else _loose
    preds: dict[str, set[str]] = {}
    for ent in entities:
        preds.setdefault(ent.entity_type.value, set()).add(_norm(ent.normalized_text))
    out = {}
    for etype in sorted(set(gold) | set(preds)):
        required = {_norm(g) for g in gold.get(etype, ())}
        ok = required | {_norm(g) for g in acceptable.get(etype, ())}
        got = preds.get(etype, set())
        tp = sum(1 for p in got if any(match(p, g) for g in ok))
        found = sum(1 for g in required if any(match(g, p) for p in got))
        out[etype] = [tp, len(got) - tp, found, len(required)]
    return out


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
            (src_l in fs and tgt_l in ft) or (tgt_l in fs and src_l in ft) for fs, ft in found_pairs
        )
        if matched:
            found += 1
        else:
            missed.add((exp_src, exp_tgt))
    return found, len(expected), missed


def generate_graph_html(result, output_path):
    """Generate a force-directed graph visualization."""
    type_colors = {
        "person": "#e74c3c",
        "organization": "#3498db",
        "location": "#2ecc71",
        "money": "#f39c12",
        "date": "#9b59b6",
        "email": "#1abc9c",
        "phone": "#e67e22",
        "document_id": "#95a5a6",
        "address": "#34495e",
    }

    nodes = []
    node_ids = {}
    for i, ent in enumerate(result.entities):
        nid = f"n{i}"
        node_ids[ent.normalized_text.lower()] = nid
        nodes.append(
            {
                "id": nid,
                "label": ent.normalized_text,
                "type": ent.entity_type.value,
                "color": type_colors.get(ent.entity_type.value, "#bdc3c7"),
                "confidence": ent.confidence,
                "occurrences": ent.metadata.get("occurrence_count", 1),
            }
        )

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
            edges.append(
                {
                    "source": src_id,
                    "target": tgt_id,
                    "label": rel.get("relation", ""),
                    "confidence": rel.get("confidence", 0.5),
                }
            )

    graph_data = json.dumps({"nodes": nodes, "edges": edges})

    # Write the HTML file with embedded graph data. The JSON is spliced straight
    # into a <script> element, so it must be escaped — see escape_json_for_script.
    output_path.write_text(
        _GRAPH_HTML_TEMPLATE.replace("__GRAPH_DATA__", escape_json_for_script(graph_data))
    )
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
const D=__GRAPH_DATA__;
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
const bb=document.createElement('b');bb.textContent='Connections:';b.appendChild(bb);el.appendChild(b);
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


PRODUCTION_MODEL = "urchade/gliner_medium-v2.1"  # what extract._get_gliner loads


class Gliner2Adapter:
    """A GLiNER2 model behind GLiNER's predict_entities, so production windowing,
    label mapping, scoring and merge run unchanged. Counts errors, which
    _mentions_from_gliner would otherwise swallow."""

    def __init__(self, model):
        self.model, self.errors = model, 0

    def predict_entities(self, text, labels, threshold=0.5):
        try:
            out = self.model.extract_entities(
                text, labels, threshold=threshold, include_confidence=True, include_spans=True
            )
        except Exception:
            self.errors += 1
            raise
        return [
            {"text": e["text"], "start": e["start"], "end": e["end"], "label": label}
            | {"score": e["confidence"]}
            for label, ents in out["entities"].items()
            for e in ents
        ]


def load_tier2(model_id, word_splitter=None):
    """Make *model_id* the model the gliner tier uses."""
    from openfoia.pipeline import extract

    extract._GLINER_MODEL = None
    if model_id == PRODUCTION_MODEL:
        return extract._get_gliner()  # the production loader, offline-first
    if model_id.startswith("fastino/"):
        from gliner2 import AutoExtractor

        kw = {"word_splitter": word_splitter} if word_splitter else {}
        model = Gliner2Adapter(AutoExtractor.from_pretrained(model_id, map_location="cpu", **kw))
        extract._gliner_available = lambda: True  # the probe imports `gliner`
    else:
        from gliner import GLiNER

        model = GLiNER.from_pretrained(model_id)
    extract._GLINER_MODEL = model
    return model


def ner_micro(scores):
    """Micro-averaged P/R/F1 over person+organization+location."""
    counts = [sum(v[i] for t, v in scores.items() if t in NER_TYPES) for i in range(4)]
    p, r, f = prf(*counts)
    return {"precision": p, "recall": r, "f1": f, "counts": counts}


def print_prf(scores, strict_scores):
    print(f"  {'type (loose)':<13} {'P':>5} {'R':>5} {'F1':>5}   tp  fp  found/gold")
    for etype, (tp, fp, found, total) in scores.items():
        p, r, f = prf(tp, fp, found, total)
        print(f"  {etype:<13} {p:5.2f} {r:5.2f} {f:5.2f}  {tp:3d} {fp:3d}  {found}/{total}")
    for name, s in (("NER loose", scores), ("NER strict", strict_scores)):
        m = ner_micro(s)
        print(
            f"  {name:<13} {m['precision']:5.2f} {m['recall']:5.2f} {m['f1']:5.2f}  {m['counts']}"
        )
    return ner_micro(scores), ner_micro(strict_scores)


def run_multilingual(ext):
    """Score each MULTILINGUAL sentence as its own document, loose and strict."""
    rows, totals = [], {"loose": [0, 0, 0, 0], "strict": [0, 0, 0, 0]}
    start = time.perf_counter()
    for lang, sentence, gold_pairs in MULTILINGUAL:
        gold: dict[str, set[str]] = {}
        for etype, name in gold_pairs:
            assert name in sentence, (lang, name)
            gold.setdefault(etype, set()).add(name)
        result = asyncio.run(ext.extract(sentence))
        ner = [e for e in result.entities if e.entity_type.value in NER_TYPES]
        row = {
            "lang": lang,
            "got": sorted(f"{e.entity_type.value}:{e.normalized_text}" for e in ner),
        }
        for rule in totals:
            counts = ner_micro(score_types(ner, gold, MULTILINGUAL_ACCEPTABLE, rule == "strict"))
            row[rule] = counts["counts"]
            totals[rule] = [a + b for a, b in zip(totals[rule], counts["counts"], strict=True)]
        rows.append(row)
        print(f"    {lang} loose {row['loose']} strict {row['strict']}  {row['got']}")
    out = {"rows": rows, "s": time.perf_counter() - start}
    for rule, counts in totals.items():
        p, r, f = prf(*counts)
        out[rule] = {"precision": p, "recall": r, "f1": f, "counts": counts}
        print(f"  Multilingual NER {rule}: P {p:.2f} R {r:.2f} F1 {f:.2f}  {counts}")
    return out


def run_interleaved(models, repeat, out):
    """Load every model, then time DOC round-robin, one page per model per round,
    so load from other processes hits all models alike (paired comparison)."""
    from openfoia.pipeline import extract

    loaded = {m: load_tier2(m) for m in models}
    ext = EntityExtractor(tier="gliner")
    times = {m: [] for m in models}
    for _ in range(repeat):
        for m, model in loaded.items():
            extract._GLINER_MODEL = model
            start = time.perf_counter()
            asyncio.run(ext.extract(DOC))
            times[m].append(time.perf_counter() - start)
    report = {"interleaved_page_s": times, "loadavg_after": os.getloadavg()}
    for m, ts in times.items():
        warm = ts[1:] or ts
        print(f"{m:<32} median {statistics.median(warm):.2f}s  min {min(warm):.2f}  n={len(warm)}")
    report["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    if out:
        out.write_text(json.dumps(report, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenFOIA entity extraction benchmark")
    ap.add_argument(
        "--model",
        action="append",
        help="tier-2 model to run instead of the production one (repeatable); "
        "fastino/* ids load with gliner2, others with gliner",
    )
    ap.add_argument("--repeat", type=int, default=1, help="timed extractions of DOC per backend")
    ap.add_argument("--threads", type=int, help="torch.set_num_threads before loading")
    ap.add_argument("--word-splitter", help="GLiNER2 word splitter, e.g. char")
    ap.add_argument("--multilingual", action="store_true", help="also score MULTILINGUAL")
    ap.add_argument("--interleave", action="store_true", help="time all --model ids round-robin")
    ap.add_argument("--json", type=Path, help="write the numbers here")
    ap.add_argument("--graph", type=Path, default=Path("test_graph.html"))
    args = ap.parse_args(argv)

    if args.threads:
        import torch

        torch.set_num_threads(args.threads)
    if args.interleave:
        return run_interleaved(args.model or [PRODUCTION_MODEL], args.repeat, args.json)

    cfg = load_config()
    backends = [("regex", None, None)]
    if args.model:
        backends += [("gliner", None, m) for m in args.model]
    else:
        if _gliner_available():
            backends.append(("gliner", None, PRODUCTION_MODEL))
        if _llm_available(cfg.ai.provider, cfg.ai.api_key, cfg.ai.base_url):
            backends.append(("llm", cfg, None))

    print("=" * 70)
    print("OpenFOIA Entity Extraction Benchmark")
    print("=" * 70)
    print(f"\nDocument: {len(DOC)} chars, {len(DOC.split())} words (one page)")
    print(
        f"Expected: {len(EXPECTED_PERSONS)} persons, {len(EXPECTED_ORGS)} orgs, "
        f"{len(EXPECTED_MONEY)} money, {len(EXPECTED_RELATIONSHIPS)} relationships"
    )
    print(f"Backends: {', '.join(b[0] + (f' {b[2]}' if b[2] else '') for b in backends)}\n")

    best_result = None
    best_score = 0
    report = {"doc_chars": len(DOC), "doc_words": len(DOC.split()), "runs": []}

    for backend_name, cfg_override, model_id in backends:
        ext = EntityExtractor(config=cfg_override, tier=backend_name)
        row = {"backend": backend_name, "model": model_id}

        print(f"--- {backend_name.upper()}{f' {model_id}' if model_id else ''} ---")
        if model_id:
            start = time.perf_counter()
            load_tier2(model_id, args.word_splitter)
            row["load_s"] = time.perf_counter() - start
            print(f"  Load:          {row['load_s']:.1f}s")
        times, cpu = [], []
        row["loadavg_before"] = os.getloadavg()  # other load on the box skews wall time
        for _ in range(max(1, args.repeat)):
            start, start_cpu = time.perf_counter(), time.process_time()
            result = asyncio.run(ext.extract(DOC))
            times.append(time.perf_counter() - start)
            cpu.append(time.process_time() - start_cpu)
        elapsed = times[0]
        row["page_s"], row["page_cpu_s"] = times, cpu

        p_found, p_total, p_missed = check_recall(result, EXPECTED_PERSONS, "person")
        o_found, o_total, o_missed = check_recall(result, EXPECTED_ORGS, "organization")
        m_found, m_total, _m_missed = check_recall(result, EXPECTED_MONEY, "money")
        r_found, r_total, r_missed = check_relationship_recall(result, EXPECTED_RELATIONSHIPS)

        pr = p_found / p_total if p_total else 0
        orr = o_found / o_total if o_total else 0
        mr = m_found / m_total if m_total else 0
        rr = r_found / r_total if r_total else 0
        overall = (pr + orr + mr + rr) / 4

        print(f"  Time:          {elapsed:.1f}s (first extraction)")
        if len(times) > 1:
            warm = times[1:]
            print(
                f"  Warm:          median {statistics.median(warm):.2f}s/page "
                f"(min {min(warm):.2f}, max {max(warm):.2f}, n={len(warm)})"
            )
        print(f"  Entities:      {len(result.entities)} total")
        print(f"  Persons:       {p_found}/{p_total} ({pr:.0%})")
        for m in p_missed:
            print(f"    MISSED: {m}")
        print(f"  Organizations: {o_found}/{o_total} ({orr:.0%})")
        for m in o_missed:
            print(f"    MISSED: {m}")
        print(f"  Money:         {m_found}/{m_total} ({mr:.0%})")
        print(
            f"  Relationships: {r_found}/{r_total} ({rr:.0%}) [{len(result.relationships)} total]"
        )
        for s, t in list(r_missed)[:5]:
            print(f"    MISSED: {s} <-> {t}")
        print(f"  OVERALL:       {overall:.0%}\n")

        gold, ok = {k: v[0] for k, v in GOLD.items()}, {k: v[1] for k, v in GOLD.items()}
        row["types"] = score_types(result.entities, gold, ok)
        row["types_strict"] = score_types(result.entities, gold, ok, strict=True)
        row["ner"], row["ner_strict"] = print_prf(row["types"], row["types_strict"])
        row["relationships"] = [r_found, r_total]
        row["entities"] = sorted(
            f"{e.entity_type.value}:{e.normalized_text}" for e in result.entities
        )
        if args.multilingual:
            print("  Multilingual:")
            row["multilingual"] = run_multilingual(ext)
        if model_id:
            from openfoia.pipeline import extract

            row["errors"] = getattr(extract._GLINER_MODEL, "errors", 0)
            if row["errors"]:
                print(f"  WARNING: {row['errors']} window(s) raised and were skipped")
        report["runs"].append(row)
        print()

        if overall > best_score:
            best_score = overall
            best_result = result

    # Linux reports ru_maxrss in KiB: the peak of this whole process.
    report["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"Peak RSS (whole process): {report['peak_rss_mib']:.0f} MiB")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    if best_result:
        out = args.graph
        generate_graph_html(best_result, out)
        print(f"Open {out} in browser to see the graph")


if __name__ == "__main__":
    main()
