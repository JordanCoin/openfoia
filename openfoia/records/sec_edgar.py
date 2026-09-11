"""SEC EDGAR adapter for public company filings.

Uses the EDGAR full-text search API (EFTS) which requires no API key.
https://efts.sec.gov/LATEST/search-index?q=<query>
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .base import AdapterRequestError, RecordAdapter, RecordEntity, SearchResult

EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"
EDGAR_FILING_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"

#: SEC requires a descriptive User-Agent. Keep it generic and overridable —
#: announcing "OpenFOIA" tells a government endpoint that the requester is
#: using an adversarial-journalism tool.
DEFAULT_USER_AGENT = "Research Client/1.0 (contact: research@example.org)"


def _edgar_headers() -> dict[str, str]:
    """Headers for EDGAR requests, honouring OPENFOIA_SEC_USER_AGENT."""
    import os

    return {"User-Agent": os.environ.get("OPENFOIA_SEC_USER_AGENT", DEFAULT_USER_AGENT)}


class SECEdgarAdapter(RecordAdapter):
    """Adapter for SEC EDGAR full-text search."""

    source_name = "sec"

    @staticmethod
    def normalize_cik(value: str) -> str:
        """Validate and return a CIK in SEC's ten-digit representation."""
        value = value.strip()
        if not re.fullmatch(r"\d{1,10}", value):
            raise ValueError("CIK must contain 1-10 decimal digits")
        return value.zfill(10)

    async def resolve_ticker(self, ticker: str) -> tuple[str, str]:
        """Resolve a ticker to ``(ten_digit_cik, company_name)``."""
        ticker = ticker.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker):
            raise ValueError("ticker must be 1-10 letters, digits, periods, or hyphens")
        data = await self._request(SEC_TICKERS_URL, headers=_edgar_headers())
        if not isinstance(data, dict):
            raise AdapterRequestError("SEC ticker response was not a JSON object")
        rows = data.values()
        for row in rows:
            if not isinstance(row, dict):
                raise AdapterRequestError("SEC ticker response contained a non-object row")
            if str(row.get("ticker", "")).upper() != ticker:
                continue
            try:
                cik = self.normalize_cik(str(row.get("cik_str", "")))
            except ValueError as exc:
                raise AdapterRequestError("SEC ticker response contained an invalid CIK") from exc
            return cik, str(row.get("title") or ticker)
        raise ValueError(f"ticker '{ticker}' was not found in SEC company_tickers.json")

    @staticmethod
    def _validate_forms(forms: str | None) -> set[str] | None:
        if not forms:
            return None
        values = {part.strip().upper() for part in forms.split(",")}
        if not values or any(
            not re.fullmatch(r"[A-Z0-9](?:[A-Z0-9./ -]*[A-Z0-9])?", value) for value in values
        ):
            raise ValueError("forms must be a comma-separated list such as 10-K,10-Q,8-K")
        return values

    @staticmethod
    def _filing_rows(block: dict[str, Any], *, allow_empty: bool = True) -> list[dict[str, Any]]:
        """Validate and transpose SEC's column-oriented metadata into rows."""
        if not block:
            if not allow_empty:
                raise AdapterRequestError("SEC archive filing metadata block was empty")
            return []
        required = ("accessionNumber", "filingDate", "form")
        if any(key not in block or not isinstance(block[key], list) for key in required):
            raise AdapterRequestError("SEC filing metadata block is missing a required column")
        length = len(block[required[0]])
        if length == 0 and not allow_empty:
            raise AdapterRequestError("SEC archive filing metadata block was empty")
        if any(not isinstance(value, list) or len(value) != length for value in block.values()):
            raise AdapterRequestError("SEC filing metadata columns have unequal lengths")
        return [
            {key: value[index] for key, value in block.items() if isinstance(value, list)}
            for index in range(length)
        ]

    @staticmethod
    def _filing_entity(
        filing: dict[str, Any], *, cik: str, company_name: str
    ) -> RecordEntity | None:
        if not isinstance(filing, dict):
            raise AdapterRequestError("SEC filing row was not an object")
        accession = (
            filing["accessionNumber"] if "accessionNumber" in filing else filing.get("accession_no")
        )
        filing_date = filing["filingDate"] if "filingDate" in filing else filing.get("file_date")
        form = filing["form"] if "form" in filing else filing.get("form_type")
        if (
            not isinstance(accession, str)
            or not isinstance(filing_date, str)
            or not isinstance(form, str)
        ):
            raise AdapterRequestError("SEC filing row contained non-string metadata")
        if (
            not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", filing_date)
            or not re.fullmatch(r"[A-Z0-9](?:[A-Z0-9./ -]*[A-Z0-9])?", form.upper())
        ):
            raise AdapterRequestError("SEC filing row contained invalid metadata")
        try:
            date.fromisoformat(filing_date)
        except ValueError as exc:
            raise AdapterRequestError("SEC filing row contained an invalid filing date") from exc
        primary_value = filing.get("primaryDocument")
        if primary_value is not None and not isinstance(primary_value, str):
            raise AdapterRequestError("SEC filing row contained a non-string primary document")
        primary = primary_value or ""
        # SEC metadata is remote input; only a plain archive basename may be
        # interpolated into a URL.  Suspicious values use the safe index URL.
        safe_primary = (
            primary
            if primary not in {".", ".."} and re.fullmatch(r"[A-Za-z0-9._-]+", primary)
            else ""
        )
        clean_accession = accession.replace("-", "")
        numeric_cik = str(int(cik))
        if safe_primary:
            url = f"{EDGAR_FILING_BASE}/{numeric_cik}/{clean_accession}/{safe_primary}"
        else:
            url = f"{EDGAR_FILING_BASE}/{numeric_cik}/{clean_accession}/{accession}-index.htm"
        return RecordEntity(
            entity_type="ORGANIZATION",
            name=company_name,
            source="sec",
            source_url=url,
            jurisdiction="us_federal",
            identifiers={"cik": cik, "accession_number": accession},
            extra_data={
                "filing_type": form,
                "filing_date": filing_date,
                "primary_document": primary,
            },
        )

    async def filings(
        self,
        company: str,
        *,
        since: str | None = None,
        forms: str | None = None,
    ) -> SearchResult:
        """Return a company's complete recent and archived filing history."""
        if since:
            try:
                since_date = date.fromisoformat(since)
            except ValueError as exc:
                raise ValueError("since must be an ISO date in YYYY-MM-DD format") from exc
        else:
            since_date = None
        form_filter = self._validate_forms(forms)
        company = company.strip()
        if not company:
            raise ValueError("ticker or CIK is required")

        try:
            if re.fullmatch(r"\d{1,10}", company):
                cik = self.normalize_cik(company)
                company_name = cik
            else:
                cik, company_name = await self.resolve_ticker(company)
            root_url = f"{SEC_SUBMISSIONS_BASE}/CIK{cik}.json"
            root = await self._request(root_url, headers=_edgar_headers())
            if not isinstance(root, dict):
                raise AdapterRequestError("SEC submissions response was not a JSON object")
            submissions = root.get("filings")
            if not isinstance(submissions, dict):
                raise AdapterRequestError("SEC submissions filings block was not a JSON object")
            if "recent" not in submissions or "files" not in submissions:
                raise AdapterRequestError(
                    "SEC submissions filings block was missing recent or files"
                )
            filings: list[dict[str, Any]] = []
            recent = submissions["recent"]
            if not isinstance(recent, dict):
                raise AdapterRequestError("SEC submissions recent block was not a JSON object")
            if isinstance(recent, dict):
                filings.extend(self._filing_rows(recent))
            archive_files = submissions["files"]
            if not isinstance(archive_files, list):
                raise AdapterRequestError("SEC submissions files block was not a list")
            archive_pattern = re.compile(rf"CIK{re.escape(cik)}-submissions-\d{{3}}\.json")
            for archive in archive_files:
                if not isinstance(archive, dict) or not isinstance(archive.get("name"), str):
                    raise AdapterRequestError("SEC submissions archive entry was malformed")
                name = archive["name"]
                if not archive_pattern.fullmatch(name):
                    raise AdapterRequestError("SEC submissions archive name was malformed")
                archived = await self._request(
                    f"{SEC_SUBMISSIONS_BASE}/{name}", headers=_edgar_headers()
                )
                if not isinstance(archived, dict):
                    raise AdapterRequestError(f"SEC archive {name} was not a JSON object")
                block = archived.get("filings", archived)
                if not isinstance(block, dict):
                    raise AdapterRequestError(f"SEC archive {name} filings block was invalid")
                filings.extend(self._filing_rows(block, allow_empty=False))
            if isinstance(root.get("name"), str) and root["name"]:
                company_name = root["name"]
            entities = []
            for filing in filings:
                entity = self._filing_entity(filing, cik=cik, company_name=company_name)
                filing_date = entity.extra_data["filing_date"]
                if since_date and filing_date < since_date.isoformat():
                    continue
                if form_filter and entity.extra_data["filing_type"].upper() not in form_filter:
                    continue
                entities.append(entity)
            entities.sort(key=lambda entity: entity.extra_data["filing_date"], reverse=True)
            return SearchResult(
                source=self.source_name,
                query=company,
                total_results=len(entities),
                entities=entities,
                raw_response=root,
            )
        except AdapterRequestError as exc:
            return self._failed(company, str(exc))

    async def search(self, query: str, **kwargs: Any) -> SearchResult:
        """Search SEC EDGAR filings by keyword.

        Args:
            query: Search term (company name, keyword, etc.)
            filing_type: Optional filing type filter (e.g. "10-K", "10-Q", "8-K").
            page: Page number starting from 1 (default 1).

        Returns:
            SearchResult with filing entities.
        """
        params: dict[str, Any] = {"q": query}

        filing_type = kwargs.get("filing_type")
        if filing_type:
            params["forms"] = filing_type

        page = kwargs.get("page", 1)
        per_page = 25
        params["from"] = (page - 1) * per_page
        params["size"] = per_page

        try:
            data = await self._request(EFTS_BASE, params=params, headers=_edgar_headers())
        except AdapterRequestError as exc:
            return self._failed(query, str(exc), page=page, per_page=per_page)

        hits = data.get("hits", {})
        total = (
            hits.get("total", {}).get("value", 0)
            if isinstance(hits.get("total"), dict)
            else hits.get("total", 0)
        )
        hit_list = hits.get("hits", [])

        entities: list[RecordEntity] = []
        for hit in hit_list:
            entity = self._parse_filing(hit)
            if entity:
                entities.append(entity)

        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=total,
            entities=entities,
            page=page,
            per_page=per_page,
            raw_response=data,
        )

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Fetch a specific filing by accession number.

        Args:
            identifier: SEC accession number (e.g. "0001193125-21-123456").

        Returns:
            RecordEntity if found, None otherwise.
        """
        # Search by accession number
        params = {"q": f'"{identifier}"', "size": 1}

        try:
            data = await self._request(EFTS_BASE, params=params, headers=_edgar_headers())
        except AdapterRequestError:
            return None

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            return None

        return self._parse_filing(hits[0])

    def _parse_filing(self, hit: dict[str, Any]) -> RecordEntity | None:
        """Parse an EFTS hit into a RecordEntity."""
        source = hit.get("_source", {})

        display_names = source.get("display_names", [])
        company_name = display_names[0] if display_names else source.get("entity_name", "")

        if not company_name:
            return None

        form_type = source.get("form_type", source.get("file_type", ""))
        filing_date = source.get("file_date", source.get("period_of_report", ""))
        file_number = source.get("file_num", "")

        # Build filing URL
        accession = source.get("accession_no", "")
        cik = source.get("entity_id", "")
        filing_url = ""
        if accession and cik:
            clean_accession = accession.replace("-", "")
            filing_url = f"{EDGAR_FILING_BASE}/{cik}/{clean_accession}/{accession}-index.htm"

        identifiers: dict[str, str] = {}
        if accession:
            identifiers["accession_number"] = accession
        if cik:
            identifiers["cik"] = str(cik)
        if file_number:
            identifiers["file_number"] = file_number

        return RecordEntity(
            entity_type="ORGANIZATION",
            name=company_name,
            source=self.source_name,
            source_url=filing_url or None,
            jurisdiction="us_federal",
            status=None,
            identifiers=identifiers,
            extra_data={
                "filing_type": form_type,
                "filing_date": filing_date,
                "display_names": display_names,
            },
        )
