"""Store a fetched record's text as a local Document (shared by text-pulling adapters)."""

from __future__ import annotations

import logging

from .base import RecordEntity

logger = logging.getLogger(__name__)


def store_text_document(
    entity: RecordEntity,
    *,
    request_number: str,
    filename: str,
    body: str,
    case_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Save *entity*'s ``full_text`` (and ``page_texts``) under a placeholder Request.

    Re-fetching an existing *request_number* updates the stored text in place
    and attaches the request to *case_id* if it had no case. Returns
    ``(document_id, full_text)`` or ``(None, None)`` when there is no text.
    """
    full_text = entity.extra_data.get("full_text")
    if not full_text:
        return None, None

    from uuid import uuid4

    from ..db import ensure_db, get_session
    from ..doctext import replace_pages
    from ..models import (
        Agency,
        DeliveryMethod,
        Document,
        DocumentPage,
        DocumentType,
        Request,
        RequestStatus,
        User,
    )

    ensure_db()
    page_texts = entity.extra_data.get("page_texts")
    pages = page_texts or [full_text]

    with get_session() as session:
        existing_req = (
            session.query(Request).filter(Request.request_number == request_number).first()
        )
        if existing_req:
            if case_id and existing_req.case_id is None:
                existing_req.case_id = case_id
            existing_doc = (
                session.query(Document)
                # Ingested files (NARA --objects) share the request; the text doc has no file.
                .filter(Document.request_id == existing_req.id, Document.file_path == "")
                .first()
            )
            if existing_doc:
                existing_doc.extracted_text = full_text
                existing_doc.file_size = len(full_text.encode())
                stored = (
                    session.query(DocumentPage)
                    .filter(DocumentPage.document_id == existing_doc.id)
                    .count()
                )
                if page_texts is None and stored > 1:
                    logger.warning(
                        "Per-page text unavailable on re-fetch of %s; kept the %d stored pages",
                        request_number,
                        stored,
                    )
                else:
                    replace_pages(session, existing_doc.id, pages)
                return existing_doc.id, full_text

        user = session.query(User).first()
        if not user:
            user = User(id=str(uuid4()), email="local@openfoia.local", name="Local User")
            session.add(user)
            session.flush()

        agency = session.query(Agency).first()
        req = Request(
            id=str(uuid4()),
            request_number=request_number,
            requester_id=user.id,
            agency_id=agency.id if agency else user.id,
            subject=entity.name,
            body=body,
            delivery_method=DeliveryMethod.EMAIL,
            status=RequestStatus.COMPLETE,
            case_id=case_id,
        )
        session.add(req)
        session.flush()

        doc_id = str(uuid4())
        session.add(
            Document(
                id=doc_id,
                request_id=req.id,
                doc_type=DocumentType.FULL_RESPONSE,
                filename=filename,
                file_path="",
                file_size=len(full_text.encode()),
                mime_type="text/plain",
                page_count=entity.extra_data.get("pages"),
                extracted_text=full_text,
                ocr_completed=True,
            )
        )
        replace_pages(session, doc_id, pages)

    return doc_id, full_text
