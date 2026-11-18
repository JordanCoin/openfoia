"""Metadata stripping must report what really happened to each stored copy.

A file the stripper cannot clean used to get the same green check as one it
cleaned, the web UI upload never stripped at all while the README said it did,
and a PDF whose author sat only in its XMP packet was stored unchanged.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openfoia.pipeline import metadata as metadata_mod
from openfoia.pipeline.metadata import NOT_STRIPPED, describe, strip_metadata

# ---------------------------------------------------------------------------
# Fixtures: files that carry an identifying name
# ---------------------------------------------------------------------------

SOURCE = "Jane Source"


def _odt(path: Path, creator: str = SOURCE) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text")
        z.writestr(
            "content.xml",
            '<?xml version="1.0"?><office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body>'
            "<office:text><text:p>Memo</text:p></office:text></office:body>"
            "</office:document-content>",
        )
        z.writestr(
            "meta.xml",
            '<?xml version="1.0"?><office:document-meta '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><office:meta>'
            f"<dc:creator>{creator}</dc:creator></office:meta></office:document-meta>",
        )
        z.writestr(
            "META-INF/manifest.xml",
            '<?xml version="1.0"?><manifest:manifest '
            'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            '<manifest:file-entry manifest:full-path="/" '
            'manifest:media-type="application/vnd.oasis.opendocument.text"/>'
            "</manifest:manifest>",
        )
    return path


def _docx(path: Path) -> Path:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Body text")
    doc.core_properties.author = SOURCE
    doc.save(str(path))
    return path


def _xmp_only_pdf(path: Path) -> Path:
    """A PDF whose only metadata is an XMP packet: no /Info entries at all."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    stream = DecodedStreamObject()
    stream.set_data(
        b'<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:creator>Jane Source</dc:creator></rdf:Description></rdf:RDF>"
        b'</x:xmpmeta><?xpacket end="w"?>'
    )
    stream[NameObject("/Type")] = NameObject("/Metadata")
    stream[NameObject("/Subtype")] = NameObject("/XML")
    writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)
    info = writer._info.get_object()
    for key in list(info.keys()):
        del info[key]  # pypdf adds /Producer by itself
    with open(path, "wb") as f:
        writer.write(f)
    assert not PdfReader(str(path)).metadata
    assert SOURCE.encode() in path.read_bytes()
    return path


def _no_mat2(monkeypatch):
    monkeypatch.setattr(metadata_mod.shutil, "which", lambda name: None)


# ---------------------------------------------------------------------------
# Status of every built-in path
# ---------------------------------------------------------------------------


def test_pdf_with_author_only_in_xmp_is_cleaned(tmp_path):
    path = _xmp_only_pdf(tmp_path / "xmp.pdf")

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert "XMP" in result["stripped"]
    assert SOURCE.encode() not in path.read_bytes()


def test_docx_author_is_stripped_and_reported(tmp_path):
    path = _docx(tmp_path / "memo.docx")

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert describe(result) == (True, f"stripped {len(result['stripped'])} metadata fields")


def _has_pillow() -> bool:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


needs_pillow = pytest.mark.skipif(not _has_pillow(), reason="Pillow not installed")


@pytest.mark.parametrize(
    "name",
    [
        "broken.pdf",
        "broken.docx",
        pytest.param("broken.jpg", marks=needs_pillow),
    ],
)
def test_file_a_built_in_handler_cannot_open_is_a_failure_not_a_clean_file(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"not really that format")

    result = strip_metadata(path)

    assert result["status"] == "failed"
    clean, note = describe(result)
    assert not clean
    assert note.startswith("metadata NOT stripped: could not open the file as")


def _jpeg_with_exif(path: Path) -> Path:
    from PIL import Image

    img = Image.radial_gradient("L").convert("RGB")  # 256x256, big enough to truncate
    exif = Image.Exif()
    exif[0x013B] = SOURCE  # Artist
    img.save(path, "JPEG", exif=exif.tobytes())
    assert SOURCE.encode() in path.read_bytes()
    return path


