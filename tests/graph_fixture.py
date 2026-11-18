"""Deterministic investigation fixture for the entity-graph tests.

Builds a made-up procurement investigation: several documents, a few hundred
entity mentions of mixed types, relationships inside each document, and names
in Cyrillic, Arabic, Hebrew and Chinese. Every name is invented.

The same entity is deliberately mentioned in more than one document, because
that is what a real case looks like: the graph page merges mentions that share
a type and a label into one node, so ``Spec.entity_keys`` (distinct entities)
and ``Spec.mentions`` (database rows) are different numbers.
"""

# ruff: noqa: RUF001  (the Cyrillic names are meant to be Cyrillic)
from __future__ import annotations

import random
from dataclasses import dataclass, field

# A sentence that exists only in document bodies, never in an entity label or a
# context snippet. --no-text output must not contain it.
BODY_SENTINEL = "ZQX-BODY-ONLY-SENTINEL-7731"

NON_LATIN = [
    ("person", "Алексей Воронцов"),
    ("person", "Ирина Белова"),
    ("person", "خالد المنصوري"),
    ("person", "ليلى الحداد"),
    ("person", "יעל שמואלי"),
    ("person", "אבנר רוזנטל"),
    ("person", "王建国"),
    ("person", "李秀英"),
    ("organization", "ООО «Северный Мост Инвест»"),
    ("organization", "شركة النور القابضة"),
    ("organization", "אופק השקעות בע״מ"),
    ("organization", "华东建设集团有限公司"),
    ("location", "Новосибирск"),
    ("location", "بيروت"),
    ("location", "חיפה"),
    ("location", "上海"),
]

_FIRST = [
    "Amara",
    "Bruno",
    "Carla",
    "Dmitri",
    "Elif",
    "Farid",
    "Grace",
    "Hugo",
    "Ines",
    "Jomo",
    "Katya",
    "Luis",
    "Mwangi",
    "Nadia",
    "Omar",
    "Priya",
    "Quentin",
    "Rosa",
    "Selim",
    "Tariq",
    "Uma",
    "Viktor",
    "Wanjiru",
    "Xavier",
    "Yusuf",
    "Zeynep",
    "Adaeze",
    "Burak",
    "Chiara",
    "Davi",
]
_LAST = [
    "Okafor",
    "Silva",
    "Demir",
    "Kimani",
    "Haddad",
    "Petrov",
    "Moreau",
    "Yilmaz",
    "Santos",
    "Mutua",
    "Rahman",
    "Costa",
    "Njoroge",
    "Aydin",
    "Ferreira",
    "Kovacs",
    "Mbeki",
    "Oliveira",
    "Kaya",
    "Otieno",
    "Almeida",
    "Arslan",
    "Barros",
    "Wekesa",
]
_ORG_A = [
    "Meridian",
    "Kestrel",
    "Anatolia",
    "Savanna",
    "Atlantico",
    "Bosphorus",
    "Rift",
    "Paulista",
    "Halcyon",
    "Orion",
    "Baobab",
    "Marmara",
    "Tietê",
    "Corvus",
    "Levant",
    "Ngong",
    "Tagus",
    "Altin",
    "Serra",
    "Umoja",
]
_ORG_B = [
    "Holdings Ltd",
    "Trading LLC",
    "Construction A.Ş.",
    "Logistics S.A.",
    "Capital Partners",
    "Foundation",
    "Consulting Ltda",
    "Infrastructure Group",
    "Shipping Co.",
    "Development Bank",
    "Ministry Tender Board",
    "Mining Corp",
]
_PLACES = (
    "Istanbul Nairobi São Paulo Ankara Mombasa Brasília Izmir Kisumu Recife Nicosia Dubai "
    "Geneva Valletta Limassol Panama City Tortola Riga Tbilisi Lagos Accra Luanda Maputo "
    "Lisbon Zug Vaduz Singapore Baku Astana Doha Beirut Amman Tunis Dakar Kampala Kigali "
    "Curitiba Manaus Bursa Adana Eldoret Nakuru Santos Porto Alegre Belgrade Chişinău"
)
_PLACE_LIST: list[str] = []
for _tok in _PLACES.split():
    # Re-join the two-word place names split above.
    if _tok in {"Paulo", "City", "Alegre"}:
        _PLACE_LIST[-1] += " " + _tok
    else:
        _PLACE_LIST.append(_tok)
_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
]
_RELATIONS = {
    ("person", "organization"): ["director_of", "employed_by", "shareholder_of"],
    ("organization", "organization"): ["subsidiary_of", "contracted_by", "paid"],
    ("person", "person"): ["associate_of", "relative_of"],
    ("organization", "location"): ["registered_in", "operates_in"],
    ("person", "location"): ["resident_of"],
    ("organization", "money"): ["received", "paid"],
    ("person", "money"): ["received"],
}


