"""Regression tests for public-records search output."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from typer.testing import CliRunner

from openfoia import net as net_mod
from openfoia.cli import app
from openfoia.records.base import AdapterRequestError, RecordEntity, SearchResult


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Searches log to the active case, so never touch the real data dir."""
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))


def test_typed_sec_empty_search_reports_scope_and_coverage_limit(monkeypatch):
    """A zero typed SEC result must not look like proof that no filing exists."""

    class Adapter:
        async def search(self, query, **kwargs):
            assert query == "Webull Corp"
            assert kwargs == {"filing_type": "F-3"}
            return SearchResult(source="sec", query=query, total_results=0, entities=[])

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "Webull Corp", "--source", "sec", "--type", "F-3"],
    )

    assert result.exit_code == 0
    output = " ".join(result.output.lower().split())
    assert "sec returned 0 total results for 'webull corp'" in output
    assert "showing 0 of 0 results" in output
    assert "applied sec filing type filter: f-3" in output
    assert "0 results is not proof that no filing exists" in output


def test_non_sec_empty_search_keeps_existing_message(monkeypatch):
    """The typed SEC warning must not change other sources' output."""

    class Adapter:
        async def search(self, query, **kwargs):
            return SearchResult(source="opencorporates", query=query, total_results=0, entities=[])

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app,
        ["records", "search", "No Such Company", "--source", "opencorporates"],
    )

    assert result.exit_code == 0
    assert "No results found for 'No Such Company' on opencorporates." in result.output


# --- Search workbench: paging, --json, errors that are errors ---------------


def _mock_transport(monkeypatch, module, handler):
    real = net_mod.egress_client

    def fake(policy=None, *, timeout=15.0, isolation_token=None, headers=None, **kw):
        return real(policy, timeout=timeout, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(module, "egress_client", fake)


def _dc_entity(n):
    return RecordEntity(
        entity_type="DOCUMENT",
        name=f"Doc {n}",
        source="documentcloud",
        source_url=f"https://www.documentcloud.org/documents/{n}/",
        identifiers={"documentcloud_id": str(n), "slug": f"doc-{n}"},
        extra_data={"pages": 3, "created_at": "2020-01-01", "highlights": ["x" * 300]},
    )


def test_documentcloud_limit_above_25_pages_through_results(monkeypatch):
    calls = []

    class Adapter:
        async def search(self, query, page=1, page_size=25, **kwargs):
            calls.append((page, page_size))
            start = (page - 1) * page_size
            ents = [_dc_entity(i) for i in range(start, min(start + page_size, 230))]
            return SearchResult(
                source="documentcloud", query=query, total_results=230, entities=ents
            )

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app, ["records", "search", "PAS", "-s", "documentcloud", "-n", "250", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [(1, 100), (2, 100), (3, 100)]
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == 230
    assert rows[0] == {
        "id": "0",
        "title": "Doc 0",
        "source": "documentcloud",
        "pages": 3,
        "created": "2020-01-01",
        "url": "https://www.documentcloud.org/documents/0/",
        "snippet": "x" * 200,
    }


def test_documentcloud_adapter_sends_page_number(monkeypatch):
    from openfoia.records import documentcloud

    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json={"count": 0, "results": []})

    _mock_transport(monkeypatch, documentcloud, handler)

    asyncio.run(documentcloud.DocumentCloudAdapter().search("x", page=3, page_size=100))

    assert seen["page"] == "3"
    assert seen["per_page"] == "100"


def test_muckrock_401_is_an_error_not_no_results(monkeypatch):
    from openfoia.records import muckrock

    _mock_transport(monkeypatch, muckrock, lambda request: httpx.Response(401, json={}))

    result = CliRunner().invoke(app, ["records", "search", "CIA", "-s", "muckrock"])

    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "MuckRock API requires an API token (HTTP 401). Set OPENFOIA_MUCKROCK_TOKEN." in output
    assert "No results found" not in result.output


def test_documentcloud_rate_limit_exits_nonzero(monkeypatch):
    from openfoia.records import documentcloud

    _mock_transport(monkeypatch, documentcloud, lambda request: httpx.Response(429))

    result = CliRunner().invoke(app, ["records", "search", "PAS", "-s", "documentcloud"])

    assert result.exit_code == 1
    assert "rate limit" in result.output
    assert "No results found" not in result.output


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            403,
            headers={"server": "cloudflare", "cf-ray": "abc123", "cf-mitigated": "challenge"},
            text="Forbidden",
        ),
        httpx.Response(
            403,
            headers={"content-type": "text/html"},
            text="<html><title>Just a moment...</title>challenge-platform</html>",
        ),
    ],
)
def test_documentcloud_cloudflare_block_is_specific_error(monkeypatch, response):
    from openfoia.records import documentcloud

    _mock_transport(monkeypatch, documentcloud, lambda request: response)

    result = asyncio.run(documentcloud.DocumentCloudAdapter().search("PAS"))

    assert result.entities == []
    assert result.error is not None
    assert "may be blocked from this connection" in result.error
    assert "browser" in result.error
    assert "docs ingest" in result.error


