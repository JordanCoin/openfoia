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
    monkeypatch.delenv("OPENFOIA_NARA_IGNORE_QUOTA", raising=False)
    monkeypatch.setattr(nara_mod, "_last_call", 0.0)


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
    _patch_request(monkeypatch, _response([_hit(text=("page one", "", "page three"))]), calls)
    entity = asyncio.run(NARAAdapter(api_key="k").fetch("NARA-7283224"))
    assert calls[0]["params"] == {"naId": "7283224", "includeExtractedText": "true"}
    assert entity.extra_data["page_texts"] == ["page one", "page three"]
    assert entity.extra_data["text_kind"] == "ocr"

    _patch_request(monkeypatch, _response([_hit()]), calls)
    entity = asyncio.run(NARAAdapter(api_key="k").fetch("7283224"))
    assert entity.extra_data["text_kind"] == "description"
    assert "declassified CIA studies article" in entity.extra_data["full_text"]


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
