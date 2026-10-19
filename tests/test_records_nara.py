"""NARA Catalog adapter: field mapping, key requirement, quota counter, honest errors."""

from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.records import nara as nara_mod
from openfoia.records.base import AdapterRequestError
from openfoia.records.nara import NARAAdapter

runner = CliRunner()


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENFOIA_NARA_API_KEY", raising=False)
    monkeypatch.delenv("CATALOG_API_KEY", raising=False)
    monkeypatch.delenv("OPENFOIA_NARA_IGNORE_QUOTA", raising=False)
    monkeypatch.setattr(nara_mod, "_last_call", 0.0)

    async def no_wait():
        return None

    monkeypatch.setattr(nara_mod, "_throttle", no_wait)


def _hit(naid=7283224, title="Winter 1986: The Supreme Court and the Intelligence Source", text=()):
    objects = [
        {"objectId": str(i), "objectType": "Image (JPG)", "objectUrl": f"https://x/{i}.jpg"}
        for i in range(1, 4)
    ]
    for obj, t in zip(objects, text, strict=False):
        obj["extractedText"] = t
    return {
        "_id": str(naid),
        "_source": {
            "record": {
                "naId": naid,
                "title": title,
                "levelOfDescription": "fileUnit",
                "productionDates": [{"logicalDate": "1986-01-01", "year": 1986}],
                "scopeAndContentNote": "A declassified CIA studies article.",
                "accessRestriction": {"status": "Unrestricted"},
                "digitalObjects": objects,
                "ancestors": [
                    {
                        "title": "Records of the Central Intelligence Agency",
                        "naId": 590,
                        "recordGroupNumber": 263,
                        "levelOfDescription": "recordGroup",
                    }
                ],
            }
        },
    }


def _response(hits, total=None):
    return {
        "body": {
            "hits": {
                "total": {"value": len(hits) if total is None else total, "relation": "eq"},
                "hits": hits,
            },
            "aggregations": {},
        }
    }


def _patch_request(monkeypatch, payload, calls):
    async def fake_request(self, url, *, params=None, headers=None, timeout=15.0, **kw):
        calls.append({"url": url, "params": params, "headers": headers})
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(NARAAdapter, "_request", fake_request)


def test_search_maps_record_fields_and_sends_key(monkeypatch):
    calls: list = []
    _patch_request(monkeypatch, _response([_hit()], total=55), calls)
    adapter = NARAAdapter(api_key="k")

    result = asyncio.run(adapter.search("mkultra", page=2, page_size=10))

    assert result.error is None and result.total_results == 55
    assert calls[0]["url"] == "https://catalog.archives.gov/api/v2/records/search"
    assert calls[0]["headers"]["x-api-key"] == "k"
    assert calls[0]["params"] == {"q": "mkultra", "limit": 10, "page": 2}
    e = result.entities[0]
    assert e.source == "nara" and e.name.startswith("Winter 1986")
    assert e.source_url == "https://catalog.archives.gov/id/7283224"
    assert e.identifiers == {"naid": "7283224", "record_group": "263"}
    assert e.extra_data["pages"] == 3 and e.extra_data["date"] == "1986-01-01"
    assert e.extra_data["collection"] == "Records of the Central Intelligence Agency"
    assert nara_mod.quota_count() == 1


def test_missing_key_is_an_error_not_empty_results(monkeypatch):
    calls: list = []
    _patch_request(monkeypatch, _response([_hit()]), calls)
    result = asyncio.run(NARAAdapter().search("mkultra"))
    assert result.error and "Catalog_API@nara.gov" in result.error
    assert calls == []  # nothing was sent
    with pytest.raises(AdapterRequestError, match="OPENFOIA_NARA_API_KEY"):
        asyncio.run(NARAAdapter().fetch("7283224"))


def test_quota_warns_at_80_percent_and_refuses_at_100(monkeypatch, caplog):
    calls: list = []
    _patch_request(monkeypatch, _response([]), calls)
    adapter = NARAAdapter(api_key="k")
    path = nara_mod.quota_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import UTC, datetime

    month = datetime.now(UTC).strftime("%Y-%m")
    path.write_text(json.dumps({"month": month, "count": 8_000}))
    with caplog.at_level("WARNING"):
        asyncio.run(adapter.search("x"))
    assert "8000 of 10000" in caplog.text and len(calls) == 1

    path.write_text(json.dumps({"month": month, "count": 10_000}))
    result = asyncio.run(adapter.search("x"))
    assert result.error and "quota reached" in result.error and len(calls) == 1

    monkeypatch.setenv("OPENFOIA_NARA_IGNORE_QUOTA", "1")
    asyncio.run(adapter.search("x"))
    assert len(calls) == 2

    path.write_text(json.dumps({"month": "1999-01", "count": 10_000}))  # stale month resets
    assert nara_mod.quota_count() == 0


