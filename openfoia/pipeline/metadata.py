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
import re
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
PILLOW_HINT = (
    "image metadata stripping needs Pillow, which is not installed "
    "(pip install pillow, or openfoia install-extras ocr)"
)


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
        # Pillow is optional (the ocr extra brings it). Without it, hand the image
        # to mat2 if that is installed; otherwise report it instead of crashing
        # the whole ingest on the import.
        if not _pillow_available():
            mat2 = shutil.which("mat2")
            if mat2:
                return _strip_with_mat2(mat2, file_path, keep_hash)
            return {
                "stripped": [],
                "file_type": "image",
                "status": "unsupported",
                "error": PILLOW_HINT,
            }
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

#: Largest image (width x height) cleaned. Checked from the header, before any
#: pixel is decoded: a 200 KiB PNG can declare 8000x8000 pixels, and cleaning
#: holds the decoded image twice (Pillow keeps RGB at 4 bytes a pixel, so about
#: 400 MB at the cap). 50 megapixels covers common phone and camera photos; the
#: largest sensors (108 and 200 MP phone modes) are refused and reported.
MAX_IMAGE_PIXELS = 50_000_000

#: Image.info keys that describe how the pixels are encoded, not who made them.
_IMAGE_TECHNICAL_INFO = frozenset(
    {
        "adobe", "adobe_transform", "aspect", "background", "chromaticity",
        "compression", "dpi", "duration", "gamma", "interlace", "jfif",
        "jfif_density", "jfif_unit", "jfif_version", "loop", "progression",
        "progressive", "resolution", "srgb", "transparency",
    }
)  # fmt: skip


def _too_large(width: int, height: int) -> dict[str, str]:
    return {
        "status": "failed",
        "error": (
            f"image too large to clean safely ({width}x{height} pixels, over "
            f"{MAX_IMAGE_PIXELS // 1_000_000} megapixels)"
        ),
    }


