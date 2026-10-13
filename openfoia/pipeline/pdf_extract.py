"""PDF text extraction via compiled binary.

This is NOT OCR. It reads text directly from PDF structures — font tables,
ToUnicode maps, glyph names, embedded cmaps. Fast (3-5ms/page) and lossless
on born-digital PDFs.

If the PDF is scanned (images of text with no extractable characters), this
will return little or nothing, and the caller should fall back to OCR.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Per-page verdicts the glyph `--json` contract emits (docs/JSON_CONTRACT.md).
VERDICT_TEXT_LAYER = "text_layer"
VERDICT_GARBLED = "garbled"
VERDICT_IMAGE_ONLY = "image_only"
NEEDS_OCR = frozenset({VERDICT_GARBLED, VERDICT_IMAGE_ONLY})


class PDFExtractTimeout(RuntimeError):
    """The extraction binary did not finish within the size-scaled time limit."""


def timeout_for(pdf_path: Path) -> int:
    """10 s plus 1 s per MB, capped at 600 s."""
    try:
        size_mb = pdf_path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0
    return int(min(600, 10 + size_mb))


@dataclass
class PDFExtractResult:
    """Result of direct PDF text extraction."""

    text: str
    page_count: int
    char_count: int
    backend: str  # which binary was used
    pages: list[str]  # text of each page, page 1 first
    #: per-page verdict (text_layer / garbled / image_only); None when the
    #: binary only produced plain form-feed output
    verdicts: list[str] | None = None


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
    profile: str = "large",
    max_workers: int | None = None,
) -> PDFExtractResult | None:
    """Extract text from a PDF using the compiled binary.

    Prefers the binary's ``--json`` output (per-page text and verdict) and
    falls back to plain form-feed output for older binaries.

    Returns None if:
    - No binary found
    - Binary fails
    - Extracted text is below min_chars (probably a scanned PDF, needs OCR)

    Raises PDFExtractTimeout when the binary exceeds the size-scaled limit.
    """
    binary = find_binary(binary_path)
    if not binary:
        return None

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return None

    # OpenFOIA default: use the large-document extraction profile.
    profile_env = os.environ.get("OPENFOIA_PDF_EXTRACT_PROFILE")
    if profile_env is not None and profile_env.strip():
        profile = profile_env.strip()

    workers_env = os.environ.get("OPENFOIA_PDF_EXTRACT_MAX_WORKERS")
    if workers_env is not None and workers_env.strip():
        try:
            max_workers = int(workers_env)
        except ValueError:
            max_workers = None

    if max_workers is not None and max_workers <= 0:
        max_workers = None

    flags: list[str] = []
    if profile:
        flags.extend(["--profile", profile])
    if max_workers is not None:
        flags.extend(["--max-workers", str(max_workers)])

    timeout = timeout_for(pdf_path)

    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _unknown_flag(result: subprocess.CompletedProcess, *names: str) -> bool:
        stderr = (result.stderr or "").lower()
        return ("unknown argument" in stderr or "unexpected argument" in stderr) and any(
            n in stderr for n in names
        )

    try:
        result = await asyncio.to_thread(_run, [binary, str(pdf_path), "--json", *flags])
        if result.returncode == 0:
            parsed = _parse_json(result.stdout)
            if parsed is not None:
                pages, verdicts = parsed
                text = "\f".join(pages)
                if len(text.strip()) < min_chars:
                    return None
                return PDFExtractResult(
                    text=text,
                    page_count=len(pages),
                    char_count=sum(len(p) for p in pages),
                    backend=binary,
                    pages=pages,
                    verdicts=verdicts,
                )
        elif not _unknown_flag(result, "--json"):
            return None

        # Older binary without --json: plain form-feed output.
        result = await asyncio.to_thread(_run, [binary, str(pdf_path), *flags])
        # Backward compatibility: if the installed extractor does not support
        # profile/worker flags yet, retry without them.
        if result.returncode != 0 and _unknown_flag(result, "--profile", "--max-workers"):
            result = await asyncio.to_thread(_run, [binary, str(pdf_path)])
    except subprocess.TimeoutExpired:
        raise PDFExtractTimeout(
            f"Text extraction of {pdf_path} did not finish within {timeout}s"
        ) from None
    except OSError:
        return None

    if result.returncode != 0:
        return None

    text = result.stdout
    if len(text.strip()) < min_chars:
        return None

    from ..doctext import split_pages

    # Pages are separated by form feed (0x0C)
    pages = split_pages(text)

    return PDFExtractResult(
        text=text,
        page_count=len(pages),
        char_count=len(text.replace("\f", "")),
        backend=binary,
        pages=pages,
    )


def _parse_json(stdout: str) -> tuple[list[str], list[str]] | None:
    """(pages, verdicts) from glyph ``--json`` output, or None if it is not that shape."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("engine") != "glyph-api":
        return None
    pages = data.get("pages")
    if not isinstance(pages, list):
        return None
    try:
        ordered = sorted(pages, key=lambda p: int(p["n"]))
        return (
            [str(p.get("text", "")) for p in ordered],
            [str(p.get("verdict") or VERDICT_TEXT_LAYER) for p in ordered],
        )
    except (KeyError, TypeError, ValueError):
        return None
