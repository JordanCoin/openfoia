"""PDF text extraction via compiled binary.

This is NOT OCR. It reads text directly from PDF structures — font tables,
ToUnicode maps, glyph names, embedded cmaps. Fast (3-5ms/page) and lossless
on born-digital PDFs.

If the PDF is scanned (images of text with no extractable characters), this
will return little or nothing, and the caller should fall back to OCR.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PDFExtractResult:
    """Result of direct PDF text extraction."""

    text: str
    page_count: int
    char_count: int
    backend: str  # which binary was used


def find_binary(configured_path: str | None = None) -> str | None:
    """Find the PDF extraction binary.

    Checks in order:
    1. Explicitly configured path
    2. 'pdf-extract' on $PATH
    3. Common install locations
    """
    if configured_path is not None:
        if configured_path == "":
            return None  # explicitly disabled
        p = Path(configured_path)
        if p.is_file():
            return str(p)
        return None

    # Check PATH
    found = shutil.which("pdf-extract")
    if found:
        return found

    # Check common locations
    for candidate in [
        Path.home() / ".local" / "bin" / "pdf-extract",
        Path("/usr/local/bin/pdf-extract"),
    ]:
        if candidate.is_file():
            return str(candidate)

    return None


async def extract_text(
    pdf_path: Path | str,
    binary_path: str | None = None,
    min_chars: int = 50,
) -> PDFExtractResult | None:
    """Extract text from a PDF using the compiled binary.

    Returns None if:
    - No binary found
    - Binary fails
    - Extracted text is below min_chars (probably a scanned PDF, needs OCR)
    """
    binary = find_binary(binary_path)
    if not binary:
        return None

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return None

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            [binary, str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        result = await asyncio.to_thread(_run)
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    text = result.stdout
    if len(text.strip()) < min_chars:
        return None

    # Pages are separated by form feed (0x0C)
    pages = text.split("\f")
    page_count = len(pages)

    return PDFExtractResult(
        text=text,
        page_count=page_count,
        char_count=len(text.replace("\f", "")),
        backend=binary,
    )
