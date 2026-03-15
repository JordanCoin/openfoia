"""FastAPI server for OpenFOIA web interface.

Binds only to localhost. Requires token authentication.
Your data never leaves your machine.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, HTTPException, Depends, Query, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func


class CreateRequestBody(BaseModel):
    agency_id: str
    subject: str
    body: str
    method: str = "email"
    fee_waiver: bool = True
    expedited: bool = False


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
    app.state.data_dir = data_dir or Path.home() / ".openfoia"
    app.state.data_dir.mkdir(parents=True, exist_ok=True)

    # CORS - only allow localhost
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1", "http://localhost"],
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Token verification dependency
    async def verify_token(
        request: Request,
        token: str = Query(None, alias="token"),
    ):
        # Check query param first, then cookie
        auth_token = token or request.cookies.get("openfoia_token")
        if auth_token != app.state.auth_token:
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
            pending = session.query(func.count(FOIARequest.id)).filter(
                FOIARequest.status.in_([
                    RequestStatus.SENT,
                    RequestStatus.ACKNOWLEDGED,
                    RequestStatus.PROCESSING,
                ])
            ).scalar() or 0
            complete = session.query(func.count(FOIARequest.id)).filter(
                FOIARequest.status == RequestStatus.COMPLETE
            ).scalar() or 0
            denied = session.query(func.count(FOIARequest.id)).filter(
                FOIARequest.status == RequestStatus.DENIED
            ).scalar() or 0

            total_docs = session.query(func.count(Document.id)).scalar() or 0
            processed_docs = session.query(func.count(Document.id)).filter(
                Document.ocr_completed == True  # noqa: E712
            ).scalar() or 0
            total_pages = session.query(func.coalesce(func.sum(Document.page_count), 0)).scalar()

            total_entities = session.query(func.count(Entity.id)).scalar() or 0
            people = session.query(func.count(Entity.id)).filter(
                Entity.entity_type == EntityType.PERSON
            ).scalar() or 0
            orgs = session.query(func.count(Entity.id)).filter(
                Entity.entity_type == EntityType.ORGANIZATION
            ).scalar() or 0

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
                    pass

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
                q = q.filter(
                    (Agency.name.ilike(search)) |
                    (Agency.abbreviation.ilike(search))
                )

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
        import shutil

        docs_dir = app.state.data_dir / "docs"
        docs_dir.mkdir(exist_ok=True)

        # Save file
        dest = docs_dir / file.filename
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)

        file_size = dest.stat().st_size

        from .db import get_session
        from .models import Document, DocumentType

        doc_id = str(uuid4())
        with get_session() as session:
            doc = Document(
                id=doc_id,
                request_id=request_id or "",
                doc_type=DocumentType.CORRESPONDENCE,
                filename=file.filename,
                file_path=str(dest),
                file_size=file_size,
                mime_type=file.content_type or "application/octet-stream",
            )
            session.add(doc)

        return {"id": doc_id, "filename": file.filename, "status": "uploaded"}

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

            nodes = [
                {
                    "id": e.id,
                    "label": e.normalized_text,
                    "type": e.entity_type.value,
                }
                for e in entities
            ]

            # Get links
            links = session.query(entity_links).all()
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
    """Return the main HTML interface."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenFOIA</title>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .gradient-bg {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
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
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Agency</label>
                            <input type="text" id="agency-search" placeholder="Search agencies..."
                                   class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
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
                        <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 rounded-lg px-4 py-3 font-semibold transition">
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
                        <button class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            Import Documents
                        </button>
                        <button class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            Search Entities
                        </button>
                        <button class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            View Entity Graph
                        </button>
                        <button class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            Generate Report
                        </button>
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

        <!-- Recent Requests -->
        <div class="mt-12">
            <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                <h2 class="text-xl font-semibold mb-4">Recent Requests</h2>
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

    <script>
        // Get token from URL
        const urlParams = new URLSearchParams(window.location.search);
        const token = urlParams.get('token');

        // Fetch stats on load
        async function loadStats() {
            try {
                const resp = await fetch(`/api/stats?token=${token}`);
                const data = await resp.json();
                document.getElementById('stat-requests').textContent = data.requests.total;
                document.getElementById('stat-documents').textContent = data.documents.total;
                document.getElementById('stat-pages').textContent = data.documents.pages;
                document.getElementById('stat-entities').textContent = data.entities.total;
            } catch (e) {
                console.error('Failed to load stats:', e);
            }
        }

        // Load recent requests
        async function loadRequests() {
            try {
                const resp = await fetch(`/api/requests?token=${token}&limit=10`);
                const data = await resp.json();
                const list = document.getElementById('requests-list');
                if (data.requests.length === 0) {
                    list.innerHTML = '<p class="text-gray-400 text-center py-8">No requests yet. Create your first FOIA request above.</p>';
                    return;
                }
                let html = '<table class="w-full text-left"><thead><tr class="text-gray-400 text-sm">' +
                    '<th class="pb-2">Request #</th><th class="pb-2">Agency</th><th class="pb-2">Subject</th>' +
                    '<th class="pb-2">Status</th><th class="pb-2">Days</th></tr></thead><tbody>';
                for (const r of data.requests) {
                    const statusColor = r.status === 'complete' ? 'text-green-400' :
                        r.status === 'denied' ? 'text-red-400' : 'text-yellow-400';
                    html += `<tr class="border-t border-white/10"><td class="py-2 text-cyan-400">${r.request_number}</td>` +
                        `<td class="py-2">${r.agency}</td><td class="py-2">${r.subject}</td>` +
                        `<td class="py-2 ${statusColor}">${r.status}</td>` +
                        `<td class="py-2">${r.days_pending}</td></tr>`;
                }
                html += '</tbody></table>';
                list.innerHTML = html;
            } catch (e) {
                console.error('Failed to load requests:', e);
            }
        }

        loadStats();
        loadRequests();
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
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]

    # Create app
    app = create_app(token=token, data_dir=data_dir)

    # Print startup message
    url = f"http://{host}:{port}/?token={token}"
    print(f"\nOpenFOIA")
    print("-" * 50)
    print(f"Local server: {url}")
    print(f"Data stored:  {data_dir or Path.home() / '.openfoia'}")
    print("-" * 50)
    print("Your data never leaves this machine.")
    print("Press Ctrl+C to stop the server.\n")

    # Run server
    uvicorn.run(app, host=host, port=port, log_level="warning")
