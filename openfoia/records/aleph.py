"""OCCRP Aleph adapter: company registries, leaks, court records and more, as FollowTheMoney.

Aleph (now "Aleph Pro", https://aleph.occrp.org) is run by the Organized Crime
and Corruption Reporting Project. Its entities are FollowTheMoney (FtM)
objects, so a fetched entity and its neighbours go into the local entity graph
through :mod:`openfoia.ftm_import`, the same path as ``openfoia import``.

Checked against the live service on 2026-11-19 (unauthenticated requests only):

- ``GET /api/2/entities?q=...`` (search) answers HTTP 401 ``{"message": "You
  are not authorized to do this."}`` without a key; so does
  ``/api/2/collections``. Anonymous search no longer works, although the FAQ
  in ``/api/2/metadata`` still says "rate limits apply for non-registered
  users". ``GET /api/2/roles/me`` says ``"Not logged in"``.
- A bad key in ``Authorization: ApiKey <key>`` gets HTTP 401 ``invalid
  credentials for "Authorization: ApiKey"`` (text/plain). The OpenAPI document
  at ``/api/openapi.json`` names the header ``Authorization`` and accepts
  ``ApiKey <key>``.
- ``/api/openapi.json`` documents ``q``, ``limit``, ``offset``,
  ``collection_id`` and ``schema`` on ``/api/2/entities``, and
  ``/api/2/entities/{id}`` and ``/api/2/entities/{id}/expand``. It gives no
  response shapes. The shapes used here (``results``, ``total``, each result
  with ``id``, ``schema``, ``properties``, ``collection``) are the ones the
  open-source Aleph API returned and alephclient reads; Aleph Pro says it is
  backwards compatible with alephclient. They were NOT seen with a key, and
  neither was the ``filter:countries`` parameter, so the country and type
  filters are also applied locally to what comes back (see :func:`_matches`).
- Accounts: a registration form (https://requests.occrp.org/register),
  reviewed by hand, then a mandatory authenticator app (2FA). An API key is
  generated under Settings; generating a new one invalidates the old one, and
  the key "allows anyone holding it access to all of your Aleph data and
  activity". Leaks and other restricted material need extended access that
  OCCRP grants case by case.
- Terms (``/pages/terms`` and ``/pages/acceptable-use``, updated 2026-03-12):
  journalism in the public interest only, no KYC or due-diligence use, no
  "bots, scrapers, spiders, or other automated or high-volume methods", no
  credential sharing; stories must credit "According to data made available
  via OCCRP Aleph Pro". No numeric rate limit is published.

So: the key is read from ``OPENFOIA_ALEPH_API_KEY`` only (never config.json),
it identifies the user to OCCRP, and it is never sent over Tor (Aleph does not
answer searches without it, so Aleph is not used over Tor). Each search or
fetch is one or two requests started by the user, never a crawl, and a local
budget of OpenFOIA's own choosing (30 a minute, 500 a day; not an OCCRP
number) keeps a batch fetch from becoming "high-volume".
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

from . import keyed_api
from .base import AdapterRequestError, RecordAdapter, RecordEntity, SearchResult
from .ftm_store import clean_ftm, import_ftm_entities
from .keyed_api import (
    MAX_WAIT,
    RequestBudget,
    clean_line,
    clean_text,
    error_detail,
    no_markup,
    read_capped,
    strip_tags,
    when,
)
from .store import store_text_document

logger = logging.getLogger(__name__)

SITE = "https://aleph.occrp.org"
API_HOST = "aleph.occrp.org"
API_PATH = "/api/2/"
API_BASE = SITE + API_PATH.rstrip("/")
KEY_ENV = "OPENFOIA_ALEPH_API_KEY"
BUDGET_ENV = "OPENFOIA_ALEPH_IGNORE_BUDGET"
REGISTER_URL = "https://requests.occrp.org/register"
CREDIT = "According to data made available via OCCRP Aleph Pro (https://aleph.occrp.org)"

#: OpenFOIA's own cap, not a published OCCRP limit: OCCRP forbids "high-volume" access.
BUDGET = RequestBudget(
    provider="Aleph",
    filename="aleph_budget.json",
    limits=((60, 30, "a minute"), (86400, 500, "a day")),
    ignore_env=BUDGET_ENV,
)
DAY_LIMIT = 500

PAGE_MAX = 50  # results per request; a search asks for no more than the CLI's --limit
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_RELATED = 200  # neighbour entities kept from /expand
MAX_TEXT = 2_000_000

NO_KEY_MESSAGE = (
    "OCCRP Aleph needs an API key: it refuses anonymous searches (HTTP 401). Register at "
    f"{REGISTER_URL} (reviewed by hand; an authenticator app is required), generate a key "
    f"under Settings at {SITE} and set {KEY_ENV}. The key is personal: it identifies you to "
    "OCCRP, which can link your searches to your account."
)
TOR_KEY_MESSAGE = (
    "OCCRP Aleph needs your personal API key, which would identify you, so Aleph is not used "
    "over Tor (it refuses searches without a key). Turn off network.tor to query it directly."
)

#: --type values -> (schema sent to Aleph, result schemata accepted locally).
_DOCUMENT_SCHEMATA = frozenset(
    {
        "Document",
        "Pages",
        "Page",
        "PlainText",
        "HyperText",
        "Email",
        "Image",
        "Table",
        "Workbook",
        "Folder",
        "Package",
        "Audio",
        "Video",
        "Article",
    }
)
SCHEMA_TYPES: dict[str, tuple[str, frozenset[str]]] = {
    "person": ("Person", frozenset({"Person"})),
    "company": ("Company", frozenset({"Company"})),
    "organization": ("Organization", frozenset({"Organization", "Company", "PublicBody"})),
    "publicbody": ("PublicBody", frozenset({"PublicBody"})),
    "legalentity": (
        "LegalEntity",
        frozenset({"LegalEntity", "Person", "Company", "Organization", "PublicBody"}),
    ),
    "vessel": ("Vessel", frozenset({"Vessel"})),
    "airplane": ("Airplane", frozenset({"Airplane"})),
    "realestate": ("RealEstate", frozenset({"RealEstate"})),
    "document": ("Document", _DOCUMENT_SCHEMATA),
}
TYPE_CHOICES = ", ".join(SCHEMA_TYPES)

_ENTITY_TYPES = {
    "Person": "PERSON",
    "Company": "ORGANIZATION",
    "Organization": "ORGANIZATION",
    "PublicBody": "ORGANIZATION",
    "LegalEntity": "ORGANIZATION",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_COUNTRY = re.compile(r"^[a-z]{2}(?:-[a-z0-9]{1,3})?$")
_DATE_PROPS = ("incorporationDate", "birthDate", "date", "startDate", "createdAt", "publishedAt")
_NAME_PROPS = ("name", "title", "fileName", "subject", "caption")
_TEXT_PROPS = ("bodyText", "indexText", "description", "summary", "notes")
#: Properties shown in the stored text, in this order; the rest follow alphabetically.
_SHOWN_FIRST = (
    "name",
    "alias",
    "previousName",
    "registrationNumber",
    "jurisdiction",
    "country",
    "incorporationDate",
    "dissolutionDate",
    "status",
    "address",
    "birthDate",
    "nationality",
)


# ---------------------------------------------------------------------------
# Identifiers and filters (pure)
# ---------------------------------------------------------------------------


def parse_identifier(identifier: str) -> str:
    """An Aleph entity id from an id or an ``https://aleph.occrp.org/entities/<id>`` URL.

    Ids are hex hashes, often ``<hash>.<signature>``, or dataset slugs; only
    letters, digits, ``.``, ``_`` and ``-`` are accepted and never ``..``, so
    an id can never step outside ``/api/2/entities/``. Raises ValueError.
    """
    raw = str(identifier or "").strip()
    if raw.lower().startswith(("https://", "http://")):
        parts = urlsplit(raw)
        m = re.fullmatch(r"/entities/([^/]+)/?", parts.path or "")
        if parts.scheme != "https" or parts.hostname != API_HOST or parts.port or not m:
            raise ValueError(
                f"Not an Aleph entity URL: {clean_line(raw)!r} (expected {SITE}/entities/<id>)."
            )
        raw = m.group(1)
    raw = raw.removeprefix("aleph:")
    if not _ID.match(raw) or ".." in raw:
        raise ValueError(f"Not an Aleph entity id: {clean_line(raw)!r}.")
    return raw


canonical_id = parse_identifier


def schema_filter(value: Any) -> tuple[str, frozenset[str]] | None:
    """(schema, accepted schemata) for a --type value; None for no filter; ValueError if unknown."""
    if not value:
        return None
    key = re.sub(r"[\s_-]+", "", str(value).strip().lower())
    if key not in SCHEMA_TYPES:
        raise ValueError(f"Unknown Aleph type {clean_line(value)!r}. Use one of: {TYPE_CHOICES}.")
    return SCHEMA_TYPES[key]


def country_filter(value: Any) -> str | None:
    """A lowercase ISO 3166 country code for a --jurisdiction value; ValueError otherwise."""
    if not value:
        return None
    code = str(value).strip().lower()
    code = {"uk": "gb"}.get(code, code)
    if not _COUNTRY.match(code):
        raise ValueError(
            f"Aleph filters by country code (e.g. gb, ru, br), not {clean_line(value)!r}."
        )
    return code


def build_params(query: str, page: int, page_size: int, **kwargs: Any) -> dict[str, Any]:
    """Query parameters for ``/entities``. Raises ValueError on a filter Aleph would misread."""
    params: dict[str, Any] = {
        "q": query,
        "limit": max(1, min(int(page_size), PAGE_MAX)),
        "offset": (max(1, int(page)) - 1) * max(1, min(int(page_size), PAGE_MAX)),
    }
    schema = schema_filter(kwargs.get("schema") or kwargs.get("filing_type"))
    if schema:
        params["schema"] = schema[0]
    country = country_filter(kwargs.get("country") or kwargs.get("jurisdiction"))
    if country:
        params["filter:countries"] = country
    return params


def _props(entity: dict[str, Any]) -> dict[str, list[Any]]:
    props = entity.get("properties")
    return props if isinstance(props, dict) else {}


def _first(props: dict[str, list[Any]], *names: str, limit: int = 300) -> str:
    for name in names:
        values = props.get(name)
        if isinstance(values, list):
            for v in values:
                if isinstance(v, str | int | float) and not isinstance(v, bool):
                    text = clean_line(v, limit)
                    if text:
                        return text
    return ""


def _countries(entity: dict[str, Any]) -> set[str]:
    props = _props(entity)
    found: set[str] = set()
    for name in ("country", "jurisdiction", "nationality", "citizenship", "countries", "flag"):
        for v in props.get(name) or []:
            if isinstance(v, str):
                found.add(v.strip().lower())
    for v in entity.get("countries") or []:
        if isinstance(v, str):
            found.add(v.strip().lower())
    collection = entity.get("collection")
    if isinstance(collection, dict):
        for v in collection.get("countries") or []:
            if isinstance(v, str):
                found.add(v.strip().lower())
    return found


def _matches(entity: dict[str, Any], schemata: frozenset[str] | None, country: str | None) -> bool:
    """True if *entity* satisfies the filters: the server's filtering is not trusted blindly.

    An ignored filter would otherwise show unfiltered results under a filtered
    query. Results that do not match are dropped and counted.
    """
    if schemata is not None and entity.get("schema") not in schemata:
        return False
    return not country or country in _countries(entity)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AlephAdapter(RecordAdapter):
    """Search OCCRP Aleph and fetch entities with their neighbours."""

    source_name = "aleph"

    def __init__(self, api_key: str | None = None, *, egress=None):
        super().__init__(egress=egress)
        # Environment only: a personal key never goes in config.json.
        self._api_key = self._personal_credential(api_key or os.environ.get(KEY_ENV))

    #: Shown by ``openfoia search`` before the query is sent.
    search_notice = (
        "sends your Aleph API key, which identifies your OCCRP account; OCCRP can link "
        "these searches to you. Journalism use only under OCCRP's terms; credit OCCRP Aleph "
        "Pro if you publish."
    )

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def unavailable_reason(self) -> str | None:
        if self._api_key:
            return None
        if self.credentials_withheld:
            return "personal API key not sent over Tor"
        return f"no {KEY_ENV} (Aleph refuses anonymous searches)"

    def missing_key_message(self) -> str:
        return TOR_KEY_MESSAGE if self.credentials_withheld else NO_KEY_MESSAGE

    def _redact(self, text: object) -> str:
        out = no_markup(clean_line(text, 500))
        return out.replace(self._api_key, "<redacted>") if self._api_key else out

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict | None:
        """GET an API path (never a URL from a response) as JSON; None on 404."""
        if not self._api_key:
            raise AdapterRequestError(self.missing_key_message())
        if not path.startswith("/") or ".." in path:
            raise AdapterRequestError(f"Refusing Aleph path {clean_line(path)!r}.")
        url = API_BASE + path
        headers = {"Authorization": f"ApiKey {self._api_key}", "Accept": "application/json"}
        waited = False
        while True:
            await BUDGET.reserve()
            try:
                status, body, retry = await read_capped(
                    self._egress,
                    url,
                    params=params,
                    headers=headers,
                    max_bytes=MAX_RESPONSE_BYTES,
                    label="Aleph",
                )
            except AdapterRequestError:
                raise
            except Exception as exc:
                raise AdapterRequestError(
                    f"Aleph request failed: {type(exc).__name__}: {self._redact(exc)}"
                ) from exc
            if status != 429:
                break
            if retry is not None and retry <= MAX_WAIT and not waited:
                waited = True
                logger.warning("Aleph rate limit (HTTP 429); waiting %.0fs.", retry)
                await keyed_api._sleep(retry)  # the module attribute: patched in tests
                continue
            delay = retry if retry is not None else 300.0
            BUDGET.block_until(time.time() + delay)
            raise AdapterRequestError(
                f"Aleph rate limit reached (HTTP 429){self._detail(body)}. Try again after "
                f"{when(time.time() + delay, time.time())}."
            )
        if status == 404:
            return None
        if status == 401:
            raise AdapterRequestError(
                f"Aleph rejected the API key (HTTP 401){self._detail(body)}. Check {KEY_ENV}; "
                "generating a new key in Aleph's Settings invalidates the old one."
            )
        if status == 403:
            raise AdapterRequestError(
                f"Aleph refused access (HTTP 403){self._detail(body)}. Leaks and other "
                "restricted datasets need extended access that OCCRP grants case by case."
            )
        if status >= 400:
            raise AdapterRequestError(f"Aleph API error (HTTP {status}){self._detail(body)}.")
        if status >= 300:
            raise AdapterRequestError(
                f"Aleph answered with a redirect (HTTP {status}); not followed, so the key is "
                "only ever sent to aleph.occrp.org."
            )
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise AdapterRequestError("Aleph returned something that is not JSON.") from exc
        if not isinstance(data, dict):
            raise AdapterRequestError("Aleph returned an unexpected JSON shape.")
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
        """One page of Aleph entity search (one request).

        ``schema`` / ``filing_type`` (the CLI's ``--type``): person, company,
        organization, publicbody, legalentity, vessel, airplane, realestate or
        document. ``country`` / ``jurisdiction`` (``-j``): an ISO country code.
        """
        try:
            params = build_params(query, page, page_size, **kwargs)
            schema = schema_filter(kwargs.get("schema") or kwargs.get("filing_type"))
            country = country_filter(kwargs.get("country") or kwargs.get("jurisdiction"))
        except ValueError as exc:
            return self._failed(query, str(exc), page=page, per_page=page_size)
        if not (query or "").strip():
            return self._failed(query, "Give a search term for Aleph.", page=page)
        try:
            data = await self._get("/entities", params)
        except AdapterRequestError as exc:
            return self._failed(query, str(exc), page=page, per_page=params["limit"])
        if data is None:
            return self._failed(query, "Aleph search endpoint not found (HTTP 404).", page=page)
        results = data.get("results")
        if not isinstance(results, list):
            return self._failed(query, "Aleph search returned no result list.", page=page)
        accepted = schema[1] if schema else None
        dropped = 0
        entities = []
        for hit in results:
            if not isinstance(hit, dict):
                continue
            if not _matches(hit, accepted, country):
                dropped += 1
                continue
            entity = _entity(hit)
            if entity:
                entities.append(entity)
        total = data.get("total")
        if isinstance(total, dict):  # some Elasticsearch versions: {"value": n, "relation": ...}
            total = total.get("value")
        total = total if isinstance(total, int) and total >= 0 else len(entities)
        return SearchResult(
            source=self.source_name,
            query=query,
            total_results=total,
            entities=entities,
            page=page,
            per_page=params["limit"],
            raw_response={
                "total": total,
                "filtered_out_locally": dropped,
                "has_more": params["offset"] + len(results) < total,
            },
        )

    # -- fetch -------------------------------------------------------------

    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """One entity with its neighbours (two requests). None if Aleph has no such entity.

        Raises ValueError for a malformed id and AdapterRequestError when the
        entity cannot be read. If the neighbours cannot be read the entity is
        still returned, with the failure in ``extra_data["errors"]``.
        """
        entity_id = parse_identifier(identifier)
        path = f"/entities/{quote(entity_id, safe='')}"
        data = await self._get(path)
        if data is None:
            return None
        entity = _entity(data, fallback_id=entity_id)
        if entity is None:
            raise AdapterRequestError("Aleph returned an entity without an id or schema.")
        errors: list[str] = []
        expanded: dict[str, Any] = {}
        try:
            expanded = await self._get(path + "/expand") or {}
        except AdapterRequestError as exc:
            errors.append(f"related entities not retrieved: {exc}")
        related = _expanded(expanded, data)
        main = clean_ftm({**data, "id": entity.identifiers["aleph_id"]}) or {"properties": {}}
        ftm = [main, *(e for e in (clean_ftm(r["entity"]) for r in related) if e)]
        x = entity.extra_data
        x.update(
            properties=main["properties"],
            related=[
                {
                    "property": r["property"],
                    "schema": r["entity"].get("schema"),
                    "name": r["name"],
                    "id": r["id"],
                }
                for r in related
            ],
            errors=errors,
            ftm_entities=ftm,
            text_kind="aleph",
        )
        x["full_text"] = _entity_text(entity, _first_text(data))
        x["text_length"] = len(x["full_text"])
        return entity

    def store(
        self, entity: RecordEntity, case_id: str | None = None
    ) -> tuple[str | None, str | None]:
        """Save the entity as a local Document (ALEPH-<id>) and import its FtM graph."""
        ident = entity.identifiers["aleph_id"]
        doc_id, text = store_text_document(
            entity,
            request_number=f"ALEPH-{ident}",
            filename=f"aleph-{ident}.txt",
            body=f"Pulled from OCCRP Aleph: {entity.source_url}. {CREDIT}.",
            case_id=case_id,
        )
        ftm = entity.extra_data.get("ftm_entities") or []
        if doc_id and ftm:
            added, links = import_ftm_entities(
                ftm, name=f"aleph-{ident}", document_id=doc_id, tag="aleph"
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


def _valid_id(value: Any) -> str | None:
    try:
        return parse_identifier(str(value)) if value else None
    except ValueError:
        return None


def _entity(hit: dict[str, Any], fallback_id: str | None = None) -> RecordEntity | None:
    entity_id = _valid_id(hit.get("id")) or fallback_id
    schema = hit.get("schema")
    if (
        not entity_id
        or not isinstance(schema, str)
        or not re.match(r"^[A-Z][A-Za-z]{1,40}$", schema)
    ):
        return None
    props = _props(hit)
    collection = hit.get("collection") if isinstance(hit.get("collection"), dict) else {}
    name = _first(props, *_NAME_PROPS) or f"{schema} {entity_id[:12]}"
    jurisdiction = _first(props, "jurisdiction", "country", limit=10).lower()
    identifiers = {"aleph_id": entity_id}
    reg = _first(props, "registrationNumber", limit=100)
    if reg:
        identifiers["registration_number"] = reg
    highlight = hit.get("highlight")
    snippet = (
        " … ".join(strip_tags(h) for h in highlight[:3] if isinstance(h, str))
        if isinstance(highlight, list)
        else ""
    )
    countries = sorted(c for c in _countries(hit) if _COUNTRY.match(c))
    return RecordEntity(
        entity_type=_ENTITY_TYPES.get(
            schema, "DOCUMENT" if schema in _DOCUMENT_SCHEMATA else schema.upper()
        ),
        name=name,
        source="aleph",
        source_url=f"{SITE}/entities/{quote(entity_id, safe='')}",
        jurisdiction=jurisdiction if _COUNTRY.match(jurisdiction) else None,
        status=_first(props, "status", limit=60) or None,
        identifiers=identifiers,
        extra_data={
            "schema": schema,
            "dataset": clean_line(collection.get("label")) or None,
            "dataset_id": clean_line(collection.get("collection_id") or collection.get("id"), 40)
            or None,
            "category": clean_line(collection.get("category"), 40) or None,
            "countries": countries,
            "date": _date(_first(props, *_DATE_PROPS, limit=40)),
            "snippet": snippet or _first(props, "summary", "description", "notes", limit=500),
        },
    )


def _date(value: str) -> str | None:
    return value[:10] if re.match(r"\d{4}(-\d{2}(-\d{2})?)?", value or "") else None


def _first_text(hit: dict[str, Any]) -> str:
    props = _props(hit)
    for name in _TEXT_PROPS:
        values = props.get(name)
        if isinstance(values, list):
            text = "\n\n".join(v for v in values if isinstance(v, str))
            if text.strip():
                return clean_text(text, MAX_TEXT)
    return ""


def _nested(entity: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Entities embedded in reference properties: Aleph's API returns ``director: [{...}]``."""
    return [
        (key, value)
        for key, values in _props(entity).items()
        if isinstance(values, list)
        for value in values
        if isinstance(value, dict)
    ]


def _expanded(data: dict[str, Any], main: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Neighbour entities: from ``/expand`` (``results[*] = {property, entities}``), the
    entities embedded in the main entity's references, and those embedded one level down."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    main_id = _valid_id((main or {}).get("id"))
    if main_id:
        seen.add(main_id)

    def add(prop: str, ent: Any) -> None:
        if not isinstance(ent, dict) or len(out) >= MAX_RELATED:
            return
        ent_id = _valid_id(ent.get("id"))
        if not ent_id or ent_id in seen or not isinstance(ent.get("schema"), str):
            return
        seen.add(ent_id)
        out.append(
            {
                "property": prop,
                "id": ent_id,
                "name": _first(_props(ent), *_NAME_PROPS) or ent_id[:12],
                "entity": ent,
            }
        )

    groups: list[tuple[str, list[Any]]] = [(k, [v]) for k, v in _nested(main or {})]
    for group in data.get("results") or []:
        if isinstance(group, dict) and isinstance(group.get("entities"), list):
            groups.append((clean_line(group.get("property"), 60) or "related", group["entities"]))
    for prop, ents in groups:
        for ent in ents:
            add(prop, ent)
            if isinstance(ent, dict):
                for key, inner in _nested(ent):
                    add(f"{prop} > {clean_line(key, 60)}", inner)
    return out


def _entity_text(entity: RecordEntity, body_text: str) -> str:
    """A plain-text record: what is stored locally and searched with ``docs grep``."""
    x = entity.extra_data
    lines = [
        f"OCCRP Aleph entity {entity.identifiers['aleph_id']}",
        f"{x['schema']}: {entity.name}",
    ]
    if x.get("dataset"):
        lines.append(
            f"Dataset: {x['dataset']}" + (f" ({x['category']})" if x.get("category") else "")
        )
    if x.get("countries"):
        lines.append("Countries: " + ", ".join(x["countries"]))
    lines.append(f"Source: {entity.source_url}")
    lines.append(f"Credit: {CREDIT}")
    for err in x.get("errors") or []:
        lines.append(f"NOTE: {err}")
    props = x.get("properties") or {}
    order = [p for p in _SHOWN_FIRST if p in props] + sorted(
        p for p in props if p not in _SHOWN_FIRST and p not in _TEXT_PROPS
    )
    if order:
        lines += ["", "Properties"]
        lines += [f"- {p}: {'; '.join(props[p])}" for p in order]
    related = x.get("related") or []
    lines += ["", f"Related entities: {len(related)}"]
    lines += [f"- {r['property']}: {r['schema']} {r['name']} ({r['id']})" for r in related]
    if body_text:
        lines += ["", "Text", body_text]
    return "\n".join(lines) + "\n"
