"""FastAPI server for OpenFOIA web interface.

Binds only to localhost. Requires token authentication.
Your data never leaves your machine.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


def create_app(token: str, data_dir: Path | None = None) -> FastAPI:
    """Create the FastAPI application with token authentication."""
    
    app = FastAPI(
        title="OpenFOIA",
        description="Crowdsourced FOIA automation with AI-powered document analysis",
        version="0.0.1",
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
        allow_origins=["http://127.0.0.1:*", "http://localhost:*"],
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
        return {"status": "ok", "version": "0.0.1"}
    
    @app.get("/api/stats")
    async def stats(token: str = Depends(verify_token)):
        """Get overview statistics."""
        # TODO: Query actual database
        return {
            "requests": {
                "total": 0,
                "pending": 0,
                "complete": 0,
                "denied": 0,
            },
            "documents": {
                "total": 0,
                "processed": 0,
                "pages": 0,
            },
            "entities": {
                "total": 0,
                "people": 0,
                "organizations": 0,
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
        # TODO: Query database
        return {"requests": [], "total": 0}
    
    @app.post("/api/requests")
    async def create_request(
        token: str = Depends(verify_token),
        request_data: dict[str, Any] = {},
    ):
        """Create a new FOIA request."""
        # TODO: Implement
        return {"id": "new-request-id", "status": "draft"}
    
    @app.get("/api/agencies")
    async def list_agencies(
        token: str = Depends(verify_token),
        query: str | None = None,
        level: str | None = None,
    ):
        """Search agencies."""
        # TODO: Query agency database
        return {"agencies": [], "total": 0}
    
    @app.get("/api/documents")
    async def list_documents(
        token: str = Depends(verify_token),
        request_id: str | None = None,
    ):
        """List documents."""
        # TODO: Query database
        return {"documents": [], "total": 0}
    
    @app.post("/api/documents/upload")
    async def upload_document(
        token: str = Depends(verify_token),
        # file: UploadFile,  # TODO: Add file upload
    ):
        """Upload a document for processing."""
        # TODO: Implement
        return {"id": "new-doc-id", "status": "pending"}
    
    @app.get("/api/entities")
    async def list_entities(
        token: str = Depends(verify_token),
        query: str | None = None,
        entity_type: str | None = None,
    ):
        """Search entities."""
        # TODO: Query database
        return {"entities": [], "total": 0}
    
    @app.get("/api/graph")
    async def get_graph(
        token: str = Depends(verify_token),
        request_ids: str | None = None,
    ):
        """Get entity relationship graph."""
        # TODO: Build graph
        return {"nodes": [], "edges": []}
    
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
                    <h1 class="text-4xl font-bold mb-2">🔒 OpenFOIA</h1>
                    <p class="text-gray-400">Crowdsourced FOIA automation • Your data stays local</p>
                </div>
                <div class="text-right text-sm text-gray-500">
                    <div>v0.0.1</div>
                    <div id="status" class="text-green-400">● Connected</div>
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
                    <h2 class="text-xl font-semibold mb-4">📝 New FOIA Request</h2>
                    <form id="new-request-form" class="space-y-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Agency</label>
                            <input type="text" placeholder="Search agencies..." 
                                   class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Subject</label>
                            <input type="text" placeholder="Brief description of records requested"
                                   class="w-full bg-white/5 border border-white/20 rounded-lg px-4 py-2 focus:outline-none focus:border-blue-500">
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Records Requested</label>
                            <textarea rows="4" placeholder="Describe the specific records you're requesting..."
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
                                <input type="checkbox" checked class="rounded">
                                <span class="text-sm">Request fee waiver</span>
                            </label>
                            <label class="flex items-center gap-2">
                                <input type="checkbox" class="rounded">
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
                    <h2 class="text-xl font-semibold mb-4">⚡ Quick Actions</h2>
                    <div class="space-y-3">
                        <button class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            📄 Import Documents
                        </button>
                        <button class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            🔍 Search Entities
                        </button>
                        <button class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            🗺️ View Entity Graph
                        </button>
                        <button class="w-full bg-white/5 hover:bg-white/10 rounded-lg px-4 py-3 text-left transition">
                            📊 Generate Report
                        </button>
                    </div>
                </div>

                <div class="bg-white/10 rounded-xl p-6 backdrop-blur">
                    <h2 class="text-xl font-semibold mb-4">🔒 Privacy</h2>
                    <div class="text-sm text-gray-400 space-y-2">
                        <p>✓ All data stored locally</p>
                        <p>✓ No cloud services required</p>
                        <p>✓ Server only binds to localhost</p>
                        <p>✓ Token-authenticated session</p>
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
                <h2 class="text-xl font-semibold mb-4">📋 Recent Requests</h2>
                <div id="requests-list" class="text-gray-400 text-center py-8">
                    No requests yet. Create your first FOIA request above.
                </div>
            </div>
        </div>

        <!-- Footer -->
        <footer class="mt-12 text-center text-gray-500 text-sm">
            <p>OpenFOIA is open source software. <a href="https://github.com/JordanCoin/openfoia" class="text-blue-400 hover:underline">View on GitHub</a></p>
            <p class="mt-2">Transparency is patriotic. 🇺🇸</p>
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
        
        loadStats();
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
    print(f"\n🔒 OpenFOIA")
    print("─" * 50)
    print(f"Local server: {url}")
    print(f"Data stored:  {data_dir or Path.home() / '.openfoia'}")
    print("─" * 50)
    print("Your data never leaves this machine.")
    print("Press Ctrl+C to stop the server.\n")
    
    # Run server
    uvicorn.run(app, host=host, port=port, log_level="warning")
