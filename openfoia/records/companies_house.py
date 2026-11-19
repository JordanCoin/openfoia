"""UK Companies House adapter: companies, directors, owners (PSC) and filing history.

Uses the Companies House Public Data API (https://api.company-information.service.gov.uk).
Every call needs a free API key, sent with HTTP Basic authentication as the
username with an empty password. Without one the API answers HTTP 401
``{"error": "Empty Authorization header", "type": "ch:service"}`` (tried
unauthenticated on 2026-11-19).

Read on 2026-11-19:

- https://developer.company-information.service.gov.uk/authentication: Basic
  auth, key as username, password ignored.
- https://developer.company-information.service.gov.uk/developer-guidelines:
  "You can make up to 600 requests within a 5 minute period", then HTTP 429
  "for each request made within the rest of the 5 minute time frame"; Companies
  House "reserve[s] the right to ban without notice applications that
  regularly exceed or attempt to bypass the rate limits". Keys should live in
  environment variables, not the source tree.
- https://developer.company-information.service.gov.uk/get-started: a
  Companies House user account is needed to create an application and key.
- The Swagger 2.0 specification at
  https://developer-specs.company-information.service.gov.uk/api.ch.gov.uk-specifications/swagger-2.0/spec/swagger.json
  and the files it references (search.json, companyProfile.json,
  companyOfficerList.json, psc.json, pscModels.json, filingHistory.json,
  officerAppointmentList.json): the paths, parameters and response fields used
  here. The specification has no example bodies, and no call was made with a
  key, so the test fixtures are written from those schemas, not recorded.

The key is read from ``OPENFOIA_COMPANIES_HOUSE_API_KEY`` only, never from
config.json. It belongs to the user's Companies House account (an email
address), so it identifies them to Companies House and is never sent over Tor;
without it the API answers nothing, so Companies House is not used over Tor.

A company fetch is four requests (profile, officers, persons with significant
control, filing history); an officer fetch is one. Requests are counted locally
against the 600-per-5-minutes limit (rolling, which is stricter than Companies
House's fixed window), waiting for up to a minute and otherwise stopping before
anything is sent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

from . import keyed_api
from .base import AdapterRequestError, RecordAdapter, RecordEntity, SearchResult
from .ftm_store import import_ftm_entities
from .keyed_api import (
    MAX_WAIT,
    RequestBudget,
    clean_line,
    error_detail,
    no_markup,
    read_capped,
    when,
)
from .store import store_text_document

logger = logging.getLogger(__name__)

API_HOST = "api.company-information.service.gov.uk"
API_BASE = f"https://{API_HOST}"
SITE = "https://find-and-update.company-information.service.gov.uk"
DOCUMENT_API = "https://document-api.company-information.service.gov.uk"
KEY_ENV = "OPENFOIA_COMPANIES_HOUSE_API_KEY"
BUDGET_ENV = "OPENFOIA_COMPANIES_HOUSE_IGNORE_BUDGET"
KEY_URL = "https://developer.company-information.service.gov.uk/how-to-create-an-application"

WINDOW_LIMIT = 600
BUDGET = RequestBudget(
    provider="Companies House",
    filename="companies_house_budget.json",
    limits=((300, WINDOW_LIMIT, "5 minutes"),),
    ignore_env=BUDGET_ENV,
)
#: Requests one company fetch uses: profile, officers, PSC, filing history.
COMPANY_FETCH_REQUESTS = 4

PAGE_MAX = 100
LIST_PAGE = 100  # officers, PSCs and filings read per company fetch (one page each)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

NO_KEY_MESSAGE = (
    "Companies House needs a free API key: it refuses anonymous calls (HTTP 401). Register "
    f"a Companies House account, create an application and an API key ({KEY_URL}) and set "
    f"{KEY_ENV}. The key belongs to your account, so it identifies you to Companies House."
)
TOR_KEY_MESSAGE = (
    "Companies House needs your personal API key, which would identify you, so it is not "
    "used over Tor (it refuses calls without a key). Turn off network.tor to query it directly."
)

SEARCH_TYPES = {
    "company": "company",
    "companies": "company",
    "officer": "officer",
    "officers": "officer",
    "director": "officer",
    "directors": "officer",
}
TYPE_CHOICES = "company (default), officer"

_NUMBER = re.compile(r"^[A-Z0-9]{8}$")
_OFFICER_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_OFFICER_PATH = re.compile(r"^/officers/([A-Za-z0-9_-]{6,64})/appointments/?$")
_PSC_PATH = re.compile(
    r"^/company/[A-Z0-9]{8}/persons-with-significant-control/[a-z-]{3,60}/([A-Za-z0-9_-]{4,80})/?$"
)
_DOCUMENT_URL = re.compile(
    r"^https://document-api\.company-information\.service\.gov\.uk/document/[A-Za-z0-9_-]{1,100}$"
)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Identifiers (pure)
# ---------------------------------------------------------------------------


def parse_identifier(identifier: str) -> tuple[str, str]:
    """``("company", "00000006")`` or ``("officer", "<id>")`` for a fetch identifier.

    Accepts a company number (digits are zero-padded to 8: ``6`` is
    ``00000006``; ``SC123456``), ``officer:<id>``, or a
    find-and-update.company-information.service.gov.uk company or officer
    URL. Raises ValueError for anything else, so nothing unchecked reaches a
    request path.
    """
    raw = str(identifier or "").strip()
    if raw.lower().startswith(("https://", "http://")):
        parts = urlsplit(raw)
        if parts.scheme != "https" or parts.hostname != urlsplit(SITE).hostname or parts.port:
            raise ValueError(f"Not a Companies House URL: {clean_line(raw)!r}.")
        m = re.fullmatch(r"/company/([A-Za-z0-9]{1,8})(?:/.*)?", parts.path or "")
        if m:
            raw = m.group(1)
        else:
            m = _OFFICER_PATH.fullmatch(parts.path or "")
            if not m:
                raise ValueError(
                    f"Not a Companies House company or officer URL: {clean_line(raw)!r}."
                )
            return "officer", m.group(1)
    kind, sep, rest = raw.partition(":")
    if sep:
        if kind.lower() != "officer" or not _OFFICER_ID.match(rest):
            raise ValueError(
                f"Not a Companies House id: {clean_line(raw)!r}. Use a company number "
                "(00000006, SC123456) or officer:<id>."
            )
        return "officer", rest
    number = re.sub(r"\s+", "", raw).upper()
    if number.isascii() and number.isdigit() and len(number) <= 8:
        number = number.zfill(8)
    if not _NUMBER.match(number):
        raise ValueError(
            f"Not a Companies House company number: {clean_line(raw)!r} (expected 8 letters "
            "and digits, e.g. 00000006 or SC123456; or officer:<id>)."
        )
    return "company", number


def canonical_id(identifier: str) -> str:
    """The id stored in a request number: ``00000006`` or ``officer:<id>``."""
    kind, value = parse_identifier(identifier)
    return value if kind == "company" else f"officer:{value}"


def officer_id_from_link(path: Any) -> str | None:
    """The officer id in an API ``/officers/<id>/appointments`` link, or None.

    Links come from the response body; anything that is not exactly that path
    (another host, traversal, a query) is refused.
    """
    if not isinstance(path, str):
        return None
    m = _OFFICER_PATH.fullmatch(path)
    return m.group(1) if m else None


def document_url(value: Any) -> str | None:
    """A Document API metadata link if it is exactly one, else None (shown, never fetched here)."""
    return value if isinstance(value, str) and _DOCUMENT_URL.fullmatch(value) else None


def search_type(value: Any) -> str:
    key = str(value or "company").strip().lower()
    if key not in SEARCH_TYPES:
        raise ValueError(
            f"Unknown Companies House search type {clean_line(value)!r}; use {TYPE_CHOICES}."
        )
    return SEARCH_TYPES[key]


def _date(value: Any) -> str | None:
    text = clean_line(value, 20)
    return text if _DATE.match(text) else None


def _dob(value: Any) -> str | None:
    if isinstance(value, dict):
        year, month = value.get("year"), value.get("month")
        if isinstance(year, int) and isinstance(month, int) and 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    return None


def _address(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    keys = ("care_of", "premises", "address_line_1", "address_line_2", "locality", "region")
    parts = [clean_line(value.get(k), 120) for k in (*keys, "postal_code", "country")]
    return ", ".join(p for p in parts if p)


def _words(value: Any) -> str:
    """An enumeration key (``corporate-director``) as words."""
    return clean_line(value, 120).replace("-", " ")


def _hash_id(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CompaniesHouseAdapter(RecordAdapter):
    """Search the UK company register and fetch a company's officers, owners and filings."""

    source_name = "companieshouse"

    def __init__(self, api_key: str | None = None, *, egress=None):
        super().__init__(egress=egress)
        # Environment only: a personal key never goes in config.json.
        self._api_key = self._personal_credential(api_key or os.environ.get(KEY_ENV))

    #: Shown by ``openfoia search`` before the query is sent.
    search_notice = (
        "sends your API key, which belongs to your Companies House account and identifies "
        f"you to Companies House. Counts against {WINDOW_LIMIT} requests per 5 minutes."
    )

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def unavailable_reason(self) -> str | None:
        if self._api_key:
            return None
        if self.credentials_withheld:
            return "personal API key not sent over Tor"
        return f"no {KEY_ENV} (Companies House refuses anonymous calls)"

    def missing_key_message(self) -> str:
        return TOR_KEY_MESSAGE if self.credentials_withheld else NO_KEY_MESSAGE

    def _auth(self) -> str:
        return "Basic " + base64.b64encode(f"{self._api_key}:".encode()).decode()

    def _redact(self, text: object) -> str:
        out = no_markup(clean_line(text, 500))
        if self._api_key:
            out = out.replace(self._api_key, "<redacted>").replace(self._auth(), "<redacted>")
        return out

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict | None:
        """GET an API path built here (never a URL from a response) as JSON; None on 404."""
        if not self._api_key:
            raise AdapterRequestError(self.missing_key_message())
        if not path.startswith("/") or ".." in path or "//" in path:
            raise AdapterRequestError(f"Refusing Companies House path {clean_line(path)!r}.")
        headers = {"Authorization": self._auth(), "Accept": "application/json"}
        waited = False
        while True:
            await BUDGET.reserve()
            try:
                status, body, retry = await read_capped(
                    self._egress,
                    API_BASE + path,
                    params=params,
                    headers=headers,
                    max_bytes=MAX_RESPONSE_BYTES,
                    label="Companies House",
                )
            except AdapterRequestError:
                raise
            except Exception as exc:
                raise AdapterRequestError(
                    f"Companies House request failed: {type(exc).__name__}: {self._redact(exc)}"
                ) from exc
            if status != 429:
                break
            if retry is not None and retry <= MAX_WAIT and not waited:
                waited = True
                logger.warning("Companies House rate limit (HTTP 429); waiting %.0fs.", retry)
                await keyed_api._sleep(retry)  # the module attribute: patched in tests
                continue
            # Companies House answers 429 for the rest of its 5-minute window.
            delay = retry if retry is not None else 300.0
            BUDGET.block_until(time.time() + delay)
            raise AdapterRequestError(
                f"Companies House rate limit reached (HTTP 429){self._detail(body)}: "
                f"{WINDOW_LIMIT} requests per 5 minutes. Try again after "
                f"{when(time.time() + delay, time.time())}. Applications that keep exceeding "
                "the limit can be banned."
            )
        if status == 404:
            return None
        if status == 401:
            raise AdapterRequestError(
                f"Companies House rejected the API key (HTTP 401){self._detail(body)}. Check "
                f"{KEY_ENV}; it must be a REST API key, not a stream key."
            )
        if status == 403:
            raise AdapterRequestError(
                f"Companies House refused access (HTTP 403){self._detail(body)}. A key "
                "restricted to other IP addresses or domains is refused."
            )
        if status >= 400:
            raise AdapterRequestError(
                f"Companies House API error (HTTP {status}){self._detail(body)}."
            )
        if status >= 300:
            raise AdapterRequestError(
                f"Companies House answered with a redirect (HTTP {status}); not followed, so "
                f"the key is only ever sent to {API_HOST}."
            )
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise AdapterRequestError(
                "Companies House returned something that is not JSON."
            ) from exc
        if not isinstance(data, dict):
            raise AdapterRequestError("Companies House returned an unexpected JSON shape.")
        return data

    def _detail(self, body: bytes) -> str:
        detail = error_detail(body)
        if self._api_key:
            detail = detail.replace(self._api_key, "<redacted>")
        return f": {detail}" if detail else ""

    # -- search ------------------------------------------------------------

    async def search(
        self, query: str, page: int = 1, page_size: int = 25, **kwargs: Any
    ) -> SearchResult:
        """One page of company (default) or officer search: one request.

        ``search_type`` or ``filing_type`` (the CLI's ``--type``): company or officer.
        """
        try:
            stype = search_type(kwargs.get("search_type") or kwargs.get("filing_type"))
        except ValueError as exc:
            return self._failed(query, str(exc), page=page, per_page=page_size)
        if not (query or "").strip():
            return self._failed(query, "Give a search term for Companies House.", page=page)
        per_page = max(1, min(int(page_size), PAGE_MAX))
        params = {
            "q": query,
            "items_per_page": per_page,
            "start_index": (max(1, page) - 1) * per_page,
        }
        path = "/search/companies" if stype == "company" else "/search/officers"
        try:
            data = await self._get(path, params)
        except AdapterRequestError as exc:
            return self._failed(query, str(exc), page=page, per_page=per_page)
        if data is None:
            return self._failed(query, "Companies House search endpoint not found (HTTP 404).")
        items = data.get("items", [])
        if not isinstance(items, list):
            return self._failed(query, "Companies House search returned no result list.")
        build = _company_hit if stype == "company" else _officer_hit
        entities = [e for e in (build(i) for i in items if isinstance(i, dict)) if e]
        total = data.get("total_results")
        total = total if isinstance(total, int) and total >= 0 else len(entities)
        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=total,
            entities=entities,
            page=page,
            per_page=per_page,
            raw_response={"total": total, "type": stype},
        )

    # -- fetch -------------------------------------------------------------

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """A company (profile, officers, owners, filings) or an officer's appointments.

        None if Companies House has no such company or officer. Raises
        ValueError for a malformed id and AdapterRequestError when the profile
        or appointment list cannot be read; a company whose officers, PSCs or
        filings fail still returns, with the failures in ``extra_data["errors"]``.
        """
        kind, value = parse_identifier(identifier)
        if kind == "officer":
            return await self._fetch_officer(value)
        return await self._fetch_company(value)

    async def _list(self, path: str, params: dict[str, Any], what: str, errors: list[str]):
        try:
            page = await self._get(path, params)
        except AdapterRequestError as exc:
            errors.append(f"{what} not retrieved: {exc}")
            return [], 0
        if page is None:  # Companies House answers 404 when a company has none
            return [], 0
        items = [i for i in page.get("items") or [] if isinstance(i, dict)]
        total = page.get("total_results", page.get("total_count"))
        total = total if isinstance(total, int) else len(items)
        if total > len(items):
            errors.append(f"{total} {what} on Companies House; the first {len(items)} are shown")
        return items, total

    async def _fetch_company(self, number: str) -> RecordEntity | None:
        base = f"/company/{quote(number, safe='')}"
        profile = await self._get(base)
        if profile is None:
            return None
        errors: list[str] = []
        officers, _ = await self._list(
            f"{base}/officers", {"items_per_page": LIST_PAGE}, "officers", errors
        )
        pscs, _ = await self._list(
            f"{base}/persons-with-significant-control",
            {"items_per_page": LIST_PAGE, "start_index": 0, "register_view": "false"},
            "persons with significant control",
            errors,
        )
        filings, _ = await self._list(
            f"{base}/filing-history", {"items_per_page": LIST_PAGE}, "filings", errors
        )
        entity = _company_entity(profile, number)
        x = entity.extra_data
        x.update(
            officers=[_officer(o) for o in officers],
            pscs=[_psc(p) for p in pscs],
            filings=[_filing(f) for f in filings],
            errors=errors,
            text_kind="companieshouse",
        )
        refused = sum(1 for f in x["filings"] if f.pop("document_refused"))
        if refused:
            errors.append(f"{refused} filing document link(s) refused: not the Document API")
        x["ftm_entities"] = _company_ftm(entity)
        x["full_text"] = _company_text(entity)
        x["text_length"] = len(x["full_text"])
        return entity

    async def _fetch_officer(self, officer_id: str) -> RecordEntity | None:
        data = await self._get(
            f"/officers/{quote(officer_id, safe='')}/appointments", {"items_per_page": 50}
        )
        if data is None:
            return None
        errors: list[str] = []
        items = [i for i in data.get("items") or [] if isinstance(i, dict)]
        total = data.get("total_results")
        if isinstance(total, int) and total > len(items):
            errors.append(
                f"{total} appointments on Companies House; the first {len(items)} are shown"
            )
        appointments = [_appointment(a) for a in items]
        name = clean_line(data.get("name")) or next(
            (a["name"] for a in appointments if a["name"]), f"Officer {officer_id}"
        )
        entity = RecordEntity(
            entity_type="ORGANIZATION" if data.get("is_corporate_officer") else "PERSON",
            name=name,
            source="companieshouse",
            source_url=f"{SITE}/officers/{officer_id}/appointments",
            jurisdiction="gb",
            identifiers={"officer": f"officer:{officer_id}"},
            extra_data={
                "date_of_birth": _dob(data.get("date_of_birth")),
                "appointments": appointments,
                "errors": errors,
                "text_kind": "companieshouse",
            },
        )
        entity.extra_data["ftm_entities"] = _officer_ftm(entity, officer_id)
        entity.extra_data["full_text"] = _officer_text(entity)
        entity.extra_data["text_length"] = len(entity.extra_data["full_text"])
        return entity

    def store(
        self, entity: RecordEntity, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        """Save the record as a local Document (CH-<id>) and import its people and companies."""
        ident = next(iter(entity.identifiers.values()))
        doc_id, text = store_text_document(
            entity,
            request_number=f"CH-{ident}",
            filename=f"companieshouse-{ident.replace(':', '-')}.txt",
            body=f"Pulled from Companies House: {entity.source_url}",
            case_id=case_id,
        )
        ftm = entity.extra_data.get("ftm_entities") or []
        if doc_id and ftm:
            added, links = import_ftm_entities(
                ftm,
                name=f"companieshouse-{ident.replace(':', '-')}",
                document_id=doc_id,
                tag="companieshouse",
            )
            entity.extra_data["imported"] = {"entities": added, "relationships": links}
        return doc_id, text

    async def pull_text(
        self, identifier: str, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        entity = await self.fetch(identifier)
        if not entity:
            return None, None
        return self.store(entity, case_id)


# ---------------------------------------------------------------------------
# Response mapping (every string cleaned; ids checked before they reach a URL)
# ---------------------------------------------------------------------------


def _company_number(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if _NUMBER.match(text) else None


def _company_hit(item: dict[str, Any]) -> RecordEntity | None:
    number = _company_number(item.get("company_number"))
    if not number:
        return None
    return RecordEntity(
        entity_type="ORGANIZATION",
        name=clean_line(item.get("title")) or f"Company {number}",
        source="companieshouse",
        source_url=f"{SITE}/company/{number}",
        jurisdiction="gb",
        status=clean_line(item.get("company_status"), 40) or None,
        identifiers={"company_number": number},
        extra_data={
            "company_type": _words(item.get("company_type")) or None,
            "date": _date(item.get("date_of_creation")),
            "date_of_cessation": _date(item.get("date_of_cessation")),
            "address": clean_line(item.get("address_snippet")) or _address(item.get("address")),
            "snippet": clean_line(item.get("description") or item.get("snippet"), 300),
        },
    )


def _officer_hit(item: dict[str, Any]) -> RecordEntity | None:
    links = item.get("links") if isinstance(item.get("links"), dict) else {}
    officer_id = officer_id_from_link(links.get("self"))
    if not officer_id:
        return None
    count = item.get("appointment_count")
    return RecordEntity(
        entity_type="PERSON",
        name=clean_line(item.get("title")) or f"Officer {officer_id}",
        source="companieshouse",
        source_url=f"{SITE}/officers/{officer_id}/appointments",
        jurisdiction="gb",
        identifiers={"officer": f"officer:{officer_id}"},
        extra_data={
            "appointment_count": count if isinstance(count, int) else None,
            "date_of_birth": _dob(item.get("date_of_birth")),
            "address": clean_line(item.get("address_snippet")) or _address(item.get("address")),
            "snippet": clean_line(item.get("description") or item.get("snippet"), 300),
        },
    )


def _company_entity(profile: dict[str, Any], number: str) -> RecordEntity:
    previous = [
        {
            "name": clean_line(p.get("name")),
            "from": _date(p.get("effective_from")),
            "to": _date(p.get("ceased_on")),
        }
        for p in profile.get("previous_company_names") or []
        if isinstance(p, dict) and p.get("name")
    ]
    accounts = profile.get("accounts") if isinstance(profile.get("accounts"), dict) else {}
    confirmation = (
        profile.get("confirmation_statement")
        if isinstance(profile.get("confirmation_statement"), dict)
        else {}
    )
    return RecordEntity(
        entity_type="ORGANIZATION",
        name=clean_line(profile.get("company_name")) or f"Company {number}",
        source="companieshouse",
        source_url=f"{SITE}/company/{number}",
        jurisdiction="gb",
        status=clean_line(profile.get("company_status"), 40) or None,
        identifiers={"company_number": number},
        extra_data={
            "company_type": _words(profile.get("type")) or None,
            "uk_jurisdiction": _words(profile.get("jurisdiction")) or None,
            "status_detail": _words(profile.get("company_status_detail")) or None,
            "date": _date(profile.get("date_of_creation")),
            "date_of_cessation": _date(profile.get("date_of_cessation")),
            "address": _address(profile.get("registered_office_address")),
            "sic_codes": [
                clean_line(s, 10) for s in profile.get("sic_codes") or [] if isinstance(s, str)
            ][:20],
            "previous_names": previous[:50],
            "accounts_overdue": bool(accounts.get("overdue"))
            or bool((accounts.get("next_accounts") or {}).get("overdue")),
            "confirmation_overdue": bool(confirmation.get("overdue")),
            "has_charges": bool(profile.get("has_charges")),
            "has_insolvency_history": bool(profile.get("has_insolvency_history")),
        },
    )


def _officer(o: dict[str, Any]) -> dict[str, Any]:
    links = o.get("links") if isinstance(o.get("links"), dict) else {}
    officer_links = links.get("officer") if isinstance(links.get("officer"), dict) else {}
    role = clean_line(o.get("officer_role"), 60)
    return {
        "id": officer_id_from_link(officer_links.get("appointments")),
        "name": clean_line(o.get("name")),
        "role": role,
        "corporate": role.startswith("corporate"),
        "appointed_on": _date(o.get("appointed_on")),
        "resigned_on": _date(o.get("resigned_on")),
        "nationality": clean_line(o.get("nationality"), 80) or None,
        "occupation": clean_line(o.get("occupation"), 120) or None,
        "country_of_residence": clean_line(o.get("country_of_residence"), 80) or None,
        "date_of_birth": _dob(o.get("date_of_birth")),
    }


def _psc(p: dict[str, Any]) -> dict[str, Any]:
    links = p.get("links") if isinstance(p.get("links"), dict) else {}
    self_link = links.get("self") if isinstance(links.get("self"), str) else ""
    m = _PSC_PATH.fullmatch(self_link)
    kind = clean_line(p.get("kind"), 80)
    return {
        "id": m.group(1) if m else None,
        "name": clean_line(p.get("name"))
        or (
            "(super-secure person: details withheld by Companies House)"
            if "super-secure" in kind
            else ""
        ),
        "kind": kind,
        "natures_of_control": [
            _words(n) for n in p.get("natures_of_control") or [] if isinstance(n, str)
        ][:20],
        "notified_on": _date(p.get("notified_on")),
        "ceased_on": _date(p.get("ceased_on")),
        "nationality": clean_line(p.get("nationality"), 80) or None,
        "country_of_residence": clean_line(p.get("country_of_residence"), 80) or None,
        "date_of_birth": _dob(p.get("date_of_birth")),
    }


def _filing(f: dict[str, Any]) -> dict[str, Any]:
    links = f.get("links") if isinstance(f.get("links"), dict) else {}
    raw_doc = links.get("document_metadata")
    doc = document_url(raw_doc)
    values = f.get("description_values") if isinstance(f.get("description_values"), dict) else {}
    detail = "; ".join(
        f"{_words(k)}: {clean_line(v, 120)}"
        for k, v in list(values.items())[:6]
        if isinstance(v, str | int) and not isinstance(v, bool)
    )
    pages = f.get("pages")
    return {
        "transaction_id": clean_line(f.get("transaction_id"), 40) or None,
        "date": _date(f.get("date")),
        "category": _words(f.get("category")) or None,
        "type": clean_line(f.get("type"), 20) or None,
        "description": _words(f.get("description")) + (f" ({detail})" if detail else ""),
        "pages": pages if isinstance(pages, int) else None,
        "document_metadata": doc,
        "document_refused": bool(raw_doc) and doc is None,
    }


def _appointment(a: dict[str, Any]) -> dict[str, Any]:
    to = a.get("appointed_to") if isinstance(a.get("appointed_to"), dict) else {}
    return {
        "name": clean_line(a.get("name")),
        "company_name": clean_line(to.get("company_name")),
        "company_number": _company_number(to.get("company_number")),
        "company_status": clean_line(to.get("company_status"), 40) or None,
        "role": clean_line(a.get("officer_role"), 60),
        "appointed_on": _date(a.get("appointed_on")),
        "resigned_on": _date(a.get("resigned_on")),
        "nationality": clean_line(a.get("nationality"), 80) or None,
        "occupation": clean_line(a.get("occupation"), 120) or None,
    }


# -- FollowTheMoney ----------------------------------------------------------


def _company_ftm_id(number: str) -> str:
    return f"gb-coh-{number.lower()}"


def _company_ftm(entity: RecordEntity) -> list[dict[str, Any]]:
    """The company, its officers (Directorship) and owners (Ownership) as FtM entities."""
    x = entity.extra_data
    number = entity.identifiers["company_number"]
    company_id = _company_ftm_id(number)
    props: dict[str, list[str]] = {
        "name": [entity.name],
        "registrationNumber": [number],
        "jurisdiction": ["gb"],
    }
    if x.get("date"):
        props["incorporationDate"] = [x["date"]]
    if x.get("date_of_cessation"):
        props["dissolutionDate"] = [x["date_of_cessation"]]
    if entity.status:
        props["status"] = [entity.status]
    if x.get("address"):
        props["address"] = [x["address"]]
    if x.get("previous_names"):
        props["previousName"] = [p["name"] for p in x["previous_names"] if p["name"]]
    out = [{"id": company_id, "schema": "Company", "properties": props}]
    for o in x.get("officers") or []:
        if not o["name"]:
            continue
        pid = f"gb-coh-officer-{o['id'] or _hash_id(o['name'], o['date_of_birth'])}"
        person: dict[str, list[str]] = {"name": [o["name"]]}
        if not o["corporate"]:
            if o["nationality"]:
                person["nationality"] = [o["nationality"]]
            if o["date_of_birth"]:
                person["birthDate"] = [o["date_of_birth"]]
        out.append(
            {"id": pid, "schema": "Company" if o["corporate"] else "Person", "properties": person}
        )
        rel = {"director": [pid], "organization": [company_id], "role": [o["role"] or "officer"]}
        if o["appointed_on"]:
            rel["startDate"] = [o["appointed_on"]]
        if o["resigned_on"]:
            rel["endDate"] = [o["resigned_on"]]
        out.append(
            {
                "id": f"gb-coh-dir-{_hash_id(pid, company_id, o['role'], o['appointed_on'])}",
                "schema": "Directorship",
                "properties": rel,
            }
        )
    for p in x.get("pscs") or []:
        if not p["name"] or "super-secure" in p["kind"]:
            continue
        oid = f"gb-coh-psc-{p['id'] or _hash_id(p['name'], p['date_of_birth'])}"
        individual = p["kind"].startswith("individual")
        owner: dict[str, list[str]] = {"name": [p["name"]]}
        if individual and p["nationality"]:
            owner["nationality"] = [p["nationality"]]
        if individual and p["date_of_birth"]:
            owner["birthDate"] = [p["date_of_birth"]]
        out.append(
            {"id": oid, "schema": "Person" if individual else "LegalEntity", "properties": owner}
        )
        rel = {"owner": [oid], "asset": [company_id]}
        if p["natures_of_control"]:
            rel["role"] = ["; ".join(p["natures_of_control"])[:1000]]
        if p["notified_on"]:
            rel["startDate"] = [p["notified_on"]]
        if p["ceased_on"]:
            rel["endDate"] = [p["ceased_on"]]
        out.append(
            {
                "id": f"gb-coh-own-{_hash_id(oid, company_id, p['notified_on'])}",
                "schema": "Ownership",
                "properties": rel,
            }
        )
    return out


def _officer_ftm(entity: RecordEntity, officer_id: str) -> list[dict[str, Any]]:
    pid = f"gb-coh-officer-{officer_id}"
    x = entity.extra_data
    person: dict[str, list[str]] = {"name": [entity.name]}
    if x.get("date_of_birth") and entity.entity_type == "PERSON":
        person["birthDate"] = [x["date_of_birth"]]
    out = [
        {
            "id": pid,
            "schema": "Person" if entity.entity_type == "PERSON" else "Company",
            "properties": person,
        }
    ]
    for a in x.get("appointments") or []:
        if not a["company_number"]:
            continue
        cid = _company_ftm_id(a["company_number"])
        company = {
            "name": [a["company_name"] or a["company_number"]],
            "registrationNumber": [a["company_number"]],
            "jurisdiction": ["gb"],
        }
        if a["company_status"]:
            company["status"] = [a["company_status"]]
        out.append({"id": cid, "schema": "Company", "properties": company})
        rel = {"director": [pid], "organization": [cid], "role": [a["role"] or "officer"]}
        if a["appointed_on"]:
            rel["startDate"] = [a["appointed_on"]]
        if a["resigned_on"]:
            rel["endDate"] = [a["resigned_on"]]
        out.append(
            {
                "id": f"gb-coh-dir-{_hash_id(pid, cid, a['role'], a['appointed_on'])}",
                "schema": "Directorship",
                "properties": rel,
            }
        )
    return out


# -- stored text -------------------------------------------------------------


def _company_text(entity: RecordEntity) -> str:
    """A plain-text company record: what is stored locally and searched with ``docs grep``."""
    x = entity.extra_data
    lines = [
        f"Companies House company {entity.identifiers['company_number']}",
        f"Name: {entity.name}",
    ]
    for label, value in (
        ("Status", " / ".join(v for v in (entity.status, x.get("status_detail")) if v)),
        ("Type", x.get("company_type")),
        ("Jurisdiction", x.get("uk_jurisdiction")),
        ("Incorporated", x.get("date")),
        ("Ceased", x.get("date_of_cessation")),
        ("Registered office", x.get("address")),
        ("SIC codes", ", ".join(x.get("sic_codes") or [])),
    ):
        if value:
            lines.append(f"{label}: {value}")
    flags = [
        label
        for label, key in (
            ("accounts overdue", "accounts_overdue"),
            ("confirmation statement overdue", "confirmation_overdue"),
            ("has charges", "has_charges"),
            ("insolvency history", "has_insolvency_history"),
        )
        if x.get(key)
    ]
    if flags:
        lines.append("Flags: " + ", ".join(flags))
    for p in x.get("previous_names") or []:
        lines.append(f"Previous name: {p['name']} ({p['from'] or '?'} to {p['to'] or '?'})")
    lines.append(f"Source: {entity.source_url}")
    for err in x.get("errors") or []:
        lines.append(f"NOTE: {err}")

    lines += ["", "Officers"]
    for o in x.get("officers") or []:
        span = f"appointed {o['appointed_on'] or '?'}" + (
            f", resigned {o['resigned_on']}" if o["resigned_on"] else ""
        )
        more = ", ".join(
            v
            for v in (
                o["nationality"],
                o["occupation"],
                f"born {o['date_of_birth']}" if o["date_of_birth"] else None,
                f"officer:{o['id']}" if o["id"] else None,
            )
            if v
        )
        lines.append(f"- {o['name']} ({_words(o['role'])}; {span})" + (f" {more}" if more else ""))
    if not x.get("officers"):
        lines.append("- none listed")

    lines += ["", "Persons with significant control (owners)"]
    for p in x.get("pscs") or []:
        span = f"notified {p['notified_on'] or '?'}" + (
            f", ceased {p['ceased_on']}" if p["ceased_on"] else ""
        )
        control = "; ".join(p["natures_of_control"]) or "control not stated"
        lines.append(f"- {p['name']} ({_words(p['kind'])}; {span}): {control}")
    if not x.get("pscs"):
        lines.append("- none listed")

    lines += ["", "Filing history"]
    for f in x.get("filings") or []:
        pages = f", {f['pages']} pages" if f["pages"] else ""
        lines.append(
            f"- {f['date'] or '?'}  {f['type'] or '-'}  {f['category'] or '-'}: "
            f"{f['description']}{pages}"
        )
    if not x.get("filings"):
        lines.append("- none listed")
    return "\n".join(lines) + "\n"


def _officer_text(entity: RecordEntity) -> str:
    x = entity.extra_data
    lines = [
        f"Companies House officer {entity.identifiers['officer']}",
        f"Name: {entity.name}",
    ]
    if x.get("date_of_birth"):
        lines.append(f"Born: {x['date_of_birth']}")
    lines.append(f"Source: {entity.source_url}")
    for err in x.get("errors") or []:
        lines.append(f"NOTE: {err}")
    lines += ["", "Appointments"]
    for a in x.get("appointments") or []:
        span = f"appointed {a['appointed_on'] or '?'}" + (
            f", resigned {a['resigned_on']}" if a["resigned_on"] else ""
        )
        lines.append(
            f"- {a['company_name'] or '?'} ({a['company_number'] or '?'}, "
            f"{a['company_status'] or 'status unknown'}): {_words(a['role'])}; {span}"
        )
    if not x.get("appointments"):
        lines.append("- none listed")
    return "\n".join(lines) + "\n"
