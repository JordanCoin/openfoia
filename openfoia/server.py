"""FastAPI server for OpenFOIA web interface.

Binds only to localhost. Requires token authentication.
Your data never leaves your machine.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, HTTPException, Depends, Query, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel
from sqlalchemy import func


#: Hard cap on a single uploaded document (100 MiB), matching CLI ingest.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class _UploadTooLarge(Exception):
    """Internal signal that an upload exceeded MAX_UPLOAD_BYTES."""


class CreateRequestBody(BaseModel):
    agency_id: str
    subject: str
    body: str
    method: str = "email"
    fee_waiver: bool = True
    expedited: bool = False


def _default_data_dir() -> Path:
    """Resolve the data dir the same way the rest of the toolkit does."""
    from .db import get_data_dir

    return get_data_dir()


def create_app(token: str, data_dir: Path | None = None) -> FastAPI:
    """Create the FastAPI application with token authentication."""

    app = FastAPI(
        title="OpenFOIA",
        description="Crowdsourced FOIA automation with AI-powered document analysis",
        version="0.1.0",
        docs_url=None,  # Disable public docs
        redoc_url=None,
    )

    # Store token and data directory in app state
    app.state.auth_token = token
    from .db import _ensure_private_dir

    app.state.data_dir = _ensure_private_dir(Path(data_dir) if data_dir else _default_data_dir())

    # Serve the vendored stylesheet from disk — no CDN, works air-gapped.
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Reject requests that did not arrive addressed to loopback (DNS rebinding).
    # Starlette compares the Host header with the port stripped.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "::1", "[::1]"],
    )

    # CORS - only allow localhost
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1", "http://localhost"],
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Structurally forbid the page from talking to anything off-machine.

        Defence in depth behind the escaping fixes: even if untrusted document
        text did reach the DOM as markup, `connect-src 'self'` blocks the
        exfiltration step, and `default-src 'self'` blocks remote subresources.
        """
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'"
        )
        # Keep the ?token= out of outbound Referer headers and shared caches.
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    # Token verification dependency
    async def verify_token(
        request: Request,
        token: str | None = Query(None, alias="token"),
    ):
        # Check query param first, then cookie
        auth_token = token or request.cookies.get("openfoia_token")
        # Constant-time: a plain != leaks the token prefix through timing.
        if not secrets.compare_digest(auth_token or "", app.state.auth_token):
            raise HTTPException(status_code=401, detail="Invalid or missing token")
        return auth_token

    # === Routes ===

    @app.get("/", response_class=HTMLResponse)
    async def index(token: str = Depends(verify_token)):
        """Serve the main web interface."""
        return get_index_html()

    @app.get("/api/health")
    async def health():
        """Health check (no auth required)."""
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/stats")
    async def stats(token: str = Depends(verify_token)):
        """Get overview statistics."""
        from .db import get_session
        from .models import (
            Request as FOIARequest,
            Document,
            Entity,
            RequestStatus,
            EntityType,
        )

        with get_session() as session:
            total_requests = session.query(func.count(FOIARequest.id)).scalar() or 0
            pending = (
                session.query(func.count(FOIARequest.id))
                .filter(
                    FOIARequest.status.in_(
                        [
                            RequestStatus.SENT,
                            RequestStatus.ACKNOWLEDGED,
                            RequestStatus.PROCESSING,
                        ]
                    )
                )
                .scalar()
                or 0
            )
            complete = (
                session.query(func.count(FOIARequest.id))
                .filter(FOIARequest.status == RequestStatus.COMPLETE)
                .scalar()
                or 0
            )
            denied = (
                session.query(func.count(FOIARequest.id))
                .filter(FOIARequest.status == RequestStatus.DENIED)
                .scalar()
                or 0
            )

            total_docs = session.query(func.count(Document.id)).scalar() or 0
            processed_docs = (
                session.query(func.count(Document.id))
                .filter(Document.ocr_completed.is_(True))
                .scalar()
                or 0
            )
            total_pages = session.query(func.coalesce(func.sum(Document.page_count), 0)).scalar()

            total_entities = session.query(func.count(Entity.id)).scalar() or 0
            people = (
                session.query(func.count(Entity.id))
                .filter(Entity.entity_type == EntityType.PERSON)
                .scalar()
                or 0
            )
            orgs = (
                session.query(func.count(Entity.id))
                .filter(Entity.entity_type == EntityType.ORGANIZATION)
                .scalar()
                or 0
            )

        return {
            "requests": {
                "total": total_requests,
                "pending": pending,
                "complete": complete,
                "denied": denied,
            },
            "documents": {
                "total": total_docs,
                "processed": processed_docs,
                "pages": total_pages,
            },
            "entities": {
                "total": total_entities,
                "people": people,
                "organizations": orgs,
            },
            "data_dir": str(app.state.data_dir),
        }

    @app.get("/api/requests")
    async def list_requests(
        token: str = Depends(verify_token),
        status: str | None = None,
        limit: int = 50,
    ):
        """List FOIA requests."""
        from .db import get_session
        from .models import Request as FOIARequest, RequestStatus, Agency

        with get_session() as session:
            query = session.query(FOIARequest).join(Agency)

            if status:
                try:
                    status_enum = RequestStatus(status.lower())
                    query = query.filter(FOIARequest.status == status_enum)
                except ValueError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid status '{status}'. Valid: {', '.join(s.value for s in RequestStatus)}",
                    )

            requests = query.order_by(FOIARequest.created_at.desc()).limit(limit).all()

            return {
                "requests": [
                    {
                        "id": r.id,
                        "request_number": r.request_number,
                        "agency": r.agency.abbreviation or r.agency.name,
                        "subject": r.subject,
                        "status": r.status.value,
                        "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                        "days_pending": r.days_pending(),
                        "is_overdue": r.is_overdue(),
                    }
                    for r in requests
                ],
                "total": len(requests),
            }

    @app.post("/api/requests")
    async def create_request(
        request_data: CreateRequestBody,
        token: str = Depends(verify_token),
    ):
        """Create a new FOIA request."""
        from .db import get_session
        from .models import (
            Request as FOIARequest,
            Agency,
            User,
            RequestStatus,
            DeliveryMethod,
        )

        with get_session() as session:
            agency = session.query(Agency).filter(Agency.id == request_data.agency_id).first()
            if not agency:
                raise HTTPException(status_code=404, detail="Agency not found")

            # Get or create a default local user
            user = session.query(User).first()
            if not user:
                user = User(
                    id=str(uuid4()),
                    email="local@openfoia.local",
                    name="Local User",
                )
                session.add(user)
                session.flush()

            req_num = f"REQ-{datetime.utcnow().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"

            try:
                method = DeliveryMethod(request_data.method)
            except ValueError:
                method = DeliveryMethod.EMAIL

            new_request = FOIARequest(
                id=str(uuid4()),
                request_number=req_num,
                requester_id=user.id,
                agency_id=agency.id,
                subject=request_data.subject,
                body=request_data.body,
                delivery_method=method,
                status=RequestStatus.DRAFT,
                fee_waiver_requested=request_data.fee_waiver,
                expedited_requested=request_data.expedited,
            )
            session.add(new_request)

        return {"id": new_request.id, "request_number": req_num, "status": "draft"}

    @app.get("/api/agencies")
    async def list_agencies(
        token: str = Depends(verify_token),
        query: str | None = None,
        level: str | None = None,
    ):
        """Search agencies."""
        from .db import get_session
        from .models import Agency, AgencyLevel

        with get_session() as session:
            q = session.query(Agency)

            if query:
                search = f"%{query}%"
                q = q.filter((Agency.name.ilike(search)) | (Agency.abbreviation.ilike(search)))

            if level:
                try:
                    level_enum = AgencyLevel(level.lower())
                    q = q.filter(Agency.level == level_enum)
                except ValueError:
                    pass

            agencies = q.order_by(Agency.name).limit(100).all()

            return {
                "agencies": [
                    {
                        "id": a.id,
                        "name": a.name,
                        "abbreviation": a.abbreviation,
                        "level": a.level.value,
                        "foia_email": a.foia_email,
                        "foia_portal_url": a.foia_portal_url,
                        "preferred_method": a.preferred_method.value,
                        "typical_response_days": a.typical_response_days,
                    }
                    for a in agencies
                ],
                "total": len(agencies),
            }

    @app.get("/api/documents")
    async def list_documents(
        token: str = Depends(verify_token),
        request_id: str | None = None,
    ):
        """List documents."""
        from .db import get_session
        from .models import Document

        with get_session() as session:
            q = session.query(Document)

            if request_id:
                q = q.filter(Document.request_id == request_id)

            docs = q.order_by(Document.received_at.desc()).limit(100).all()

            return {
                "documents": [
                    {
                        "id": d.id,
                        "request_id": d.request_id,
                        "filename": d.filename,
                        "doc_type": d.doc_type.value,
                        "file_size": d.file_size,
                        "page_count": d.page_count,
                        "ocr_completed": d.ocr_completed,
                        "entities_extracted": d.entities_extracted,
                        "received_at": d.received_at.isoformat(),
                    }
                    for d in docs
                ],
                "total": len(docs),
            }

    @app.post("/api/documents/upload")
    async def upload_document(
        file: UploadFile = File(...),
        request_id: str | None = Query(None),
        token: str = Depends(verify_token),
    ):
        """Upload a document for processing."""
        if not request_id:
            raise HTTPException(
                status_code=400, detail="request_id is required for document uploads"
            )

        docs_dir = app.state.data_dir / "docs"
        docs_dir.mkdir(exist_ok=True)

        # Sanitize filename to prevent path traversal
        safe_name = Path(file.filename).name if file.filename else "upload"
        stored_name = f"{uuid4().hex[:12]}_{safe_name}"
        dest = docs_dir / stored_name

        # Stream with a hard cap. Without one, a single upload (or a crafted
        # "response" the user was lured into importing) fills the disk.
        written = 0
        try:
            with open(dest, "wb") as f:
                while chunk := await file.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        raise _UploadTooLarge()
                    f.write(chunk)
        except _UploadTooLarge:
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB upload limit.",
            )
        except Exception:
            dest.unlink(missing_ok=True)
            raise

        file_size = written
        mime = file.content_type or "application/octet-stream"

        # Extract text from the uploaded file
        from .pipeline.ingest import DocumentIngester

        ingester = DocumentIngester(storage_path=docs_dir)
        extracted_text = None
        try:
            extracted_text = await ingester._extract_text(dest, mime)
        except Exception:
            pass

        from .db import get_session
        from .models import Document, DocumentType

        doc_id = str(uuid4())
        with get_session() as session:
            doc = Document(
                id=doc_id,
                request_id=request_id,
                doc_type=DocumentType.CORRESPONDENCE,
                filename=safe_name,
                file_path=str(dest),
                file_size=file_size,
                mime_type=mime,
                extracted_text=extracted_text,
                ocr_completed=bool(extracted_text),
            )
            session.add(doc)

        return {
            "id": doc_id,
            "filename": safe_name,
            "status": "uploaded",
            "text_extracted": bool(extracted_text),
        }

    @app.get("/api/entities")
    async def list_entities(
        token: str = Depends(verify_token),
        query: str | None = None,
        entity_type: str | None = None,
    ):
        """Search entities."""
        from .db import get_session
        from .models import Entity, EntityType

        with get_session() as session:
            q = session.query(Entity)

            if query:
                search = f"%{query}%"
                q = q.filter(Entity.normalized_text.ilike(search))

            if entity_type:
                try:
                    type_enum = EntityType(entity_type.lower())
                    q = q.filter(Entity.entity_type == type_enum)
                except ValueError:
                    pass

            entities = q.limit(100).all()

            return {
                "entities": [
                    {
                        "id": e.id,
                        "entity_type": e.entity_type.value,
                        "raw_text": e.raw_text,
                        "normalized_text": e.normalized_text,
                        "confidence": e.confidence,
                        "document_id": e.document_id,
                    }
                    for e in entities
                ],
                "total": len(entities),
            }

    @app.get("/api/deadlines")
    async def deadlines(token: str = Depends(verify_token)):
        """Get upcoming deadlines for pending requests."""
        from .db import get_session
        from .models import Request as FOIARequest, RequestStatus, Agency

        with get_session() as session:
            active_statuses = [
                RequestStatus.SENT,
                RequestStatus.ACKNOWLEDGED,
                RequestStatus.PROCESSING,
                RequestStatus.FEE_ESTIMATE,
                RequestStatus.FEE_PAID,
                RequestStatus.PARTIAL_RESPONSE,
                RequestStatus.APPEALED,
            ]
            requests = (
                session.query(FOIARequest)
                .join(Agency)
                .filter(FOIARequest.status.in_(active_statuses))
                .order_by(FOIARequest.due_date.asc().nullslast(), FOIARequest.sent_at.asc())
                .limit(20)
                .all()
            )

            return {
                "deadlines": [
                    {
                        "id": r.id,
                        "request_number": r.request_number,
                        "agency": r.agency.abbreviation or r.agency.name,
                        "subject": r.subject,
                        "status": r.status.value,
                        "due_date": r.due_date.isoformat() if r.due_date else None,
                        "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                        "days_pending": r.days_pending(),
                        "is_overdue": r.is_overdue(),
                    }
                    for r in requests
                ],
                "total": len(requests),
            }

    @app.get("/api/graph")
    async def get_graph(
        token: str = Depends(verify_token),
        request_ids: str | None = None,
    ):
        """Get entity relationship graph."""
        from .db import get_session
        from .models import Entity, Document, entity_links

        with get_session() as session:
            q = session.query(Entity)

            if request_ids:
                ids = [rid.strip() for rid in request_ids.split(",")]
                q = q.join(Document).filter(Document.request_id.in_(ids))

            entities = q.all()
            entity_ids = {e.id for e in entities}

            nodes = [
                {
                    "id": e.id,
                    "label": e.normalized_text,
                    "type": e.entity_type.value,
                }
                for e in entities
            ]

            # Get links filtered to selected entities
            links_q = session.query(entity_links)
            if entity_ids:
                links_q = links_q.filter(
                    entity_links.c.source_id.in_(entity_ids),
                    entity_links.c.target_id.in_(entity_ids),
                )
            links = links_q.all()
            edges = [
                {
                    "source": link.source_id,
                    "target": link.target_id,
                    "type": link.link_type,
                }
                for link in links
            ]

        return {"nodes": nodes, "edges": edges}

    return app