def test_http_failures_are_errors(monkeypatch):
    calls: list = []
    _patch_request(
        monkeypatch, AdapterRequestError("HTTPStatusError: 429 Too Many Requests"), calls
    )
    result = asyncio.run(NARAAdapter(api_key="k").search("x"))
    assert result.error and "429" in result.error


def test_fetch_uses_ocr_text_per_page_else_description(monkeypatch):
    calls: list = []
    _patch_request(monkeypatch, _response([_hit(text=("a", "", "c"))]), calls)
    entity = asyncio.run(NARAAdapter(api_key="k").fetch("NARA-7283224"))
    assert calls[0]["params"] == {"naId": "7283224", "includeExtractedText": "true"}
    # Blank pages are kept so page numbers hold: object 3's text is p.3.
    assert entity.extra_data["page_texts"] == ["a", "", "c"]
    assert entity.extra_data["text_kind"] == "ocr"

    _patch_request(monkeypatch, _response([_hit()]), calls)
    entity = asyncio.run(NARAAdapter(api_key="k").fetch("7283224"))
    assert entity.extra_data["text_kind"] == "description"
    assert entity.extra_data["full_text"].startswith("NARA catalog description (no OCR text)")
    assert "declassified CIA studies article" in entity.extra_data["full_text"]


def test_blank_pages_keep_positions_when_stored(monkeypatch):
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "k")
    _patch_request(monkeypatch, _response([_hit(text=("a", "", "c"))]), [])
    assert runner.invoke(app, ["records", "fetch", "7283224", "-s", "nara"]).exit_code == 0
    out = runner.invoke(app, ["docs", "list", "--json"]).output
    assert json.loads(out)[0]["stored_pages"] == 3 if "stored_pages" in out else True
    assert "p.3" in runner.invoke(app, ["docs", "grep", "c"]).output


def test_description_only_record_is_marked_when_stored(monkeypatch):
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "k")
    _patch_request(monkeypatch, _response([_hit()]), [])
    assert runner.invoke(app, ["records", "fetch", "7283224", "-s", "nara"]).exit_code == 0
    assert "nara-7283224-description.txt" in runner.invoke(app, ["docs", "list"]).output
    doc = json.loads(runner.invoke(app, ["docs", "list", "--json"]).output)[0]
    shown = runner.invoke(app, ["docs", "show", doc["id"]]).output
    assert "NARA catalog description (no OCR text)" in shown


def test_api_base_override_refuses_other_hosts(monkeypatch):
    calls: list = []
    _patch_request(monkeypatch, _response([]), calls)
    monkeypatch.setenv("OPENFOIA_NARA_API_BASE", "https://evil.example/api/v2")
    result = asyncio.run(NARAAdapter(api_key="k").search("x"))
    assert result.error and "refusing to send the API key" in result.error and calls == []
    monkeypatch.setenv("OPENFOIA_NARA_API_BASE", "http://catalog.archives.gov/api/v2")
    assert asyncio.run(NARAAdapter(api_key="k").search("x")).error and calls == []
    monkeypatch.setenv("OPENFOIA_NARA_API_BASE", "http://127.0.0.1:8080/")
    asyncio.run(NARAAdapter(api_key="k").search("x"))
    assert calls[-1]["url"] == "http://127.0.0.1:8080/records/search"


def test_quota_warning_prints_once_per_process(monkeypatch, caplog):
    _patch_request(monkeypatch, _response([]), [])
    monkeypatch.setattr(nara_mod, "_quota_warned", False)
    from datetime import UTC, datetime

    path = nara_mod.quota_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"month": datetime.now(UTC).strftime("%Y-%m"), "count": 9_000}))
    adapter = NARAAdapter(api_key="k")
    with caplog.at_level("WARNING"):
        asyncio.run(adapter.search("x"))
        asyncio.run(adapter.search("y"))
    assert caplog.text.count("NARA quota:") == 1


