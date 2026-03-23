# Stack Context

Generated: 2026-03-23

## Stack
- **Language**: Python >=3.11
- **Framework**: FastAPI (web), SQLAlchemy 2.0 (ORM), Typer (CLI), Alembic (migrations)
- **Build**: hatchling (PEP 517)
- **Test**: `pytest` + `pytest-asyncio` (24 tests)
- **Lint**: ruff [CI gate: no]
- **Format**: ruff [CI gate: no]
- **Type check**: mypy strict [CI gate: no]

## Secondary Languages
- Rust (glyph-api PDF extraction engine, separate repo)
- Bash (install.sh, uninstall.sh)
- HTML/JS (inline web UI in server.py)

## Conventions
- Error handling: try/except with user-friendly rprint messages, typer.Exit(1) for CLI
- Module structure: openfoia/ root with subpackages (pipeline/, records/, gateways/, migrations/)
- Naming: snake_case throughout, CLI commands are hyphenated (install-extras, analyze-graph)
- Tests: tests/ directory, test_*.py naming, pytest fixtures with monkeypatch for isolation
- Optional deps: lazy imports inside functions, ImportError → helpful "openfoia install-extras" message
- Config: dataclass-based config.py, env vars with OPENFOIA_ prefix, JSON config file

## CI Gates
- No Python CI configured (no .github/workflows)
- glyph-api has CI: cargo fmt --check, cargo clippy -D warnings, cargo test
- Pre-commit hook on glyph-api: fmt + clippy

## Notes
- No CLAUDE.md in this repo
- 40 Python files, ~530KB of source
- Hub files: models.py, cli.py, extract.py, server.py, db.py
- cli.py is ~4000 lines (largest file, contains all CLI commands)