def test_documentcloud_generic_forbidden_is_not_cloudflare_error(monkeypatch):
    from openfoia.records import documentcloud

    _mock_transport(
        monkeypatch,
        documentcloud,
        lambda request: httpx.Response(
            403,
            headers={"server": "cloudflare", "cf-ray": "abc123"},
            text="Forbidden. Cloudflare Ray ID: abc123",
        ),
    )

    result = asyncio.run(documentcloud.DocumentCloudAdapter().search("PAS"))

    assert result.entities == []
    assert result.error is not None
    assert "search failed" in result.error
    assert "may be blocked from this connection" not in result.error


def test_documentcloud_read_timeout_is_uncertain_block_error(monkeypatch):
    from openfoia.records import documentcloud

    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    _mock_transport(monkeypatch, documentcloud, handler)

    result = asyncio.run(documentcloud.DocumentCloudAdapter().search("PAS"))

    assert result.entities == []
    assert result.error is not None
    assert "did not complete before the response timeout" in result.error
    assert "may be slow or unavailable" in result.error
    assert "may be blocked from this connection" in result.error
    assert "did not return a response" not in result.error
    assert "browser" in result.error
    assert "docs ingest" in result.error


def test_all_sources_reports_each_source_status(monkeypatch):
    class Ok:
        async def search(self, query, **kwargs):
            return SearchResult(source="sec", query=query, total_results=7, entities=[])

    class Broken:
        async def search(self, query, **kwargs):
            raise AdapterRequestError("HTTP 401")

    monkeypatch.setattr("openfoia.records.list_sources", lambda: ["govinfo", "muckrock", "sec"])
    monkeypatch.setattr(
        "openfoia.records.get_adapter", lambda s: Broken() if s == "muckrock" else Ok()
    )

    result = CliRunner().invoke(app, ["records", "search", "PAS", "--all-sources"])

    assert result.exit_code == 2, result.output  # partial failure
    assert "govinfo         ok 7" in result.output
    assert "muckrock        error: HTTP 401" in result.output

    result = CliRunner().invoke(app, ["records", "search", "PAS", "--all-sources", "-t", "10-K"])

    assert result.exit_code == 0  # muckrock skipped, nothing errored
    assert "govinfo         skipped (filing_type filter not supported)" in result.output
    assert "sec             ok 7" in result.output


def test_documentcloud_failed_page_keeps_earlier_pages(monkeypatch):
    class Adapter:
        async def search(self, query, page=1, page_size=25, **kwargs):
            if page == 3:
                return SearchResult(
                    source="documentcloud", query=query, total_results=0, entities=[], error="429"
                )
            ents = [_dc_entity((page - 1) * page_size + i) for i in range(page_size)]
            return SearchResult(
                source="documentcloud", query=query, total_results=500, entities=ents
            )

    monkeypatch.setattr("openfoia.records.get_adapter", lambda source: Adapter())

    result = CliRunner().invoke(
        app, ["records", "search", "PAS", "-s", "documentcloud", "-n", "300", "--json"]
    )

    assert result.exit_code == 1
    assert len(result.stdout.splitlines()) == 200
    assert "Search error (documentcloud): 429" in result.stderr


def test_all_sources_exits_nonzero_when_every_source_fails(monkeypatch):
    class Broken:
        async def search(self, query, **kwargs):
            raise AdapterRequestError("down")

    monkeypatch.setattr("openfoia.records.list_sources", lambda: ["muckrock"])
    monkeypatch.setattr("openfoia.records.get_adapter", lambda s: Broken())

    result = CliRunner().invoke(app, ["records", "search", "PAS", "--all-sources"])

    assert result.exit_code == 1
