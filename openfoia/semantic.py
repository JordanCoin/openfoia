"""Offline semantic search over stored document pages (optional extra).

``docs grep`` matches words. A journalist searching "payments to the mayor's
brother" misses a page that says "funds transferred to a sibling of the city
executive". This module embeds page text with a small static embedding model
(model2vec, CPU only) and ranks pages by meaning.

Design, and why:

* **Optional.** Needs ``openfoia install-extras semantic`` (model2vec +
  sqlite-vec). Nothing here is imported by the core; every heavy import is
  inside a function. Missing pieces raise :class:`SemanticUnavailable` with
  the command that fixes them.
* **The model download is the only network action, and it is explicit.**
  :func:`download_model` is called by ``openfoia docs embed-model`` only,
  after a warning. It fetches pinned files at a pinned revision through the
  egress choke point (``openfoia.net``, so ``--tor`` works) and checks every
  file's SHA-256. Files go under ``get_data_dir()/models`` (portable mode
  respected), never the Hugging Face cache. Loading never touches the network:
  model2vec is only ever handed an existing local directory, and the Hugging
  Face hub library is put in offline mode before it is imported.
* **Vectors live in an ordinary table** (``document_page_vectors``) in the
  same database file as the pages, so they are encrypted with it under
  SQLCipher and destroyed with it by ``purge``. A plain table (not a sqlite-vec
  ``vec0`` virtual table) means nothing in the schema depends on the
  extension: page inserts and deletes, ``db encrypt`` (``iterdump``) and the
  numpy fallback all work on a machine where sqlite-vec cannot load.
* **Purged with the page.** Triggers on ``document_pages`` and ``documents``
  delete a page's vectors when the page is deleted or its text changes, and
  foreign keys cascade as a second line. Embeddings are derived data that can
  leak content (embedding inversion), so they must never outlive their text.
* **Two search paths.** sqlite-vec's ``vec_distance_cosine`` when the
  extension loads into the connection (it does under sqlcipher3-binary), else
  brute-force cosine in numpy (numpy comes with model2vec). Results report
  which one ran.
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import text as sql

from .doctext import _doc_info, backfill_single_pages
from .models import DocumentPage

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import Session

    from .net import EgressPolicy

VECTOR_TABLE = "document_page_vectors"
INSTALL_HINT = "openfoia install-extras semantic"
DOWNLOAD_HINT = "openfoia docs embed-model"

#: Words per embedded chunk, and how many words consecutive chunks share.
#: Static embeddings average their tokens, so one vector for a whole page
#: blurs a single relevant sentence into everything around it; chunks keep a
#: passage findable and give ``grep --semantic`` a passage to show. 25-40
#: words found paraphrases far more often than 100 on the #118 corpus; 40
#: keeps the index ~40% smaller than 25 (docs/benchmarks/semantic-search.md).
CHUNK_WORDS = 40
CHUNK_OVERLAP = 10
# Thai/Lao, Myanmar, Khmer, CJK, Hangul: written without spaces between words.
_NO_SPACE = "\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff"
_WORD = re.compile(f"[{_NO_SPACE}]{{1,2}}|[^\\s{_NO_SPACE}]+")

_DDL = [
    f"CREATE TABLE IF NOT EXISTS {VECTOR_TABLE} ("
    "id INTEGER PRIMARY KEY, "
    "page_id INTEGER NOT NULL REFERENCES document_pages(id) ON DELETE CASCADE, "
    "document_id VARCHAR(36) NOT NULL REFERENCES documents(id) ON DELETE CASCADE, "
    "chunk INTEGER NOT NULL, "
    "char_start INTEGER NOT NULL, "
    "char_end INTEGER NOT NULL, "
    "model VARCHAR(64) NOT NULL, "
    "dim INTEGER NOT NULL, "
    "vector BLOB NOT NULL)",
    f"CREATE INDEX IF NOT EXISTS ix_{VECTOR_TABLE}_page ON {VECTOR_TABLE}(page_id)",
    f"CREATE INDEX IF NOT EXISTS ix_{VECTOR_TABLE}_model_doc ON {VECTOR_TABLE}(model, document_id)",
    # Triggers fire whether or not PRAGMA foreign_keys is on for the
    # connection doing the delete; the FK cascade above is the second line.
    "CREATE TRIGGER IF NOT EXISTS document_pages_vec_ad AFTER DELETE ON document_pages "
    f"BEGIN DELETE FROM {VECTOR_TABLE} WHERE page_id = old.id; END",
    "CREATE TRIGGER IF NOT EXISTS document_pages_vec_au AFTER UPDATE OF text ON document_pages "
    f"BEGIN DELETE FROM {VECTOR_TABLE} WHERE page_id = old.id; END",
    "CREATE TRIGGER IF NOT EXISTS documents_vec_ad AFTER DELETE ON documents "
    f"BEGIN DELETE FROM {VECTOR_TABLE} WHERE document_id = old.id; END",
]
_TRIGGERS = ("document_pages_vec_ad", "document_pages_vec_au", "documents_vec_ad")


class SemanticUnavailable(RuntimeError):
    """The semantic extra or its model is missing. The message says how to fix it."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo: str
    revision: str  # a commit, never a branch: the files below are pinned to it
    files: dict[str, tuple[int, str]]  # filename -> (bytes, sha256)
    languages: str
    about: str

    @property
    def download_bytes(self) -> int:
        return sum(size for size, _ in self.files.values())