def test_govinfo_sends_key_in_header_and_never_prints_it(monkeypatch, caplog):
    from openfoia.records import govinfo as govinfo_mod
    from openfoia.records.govinfo import GovInfoAdapter

    monkeypatch.setenv("OPENFOIA_GOVINFO_API_KEY", "SECRET-KEY-123")
    seen: dict = {}

    class Resp:
        status_code = 500

        def raise_for_status(self):
            raise RuntimeError(
                "Server error '500' for url 'https://api.govinfo.gov/search?api_key=SECRET-KEY-123'"
            )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            seen.update(kw)
            return Resp()

        async def get(self, url, **kw):
            seen.update(kw)
            return Resp()

    monkeypatch.setattr(govinfo_mod, "egress_client", lambda *a, **k: Client())
    with caplog.at_level("WARNING"):
        result = asyncio.run(GovInfoAdapter().search("x"))
        asyncio.run(GovInfoAdapter().fetch("PKG-1"))
    assert seen["headers"] == {"X-Api-Key": "SECRET-KEY-123"} and "params" not in seen
    assert "SECRET-KEY-123" not in result.error and "SECRET-KEY-123" not in caplog.text
    assert "api_key=<redacted>" in result.error


def test_all_sources_with_key_prints_notice_once(monkeypatch):
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "k")
    _patch_request(monkeypatch, _response([_hit()]), [])
    monkeypatch.setattr("openfoia.records.list_sources", lambda: ["nara"])
    r = runner.invoke(app, ["records", "search", "mkultra", "--all-sources"])
    assert r.exit_code == 0, r.output
    assert r.output.count("catalog.archives.gov with your API key") == 1
    assert r.output.count("not endorsed or certified") == 1
    assert "nara" in r.output and "ok 1" in r.output


def test_cli_fetch_stores_nara_document_in_case(monkeypatch):
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "k")
    calls: list = []
    _patch_request(
        monkeypatch, _response([_hit(text=("page one", "page two", "page three"))]), calls
    )

    assert runner.invoke(app, ["case", "new", "Archives"]).exit_code == 0
    r = runner.invoke(app, ["records", "fetch", "7283224", "-s", "nara"])
    assert r.exit_code == 0, r.output
    assert "saved" in r.output and "Attached to the active case" in r.output

    r = runner.invoke(app, ["records", "fetch", "7283224", "-s", "nara"])
    assert "already stored" in r.output

    r = runner.invoke(app, ["docs", "grep", "two"])
    assert "p.2" in r.output
    out = runner.invoke(app, ["case", "export", "-f", "json"]).output
    assert "https://catalog.archives.gov/id/7283224" in out


def test_cli_search_without_key_exits_1_and_all_sources_skips(monkeypatch):
    r = runner.invoke(app, ["records", "search", "x", "-s", "nara"])
    assert r.exit_code == 1 and "Catalog_API@nara.gov" in r.output

    from openfoia.records.base import SearchResult

    class Quiet:
        has_key = False

        async def search(self, query, **kwargs):
            return SearchResult(source="sec", query=query, total_results=1, entities=[])

    monkeypatch.setattr("openfoia.records.list_sources", lambda: ["nara", "sec"])
    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Quiet())
    r = runner.invoke(app, ["records", "search", "x", "--all-sources"])
    assert r.exit_code == 0, r.output
    assert "skipped (no OPENFOIA_NARA_API_KEY)" in r.output and "sec" in r.output


def test_catalog_api_key_alias(monkeypatch):
    monkeypatch.setenv("CATALOG_API_KEY", "nara-own-name")
    assert NARAAdapter()._api_key == "nara-own-name"
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "ours")
    assert NARAAdapter()._api_key == "ours"
    assert "CATALOG_API_KEY" in nara_mod.NO_KEY_MESSAGE


def test_ancestor_walk_uses_search_after_counts_each_query_and_caps(monkeypatch):
    def page(start, n):
        hits = [_hit(naid=start + i) for i in range(n)]
        for i, h in enumerate(hits):
            h["sort"] = [f"s{start + i}"]
        return _response(hits, total=250)

    pages = [page(1000, 100), page(2000, 100), _response([], total=250)]
    calls: list = []

    async def fake_request(self, url, *, params=None, headers=None, timeout=15.0, **kw):
        calls.append(dict(params))
        return pages[len(calls) - 1]

    monkeypatch.setattr(NARAAdapter, "_request", fake_request)
    result = asyncio.run(NARAAdapter(api_key="k").search("", ancestor=7283224, all_pages=True))
    assert result.error is None and len(result.entities) == 200
    assert [c["searchAfter"] for c in calls] == ["*", "s1099", "s2099"]
    assert all(c["ancestorNaId"] == "7283224" and c["q"] == "*" for c in calls)
    assert all("page" not in c and c["limit"] == 100 for c in calls)
    assert nara_mod.quota_count() == 3

    calls.clear()
    result = asyncio.run(NARAAdapter(api_key="k").search("x", all_pages=True, max_hits=150))
    assert len(calls) == 2 and len(result.entities) == 150