def get_index_html() -> str:
    """Return the main HTML interface.

    All rendering uses safe DOM methods (textContent, createElement) for
    user-supplied data.  Only static structural markup is built as strings.
    This is a localhost-only tool with token auth; the data originates
    entirely from the local SQLite database.
    """
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenFOIA</title>
    <!-- Styles are served from disk (openfoia/static/app.css). Nothing is
         loaded from a CDN: an external subresource would tell the CDN and any
         on-path observer when this machine is running an investigation. -->
    <link rel="stylesheet" href="/static/app.css">
    <style>
        .gradient-bg {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
        }
        #agency-dropdown { max-height: 200px; overflow-y: auto; }
        .agency-option:hover { background: rgba(59, 130, 246, 0.3); }
        .status-filter.active { background: rgba(59, 130, 246, 0.5); }
        .toast {
            position: fixed; bottom: 2rem; right: 2rem; padding: 1rem 1.5rem;
            border-radius: 0.75rem; z-index: 50; transition: opacity 0.3s;
        }
    </style>
</head>
<body class="gradient-bg min-h-screen text-white">
    <div class="container mx-auto px-4 py-8">
        <!-- Header -->
        <header class="mb-12">
            <div class="flex items-center justify-between">
                <div>
                    <h1 class="text-4xl font-bold mb-2">OpenFOIA</h1>
                    <p class="text-gray-400">Crowdsourced FOIA automation &bull; Your data stays local</p>
                </div>
                <div class="text-right text-sm text-gray-500">
                    <div>v0.1.0</div>
                    <div id="status" class="text-green-400">Connected</div>
                </div>
            </div>
        </header>

        <!-- Stats Cards -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-12">
            <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                <div class="text-3xl font-bold" id="stat-requests">0</div>
                <div class="text-gray-400">Requests</div>
            </div>
            <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                <div class="text-3xl font-bold" id="stat-documents">0</div>
                <div class="text-gray-400">Documents</div>
            </div>
            <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                <div class="text-3xl font-bold" id="stat-pages">0</div>
                <div class="text-gray-400">Pages Processed</div>
            </div>
            <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                <div class="text-3xl font-bold" id="stat-entities">0</div>
                <div class="text-gray-400">Entities Found</div>
            </div>
        </div>

        <!-- Main Content -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
            <!-- New Request -->
            <div class="lg:col-span-2">
                <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                    <h2 class="text-xl font-semibold mb-4">New FOIA Request</h2>
                    <form id="new-request-form" class="space-y-4">
                        <div class="relative">
                            <label class="block text-sm text-gray-400 mb-1">Agency</label>
                            <input type="text" id="agency-search" placeholder="Search agencies..."
                                   autocomplete="off"
                                   class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
                            <input type="hidden" id="agency-id" value="">
                            <div id="agency-dropdown" class="absolute z-10 w-full mt-1 bg-gray-800 border border-white/20 rounded-lg hidden"></div>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Subject</label>
                            <input type="text" id="request-subject" placeholder="Brief description of records requested"
                                   class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Records Requested</label>
                            <textarea rows="4" id="request-body" placeholder="Describe the specific records you're requesting..."
                                      class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500"></textarea>
                        </div>
                        <div class="grid grid-cols-2 gap-4">
                            <div>
                                <label class="block text-sm text-gray-400 mb-1">Date Range Start</label>
                                <input type="date" class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
                            </div>
                            <div>
                                <label class="block text-sm text-gray-400 mb-1">Date Range End</label>
                                <input type="date" class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
                            </div>
                        </div>
                        <div class="flex items-center gap-4">
                            <label class="flex items-center gap-2">
                                <input type="checkbox" id="fee-waiver" checked class="rounded">
                                <span class="text-sm">Request fee waiver</span>
                            </label>
                            <label class="flex items-center gap-2">
                                <input type="checkbox" id="expedited" class="rounded">
                                <span class="text-sm">Request expedited processing</span>
                            </label>
                        </div>
                        <div id="form-error" class="hidden text-red-400 text-sm"></div>
                        <button type="submit" id="submit-btn" class="w-full bg-blue-600 hover:bg-blue-700 rounded-lg px-4 py-3 font-semibold transition">
                            Draft Request
                        </button>
                    </form>
                </div>
            </div>

            <!-- Quick Actions -->
            <div class="space-y-6">
                <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                    <h2 class="text-xl font-semibold mb-4">Quick Actions</h2>
                    <div class="space-y-3">
                        <button id="btn-import-docs" class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            Import Documents
                        </button>
                        <button id="btn-search-entities" class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            Search Entities
                        </button>
                        <button id="btn-view-graph" class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            View Entity Graph
                        </button>
                        <button class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition opacity-50 cursor-not-allowed" disabled>
                            Generate Report (coming soon)
                        </button>
                    </div>
                </div>

                <!-- Import Documents Panel (hidden by default) -->
                <div id="import-panel" class="bg-white/10 rounded-xl p-6 backdrop-blur hidden">
                    <div class="flex items-center justify-between mb-4">
                        <h2 class="text-xl font-semibold">Import Documents</h2>
                        <button onclick="document.getElementById('import-panel').classList.add('hidden')" class="text-gray-400 hover:text-white">&times;</button>
                    </div>
                    <div class="space-y-3">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Request</label>
                            <select id="upload-request-id" class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
                                <option value="">Select a request...</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">File</label>
                            <input type="file" id="upload-file" class="w-full text-sm text-gray-400 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:bg-blue-600 file:text-white file:cursor-pointer">
                        </div>
                        <button id="upload-submit" class="w-full bg-green-600 hover:bg-green-700 rounded-lg px-4 py-3 font-semibold transition">
                            Upload
                        </button>
                    </div>
                </div>

                <!-- Entity Search Panel (hidden by default) -->
                <div id="entity-panel" class="bg-white/10 rounded-xl p-6 backdrop-blur hidden">
                    <div class="flex items-center justify-between mb-4">
                        <h2 class="text-xl font-semibold">Search Entities</h2>
                        <button onclick="document.getElementById('entity-panel').classList.add('hidden')" class="text-gray-400 hover:text-white">&times;</button>
                    </div>
                    <div class="space-y-3">
                        <input type="text" id="entity-query" placeholder="Search people, orgs, locations..."
                               class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
                        <select id="entity-type-filter" class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
                            <option value="">All types</option>
                            <option value="person">People</option>
                            <option value="organization">Organizations</option>
                            <option value="location">Locations</option>
                            <option value="date">Dates</option>
                            <option value="money">Money</option>
                        </select>
                        <div id="entity-results" class="text-sm text-gray-400">Enter a search term above.</div>
                    </div>
                </div>

                <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                    <h2 class="text-xl font-semibold mb-4">Privacy</h2>
                    <div class="text-sm text-gray-400 space-y-2">
                        <p>All data stored locally</p>
                        <p>No cloud services required</p>
                        <p>Server only binds to localhost</p>
                        <p>Token-authenticated session</p>
                    </div>
                    <div class="mt-4 pt-4 border-t border-white/10 text-xs text-gray-500">
                        Data directory: <code>~/.openfoia/</code>
                    </div>
                </div>
            </div>
        </div>

        <!-- Deadlines -->
        <div class="mt-12">
            <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                <h2 class="text-xl font-semibold mb-4">Upcoming Deadlines</h2>
                <div id="deadlines-list" class="text-gray-400 text-center py-8">
                    Loading deadlines...
                </div>
            </div>
        </div>

        <!-- Recent Requests -->
        <div class="mt-8">
            <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                <div class="flex items-center justify-between mb-4">
                    <h2 class="text-xl font-semibold">Recent Requests</h2>
                    <div class="flex gap-2" id="status-filters">
                        <button onclick="filterRequests('')" class="status-filter active text-xs px-3 py-1 rounded-full bg-white/20">All</button>
                        <button onclick="filterRequests('draft')" class="status-filter text-xs px-3 py-1 rounded-full bg-white/5 hover:bg-white/10">Draft</button>
                        <button onclick="filterRequests('sent')" class="status-filter text-xs px-3 py-1 rounded-full bg-white/5 hover:bg-white/10">Sent</button>
                        <button onclick="filterRequests('processing')" class="status-filter text-xs px-3 py-1 rounded-full bg-white/5 hover:bg-white/10">Processing</button>
                        <button onclick="filterRequests('complete')" class="status-filter text-xs px-3 py-1 rounded-full bg-white/5 hover:bg-white/10">Complete</button>
                        <button onclick="filterRequests('denied')" class="status-filter text-xs px-3 py-1 rounded-full bg-white/5 hover:bg-white/10">Denied</button>
                    </div>
                </div>
                <div id="requests-list" class="text-gray-400 text-center py-8">
                    No requests yet. Create your first FOIA request above.
                </div>
            </div>
        </div>

        <!-- Footer -->
        <footer class="mt-12 text-center text-gray-500 text-sm">
            <p>OpenFOIA is open source software. <a href="https://github.com/JordanCoin/openfoia" class="text-blue-400 hover:underline">View on GitHub</a></p>
            <p class="mt-2">Transparency is patriotic.</p>
        </footer>
    </div>

    <!-- Toast notification -->
    <div id="toast" class="toast hidden text-white"></div>

    <script>
        // Get token from URL
        const urlParams = new URLSearchParams(window.location.search);
        const token = urlParams.get('token');

        // --- Helpers ---
        function showToast(msg, isError) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'toast ' + (isError ? 'bg-red-600' : 'bg-green-600');
            setTimeout(function() { t.classList.add('hidden'); }, 3000);
        }

        function apiUrl(path) {
            return path + (path.includes('?') ? '&' : '?') + 'token=' + token;
        }

        function esc(s) {
            const d = document.createElement('div');
            d.textContent = s;
            return d.textContent;
        }

        // --- Stats ---
        async function loadStats() {
            try {
                const resp = await fetch(apiUrl('/api/stats'));
                const data = await resp.json();
                document.getElementById('stat-requests').textContent = data.requests.total;
                document.getElementById('stat-documents').textContent = data.documents.total;
                document.getElementById('stat-pages').textContent = data.documents.pages;
                document.getElementById('stat-entities').textContent = data.entities.total;
            } catch (e) {
                console.error('Failed to load stats:', e);
            }
        }

        // --- Requests Table ---
        function renderRequestsTable(container, requests, emptyMsg) {
            container.replaceChildren();
            if (requests.length === 0) {
                var p = document.createElement('p');
                p.className = 'text-gray-400 text-center py-8';
                p.textContent = emptyMsg;
                container.appendChild(p);
                return;
            }
            var tbl = document.createElement('table');
            tbl.className = 'w-full text-left';
            var thead = document.createElement('thead');
            var hrow = document.createElement('tr');
            hrow.className = 'text-gray-400 text-sm';
            ['Request #', 'Agency', 'Subject', 'Status', 'Days'].forEach(function(h) {
                var th = document.createElement('th');
                th.className = 'pb-2';
                th.textContent = h;
                hrow.appendChild(th);
            });
            thead.appendChild(hrow);
            tbl.appendChild(thead);
            var tbody = document.createElement('tbody');
            requests.forEach(function(r) {
                var tr = document.createElement('tr');
                tr.className = 'border-t border-white/10';
                var td1 = document.createElement('td'); td1.className = 'py-2 text-cyan-400'; td1.textContent = r.request_number;
                var td2 = document.createElement('td'); td2.className = 'py-2'; td2.textContent = r.agency;
                var td3 = document.createElement('td'); td3.className = 'py-2'; td3.textContent = r.subject;
                var td4 = document.createElement('td');
                td4.className = 'py-2 ' + (r.status === 'complete' ? 'text-green-400' : r.status === 'denied' ? 'text-red-400' : 'text-yellow-400');
                td4.textContent = r.status;
                var td5 = document.createElement('td'); td5.className = 'py-2'; td5.textContent = r.days_pending;
                tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3); tr.appendChild(td4); tr.appendChild(td5);
                tbody.appendChild(tr);
            });
            tbl.appendChild(tbody);
            container.appendChild(tbl);
        }

        var currentStatusFilter = '';

        function filterRequests(status) {
            currentStatusFilter = status;
            // Update active button styling
            document.querySelectorAll('.status-filter').forEach(function(btn) {
                btn.classList.remove('active');
                btn.classList.remove('bg-white/20');
                btn.classList.add('bg-white/5');
            });
            event.target.classList.add('active');
            event.target.classList.remove('bg-white/5');
            event.target.classList.add('bg-white/20');
            loadRequests();
        }

        async function loadRequests() {
            try {
                var url = '/api/requests?limit=10';
                if (currentStatusFilter) url += '&status=' + currentStatusFilter;
                const resp = await fetch(apiUrl(url));
                const data = await resp.json();
                renderRequestsTable(
                    document.getElementById('requests-list'),
                    data.requests,
                    'No requests yet. Create your first FOIA request above.'
                );
            } catch (e) {
                console.error('Failed to load requests:', e);
            }
        }

        // --- Deadlines ---
        async function loadDeadlines() {
            try {
                const resp = await fetch(apiUrl('/api/deadlines'));
                const data = await resp.json();
                var el = document.getElementById('deadlines-list');
                el.replaceChildren();
                if (data.deadlines.length === 0) {
                    var p = document.createElement('p');
                    p.className = 'text-gray-400 text-center py-4';
                    p.textContent = 'No pending deadlines.';
                    el.appendChild(p);
                    return;
                }
                var tbl = document.createElement('table');
                tbl.className = 'w-full text-left';
                var thead = document.createElement('thead');
                var hrow = document.createElement('tr');
                hrow.className = 'text-gray-400 text-sm';
                ['Request #', 'Agency', 'Subject', 'Status', 'Due Date', 'Days Pending'].forEach(function(h) {
                    var th = document.createElement('th'); th.className = 'pb-2'; th.textContent = h; hrow.appendChild(th);
                });
                thead.appendChild(hrow);
                tbl.appendChild(thead);
                var tbody = document.createElement('tbody');
                data.deadlines.forEach(function(d) {
                    var tr = document.createElement('tr');
                    tr.className = 'border-t border-white/10';
                    var td1 = document.createElement('td'); td1.className = 'py-2 text-cyan-400'; td1.textContent = d.request_number;
                    var td2 = document.createElement('td'); td2.className = 'py-2'; td2.textContent = d.agency;
                    var td3 = document.createElement('td'); td3.className = 'py-2'; td3.textContent = d.subject;
                    var td4 = document.createElement('td'); td4.className = 'py-2 text-yellow-400'; td4.textContent = d.status;
                    var td5 = document.createElement('td');
                    td5.className = 'py-2' + (d.is_overdue ? ' text-red-400 font-bold' : '');
                    td5.textContent = (d.due_date ? new Date(d.due_date).toLocaleDateString() : 'Not set') + (d.is_overdue ? ' OVERDUE' : '');
                    var td6 = document.createElement('td'); td6.className = 'py-2'; td6.textContent = d.days_pending;
                    tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3); tr.appendChild(td4); tr.appendChild(td5); tr.appendChild(td6);
                    tbody.appendChild(tr);
                });
                tbl.appendChild(tbody);
                el.appendChild(tbl);
            } catch (e) {
                console.error('Failed to load deadlines:', e);
            }
        }

        // --- Agency Search / Autocomplete ---
        var agencySearchTimeout = null;
        var agencyInput = document.getElementById('agency-search');
        var agencyDropdown = document.getElementById('agency-dropdown');
        var agencyIdField = document.getElementById('agency-id');

        agencyInput.addEventListener('input', function() {
            clearTimeout(agencySearchTimeout);
            var q = this.value.trim();
            agencyIdField.value = '';
            if (q.length < 2) { agencyDropdown.classList.add('hidden'); return; }
            agencySearchTimeout = setTimeout(async function() {
                try {
                    var resp = await fetch(apiUrl('/api/agencies?query=' + encodeURIComponent(q)));
                    var data = await resp.json();
                    agencyDropdown.replaceChildren();
                    if (data.agencies.length === 0) {
                        var nf = document.createElement('div');
                        nf.className = 'px-4 py-2 text-gray-500';
                        nf.textContent = 'No agencies found';
                        agencyDropdown.appendChild(nf);
                    } else {
                        data.agencies.forEach(function(a) {
                            var opt = document.createElement('div');
                            opt.className = 'agency-option px-4 py-2 cursor-pointer text-sm';
                            opt.dataset.id = a.id;
                            opt.dataset.name = (a.abbreviation ? a.abbreviation + ' - ' : '') + a.name;
                            opt.textContent = opt.dataset.name + ' (' + a.level + ')';
                            agencyDropdown.appendChild(opt);
                        });
                    }
                    agencyDropdown.classList.remove('hidden');
                } catch (e) { console.error('Agency search failed:', e); }
            }, 250);
        });

        agencyDropdown.addEventListener('click', function(e) {
            var opt = e.target.closest('.agency-option');
            if (!opt) return;
            agencyIdField.value = opt.dataset.id;
            agencyInput.value = opt.dataset.name;
            agencyDropdown.classList.add('hidden');
        });

        document.addEventListener('click', function(e) {
            if (!agencyInput.contains(e.target) && !agencyDropdown.contains(e.target)) {
                agencyDropdown.classList.add('hidden');
            }
        });

        // --- New Request Form Submission ---
        document.getElementById('new-request-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            var errorEl = document.getElementById('form-error');
            errorEl.classList.add('hidden');

            var agencyId = agencyIdField.value;
            var subject = document.getElementById('request-subject').value.trim();
            var body = document.getElementById('request-body').value.trim();
            var feeWaiver = document.getElementById('fee-waiver').checked;
            var expedited = document.getElementById('expedited').checked;

            if (!agencyId) { errorEl.textContent = 'Please select an agency from the dropdown.'; errorEl.classList.remove('hidden'); return; }
            if (!subject) { errorEl.textContent = 'Subject is required.'; errorEl.classList.remove('hidden'); return; }
            if (!body) { errorEl.textContent = 'Request body is required.'; errorEl.classList.remove('hidden'); return; }

            var btn = document.getElementById('submit-btn');
            btn.disabled = true;
            btn.textContent = 'Submitting...';

            try {
                var resp = await fetch(apiUrl('/api/requests'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        agency_id: agencyId,
                        subject: subject,
                        body: body,
                        method: 'email',
                        fee_waiver: feeWaiver,
                        expedited: expedited,
                    }),
                });

                if (!resp.ok) {
                    var err = await resp.json();
                    throw new Error(err.detail || 'Request failed');
                }

                var result = await resp.json();
                showToast('Request ' + result.request_number + ' created!', false);

                // Reset form
                document.getElementById('new-request-form').reset();
                agencyIdField.value = '';
                document.getElementById('fee-waiver').checked = true;

                // Reload data
                loadRequests();
                loadStats();
                loadDeadlines();
            } catch (err) {
                errorEl.textContent = err.message;
                errorEl.classList.remove('hidden');
                showToast('Failed: ' + err.message, true);
            } finally {
                btn.disabled = false;
                btn.textContent = 'Draft Request';
            }
        });

        // --- Import Documents ---
        document.getElementById('btn-import-docs').addEventListener('click', async function() {
            var panel = document.getElementById('import-panel');
            panel.classList.toggle('hidden');
            if (!panel.classList.contains('hidden')) {
                try {
                    var resp = await fetch(apiUrl('/api/requests?limit=50'));
                    var data = await resp.json();
                    var sel = document.getElementById('upload-request-id');
                    sel.replaceChildren();
                    var defaultOpt = document.createElement('option');
                    defaultOpt.value = '';
                    defaultOpt.textContent = 'Select a request...';
                    sel.appendChild(defaultOpt);
                    data.requests.forEach(function(r) {
                        var opt = document.createElement('option');
                        opt.value = r.id;
                        opt.textContent = r.request_number + ' - ' + r.subject;
                        sel.appendChild(opt);
                    });
                } catch (e) { console.error('Failed to load requests for upload:', e); }
            }
        });

        document.getElementById('upload-submit').addEventListener('click', async function() {
            var requestId = document.getElementById('upload-request-id').value;
            var fileInput = document.getElementById('upload-file');
            if (!requestId) { showToast('Please select a request.', true); return; }
            if (!fileInput.files.length) { showToast('Please select a file.', true); return; }

            var formData = new FormData();
            formData.append('file', fileInput.files[0]);

            try {
                var resp = await fetch(apiUrl('/api/documents/upload?request_id=' + requestId), {
                    method: 'POST',
                    body: formData,
                });
                if (!resp.ok) {
                    var err = await resp.json();
                    throw new Error(err.detail || 'Upload failed');
                }
                var result = await resp.json();
                showToast('Uploaded: ' + result.filename, false);
                fileInput.value = '';
                loadStats();
            } catch (err) {
                showToast('Upload failed: ' + err.message, true);
            }
        });

        // --- Entity Search ---
        document.getElementById('btn-search-entities').addEventListener('click', function() {
            document.getElementById('entity-panel').classList.toggle('hidden');
        });

        var entitySearchTimeout = null;
        function doEntitySearch() {
            clearTimeout(entitySearchTimeout);
            entitySearchTimeout = setTimeout(async function() {
                var q = document.getElementById('entity-query').value.trim();
                var et = document.getElementById('entity-type-filter').value;
                var resultsEl = document.getElementById('entity-results');

                if (!q && !et) { resultsEl.textContent = 'Enter a search term above.'; return; }

                var url = '/api/entities?';
                if (q) url += 'query=' + encodeURIComponent(q) + '&';
                if (et) url += 'entity_type=' + et + '&';

                try {
                    var resp = await fetch(apiUrl(url));
                    var data = await resp.json();
                    resultsEl.replaceChildren();
                    if (data.entities.length === 0) {
                        resultsEl.textContent = 'No entities found.';
                        return;
                    }
                    var wrapper = document.createElement('div');
                    wrapper.className = 'space-y-2 max-h-60 overflow-y-auto';
                    data.entities.forEach(function(ent) {
                        var row = document.createElement('div');
                        row.className = 'flex items-center gap-2 py-1';
                        var badge = document.createElement('span');
                        var typeColor = ent.entity_type === 'person' ? 'bg-blue-600' :
                            ent.entity_type === 'organization' ? 'bg-purple-600' :
                            ent.entity_type === 'location' ? 'bg-green-600' : 'bg-gray-600';
                        badge.className = typeColor + ' text-xs px-2 py-0.5 rounded';
                        badge.textContent = ent.entity_type;
                        var nameSpan = document.createElement('span');
                        nameSpan.textContent = ent.normalized_text;
                        var confSpan = document.createElement('span');
                        confSpan.className = 'text-gray-500 text-xs ml-auto';
                        confSpan.textContent = Math.round(ent.confidence * 100) + '%';
                        row.appendChild(badge); row.appendChild(nameSpan); row.appendChild(confSpan);
                        wrapper.appendChild(row);
                    });
                    resultsEl.appendChild(wrapper);
                    var countP = document.createElement('p');
                    countP.className = 'text-gray-500 text-xs mt-2';
                    countP.textContent = data.total + ' result(s)';
                    resultsEl.appendChild(countP);
                } catch (e) { resultsEl.textContent = 'Search failed.'; }
            }, 300);
        }

        document.getElementById('entity-query').addEventListener('input', doEntitySearch);
        document.getElementById('entity-type-filter').addEventListener('change', doEntitySearch);

        // --- View Entity Graph ---
        document.getElementById('btn-view-graph').addEventListener('click', async function() {
            try {
                var resp = await fetch(apiUrl('/api/graph'));
                var data = await resp.json();
                showToast('Graph: ' + data.nodes.length + ' nodes, ' + data.edges.length + ' edges', false);
            } catch (e) { showToast('Failed to load graph.', true); }
        });

        // --- Init ---
        loadStats();
        loadRequests();
        loadDeadlines();
    </script>
</body>
</html>"""


def run_server(
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    data_dir: Path | None = None,
) -> None:
    """Run the OpenFOIA server."""
    import socket
    import uvicorn

    # Generate token if not provided
    if token is None:
        token = secrets.token_urlsafe(16)

    # Find available port if not specified
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

    # Create app
    app = create_app(token=token, data_dir=data_dir)

    # Print startup message
    url = f"http://{host}:{port}/?token={token}"
    print("\nOpenFOIA")
    print("-" * 50)
    print(f"Local server: {url}")
    print(f"Data stored:  {data_dir or Path.home() / '.openfoia'}")
    print("-" * 50)
    print("Your data never leaves this machine.")
    print("Press Ctrl+C to stop the server.\n")

    # Run server
    uvicorn.run(app, host=host, port=port, log_level="warning")
