"""Offline tests for SEC company filing history."""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from openfoia.cli import app
from openfoia.config import OpenFOIAConfig
from openfoia.net import EgressMode, EgressPolicy
from openfoia.records.base import AdapterRequestError, SearchResult
from openfoia.records.sec_edgar import (
    SEC_REQUEST_DELAY,
    SEC_SUBMISSIONS_BASE,
    SEC_TICKERS_URL,
    SECEdgarAdapter,
)


def test_normalize_cik_and_reject_invalid_values() -> None:
    assert SECEdgarAdapter.normalize_cik("123") == "0000000123"
    with pytest.raises(ValueError, match="CIK"):
        SECEdgarAdapter.normalize_cik("12345678901")
    assert SECEdgarAdapter._validate_forms("10-K/A, SC 13D") == {"10-K/A", "SC 13D"}
    with pytest.raises(ValueError):
        SECEdgarAdapter._validate_forms("10-K,../bad")


def test_filings_resolves_ticker_aggregates_archives_and_filters() -> None:
    class FakeAdapter(SECEdgarAdapter):
        def __init__(self) -> None:
            super().__init__(egress=EgressPolicy(mode=EgressMode.TOR))
            self.calls: list[tuple[str, dict]] = []

        async def _request(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append((url, kwargs))
            if url == SEC_TICKERS_URL:
                return {"0": {"ticker": "ABC", "cik_str": 123, "title": "ABC Corp"}}
            if url.endswith("CIK0000000123.json"):
                return {
                    "name": "ABC Corp",
                    "filings": {
                        "recent": {
                            "accessionNumber": [
                                "0000000001-02-000002",
                                "0000000001-01-000001",
                            ],
                            "filingDate": ["2025-01-02", "2024-01-01"],
                            "form": ["10-K", "8-K"],
                            "primaryDocument": ["annual.htm", "event.htm"],
                        },
                        "files": [{"name": "CIK0000000123-submissions-001.json"}],
                    },
                }
            return {
                "accessionNumber": ["0000000001-01-000003"],
                "filingDate": ["2023-01-01"],
                "form": ["10-K"],
                "primaryDocument": ["old.htm"],
            }

    adapter = FakeAdapter()
    result = asyncio.run(adapter.filings("abc"))

    assert [e.extra_data["filing_date"] for e in result.entities] == [
        "2025-01-02",
        "2024-01-01",
        "2023-01-01",
    ]
    assert result.entities[0].source_url.endswith("/123/000000000102000002/annual.htm")
    assert result.entities[-1].source_url.endswith("/123/000000000101000003/old.htm")
    assert all(kwargs["headers"]["User-Agent"] for _, kwargs in adapter.calls)
    assert all("_egress" not in kwargs for _, kwargs in adapter.calls)
    assert adapter.calls[0][0] == SEC_TICKERS_URL
    assert adapter.calls[1][0] == f"{SEC_SUBMISSIONS_BASE}/CIK0000000123.json"


def test_suspicious_primary_document_falls_back_to_index() -> None:
    entity = SECEdgarAdapter._filing_entity(
        {
            "accessionNumber": "0000000001-02-000001",
            "filingDate": "2025-01-02",
            "form": "10-K",
            "primaryDocument": "../secret.txt",
        },
        cik="0000000123",
        company_name="ABC",
    )
    assert entity is not None
    assert entity.source_url.endswith("/123/000000000102000001/0000000001-02-000001-index.htm")


@pytest.mark.parametrize(
    "filing",
    [
        {"accessionNumber": "bad", "filingDate": "2025-01-02", "form": "10-K"},
        {"accessionNumber": "0000000001-02-000001", "filingDate": "2025-99-99", "form": "10-K"},
    ],
)
def test_invalid_filing_metadata_is_skipped(filing) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(AdapterRequestError):
        SECEdgarAdapter._filing_entity(filing, cik="0000000123", company_name="ABC")


@pytest.mark.parametrize(
    "filing",
    [
        {
            "accessionNumber": "0000000001-02-000001",
            "filingDate": "2025-01-02",
            "form": ["10-K"],
        },
        {
            "accessionNumber": "0000000001-02-000001",
            "filingDate": "2025-01-02",
            "form": "10-K",
            "primaryDocument": ["safe.htm"],
        },
    ],
)
def test_non_string_remote_metadata_is_source_error(filing) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(AdapterRequestError):
        SECEdgarAdapter._filing_entity(filing, cik="0000000123", company_name="ABC")


def test_unequal_optional_column_is_source_error() -> None:
    with pytest.raises(AdapterRequestError):
        SECEdgarAdapter._filing_rows(
            {
                "accessionNumber": ["0000000001-02-000001"],
                "filingDate": ["2025-01-02"],
                "form": ["10-K"],
                "optional": [],
            }
        )


def test_dot_primary_document_falls_back_to_index() -> None:
    entity = SECEdgarAdapter._filing_entity(
        {
            "accessionNumber": "0000000001-02-000001",
            "filingDate": "2025-01-02",
            "form": "10-K",
            "primaryDocument": "..",
        },
        cik="0000000123",
        company_name="ABC",
    )
    assert entity is not None
    assert entity.source_url.endswith("-index.htm")


def test_source_failure_is_not_reported_as_no_results() -> None:
    class FailedAdapter(SECEdgarAdapter):
        async def _request(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            raise AdapterRequestError("offline")

    result = asyncio.run(FailedAdapter().filings("ABC"))
    assert result.entities == []
    assert result.error == "offline"


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        {},
        {"filings": {}},
        {"filings": []},
        {"accessionNumber": [], "filingDate": [], "form": []},
    ],
)
def test_malformed_archive_json_is_a_source_failure(malformed) -> None:  # type: ignore[no-untyped-def]
    class MalformedAdapter(SECEdgarAdapter):
        async def _request(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            if url == SEC_TICKERS_URL:
                return {"0": {"ticker": "ABC", "cik_str": 123, "title": "ABC Corp"}}
            if url.endswith("CIK0000000123.json"):
                return {
                    "filings": {
                        "recent": {},
                        "files": [{"name": "CIK0000000123-submissions-001.json"}],
                    }
                }
            return malformed

    result = asyncio.run(MalformedAdapter().filings("ABC"))
    assert result.entities == []
    assert result.error is not None


def test_malformed_root_json_is_a_source_failure() -> None:
    class MalformedAdapter(SECEdgarAdapter):
        async def _request(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            if url == SEC_TICKERS_URL:
                return {"0": {"ticker": "ABC", "cik_str": 123, "title": "ABC Corp"}}
            return []

    result = asyncio.run(MalformedAdapter().filings("ABC"))
    assert result.entities == []
    assert result.error is not None


@pytest.mark.parametrize(
    "root",
    [
        {},
        {"filings": {}},
        {"filings": {"recent": {}}},
        {"filings": {"recent": [], "files": []}},
        {
            "filings": {
                "recent": {
                    "accessionNumber": ["0000000001-02-000001"],
                    "filingDate": ["2025-01-02"],
                },
                "files": [],
            }
        },
        {
            "filings": {
                "recent": {
                    "accessionNumber": ["0000000001-02-000001"],
                    "filingDate": ["2025-01-02"],
                    "form": ["10-K"],
                    "primaryDocument": [],
                },
                "files": [],
            }
        },
    ],
)
def test_submission_schema_drift_is_a_source_failure(root) -> None:  # type: ignore[no-untyped-def]
    class SchemaAdapter(SECEdgarAdapter):
        async def _request(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            return root

    result = asyncio.run(SchemaAdapter().filings("123"))
    assert result.entities == []
    assert result.error is not None


def test_invalid_row_is_a_source_failure_before_filters() -> None:
    class InvalidRowAdapter(SECEdgarAdapter):
        async def _request(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            return {
                "filings": {
                    "recent": {
                        "accessionNumber": ["not-an-accession"],
                        "filingDate": ["2020-01-01"],
                        "form": ["10-K"],
                    },
                    "files": [],
                }
            }

    result = asyncio.run(InvalidRowAdapter().filings("123", since="2025-01-01", forms="10-K"))
    assert result.entities == []
    assert result.error is not None


def test_cli_decline_does_not_construct_adapter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    monkeypatch.setattr("openfoia.config.load_config", lambda: OpenFOIAConfig())
    monkeypatch.setattr("openfoia.cli._check_tor_or_exit", lambda policy: None)
    monkeypatch.setattr("typer.confirm", lambda prompt: False)

    class ExplodingAdapter(SECEdgarAdapter):
        def __init__(self, **kwargs):
            raise AssertionError("adapter must not be constructed after decline")

    monkeypatch.setattr("openfoia.records.sec_edgar.SECEdgarAdapter", ExplodingAdapter)
    result = runner.invoke(app, ["records", "filings", "ABC"])
    assert result.exit_code == 0
    assert "Nothing left your machine" in result.output


def test_cli_yes_forwards_policy_and_reports_source_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    config = OpenFOIAConfig()
    config.network.tor = True
    monkeypatch.setattr("openfoia.config.load_config", lambda: config)
    monkeypatch.setattr("openfoia.cli._check_tor_or_exit", lambda policy: None)
    seen: dict[str, object] = {}

    class FakeAdapter(SECEdgarAdapter):
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def filings(self, company, **kwargs):  # type: ignore[no-untyped-def]
            return SearchResult(
                source="sec", query=company, total_results=0, entities=[], error="offline"
            )

    monkeypatch.setattr("openfoia.records.sec_edgar.SECEdgarAdapter", FakeAdapter)
    result = runner.invoke(app, ["records", "filings", "ABC", "--yes"])
    assert result.exit_code == 1
    assert "SEC source error: offline" in result.output
    assert getattr(seen["egress"], "is_tor", False) is True


def _shard_adapter(files: list[dict]) -> type[SECEdgarAdapter]:
    class FakeAdapter(SECEdgarAdapter):
        def __init__(self) -> None:
            super().__init__(egress=EgressPolicy(mode=EgressMode.DIRECT))
            self.calls: list[str] = []

        async def _request(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(url)
            if url.endswith("CIK0000000123.json"):
                return {
                    "name": "ABC Corp",
                    "filings": {
                        "recent": {
                            "accessionNumber": ["0000000001-25-000001"],
                            "filingDate": ["2025-01-02"],
                            "form": ["10-K"],
                        },
                        "files": files,
                    },
                }
            return {
                "accessionNumber": ["0000000001-99-000001"],
                "filingDate": ["1999-01-01"],
                "form": ["10-K"],
            }

    return FakeAdapter


def test_archive_shards_are_paced_to_respect_sec_fair_access(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Shard walks must not burst past SEC's documented request ceiling."""

    slept: list[float] = []

    async def record_sleep(duration: float) -> None:
        slept.append(duration)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    files = [{"name": f"CIK0000000123-submissions-00{n}.json"} for n in (1, 2, 3)]
    adapter = _shard_adapter(files)()

    result = asyncio.run(adapter.filings("123"))

    assert len([url for url in adapter.calls if "submissions-" in url]) == 3
    assert slept == [SEC_REQUEST_DELAY] * 3
    assert len(result.entities) == 4


def test_shards_entirely_before_since_are_not_fetched() -> None:
    """A dated shard that cannot satisfy --since must not cost a request."""

    files = [
        {"name": "CIK0000000123-submissions-001.json", "filingTo": "1999-12-31"},
        {"name": "CIK0000000123-submissions-002.json", "filingTo": "2024-12-31"},
        {"name": "CIK0000000123-submissions-003.json"},
    ]
    adapter = _shard_adapter(files)()

    result = asyncio.run(adapter.filings("123", since="2000-01-01"))

    fetched = [url for url in adapter.calls if "submissions-" in url]
    assert "submissions-001" not in " ".join(fetched)
    assert len(fetched) == 2
    # Only the 2025 filing clears --since; the undated shards' 1999 rows do not.
    assert [e.extra_data["filing_date"] for e in result.entities] == ["2025-01-02"]
