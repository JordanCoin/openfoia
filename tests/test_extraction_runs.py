"""Versioned extraction runs (#63): re-extracting keeps earlier runs, readers use one.

Every reader of entities must see only each document's current run, or a
document re-extracted three times is counted three times in the graph,
crossref, exports, search and the agent. Pruning a document's runs and
purging must leave no runs or entities behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.models import EntityType

runner = CliRunner()

TEXT_V1 = "Payment of $1,250,000 was approved on March 3, 2024. Contact jane.roe@example.org."
TEXT_V2 = "Invoice dated June 9, 2025 for $87,000. Questions: press@example.net or 555-867-5309."


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    from openfoia import db as dbmod

    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    monkeypatch.setenv("OPENFOIA_AI_PROVIDER", "none")
    monkeypatch.setattr(dbmod, "_schema_checked", False)
    return tmp_path / "data"


def _ok(result):
    assert result.exit_code == 0, result.output
    return result.output


def _document(text: str = TEXT_V1, number: str = "REQ-RUNS-1") -> str:
    from openfoia.db import get_session, init_db
    from openfoia.models import Agency, DeliveryMethod, Document, DocumentType, Request, User

    init_db()
    with get_session() as s:
        user = s.query(User).first() or User(email="runs@example.invalid", name="Runs")
        s.add(user)
        s.flush()
        req = Request(
            request_number=number,
            requester_id=user.id,
            agency_id=s.query(Agency).first().id,
            subject="Port tender",
            body="",
            delivery_method=DeliveryMethod.EMAIL,
        )
        s.add(req)
        s.flush()
        doc = Document(
            request_id=req.id,
            doc_type=DocumentType.FULL_RESPONSE,
            filename=f"{number}.pdf",
            file_path="",
            file_size=len(text),
            mime_type="application/pdf",
            extracted_text=text,
        )
        s.add(doc)
        s.flush()
        return doc.id


def _result(*names: str, etype: EntityType = EntityType.PERSON, link: bool = True):
    from openfoia.pipeline.extract import ExtractedEntity

    entities = [ExtractedEntity(etype, n, n, 0.9, f"... {n} ...") for n in names]
    rels = [{"source": names[0], "target": names[1], "relation": "related_to"}] if link else []
    return SimpleNamespace(entities=entities, relationships=rels)


def _store(doc_id: str, *names: str, backend: str = "regex"):
    from openfoia.db import get_session
    from openfoia.extraction_runs import record_run
    from openfoia.models import Document

    with get_session() as s:
        run, _ = record_run(
            s, s.get(Document, doc_id), _result(*names), backend=backend, model=None
        )
        return run.id


def _legacy_entities(doc_id: str, *names: str) -> None:
    """Rows as an extraction before migration 008 left them: no run."""
    from openfoia.db import get_session
    from openfoia.models import Document, Entity, entity_links

    with get_session() as s:
        ents = [
            Entity(
                document_id=doc_id,
                entity_type=EntityType.PERSON,
                raw_text=n,
                normalized_text=n,
                confidence=0.9,
            )
            for n in names
        ]
        s.add_all(ents)
        s.flush()
        s.execute(entity_links.insert().values(source_id=ents[0].id, target_id=ents[1].id))
        s.get(Document, doc_id).entities_extracted = True


def _rows(sql: str) -> list[tuple]:
    from openfoia.db import get_db_path

    con = sqlite3.connect(get_db_path())
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _graph_labels(tmp_path) -> set[str]:
    out = tmp_path / "graph.json"
    _ok(runner.invoke(app, ["analyze", "graph", "--all", "--no-text", "-o", str(out)]))
    return {n["label"] for n in json.loads(out.read_text())["nodes"]}


# --- schema -------------------------------------------------------------------


def test_fresh_database_has_runs_table_and_entity_run_column():
    from openfoia.db import init_db

    init_db(seed=False)
    assert _rows("select version_num from alembic_version") == [("008",)]
    cols = {r[1] for r in _rows("pragma table_info(extraction_runs)")}
    assert {
        "id",
        "document_id",
        "backend",
        "model",
        "prompt_hash",
        "started_at",
        "completed_at",
        "entity_count",
        "is_current",
        "notes",
    } <= cols
    fks = _rows("pragma foreign_key_list(entities)")
    assert any(r[2] == "extraction_runs" and r[3] == "extraction_run_id" for r in fks)


def test_upgrade_from_007_keeps_entities_without_a_run(data_dir, monkeypatch):
    from alembic import command

    from openfoia import db as dbmod

    cfg = dbmod._alembic_config()
    data_dir.mkdir(parents=True)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{dbmod.get_db_path()}")
    command.upgrade(cfg, "007")
    con = sqlite3.connect(dbmod.get_db_path())
    con.executescript(
        """
        insert into users (id, email, name, is_journalist, created_at)
            values ('u1', 'a@b.c', 'A', 0, '2026-01-01');
        insert into agencies (id, name, level, preferred_method, total_requests_tracked)
            values ('a1', 'Agency', 'FEDERAL', 'EMAIL', 0);
        insert into requests (id, request_number, requester_id, agency_id, subject, body,
            delivery_method, status, fee_waiver_requested, expedited_requested, created_at)
            values ('r1', 'REQ-OLD', 'u1', 'a1', 's', 'b', 'EMAIL', 'DRAFT', 1, 0, '2026-01-01');
        insert into documents (id, request_id, doc_type, filename, file_path, file_size,
            mime_type, ocr_completed, extracted_text, entities_extracted, received_at)
            values ('d1', 'r1', 'FULL_RESPONSE', 'old.pdf', '', 1, 'application/pdf', 1,
            'x', 1, '2026-01-01');
        insert into entities (id, document_id, entity_type, raw_text, normalized_text,
            confidence) values ('e1', 'd1', 'PERSON', 'Ana Old', 'Ana Old', 0.9);
        """
    )
    con.commit()
    con.close()

    # get_session() migrates a database that is behind head.
    from openfoia.extraction_runs import current_entities
    from openfoia.models import Entity

    with dbmod.get_session() as s:
        (ent,) = current_entities(s.query(Entity)).all()
        assert ent.id == "e1" and ent.extraction_run_id is None
    assert _rows("select version_num from alembic_version") == [("008",)]

    # The ADD COLUMN carries a real foreign key with ON DELETE CASCADE.
    run_id = _store("d1", "Ana New", "Bo New")
    con = sqlite3.connect(dbmod.get_db_path())
    con.execute("pragma foreign_keys=on")
    con.execute("delete from entity_links")
    con.execute("delete from extraction_runs where id = ?", (run_id,))
    con.commit()
    left = con.execute("select count(*) from entities where extraction_run_id = ?", (run_id,))
    assert left.fetchone() == (0,)
    con.close()


def test_only_one_current_run_per_document_is_enforced():
    from sqlalchemy.exc import IntegrityError

    from openfoia.db import get_session
    from openfoia.models import ExtractionRun

    doc_id = _document()
    _store(doc_id, "A One", "B One")
    with pytest.raises(IntegrityError), get_session() as s:
        s.add(ExtractionRun(document_id=doc_id, backend="regex", is_current=True))
        s.flush()


# --- extract / runs / prefer-run -----------------------------------------------


def test_force_reextract_keeps_the_earlier_run_and_lists_both(tmp_path):
    doc_id = _document(TEXT_V1)
    _ok(runner.invoke(app, ["analyze", "extract", doc_id, "--tier", "regex"]))
    first = _graph_labels(tmp_path)
    assert "$1,250,000" in first

    from openfoia.db import get_session
    from openfoia.models import Document

    with get_session() as s:
        s.get(Document, doc_id).extracted_text = TEXT_V2
    _ok(runner.invoke(app, ["analyze", "extract", doc_id, "--tier", "regex", "--force"]))

    runs = _rows(
        "select id, backend, model, entity_count, is_current, started_at, completed_at "
        "from extraction_runs order by started_at"
    )
    assert len(runs) == 2
    assert [r[1] for r in runs] == ["regex", "regex"]
    assert [r[2] for r in runs] == [None, None]
    assert [r[4] for r in runs] == [0, 1]
    assert all(r[5] and r[6] for r in runs)
    # Both runs' entities are still stored, each pointing at its run.
    for run_id, _backend, _model, count, *_ in runs:
        stored = _rows(f"select count(*) from entities where extraction_run_id = '{run_id}'")
        assert stored == [(count,)] and count > 0

    second = _graph_labels(tmp_path)
    assert "$87,000" in second and "$1,250,000" not in second
    assert len(second) == runs[1][3]

    listing = _ok(
        runner.invoke(app, ["analyze", "runs", doc_id[:8], "--csv", "-"], env={"COLUMNS": "200"})
    )
    rows = listing.strip().splitlines()
    assert rows[0] == "Current,Run,Backend,Model,Started (UTC),Entities"
    assert [r.split(",")[0] for r in rows[1:]] == ["", "yes"]
    assert rows[1].split(",")[1] == runs[0][0]
    assert rows[2].split(",")[5] == str(runs[1][3])


def test_prefer_run_switches_what_every_reader_sees(tmp_path):
    doc_id = _document()
    old = _store(doc_id, "Ana Alpha", "Bo Alpha")
    _store(doc_id, "Cy Beta", "Di Beta")
    assert _graph_labels(tmp_path) == {"Cy Beta", "Di Beta"}

    out = _ok(runner.invoke(app, ["analyze", "extract", doc_id, "--prefer-run", old[:8]]))
    assert old[:8] in out
    assert _graph_labels(tmp_path) == {"Ana Alpha", "Bo Alpha"}
    assert _rows("select id from extraction_runs where is_current = 1") == [(old,)]

    bad = runner.invoke(app, ["analyze", "extract", doc_id, "--prefer-run", "nope"])
    assert bad.exit_code == 1
    assert _rows("select id from extraction_runs where is_current = 1") == [(old,)]


def test_legacy_entities_become_a_run_on_reextract(tmp_path):
    doc_id = _document()
    _legacy_entities(doc_id, "Ana Legacy", "Bo Legacy")
    assert _graph_labels(tmp_path) == {"Ana Legacy", "Bo Legacy"}

    _store(doc_id, "Cy New", "Di New")
    assert _graph_labels(tmp_path) == {"Cy New", "Di New"}
    runs = _rows("select backend, entity_count, is_current, started_at from extraction_runs")
    assert sorted(runs) == [("regex", 2, 1, runs[0][3] or runs[1][3]), ("unknown", 2, 0, None)]
    assert _rows("select count(*) from entities where extraction_run_id is null") == [(0,)]

    listing = _ok(runner.invoke(app, ["analyze", "runs", doc_id, "--csv", "-"]))
    assert "unknown" in listing.splitlines()[1]


# --- every reader uses the current run only --------------------------------------


def test_every_entity_reader_counts_a_reextracted_document_once(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from openfoia.agent import OpenFOIAAgent
    from openfoia.db import get_session
    from openfoia.models import Request
    from openfoia.server import create_app

    doc_id = _document()
    _store(doc_id, "Ana Stale", "Bo Stale")
    _store(doc_id, "Olu Sample", "Cy Current")

    # graph
    assert _graph_labels(tmp_path) == {"Olu Sample", "Cy Current"}

    # FtM export
    export = tmp_path / "out.ftm.json"
    _ok(runner.invoke(app, ["analyze", "export", "-o", str(export)]))
    exported = [json.loads(line)["properties"] for line in export.read_text().splitlines()]
    names = {p["name"][0] for p in exported if "name" in p}  # relationship lines have no name
    assert names == {"Olu Sample", "Cy Current"}

    # crossref (offline OpenSanctions fixture: nothing leaves the machine)
    import openfoia.crossref as crossref_mod

    seen: list[list[str]] = []
    real = crossref_mod.crossref_entities

    async def spy(entities, *args, **kwargs):
        seen.append(sorted(e.normalized_text for e in entities))
        return await real(entities, *args, **kwargs)

    monkeypatch.setattr(crossref_mod, "crossref_entities", spy)
    fixture = tmp_path.parent / "osfix"
    fixture.mkdir()
    from pathlib import Path

    src = Path(__file__).parent / "fixtures" / "opensanctions" / "targets.simple.csv"
    (fixture / "targets.simple.csv").write_text(src.read_text())
    _ok(
        runner.invoke(
            app,
            ["crossref", "--sources", "opensanctions-local", "--opensanctions-data", str(fixture)],
        )
    )
    assert seen == [["Cy Current", "Olu Sample"]]

    # case summary
    _ok(runner.invoke(app, ["case", "new", "Runs"]))
    with get_session() as s:
        s.query(Request).update({Request.case_id: _rows("select id from cases")[0][0]})
    assert "Entities:       2" in _ok(runner.invoke(app, ["case", "show", "Runs"]))

    # web UI API
    client = TestClient(create_app(token="t", data_dir=tmp_path), base_url="http://127.0.0.1")
    auth = {"Authorization": "Bearer t"}  # the token is never accepted in the query string
    assert client.get("/api/stats", headers=auth).json()["entities"] == {
        "total": 2,
        "people": 2,
        "organizations": 0,
    }
    listed = client.get("/api/entities", headers=auth).json()
    assert {e["normalized_text"] for e in listed["entities"]} == {"Olu Sample", "Cy Current"}
    graph = client.get("/api/graph", headers=auth).json()
    assert {n["label"] for n in graph["nodes"]} == {"Olu Sample", "Cy Current"}
    assert len(graph["edges"]) == 1

    # agent tools
    with get_session() as s:
        agent = OpenFOIAAgent(db_session=s, config={})
        found = asyncio.run(agent._search_entities({"query": ""}))
        assert {r["name"] for r in found["results"]} == {"Olu Sample", "Cy Current"}
        assert asyncio.run(agent._build_entity_graph({})) == {"entities": 2, "relationships": 1}
        req_id = s.query(Request).one().id
        report = asyncio.run(agent._generate_report({"request_ids": [req_id]}))
        assert "Entities Found: 2" in report["report_content"]
        assert "Stale" not in report["report_content"]


def test_ftm_import_dedups_against_current_entities_only(tmp_path):
    from openfoia.ftm_import import import_ftm_to_db

    doc_id = _document()
    _store(doc_id, "Ana Stale", "Bo Stale")
    _store(doc_id, "Cy Current", "Di Current")

    ftm = tmp_path / "in.ftm.json"
    lines = [
        {"id": "f1", "schema": "Person", "properties": {"name": ["Ana Stale"]}},
        {"id": "f2", "schema": "Person", "properties": {"name": ["Cy Current"]}},
    ]
    ftm.write_text("\n".join(json.dumps(x) for x in lines))
    added, _ = import_ftm_to_db(ftm)
    # "Cy Current" is already in use; "Ana Stale" only exists in a superseded run.
    assert added == 1
    assert "Ana Stale" in _graph_labels(tmp_path)


# --- run metadata -----------------------------------------------------------------


def test_run_metadata_names_backend_model_and_prompt_hash():
    from openfoia.extraction_runs import describe_extraction
    from openfoia.pipeline import extract as ex

    extractor = SimpleNamespace(model="llama3.2:3b", provider="ollama")
    regex = describe_extraction(extractor, {"backend": "regex", "backends": ["regex"]}, False)
    assert regex["backend"] == "regex" and regex["model"] is None
    assert regex["prompt_hash"] is None

    ner = describe_extraction(extractor, {"backends": ["gliner", "regex"]}, False)
    assert ner == {
        "backend": "gliner",
        "model": ex.GLINER_MODEL_ID,
        "prompt_hash": None,
        "notes": "backends: gliner+regex",
    }

    validated = describe_extraction(
        extractor,
        {
            "backend": "gliner+regex+llm-validator",
            "backends": ["gliner", "regex", "llm-validator"],
            "llm_validated": True,
        },
        False,
    )
    assert validated["backend"] == "llm" and validated["model"] == "llama3.2:3b"
    expected = hashlib.sha256(ex.LLM_VALIDATION_PROMPT.encode()).hexdigest()
    assert validated["prompt_hash"] == expected
    assert "ollama" in validated["notes"] and ex.GLINER_MODEL_ID in validated["notes"]

    both = describe_extraction(extractor, {"backends": ["gliner", "regex"]}, True)
    assert both["backend"] == "ensemble" and ex.GLINER_MODEL_ID in both["model"]


def test_validation_prompt_constant_is_what_the_validator_sends(monkeypatch):
    from openfoia.pipeline import extract as ex

    sent: list[str] = []

    def fake_ollama(prompt, *args):
        sent.append(prompt)
        return '{"keep": [], "remove": []}'

    monkeypatch.setattr(ex, "_call_ollama", fake_ollama)
    extractor = ex.EntityExtractor(provider="ollama", model="m")
    ents = [ex.ExtractedEntity(EntityType.PERSON, "Ana", "Ana", 0.9, "")]
    asyncio.run(extractor._llm_validate_entities(ents, "Ana"))
    assert sent == [
        ex.LLM_VALIDATION_PROMPT.format(entity_list='- person: "Ana" (confidence: 90%)')
    ]


# --- deletion ---------------------------------------------------------------------


def test_prune_deletes_earlier_runs_their_entities_and_links(tmp_path):
    doc_id = _document()
    _legacy_entities(doc_id, "Ana Legacy", "Bo Legacy")
    _store(doc_id, "Cy Old", "Di Old")
    keep = _store(doc_id, "Eve Kept", "Fay Kept")
    other_doc = _document(number="REQ-RUNS-2")
    other = _store(other_doc, "Gus Other", "Hal Other")

    out = _ok(runner.invoke(app, ["analyze", "runs", doc_id, "--prune", "--yes"]))
    assert "Deleted 2 run(s) and 4 entities" in out

    assert sorted(_rows("select id from extraction_runs")) == sorted([(keep,), (other,)])
    assert _rows(
        f"select normalized_text from entities where document_id = '{doc_id}' order by 1"
    ) == [("Eve Kept",), ("Fay Kept",)]
    # No entity without a live run, no link to a deleted entity.
    assert _rows(
        "select count(*) from entities where extraction_run_id is not null "
        "and extraction_run_id not in (select id from extraction_runs)"
    ) == [(0,)]
    assert _rows(
        "select count(*) from entity_links where source_id not in (select id from entities) "
        "or target_id not in (select id from entities)"
    ) == [(0,)]
    assert _rows("select count(*) from entity_links") == [(2,)]
    assert _graph_labels(tmp_path) == {"Eve Kept", "Fay Kept", "Gus Other", "Hal Other"}


def test_prune_deletes_runless_entities_hidden_by_the_current_run(tmp_path):
    from openfoia.ftm_import import import_ftm_to_db

    doc_id = _document()
    _store(doc_id, "Eve Kept", "Fay Kept")
    ftm = tmp_path / "in.ftm.json"
    ftm.write_text(json.dumps({"id": "f1", "schema": "Person", "properties": {"name": ["Ivo"]}}))
    assert import_ftm_to_db(ftm, document_id=doc_id) == (1, 0)
    assert _rows("select count(*) from entities where extraction_run_id is null") == [(1,)]

    _ok(runner.invoke(app, ["analyze", "runs", doc_id, "--prune", "--yes"]))
    assert _rows("select count(*) from entities where extraction_run_id is null") == [(0,)]
    assert _rows("select count(*) from entities") == [(2,)]


def test_prune_leaves_no_copy_of_pruned_names_in_the_database_file():
    from openfoia.db import get_db_path

    doc_id = _document()
    _store(doc_id, "Zebulon Quarrystone", "Wilhelmina Oddfellow")
    _store(doc_id, "Eve Kept", "Fay Kept")
    assert b"Zebulon Quarrystone" in get_db_path().read_bytes()

    _ok(runner.invoke(app, ["analyze", "runs", doc_id, "--prune", "--yes"]))
    raw = get_db_path().read_bytes()
    assert b"Zebulon Quarrystone" not in raw
    assert b"Wilhelmina Oddfellow" not in raw
    assert b"Eve Kept" in raw


def test_prune_asks_first():
    doc_id = _document()
    _store(doc_id, "Cy Old", "Di Old")
    _store(doc_id, "Eve Kept", "Fay Kept")
    result = runner.invoke(app, ["analyze", "runs", doc_id, "--prune"], input="n\n")
    assert result.exit_code == 1
    assert _rows("select count(*) from extraction_runs") == [(2,)]


@pytest.mark.parametrize("secure", [False, True])
def test_purge_leaves_no_runs_or_entities(data_dir, monkeypatch, secure):
    """Purge removes the whole data directory, so no run or entity row can survive it."""
    import openfoia.security as security
    from openfoia.db import get_db_path

    monkeypatch.setattr(security, "clear_shell_history", list)  # never touch the real history
    doc_id = _document()
    _store(doc_id, "Cy Old", "Di Old")
    _store(doc_id, "Eve Kept", "Fay Kept")
    db_path = get_db_path()
    assert _rows("select count(*) from extraction_runs") == [(2,)]
    assert _rows("select count(*) from entities") == [(4,)]

    args = ["purge", "--yes"] + (["--secure"] if secure else [])
    _ok(runner.invoke(app, args))

    assert not db_path.exists()
    assert not data_dir.exists()


def test_ftm_export_writes_the_relationships_between_exported_entities(tmp_path):
    """`analyze export` passed link ends as database ids, but the FtM writer matches by name."""
    import json

    from typer.testing import CliRunner

    from openfoia.cli import app

    doc = _document()
    _store(doc, "Jane Roe", "Acme Port Authority")
    out = tmp_path / "case.ftm.json"

    _ok(CliRunner().invoke(app, ["analyze", "export", "-o", str(out)]))

    lines = [json.loads(line) for line in out.read_text().splitlines()]
    people = {
        line["properties"]["name"][0]: line["id"] for line in lines if "name" in line["properties"]
    }
    links = [line for line in lines if "name" not in line["properties"]]
    assert len(links) == 1
    ends = {v[0] for k, v in links[0]["properties"].items() if k != "summary"}
    assert ends == {people["Jane Roe"], people["Acme Port Authority"]}
