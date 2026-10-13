"""Regression tests for public-records CLI output and documentation."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.records import list_sources
from openfoia.records.base import RecordEntity, SearchResult


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Searches log to the active case, so never touch the real data dir."""
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))


def test_records_search_raw_is_parseable_json_without_terminal_output(monkeypatch):
    """Raw mode must be pipe-safe even when upstream data has control characters."""

    class Adapter:
        async def search(self, query, **kwargs):
            return SearchResult(
                source="sec",
                query=query,
                total_results=1,
                entities=[
                    RecordEntity(
                        entity_type="ORGANIZATION",
                        name="Uranium\x1fEnergy",
                        source="sec",
                        extra_data={"snippet": "filing\x00text"},
                    )
                ],
            )

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "Uranium Energy", "--source", "sec", "--raw"],
    )

    assert result.exit_code == 0
    assert "\x1f" not in result.stdout
    assert "\x00" not in result.stdout
    assert json.loads(result.stdout) == [
        {
            "entity_type": "ORGANIZATION",
            "name": "Uranium\x1fEnergy",
            "source": "sec",
            "source_url": None,
            "jurisdiction": None,
            "status": None,
            "identifiers": {},
            "extra_data": {"snippet": "filing\x00text"},
        }
    ]


def test_records_search_help_lists_every_registered_source():
    """CLI help must not hide supported public-records sources."""

    result = CliRunner().invoke(app, ["records", "search", "--help"])

    assert result.exit_code == 0
    for source in list_sources():
        assert source in result.output


def test_records_search_raw_reports_source_errors_instead_of_empty_json(monkeypatch):
    """A failed search must not look like a successful empty result to a script."""

    class Adapter:
        async def search(self, query, **kwargs):
            return SearchResult(
                source="sec",
                query=query,
                total_results=0,
                entities=[],
                error="AdapterRequestError: HTTP 503",
            )

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "Acme Corp", "--source", "sec", "--raw"],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == ""


def test_records_search_raw_round_trips_non_ascii_names(monkeypatch):
    """Names outside ASCII must survive --raw intact, not arrive mangled."""

    class Adapter:
        async def search(self, query, **kwargs):
            return SearchResult(
                source="opencorporates",
                query=query,
                total_results=1,
                entities=[
                    RecordEntity(
                        entity_type="ORGANIZATION",
                        name="Şirket Holding A.Ş.",
                        source="opencorporates",
                    )
                ],
            )

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "Sirket", "--source", "opencorporates", "--raw"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["name"] == "Şirket Holding A.Ş."


def test_records_search_raw_keeps_crashes_off_stdout(monkeypatch):
    """An adapter crash must not put prose where a script expects JSON."""

    class Adapter:
        async def search(self, query, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "Acme Corp", "--source", "sec", "--raw"],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == ""


def _rank_adapter(monkeypatch):
    class Adapter:
        async def search(self, query, **kwargs):
            def ent(name, snippet, ident):
                return RecordEntity(
                    entity_type="DOCUMENT",
                    name=name,
                    source="sec",
                    identifiers={"id": ident},
                    extra_data={"snippet": snippet},
                )

            return SearchResult(
                source="sec",
                query=query,
                total_results=3,
                entities=[
                    ent("Budget memo", "nothing relevant", "1"),
                    ent("School gifted program", "gifted child testing in school", "2"),
                    ent("Child welfare", "one child", "3"),
                ],
            )

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())


def test_records_search_rank_scores_and_sorts(monkeypatch):
    _rank_adapter(monkeypatch)
    result = CliRunner().invoke(
        app, ["records", "search", "x", "-s", "sec", "--json", "--rank", "child,school,gifted"]
    )
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(r["id"], r["score"]) for r in rows] == [("2", 5), ("3", 2), ("1", 0)]

    result = CliRunner().invoke(app, ["records", "search", "x", "-s", "sec", "--rank", "child"])
    assert result.exit_code == 0, result.output
    assert result.stdout.index("Child welfare") < result.stdout.index("Budget memo")
    assert "Score" in result.stdout


