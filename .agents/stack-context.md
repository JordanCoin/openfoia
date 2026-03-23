# Stack Context

Generated: 2026-03-23

## Stack
- **Language**: Python >=3.11
- **Framework**: FastAPI (web), SQLAlchemy 2.0 (ORM), Typer (CLI), Alembic (migrations)
- **Build**: hatchling (PEP 517)
- **Test**: `pytest` + `pytest-asyncio` (24 tests)
- **Lint**: ruff [CI gate: yes, pre-commit hook: yes]
- **Format**: ruff [CI gate: yes, pre-commit hook: yes]
- **Type check**: mypy strict [CI gate: yes, continue-on-error]

## Secondary Languages
- Bash (install.sh, uninstall.sh)
- HTML/JS (inline web UI in server.py)
- A compiled Rust binary (`pdf-extract`) handles PDF text extraction — distributed via GitHub Releases

## Conventions
- Error handling: try/except with user-friendly rprint messages, typer.Exit(1) for CLI
- Module structure: openfoia/ root with subpackages (pipeline/, records/, gateways/, migrations/)
- Naming: snake_case throughout, CLI commands are hyphenated (install-extras, analyze-graph)
- Tests: tests/ directory, test_*.py naming, pytest fixtures with monkeypatch for isolation
- Optional deps: lazy imports inside functions, ImportError → "openfoia install-extras X" message
- Config: dataclass-based config.py, env vars with OPENFOIA_ prefix, JSON config file
- Security: honest threat model (docs/THREAT_MODEL.md), no stored password hashes, warn before network calls

## CI Gates
- ruff check (lint) — blocks
- ruff format --check — blocks
- mypy --ignore-missing-imports — runs but continue-on-error
- pytest (24 tests, excludes GLiNER/spaCy/LLM/benchmark) — blocks
- Tested on Python 3.11, 3.12, 3.13
- Pre-commit hook: .githooks/pre-commit (ruff lint + format)
