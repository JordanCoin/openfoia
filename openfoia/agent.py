"""Agent interface for LLM orchestration.

Allows an AI agent to drive the entire FOIA workflow:
- Research which agency to FOIA
- Draft requests
- Send via appropriate channel
- Track responses
- Process incoming documents
- Extract and link entities
- Generate reports
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import Agency, DeliveryMethod, Request, RequestStatus


@dataclass
class AgentAction:
    """An action the agent can take."""

    name: str
    description: str
    parameters: dict[str, Any]
    result: str | None = None


class OpenFOIAAgent:
    """Agent interface for AI-driven FOIA workflows.
    
    Exposes a tool-calling interface that LLMs can use to:
    1. Search for agencies
    2. Draft FOIA requests
    3. Send requests
    4. Check status
    5. Process responses
    6. Analyze documents
    7. Build entity graphs
    """

    def __init__(self, db_session: Any, config: dict[str, Any]):
        self.db = db_session
        self.config = config
        self._gateways: dict[str, Any] = {}

    def get_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions for LLM function calling."""
        return [
            {
                "name": "search_agencies",
                "description": "Search for government agencies that handle FOIA requests",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (agency name, abbreviation, or topic)",
                        },
                        "level": {
                            "type": "string",
                            "enum": ["federal", "state", "local", "all"],
                            "description": "Government level to search",
                        },
                        "state": {
                            "type": "string",
                            "description": "State code for state/local agencies (e.g., 'CA')",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_agency_info",
                "description": "Get detailed information about a specific agency",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agency_id": {
                            "type": "string",
                            "description": "Agency ID",
                        },
                    },
                    "required": ["agency_id"],
                },
            },
            {
                "name": "draft_request",
                "description": "Draft a new FOIA request",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agency_id": {
                            "type": "string",
                            "description": "Target agency ID",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Brief subject line for the request",
                        },
                        "records_requested": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of specific records being requested",
                        },
                        "date_range_start": {
                            "type": "string",
                            "description": "Start date for records (YYYY-MM-DD)",
                        },
                        "date_range_end": {
                            "type": "string",
                            "description": "End date for records (YYYY-MM-DD)",
                        },
                        "fee_waiver_justification": {
                            "type": "string",
                            "description": "Why a fee waiver should be granted",
                        },
                    },
                    "required": ["agency_id", "subject", "records_requested"],
                },
            },
            {
                "name": "send_request",
                "description": "Send a drafted FOIA request",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "description": "Request ID to send",
                        },
                        "method": {
                            "type": "string",
                            "enum": ["email", "fax", "mail"],
                            "description": "Delivery method (defaults to agency preference)",
                        },
                    },
                    "required": ["request_id"],
                },
            },
            {
                "name": "check_request_status",
                "description": "Check the status of a FOIA request",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "description": "Request ID to check",
                        },
                    },
                    "required": ["request_id"],
                },
            },
            {
                "name": "list_requests",
                "description": "List FOIA requests with optional filters",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["draft", "sent", "processing", "complete", "denied", "all"],
                            "description": "Filter by status",
                        },
                        "agency_id": {
                            "type": "string",
                            "description": "Filter by agency",
                        },
                        "days_pending": {
                            "type": "integer",
                            "description": "Filter by minimum days pending",
                        },
                    },
                },
            },
            {
                "name": "process_document",
                "description": "Process an incoming document (OCR + entity extraction)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_path": {
                            "type": "string",
                            "description": "Path to the document file",
                        },
                        "request_id": {
                            "type": "string",
                            "description": "Associated request ID",
                        },
                        "document_type": {
                            "type": "string",
                            "enum": ["response", "denial", "fee_estimate", "acknowledgment"],
                            "description": "Type of document",
                        },
                    },
                    "required": ["document_path"],
                },
            },
            {
                "name": "extract_entities",
                "description": "Extract entities from a processed document",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "description": "Document ID to analyze",
                        },
                    },
                    "required": ["document_id"],
                },
            },
            {
                "name": "build_entity_graph",
                "description": "Build/update the entity relationship graph",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Request IDs to include (empty = all)",
                        },
                    },
                },
            },
            {
                "name": "search_entities",
                "description": "Search across extracted entities",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Entity name or keyword to search",
                        },
                        "entity_type": {
                            "type": "string",
                            "enum": ["person", "organization", "location", "all"],
                            "description": "Filter by entity type",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "generate_report",
                "description": "Generate a report on FOIA findings",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Request IDs to include",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["markdown", "json", "html"],
                            "description": "Output format",
                        },
                        "include_evidence": {
                            "type": "boolean",
                            "description": "Include source citations",
                        },
                    },
                    "required": ["request_ids"],
                },
            },
        ]

    async def execute_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool and return the result."""
        handlers = {
            "search_agencies": self._search_agencies,
            "get_agency_info": self._get_agency_info,
            "draft_request": self._draft_request,
            "send_request": self._send_request,
            "check_request_status": self._check_request_status,
            "list_requests": self._list_requests,
            "process_document": self._process_document,
            "extract_entities": self._extract_entities,
            "build_entity_graph": self._build_entity_graph,
            "search_entities": self._search_entities,
            "generate_report": self._generate_report,
        }
        
        handler = handlers.get(name)
        if not handler:
            return {"error": f"Unknown tool: {name}"}
        
        try:
            return await handler(params)
        except Exception as e:
            return {"error": str(e)}

    async def _search_agencies(self, params: dict[str, Any]) -> dict[str, Any]:
        """Search for agencies."""
        # TODO: Implement with database query
        return {
            "agencies": [
                {
                    "id": "fbi-001",
                    "name": "Federal Bureau of Investigation",
                    "abbreviation": "FBI",
                    "level": "federal",
                    "preferred_method": "email",
                    "avg_response_days": 45,
                },
                {
                    "id": "doj-001",
                    "name": "Department of Justice",
                    "abbreviation": "DOJ",
                    "level": "federal",
                    "preferred_method": "email",
                    "avg_response_days": 60,
                },
            ],
            "total": 2,
        }

    async def _get_agency_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get agency details."""
        # TODO: Implement
        return {
            "id": params.get("agency_id"),
            "name": "Federal Bureau of Investigation",
            "foia_email": "foiparequest@fbi.gov",
            "foia_address": "FBI FOIA/PA Request\\nRecord Management Division\\n170 Marcel Drive\\nWinchester, VA 22602",
            "foia_portal": "https://vault.fbi.gov/",
            "typical_response_days": 45,
            "fee_waiver_criteria": "News media, educational institutions, scientific research",
        }

    async def _draft_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Draft a FOIA request."""
        # Build request body
        records = params.get("records_requested", [])
        records_text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(records))
        
        body = f"""This is a request under the Freedom of Information Act, 5 U.S.C. § 552.

