"""Measure offline semantic search (issue #118) against FTS5 on a generated corpus.

Builds a throwaway OpenFOIA data directory, fills it with ~1,000 pages of
generated public-records-style text (council minutes, contracts, emails,
audits, police logs) with a few planted "needle" passages that paraphrase a
query, then measures, per model:

* model load time and peak RAM (each phase runs in its own process),
* embedding time per page and index size per 1,000 pages,
* query latency through sqlite-vec and through the numpy fallback,
* where the needle page ranks for each query, next to what FTS5 returns.

Nothing here touches the network. The models must already be downloaded
(``openfoia docs embed-model <name>``); pass the data directory that holds them
with ``--models-from``.

    python scripts/semantic_search_eval.py --models-from ~/.openfoia --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # measure this checkout, not an installed copy

# --- corpus -----------------------------------------------------------------

FIRST = [
    "Maria",
    "James",
    "Ahmet",
    "Wanjiru",
    "Joao",
    "Elena",
    "Robert",
    "Fatima",
    "Daniel",
    "Grace",
    "Kemal",
    "Ana",
]
LAST = [
    "Okafor",
    "Silva",
    "Yilmaz",
    "Kamau",
    "Pereira",
    "Novak",
    "Hughes",
    "Haddad",
    "Brennan",
    "Mwangi",
    "Demir",
    "Costa",
]
ORGS = [
    "Northgate Paving LLC",
    "Riverside Consulting Group",
    "Apex Facilities Ltd",
    "Harbor Light Logistics",
    "Summit Engineering Partners",
    "Blue Ridge Supply Co",
    "Metro Waste Services",
    "Cedar Point Holdings",
]
DEPTS = [
    "Public Works",
    "Parks and Recreation",
    "Finance",
    "Police Department",
    "Fire Department",
    "Planning Commission",
    "Water Utility",
    "School District",
    "Health Department",
]
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
]

SENTENCES = {
    "minutes": [
        "The council meeting was called to order at {h}:{m:02d} p.m. by the presiding officer.",
        "Council member {p} moved to approve the minutes of the {mo} {d} session.",
        "The motion was seconded and carried by a vote of {n} to {n2}.",
        "Residents raised concerns about traffic on {st} Street near the elementary school.",
        "The mayor reported on the status of the {dept} capital improvement plan.",
        "Staff presented a revised schedule for the {st} Avenue resurfacing project.",
        "Public comment was received from {n} residents regarding the proposed fee increase.",
        "The clerk read a proclamation recognizing volunteers of the annual river cleanup.",
        "Discussion of the library parking lot was deferred to the next regular meeting.",
        "The finance director summarized quarterly revenue, which was {pct} percent above forecast.",
        "A public hearing on the zoning variance for {st} Road was scheduled for {mo}.",
        "The meeting adjourned at {h}:{m:02d} p.m. with no further business.",
    ],
    "contract": [
        "This agreement is entered into between the City and {org} as of {mo} {d}, 2024.",
        "The contractor shall furnish all labor, materials and equipment for the work described.",
        "Payment shall be made within thirty days of receipt of an approved invoice.",
        "The total contract price shall not exceed ${amt:,} including all change orders.",
        "The contractor shall maintain general liability insurance of not less than $1,000,000.",
        "Either party may terminate this agreement upon {n} days written notice.",
        "All work shall comply with the specifications on file with the {dept} office.",
        "Retainage of five percent will be withheld until final acceptance of the work.",
        "The contractor certifies that no city employee has a financial interest in this contract.",
        "Disputes shall be resolved under the laws of the state and venue in the county court.",
        "Invoices must reference purchase order {po} and the project number.",
        "Signed on behalf of {org} by {p}, authorized representative.",
    ],
    "email": [
        "From: {p} To: {p2} Subject: follow up on the {dept} schedule",
        "Thanks for the update yesterday, I will circulate the draft to the team.",
        "Can we move the call to {day} afternoon? I have a conflict in the morning.",
        "Attached is the spreadsheet with the revised figures for the second quarter.",
        "Please make sure the vendor gets the signed copy before the end of the week.",
        "I spoke with {p2} and they are fine with the proposed timeline.",
        "Let me know if you need anything else from {dept} before the board meeting.",
        "The printer on the third floor is out of toner again, facilities has been notified.",
        "Reminder that timesheets are due by noon on {day}.",
        "I think we should hold off on the press release until legal has reviewed it.",
        "Sorry for the delay, I was out sick most of last week.",
        "Best regards, {p}",
    ],
    "audit": [
        "The internal audit division reviewed {dept} expenditures for fiscal year 2023.",
        "Our objective was to determine whether controls over disbursements were adequate.",
        "We tested a sample of {n} transactions totaling ${amt:,}.",
        "Supporting documentation was missing for {n2} of the transactions sampled.",
        "We recommend that management strengthen review of purchase card statements.",
        "Management concurred with the finding and provided a corrective action plan.",
        "Segregation of duties between ordering and receiving was not consistently enforced.",
        "The prior year finding related to inventory counts has been resolved.",
        "Travel reimbursements were generally processed in accordance with policy.",
        "This report is intended for the information of the council and management.",
        "We conducted our audit in accordance with generally accepted auditing standards.",
        "Fixed asset records were reconciled to the general ledger without exception.",
    ],
    "police": [
        "Officer {p} responded to a report of a disturbance at the {st} Street apartments.",
        "Units were dispatched at {h}:{m:02d} hours for a vehicle collision with injuries.",
        "The complainant stated that her bicycle was taken from the porch overnight.",
        "No arrests were made and the case was referred to the detective bureau.",
        "A welfare check was conducted at the request of a family member.",
        "The suspect vehicle was described as a gray sedan with partial plate {po}.",
        "Patrol logged {n} traffic stops and issued {n2} citations during the shift.",
        "The sergeant reviewed body camera footage as part of routine supervision.",
        "An alarm call at the pharmacy on {st} Avenue was determined to be false.",
        "Evidence was logged and placed in the property room under case {po}.",
        "The shift commander approved the overtime request for the festival detail.",
        "Report completed by {p}, badge {n}{n2}.",
    ],
}
STREETS = ["Oak", "Maple", "Elm", "Harbor", "Mill", "Church", "Lake", "Hill", "Market", "Bridge"]

# (query, needle planted once, lexical distractor planted once or None).
# The distractor shares the query's words but not its meaning, so a keyword
# match on it is a false positive. Needles share as few words as possible.
CASES = [
    (
        "payments to the mayor's brother",
        "Funds were transferred to a sibling of the city executive through a consulting firm "
        "that had been registered only weeks earlier.",
        "The mayor's brother gave a short speech at the parade; payments for the floats came "
        "from the chamber of commerce.",
    ),
    (
        "police officer fired for misconduct",
        "The patrolman was dismissed from the force after internal affairs substantiated the "
        "allegations of abuse against detainees.",
        "The police officer fired a warning signal at the start of the charity race.",
    ),
    (
        "contract awarded without competitive bidding",
        "The procurement office granted the agreement to a single vendor; no other offers were "
        "solicited and no notice was published.",
        None,
    ),
    (
        "school lunch program budget cut",
        "Funding for cafeteria meals for students will be reduced by forty percent next fiscal "
        "year, the superintendent wrote.",
        None,
    ),
    (
        "lead contamination in drinking water",
        "Tests found elevated levels of a toxic heavy metal leaching from aging service lines "
        "into household taps.",
        "Staff will take the lead on drafting the water conservation brochure.",
    ),
    (
        "bribe paid to the building inspector",
        "An envelope of cash was handed to the official who certifies construction permits in "
        "exchange for signing off on the project.",
        None,
    ),
    (
        "firefighters claiming overtime they did not work",
        "Several members of the fire brigade reported extra hours they never served, inflating "
        "their pay for most of the year.",
        None,
    ),
    (
        "emails deleted before the audit",
        "Staff erased correspondence from the mail server days before the examiners arrived to "
        "review the accounts.",
        None,
    ),
    (
        "patients died because the hospital was understaffed",
        "Fatalities on the ward rose after nurse shifts were left vacant for months.",
        None,
    ),
    (
        "developer donated to the councilwoman's campaign",
        "A real estate company that later won the rezoning gave money to her reelection fund.",
        None,
    ),
    # Exact strings: what keyword search is for. Semantic search is not
    # expected to win these; they are here to show the trade-off honestly.
    ("INV-2024-0917", "Invoice INV-2024-0917 for road salt was paid twice in March.", None),
    (
        "Hocking Technical College",
        "Two trainees were sent to Hocking Technical College for the certification course.",
        None,
    ),
]
# Non-English needles, each queried in its own language and in English.
MULTI = [
    (
        "pt",
        "Os recursos foram transferidos para o irmão do prefeito por meio de uma empresa de "
        "consultoria.",
        "pagamentos ao irmão do prefeito",
        "payments to the mayor's brother",
    ),
    (
        "tr",
        "Belediye başkanının kardeşine bir danışmanlık şirketi üzerinden para aktarıldı.",  # noqa: RUF001 (Turkish dotless i)
        "belediye başkanının kardeşine yapılan ödemeler",  # noqa: RUF001
        "payments to the mayor's brother",
    ),
    (
        "es",
        "La empresa constructora entregó dinero en efectivo al inspector municipal a cambio del "
        "permiso.",
        "soborno al inspector de obras",
        "bribe paid to the building inspector",
    ),
    (
        "sw",
        "Fedha za umma zilihamishiwa kwa kaka wa meya kupitia kampuni ya ushauri.",
        "malipo kwa kaka wa meya",
        "payments to the mayor's brother",
    ),
]


def _fill(rng: random.Random, template: str) -> str:
    def person() -> str:
        return f"{rng.choice(FIRST)} {rng.choice(LAST)}"

    return template.format(
        p=person(),
        p2=person(),
        org=rng.choice(ORGS),
        dept=rng.choice(DEPTS),
        mo=rng.choice(MONTHS),
        d=rng.randint(1, 28),
        h=rng.randint(1, 11),
        m=rng.randint(0, 59),
        n=rng.randint(2, 40),
        n2=rng.randint(1, 9),
        pct=rng.randint(1, 12),
        amt=rng.randint(10, 900) * 1000,
        st=rng.choice(STREETS),
        po=f"PO-{rng.randint(10000, 99999)}",
        day=rng.choice(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]),
    )


def generate(pages: int, seed: int = 118):
    """Return (documents, needles): documents as (title, [page text]), needles as
    {label: (doc index, page number)}."""
    rng = random.Random(seed)
    kinds = list(SENTENCES)
    docs: list[tuple[str, list[str]]] = []
    made = 0
    while made < pages:
        kind = rng.choice(kinds)
        n_pages = min(rng.randint(3, 20), pages - made)
        body = []
        for _ in range(n_pages):
            # ~300 words: a FOIA page of running text, not a dense table.
            sents = [_fill(rng, rng.choice(SENTENCES[kind])) for _ in range(rng.randint(20, 26))]
            body.append(" ".join(sents))
        docs.append((f"{kind} {len(docs) + 1:03d}", body))
        made += n_pages

    needles: dict[str, tuple[int, int]] = {}

    def plant(label: str, sentence: str) -> None:
        while True:
            di = rng.randrange(len(docs))
            pi = rng.randrange(len(docs[di][1]))
            if (di, pi + 1) not in needles.values():
                break
        sents = docs[di][1][pi].split(". ")
        sents.insert(rng.randrange(1, len(sents)), sentence.rstrip("."))
        docs[di][1][pi] = ". ".join(sents)
        needles[label] = (di, pi + 1)

    for i, (_, needle, distractor) in enumerate(CASES):
        plant(f"q{i}", needle)
        if distractor:
            plant(f"q{i}-distractor", distractor)
    for lang, needle, _, _ in MULTI:
        plant(f"ml-{lang}", needle)
    return docs, needles


# --- phases (each in its own process, so peak RSS is per phase) --------------


def _rss_mb() -> float:
    # ru_maxrss is KiB on Linux, bytes on macOS.
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def _db_bytes() -> int:
    from openfoia.db import get_db_path

    p = get_db_path()
    return sum(Path(str(p) + s).stat().st_size for s in ("", "-wal") if Path(str(p) + s).exists())


def phase_build(pages: int) -> dict:
    from uuid import uuid4

    from openfoia import doctext
    from openfoia.db import get_session, init_db
    from openfoia.models import Agency, DeliveryMethod, Document, DocumentType, Request, User

    init_db()
    docs, needles = generate(pages)
    ids = []
    with get_session() as s:
        user = User(id=str(uuid4()), email="eval@example.org", name="Eval")
        s.add(user)
        agency = s.query(Agency).first().id
        for title, body in docs:
            req = Request(
                id=str(uuid4()),
                request_number=f"DC-{uuid4().hex[:6]}",
                requester_id=user.id,
                agency_id=agency,
                subject=title,
                body="",
                delivery_method=DeliveryMethod.EMAIL,
            )
            s.add(req)
            doc = Document(
                id=str(uuid4()),
                request_id=req.id,
                doc_type=DocumentType.FULL_RESPONSE,
                filename=f"{title}.pdf",
                file_path="",
                file_size=0,
                mime_type="application/pdf",
                page_count=len(body),
                extracted_text=doctext.join_pages(body),
            )
            s.add(doc)
            doctext.replace_pages(s, doc.id, body)
            ids.append(doc.id)
    words = sum(len(p.split()) for _, b in docs for p in b)
    return {
        "pages": sum(len(b) for _, b in docs),
        "documents": len(docs),
        "avg_words_per_page": round(words / pages),
        "needles": {k: [ids[d], p] for k, (d, p) in needles.items()},
        "db_bytes_before_embedding": _db_bytes(),
    }


def phase_embed(model: str) -> dict:
    from openfoia.db import get_session
    from openfoia.semantic import embed_missing, index_status, load_embedder

    before = _db_bytes()
    rss_base = _rss_mb()
    t0 = time.perf_counter()
    embedder = load_embedder(model)
    load_s = time.perf_counter() - t0
    rss_loaded = _rss_mb()
    with get_session() as s:
        t1 = time.perf_counter()
        n = embed_missing(s, embedder)
        embed_s = time.perf_counter() - t1
    with get_session() as s:
        st = index_status(s).by_model[model]
    return {
        "rss_before_load_mb": round(rss_base, 1),
        "load_s": round(load_s, 3),
        "rss_after_load_mb": round(rss_loaded, 1),
        "pages_embedded": n,
        "chunks": st["chunks"],
        "embed_s": round(embed_s, 3),
        "ms_per_page": round(embed_s * 1000 / n, 3),
        "vector_bytes": st["bytes"],
        "db_growth_bytes": _db_bytes() - before,
        "peak_rss_mb": round(_rss_mb(), 1),
    }


def _rank(ordered_pages: list[tuple[str, int]], target: tuple[str, int]) -> int | None:
    seen: list[tuple[str, int]] = []
    for p in ordered_pages:
        if p not in seen:
            seen.append(p)
    return seen.index(target) + 1 if target in seen else None


def phase_query(model: str, backend: str, needles: dict, repeats: int) -> dict:
    from openfoia.db import get_session
    from openfoia.semantic import load_embedder, semantic_search

    embedder = load_embedder(model)
    results, lat = {}, []
    with get_session() as s:
        queries = [(f"q{i}", q) for i, (q, _, _) in enumerate(CASES)]
        for lang, _, native, english in MULTI:
            queries += [(f"ml-{lang}", native), (f"ml-{lang}", english)]
        for label, q in queries:
            target = tuple(needles[label])
            # Latency at the CLI default (-n 10), including the query's embedding.
            for _ in range(repeats):
                t0 = time.perf_counter()
                semantic_search(s, q, embedder, limit=10, backend=backend)
                lat.append((time.perf_counter() - t0) * 1000)
            # Ranks need the whole list.
            hits, used, why = semantic_search(s, q, embedder, limit=1000, backend=backend)
            ranked = [(h.document_id, h.page) for h in hits]
            entry = {"rank": _rank(ranked, target), "top_score": hits[0].score if hits else None}
            d = needles.get(f"{label}-distractor")
            if d:
                entry["distractor_rank"] = _rank(ranked, tuple(d))
            results[f"{label}|{q}"] = entry
    lat.sort()
    return {
        "backend": used,
        "why_not_sqlite_vec": why,
        "latency_ms_median": round(statistics.median(lat), 2),
        "latency_ms_p95": round(lat[int(len(lat) * 0.95) - 1], 2),
        "queries": results,
        "peak_rss_mb": round(_rss_mb(), 1),
    }


def phase_fts(needles: dict) -> dict:
    import re

    from openfoia.db import get_session
    from openfoia.doctext import search_pages

    out = {}
    with get_session() as s:
        queries = [(f"q{i}", q) for i, (q, _, _) in enumerate(CASES)]
        queries += [(f"ml-{lang}", native) for lang, _, native, _ in MULTI]
        for label, q in queries:
            target = tuple(needles[label])
            words = re.findall(r"\w+", q)
            entry = {}
            for mode, fq in (("as typed", q), ("any word (OR)", " OR ".join(words))):
                t0 = time.perf_counter()
                hits, engine = search_pages(s, fq, limit=5000)
                ms = (time.perf_counter() - t0) * 1000
                ranked = [(h.document_id, h.page) for h in hits]
                e = {
                    "engine": engine,
                    "pages_returned": len(set(ranked)),
                    "rank": _rank(ranked, target),
                    "ms": round(ms, 2),
                }
                d = needles.get(f"{label}-distractor")
                if d:
                    e["distractor_rank"] = _rank(ranked, tuple(d))
                entry[mode] = e
            out[f"{label}|{q}"] = entry
    return out


def phase_scale(sizes: list[int], dim: int, repeats: int) -> dict:
    """Search latency on N synthetic vectors (no model): how brute force scales."""
    import numpy as np
    from sqlalchemy import create_engine

    from openfoia.semantic import (
        VECTOR_TABLE,
        _rank_numpy,
        _rank_sqlite_vec,
        load_sqlite_vec,
        pack,
    )

    out = {}
    rng = np.random.default_rng(0)
    tmp = Path(tempfile.mkdtemp())
    for n in sizes:
        eng = create_engine(f"sqlite:///{tmp / f'scale{n}.db'}")
        with eng.begin() as conn:
            conn.exec_driver_sql(
                f"CREATE TABLE {VECTOR_TABLE} (id INTEGER PRIMARY KEY, page_id INT, "
                "document_id TEXT, chunk INT, char_start INT, char_end INT, model TEXT, "
                "dim INT, vector BLOB)"
            )
            conn.exec_driver_sql(f"CREATE INDEX ix_m ON {VECTOR_TABLE}(model, document_id)")
            vecs = rng.standard_normal((n, dim)).astype("<f4")
            conn.exec_driver_sql(
                f"INSERT INTO {VECTOR_TABLE} (page_id, document_id, chunk, char_start, "
                "char_end, model, dim, vector) VALUES (?, 'd', 0, 0, 1, 'm', ?, ?)",
                [(i, dim, vecs[i].tobytes()) for i in range(n)],
            )
        row = {}
        with eng.connect() as conn:
            q = rng.standard_normal(dim).astype("<f4")
            ok, _ = load_sqlite_vec(conn)
            for name, fn, arg in (
                ("sqlite-vec", _rank_sqlite_vec, pack(q)),
                ("numpy", _rank_numpy, q.tolist()),
            ):
                if name == "sqlite-vec" and not ok:
                    continue
                times = []
                for _ in range(repeats):
                    t0 = time.perf_counter()
                    fn(conn, arg, "m", "", {}, 50)
                    times.append((time.perf_counter() - t0) * 1000)
                row[name] = round(statistics.median(times), 2)
        out[str(n)] = row
        eng.dispose()
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def _child(phase: str, data_dir: Path, *args: str) -> dict:
    env = {**os.environ, "OPENFOIA_DATA_DIR": str(data_dir), "PYTHONPATH": str(ROOT)}
    res = subprocess.run(
        [sys.executable, __file__, "--phase", phase, *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise SystemExit(f"phase {phase} failed:\n{res.stderr[-3000:]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


def _cli_wall(data_dir: Path, grep_args: list[str], repeats: int = 3) -> float:
    """Median wall time of a whole `openfoia docs grep` process (start, load, search)."""
    env = {**os.environ, "OPENFOIA_DATA_DIR": str(data_dir), "PYTHONPATH": str(ROOT)}
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        subprocess.run(
            [sys.executable, "-m", "openfoia.cli", "docs", "grep", *grep_args, "--all-docs"],
            env=env,
            capture_output=True,
            check=True,
        )
        times.append(time.perf_counter() - t0)
    return round(statistics.median(times), 2)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models-from", type=Path, help="data dir holding models/ (downloaded)")
    ap.add_argument("--model", action="append", help="model(s) to measure; default all present")
    ap.add_argument("--pages", type=int, default=1000)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--scale", default="1000,10000,50000,100000")
    ap.add_argument("--chunk", help="WORDS,OVERLAP per embedded chunk (default: the module's)")
    ap.add_argument("--json", type=Path, help="write all results here")
    ap.add_argument("--phase", help=argparse.SUPPRESS)
    ap.add_argument("phase_args", nargs="*", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.phase:
        chunk = os.environ.get("OPENFOIA_EVAL_CHUNK")
        if chunk:
            from openfoia import semantic

            semantic.CHUNK_WORDS, semantic.CHUNK_OVERLAP = (int(x) for x in chunk.split(","))
        if a.phase == "build":
            r = phase_build(int(a.phase_args[0]))
        elif a.phase == "embed":
            r = phase_embed(a.phase_args[0])
        elif a.phase == "query":
            m, backend, needles, reps = a.phase_args
            r = phase_query(m, backend, json.loads(needles), int(reps))
        elif a.phase == "fts":
            r = phase_fts(json.loads(a.phase_args[0]))
        elif a.phase == "scale":
            sizes, dim, reps = a.phase_args
            r = phase_scale([int(x) for x in sizes.split(",")], int(dim), int(reps))
        else:
            raise SystemExit(f"unknown phase {a.phase}")
        print(json.dumps(r))
        return

    if not a.models_from:
        ap.error("--models-from is required")
    if a.chunk:
        os.environ["OPENFOIA_EVAL_CHUNK"] = a.chunk
    src_models = (a.models_from / "models").resolve()
    from openfoia.semantic import MODELS

    names = a.model or [n for n in MODELS if (src_models / n / "openfoia-model.json").exists()]
    if not names:
        raise SystemExit(f"No downloaded models under {src_models}")

    from openfoia import semantic

    report: dict = {
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "chunk": a.chunk or f"{semantic.CHUNK_WORDS},{semantic.CHUNK_OVERLAP}",
        "models": {},
    }
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        built = _child("build", base, str(a.pages))
        report["corpus"] = {k: v for k, v in built.items() if k != "needles"}
        needles = json.dumps(built["needles"])
        report["fts5"] = _child("fts", base, needles)
        for name in names:
            work = Path(tmp) / name
            shutil.copytree(base, work)
            (work / "models").mkdir(exist_ok=True)
            os.symlink(src_models / name, work / "models" / name)
            spec = MODELS[name]
            r = {"download_bytes": spec.download_bytes, "embed": _child("embed", work, name)}
            for backend in ("sqlite-vec", "numpy"):
                r[backend] = _child("query", work, name, backend, needles, str(a.repeats))
            r["cli_wall_s"] = _cli_wall(work, ["--semantic", CASES[0][0], "--model", name])
            report["models"][name] = r
            report.setdefault("fts5_cli_wall_s", _cli_wall(work, ["Hocking"]))
            print(f"{name}: done", file=sys.stderr)
        report["scale"] = {
            str(d): _child("scale", base, a.scale, str(d), str(a.repeats)) for d in (256, 512)
        }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if a.json:
        a.json.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