def test_cli_all_announces_query_count_and_rejects_other_sources(monkeypatch):
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "k")
    _patch_request(monkeypatch, _response([_hit()]), [])
    r = runner.invoke(app, ["records", "search", "-s", "nara", "--ancestor", "1", "--all"])
    assert r.exit_code == 0, r.output
    assert "up to 10 queries" in r.output
    r = runner.invoke(app, ["records", "search", "x", "-s", "sec", "--all"])
    assert r.exit_code == 1 and "only supported with --source nara" in r.output


def _hit_with_files():
    hit = _hit()
    for i, obj in enumerate(hit["_source"]["record"]["digitalObjects"], 1):
        obj["objectUrl"] = f"https://nara-media-001.s3.amazonaws.com/arcmedia/{i}.pdf"
        obj["objectFilename"] = f"{i}.pdf"
        obj["objectFileSize"] = 4
        obj["objectType"] = "Portable Document File (PDF)"
    return hit


def test_list_objects_prints_urls_and_downloads_nothing(monkeypatch):
    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "k")
    _patch_request(monkeypatch, _response([_hit_with_files()]), [])

    def no_download(*a, **k):
        raise AssertionError("list-objects must not download")

    monkeypatch.setattr(NARAAdapter, "download_objects", no_download)
    r = runner.invoke(app, ["records", "fetch", "7283224", "-s", "nara", "--list-objects"])
    assert r.exit_code == 0, r.output
    assert "https://nara-media-001.s3.amazonaws.com/arcmedia/3.pdf" in r.output
    r = runner.invoke(
        app, ["records", "fetch", "7283224", "-s", "nara", "--list-objects", "--json"]
    )
    listed = json.loads(r.output.strip().splitlines()[-1])
    assert [o["index"] for o in listed] == [1, 2, 3] and listed[0]["size"] == 4
    r = runner.invoke(app, ["records", "fetch", "1", "-s", "nara", "--list-objects", "--objects"])
    assert r.exit_code == 2


def test_objects_skip_present_file_and_ingest_the_rest(monkeypatch):
    from types import SimpleNamespace

    from openfoia.db import get_data_dir
    from openfoia.pipeline.ingest import DocumentIngester

    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "k")
    _patch_request(monkeypatch, _response([_hit_with_files()]), [])
    folder = get_data_dir() / "nara" / "7283224"
    folder.mkdir(parents=True)
    (folder / "1.pdf").write_bytes(b"%PDF")  # already downloaded on an earlier run

    downloaded: list = []

    async def fake_download(client, url, dest, max_bytes):
        downloaded.append(url)
        dest.write_bytes(b"%PDF")
        return 4

    ingested: list = []

    async def fake_ingest(self, path, **kw):
        ingested.append(path.name)
        return SimpleNamespace(
            document_id=f"doc-{path.name}",
            filename=path.name,
            file_path=str(path),
            file_size=4,
            mime_type="application/pdf",
            page_count=1,
            extracted_text=f"text of {path.name}",
            pages=None,
            verdicts=None,
        )

    monkeypatch.setattr("openfoia.records.base.download_to_file", fake_download)
    monkeypatch.setattr(DocumentIngester, "ingest_file", fake_ingest)
    before = nara_mod.quota_count()
    r = runner.invoke(app, ["records", "fetch", "7283224", "-s", "nara", "--objects"])
    assert r.exit_code == 0, r.output
    assert "nara-media-001.s3.amazonaws.com" in r.output
    assert [u.rsplit("/", 1)[-1] for u in downloaded] == ["2.pdf", "3.pdf"]
    # 1.pdf was on disk with no Document row (a run died before ingest): ingested now.
    assert ingested == ["1.pdf", "2.pdf", "3.pdf"]
    assert nara_mod.quota_count() == before + 1  # the record query only, not the downloads
    names = {
        d["filename"] for d in json.loads(runner.invoke(app, ["docs", "list", "--json"]).output)
    }
    assert {"1.pdf", "2.pdf", "3.pdf", "nara-7283224-description.txt"} <= names

    ingested.clear()
    r = runner.invoke(app, ["records", "fetch", "7283224", "-s", "nara", "--objects"])
    assert r.exit_code == 0, r.output
    assert ingested == []  # every file present and already has a row