def _fake_pull_text(monkeypatch, failing=()):
    from openfoia.records.documentcloud import DocumentCloudAdapter

    calls = []

    async def pull_text(self, identifier, case_id=None):
        calls.append(identifier)
        if identifier in failing:
            return None, None
        from uuid import uuid4

        from openfoia.db import ensure_db, get_session
        from openfoia.models import Agency, DeliveryMethod, Document, DocumentType, Request, User

        ensure_db()
        with get_session() as s:
            existing = s.query(Request).filter_by(request_number=f"DC-{identifier}").first()
            if existing:
                return s.query(Document).filter_by(request_id=existing.id).first().id, "text"
            user = s.query(User).first() or User(id=str(uuid4()), email="a@b.c", name="L")
            s.add(user)
            s.flush()
            req = Request(
                id=str(uuid4()),
                request_number=f"DC-{identifier}",
                requester_id=user.id,
                agency_id=s.query(Agency).first().id,
                subject=f"doc {identifier}",
                body="",
                delivery_method=DeliveryMethod.EMAIL,
                case_id=case_id,
            )
            s.add(req)
            s.flush()
            doc = Document(
                id=str(uuid4()),
                request_id=req.id,
                doc_type=DocumentType.FULL_RESPONSE,
                filename="x",
                file_path="",
                file_size=1,
                mime_type="text/plain",
            )
            s.add(doc)
            return doc.id, "text"

    monkeypatch.setattr(DocumentCloudAdapter, "pull_text", pull_text)
    return calls


def test_records_fetch_batch_from_results_file(monkeypatch, tmp_path):
    calls = _fake_pull_text(monkeypatch, failing={"3"})
    hits = tmp_path / "hits.json"
    hits.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"id": "1", "source": "documentcloud"},
                {"id": "2", "source": "documentcloud"},
                {"id": "9", "source": "muckrock"},
            ]
        )
    )
    runner = CliRunner()
    result = runner.invoke(app, ["records", "fetch", "3", "--from", str(hits)])
    assert result.exit_code == 2, result.output
    assert calls == ["3", "1", "2"]
    assert "Fetched 2, skipped 0 already stored, failed 1" in result.output
    assert "Ignored 1 result(s)" in result.output

    # Already stored ids are skipped without a network call unless --refresh.
    calls.clear()
    raw = tmp_path / "raw.json"
    raw.write_text(
        json.dumps([{"identifiers": {"documentcloud_id": "1"}, "source": "documentcloud"}])
    )
    result = runner.invoke(app, ["records", "fetch", "2", "--from", str(raw)])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "Fetched 0, skipped 2 already stored, failed 0" in result.output

    result = runner.invoke(app, ["records", "fetch", "2", "--refresh"])
    assert result.exit_code == 0, result.output
    assert calls == ["2"]

    assert runner.invoke(app, ["records", "fetch", "3"]).exit_code == 1
    assert runner.invoke(app, ["records", "fetch"]).exit_code == 2

    # A plain one-id-per-line file works; garbage exits 2 with a message.
    calls.clear()
    plain = tmp_path / "ids.txt"
    plain.write_text("7\n8\n")
    assert runner.invoke(app, ["records", "fetch", "--from", str(plain)]).exit_code == 0
    assert calls == ["7", "8"]
    bad = tmp_path / "bad.json"
    bad.write_text("[[1], 2]")
    result = runner.invoke(app, ["records", "fetch", "--from", str(bad)])
    assert result.exit_code == 2 and "expected" in result.output
    assert runner.invoke(app, ["records", "fetch", "--from", str(tmp_path / "no")]).exit_code == 2


def test_records_fetch_reports_document_held_by_another_case(monkeypatch):
    _fake_pull_text(monkeypatch)
    runner = CliRunner()
    assert runner.invoke(app, ["case", "new", "B"]).exit_code == 0
    assert runner.invoke(app, ["records", "fetch", "1"]).exit_code == 0
    assert runner.invoke(app, ["case", "new", "A"]).exit_code == 0
    result = runner.invoke(app, ["records", "fetch", "1"])
    assert result.exit_code == 0, result.output
    assert "stored in case B" in result.output
    assert "Attached to the active case" not in result.output
