"""Per-page document text: storage, local full-text index, concordance search.

Everything here runs against the local database. Nothing touches the network.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from sqlalchemy import text as sql
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

FTS_TABLE = "document_pages_fts"

_FTS_DDL = [
    f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
    "text, content='document_pages', content_rowid='id', tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER document_pages_ai AFTER INSERT ON document_pages BEGIN "
    f"INSERT INTO {FTS_TABLE}(rowid, text) VALUES (new.id, new.text); END",
    "CREATE TRIGGER document_pages_ad AFTER DELETE ON document_pages BEGIN "
    f"INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, text) VALUES ('delete', old.id, old.text); END",
    "CREATE TRIGGER document_pages_au AFTER UPDATE ON document_pages BEGIN "
    f"INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, text) VALUES ('delete', old.id, old.text); "
    f"INSERT INTO {FTS_TABLE}(rowid, text) VALUES (new.id, new.text); END",
    f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild')",
]

_OPEN, _CLOSE = "\x02", "\x03"
_FTS_QUERY_ERRORS = ("fts5: syntax error", "unterminated string", "no such column")


def split_pages(text: str) -> list[str]:
    """Split extractor output on form feeds; a trailing \\f does not start a page."""
    pages = text.split("\f")
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    return pages


def join_pages(pages: list[str]) -> str:
    """The joined form stored in Document.extracted_text."""
    return "\n\n".join(p.strip() for p in pages if p.strip())


def replace_pages(
    session: Session,
    document_id: str,
    pages: list[str],
    verdicts: list[str | None] | None = None,
) -> None:
    """Store *pages* (page 1 first) for a document, replacing any earlier set.

    *verdicts* (same length as pages) is the per-page extraction verdict:
    text_layer, garbled, image_only, or ocr:<original> once a page was OCR'd.
    """
    from .models import DocumentPage

    verdicts = verdicts or [None] * len(pages)
    session.query(DocumentPage).filter(DocumentPage.document_id == document_id).delete()
    session.add_all(
        DocumentPage(document_id=document_id, page_number=i, text=t, char_count=len(t), verdict=v)
        for i, (t, v) in enumerate(zip((p.strip() for p in pages), verdicts, strict=False), start=1)
    )


def backfill_single_pages(conn: Connection) -> int:
    """Give every document that has text but no stored pages a single page 1.

    Covers rows written before pages existed and writers that only set
    extracted_text. Page numbers for such documents are not real page numbers.
    """
    result = conn.exec_driver_sql(
        "INSERT INTO document_pages (document_id, page_number, text, char_count) "
        "SELECT d.id, 1, d.extracted_text, length(d.extracted_text) FROM documents d "
        "WHERE d.extracted_text IS NOT NULL AND d.extracted_text != '' "
        "AND NOT EXISTS (SELECT 1 FROM document_pages p WHERE p.document_id = d.id)"
    )
    return result.rowcount or 0


def ensure_fts(conn: Connection) -> bool:
    """Create the FTS5 index (and its sync triggers) if the SQLite build supports it.

    Returns False when FTS5 is unavailable, e.g. a SQLCipher build compiled
    without it; callers then fall back to a LIKE scan.
    """
    exists = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (FTS_TABLE,)
    ).first()
    if exists:
        return True
    try:
        conn.exec_driver_sql("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        conn.exec_driver_sql("DROP TABLE temp._fts5_probe")
    except OperationalError:
        return False
    for stmt in _FTS_DDL:
        conn.exec_driver_sql(stmt)
    return True


@dataclass
class GrepHit:
    document_id: str
    title: str
    page: int
    passage: str
    match_start: int  # offsets of the first match within passage
    match_end: int
    page_count: int | None  # pages in the source, when known
    stored_pages: int
    pages_are_real: bool  # False when fewer pages are stored than the source has

    def to_dict(self) -> dict:
        return asdict(self)


def _spans_from_highlight(marked: str) -> tuple[str, list[tuple[int, int]]]:
    plain: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    start = 0
    for ch in marked:
        if ch == _OPEN:
            start = pos
        elif ch == _CLOSE:
            spans.append((start, pos))
        else:
            plain.append(ch)
            pos += 1
    return "".join(plain), spans


def _spans_from_substring(text: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    return [m.span() for m in re.finditer(re.escape(needle), text, re.IGNORECASE)]


def _passages(text: str, spans: list[tuple[int, int]], context: int):
    """One passage per cluster of matches; matches inside a shown passage merge."""
    last_end = -1
    for s, e in spans:
        if s < last_end:
            continue
        lo, hi = max(0, s - context), min(len(text), e + context)
        last_end = hi
        yield " ".join(text[lo:s].split()), text[s:e], " ".join(text[e:hi].split())


def _doc_info(session: Session, ids: set[str]) -> dict[str, tuple[str, int | None, int]]:
    """id -> (title, source page_count, stored page count)."""
    from sqlalchemy import func

    from .models import Document, DocumentPage, Request

    stored = (
        session.query(func.count(DocumentPage.id))
        .filter(DocumentPage.document_id == Document.id)
        .scalar_subquery()
    )
    rows = (
        session.query(
            Document.id,
            Document.filename,
            Request.request_number,
            Request.subject,
            Document.page_count,
            stored,
        )
        .outerjoin(Request, Request.id == Document.request_id)
        .filter(Document.id.in_(ids))
    )
    return {
        i: (subj if (num or "").startswith("DC-") and subj else fname, declared, n)
        for i, fname, num, subj, declared, n in rows
    }


def search_pages(
    session: Session,
    query: str,
    *,
    context: int = 200,
    limit: int = 50,
    document_ids: list[str] | None = None,
) -> tuple[list[GrepHit], str]:
    """Keyword-in-context search over stored pages.

    Returns (hits, engine) where engine is "fts5", "fts5-literal" (the query was
    not valid FTS5 syntax and was searched as a phrase) or "like" (no FTS5).
    """
    conn = session.connection()
    backfill_single_pages(conn)
    doc_filter, params = "", {}
    if document_ids:
        keys = [f"d{i}" for i in range(len(document_ids))]
        doc_filter = f" AND p.document_id IN ({', '.join(':' + k for k in keys)})"
        params = dict(zip(keys, document_ids, strict=True))

    engine = "fts5" if ensure_fts(conn) else "like"
    if engine == "like":
        literal = query.strip().strip('"')
        escaped = literal.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            sql(
                "SELECT p.document_id, p.page_number, p.text FROM document_pages p "
                f"WHERE p.text LIKE :pat ESCAPE '\\'{doc_filter} "
                "ORDER BY p.document_id, p.page_number"
            ),
            {"pat": f"%{escaped}%", **params},
        )
        pages = ((d, n, t, _spans_from_substring(t, literal)) for d, n, t in rows)
    else:
        stmt = sql(
            f"SELECT p.document_id, p.page_number, highlight({FTS_TABLE}, 0, char(2), char(3)) "
            f"FROM {FTS_TABLE} JOIN document_pages p ON p.id = {FTS_TABLE}.rowid "
            f"WHERE {FTS_TABLE} MATCH :q{doc_filter} ORDER BY rank"
        )
        try:
            rows = conn.execute(stmt, {"q": query, **params}).fetchall()
        except OperationalError as e:
            # Query-syntax errors only. "no such column" is FTS5 reading a
            # hyphenated term (FOIA-2024) as a column filter.
            msg = str(e.orig)
            if not any(m in msg for m in _FTS_QUERY_ERRORS):
                raise
            engine = "fts5-literal"
            phrase = '"' + query.replace('"', '""') + '"'
            rows = conn.execute(stmt, {"q": phrase, **params}).fetchall()
        pages = ((d, n, *_spans_from_highlight(m)) for d, n, m in rows)

    found: list[tuple[str, int, str, str, str]] = []
    for doc_id, page_no, text, spans in pages:
        for before, match, after in _passages(text, spans, context):
            found.append((doc_id, page_no, before, match, after))
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break

    info = _doc_info(session, {f[0] for f in found}) if found else {}
    hits = []
    for doc_id, page_no, before, match, after in found:
        title, declared, stored = info.get(doc_id, ("", None, 0))
        start = len(before) + (1 if before else 0)
        passage = " ".join(x for x in (before, match, after) if x)
        hits.append(
            GrepHit(
                doc_id,
                title,
                page_no,
                passage,
                start,
                start + len(match),
                page_count=declared,
                stored_pages=stored,
                pages_are_real=not (declared and declared > stored),
            )
        )
    return hits, engine
