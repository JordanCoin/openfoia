"""Versioned entity-extraction runs.

Every extraction writes one ``ExtractionRun`` row and its entities point at
it. Re-extracting adds a run; earlier runs and their entities stay in the
database until ``prune_runs`` deletes them. Each document has at most one
current run: the newest, unless the user picked another with
``set_current_run``. Everything that reads entities filters with
``current_entities`` so a document is counted once, not once per run.

Entities stored before runs existed (and FtM imports) have no run. They are
used while their document has no run; the first new extraction adopts them
into a run with backend "unknown", so they can still be listed, picked or
pruned.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, exists, or_, select

from .models import Entity, ExtractionRun, entity_links, utcnow

BACKENDS = ("llm", "gliner", "spacy", "regex", "ensemble")


def current_entity_clause() -> Any:
    """SQL condition true for entities in their document's current run.

    A legacy entity (no run) counts only while its document has no run at all.
    """
    current_runs = select(ExtractionRun.id).where(ExtractionRun.is_current.is_(True))
    document_has_runs = exists().where(ExtractionRun.document_id == Entity.document_id)
    return or_(
        Entity.extraction_run_id.in_(current_runs),
        and_(Entity.extraction_run_id.is_(None), ~document_has_runs),
    )


def current_entities(query: Any) -> Any:
    """Restrict an ``Entity`` query to current runs."""
    return query.filter(current_entity_clause())


def describe_extraction(extractor: Any, metadata: dict[str, Any], ensemble: bool) -> dict:
    """Backend, model, prompt hash and notes for a finished extraction.

    *metadata* is ``ExtractionResult.metadata``: it lists the backends that
    actually ran, which can differ from the one requested.
    """
    from .pipeline import extract as ex

    backends = [b for b in metadata.get("backends", []) if b != "llm-validator"]
    llm = bool(metadata.get("llm_validated"))
    ner_models = {"gliner": ex.GLINER_MODEL_ID, "spacy": ex.spacy_model_name()}

    if ensemble:
        backend = "ensemble"
    elif llm:
        backend = "llm"
    elif "gliner" in backends:
        backend = "gliner"
    elif "spacy" in backends:
        backend = "spacy"
    else:
        backend = "regex"

    if backend == "ensemble":
        used = [f"{b}:{ner_models[b]}" for b in backends if ner_models.get(b)]
        model = "; ".join(used) or None
    elif backend == "llm":
        model = extractor.model
    else:
        model = ner_models.get(backend)

    notes = [f"backends: {metadata.get('backend') or '+'.join(backends) or backend}"]
    if llm:
        notes.append(f"LLM provider: {extractor.provider}")
        ner = [f"{b}:{ner_models[b]}" for b in backends if ner_models.get(b)]
        if ner:
            notes.append(f"NER: {', '.join(ner)}")
    prompt_hash = (
        hashlib.sha256(ex.LLM_VALIDATION_PROMPT.encode()).hexdigest() if backend == "llm" else None
    )
    return {
        "backend": backend,
        "model": model,
        "prompt_hash": prompt_hash,
        "notes": "; ".join(notes),
    }


def adopt_legacy_entities(session: Any, document_id: str) -> ExtractionRun | None:
    """Put a document's run-less entities into a run of their own.

    Returns the new run, or None when the document has no such entities.
    """
    legacy = session.query(Entity).filter(
        Entity.document_id == document_id, Entity.extraction_run_id.is_(None)
    )
    count = legacy.count()
    if not count:
        return None
    has_current = (
        session.query(ExtractionRun)
        .filter(ExtractionRun.document_id == document_id, ExtractionRun.is_current.is_(True))
        .count()
    )
    run = ExtractionRun(
        id=str(uuid4()),
        document_id=document_id,
        backend="unknown",
        entity_count=count,
        is_current=not has_current,
        notes="Entities stored before extraction runs were recorded (an earlier "
        "extraction or an FtM import); backend, model and time were not recorded.",
    )
    session.add(run)
    session.flush()
    legacy.update({Entity.extraction_run_id: run.id}, synchronize_session=False)
    return run


def set_current_run(session: Any, run: ExtractionRun) -> None:
    """Make *run* the one its document's readers use."""
    session.query(ExtractionRun).filter(
        ExtractionRun.document_id == run.document_id, ExtractionRun.id != run.id
    ).update({ExtractionRun.is_current: False}, synchronize_session=False)
    session.flush()
    run.is_current = True
    session.flush()


