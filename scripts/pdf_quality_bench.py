#!/usr/bin/env python3
"""Compare PDF quality-gate policies on a directory of real PDFs (issue #61).

Runs the real ``pdf-extract --json`` on every PDF under DIR and reports, per
file and in total, how many text-layer characters each policy keeps and how
many pages it sends to OCR:

    main   before #105: whole-document OCR only when the stripped text is
           under 50 chars; otherwise garbled/image_only pages go to OCR one
           by one and text_layer pages keep their text.
    pr     #105 as submitted: any whole-document gate failure (non-whitespace
           chars < 50, chars/page < 20, alpha ratio < 0.20) OCRs every page.
    fixed  with per-page verdicts only the 50-char gate applies; everything
           else is routed per page as on main.

"Chars kept" counts non-whitespace characters of text_layer pages that keep
their direct text. Stdlib only; nothing leaves the machine.

Usage:
    python scripts/pdf_quality_bench.py DIR [--binary pdf-extract] [--timeout 300]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

NEEDS_OCR = {"garbled", "image_only"}
MIN_CHARS, MIN_CHARS_PER_PAGE, MIN_ALPHA_RATIO = 50, 20, 0.20
POLICIES = ("main", "pr", "fixed")


def _nonws(text: str) -> int:
    return sum(not c.isspace() for c in text)


def _pr_rejects(text: str, page_count: int) -> bool:
    chars = _nonws(text)
    alpha = sum(c.isalpha() for c in text) / chars if chars else 0.0
    per_page = chars / page_count if page_count else 0.0
    return chars < MIN_CHARS or per_page < MIN_CHARS_PER_PAGE or alpha < MIN_ALPHA_RATIO


def score(pages: list[str], verdicts: list[str]) -> dict[str, tuple[int, int]]:
    """policy -> (chars kept from the text layer, pages sent to OCR)."""
    text = "\f".join(pages)
    n = len(pages)
    per_page = (
        sum(_nonws(p) for p, v in zip(pages, verdicts, strict=True) if v not in NEEDS_OCR),
        sum(v in NEEDS_OCR for v in verdicts),
    )
    whole = (0, n)
    return {
        "main": whole if len(text.strip()) < MIN_CHARS else per_page,
        "pr": whole if _pr_rejects(text, n) else per_page,
        "fixed": whole if _nonws(text) < MIN_CHARS else per_page,
    }


def extract(binary: str, pdf: Path, timeout: int) -> tuple[list[str], list[str]] | str:
    try:
        r = subprocess.run(
            [binary, str(pdf), "--json"], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    if r.returncode != 0:
        return f"exit {r.returncode}"
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return "bad json"
    pages = sorted(data.get("pages", []), key=lambda p: p["n"])
    if not pages:
        return "no pages"
    return [p.get("text", "") for p in pages], [p.get("verdict", "") for p in pages]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir", type=Path)
    ap.add_argument("--binary", default="pdf-extract")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    binary = shutil.which(args.binary)
    if not binary:
        print(f"{args.binary} not found on PATH", file=sys.stderr)
        return 2

    groups: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {p: [0, 0, 0] for p in (*POLICIES, "_")}
    )
    errors: dict[str, int] = defaultdict(int)
    print("| file | pages | " + " | ".join(f"{p} kept / OCR" for p in POLICIES) + " |")
    print("|---|---:|" + "---:|" * len(POLICIES))
    for pdf in sorted(args.dir.rglob("*.pdf")):
        rel = pdf.relative_to(args.dir)
        group = rel.parts[0] if len(rel.parts) > 1 else "."
        got = extract(binary, pdf, args.timeout)
        if isinstance(got, str):
            errors[got] += 1
            print(f"| {rel} | {got} |" + " |" * len(POLICIES))
            continue
        pages, verdicts = got
        result = score(pages, verdicts)
        g = groups[group]
        g["_"][0] += 1
        g["_"][1] += len(pages)
        for p in POLICIES:
            g[p][0] += result[p][0]
            g[p][1] += result[p][1]
            g[p][2] += result[p] != result["main"]
        cells = " | ".join(f"{result[p][0]:,} / {result[p][1]}" for p in POLICIES)
        print(f"| {rel} | {len(pages)} | {cells} |")

    print("\n| group | files | pages | " + " | ".join(f"{p} kept / OCR" for p in POLICIES) + " |")
    print("|---|---:|---:|" + "---:|" * len(POLICIES))
    total = {p: [0, 0, 0] for p in (*POLICIES, "_")}
    for name in sorted(groups):
        g = groups[name]
        for k in total:
            total[k] = [a + b for a, b in zip(total[k], g[k], strict=True)]
        cells = " | ".join(f"{g[p][0]:,} / {g[p][1]:,}" for p in POLICIES)
        print(f"| {name} | {g['_'][0]} | {g['_'][1]:,} | {cells} |")
    cells = " | ".join(f"**{total[p][0]:,} / {total[p][1]:,}**" for p in POLICIES)
    print(f"| **total** | {total['_'][0]} | {total['_'][1]:,} | {cells} |")
    print(
        "\nfiles where the policy differs from main: "
        + ", ".join(f"{p} {total[p][2]}" for p in POLICIES[1:])
    )
    if errors:
        print("not extracted: " + ", ".join(f"{k} {v}" for k, v in sorted(errors.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
