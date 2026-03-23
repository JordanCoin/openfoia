# OpenFOIA

Local-first investigation toolkit for journalists. Python 3.11+, FastAPI, SQLAlchemy, Typer.

## Quick Start

```bash
source /tmp/openfoia-venv/bin/activate  # or wherever your venv is
pip install -e .
openfoia init
python -m pytest tests/ -v
```

## Architecture

```
openfoia/
├── cli.py           # All CLI commands (~4000 lines, the main interface)
├── server.py        # FastAPI web UI (localhost only, token auth)
├── agent.py         # LLM tool-calling interface (same ops as CLI)
├── models.py        # SQLAlchemy ORM (Agency, Request, Document, Entity, Campaign)
├── db.py            # Database session management, encryption, portable mode
├── config.py        # Dataclass config, env vars (OPENFOIA_*), JSON config
├── security.py      # Secure delete, duress mode, shell history scrubbing
├── crossref.py      # Cross-reference engine (MuckRock, SEC, OpenSanctions, etc.)
├── ftm.py           # FollowTheMoney export (Aleph/OpenAleph interop)
├── ftm_import.py    # FollowTheMoney import
├── pipeline/
│   ├── extract.py   # Entity extraction (4-tier: LLM → GLiNER → spaCy → regex)
│   ├── ocr.py       # OCR engine (pdf-extract binary first, tesseract fallback)
│   ├── ingest.py    # Document ingestion + metadata stripping
│   ├── pdf_extract.py  # Compiled binary wrapper for PDF text extraction
│   ├── web.py       # Web archive ingestion via Tor
│   └── metadata.py  # EXIF/PDF/DOCX metadata stripping
├── records/         # Public records adapters (MuckRock, OpenCorporates, SEC EDGAR)
├── gateways/        # Delivery (email, fax via Twilio, mail via Lob)
└── migrations/      # Alembic migrations
```

## Key Patterns

- **Optional deps are lazy-imported** inside functions. If missing, show `openfoia install-extras <name>` message. Never crash on missing optional dep at import time.
- **All data goes through `get_data_dir()`** which respects `OPENFOIA_DATA_DIR` env var and portable mode (`.openfoia-portable` marker file).
- **Config uses `_default_config_path()`** which also respects portable mode. Don't hardcode `~/.openfoia/`.
- **CLI commands use `typer`**. Rich for output. `rprint()` for colored messages.
- **Entity extraction has 4 tiers**: LLM (ollama/anthropic/openai) → GLiNER → spaCy → regex. Each tier falls back to the next. Regex always works.
- **Security claims must be honest**. See `docs/THREAT_MODEL.md`. Don't claim "no traces" — say what's actually cleaned and what isn't.
- **No password hashes in config**. Duress mode uses SQLCipher as the verifier (try to open the DB).

## Testing

```bash
python -m pytest tests/ -v                              # fast tests (24)
python -m pytest tests/ -v -k "gliner"                  # GLiNER tests (needs pip install gliner)
python -m pytest tests/ -v -k "LLM"                     # LLM tests (needs ollama running)
python tests/benchmark_extraction.py                    # full benchmark with graph output
```

## Common Tasks

- **Add a new CLI command**: Add to `openfoia/cli.py` under the appropriate `*_app` typer group
- **Add a new records adapter**: Create `openfoia/records/<name>.py`, implement `RecordAdapter`, register in `__init__.py`
- **Add a new gateway**: Create `openfoia/gateways/<name>.py`, implement `DeliveryGateway`
- **Change the DB schema**: Update `openfoia/models.py`, create migration with `alembic revision --autogenerate`

## Don't

- Don't add core deps for optional features — use `[project.optional-dependencies]` in pyproject.toml
- Don't send data to external APIs without warning the user (see #32, #33)
- Don't store passwords or hashes in config files (see #31)
- Don't use `innerHTML` in the web UI — use `textContent` and `createElement`
- Don't claim security guarantees you can't back up — read `docs/THREAT_MODEL.md`