def record_run(
    session: Any,
    document: Any,
    result: Any,
    *,
    backend: str,
    model: str | None,
    prompt_hash: str | None = None,
    notes: str | None = None,
    started_at: datetime | None = None,
) -> tuple[ExtractionRun, int]:
    """Store *result* as a new current run of *document*; earlier runs are kept.

    Returns the run and the number of relationships saved.
    """
    adopt_legacy_entities(session, document.id)
    run = ExtractionRun(
        id=str(uuid4()),
        document_id=document.id,
        backend=backend,
        model=model,
        prompt_hash=prompt_hash,
        started_at=started_at or utcnow(),
        completed_at=utcnow(),
        entity_count=len(result.entities),
        is_current=False,
        notes=notes,
    )
    session.add(run)
    session.flush()

    entity_id_map: dict[str, str] = {}  # normalized_text.lower() -> entity.id
    for ent in result.entities:
        eid = str(uuid4())
        entity_id_map[ent.normalized_text.lower()] = eid
        session.add(
            Entity(
                id=eid,
                document_id=document.id,
                extraction_run_id=run.id,
                entity_type=ent.entity_type,
                raw_text=ent.raw_text,
                normalized_text=ent.normalized_text,
                confidence=ent.confidence,
                context=ent.context,
                page_number=ent.page_number,
            )
        )
    # Flush entities so FKs exist before inserting links
    session.flush()

    rels_saved = 0
    seen: set[tuple[str, str]] = set()
    for rel in result.relationships:
        src = (rel.get("source") or "").lower()
        tgt = (rel.get("target") or "").lower()
        src_id = entity_id_map.get(src)
        tgt_id = entity_id_map.get(tgt)
        if not src_id or not tgt_id or src_id == tgt_id:
            # Try substring match
            for key, eid in entity_id_map.items():
                if not src_id and src in key:
                    src_id = eid
                if not tgt_id and tgt in key:
                    tgt_id = eid
        if src_id and tgt_id and src_id != tgt_id and (src_id, tgt_id) not in seen:
            seen.add((src_id, tgt_id))
            session.execute(
                entity_links.insert().values(
                    source_id=src_id,
                    target_id=tgt_id,
                    link_type=rel.get("relation", "related_to"),
                )
            )
            rels_saved += 1

    set_current_run(session, run)
    return run, rels_saved


def runs_for_document(session: Any, document_id: str) -> list[ExtractionRun]:
    """A document's runs, oldest first (SQLite sorts runs with no recorded time first)."""
    return (
        session.query(ExtractionRun)
        .filter(ExtractionRun.document_id == document_id)
        .order_by(ExtractionRun.started_at, ExtractionRun.completed_at)
        .all()
    )


def resolve_run(session: Any, document_id: str, ref: str) -> ExtractionRun | None:
    """The document's run whose id is *ref* or starts with it (unambiguously)."""
    matches = (
        session.query(ExtractionRun)
        .filter(ExtractionRun.document_id == document_id, ExtractionRun.id.like(f"{ref}%"))
        .all()
    )
    exact = [r for r in matches if r.id == ref]
    if exact:
        return exact[0]
    return matches[0] if len(matches) == 1 else None


def _delete_entities(session: Any, entity_filter: Any) -> int:
    ids = select(Entity.id).where(entity_filter)
    session.execute(
        entity_links.delete().where(
            entity_links.c.source_id.in_(ids) | entity_links.c.target_id.in_(ids)
        )
    )
    return session.query(Entity).filter(entity_filter).delete(synchronize_session=False)


def prune_runs(session: Any, document_id: str) -> tuple[int, int]:
    """Delete every run of a document except the current one, with their entities.

    Run-less entities the current run superseded go too. Freed database pages
    are zeroed (SQLite ``secure_delete``). Returns (runs, entities) deleted.
    """
    session.connection().exec_driver_sql("PRAGMA secure_delete = ON")
    current = (
        session.query(ExtractionRun.id)
        .filter(ExtractionRun.document_id == document_id, ExtractionRun.is_current.is_(True))
        .scalar()
    )
    if current is None:
        return 0, 0
    stale = select(ExtractionRun.id).where(
        ExtractionRun.document_id == document_id, ExtractionRun.id != current
    )
    entities = _delete_entities(
        session,
        and_(
            Entity.document_id == document_id,
            or_(Entity.extraction_run_id.in_(stale), Entity.extraction_run_id.is_(None)),
        ),
    )
    runs = (
        session.query(ExtractionRun)
        .filter(ExtractionRun.document_id == document_id, ExtractionRun.id != current)
        .delete(synchronize_session=False)
    )
    return runs, entities
