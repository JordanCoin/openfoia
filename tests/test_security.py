"""Regression tests pinning graph-injection and offline-web-UI guarantees.

These are cheap, offline, and deliberately narrow: each one locks a specific
defect that must not come back. They complement the broader v4.0.0 security
suite (tests/test_security_injection.py, tests/test_security_network.py).

- C7: document text was spliced raw into the graph's <script> block, so a PDF
  containing `</script>` broke out and executed on `file://`. v4.0.0's
  escape_json_for_script escapes `<`, `>`, `&` and U+2028/U+2029.
- C6: the web UI must load nothing from a CDN, so merely opening it never
  phones a third party.
"""

from __future__ import annotations

import json
import re

import pytest

from openfoia.graph_template import escape_json_for_script, render
from openfoia.server import get_index_html

# A document body that breaks out of a <script> block and runs attacker JS.
PAYLOAD = "</script><script>window.PWNED=1</script>"


def _render_graph(tmp_path, doc_text):
    graph_data = {
        "nodes": [{"id": "e1", "label": "Acme Corp", "type": "organization"}],
        "edges": [],
        "documents": {
            "d1": {
                "id": "d1",
                "filename": "response.pdf",
                "page_count": 1,
                "text": doc_text,
                "request_id": "REQ-001",
                "source_url": None,
            }
        },
    }
    out = tmp_path / "graph.html"
    render(json.dumps(graph_data), out)
    return out.read_text()


class TestGraphScriptInjection:
    """C7 -- graph HTML must not let document text break out of <script>."""

    def test_payload_does_not_close_the_script_block(self, tmp_path):
        html = _render_graph(tmp_path, PAYLOAD)

        # The data block starts at `var graphData =` and the only `</script>`
        # after it must be the template's own closing tag.
        start = html.index("var graphData =")
        data_block = html[start : html.index("</script>", start)]

        assert "</script>" not in data_block
        # If the payload text appears, its opening `<` must be \u003c-escaped so
        # it cannot start or close a tag. v4.0.0 escapes both `<` and `>`.
        assert "window.PWNED" not in data_block or "\\u003c/script" in data_block
        # The literal breakout sequence must be neutralized wherever it appears.
        assert PAYLOAD not in html
        # No raw `<` survives in the embedded data itself -- every one is
        # \u003c-escaped, which also kills `<!--` and `<script` breakout
        # variants, not just `</`. Scope to the assignment line, not the
        # trailing JS (which legitimately contains `<` in loop conditions).
        data_line = html[start + len("var graphData =") : html.index("\n", start)]
        assert "<" not in data_line

    def test_payload_survives_a_json_round_trip(self, tmp_path):
        """Escaping must not corrupt the data -- `\\/` decodes back to `/`."""
        html = _render_graph(tmp_path, PAYLOAD)
        start = html.index("var graphData =")
        raw = html[start + len("var graphData =") : html.index("\n", start)].strip()
        raw = raw.rstrip(";")

        # `<\/` is a valid JSON escape, so this parses and yields the original.
        assert json.loads(raw)["documents"]["d1"]["text"] == PAYLOAD

    @pytest.mark.parametrize(
        "text",
        [
            PAYLOAD,
            "</SCRIPT ><img src=x onerror=alert(1)>",
            "comment breakout <!--<script>window.HACKED=1//-->",
            "bare <script>alert(1)</script> and <b>markup</b>",
            "quote \" and apostrophe ' and backslash \\",
            "line sep \u2028 and paragraph sep \u2029 mid-text",
            "Prince George's County v. O'Brien",
        ],
    )
    def test_hostile_and_awkward_text_round_trips(self, tmp_path, text):
        html = _render_graph(tmp_path, text)
        start = html.index("var graphData =")
        raw = html[start + len("var graphData =") : html.index("\n", start)].strip().rstrip(";")

        assert json.loads(raw)["documents"]["d1"]["text"] == text
        # Raw U+2028/U+2029 would be an illegal line terminator in older JS.
        assert "\u2028" not in raw
        assert "\u2029" not in raw

    def test_escape_helper_is_a_no_op_on_benign_json(self):
        benign = json.dumps({"text": "Department of Justice, 441 G Street NW"})
        assert escape_json_for_script(benign) == benign

    def test_graph_html_loads_nothing_from_the_network(self, tmp_path):
        html = _render_graph(tmp_path, "harmless text")
        assert not re.findall(r'(?:src|href)\s*=\s*"https?://', html)


class TestWebUIIsOffline:
    """C6 -- `openfoia serve` must make zero external requests."""

    def test_no_external_resources_are_loaded(self):
        html = get_index_html()
        loaded = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]*)"', html)
        # An <a href> the user must click is fine; a loaded resource is not.
        anchors = re.findall(r'<a\b[^>]*href="(https?://[^"]*)"', html)
        assert [u for u in loaded if u not in anchors] == []

    def test_no_cdn_script_tag(self):
        html = get_index_html()
        assert "cdn.tailwindcss.com" not in html
        assert not re.findall(r"<script[^>]*\ssrc\s*=", html)
