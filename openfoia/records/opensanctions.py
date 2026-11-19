"""OpenSanctions adapter: sanctions lists, politically exposed persons (PEPs) and other watchlists.

OpenSanctions (https://www.opensanctions.org) merges 480+ sanctions lists, PEP
registers, criminal, debarment and regulatory-action lists into one database of
FollowTheMoney entities. This adapter answers a name two ways:

* **Offline, from the bulk file** (preferred whenever it is present):
  ``targets.simple.csv`` from the ``default`` collection, stored under
  ``<data dir>/opensanctions/`` by ``openfoia records opensanctions-data
  --download``. Whole-name and alias matching on this machine; nothing is sent.
* **The hosted API** (``POST https://api.opensanctions.org/match/default``) when
  there is no local file and ``OPENFOIA_OPENSANCTIONS_API_KEY`` is set. The name
  goes in the POST body, not the URL.

What was verified, and where (all read 2026-11-19):

* API reference https://api.opensanctions.org/ (openapi.json, yente 5.5.0):
  ``/match/{dataset}`` is "entity matching": use it to check people or
  companies against sanctions or PEP lists; its results carry ``score``
  (0..1) and ``match`` (score over the threshold, default 0.7). ``/search`` is
  a user-facing text search whose relevance scores are deliberately not
  exposed (https://www.opensanctions.org/docs/api/faq/), so it cannot give a
  confidence. ``/entities/{id}`` returns the full nested entity, or a 308 when
  the id was merged into another.
* Every hosted call needs a key: ``/match`` and ``/entities`` without one answer
  HTTP 401 ``{"detail": "No API key provided."}`` (tried 2026-11-19). The key
  goes in ``Authorization: ApiKey <key>``
  (https://www.opensanctions.org/docs/api/authentication/).
* Free keys are a grant for public-interest work: journalists (staff or
  freelance, including at commercial outlets), advocacy groups and nonprofits
  working on governance or corruption, academic researchers; OpenSanctions
  verifies before granting (https://www.opensanctions.org/docs/commercial/exemption/).
  Otherwise the API is prepaid credits: ``/match`` costs one credit per query,
  ``/entities`` is free, only HTTP 200 answers count, and each key has a quota
  past which requests get HTTP 429 (https://www.opensanctions.org/docs/api/faq/).
* Request logs are kept 90 days and include GET URLs, so the ``q`` of
  ``/search`` is logged; ``/match`` bodies are held in memory only, never
  logged. Billing keeps time, endpoint and key id per query for three years
  (same FAQ). The key itself identifies the account, so it is personal: never
  read from config.json, never sent over Tor.
* The bulk data is free for non-commercial use under CC BY-NC 4.0 and
  downloads without an account or key (https://www.opensanctions.org/docs/bulk/).
  ``https://data.opensanctions.org/datasets/latest/default/index.json``
  redirects (307) to the current version's ``index.json``, which lists each
  file's ``url``, ``size`` and a 40-hex-digit (SHA-1) ``checksum``.
  ``targets.simple.csv`` was 478,559,967 bytes on 2026-11-19. Its columns
  (https://www.opensanctions.org/docs/bulk/csv/) have no topics, so offline
  results infer sanction/PEP status from list titles and program ids and say so.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ..db import get_data_dir
from ..net import _retry_after, egress_client
from ..termsafe import safe_text
from .base import (
    AdapterRequestError,
    RecordAdapter,
    RecordEntity,
    SearchResult,
    download_to_file,
    validate_download_url,
)

logger = logging.getLogger(__name__)

API_HOST = "api.opensanctions.org"
API_BASE = f"https://{API_HOST}"
SITE = "https://www.opensanctions.org"
DATA_HOST = "data.opensanctions.org"
INDEX_URL = f"https://{DATA_HOST}/datasets/latest/default/index.json"
DATASET = "default"

KEY_ENV = "OPENFOIA_OPENSANCTIONS_API_KEY"
DATA_SUBDIR = "opensanctions"
DATA_FILE = "targets.simple.csv"
META_FILE = "targets.simple.meta.json"
LICENSE = "OpenSanctions data, CC BY-NC 4.0 (free for non-commercial use)"
APPROX_SIZE = "about 480 MB (478,559,967 bytes on 2026-11-19)"

#: Results under this score are not returned (``cutoff``); crossref has always
#: dropped API results under 0.5.
MATCH_CUTOFF = 0.5
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
#: Offline scores: a whole-name match, and a match on one of the aliases.
LOCAL_EXACT_SCORE = 1.0
LOCAL_ALIAS_SCORE = 0.8

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_INDEX_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_FIELD = 300
MAX_LIST = 50

#: FtM schemas a name query may be matched as.
SCHEMAS = ("LegalEntity", "Person", "Organization", "Company", "Vessel", "Airplane")
DEFAULT_SCHEMA = "LegalEntity"
_ENTITY_TYPES = {
    "Person": "PERSON",
    "LegalEntity": "ORGANIZATION",
    "Organization": "ORGANIZATION",
    "Company": "ORGANIZATION",
    "PublicBody": "ORGANIZATION",
}

NO_KEY_MESSAGE = (
    f"OpenSanctions needs either the bulk file for offline screening (run "
    f"'openfoia records opensanctions-data --download', {APPROX_SIZE}) or an API "
    f"key in {KEY_ENV}. Free keys for journalists, nonprofits and researchers: "
    f"{SITE}/docs/commercial/exemption/"
)
TOR_KEY_MESSAGE = (
    "OpenSanctions needs your personal API key, which identifies you, so the API "
    "is not used over Tor. Download the bulk file instead ('openfoia records "
    "opensanctions-data --download') to screen offline."
)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_PEP_RE = re.compile(r"(?<![a-z])peps?(?![a-z])|politically exposed", re.IGNORECASE)
_CHECKSUM = re.compile(r"[0-9a-f]{40}")


class OpenSanctionsError(AdapterRequestError):
    """Screening did not complete. *kind* says why; *partial* holds results found before.

    kinds: no_key, withheld, auth, rate_limit, http, transport, response,
    no_data, read.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        partial: list[RecordEntity] | None = None,
        error_type: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.partial = partial or []
        #: A short identifier for reports (no names, no URLs).
        self.error_type = error_type or kind