MODELS: dict[str, ModelSpec] = {
    m.name: m
    for m in (
        ModelSpec(
            name="potion-base-8M",
            repo="minishlab/potion-base-8M",
            revision="bf8b056651a2c21b8d2565580b8569da283cab23",
            files={
                "config.json": (
                    202,
                    "2a6ac0e9aaa356a68a5688070db78fc3a464fefe85d2f06a1905ce3718687553",
                ),
                "tokenizer.json": (
                    683666,
                    "e67e803f624fb4d67dea1c730d06e1067e1b14d830e2c2202569e3ef0f70bb50",
                ),
                "model.safetensors": (
                    30236760,
                    "f65d0f325faadc1e121c319e2faa41170d3fa07d8c89abd48ca5358d9a223de2",
                ),
            },
            languages="English",
            about="small and fast; English only",
        ),
        ModelSpec(
            name="potion-retrieval-32M",
            repo="minishlab/potion-retrieval-32M",
            revision="6fc8051fab2a1e0ee76689cf08c853792ac285e7",
            files={
                "config.json": (
                    202,
                    "63c00d90824c832c04ec1d02b6a983fb90489bf049f29fbff15ba481b8a432ee",
                ),
                "tokenizer.json": (
                    1493150,
                    "7d75cbc54318138807c401b0f0c9721117c628b39de8e8e0edb6cb17e0ee7d18",
                ),
                "model.safetensors": (
                    129210456,
                    "07609e5bd33aad37900b3fd62f4ec96f6daec88ca4d46b9d8b928bfababf6ea0",
                ),
            },
            languages="English",
            about="tuned for search; English only; 512-dim vectors (2x the index size)",
        ),
        ModelSpec(
            name="potion-multilingual-128M",
            repo="minishlab/potion-multilingual-128M",
            revision="73908c3438cf03b6a01bcb9611d62b23d0726f08",
            files={
                "config.json": (
                    271,
                    "595e4cab2093732efd5dbe084fd5c1826b5eea693b73b4c1fd971672867d2e54",
                ),
                "tokenizer.json": (
                    18616131,
                    "19f1909063da3cfe3bd83a782381f040dccea475f4816de11116444a73e1b6a1",
                ),
                "model.safetensors": (
                    512361560,
                    "14b5eb39cb4ce5666da8ad1f3dc6be4346e9b2d601c073302fa0a31bf7943397",
                ),
            },
            languages="101 languages",
            about="multilingual; 531 MB download, ~1.9 GB peak RAM while loading",
        ),
    )
}
DEFAULT_MODEL = "potion-base-8M"
_MARKER = "openfoia-model.json"


def models_dir() -> Path:
    """Where models live. Not created here: only a download creates it."""
    from .db import get_data_dir  # lazy: db import probes for SQLCipher

    return get_data_dir() / "models"


def model_path(name: str) -> Path:
    return models_dir() / name


def get_spec(name: str) -> ModelSpec:
    try:
        return MODELS[name]
    except KeyError:
        raise SemanticUnavailable(
            f"Unknown embedding model '{name}'. Known: {', '.join(MODELS)}"
        ) from None


def model_installed(name: str) -> bool:
    """All pinned files present at their pinned sizes (hashes are checked on download)."""
    spec = get_spec(name)
    folder = model_path(name)
    if not (folder / _MARKER).is_file():
        return False
    for fname, (size, _) in spec.files.items():
        f = folder / fname
        if not f.is_file() or f.stat().st_size != size:
            return False
    return True


