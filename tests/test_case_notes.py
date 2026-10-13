"""Case notes: findings pinned to pages, grep --save, citation export, graph --mentions."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from openfoia import doctext
from openfoia.cli import app

runner = CliRunner()

CREST = "CIA-RDP96-00789R002200230001-2"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    return tmp_path / "data"


def _ok(result):
    assert result.exit_code == 0, result.output
    return result.output


def _add_doc(pages, title="SRI PAS review", page_count=None, case_id=None, dc_id=None, labels=()):
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

    dc_id = dc_id or uuid4().hex[:7]
    with get_session() as s:
        user = s.query(User).first() or User(id=str(uuid4()), email="a@b.c", name="L")
        s.add(user)
        s.flush()
        req = Request(
            id=str(uuid4()),
            request_number=f"DC-{dc_id}",
            requester_id=user.id,
            agency_id=s.query(Agency).first().id,
            subject=title,
            body=f"Pulled from DocumentCloud: https://www.documentcloud.org/documents/{dc_id}-x/",
            delivery_method=DeliveryMethod.EMAIL,
            case_id=case_id,
        )
        s.add(req)
        s.flush()
        doc = Document(
            id=str(uuid4()),
            request_id=req.id,
            doc_type=DocumentType.FULL_RESPONSE,
            filename=f"documentcloud-{dc_id}.txt",
            file_path="",
            file_size=0,
            mime_type="text/plain",
            page_count=page_count,
            extracted_text=doctext.join_pages(pages),
        )
        s.add(doc)
        doctext.replace_pages(s, doc.id, pages)
        for label, page in labels:
            s.add(
                Entity(
                    document_id=doc.id,
                    entity_type=EntityType.PERSON,
                    raw_text=label,
                    normalized_text=label,
                    page_number=page,
                )
            )
        return doc.id


def test_note_lifecycle_and_citation_export():
    from openfoia.cases import get_active_case_id

    _ok(runner.invoke(app, ["case", "new", "PAS"]))
    case_id = get_active_case_id()
    doc_id = _add_doc(
        ["cover", f"Approved for release {CREST}", "Hocking Technical College students"],
        page_count=3,
        case_id=case_id,
        dc_id="23058027",
    )

    out = _ok(
        runner.invoke(
            app,
            [
                "case",
                "note",
                "PAS was tested on college students",
                "--doc",
                doc_id[:8],
                "--page",
                "3",
                "--quote",
                "Hocking Technical College students",
                "--tier",
                "says",
            ],
        )
    )
    assert "Note 1 added" in out
    _ok(runner.invoke(app, ["case", "note", "Maybe a school program", "--tier", "speculation"]))
    assert runner.invoke(app, ["case", "note", "x", "--tier", "guess"]).exit_code == 2

    notes = json.loads(_ok(runner.invoke(app, ["case", "notes", "--json"])))
    assert [n["id"] for n in notes] == [1, 2]
    assert notes[0]["crest"] == CREST
    assert notes[0]["identifier"] == "23058027"
    assert notes[0]["page_is_real"] is True
    assert notes[1]["document_id"] is None

    md = _ok(runner.invoke(app, ["case", "export", "--format", "md"]))
    assert "## Findings" in md
    assert "1. **says** PAS was tested on college students" in md
    assert "   > Hocking Technical College students" in md
    assert f"   Source: SRI PAS review, DocumentCloud 23058027, {CREST}, p.3, retrieved" in md
    assert "2. **speculation** Maybe a school program" in md
    assert "## Sources" in md
    assert "https://www.documentcloud.org/documents/23058027-x/" in md
    assert "Notes:          2" in _ok(runner.invoke(app, ["case", "show"]))

    exported = json.loads(_ok(runner.invoke(app, ["case", "export", "--format", "json"])))
    assert exported["findings"][0]["tier"] == "says"
    assert exported["sources"][0]["crest"] == CREST

    assert "Removed 1 note(s)" in _ok(runner.invoke(app, ["case", "note", "rm", "2"]))
    assert len(json.loads(_ok(runner.invoke(app, ["case", "notes", "--json"])))) == 1
    assert runner.invoke(app, ["case", "note", "rm"]).exit_code == 2


def test_grep_save_attaches_hits_and_flags_partial_pages():
    from openfoia.cases import get_active_case_id

    _ok(runner.invoke(app, ["case", "new", "PAS"]))
    # One stored page for a 40-page source: the page number is not the source's.
    _add_doc(["the WISC was given to children"], page_count=40, case_id=get_active_case_id())

    assert runner.invoke(app, ["docs", "grep", "WISC", "--save"]).exit_code == 0
    _ok(runner.invoke(app, ["case", "close"]))
    assert runner.invoke(app, ["docs", "grep", "WISC", "--save"]).exit_code == 1
    _ok(runner.invoke(app, ["case", "use", "pas"]))
    # Same passage again is a duplicate; a narrower context is a new passage.
    r = runner.invoke(app, ["docs", "grep", "children", "--save"])
    assert "1 already saved" in r.output
    _ok(runner.invoke(app, ["docs", "grep", "children", "-C", "4", "--note", "kids mentioned"]))

    notes = json.loads(_ok(runner.invoke(app, ["case", "notes", "--json"])))
    assert len(notes) == 2
    assert notes[0]["quote"] == "the WISC was given to children"
    assert notes[0]["note"] is None and notes[0]["tier"] == "says"
    assert notes[1]["note"] == "kids mentioned"
    assert notes[0]["page"] == 1 and notes[0]["page_is_real"] is False

    md = _ok(runner.invoke(app, ["case", "export"]))
    assert "p.1*" in md
    assert "fewer pages are stored than the source has" in md


def test_graph_mentions_lists_shared_documents_and_path(tmp_path):
    from openfoia.cases import get_active_case_id
    from openfoia.db import get_data_dir

    _ok(runner.invoke(app, ["case", "new", "PAS"]))
    case_id = get_active_case_id()
    both = _add_doc(
        ["Gittinger met Zimbardo", "Zimbardo again"],
        title="Both",
        case_id=case_id,
        labels=[("John Gittinger", 1), ("Philip Zimbardo", None)],
    )
    _add_doc(["only Gittinger"], title="One", case_id=case_id, labels=[("John Gittinger", 1)])

    out = _ok(runner.invoke(app, ["analyze", "graph", "--mentions", "Gittinger", "Zimbardo"]))
    assert f"{both[:8]}  Both  Gittinger p.1; Zimbardo p.1,2" in out
    assert "One" not in out
    assert "Pass --name" in out

    graph = {
        "nodes": [
            {"id": "a", "label": "John Gittinger"},
            {"id": "b", "label": "SRI"},
            {"id": "c", "label": "Philip Zimbardo"},
        ],
        "edges": [{"source": "a", "target": "b"}, {"source": "c", "target": "b"}],
        "documents": {},
    }
    (get_data_dir() / "graphs").mkdir(parents=True)
    (get_data_dir() / "graphs" / "pas.json").write_text(json.dumps(graph))
    out = _ok(
        runner.invoke(
            app, ["analyze", "graph", "--mentions", "Gittinger", "Zimbardo", "--name", "pas"]
        )
    )
    assert "Shortest path in 'pas': John Gittinger -> SRI -> Philip Zimbardo" in out
    assert not (tmp_path / "graph.json").exists()

    out = _ok(runner.invoke(app, ["analyze", "graph", "--mentions", "Gittinger", "Nobody"]))
    assert "No document in scope mentions both" in out


def test_grep_and_export_never_leak_another_case(tmp_path):
    from openfoia.cases import get_active_case_id

    _ok(runner.invoke(app, ["case", "new", "B"]))
    b_doc = _add_doc(
        [f"SECRET-B passage {CREST}"],
        title="B-TITLE",
        case_id=get_active_case_id(),
        dc_id="b900leak",
    )
    _ok(runner.invoke(app, ["case", "new", "A"]))
    a_id = get_active_case_id()
    _add_doc(["SECRET-A passage"], title="A-TITLE", case_id=a_id, dc_id="a901")

    out = _ok(runner.invoke(app, ["docs", "grep", "passage"]))
    assert "A-TITLE" in out and "B-TITLE" not in out
    out = _ok(runner.invoke(app, ["docs", "grep", "passage", "--all-docs"]))
    assert "B-TITLE" in out

    # --save with --all-docs sees B's hit but refuses to store it; --save twice dedupes.
    r = runner.invoke(app, ["docs", "grep", "passage", "--all-docs", "--save"])
    assert r.exit_code == 0, r.output
    assert "Saved 1 note(s)" in r.output and "Skipped 1 hit(s)" in r.output
    r = runner.invoke(app, ["docs", "grep", "passage", "--save"])
    assert "Saved 0 note(s) to the active case. 1 already saved." in r.output
    assert runner.invoke(app, ["case", "note", "x", "--doc", b_doc[:8]]).exit_code == 1

    for args in (["case", "export"], ["case", "export", "--format", "json"], ["case", "notes"]):
        out = _ok(runner.invoke(app, args))
        # Markers carry letters: a bare "900" can appear in a timestamp or uuid.
        for leak in ("B-TITLE", "SECRET-B", "b900leak", CREST):
            assert leak not in out, (args, leak)
    assert "SECRET-A" in _ok(runner.invoke(app, ["case", "export"]))


def test_export_keeps_multiline_quotes_on_one_line():
    _ok(runner.invoke(app, ["case", "new", "A"]))
    _ok(runner.invoke(app, ["case", "note", "line one\nline two", "--quote", "q1\n\nq2"]))
    md = _ok(runner.invoke(app, ["case", "export"]))
    assert "1. line one line two" in md
    assert "   > q1 q2" in md


def test_mentions_match_whole_words_only():
    from openfoia.cases import get_active_case_id

    _ok(runner.invoke(app, ["case", "new", "A"]))
    _add_doc(["x"], title="Both", case_id=get_active_case_id(), labels=[("Art", 1), ("Bo", 1)])
    _add_doc(["x"], title="Sub", case_id=get_active_case_id(), labels=[("Martin", 1), ("Bo", 1)])
    out = _ok(runner.invoke(app, ["analyze", "graph", "--mentions", "Art", "Bo"]))
    assert "Both" in out and "Sub" not in out


def test_mentions_fall_back_to_page_text_when_entities_miss_a_label():
    """A name the extractor never tagged still counts: the stored pages are the record."""
    from openfoia.cases import get_active_case_id

    _ok(runner.invoke(app, ["case", "new", "PAS"]))
    case_id = get_active_case_id()
    doc = _add_doc(
        ["Gittinger wrote to Hocking.", "Hocking replied."],
        title="Letters",
        case_id=case_id,
        labels=[("John Gittinger", 1)],
    )
    _add_doc(["Gittinger alone"], title="Solo", case_id=case_id, labels=[("John Gittinger", 1)])

    out = _ok(runner.invoke(app, ["analyze", "graph", "--mentions", "Gittinger", "Hocking"]))
    assert f"{doc[:8]}  Letters  Gittinger p.1; Hocking p.1,2" in out
    assert "Solo" not in out


def test_grep_save_records_the_says_tier():
    from openfoia.cases import get_active_case_id

    _ok(runner.invoke(app, ["case", "new", "PAS"]))
    _add_doc(["The WAIS was used."], case_id=get_active_case_id())
    _ok(runner.invoke(app, ["docs", "grep", "WAIS", "--save"]))
    out = _ok(runner.invoke(app, ["case", "export", "-f", "json"]))
    assert json.loads(out)["findings"][0]["tier"] == "says"


def test_note_edit_changes_only_what_is_passed():
    _ok(runner.invoke(app, ["case", "new", "PAS"]))
    _ok(runner.invoke(app, ["case", "note", "first draft", "--tier", "speculation"]))
    _ok(runner.invoke(app, ["case", "note", "edit", "1", "--tier", "infer", "--page", "4"]))
    _ok(runner.invoke(app, ["case", "note", "edit", "1", "second", "draft"]))
    notes = json.loads(_ok(runner.invoke(app, ["case", "notes", "--json"])))
    assert (notes[0]["note"], notes[0]["tier"], notes[0]["page"]) == ("second draft", "infer", 4)
    assert runner.invoke(app, ["case", "note", "edit", "9"]).exit_code == 1
    assert runner.invoke(app, ["case", "note", "edit", "x"]).exit_code == 2