@dataclass
class Mention:
    doc: int
    type: str
    label: str
    confidence: float
    page: int
    context: str


@dataclass
class Spec:
    documents: list[dict] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    # (index into mentions, index into mentions, relation); both in one document.
    links: list[tuple[int, int, str]] = field(default_factory=list)
    # Planted structure, keyed by (type, label). Tests compare the page to it.
    cluster: dict[tuple[str, str], int] = field(default_factory=dict)
    main_hubs: list[tuple[str, str]] = field(default_factory=list)
    bridge_links: int = 0

    @property
    def entity_keys(self) -> set[tuple[str, str]]:
        """Distinct entities: what the page draws as one node each."""
        return {(m.type, m.label) for m in self.mentions}

    @property
    def types(self) -> set[str]:
        return {m.type for m in self.mentions}

    def neighbours(self) -> dict[tuple[str, str], set[tuple[str, str]]]:
        """True neighbour set of every entity, from the relationship rows."""
        out: dict[tuple[str, str], set[tuple[str, str]]] = {k: set() for k in self.entity_keys}
        for a, b, _ in self.links:
            ka = (self.mentions[a].type, self.mentions[a].label)
            kb = (self.mentions[b].type, self.mentions[b].label)
            if ka != kb:
                out[ka].add(kb)
                out[kb].add(ka)
        return out

    def mentions_of(self, key: tuple[str, str]) -> list[Mention]:
        return [m for m in self.mentions if (m.type, m.label) == key]


def _entity_pool(rng: random.Random, n: int) -> list[tuple[str, str]]:
    pool: list[tuple[str, str]] = list(NON_LATIN)
    seen = set(pool)
    # Rough mix of a document-heavy investigation.
    weights = [("person", 34), ("organization", 28), ("location", 14), ("money", 12), ("date", 12)]
    kinds = [k for k, w in weights for _ in range(w)]
    while len(pool) < n:
        kind = rng.choice(kinds)
        if kind == "person":
            label = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
        elif kind == "organization":
            label = f"{rng.choice(_ORG_A)} {rng.choice(_ORG_B)}"
        elif kind == "location":
            label = rng.choice(_PLACE_LIST)
        elif kind == "money":
            cur = rng.choice(["$", "€", "KSh ", "R$ ", "₺"])
            label = f"{cur}{rng.randrange(12, 9800) * 1000:,}"
        else:
            label = f"{rng.randrange(1, 29)} {rng.choice(_MONTHS)} {rng.randrange(2016, 2025)}"
        if (kind, label) not in seen:
            seen.add((kind, label))
            pool.append((kind, label))
    return pool