def installed_models() -> list[str]:
    return [n for n in MODELS if model_installed(n)]


def resolve_model_name(name: str | None) -> str:
    """The model to use: *name*, else the default if downloaded, else any downloaded one."""
    if name:
        get_spec(name)
        return name
    have = installed_models()
    if DEFAULT_MODEL in have or not have:
        return DEFAULT_MODEL
    return have[0]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_model(name: str) -> list[str]:
    """Re-hash every file. Returns the problems found (empty when the model is intact)."""
    spec = get_spec(name)
    folder = model_path(name)
    problems = []
    for fname, (size, digest) in spec.files.items():
        f = folder / fname
        if not f.is_file():
            problems.append(f"{fname}: missing")
        elif f.stat().st_size != size:
            problems.append(f"{fname}: {f.stat().st_size} bytes, expected {size}")
        elif _sha256(f) != digest:
            problems.append(f"{fname}: SHA-256 does not match the pinned value")
    return problems


def download_url(spec: ModelSpec, fname: str) -> str:
    return f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{fname}"


async def _download(
    spec: ModelSpec,
    policy: EgressPolicy | None,
    progress: Callable[[str, int, int], None] | None,
    transport: Any = None,
) -> Path:
    from .db import _ensure_private_dir  # lazy: db import probes for SQLCipher
    from .net import egress_client  # lazy: monkeypatched in tests

    _ensure_private_dir(models_dir())
    folder = _ensure_private_dir(model_path(spec.name))
    kwargs: dict[str, Any] = {"follow_redirects": True}
    if transport is not None:
        kwargs["transport"] = transport
    async with egress_client(policy, timeout=120.0, **kwargs) as client:
        for fname, (size, digest) in spec.files.items():
            dest = folder / fname
            if dest.is_file() and dest.stat().st_size == size and _sha256(dest) == digest:
                if progress:
                    progress(fname, size, size)
                continue
            part = folder / (fname + ".part")
            h = hashlib.sha256()
            got = 0
            async with client.stream("GET", download_url(spec, fname)) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"{fname}: HTTP {resp.status_code} from {resp.url.host}")
                with open(part, "wb") as out:
                    async for block in resp.aiter_bytes(1 << 20):
                        out.write(block)
                        h.update(block)
                        got += len(block)
                        if got > size:
                            break
                        if progress:
                            progress(fname, got, size)
            if got != size or h.hexdigest() != digest:
                part.unlink(missing_ok=True)
                raise RuntimeError(
                    f"{fname}: downloaded file does not match the pinned size/SHA-256 "
                    "(nothing was installed). Retry, or report it if it persists."
                )
            part.replace(dest)
    marker = {"repo": spec.repo, "revision": spec.revision, "files": sorted(spec.files)}
    (folder / _MARKER).write_text(json.dumps(marker, indent=2))
    return folder


def download_model(
    name: str,
    policy: EgressPolicy | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    transport: Any = None,
) -> Path:
    """Fetch the pinned files of model *name* into the data directory.

    This is a network action. Only ``openfoia docs embed-model`` calls it, after
    telling the user what is sent where. Hugging Face (and, with Tor, the exit
    node) sees the requesting IP and which model files are fetched; nothing
    about the user's documents is sent.
    """
    return asyncio.run(_download(get_spec(name), policy, progress, transport))


# ---------------------------------------------------------------------------
# Embedders
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


def _import_model2vec() -> Any:
    # Belt and braces: we only ever pass a local directory, which model2vec
    # loads without the hub, but if anything inside reaches for the hub it
    # must fail rather than download, and must not send telemetry. The hub
    # library reads these once, at import, so they hold for the rest of this
    # process (one CLI command) if it was not imported earlier.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        from model2vec import StaticModel  # optional: semantic
    except ImportError:
        raise SemanticUnavailable(
            f"Semantic search needs the optional extra. Run: {INSTALL_HINT}"
        ) from None
    return StaticModel


class Model2VecEmbedder:
    """A downloaded model2vec model, loaded from the data directory only."""

    def __init__(self, name: str):
        static_model = _import_model2vec()
        if not model_installed(name):
            spec = get_spec(name)
            raise SemanticUnavailable(
                f"The embedding model '{name}' is not downloaded. Run: {DOWNLOAD_HINT}"
                + ("" if name == DEFAULT_MODEL else f" {name}")
                + f"  (~{spec.download_bytes / 1e6:.0f} MB from huggingface.co, once)"
            )
        folder = model_path(name)
        # An existing directory: model2vec never falls back to the hub for it.
        self._model = static_model.from_pretrained(str(folder), force_download=False)
        self.name = name
        self.dim = int(self._model.dim)

    def encode(self, texts: Sequence[str]) -> Any:
        return self._model.encode(
            list(texts), show_progress_bar=False, use_multiprocessing=False, batch_size=256
        )


