# OpenFOIA

## Mission

OpenFOIA exists so that anyone, anywhere, can investigate power safely.

FOIA is an American privilege. Most of the world has no legal right to demand information from their government. Even where it exists, the act of asking can be dangerous. This project is for the journalist in Istanbul investigating corruption, the researcher in Nairobi tracking government contracts, the citizen in São Paulo following public money. Local-first. Offline-capable. Purgeable. Free.

Every coding decision flows from this: **the user's safety comes first.** If a feature leaks data, warns too late, or overpromises security — that's not a bug, it's a betrayal of the mission.

## Principles

1. **Data never leaves the machine unless the user explicitly chooses.** No silent cloud calls. No analytics. No telemetry. Warn before any network request.
2. **Work offline by default.** Every core feature must work with zero internet. Network features (crossref, MuckRock, cloud AI) are opt-in enhancements.
3. **Be honest about what we protect and what we don't.** Read `docs/THREAT_MODEL.md`. Never say "no traces" — say exactly what's cleaned and what isn't.
4. **The tool works on a $300 laptop.** Core install is ~30MB. Heavy deps (GLiNER, PyTorch) are optional. The regex fallback always works.
5. **A human and an AI agent use the same interface.** CLI commands and agent tool calls hit the same code paths, same database, same privacy guarantees.

## Quick Start

```bash
pip install -e ".[dev]"
git config core.hooksPath .githooks   # pre-commit: ruff lint + format
openfoia init
python -m pytest tests/ -v
```

## Architecture

```
openfoia/
├── cli.py           # All CLI commands (~4000 lines)
├── server.py        # FastAPI web UI (localhost, token auth)
├── agent.py         # LLM tool-calling interface
├── models.py        # SQLAlchemy ORM
├── db.py            # DB sessions, encryption, portable mode
├── config.py        # Dataclass config, OPENFOIA_* env vars
├── security.py      # Secure delete, duress mode
├── crossref.py      # Cross-reference engine
├── ftm.py           # FollowTheMoney export
├── ftm_import.py    # FollowTheMoney import
├── pipeline/        # extract, ocr, ingest, pdf_extract, web, metadata
├── records/         # MuckRock, OpenCorporates, SEC EDGAR adapters
├── gateways/        # email, fax (Twilio), mail (Lob)
└── migrations/      # Alembic
```

## Key Patterns

- **Optional deps are lazy-imported** inside functions. Missing dep → show `openfoia install-extras <name>`. Never crash at import time.
- **All data paths go through `get_data_dir()`** — respects `OPENFOIA_DATA_DIR` and portable mode.
- **Config uses `_default_config_path()`** — also respects portable mode. Never hardcode `~/.openfoia/`.
- **Entity extraction has 4 tiers**: LLM → GLiNER → spaCy → regex. Each falls back. Regex always works.
- **No password hashes in config.** Duress mode uses SQLCipher as verifier.
- **Crossref warns before network calls.** Distinguish "no results" from "API error."

## Testing

```bash
python -m pytest tests/ -v                    # fast tests (24)
python -m pytest tests/ -v -k "gliner"        # needs: pip install gliner
python -m pytest tests/ -v -k "LLM"           # needs: ollama running
python tests/benchmark_extraction.py          # full benchmark + graph
```

## Common Tasks

- **New CLI command**: add to `cli.py` under the appropriate `*_app` typer group
- **New records adapter**: `openfoia/records/<name>.py`, implement `RecordAdapter`, register in `__init__.py`
- **New gateway**: `openfoia/gateways/<name>.py`, implement `DeliveryGateway`
- **Schema change**: update `models.py`, then `alembic revision --autogenerate`

## Don't

- Don't add core deps for optional features — use `[project.optional-dependencies]`
- Don't send data to external APIs without warning the user
- Don't store passwords or hashes in config files
- Don't use `innerHTML` in the web UI — use `textContent` and `createElement`
- Don't claim security guarantees you can't back up — read `docs/THREAT_MODEL.md`
- Don't optimize for developer convenience at the cost of user safety