# ---------------------------------------------------------------------------
# Untrusted strings and ids
# ---------------------------------------------------------------------------


def clean_line(value: Any, limit: int = MAX_FIELD) -> str:
    """One printable line from an untrusted value: no control characters, capped."""
    if value is None:
        return ""
    text = safe_text(" ".join(str(value).split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strings(values: Any, limit: int = MAX_LIST) -> list[str]:
    """Cleaned, non-empty strings from a list that may hold anything."""
    if not isinstance(values, list):
        return []
    out = [clean_line(v) for v in values if isinstance(v, str | int | float)]
    return [v for v in out if v][:limit]


def valid_id(value: Any) -> str | None:
    """*value* if it is a safe OpenSanctions entity id (used in URLs and request numbers)."""
    text = str(value).strip() if isinstance(value, str) else ""
    return text if _ID.fullmatch(text) and ".." not in text else None


def entity_url(entity_id: Any) -> str | None:
    ident = valid_id(entity_id)
    return f"{SITE}/entities/{ident}/" if ident else None


def normalize_name(text: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed (offline matching)."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def classify_lists(datasets: list[str], program_ids: list[str] | None = None) -> tuple[bool, bool]:
    """(is_pep, is_sanctioned) from dataset titles, for the bulk CSV (which has no topics).

    Only a list whose title says "sanction" (or OFAC), or a sanctions program
    id, counts: OpenSanctions also carries procurement debarments, health-care
    exclusions and wanted notices, and the CSV's ``sanctions`` column is filled
    for those too.
    """
    is_pep = any(_PEP_RE.search(d) for d in datasets)
    is_sanctioned = bool(program_ids) or any(
        "sanction" in d.lower() or "ofac" in d.lower() for d in datasets
    )
    return is_pep, is_sanctioned


def detail_parts(
    is_pep: bool, is_sanctioned: bool, countries: list[str], datasets: list[str], schema: str
) -> list[str]:
    """Short human-readable flags for a hit, as crossref prints them."""
    parts = []
    if is_pep:
        parts.append("PEP (Politically Exposed Person)")
    if is_sanctioned:
        parts.append("SANCTIONED")
    if countries:
        parts.append(f"Countries: {', '.join(countries[:3])}")
    if datasets:
        parts.append(f"Lists: {'; '.join(datasets[:2])}")
    if not parts:
        parts.append(schema)
    return parts


# ---------------------------------------------------------------------------
# Local data file
# ---------------------------------------------------------------------------


def default_data_dir() -> Path:
    """Where the bulk file lives: ``<data dir>/opensanctions`` (respects portable mode)."""
    return get_data_dir() / DATA_SUBDIR


def local_status(data_dir: Path | str | None = None) -> dict[str, Any]:
    """What is on disk: path, whether it exists, size, and the metadata saved at download."""
    folder = Path(data_dir) if data_dir else default_data_dir()
    path = folder / DATA_FILE
    status: dict[str, Any] = {"path": str(path), "present": path.is_file()}
    if status["present"]:
        status["size"] = path.stat().st_size
    try:
        meta = json.loads((folder / META_FILE).read_text())
    except (OSError, ValueError):
        meta = None
    if isinstance(meta, dict):
        status["meta"] = {k: meta.get(k) for k in ("version", "checksum", "size", "downloaded_at")}
    return status


def _split(cell: str | None) -> list[str]:
    """A nested ``;``-separated cell (quoted the CSV way) as a list."""
    if not cell:
        return []
    return [v for v in next(csv.reader([cell], delimiter=";"), []) if v]


def _row_entity(row: dict[str, str], match_type: str, score: float) -> RecordEntity | None:
    ident = valid_id(row.get("id"))
    if not ident:
        return None
    schema = clean_line(row.get("schema"), 40) or "Thing"
    datasets = _strings(_split(row.get("dataset")))
    countries = _strings(_split(row.get("countries")))
    program_ids = _strings(_split(row.get("program_ids")))
    is_pep, is_sanctioned = classify_lists(datasets, program_ids)
    topics = (["sanction"] if is_sanctioned else []) + (["role.pep"] if is_pep else [])
    name = clean_line(row.get("name")) or ident
    aliases = _strings(_split(row.get("aliases")))
    births = _strings(_split(row.get("birth_date")), 5)
    props: dict[str, list[str]] = {"name": [name]}
    for key, values in (
        ("alias", aliases),
        ("birthDate", births),
        ("country", countries),
        ("address", _strings(_split(row.get("addresses")), 10)),
        ("idNumber", _strings(_split(row.get("identifiers")), 20)),
        ("phone", _strings(_split(row.get("phones")), 10)),
        ("email", _strings(_split(row.get("emails")), 10)),
        ("programId", program_ids),
    ):
        if values:
            props[key] = values
    return _entity(
        ident=ident,
        schema=schema,
        name=name,
        datasets=datasets,
        topics=topics,
        countries=countries,
        score=score,
        match=match_type in ("exact", "alias"),
        match_type=match_type,
        mode="local",
        extra={
            "topics_inferred": True,
            "aliases": aliases,
            "birth_dates": births,
            "program_ids": program_ids,
            "sanctions_details": _strings(_split(row.get("sanctions")), 10),
            "first_seen": clean_line(row.get("first_seen"), 40) or None,
            "last_change": clean_line(row.get("last_change"), 40) or None,
        },
        ftm={"id": ident, "schema": schema, "properties": props},
    )


def _entity(
    *,
    ident: str,
    schema: str,
    name: str,
    datasets: list[str],
    topics: list[str],
    countries: list[str],
    score: float | None,
    match: bool | None,
    match_type: str,
    mode: str,
    extra: dict[str, Any],
    ftm: dict[str, Any],
) -> RecordEntity:
    is_pep = "role.pep" in topics
    is_sanctioned = "sanction" in topics
    flags = [p for p, on in (("SANCTIONED", is_sanctioned), ("PEP", is_pep)) if on]
    snippet = " · ".join(
        x
        for x in (
            ", ".join(flags),
            f"topics: {', '.join(topics[:6])}" if topics else "",
            f"lists: {'; '.join(datasets[:3])}" if datasets else "",
            f"countries: {', '.join(countries[:5])}" if countries else "",
            f"match {score:.2f}" if isinstance(score, float) else "",
        )
        if x
    )
    return RecordEntity(
        entity_type=_ENTITY_TYPES.get(schema, schema.upper()),
        name=name,
        source="opensanctions",
        source_url=entity_url(ident),
        jurisdiction=countries[0] if countries else None,
        status="sanctioned" if is_sanctioned else None,
        identifiers={"opensanctions_id": ident},
        extra_data={
            "schema": schema,
            "datasets": datasets,
            "topics": topics,
            "countries": countries,
            "match_score": score,
            "match": match,
            "match_type": match_type,
            "is_sanctioned": is_sanctioned,
            "is_pep": is_pep,
            "mode": mode,
            "snippet": snippet,
            "license": LICENSE,
            **extra,
            "ftm": ftm,
        },
    )


def _scan(
    path: Path, *, name: str | None = None, entity_id: str | None = None, limit: int
) -> list[RecordEntity]:
    """Stream the CSV once: rows whose name or alias equals *name*, or the row with *entity_id*.

    Raises OpenSanctionsError("read") with the rows found so far when the
    file cannot be read. Logs the file name only, never the name searched.
    """
    target = normalize_name(name) if name else None
    found: list[RecordEntity] = []
    try:
        with open(path, encoding="utf-8", errors="ignore", newline="") as f:
            for row in csv.DictReader(f):
                if entity_id is not None:
                    if row.get("id") != entity_id:
                        continue
                    match_type, score = "id", None
                elif normalize_name(row.get("name") or "") == target:
                    match_type, score = "exact", LOCAL_EXACT_SCORE
                elif any(normalize_name(a) == target for a in _split(row.get("aliases"))):
                    match_type, score = "alias", LOCAL_ALIAS_SCORE
                else:
                    continue
                entity = _row_entity(row, match_type, score)
                if entity is not None:
                    found.append(entity)
                if len(found) >= limit:
                    break
    except Exception as exc:
        logger.warning("Failed to read OpenSanctions file %s: %s", path.name, type(exc).__name__)
        raise OpenSanctionsError(
            "read",
            f"Could not read the local OpenSanctions file ({type(exc).__name__}).",
            partial=found,
            error_type=type(exc).__name__,
        ) from exc
    return found


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpenSanctionsAdapter(RecordAdapter):
    """Screen names against OpenSanctions: the local bulk file, else the hosted API.

    *mode* is ``"auto"`` (the local file when it exists, else the API),
    ``"local"`` or ``"api"``. It is settled here, before any search, and
    ``offline`` says which: an offline adapter never builds an HTTP client, so
    ``openfoia search`` can leave it out of the list of sources contacted.
    """

    source_name = "opensanctions"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        egress=None,
        data_dir: Path | str | None = None,
        mode: str = "auto",
    ):
        super().__init__(egress=egress)
        if mode not in ("auto", "local", "api"):
            raise ValueError(f"mode must be auto, local or api, got {mode!r}")
        self._data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_path = self._data_dir / DATA_FILE
        if mode == "auto":
            mode = "local" if self.data_path.is_file() else "api"
        self.mode = mode
        #: True: every answer comes from the file on this machine; nothing is sent.
        self.offline = mode == "local"
        self._api_key: str | None = None
        if mode == "api":
            # Environment only: a personal key never goes in config.json.
            self._api_key = self._personal_credential(api_key or os.environ.get(KEY_ENV))

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    @property
    def search_notice(self) -> str | None:
        """Shown by ``openfoia search`` before anything is sent (API mode only)."""
        if self.offline:
            return None
        return (
            "sends the name in a POST body with your API key, which identifies your "
            "account; each name screened uses one /match credit. Download the bulk file "
            "to screen offline instead."
        )

    @property
    def local_notice(self) -> str:
        """One line for the local-search notice: which file, which version."""
        meta = local_status(self._data_dir).get("meta") or {}
        version = clean_line(meta.get("version"), 60)
        return f"local file {DATA_FILE}" + (f", version {version}" if version else "")

    def unavailable_reason(self) -> str | None:
        if self.offline:
            if self.data_path.is_file():
                return None
            return "no local targets.simple.csv (openfoia records opensanctions-data --download)"
        if self._api_key:
            return None
        if self.credentials_withheld:
            return "personal API key not sent over Tor; no local data file"
        return f"no local data file and no {KEY_ENV}"

    def match_confidence(self, entity: RecordEntity) -> float | None:
        """The match score for *entity* (0..1), shown as ``confidence`` by ``openfoia search``.

        API mode: the /match score. Offline: 1.0 for a whole-name match, 0.8 for
        an alias match.
        """
        score = entity.extra_data.get("match_score")
        if isinstance(score, bool) or not isinstance(score, int | float):
            return None
        return round(float(score), 4) if 0.0 <= score <= 1.0 else None

    def _redact(self, text: object) -> str:
        """Error text for the terminal: one line, no key, no brackets Rich would read as markup."""
        out = clean_line(text, 300).replace("[", "(").replace("]", ")")
        return out.replace(self._api_key, "<redacted>") if self._api_key else out

    # -- screening -----------------------------------------------------------

    async def screen(
        self, name: str, *, schema: str = DEFAULT_SCHEMA, limit: int = DEFAULT_LIMIT
    ) -> list[RecordEntity]:
        """Entities matching *name*, best first. Raises OpenSanctionsError when screening fails.

        This is the one code path for ``openfoia search``, ``records search`` and
        ``crossref``.
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        if self.offline:
            if not self.data_path.is_file():
                raise OpenSanctionsError(
                    "no_data",
                    f"No {DATA_FILE} in {self._data_dir}. Run 'openfoia records "
                    "opensanctions-data --download' first.",
                    error_type="NoOpenSanctionsData",
                )
            if not normalize_name(name):
                return []
            return await asyncio.to_thread(_scan, self.data_path, name=name, limit=limit)
        if schema not in SCHEMAS:
            raise OpenSanctionsError(
                "response", f"Unknown schema {clean_line(schema, 40)!r}; use {', '.join(SCHEMAS)}"
            )
        body = {"queries": {"q": {"schema": schema, "properties": {"name": [name]}}}}
        data = await self._call(
            "POST",
            f"/match/{DATASET}",
            params={"limit": limit, "cutoff": MATCH_CUTOFF},
            json_body=body,
        )
        response = (data.get("responses") or {}).get("q") if isinstance(data, dict) else None
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            raise OpenSanctionsError(
                "response",
                "OpenSanctions /match returned no result list.",
                error_type="BadResponse",
            )
        entities = [e for e in (_api_entity(r, "fuzzy") for r in results) if e]
        entities.sort(key=lambda e: -(e.extra_data["match_score"] or 0.0))
        return entities[:limit]

    async def search(self, query: str, **kwargs: Any) -> SearchResult:
        """Screen *query* as a name. ``schema`` (Person, Company, ...) narrows API matching.

        ``limit`` or ``page_size`` caps results (default 10, at most 50). A
        failure is reported in ``SearchResult.error``, with any rows found
        before it, never as zero results.
        """
        query = (query or "").strip()
        limit = kwargs.get("limit") or kwargs.get("page_size") or DEFAULT_LIMIT
        schema = kwargs.get("schema") or DEFAULT_SCHEMA
        raw = {"mode": self.mode, "dataset": DATASET}
        try:
            entities = await self.screen(query, schema=schema, limit=limit)
        except OpenSanctionsError as exc:
            result = self._failed(query, str(exc), raw_response=raw)
            result.entities = exc.partial
            return result
        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=len(entities),
            entities=entities,
            per_page=len(entities),
            raw_response=raw,
        )

    # -- fetch ---------------------------------------------------------------

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """One entity by OpenSanctions id: from the local file, or ``/entities/{id}`` (free).

        Raises ValueError for an id that is not an OpenSanctions id and
        OpenSanctionsError when it cannot be read. None when there is no such id.
        """
        ident = valid_id(identifier)
        if not ident:
            raise ValueError(
                f"Not an OpenSanctions id: {clean_line(identifier, 80)!r} (e.g. NK-a1b2c3, Q7747)"
            )
        if self.offline:
            if not self.data_path.is_file():
                raise OpenSanctionsError("no_data", f"No {DATA_FILE} in {self._data_dir}.")
            found = await asyncio.to_thread(_scan, self.data_path, entity_id=ident, limit=1)
            return found[0] if found else None
        data = await self._call(
            "GET", f"/entities/{quote(ident, safe='')}", params={"nested": "true"}
        )
        if data is None:
            return None
        return _api_entity(data, "id", nested=True)

    # -- HTTP ----------------------------------------------------------------

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """One API call as JSON; None on 404. Follows one same-host 308 (a merged entity id)."""
        if not self._api_key:
            if self.credentials_withheld:
                raise OpenSanctionsError("withheld", TOR_KEY_MESSAGE)
            raise OpenSanctionsError("no_key", NO_KEY_MESSAGE)
        url = API_BASE + path
        headers = {"Authorization": f"ApiKey {self._api_key}", "Accept": "application/json"}
        for _ in range(2):
            try:
                status, body, retry, location = await self._send(
                    method, url, params, json_body, headers
                )
            except OpenSanctionsError:
                raise
            except Exception as exc:
                raise OpenSanctionsError(
                    "transport",
                    f"OpenSanctions request failed: {type(exc).__name__}: {self._redact(exc)}",
                    error_type=type(exc).__name__,
                ) from exc
            if status == 308 and method == "GET" and location:
                url = _same_api_url(location)
                continue
            break
        if status == 404:
            return None
        if status in (401, 403):
            raise OpenSanctionsError(
                "auth",
                f"OpenSanctions refused the API key (HTTP {status}){self._detail(body)}. "
                f"Check {KEY_ENV}.",
                error_type=f"HTTP{status}",
            )
        if status == 429:
            when = f" Retry after {retry:.0f}s." if retry is not None else ""
            raise OpenSanctionsError(
                "rate_limit",
                f"OpenSanctions rate limit or key quota reached (HTTP 429){self._detail(body)}."
                f"{when} The quota is shown in your account at {SITE}/account/.",
                error_type="RateLimited",
            )
        if status >= 400:
            raise OpenSanctionsError(
                "http",
                f"OpenSanctions API error (HTTP {status}){self._detail(body)}.",
                error_type=f"HTTP{status}",
            )
        if status >= 300:
            raise OpenSanctionsError(
                "http",
                f"OpenSanctions answered with a redirect (HTTP {status}); not followed, so "
                "the key is only ever sent to the API host.",
                error_type=f"HTTP{status}",
            )
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise OpenSanctionsError(
                "response",
                "OpenSanctions returned something that is not JSON.",
                error_type="BadResponse",
            ) from exc
        if not isinstance(data, dict):
            raise OpenSanctionsError(
                "response",
                "OpenSanctions returned an unexpected JSON shape.",
                error_type="BadResponse",
            )
        return data

    async def _send(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> tuple[int, bytes, float | None, str | None]:
        """Status, body (capped), Retry-After seconds and Location for one request."""
        async with (
            egress_client(self._egress, timeout=30, follow_redirects=False) as client,
            client.stream(method, url, params=params, json=json_body, headers=headers) as resp,
        ):
            cap = MAX_RESPONSE_BYTES if resp.status_code < 400 else 4096
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > cap:
                    if resp.status_code >= 400:
                        break
                    raise OpenSanctionsError(
                        "response",
                        f"OpenSanctions response over {MAX_RESPONSE_BYTES // (1024 * 1024)} MB; "
                        "refusing to read it.",
                        error_type="ResponseTooLarge",
                    )
            return resp.status_code, bytes(body), _retry_after(resp), resp.headers.get("location")

    def _detail(self, body: bytes) -> str:
        try:
            detail = json.loads(body).get("detail")
        except (ValueError, AttributeError):
            return ""
        return f": {self._redact(clean_line(detail, 200))}" if isinstance(detail, str) else ""

    # -- storage ---------------------------------------------------------------

    def store(
        self, entity: RecordEntity, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        """Save *entity* as a text Document under request number ``OS-<id>`` plus one Entity row.

        The text carries the readable summary and the FollowTheMoney entity as
        JSON; the Entity row is shaped like an FtM import (``ftm_schema``,
        ``ftm_id``) so it joins the case graph.
        """
        from .store import store_text_document  # lazy: imports the ORM

        ident = entity.identifiers["opensanctions_id"]
        entity.extra_data["full_text"] = entity_text(entity)
        doc_id, text = store_text_document(
            entity,
            request_number=f"OS-{ident}",
            filename=f"opensanctions-{ident}.txt",
            body=f"Pulled from OpenSanctions: {entity.source_url}. {LICENSE}.",
            case_id=case_id,
        )
        if doc_id:
            _store_entity_row(entity, doc_id)
        return doc_id, text

    async def pull_text(
        self, identifier: str, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        entity = await self.fetch(identifier)
        if not entity:
            return None, None
        return self.store(entity, case_id)


def _same_api_url(location: str) -> str:
    """A redirect target, only if it is another entity on the API host (the key goes there)."""
    if location.startswith("/"):
        location = API_BASE + location
    parts = urlsplit(location)
    path_id = parts.path.removeprefix("/entities/")
    if (
        parts.scheme != "https"
        or parts.hostname != API_HOST
        or parts.port not in (None, 443)
        or parts.username
        or not parts.path.startswith("/entities/")
        or not valid_id(path_id)
    ):
        raise OpenSanctionsError(
            "http",
            f"Refusing to follow an OpenSanctions redirect off {API_BASE}/entities/: "
            f"{clean_line(location, 120)!r}",
            error_type="BadRedirect",
        )
    return f"{API_BASE}/entities/{path_id}"


def _api_entity(data: Any, match_type: str, *, nested: bool = False) -> RecordEntity | None:
    """A RecordEntity from one API entity (a /match result or an /entities answer)."""
    if not isinstance(data, dict):
        return None
    ident = valid_id(data.get("id"))
    if not ident:
        return None
    schema = clean_line(data.get("schema"), 40) or "Thing"
    raw_props = data.get("properties") if isinstance(data.get("properties"), dict) else {}
    props: dict[str, list[str]] = {}
    related: list[dict[str, Any]] = []
    sanctions: list[dict[str, Any]] = []
    for key, values in list(raw_props.items())[:200]:
        prop = clean_line(key, 60)
        if not prop or not isinstance(values, list):
            continue
        texts = []
        for value in values[:MAX_LIST]:
            if isinstance(value, dict):  # a nested entity (only in /entities answers)
                child = _nested(value, prop)
                if child:
                    related.append(child)
                    texts.append(child["id"])  # FtM: a reference is the entity id
                    if child["schema"] == "Sanction":
                        sanctions.append(child)
            elif isinstance(value, str | int | float):
                text = clean_line(value)
                if text:
                    texts.append(text)
        if texts:
            props[prop] = texts
    topics = props.get("topics", [])
    countries = list(
        dict.fromkeys(
            props.get("country", []) + props.get("nationality", []) + props.get("jurisdiction", [])
        )
    )[:MAX_LIST]
    score = data.get("score")
    score = float(score) if isinstance(score, int | float) and not isinstance(score, bool) else None
    if score is not None and not 0.0 <= score <= 1.0:
        score = None
    match = data.get("match") if isinstance(data.get("match"), bool) else None
    if match_type == "fuzzy" and score is not None and score > 0.9:
        match_type = "exact"
    name = clean_line(data.get("caption")) or (props.get("name") or [ident])[0]
    return _entity(
        ident=ident,
        schema=schema,
        name=name,
        datasets=_strings(data.get("datasets")),
        topics=topics,
        countries=countries,
        score=score,
        match=match,
        match_type=match_type,
        mode="api",
        extra={
            "topics_inferred": False,
            "aliases": props.get("alias", [])[:MAX_LIST],
            "birth_dates": props.get("birthDate", [])[:5],
            "program_ids": props.get("programId", []),
            "sanctions_details": [s["summary"] for s in sanctions][:MAX_LIST],
            "related": related[:MAX_LIST] if nested else [],
            "first_seen": clean_line(data.get("first_seen"), 40) or None,
            "last_change": clean_line(data.get("last_change"), 40) or None,
            "referents": _strings(data.get("referents"), 20),
        },
        ftm={"id": ident, "schema": schema, "properties": props},
    )


def _nested(value: dict[str, Any], prop: str) -> dict[str, Any] | None:
    ident = valid_id(value.get("id"))
    if not ident:
        return None
    schema = clean_line(value.get("schema"), 40) or "Thing"
    props = value.get("properties") if isinstance(value.get("properties"), dict) else {}

    def first(key: str) -> str:
        vals = props.get(key)
        return (
            clean_line(vals[0])
            if isinstance(vals, list) and vals and isinstance(vals[0], str)
            else ""
        )

    summary = clean_line(value.get("caption")) or ident
    if schema == "Sanction":
        bits = [first("authority"), first("program"), first("startDate")]
        summary = " · ".join(b for b in bits if b) or summary
    return {"id": ident, "schema": schema, "property": prop, "summary": summary}


def entity_text(entity: RecordEntity) -> str:
    """Readable text for a stored entity: summary lines, then the FtM entity as JSON."""
    x = entity.extra_data
    lines = [
        entity.name,
        f"OpenSanctions id: {entity.identifiers.get('opensanctions_id')}",
        f"Type: {x.get('schema')}",
        f"Source: {entity.source_url}",
        f"Retrieved from: {'the local bulk file (offline)' if x.get('mode') == 'local' else API_HOST}",
    ]
    topics = x.get("topics") or []
    if topics:
        inferred = (
            " (inferred from list titles; the bulk CSV has no topics)"
            if x.get("topics_inferred")
            else ""
        )
        lines.append(f"Topics: {', '.join(topics)}{inferred}")
    for label, key in (
        ("Lists", "datasets"),
        ("Countries", "countries"),
        ("Aliases", "aliases"),
        ("Birth dates", "birth_dates"),
        ("Sanctions programs", "program_ids"),
        ("Sanctions", "sanctions_details"),
    ):
        if x.get(key):
            lines.append(f"{label}: {'; '.join(x[key])}")
    for label, key in (("First seen", "first_seen"), ("Last change", "last_change")):
        if x.get(key):
            lines.append(f"{label}: {x[key]}")
    related = x.get("related") or []
    if related:
        lines += ["", "Related entities"]
        lines += [f"- {r['summary']} ({r['schema']}, {r['property']}, {r['id']})" for r in related]
    lines += [
        "",
        LICENSE,
        "",
        "FollowTheMoney entity:",
        json.dumps(x.get("ftm"), ensure_ascii=False),
    ]
    return "\n".join(lines) + "\n"


def _store_entity_row(entity: RecordEntity, doc_id: str) -> None:
    """One Entity for the fetched record, attached to its document, FtM-import shaped."""
    from ..db import get_session  # lazy: monkeypatched in tests
    from ..models import Entity, EntityType

    try:
        etype = EntityType(entity.entity_type.lower())
    except ValueError:
        etype = EntityType.ORGANIZATION
    ident = entity.identifiers["opensanctions_id"]
    x = entity.extra_data
    meta = {
        "source": "opensanctions",
        "ftm_schema": x.get("schema"),
        "ftm_id": ident,
        "topics": x.get("topics") or [],
        "datasets": x.get("datasets") or [],
        "url": entity.source_url,
    }
    with get_session() as session:
        existing = [
            e
            for e in session.query(Entity).filter(Entity.document_id == doc_id).all()
            if (e.extra_data or {}).get("ftm_id") == ident
        ]
        if existing:
            existing[0].extra_data = meta
            return
        session.add(
            Entity(
                document_id=doc_id,
                entity_type=etype,
                raw_text=entity.name,
                normalized_text=entity.name,
                confidence=1.0,  # a record fetched by id, not an extraction guess
                context="; ".join(x.get("topics") or []) or None,
                extra_data=meta,
            )
        )


# ---------------------------------------------------------------------------
# Bulk file download (explicit only: `openfoia records opensanctions-data --download`)
# ---------------------------------------------------------------------------


def _data_url(url: Any) -> str:
    """*url* if it is an https URL on data.opensanctions.org, else OpenSanctionsError."""
    if not isinstance(url, str):
        raise OpenSanctionsError("response", "The OpenSanctions index has no file URL.")
    parts = urlsplit(url)
    try:
        validate_download_url(url)
    except ValueError as exc:
        raise OpenSanctionsError("response", str(exc), error_type="BadURL") from exc
    if parts.hostname != DATA_HOST or parts.port not in (None, 443) or parts.username:
        raise OpenSanctionsError(
            "response",
            f"Refusing a download URL off {DATA_HOST}: {clean_line(url, 120)!r}",
            error_type="BadURL",
        )
    return url


async def read_index(egress=None) -> dict[str, Any]:
    """The current ``default`` index's entry for targets.simple.csv: url, size, checksum, version.

    One GET to data.opensanctions.org (plus its one redirect to the versioned
    index). Sends no name and no key.
    """
    url = INDEX_URL
    async with egress_client(egress, timeout=30, follow_redirects=False) as client:
        for _ in range(3):
            async with client.stream("GET", url) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    url = _data_url(resp.headers.get("location"))
                    continue
                if resp.status_code != 200:
                    raise OpenSanctionsError(
                        "http",
                        f"OpenSanctions index answered HTTP {resp.status_code}.",
                        error_type=f"HTTP{resp.status_code}",
                    )
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_INDEX_BYTES:
                        raise OpenSanctionsError("response", "OpenSanctions index is too large.")
                break
        else:
            raise OpenSanctionsError("http", "Too many redirects reading the OpenSanctions index.")
    try:
        index = json.loads(bytes(body))
    except ValueError as exc:
        raise OpenSanctionsError("response", "OpenSanctions index is not JSON.") from exc
    resources = index.get("resources") if isinstance(index, dict) else None
    entry = next(
        (r for r in resources or [] if isinstance(r, dict) and r.get("name") == DATA_FILE), None
    )
    if entry is None:
        raise OpenSanctionsError("response", f"The OpenSanctions index lists no {DATA_FILE}.")
    size = entry.get("size")
    checksum = str(entry.get("checksum") or "").lower()
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_DOWNLOAD_BYTES:
        raise OpenSanctionsError("response", f"Implausible size in the index: {clean_line(size)}")
    if not _CHECKSUM.fullmatch(checksum):
        raise OpenSanctionsError("response", "The index has no SHA-1 checksum for the file.")
    return {
        "url": _data_url(entry.get("url")),
        "size": size,
        "checksum": checksum,
        "version": clean_line(index.get("version"), 60) or None,
    }


def _sha1(path: Path) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def download_bulk(entry: dict[str, Any], egress=None, data_dir: Path | None = None) -> Path:
    """Download *entry* (from :func:`read_index`), verify size and SHA-1, then swap it in.

    The old file stays in place until the new one is complete and verified.
    """
    folder = Path(data_dir) if data_dir else default_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / DATA_FILE
    staging = folder / (DATA_FILE + ".download")
    try:
        async with egress_client(egress, timeout=120, follow_redirects=False) as client:
            written = await download_to_file(
                client, entry["url"], staging, max_bytes=entry["size"], egress=egress
            )
        if written != entry["size"]:
            raise OpenSanctionsError(
                "response",
                f"Download incomplete: {written:,} of {entry['size']:,} bytes. Nothing replaced.",
            )
        actual = await asyncio.to_thread(_sha1, staging)
        if actual != entry["checksum"]:
            raise OpenSanctionsError(
                "response",
                "Checksum mismatch (SHA-1 differs from the published index). Nothing replaced.",
                error_type="ChecksumMismatch",
            )
        staging.replace(dest)
    finally:
        staging.unlink(missing_ok=True)
    meta = {
        "version": entry.get("version"),
        "checksum": entry["checksum"],
        "size": entry["size"],
        "url": entry["url"],
        "downloaded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "license": LICENSE,
    }
    (folder / META_FILE).write_text(json.dumps(meta, indent=2))
    return dest