@needs_pillow
def test_jpeg_exif_is_stripped(tmp_path):
    path = _jpeg_with_exif(tmp_path / "photo.jpg")

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert SOURCE.encode() not in path.read_bytes()
    assert describe(result)[0]


@needs_pillow
def test_truncated_image_is_a_failure_not_a_crash(tmp_path):
    """Image.open succeeds on a truncated JPEG; decoding it used to raise OSError."""
    path = _jpeg_with_exif(tmp_path / "cut.jpg")
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])

    result = strip_metadata(path)

    assert result["status"] == "failed"
    assert describe(result) == (False, "metadata NOT stripped: could not decode the image")
    assert list(tmp_path.iterdir()) == [path]


def test_image_without_pillow_is_reported_not_crashed(tmp_path, monkeypatch):
    """Pillow is optional. Without it an image is not cleaned, and the user is told how to fix it.

    This used to raise ModuleNotFoundError, which aborted the ingest of the file.
    """
    _no_mat2(monkeypatch)
    monkeypatch.setattr(metadata_mod, "_pillow_available", lambda: False)
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0 not decoded")

    result = strip_metadata(path)

    assert result["status"] == "unsupported"
    clean, note = describe(result)
    assert not clean
    assert "pip install pillow" in note
    assert "install-extras ocr" in note


def test_image_without_pillow_goes_to_mat2_when_installed(fake_mat2, monkeypatch):
    monkeypatch.setattr(metadata_mod, "_pillow_available", lambda: False)
    odt = fake_mat2("ok")
    path = odt.with_suffix(".jpg")
    odt.rename(path)

    result = strip_metadata(path)

    assert result["status"] == "stripped"
    assert result["backend"] == "mat2"