def load_embedder(name: str | None = None) -> Model2VecEmbedder:
    return Model2VecEmbedder(resolve_model_name(name))


def extra_status() -> dict[str, Any]:
    """What is installed, without importing anything heavy or touching the network."""
    return {
        "model2vec": importlib.util.find_spec("model2vec") is not None,
        "sqlite_vec": importlib.util.find_spec("sqlite_vec") is not None,
        "numpy": importlib.util.find_spec("numpy") is not None,
        "models": installed_models(),
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def pack(vector: Iterable[float]) -> bytes:
    """float32 little-endian, the layout sqlite-vec reads."""
    a = array.array("f", (float(x) for x in vector))
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def unpack(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    if sys.byteorder != "little":
        a.byteswap()
    return a.tolist()


def ensure_vector_table(conn: Connection) -> None:
    """Create the vector table and its purge triggers if missing (idempotent).

    Created on first use rather than by a migration, so a database that never
    uses semantic search never carries the table. Triggers are re-created if a
    batch migration that rebuilt ``document_pages`` dropped them, and vectors
    whose page or document is gone are removed.
    """
    have = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE name IN (?, ?, ?, ?)",
            (VECTOR_TABLE, *_TRIGGERS),
        )
    }
    if have == {VECTOR_TABLE, *_TRIGGERS}:
        return
    for stmt in _DDL:
        conn.exec_driver_sql(stmt)
    if VECTOR_TABLE in have:
        sweep_orphans(conn)


def vector_table_exists(conn: Connection) -> bool:
    return (
        conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (VECTOR_TABLE,)
        ).first()
        is not None
    )


def sweep_orphans(conn: Connection) -> int:
    """Delete vectors whose page is gone or now belongs to another document."""
    if not vector_table_exists(conn):
        return 0
    res = conn.exec_driver_sql(
        f"DELETE FROM {VECTOR_TABLE} WHERE NOT EXISTS (SELECT 1 FROM document_pages p "
        f"WHERE p.id = {VECTOR_TABLE}.page_id AND p.document_id = {VECTOR_TABLE}.document_id)"
    )
    return res.rowcount or 0


def drop_vectors(conn: Connection, model: str | None = None) -> int:
    """Delete stored vectors (all, or one model's).

    ``secure_delete`` makes SQLite zero the freed space instead of leaving the
    old vectors in free pages until they happen to be reused.
    """
    if not vector_table_exists(conn):
        return 0
    conn.exec_driver_sql("PRAGMA secure_delete = ON")
    if model:
        res = conn.exec_driver_sql(f"DELETE FROM {VECTOR_TABLE} WHERE model = ?", (model,))
    else:
        res = conn.exec_driver_sql(f"DELETE FROM {VECTOR_TABLE}")
    return res.rowcount or 0


def chunk_spans(text: str, words: int | None = None, overlap: int | None = None):
    """(start, end) character spans of overlapping word windows over *text*.

    Scripts written without spaces (Chinese, Japanese, Korean, Thai, Khmer,
    Myanmar) count every two characters as a word, so a page of them is not
    one giant "word".
    """
    words = words or CHUNK_WORDS
    overlap = CHUNK_OVERLAP if overlap is None else overlap
    toks = [m.span() for m in _WORD.finditer(text)]
    if not toks:
        return []
    step = max(1, words - overlap)
    spans = []
    for i in range(0, len(toks), step):
        window = toks[i : i + words]
        spans.append((window[0][0], window[-1][1]))
        if i + words >= len(toks):
            break
    return spans


def _missing_pages_sql(doc_filter: str = "") -> str:
    return (
        "SELECT p.id, p.document_id, p.text FROM document_pages p "
        "WHERE trim(p.text) != '' AND NOT EXISTS "
        f"(SELECT 1 FROM {VECTOR_TABLE} v WHERE v.page_id = p.id AND v.model = :model)"
        f"{doc_filter} ORDER BY p.id"
    )


