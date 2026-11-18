"""Importing an entry point must not load any optional extra.

Optional dependencies are imported lazily, inside the function that needs
them, so the core install stays small and `openfoia --help` stays fast on a
$300 laptop. Hoisting one of those imports to a module top (or importing a
module that does) silently breaks that. Each check runs in a fresh
interpreter, because this test process has long since imported everything.

An extra that is not installed cannot leak, so the check only bites where the
package is present; the dev install includes Pillow, the rest are covered on
machines with the extras installed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

OPTIONAL_EXTRAS = (
    "torch",
    "gliner",
    "gliner2",
    "spacy",
    "playwright",
    "PIL",
    "pytesseract",
    "model2vec",
    "sqlite_vec",
    "numpy",
    "mcp",
    "anthropic",
    "openai",
    "twilio",
    "lob",
    "socksio",
    "pdf2image",
    "reportlab",
    "extract_msg",
    "documentcloud",
    "huggingface_hub",
    "followthemoney",
    # openfoia.db probes for a SQLCipher driver at import, so the entry points
    # import db lazily; this keeps it that way.
    "sqlcipher3",
    "pysqlcipher3",
)

ENTRY_POINTS = (
    "openfoia.cli",
    "openfoia.server",
    "openfoia.agent",
    "openfoia.mcp_server",
    "openfoia.semantic",
)

_PROBE = """
import json, sys
import openfoia, {module}
print(json.dumps({{"file": openfoia.__file__, "modules": sorted(sys.modules)}}))
"""


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_core_import_loads_no_optional_extras(module: str) -> None:
    if importlib.util.find_spec(module) is None:
        pytest.skip(f"{module} is not part of this checkout")

    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module)],
        cwd=REPO,  # `-c` puts the cwd first on sys.path: test this checkout
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(result["file"]).resolve().is_relative_to(REPO), result["file"]

    loaded = {name.partition(".")[0] for name in result["modules"]}
    leaked = sorted(loaded.intersection(OPTIONAL_EXTRAS))
    assert not leaked, f"`import {module}` loads optional extras: {', '.join(leaked)}"
