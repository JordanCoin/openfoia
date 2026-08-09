"""Security regression tests: the LLM agent tool surface.

Threat model: the agent reads UNTRUSTED document text (extract_entities feeds
document content straight to the model). A prompt-injected document must not
be able to make the agent read arbitrary files off the journalist's disk.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "openfoia-data"
    d.mkdir()
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(d))
    return d


def _agent():
    from openfoia.agent import OpenFOIAAgent

    return OpenFOIAAgent(db_session=None, config={})


def _run(coro):
    return asyncio.run(coro)


def test_process_document_rejects_path_outside_data_dir(data_dir, tmp_path):
    """The classic prompt-injection target: read the user's SSH key."""
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")

    result = _run(_agent().execute_tool("process_document", {"document_path": str(secret)}))

    assert "error" in result
    assert "outside" in result["error"].lower() or "not allowed" in result["error"].lower()
    # Must not leak the resolved absolute path of the probed file back to the LLM.
    assert "BEGIN OPENSSH" not in str(result)


def test_process_document_rejects_traversal_escape(data_dir, tmp_path):
    """../ escapes out of the data dir must be caught after resolution."""
    secret = tmp_path / "outside.txt"
    secret.write_text("sensitive")

    sneaky = str(data_dir / ".." / "outside.txt")
    result = _run(_agent().execute_tool("process_document", {"document_path": sneaky}))

    assert "error" in result
    assert "outside" in result["error"].lower() or "not allowed" in result["error"].lower()


def test_process_document_rejects_symlink_escape(data_dir, tmp_path):
    """A symlink inside the data dir pointing out of it must not be followed."""
    secret = tmp_path / "secret.txt"
    secret.write_text("sensitive")
    link = data_dir / "innocent.pdf"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    result = _run(_agent().execute_tool("process_document", {"document_path": str(link)}))

    assert "error" in result
    assert "outside" in result["error"].lower() or "not allowed" in result["error"].lower()


def test_process_document_reports_missing_file_without_echoing_path(data_dir):
    """Non-existent paths inside the data dir are a normal error."""
    missing = data_dir / "nope.pdf"
    result = _run(_agent().execute_tool("process_document", {"document_path": str(missing)}))

    assert "error" in result
    assert "not found" in result["error"].lower()


def test_agent_tool_errors_are_not_raw_exception_text(data_dir):
    """Raw exception strings leak absolute paths / DB internals into the LLM."""
    agent = OpenFOIAAgentWithBrokenDB()
    result = _run(agent.execute_tool("list_requests", {}))

    assert "error" in result
    assert "Traceback" not in result["error"]
    assert "/home/" not in result["error"]


class OpenFOIAAgentWithBrokenDB:
    """Minimal harness: a db session whose query() blows up with a pathy message."""

    def __new__(cls):
        from openfoia.agent import OpenFOIAAgent

        class _BoomDB:
            def query(self, *a, **k):
                raise RuntimeError("no such table: /home/journalist/.openfoia/data.db is locked")

        return OpenFOIAAgent(db_session=_BoomDB(), config={})


def test_system_prompt_hardens_against_prompt_injection():
    """The agent must be told document content is data, never instructions."""
    from openfoia import agent as agent_mod

    prompt = getattr(agent_mod, "AGENT_SYSTEM_PROMPT", "")
    lowered = prompt.lower()

    assert "never" in lowered or "do not" in lowered
    assert "instruction" in lowered
    # Must explicitly frame document content as untrusted data.
    assert "untrusted" in lowered or "not instructions" in lowered
