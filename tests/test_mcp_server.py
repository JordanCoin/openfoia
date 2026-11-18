"""`openfoia mcp serve`: the MCP server, its gates, its read-only tools and its stdout.

Offline. The handler tests call :func:`openfoia.mcp_server.call_tool` directly
and run without the optional ``mcp`` package. The stdio tests start the real
server as a subprocess (sockets blocked inside it too) and are skipped when
``mcp`` is not installed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from openfoia import doctext, net
from openfoia import mcp_server as M
from openfoia import search as search_mod
from openfoia.cli import app
from openfoia.net import EgressPolicy
from openfoia.records.base import RecordEntity, SearchResult

runner = CliRunner()
REPO_ROOT = Path(__file__).parent.parent
HAS_MCP = importlib.util.find_spec("mcp") is not None
needs_mcp = pytest.mark.skipif(not HAS_MCP, reason="optional extra 'mcp' not installed")

OFF = M.ServerOptions()
ON = M.ServerOptions(allow_network=True)

EXPECTED_TOOLS = {
    "openfoia_search",
    "openfoia_docs_grep",
    "openfoia_read_page",
    "openfoia_list_cases",
    "openfoia_case_summary",
    "openfoia_list_requests",
    "openfoia_entities",
}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    for var in (
        "OPENFOIA_DB_PASSWORD",
        "OPENFOIA_TOR",
        "OPENFOIA_TOR_HOST",
        "OPENFOIA_TOR_PORT",
        "OPENFOIA_NARA_API_KEY",
        "CATALOG_API_KEY",
        "OPENFOIA_MUCKROCK_TOKEN",
        "OPENFOIA_SEMANTIC_BACKEND",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "data"


def call(name, args=None, opts=OFF):
    return asyncio.run(M.call_tool(name, args if args is not None else {}, opts))


# --- fakes -----------------------------------------------------------------------


class Fake:
    """A records adapter stand-in that records every search it is asked to run."""

    calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, name, behaviour="ok"):
        self.name = name
        self.behaviour = behaviour
        self.credentials_withheld = False

    async def search(self, query, **kwargs):
        Fake.calls.append((self.name, query))
        if self.behaviour == "api_error":
            return SearchResult(self.name, query, 0, [], error="HTTP 503 from upstream")
        entity = RecordEntity(
            entity_type="DOCUMENT",
            name=f"{self.name} hit",
            source=self.name,
            source_url=f"https://{self.name}.example/1",
            identifiers={"id": f"{self.name}-1"},
            extra_data={"description": "a snippet"},
        )
        return SearchResult(self.name, query, 1, [entity])


@pytest.fixture
def registry(monkeypatch):
    Fake.calls = []

    def install(**spec):
        monkeypatch.setattr("openfoia.records.list_sources", lambda: sorted(spec))
        monkeypatch.setattr(
            "openfoia.records.get_adapter",
            lambda name, *, egress=None, api_data_gov_key=None: Fake(name, spec[name]),
        )

    return install


def _tor(monkeypatch, *, reachable):
    class Client:
        async def aclose(self):
            return None

    async def check_tor(policy, timeout=5.0):
        return reachable

    monkeypatch.setattr(net, "egress_client", lambda *a, **k: Client())
    monkeypatch.setattr(net, "check_tor", check_tor)


# --- seeded database ----------------------------------------------------------------


def _ok(result):
    assert result.exit_code == 0, result.output
    return result.output


def _add_doc(pages, *, title, case_id=None, entities=(), number=None):
    """A stored document with pages and (type, name) entities, linked in a chain."""
    from openfoia.db import get_session
    from openfoia.models import (
        Agency,
        DeliveryMethod,
        Document,
        DocumentType,
        Entity,
        EntityType,
        Request,
        RequestStatus,
        User,
        entity_links,
    )

    with get_session() as s:
        user = s.query(User).first() or User(id=str(uuid4()), email="a@b.c", name="L")
        s.add(user)
        s.flush()
        req = Request(
            id=str(uuid4()),
            request_number=number or f"REQ-{uuid4().hex[:6]}",
            requester_id=user.id,
            agency_id=s.query(Agency).first().id,
            subject=title,
            body="local",
            delivery_method=DeliveryMethod.EMAIL,
            status=RequestStatus.SENT,
            case_id=case_id,
        )
        s.add(req)
        s.flush()
        doc = Document(
            id=str(uuid4()),
            request_id=req.id,
            doc_type=DocumentType.FULL_RESPONSE,
            filename=f"{title}.txt",
            file_path="",
            file_size=0,
            mime_type="text/plain",
            page_count=len(pages),
            extracted_text=doctext.join_pages(pages),
        )
        s.add(doc)
        doctext.replace_pages(s, doc.id, pages)
        rows = []
        for etype, name in entities:
            e = Entity(
                document_id=doc.id,
                entity_type=EntityType(etype),
                raw_text=name,
                normalized_text=name,
                confidence=0.9,
            )
            s.add(e)
            rows.append(e)
        s.flush()
        for a, b in itertools.pairwise(rows):
            s.execute(
                entity_links.insert().values(source_id=a.id, target_id=b.id, link_type="mentioned")
            )
        return doc.id


@pytest.fixture
def seeded(data_dir):
    """Two cases; 'Water' is active and holds the Flint memo."""
    from openfoia.cases import get_active_case_id
    from openfoia.db import init_db

    init_db()
    _ok(runner.invoke(app, ["case", "new", "Other"]))
    other = get_active_case_id()
    secret = _add_doc(
        ["Unrelated payroll ledger. Hocking appears here too."], title="ledger", case_id=other
    )
    _ok(runner.invoke(app, ["case", "new", "Water", "--desc", "Flint pipes"]))
    water = get_active_case_id()
    memo = _add_doc(
        ["Cover page", "Darnell Earley approved the switch to the Flint River. Hocking signed."],
        title="flint-memo",
        case_id=water,
        entities=[("person", "Darnell Earley"), ("organization", "Flint River Authority")],
        number="REQ-FLINT-1",
    )
    return {"memo": memo, "secret": secret, "water": water, "other": other}


# --- tool list and schemas ------------------------------------------------------------


def test_tool_list_is_exactly_the_safe_set(registry):
    registry(documentcloud="ok", sec="ok")
    tools = M.tool_list(OFF)
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    for t in tools:
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "untrusted" in t["description"] and "never instructions" in t["description"]
        assert t["annotations"]["destructiveHint"] is False
        network = t["name"] == "openfoia_search"
        assert t["annotations"]["openWorldHint"] is network
        assert t["annotations"]["readOnlyHint"] is (not network)
    # Nothing that deletes, purges, sends, ingests or configures.
    for word in ("delete", "purge", "send", "mail", "fax", "config", "ingest", "rm", "write"):
        assert not any(word in t["name"].split("_") for t in tools), word


def test_search_tool_has_the_agent_tools_input_shape_and_live_sources(registry):
    from openfoia.agent import OpenFOIAAgent

    registry(documentcloud="ok", sec="ok")
    agent = next(t for t in OpenFOIAAgent(None, {}).get_tools() if t["name"] == "search_records")
    tool = next(t for t in M.tool_list(OFF) if t["name"] == "openfoia_search")
    assert tool["inputSchema"]["properties"] == agent["parameters"]["properties"]
    assert tool["inputSchema"]["required"] == ["query"]
    props = tool["inputSchema"]["properties"]
    assert props["sources"]["items"]["enum"] == ["documentcloud", "sec"]
    assert props["egress"]["enum"] == ["tor", "direct"]
    assert set(props) == {"query", "sources", "egress", "limit", "since", "confirmed"}


def test_search_description_says_whether_the_operator_enabled_network(registry):
    registry(sec="ok")
    off = next(t for t in M.tool_list(OFF) if t["name"] == "openfoia_search")
    on = next(t for t in M.tool_list(ON) if t["name"] == "openfoia_search")
    assert "Network search is OFF" in off["description"]
    assert "ENABLED" in on["description"]


def test_mcp_tools_command_lists_tools_without_the_extra(registry):
    registry(sec="ok")
    out = json.loads(_ok(runner.invoke(app, ["mcp", "tools", "--json"])))
    assert {t["name"] for t in out} == EXPECTED_TOOLS
    assert "openfoia_search" in _ok(runner.invoke(app, ["mcp", "tools"]))


# --- openfoia_search gates --------------------------------------------------------------


def test_search_network_is_off_unless_the_operator_enabled_it(registry):
    registry(documentcloud="ok")
    out, is_error = call("openfoia_search", {"query": "Acme", "confirmed": True})
    assert is_error
    assert "turned off" in out["error"] and "--allow-network" in out["error"]
    assert out["would_contact"] == ["documentcloud"]  # the preview, still nothing sent
    assert Fake.calls == []


def test_search_first_call_without_confirmed_returns_the_warning_and_sends_nothing(registry):
    registry(documentcloud="ok", sec="ok")
    out, is_error = call("openfoia_search", {"query": "Acme"}, ON)
    assert not is_error
    assert out["status"] == "confirmation_required"
    plan = search_mod.plan_search("Acme", policy=EgressPolicy())
    assert out["warning"] == plan.warning_lines()  # the CLI's warning, word for word
    assert out["would_contact"] == ["documentcloud", "sec"]
    assert Fake.calls == []


def test_search_confirmed_false_or_truthy_string_is_not_consent(registry):
    registry(sec="ok")
    out, _ = call("openfoia_search", {"query": "Acme", "confirmed": False}, ON)
    assert out["status"] == "confirmation_required"
    out, is_error = call("openfoia_search", {"query": "Acme", "confirmed": "true"}, ON)
    assert is_error and "boolean" in out["error"]
    assert Fake.calls == []


def test_search_returns_the_same_json_as_the_cli(registry):
    registry(documentcloud="ok", sec="api_error")
    out, is_error = call("openfoia_search", {"query": "Acme", "confirmed": True, "limit": 1}, ON)
    assert not is_error
    assert Fake.calls == [("documentcloud", "Acme"), ("sec", "Acme")]
    cli = runner.invoke(app, ["search", "Acme", "--json", "-y", "-n", "1"])
    cli_out = json.loads(cli.stdout)

    def strip_times(d):
        d = json.loads(json.dumps(d))
        for item in d["sources"] + d["results"]:
            item.pop("retrieved_at")
        return d

    sent = json.loads(M.dumps(out))  # what the client receives
    assert strip_times(sent) == strip_times(cli_out)
    assert M.dumps(out).isascii()


def test_search_cannot_switch_a_configured_tor_route_to_direct(registry, monkeypatch):
    registry(documentcloud="ok")
    monkeypatch.setenv("OPENFOIA_TOR", "true")
    _tor(monkeypatch, reachable=True)
    out, is_error = call(
        "openfoia_search", {"query": "Acme", "egress": "direct", "confirmed": True}, ON
    )
    assert is_error and "cannot switch a search to direct" in out["error"]
    assert Fake.calls == []


def test_search_tor_fails_closed(registry, monkeypatch):
    registry(documentcloud="ok")
    monkeypatch.setenv("OPENFOIA_TOR", "true")  # configured route, no egress argument
    _tor(monkeypatch, reachable=False)
    out, is_error = call("openfoia_search", {"query": "Acme", "confirmed": True}, ON)
    assert is_error and "not reachable" in out["error"] and "Nothing was sent" in out["error"]
    out, is_error = call(
        "openfoia_search", {"query": "Acme", "egress": "tor", "confirmed": True}, ON
    )
    assert is_error and "not reachable" in out["error"]
    assert Fake.calls == []


def test_search_bad_arguments_are_refused_before_planning(registry):
    registry(sec="ok")
    for args, msg in (
        ({}, "Missing argument"),
        ({"query": "Acme", "sources": ["nowhere"]}, "must be one of"),
        ({"query": "Acme", "egress": "socks"}, "must be one of"),
        ({"query": "Acme", "limit": 0}, "at least 1"),
        ({"query": "Acme", "limit": True}, "integer"),
        ({"query": "Acme", "allow_network": True}, "Unknown argument"),
        ({"query": "Acme", "since": "last week"}, "YYYY-MM-DD"),
    ):
        out, is_error = call("openfoia_search", args, ON)
        assert is_error and msg in out["error"], (args, out)
    assert Fake.calls == []


def test_unknown_tool_is_an_error():
    out, is_error = call("openfoia_purge", {})
    assert is_error and "Unknown tool" in out["error"]


# --- read-only local tools ------------------------------------------------------------


def test_docs_grep_returns_stored_passages_scoped_to_the_active_case(seeded):
    out, is_error = call("openfoia_docs_grep", {"query": "Hocking"})
    assert not is_error
    assert out["engine"] in ("fts5", "like")
    assert out["scope"] == "case 'Water'"
    assert [h["document_id"] for h in out["hits"]] == [seeded["memo"]]
    hit = out["hits"][0]
    assert hit["page"] == 2 and "Hocking" in hit["passage"]
    assert out["content_trust"].startswith("untrusted")

    everything, _ = call("openfoia_docs_grep", {"query": "Hocking", "all_docs": True})
    assert {h["document_id"] for h in everything["hits"]} == {seeded["memo"], seeded["secret"]}
    other, _ = call("openfoia_docs_grep", {"query": "Hocking", "case": "Other"})
    assert [h["document_id"] for h in other["hits"]] == [seeded["secret"]]


def test_docs_grep_never_reaches_another_cases_document_by_id(seeded):
    out, _ = call("openfoia_docs_grep", {"query": "Hocking", "documents": [seeded["secret"][:8]]})
    assert out["hits"] == []


def test_semantic_mode_unavailable_says_so(seeded, monkeypatch):
    from openfoia import semantic

    def missing(name=None):
        raise semantic.SemanticUnavailable("Semantic search needs the optional extra.")

    monkeypatch.setattr(semantic, "load_embedder", missing)
    out, is_error = call("openfoia_docs_grep", {"query": "river", "mode": "semantic"})
    assert is_error and "optional extra" in out["error"] and "mode=keyword" in out["error"]


@pytest.mark.skipif(importlib.util.find_spec("numpy") is None, reason="numpy not installed")
def test_semantic_mode_ranks_by_meaning_and_names_the_engine(seeded, monkeypatch):
    from openfoia import semantic

    class Embedder:
        name, dim = "fake-test", 8

        def encode(self, texts):
            return [
                [float(t.lower().count(w)) for w in ("river", "flint", "x") + ("",) * 5]
                for t in texts
            ]

    monkeypatch.setenv("OPENFOIA_SEMANTIC_BACKEND", "numpy")
    monkeypatch.setattr(semantic, "load_embedder", lambda name=None: Embedder())
    out, is_error = call("openfoia_docs_grep", {"query": "flint river", "mode": "semantic"})
    assert not is_error, out
    assert out["mode"] == "semantic" and out["engine"] == "numpy"
    assert out["hits"][0]["document_id"] == seeded["memo"]
    assert "score" in out["hits"][0]
    assert "Embedded" in out["note"]


def test_read_page_returns_the_page_and_its_citation(seeded):
    out, is_error = call("openfoia_read_page", {"document": seeded["memo"][:8], "page": 2})
    assert not is_error
    assert "Darnell Earley approved" in out["text"]
    assert out["document"]["id"] == seeded["memo"]
    assert out["document"]["stored_pages"] == 2
    assert out["page_is_real"] is True and out["truncated"] is False
    missing, is_error = call("openfoia_read_page", {"document": seeded["memo"], "page": 9})
    assert is_error and "no stored page 9" in missing["error"]


def test_list_cases_and_case_summary(seeded):
    out, _ = call("openfoia_list_cases")
    by_name = {c["name"]: c for c in out["cases"]}
    assert set(by_name) == {"Other", "Water"}
    assert by_name["Water"]["active"] and not by_name["Other"]["active"]
    assert by_name["Water"]["documents"] == 1

    summary, is_error = call("openfoia_case_summary")
    assert not is_error
    assert summary["name"] == "Water" and summary["description"] == "Flint pipes"
    assert [d["id"] for d in summary["documents"]] == [seeded["memo"]]
    assert summary["entity_count"] == 2
    other, _ = call("openfoia_case_summary", {"case": "Other"})
    assert [d["id"] for d in other["documents"]] == [seeded["secret"]]
    nope, is_error = call("openfoia_case_summary", {"case": "Nope"})
    assert is_error and "No case named" in nope["error"]


def test_list_requests_is_the_agent_tools_listing(seeded):
    out, is_error = call("openfoia_list_requests", {"status": "sent"})
    assert not is_error
    numbers = {r["request_number"] for r in out["requests"]}
    assert "REQ-FLINT-1" in numbers and out["total"] == 2
    none, _ = call("openfoia_list_requests", {"status": "denied"})
    assert none["requests"] == []


def test_entities_and_graph_summary_for_a_document_and_a_case(seeded):
    out, is_error = call("openfoia_entities", {"document": seeded["memo"]})
    assert not is_error
    assert out["scope"] == f"document {seeded['memo']}"
    assert {(e["type"], e["name"]) for e in out["entities"]} == {
        ("person", "Darnell Earley"),
        ("organization", "Flint River Authority"),
    }
    assert out["relationships"] == 1
    assert out["top_relationships"] == [
        {
            "source": "Darnell Earley",
            "source_type": "person",
            "target": "Flint River Authority",
            "target_type": "organization",
            "type": "mentioned",
            "count": 1,
        }
    ]
    case, _ = call("openfoia_entities", {"entity_type": "person"})
    assert case["scope"] == "case 'Water'"
    assert [e["name"] for e in case["entities"]] == ["Darnell Earley"]
    other, _ = call("openfoia_entities", {"case": "Other"})
    assert other["entities"] == [] and other["relationships"] == 0


def test_local_tools_never_create_a_database(data_dir):
    from openfoia.db import get_db_path

    out, is_error = call("openfoia_list_cases")
    assert is_error and "openfoia init" in out["error"]
    assert not get_db_path().exists()


def test_local_tools_never_touch_the_network(seeded, monkeypatch):
    """conftest blocks sockets; also make sure no egress client is even built."""

    def boom(*a, **k):
        raise AssertionError("egress client built by a local tool")

    monkeypatch.setattr(net, "egress_client", boom)
    for name, args in (
        ("openfoia_docs_grep", {"query": "Flint"}),
        ("openfoia_read_page", {"document": seeded["memo"]}),
        ("openfoia_list_cases", {}),
        ("openfoia_case_summary", {}),
        ("openfoia_list_requests", {}),
        ("openfoia_entities", {}),
    ):
        out, is_error = call(name, args)
        assert not is_error, (name, out)


# --- path confinement ---------------------------------------------------------------


def test_no_tool_takes_a_path(registry):
    registry(sec="ok")
    pathish = ("path", "file", "dir", "folder", "output", "url")
    for t in M.tool_list(ON):
        for prop in t["inputSchema"]["properties"]:
            assert not any(p in prop.lower() for p in pathish), (t["name"], prop)


def test_document_arguments_are_ids_not_paths(seeded, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET KEY MATERIAL")
    for ref in (str(secret), "../../../etc/passwd", "~/.ssh/id_rsa", "%", "_"):
        out, is_error = call("openfoia_read_page", {"document": ref})
        assert is_error, ref
        assert "not found" in out["error"] or "too long" in out["error"], ref
        assert "TOP SECRET" not in json.dumps(out)
    # A path smuggled in as an extra argument is refused, not ignored.
    out, is_error = call("openfoia_docs_grep", {"query": "x", "path": str(secret)})
    assert is_error and "Unknown argument" in out["error"]


# --- logging -------------------------------------------------------------------------


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_default_logging_drops_arguments_and_exception_text(restore_logging, capsys):
    M.configure_logging()
    log = logging.getLogger("openfoia.test")
    log.warning("Search for %s failed", "Darnell Earley")
    try:
        raise ValueError("page text: Darnell Earley approved")
    except ValueError:
        log.exception("Tool %s failed", "openfoia_docs_grep")
    logging.getLogger("httpx").info("GET https://x.example/?q=Darnell+Earley")
    err = capsys.readouterr()
    assert err.out == ""
    assert "Search for %s failed" in err.err and "[ValueError]" in err.err
    assert "Darnell" not in err.err


def test_a_failing_tool_returns_the_type_only(seeded, monkeypatch):
    def broken(args):
        raise RuntimeError("/home/journalist/secret path and page text")

    monkeypatch.setattr(M, "_tool_list_cases", broken)
    out, is_error = call("openfoia_list_cases")
    assert is_error and out["error"] == "Tool 'openfoia_list_cases' failed (RuntimeError)."


# --- the optional extra -----------------------------------------------------------


def test_core_import_does_not_import_the_mcp_package():
    code = (
        "import sys, openfoia.cli, openfoia.mcp_server, openfoia.agent; print('mcp' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT, check=True
    )
    assert out.stdout.strip() == "False"


def test_serve_without_the_extra_prints_the_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp", None)  # import mcp -> ImportError
    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 1
    assert "openfoia install-extras mcp" in result.output


def test_install_extras_knows_mcp(monkeypatch):
    seen = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )
    _ok(runner.invoke(app, ["install-extras", "mcp"]))
    assert seen[0][-1] == "openfoia[mcp]"


@needs_mcp
def test_server_drops_the_sdks_telemetry_middleware():
    server = M.build_server(OFF)
    names = [type(m).__name__ for m in getattr(server, "middleware", [])]
    assert not any("OpenTelemetry" in n for n in names)


def test_server_uses_stdio_only():
    """The server module imports the stdio transport and nothing that listens."""
    import ast

    imported = set()
    for node in ast.walk(ast.parse(Path(M.__file__).read_text())):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    assert "mcp.server.stdio.stdio_server" in imported
    for name in imported:
        for bad in ("socket", "sse", "streamable_http", "uvicorn", "starlette", "http.server"):
            assert bad not in name.split("."), name


# --- the real server over stdio -------------------------------------------------------

# Runs the CLI with outbound sockets blocked, so a leak fails instead of connecting.
BOOT = """
import socket, sys
def _blocked(*a, **k):
    raise OSError("network blocked by test")
socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
{extra}
from openfoia.cli import main
sys.argv = ["openfoia", *sys.argv[1:]]
main()
"""

STRAY = """
import os, sys
from openfoia import mcp_server
_real = mcp_server._tool_list_cases
def _noisy(args):
    print("STRAY print from a handler")
    sys.stdout.write("STRAY sys.stdout write\\n")
    sys.stdout.flush()
    os.write(1, b"STRAY raw fd 1 write\\n")
    os.system("echo STRAY child process")
    return _real(args)
mcp_server._tool_list_cases = _noisy
"""


class StdioClient:
    """Line-by-line JSON-RPC over a subprocess's stdin/stdout."""

    def __init__(self, env, *args, extra=""):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", BOOT.format(extra=extra), "mcp", "serve", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
        )
        self.raw: list[str] = []
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.next_id = 0

    def _pump(self):
        for line in self.proc.stdout:
            self.raw.append(line)
            self.lines.put(line)
        self.lines.put(None)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        while True:
            line = self.lines.get(timeout=60)
            assert line is not None, "server closed stdout"
            frame = json.loads(line)  # every stdout line must be a JSON-RPC frame
            if frame.get("id") == self.next_id:
                return frame

    def call(self, name, arguments):
        frame = self.request("tools/call", {"name": name, "arguments": arguments})
        result = frame["result"]
        return json.loads(result["content"][0]["text"]), result.get("isError", False)

    def close(self):
        self.proc.stdin.close()
        _, err = self.proc.communicate(timeout=30)
        return err