def build_spec(n_entities: int = 260, n_docs: int = 9, n_clusters: int = 3, seed: int = 7) -> Spec:
    """Build a fixture with *n_entities* distinct entities across *n_docs* documents.

    The structure is planted so tests have a known right answer:

    - *n_clusters* separate strands of the investigation. Each document belongs
      to one strand and each entity to one strand.
    - Each strand has one main hub (an organization tied to roughly 45% of the
      strand's people, payments, places and dates) and two smaller hubs.
    - Strands are joined only by a few bridges: an entity of one strand that
      is also mentioned once, with one relationship, in a document of the next.
    - About one entity in ten is extracted but tied to nothing (an isolate).
    """
    rng = random.Random(seed)
    pool = _entity_pool(rng, n_entities)
    spec = Spec()
    for d in range(n_docs):
        pages = rng.randrange(6, 60)
        body = (
            f"TENDER FILE {d + 1}\n\n{BODY_SENTINEL} "
            + "The committee reviewed the submitted bids and the payment schedule. " * 40
        )
        spec.documents.append(
            {"filename": f"tender-file-{d + 1:02d}.pdf", "pages": pages, "text": body}
        )
    docs_in = {c: [d for d in range(n_docs) if d % n_clusters == c] for c in range(n_clusters)}

    order = list(range(len(pool)))
    rng.shuffle(order)
    cluster_of = {i: pos % n_clusters for pos, i in enumerate(order)}
    members = {c: [i for i in order if cluster_of[i] == c] for c in range(n_clusters)}
    for i, key in enumerate(pool):
        spec.cluster[key] = cluster_of[i]

    main_hub: dict[int, int] = {}
    minor_hubs: dict[int, list[int]] = {}
    for c in range(n_clusters):
        orgs = [i for i in members[c] if pool[i][0] == "organization"]
        people = [i for i in members[c] if pool[i][0] == "person"]
        main_hub[c] = orgs[0]
        minor_hubs[c] = [people[0], orgs[1]]
        spec.main_hubs.append(pool[orgs[0]])
    hubs = set(main_hub.values()) | {i for v in minor_hubs.values() for i in v}
    isolates = {i for i in order if i not in hubs and rng.random() < 0.10}

    index_of: dict[tuple[int, int], int] = {}
    by_doc: dict[int, list[int]] = {d: [] for d in range(n_docs)}

    def mention(d: int, i: int) -> None:
        kind, label = pool[i]
        conf = rng.choice([0.45, 0.55, 0.62, 0.7, 0.78, 0.85, 0.9, 0.95, 0.99])
        page = rng.randrange(1, spec.documents[d]["pages"] + 1)
        context = f"… the tender board noted that {label} appeared in schedule {page} …"
        index_of[(d, i)] = len(spec.mentions)
        by_doc[d].append(i)
        spec.mentions.append(Mention(d, kind, label, conf, page, context))

    for i in order:
        own = docs_in[cluster_of[i]]
        if i in hubs:
            chosen = own
        elif rng.random() < 0.12 and len(own) > 1:
            chosen = rng.sample(own, 2)
        else:
            chosen = [rng.choice(own)]
        for d in chosen:
            mention(d, i)

    seen_links: set[tuple[int, int]] = set()

    def link(d: int, a: int, b: int) -> bool:
        if a == b:
            return False
        ia, ib = index_of[(d, a)], index_of[(d, b)]
        if (ia, ib) in seen_links or (ib, ia) in seen_links:
            return False
        seen_links.add((ia, ib))
        ta, tb = pool[a][0], pool[b][0]
        rels = _RELATIONS.get((ta, tb)) or _RELATIONS.get((tb, ta)) or ["related_to"]
        spec.links.append((ia, ib, rng.choice(rels)))
        return True

    for d in range(n_docs):
        c = d % n_clusters
        actors = [
            i
            for i in by_doc[d]
            if pool[i][0] in ("person", "organization") and i not in isolates and i not in hubs
        ]
        for i in by_doc[d]:
            if i in isolates or i in hubs:
                continue
            roll = rng.random()
            if roll < 0.45:
                link(d, i, main_hub[c])
            elif roll < 0.65:
                link(d, i, rng.choice(minor_hubs[c]))
            elif actors:
                link(d, i, rng.choice(actors))
            if actors and rng.random() < 0.25:
                link(d, i, rng.choice(actors))
        if d == docs_in[c][0]:
            for h in minor_hubs[c]:
                link(d, main_hub[c], h)

    # Bridges: two per neighbouring pair of strands, one relationship each.
    for c in range(n_clusters - 1):
        candidates = [
            i for i in members[c] if pool[i][0] == "person" and i not in hubs and i not in isolates
        ]
        for i in candidates[:2]:
            d = docs_in[c + 1][0]
            others = [
                j
                for j in by_doc[d]
                if pool[j][0] == "organization" and j not in hubs and j not in isolates
            ]
            mention(d, i)
            if link(d, i, rng.choice(others)):
                spec.bridge_links += 1

    # Document bodies name their entities, so the reader view has text to mark.
    for m in spec.mentions:
        spec.documents[m.doc]["text"] += f"\nSchedule {m.page}: {m.label} is named in the record."
    return spec


def load_spec(spec: Spec) -> None:
    """Write *spec* into the database selected by ``OPENFOIA_DATA_DIR``."""
    from openfoia.db import get_session, init_db
    from openfoia.models import (
        Agency,
        DeliveryMethod,
        Document,
        DocumentType,
        Entity,
        EntityType,
        Request,
        User,
        entity_links,
    )

    init_db()
    with get_session() as session:
        user = session.query(User).first() or User(email="fixture@example.invalid", name="Fixture")
        session.add(user)
        session.flush()
        req = Request(
            request_number="FIX-GRAPH-0001",
            requester_id=user.id,
            agency_id=session.query(Agency).first().id,
            subject="Port expansion tender",
            body="",
            delivery_method=DeliveryMethod.EMAIL,
        )
        session.add(req)
        session.flush()
        doc_ids = []
        for d in spec.documents:
            doc = Document(
                request_id=req.id,
                doc_type=DocumentType.FULL_RESPONSE,
                filename=d["filename"],
                file_path="",
                file_size=len(d["text"]),
                mime_type="application/pdf",
                page_count=d["pages"],
                extracted_text=d["text"],
            )
            session.add(doc)
            session.flush()
            doc_ids.append(doc.id)
        ids = []
        for m in spec.mentions:
            ent = Entity(
                document_id=doc_ids[m.doc],
                entity_type=EntityType(m.type),
                raw_text=m.label,
                normalized_text=m.label,
                confidence=m.confidence,
                page_number=m.page,
                context=m.context,
            )
            session.add(ent)
            session.flush()
            ids.append(ent.id)
        for a, b, rel in spec.links:
            session.execute(
                entity_links.insert().values(source_id=ids[a], target_id=ids[b], link_type=rel)
            )
