"""Automatic metadata stripping on document ingest.

Strips potentially sensitive metadata (GPS coordinates, author info,
timestamps, etc.) from ingested documents while preserving a SHA256
hash of the original metadata for chain-of-custody purposes.

Every result carries a ``status`` so callers can tell the user what really
happened to the stored copy, instead of showing the same success line for a
file that was cleaned and one that was not:

- ``stripped``: a handler ran; ``stripped`` lists the fields removed (it may
  be empty when the file had nothing to remove).
- ``none``: plain text, which has no embedded metadata.
- ``unsupported``: NOT stripped; nothing here handles the format.
- ``failed``: NOT stripped; a handler ran and could not clean the file.
- ``partial``: some metadata was removed but ``remaining`` is still present.

PDF, DOCX and common images are handled here. Other formats (OpenDocument,
XLSX, PPTX, EPUB, audio, video, HEIC, GIF, SVG and more) go to mat2 when it
is installed; its result is checked with a second ``mat2 --show`` rather than
trusted. Neither is a guarantee: mat2's own documentation says the same.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# File extensions grouped by handler
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif"}
PDF_EXTENSIONS = {".pdf"}
DOCX_EXTENSIONS = {".docx"}
#: Formats with no embedded metadata block to strip.
TEXT_EXTENSIONS = {".txt", ".text", ".md", ".csv", ".tsv"}

SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS | DOCX_EXTENSIONS

#: Statuses meaning the stored copy still carries the metadata it arrived with.
NOT_STRIPPED = frozenset({"unsupported", "failed", "partial"})

MAT2_TIMEOUT_SECONDS = 120
MAT2_HINT = "install mat2 to clean it (apt install mat2; preinstalled on Tails)"


def strip_metadata(file_path: Path | str, keep_hash: bool = True) -> dict[str, Any]:
    """Strip metadata from a file in-place.

    Detects file type by extension and removes sensitive metadata.
    Returns a dict describing what was stripped, suitable for an audit log.

    Args:
        file_path: Path to the file (modified in-place).
        keep_hash: If True, include a SHA256 hash of the original metadata
                   in the return dict for chain-of-custody tracking.

    Returns:
        A dict with keys:
            - ``stripped``: list of metadata field names that were removed
            - ``file_type``: detected file type (image/pdf/docx/unsupported)
            - ``original_metadata_hash``: SHA256 hex digest (only if keep_hash=True)
    """
    file_path = Path(file_path)
    suffix = file_path.suffix.lower()

    if suffix in IMAGE_EXTENSIONS:
        return _strip_image_metadata(file_path, keep_hash)
    if suffix in PDF_EXTENSIONS:
        return _strip_pdf_metadata(file_path, keep_hash)
    if suffix in DOCX_EXTENSIONS:
        return _strip_docx_metadata(file_path, keep_hash)
    if suffix in TEXT_EXTENSIONS:
        return {"stripped": [], "file_type": "text", "status": "none"}

    mat2 = shutil.which("mat2")
    if mat2:
        return _strip_with_mat2(mat2, file_path, keep_hash)
    return {
        "stripped": [],
        "file_type": "unsupported",
        "status": "unsupported",
        "error": f"no metadata stripper for {suffix or 'files without an extension'}",
    }


def _hash_metadata(metadata_dict: dict[str, Any]) -> str:
    """Produce a stable SHA256 hex digest of a metadata dictionary."""
    canonical = json.dumps(metadata_dict, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Image metadata (EXIF) — uses Pillow
# ---------------------------------------------------------------------------


def _strip_image_metadata(file_path: Path, keep_hash: bool) -> dict[str, Any]:
    from PIL import Image

    result: dict[str, Any] = {"stripped": [], "file_type": "image", "status": "stripped"}

    try:
        img = Image.open(file_path)
    except Exception as exc:
        logger.warning("Could not open image %s: %s", file_path, exc)
        return {**result, "status": "failed", "error": "could not open the file as an image"}

    # Collect original EXIF for hashing
    exif_data = {}
    info = img.info or {}

    if hasattr(img, "_getexif") and img._getexif():
        from PIL.ExifTags import TAGS

        raw_exif = img._getexif()
        for tag_id, value in raw_exif.items():
            tag_name = TAGS.get(tag_id, str(tag_id))
            try:
                exif_data[tag_name] = str(value)
            except Exception:
                exif_data[tag_name] = repr(value)

    if exif_data:
        result["stripped"] = list(exif_data.keys())

    if keep_hash and exif_data:
        result["original_metadata_hash"] = _hash_metadata(exif_data)

    # Re-save without EXIF by creating a clean copy
    # Preserve format and mode
    fmt = img.format or file_path.suffix.lstrip(".").upper()
    if fmt == "JPG":
        fmt = "JPEG"

    clean = Image.new(img.mode, img.size)
    clean.putdata(list(img.getdata()))

    save_kwargs: dict[str, Any] = {}
    if fmt == "JPEG":
        save_kwargs["quality"] = 95
    if fmt == "PNG" and "transparency" in info:
        save_kwargs["transparency"] = info["transparency"]

    clean.save(file_path, format=fmt, **save_kwargs)

    return result


# ---------------------------------------------------------------------------
# PDF metadata — uses pypdf
# ---------------------------------------------------------------------------

_PDF_SENSITIVE_KEYS = {
    "/Author",
    "/Creator",
    "/Producer",
    "/CreationDate",
    "/ModDate",
    "/Title",
    "/Subject",
    "/Keywords",
}


def _strip_pdf_metadata(file_path: Path, keep_hash: bool) -> dict[str, Any]:
    from pypdf import PdfReader, PdfWriter

    result: dict[str, Any] = {"stripped": [], "file_type": "pdf", "status": "stripped"}

    try:
        reader = PdfReader(str(file_path))
    except Exception as exc:
        logger.warning("Could not open PDF %s: %s", file_path, exc)
        return {**result, "status": "failed", "error": "could not open the file as a PDF"}

    # Collect original metadata
    original_meta: dict[str, str] = {}
    if reader.metadata:
        for key in reader.metadata:
            try:
                original_meta[key] = str(reader.metadata[key])
            except Exception:
                original_meta[key] = ""

    stripped_keys = [k for k in original_meta if k in _PDF_SENSITIVE_KEYS]
    # The XMP packet duplicates author, tool and timestamps (sometimes GPS), and
    # some producers write it with no /Info entries at all.
    if _pdf_has_xmp(reader):
        stripped_keys.append("XMP")
    result["stripped"] = stripped_keys

    if keep_hash and original_meta:
        result["original_metadata_hash"] = _hash_metadata(original_meta)

    if not stripped_keys:
        return result

    # Rewrite PDF without sensitive metadata
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)

    # Copy metadata, omitting sensitive fields
    if reader.metadata:
        kept = {}
        for key, value in original_meta.items():
            if key not in _PDF_SENSITIVE_KEYS:
                kept[key] = value
        if kept:
            writer.add_metadata(kept)

    # Drop the XMP packet. Modern PDFs carry an /Metadata stream that
    # duplicates Author/Creator/timestamps (and sometimes GPS); clearing only
    # the /Info dictionary left all of it readable in the shipped file.
    _remove_xmp_metadata(writer)

    with open(file_path, "wb") as f:
        writer.write(f)

    return result


def _pdf_has_xmp(reader: Any) -> bool:
    """Whether the document catalog or any page carries an XMP /Metadata stream."""
    try:
        if "/Metadata" in reader.trailer["/Root"]:
            return True
        return any("/Metadata" in page for page in reader.pages)
    except Exception:  # pragma: no cover - a malformed catalog is treated as "has XMP"
        return True


def _remove_xmp_metadata(writer: Any) -> None:
    """Remove the document-level XMP metadata stream from a PdfWriter."""
    from pypdf.generic import NameObject

    try:
        root = writer._root_object
        if NameObject("/Metadata") in root:
            del root[NameObject("/Metadata")]
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not remove XMP metadata: %s", exc)

    # Page-level XMP is rare but possible.
    try:
        for page in writer.pages:
            if NameObject("/Metadata") in page:
                del page[NameObject("/Metadata")]
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not remove page XMP metadata: %s", exc)


# ---------------------------------------------------------------------------
# DOCX metadata — uses python-docx
# ---------------------------------------------------------------------------

_DOCX_SENSITIVE_ATTRS = [
    "author",
    "last_modified_by",
    "company",
    "manager",
    "revision",
    "comments",
    "created",
    "modified",
]


def _strip_docx_metadata(file_path: Path, keep_hash: bool) -> dict[str, Any]:
    from docx import Document

    result: dict[str, Any] = {"stripped": [], "file_type": "docx", "status": "stripped"}

    try:
        doc = Document(str(file_path))
    except Exception as exc:
        logger.warning("Could not open DOCX %s: %s", file_path, exc)
        return {**result, "status": "failed", "error": "could not open the file as a DOCX"}

    core = doc.core_properties
    original_meta: dict[str, str] = {}

    for attr in _DOCX_SENSITIVE_ATTRS:
        value = getattr(core, attr, None)
        if value is not None:
            original_meta[attr] = str(value)

    stripped_keys = [k for k, v in original_meta.items() if v and v != "None"]
    result["stripped"] = stripped_keys

    if keep_hash and original_meta:
        result["original_metadata_hash"] = _hash_metadata(original_meta)

    if not stripped_keys:
        return result

    # Clear sensitive properties
    for attr in _DOCX_SENSITIVE_ATTRS:
        try:
            if attr in ("created", "modified") or attr == "revision":
                setattr(core, attr, None)
            else:
                setattr(core, attr, "")
        except Exception:
            pass  # Some read-only props may fail

    doc.save(str(file_path))

    # company/manager are extended properties in docProps/app.xml, which
    # python-docx cannot reach. Without this the module reported them as
    # stripped while leaving them in the shipped file.
    extended = _strip_docx_extended_props(file_path)
    if extended is None:
        result["status"] = "partial"
        result["remaining"] = ["company", "manager", "custom_properties"]
        result["error"] = "could not rewrite docProps/app.xml"
        return result
    for key in extended:
        if key not in result["stripped"]:
            result["stripped"].append(key)

    return result


#: Extended/custom properties (docProps/app.xml) that identify the author's
#: organization. Not exposed by python-docx's core_properties.
_DOCX_EXTENDED_TAGS = ("Company", "Manager")


def _strip_docx_extended_props(file_path: Path) -> list[str] | None:
    """Blank out Company/Manager in docProps/app.xml and drop custom.xml.

    A .docx is a zip, so this rewrites the archive in place. Returns the list
    of property names that were actually present and removed, or None when the
    archive could not be read or rewritten (those properties are still there).
    """
    import os
    import re as _re
    import shutil
    import tempfile
    import zipfile

    removed: list[str] = []

    try:
        with zipfile.ZipFile(file_path) as src:
            names = src.namelist()
            members = {name: src.read(name) for name in names}
    except Exception as exc:
        logger.warning("Could not open DOCX archive %s: %s", file_path, exc)
        return None

    app_xml = members.get("docProps/app.xml")
    if app_xml is not None:
        text = app_xml.decode("utf-8", "replace")
        for tag in _DOCX_EXTENDED_TAGS:
            pattern = _re.compile(rf"<{tag}>(.*?)</{tag}>", _re.DOTALL)
            match = pattern.search(text)
            if match and match.group(1).strip():
                removed.append(tag.lower())
            text = pattern.sub(f"<{tag}></{tag}>", text)
        members["docProps/app.xml"] = text.encode("utf-8")

    # Custom properties are arbitrary user-defined fields — drop entirely,
    # along with the content-type override and relationship that point at it,
    # so the resulting archive is still a valid .docx.
    if "docProps/custom.xml" in members:
        del members["docProps/custom.xml"]
        removed.append("custom_properties")

        ct = members.get("[Content_Types].xml")
        if ct is not None:
            text = ct.decode("utf-8", "replace")
            text = _re.sub(r"<Override[^>]*custom\.xml[^>]*/>", "", text)
            members["[Content_Types].xml"] = text.encode("utf-8")

        rels = members.get("_rels/.rels")
        if rels is not None:
            text = rels.decode("utf-8", "replace")
            text = _re.sub(r"<Relationship[^>]*custom\.xml[^>]*/>", "", text)
            members["_rels/.rels"] = text.encode("utf-8")

    # Securely created in the same directory as the target: mktemp() is a
    # TOCTOU race (an attacker can win the name and plant a symlink), and this
    # function rewrites sensitive documents.
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=str(Path(file_path).parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for name, data in members.items():
                dst.writestr(name, data)
        shutil.move(tmp, file_path)
    except Exception as exc:
        logger.warning("Could not rewrite DOCX archive %s: %s", file_path, exc)
        if Path(tmp).exists():
            Path(tmp).unlink()
        return None

    return removed


# ---------------------------------------------------------------------------
# Everything else — mat2, when installed
# ---------------------------------------------------------------------------


def _run_mat2(mat2: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [mat2, *args],
        capture_output=True,
        text=True,
        timeout=MAT2_TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _parse_mat2_show(proc: subprocess.CompletedProcess[str]) -> dict[str, str] | None:
    """Fields listed by ``mat2 --show`` as {"member: key": value}, or None on failure.

    ``mat2 --show`` exits 0 even when it cannot read the file (it prints a line
    starting with ``[-]``), and a broken mat2 install crashes with empty stdout.
    Only output that either lists fields or says "No metadata found" counts.
    """
    if proc.returncode != 0:
        return None
    fields: dict[str, str] = {}
    member = ""
    understood = False
    for line in proc.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("[-]"):
            return None
        if text.startswith("No metadata found"):
            understood = True
            continue
        if text.startswith("[++] Metadata for "):
            member = text[len("[++] Metadata for ") :].rstrip(":")
            continue
        if text.startswith("[+]"):
            understood = True
            continue
        key, sep, value = text.partition(": ")
        if sep:
            fields[f"{member}: {key}" if member else key] = value
    return fields if (fields or understood) else None


def _field_names(fields: dict[str, str]) -> list[str]:
    """Field names without the archive member prefix, first occurrence order."""
    names: list[str] = []
    for key in fields:
        name = key.rsplit(": ", 1)[-1]
        if name not in names:
            names.append(name)
    return names


def _mat2_refusal(proc: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    """(status, error) for a mat2 run that did not clean the file."""
    output = f"{proc.stdout}\n{proc.stderr}"
    if "is not supported" in output:
        return "unsupported", "neither OpenFOIA nor mat2 can clean this format"
    if "Traceback" in output:
        return "failed", "mat2 is installed but crashed"
    return "failed", f"mat2 could not clean the file (exit status {proc.returncode})"


def _strip_with_mat2(mat2: str, file_path: Path, keep_hash: bool) -> dict[str, Any]:
    suffix = file_path.suffix.lower().lstrip(".") or "unknown"
    result: dict[str, Any] = {"stripped": [], "file_type": suffix, "backend": "mat2"}
    target = str(file_path)
    try:
        before = _run_mat2(mat2, "--show", "--", target)
        fields = _parse_mat2_show(before)
        if fields is None:
            status, error = _mat2_refusal(before)
            return {**result, "status": status, "error": error}
        if not fields:
            return {**result, "status": "stripped"}

        cleaned = _run_mat2(mat2, "--inplace", "--", target)
        if cleaned.returncode != 0:
            status, error = _mat2_refusal(cleaned)
            return {**result, "status": status, "error": error}
        remaining = _parse_mat2_show(_run_mat2(mat2, "--show", "--", target))
    except subprocess.TimeoutExpired:
        return {**result, "status": "failed", "error": "mat2 timed out"}
    except OSError as exc:
        return {**result, "status": "failed", "error": f"mat2 did not run ({type(exc).__name__})"}

    result["stripped"] = _field_names(fields)
    if keep_hash:
        result["original_metadata_hash"] = _hash_metadata(fields)
    if remaining is None:
        result["status"] = "partial"
        result["remaining"] = []
        result["error"] = "mat2 cleaned the file but could not re-read it to confirm"
    elif remaining:
        result["status"] = "partial"
        result["remaining"] = _field_names(remaining)
    else:
        result["status"] = "stripped"
    return result


def describe(info: dict[str, Any] | None) -> tuple[bool, str]:
    """(stored copy is clean, one-line summary) for a strip_metadata result.

    None or {} means stripping was skipped on request (--keep-metadata).
    """
    if not info:
        return False, "metadata kept as received (--keep-metadata)"
    status = info.get("status", "stripped")
    via = " with mat2" if info.get("backend") == "mat2" else ""
    if status == "stripped":
        n = len(info.get("stripped", []))
        if not n:
            return True, f"no metadata found{via}"
        return True, f"stripped {n} metadata field{'s' if n != 1 else ''}{via}"
    if status == "none":
        return True, "plain text, no embedded metadata"
    if status == "partial":
        left = ", ".join(info.get("remaining", [])) or "unknown fields"
        return False, f"metadata only partly stripped{via}; still present: {left}"
    error = info.get("error", "not stripped")
    if status == "unsupported" and "mat2" not in error:
        error = f"{error}; {MAT2_HINT}"
    return False, f"metadata NOT stripped: {error}"