def _strip_image_metadata(file_path: Path, keep_hash: bool) -> dict[str, Any]:
    from PIL import Image

    result: dict[str, Any] = {"stripped": [], "file_type": "image", "status": "stripped"}

    try:
        img = Image.open(file_path)
    except Image.DecompressionBombError:
        return {**result, "status": "failed", "error": "image too large to clean safely"}
    except Exception as exc:
        logger.warning("Could not open image %s: %s", file_path, exc)
        return {**result, "status": "failed", "error": "could not open the file as an image"}

    with img:
        # Image.open reads only the header. Refuse a huge canvas before a single
        # pixel is decoded.
        width, height = img.size
        if width * height > MAX_IMAGE_PIXELS:
            return {**result, **_too_large(width, height)}

        # Collect original EXIF for hashing
        exif_data = {}
        info = dict(img.info or {})

        if hasattr(img, "_getexif") and img._getexif():
            from PIL.ExifTags import TAGS

            raw_exif = img._getexif()
            for tag_id, value in raw_exif.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                try:
                    exif_data[tag_name] = str(value)
                except Exception:
                    exif_data[tag_name] = repr(value)

        stripped = list(exif_data.keys())
        # Everything else in info that is not about the encoding goes too: XMP,
        # ICC profile, JPEG comment, PNG text chunks, Photoshop/IPTC blocks.
        for key in info:
            name = str(key)
            if key != "exif" and name not in _IMAGE_TECHNICAL_INFO and name not in stripped:
                stripped.append(name)
        if "exif" in info and not exif_data:
            stripped.append("exif")
        result["stripped"] = stripped

        if keep_hash and exif_data:
            result["original_metadata_hash"] = _hash_metadata(exif_data)

        fmt = img.format or file_path.suffix.lstrip(".").upper()
        if fmt == "JPG":
            fmt = "JPEG"

        # Image.open is lazy: a truncated or corrupt file opens fine and only
        # fails here, when the pixels are decoded. copy() duplicates the decoded
        # pixels in C (no per-pixel Python objects) into a plain Image: it does
        # not carry the TIFF tag directory, which Pillow re-writes on save for
        # an opened TIFF (XMP, IPTC, Photoshop blocks). Its info dict, which
        # Pillow's savers also fall back to (JPEG comment, PNG ICC profile), is
        # emptied.
        try:
            img.load()
            clean = img.copy()
        except Exception as exc:
            logger.warning("Could not decode image %s: %s", file_path, exc)
            return {**result, "status": "failed", "error": "could not decode the image"}
    clean.info = {}

    save_kwargs: dict[str, Any] = {}
    if fmt == "JPEG":
        save_kwargs["quality"] = 95
    if fmt == "PNG" and "transparency" in info:
        save_kwargs["transparency"] = info["transparency"]

    # Write beside the file and swap it in, so a failed save cannot leave a
    # half-written copy behind.
    tmp = file_path.with_name(file_path.name + ".clean")
    try:
        clean.save(tmp, format=fmt, **save_kwargs)
        tmp.replace(file_path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        logger.warning("Could not save cleaned image %s: %s", file_path, exc)
        return {**result, "status": "failed", "error": "could not save the cleaned image"}
    finally:
        clean.close()

    return result


# ---------------------------------------------------------------------------
# Embedded JPEG and PNG data (inside PDFs and DOCX files) — byte level
# ---------------------------------------------------------------------------

#: JPEG APPn segments kept, by marker and signature: the JFIF header and the
#: Adobe colour-transform flag, which decoders need. Every other APPn segment
#: (EXIF and GPS, XMP, ICC profile, IPTC/Photoshop, maker data) and every
#: comment is dropped.
_JPEG_KEEP_APP = {0xE0: b"JFIF\x00", 0xEE: b"Adobe"}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: PNG chunks kept: pixels, palette, transparency, colour and animation
#: control. Text (tEXt, zTXt, iTXt), eXIf, tIME, ICC profiles and private
#: chunks are dropped.
_PNG_KEEP = frozenset(
    {
        b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB",
        b"sBIT", b"bKGD", b"hIST", b"pHYs", b"acTL", b"fcTL", b"fdAT", b"cICP",
    }
)  # fmt: skip


def _strip_jpeg_bytes(data: bytes) -> tuple[bytes, list[str]] | None:
    """(JPEG without metadata segments, names of what was dropped), or None.

    Works on the marker segments without decoding or re-encoding the image.
    Anything after the end-of-image marker (phone maker trailers, appended
    previews or videos) is dropped too. None when the data is not a JPEG this
    can walk to its end; the caller must then report it as not checked.
    """
    n = len(data)
    if data[:2] != b"\xff\xd8":
        return None
    out = bytearray(b"\xff\xd8")
    removed: list[str] = []
    i = 2
    while True:
        if i >= n or data[i] != 0xFF:
            return None
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker == 0xD9:  # end of image
            out += b"\xff\xd9"
            if data[i:].strip(b"\x00\r\n \t"):
                removed.append("data after the end of the image")
            return bytes(out), removed
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # no length field
            out += bytes((0xFF, marker))
            continue
        if i + 2 > n:
            return None
        length = int.from_bytes(data[i : i + 2], "big")
        if length < 2 or i + length > n:
            return None
        segment = data[i + 2 : i + length]
        if 0xE0 <= marker <= 0xEF:
            signature = _JPEG_KEEP_APP.get(marker)
            keep = signature is not None and segment.startswith(signature)
            if not keep:
                removed.append(f"APP{marker - 0xE0}")
        elif marker == 0xFE:
            keep = False
            removed.append("comment")
        else:
            keep = True
        if keep:
            out += bytes((0xFF, marker)) + data[i : i + length]
        i += length
        if marker == 0xDA:  # start of scan: entropy-coded data up to the next marker
            j = i
            while True:
                j = data.find(b"\xff", j)
                if j < 0 or j + 1 >= n:
                    return None
                following = data[j + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7:
                    j += 2  # stuffed byte or restart marker: still scan data
                elif following == 0xFF:
                    j += 1  # fill byte before a marker
                else:
                    break
            out += data[i:j]
            i = j


def _strip_png_bytes(data: bytes) -> tuple[bytes, list[str]] | None:
    """(PNG without metadata chunks, names of what was dropped), or None."""
    n = len(data)
    if not data.startswith(_PNG_SIGNATURE):
        return None
    out = bytearray(_PNG_SIGNATURE)
    removed: list[str] = []
    i = len(_PNG_SIGNATURE)
    while i + 12 <= n:
        length = int.from_bytes(data[i : i + 4], "big")
        kind = data[i + 4 : i + 8]
        end = i + 12 + length
        if end > n:
            return None
        if kind in _PNG_KEEP:
            out += data[i:end]
        else:
            removed.append(kind.decode("latin-1"))
        i = end
        if kind == b"IEND":
            if data[i:]:
                removed.append("data after the end of the image")
            return bytes(out), removed
    return None


# ---------------------------------------------------------------------------
# PDF metadata — uses pypdf
# ---------------------------------------------------------------------------

#: Keys removed wherever they appear in the rewritten file, with the name
#: reported for them. /Metadata is an XMP packet (document, page, image, font
#: or form level); /PieceInfo is private application data with its own dates;
#: /Thumb is a stored page thumbnail that can show an earlier version.
_PDF_DROP_KEYS = {
    "/Metadata": "XMP",
    "/PieceInfo": "application private data (/PieceInfo)",
    "/LastModified": "modification date (/LastModified)",
    "/Thumb": "page thumbnail",
}

#: Annotation subtypes that carry a whole file or media clip, which is not
#: cleaned here.
_PDF_EMBEDDING_ANNOTS = frozenset(
    {"/FileAttachment", "/Sound", "/Movie", "/RichMedia", "/3D", "/Screen"}
)


def _strip_pdf_metadata(file_path: Path, keep_hash: bool) -> dict[str, Any]:
    """Rewrite the PDF from its pages with no document information at all.

    The whole /Info dictionary goes (custom keys included), as do XMP packets
    at every level, annotation authors and dates, application private data,
    page thumbnails and EXIF/XMP inside embedded JPEG images. The document
    catalog is not copied, so outlines, document JavaScript, form definitions
    and attachments listed there are dropped; earlier revisions kept by
    incremental saves are not written. What the format can still carry and
    this cannot clean (attached files, JPEG 2000 images) makes the result
    "partial" and is named in ``remaining``.
    """
    from pypdf import PdfReader, PdfWriter

    result: dict[str, Any] = {"stripped": [], "file_type": "pdf", "status": "stripped"}

    try:
        reader = PdfReader(str(file_path))
        if reader.is_encrypted and not reader.decrypt(""):
            return {**result, "status": "failed", "error": "the PDF is password protected"}
        len(reader.pages)
    except Exception as exc:
        logger.warning("Could not open PDF %s: %s", file_path, exc)
        return {**result, "status": "failed", "error": "could not open the file as a PDF"}

    original_meta: dict[str, str] = {}
    try:
        info = reader.metadata or {}
        for key in info:
            try:
                original_meta[str(key)] = str(info[key])
            except Exception:
                original_meta[str(key)] = ""
    except Exception:  # a malformed /Info is dropped all the same
        original_meta["/Info"] = "unreadable"

    stripped: list[str] = list(original_meta)
    remaining: list[str] = []
    # The catalog is not copied; say what of it carried metadata or files.
    try:
        catalog = reader.trailer["/Root"]
        if "/Metadata" in catalog:
            stripped.append("XMP")
        names = catalog.get("/Names")
        if names is not None and "/EmbeddedFiles" in names.get_object():
            stripped.append("document-level attached files")
    except Exception:
        pass

    tmp = file_path.with_name(file_path.name + ".clean")
    try:
        writer = PdfWriter()
        writer.append_pages_from_reader(reader)
        writer.metadata = None  # no /Info at all, not even pypdf's /Producer
        _scrub_pdf_objects(writer, stripped, remaining)
        with open(tmp, "wb") as f:
            writer.write(f)
        tmp.replace(file_path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        logger.warning("Could not rewrite PDF %s: %s", file_path, exc)
        return {**result, "status": "failed", "error": "could not rewrite the PDF"}

    result["stripped"] = stripped
    if keep_hash and original_meta:
        result["original_metadata_hash"] = _hash_metadata(original_meta)
    if remaining:
        result["status"] = "partial"
        result["remaining"] = remaining
    return result


def _note(names: list[str], name: str) -> None:
    if name not in names:
        names.append(name)


def _scrub_pdf_objects(writer: Any, stripped: list[str], remaining: list[str]) -> None:
    """Remove metadata from every object the writer is about to write."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    def visit(obj: Any) -> None:
        if isinstance(obj, IndirectObject):
            return  # indirect objects are visited from writer._objects
        if isinstance(obj, ArrayObject):
            for item in obj:
                visit(item)
            return
        if not isinstance(obj, DictionaryObject):
            return
        _scrub_pdf_dict(obj, stripped, remaining)
        for value in list(obj.values()):
            visit(value)

    for obj in list(writer._objects):
        if obj is not None:
            visit(obj)
    _drop_unreachable_pdf_objects(writer)


def _drop_unreachable_pdf_objects(writer: Any) -> None:
    """Free every object no longer reachable from the catalog.

    pypdf writes all objects it holds, referenced or not, so a removed XMP
    packet or /PieceInfo dictionary would otherwise still be in the file.
    """
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    reachable: set[int] = set()
    stack: list[Any] = [writer._root_object.indirect_reference]
    while stack:
        obj = stack.pop()
        if isinstance(obj, IndirectObject):
            if obj.pdf is not writer or obj.idnum in reachable:
                continue
            reachable.add(obj.idnum)
            if 0 < obj.idnum <= len(writer._objects):
                stack.append(writer._objects[obj.idnum - 1])
        elif isinstance(obj, DictionaryObject):
            stack.extend(obj.values())
        elif isinstance(obj, ArrayObject):
            stack.extend(obj)
    for idnum in range(1, len(writer._objects) + 1):
        if idnum not in reachable:
            writer._objects[idnum - 1] = None


def _scrub_pdf_dict(d: Any, stripped: list[str], remaining: list[str]) -> None:
    from pypdf.generic import ArrayObject, NameObject, StreamObject

    for key, label in _PDF_DROP_KEYS.items():
        if key in d:
            del d[NameObject(key)]
            _note(stripped, label)

    subtype = d.get("/Subtype")
    is_annotation = d.get("/Type") == "/Annot" or (subtype is not None and "/Rect" in d)
    if is_annotation:
        # On a form field /T is the field's name; on every other annotation
        # (comments, highlights, their pop-ups) it is the reviewer's name.
        if subtype != "/Widget" and "/FT" not in d:
            for key, label in (
                ("/T", "annotation author (/T)"),
                ("/CreationDate", "annotation creation date"),
            ):
                if key in d:
                    del d[NameObject(key)]
                    _note(stripped, label)
        if "/M" in d:
            del d[NameObject("/M")]
            _note(stripped, "annotation modification date")
        if subtype in _PDF_EMBEDDING_ANNOTS:
            _note(remaining, f"{subtype[1:]} annotation (attached file or media not cleaned)")

    if d.get("/Type") == "/EmbeddedFile" or "/EF" in d:
        _note(remaining, "attached file (not cleaned)")

    if subtype == "/Image" and isinstance(d, StreamObject):
        raw_filter = d.get("/Filter")
        if isinstance(raw_filter, ArrayObject):
            filters = [str(f) for f in raw_filter]
        else:
            filters = [str(raw_filter)] if raw_filter is not None else []
        if filters == ["/DCTDecode"]:
            cleaned = _strip_jpeg_bytes(bytes(d._data))
            if cleaned is None:
                _note(remaining, "embedded JPEG image (could not be checked)")
            elif cleaned[1]:
                d._data = cleaned[0]
                _note(stripped, "EXIF/XMP in embedded JPEG images")
        elif "/JPXDecode" in filters:
            _note(remaining, "embedded JPEG 2000 image (metadata boxes not checked)")
        elif "/DCTDecode" in filters:
            _note(remaining, "embedded JPEG image under a second filter (not checked)")


# ---------------------------------------------------------------------------
# DOCX metadata — the zip is rewritten member by member
# ---------------------------------------------------------------------------

#: Limits checked from the zip's central directory before any member is
#: decompressed. A 1 MB .docx can declare a 1 GiB part: reading it whole drove
#: a 3 GiB peak before these existed. Python's zipfile never returns more than
#: a member's declared size, so the directory figures bound what is read.
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_MEMBER_BYTES = 128 * 2**20
MAX_ZIP_TOTAL_BYTES = 1024 * 2**20
#: A member larger than 1 MiB that expands more than this many times its
#: compressed size is treated as a zip bomb. Word's own XML is 5-20x.
MAX_ZIP_RATIO = 200
_ZIP_RATIO_FLOOR = 2**20

_W_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "http://purl.oclc.org/ooxml/wordprocessingml/main",
    }
)
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

#: Parts removed whole: user-defined properties, the first-page thumbnail
#: (which can show an earlier draft), SharePoint/data-store items, and the
#: list of reviewers with their account ids.
_DOCX_DROP_PREFIXES = ("customxml/",)
_DOCX_DROP_PARTS = {
    "docprops/custom.xml": "custom_properties",
    "word/people.xml": "reviewer accounts (people.xml)",
}

#: docProps/core.xml element -> name reported (names kept from earlier versions).
_DOCX_CORE_NAMES = {
    "creator": "author",
    "lastModifiedBy": "last_modified_by",
    "description": "comments",
    "lastPrinted": "last_printed",
    "contentStatus": "content_status",
}

#: docProps/app.xml elements reported when they had a value. All are removed.
_DOCX_APP_REPORTED = ("Company", "Manager", "Template", "HyperlinkBase", "TotalTime")

#: WordprocessingML elements removed whole: revision-session ids, the path of
#: the attached template (often C:\Users\<name>\...), document variables and
#: document ids.
_DOCX_DROP_ELEMENTS = {
    "rsids": "rsid",
    "attachedTemplate": "attached template path",
    "docVars": "document variables",
    "docId": "document id",
}

_PLURAL = {"initials": "initials", "date": "dates"}

_LOCAL_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|file:|/(?:Users|home|Volumes)/)", re.I)


def docx_archive_problem(file_path: Path | str) -> str | None:
    """Why this zip must not be decompressed, or None if its directory is sane.

    Only the central directory is read. Exposed so anything else that opens a
    stored .docx (text extraction) can apply the same limits.
    """
    import zipfile

    with zipfile.ZipFile(file_path) as z:
        infos = z.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        return f"archive has too many members ({len(infos)})"
    names = [info.filename.lower() for info in infos]
    if len(set(names)) != len(names):
        return "archive has two members with the same name"
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            return "archive member is encrypted"
        size = info.file_size
        total += size
        if size > MAX_ZIP_MEMBER_BYTES:
            return f"archive member expands to {size // 2**20} MiB"
        if size > _ZIP_RATIO_FLOOR and size > MAX_ZIP_RATIO * max(info.compress_size, 1):
            return "archive member expands too much (zip bomb)"
    if total > MAX_ZIP_TOTAL_BYTES:
        return f"archive expands to {total // 2**20} MiB"
    return None


def _strip_docx_metadata(file_path: Path, keep_hash: bool) -> dict[str, Any]:
    """Clean every part of the .docx that is known to carry metadata.

    Document properties (core, extended, custom), tracked-change and comment
    authors, initials and dates, revision-session ids, the reviewer list,
    data-store items, the thumbnail, the attached template path, document
    variables and ids, local file paths in picture descriptions and external
    links, and EXIF/XMP/text chunks in JPEG and PNG images are removed. Other
    embedded images and objects are not checked and make the result "partial".
    Every other member is written back unchanged, with zip timestamps and
    extra fields reset.
    """
    import os
    import tempfile
    import zipfile

    result: dict[str, Any] = {"stripped": [], "file_type": "docx", "status": "stripped"}

    try:
        problem = docx_archive_problem(file_path)
    except Exception as exc:
        logger.warning("Could not open DOCX %s: %s", file_path, exc)
        return {**result, "status": "failed", "error": "could not open the file as a DOCX"}
    if problem:
        return {**result, "status": "failed", "error": f"too large to clean safely: {problem}"}

    stripped: list[str] = []
    remaining: list[str] = []
    original_meta: dict[str, str] = {}

    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=str(file_path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(file_path) as src:
            infos = src.infolist()
            lower = {info.filename.lower() for info in infos}
            if "[content_types].xml" not in lower or "word/document.xml" not in lower:
                raise _NotDocx
            dropped = {name for name in lower if _docx_part_dropped(name)}
            for name in sorted(dropped):
                _note(stripped, _docx_drop_label(name))
            with zipfile.ZipFile(tmp, "w") as dst:
                for info in infos:
                    name = info.filename
                    if name.lower() in dropped or name.endswith("/"):
                        continue
                    with src.open(info) as member:
                        data = member.read(MAX_ZIP_MEMBER_BYTES + 1)
                    data = _clean_docx_member(
                        name, data, dropped, stripped, remaining, original_meta
                    )
                    out = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    out.compress_type = (
                        zipfile.ZIP_STORED
                        if info.compress_type == zipfile.ZIP_STORED
                        else zipfile.ZIP_DEFLATED
                    )
                    out.create_system = 0
                    dst.writestr(out, data)
        os.replace(tmp, file_path)
    except _NotDocx:
        Path(tmp).unlink(missing_ok=True)
        return {**result, "status": "failed", "error": "could not open the file as a DOCX"}
    except Exception as exc:
        Path(tmp).unlink(missing_ok=True)
        logger.warning("Could not clean DOCX %s: %s", file_path, exc)
        if isinstance(exc, zipfile.BadZipFile):
            return {**result, "status": "failed", "error": "could not open the file as a DOCX"}
        return {**result, "status": "failed", "error": "could not rewrite the DOCX"}

    result["stripped"] = stripped
    if keep_hash and original_meta:
        result["original_metadata_hash"] = _hash_metadata(original_meta)
    if remaining:
        result["status"] = "partial"
        result["remaining"] = remaining
    return result


class _NotDocx(Exception):
    pass


def _docx_part_dropped(name: str) -> bool:
    return (
        name in _DOCX_DROP_PARTS
        or name.startswith(_DOCX_DROP_PREFIXES)
        or name.startswith("docprops/thumbnail")
    )


def _docx_drop_label(name: str) -> str:
    if name in _DOCX_DROP_PARTS:
        return _DOCX_DROP_PARTS[name]
    if name.startswith("docprops/thumbnail"):
        return "thumbnail"
    return "custom XML data (customXml)"


def _clean_docx_member(
    name: str,
    data: bytes,
    dropped: set[str],
    stripped: list[str],
    remaining: list[str],
    original_meta: dict[str, str],
) -> bytes:
    """The cleaned bytes of one archive member (unchanged when nothing applies)."""
    low = name.lower()
    if low.endswith(".rels"):
        return _clean_docx_xml(
            name, data, remaining, lambda r: _clean_rels(r, low, dropped, stripped)
        )
    if low == "[content_types].xml":
        return _clean_docx_xml(name, data, remaining, lambda r: _clean_content_types(r, dropped))
    if low == "docprops/core.xml":
        return _clean_docx_xml(
            name, data, remaining, lambda r: _clean_core_props(r, stripped, original_meta)
        )
    if low == "docprops/app.xml":
        return _clean_docx_xml(
            name,
            data,
            remaining,
            lambda r: _strip_docx_extended_props(r, stripped, original_meta),
        )
    if low.endswith(".xml"):
        return _clean_docx_xml(name, data, remaining, lambda r: _clean_word_xml(r, stripped))
    if low.endswith((".jpg", ".jpeg", ".jpe")):
        return _clean_docx_image(name, data, _strip_jpeg_bytes, stripped, remaining)
    if low.endswith(".png"):
        return _clean_docx_image(name, data, _strip_png_bytes, stripped, remaining)
    if low.startswith("word/fonts/") or low == "mimetype":
        return data
    if low.startswith("word/embeddings/"):
        _note(remaining, f"embedded object {name} (not checked)")
    else:
        _note(remaining, f"{name} (not checked)")
    return data


def _clean_docx_image(
    name: str, data: bytes, stripper: Any, stripped: list[str], remaining: list[str]
) -> bytes:
    cleaned = stripper(data)
    if cleaned is None:
        _note(remaining, f"{name} (could not be checked)")
        return data
    if cleaned[1]:
        _note(stripped, f"image metadata in {name}")
    return cleaned[0]


def _clean_docx_xml(name: str, data: bytes, remaining: list[str], clean: Any) -> bytes:
    from lxml import etree

    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )
    try:
        root = etree.fromstring(data, parser)
    except Exception:
        _note(remaining, f"{name} (could not be parsed)")
        return data
    if root is None:
        _note(remaining, f"{name} (could not be parsed)")
        return data
    clean(root)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _ns(tag: Any) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _clean_core_props(root: Any, stripped: list[str], original_meta: dict[str, str]) -> None:
    for child in list(root):
        local = _local(child.tag)
        value = "".join(child.itertext()).strip()
        if value:
            label = _DOCX_CORE_NAMES.get(local, local)
            original_meta[label] = value
            _note(stripped, label)
        root.remove(child)


def _strip_docx_extended_props(
    root: Any, stripped: list[str], original_meta: dict[str, str]
) -> None:
    """Empty docProps/app.xml: company, manager, template, editing time, and the rest."""
    for child in list(root):
        local = _local(child.tag)
        value = "".join(child.itertext()).strip()
        if value and local in _DOCX_APP_REPORTED and not (local == "TotalTime" and value == "0"):
            original_meta[local.lower()] = value
            _note(stripped, local.lower())
        root.remove(child)


def _clean_rels(root: Any, rels_name: str, dropped: set[str], stripped: list[str]) -> None:
    import posixpath

    # word/_rels/document.xml.rels describes word/document.xml: targets are
    # relative to word/.
    base = posixpath.dirname(posixpath.dirname(rels_name))
    for rel in list(root):
        if _local(rel.tag) != "Relationship":
            continue
        target = rel.get("Target", "")
        kind = rel.get("Type", "")
        if kind.endswith("/attachedTemplate"):
            root.remove(rel)
            _note(stripped, "attached template path")
            continue
        if rel.get("TargetMode") == "External":
            if _LOCAL_PATH.match(target):
                rel.set("Target", "")
                _note(stripped, "local file paths in links")
            continue
        path = target.lstrip("/") if target.startswith("/") else posixpath.join(base, target)
        if posixpath.normpath(path).lower() in dropped:
            root.remove(rel)


def _clean_content_types(root: Any, dropped: set[str]) -> None:
    for entry in list(root):
        part = entry.get("PartName", "").lstrip("/").lower()
        if _local(entry.tag) == "Override" and part in dropped:
            root.remove(entry)


def _clean_word_xml(root: Any, stripped: list[str]) -> None:
    """Remove authors, initials, dates and session ids from a WordprocessingML part."""
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue  # comments, processing instructions
        local = _local(el.tag)
        if local in _DOCX_DROP_ELEMENTS and (_ns(el.tag) in _W_NAMESPACES or local == "docId"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                _note(stripped, _DOCX_DROP_ELEMENTS[local])
            continue
        for attr in list(el.attrib):
            attr_local = _local(attr) if attr.startswith("{") else attr
            in_w = _ns(attr) in _W_NAMESPACES
            if in_w and attr_local == "author":
                if el.attrib[attr]:
                    el.attrib[attr] = ""  # required on revisions and comments
                    _note(stripped, "revision and comment authors")
            elif in_w and attr_local in ("initials", "date"):
                del el.attrib[attr]
                _note(stripped, f"revision and comment {_PLURAL[attr_local]}")
            elif in_w and attr_local.startswith("rsid"):
                del el.attrib[attr]
                _note(stripped, "rsid")
            elif attr_local == "dateUtc":
                del el.attrib[attr]
                _note(stripped, "revision and comment dates")
            elif attr_local in ("descr", "title", "name") and _LOCAL_PATH.match(el.attrib[attr]):
                el.attrib[attr] = ""
                _note(stripped, "local file paths of inserted pictures")


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


def _pillow_available() -> bool:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


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