def _doc_filter(document_ids: list[str] | None, alias: str) -> tuple[str, dict[str, str]]:
    if not document_ids:
        return "", {}
    keys = [f"d{i}" for i in range(len(document_ids))]
    clause = f" AND {alias}.document_id IN ({', '.join(':' + k for k in keys)})"
    return clause, dict(zip(keys, document_ids, strict=True))


def count_missing(
    session: Session, model: str, document_ids: list[str] | None = None
) -> tuple[int, int]:
    """(pages with text and no vectors for *model*, pages with text)."""
    conn = session.connection()
    ensure_vector_table(conn)
    flt, params = _doc_filter(document_ids, "p")
    missing = conn.execute(
        sql(f"SELECT count(*) FROM ({_missing_pages_sql(flt)})"), {"model": model, **params}
    ).scalar()
    total = conn.execute(
        sql(f"SELECT count(*) FROM document_pages p WHERE trim(p.text) != ''{flt}"), params
    ).scalar()
    return int(missing or 0), int(total or 0)


def embed_missing(
    session: Session,
    embedder: Embedder,
    *,
    document_ids: list[str] | None = None,
    batch_pages: int = 64,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Embed every stored page that has text but no vectors for this model.

    Runs entirely offline once the model is loaded. Returns pages embedded.
    Commits per batch, so an interrupted backfill keeps what it finished.
    """
    conn = session.connection()
    backfill_single_pages(conn)
    ensure_vector_table(conn)
    flt, params = _doc_filter(document_ids, "p")
    rows = conn.execute(sql(_missing_pages_sql(flt)), {"model": embedder.name, **params}).fetchall()
    total = len(rows)
    done = 0
    for b in range(0, total, batch_pages):
        batch = rows[b : b + batch_pages]
        chunks: list[tuple[int, str, int, int, int]] = []
        texts: list[str] = []
        for page_id, doc_id, text in batch:
            for n, (s, e) in enumerate(chunk_spans(text)):
                chunks.append((page_id, doc_id, n, s, e))
                texts.append(text[s:e])
        vectors = embedder.encode(texts) if texts else []
        conn.execute(
            sql(
                f"INSERT INTO {VECTOR_TABLE} "
                "(page_id, document_id, chunk, char_start, char_end, model, dim, vector) "
                "VALUES (:p, :d, :c, :s, :e, :m, :n, :v)"
            ),
            [
                {
                    "p": p,
                    "d": d,
                    "c": c,
                    "s": s,
                    "e": e,
                    "m": embedder.name,
                    "n": embedder.dim,
                    "v": pack(vec),
                }
                for (p, d, c, s, e), vec in zip(chunks, vectors, strict=True)
            ],
        )
        session.commit()
        conn = session.connection()
        done += len(batch)
        if progress:
            progress(done, total)
    return total


@dataclass
class IndexStatus:
    table: bool
    pages_with_text: int
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def index_status(session: Session) -> IndexStatus:
    conn = session.connection()
    total = conn.exec_driver_sql(
        "SELECT count(*) FROM document_pages WHERE trim(text) != ''"
    ).scalar()
    if not vector_table_exists(conn):
        return IndexStatus(False, int(total or 0))
    by_model = {
        m: {"pages": int(p), "chunks": int(c), "bytes": int(b or 0)}
        for m, p, c, b in conn.exec_driver_sql(
            f"SELECT model, count(DISTINCT page_id), count(*), sum(length(vector)) "
            f"FROM {VECTOR_TABLE} GROUP BY model"
        )
    }
    return IndexStatus(True, int(total or 0), by_model)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def load_sqlite_vec(conn: Connection) -> tuple[bool, str]:
    """Load sqlite-vec into this connection. Returns (loaded, reason if not).

    Fails on Pythons built without extension loading (macOS system Python,
    some distro builds) and when sqlite-vec is not installed; callers then
    use the numpy path. Extension loading is switched off again straight
    after, so SQL cannot load other extensions through this connection.
    """
    raw = conn.connection.dbapi_connection
    try:
        raw.execute("SELECT vec_version()").fetchone()
        return True, ""
    except Exception:  # not loaded yet on this connection
        pass
    try:
        import sqlite_vec  # optional: semantic
    except ImportError:
        return False, "sqlite-vec is not installed"
    enable = getattr(raw, "enable_load_extension", None)
    if enable is None:
        return False, "this Python's sqlite3 cannot load extensions (e.g. macOS system Python)"
    try:
        enable(True)
        try:
            raw.load_extension(sqlite_vec.loadable_path())
        finally:
            enable(False)
        raw.execute("SELECT vec_version()").fetchone()
    except Exception as e:
        return False, f"sqlite-vec did not load: {e}"
    return True, ""


@dataclass
class SemanticHit:
    document_id: str
    title: str
    page: int
    passage: str
    score: float  # cosine similarity, 1.0 = same direction
    page_count: int | None
    stored_pages: int
    pages_are_real: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _rank_sqlite_vec(conn, qvec: bytes, model: str, flt: str, params: dict, k: int):
    rows = conn.execute(
        sql(
            f"SELECT v.page_id, v.char_start, v.char_end, 1.0 - vec_distance_cosine(v.vector, :q) "
            f"FROM {VECTOR_TABLE} v WHERE v.model = :model{flt} "
            "ORDER BY vec_distance_cosine(v.vector, :q) LIMIT :k"
        ),
        {"q": qvec, "model": model, "k": k, **params},
    ).fetchall()
    return [(p, s, e, float(score)) for p, s, e, score in rows]


def _rank_numpy(conn, qvec: Sequence[float], model: str, flt: str, params: dict, k: int):
    try:
        import numpy as np  # optional: semantic
    except ImportError:
        raise SemanticUnavailable(
            f"Neither sqlite-vec nor numpy is available. Run: {INSTALL_HINT}"
        ) from None
    rows = conn.execute(
        sql(
            f"SELECT v.page_id, v.char_start, v.char_end, v.vector FROM {VECTOR_TABLE} v "
            f"WHERE v.model = :model{flt}"
        ),
        {"model": model, **params},
    ).fetchall()
    if not rows:
        return []
    mat = np.frombuffer(b"".join(r[3] for r in rows), dtype="<f4").reshape(len(rows), -1)
    q = np.asarray(qvec, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) or 1.0)
    scores = (mat @ q) / np.where(norms == 0, 1.0, norms)
    top = np.argsort(-scores)[:k]
    return [(rows[i][0], rows[i][1], rows[i][2], float(scores[i])) for i in top]


def semantic_search(
    session: Session,
    query: str,
    embedder: Embedder,
    *,
    limit: int = 20,
    document_ids: list[str] | None = None,
    backend: str | None = None,
) -> tuple[list[SemanticHit], str, str]:
    """Rank stored pages by meaning. Returns (hits, backend used, why not sqlite-vec).

    One hit per page (its best chunk). *backend* forces "sqlite-vec" or
    "numpy"; ``OPENFOIA_SEMANTIC_BACKEND`` does the same. Pages without
    vectors are not searched: call :func:`embed_missing` first.
    """
    conn = session.connection()
    ensure_vector_table(conn)
    backend = backend or os.environ.get("OPENFOIA_SEMANTIC_BACKEND") or None
    reason = ""
    if backend == "numpy":
        use_vec, reason = False, "numpy requested"
    else:
        use_vec, reason = load_sqlite_vec(conn)
        if backend == "sqlite-vec" and not use_vec:
            raise SemanticUnavailable(f"sqlite-vec backend requested but {reason}")

    qvec = list(embedder.encode([query])[0])
    flt, params = _doc_filter(document_ids, "v")
    # Over-fetch: several chunks of one page may rank near each other.
    k = max(limit * 5, 50)
    if use_vec:
        ranked = _rank_sqlite_vec(conn, pack(qvec), embedder.name, flt, params, k)
        used = "sqlite-vec"
    else:
        ranked = _rank_numpy(conn, qvec, embedder.name, flt, params, k)
        used = "numpy"

    best: dict[int, tuple[int, int, float]] = {}
    for page_id, s, e, score in ranked:
        if page_id not in best or score > best[page_id][2]:
            best[page_id] = (s, e, score)
    order = sorted(best.items(), key=lambda kv: -kv[1][2])[:limit]
    if not order:
        return [], used, reason
    pages = {
        p.id: p
        for p in session.query(DocumentPage).filter(DocumentPage.id.in_([o[0] for o in order]))
    }
    info = _doc_info(session, {p.document_id for p in pages.values()})
    hits = []
    for page_id, (s, e, score) in order:
        page = pages.get(page_id)
        if page is None:
            continue
        title, declared, stored = info.get(page.document_id, ("", None, 0))
        hits.append(
            SemanticHit(
                page.document_id,
                title,
                page.page_number,
                " ".join(page.text[s:e].split()),
                round(score, 4),
                page_count=declared,
                stored_pages=stored,
                pages_are_real=not (declared and declared > stored),
            )
        )
    return hits, used, reason