def test_plain_text_has_no_embedded_metadata(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("plain")

    result = strip_metadata(path)

    assert result["status"] == "none"
    assert describe(result) == (True, "plain text, no embedded metadata")


def test_format_nothing_handles_is_reported_with_the_remedy(tmp_path, monkeypatch):
    _no_mat2(monkeypatch)
    path = _odt(tmp_path / "memo.odt")
    before = path.read_bytes()

    result = strip_metadata(path)

    assert result["status"] == "unsupported"
    assert path.read_bytes() == before
    clean, note = describe(result)
    assert not clean
    assert "metadata NOT stripped: no metadata stripper for .odt" in note
    assert "install mat2" in note


def test_keep_metadata_is_never_described_as_clean():
    assert describe(None) == (False, "metadata kept as received (--keep-metadata)")
    assert describe({}) == describe(None)


def test_every_not_stripped_status_describes_as_not_clean():
    for status in NOT_STRIPPED:
        clean, _ = describe({"stripped": [], "status": status})
        assert not clean, status


# ---------------------------------------------------------------------------
# mat2: simulated, covering each behaviour observed in mat2 0.13.4
# ---------------------------------------------------------------------------

FAKE_MAT2 = textwrap.dedent(
    """\
    #!{python}
    import os, sys, time
    mode = os.environ["FAKE_MAT2_MODE"]
    args = sys.argv[1:]
    target = args[-1]
    done = target + ".cleaned"
    if mode == "timeout":
        time.sleep(5)
    if mode == "crash":
        sys.stderr.write("Traceback (most recent call last):\\n  ImportError: _gi\\n")
        sys.exit(1)
    if mode == "unsupported":
        print(f"[-] {{target}}'s format (application/msword) is not supported")
        sys.exit(0 if "--show" in args else 255)
    if "--inplace" in args:
        if mode == "inplace-fails":
            print(f"[-] something went wrong when cleaning {{target}}: ")
            sys.exit(255)
        open(done, "w").close()
        sys.exit(0)
    if os.path.exists(done) and mode != "leaves-some":
        print(f"  No metadata found in {{target}}.")
        sys.exit(0)
    print(f"[+] Metadata for {{target}}:")
    print("[++] Metadata for meta.xml:")
    print("      dc:creator: Jane Source")
    if not os.path.exists(done):
        print("      meta:generator: LibreOffice/7.6")
    """
)


@pytest.fixture
def fake_mat2(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "mat2"
    script.write_text(FAKE_MAT2.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setattr(metadata_mod.shutil, "which", lambda name: str(script))

    def use(mode: str) -> Path:
        monkeypatch.setenv("FAKE_MAT2_MODE", mode)
        return _odt(tmp_path / f"{mode}.odt")

    return use


def test_mat2_clean_is_verified_and_reported(fake_mat2):
    result = strip_metadata(fake_mat2("ok"))

    assert result["status"] == "stripped"
    assert result["backend"] == "mat2"
    assert result["stripped"] == ["dc:creator", "meta:generator"]
    assert "original_metadata_hash" in result
    assert describe(result) == (True, "stripped 2 metadata fields with mat2")


def test_mat2_unsupported_format_is_not_called_clean(fake_mat2):
    """mat2 --show exits 0 here; only its '[-]' line says it failed."""
    result = strip_metadata(fake_mat2("unsupported"))

    assert result["status"] == "unsupported"
    clean, note = describe(result)
    assert not clean
    assert note == "metadata NOT stripped: neither OpenFOIA nor mat2 can clean this format"


def test_broken_mat2_install_is_a_failure(fake_mat2):
    """A mat2 that crashes on import prints nothing on stdout; that is not 'clean'."""
    result = strip_metadata(fake_mat2("crash"))

    assert result["status"] == "failed"
    assert describe(result) == (False, "metadata NOT stripped: mat2 is installed but crashed")


def test_mat2_cleaning_failure_is_reported(fake_mat2):
    result = strip_metadata(fake_mat2("inplace-fails"))

    assert result["status"] == "failed"
    assert not describe(result)[0]


def test_metadata_mat2_left_behind_is_named(fake_mat2):
    result = strip_metadata(fake_mat2("leaves-some"))

    assert result["status"] == "partial"
    assert result["remaining"] == ["dc:creator"]
    assert describe(result) == (
        False,
        "metadata only partly stripped with mat2; still present: dc:creator",
    )


def test_mat2_timeout_is_a_failure(fake_mat2, monkeypatch):
    monkeypatch.setattr(metadata_mod, "MAT2_TIMEOUT_SECONDS", 0.5)

    result = strip_metadata(fake_mat2("timeout"))

    assert result == {
        "stripped": [],
        "file_type": "odt",
        "backend": "mat2",
        "status": "failed",
        "error": "mat2 timed out",
    }


def _working_mat2() -> str | None:
    mat2 = shutil.which("mat2")
    if not mat2:
        return None
    try:
        run = subprocess.run([mat2, "--version"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return mat2 if run.returncode == 0 else None


@pytest.mark.skipif(_working_mat2() is None, reason="no working mat2 on PATH")
def test_real_mat2_removes_the_creator_from_an_odt(tmp_path):
    path = _odt(tmp_path / "memo.odt")

    result = strip_metadata(path)

    assert result["status"] == "stripped", result
    assert result["backend"] == "mat2"
    assert "dc:creator" in result["stripped"]
    # mat2 drops meta.xml altogether; the name must be gone from every member.
    with zipfile.ZipFile(path) as z:
        assert all(SOURCE.encode() not in z.read(name) for name in z.namelist())


# ---------------------------------------------------------------------------
# Every surface reports it: CLI, web UI upload, agent
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(d))
    return d


def test_docs_ingest_flags_a_file_it_could_not_clean(tmp_path, data_dir, monkeypatch):
    from openfoia.cli import app

    _no_mat2(monkeypatch)
    folder = tmp_path / "in"
    folder.mkdir()
    _docx(folder / "clean.docx")
    _odt(folder / "dirty.odt")

    result = CliRunner().invoke(app, ["docs", "ingest", str(folder / "dirty.odt")])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "⚠ dirty.odt" in out
    assert "metadata NOT stripped: no metadata stripper for .odt" in out
    assert "Metadata: 0 clean, 1 still carrying metadata" in out
    assert "1 stored copy still carries the metadata it arrived with" in out
    assert "✓ dirty.odt" not in out


def test_docs_ingest_of_a_photo_without_pillow_still_ingests_and_warns(
    tmp_path, data_dir, monkeypatch
):
    from openfoia.cli import app

    _no_mat2(monkeypatch)
    monkeypatch.setattr(metadata_mod, "_pillow_available", lambda: False)
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0 not decoded")

    result = CliRunner().invoke(app, ["docs", "ingest", str(photo)])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "Ingested 1 document" in out
    assert "⚠ photo.jpg" in out
    assert "needs Pillow" in out
    assert "Metadata: 0 clean, 1 still carrying metadata" in out


def test_docs_ingest_directory_reports_each_file(tmp_path, data_dir, monkeypatch):
    from openfoia.cli import app

    _no_mat2(monkeypatch)
    folder = tmp_path / "in"
    folder.mkdir()
    _docx(folder / "memo.docx")
    (folder / "notes.txt").write_text("plain")

    result = CliRunner().invoke(app, ["docs", "ingest", str(folder)])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "✓ memo.docx" in out and "stripped" in out
    assert "✓ notes.txt" in out and "plain text, no embedded metadata" in out
    assert "Metadata: 2 clean, 0 still carrying metadata" in out


def test_docs_ingest_keep_metadata_says_so(tmp_path, data_dir):
    from openfoia.cli import app

    path = _docx(tmp_path / "memo.docx")

    result = CliRunner().invoke(app, ["docs", "ingest", str(path), "--keep-metadata"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "metadata kept as received (--keep-metadata)" in out
    assert "Metadata: kept as received (--keep-metadata)" in out


def _client(data_dir):
    from fastapi.testclient import TestClient

    from openfoia.db import ensure_db
    from openfoia.server import create_app

    ensure_db()
    _request("req-1")
    return TestClient(create_app(token="t", data_dir=data_dir), base_url="http://127.0.0.1")


def _request(request_id: str) -> None:
    """The upload endpoint attaches the document to an existing request."""
    from openfoia.db import get_session
    from openfoia.models import (
        Agency,
        AgencyLevel,
        DeliveryMethod,
        Request,
        RequestStatus,
        User,
    )

    with get_session() as session:
        user = User(id="user-1", email="local@openfoia.local", name="Local User")
        agency = Agency(id="agency-1", name="Test Agency", level=AgencyLevel.FEDERAL)
        session.add_all([user, agency])
        session.flush()
        session.add(
            Request(
                id=request_id,
                request_number="REQ-TEST-1",
                requester_id=user.id,
                agency_id=agency.id,
                subject="Test",
                body="Test",
                delivery_method=DeliveryMethod.EMAIL,
                status=RequestStatus.DRAFT,
            )
        )


def test_web_upload_strips_metadata_and_reports_it(tmp_path, data_dir):
    client = _client(data_dir)
    body = _docx(tmp_path / "memo.docx").read_bytes()
    assert SOURCE.encode() in zipfile.ZipFile(io.BytesIO(body)).read("docProps/core.xml")

    resp = client.post(
        "/api/documents/upload",
        params={"request_id": "req-1"},
        headers={"Authorization": "Bearer t"},
        files={"file": ("memo.docx", io.BytesIO(body), "application/octet-stream")},
    )

    assert resp.status_code == 200, resp.text
    meta = resp.json()["metadata"]
    assert meta["clean"] is True and meta["status"] == "stripped"
    stored = next((data_dir / "docs").rglob("*memo.docx"))
    assert SOURCE.encode() not in zipfile.ZipFile(stored).read("docProps/core.xml")


def test_web_upload_reports_a_file_it_could_not_clean(tmp_path, data_dir, monkeypatch):
    _no_mat2(monkeypatch)
    client = _client(data_dir)
    body = _odt(tmp_path / "memo.odt").read_bytes()

    resp = client.post(
        "/api/documents/upload",
        params={"request_id": "req-1"},
        headers={"Authorization": "Bearer t"},
        files={"file": ("memo.odt", io.BytesIO(body), "application/octet-stream")},
    )

    assert resp.status_code == 200, resp.text
    meta = resp.json()["metadata"]
    assert meta["clean"] is False and meta["status"] == "unsupported"
    assert meta["note"].startswith("metadata NOT stripped")