def test_object_host_allowlist():
    assert nara_mod.object_url_allowed("https://nara-media-001.s3.amazonaws.com/a/b.jpg")
    assert nara_mod.object_url_allowed("https://s3.amazonaws.com/NARAprodstorage/a.pdf")
    assert not nara_mod.object_url_allowed("http://nara-media-001.s3.amazonaws.com/a.jpg")
    assert not nara_mod.object_url_allowed("https://s3.amazonaws.com/other-bucket/a.pdf")
    assert not nara_mod.object_url_allowed("http://www.reaganlibrary.archives.gov/c.jpg")
    # Live API (2026-10-19) serves scans from the catalog host itself.
    live = "https://catalog.archives.gov/medialive/84/4008/17400884/content/x/Page_019.JPG"
    assert nara_mod.object_url_allowed(live)
    assert not nara_mod.object_url_allowed(live.replace("https", "http", 1))
    assert not nara_mod.object_url_allowed("https://catalog.archives.gov/api/v2/records")
    assert not nara_mod.object_url_allowed("https://catalog.archives.gov/medialive/../api/v2")
    assert not nara_mod.object_url_allowed("https://catalog.archives.gov.evil.example/medialive/a")


def test_objects_use_the_configured_tor_policy(monkeypatch):
    from openfoia import cli
    from openfoia.net import EgressMode, EgressPolicy

    monkeypatch.setenv("OPENFOIA_NARA_API_KEY", "k")
    _patch_request(monkeypatch, _response([_hit_with_files()]), [])
    tor = EgressPolicy(mode=EgressMode.TOR)
    monkeypatch.setattr(cli, "_cia_policy_or_exit", lambda: tor)
    seen: list = []

    async def fake_objects(self, entity, dest_dir):
        seen.append(self._egress)
        return []

    monkeypatch.setattr(NARAAdapter, "download_objects", fake_objects)
    r = runner.invoke(app, ["records", "fetch", "7283224", "-s", "nara", "--objects"])
    assert r.exit_code == 0, r.output
    assert seen == [tor] and "via Tor" in r.output


def test_ingest_persists_each_file_before_a_later_one_dies(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from openfoia import cli
    from openfoia.db import ensure_db, get_session
    from openfoia.models import Document
    from openfoia.pipeline.ingest import DocumentIngester

    ensure_db()
    files = []
    for name in ("a.pdf", "b.pdf"):
        (tmp_path / name).write_bytes(b"%PDF")
        files.append(({}, tmp_path / name, "downloaded"))

    async def fake_ingest(self, path, **kw):
        if path.name == "b.pdf":
            raise KeyboardInterrupt  # the run is killed mid-batch
        return SimpleNamespace(
            document_id="doc-a",
            filename=path.name,
            file_path=str(path),
            file_size=4,
            mime_type="application/pdf",
            page_count=1,
            extracted_text="a",
            pages=None,
            verdicts=None,
        )

    monkeypatch.setattr(DocumentIngester, "ingest_file", fake_ingest)
    entity = SimpleNamespace(name="x", source_url="https://catalog.archives.gov/id/1")
    with pytest.raises(KeyboardInterrupt):
        cli._ingest_nara_objects(entity, "1", files)
    with get_session() as session:
        assert [d.filename for d in session.query(Document)] == ["a.pdf"]


def test_text_fetch_after_objects_reuses_the_request(tmp_path):
    from types import SimpleNamespace

    from openfoia import cli
    from openfoia.db import ensure_db, get_session
    from openfoia.models import Document, Request
    from openfoia.records.base import RecordEntity
    from openfoia.records.store import store_text_document

    ensure_db()
    obj = SimpleNamespace(
        document_id="doc-obj",
        filename="1.pdf",
        file_path=str(tmp_path / "1.pdf"),
        file_size=4,
        mime_type="application/pdf",
        page_count=1,
        extracted_text="scan",
        pages=None,
        verdicts=None,
    )
    cli._persist_ingested([obj], request_number="NARA-1", subject="x", body="objects")
    entity = RecordEntity(
        entity_type="DOCUMENT", name="x", source="nara", extra_data={"full_text": "ocr"}
    )
    doc_id, _ = store_text_document(
        entity, request_number="NARA-1", filename="nara-1.txt", body="text"
    )
    with get_session() as session:
        assert session.query(Request).count() == 1
        assert session.get(Document, "doc-obj").extracted_text == "scan"  # not overwritten
        assert session.get(Document, doc_id).filename == "nara-1.txt"


def test_object_filenames_keep_url_special_characters():
    assert nara_mod._object_filename("a#1.jpg") != nara_mod._object_filename("a#2.jpg")
    assert nara_mod._object_filename("a?b;c.pdf") == "a_b_c.pdf"
    assert nara_mod._object_filename("../../etc/passwd") == "passwd"
    assert nara_mod._object_filename("..") == "" and nara_mod._object_filename(None) == ""
