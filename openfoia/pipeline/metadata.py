"""Automatic metadata stripping on document ingest.

Strips potentially sensitive metadata (GPS coordinates, author info,
timestamps, etc.) from ingested documents while preserving a SHA256
hash of the original metadata for chain-of-custody purposes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# File extensions grouped by handler
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif"}
PDF_EXTENSIONS = {".pdf"}
DOCX_EXTENSIONS = {".docx"}

SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS | DOCX_EXTENSIONS


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
    elif suffix in PDF_EXTENSIONS:
        return _strip_pdf_metadata(file_path, keep_hash)
    elif suffix in DOCX_EXTENSIONS:
        return _strip_docx_metadata(file_path, keep_hash)
    else:
        return {"stripped": [], "file_type": "unsupported"}


def _hash_metadata(metadata_dict: dict[str, Any]) -> str:
    """Produce a stable SHA256 hex digest of a metadata dictionary."""
    canonical = json.dumps(metadata_dict, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Image metadata (EXIF) — uses Pillow
# ---------------------------------------------------------------------------


def _strip_image_metadata(file_path: Path, keep_hash: bool) -> dict[str, Any]:
    from PIL import Image

    result: dict[str, Any] = {"stripped": [], "file_type": "image"}

    try:
        img = Image.open(file_path)
    except Exception as exc:
        logger.warning("Could not open image %s: %s", file_path, exc)
        return result

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

    result: dict[str, Any] = {"stripped": [], "file_type": "pdf"}

    try:
        reader = PdfReader(str(file_path))
    except Exception as exc:
        logger.warning("Could not open PDF %s: %s", file_path, exc)
        return result

    # Collect original metadata
    original_meta: dict[str, str] = {}
    if reader.metadata:
        for key in reader.metadata:
            try:
                original_meta[key] = str(reader.metadata[key])
            except Exception:
                original_meta[key] = ""

    stripped_keys = [k for k in original_meta if k in _PDF_SENSITIVE_KEYS]
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

    result: dict[str, Any] = {"stripped": [], "file_type": "docx"}

    try:
        doc = Document(str(file_path))
    except Exception as exc:
        logger.warning("Could not open DOCX %s: %s", file_path, exc)
        return result

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
            if attr in ("created", "modified"):
                setattr(core, attr, None)
            elif attr == "revision":
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
    for key in extended:
        if key not in result["stripped"]:
            result["stripped"].append(key)

    return result


#: Extended/custom properties (docProps/app.xml) that identify the author's
#: organization. Not exposed by python-docx's core_properties.
_DOCX_EXTENDED_TAGS = ("Company", "Manager")


def _strip_docx_extended_props(file_path: Path) -> list[str]:
    """Blank out Company/Manager in docProps/app.xml and drop custom.xml.

    A .docx is a zip, so this rewrites the archive in place. Returns the list
    of property names that were actually present and removed.
    """
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
        return removed

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

    tmp = tempfile.mktemp(suffix=".docx")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for name, data in members.items():
                dst.writestr(name, data)
        shutil.move(tmp, file_path)
    except Exception as exc:
        logger.warning("Could not rewrite DOCX archive %s: %s", file_path, exc)
        if Path(tmp).exists():
            Path(tmp).unlink()
        return []

    return removed