I request copies of the following records:

{records_text}

Date range: {params.get('date_range_start', 'earliest available')} to {params.get('date_range_end', 'present')}.

Fee Waiver Request:
{params.get('fee_waiver_justification', 'I request a fee waiver as disclosure of this information is in the public interest.')}

Please contact me if you have questions about this request.
"""
        
        import uuid
        request_id = str(uuid.uuid4())[:8]
        
        return {
            "request_id": request_id,
            "status": "draft",
            "agency_id": params.get("agency_id"),
            "subject": params.get("subject"),
            "body_preview": body[:500] + "...",
            "message": "Request drafted. Use send_request to send it.",
        }

    async def _send_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Send a FOIA request."""
        # TODO: Implement with gateway
        return {
            "request_id": params.get("request_id"),
            "status": "sent",
            "method": params.get("method", "email"),
            "sent_at": datetime.utcnow().isoformat(),
            "tracking_id": "EMAIL-2026-0001",
            "message": "Request sent successfully.",
        }

    async def _check_request_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check request status."""
        # TODO: Implement
        return {
            "request_id": params.get("request_id"),
            "status": "processing",
            "agency_tracking_number": "FOI-2026-12345",
            "sent_at": "2026-01-15T10:00:00Z",
            "acknowledged_at": "2026-01-17T14:22:00Z",
            "days_pending": 35,
            "is_overdue": False,
            "timeline": [
                {"event": "sent", "date": "2026-01-15"},
                {"event": "acknowledged", "date": "2026-01-17"},
                {"event": "processing", "date": "2026-01-18"},
            ],
        }

    async def _list_requests(self, params: dict[str, Any]) -> dict[str, Any]:
        """List requests."""
        # TODO: Implement with database query
        return {
            "requests": [
                {
                    "request_id": "req-001",
                    "agency": "FBI",
                    "subject": "Records on Project X",
                    "status": "processing",
                    "days_pending": 35,
                },
            ],
            "total": 1,
        }

    async def _process_document(self, params: dict[str, Any]) -> dict[str, Any]:
        """Process a document."""
        # TODO: Implement with pipeline
        return {
            "document_id": "doc-001",
            "filename": params.get("document_path", "").split("/")[-1],
            "pages": 15,
            "ocr_confidence": 0.94,
            "text_extracted": True,
            "message": "Document processed. Use extract_entities to analyze.",
        }

    async def _extract_entities(self, params: dict[str, Any]) -> dict[str, Any]:
        """Extract entities from a document."""
        # TODO: Implement with extractor
        return {
            "document_id": params.get("document_id"),
            "entities": [
                {"type": "PERSON", "text": "John Smith", "confidence": 0.98},
                {"type": "ORGANIZATION", "text": "Acme Corp", "confidence": 0.95},
                {"type": "MONEY", "text": "$1,500,000", "confidence": 0.99},
            ],
            "relationships": [
                {"source": "John Smith", "relation": "works_for", "target": "Acme Corp"},
            ],
            "total_entities": 3,
        }

    async def _build_entity_graph(self, params: dict[str, Any]) -> dict[str, Any]:
        """Build entity graph."""
        # TODO: Implement
        return {
            "entities": 234,
            "relationships": 567,
            "connected_components": 12,
            "graph_file": "graph.json",
        }

    async def _search_entities(self, params: dict[str, Any]) -> dict[str, Any]:
        """Search entities."""
        # TODO: Implement
        return {
            "query": params.get("query"),
            "results": [
                {
                    "id": "ent-001",
                    "type": "PERSON",
                    "name": params.get("query"),
                    "occurrences": 12,
                    "documents": ["doc-001", "doc-003"],
                    "linked_entities": ["Acme Corp", "DOJ"],
                },
            ],
        }

    async def _generate_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """Generate a report."""
        # TODO: Implement
        return {
            "format": params.get("format", "markdown"),
            "report_file": "report.md",
            "sections": [
                "Executive Summary",
                "Key Findings",
                "Entity Analysis",
                "Evidence Chain",
                "Appendix: Source Documents",
            ],
            "message": "Report generated.",
        }


# System prompt for AI agents using OpenFOIA

AGENT_SYSTEM_PROMPT = """You are an AI assistant helping with Freedom of Information Act (FOIA) requests.

You have access to the OpenFOIA toolkit which allows you to:
1. Search for and identify the correct government agency to FOIA
2. Draft properly-formatted FOIA requests
3. Send requests via email, fax, or mail
4. Track request status and deadlines
5. Process incoming documents (OCR, entity extraction)
6. Build relationship graphs across documents
7. Generate analysis reports

FOIA Best Practices:
- Be specific about what records you're requesting
- Include date ranges to narrow the scope
- Request fee waivers when disclosure is in the public interest
- Follow up on requests that exceed response deadlines
- Appeal denials when exemptions are improperly applied

When processing responses:
- Extract all named entities (people, organizations, dates, amounts)
- Build relationship graphs to identify connections
- Document evidence chains with source citations
- Flag redactions and note which exemptions were cited

Always cite specific documents and page numbers when reporting findings.
"""
