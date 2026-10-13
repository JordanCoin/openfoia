"""Case container: model, CLI, attaching work to the active case, graph scoping."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.records.base import RecordEntity, SearchResult

runner = CliRunner()


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    return tmp_path / "data"


def _ok(result):
    assert result.exit_code == 0, result.output
    return result.output


def _fake_dc_fetch(monkeypatch):
    from openfoia.records.documentcloud import DocumentCloudAdapter

    async def fetch(self, identifier, **kwargs):
        return RecordEntity(
            entity_type="DOCUMENT",
            name=f"PAS memo {identifier}",
            source="documentcloud",
            source_url=f"https://www.documentcloud.org/documents/{identifier}-pas/",
            identifiers={"documentcloud_id": identifier},
            extra_data={"pages": 12, "full_text": "Langley memo text"},
        )

    monkeypatch.setattr(DocumentCloudAdapter, "fetch", fetch)


def _fake_search(monkeypatch, total=3):
    class Adapter:
        async def search(self, query, **kwargs):
            return SearchResult(source="sec", query=query, total_results=total, entities=[])

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())


def test_case_lifecycle_attaches_fetches_and_queries(monkeypatch, data_dir):
    _ok(runner.invoke(app, ["case", "new", "CIA PAS", "--desc", "PAS children"]))

    # The pointer file lives outside the encrypted DB: it must not name the case.
    pointer = (data_dir / "active_case.json").read_text()
    assert "PAS" not in pointer
    assert set(json.loads(pointer)) == {"id"}

    assert "* CIA PAS" in _ok(runner.invoke(app, ["case", "list"]))

    _fake_dc_fetch(monkeypatch)
    _ok(runner.invoke(app, ["records", "fetch", "2090186"]))
    _fake_search(monkeypatch)
    _ok(runner.invoke(app, ["records", "search", "PAS", "-s", "sec"]))

    show = _ok(runner.invoke(app, ["case", "show"]))
    assert "Documents:      1" in show
    assert "Queries logged: 1" in show

    exported = json.loads(_ok(runner.invoke(app, ["case", "export", "--format", "json"])))
    assert exported["name"] == "CIA PAS"
    doc = exported["documents"][0]
    assert doc["documentcloud_id"] == "2090186"
    assert doc["title"] == "PAS memo 2090186"
    assert doc["pages"] == 12
    assert doc["source_url"] == "https://www.documentcloud.org/documents/2090186-pas/"
    assert exported["queries"][0]["source"] == "sec"
    assert exported["queries"][0]["total_results"] == 3

    md = _ok(runner.invoke(app, ["case", "export"]))
    assert "| PAS memo 2090186 |" in md
    assert "`sec` 'PAS' (3 results)" in md


def test_no_active_case_logs_nothing_and_fetch_stays_unattached(monkeypatch):
    from openfoia.db import get_session
    from openfoia.models import CaseQuery, Request

    _ok(runner.invoke(app, ["case", "new", "Other"]))
    _ok(runner.invoke(app, ["case", "close"]))

    _fake_dc_fetch(monkeypatch)
    _ok(runner.invoke(app, ["records", "fetch", "1"]))
    _fake_search(monkeypatch)
    _ok(runner.invoke(app, ["records", "search", "PAS", "-s", "sec"]))

    with get_session() as session:
        assert session.query(CaseQuery).count() == 0
        assert session.query(Request).filter(Request.request_number == "DC-1").one().case_id is None


def test_failed_search_is_logged_without_a_result_count(monkeypatch):
    from openfoia.db import get_session
    from openfoia.models import CaseQuery

    _ok(runner.invoke(app, ["case", "new", "PAS"]))

    class Adapter:
        async def search(self, query, **kwargs):
            return SearchResult(source="sec", query=query, total_results=0, entities=[], error="x")

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())
    assert runner.invoke(app, ["records", "search", "PAS", "-s", "sec"]).exit_code == 1

    with get_session() as session:
        assert session.query(CaseQuery).one().total_results is None


def test_case_use_by_slug_and_unknown_case(monkeypatch):
    _ok(runner.invoke(app, ["case", "new", "First Case"]))
    _ok(runner.invoke(app, ["case", "new", "Second"]))
    assert "First Case" in _ok(runner.invoke(app, ["case", "use", "first-case"]))
    assert "* First Case" in _ok(runner.invoke(app, ["case", "list"]))
    assert runner.invoke(app, ["case", "use", "nope"]).exit_code == 1
    assert runner.invoke(app, ["case", "new", "first case"]).exit_code == 1


def _seed_doc_with_entity(case_id, label):
    from openfoia.db import get_session
    from openfoia.models import (
        Agency,
        DeliveryMethod,
        Document,
        DocumentType,
        Entity,
        EntityType,
        Request,
        User,
    )

    with get_session() as session:
        user = session.query(User).first() or User(email="l@x", name="L")
        session.add(user)
        session.flush()
        req = Request(
            request_number=f"T-{uuid4().hex[:8]}",
            requester_id=user.id,
            agency_id=session.query(Agency).first().id,
            subject=label,
            body="",
            delivery_method=DeliveryMethod.EMAIL,
            case_id=case_id,
        )
        session.add(req)
        session.flush()
        doc = Document(
            request_id=req.id,
            doc_type=DocumentType.FULL_RESPONSE,
            filename=f"{label}.txt",
            file_path="",
            file_size=1,
            mime_type="text/plain",
        )
        session.add(doc)
        session.flush()
        session.add(
            Entity(
                document_id=doc.id,
                entity_type=EntityType.PERSON,
                raw_text=label,
                normalized_text=label,
            )
        )


def test_graph_scopes_to_active_case_unless_all(tmp_path):
    from openfoia.cases import get_active_case_id

    _ok(runner.invoke(app, ["case", "new", "PAS"]))
    _seed_doc_with_entity(get_active_case_id(), "in-case")
    _seed_doc_with_entity(None, "elsewhere")
    out = tmp_path / "g.json"

    def labels(*args):
        output = _ok(runner.invoke(app, ["analyze", "graph", "-o", str(out), *args]))
        return output, {n["label"] for n in json.loads(out.read_text())["nodes"]}

    output, got = labels()
    assert "Scope: case 'PAS'" in output
    assert got == {"in-case"}

    output, got = labels("--all")
    assert "Scope: all documents" in output
    assert got == {"in-case", "elsewhere"}

    _ok(runner.invoke(app, ["case", "close"]))
    _, got = labels("--case", "pas")
    assert got == {"in-case"}
    _, got = labels()
    assert got == {"in-case", "elsewhere"}