def _env(data_dir):
    env = dict(os.environ)
    env["OPENFOIA_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), env.get("PYTHONPATH", "")])
    for var in ("OPENFOIA_TOR", "OPENFOIA_DB_PASSWORD", "OPENFOIA_MUCKROCK_TOKEN"):
        env.pop(var, None)
    return env


def _initialize(client):
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "openfoia-test", "version": "0"},
        },
    )
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return init


@needs_mcp
def test_stdio_session_puts_nothing_but_protocol_frames_on_stdout(seeded, data_dir):
    client = StdioClient(_env(data_dir))
    try:
        init = _initialize(client)
        assert init["result"]["serverInfo"]["name"] == "openfoia"
        assert "tools" in init["result"]["capabilities"]

        tools = client.request("tools/list")["result"]["tools"]
        assert {t["name"] for t in tools} == EXPECTED_TOOLS

        out, is_error = client.call("openfoia_search", {"query": "Darnell Earley"})
        assert is_error and "turned off" in out["error"]  # no --allow-network

        out, is_error = client.call("openfoia_docs_grep", {"query": "Hocking"})
        assert not is_error and out["hits"][0]["document_id"] == seeded["memo"]
    finally:
        err = client.close()
    assert client.proc.returncode == 0
    for line in client.raw:
        frame = json.loads(line)
        assert frame["jsonrpc"] == "2.0"
    # Default logging never echoes a query or a passage into the client's log file.
    assert "Darnell" not in err and "Hocking" not in err


@needs_mcp
def test_stdio_search_with_allow_network_still_needs_confirmation(data_dir):
    client = StdioClient(_env(data_dir), "--allow-network")
    try:
        _initialize(client)
        out, is_error = client.call("openfoia_search", {"query": "Acme", "sources": ["sec"]})
        assert not is_error
        assert out["status"] == "confirmation_required"
        assert out["would_contact"] == ["sec"]
    finally:
        client.close()


@needs_mcp
def test_stray_output_from_handlers_and_children_never_reaches_stdout(seeded, data_dir):
    client = StdioClient(_env(data_dir), extra=STRAY)
    try:
        _initialize(client)
        out, is_error = client.call("openfoia_list_cases", {})
        assert not is_error and out["total"] == 2
        assert client.request("tools/list")["result"]["tools"]
    finally:
        err = client.close()
    stdout = "".join(client.raw)
    assert "STRAY" not in stdout
    for line in client.raw:
        json.loads(line)
    assert "STRAY" not in err  # the null device, not the client's stderr log
