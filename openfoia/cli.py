"""OpenFOIA command-line interface."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import functools
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import webbrowser
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import typer
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text
from sqlalchemy import func, or_

from . import __version__
from .models import (
    Agency,
    AgencyLevel,
    Campaign,
    Case,
    CaseNote,
    CaseQuery,
    DeliveryMethod,
    Document,
    DocumentPage,
    DocumentType,
    Entity,
    Request,
    RequestStatus,
    TimelineEvent,
    User,
    entity_links,
    utcnow,
)
from .models import Agency as AgencyModel
from .models import Document as DocModel
from .models import Request as ReqModel
from .models import Request as RequestModel
from .models import utcnow as _utcnow
from .termsafe import SafeConsole, safe_cell, safe_json, safe_text
from .termsafe import safe_markup as _safe

if TYPE_CHECKING:
    from .net import EgressPolicy

# Every Rich string this module prints goes through SafeConsole, which removes
# terminal control characters (ESC, C1, bidi overrides) as a backstop. Values
# from records APIs, crossref sources, imported files and documents are also
# escaped at each call site (_safe for markup strings, _row / safe_cell for
# table cells, _echo for plain output, safe_json for JSON), so they are never
# parsed as Rich markup either.
console = SafeConsole()
rprint = console.print


def _row(table: Table, *cells: Any) -> None:
    """Add a table row of untrusted values: each cell is cleaned Text, never markup."""
    table.add_row(*(safe_cell(c) for c in cells))


def _echo(message: Any = "", **kwargs: Any) -> None:
    """typer.echo for text that carries upstream or document-derived values."""
    typer.echo(safe_text(message, multiline=True), **kwargs)


app = typer.Typer(
    name="openfoia",
    help="Crowdsourced FOIA automation with AI-powered document analysis.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        rprint(f"openfoia {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the installed OpenFOIA version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Crowdsourced FOIA automation with AI-powered document analysis."""
    # Portable mode: keep model/library caches on the stick, not in the host's
    # home directory. A no-op for an ordinary install.
    from .config import confine_caches_to_data_dir  # lazy: startup time

    confine_caches_to_data_dir()


# === Init Command ===


@app.command()
def init(
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-initialize even if database exists"
    ),
    no_seed: bool = typer.Option(False, "--no-seed", help="Don't seed agency data"),
    encrypt: bool = typer.Option(
        False, "--encrypt", help="Encrypt the database at rest (prompts for a passphrase)"
    ),
    duress: bool = typer.Option(
        False, "--duress", help="Also set up a decoy database (prompts for a passphrase)"
    ),
    password: str | None = typer.Option(
        None,
        "--password",
        prompt=False,
        hide_input=True,
        help="Encryption passphrase. Prefer --encrypt, which prompts: a passphrase "
        "passed here is recorded in shell history and visible in the process list.",
    ),
    duress_password: str | None = typer.Option(
        None,
        "--duress-password",
        prompt=False,
        hide_input=True,
        help="Duress passphrase. Prefer --duress, which prompts (see --password).",
    ),
):
    """Initialize the OpenFOIA database.

    Creates ~/.openfoia/ directory and initializes the SQLite database
    with tables and seed data (federal agencies).

    Pass --password to encrypt the database at rest with AES-256 (requires
    the sqlcipher C library and pysqlcipher3: pip install 'openfoia[encryption]').

    Pass --duress-password to set up a decoy database. When this password is
    supplied (via OPENFOIA_DB_PASSWORD or direct input), OpenFOIA transparently
    opens a decoy DB with innocent-looking data instead of the real one.

    Examples:
        openfoia init                        # Initialize with agency data
        openfoia init --no-seed              # Initialize without seed data
        openfoia init --force                # Re-initialize (WARNING: loses data)
        openfoia init --encrypt              # Initialize with encryption (prompts)
        openfoia init --encrypt --duress     # Also set up a decoy database
    """
    from .db import get_data_dir, get_db_path, has_sqlcipher, init_db  # lazy: startup time

    data_dir = get_data_dir()

    rprint("\n[bold green]🔒 OpenFOIA Initialization[/bold green]")
    rprint("─" * 50)
    from .pipeline.pdf_extract import find_binary, font_pack_dir  # lazy: startup time

    engine = find_binary()
    if engine:
        pack = font_pack_dir(engine)
        if pack is not None:
            rprint(f"PDF engine font pack: {pack}")
        else:
            rprint(
                "PDF engine font pack: not installed (optional; "
                "install.sh --with-fonts fetches it)."
            )

    # Prefer prompting: a passphrase in argv is written to shell history and
    # is readable from the process list while the command runs.
    if password:
        rprint(
            "[yellow]WARNING: --password was read from the command line. It is now in "
            "your shell history and was visible in the process list. "
            "Prefer --encrypt, which prompts.[/yellow]"
        )
    elif encrypt:
        password = typer.prompt("Encryption passphrase", hide_input=True, confirmation_prompt=True)

    if duress_password:
        rprint(
            "[yellow]WARNING: --duress-password was read from the command line "
            "(shell history + process list). Prefer --duress, which prompts.[/yellow]"
        )
    elif duress:
        duress_password = typer.prompt(
            "Duress passphrase", hide_input=True, confirmation_prompt=True
        )

    if duress_password and duress_password == password:
        rprint("[bold red]Error: duress password must differ from the real password.[/bold red]")
        raise typer.Exit(1)

    db_path = get_db_path(password=password)

    if (password or duress_password) and not has_sqlcipher():
        rprint("[bold red]ERROR: pysqlcipher3 is not installed.[/bold red]")
        rprint("[red]Cannot create an encrypted database or decoy profile without it.[/red]")
        rprint("[yellow]Install with: openfoia install-extras encryption[/yellow]")
        raise typer.Exit(1)

    if db_path.exists() and not force:
        rprint(f"[cyan]Database already exists:[/cyan] {db_path}")
        rprint("[dim]Use --force to re-initialize (WARNING: loses data)[/dim]")

        if duress_password:
            # Adding a decoy to an existing database: setup_duress_mode refuses
            # unless that database is already encrypted.
            _setup_duress_or_exit(duress_password)
            return

        # Show stats
        from .db import get_session  # lazy: startup time

        try:
            with get_session(password=password) as session:
                agency_count = session.query(Agency).count()
                request_count = session.query(Request).count()
                doc_count = session.query(Document).count()
        except Exception:
            rprint(
                "[dim]Could not read it (encrypted? set OPENFOIA_DB_PASSWORD, "
                "or pass --encrypt to be asked for the passphrase).[/dim]"
            )
            return

        rprint("\n[cyan]Current data:[/cyan]")
        rprint(f"  Agencies: {agency_count}")
        rprint(f"  Requests: {request_count}")
        rprint(f"  Documents: {doc_count}")
        return

    if duress_password and not password:
        # An encrypted decoy next to a plaintext real database points an
        # examiner straight at the readable investigation. Refuse before
        # touching anything.
        rprint(
            "[bold red]Error: duress mode needs the real database encrypted too.[/bold red]\n"
            "[red]Nothing was created. Re-run with both:[/red] openfoia init --encrypt --duress"
        )
        raise typer.Exit(1)

    if force:
        removed = _shred_all_db_files(data_dir)
        if removed:
            rprint("[yellow]Removed (overwritten) the existing database files:[/yellow]")
            for name in removed:
                rprint(f"  [yellow]{name}[/yellow]")
            rprint(
                f"[yellow]Files under {data_dir} (ingested documents, exports, config) were "
                "not removed. Use 'openfoia purge --secure' to remove everything.[/yellow]"
            )
        db_path = get_db_path(password=password)

    rprint(f"[cyan]Data directory:[/cyan] {data_dir}")
    rprint(f"[cyan]Database:[/cyan] {db_path}")
    if password and has_sqlcipher():
        rprint("[cyan]Encryption:[/cyan] AES-256 (SQLCipher)")

    # Initialize
    rprint("\n[cyan]Creating tables...[/cyan]")
    init_db(seed=not no_seed, password=password)

    if not no_seed:
        from .db import get_session  # lazy: startup time

        with get_session(password=password) as session:
            count = session.query(Agency).count()
        rprint(f"[green]✓ Seeded {count} federal agencies[/green]")

    # Duress mode setup
    if duress_password:
        _setup_duress_or_exit(duress_password)

    rprint("\n[bold green]✓ Initialization complete![/bold green]")
    rprint("[dim]Run 'openfoia serve' to start the web interface.[/dim]\n")


def _setup_duress_or_exit(duress_password: str) -> None:
    """Configure the decoy profile, or explain why not and exit 1."""
    from .security import DuressSetupError, setup_duress_mode  # lazy: startup time

    try:
        decoy_path = setup_duress_mode(duress_password)
    except (DuressSetupError, RuntimeError) as exc:
        rprint(f"[bold red]Duress mode NOT configured:[/bold red] {exc}")
        raise typer.Exit(1) from None
    rprint("[green]✓ Duress mode configured (both profiles checked: encrypted on disk)[/green]")
    rprint(f"[dim]  Decoy database: {decoy_path}[/dim]")
    rprint("[dim]  When the duress password is used, the decoy DB is opened transparently.[/dim]")
    rprint("[dim]  It is a decoy, not deniability: see docs/THREAT_MODEL.md.[/dim]")


def _shred_all_db_files(data_dir: Path) -> list[str]:
    """Overwrite and delete every database file (all profiles, with WAL/SHM/journal).

    ``init --force`` used to plain-unlink just the one database, leaving its
    blocks recoverable, its -wal/-shm sidecars in place, and (with duress mode
    set up) the other profile next to a fresh data.db, which tells an examiner
    which file was the decoy.
    """
    from .db import _DB_SIDECAR_SUFFIXES  # lazy: startup time
    from .security import _PROFILE_SLOTS, secure_delete_file  # lazy: startup time

    removed: list[str] = []
    for base in ("data.db", *_PROFILE_SLOTS):
        for suffix in ("", *_DB_SIDECAR_SUFFIXES):
            candidate = data_dir / f"{base}{suffix}"
            if not candidate.exists():
                continue
            report = secure_delete_file(candidate)
            if report.survived:
                rprint(
                    f"[bold red]Could not remove {candidate}; nothing was re-initialized.[/bold red]"
                )
                raise typer.Exit(1)
            if report.unlinked_only:
                rprint(f"[yellow]{candidate} was deleted but could not be overwritten.[/yellow]")
            removed.append(candidate.name)
    return removed


# === Server Command ===


@app.command("install-extras")
def install_extras(
    extra: str = typer.Argument(
        ...,
        help="Extra to install: ner, ner-multilingual, ner-spacy, ocr, fax, mail, cloud-ai, "
        "tor, browser, encryption, semantic, mcp, all",
    ),
):
    """Install optional features after the initial setup.

    The base install is lightweight (~30MB). Add features as you need them.

    Examples:
        openfoia install-extras ner          # GLiNER entity extraction (~2GB)
        openfoia install-extras ner-multilingual  # GLiNER2, names in many languages
        openfoia install-extras ocr          # Tesseract OCR support
        openfoia install-extras cloud-ai     # Anthropic + OpenAI
        openfoia install-extras encryption   # SQLCipher encrypted database
        openfoia install-extras semantic     # offline search by meaning (docs grep --semantic)
        openfoia install-extras mcp          # MCP server for agent clients (openfoia mcp serve)
        openfoia install-extras all          # everything
    """
    valid = {
        "ner",
        "ner-multilingual",
        "ner-spacy",
        "ocr",
        "fax",
        "mail",
        "cloud-ai",
        "tor",
        "browser",
        "encryption",
        "semantic",
        "mcp",
        "all",
    }
    if extra not in valid:
        rprint(f"[red]Unknown extra '{extra}'. Choose from: {', '.join(sorted(valid))}[/red]")
        raise typer.Exit(1)

    size_hints = {
        "ner": "~2GB (PyTorch + GLiNER model)",
        "ner-multilingual": "~900MB (PyTorch + GLiNER2; the model, ~1.25GB, downloads on first use)",
        "ner-spacy": "~100MB (+ download a model after)",
        "ocr": "~5MB (also needs system tesseract-ocr + poppler-utils)",
        "fax": "~20MB",
        "mail": "~5MB",
        "cloud-ai": "~30MB",
        "tor": "~1MB",
        "browser": "~50MB (+ playwright install after)",
        "encryption": "~5MB (also needs system libsqlcipher-dev)",
        "semantic": "~90MB (model2vec, tokenizers, numpy, sqlite-vec; the model is a separate download)",
        "mcp": "~15MB (the MCP Python SDK, for openfoia mcp serve)",
        "all": "~2.5GB total",
    }

    rprint(f"[cyan]Installing: openfoia[{extra}] — {size_hints.get(extra, '')}[/cyan]")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", f"openfoia[{extra}]"],
        capture_output=False,
    )

    if result.returncode == 0:
        rprint(f"\n[green]Installed openfoia[{extra}][/green]")
        if extra == "ocr":
            rprint(
                "[dim]Also install system packages: apt install tesseract-ocr poppler-utils (Linux) or brew install tesseract poppler (macOS)[/dim]"
            )
        elif extra == "ner-spacy":
            rprint("[cyan]Downloading English language model...[/cyan]")
            dl_result = subprocess.run(
                [sys.executable, "-m", "spacy", "download", "en_core_web_sm"],
                capture_output=False,
            )
            if dl_result.returncode == 0:
                rprint("[green]spaCy model downloaded.[/green]")
            else:
                rprint(
                    "[yellow]Model download failed. Run manually: python -m spacy download en_core_web_sm[/yellow]"
                )
        elif extra == "ner-multilingual":
            rprint(
                "[dim]Select it: openfoia analyze extract <doc> --ner-model multilingual, "
                'OPENFOIA_NER_MODEL=multilingual, or "entities": {"ner_model": "multilingual"} '
                "in config.json. The first extraction downloads ~1.25 GB from huggingface.co "
                "(it warns first); it uses ~3 GB of RAM while extracting.[/dim]"
            )
        elif extra == "browser":
            rprint("[dim]Also run: playwright install chromium[/dim]")
        elif extra == "encryption":
            rprint(
                "[dim]Also install system package: apt install libsqlcipher-dev (Linux) or brew install sqlcipher (macOS)[/dim]"
            )
        elif extra == "semantic":
            rprint(
                "[dim]Next, download the embedding model once (asks first; ~31 MB from "
                "huggingface.co): openfoia docs embed-model[/dim]"
            )
    else:
        rprint(f"[red]Install failed. Try manually: pip install 'openfoia[{extra}]'[/red]")
        raise typer.Exit(1)


@app.command()
def portable(
    enable: bool = typer.Option(True, "--enable/--disable", help="Enable or disable portable mode"),
):
    """Enable portable mode — keep all data next to the program.

    Creates a .openfoia-portable marker file in the current directory.
    When this file exists, all data (database, documents, graphs, config)
    is stored in ./openfoia-data/ instead of ~/.openfoia/.

    Use this when running from a USB stick or portable install so nothing
    touches the host machine's disk.

    Examples:
        cd /Volumes/MY_USB
        openfoia portable                # enable — data stays on USB
        openfoia init                    # database created on USB
        openfoia serve                   # everything runs from USB

        openfoia portable --disable      # go back to ~/.openfoia/
    """
    marker = Path.cwd() / ".openfoia-portable"

    if enable:
        marker.touch()
        data_dir = Path.cwd() / "openfoia-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        rprint("[green]Portable mode enabled.[/green]")
        rprint(f"  Marker: {marker}")
        rprint(f"  Data directory: {data_dir}")
        rprint(f"\n[dim]All data will be stored in {data_dir}[/dim]")
        rprint("[dim]Nothing will be written to ~/.openfoia/[/dim]")
        rprint("[dim]To undo: openfoia portable --disable[/dim]")
    else:
        if marker.exists():
            marker.unlink()
            rprint("[green]Portable mode disabled.[/green]")
            rprint("[dim]Data will now be stored in ~/.openfoia/[/dim]")
        else:
            rprint("[dim]Portable mode is not active in this directory.[/dim]")


@app.command()
def guide():
    """Interactive quickstart guide for new users.

    Walks you through the basics: where to put files, how to file a request,
    how to analyze documents, and how to stay safe.
    """
    rprint("""
[bold cyan]OpenFOIA Quickstart Guide[/bold cyan]
[dim]Everything runs locally. Your data never leaves this machine.[/dim]

[bold]1. Your data lives here:[/bold]
   [cyan]~/.openfoia/[/cyan]
   ├── data.db       [dim]Database (requests, entities, graphs)[/dim]
   ├── docs/         [dim]Ingested documents[/dim]
   ├── graphs/       [dim]Saved investigation graphs[/dim]
   ├── exports/      [dim]Reports[/dim]
   └── config.json   [dim]Your settings[/dim]

[bold]2. Start an investigation:[/bold]

   [green]# File a FOIA request[/green]
   openfoia request new --agency FBI --subject "Records on X" \\
     --body "I request..." --name "Your Name" --email you@email.com

   [green]# Or search existing completed requests (46k+ on MuckRock)[/green]
   openfoia records search "EPA water contamination" --source muckrock
   openfoia records download 68490 --ingest

[bold]3. Analyze documents:[/bold]

   [green]# Ingest your own files (PDFs, DOCX, TXT)[/green]
   openfoia docs ingest ./my-documents/

   [green]# Extract entities (people, orgs, money, dates)[/green]
   openfoia analyze extract <document-id>

   [green]# Build and view a relationship graph[/green]
   openfoia analyze graph --name my-investigation --view

[bold]4. Cross-reference against public databases:[/bold]

   [green]# Check every entity against MuckRock, SEC, OpenSanctions, etc.[/green]
   openfoia crossref

[bold]5. Work with other tools:[/bold]

   [green]# Export for Aleph/OpenAleph/OpenSanctions[/green]
   openfoia analyze export -o data.ftm.json

   [green]# Import from those tools[/green]
   openfoia analyze import colleague-data.ftm.json

[bold]6. Stay safe:[/bold]

   [green]# Encrypt your database[/green]
   openfoia db encrypt --password <secret>

   [green]# When you're done — destroy everything[/green]
   openfoia purge --secure --yes

   [green]# For maximum safety: run from an encrypted USB or Tails OS[/green]
   [dim]See docs/TAILS.md, docs/USB.md, docs/AIRGAP.md[/dim]

[bold]7. Custom entity types for your investigation:[/bold]

   [green]# Add patterns specific to what you're looking for[/green]
   openfoia entities add -n CONTRACT_NUMBER -p '\\b[A-Z]{{2,4}}-\\d{{4,}}-\\d{{4,}}\\b' -d "Federal contracts"

   [green]# Import from a spreadsheet (AI helps if columns are messy)[/green]
   openfoia entities import patterns.csv

[bold]Need help?[/bold]
   openfoia --help              [dim]All commands[/dim]
   openfoia request --help      [dim]Request commands[/dim]
   openfoia analyze --help      [dim]Analysis commands[/dim]
   [dim]https://github.com/JordanCoin/openfoia[/dim]
""")


@app.command()
def serve(
    port: int = typer.Option(0, "--port", "-p", help="Port to run on (0 = random)"),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to"),
    browser: str | None = typer.Option(
        None, "--browser", "-b", help="Browser to open (safari/firefox/chrome/brave/tor)"
    ),
    private: bool = typer.Option(
        True, "--private/--no-private", help="Open in private/incognito mode"
    ),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open browser automatically"),
    tor: bool = typer.Option(False, "--tor", help="Use Tor (Brave or Tor Browser)"),
):
    """Start the OpenFOIA local server.

    Your data stays on your machine. The server only binds to localhost.
    For maximum privacy, we recommend opening in a private/incognito window.

    The browser is opened at a login link (/login?nonce=...) that works once
    and expires after 60 seconds; it logs the browser in with a session cookie.
    Press Enter in this terminal to print a fresh link (for another browser,
    or after the first one expired).

    Examples:
        openfoia serve                    # Auto-select browser, private mode
        openfoia serve --browser firefox  # Use Firefox
        openfoia serve --tor              # Use Tor Browser or Brave with Tor
        openfoia serve --no-browser       # Just print the login link
    """
    from .browser import (  # lazy: startup time
        BrowserType,
        detect_browsers,
        launch_browser,
        print_browser_menu,
    )

    # Find available port if not specified
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

    open_browser = None
    if not no_browser:
        browsers = detect_browsers()

        if browser:
            # User specified a browser
            try:
                browser_type = BrowserType(browser.lower())
                target_browser = next((b for b in browsers if b.browser_type == browser_type), None)
            except ValueError:
                rprint(f"[yellow]Unknown browser '{browser}'. Available:[/yellow]")
                print_browser_menu(browsers)
                target_browser = None
        else:
            # Auto-select: prefer privacy-focused browsers
            target_browser = None
            if tor:
                # Prefer Tor Browser, then Brave with Tor
                for b in browsers:
                    if b.browser_type == BrowserType.TOR:
                        target_browser = b
                        break
                    elif b.browser_type == BrowserType.BRAVE:
                        target_browser = b
                        # Don't break - keep looking for actual Tor Browser
            else:
                # Prefer Brave > Firefox > Safari > Chrome
                for bt in [
                    BrowserType.BRAVE,
                    BrowserType.FIREFOX,
                    BrowserType.SAFARI,
                    BrowserType.CHROME,
                ]:
                    for b in browsers:
                        if b.browser_type == bt:
                            target_browser = b
                            break
                    if target_browser:
                        break

        if target_browser:
            mode = "Tor" if tor else ("private" if private else "normal")
            rprint(f"[green]Opening {target_browser.name} ({mode} mode)...[/green]")

            def open_browser(url: str) -> None:
                # The link is a single-use login nonce, not the session token:
                # the browser's command line is readable by every local account.
                launch_browser(url, target_browser, private=private, tor_mode=tor)

        else:
            rprint("[yellow]No browser auto-selected. Open the login link below.[/yellow]")

    # Start the server. It prints the login link (single-use, short-lived) and
    # opens the browser once it is accepting connections.
    from .db import get_data_dir  # lazy: startup time
    from .server import run_server  # lazy: startup time

    run_server(host=host, port=port, data_dir=get_data_dir(), open_browser=open_browser)


CSV_HELP = "Write the table as CSV to this file ('-' for stdout)"
_csv_stdout = None  # the real stdout while a `--csv -` command prints everything else to stderr


def _csv_stdout_safe(func):
    """With `--csv -`, route the command's other output to stderr so stdout is only CSV."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _csv_stdout
        if kwargs.get("csv_path") != "-":
            return func(*args, **kwargs)
        _csv_stdout = sys.stdout
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return func(*args, **kwargs)
        finally:
            _csv_stdout = None

    return wrapper


def _csv_cell(v: str) -> str:
    """Make one upstream value safe for a spreadsheet and a terminal.

    Drops control characters (terminal escape injection via `cat file.csv`) and
    prefixes formula triggers so a cell like =HYPERLINK(...) never executes.
    A bare "-" placeholder is left alone.
    """
    v = "".join(ch for ch in v if ch.isprintable() or ch in " \t")
    if v != "-" and v.lstrip(" \t")[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def _emit_table(table: Table, csv_path: str | None) -> None:
    """Print a Rich table, or write its columns and rows as CSV (csv_path '-' = stdout)."""
    if not csv_path:
        console.print(table)
        return
    columns = [
        [_csv_cell(c.plain if isinstance(c, Text) else str(c)) for c in col.cells]
        for col in table.columns
    ]
    rows = [list(row) for row in zip(*columns, strict=True)]
    header = [_csv_cell(str(col.header)) for col in table.columns]
    if csv_path == "-":
        csv.writer(_csv_stdout or sys.stdout).writerows([header, *rows])
        return
    from .exports import record_export  # lazy: startup time

    record_export(csv_path)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([header, *rows])
    rprint(f"[green]Wrote {len(rows)} rows to {csv_path}[/green]")


# Subcommands
request_app = typer.Typer(help="Manage FOIA requests")
docs_app = typer.Typer(help="Process documents")
campaign_app = typer.Typer(help="Manage campaigns")
agency_app = typer.Typer(help="Manage agencies")
jurisdiction_app = typer.Typer(help="Which public-records law applies: federal FOIA or a state act")
analyze_app = typer.Typer(help="Analyze documents")
template_app = typer.Typer(help="Request templates")
records_app = typer.Typer(
    help="Search public records. fec, govinfo and regulations share the rate-limited "
    "DEMO_KEY unless you set a free api.data.gov key: OPENFOIA_API_DATA_GOV_KEY, or "
    '"records": {"_api_data_gov_key": "..."} in config.json.'
)

entities_app = typer.Typer(help="Manage custom entity types")
deadline_app = typer.Typer(help="Track FOIA deadlines")
db_app = typer.Typer(help="Database management")
case_app = typer.Typer(help="Group searches, documents and graphs into an investigation")

app.add_typer(request_app, name="request")
app.add_typer(docs_app, name="docs")
app.add_typer(campaign_app, name="campaign")
app.add_typer(agency_app, name="agency")
app.add_typer(jurisdiction_app, name="jurisdiction")
app.add_typer(analyze_app, name="analyze")
app.add_typer(template_app, name="template")
app.add_typer(records_app, name="records")
app.add_typer(entities_app, name="entities")
app.add_typer(deadline_app, name="deadlines")
app.add_typer(db_app, name="db")
app.add_typer(case_app, name="case")


@db_app.command()
def upgrade(
    revision: str = typer.Argument("head", help="Revision to upgrade to (default: head)"),
):
    """Run database migrations.

    Applies pending alembic migrations to bring the schema up to date.

    Examples:
        openfoia db upgrade          # Upgrade to latest
        openfoia db upgrade head     # Same as above
    """
    from .db import get_db_path, run_migrations  # lazy: startup time

    db_path = get_db_path()
    rprint(f"\n[cyan]Database:[/cyan] {db_path}")
    rprint(f"[cyan]Upgrading to:[/cyan] {revision}")

    if revision != "head":
        rprint("[yellow]Only 'head' is supported for encrypted databases.[/yellow]")

    # Route through run_migrations so an encrypted database is migrated with
    # its key attached. Building a bare sqlite:/// URL here silently failed
    # on encrypted databases.
    run_migrations()

    rprint("[bold green]Database upgraded successfully.[/bold green]\n")


@db_app.command()
def encrypt(
    password: str = typer.Option(
        ...,
        "--password",
        prompt="Encryption password",
        hide_input=True,
        help="Password for AES-256 encryption",
    ),
):
    """Encrypt an existing plaintext database with SQLCipher (AES-256).

    Creates an encrypted copy of the current database, checks it, shreds the
    plaintext original and its WAL/journal files in place, and swaps the copy
    in. No plaintext backup is kept.

    Requires pysqlcipher3: pip install 'openfoia[encryption]'

    Examples:
        openfoia db encrypt   # prompts for the passphrase (keeps it out of shell history)
    """
    from .db import encrypt_database, get_db_path, has_sqlcipher  # lazy: startup time

    if not has_sqlcipher():
        rprint("[bold red]Error:[/bold red] pysqlcipher3 is not installed.")
        rprint("[yellow]Install with: pip install 'openfoia[encryption]'[/yellow]")
        raise typer.Exit(1)

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[bold red]Error:[/bold red] No database found. Run 'openfoia init' first.")
        raise typer.Exit(1)

    rprint(f"\n[cyan]Database:[/cyan] {db_path}")
    rprint("[cyan]Encrypting with AES-256 (SQLCipher)...[/cyan]")

    try:
        encrypt_database(password)
    except Exception as e:
        rprint(f"[bold red]Encryption failed:[/bold red] {_safe(e)}")
        raise typer.Exit(1) from None

    rprint("[bold green]Database encrypted successfully.[/bold green]")
    rprint("[green]Plaintext database and its WAL/journal files were shredded in place.[/green]")
    rprint("")
    rprint("[dim]Set OPENFOIA_DB_PASSWORD env var or pass --password to commands.[/dim]")
    rprint(
        "[dim]No plaintext backup was kept. On SSDs, overwriting is best-effort — "
        "use full-disk encryption. See docs/THREAT_MODEL.md.[/dim]\n"
    )


# === Configuration ===


@app.command()
def config(
    init: bool = typer.Option(False, "--init", help="Initialize configuration"),
    show: bool = typer.Option(False, "--show", help="Show current configuration"),
):
    """Manage OpenFOIA configuration."""
    from .db import get_data_dir  # lazy: startup time

    config_path = get_data_dir() / "config.json"

    if init:
        config_path.parent.mkdir(parents=True, exist_ok=True)

        rprint("[bold]OpenFOIA Configuration Setup[/bold]\n")

        # Collect configuration
        config_data = {}

        # Email settings
        rprint("[cyan]Email Configuration (for sending requests)[/cyan]")
        config_data["email"] = {
            "smtp_host": typer.prompt("SMTP host", default="smtp.gmail.com"),
            "smtp_port": int(typer.prompt("SMTP port", default="587")),
            "smtp_user": typer.prompt("SMTP username (email)"),
            "from_name": typer.prompt("Your name"),
        }

        # Optional: Twilio for fax
        if typer.confirm("Configure Twilio for fax sending?", default=False):
            config_data["twilio"] = {
                "account_sid": typer.prompt("Twilio Account SID"),
                "from_number": typer.prompt("Twilio fax number"),
            }

        # Optional: Lob for mail
        if typer.confirm("Configure Lob for physical mail?", default=False):
            config_data["lob"] = {
                "return_address": {
                    "name": typer.prompt("Return address name"),
                    "address_line1": typer.prompt("Address line 1"),
                    "address_city": typer.prompt("City"),
                    "address_state": typer.prompt("State (2 letter)"),
                    "address_zip": typer.prompt("ZIP code"),
                },
            }

        # AI settings
        rprint("\n[cyan]AI Configuration (for document analysis)[/cyan]")
        ai_provider = typer.prompt("AI provider", default="anthropic")
        config_data["ai"] = {
            "provider": ai_provider,
            "model": typer.prompt("Model", default="claude-sonnet-4-20250514"),
        }

        # OCR settings
        rprint("\n[cyan]OCR Configuration[/cyan]")
        config_data["ocr"] = {
            "backend": typer.prompt("OCR backend (tesseract/google/aws)", default="tesseract"),
        }

        # Save
        config_path.write_text(json.dumps(config_data, indent=2))
        rprint(f"\n[green]Configuration saved to {config_path}[/green]")

    elif show:
        if config_path.exists():
            from .config import redact_secrets  # lazy: startup time

            config_data = json.loads(config_path.read_text())
            # Never print secrets: terminal scrollback outlives the session,
            # and this file can hold the database decryption password.
            rprint(json.dumps(redact_secrets(config_data), indent=2))
            rprint(f"\n[dim]Secrets are masked. File: {config_path}[/dim]")
        else:
            rprint(
                "[yellow]No configuration found. Run 'openfoia config --init' to create one.[/yellow]"
            )
    else:
        rprint("Use --init to create configuration or --show to display it.")


# === Request Commands ===


@request_app.command("new")
def request_new(
    agency: str = typer.Option(..., "--agency", "-a", help="Target agency name or ID"),
    subject: str = typer.Option(..., "--subject", "-s", help="Request subject"),
    body: str | None = typer.Option(None, "--body", "-b", help="Request body (or use --file)"),
    body_file: Path | None = typer.Option(
        None, "--file", "-f", help="File containing request body"
    ),
    method: str = typer.Option("email", "--method", "-m", help="Delivery method (email/fax/mail)"),
    name: str = typer.Option(..., "--name", "-n", help="Your full name"),
    email_addr: str = typer.Option(..., "--email", "-e", help="Your email address"),
):
    """Create a new FOIA request."""
    from .db import get_db_path, get_session, init_db  # lazy: startup time

    if body_file:
        body = body_file.read_text()
    elif not body:
        rprint("[yellow]Enter request body (Ctrl+D when done):[/yellow]")
        body = sys.stdin.read()

    # Ensure database exists
    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Initializing database...[/yellow]")
        init_db()

    with get_session() as session:
        # Find agency
        found = (
            session.query(AgencyModel)
            .filter(
                (AgencyModel.abbreviation.ilike(agency)) | (AgencyModel.name.ilike(f"%{agency}%"))
            )
            .first()
        )

        if not found:
            rprint(
                f"[red]Agency '{agency}' not found. Run 'openfoia agency search {agency}' to search.[/red]"
            )
            raise typer.Exit(1)

        # Get or create user
        user = session.query(User).filter(User.email == email_addr).first()
        if not user:
            user = User(id=str(uuid4()), email=email_addr, name=name)
            session.add(user)
            session.flush()

        # Create request
        req_num = f"REQ-{_utcnow().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"

        try:
            delivery = DeliveryMethod(method.lower())
        except ValueError:
            delivery = DeliveryMethod.EMAIL

        # Capture values before session closes (avoid detached ORM access)
        agency_name = found.name

        request = RequestModel(
            id=str(uuid4()),
            request_number=req_num,
            requester_id=user.id,
            agency_id=found.id,
            subject=subject,
            body=body,
            delivery_method=delivery,
            status=RequestStatus.DRAFT,
            fee_waiver_requested=True,
        )
        session.add(request)

    table = Table(title="New FOIA Request")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Request #", req_num)
    table.add_row("Agency", agency_name)
    table.add_row("Subject", subject)
    table.add_row("Method", method)
    table.add_row("Body", body[:100] + "..." if len(body) > 100 else body)
    console.print(table)

    rprint("\n[green]Request saved to database.[/green]")
    rprint(
        f'[cyan]Use \'openfoia request send --agency {agency} --subject "{subject}" --name "{name}" --email {email_addr}\' to send.[/cyan]'
    )


@request_app.command("list")
@_csv_stdout_safe
def request_list(
    status: str | None = typer.Option(None, "--status", "-s", help="Filter by status"),
    agency: str | None = typer.Option(None, "--agency", "-a", help="Filter by agency"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum results"),
    csv_path: str | None = typer.Option(None, "--csv", help=CSV_HELP),
):
    """List FOIA requests."""
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = session.query(RequestModel).join(AgencyModel)

        if status:
            try:
                status_enum = RequestStatus(status.lower())
                query = query.filter(RequestModel.status == status_enum)
            except ValueError:
                rprint(f"[red]Invalid status '{_safe(status)}'.[/red]")
                raise typer.Exit(1) from None

        if agency:
            query = query.filter(
                (AgencyModel.abbreviation.ilike(agency)) | (AgencyModel.name.ilike(f"%{agency}%"))
            )

        requests = query.order_by(RequestModel.created_at.desc()).limit(limit).all()

        if not requests:
            rprint("[yellow]No requests found.[/yellow]")
            if not csv_path:
                return

        table = Table(title=f"FOIA Requests ({len(requests)} results)")
        table.add_column("Request #", style="cyan")
        table.add_column("Agency")
        table.add_column("Subject", max_width=43, no_wrap=True, overflow="ellipsis")
        table.add_column("Status")
        table.add_column("Sent")
        table.add_column("Days")

        status_colors = {
            "draft": "dim",
            "sent": "cyan",
            "processing": "yellow",
            "complete": "green",
            "denied": "red",
            "appealed": "magenta",
        }

        for r in requests:
            color = status_colors.get(r.status.value, "white")
            sent_str = r.sent_at.strftime("%Y-%m-%d") if r.sent_at else "-"
            days = str(r.days_pending()) if r.sent_at else "-"
            # Subjects of fetched records are the upstream document titles.
            table.add_row(
                safe_cell(r.request_number),
                safe_cell(r.agency.abbreviation or r.agency.name),
                safe_cell(r.subject),
                Text(r.status.value, style=color),
                sent_str,
                days,
            )

        _emit_table(table, csv_path)


@request_app.command("status")
def request_status(
    request_id: str = typer.Argument(..., help="Request ID or number"),
):
    """Check status of a FOIA request."""
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        request = (
            session.query(RequestModel)
            .filter((RequestModel.request_number == request_id) | (RequestModel.id == request_id))
            .first()
        )

        if not request:
            rprint(f"[red]Request '{_safe(request_id)}' not found.[/red]")
            raise typer.Exit(1)

        rprint(f"\n[bold cyan]{_safe(request.request_number)}[/bold cyan]")
        rprint("=" * 50)

        info_table = Table(show_header=False, box=None)
        info_table.add_column("Field", style="cyan", width=20)
        info_table.add_column("Value")

        _row(info_table, "Agency", request.agency.name)
        _row(info_table, "Subject", request.subject)
        info_table.add_row("Status", request.status.value.replace("_", " ").title())
        info_table.add_row(
            "Delivery Method", request.delivery_method.value.replace("_", " ").title()
        )
        info_table.add_row("Created", request.created_at.strftime("%Y-%m-%d %H:%M"))
        if request.sent_at:
            info_table.add_row("Sent", request.sent_at.strftime("%Y-%m-%d %H:%M"))
        if request.acknowledged_at:
            info_table.add_row("Acknowledged", request.acknowledged_at.strftime("%Y-%m-%d %H:%M"))
        from .clocks import request_clocks, rule_text, tolls_of  # lazy: startup time

        clocks = request_clocks(request)
        law = clocks.law
        request.due_date = clocks.response_due  # cache of the recomputed clock
        info_table.add_row(
            "Law",
            f"{law['id']}: {law['statute']['name'] or 'unknown statute'}"
            + (
                " [dim](agency has no jurisdiction; federal FOIA assumed)[/dim]"
                if clocks.fallback
                else ""
            ),
        )
        if clocks.response_due:
            info_table.add_row(
                clocks.response_kind.capitalize() + " due",
                f"{clocks.response_due:%Y-%m-%d} {_days_left(clocks.response_due)}  "
                f"[dim]{clocks.response_reason or rule_text(law)}[/dim]",
            )
        elif request.sent_at and clocks.response_reason:
            info_table.add_row(
                "Appeal decision" if request.appealed_at else "Response due",
                f"no statutory deadline  [dim]{clocks.response_reason}[/dim]",
            )
        if request.extended_at:
            info_table.add_row("Extension taken", f"{request.extended_at:%Y-%m-%d}")
        for start, until in tolls_of(request):
            info_table.add_row("Clock tolled", f"{start:%Y-%m-%d} to {until:%Y-%m-%d}")
        if request.denied_at:
            info_table.add_row("Denied", f"{request.denied_at:%Y-%m-%d}")
        if clocks.appeal_due:
            info_table.add_row(
                "Appeal due",
                f"{clocks.appeal_due:%Y-%m-%d} {_days_left(clocks.appeal_due)}  "
                f"[dim]{law['appeal']['rule_text'] or ''}[/dim]",
            )
        elif clocks.appeal_reason:
            info_table.add_row("Appeal due", clocks.appeal_reason)
        if request.appealed_at:
            info_table.add_row("Appealed", f"{request.appealed_at:%Y-%m-%d}")
        if clocks.still_interested_due:
            info_table.add_row(
                "Still interested",
                f"reply due {clocks.still_interested_due:%Y-%m-%d} "
                f"{_days_left(clocks.still_interested_due)}",
            )
        if request.agency_tracking_number:
            _row(info_table, "Tracking #", request.agency_tracking_number)
        if request.fee_estimate:
            info_table.add_row("Fee Estimate", f"${request.fee_estimate:,.2f}")
        if request.fee_paid:
            info_table.add_row("Fee Paid", f"${request.fee_paid:,.2f}")
        if request.sent_at:
            info_table.add_row("Days Pending", str(request.days_pending()))

        console.print(info_table)

        # Show timeline events
        events = (
            session.query(TimelineEvent)
            .filter(TimelineEvent.request_id == request.id)
            .order_by(TimelineEvent.occurred_at)
            .all()
        )

        if events:
            rprint("\n[bold]Timeline[/bold]")
            timeline_table = Table()
            timeline_table.add_column("Event", style="cyan")
            timeline_table.add_column("Date")
            timeline_table.add_column("Details")

            for event in events:
                _row(
                    timeline_table,
                    event.event_type,
                    event.occurred_at.strftime("%Y-%m-%d %H:%M"),
                    event.description,
                )
            console.print(timeline_table)

        rprint("")


MARK_EVENTS = (
    "acknowledged",
    "extended",
    "tolled",
    "received",
    "denied",
    "appealed",
    "still-interested",
    "replied",
    "closed",
)


@request_app.command("mark")
def request_mark(
    request_id: str = typer.Argument(..., help="Request ID or number"),
    event: str = typer.Argument(..., help="One of: " + ", ".join(MARK_EVENTS)),
    on: str | None = typer.Option(
        None, "--on", help="Date it happened, YYYY-MM-DD (default today)"
    ),
    until: str | None = typer.Option(None, "--until", help="tolled: date the clock resumes"),
    due: str | None = typer.Option(None, "--due", help="still-interested: date your reply is due"),
    force: bool = typer.Option(False, "--force", help="appealed: allow without a recorded denial"),
):
    """Record a lifecycle event and recompute the clocks from the jurisdiction's rule.

    Examples:
        openfoia request mark REQ-1 acknowledged --on 2026-10-20
        openfoia request mark REQ-1 extended
        openfoia request mark REQ-1 tolled --until 2026-11-01
        openfoia request mark REQ-1 denied
        openfoia request mark REQ-1 still-interested --due 2026-11-15
        openfoia request mark REQ-1 replied
    """
    from .clocks import request_clocks  # lazy: startup time
    from .db import get_session  # lazy: startup time

    if event not in MARK_EVENTS:
        rprint(
            f"[red]Unknown event '{_safe(event)}'. Choose from: {_safe(', '.join(MARK_EVENTS))}[/red]"
        )
        raise typer.Exit(2)
    when = _parse_day(on, "--on") or _utcnow()
    until_at = _parse_day(until, "--until")
    due_at = _parse_day(due, "--due")
    if event == "tolled" and not until_at:
        rprint("[red]tolled needs --until <date>.[/red]")
        raise typer.Exit(2)
    if event == "tolled" and until_at <= when:
        rprint("[red]--until must be after the toll start (--on, default today).[/red]")
        raise typer.Exit(2)
    if event == "still-interested" and not due_at:
        rprint("[red]still-interested needs --due <date>.[/red]")
        raise typer.Exit(2)

    _require_db()
    with get_session() as session:
        req = (
            session.query(RequestModel)
            .filter((RequestModel.request_number == request_id) | (RequestModel.id == request_id))
            .first()
        )
        if not req:
            rprint(f"[red]Request '{_safe(request_id)}' not found.[/red]")
            raise typer.Exit(1)
        if event != "closed" and not req.sent_at:
            rprint("[red]Request has not been sent; no clock is running.[/red]")
            raise typer.Exit(1)
        if req.status == RequestStatus.CLOSED and event in ("denied", "appealed", "received"):
            rprint("[red]Request is closed; reopen it before recording that.[/red]")
            raise typer.Exit(1)
        if event == "appealed" and req.status != RequestStatus.DENIED and not force:
            rprint("[red]No denial recorded; mark it denied first, or pass --force.[/red]")
            raise typer.Exit(1)
        detail = ""
        if event == "acknowledged":
            req.acknowledged_at = when
            if req.status == RequestStatus.SENT:
                req.status = RequestStatus.ACKNOWLEDGED
        elif event == "extended":
            req.extended_at = when
        elif event == "tolled":
            extra = dict(req.extra_data or {})
            tolls = [t for t in extra.get("tolls", []) if t[0] != when.isoformat()]
            tolls.append([when.isoformat(), until_at.isoformat()])
            extra["tolls"] = tolls
            req.extra_data = extra
            req.tolled_until = until_at
        elif event == "received":
            req.completed_at = when
            if req.status not in (RequestStatus.DENIED, RequestStatus.APPEALED):
                req.status = RequestStatus.COMPLETE
        elif event == "denied":
            req.denied_at = when
            req.status = RequestStatus.DENIED
        elif event == "appealed":
            req.appealed_at = when
            req.status = RequestStatus.APPEALED
        elif event == "still-interested":
            req.still_interested_due_at = due_at
            detail = f"reply by {due_at:%Y-%m-%d} or the request closes"
        elif event == "replied":
            req.still_interested_due_at = None
        elif event == "closed":
            req.status = RequestStatus.CLOSED
            req.completed_at = req.completed_at or when

        clocks = request_clocks(req)
        req.due_date = clocks.response_due
        if event in ("extended", "tolled"):
            detail = (
                f"response due {clocks.response_due:%Y-%m-%d}"
                if clocks.response_due
                else clocks.response_reason
            )
        elif event == "denied":
            detail = (
                f"appeal due {clocks.appeal_due:%Y-%m-%d}"
                if clocks.appeal_due
                else clocks.appeal_reason
            )
        elif event == "appealed":
            detail = (
                f"appeal decision due {clocks.response_due:%Y-%m-%d}"
                if clocks.response_due
                else clocks.response_reason
            )

        existing = (
            session.query(TimelineEvent)
            .filter(TimelineEvent.request_id == req.id, TimelineEvent.event_type == event)
            .first()
        )
        if existing and event != "tolled":
            existing.occurred_at = when
            existing.description = detail or event
        else:
            session.add(
                TimelineEvent(
                    request_id=req.id,
                    event_type=event,
                    description=detail or event,
                    occurred_at=when,
                    extra_data={"jurisdiction": clocks.law["id"]},
                )
            )
        rprint(
            f"[green]{_safe(req.request_number)}[/green] {_safe(event)}"
            + (f": {_safe(detail)}" if detail else "")
        )
        if clocks.fallback:
            rprint("[dim]Agency has no jurisdiction set; federal FOIA clocks assumed.[/dim]")


@request_app.command("send")
def request_send(
    agency: str = typer.Option(..., "--agency", "-a", help="Target agency (name or abbreviation)"),
    subject: str = typer.Option(..., "--subject", "-s", help="Request subject"),
    body: str | None = typer.Option(None, "--body", "-b", help="Request body text"),
    body_file: Path | None = typer.Option(
        None, "--file", "-f", help="File containing request body"
    ),
    template: str | None = typer.Option(
        None, "--template", "-t", help="Use template (standard/self)"
    ),
    name: str = typer.Option(..., "--name", "-n", help="Your full name"),
    email: str = typer.Option(..., "--email", "-e", help="Your email address"),
    method: str = typer.Option("email", "--method", "-m", help="Delivery method (email/fax/mail)"),
    to_address: str | None = typer.Option(
        None, "--to", help="Override recipient address (email, fax number, or mailing address)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be sent without sending"
    ),
):
    """Send a FOIA request to an agency.

    Supports three delivery methods:
    - email (default): Send via SMTP. Requires SMTP configuration.
    - fax: Send via Twilio fax. Requires Twilio credentials.
    - mail: Send physical letter via Lob. Requires Lob API key.

    Examples:
        # Send via email (default)
        openfoia request send -a FBI -s "Records on X" -b "I request..." -n "Jane Doe" -e jane@example.com

        # Send via fax
        openfoia request send -a FBI -s "Records on X" -t standard -n "Jane Doe" -e jane@example.com -m fax

        # Send via physical mail (certified)
        openfoia request send -a EPA -s "Pollution data" -t standard -n "Jane Doe" -e jane@example.com -m mail

        # Override recipient fax number
        openfoia request send -a FBI -s "Records" -t standard -n "Jane" -e j@x.com -m fax --to "+12025551234"

        # Dry run (preview without sending)
        openfoia request send -a FBI -s "Test" -t standard -n "Test User" -e test@example.com --dry-run
    """
    from .db import get_db_path, get_session  # lazy: startup time
    from .gateways.base import DeliveryPayload  # lazy: startup time

    if method not in ("email", "fax", "mail"):
        rprint(f"[red]Unknown method '{_safe(method)}'. Use: email, fax, mail[/red]")
        raise typer.Exit(1)

    # Get agency from database
    agency_name = agency
    agency_email = None
    agency_fax = None
    agency_address = None

    db_path = get_db_path()
    if db_path.exists():
        with get_session() as session:
            found = (
                session.query(Agency)
                .filter((Agency.abbreviation.ilike(agency)) | (Agency.name.ilike(f"%{agency}%")))
                .first()
            )
            if found:
                agency_name = found.name
                agency_email = found.foia_email
                agency_fax = getattr(found, "foia_fax", None)
                agency_address = getattr(found, "foia_address", None)

    # Determine recipient address based on method
    if method == "email":
        recipient = to_address or agency_email
        if not recipient:
            rprint(f"[red]No FOIA email found for '{_safe(agency)}'. Specify with --to.[/red]")
            raise typer.Exit(1)
    elif method == "fax":
        recipient = to_address or agency_fax
        if not recipient:
            rprint(f"[red]No FOIA fax number found for '{_safe(agency)}'. Specify with --to.[/red]")
            rprint("[dim]Example: --to '+12025551234'[/dim]")
            raise typer.Exit(1)
    elif method == "mail":
        recipient = to_address or agency_address
        if not recipient:
            rprint(
                f"[red]No FOIA mailing address found for '{_safe(agency)}'. Specify with --to.[/red]"
            )
            rprint("[dim]Format: 'Name\\nStreet\\nCity, ST ZIP' (use \\n for newlines)[/dim]")
            raise typer.Exit(1)
        # Handle escaped newlines from CLI
        recipient = recipient.replace("\\n", "\n")

    # Get body content
    if template:
        from .templates import (  # lazy: startup time
            RequestDetails,
            RequesterInfo,
            records_about_self,
            standard_request,
        )

        requester = RequesterInfo(name=name, email=email)
        details = RequestDetails(subject=subject, description=subject)

        if template == "standard":
            body = standard_request(requester=requester, agency_name=agency_name, details=details)
        elif template == "self":
            body = records_about_self(
                requester=requester, agency_name=agency_name, record_type=subject
            )
        else:
            rprint(f"[red]Unknown template '{_safe(template)}'. Use: standard, self[/red]")
            raise typer.Exit(1)
    elif body_file:
        body = body_file.read_text()
    elif not body:
        rprint("[yellow]Enter request body (Ctrl+D when done):[/yellow]")
        body = sys.stdin.read()

    # Build payload
    payload = DeliveryPayload(
        recipient_name=f"FOIA Officer at {agency_name}",
        recipient_address=recipient,
        subject=subject,
        body=body,
        return_address=f"{name}\n{email}",
    )

    # Preview
    method_labels = {"email": "Email", "fax": "Fax", "mail": "Physical Mail"}
    rprint(f"\n[bold cyan]FOIA Request ({method_labels[method]})[/bold cyan]")
    rprint("─" * 50)
    rprint(f"[cyan]Method:[/cyan] {method_labels[method]}")
    rprint(f"[cyan]To:[/cyan] {_safe(recipient)}")
    rprint(f"[cyan]Subject:[/cyan] FOIA Request: {_safe(subject)}")
    rprint(f"[cyan]From:[/cyan] {_safe(name)} <{_safe(email)}>")

    # Show cost estimate for paid methods
    if method == "fax":
        from .gateways.fax import TwilioFaxGateway  # lazy: startup time

        est_pages = max(1, len(body) // 3000 + 1) + (1 if payload.cover_page else 0)
        est_cost = est_pages * TwilioFaxGateway.COST_PER_PAGE_CENTS
        rprint(f"[cyan]Est. Cost:[/cyan] ${est_cost / 100:.2f} ({est_pages} pages @ $0.07/page)")
    elif method == "mail":
        from .gateways.mail import LobMailGateway  # lazy: startup time

        est_pages = max(1, len(body) // 3000 + 1)
        est_cost = 63 + max(0, (est_pages - 1) * 15) + 400 + 50  # certified + return envelope
        rprint(f"[cyan]Est. Cost:[/cyan] ${est_cost / 100:.2f} ({est_pages} pages, certified mail)")

    rprint("─" * 50)

    if dry_run:
        rprint(f"\n[yellow]DRY RUN ({method_labels[method]}) - Request not sent[/yellow]")
        rprint("\n[dim]Request body preview:[/dim]")
        preview = body[:500] + "..." if len(body) > 500 else body
        rprint(preview)
        return

    # Load config
    from .db import get_data_dir  # lazy: startup time

    config_path = get_data_dir() / "config.json"

    config = {}
    if config_path.exists():
        with contextlib.suppress(json.JSONDecodeError):
            config = json.loads(config_path.read_text())

    # Build and send via the appropriate gateway
    if method == "email":
        from .gateways.email import EmailGateway  # lazy: startup time

        smtp_user = os.environ.get("OPENFOIA_SMTP_USER")
        smtp_password = os.environ.get("OPENFOIA_SMTP_PASSWORD")

        if not smtp_user or not smtp_password:
            smtp_config = config.get("email", {})
            smtp_user = smtp_user or smtp_config.get("smtp_user")
            smtp_password = smtp_password or smtp_config.get("smtp_password")

        if not smtp_user or not smtp_password:
            rprint("[red]SMTP credentials not configured.[/red]")
            rprint("[dim]Set OPENFOIA_SMTP_USER and OPENFOIA_SMTP_PASSWORD env vars[/dim]")
            rprint("[dim]Or run 'openfoia config --init'[/dim]")
            raise typer.Exit(1)

        gateway = EmailGateway(
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            from_email=email,
            from_name=name,
        )

    elif method == "fax":
        from .gateways.fax import TwilioFaxGateway  # lazy: startup time

        account_sid = os.environ.get("OPENFOIA_TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("OPENFOIA_TWILIO_AUTH_TOKEN")
        from_number = os.environ.get("OPENFOIA_TWILIO_FROM_NUMBER")

        if not account_sid or not auth_token:
            fax_config = config.get("fax", {})
            account_sid = (
                account_sid or fax_config.get("account_sid") or fax_config.get("_account_sid")
            )
            auth_token = auth_token or fax_config.get("auth_token") or fax_config.get("_auth_token")
            from_number = from_number or fax_config.get("from_number")

        if not account_sid or not auth_token or not from_number:
            rprint("[red]Twilio credentials not configured for fax delivery.[/red]")
            rprint("[dim]Set environment variables:[/dim]")
            rprint("[dim]  OPENFOIA_TWILIO_ACCOUNT_SID[/dim]")
            rprint("[dim]  OPENFOIA_TWILIO_AUTH_TOKEN[/dim]")
            rprint("[dim]  OPENFOIA_TWILIO_FROM_NUMBER[/dim]")
            rprint("[dim]Or run 'openfoia config --init'[/dim]")
            raise typer.Exit(1)

        gateway = TwilioFaxGateway(
            account_sid=account_sid,
            auth_token=auth_token,
            from_number=from_number,
            media_base_url=os.environ.get("OPENFOIA_MEDIA_BASE_URL"),
        )

    elif method == "mail":
        from .gateways.mail import LobMailGateway  # lazy: startup time

        lob_api_key = os.environ.get("OPENFOIA_LOB_API_KEY")

        if not lob_api_key:
            mail_config = config.get("mail", {})
            lob_api_key = lob_api_key or mail_config.get("api_key") or mail_config.get("_api_key")

        if not lob_api_key:
            rprint("[red]Lob API key not configured for physical mail delivery.[/red]")
            rprint("[dim]Set environment variable: OPENFOIA_LOB_API_KEY[/dim]")
            rprint("[dim]Or run 'openfoia config --init'[/dim]")
            raise typer.Exit(1)

        # Return address from config or environment
        return_addr = config.get("mail", {}).get("return_address", {})
        if not return_addr:
            rprint("[red]Return address not configured for physical mail.[/red]")
            rprint("[dim]Add to ~/.openfoia/config.json under mail.return_address:[/dim]")
            rprint(
                '[dim]  {"name": "...", "address_line1": "...", "address_city": "...", "address_state": "...", "address_zip": "..."}[/dim]'
            )
            raise typer.Exit(1)

        gateway = LobMailGateway(
            api_key=lob_api_key,
            return_address=return_addr,
        )

    # No delivery gateway is routed through Tor. Say so before anything is sent.
    from .config import load_config  # lazy: startup time

    if load_config().network.tor:
        from .gateways.email import TOR_SMTP_WARNING  # lazy: startup time

        tor_warning = (
            TOR_SMTP_WARNING
            if method == "email"
            else (
                f"Tor mode is on, but {method_labels[method]} is sent through "
                f"{'Twilio' if method == 'fax' else 'Lob'} directly, not through Tor: "
                "that service sees your real IP address."
            )
        )
        rprint(f"\n[yellow]WARNING: {_safe(tor_warning)}[/yellow]")
        if sys.stdin.isatty() and not typer.confirm("Send anyway?", default=False):
            rprint("[dim]Not sent.[/dim]")
            raise typer.Exit(1)

    rprint(f"\n[cyan]Sending via {method_labels[method]}...[/cyan]")
    result = asyncio.run(gateway.send(payload))

    if result.success:
        rprint("[bold green]Request sent![/bold green]")
        rprint(f"  Reference: {_safe(result.reference_id)}")
        rprint(f"  Sent at: {_safe(result.sent_at)}")
        if result.cost_cents:
            rprint(f"  Estimated cost: ${result.cost_cents / 100:.2f}")

        # Show method-specific details
        if result.metadata:
            if method == "fax" and result.metadata.get("estimated_pages"):
                rprint(f"  Estimated pages: {_safe(result.metadata['estimated_pages'])}")
            if method == "mail":
                if result.metadata.get("tracking_number"):
                    rprint(f"  Tracking #: {_safe(result.metadata['tracking_number'])}")
                if result.metadata.get("expected_delivery_date"):
                    rprint(
                        f"  Expected delivery: {_safe(result.metadata['expected_delivery_date'])}"
                    )

        # Update the matching Request in the DB if one exists
        from .db import get_db_path, get_session  # lazy: startup time

        db_path = get_db_path()
        if db_path.exists():
            with get_session() as session:
                # Find the draft request matching this agency + subject
                req = (
                    session.query(RequestModel)
                    .join(AgencyModel)
                    .filter(
                        (AgencyModel.abbreviation.ilike(agency))
                        | (AgencyModel.name.ilike(f"%{agency}%")),
                        RequestModel.subject == subject,
                        RequestModel.status == RequestStatus.DRAFT,
                    )
                    .first()
                )
                if req:
                    req.status = RequestStatus.SENT
                    req.sent_at = _utcnow()
                    req.delivery_reference = result.reference_id
                    from .clocks import request_clocks  # lazy: startup time

                    req.due_date = request_clocks(req).response_due
                    if req.due_date is None:
                        rprint(f"  [yellow]{NO_CLOCK}; no due date set.[/yellow]")

        rprint("\n[dim]Save this reference to track your request.[/dim]")
    else:
        rprint("[bold red]Failed to send[/bold red]")
        rprint(f"  Error: {_safe(result.error_message)}")
        raise typer.Exit(1)


# === Document Commands ===


@docs_app.command("ingest")
def docs_ingest(
    path: Path = typer.Argument(..., help="File or directory to ingest"),
    request_id: str | None = typer.Option(None, "--request", "-r", help="Associate with request"),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive", help="Recurse into directories"
    ),
    ocr: bool = typer.Option(False, "--ocr", help="Run OCR after ingestion"),
    keep_metadata: bool = typer.Option(
        False, "--keep-metadata", help="Skip automatic metadata stripping"
    ),
):
    """Ingest documents into the system.

    Copies documents to ~/.openfoia/docs/ and tracks them in the database.
    Strips sensitive metadata (EXIF, author, editing tool, timestamps) from
    the stored copy unless --keep-metadata is given: PDF, DOCX and JPEG/PNG/TIFF
    always, and OpenDocument, XLSX, PPTX, EPUB, audio, video and more when mat2
    is installed. Each file's line says whether its metadata was stripped; a
    file that could not be cleaned is reported, never shown as done.
    Supports PDF, DOCX, TXT, and image files.

    Examples:
        openfoia docs ingest ./response.pdf
        openfoia docs ingest ./foia-docs/ --ocr
        openfoia docs ingest ./evidence/ -r REQ-2026-001
        openfoia docs ingest ./doc.pdf --keep-metadata
    """
    from .db import ensure_db, get_data_dir  # lazy: startup time
    from .pipeline.ingest import DocumentIngester  # lazy: startup time
    from .pipeline.metadata import describe  # lazy: startup time

    ensure_db()

    storage_path = get_data_dir() / "docs"
    ingester = DocumentIngester(storage_path=storage_path)

    results = []
    still_carrying: list[str] = []

    def report(name: str, result) -> None:
        clean, note = describe(result.metadata.get("metadata_stripped"))
        if clean:
            rprint(f"[green]✓[/green] {_safe(name)} → {result.document_id[:8]}... ({_safe(note)})")
        else:
            still_carrying.append(name)
            rprint(
                f"[yellow]⚠[/yellow] {_safe(name)} → {result.document_id[:8]}... "
                f"[yellow]({_safe(note)})[/yellow]"
            )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        if path.is_file():
            task = progress.add_task(f"Ingesting {_safe(path.name)}...", total=None)
            try:
                result = asyncio.run(
                    ingester.ingest_file(
                        path,
                        request_id=request_id,
                        strip_metadata=not keep_metadata,
                    )
                )
                results.append(result)
                report(path.name, result)
            except Exception as e:
                rprint(f"[red]✗[/red] {_safe(path.name)}: {_safe(e)}")
        else:
            # Directory
            patterns = [
                "*.pdf",
                "*.PDF",
                "*.doc",
                "*.docx",
                "*.txt",
                "*.jpg",
                "*.png",
                "*.tiff",
                "*.msg",
            ]
            files = []
            for pattern in patterns:
                if recursive:
                    files.extend(path.rglob(pattern))
                else:
                    files.extend(path.glob(pattern))

            if not files:
                rprint(f"[yellow]No supported files found in {_safe(path)}[/yellow]")
                return

            task = progress.add_task("Ingesting...", total=len(files))

            for file in files:
                progress.update(task, description=f"Ingesting {_safe(file.name)}...")
                try:
                    result = asyncio.run(
                        ingester.ingest_file(
                            file,
                            request_id=request_id,
                            strip_metadata=not keep_metadata,
                        )
                    )
                    results.append(result)
                    report(file.name, result)
                except Exception as e:
                    rprint(f"[red]✗[/red] {_safe(file.name)}: {_safe(e)}")
                progress.advance(task)

    # Persist Document rows to database
    if results:
        from .db import get_session  # lazy: startup time
        from .doctext import replace_pages  # lazy: startup time

        with get_session() as session:
            for r in results:
                doc = Document(
                    id=r.document_id,
                    request_id=request_id or "",
                    doc_type=DocumentType.FULL_RESPONSE,
                    filename=r.filename,
                    file_path=r.file_path,
                    file_size=r.file_size,
                    mime_type=r.mime_type,
                    page_count=r.page_count,
                    extracted_text=r.extracted_text,
                    ocr_completed=bool(r.extracted_text),
                )
                # Check if request_id is valid, otherwise create without it
                if not request_id:
                    # Create a placeholder request for unassociated documents
                    user = session.query(User).first()
                    if not user:
                        user = User(
                            id=str(uuid4()), email="local@openfoia.local", name="Local User"
                        )
                        session.add(user)
                        session.flush()

                    agency = session.query(Agency).first()
                    placeholder_req = Request(
                        id=str(uuid4()),
                        request_number=f"INGEST-{uuid4().hex[:6].upper()}",
                        requester_id=user.id,
                        agency_id=agency.id if agency else user.id,
                        subject=f"Document ingest: {r.filename}",
                        body="Auto-created from openfoia docs ingest",
                        delivery_method=DeliveryMethod.EMAIL,
                        status=RequestStatus.DRAFT,
                    )
                    session.add(placeholder_req)
                    session.flush()
                    doc.request_id = placeholder_req.id
                session.add(doc)
                if r.extracted_text:
                    replace_pages(session, doc.id, r.pages or [r.extracted_text], r.verdicts)

    # Summary
    rprint(f"\n[green]✓ Ingested {len(results)} documents[/green]")

    total_pages = sum(r.page_count or 0 for r in results)
    total_size = sum(r.file_size for r in results)
    rprint(f"  Total pages: {total_pages}")
    rprint(f"  Total size: {total_size / 1024 / 1024:.1f} MB")
    rprint(f"  Storage: {_safe(storage_path)}")

    if keep_metadata:
        rprint("  Metadata: kept as received (--keep-metadata)")
    elif results:
        clean = len(results) - len(still_carrying)
        rprint(f"  Metadata: {clean} clean, {len(still_carrying)} still carrying metadata")
        if still_carrying:
            rprint(
                f"\n[yellow]{len(still_carrying)} stored "
                f"{'copy still carries' if len(still_carrying) == 1 else 'copies still carry'} "
                f"the metadata {'it' if len(still_carrying) == 1 else 'they'} arrived with "
                "(author, device, location, timestamps): "
                f"{_safe(', '.join(still_carrying))}. The files you ingested were never modified. "
                "Remove it before sharing these documents.[/yellow]"
            )

    # Run OCR if requested
    if ocr and results:
        rprint("\n[cyan]Running OCR...[/cyan]")
        from .db import get_session  # lazy: startup time
        from .doctext import replace_pages  # lazy: startup time
        from .pipeline.ocr import OCREngine  # lazy: startup time

        engine = OCREngine(backend="tesseract")

        for result in results:
            if result.mime_type == "application/pdf":
                verdicts = result.verdicts or []
                if verdicts and all(v and v.startswith("ocr") for v in verdicts):
                    rprint(f"  OCR: {_safe(result.filename)} already OCR'd during ingest")
                    continue
                rprint(f"  OCR: {_safe(result.filename)}...")
                try:
                    ocr_result = asyncio.run(engine.process_pdf(result.file_path, force_ocr=True))
                    rprint(
                        f"    [green]✓[/green] {ocr_result.page_count} pages, {ocr_result.confidence:.1%} confidence"
                    )
                    if ocr_result.text.strip():
                        with get_session() as session:
                            doc = session.get(Document, result.document_id)
                            if doc is None:
                                continue
                            doc.extracted_text = ocr_result.text
                            doc.ocr_completed = True
                            replace_pages(
                                session,
                                doc.id,
                                [p["text"] for p in ocr_result.pages],
                                [p.get("verdict") for p in ocr_result.pages],
                            )
                except Exception as e:
                    rprint(f"    [red]✗[/red] OCR failed: {_safe(e)}")


@docs_app.command("ocr")
def docs_ocr(
    file_path: Path = typer.Argument(..., help="PDF file to OCR"),
    backend: str = typer.Option(
        "tesseract", "--backend", "-b", help="OCR backend (tesseract/google/aws)"
    ),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output text file"),
):
    """Run OCR on a PDF document.

    Extracts text from scanned PDFs using Tesseract (default) or cloud APIs.

    Requirements:
        - Tesseract: brew install tesseract (macOS) or apt install tesseract-ocr (Linux)
        - pdf2image: requires poppler (brew install poppler)

    Examples:
        openfoia docs ocr response.pdf
        openfoia docs ocr scanned.pdf -o extracted.txt
        openfoia docs ocr document.pdf --backend google
    """
    from .pipeline.ocr import OCREngine, RedactionDetector  # lazy: startup time

    if not file_path.exists():
        rprint(f"[red]File not found: {_safe(file_path)}[/red]")
        raise typer.Exit(1)

    if file_path.suffix.lower() != ".pdf":
        rprint("[yellow]Warning: OCR works best on PDF files[/yellow]")

    engine = OCREngine(backend=backend)
    detector = RedactionDetector()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Running OCR...", total=None)

        try:
            result = asyncio.run(engine.process_pdf(file_path))
        except ImportError as e:
            rprint(f"[red]Missing dependency: {_safe(e)}[/red]")
            rprint("[dim]Install with: pip install pytesseract pdf2image[/dim]")
            rprint("[dim]Also need: brew install tesseract poppler (macOS)[/dim]")
            raise typer.Exit(1) from None
        except Exception as e:
            rprint(f"[red]OCR failed: {_safe(e)}[/red]")
            raise typer.Exit(1) from None

        progress.update(task, description="Detecting redactions...")
        redactions = asyncio.run(detector.analyze(result.text, file_path))

    # Results
    rprint("\n[bold green]✓ OCR Complete[/bold green]")
    rprint("─" * 50)

    table = Table(show_header=False, box=None)
    table.add_column("Metric", style="cyan", width=20)
    table.add_column("Value")

    table.add_row("Pages", str(result.page_count))
    table.add_row("Confidence", f"{result.confidence:.1%}")
    table.add_row("Characters", f"{len(result.text):,}")
    table.add_row("Backend", backend)

    if redactions["exemptions_cited"]:
        exemptions = ", ".join(e["code"] for e in redactions["exemptions_cited"])
        table.add_row("Exemptions Found", exemptions)

    console.print(table)

    # Output
    if output:
        from .exports import record_export  # lazy: startup time

        record_export(output)
        output.write_text(result.text)
        rprint(f"\n[green]Text saved to {output}[/green]")
    else:
        rprint("\n[dim]Use --output to save extracted text[/dim]")

    # Show redaction details if found
    if redactions["exemptions_cited"]:
        rprint("\n[yellow]⚠️  Exemptions cited in document:[/yellow]")
        for ex in redactions["exemptions_cited"]:
            rprint(f"  • {_safe(ex['code'])}: {_safe(ex['description'])} ({ex['count']}x)")


def _open_docs_db() -> None:
    """Exit if there is no database (get_session() handles schema upgrades)."""
    from .db import get_db_path  # lazy: startup time

    if not get_db_path().exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)


def _resolve_doc(session, doc_id: str):
    """Find one Document by full id or unique id prefix, or exit."""
    matches = session.query(Document).filter(Document.id.like(f"{doc_id}%")).limit(2).all()
    if len(matches) != 1:
        problem = "not found" if not matches else "is ambiguous; use more characters"
        _echo(f"Document id '{doc_id}' {problem}.", err=True)
        raise typer.Exit(1)
    return matches[0]


NO_CLOCK = "no statutory deadline known for this body"

SINGLE_PAGE_CAVEAT = (
    "fewer pages are stored than the source has, so page numbers here are not "
    "the source's page numbers."
)


def _case_document_ids(session, case_id: str) -> list[str]:
    rows = (
        session.query(Document.id)
        .join(RequestModel, Document.request_id == RequestModel.id)
        .filter(RequestModel.case_id == case_id)
    )
    return [r[0] for r in rows]


def _verdict_summary(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def _doc_source(request_number: str | None) -> str:
    prefix = (request_number or "").split("-", 1)[0]
    return {
        "DC": "documentcloud",
        "MUCKROCK": "muckrock",
        "NARA": "nara",
        "CL": "courtlistener",
    }.get(prefix, "local")


@docs_app.command("list")
def docs_list(
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List stored documents: id, title, pages, source, text length."""
    from .db import get_session  # lazy: startup time

    _open_docs_db()
    with get_session() as session:
        stored = (
            session.query(DocumentPage.document_id, func.count().label("n"))
            .group_by(DocumentPage.document_id)
            .subquery()
        )
        rows = (
            session.query(
                Document.id,
                Document.filename,
                Document.page_count,
                func.length(Document.extracted_text),
                Request.request_number,
                Request.subject,
                stored.c.n,
            )
            .outerjoin(Request, Request.id == Document.request_id)
            .outerjoin(stored, stored.c.document_id == Document.id)
            .order_by(Document.received_at)
            .all()
        )
        verdicts: dict[str, dict[str, int]] = {}
        for doc_id, verdict, n in (
            session.query(DocumentPage.document_id, DocumentPage.verdict, func.count())
            .filter(DocumentPage.verdict.isnot(None))
            .group_by(DocumentPage.document_id, DocumentPage.verdict)
        ):
            verdicts.setdefault(doc_id, {})[verdict] = n

    docs = [
        {
            "id": doc_id,
            "title": subject if _doc_source(num) == "documentcloud" and subject else filename,
            "filename": filename,
            "pages": page_count,
            "stored_pages": n or 0,
            "verdicts": verdicts.get(doc_id, {}),
            "source": _doc_source(num),
            "text_length": text_len or 0,
        }
        for doc_id, filename, page_count, text_len, num, subject, n in rows
    ]
    if as_json:
        typer.echo(safe_json(docs, indent=2))
        return
    if not docs:
        rprint("[yellow]No documents stored yet.[/yellow]")
        return
    table = Table(title=f"Documents ({len(docs)})")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Pages", justify="right")
    table.add_column("Source")
    table.add_column("Text", justify="right")
    table.add_column("Verdicts")
    for d in docs:
        pages = str(d["stored_pages"])
        if d["pages"] and d["pages"] != d["stored_pages"]:
            pages += f"/{d['pages']}"
        _row(
            table,
            d["id"][:8],
            d["title"],
            pages,
            d["source"],
            f"{d['text_length']:,}",
            _verdict_summary(d["verdicts"]),
        )
    console.print(table)
    rprint("[dim]Pages shows stored/actual when they differ (text kept as fewer pages).[/dim]")
    rprint(
        "[dim]Verdicts: text_layer, garbled, image_only per page; "
        "ocr:* means that page was OCR'd.[/dim]"
    )


@docs_app.command("show")
def docs_show(
    doc_id: str = typer.Argument(..., help="Document id (or unique prefix)"),
    page: int | None = typer.Option(None, "--page", "-p", help="Only this page (1-based)"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Print a stored document's text and its page map.

    Examples:
        openfoia docs show 3f2a
        openfoia docs show 3f2a --page 12
    """
    from .db import get_session  # lazy: startup time
    from .doctext import backfill_single_pages  # lazy: startup time

    _open_docs_db()
    with get_session() as session:
        backfill_single_pages(session.connection())
        doc = _resolve_doc(session, doc_id)
        pages = [(p.page_number, p.char_count, p.text, p.verdict) for p in doc.pages]
        req = doc.request
        from_dc = req is not None and _doc_source(req.request_number) == "documentcloud"
        title = req.subject if from_dc else doc.filename
        header = {"id": doc.id, "title": title, "filename": doc.filename}
        declared = doc.page_count

    selected = [p for p in pages if page is None or p[0] == page]
    if page is not None and not selected:
        typer.echo(f"Page {page} not stored; this document has {len(pages)} page(s).", err=True)
        raise typer.Exit(1)

    if as_json:
        out = {
            **header,
            "page_count": declared,
            "page_map": [{"page": n, "chars": c, "verdict": v} for n, c, _, v in pages],
            "pages": [{"page": n, "text": t} for n, _, t, _ in selected],
        }
        typer.echo(safe_json(out, indent=2))
        return

    _echo(f"{header['id']}  {safe_text(header['title'])}")
    typer.echo("Page map: " + ", ".join(f"p{n}={c:,}" for n, c, _, _ in pages))
    counts: dict[str, int] = {}
    for _, _, _, v in pages:
        if v:
            counts[v] = counts.get(v, 0) + 1
    if counts:
        _echo("Verdicts: " + _verdict_summary(counts))
    if declared and declared > len(pages):
        typer.echo(
            f"Note: the source has {declared} pages but the text is stored as {len(pages)}; "
            + SINGLE_PAGE_CAVEAT
        )
    for n, _, text, v in selected:
        # Document text is untrusted: a hostile PDF's text layer can carry ESC.
        _echo(f"\n--- page {n}{f' ({v})' if v else ''} ---\n{text}")


@docs_app.command("grep")
def docs_grep(
    query: str = typer.Argument(
        ..., help='Search text. FTS5 syntax: "exact phrase", a OR b, NEAR(a b, 10)'
    ),
    context: int = typer.Option(
        200, "--context", "-C", help="Characters shown each side of a match"
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Maximum passages (default 50; 10 with --semantic)"
    ),
    doc: list[str] | None = typer.Option(
        None, "--doc", help="Only this document id or prefix (repeatable)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
    save: bool = typer.Option(
        False,
        "--save",
        help="Attach each hit to the active case as a note (document, page, passage)",
    ),
    note: str | None = typer.Option(
        None, "--note", help="Save the hits (as --save) with this note"
    ),
    all_docs: bool = typer.Option(
        False, "--all-docs", help="Search every document, not only the active case's"
    ),
    semantic: bool = typer.Option(
        False,
        "--semantic",
        help="Rank pages by meaning instead of matching words (optional extra: "
        "openfoia install-extras semantic). Offline once the model is downloaded.",
    ),
    model: str | None = typer.Option(
        None, "--model", help="Embedding model for --semantic (see docs embed-model --list)"
    ),
):
    """Keyword-in-context search over every stored page, fully offline.

    While a case is active only that case's documents are searched
    (--all-docs for everything); --save never stores hits from other cases.

    Prints document id, title, page and the passage around each match.
    Uses the local SQLite FTS5 index; if this SQLite build has no FTS5
    (some SQLCipher builds) it falls back to a plain substring scan.

    With --semantic, pages are ranked by similarity of meaning to the query
    ("payments to the mayor's brother" can find "funds transferred to a
    sibling of the city executive"), one passage per page, best first, with a
    score. Every page gets a score, so the last results may be unrelated: read
    them, do not count them. Pages not yet embedded are embedded first, on
    this machine. Needs the optional extra and a one-time model download
    (openfoia docs embed-model).

    Examples:
        openfoia docs grep Hocking
        openfoia docs grep '"school board" OR WISC' --context 80
        openfoia docs grep 'NEAR(contract audit, 15)' --doc 3f2a --json
        openfoia docs grep --semantic "payments to the mayor's brother"
    """
    from .cases import active_case_id_in_db  # lazy: startup time
    from .db import get_session  # lazy: startup time
    from .doctext import search_pages  # lazy: startup time

    _open_docs_db()
    saving = save or note is not None
    case_id = active_case_id_in_db()
    if saving and not case_id:
        typer.echo("No active case to save to; run 'openfoia case use <name>'.", err=True)
        raise typer.Exit(1)
    with get_session() as session:
        case_docs = set(_case_document_ids(session, case_id)) if case_id else set()
        doc_ids = [_resolve_doc(session, d).id for d in doc] if doc else None
        if case_id and not all_docs:
            doc_ids = [d for d in doc_ids if d in case_docs] if doc_ids else list(case_docs)
            if not doc_ids:
                typer.echo("No matches (no documents in the active case; --all-docs).")
                return
        why_not_vec = ""
        if semantic:
            limit = limit or 10
            hits, engine, why_not_vec = _semantic_grep(session, query, limit, doc_ids, model)
        else:
            limit = limit or 50
            hits, engine = search_pages(
                session, query, context=context, limit=limit, document_ids=doc_ids
            )

    notice = {
        "like": "FTS5 is not available in this SQLite build; using a plain substring scan.",
        "fts5-literal": "Query is not valid FTS5 syntax; searched it as an exact phrase.",
        "sqlite-vec": "Semantic search: cosine similarity in SQLite (sqlite-vec).",
        "numpy": "Semantic search: brute-force cosine in numpy"
        + (f" (sqlite-vec not used: {why_not_vec})." if why_not_vec else "."),
    }.get(engine)
    if notice:
        typer.echo(f"Note: {notice}", err=True)

    if saving and hits:
        saved = skipped = dupes = 0
        with get_session() as session:
            seen = {
                (n.document_id, n.page, n.quote)
                for n in session.query(CaseNote).filter(CaseNote.case_id == case_id)
            }
            for h in hits:
                if h.document_id not in case_docs:
                    skipped += 1  # never attach another case's document to this one
                elif (h.document_id, h.page, h.passage) in seen:
                    dupes += 1
                else:
                    seen.add((h.document_id, h.page, h.passage))
                    session.add(
                        CaseNote(
                            case_id=case_id,
                            document_id=h.document_id,
                            page=h.page,
                            quote=h.passage,
                            note=note,
                            tier="says",
                        )
                    )
                    saved += 1
        msg = f"Saved {saved} note(s) to the active case."
        if dupes:
            msg += f" {dupes} already saved."
        if skipped:
            msg += f" Skipped {skipped} hit(s) from documents outside the case."
        typer.echo(msg, err=True)

    if as_json:
        out = {"query": query, "engine": engine, "hits": [h.to_dict() for h in hits]}
        typer.echo(safe_json(out, indent=2))
        return
    if not hits:
        typer.echo("No matches." + (" (no embedded pages to search)" if semantic else ""))
        return
    for h in hits:
        star = "" if h.pages_are_real else "*"
        head = f"{h.document_id[:8]}  {safe_text(h.title)}  p.{h.page}{star}"
        if semantic:
            head += f"  score {h.score:.2f}"
            body = Text(safe_text(h.passage, multiline=True))
        else:
            # Clean each part separately so the match offsets still line up.
            body = Text(safe_text(h.passage[: h.match_start], multiline=True))
            body.append(
                safe_text(h.passage[h.match_start : h.match_end], multiline=True),
                style="bold yellow",
            )
            body.append(safe_text(h.passage[h.match_end :], multiline=True))
        console.print(Text(head, style="cyan"))
        console.print(body)
        console.print()
    typer.echo(f"{len(hits)} passage(s)" + (" (limit reached)" if len(hits) >= limit else ""))
    if not all(h.pages_are_real for h in hits):
        typer.echo(f"* {SINGLE_PAGE_CAVEAT}")


def _semantic_grep(session, query: str, limit: int, doc_ids, model: str | None):
    """Embed any pages not yet indexed (offline), then rank by meaning."""
    from .semantic import (  # lazy: startup time
        SemanticUnavailable,
        count_missing,
        embed_missing,
        load_embedder,
        semantic_search,
    )

    try:
        embedder = load_embedder(model)
        missing, _ = count_missing(session, embedder.name, doc_ids)
        if missing:
            typer.echo(f"Embedding {missing} page(s) not yet indexed, on this machine...", err=True)
            embed_missing(session, embedder, document_ids=doc_ids)
        return semantic_search(session, query, embedder, limit=limit, document_ids=doc_ids)
    except SemanticUnavailable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


@docs_app.command("embed-model")
def docs_embed_model(
    name: str | None = typer.Argument(None, help="Model to download (default potion-base-8M)"),
    list_models: bool = typer.Option(False, "--list", help="List known models and exit"),
    verify: bool = typer.Option(
        False, "--verify", help="Re-check the SHA-256 of a downloaded model; no network"
    ),
    tor: bool | None = typer.Option(
        None, "--tor/--no-tor", help="Route the download through Tor; default is network.tor"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before downloading"),
):
    """Download the embedding model for docs grep --semantic. Network, once.

    The only network action of semantic search. Fetches pinned files at a
    pinned revision from huggingface.co, checks each file's SHA-256, and
    stores them in the data directory (models/, portable mode respected),
    not in a shared cache. Hugging Face sees your IP address (the Tor exit
    node's with --tor) and which model you fetch. Nothing from your documents
    or searches is sent. After this, embedding and search run offline.

    \b
    Models:
      potion-base-8M            English, ~31 MB, ~150 MB RAM (default)
      potion-retrieval-32M      English, tuned for search, ~131 MB, ~350 MB RAM
      potion-multilingual-128M  101 languages, ~531 MB, ~1.9 GB peak RAM
    """
    from .semantic import (  # lazy: startup time
        DEFAULT_MODEL,
        INSTALL_HINT,
        MODELS,
        SemanticUnavailable,
        download_model,
        download_url,
        extra_status,
        get_spec,
        model_installed,
        model_path,
        verify_model,
    )

    if list_models:
        for spec in MODELS.values():
            state = "downloaded" if model_installed(spec.name) else "not downloaded"
            typer.echo(
                f"{spec.name:26} {spec.download_bytes / 1e6:6.0f} MB  {spec.languages:14} "
                f"{state}  ({spec.about})"
            )
        return

    name = name or DEFAULT_MODEL
    try:
        spec = get_spec(name)
    except SemanticUnavailable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None

    if verify:
        problems = verify_model(name)
        if problems:
            for p in problems:
                typer.echo(f"  {p}", err=True)
            typer.echo(f"{name} is not intact. Download it again: openfoia docs embed-model {name}")
            raise typer.Exit(1)
        typer.echo(f"{name}: every file matches its pinned SHA-256.")
        return

    if model_installed(name):
        typer.echo(f"{name} is already downloaded: {model_path(name)}")
        typer.echo("Check it with --verify. No network request was made.")
        return

    from .config import load_config  # lazy: startup time
    from .net import TorUnavailableError  # lazy: startup time

    policy = _egress_policy_from(load_config(), tor=tor)
    route = (
        "through Tor; Hugging Face sees the exit node's IP, not yours"
        if policy.is_tor
        else "directly; Hugging Face sees your IP address"
    )
    mb = spec.download_bytes / 1e6
    rprint("[bold yellow]Network request[/bold yellow]")
    rprint(f"  Downloads {spec.name} ({mb:.0f} MB) from huggingface.co {route}.")
    rprint("  Sent: which model files you fetch. Nothing from your documents or searches.")
    rprint(f"  Pinned revision {spec.revision[:12]}; every file's SHA-256 is checked.")
    rprint(f"  Saved to: {model_path(name)}")
    if not extra_status()["model2vec"]:
        rprint(f"[yellow]model2vec is not installed yet; also run: {INSTALL_HINT}[/yellow]")
    if not yes and not typer.confirm("Download now?", default=False):
        rprint("[dim]Cancelled. Nothing was sent.[/dim]")
        raise typer.Exit(1)

    _check_tor_or_exit(policy)
    for fname in spec.files:
        rprint(f"  [dim]{download_url(spec, fname)}[/dim]")
    with Progress() as progress:
        tasks: dict[str, Any] = {}

        def _tick(fname: str, got: int, total: int) -> None:
            if fname not in tasks:
                tasks[fname] = progress.add_task(fname, total=total)
            progress.update(tasks[fname], completed=got)

        try:
            folder = download_model(name, policy, _tick)
        except TorUnavailableError:
            _report_tor_unavailable()
        except Exception as e:
            rprint(f"[red]Download failed: {e}[/red]")
            raise typer.Exit(1) from None
    rprint(f"[green]Downloaded and verified {name}[/green] -> {folder}")
    rprint("[dim]Next: openfoia docs embed (offline), then docs grep --semantic.[/dim]")


@docs_app.command("embed")
def docs_embed(
    model: str | None = typer.Option(None, "--model", help="Embedding model to use"),
    status: bool = typer.Option(False, "--status", help="Show what is indexed; embed nothing"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Re-embed every page for this model"),
    drop: bool = typer.Option(
        False, "--drop", help="Delete stored embeddings (all models, or --model's)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before --drop"),
    as_json: bool = typer.Option(False, "--json", help="Print --status as JSON"),
):
    """Embed stored pages for docs grep --semantic. Offline.

    Embeds every page that has text and no embedding yet, on this machine
    with the downloaded model. docs grep --semantic does this too before
    searching; run it ahead of time for a large collection. Embeddings are
    stored in the database next to the page text (encrypted with it when the
    database is) and deleted with their page.

    \b
    Examples:
        openfoia docs embed               # index whatever is new
        openfoia docs embed --status      # what is indexed, which search path runs
        openfoia docs embed --drop        # remove every stored embedding
    """
    from .db import get_session  # lazy: startup time
    from .semantic import (  # lazy: startup time
        INSTALL_HINT,
        SemanticUnavailable,
        drop_vectors,
        embed_missing,
        extra_status,
        index_status,
        load_embedder,
        load_sqlite_vec,
    )

    _open_docs_db()
    if status:
        with get_session() as session:
            st = index_status(session)
            vec_ok, why = load_sqlite_vec(session.connection())
        extras = extra_status()
        out = {
            **st.to_dict(),
            "extra": extras,
            "search_path": "sqlite-vec" if vec_ok else "numpy",
            "sqlite_vec_unavailable": why or None,
        }
        if as_json:
            typer.echo(json.dumps(out, indent=2))
            return
        typer.echo(f"Pages with text: {st.pages_with_text}")
        if not st.by_model:
            typer.echo("Embedded: none")
        for m, d in st.by_model.items():
            typer.echo(
                f"Embedded with {m}: {d['pages']} page(s), {d['chunks']} chunk(s), "
                f"{d['bytes'] / 1e6:.1f} MB of vectors"
            )
        typer.echo(
            f"model2vec: {'installed' if extras['model2vec'] else 'missing'}; "
            f"sqlite-vec: {'installed' if extras['sqlite_vec'] else 'missing'}; "
            f"models downloaded: {', '.join(extras['models']) or 'none'}"
        )
        typer.echo("Search path: " + ("sqlite-vec" if vec_ok else f"numpy brute force ({why})"))
        if not extras["model2vec"]:
            typer.echo(f"Install with: {INSTALL_HINT}")
        return

    if drop:
        target = f"{model}'s" if model else "all"
        if not yes and not typer.confirm(
            f"Delete {target} stored embeddings? Page text is kept.", default=False
        ):
            raise typer.Exit(1)
        with get_session() as session:
            n = drop_vectors(session.connection(), model)
        typer.echo(f"Deleted {n} embedding row(s).")
        return

    try:
        embedder = load_embedder(model)
    except SemanticUnavailable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    with get_session() as session:
        if rebuild:
            drop_vectors(session.connection(), embedder.name)
            session.commit()
        with Progress() as progress:
            task = progress.add_task(f"Embedding with {embedder.name}", total=None)
            n = embed_missing(
                session,
                embedder,
                progress=lambda done, total: progress.update(task, completed=done, total=total),
            )
    typer.echo(f"Embedded {n} page(s) with {embedder.name}." if n else "Nothing new to embed.")


# === Agency Commands ===


@agency_app.command("list")
def agency_list(
    level: str | None = typer.Option(
        None,
        "--level",
        "-l",
        help="Filter by level: federal, state, local, tribal, or a body type such as school_district",
    ),
    state: str | None = typer.Option(None, "--state", "-s", help="Filter by state (2-letter code)"),
    jurisdiction: str | None = typer.Option(
        None, "--jurisdiction", "-j", help="Filter by jurisdiction id, e.g. us or us-mi"
    ),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum results"),
):
    """List agencies in the database."""
    from .db import get_db_path, get_session  # lazy: startup time
    from .jurisdictions import BODY_TYPES  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = session.query(Agency)

        if level:
            if level.lower() in BODY_TYPES and level.lower() not in ("federal", "state"):
                query = query.filter(Agency.body_type == level.lower())
            else:
                try:
                    level_enum = AgencyLevel(level.lower())
                    query = query.filter(Agency.level == level_enum)
                except ValueError:
                    rprint(
                        f"[red]Invalid level '{level}'. Use: federal, state, local, tribal, "
                        f"or {', '.join(b for b in BODY_TYPES if b not in ('federal', 'state'))}[/red]"
                    )
                    raise typer.Exit(1) from None

        if state:
            query = query.filter(Agency.state == state.upper())
        if jurisdiction:
            query = query.filter(Agency.jurisdiction_id == jurisdiction.lower())

        agencies = query.order_by(Agency.name).limit(limit).all()

        if not agencies:
            rprint("[yellow]No agencies found.[/yellow]")
            return

        table = Table(title=f"Agencies ({len(agencies)} results)")
        table.add_column("Abbr", style="cyan", width=8)
        table.add_column("Name")
        table.add_column("Level", width=15)
        table.add_column("Law", width=6)
        table.add_column("Contact", width=30)

        for a in agencies:
            contact = a.foia_email or a.foia_portal_url or "—"
            if len(contact) > 28:
                contact = contact[:25] + "..."
            _row(
                table,
                a.abbreviation or "—",
                a.name,
                a.body_type or a.level.value,
                a.jurisdiction_id or "—",
                contact,
            )

        console.print(table)


@agency_app.command("add")
def agency_add(
    name: str = typer.Option(..., "--name", help="Public body name"),
    jurisdiction: str = typer.Option(
        ...,
        "--jurisdiction",
        "-j",
        help="Jurisdiction id, e.g. us-mi (see 'openfoia jurisdiction list')",
    ),
    level: str = typer.Option(
        ...,
        "--level",
        "-l",
        help="federal, state, county, city, school_district, university, other",
    ),
    abbreviation: str | None = typer.Option(None, "--abbr", help="Short name"),
    email: str | None = typer.Option(None, "--email", help="Records request email"),
    address: str | None = typer.Option(None, "--address", help="Mailing address"),
    portal: str | None = typer.Option(None, "--portal", help="Request form or portal URL"),
    custodian_title: str | None = typer.Option(
        None, "--custodian-title", help="Who to address, e.g. FOIA Coordinator"
    ),
    response_days: int | None = typer.Option(
        None, "--response-days", help="Statutory response days (default: the jurisdiction's)"
    ),
):
    """Add a public body: a school district, city, county, university or state agency.

    Example:
        openfoia agency add --name "Detroit Public Schools Community District" -j us-mi \
            -l school_district --email foia.request@detroitk12.org --custodian-title "FOIA Coordinator"
    """
    from .db import get_session  # lazy: startup time
    from .jurisdictions import BODY_TYPES  # lazy: startup time
    from .jurisdictions import get as get_jurisdiction  # lazy: startup time

    _require_db()
    body_type = level.lower()
    if body_type not in BODY_TYPES:
        rprint(f"[red]--level must be one of: {', '.join(BODY_TYPES)}[/red]")
        raise typer.Exit(2)
    j = get_jurisdiction(jurisdiction)
    if j is None:
        rprint(
            f"[red]Unknown jurisdiction '{jurisdiction}'. See 'openfoia jurisdiction list'.[/red]"
        )
        raise typer.Exit(2)
    if j["level"] == "federal" and body_type != "federal":
        rprint("[red]A non-federal body must use its state's jurisdiction, e.g. us-mi.[/red]")
        raise typer.Exit(2)
    if body_type == "federal" and j["level"] != "federal":
        rprint("[red]A federal body uses jurisdiction us, not a state law.[/red]")
        raise typer.Exit(2)
    enum_level = {"federal": AgencyLevel.FEDERAL, "state": AgencyLevel.STATE}.get(
        body_type, AgencyLevel.LOCAL
    )
    # None stays None: an unknown clock must never become a fake 20-day law.
    days = response_days if response_days is not None else j["response"]["days"]
    with get_session() as session:
        if session.query(Agency).filter(Agency.name.ilike(_like_escape(name), escape="\\")).first():
            rprint(f"[red]An agency named '{_safe(name)}' already exists.[/red]")
            raise typer.Exit(1)
        a = Agency(
            name=name,
            abbreviation=abbreviation,
            level=enum_level,
            state=j["id"][3:].upper() if j["id"].startswith("us-") else None,
            jurisdiction_id=j["id"],
            body_type=body_type,
            custodian_title=custodian_title,
            foia_email=email,
            foia_address=address,
            foia_portal_url=portal,
            preferred_method=DeliveryMethod.EMAIL if email else DeliveryMethod.MAIL,
            typical_response_days=days,
        )
        session.add(a)
        session.flush()
        clock = f"{days} response days" if days is not None else "unknown (no statutory day count)"
        rprint(
            f"[green]Added[/green] [cyan]{name}[/cyan] ({body_type}, {j['name']}: "
            f"{j['statute']['name'] or 'statute unknown'}; {clock})"
        )
        if not j["verified"]:
            rprint(
                f"[yellow]{j['id']} is an unverified stub: check the law before relying on the "
                f"deadline. Sources: {', '.join(j['sources']) or 'none'}[/yellow]"
            )


def _fmt(v) -> str:
    if v is None:
        return "[dim]unknown[/dim]"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


@jurisdiction_app.command("list")
def jurisdiction_list(
    unverified: bool = typer.Option(False, "--unverified", help="Only stubs still to be filled"),
):
    """List every jurisdiction openfoia knows, with its statute and response clock."""
    from .jurisdictions import load_all  # lazy: startup time

    rows = [j for j in load_all().values() if not unverified or not j["verified"]]
    table = Table(title=f"Jurisdictions ({len(rows)})")
    table.add_column("Id", style="cyan", width=6)
    table.add_column("Name")
    table.add_column("Statute")
    table.add_column("Response", width=14)
    table.add_column("Verified", width=8)
    for j in rows:
        r = j["response"]
        if r["days"] is not None:
            clock = f"{r['days']} {r['unit']}"
        elif r["unit"] == "none":
            clock = "no fixed clock"
        else:
            clock = "unknown"
        table.add_row(
            j["id"],
            j["name"],
            j["statute"]["name"] or "unknown",
            clock,
            "yes" if j["verified"] else "no",
        )
    console.print(table)
    stubs = sum(1 for j in rows if not j["verified"])
    rprint(f"[dim]{len(rows) - stubs} verified, {stubs} unverified.[/dim]")
    if stubs:
        rprint("[dim]Unverified: a value is only as good as the source listed in `show`.[/dim]")


@jurisdiction_app.command("show")
def jurisdiction_show(
    jurisdiction_id: str = typer.Argument(..., help="e.g. us, us-mi"),
    as_json: bool = typer.Option(False, "--json", help="Print the raw record"),
):
    """Everything known about one jurisdiction: clocks, fees, residency, appeals, sources."""
    from .jurisdictions import get as get_jurisdiction  # lazy: startup time

    j = get_jurisdiction(jurisdiction_id)
    if j is None:
        rprint(
            f"[red]Unknown jurisdiction '{jurisdiction_id}'. See 'openfoia jurisdiction list'.[/red]"
        )
        raise typer.Exit(1)
    if as_json:
        print(json.dumps(j, indent=2))
        return
    rprint(f"\n[bold cyan]{j['name']}[/bold cyan] [dim]({j['id']}, {j['level']})[/dim]")
    if not j["verified"]:
        rprint(
            "[bold yellow]UNVERIFIED: every value below is a placeholder until checked.[/bold yellow]"
        )
    else:
        rprint(f"[green]Verified {j['verified_at']}[/green]")
    st = j["statute"]
    rprint(f"Statute: {_fmt(st['name'])}, {_fmt(st['citation'])} {st['url'] or ''}")
    r = j["response"]
    if r["days"] is not None:
        clock = f"{r['days']} {r['unit'] or ''} days".replace("  ", " ")
    elif r["unit"] == "none":
        clock = "no fixed statutory clock"
    else:
        clock = _fmt(None)
    ext = (
        f"{r['extension_days']} {r['extension_unit'] or ''}".strip()
        if r["extension_days"]
        else _fmt(None)
    )
    rprint(f"Response: {clock}; extension {ext}; silence is denial: {_fmt(r['silence_is_denial'])}")
    if r["rule_text"]:
        rprint(f"  {r['rule_text']}")
    f = j["fees"]
    rprint(
        f"Fees: search allowed {_fmt(f['search_allowed'])}; estimate required {_fmt(f['estimate_required'])}"
    )
    for k in ("labor_rule", "copy_rule", "waiver_rule", "late_penalty"):
        if f[k]:
            rprint(f"  {k.replace('_', ' ')}: {f[k]}")
    res = j["residency"]
    rprint(f"Residency restricted: {_fmt(res['restricted'])} {res['rule_text'] or ''}")
    a = j["appeal"]
    rprint(
        f"Appeal: {_fmt(a['body'])} ({_fmt(a['type'])}); deadline {_fmt(a['deadline_days'])} "
        f"{a['deadline_unit'] or ''}"
    )
    if a["rule_text"]:
        rprint(f"  {a['rule_text']}")
    rprint(f"Exemptions: {_fmt(j['exemptions_citation'])}")
    rprint(f"Format rule: {_fmt(j['format_rule'])}")
    c = j["covers"]
    rprint(
        "Covers: governor "
        + _fmt(c["governor"])
        + ", legislature "
        + _fmt(c["legislature"])
        + ", public universities "
        + _fmt(c["universities_public"])
        + ", school districts "
        + _fmt(c["school_districts"])
    )
    if j["notes"]:
        rprint(f"Notes: {j['notes']}")
    rprint("Sources:")
    for u in j["sources"] or ["(none)"]:
        rprint(f"  {u}")


@jurisdiction_app.command("search")
def jurisdiction_search(text: str = typer.Argument(..., help="Part of a name, id or statute")):
    """Find a jurisdiction by name, id or statute name."""
    from .jurisdictions import search as search_jurisdictions  # lazy: startup time

    hits = search_jurisdictions(text)
    if not hits:
        rprint(f"[yellow]No jurisdiction matches '{_safe(text)}'.[/yellow]")
        raise typer.Exit(1)
    for j in hits:
        flag = "" if j["verified"] else " [yellow](unverified)[/yellow]"
        rprint(
            f"[cyan]{j['id']}[/cyan]  {j['name']}  [dim]{j['statute']['name'] or ''}[/dim]{flag}"
        )


@agency_app.command("search")
def agency_search(
    query: str = typer.Argument(..., help="Search term"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum results"),
):
    """Search for agencies by name or abbreviation."""
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        # Search by name or abbreviation
        search_term = f"%{query}%"
        agencies = (
            session.query(Agency)
            .filter((Agency.name.ilike(search_term)) | (Agency.abbreviation.ilike(search_term)))
            .order_by(Agency.name)
            .limit(limit)
            .all()
        )

        if not agencies:
            rprint(f"[yellow]No agencies found matching '{_safe(query)}'.[/yellow]")
            return

        table = Table(title=f"Search results for '{query}' ({len(agencies)} found)")
        table.add_column("Abbr", style="cyan", width=8)
        table.add_column("Name")
        table.add_column("Email/Portal")

        for a in agencies:
            contact = a.foia_email or a.foia_portal_url or "—"
            _row(
                table,
                a.abbreviation or "—",
                a.name,
                contact,
            )

        console.print(table)


@agency_app.command("info")
def agency_info(
    agency_id: str = typer.Argument(..., help="Agency abbreviation or name"),
):
    """Show detailed information about an agency."""
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        # Try abbreviation first, then name
        agency = (
            session.query(Agency)
            .filter((Agency.abbreviation.ilike(agency_id)) | (Agency.name.ilike(f"%{agency_id}%")))
            .first()
        )

        if not agency:
            rprint(f"[red]Agency '{_safe(agency_id)}' not found.[/red]")
            raise typer.Exit(1)

        rprint(f"\n[bold cyan]{_safe(agency.name)}[/bold cyan]")
        if agency.abbreviation:
            rprint(f"[dim]({_safe(agency.abbreviation)})[/dim]")
        rprint("─" * 50)

        table = Table(show_header=False, box=None)
        table.add_column("Field", style="cyan", width=20)
        table.add_column("Value")

        table.add_row("Level", agency.level.value.title())
        if agency.body_type:
            table.add_row("Body type", agency.body_type)
        if agency.state:
            table.add_row("State", agency.state)
        if agency.jurisdiction_id:
            from .jurisdictions import get as get_jurisdiction  # lazy: startup time

            j = get_jurisdiction(agency.jurisdiction_id)
            law = (j["statute"]["name"] or "statute unknown") if j else "unknown jurisdiction"
            table.add_row("Law", f"{_safe(agency.jurisdiction_id)}: {_safe(law)}")
            if j and not j["verified"]:
                table.add_row(
                    "", "[yellow]jurisdiction data unverified; check the sources[/yellow]"
                )
        if agency.custodian_title:
            table.add_row("Custodian", agency.custodian_title)

        rprint("\n[bold]Contact Information[/bold]")
        if agency.foia_email:
            table.add_row("Email", agency.foia_email)
        if agency.foia_fax:
            table.add_row("Fax", agency.foia_fax)
        if agency.foia_portal_url:
            table.add_row("Portal", agency.foia_portal_url)
        if agency.foia_address:
            table.add_row("Address", agency.foia_address.replace("\n", "\n                      "))

        table.add_row("Preferred Method", agency.preferred_method.value.replace("_", " ").title())
        table.add_row(
            "Typical Response",
            f"{agency.typical_response_days} days"
            if agency.typical_response_days is not None
            else "unknown (no statutory day count)",
        )

        if agency.fee_waiver_criteria:
            table.add_row(
                "Fee Waiver",
                agency.fee_waiver_criteria[:100] + "..."
                if len(agency.fee_waiver_criteria) > 100
                else agency.fee_waiver_criteria,
            )

        console.print(table)
        rprint("")


# === Template Commands ===


@template_app.command("list")
def template_list():
    """List available request templates."""
    from .templates import list_templates  # lazy: startup time

    templates = list_templates()

    from .templates import JURISDICTION_AWARE  # lazy: startup time

    table = Table(title="Available Templates")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("State law")

    for t in templates:
        aware = "yes, --jurisdiction" if t["name"] in JURISDICTION_AWARE else "federal only"
        table.add_row(t["name"], t["description"], aware)

    console.print(table)
    rprint("\n[dim]Use 'openfoia template generate <name>' to create a request.[/dim]")


@template_app.command("show")
def template_show(
    template_name: str = typer.Argument(..., help="standard or school-program"),
    jurisdiction: str = typer.Option(
        "us",
        "--jurisdiction",
        help="Jurisdiction id, e.g. us-mi (see 'openfoia jurisdiction list')",
    ),
):
    """Print a template's skeleton for a jurisdiction, placeholders left in.

    Example:
        openfoia template show school-program --jurisdiction us-mi
    """
    from .templates import (  # lazy: startup time
        JURISDICTION_AWARE,
        RequestDetails,
        RequesterInfo,
        school_program_request,
        standard_request,
    )

    if template_name not in JURISDICTION_AWARE:
        rprint(
            f"[red]Only these templates take a jurisdiction: {', '.join(sorted(JURISDICTION_AWARE))}[/red]"
        )
        raise typer.Exit(2)
    j = _jurisdiction_or_exit(jurisdiction)
    requester = RequesterInfo(name="[YOUR NAME]", email="[YOUR EMAIL]", address="[YOUR ADDRESS]")
    if template_name == "school-program":
        letter = school_program_request(requester, "[PUBLIC BODY]", "[PROGRAM NAME]", j)
    else:
        details = RequestDetails(subject="[SUBJECT]", description="[RECORDS REQUESTED]")
        letter = standard_request(requester, "[PUBLIC BODY]", details, jurisdiction=j)
    if not j["verified"]:
        rprint(
            f"[yellow]{j['name']} is an unverified stub; placeholders mark what is unknown.[/yellow]"
        )
    rprint(letter)


def _jurisdiction_or_exit(jurisdiction_id: str) -> dict:
    from .jurisdictions import get as get_jurisdiction  # lazy: startup time

    j = get_jurisdiction(jurisdiction_id)
    if j is None:
        rprint(
            f"[red]Unknown jurisdiction '{jurisdiction_id}'. See 'openfoia jurisdiction list'.[/red]"
        )
        raise typer.Exit(2)
    return j


@template_app.command("generate")
def template_generate(
    template_name: str = typer.Argument(
        ..., help="Template name (standard/appeal/self/school-program)"
    ),
    agency: str = typer.Option(..., "--agency", "-a", help="Target agency (name or abbreviation)"),
    subject: str = typer.Option(..., "--subject", "-s", help="Request subject/description"),
    name: str = typer.Option(..., "--name", "-n", help="Your full name"),
    email: str = typer.Option(..., "--email", "-e", help="Your email address"),
    address: str = typer.Option("", "--address", help="Your mailing address"),
    organization: str | None = typer.Option(None, "--org", help="Your organization"),
    journalist: bool = typer.Option(False, "--journalist", "-j", help="You are a journalist"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Output file (default: stdout)"
    ),
    no_fee_waiver: bool = typer.Option(
        False, "--no-fee-waiver", help="Don't include fee waiver request"
    ),
    expedited: bool = typer.Option(False, "--expedited", help="Request expedited processing"),
    jurisdiction: str | None = typer.Option(
        None,
        "--jurisdiction",
        help="Jurisdiction id, e.g. us-mi; default: the agency's, else federal",
    ),
):
    """Generate a FOIA or public-records request from a template.

    Examples:
        openfoia template generate standard -a FBI -s "Records on X" -n "Jane Doe" -e jane@example.com
        openfoia template generate standard -a EPA -s "Pollution data" -n "John Smith" -e john@example.com -j
        openfoia template generate school-program -a "Detroit Public Schools Community District" -s "Gifted and Talented Program" -n "Jane Doe" -e jane@example.com --jurisdiction us-mi
    """
    from .templates import (  # lazy: startup time
        RequestDetails,
        RequesterInfo,
        records_about_self,
        school_program_request,
        standard_request,
    )

    # Build requester info
    requester = RequesterInfo(
        name=name,
        email=email,
        address=address,
        organization=organization,
        is_journalist=journalist,
    )

    # Get agency name from database if abbreviation
    from .db import get_db_path  # lazy: startup time

    agency_name = agency
    db_path = get_db_path()
    if db_path.exists():
        from .db import get_session  # lazy: startup time

        with get_session() as session:
            found = (
                session.query(Agency)
                .filter((Agency.abbreviation.ilike(agency)) | (Agency.name.ilike(agency)))
                .first()
            )
            if found is None:
                matches = (
                    session.query(Agency).filter(Agency.name.ilike(f"%{agency}%")).limit(10).all()
                )
                if len(matches) > 1:
                    rprint(f"[red]'{agency}' matches more than one agency; be exact:[/red]")
                    for m in matches:
                        rprint(
                            f"  {_safe(m.name)}"
                            + (f" ({_safe(m.abbreviation)})" if m.abbreviation else "")
                        )
                    raise typer.Exit(2)
                found = matches[0] if matches else None
            if found:
                agency_name = found.name
                if jurisdiction is None and found.jurisdiction_id:
                    jurisdiction = found.jurisdiction_id

    j = _jurisdiction_or_exit(jurisdiction) if jurisdiction else None
    non_federal = bool(j) and j["id"] != "us"
    if non_federal and template_name == "self":
        rprint(
            "[red]The 'self' template is a federal FOIA and Privacy Act letter; "
            "it cannot be written under a state law. Use 'standard' with --jurisdiction.[/red]"
        )
        raise typer.Exit(2)
    if non_federal and expedited:
        rprint(
            "[yellow]Expedited processing is a federal FOIA provision; "
            f"the {j['name']} letter omits it.[/yellow]"
        )
    if j and not j["verified"]:
        rprint(
            f"[yellow]{j['name']} is an unverified stub: the letter leaves placeholders "
            "where the law is unknown. Check the sources in 'openfoia jurisdiction show'.[/yellow]"
        )

    # Generate based on template type
    if template_name == "standard":
        details = RequestDetails(subject=subject, description=subject)
        letter = standard_request(
            requester=requester,
            agency_name=agency_name,
            details=details,
            fee_waiver=not no_fee_waiver,
            expedited=expedited,
            jurisdiction=j,
        )
    elif template_name == "school-program":
        letter = school_program_request(
            requester, agency_name, subject, j, fee_waiver=not no_fee_waiver
        )
    elif template_name == "self":
        letter = records_about_self(
            requester=requester,
            agency_name=agency_name,
            record_type=subject,
        )
    elif template_name == "appeal":
        rprint("[yellow]Appeal template requires additional information.[/yellow]")
        rprint("[dim]Use the interactive mode: openfoia template appeal-wizard[/dim]")
        return
    else:
        rprint(
            f"[red]Unknown template '{template_name}'. Use 'openfoia template list' to see options.[/red]"
        )
        raise typer.Exit(1)

    # Output
    if output:
        from .exports import record_export  # lazy: startup time

        record_export(output)
        output.write_text(letter)
        rprint(f"[green]✓ Request saved to {output}[/green]")
    else:
        rprint("\n" + "─" * 60)
        rprint(letter)
        rprint("─" * 60 + "\n")


@template_app.command("exemptions")
def template_exemptions():
    """List common FOIA exemptions with explanations."""

    exemptions = [
        (
            "b(1)",
            "National Security",
            "Classified information regarding national defense or foreign policy",
        ),
        (
            "b(2)",
            "Internal Personnel Rules",
            "Related solely to internal personnel rules and practices",
        ),
        ("b(3)", "Statutory Exemption", "Specifically exempted by another statute"),
        (
            "b(4)",
            "Trade Secrets",
            "Trade secrets and confidential commercial/financial information",
        ),
        (
            "b(5)",
            "Deliberative Process",
            "Inter/intra-agency memos that are pre-decisional and deliberative",
        ),
        (
            "b(6)",
            "Personal Privacy",
            "Personnel, medical, or similar files where disclosure would invade privacy",
        ),
        (
            "b(7)(A)",
            "Law Enforcement - Interference",
            "Could interfere with enforcement proceedings",
        ),
        ("b(7)(B)", "Law Enforcement - Fair Trial", "Would deprive a person of a fair trial"),
        (
            "b(7)(C)",
            "Law Enforcement - Privacy",
            "Could constitute unwarranted invasion of privacy",
        ),
        ("b(7)(D)", "Law Enforcement - Confidential Source", "Could reveal a confidential source"),
        ("b(7)(E)", "Law Enforcement - Techniques", "Would disclose investigation techniques"),
        ("b(7)(F)", "Law Enforcement - Safety", "Could endanger life or physical safety"),
        (
            "b(8)",
            "Financial Institutions",
            "Examination/operating reports of financial institutions",
        ),
        ("b(9)", "Geological Info", "Geological/geophysical info about wells"),
    ]

    table = Table(title="FOIA Exemptions (5 U.S.C. § 552(b))")
    table.add_column("Exemption", style="cyan", width=10)
    table.add_column("Name", width=25)
    table.add_column("Description")

    for code, name, desc in exemptions:
        table.add_row(code, name, desc)

    console.print(table)
    rprint("\n[dim]When appealing, challenge the agency's application of these exemptions.[/dim]")


# === Campaign Commands ===


@campaign_app.command("create")
def campaign_create(
    name: str = typer.Option(..., "--name", "-n", help="Campaign name"),
    description: str = typer.Option(..., "--desc", "-d", help="Campaign description"),
    template_file: Path = typer.Option(..., "--template", "-t", help="Request template file"),
    target: int = typer.Option(100, "--target", help="Target number of requests"),
    organizer_name: str = typer.Option(..., "--organizer", help="Organizer name"),
    organizer_email: str = typer.Option(..., "--email", "-e", help="Organizer email"),
):
    """Create a new crowdsourced campaign."""
    from .db import get_db_path, get_session, init_db  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        init_db()

    if not template_file.exists():
        rprint(f"[red]Template file not found: {template_file}[/red]")
        raise typer.Exit(1)

    template_body = template_file.read_text()

    with get_session() as session:
        # Get or create organizer
        user = session.query(User).filter(User.email == organizer_email).first()
        if not user:
            user = User(id=str(uuid4()), email=organizer_email, name=organizer_name)
            session.add(user)
            session.flush()

        campaign = Campaign(
            id=str(uuid4()),
            name=name,
            description=description,
            organizer_id=user.id,
            request_template=template_body,
            target_agency_ids=[],
            target_request_count=target,
            is_active=True,
        )
        session.add(campaign)
        campaign_id = campaign.id

    rprint(f"[green]Campaign created: {campaign_id[:8]}[/green]")
    rprint(f"  Name: {_safe(name)}")
    rprint(f"  Target: {target} requests")
    rprint(f"  Template: {template_file}")


@campaign_app.command("list")
def campaign_list():
    """List all campaigns."""
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        campaigns = session.query(Campaign).order_by(Campaign.created_at.desc()).all()

        if not campaigns:
            rprint("[yellow]No campaigns found.[/yellow]")
            return

        table = Table(title=f"Campaigns ({len(campaigns)})")
        table.add_column("ID", style="cyan", width=10)
        table.add_column("Name")
        table.add_column("Active")
        table.add_column("Requests")
        table.add_column("Target")
        table.add_column("Created")

        for c in campaigns:
            active = "[green]Yes[/green]" if c.is_active else "[dim]No[/dim]"
            _row(
                table,
                c.id[:8],
                c.name,
                active,
                str(c.request_count()),
                str(c.target_request_count),
                c.created_at.strftime("%Y-%m-%d"),
            )

        console.print(table)


@campaign_app.command("status")
def campaign_status(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)"),
):
    """Check campaign progress."""
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(Campaign.id.like(f"{campaign_id}%")).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{_safe(campaign_id)}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(
                f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]"
            )
            raise typer.Exit(1)

        campaign = matches[0]

        requests = campaign.requests
        total = len(requests)
        responded = sum(
            1
            for r in requests
            if r.status
            in (
                RequestStatus.PARTIAL_RESPONSE,
                RequestStatus.COMPLETE,
                RequestStatus.DENIED,
            )
        )
        denied = sum(1 for r in requests if r.status == RequestStatus.DENIED)
        docs_count = sum(len(r.documents) for r in requests)
        total_pages = sum(d.page_count or 0 for r in requests for d in r.documents)

        table = Table(title=f"Campaign: {campaign.name}")
        table.add_column("Metric", style="cyan")
        table.add_column("Value")

        table.add_row("Participants", str(len(campaign.participants)))
        table.add_row("Requests Filed", f"{total} / {campaign.target_request_count}")
        table.add_row("Completion", f"{campaign.completion_rate() * 100:.1f}%")
        table.add_row("Responses Received", str(responded))
        table.add_row("Denials", str(denied))
        table.add_row("Documents Collected", f"{docs_count} ({total_pages} pages)")
        table.add_row("Active", "Yes" if campaign.is_active else "No")

        console.print(table)


@campaign_app.command("join")
def campaign_join(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)"),
    name: str = typer.Option(..., "--name", "-n", help="Your full name"),
    email: str = typer.Option(..., "--email", "-e", help="Your email address"),
):
    """Join a campaign as a participant."""
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(Campaign.id.like(f"{campaign_id}%")).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{_safe(campaign_id)}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(
                f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]"
            )
            raise typer.Exit(1)

        campaign = matches[0]

        if not campaign.is_active:
            rprint(f"[red]Campaign '{_safe(campaign.name)}' is no longer active.[/red]")
            raise typer.Exit(1)

        # Get or create user
        user = session.query(User).filter(User.email == email).first()
        if not user:
            user = User(id=str(uuid4()), email=email, name=name)
            session.add(user)
            session.flush()

        # Check if already a participant
        if user in campaign.participants:
            rprint(f"[yellow]You are already a participant in '{_safe(campaign.name)}'.[/yellow]")
            return

        campaign.participants.append(user)
        campaign_name = campaign.name
        participant_count = len(campaign.participants)

    rprint(f"[green]Joined campaign: {_safe(campaign_name)}[/green]")
    rprint(f"  Participants now: {participant_count}")


@campaign_app.command("distribute")
def campaign_distribute(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)"),
):
    """Distribute FOIA requests to campaign participants.

    Generates individual requests from the campaign template for each
    target agency and assigns them round-robin to participants.
    """
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(Campaign.id.like(f"{campaign_id}%")).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{_safe(campaign_id)}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(
                f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]"
            )
            raise typer.Exit(1)

        campaign = matches[0]

        if not campaign.is_active:
            rprint(f"[red]Campaign '{_safe(campaign.name)}' is no longer active.[/red]")
            raise typer.Exit(1)

        participants = list(campaign.participants)
        if not participants:
            rprint(
                f"[red]No participants in campaign '{campaign.name}'. Use 'openfoia campaign join' first.[/red]"
            )
            raise typer.Exit(1)

        # Resolve target agencies
        target_ids = campaign.target_agency_ids or []
        if target_ids:
            agencies = session.query(AgencyModel).filter(AgencyModel.id.in_(target_ids)).all()
        else:
            # If no target agencies specified, use all agencies in the DB
            agencies = session.query(AgencyModel).all()

        if not agencies:
            rprint("[red]No target agencies found.[/red]")
            raise typer.Exit(1)

        # Find which agency/participant combos already have requests
        existing = set()
        for req in campaign.requests:
            existing.add((req.agency_id, req.requester_id))

        # Generate requests round-robin
        created = 0
        skipped = 0
        table = Table(title=f"Distributing: {campaign.name}")
        table.add_column("Request #", style="cyan")
        table.add_column("Agency")
        table.add_column("Assigned To")
        table.add_column("Status")

        for i, agency in enumerate(agencies):
            participant = participants[i % len(participants)]

            if (agency.id, participant.id) in existing:
                skipped += 1
                continue

            req_num = f"REQ-{_utcnow().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
            request = RequestModel(
                id=str(uuid4()),
                request_number=req_num,
                requester_id=participant.id,
                agency_id=agency.id,
                campaign_id=campaign.id,
                subject=f"[{campaign.name}] FOIA Request",
                body=campaign.request_template,
                delivery_method=agency.preferred_method or DeliveryMethod.EMAIL,
                status=RequestStatus.DRAFT,
                fee_waiver_requested=True,
            )
            session.add(request)
            _row(
                table,
                req_num,
                agency.abbreviation or agency.name[:30],
                participant.name,
                "draft",
            )
            created += 1

        console.print(table)
        rprint(f"\n[green]Created {created} requests[/green] (skipped {skipped} already assigned)")
        rprint("[dim]Participants can send their requests with 'openfoia request send'.[/dim]")


@campaign_app.command("progress")
def campaign_progress(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)"),
):
    """Show per-participant, per-agency status grid for a campaign."""
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(Campaign.id.like(f"{campaign_id}%")).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{_safe(campaign_id)}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(
                f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]"
            )
            raise typer.Exit(1)

        campaign = matches[0]
        requests = campaign.requests
        participants = list(campaign.participants)

        if not participants:
            rprint(f"[yellow]No participants in campaign '{_safe(campaign.name)}'.[/yellow]")
            return

        if not requests:
            rprint(
                f"[yellow]No requests distributed yet. Run 'openfoia campaign distribute {campaign_id}'.[/yellow]"
            )
            return

        # Collect unique agencies from requests
        agency_ids = list({r.agency_id for r in requests})
        agencies = session.query(AgencyModel).filter(AgencyModel.id.in_(agency_ids)).all()
        agency_map = {a.id: (a.abbreviation or a.name[:15]) for a in agencies}

        # Build status lookup: (participant_id, agency_id) -> status
        status_map = {}
        for r in requests:
            status_map[(r.requester_id, r.agency_id)] = r.status.value

        # Status symbols
        STATUS_SYMBOLS = {
            "draft": "[dim]draft[/dim]",
            "pending_send": "[yellow]pending[/yellow]",
            "sent": "[cyan]sent[/cyan]",
            "acknowledged": "[cyan]ack[/cyan]",
            "processing": "[blue]proc[/blue]",
            "complete": "[green]done[/green]",
            "denied": "[red]denied[/red]",
            "partial_response": "[green]partial[/green]",
        }

        rprint(f"\n[bold]Campaign: {_safe(campaign.name)}[/bold]")
        rprint(
            f"Participants: {len(participants)} | Agencies: {len(agencies)} | Requests: {len(requests)}"
        )

        table = Table(title="Progress Grid")
        table.add_column("Participant", style="cyan")

        for aid in agency_ids:
            table.add_column(agency_map.get(aid, aid[:8]), justify="center")

        for p in participants:
            row = [p.name]
            for aid in agency_ids:
                status = status_map.get((p.id, aid))
                if status:
                    row.append(STATUS_SYMBOLS.get(status, status))
                else:
                    row.append("[dim]-[/dim]")
            table.add_row(*row)

        console.print(table)

        # Summary stats
        total = len(requests)
        sent_count = sum(1 for r in requests if r.status.value not in ("draft", "pending_send"))
        complete_count = sum(1 for r in requests if r.status.value == "complete")
        denied_count = sum(1 for r in requests if r.status.value == "denied")

        rprint(
            f"\n  Sent: {sent_count}/{total} | Complete: {complete_count}/{total} | Denied: {denied_count}/{total}"
        )


# === Analyze Commands ===


@analyze_app.command("extract")
@_csv_stdout_safe
def analyze_extract(
    document_id: str = typer.Argument(..., help="Document ID to analyze"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output file"),
    force: bool = typer.Option(
        False, "--force", help="Re-extract even if already done (earlier runs are kept)"
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="LLM model (e.g. llama3.1:8b, llama3.2:3b)"
    ),
    ensemble: bool = typer.Option(
        False, "--ensemble", help="Run all available NER backends together"
    ),
    tier: str | None = typer.Option(
        None,
        "--tier",
        help="Force one backend: gliner, spacy, regex (never call an LLM) or llm. "
        "Errors if it is not installed or reachable.",
    ),
    ner_model: str | None = typer.Option(
        None,
        "--ner-model",
        help="GLiNER-tier model: english (default) or multilingual (needs "
        "'openfoia install-extras ner-multilingual'). Overrides entities.ner_model "
        "and OPENFOIA_NER_MODEL.",
    ),
    csv_path: str | None = typer.Option(None, "--csv", help=CSV_HELP),
    prefer_run: str | None = typer.Option(
        None,
        "--prefer-run",
        help="Use this earlier run's entities for the document (see 'analyze runs'). "
        "Extracts nothing.",
    ),
):
    """Extract entities from a document.

    Pipeline: regex + NER → merge → LLM validation (if available).
    Use --ensemble to run ALL NER backends (GLiNER + spaCy) together.

    Each extraction is stored as a run. --force adds a new run and keeps the
    earlier ones; graphs, crossref, exports and search use the newest run
    unless --prefer-run picks another. List runs with 'openfoia analyze runs'.

    The LLM step can take minutes per document on a local model. To skip it,
    pass --tier gliner (or spacy/regex), or set OPENFOIA_AI_PROVIDER=none,
    which disables the LLM even if an API key is configured in the config
    file or in OPENFOIA_ANTHROPIC_API_KEY / OPENFOIA_OPENAI_API_KEY.

    For documents not in English, --ner-model multilingual uses a GLiNER2
    model that finds more names in other languages and scripts (see
    docs/GUIDE.md, "Documents not in English").
    """
    from .db import get_db_path, get_session  # lazy: startup time
    from .extraction_runs import (  # lazy: startup time
        describe_extraction,
        record_run,
        resolve_run,
        set_current_run,
    )

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        doc = (
            session.query(Document)
            .filter((Document.id == document_id) | (Document.id.like(f"{document_id}%")))
            .first()
        )

        if not doc:
            rprint(f"[red]Document '{_safe(document_id)}' not found.[/red]")
            raise typer.Exit(1)

        if prefer_run:
            run = resolve_run(session, doc.id, prefer_run)
            if run is None:
                rprint(
                    f"[red]No single run '{prefer_run}' for this document. "
                    f"List them: openfoia analyze runs {doc.id[:8]}[/red]"
                )
                raise typer.Exit(1)
            set_current_run(session, run)
            rprint(
                f"[green]Using run {run.id[:8]} ({run.backend}, {run.entity_count} entities) "
                f"for {doc.filename}.[/green]"
            )
            return

        if not doc.extracted_text:
            rprint("[yellow]Document has no extracted text. Run OCR first:[/yellow]")
            rprint(f"[dim]openfoia docs ocr {_safe(doc.file_path)}[/dim]")
            raise typer.Exit(1)

        if doc.entities_extracted and not force:
            rprint(
                "[yellow]Entities already extracted for this document. Use --force to "
                "re-extract (earlier runs are kept).[/yellow]"
            )
            return

        rprint(f"[cyan]Extracting entities from {_safe(doc.filename)}...[/cyan]")

        # Run extraction
        from .pipeline.extract import (  # lazy: startup time
            NER_MODELS,
            TIERS,
            BackendUnavailableError,
            EntityExtractor,
        )

        if tier is not None and tier not in TIERS:
            rprint(f"[red]Unknown --tier '{_safe(tier)}'. Choose from: {', '.join(TIERS)}[/red]")
            raise typer.Exit(1)
        if ner_model is not None and ner_model not in NER_MODELS:
            rprint(
                f"[red]Unknown --ner-model '{_safe(ner_model)}'. Choose from: "
                f"{', '.join(NER_MODELS)}[/red]"
            )
            raise typer.Exit(1)
        if tier and ensemble:
            rprint("[red]--tier and --ensemble cannot be combined.[/red]")
            raise typer.Exit(1)

        allow_remote = _remote_ai_consent() if tier in (None, "llm") else None
        extractor = EntityExtractor(
            model=model, tier=tier, allow_remote=allow_remote, ner_model=ner_model
        )
        backend = tier if tier and tier != "llm" else extractor._resolve_backend()

        if backend == "regex" and not tier:
            rprint("[yellow]Using regex-only extraction (dates, money, emails, phones).[/yellow]")
            rprint("[yellow]For people and organizations, install a NER model:[/yellow]")
            if extractor.ner_model == "multilingual":
                rprint(
                    "[cyan]  openfoia install-extras ner-multilingual[/cyan] "
                    "[dim]GLiNER2, the multilingual model you selected[/dim]"
                )
            rprint(
                "[cyan]  openfoia install-extras ner        [/cyan] [dim]GLiNER (~2GB, best accuracy)[/dim]"
            )
            rprint(
                "[cyan]  openfoia install-extras ner-spacy  [/cyan] [dim]spaCy (~100MB, good accuracy)[/dim]"
            )
            rprint("[dim]Or run a local LLM: ollama pull llama3.2:3b[/dim]")
            rprint("")

        started_at = utcnow()
        try:
            result = asyncio.run(extractor.extract(doc.extracted_text, ensemble=ensemble))
        except BackendUnavailableError as e:
            rprint(f"[red]{_safe(e)}[/red]")
            raise typer.Exit(1) from None
        except Exception as e:
            rprint(f"[red]Extraction failed: {_safe(e)}[/red]")
            rprint("[dim]Ensure AI provider is configured: openfoia config --init[/dim]")
            raise typer.Exit(1) from None

        # The run row is written first; its entities and links reference it.
        run, rels_saved = record_run(
            session,
            doc,
            result,
            started_at=started_at,
            **describe_extraction(extractor, result.metadata, ensemble),
        )

        if not result.entities:
            rprint(f"[yellow]No entities found in document (run {run.id[:8]}).[/yellow]")
            if doc.entities_extracted:
                rprint(
                    "[dim]Earlier runs are kept. Go back to one: openfoia analyze extract "
                    f"{doc.id[:8]} --prefer-run <run>[/dim]"
                )
            if csv_path:
                _emit_table(Table("Type", "Entity", "Confidence"), csv_path)
            return

        doc.entities_extracted = True

        rprint(
            f"[green]Extracted {len(result.entities)} entities, {rels_saved} relationships "
            f"(run {run.id[:8]}, {run.backend})[/green]"
        )

        table = Table(title="Extracted Entities")
        table.add_column("Type", style="cyan")
        table.add_column("Entity")
        table.add_column("Confidence")

        for ent in result.entities:
            _row(
                table,
                ent.entity_type.value,
                ent.normalized_text,
                f"{ent.confidence:.0%}",
            )

        _emit_table(table, csv_path)

        if output:
            from .exports import record_export  # lazy: startup time

            record_export(output)
            output.write_text(
                json.dumps(
                    [
                        {
                            "type": e.entity_type.value,
                            "text": e.normalized_text,
                            "confidence": e.confidence,
                        }
                        for e in result.entities
                    ],
                    indent=2,
                )
            )
            rprint(f"\n[green]Entities saved to {_safe(output)}[/green]")


@analyze_app.command("runs")
@_csv_stdout_safe
def analyze_runs(
    document_id: str = typer.Argument(..., help="Document ID (or prefix)"),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Delete every run except the one in use, with their entities and relationships, "
        "and any other entities of the document that are not in use",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the --prune confirmation"),
    csv_path: str | None = typer.Option(None, "--csv", help=CSV_HELP),
):
    """List a document's entity extraction runs, oldest first.

    The run marked current is the one graphs, crossref, exports and search
    use: the newest, unless 'analyze extract <doc> --prefer-run <run>' picked
    another. --prune deletes the others and every entity of the document
    that is not in the current run.

    Examples:
        openfoia analyze runs 3f2a
        openfoia analyze extract 3f2a --prefer-run 9c41
        openfoia analyze runs 3f2a --prune
    """
    from .db import get_session  # lazy: startup time
    from .extraction_runs import prune_runs, runs_for_document  # lazy: startup time

    _require_db()
    with get_session() as session:
        doc = (
            session.query(Document)
            .filter((Document.id == document_id) | (Document.id.like(f"{document_id}%")))
            .first()
        )
        if not doc:
            rprint(f"[red]Document '{document_id}' not found.[/red]")
            raise typer.Exit(1)
        runs = runs_for_document(session, doc.id)

        if prune:
            stale = sum(1 for r in runs if not r.is_current)
            if not yes:
                rprint(
                    f"Delete {stale} earlier run(s) of [cyan]{doc.filename}[/cyan] with their "
                    "entities and relationships? The run in use is kept."
                )
                if not typer.confirm("Continue?", default=False):
                    rprint("[yellow]Cancelled.[/yellow]")
                    raise typer.Exit(1)
            deleted_runs, deleted_entities = prune_runs(session, doc.id)
            rprint(f"[green]Deleted {deleted_runs} run(s) and {deleted_entities} entities.[/green]")
            rprint(
                "[dim]Freed database pages are overwritten. Earlier exports, backups and "
                "filesystem copies are not; 'openfoia purge --secure' removes all data.[/dim]"
            )
            return

        table = Table(title=f"Extraction runs: {doc.filename}")
        for column in ("Current", "Run", "Backend", "Model", "Started (UTC)", "Entities"):
            table.add_column(column)
        for r in runs:
            table.add_row(
                "yes" if r.is_current else "",
                r.id,
                r.backend,
                r.model or "-",
                r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "unknown",
                str(r.entity_count),
            )
        if not runs:
            rprint(f"[dim]No extraction runs for {doc.filename}.[/dim]")
            if doc.entities_extracted:
                rprint(
                    "[dim]Its entities were stored before runs were recorded; the next "
                    "'analyze extract --force' keeps them as a run.[/dim]"
                )
            if csv_path:
                _emit_table(table, csv_path)
            return
        _emit_table(table, csv_path)
        if not csv_path:
            rprint(
                f"[dim]Use another run: openfoia analyze extract {doc.id[:8]} "
                "--prefer-run <run>[/dim]"
            )


@analyze_app.command("graphs")
def analyze_graphs_list():
    """List saved investigation graphs.

    Shows all named graphs saved in ~/.openfoia/graphs/.

    Examples:
        openfoia analyze graphs
    """
    from .db import get_data_dir  # lazy: startup time

    graphs_dir = get_data_dir() / "graphs"
    if not graphs_dir.exists():
        rprint(
            "[dim]No saved graphs yet. Use 'openfoia analyze graph --name <name>' to create one.[/dim]"
        )
        return

    html_files = sorted(graphs_dir.glob("*.html"))
    json_files = sorted(graphs_dir.glob("*.json"))

    if not html_files and not json_files:
        rprint("[dim]No saved graphs yet.[/dim]")
        return

    table = Table(title="Saved Graphs")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Size")
    table.add_column("Modified")

    seen = set()
    for f in html_files + json_files:
        name = f.stem
        if name in seen:
            continue
        seen.add(name)

        html_exists = (graphs_dir / f"{name}.html").exists()
        json_exists = (graphs_dir / f"{name}.json").exists()
        types = []
        if html_exists:
            types.append("HTML")
        if json_exists:
            types.append("JSON")

        size = f.stat().st_size
        size_str = f"{size / 1024:.0f}KB" if size > 1024 else f"{size}B"
        # Local time is intended: this is a file listing shown to the user.
        modified = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")  # noqa: DTZ006

        _row(table, name, " + ".join(types), size_str, modified)

    console.print(table)
    rprint("\n[dim]View a graph: openfoia analyze graph --name <name> --view[/dim]")
    rprint(f"[dim]Graphs stored in: {_safe(graphs_dir)}[/dim]")


@analyze_app.command("graph")
def analyze_graph(
    request_id: str | None = typer.Option(None, "--request", "-r", help="Analyze single request"),
    campaign_id: str | None = typer.Option(
        None, "--campaign", "-c", help="Analyze entire campaign"
    ),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Save as named graph (stored in ~/.openfoia/graphs/)"
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file for JSON (default: exports/graph.json in the data directory; "
        "ignored if --name is set)",
    ),
    view: bool = typer.Option(
        False, "--view", "-v", help="Open interactive HTML visualization in browser"
    ),
    no_text: bool = typer.Option(
        False,
        "--no-text",
        help="Omit full document text from the export (keeps entities, links and context snippets)",
    ),
    case_name: str | None = typer.Option(None, "--case", help="Scope to one case"),
    all_cases: bool = typer.Option(
        False, "--all", help="Ignore the active case and graph every document"
    ),
    mentions: tuple[str, str] | None = typer.Option(
        None,
        "--mentions",
        help="Two entity labels: list documents mentioning both, plus the shortest path "
        "between them in the saved --name graph. Nothing is exported.",
    ),
):
    """Build entity relationship graph from extracted entities.

    Use --name to save graphs by investigation name. Each investigation
    gets its own graph that you can revisit later.

    The exported .json and .html files are plaintext and live outside the
    encrypted database. By default they embed the full text of every document
    behind the graph, which is what makes the reader view work. Pass --no-text
    to export the structure without the document bodies.

    Examples:
        openfoia analyze graph --view                          # everything, open in browser
        openfoia analyze graph --name defense-contracts --view # save + view
        openfoia analyze graph --request REQ-001 --name epa    # filter + save
        openfoia analyze graph --no-text --name epa            # structure only, no doc bodies
        openfoia analyze graph --case "CIA PAS" --view         # one investigation
        openfoia analyze graph --all                           # ignore the active case
        openfoia analyze graphs                                # list saved graphs
    """
    from .db import get_db_path, get_session  # lazy: startup time
    from .extraction_runs import current_entities  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = current_entities(session.query(Entity))

        if request_id:
            # Accept either UUID or request number (e.g., REQ-20260322-ABC)
            matching_req_ids = session.query(ReqModel.id).filter(
                (ReqModel.id == request_id) | (ReqModel.request_number == request_id)
            )
            query = query.join(Document).filter(Document.request_id.in_(matching_req_ids))
        elif campaign_id:
            query = (
                query.join(Document)
                .join(RequestModel)
                .filter(RequestModel.campaign_id.like(f"{campaign_id}%"))
            )

        case = None
        if request_id:
            scope = f"request {request_id}"
        elif campaign_id:
            scope = f"campaign {campaign_id}"
        else:
            from .cases import get_active_case, get_active_case_id  # lazy: startup time

            if case_name:
                case = _resolve_case_or_exit(session, case_name)
            elif not all_cases:
                case = get_active_case(session)
                if case is None and get_active_case_id():
                    rprint("[yellow]Active case not found; run `openfoia case use`.[/yellow]")
            if case:
                query = (
                    query.join(Document).join(RequestModel).filter(RequestModel.case_id == case.id)
                )
                scope = f"case '{_safe(case.name)}'"
            else:
                scope = "all documents (every investigation in this database)"
        rprint(f"[dim]Scope: {scope}[/dim]")

        entities = query.all()

        if mentions:
            if case:
                doc_ids = _case_document_ids(session, case.id)
            else:
                doc_ids = [r[0] for r in session.query(Document.id)]
            _print_mentions(session, entities, mentions, name, doc_ids)
            return

        if not entities:
            if case:
                rprint(
                    f"[yellow]No entities in case {case.name}. "
                    "Run extraction or use --all.[/yellow]"
                )
            else:
                rprint("[yellow]No entities found. Run entity extraction first.[/yellow]")
            return

        entity_ids = {e.id for e in entities}

        # Build node data with document occurrences
        nodes = []
        for e in entities:
            node = {
                "id": e.id,
                "label": e.normalized_text,
                "type": e.entity_type.value,
                "confidence": e.confidence,
                "document_id": e.document_id,
                "raw_text": e.raw_text,
                "context": e.context or "",
                "page_number": e.page_number,
            }
            nodes.append(node)

        links_q = session.query(entity_links)
        if entity_ids:
            links_q = links_q.filter(
                entity_links.c.source_id.in_(entity_ids),
                entity_links.c.target_id.in_(entity_ids),
            )
        edges = [
            {
                "source": link.source_id,
                "target": link.target_id,
                "type": link.link_type,
            }
            for link in links_q.all()
        ]

        # Enrich with document metadata for the reader view
        doc_ids = {e.document_id for e in entities if e.document_id}
        documents = {}
        if doc_ids:
            for doc in session.query(DocModel).filter(DocModel.id.in_(doc_ids)).all():
                req = session.get(ReqModel, doc.request_id) if doc.request_id else None
                source_url = _doc_source_url(doc, req)

                documents[doc.id] = {
                    "id": doc.id,
                    "filename": doc.filename or "Unknown",
                    "page_count": doc.page_count,
                    "text": "" if no_text else (doc.extracted_text or ""),
                    "request_id": doc.request_id,
                    "source_url": source_url,
                }

        graph_data = {"nodes": nodes, "edges": edges, "documents": documents}

        # Determine output paths
        if name:
            from .db import get_data_dir  # lazy: startup time

            graphs_dir = get_data_dir() / "graphs"
            graphs_dir.mkdir(parents=True, exist_ok=True)
            safe_name = _graph_safe_name(name)
            json_path = graphs_dir / f"{safe_name}.json"
            html_path = graphs_dir / f"{safe_name}.html"
        else:
            from .exports import record_export, resolve_output  # lazy: startup time

            json_path = resolve_output(output, "graph.json")
            html_path = json_path.with_suffix(".html")
            if view:
                record_export(html_path)

        json_path.write_text(json.dumps(graph_data, indent=2))

        rprint(f"[green]Graph exported to {_safe(json_path)}[/green]")
        # The graph page draws one node per (type, label); say so, so the two counts match.
        distinct = len({(n["type"], n["label"]) for n in nodes})
        rprint(f"  Entities: {distinct} ({len(nodes)} mentions)")
        rprint(f"  Relationships: {len(edges)}")
        if name:
            rprint(f"  Saved as: [cyan]{_safe(name)}[/cyan]")
        if no_text:
            rprint("  [dim]Document text omitted (--no-text). The reader view will be empty.[/dim]")
        elif documents:
            rprint(
                "[yellow]Note:[/yellow] this export embeds full document text in plaintext, "
                "outside the encrypted database — share deliberately, or re-run with --no-text."
            )

        if view:
            _generate_graph_html(graph_data, html_path)
            rprint(f"[green]Visualization: {_safe(html_path)}[/green]")

            webbrowser.open(f"file://{html_path.resolve()}")
            rprint("[cyan]Opened in browser.[/cyan]")


def _print_mentions(
    session, entities, labels: tuple[str, str], graph_name: str | None, doc_ids: list[str]
) -> None:
    """Documents where both labels occur (whole word, case-insensitive) and their pages.

    Extracted entities are checked first; documents whose entities miss a label
    are then checked against their stored page text, so a name the extractor
    never tagged still counts as a mention.
    """
    needles = [lbl.lower() for lbl in labels]
    patterns = [re.compile(rf"\b{re.escape(n)}\b") for n in needles]
    found: dict[str, list[set[int] | None]] = {}
    for e in entities:
        text = (e.normalized_text or e.raw_text or "").lower()
        for i, pattern in enumerate(patterns):
            if pattern.search(text):
                slot = found.setdefault(e.document_id, [None, None])
                slot[i] = slot[i] or set()
                if e.page_number:
                    slot[i].add(e.page_number)
    both = {d: hit for d, hit in found.items() if None not in hit}
    for doc_id in doc_ids:
        if doc_id in both:
            continue
        hit: list[set[int] | None] = [None, None]
        for i, (label, pattern) in enumerate(zip(labels, patterns, strict=True)):
            rows = session.query(DocumentPage.page_number, DocumentPage.text).filter(
                DocumentPage.document_id == doc_id,
                DocumentPage.text.ilike(f"%{_like_escape(label)}%", escape="\\"),
            )
            pages = {n for n, text in rows if pattern.search((text or "").lower())}
            if pages:
                hit[i] = pages
        if None not in hit:
            both[doc_id] = hit
    if not both:
        rprint(
            f"[yellow]No document in scope mentions both {_safe(repr(labels[0]))} and "
            f"{_safe(repr(labels[1]))}.[/yellow]"
        )
    for doc_id, hit in both.items():
        doc = session.get(Document, doc_id)
        req = session.get(RequestModel, doc.request_id) if doc.request_id else None
        cells = []
        for pages, label in zip(hit, labels, strict=True):
            if not pages:  # entities carry no page: look the label up in the stored pages
                rows = session.query(DocumentPage.page_number).filter(
                    DocumentPage.document_id == doc_id,
                    DocumentPage.text.ilike(f"%{_like_escape(label)}%", escape="\\"),
                )
                pages = {r[0] for r in rows}
            cells.append(
                f"{label} p.{','.join(map(str, sorted(pages)))}" if pages else f"{label} p.?"
            )
        rprint(
            f"[cyan]{doc_id[:8]}[/cyan]  {_safe(req.subject if req else doc.filename)}  "
            f"[dim]{_safe('; '.join(cells))}[/dim]"
        )

    if not graph_name:
        rprint("[dim]Pass --name <graph> to search a saved graph for a path between them.[/dim]")
        return
    from .db import get_data_dir  # lazy: startup time

    path = get_data_dir() / "graphs" / f"{_graph_safe_name(graph_name)}.json"
    if not path.exists():
        rprint(f"[yellow]No saved graph named {_safe(repr(graph_name))}.[/yellow]")
        return
    g = json.loads(path.read_text())
    label_of = {n["id"]: n["label"] for n in g["nodes"]}
    adj: dict[str, set[str]] = {}
    for edge in g["edges"]:
        adj.setdefault(edge["source"], set()).add(edge["target"])
        adj.setdefault(edge["target"], set()).add(edge["source"])
    starts = [i for i, lbl in label_of.items() if needles[0] in lbl.lower()]
    targets = {i for i, lbl in label_of.items() if needles[1] in lbl.lower()}
    prev: dict[str, str | None] = dict.fromkeys(starts)
    queue = list(starts)
    end = next((i for i in starts if i in targets), None)
    while queue and end is None:
        cur = queue.pop(0)
        for nxt in adj.get(cur, ()):
            if nxt not in prev:
                prev[nxt] = cur
                if nxt in targets:
                    end = nxt
                    break
                queue.append(nxt)
    if end is None:
        rprint(f"[yellow]No path between them in graph {_safe(repr(graph_name))}.[/yellow]")
        return
    chain = []
    while end is not None:
        chain.append(label_of[end])
        end = prev[end]
    rprint(f"Shortest path in {_safe(repr(graph_name))}: " + _safe(" -> ".join(reversed(chain))))


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _graph_safe_name(name: str) -> str:
    return name.lower().replace(" ", "-").replace("/", "-")


def _generate_graph_html(graph_data: dict, output_path: Path) -> None:
    """Generate a standalone HTML file with an interactive entity graph visualization."""
    from .graph_template import render  # lazy: startup time

    render(json.dumps(graph_data), output_path)


# === Entity Type Commands ===


def _load_config_data() -> tuple[Path, dict]:
    """Load config.json as raw dict."""
    from .db import get_data_dir  # lazy: startup time

    config_path = get_data_dir() / "config.json"
    data = json.loads(config_path.read_text()) if config_path.exists() else {}
    return config_path, data


def _save_config_data(config_path: Path, data: dict) -> None:
    """Save config dict to config.json."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2))


@entities_app.command("list")
def entities_list():
    """List custom entity types."""
    _, data = _load_config_data()
    custom_types = data.get("entities", {}).get("custom_types", [])

    if not custom_types:
        rprint("[dim]No custom entity types configured.[/dim]")
        rprint("[dim]Use 'openfoia entities add' or 'openfoia entities import' to add some.[/dim]")
        return

    table = Table(title=f"Custom Entity Types ({len(custom_types)})")
    table.add_column("Name", style="cyan")
    table.add_column("Pattern")
    table.add_column("Description")

    for ct in custom_types:
        _row(table, ct.get("name", ""), ct.get("pattern", ""), ct.get("description", ""))

    console.print(table)


@entities_app.command("add")
def entities_add(
    name: str = typer.Option(..., "--name", "-n", help="Entity type name (e.g., CONTRACT_NUMBER)"),
    pattern: str = typer.Option(..., "--pattern", "-p", help="Regex pattern to match"),
    description: str = typer.Option(
        "", "--description", "-d", help="What this entity type represents"
    ),
):
    """Add a custom entity type.

    Examples:
        openfoia entities add -n CONTRACT_NUMBER -p '\\b[A-Z]{2,4}-\\d{4,}-\\d{4,}\\b' -d "Federal contract numbers"
        openfoia entities add -n CASE_NUMBER -p '\\b\\d{2}-cv-\\d{4,}\\b' -d "Federal court case numbers"
    """
    # Validate the regex
    try:
        re.compile(pattern)
    except re.error as e:
        rprint(f"[red]Invalid regex pattern: {_safe(e)}[/red]")
        raise typer.Exit(1) from None

    name = name.upper().replace(" ", "_")

    config_path, data = _load_config_data()
    entities_conf = data.setdefault("entities", {})
    custom_types = entities_conf.setdefault("custom_types", [])

    # Check for duplicate
    existing = [ct for ct in custom_types if ct.get("name") == name]
    if existing:
        rprint(
            f"[yellow]Entity type '{name}' already exists. Use 'openfoia entities remove' first.[/yellow]"
        )
        raise typer.Exit(1)

    custom_types.append({"name": name, "pattern": pattern, "description": description})
    _save_config_data(config_path, data)

    rprint(f"[green]Added entity type '{_safe(name)}'[/green]")
    rprint(f"  Pattern: {_safe(pattern)}")
    rprint(f"  Description: {_safe(description)}")


@entities_app.command("remove")
def entities_remove(
    name: str = typer.Argument(..., help="Entity type name to remove"),
):
    """Remove a custom entity type."""
    name = name.upper().replace(" ", "_")

    config_path, data = _load_config_data()
    custom_types = data.get("entities", {}).get("custom_types", [])

    original_len = len(custom_types)
    custom_types = [ct for ct in custom_types if ct.get("name") != name]

    if len(custom_types) == original_len:
        rprint(f"[yellow]Entity type '{_safe(name)}' not found.[/yellow]")
        raise typer.Exit(1)

    data["entities"]["custom_types"] = custom_types
    _save_config_data(config_path, data)
    rprint(f"[green]Removed entity type '{_safe(name)}'[/green]")


def _fuzzy_match_column(header: str) -> str | None:
    """Fuzzy-match a CSV column header to one of: name, pattern, description."""
    h = header.strip().lower().replace(" ", "_").replace("-", "_")
    name_words = {
        "name",
        "type",
        "entity",
        "entity_name",
        "entity_type",
        "label",
        "category",
        "kind",
    }
    pattern_words = {
        "pattern",
        "regex",
        "regexp",
        "match",
        "expression",
        "rule",
        "format",
        "match_pattern",
    }
    desc_words = {
        "description",
        "desc",
        "notes",
        "note",
        "comment",
        "info",
        "detail",
        "details",
        "what",
        "meaning",
    }
    if h in name_words:
        return "name"
    if h in pattern_words:
        return "pattern"
    if h in desc_words:
        return "description"
    return None


def _remote_ai_consent() -> bool | None:
    """Ask before text goes to an AI provider that is not on this machine.

    Returns True or False for the user's answer, or None to leave the decision
    to config (``ai.allow_remote``): when the provider is local, already
    allowed, refused anyway (Tor mode), or nobody is at a terminal to answer.
    A warning you cannot answer is not consent.
    """
    from .config import load_config  # lazy: startup time
    from .pipeline.extract import remote_llm_host, remote_llm_warning  # lazy: startup time

    cfg = load_config()
    host = remote_llm_host(cfg.ai.provider, cfg.ai.base_url)
    if host is None or cfg.ai.allow_remote or cfg.network.tor or not sys.stdin.isatty():
        return None
    rprint(f"[yellow]{_safe(remote_llm_warning(cfg.ai.provider, cfg.ai.base_url))}[/yellow]")
    return typer.confirm(f"Send document text to {safe_text(host)}?", default=False)


def _helper_llm_allowed(cfg) -> bool:
    """The entities-import helpers' consent check: same rule as EntityExtractor."""
    from .pipeline.extract import remote_llm_refusal, remote_llm_warning  # lazy: startup time

    refusal = remote_llm_refusal(
        cfg.ai.provider, cfg.ai.base_url, allow_remote=cfg.ai.allow_remote, tor=cfg.network.tor
    )
    message = refusal or remote_llm_warning(cfg.ai.provider, cfg.ai.base_url)
    if message:
        typer.echo(safe_text(message), err=True)
    return refusal is None


def _llm_map_columns(headers: list[str], sample_rows: list[list[str]]) -> dict[str, int] | None:
    """Use the configured LLM to figure out which columns map to name/pattern/description."""
    from .config import load_config  # lazy: startup time
    from .pipeline.extract import _call_ollama, _llm_available  # lazy: startup time

    cfg = load_config()
    if not _helper_llm_allowed(cfg):
        return None
    if not _llm_available(cfg.ai.provider, cfg.ai.api_key, cfg.ai.base_url):
        return None

    # Build a preview of the data
    preview = "Headers: " + " | ".join(headers) + "\n"
    for row in sample_rows[:5]:
        preview += " | ".join(row) + "\n"

    # Number the columns explicitly
    col_list = "\n".join(f'  Column {i}: "{h}"' for i, h in enumerate(headers))

    prompt = f"""A CSV has these columns:
{col_list}

Here are some sample rows:
{preview}
I need to know which column number contains each of these 3 things:
1. "name" = the label for the entity type (like "Grant ID", "Wire Transfer")
2. "pattern" = how to find it in text (regex or English description like "looks like XX-1234")
3. "description" = why it matters or what it means

Example: if column 0 has names, column 1 has patterns, column 2 has descriptions:
{{"name": 0, "pattern": 1, "description": 2}}

What are the correct column numbers for this CSV? Reply with ONLY the JSON."""

    try:
        if cfg.ai.provider == "ollama":
            resp = _call_ollama(prompt, cfg.ai.model, cfg.ai.base_url, 0.1, 200)
        else:
            return None  # only use local LLM for this

        match = re.search(r"\{[^}]+\}", resp)
        if match:
            mapping = json.loads(match.group())
            return {k: int(v) for k, v in mapping.items() if int(v) >= 0}
    except Exception:
        pass
    return None


def _llm_generate_regex(description: str) -> str | None:
    """Use LLM to generate a regex from a plain English pattern description.

    Validates the output by checking that the generated regex:
    1. Is valid regex
    2. Matches at least one example from the description (if examples are present)
    3. Is reasonably short (not hallucinated garbage)
    """
    from .config import load_config  # lazy: startup time
    from .pipeline.extract import _llm_available  # lazy: startup time

    cfg = load_config()
    if not _helper_llm_allowed(cfg):
        return None
    if not _llm_available(cfg.ai.provider, cfg.ai.api_key, cfg.ai.base_url):
        return None

    prompt = f"""Write a Python regex that matches this pattern.

Description: {description}

Rules:
- Return ONLY the regex, one line, no explanation
- Use \\b for word boundaries
- Use \\d for digits, [A-Z] for uppercase letters
- Keep it simple and short

Examples:
  "looks like GR-2024-00456" → \\bGR-\\d{{4}}-\\d{{5}}\\b
  "starts with WT then 8 digits" → \\bWT\\d{{8}}\\b
  "two letters dash two digits like CA-12" → \\b[A-Z]{{2}}-\\d{{2}}\\b
  "starts with LR- then 6 digits" → \\bLR-\\d{{6}}\\b

Your regex:"""

    try:
        if cfg.ai.provider == "ollama":
            # Call ollama WITHOUT json format — we want raw text for regex
            import urllib.request  # lazy: startup time

            url = (cfg.ai.base_url or "http://localhost:11434").rstrip("/")
            payload = json.dumps(
                {
                    "model": cfg.ai.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 80},
                }
            ).encode()
            req = urllib.request.Request(
                f"{url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as http_resp:
                body = json.loads(http_resp.read())
            resp = body.get("response", "")
        else:
            return None

        # Clean up: take only the first line, strip quotes/backticks/json
        lines = resp.strip().split("\n")
        pattern = lines[0].strip().strip("`\"'{}").strip()

        # Remove JSON artifacts if the LLM wrapped it
        if ":" in pattern and pattern.startswith("{"):
            return None
        if len(pattern) > 100:
            return None  # hallucinated garbage

        if not pattern or not any(c in pattern for c in r"\[]()+*?{}dswb"):
            return None  # doesn't look like regex

        re.compile(pattern)  # validate

        # Sanity check: try to find an example in the description and see if regex matches
        # Extract things that look like examples (alphanumeric patterns with dashes/digits)
        examples = re.findall(r"[A-Z]{1,4}[-]?\d{2,}[-]?\d*[-]?[A-Z]?[-]?\d*", description)
        if examples:
            matched_any = any(re.search(pattern, ex) for ex in examples)
            if not matched_any:
                return None  # regex doesn't match its own examples

        return pattern
    except Exception:
        pass
    return None


@entities_app.command("import")
def entities_import(
    file: Path = typer.Argument(..., help="CSV or Excel file with entity types"),
    smart: bool = typer.Option(
        True, "--smart/--no-smart", help="Use AI to map columns and generate regex"
    ),
):
    """Import custom entity types from a CSV file.

    Columns can be named anything — the tool will figure out which is which.
    If a local LLM is running (ollama), it will:
    - Auto-detect which columns contain names, patterns, and descriptions
    - Generate regex patterns from plain English descriptions

    Without an LLM, falls back to fuzzy column name matching.

    Examples:
        openfoia entities import my_entities.csv
        openfoia entities import spreadsheet.csv --no-smart
    """
    if not file.exists():
        rprint(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    config_path, data = _load_config_data()
    entities_conf = data.setdefault("entities", {})
    custom_types = entities_conf.setdefault("custom_types", [])
    existing_names = {ct.get("name") for ct in custom_types}

    added = 0
    skipped = 0
    errors = 0
    ai_generated = 0

    with open(file, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        all_rows = list(reader)

    if not all_rows:
        rprint("[yellow]File is empty.[/yellow]")
        return

    # Detect header: if the first row has values that look like column names
    has_header = False
    if all_rows:
        first_row = all_rows[0]
        looks_like_header = all(
            not val.strip().replace(".", "").replace("-", "").isdigit()
            and len(val.strip()) < 50
            and not val.strip().startswith("\\")
            for val in first_row
            if val.strip()
        )
        # Also check: does first row have any common header-like words?
        header_words = {
            "name",
            "type",
            "pattern",
            "regex",
            "description",
            "desc",
            "notes",
            "label",
            "category",
            "format",
            "rule",
            "entity",
            "match",
            "comment",
        }
        has_header_word = any(
            val.strip().lower().replace(" ", "_") in header_words for val in first_row
        )
        has_header = looks_like_header and (has_header_word or len(first_row) >= 2)

    # --- Column mapping ---
    header = []
    data_rows = all_rows
    name_idx, pattern_idx, desc_idx = 0, 1, 2

    if has_header:
        header = [h.strip() for h in all_rows[0]]
        data_rows = all_rows[1:]

        rprint(f"[cyan]Columns found: {_safe(', '.join(header))}[/cyan]")

        # Try fuzzy matching first
        col_map: dict[str, int] = {}
        for i, h in enumerate(header):
            match = _fuzzy_match_column(h)
            if match and match not in col_map:
                col_map[match] = i

        # If fuzzy didn't get all 3, try LLM
        if smart and len(col_map) < 2:
            rprint("[cyan]Columns don't match expected names. Asking AI to figure it out...[/cyan]")
            llm_map = _llm_map_columns(header, data_rows[:5])
            if llm_map:
                col_map = llm_map
                rprint("[green]AI mapped columns:[/green]")
                for field, idx in col_map.items():
                    rprint(f"  {_safe(field)} → column '{_safe(header[idx])}' (index {idx})")
            else:
                rprint(
                    "[yellow]No AI available. Using first 3 columns as name, pattern, description.[/yellow]"
                )
        elif len(col_map) >= 2:
            rprint("[green]Auto-mapped columns:[/green]")
            for field, idx in col_map.items():
                rprint(f"  {field} → column '{_safe(header[idx])}'")

        name_idx = col_map.get("name", 0)
        pattern_idx = col_map.get("pattern", 1)
        desc_idx = col_map.get("description", 2 if len(header) > 2 else -1)
    else:
        rprint("[dim]No header row detected. Using columns: 1=name, 2=pattern, 3=description[/dim]")

    # --- Process rows ---
    for row_num, row in enumerate(data_rows, start=2 if has_header else 1):
        if len(row) < 2:
            continue

        name_val = row[name_idx].strip().upper().replace(" ", "_") if len(row) > name_idx else ""
        pattern_val = row[pattern_idx].strip() if len(row) > pattern_idx else ""
        desc_val = row[desc_idx].strip() if desc_idx >= 0 and len(row) > desc_idx else ""

        if not name_val:
            continue

        # Detect if pattern_val is plain English rather than regex.
        # Plain English patterns have spaces and common words; real regex has
        # metacharacters like \b, \d, [, ], +, *, {, }
        is_plain_english = False
        if pattern_val:
            has_spaces = " " in pattern_val
            has_regex_chars = bool(re.search(r"[\\[{()+*?|^$]", pattern_val))
            word_count = len(pattern_val.split())
            is_plain_english = has_spaces and not has_regex_chars and word_count >= 3

            if not is_plain_english:
                try:
                    re.compile(pattern_val)
                except re.error:
                    is_plain_english = True  # invalid regex, treat as English

        if is_plain_english and smart and pattern_val:
            rprint(
                f"[cyan]  '{_safe(name_val)}': pattern looks like English, generating regex...[/cyan]"
            )
            generated = _llm_generate_regex(pattern_val)
            if generated:
                rprint(f"[green]    Generated: {_safe(generated)}[/green]")
                # Use the original text as description if no description exists
                if not desc_val:
                    desc_val = pattern_val
                pattern_val = generated
                ai_generated += 1
            else:
                rprint(
                    f"[red]  Row {row_num}: '{_safe(pattern_val)}' is not valid regex "
                    "and AI couldn't generate one[/red]"
                )
                errors += 1
                continue
        elif not pattern_val:
            rprint(f"[red]  Row {row_num}: no pattern for '{_safe(name_val)}'[/red]")
            errors += 1
            continue

        # Final validation
        try:
            re.compile(pattern_val)
        except re.error as e:
            rprint(f"[red]  Row {row_num}: invalid regex for '{_safe(name_val)}': {_safe(e)}[/red]")
            errors += 1
            continue

        if name_val in existing_names:
            rprint(f"[dim]  Skipped '{_safe(name_val)}' (already exists)[/dim]")
            skipped += 1
            continue

        custom_types.append({"name": name_val, "pattern": pattern_val, "description": desc_val})
        existing_names.add(name_val)
        added += 1

    _save_config_data(config_path, data)

    rprint(f"\n[green]Imported {added} entity type(s)[/green]")
    if ai_generated:
        rprint(f"[cyan]{ai_generated} regex pattern(s) generated by AI from plain English[/cyan]")
    if skipped:
        rprint(f"[yellow]Skipped {skipped} (already existed)[/yellow]")
    if errors:
        rprint(f"[red]{errors} error(s)[/red]")


@entities_app.command("export")
def entities_export(
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output CSV file (default: exports/entity_types.csv in the data directory)",
    ),
):
    """Export custom entity types to a CSV file."""
    from .exports import resolve_output  # lazy: startup time

    _, data = _load_config_data()
    custom_types = data.get("entities", {}).get("custom_types", [])

    if not custom_types:
        rprint("[dim]No custom entity types to export.[/dim]")
        return

    output = resolve_output(output, "entity_types.csv")
    with open(output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "pattern", "description"])
        for ct in custom_types:
            writer.writerow([ct.get("name", ""), ct.get("pattern", ""), ct.get("description", "")])

    rprint(f"[green]Exported {len(custom_types)} entity type(s) to {output}[/green]")


@entities_app.command("test")
def entities_test(
    text: str = typer.Option(None, "--text", "-t", help="Test text (or reads from stdin)"),
    file: Path | None = typer.Option(None, "--file", "-f", help="Test against a file"),
):
    """Test your custom entity types against sample text.

    Shows which entities each pattern finds.

    Examples:
        openfoia entities test -t "Contract FA8726-24-C-0012 awarded to Acme Corp"
        echo "some text" | openfoia entities test
        openfoia entities test -f document.txt
    """
    if file:
        text = file.read_text()
    elif not text:
        rprint("[dim]Reading from stdin (Ctrl+D when done)...[/dim]")
        text = sys.stdin.read()

    _, data = _load_config_data()
    custom_types = data.get("entities", {}).get("custom_types", [])

    if not custom_types:
        rprint(
            "[yellow]No custom entity types configured. Use 'openfoia entities add' first.[/yellow]"
        )
        raise typer.Exit(1)

    table = Table(title="Entity Type Test Results")
    table.add_column("Type", style="cyan")
    table.add_column("Matches Found")
    table.add_column("Examples")

    total_matches = 0
    for ct in custom_types:
        name = ct.get("name", "")
        pattern = ct.get("pattern", "")
        try:
            matches = re.findall(pattern, text)
        except re.error:
            table.add_row(safe_cell(name), "[red]INVALID REGEX[/red]", "")
            continue

        unique_matches = sorted(set(matches))
        total_matches += len(unique_matches)
        examples = ", ".join(unique_matches[:5])
        if len(unique_matches) > 5:
            examples += f" ... +{len(unique_matches) - 5} more"

        count_str = f"[green]{len(unique_matches)}[/green]" if unique_matches else "[dim]0[/dim]"
        table.add_row(
            safe_cell(name), count_str, safe_cell(examples) if examples else "[dim]none[/dim]"
        )

    console.print(table)
    rprint(
        f"\n[cyan]{total_matches} total matches across {len(custom_types)} entity type(s)[/cyan]"
    )


# === Deadline Commands ===


def _days_left(when: datetime) -> str:
    from .clocks import is_overdue  # lazy: startup time

    now = _utcnow()
    if is_overdue(when, now):
        n = (now.date() - when.date()).days
        return f"[red]{n} day{'s' if n != 1 else ''} over[/red]"
    n = (when.date() - now.date()).days
    color = "yellow" if n <= 5 else "green"
    return f"[{color}]{n} day{'s' if n != 1 else ''} left[/{color}]"


def _parse_day(text: str | None, flag: str) -> datetime | None:
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")  # noqa: DTZ007  naive UTC, like every column
    except ValueError:
        rprint(f"[red]{flag} must be a date like 2026-10-14.[/red]")
        raise typer.Exit(2) from None


@deadline_app.command("list")
@_csv_stdout_safe
def deadline_list(
    show_all: bool = typer.Option(False, "--all", help="Include completed/closed requests"),
    csv_path: str | None = typer.Option(None, "--csv", help=CSV_HELP),
):
    """Show every clock a requester must not miss, overdue first.

    Response deadlines come from each agency's jurisdiction rule (federal
    FOIA's 20 business days, Michigan's 5, or none when the statute sets
    no clock). Appeal windows and "still interested" replies are listed too.
    """
    from .db import get_db_path, get_session  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        rows, no_clock, unsent = _collect_clocks(session, show_all)
        if not rows and not no_clock and not unsent:
            rprint("[dim]No active requests with deadlines.[/dim]")
            if not csv_path:
                return
        from .clocks import is_overdue  # lazy: startup time

        def clock_table(group) -> Table:
            table = Table()
            table.add_column("Request #", style="cyan")
            table.add_column("Agency")
            table.add_column("Clock")
            table.add_column("Due")
            table.add_column("Days")
            table.add_column("Law", style="dim")
            for kind, r, when, law in group:
                table.add_row(
                    r.request_number,
                    r.agency.abbreviation or r.agency.name,
                    kind,
                    when.strftime("%Y-%m-%d"),
                    Text.from_markup(_days_left(when)),
                    law,
                )
            return table

        if csv_path:
            # One sheet: rows are sorted by due date, so overdue clocks come first.
            table = clock_table(rows)
            for r, law in no_clock:
                table.add_row(
                    r.request_number, r.agency.abbreviation or r.agency.name, NO_CLOCK, "", "", law
                )
            _emit_table(table, csv_path)
        else:
            overdue = [r for r in rows if is_overdue(r[2], _utcnow())]
            upcoming = [r for r in rows if not is_overdue(r[2], _utcnow())]
            for title, style, group in (
                ("OVERDUE", "red", overdue),
                ("UPCOMING", "yellow", upcoming),
            ):
                if group:
                    rprint(f"\n[bold {style}]{title} ({len(group)})[/bold {style}]")
                    console.print(clock_table(group))
            if no_clock:
                rprint(f"\n[bold]NO STATUTORY DEADLINE ({len(no_clock)})[/bold]")
                for r, law in no_clock:
                    rprint(
                        f"  {r.request_number}  {r.agency.abbreviation or r.agency.name}  "
                        f"[yellow]{NO_CLOCK}[/yellow]  [dim]{law}[/dim]"
                    )
            if unsent:
                rprint(f"\n[dim]{len(unsent)} draft/unsent requests (no clock running)[/dim]")
            rprint("")
        session.query(RequestModel).count()  # keep the session's pending due_date fills


def _collect_clocks(session, show_all: bool = False):
    """(kind, request, due, law) rows for every running clock, plus no-deadline and unsent lists.

    Clocks are recomputed from the jurisdiction every time; the stored due_date is a cache.
    """
    from .clocks import request_clocks  # lazy: startup time

    active = [
        RequestStatus.SENT,
        RequestStatus.ACKNOWLEDGED,
        RequestStatus.PROCESSING,
        RequestStatus.FEE_ESTIMATE,
        RequestStatus.DENIED,
        RequestStatus.APPEALED,
    ]
    query = session.query(RequestModel)
    if not show_all:
        query = query.filter(RequestModel.status.in_(active))
    rows, no_clock, unsent = [], [], []
    for r in query.order_by(RequestModel.sent_at).all():
        if not r.sent_at:
            unsent.append(r)
            continue
        c = request_clocks(r)
        r.due_date = c.response_due
        label = c.law["id"] + (" (assumed)" if c.fallback else "")
        if c.response_due:
            rows.append((c.response_kind, r, c.response_due, label))
        elif c.response_reason:
            no_clock.append((r, f"{label}: {c.response_reason}"))
        if c.appeal_due:
            rows.append(("appeal", r, c.appeal_due, label))
        elif c.appeal_reason:
            no_clock.append((r, f"{label}: {c.appeal_reason}"))
        if c.still_interested_due:
            rows.append(("still interested", r, c.still_interested_due, label))
    rows.sort(key=lambda x: x[2])
    return rows, no_clock, unsent


@deadline_app.command("check")
def deadline_check():
    """Print one line per overdue clock. Exit code 1 if any.

    Useful for cron jobs or shell startup. Example (add to .bashrc):
        openfoia deadlines check 2>/dev/null
    """
    from .db import get_db_path, get_session  # lazy: startup time

    if not get_db_path().exists():
        return
    with get_session() as session:
        rows, _, _ = _collect_clocks(session)
        from .clocks import is_overdue  # lazy: startup time

        overdue = [x for x in rows if is_overdue(x[2], _utcnow())]
        for kind, r, when, _law in overdue:
            days_over = (_utcnow().date() - when.date()).days
            rprint(
                f"[red]OVERDUE {kind}:[/red] {r.request_number}: {r.subject} (+{days_over} day{'s' if days_over != 1 else ''})"
            )
        if overdue:
            rprint(
                f"\n[red]{len(overdue)} overdue clock(s). Run 'openfoia deadlines list' for details.[/red]"
            )
            raise typer.Exit(1)


# === Egress (Tor) Helpers ===
#
# Shared by every command that touches the network through openfoia.net's
# egress choke point (crossref, ingest/web fetch). Keeps the "opt-in, fail
# closed, be honest" contract in one place instead of re-implemented per
# command.


def _egress_policy_from(config, *, tor: bool | None = None) -> EgressPolicy:
    """Build an EgressPolicy from config.network, with an optional CLI override.

    tor=None uses the configured default (config.network.tor). tor=True or
    tor=False overrides that default for this invocation only — e.g. a CLI
    `--tor/--no-tor` flag left unset by the user should pass tor=None here so
    the configured default wins.
    """
    from .net import EgressMode, EgressPolicy  # lazy: startup time

    use_tor = config.network.tor if tor is None else tor
    return EgressPolicy(
        mode=EgressMode.TOR if use_tor else EgressMode.DIRECT,
        tor_host=config.network.tor_host,
        tor_port=config.network.tor_port,
        isolate_streams=config.network.isolate_streams,
        max_attempts=config.network.max_attempts,
    )


def _check_tor_or_exit(policy: EgressPolicy) -> None:
    """Fail-closed Tor readiness gate.

    If *policy* is DIRECT this is a no-op. If it is TOR, probe the SOCKS
    port before any request is made; if it is not reachable, print a clear
    error and abort (typer.Exit) rather than silently falling through to a
    clearnet request — that silent fallback is exactly the deanonymization
    leak Principle 1 rules out.
    """
    from .net import check_tor  # lazy: startup time

    if not policy.is_tor:
        return

    if not asyncio.run(check_tor(policy)):
        typer.echo(
            f"Tor egress requested but the SOCKS proxy at "
            f"{policy.tor_host}:{policy.tor_port} is not reachable.\n"
            "Start Tor (e.g. `tor` / `sudo systemctl start tor`), or set "
            '"network": {"tor": false} in config.json to go direct.',
            err=True,
        )
        raise typer.Exit(1)


def _report_tor_unavailable() -> None:
    """Print the fix for TorUnavailableError (missing socksio) and exit."""
    rprint("[red]Tor egress requires the 'socksio' package, which is not installed.[/red]")
    rprint("[yellow]Run: openfoia install-extras tor[/yellow]")
    raise typer.Exit(1)


@app.command("egress-status")
def egress_status(
    tor: bool | None = typer.Option(
        None, "--tor/--no-tor", help="Check this mode instead of the configured default"
    ),
):
    """Show the current network egress policy, honestly.

    Reports whether requests go out DIRECT or via TOR, whether the Tor SOCKS
    proxy is actually reachable right now, and exactly what is and is not
    protected — see docs/THREAT_MODEL.md for the full picture.
    """
    from .config import load_config  # lazy: startup time
    from .net import check_tor, describe_egress  # lazy: startup time

    cfg = load_config()
    policy = _egress_policy_from(cfg, tor=tor)
    info = describe_egress(policy)

    rprint("[bold]Egress Configuration[/bold]")
    rprint(f"  Mode: {'tor' if policy.is_tor else 'direct'}")
    if policy.is_tor:
        reachable = asyncio.run(check_tor(policy))
        status = "[green]reachable[/green]" if reachable else "[red]NOT reachable[/red]"
        rprint(f"  Tor SOCKS proxy ({policy.tor_host}:{policy.tor_port}): {status}")
        rprint(f"  Stream isolation: {policy.isolate_streams}")
        rprint("  The destination servers will NOT see your real IP.")
    else:
        rprint("  The destination servers WILL see your real IP.")

    rprint("\n[bold]What this does NOT protect[/bold]")
    for item in info["not_protected"]:
        rprint(f"  - {item}")


# === Browse Command ===


@app.command()
def browse(
    url: str = typer.Argument(..., help="URL to navigate to"),
    tor: bool | None = typer.Option(
        None, "--tor/--no-tor", help="Route through Tor; default is network.tor in config"
    ),
    headless: bool = typer.Option(False, "--headless", help="Run browser without visible window"),
    save: bool = typer.Option(
        False, "--save", help="Extract page content and save to data directory"
    ),
):
    """Browse a URL with optional Tor routing and fingerprint hardening.

    Uses Playwright to launch a hardened Chromium instance. Traffic goes
    through the Tor SOCKS5 proxy when network.tor is set in config or --tor
    is passed (the Tor daemon must be running); --no-tor goes direct.

    Fingerprint hardening is always applied: WebGL disabled, WebRTC disabled,
    timezone set to UTC, and a common user-agent is used.

    \b
    Examples:
        openfoia browse https://example.com                 # Normal browsing
        openfoia browse https://example.onion --tor         # Tor browsing
        openfoia browse https://example.com --save          # Save page content
        openfoia browse https://example.com --tor --headless --save  # Headless Tor
    """
    from .config import load_config  # lazy: startup time
    from .tor_browse import browse as _browse  # lazy: startup time

    policy = _egress_policy_from(load_config(), tor=tor)
    _check_tor_or_exit(policy)
    typer.echo(f"Opening {url} ({_route_note(policy)}).", err=True)

    try:
        result = asyncio.run(
            _browse(
                url,
                use_tor=policy.is_tor,
                headless=headless,
                save=save,
            )
        )
    except SystemExit:
        raise typer.Exit(1) from None
    except Exception as e:
        rprint(f"[red]Browse failed:[/red] {_safe(e)}")
        raise typer.Exit(1) from None

    rprint(f"\n[cyan]Title:[/cyan] {_safe(result.get('title', 'N/A'))}")
    rprint(f"[cyan]URL:[/cyan]   {_safe(result.get('url', url))}")
    if result.get("saved_to"):
        rprint(f"[cyan]Saved:[/cyan] {_safe(result['saved_to'])}")


# === Purge Command ===


@app.command()
def purge(
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    secure: bool = typer.Option(
        False,
        "--secure",
        "-s",
        help="Overwrite files 3x with random data before deletion (forensic)",
    ),
    fill: bool = typer.Option(
        False, "--fill", help="Fill free disk space with random data after purge (slow)"
    ),
):
    """Remove all OpenFOIA data.

    Removes the whole data directory (database, ingested documents, exports,
    config) and the files you exported outside it with --output. This cannot
    be undone.

    For situations where you need everything off this machine, now.

    \b
    Modes:
      openfoia purge --yes          Fast delete (unlink). Not forensic.
      openfoia purge --secure       Overwrite all files 3x, scrub shell history.
      openfoia purge --secure --fill  Also fill free space with random data.
    """
    from openfoia.exports import recorded_exports  # lazy: startup time
    from openfoia.security import (  # lazy: startup time
        LIVE_SHELL_HISTORY_NOTE,
        ShredReport,
        clear_shell_history,
        fill_free_space,
        print_ssd_warning,
        secure_delete_dir,
        secure_delete_file,
    )

    from .db import get_data_dir as _get_data_dir  # lazy: startup time

    def path_line(style: str, path: object, note: str = "") -> None:
        # Paths are printed whole (no wrapping mid-name) and never read as
        # markup, so the user can copy exactly what is left on disk.
        tail = f" {escape(note)}" if note else ""
        console.print(f"  [{style}]{escape(str(path))}[/{style}]{tail}", soft_wrap=True)

    data_dir = _get_data_dir()

    if not data_dir.exists():
        rprint("[dim]Nothing to purge. No data directory found.[/dim]")
        return

    if fill and not secure:
        rprint("[red]--fill requires --secure.[/red]")
        raise typer.Exit(1)

    # Exports the user wrote outside the data directory (recorded when written).
    outside = [p for p in recorded_exports() if p.exists() or p.is_symlink()]

    if not confirm:
        rprint("\n[bold red]This will permanently destroy:[/bold red]")
        for db_file in sorted(data_dir.glob("*.db")):
            path_line("red", db_file)
        for sub, what in (
            ("docs", "ingested documents"),
            ("web", "archived web pages"),
            ("exports", "exports and downloads"),
            ("graphs", "saved graphs"),
        ):
            if (data_dir / sub).exists():
                path_line("red", f"{data_dir / sub}/", f"— {what}")
        if (data_dir / "config.json").exists():
            path_line("red", data_dir / "config.json", "— configuration and credentials")
        rprint("\n  [bold red]Everything in:[/bold red]")
        path_line("bold red", data_dir)
        if outside:
            rprint(f"\n[bold red]And {len(outside)} file(s) you exported outside it:[/bold red]")
            for p in outside:
                path_line("red", p)
        rprint(
            "\n[yellow]Not removed:[/yellow] the original files you ingested, copies you "
            "made or moved yourself, browser history and caches, swap, and OS indexes."
        )

        if secure:
            rprint("\n[yellow]Secure mode: files will be overwritten 3x with random data.[/yellow]")
            print_ssd_warning()
        else:
            rprint(
                "\n[yellow]Fast mode: files are unlinked, not overwritten; their contents "
                "may be recoverable from the disk. Shell history is not touched. "
                "Use --secure for that.[/yellow]"
            )
        if fill:
            rprint(
                "[yellow]Fill mode: free disk space will be overwritten. This may take a long time.[/yellow]"
            )

        rprint("\n[dim]This cannot be undone.[/dim]\n")

        answer = typer.prompt("Type PURGE to confirm", default="")
        if answer != "PURGE":
            rprint("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    report = ShredReport()
    if secure:
        print_ssd_warning()
        rprint("[bold]Secure-deleting all files (3-pass overwrite)...[/bold]")
        try:
            report.merge(secure_delete_dir(data_dir))
            for p in outside:
                report.merge(secure_delete_file(p))
        finally:
            # Scrub history even if deletion went wrong: those lines name the
            # subjects of the investigation.
            rprint("[bold]Scrubbing shell history...[/bold]")
            modified = clear_shell_history()
            if modified:
                rprint(f"[green]Cleaned {len(modified)} history file(s):[/green]")
                for hist in modified:
                    path_line("green", hist)
            else:
                rprint("[dim]No shell history entries found.[/dim]")
            rprint(f"[yellow]{LIVE_SHELL_HISTORY_NOTE}[/yellow]")

        rprint(f"[green]{len(report.shredded)} file(s) overwritten and deleted.[/green]")

        if fill:
            rprint(
                "[bold]Filling free disk space with random data (this will take a while)...[/bold]"
            )
            fill_dir = data_dir.parent / ".openfoia_fill"
            fill_free_space(fill_dir)
            rprint("[green]Free space filled and cleaned up.[/green]")
    else:
        shutil.rmtree(data_dir, ignore_errors=True)
        for p in outside:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                report.survived.append((str(p), exc.strerror or str(exc)))
        if data_dir.exists():
            for root, _dirs, files in os.walk(data_dir):
                for name in files:
                    report.survived.append((str(Path(root) / name), "could not be removed"))

    if report.unlinked_only:
        rprint(
            f"\n[bold red]{len(report.unlinked_only)} file(s) were deleted but NOT "
            "overwritten; their contents may still be recoverable:[/bold red]"
        )
        for p, why in report.unlinked_only:
            path_line("red", p, f"({why})")
    if report.survived:
        rprint(
            f"\n[bold red]{len(report.survived)} file(s) could NOT be removed and are "
            "still on disk:[/bold red]"
        )
        for p, why in report.survived:
            path_line("red", p, f"({why})")

    if data_dir.exists():
        rprint("\n[bold red]Data directory NOT fully removed:[/bold red]")
        path_line("bold red", data_dir)
    else:
        rprint("\n[green]Data directory removed:[/green]")
        path_line("green", data_dir)
    rprint(
        "[dim]Note: swap files, filesystem journals, browser caches, and OS-level caches are NOT affected.[/dim]"
    )
    rprint("[dim]For maximum safety, use full-disk encryption.[/dim]\n")
    if not report.ok:
        rprint(
            "[bold red]Purge incomplete: remove or overwrite the files above yourself.[/bold red]"
        )
        raise typer.Exit(1)


# === Web Ingest Command ===


@app.command("ingest")
def ingest_url(
    url: str = typer.Option(..., "--url", "-u", help="URL to fetch and ingest"),
    tor: bool | None = typer.Option(
        None,
        "--tor/--no-tor",
        help="Route through Tor SOCKS5 proxy (default: config, see 'openfoia egress-status')",
    ),
    output: Path | None = typer.Option(None, "--output", "-o", help="Save extracted text to file"),
):
    """Ingest a web page into the document pipeline.

    Fetches the URL, strips tracking scripts, extracts main content,
    and archives the HTML + text locally.

    Use --tor to route the request through the Tor network (requires
    Tor running on localhost:9050). --tor/--no-tor overrides config for this
    run only; with neither flag, config.network.tor decides.

    Examples:
        openfoia ingest --url https://example.gov/report.html
        openfoia ingest --url https://example.onion/docs --tor
    """
    from .config import load_config  # lazy: startup time
    from .db import get_data_dir  # lazy: startup time
    from .net import TorUnavailableError  # lazy: startup time
    from .pipeline.web import archive_url  # lazy: startup time

    cfg = load_config()
    policy = _egress_policy_from(cfg, tor=tor)
    _check_tor_or_exit(policy)

    storage_path = get_data_dir() / "web"

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        mode = " via Tor" if policy.is_tor else ""
        progress.add_task(f"Fetching{_safe(mode)}: {_safe(url)}", total=None)

        try:
            result = asyncio.run(
                archive_url(url, storage_path, use_tor=policy.is_tor, egress=policy)
            )
        except TorUnavailableError:
            _report_tor_unavailable()
        except Exception as e:
            rprint(f"[red]Failed to fetch URL: {_safe(e)}[/red]")
            if policy.is_tor:
                rprint("[dim]Make sure Tor is running: brew install tor && tor[/dim]")
            raise typer.Exit(1) from None

    rprint("\n[bold green]Archived web page[/bold green]")
    rprint("=" * 50)

    table = Table(show_header=False, box=None)
    table.add_column("Field", style="cyan", width=16)
    table.add_column("Value")

    _row(table, "Title", result.title)
    _row(table, "URL", result.url)
    _row(table, "Document ID", result.document_id[:12] + "...")
    _row(table, "Size", f"{result.file_size / 1024:.1f} KB")
    _row(table, "Text length", f"{len(result.text):,} chars")
    _row(table, "HTML saved", result.html_path)
    _row(table, "Text saved", result.text_path)
    _row(table, "Checksum", result.checksum[:16] + "...")
    if policy.is_tor:
        table.add_row("Tor", "Yes")

    console.print(table)

    if output:
        from .exports import record_export  # lazy: startup time

        record_export(output)
        output.write_text(result.text)
        rprint(f"\n[green]Text saved to {output}[/green]")

    rprint(f"\n[dim]Content stored in {storage_path}[/dim]")


# === Public Records Commands ===

#: Search filters only some adapters understand; other sources are skipped
#: rather than returning unfiltered results under a filtered query.
_FILTER_SOURCES = {
    "jurisdiction": ("opencorporates",),
    "filing_type": ("sec", "federalregister"),
    "agency": ("federalregister",),
    "since": ("federalregister",),
    "until": ("federalregister",),
    "docket": ("federalregister",),
}
#: Sources whose search is paged until the CLI's --limit, with their page-size cap.
_PAGED_SOURCES = {"documentcloud": 100, "federalregister": 1000}
#: Sources that cannot answer without a personal key: skipped in --all-sources without one.
_KEYED_SOURCES = {
    "nara": "OPENFOIA_NARA_API_KEY",
    "courtlistener": "OPENFOIA_COURTLISTENER_API_KEY",
}


def _courtlistener_note(requests: int = 1) -> str:
    """What a CourtListener call discloses and costs, for the notice before it is sent."""
    from .records.courtlistener import DAY_LIMIT, budget_used  # lazy: startup time

    used = budget_used()["day"]
    what = "1 request" if requests == 1 else f"up to {requests} requests"
    return (
        f"courtlistener.com with your API token, which identifies you; CourtListener logs "
        f"API searches against your account unless query logging is off in your profile. "
        f"Uses {what} of {DAY_LIMIT} a day ({used} used in the last 24 hours on this machine)."
    )


async def _search_one(adapter, source: str, query: str, limit: int, kwargs: dict[str, Any]):
    """Run one source's search, paging DocumentCloud and the Federal Register until *limit*."""
    if source not in _PAGED_SOURCES:
        return await adapter.search(query, **kwargs)

    per_page = min(limit, _PAGED_SOURCES[source])
    entities = []
    page = 1
    while True:
        result = await adapter.search(query, page=page, page_size=per_page, **kwargs)
        if result.error:
            # Keep pages 1..N-1; the attached error still fails the command.
            result.entities = entities[:limit]
            return result
        entities.extend(result.entities)
        if (
            len(entities) >= limit
            or len(result.entities) < per_page
            or len(entities) >= result.total_results
        ):
            break
        page += 1
    result.entities = entities[:limit]
    result.page = 1
    return result


def _compact_result(e) -> dict[str, Any]:
    x = e.extra_data
    snippet = (x.get("highlights") or [None])[0] or x.get("description") or x.get("snippet")
    created = next(
        (
            x.get(k)
            for k in (
                "created_at",
                "submitted",
                "filing_date",
                "date_issued",
                "posted_date",
                "publication_date",
                "date",
            )
            if x.get(k)
        ),
        None,
    )
    out = {
        "id": next(iter(e.identifiers.values()), None),
        "title": e.name,
        "source": e.source,
        "pages": x.get("pages"),
        "created": created,
        "url": e.source_url,
        "snippet": (snippet or "")[:200],
    }
    if "score" in x:
        out["score"] = x["score"]
    return out


def _rank(entities: list, keywords: list[str]) -> list:
    """Sort by keyword hits in title + snippet/highlights; score kept in extra_data."""
    for e in entities:
        x = e.extra_data
        hay = " ".join(
            [
                e.name,
                *(x.get("highlights") or []),
                x.get("description") or "",
                x.get("snippet") or "",
            ]
        ).lower()
        x["score"] = sum(hay.count(k) for k in keywords)
    return sorted(entities, key=lambda e: -e.extra_data["score"])


def _rank_keywords(rank: str | None) -> list[str]:
    return [k.strip().lower() for k in (rank or "").split(",") if k.strip()]


def _log_case_queries(rows: list[tuple[str, str, int | None]]) -> None:
    """Record searches against the active case. total None = the search failed."""
    from .cases import active_case_id_in_db  # lazy: startup time

    case_id = active_case_id_in_db()
    if not case_id:
        return
    from .db import get_session  # lazy: startup time

    with get_session() as session:
        for source, query, total in rows:
            session.add(CaseQuery(case_id=case_id, source=source, query=query, total_results=total))


def _records_search_all(
    query: str, kwargs: dict[str, Any], limit: int, json_lines: bool, rank: list[str]
) -> None:
    from .config import load_config  # lazy: startup time
    from .records import get_adapter, list_sources  # lazy: startup time

    sources = list_sources()
    policy = _records_policy_or_exit()
    data_gov_key = load_config().records.api_data_gov_key
    adapters = {s: get_adapter(s, egress=policy, api_data_gov_key=data_gov_key) for s in sources}

    def say(msg: str) -> None:
        # Status goes to stderr in --json mode so stdout stays pure JSON lines.
        # Error text can quote the upstream response, so it is cleaned too.
        _echo(msg, err=json_lines)

    local = [s for s in sources if getattr(adapters[s], "offline", False) is True]
    remote = [s for s in sources if s not in local]
    say(
        f"Sending '{query}' to {len(remote)} services "
        f"({_route_note(policy, *adapters.values())}): "
        f"{', '.join(remote)}"
    )
    if local:
        say(f"Searched on this machine only, nothing sent: {', '.join(local)}")
    if "nara" in sources and getattr(adapters["nara"], "has_key", False):
        from .records.nara import ATTRIBUTION  # lazy: startup time

        say(
            f"Note: {query!r} also goes to catalog.archives.gov with your API key "
            f"(counted against the 10,000/month quota). {ATTRIBUTION}"
        )
    if "courtlistener" in sources and getattr(adapters["courtlistener"], "has_key", False):
        say(f"Note: {query!r} also goes to {_courtlistener_note()}")

    async def one(source: str):
        for key, owners in _FILTER_SOURCES.items():
            if kwargs.get(key) and source not in owners:
                return source, None, f"skipped ({key} filter not supported)"
        try:
            adapter = adapters[source]
            if source in _KEYED_SOURCES and not adapter.has_key:
                if getattr(adapter, "credentials_withheld", False):
                    return source, None, "skipped (personal key not sent over Tor)"
                return source, None, f"skipped (no {_KEYED_SOURCES[source]})"
            check = getattr(adapter, "unavailable_reason", None)
            reason = check() if callable(check) else None
            if reason:
                return source, None, f"skipped ({reason})"
            result = await _search_one(adapter, source, query, limit, kwargs)
        except Exception as e:
            return source, None, f"error: {e}"
        if result.error:
            return source, None, f"error: {result.error}"
        return source, result, f"ok {result.total_results}"

    async def run_all():
        return await asyncio.gather(*(one(s) for s in sources))

    outcomes = asyncio.run(run_all())
    for source, _, status in outcomes:
        say(f"  {source:<15} {status}")

    _log_case_queries(
        [
            (source, query, result.total_results if result else None)
            for source, result, status in outcomes
            if not status.startswith("skipped")
        ]
    )

    rows = []
    for source, result, _ in outcomes:
        if not result:
            continue
        ents = result.entities[:limit]
        rows += [(source, _compact_result(e)) for e in (_rank(ents, rank) if rank else ents)]
    if rank:
        rows.sort(key=lambda r: -r[1]["score"])
    for source, c in rows:
        if json_lines:
            typer.echo(safe_json(c, default=str))
        else:
            pages = f" ({c['pages']}p)" if c["pages"] else ""
            score = f"{c['score']:>3} " if rank else ""
            line = f"{score}{source:<14} {c['id'] or '-':<12} {c['title']}{pages}"
            # Titles are upstream data: drop control chars (terminal escapes).
            typer.echo(safe_text(line))

    if not any(result for _, result, _ in outcomes):
        raise typer.Exit(1)
    if any(status.startswith("error") for _, _, status in outcomes):
        raise typer.Exit(2)


@records_app.command("search")
@_csv_stdout_safe
def records_search(
    query: str = typer.Argument(
        "", help="Search term (company name, keyword, etc.); optional with --ancestor"
    ),
    source: str = typer.Option(
        "opencorporates",
        "--source",
        "-s",
        help=(
            "Data source (opencorporates, sec, muckrock, documentcloud, usaspending, "
            "nonprofits, govinfo, fec, regulations, federalregister, cia, nara, courtlistener, "
            "opensanctions)"
        ),
    ),
    jurisdiction: str | None = typer.Option(
        None,
        "--jurisdiction",
        "-j",
        help="Jurisdiction filter (e.g. us_ca, gb); courtlistener: court ids (e.g. dcd nysd)",
    ),
    filing_type: str | None = typer.Option(
        None,
        "--type",
        "-t",
        help=(
            "sec: filing type (e.g. 10-K, 8-K). federalregister: document type "
            "(rule, proposed-rule, notice, presidential; comma-separated). courtlistener: "
            "r dockets (default), rd filings, d dockets only, o opinions"
        ),
    ),
    agency: str | None = typer.Option(
        None,
        "--agency",
        help="federalregister: agency slug, name or short name (e.g. EPA); comma-separated",
    ),
    since: str | None = typer.Option(
        None, "--since", help="federalregister: published on or after (YYYY-MM-DD)"
    ),
    until: str | None = typer.Option(
        None, "--until", help="federalregister: published on or before (YYYY-MM-DD)"
    ),
    docket: str | None = typer.Option(
        None, "--docket", help="federalregister: agency docket id (e.g. EPA-HQ-OAR-2021-0317)"
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results to display"),
    raw: bool = typer.Option(False, "--raw", help="Print raw JSON output"),
    json_lines: bool = typer.Option(
        False, "--json", help="One compact JSON object per result, one per line"
    ),
    all_sources: bool = typer.Option(
        False,
        "--all-sources",
        help=(
            "Query every registered source and report each one's status. "
            "Exit 0: all ok or skipped; 2: some sources errored; 1: none succeeded."
        ),
    ),
    rank: str | None = typer.Option(
        None,
        "--rank",
        help="Comma-separated keywords; adds a score (hits in title and snippet) and sorts by it",
    ),
    ancestor: int | None = typer.Option(
        None, "--ancestor", help="nara: only records under this NAID (series, file unit)"
    ),
    all_pages: bool = typer.Option(
        False, "--all", help="nara: walk every page (one quota-counted query per 100 hits)"
    ),
    max_hits: int = typer.Option(1000, "--max", help="nara: stop --all after this many hits"),
    csv_path: str | None = typer.Option(None, "--csv", help=CSV_HELP),
):
    """Search public records databases.

    Searches external public records APIs and returns normalized entities.
    Each search sends the query to that source. When a case is active the
    query is logged to it (see 'openfoia case').

    Sources (key = environment variable it needs):
      documentcloud   - Public documents with extracted text
      muckrock        - Completed FOIA requests (key: OPENFOIA_MUCKROCK_TOKEN)
      nara            - National Archives Catalog (key: OPENFOIA_NARA_API_KEY)
      courtlistener   - Federal court dockets, filings (RECAP) and case law
                        (key: OPENFOIA_COURTLISTENER_API_KEY; 125 requests a day)
      cia             - CIA FOIA Reading Room (often blocked to scripts)
      opencorporates  - Company registrations worldwide
      sec             - SEC EDGAR filings (US public companies)
      usaspending     - Federal contracts and grants
      nonprofits      - ProPublica Nonprofit Explorer (IRS Form 990)
      govinfo         - US government publications (key: OPENFOIA_GOVINFO_API_KEY or demo)
      fec             - Campaign finance (shared demo key, rate-limited)
      regulations     - Rulemaking dockets (shared demo key, rate-limited)
      federalregister - Federal rules, proposed rules, notices, presidential documents
      opensanctions   - Sanctions lists, PEPs, crime and debarment lists. Offline from
                        the bulk file when downloaded ('records opensanctions-data
                        --download'), else the API (key: OPENFOIA_OPENSANCTIONS_API_KEY)

    Examples:
        openfoia records search "Acme Corp" --source opencorporates
        openfoia records search "Acme Corp" --source sec
        openfoia records search "Palantir" --source sec --type 10-K
        openfoia records search "Shell" --source opencorporates -j gb
        openfoia records search "EPA water" --source muckrock
        openfoia records search "PAS" -s documentcloud -n 300 --json
        openfoia records search "PAS" --all-sources
        openfoia records search "PAS" -s documentcloud --rank child,school,gifted
        openfoia records search -s nara --ancestor 7283224 --all --max 500 --json
        openfoia records search "Acme Corp" -s courtlistener -t rd -j dcd
        openfoia records search "Acme Corp" -s sec --csv results.csv
        openfoia records search "facial recognition" -s federalregister --agency DHS -t rule
        openfoia records search -s federalregister --docket EPA-HQ-OAR-2021-0317
        openfoia records search "Ivar Testerov" -s opensanctions
    """
    from .config import load_config  # lazy: startup time
    from .records import list_sources  # lazy: startup time

    if (ancestor or all_pages) and source != "nara":
        rprint("[red]--ancestor and --all are only supported with --source nara.[/red]")
        raise typer.Exit(1)
    fr_filters = {"agency": agency, "since": since, "until": until, "docket": docket}
    if any(fr_filters.values()) and source != "federalregister" and not all_sources:
        rprint(
            "[red]--agency, --since, --until and --docket are only supported with "
            "--source federalregister.[/red]"
        )
        raise typer.Exit(1)
    if not query and not ancestor and not any(fr_filters.values()):
        rprint(
            "[red]Give a search term (or --ancestor <NAID> with -s nara, or a filter "
            "such as --docket with -s federalregister).[/red]"
        )
        raise typer.Exit(1)
    if csv_path and (raw or json_lines or all_sources):
        rprint("[red]--csv cannot be combined with --raw, --json or --all-sources.[/red]")
        raise typer.Exit(1)
    kwargs: dict[str, Any] = {}
    if ancestor:
        kwargs["ancestor"] = ancestor
    if all_pages:
        kwargs.update(all_pages=True, max_hits=max_hits)
        limit = max(limit, max_hits)
    if jurisdiction:
        kwargs["jurisdiction"] = jurisdiction
    if filing_type:
        kwargs["filing_type"] = filing_type
    kwargs.update({k: v for k, v in fr_filters.items() if v})
    keywords = _rank_keywords(rank)

    if all_sources:
        _records_search_all(query, kwargs, limit, json_lines, keywords)
        return

    # Validate source
    available = list_sources()
    if source not in available:
        rprint(f"[red]Unknown source '{_safe(source)}'. Available: {', '.join(available)}[/red]")
        raise typer.Exit(1)

    policy, adapter = _records_adapter_or_exit(
        source, api_data_gov_key=load_config().records.api_data_gov_key
    )
    offline = getattr(adapter, "offline", False) is True
    # Pipe-safe modes: stdout carries only JSON, so no spinner or Rich markup.
    quiet = raw or json_lines

    if source == "courtlistener":
        _courtlistener_key_or_exit(adapter)  # before "Sending": without a key nothing is sent
    if source == "opensanctions":
        _opensanctions_usable_or_exit(adapter)  # before "Sending": nothing is sent without
    if offline:
        _echo(
            f"Searching {source} on this machine ({adapter.local_notice}); nothing is sent.",
            err=True,
        )
    elif source == "federalregister":
        # The filters are sent too, and a docket id says as much as a query.
        sent = ([repr(query)] if query else []) + [
            f"{'type' if k == 'filing_type' else k}={v}" for k, v in kwargs.items()
        ]
        _echo(
            f"Sending {', '.join(sent)} to www.federalregister.gov "
            f"({_route_note(policy, adapter)}).",
            err=True,
        )
    elif source != "cia":
        _echo(f"Sending {query!r} to {source} ({_route_note(policy, adapter)}).", err=True)
    if source == "cia":
        notice = f"WARNING: This will send the query {query!r} to cia.gov ({_route_note(policy)})."
        if quiet:
            _echo(notice, err=True)
        else:
            rprint(f"\n[yellow]{_safe(notice)}[/yellow]")
    elif source == "nara":
        from .records.nara import ATTRIBUTION  # lazy: startup time

        _nara_key_or_exit(adapter)
        _echo(
            f"Note: sending {query or '*'!r} to catalog.archives.gov with your API key "
            f"(counted against the 10,000/month quota). {ATTRIBUTION}",
            err=True,
        )
        from .records.nara import PAGE_MAX  # lazy: startup time

        planned = math.ceil(max_hits / PAGE_MAX)
        if all_pages and planned > 5:
            typer.echo(
                f"--all: up to {planned} queries ({max_hits} hits max, {PAGE_MAX} per query), "
                "one per second, each counted against the monthly quota.",
                err=True,
            )
    elif source == "courtlistener":
        typer.echo(f"Note: sending {query!r} to {_courtlistener_note()}", err=True)
    elif source == "opensanctions" and not offline:
        typer.echo(f"Note: OpenSanctions {adapter.search_notice}", err=True)

    try:
        if quiet:
            result = asyncio.run(_search_one(adapter, source, query, limit, kwargs))
        else:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task(f"Searching {source} for '{_safe(query)}'...", total=None)
                result = asyncio.run(_search_one(adapter, source, query, limit, kwargs))
    except Exception as e:
        _log_case_queries([(source, query, None)])
        if quiet:
            _echo(f"Search failed: {e}", err=True)
        else:
            rprint(f"[red]Search failed: {_safe(e)}[/red]")
        raise typer.Exit(1) from None

    _log_case_queries([(source, query, None if result.error else result.total_results)])

    if result.error:
        # An API failure must never read as "no results": report it and exit
        # non-zero (on stderr in pipe-safe modes so stdout stays parseable).
        msg = f"Search error ({source}): {result.error}"
        if result.entities:
            msg += f" Showing the {len(result.entities)} results retrieved before it failed."
        if quiet:
            _echo(msg, err=True)
        else:
            rprint(f"[red]{_safe(msg)}[/red]")
        if result.entities:
            _print_search_result(
                result, source, query, limit, raw, json_lines, filing_type, keywords, csv_path
            )
        raise typer.Exit(1)

    _print_search_result(
        result, source, query, limit, raw, json_lines, filing_type, keywords, csv_path
    )


def _print_search_result(
    result, source, query, limit, raw, json_lines, filing_type, rank=None, csv_path=None
) -> None:
    if rank:
        result.entities = _rank(result.entities, rank)
    if json_lines:
        for e in result.entities[:limit]:
            typer.echo(safe_json(_compact_result(e), default=str))
        return

    if raw:
        typer.echo(safe_json([e.to_dict() for e in result.entities[:limit]], indent=2, default=str))
        return

    # Everything below prints upstream API values: in markup strings through
    # _safe, in tables through _row (cleaned Text cells, never parsed as markup).
    query = _safe(query)

    if not result.entities:
        if source == "sec":
            rprint(
                f"[yellow]{source} returned {result.total_results} total results for '{query}'.[/yellow]"
            )
            if not csv_path:  # with --csv the shared footer below prints it
                rprint(f"[dim]Showing 0 of {result.total_results} results.[/dim]")
            if filing_type:
                rprint(f"[dim]Applied SEC filing type filter: {_safe(filing_type)}[/dim]")
            rprint(
                "[yellow]SEC EDGAR full-text search can be incomplete for a company or form type; "
                "0 results is not proof that no filing exists.[/yellow]"
            )
        else:
            rprint(f"[yellow]No results found for '{query}' on {source}.[/yellow]")
        if not csv_path:
            return
    else:
        rprint(f"\n[bold]{source}[/bold]: {result.total_results} total results for '{query}'")
        rprint("=" * 60)

    entities = result.entities[:limit]

    if rank:
        table = Table(expand=True)
        table.add_column("Score", width=5, justify="right")
        table.add_column("ID", style="dim", width=10, no_wrap=True, overflow="crop")
        table.add_column("Title", style="cyan", ratio=2)
        table.add_column("Snippet", ratio=3, style="yellow", no_wrap=True, overflow="ellipsis")
        for e in entities:
            c = _compact_result(e)
            _row(table, str(c["score"]), c["id"] or "-", e.name, c["snippet"])
        _emit_table(table, csv_path)
        rprint(f"\n[dim]Showing {len(entities)} of {result.total_results} results.[/dim]")
        return

    if source == "muckrock":
        table = Table(expand=True)
        table.add_column("ID", style="dim", width=7)
        table.add_column("Title", style="cyan", ratio=3)
        table.add_column("By", width=12)
        table.add_column("Files", width=5)
        table.add_column("Types", width=10)
        table.add_column("Date", width=10)

        for e in entities:
            file_types = e.extra_data.get("file_types", [])
            types_str = ", ".join(sorted(file_types)) if file_types else "-"
            _row(
                table,
                e.identifiers.get("muckrock_id", "-"),
                e.name,
                e.extra_data.get("username", "-"),
                str(e.extra_data.get("files_count", 0)),
                types_str,
                (e.extra_data.get("completed") or e.extra_data.get("submitted") or "-")[:10],
            )
        _emit_table(table, csv_path)
        rprint("\n[dim]Download documents: openfoia records download <ID> --source muckrock[/dim]")

    elif source == "documentcloud":
        table = Table(expand=True)
        table.add_column("ID", style="dim", width=10, no_wrap=True, overflow="crop")
        table.add_column("Title", style="cyan", ratio=2)
        table.add_column("Pages", width=5)
        table.add_column("Source", width=15)
        table.add_column("Highlight", ratio=3, style="yellow")

        for e in entities:
            highlights = e.extra_data.get("highlights", [])
            highlight_str = (
                highlights[0][:120] + "…"
                if highlights and len(highlights[0]) > 120
                else (highlights[0] if highlights else "-")
            )
            _row(
                table,
                e.identifiers.get("documentcloud_id", "-"),
                e.name,
                str(e.extra_data.get("pages") or "-"),
                e.extra_data.get("source", "-") or "-",
                highlight_str,
            )
        _emit_table(table, csv_path)
        rprint("\n[dim]Fetch full text: openfoia records fetch <ID> --source documentcloud[/dim]")

    elif source == "courtlistener":
        from .records.courtlistener import DOCKET_FETCH_REQUESTS  # lazy: startup time

        # Text cells: upstream strings are never read as Rich markup.
        table = Table(expand=True)
        table.add_column("ID", style="dim", width=12, no_wrap=True)
        table.add_column("Case", style="cyan", ratio=3)
        table.add_column("Court", width=8)
        table.add_column("Docket #", width=14)
        table.add_column("Filed", width=10)
        table.add_column("Filings", width=7)
        for e in entities:
            x = e.extra_data
            docs = x.get("documents") or []
            filings = f"{len(docs)}{'+' if x.get('more_documents') else ''}" if docs else "-"
            table.add_row(
                Text(next(iter(e.identifiers.values()), "-")),
                Text(e.name),
                Text(e.jurisdiction or "-"),
                Text(x.get("docket_number") or "-"),
                Text(x.get("date") or "-"),
                Text(filings),
            )
        _emit_table(table, csv_path)
        if result.total_results > 2000:
            rprint("[dim]CourtListener docket counts over 2,000 are estimates (about ±6%).[/dim]")
        rprint(
            "\n[dim]Fetch a docket sheet: openfoia records fetch <ID> -s courtlistener "
            f"(up to {DOCKET_FETCH_REQUESTS} API requests); a filing's text: rd:<id>; "
            "an opinion: op:<id>[/dim]"
        )

    elif source == "usaspending":
        table = Table(expand=True)
        table.add_column("Recipient", style="cyan", ratio=2)
        table.add_column("Amount", width=12, justify="right", style="green")
        table.add_column("Agency", ratio=2)
        table.add_column("Type", width=10)
        table.add_column("Description", ratio=3, no_wrap=True, overflow="ellipsis")

        for e in entities:
            _row(
                table,
                e.name,
                e.extra_data.get("amount_formatted", "-"),
                e.extra_data.get("awarding_agency", "-") or "-",
                e.extra_data.get("award_type", "-") or "-",
                e.extra_data.get("description") or "-",
            )
        _emit_table(table, csv_path)
        rprint("\n[dim]View on USAspending.gov: click award URLs in the table[/dim]")

    elif source == "nonprofits":
        table = Table(expand=True)
        table.add_column("EIN", style="dim", width=12)
        table.add_column("Name", style="cyan", ratio=3)
        table.add_column("Location", width=18)
        table.add_column("Type", width=12)

        for e in entities:
            _row(
                table,
                e.identifiers.get("strein", "-"),
                e.name,
                e.extra_data.get("location", "-") or "-",
                e.extra_data.get("subsection", "-") or "-",
            )
        _emit_table(table, csv_path)
        rprint("\n[dim]View details: openfoia records fetch <EIN> --source nonprofits[/dim]")

    elif source == "govinfo":
        table = Table(expand=True)
        table.add_column("Collection", width=18)
        table.add_column("Title", style="cyan", ratio=3)
        table.add_column("Author", ratio=2)
        table.add_column("Date", width=12)

        for e in entities:
            _row(
                table,
                e.extra_data.get("collection", "-"),
                e.name[:80],
                e.extra_data.get("government_author", "-") or "-",
                e.extra_data.get("date_issued", "-") or "-",
            )
        _emit_table(table, csv_path)

    elif source == "fec":
        table = Table(expand=True)
        table.add_column("Contributor", style="cyan", ratio=2)
        table.add_column("Amount", width=10, justify="right", style="green")
        table.add_column("To", ratio=2)
        table.add_column("Date", width=12)

        for e in entities:
            _row(
                table,
                e.name,
                e.extra_data.get("amount_formatted", "-"),
                e.extra_data.get("committee_name", "-") or "-",
                (e.extra_data.get("date") or "-")[:10],
            )
        _emit_table(table, csv_path)
        rprint("\n[dim]View on FEC.gov: openfoia records fetch <name> --source fec[/dim]")

    elif source == "regulations":
        table = Table(expand=True)
        table.add_column("Agency", width=10)
        table.add_column("Title", style="cyan", ratio=3)
        table.add_column("Type", width=16)
        table.add_column("Date", width=12)

        for e in entities:
            _row(
                table,
                e.extra_data.get("agency", "-") or "-",
                e.name[:80],
                e.extra_data.get("document_type", "-") or "-",
                e.extra_data.get("posted_date", "-") or "-",
            )
        _emit_table(table, csv_path)

    elif source == "federalregister":
        table = Table(expand=True)
        table.add_column("Document", style="dim", width=11, no_wrap=True)
        table.add_column("Type", width=14)
        table.add_column("Agency", ratio=1)
        table.add_column("Title", style="cyan", ratio=3)
        table.add_column("Published", width=10)
        table.add_column("Comments close", width=10)

        for e in entities:
            x = e.extra_data
            # Cleaned Text, not str: upstream strings must not be read as Rich markup.
            _row(
                table,
                e.identifiers.get("document_number", "-"),
                x.get("subtype") or x.get("document_type") or "-",
                ", ".join(x.get("agencies") or []) or "-",
                e.name,
                x.get("publication_date") or "-",
                x.get("comments_close_on") or "-",
            )
        _emit_table(table, csv_path)
        if result.total_results >= 10_000:
            rprint(
                "[dim]The Federal Register API caps its count at 10,000; there may be more.[/dim]"
            )
        rprint(
            "\n[dim]Download and ingest the PDF: "
            "openfoia records fetch <Document> --source federalregister[/dim]"
        )

    elif source == "opencorporates":
        table = Table()
        table.add_column("Name", style="cyan", max_width=30, overflow="fold")
        table.add_column("Jurisdiction", max_width=12)
        table.add_column("Company #", max_width=14)
        table.add_column("Status", max_width=12)
        table.add_column("Type", max_width=14)
        table.add_column("Officers", max_width=30, overflow="fold")

        for e in entities:
            officers = e.extra_data.get("officers", [])
            officer_str = ", ".join(str(o.get("name")) for o in officers[:3]) if officers else "-"
            if len(officers) > 3:
                officer_str += f" (+{len(officers) - 3} more)"

            _row(
                table,
                e.name,
                e.jurisdiction or "-",
                e.identifiers.get("company_number", "-"),
                e.status or "-",
                e.extra_data.get("company_type", "-"),
                officer_str,
            )
        _emit_table(table, csv_path)

    elif source == "sec":
        table = Table()
        table.add_column("Company", style="cyan", max_width=30, overflow="fold")
        table.add_column("Filing", width=10)
        table.add_column("Date", width=12)
        table.add_column("CIK", width=12)
        table.add_column("URL", max_width=50)

        for e in entities:
            _row(
                table,
                e.name,
                e.extra_data.get("filing_type", "-"),
                e.extra_data.get("filing_date", "-"),
                e.identifiers.get("cik", "-"),
                e.source_url or "-",
            )
        _emit_table(table, csv_path)

    elif source == "opensanctions":
        table = Table()
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Name", style="cyan", max_width=30, overflow="fold")
        table.add_column("Type", max_width=12)
        table.add_column("Score", width=5, justify="right")
        table.add_column("Topics", max_width=20, overflow="fold")
        table.add_column("Lists", max_width=40, overflow="fold")
        table.add_column("Countries", max_width=12)

        for e in entities:
            x = e.extra_data
            score = x.get("match_score")
            topics = ", ".join(x.get("topics") or []) or "-"
            if x.get("topics_inferred") and topics != "-":
                topics += " (inferred)"
            _row(
                table,
                e.identifiers.get("opensanctions_id", "-"),
                e.name,
                x.get("schema", "-"),
                f"{score:.2f}" if isinstance(score, float) else "-",
                topics,
                "; ".join(x.get("datasets") or []) or "-",
                ", ".join(x.get("countries") or []) or "-",
            )
        _emit_table(table, csv_path)
        if entities and entities[0].extra_data.get("mode") == "local":
            rprint(
                "[dim]Offline: whole-name (score 1.00) and alias (0.80) matches only; "
                "topics inferred from list titles.[/dim]"
            )
        rprint(
            "[dim]Store one in the active case: openfoia records fetch <ID> -s opensanctions. "
            "OpenSanctions data is CC BY-NC 4.0.[/dim]"
        )

    else:
        # Generic table for any future adapter
        table = Table()
        table.add_column("Name", style="cyan", overflow="fold")
        table.add_column("Type")
        table.add_column("Source")
        table.add_column("Jurisdiction")

        for e in entities:
            _row(table, e.name, e.entity_type, e.source, e.jurisdiction or "-")
        _emit_table(table, csv_path)

    rprint(f"\n[dim]Showing {len(entities)} of {result.total_results} results.[/dim]")


@records_app.command("filings")
def records_filings(
    ticker_or_cik: str = typer.Argument(..., help="Company ticker or SEC CIK"),
    since: str | None = typer.Option(None, "--since", help="Earliest filing date (YYYY-MM-DD)"),
    forms: str | None = typer.Option(None, "--forms", help="Comma-separated forms, e.g. 10-K,10-Q"),
    limit: int = typer.Option(25, "--limit", "-n", help="Maximum filings to display"),
    tor: bool | None = typer.Option(None, "--tor/--no-tor", help="Route SEC requests through Tor"),
    yes: bool = typer.Option(False, "--yes", help="Confirm sending the query to SEC"),
):
    """List a company's SEC filings in reverse chronological order.

    The ticker or CIK is sent to SEC. This command never downloads filing
    documents; it only retrieves SEC metadata and archive indexes.
    """
    from .config import load_config  # lazy: startup time
    from .net import describe_egress  # lazy: startup time
    from .records.sec_edgar import SECEdgarAdapter  # lazy: startup time

    if limit < 1:
        rprint("[red]--limit must be at least 1.[/red]")
        raise typer.Exit(2)
    if since:
        try:
            date.fromisoformat(since)
        except ValueError:
            rprint("[red]--since must be an ISO date in YYYY-MM-DD format.[/red]")
            raise typer.Exit(2) from None
    try:
        SECEdgarAdapter._validate_forms(forms)
        if ticker_or_cik.strip().isdigit():
            SECEdgarAdapter.normalize_cik(ticker_or_cik)
        elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,9}", ticker_or_cik.strip()):
            raise ValueError("ticker must be 1-10 letters, digits, periods, or hyphens")
    except ValueError as exc:
        rprint(f"[red]Invalid filing query: {_safe(exc)}[/red]")
        raise typer.Exit(2) from None

    cfg = load_config()
    policy = _egress_policy_from(cfg, tor=tor)
    _check_tor_or_exit(policy)
    egress_info = describe_egress(policy)
    rprint("\n[yellow]WARNING: This will send the ticker/CIK and filing query to SEC.[/yellow]")
    if policy.is_tor:
        rprint(
            f"[cyan]Egress: Tor — SEC will not see your real IP "
            f"(stream isolation: {egress_info['stream_isolation']}).[/cyan]"
        )
    else:
        rprint("[yellow]Egress: direct — SEC will see your real IP.[/yellow]")
    rprint(
        "[dim]Tor hides who is asking, not the ticker/CIK or query contents. "
        "No filing documents will be downloaded.[/dim]"
    )
    if not yes and not typer.confirm("Send this query to SEC?"):
        rprint("[green]Aborted. Nothing left your machine.[/green]")
        raise typer.Exit(0)

    adapter = SECEdgarAdapter(egress=policy)
    try:
        result = asyncio.run(adapter.filings(ticker_or_cik, since=since, forms=forms))
    except ValueError as exc:
        rprint(f"[red]Invalid filing query: {_safe(exc)}[/red]")
        raise typer.Exit(2) from None
    if result.error:
        rprint(f"[red]SEC source error: {_safe(result.error)}[/red]")
        raise typer.Exit(1)
    if not result.entities:
        rprint("[yellow]No SEC filings matched the requested filters.[/yellow]")
        return
    table = Table()
    table.add_column("Date", width=12)
    table.add_column("Form", width=10)
    table.add_column("CIK", width=12)
    table.add_column("Accession", width=22)
    table.add_column("URL", max_width=70)
    for entity in result.entities[:limit]:
        _row(
            table,
            entity.extra_data.get("filing_date", "-"),
            entity.extra_data.get("filing_type", "-"),
            entity.identifiers.get("cik", "-"),
            entity.identifiers.get("accession_number", "-"),
            entity.source_url or "-",
        )
    rprint(f"\n[bold]SEC filings for {_safe(ticker_or_cik)}[/bold]")
    console.print(table)
    rprint(
        f"\n[dim]Showing {min(limit, len(result.entities))} of "
        f"{len(result.entities)} filings.[/dim]"
    )


def _ids_from_results(path: Path, source: str) -> list[str]:
    """Ids in a 'records search --json' (JSON lines), '--raw' (JSON list) or one-id-per-line file."""
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    try:
        data = json.loads("\n".join(lines))
    except json.JSONDecodeError:
        try:
            data = [json.loads(ln) for ln in lines]
        except json.JSONDecodeError:
            data = [{"id": ln} for ln in lines]  # plain id list
    if isinstance(data, dict):
        data = [data]
    if isinstance(data, list):
        data = [{"id": str(i)} if isinstance(i, str | int) else i for i in data]
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        rprint(f"[red]{_safe(path)}: expected 'records search --json' or '--raw' output.[/red]")
        raise typer.Exit(2)
    ids, other = [], 0
    for item in data:
        if item.get("source", source) != source:
            other += 1
            continue
        ident = item.get("id") or next(iter((item.get("identifiers") or {}).values()), None)
        if ident:
            ids.append(str(ident))
    if other:
        rprint(f"[dim]Ignored {other} result(s) from sources other than {source}.[/dim]")
    return list(dict.fromkeys(ids))


@records_app.command("opensanctions-data")
def records_opensanctions_data(
    download: bool = typer.Option(
        False, "--download", help="Download or update the bulk file (asks first; ~480 MB)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the download confirmation"),
):
    """Show, or explicitly download, the OpenSanctions bulk file for offline screening.

    Without --download this only reads the disk: where the file is, its size,
    and the version and SHA-1 it was downloaded with. Nothing is sent.

    With --download it asks first, then reads the current index from
    data.opensanctions.org (which lists the file's size and SHA-1), downloads
    targets.simple.csv into <data dir>/opensanctions/, checks the size and the
    SHA-1 against the index, and only then replaces the old file. No name and
    no API key is sent; the server sees your IP (or a Tor exit, with network.tor)
    and that you downloaded the list. The data is CC BY-NC 4.0: free for
    journalists and other non-commercial use.

    Once the file is there, 'openfoia search', 'records search -s opensanctions',
    'records fetch -s opensanctions' and 'crossref' screen against it offline.

    Examples:
        openfoia records opensanctions-data
        openfoia records opensanctions-data --download
    """
    from .records import opensanctions as osx  # lazy: startup time

    status = osx.local_status()
    meta = status.get("meta") or {}
    if status["present"]:
        rprint(f"OpenSanctions bulk file: {_safe(status['path'])}")
        rprint(f"  Size: {status['size']:,} bytes")
        if meta:
            rprint(
                f"  Version {_safe(meta.get('version') or '?')}, downloaded "
                f"{_safe(meta.get('downloaded_at') or '?')}, SHA-1 {_safe(meta.get('checksum') or '?')}"
            )
    else:
        rprint(f"No OpenSanctions bulk file at {_safe(status['path'])}.")
    if not download:
        if not status["present"]:
            rprint(
                "[dim]Download it (asks first): openfoia records opensanctions-data --download[/dim]"
            )
        return

    policy = _records_policy_or_exit()
    rprint(
        f"\n[yellow]WARNING: this contacts {osx.DATA_HOST} ({_route_note(policy)}): one small "
        f"index request, then the file, {osx.APPROX_SIZE}; the exact size is read from the "
        "index and printed before the download starts.[/yellow]"
    )
    rprint(
        "[yellow]  No names and no API key are sent. The server sees your IP (or a Tor exit) "
        "and that you downloaded the sanctions list.[/yellow]"
    )
    rprint(f"[dim]  {osx.LICENSE}.[/dim]")
    if not yes and not typer.confirm("Download the OpenSanctions bulk file?"):
        rprint("[green]Aborted. Nothing left your machine.[/green]")
        raise typer.Exit(0)

    try:
        entry = asyncio.run(osx.read_index(policy))
    except Exception as e:
        rprint(f"[red]Could not read the OpenSanctions index: {_safe(e)}[/red]")
        raise typer.Exit(1) from None
    if meta.get("checksum") == entry["checksum"] and status["present"]:
        rprint(f"Already up to date (version {_safe(entry['version'] or '?')}).")
        return
    rprint(
        f"Downloading version {_safe(entry['version'] or '?')}: {entry['size']:,} bytes "
        f"({entry['size'] / 1_000_000:.0f} MB), SHA-1 {entry['checksum']}"
    )
    try:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            progress.add_task("Downloading targets.simple.csv...", total=None)
            dest = asyncio.run(osx.download_bulk(entry, policy))
    except Exception as e:
        rprint(f"[red]Download failed: {_safe(e)}[/red]")
        raise typer.Exit(1) from None
    rprint(f"[green]Saved and verified (size and SHA-1): {_safe(dest)}[/green]")
    rprint("[dim]Searches of opensanctions now run offline against this file.[/dim]")


@records_app.command("fetch")
def records_fetch(
    doc_ids: list[str] | None = typer.Argument(None, help="Document IDs to fetch"),
    source: str = typer.Option("documentcloud", "--source", "-s", help="Data source"),
    from_file: Path | None = typer.Option(
        None,
        "--from",
        exists=True,
        dir_okay=False,
        help="Also fetch every id in this 'records search --json' or '--raw' output file",
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Re-fetch documents already stored"),
    list_objects: bool = typer.Option(
        False, "--list-objects", help="nara: list each record's digital objects, download nothing"
    ),
    objects: bool = typer.Option(
        False,
        "--objects",
        help="nara: download every digital object to <data>/nara/<NAID>/ and ingest it",
    ),
    as_json: bool = typer.Option(False, "--json", help="With --list-objects: print JSON"),
):
    """Fetch documents' full text and save them to the local database.

    Pulls pre-extracted text from the source (no local OCR needed for
    DocumentCloud); federalregister downloads the document's PDF from GovInfo
    and extracts its text here. Documents already stored are skipped unless --refresh.
    Failures do not stop the batch; the summary lists fetched, skipped and
    failed counts. Exit 2 when some failed, 1 when all did.

    Examples:
        openfoia records fetch 2090186
        openfoia records fetch 2090186 2090187 --source documentcloud
        openfoia records search "PAS" -s documentcloud -n 300 --json > hits.json
        openfoia records fetch --from hits.json
        openfoia records fetch 7283224 -s nara --list-objects
        openfoia records fetch 7283224 -s nara --objects
        openfoia records fetch 2026-21024 -s federalregister
        openfoia records fetch 4214664 rd:2974081 -s courtlistener
        openfoia records fetch NK-a1b2c3 -s opensanctions
    """
    if (list_objects or objects) and source != "nara":
        rprint("[red]--list-objects and --objects are only supported with --source nara.[/red]")
        raise typer.Exit(1)
    if list_objects and objects:
        rprint("[red]--list-objects previews; --objects downloads. Pick one.[/red]")
        raise typer.Exit(2)
    if as_json and not list_objects:
        rprint("[red]--json only applies to --list-objects.[/red]")
        raise typer.Exit(2)
    ids = list(doc_ids or [])
    if from_file:
        ids += _ids_from_results(from_file, source)
    ids = list(dict.fromkeys(ids))
    if not ids:
        rprint("[red]Give at least one document id or --from <results.json>.[/red]")
        raise typer.Exit(2)
    if source == "courtlistener":
        from .records.courtlistener import canonical_id  # lazy: startup time

        try:
            ids = list(dict.fromkeys(canonical_id(i) for i in ids))
        except ValueError as e:
            rprint(f"[red]{escape(str(e))}[/red]")
            raise typer.Exit(2) from None
    if source == "cia":
        for doc_id in ids:
            _records_fetch_cia(doc_id)
        return
    if source == "federalregister":
        from .records.federal_register import normalize_document_number  # lazy: startup time

        def _number(i: str) -> str:
            try:
                return normalize_document_number(i)
            except ValueError:
                return i  # reported as a failure by fetch, with the reason

        ids = list(dict.fromkeys(_number(i) for i in ids))
    # Sources whose adapter has pull_text() (federalregister: PDF download and
    # ingest); the prefix is the stored request number's.
    prefixes = {
        "documentcloud": "DC-",
        "nara": "NARA-",
        "federalregister": "FR-",
        "courtlistener": "CL-",
        "opensanctions": "OS-",
    }
    if source == "opensanctions":
        from .records.opensanctions import valid_id  # lazy: startup time

        bad = [i for i in ids if not valid_id(i)]
        if bad:
            rprint(f"[red]Not an OpenSanctions id: {_safe(bad[0])!r}[/red]")
            raise typer.Exit(2)
    if source not in prefixes:
        rprint(
            f"[yellow]Fetch not yet supported for '{_safe(source)}'. "
            "Use 'records download' for MuckRock.[/yellow]"
        )
        raise typer.Exit(1)
    prefix = prefixes[source]

    from .cases import active_case_id_in_db  # lazy: startup time
    from .db import get_db_path, get_session  # lazy: startup time

    policy, adapter = _records_adapter_or_exit(source)
    offline = getattr(adapter, "offline", False) is True
    if source == "courtlistener":
        _courtlistener_key_or_exit(adapter)
    if source == "opensanctions":
        _opensanctions_usable_or_exit(adapter)
    if offline:
        typer.echo(
            f"Reading {source} from this machine ({adapter.local_notice}); nothing is sent.",
            err=True,
        )
    else:
        typer.echo(f"Fetching from {source} ({_route_note(policy, adapter)}).", err=True)
    if source == "opensanctions" and not offline:
        typer.echo(
            "Note: each id goes to api.opensanctions.org with your API key (entity "
            "lookups use no credits).",
            err=True,
        )
    if source == "federalregister":
        typer.echo(
            "Note: each document's details come from www.federalregister.gov and its PDF "
            "from www.govinfo.gov, both on that route.",
            err=True,
        )
    if source == "nara":
        from .records.nara import ATTRIBUTION  # lazy: startup time

        _nara_key_or_exit(adapter)
        typer.echo(
            "Note: fetching from catalog.archives.gov with your API key (one query per id, "
            f"counted against the 10,000/month quota). {ATTRIBUTION}",
            err=True,
        )
        if list_objects or objects:
            _records_nara_objects(
                adapter, ids, download=objects, as_json=as_json, route=_route_note(policy)
            )
            return
    if source == "courtlistener":
        from .records.courtlistener import DOCKET_FETCH_REQUESTS  # lazy: startup time

        cost = sum(1 if ":" in i else DOCKET_FETCH_REQUESTS for i in ids)
        typer.echo(f"Note: fetching from {_courtlistener_note(cost)}", err=True)
    case_id = active_case_id_in_db()

    stored: dict[str, str | None] = {}  # id -> name of another case holding it
    if get_db_path().exists():
        with get_session() as session:
            numbers = [f"{prefix}{i}" for i in ids]
            for req in session.query(RequestModel).filter(RequestModel.request_number.in_(numbers)):
                if case_id and req.case_id is None:
                    req.case_id = case_id
                other = None
                if req.case_id and req.case_id != case_id:
                    other = session.get(Case, req.case_id)
                stored[req.request_number.removeprefix(prefix)] = other.name if other else None

    fetched, skipped, failed = [], [], []
    for doc_id in ids:
        if doc_id in stored and not refresh:
            skipped.append(doc_id)
            where = f"stored in case {stored[doc_id]}" if stored[doc_id] else "already stored"
            rprint(f"  {_safe(doc_id)}  [dim]{_safe(where)} (--refresh to re-fetch)[/dim]")
            continue
        try:
            if source == "federalregister":
                result_id, text = _records_fetch_federalregister(adapter, doc_id)
            elif source == "courtlistener":
                result_id, text = _courtlistener_pull(adapter, doc_id, case_id, _route_note(policy))
            else:
                result_id, text = asyncio.run(adapter.pull_text(doc_id, case_id=case_id))
        except Exception as e:
            failed.append(doc_id)
            rprint(f"  {_safe(doc_id)}  [red]failed: {_safe(e)}[/red]")
            continue
        if not result_id or not text:
            failed.append(doc_id)
            rprint(f"  {_safe(doc_id)}  [red]no text (missing document or no extracted text)[/red]")
            continue
        fetched.append(result_id)
        rprint(
            f"  {_safe(doc_id)}  [green]saved {result_id[:8]}[/green] ({len(text):,} characters)"
        )

    rprint(
        f"\nFetched {len(fetched)}, skipped {len(skipped)} already stored, failed {len(failed)}."
    )
    if case_id and (fetched or any(stored.get(i) is None for i in skipped)):
        rprint("  [dim]Attached to the active case.[/dim]")
    if len(fetched) == 1:
        rprint(f"[dim]Extract entities: openfoia analyze extract {fetched[0]}[/dim]")
    if failed:
        raise typer.Exit(1 if not fetched and not skipped else 2)


def _records_nara_objects(
    adapter, ids: list[str], *, download: bool, as_json: bool, route: str
) -> None:
    """List (or download and ingest) each NAID's digital objects, like NARA's own scripts."""
    from .cases import active_case_id_in_db  # lazy: startup time
    from .db import ensure_db, get_data_dir  # lazy: startup time
    from .records.nara import PLACEHOLDER_SIZE as NARA_PLACEHOLDER_SIZE  # lazy: startup time

    listing, failed, ok, notified = [], 0, 0, False
    for raw in ids:
        naid = raw.strip().removeprefix("NARA-")
        try:
            entity = asyncio.run(adapter.fetch(naid))
        except Exception as e:
            failed += 1
            rprint(f"  {_safe(naid)}  [red]failed: {_safe(e)}[/red]")
            continue
        if entity is None:
            failed += 1
            rprint(f"  {_safe(naid)}  [red]no such NAID[/red]")
            continue
        objs = entity.extra_data.get("objects") or []
        ok += 1
        if not download:
            if as_json:
                listing += [{"naid": naid, "index": i, **o} for i, o in enumerate(objs, 1)]
                continue
            rprint(
                f"\n[bold]{_safe(naid)}[/bold] {_safe(entity.name)}: {len(objs)} digital objects"
            )
            for i, o in enumerate(objs, 1):
                # ponytail: NARA lists 1234 as a placeholder size on many objects
                # (seen 2026-10-20); a real 1,234-byte scan is implausible.
                real = o.get("size") and o["size"] != NARA_PLACEHOLDER_SIZE
                size = f"{o['size']:,} bytes" if real else "size unknown"
                typer.echo(
                    safe_text(f"  {i:>4}  {o.get('type') or '-'}  {size}  {o.get('url') or '-'}")
                )
            continue

        if not notified and objs:
            notified = True
            hosts = sorted({urlsplit(o.get("url") or "").hostname or "?" for o in objs})
            rprint(
                f"[yellow]Downloading digital objects from {_safe(', '.join(hosts))} (NARA's "
                f"object storage; {route}; not counted against the API quota).[/yellow]"
            )
        ensure_db()
        case_id = active_case_id_in_db()
        adapter.store(entity, naid, case_id)  # the text record the objects attach to
        results = asyncio.run(adapter.download_objects(entity, get_data_dir() / "nara" / naid))
        ingested = _ingest_nara_objects(entity, naid, results)
        bad = [r for r in results if r[2] not in ("downloaded", "present")]
        failed += len(bad)
        present = sum(r[2] == "present" for r in results)
        rprint(
            f"  {_safe(naid)}  {ingested} ingested, {present} already present, "
            f"{len(bad)} not downloaded"
        )
    if as_json and not download:
        typer.echo(safe_json(listing))
    if failed:
        raise typer.Exit(2 if ok else 1)


def _ingest_nara_objects(entity, naid: str, results) -> int:
    """Ingest and persist each file as it goes, so an interrupted run keeps what it saved.

    A file already on disk with no Document row (a run died between download and
    ingest) is ingested too; one that has a row is left alone.
    """
    from .db import get_data_dir, get_session  # lazy: startup time
    from .pipeline.ingest import DocumentIngester  # lazy: startup time

    number = f"NARA-{naid}"
    with get_session() as session:
        have = {
            name
            for (name,) in session.query(Document.filename)
            .join(Request, Document.request_id == Request.id)
            .filter(
                or_(Request.request_number == number, Request.request_number.like(f"{number}@%"))
            )
        }
    ingester = DocumentIngester(storage_path=get_data_dir() / "docs")
    done = 0
    for obj, path, status in results:
        if status == "refused":
            rprint(
                f"    [yellow]skipped {_safe(obj.get('url'))}: not NARA's object storage[/yellow]"
            )
            continue
        if status.startswith("failed"):
            rprint(f"    [red]{_safe(path.name)}: {_safe(status)}[/red]")
            continue
        if path.name in have:
            continue
        try:
            result = asyncio.run(ingester.ingest_file(path))
        except Exception as e:
            rprint(
                f"    [red]{_safe(path.name)}: ingest failed: {_safe(e)} (re-run to retry)[/red]"
            )
            continue
        _persist_ingested(
            [result],
            request_number=number,
            subject=entity.name,
            body=f"Digital objects from the National Archives Catalog: {entity.source_url}",
        )
        done += 1
    return done


def _courtlistener_pull(adapter, doc_id: str, case_id: str | None, route: str):
    """Fetch one CourtListener id and store it; a filing with no text falls back to its PDF."""
    entity = asyncio.run(adapter.fetch(doc_id))
    if entity is None:
        return None, None
    x = entity.extra_data
    for err in x.get("errors") or []:
        rprint(f"  {doc_id}  [yellow]{escape(err)}[/yellow]")
    result_id, text = adapter.store(entity, case_id)
    if text or not doc_id.startswith("rd:"):
        return result_id, text
    if x.get("pdf_refused"):
        rprint(f"  {doc_id}  [yellow]PDF link refused: not on storage.courtlistener.com[/yellow]")
    if not x.get("pdf_url"):
        if not x.get("is_available"):
            rprint(f"  {doc_id}  [yellow]not in the RECAP archive (only on PACER, paid)[/yellow]")
        return None, None

    from .db import ensure_db, get_data_dir  # lazy: startup time
    from .pipeline.ingest import DocumentIngester  # lazy: startup time

    rprint(
        f"  {doc_id}  [dim]no text on CourtListener; downloading the PDF from "
        f"storage.courtlistener.com ({route}; API token not sent, not counted)[/dim]"
    )
    path = asyncio.run(adapter.download_pdf(entity, get_data_dir() / "downloads" / "courtlistener"))
    ensure_db()
    result = asyncio.run(DocumentIngester(storage_path=get_data_dir() / "docs").ingest_file(path))
    _persist_ingested(
        [result],
        request_number=f"CL-{doc_id}",
        subject=entity.name,
        body=f"Downloaded from CourtListener: {entity.source_url}",
    )
    return result.document_id, result.extracted_text


def _courtlistener_key_or_exit(adapter) -> None:
    from .records.courtlistener import NO_KEY_MESSAGE, TOR_KEY_MESSAGE  # lazy: startup time

    if not adapter.has_key:
        rprint(f"[red]{TOR_KEY_MESSAGE if adapter.credentials_withheld else NO_KEY_MESSAGE}[/red]")
        raise typer.Exit(1)


def _opensanctions_usable_or_exit(adapter) -> None:
    """Exit with how to fix it when OpenSanctions has neither the local file nor a usable key."""
    from .records.opensanctions import NO_KEY_MESSAGE, TOR_KEY_MESSAGE  # lazy: startup time

    if adapter.unavailable_reason():
        rprint(f"[red]{TOR_KEY_MESSAGE if adapter.credentials_withheld else NO_KEY_MESSAGE}[/red]")
        raise typer.Exit(1)


def _nara_key_or_exit(adapter) -> None:
    from .records.nara import NO_KEY_MESSAGE, TOR_KEY_MESSAGE  # lazy: startup time

    if not adapter.has_key:
        rprint(f"[red]{TOR_KEY_MESSAGE if adapter.credentials_withheld else NO_KEY_MESSAGE}[/red]")
        raise typer.Exit(1)


def _records_policy_or_exit() -> EgressPolicy:
    """Egress policy from config.network.tor, fail-closed when Tor is configured but down."""
    from .config import load_config  # lazy: startup time

    policy = _egress_policy_from(load_config())
    _check_tor_or_exit(policy)
    return policy


def _records_adapter_or_exit(source: str, **kwargs: Any) -> tuple[EgressPolicy, Any]:
    """The egress policy and *source*'s adapter, failing closed on a configured Tor that is down.

    OpenSanctions with its bulk file on disk sends nothing, so the Tor check is
    skipped for it: an offline search must work with Tor (and the network) off.
    """
    from .config import load_config  # lazy: startup time
    from .records import get_adapter  # lazy: startup time

    if source != "opensanctions":
        policy = _records_policy_or_exit()
        return policy, get_adapter(source, egress=policy, **kwargs)
    policy = _egress_policy_from(load_config())
    adapter = get_adapter(source, egress=policy, **kwargs)
    if not adapter.offline:
        _check_tor_or_exit(policy)
    return policy, adapter


def _route_note(policy: EgressPolicy, *adapters: Any) -> str:
    note = "via Tor, real IP hidden" if policy.is_tor else "direct, real IP"
    if any(getattr(a, "credentials_withheld", False) for a in adapters):
        note += ", credentials withheld"
    return note


def _records_fetch_cia(identifier: str) -> None:
    """Download a CIA reading room PDF, ingest it (text/OCR/pages), attach to the active case."""
    from .cases import active_case_id_in_db  # lazy: startup time
    from .db import ensure_db, get_data_dir  # lazy: startup time
    from .pipeline.ingest import DocumentIngester  # lazy: startup time
    from .records.cia import CIAReadingRoomAdapter, normalize_identifier  # lazy: startup time

    try:
        kind, page_url, _ = normalize_identifier(identifier)
    except ValueError as e:
        rprint(f"[red]{_safe(e)}[/red]")
        raise typer.Exit(1) from None

    policy = _records_policy_or_exit()
    rprint(
        f"\n[yellow]WARNING: This will send the identifier {_safe(repr(identifier))} to cia.gov "
        f"({_route_note(policy)}).[/yellow]"
    )
    rprint(f"[dim]{_safe(kind)}: {_safe(page_url)}[/dim]")

    ensure_db()
    adapter = CIAReadingRoomAdapter(egress=policy)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(f"Fetching {_safe(identifier)} from the CIA reading room...", total=None)
        try:
            entity, pdf_path = asyncio.run(
                adapter.download(identifier, get_data_dir() / "downloads" / "cia")
            )
        except Exception as e:
            rprint(f"[red]Fetch failed: {_safe(e)}[/red]")
            raise typer.Exit(1) from None

    rprint(f"\n[bold cyan]{_safe(entity.name)}[/bold cyan]")
    rprint(
        f"  ESDN: {_safe(entity.identifiers.get('esdn'))}  "
        f"Pages: {_safe(entity.extra_data.get('pages'))}"
    )
    rprint(f"  PDF: {_safe(pdf_path)}")

    ingester = DocumentIngester(storage_path=get_data_dir() / "docs")
    try:
        result = asyncio.run(ingester.ingest_file(pdf_path))
    except Exception as e:
        rprint(f"[red]Ingest failed: {_safe(e)}[/red]")
        raise typer.Exit(1) from None
    _persist_ingested(
        [result],
        request_number=f"CIA-{entity.identifiers.get('esdn', identifier)}",
        subject=entity.name,
        body=f"Downloaded from the CIA FOIA reading room: {entity.source_url}",
    )
    rprint(f"\n[green]Saved to database:[/green] {result.document_id[:8]}...")
    rprint(f"  Text length: {len(result.extracted_text or ''):,} characters")
    if active_case_id_in_db():
        rprint("  [dim]Attached to the active case.[/dim]")
    rprint(f"\n[dim]Extract entities: openfoia analyze extract {result.document_id}[/dim]")


def _records_fetch_federalregister(adapter, doc_id: str) -> tuple[str, str | None]:
    """Download a Federal Register document's PDF, ingest it, store it under FR-<number>.

    Returns (document_id, extracted_text) for records_fetch's summary.
    """
    from .db import ensure_db, get_data_dir  # lazy: startup time
    from .pipeline.ingest import DocumentIngester  # lazy: startup time

    ensure_db()
    entity, pdf_path = asyncio.run(
        adapter.download(doc_id, get_data_dir() / "downloads" / "federalregister")
    )
    x = entity.extra_data
    number = entity.identifiers["document_number"]
    # Upstream strings: printed as Text so they are never read as Rich markup.
    console.print(Text(safe_text(f"  {number}  {entity.name}"), style="cyan"))
    details = [
        x.get("subtype") or x.get("document_type"),
        ", ".join(x.get("agencies") or []),
        x.get("citation"),
        f"published {x['publication_date']}" if x.get("publication_date") else None,
        f"comments close {x['comments_close_on']}" if x.get("comments_close_on") else None,
        f"docket {', '.join(x['docket_ids'])}" if x.get("docket_ids") else None,
    ]
    console.print(Text(safe_text("    " + " | ".join(d for d in details if d))), style="dim")

    result = asyncio.run(
        DocumentIngester(storage_path=get_data_dir() / "docs").ingest_file(pdf_path)
    )
    if not (result.extracted_text or "").strip():
        # Not stored, so a re-run after installing pdf-extract or OCR retries it
        # instead of skipping it as "already stored".
        Path(result.file_path).unlink(missing_ok=True)
        console.print(
            Text(
                f"    PDF kept at {safe_text(pdf_path)}, but no text was extracted, so it was not "
                "stored. Check that pdf-extract is installed (scanned pages also need "
                "OCR: openfoia install-extras ocr), then fetch again.",
                style="yellow",
            )
        )
        return result.document_id, None
    _persist_ingested(
        [result],
        request_number=f"FR-{number}",
        subject=entity.name,
        body=f"Downloaded from the Federal Register: {entity.source_url or number}",
    )
    return result.document_id, result.extracted_text


@records_app.command("download")
def records_download(
    request_id: str = typer.Argument(..., help="MuckRock request ID"),
    source: str = typer.Option("muckrock", "--source", "-s", help="Data source"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output directory (default: exports/downloads in the data directory)",
    ),
    ingest: bool = typer.Option(
        False, "--ingest", help="Auto-ingest downloaded files into OpenFOIA"
    ),
):
    """Download response documents from a FOIA request.

    Fetches all response PDFs/documents attached to a completed FOIA request
    and saves them locally. Optionally auto-ingests them into the pipeline.

    Examples:
        openfoia records download 68490 --source muckrock
        openfoia records download 68490 -o ./epa-docs --ingest
    """
    if source != "muckrock":
        rprint(f"[yellow]Download only supported for muckrock (got '{_safe(source)}')[/yellow]")
        raise typer.Exit(1)

    from .records.muckrock import MuckRockAdapter  # lazy: startup time

    policy = _records_policy_or_exit()
    adapter = MuckRockAdapter(egress=policy)
    rprint(f"[dim]Contacting MuckRock ({_route_note(policy, adapter)}).[/dim]")

    # First fetch to show what we're downloading
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(f"Fetching request {_safe(request_id)}...", total=None)
        try:
            entity = asyncio.run(adapter.fetch(request_id))
        except Exception as e:
            rprint(f"[red]Failed to fetch request: {_safe(e)}[/red]")
            raise typer.Exit(1) from None

    if not entity:
        rprint(f"[red]Request {_safe(request_id)} not found on MuckRock.[/red]")
        raise typer.Exit(1)

    from .exports import exports_dir, record_export  # lazy: startup time

    if output is None:
        output = exports_dir("downloads")
    files = entity.extra_data.get("files", [])
    rprint(f"\n[bold cyan]{_safe(entity.name)}[/bold cyan]")
    rprint(f"  Status: {_safe(entity.status)}")
    rprint(f"  Documents: {len(files)}")

    if not files:
        rprint("[yellow]No response documents attached to this request.[/yellow]")
        return

    rprint(f"\n[cyan]Downloading {len(files)} file(s) to {_safe(output)}/[/cyan]")

    for f in files:
        url = f.get("url", "")
        filename = url.split("/")[-1] if url else "unknown"
        rprint(f"  {_safe(filename)}")

    # Download
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("Downloading...", total=None)
        try:
            downloaded = asyncio.run(adapter.download_files(request_id, str(output)))
        except Exception as e:
            rprint(f"[red]Download failed: {_safe(e)}[/red]")
            raise typer.Exit(1) from None

    rprint(f"\n[green]{len(downloaded)} file(s) downloaded to {_safe(output)}/[/green]")

    for path in downloaded:
        record_export(path)
        rprint(f"  {_safe(path)}")

    # Auto-ingest if requested
    if ingest and downloaded:
        rprint("\n[cyan]Ingesting into OpenFOIA...[/cyan]")
        from .db import ensure_db, get_data_dir  # lazy: startup time
        from .pipeline.ingest import DocumentIngester  # lazy: startup time

        ensure_db()

        storage_path = get_data_dir() / "docs"
        ingester = DocumentIngester(storage_path=storage_path)

        ingested = 0
        ingest_results = []
        for path in downloaded:
            try:
                result = asyncio.run(ingester.ingest_file(Path(path)))
                ingest_results.append(result)
                ingested += 1
                rprint(
                    f"  [green]Ingested:[/green] {_safe(Path(path).name)} → "
                    f"{result.document_id[:8]}..."
                )
            except Exception as e:
                rprint(f"  [red]Failed:[/red] {_safe(Path(path).name)}: {_safe(e)}")

        if ingest_results:
            _persist_ingested(
                ingest_results,
                request_number=f"MUCKROCK-{request_id}",
                subject=entity.name if entity else f"MuckRock request {request_id}",
                body="Downloaded from MuckRock",
            )

        rprint(f"\n[green]{ingested} file(s) ingested.[/green]")
        rprint("[dim]Run 'openfoia analyze extract --all' to extract entities.[/dim]")


def _persist_ingested(ingest_results, *, request_number: str, subject: str, body: str) -> None:
    """Store ingested files as Document rows under one placeholder Request.

    The Request is attached to the active case. Re-running with the same
    *request_number* reuses the existing Request.
    """
    from .cases import active_case_id_in_db  # lazy: startup time
    from .db import get_session  # lazy: startup time
    from .doctext import replace_pages  # lazy: startup time

    case_id = active_case_id_in_db()
    with get_session() as session:
        user = session.query(User).first()
        if not user:
            user = User(
                id=str(uuid4()),
                email="local@openfoia.local",
                name="Local User",
            )
            session.add(user)
            session.flush()

        agency = session.query(Agency).first()
        # One placeholder per (request number, case): a re-fetch under another
        # active case must not land its documents in the first case.
        dl_req = (
            session.query(Request)
            .filter(
                Request.request_number.in_(
                    [request_number, f"{request_number}@{(case_id or '')[:8]}"]
                ),
                Request.case_id == case_id,
            )
            .first()
        )
        if dl_req is None:
            taken = session.query(Request).filter(Request.request_number == request_number).first()
            dl_req = Request(
                id=str(uuid4()),
                request_number=f"{request_number}@{case_id[:8]}"
                if taken and case_id
                else request_number,
                requester_id=user.id,
                agency_id=agency.id if agency else user.id,
                subject=subject,
                body=body,
                delivery_method=DeliveryMethod.EMAIL,
                status=RequestStatus.COMPLETE,
                case_id=case_id,
            )
            session.add(dl_req)
            session.flush()

        for r in ingest_results:
            doc = Document(
                id=r.document_id,
                request_id=dl_req.id,
                doc_type=DocumentType.FULL_RESPONSE,
                filename=r.filename,
                file_path=r.file_path,
                file_size=r.file_size,
                mime_type=r.mime_type,
                page_count=r.page_count,
                extracted_text=r.extracted_text,
                ocr_completed=bool(r.extracted_text),
            )
            session.add(doc)
            if r.extracted_text:
                replace_pages(session, doc.id, r.pages or [r.extracted_text], r.verdicts)


# === Case Commands ===


def _require_db() -> None:
    from .db import get_db_path  # lazy: startup time

    if not get_db_path().exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)


def _resolve_case_or_exit(session, name: str | None):
    """The named case, else the active case; exits when neither resolves."""
    from .cases import find_case, get_active_case  # lazy: startup time

    case = find_case(session, name) if name else get_active_case(session)
    if case is None:
        if name:
            rprint(f"[red]No case named '{_safe(name)}'. See 'openfoia case list'.[/red]")
        else:
            rprint(
                "[yellow]No active case. Pass a name or run 'openfoia case use <name>'.[/yellow]"
            )
        raise typer.Exit(1)
    return case


def _doc_source_url(doc, req) -> str | None:
    if req is not None and req.body:
        url_match = re.search(r"https?://\S+", req.body)
        if url_match:
            return url_match.group(0)
    dc_match = re.match(r"documentcloud-(\d+)", doc.filename or "")
    if dc_match:
        return f"https://www.documentcloud.org/documents/{dc_match.group(1)}/"
    mr_match = re.match(r"muckrock-(\d+)", doc.filename or "")
    if mr_match:
        return f"https://www.muckrock.com/foi/{mr_match.group(1)}/"
    nara_match = re.match(r"nara-(\d+)", doc.filename or "")
    if nara_match:
        return f"https://catalog.archives.gov/id/{nara_match.group(1)}"
    return None


_CREST_RE = re.compile(r"(?:CIA|NSA)-RDP\d{2}[A-Z]?-?\d{5}[A-Z]\d{12}-\d")
_SOURCE_LABEL = {
    "nara": "National Archives Catalog",
    "documentcloud": "DocumentCloud",
    "courtlistener": "CourtListener",
    "muckrock": "MuckRock",
    "cia": "CIA Reading Room",
}
NOTE_TIERS = ("says", "infer", "speculation")


def _doc_cite(session, doc, req) -> dict[str, Any]:
    """Everything a citation needs for one stored document."""
    number = req.request_number if req else ""
    source = _doc_source(number)
    crest = next(
        (
            m.group(0)
            for text in (
                req.subject if req else None,
                req.body if req else None,
                doc.extracted_text,
            )
            if text and (m := _CREST_RE.search(text))
        ),
        None,
    )
    stored = session.query(DocumentPage).filter(DocumentPage.document_id == doc.id).count()
    return {
        "id": doc.id,
        "title": req.subject if req else (doc.filename or doc.id),
        "source": source,
        "identifier": number.partition("-")[2] if source != "local" else None,
        "crest": crest,
        "source_url": _doc_source_url(doc, req),
        "documentcloud_id": number.removeprefix("DC-") if number.startswith("DC-") else None,
        "filename": doc.filename,
        "pages": doc.page_count,
        "stored_pages": stored,
        "retrieved": doc.received_at.isoformat() if doc.received_at else None,
    }


def _case_summary(session, case) -> dict[str, Any]:
    from .extraction_runs import current_entities  # lazy: startup time

    rows = (
        session.query(Document, RequestModel)
        .join(RequestModel, Document.request_id == RequestModel.id)
        .filter(RequestModel.case_id == case.id)
        .order_by(Document.received_at)
        .all()
    )
    queries = (
        session.query(CaseQuery)
        .filter(CaseQuery.case_id == case.id)
        .order_by(CaseQuery.created_at)
        .all()
    )
    entity_count = (
        current_entities(session.query(Entity))
        .join(Document)
        .join(RequestModel)
        .filter(RequestModel.case_id == case.id)
        .count()
    )
    notes = (
        session.query(CaseNote)
        .filter(CaseNote.case_id == case.id)
        .order_by(CaseNote.created_at, CaseNote.id)
        .all()
    )
    stamps = [
        case.updated_at,
        *(d.received_at for d, _ in rows),
        *(q.created_at for q in queries),
        *(n.created_at for n in notes),
    ]
    documents = [_doc_cite(session, doc, req) for doc, req in rows]
    sources = {d["id"]: d for d in documents}
    findings = []
    for n in notes:
        # Only this case's documents are cited: a note's document_id that is not
        # in the case (moved, deleted) gets no citation rather than leaking one.
        cite = sources.get(n.document_id) or {}
        partial = bool(cite.get("pages")) and cite["stored_pages"] < cite["pages"]
        findings.append(
            {
                "id": n.id,
                "document_id": n.document_id,
                "title": cite.get("title"),
                "source": cite.get("source"),
                "identifier": cite.get("identifier"),
                "crest": cite.get("crest"),
                "page": n.page,
                "page_is_real": not (n.page and partial),
                "retrieved": cite.get("retrieved"),
                "tier": n.tier,
                "quote": n.quote,
                "note": n.note,
                "created": n.created_at.isoformat(),
            }
        )
    return {
        "name": case.name,
        "slug": case.slug,
        "description": case.description,
        "created": case.created_at.isoformat(),
        "last_activity": max(s for s in stamps if s).isoformat(),
        "entity_count": entity_count,
        "documents": documents,
        "findings": findings,
        "sources": list(sources.values()),
        "queries": [
            {
                "source": q.source,
                "query": q.query,
                "total_results": q.total_results,
                "run_at": q.created_at.isoformat(),
            }
            for q in queries
        ],
    }


@case_app.command("new")
def case_new(
    name: str = typer.Argument(..., help="Case name, e.g. 'CIA PAS'"),
    desc: str | None = typer.Option(None, "--desc", "-d", help="Short description"),
):
    """Create a case and make it the active one."""
    from .cases import set_active_case_id, slugify  # lazy: startup time
    from .db import get_db_path, get_session, init_db  # lazy: startup time

    if not get_db_path().exists():
        init_db()

    with get_session() as session:
        slug = slugify(name)
        if session.query(Case).filter(Case.slug == slug).first():
            rprint(f"[red]A case with slug '{slug}' already exists.[/red]")
            raise typer.Exit(1)
        case = Case(name=name, slug=slug, description=desc)
        session.add(case)
        session.flush()
        set_active_case_id(case.id)

    rprint(f"[green]Created case[/green] [cyan]{_safe(name)}[/cyan] ({_safe(slug)}) — now active.")


@case_app.command("list")
def case_list():
    """List cases. The active one is marked with *."""
    from .cases import get_active_case_id  # lazy: startup time
    from .db import get_session  # lazy: startup time

    _require_db()
    active_id = get_active_case_id()
    with get_session() as session:
        cases = session.query(Case).order_by(Case.created_at).all()
        if not cases:
            rprint("[yellow]No cases yet. Create one: openfoia case new <name>[/yellow]")
            return
        for c in cases:
            marker = "*" if c.id == active_id else " "
            rprint(f"{marker} [cyan]{_safe(c.name)}[/cyan]  [dim]{_safe(c.slug)}[/dim]")


@case_app.command("use")
def case_use(name: str = typer.Argument(..., help="Case name or slug")):
    """Make a case active. Searches, fetches and graphs then attach to it."""
    from .cases import set_active_case_id  # lazy: startup time
    from .db import get_session  # lazy: startup time

    _require_db()
    with get_session() as session:
        case = _resolve_case_or_exit(session, name)
        set_active_case_id(case.id)
        rprint(f"[green]Active case:[/green] [cyan]{_safe(case.name)}[/cyan]")


@case_app.command("close")
def case_close():
    """Clear the active case. Nothing is deleted."""
    from .cases import clear_active_case  # lazy: startup time

    clear_active_case()
    rprint("[green]No active case.[/green]")


@case_app.command("edit")
def case_edit(
    name: str | None = typer.Argument(None, help="Case name (default: active)"),
    new_name: str | None = typer.Option(None, "--name", help="New name (the slug follows it)"),
    desc: str | None = typer.Option(None, "--desc", "-d", help="New description ('' to clear)"),
):
    """Rename a case or change its description."""
    from .cases import slugify  # lazy: startup time
    from .db import get_session  # lazy: startup time

    _require_db()
    if new_name is None and desc is None:
        rprint("[red]Pass --name and/or --desc.[/red]")
        raise typer.Exit(2)
    with get_session() as session:
        case = _resolve_case_or_exit(session, name)
        if new_name is not None:
            slug = slugify(new_name)
            clash = session.query(Case).filter(Case.slug == slug, Case.id != case.id).first()
            if clash:
                rprint(f"[red]A case with slug '{slug}' already exists.[/red]")
                raise typer.Exit(1)
            case.name, case.slug = new_name, slug
        if desc is not None:
            case.description = desc or None
        rprint(f"[green]Updated case[/green] [cyan]{_safe(case.name)}[/cyan] ({_safe(case.slug)}).")


@case_app.command("rm")
def case_rm(
    name: str = typer.Argument(..., help="Case name or slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a case with its notes and query log. Documents are kept and detached."""
    from .cases import clear_active_case, get_active_case_id  # lazy: startup time
    from .db import get_session  # lazy: startup time

    _require_db()
    with get_session() as session:
        case = _resolve_case_or_exit(session, name)
        notes = session.query(CaseNote).filter(CaseNote.case_id == case.id).count()
        docs = len(_case_document_ids(session, case.id))
        if not yes:
            rprint(
                f"Delete case [cyan]{case.name}[/cyan]: {notes} note(s) and its query log are "
                f"removed for good; {docs} document(s) stay in the database, detached."
            )
            if not typer.confirm("Continue?", default=False):
                rprint("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(1)
        session.query(CaseNote).filter(CaseNote.case_id == case.id).delete(
            synchronize_session=False
        )
        session.query(CaseQuery).filter(CaseQuery.case_id == case.id).delete(
            synchronize_session=False
        )
        session.query(RequestModel).filter(RequestModel.case_id == case.id).update(
            {"case_id": None}, synchronize_session=False
        )
        was_active = get_active_case_id() == case.id
        label = case.name
        session.delete(case)
    if was_active:
        clear_active_case()
    rprint(
        f"[green]Deleted case[/green] [cyan]{_safe(label)}[/cyan]; {_safe(docs)} document(s) kept."
    )


@case_app.command("move")
def case_move(
    doc: str = typer.Argument(..., help="Document id or prefix"),
    to: str = typer.Option(..., "--to", help="Destination case name or slug"),
):
    """Move a document to another case. Documents fetched together move together."""
    from .db import get_session  # lazy: startup time

    _require_db()
    with get_session() as session:
        target = _resolve_case_or_exit(session, to)
        d = _resolve_doc(session, doc)
        req = session.get(RequestModel, d.request_id) if d.request_id else None
        if req is None:
            rprint(f"[red]Document {doc} has no request record to move.[/red]")
            raise typer.Exit(1)
        if req.case_id == target.id:
            rprint(
                f"[red]Document {_safe(d.id[:8])} is already in case {_safe(target.name)}.[/red]"
            )
            raise typer.Exit(1)
        siblings = [x.id for x in session.query(Document).filter(Document.request_id == req.id)]
        req.case_id = target.id
        # Notes citing these documents follow them; a citation must stay in reach.
        moved_notes = (
            session.query(CaseNote)
            .filter(CaseNote.document_id.in_(siblings), CaseNote.case_id != target.id)
            .update({"case_id": target.id}, synchronize_session=False)
        )
        extra = f" with {len(siblings) - 1} sibling document(s)" if len(siblings) > 1 else ""
        notes = f", {moved_notes} note(s) followed" if moved_notes else ""
        rprint(
            f"[green]Document {d.id[:8]} moved to case[/green] [cyan]{target.name}[/cyan]{extra}{notes}."
        )


@case_app.command("show")
def case_show(name: str | None = typer.Argument(None, help="Case name (default: active)")):
    """Documents, entities, logged queries and last activity for a case."""
    from .db import get_session  # lazy: startup time

    _require_db()
    with get_session() as session:
        s = _case_summary(session, _resolve_case_or_exit(session, name))

    rprint(f"\n[bold cyan]{_safe(s['name'])}[/bold cyan] [dim]({_safe(s['slug'])})[/dim]")
    if s["description"]:
        rprint(f"  {_safe(s['description'])}")
    rprint(f"  Documents:      {len(s['documents'])}")
    rprint(f"  Entities:       {s['entity_count']}")
    rprint(f"  Queries logged: {len(s['queries'])}")
    rprint(f"  Notes:          {len(s['findings'])}")
    rprint(f"  Last activity:  {s['last_activity'][:19]}")


def _citation(f: dict[str, Any]) -> str:
    """One pasteable line: title, source id, CREST number, page, retrieval date."""
    parts = [f["title"] or "untitled document"]
    if f["identifier"]:
        parts.append(f"{_SOURCE_LABEL.get(f['source'], f['source'])} {f['identifier']}")
    if f["crest"]:
        parts.append(f["crest"])
    if f["page"]:
        parts.append(f"p.{f['page']}" + ("" if f["page_is_real"] else "*"))
    if f["retrieved"]:
        parts.append(f"retrieved {f['retrieved'][:10]}")
    return ", ".join(parts)


@case_app.command("note")
def case_note(
    words: list[str] = typer.Argument(
        ..., help="Note text, or: rm <id> [<id>...], or: edit <id> [new text]"
    ),
    doc: str | None = typer.Option(None, "--doc", help="Document id or prefix the note is about"),
    page: int | None = typer.Option(None, "--page", help="Page in that document"),
    quote: str | None = typer.Option(None, "--quote", help="The passage, verbatim"),
    tier: str | None = typer.Option(None, "--tier", help="says, infer or speculation"),
    to: str | None = typer.Option(None, "--to", help="With edit: move the note to this case"),
):
    """Add a finding to the active case, edit one (case note edit <id>) or remove one.

    Examples:
        openfoia case note "Hocking Technical College tested PAS" --doc 3f2a --page 51 --tier says
        openfoia case note "Likely the same program" --tier speculation
        openfoia case note edit 3 "Same program, per the 1987 review" --tier infer
        openfoia case note edit 3 --to "MKULTRA sites"
        openfoia case note rm 3
    """
    from .db import get_session  # lazy: startup time

    _require_db()
    with get_session() as session:
        case = _resolve_case_or_exit(session, None)
        if words[0] == "rm":
            try:
                ids = [int(w) for w in words[1:]]
            except ValueError:
                ids = []
            if not ids:
                rprint("[red]Usage: openfoia case note rm <id> [<id>...][/red]")
                raise typer.Exit(2)
            removed = (
                session.query(CaseNote)
                .filter(CaseNote.case_id == case.id, CaseNote.id.in_(ids))
                .delete(synchronize_session=False)
            )
            rprint(f"[green]Removed {removed} note(s).[/green]")
            return
        if tier is not None and tier not in NOTE_TIERS:
            rprint(f"[red]--tier must be one of: {', '.join(NOTE_TIERS)}[/red]")
            raise typer.Exit(2)
        document_id = _resolve_doc(session, doc).id if doc else None
        if document_id and document_id not in _case_document_ids(session, case.id):
            rprint(f"[red]Document {_safe(doc)} is not in case {_safe(case.name)}.[/red]")
            raise typer.Exit(1)
        if words[0] == "edit":
            try:
                note_id = int(words[1])
            except (IndexError, ValueError):
                rprint("[red]Usage: openfoia case note edit <id> [new text] [--tier ...][/red]")
                raise typer.Exit(2) from None
            n = session.get(CaseNote, note_id)
            if n is None or n.case_id != case.id:
                rprint(f"[red]No note {_safe(note_id)} in case {_safe(case.name)}.[/red]")
                raise typer.Exit(1)
            text = " ".join(words[2:])
            if text:
                n.note = text
            if tier is not None:
                n.tier = tier
            if page is not None:
                n.page = page
            if quote is not None:
                n.quote = quote
            if document_id:
                n.document_id = document_id
            if to is not None:
                target = _resolve_case_or_exit(session, to)
                if target.id == case.id:
                    rprint(f"[red]Note {_safe(n.id)} is already in case {_safe(case.name)}.[/red]")
                    raise typer.Exit(1)
                if n.document_id and n.document_id not in _case_document_ids(session, target.id):
                    rprint(
                        f"[red]Note {n.id} cites a document that is not in case {target.name}; "
                        "move the document first: openfoia case move <doc-id> --to <case>.[/red]"
                    )
                    raise typer.Exit(1)
                n.case_id = target.id
                rprint(
                    f"[green]Note {_safe(n.id)} moved to case[/green] [cyan]{_safe(target.name)}[/cyan]."
                )
                return
            rprint(f"[green]Note {n.id} updated.[/green]")
            return
        n = CaseNote(
            case_id=case.id,
            document_id=document_id,
            page=page,
            quote=quote,
            note=" ".join(words),
            tier=tier,
        )
        session.add(n)
        session.flush()
        rprint(f"[green]Note {_safe(n.id)} added to case[/green] [cyan]{_safe(case.name)}[/cyan].")


@case_app.command("notes")
def case_notes(
    name: str | None = typer.Argument(None, help="Case name (default: active)"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List a case's findings with their citations."""
    from .db import get_session  # lazy: startup time

    _require_db()
    with get_session() as session:
        s = _case_summary(session, _resolve_case_or_exit(session, name))
    if as_json:
        typer.echo(safe_json(s["findings"], indent=2))
        return
    if not s["findings"]:
        rprint(
            '[yellow]No notes. Add one: openfoia case note "<text>" or docs grep --save[/yellow]'
        )
        return
    for f in s["findings"]:
        # Quotes are document text and citations carry upstream titles.
        tier = _safe(f"[{f['tier'] or '-'}]")
        rprint(f"[cyan]{f['id']:>3}[/cyan]  {tier} {_safe(f['note'] or '')}")
        if f["quote"]:
            rprint(f"      > {_safe(f['quote'])}")
        if f["document_id"]:
            rprint(f"      [dim]{_safe(_citation(f))}[/dim]")
    if not all(f["page_is_real"] for f in s["findings"]):
        rprint(f"[dim]* {SINGLE_PAGE_CAVEAT}[/dim]")


@case_app.command("export")
def case_export(
    name: str | None = typer.Argument(None, help="Case name (default: active)"),
    fmt: str = typer.Option("md", "--format", "-f", help="md or json"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write to file"),
):
    """Export a case's findings with citations, document list and query log."""
    from .db import get_session  # lazy: startup time

    if fmt not in ("md", "json"):
        rprint("[red]--format must be md or json.[/red]")
        raise typer.Exit(2)
    _require_db()
    with get_session() as session:
        s = _case_summary(session, _resolve_case_or_exit(session, name))

    if fmt == "json":
        text = safe_json(s, indent=2)
    else:
        lines = [f"# {s['name']}", ""]
        if s["description"]:
            lines += [s["description"], ""]
        lines += [f"Exported {_utcnow().isoformat()[:19]} UTC", "", "## Documents", ""]
        lines += ["| Title | Source | ID / file | Pages | Retrieved |", "|---|---|---|---|---|"]
        for d in s["documents"]:
            cells = [
                d["title"],
                d["source_url"] or "-",
                d["documentcloud_id"] or d["filename"],
                str(d["pages"] or "-"),
                (d["retrieved"] or "-")[:10],
            ]
            lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
        if s["findings"]:
            lines += ["", "## Findings", ""]
            for i, f in enumerate(s["findings"], 1):
                one = lambda t: " ".join((t or "").split())  # noqa: E731
                head = f"{i}. " + (f"**{f['tier']}** " if f["tier"] else "") + one(f["note"])
                lines.append(head.rstrip())
                if f["quote"]:
                    lines.append(f"   > {one(f['quote'])}")
                if f["document_id"]:
                    lines.append(f"   Source: {_citation(f)}.")
            if not all(f["page_is_real"] for f in s["findings"]):
                lines += ["", f"\\* {SINGLE_PAGE_CAVEAT}"]
        if s["sources"]:
            lines += ["", "## Sources", ""]
            for d in s["sources"]:
                parts = [d["title"]]
                if d["identifier"]:
                    parts.append(f"{_SOURCE_LABEL.get(d['source'], d['source'])} {d['identifier']}")
                if d["crest"]:
                    parts.append(d["crest"])
                if d["source_url"]:
                    parts.append(d["source_url"])
                if d["retrieved"]:
                    parts.append(f"retrieved {d['retrieved'][:10]}")
                lines.append("- " + ". ".join(parts) + ".")
        lines += ["", "## Queries", ""]
        for q in s["queries"]:
            total = "?" if q["total_results"] is None else q["total_results"]
            lines.append(f"- {q['run_at'][:19]} `{q['source']}` {q['query']!r} ({total} results)")
        # Titles, quotes and queries are upstream or document text: a report
        # someone will `cat` must not carry terminal escapes either.
        text = safe_text("\n".join(lines) + "\n", multiline=True)

    if output:
        from .exports import record_export  # lazy: startup time

        record_export(output)
        output.write_text(text)
        rprint(
            f"[green]Exported to {_safe(output)}[/green] [dim](plaintext, outside the database)[/dim]"
        )
    else:
        typer.echo(text, nl=False)


# === Unified Search Command ===


def _search_status_line(s) -> str:
    from .search import clean  # lazy: startup time

    if s.status == "ok":
        total = f" of {s.total:,}" if s.total is not None and s.total > s.returned else ""
        text = f"ok: {s.returned} shown{total}"
        if s.older_than_since:
            text += f", {s.older_than_since} older than --since dropped"
    elif s.status == "no_results":
        text = "no results" + (f" ({s.reason})" if s.reason else "")
    elif s.status == "error":
        text = f"error: {s.reason}"
        if s.returned:
            text += f" ({s.returned} result(s) kept from before the error)"
    else:
        text = f"skipped: {s.reason}"
    if s.credentials_withheld and s.status != "skipped":
        text += " [credentials withheld]"
    return clean(f"  {s.source:<15} {text}")


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Person, company, contract or topic to search for"),
    source: list[str] | None = typer.Option(
        None,
        "--source",
        "-s",
        help="Only this source; repeat or comma-separate (default: every registered "
        "source, see 'openfoia records search --help')",
    ),
    egress: str | None = typer.Option(
        None,
        "--egress",
        help="tor or direct for this run (default: config network.tor, see "
        "'openfoia egress-status'). tor never falls back to direct.",
    ),
    limit: int = typer.Option(10, "--limit", "-n", min=1, help="Maximum results per source"),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Drop results dated before YYYY-MM-DD. Filtered here after retrieval "
        "(the sources get the same query); results with no date are kept.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Print one JSON object (per-source status and results) on stdout; "
        "warnings and prompts go to stderr",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the network confirmation prompt"),
):
    """Search every public-records source at once, with provenance on every result.

    Sends the query to each registered records source (the ones 'openfoia
    records search' knows) a few at a time, and reports each source as ok,
    no results, error (with the reason) or skipped (with the reason). A
    source that fails does not stop the others. Each result carries source,
    title, url, snippet, retrieved_at (UTC) and egress (tor or direct).

    Before anything is sent it lists the sources and the route and asks for
    confirmation (--yes skips the question). Sources that need a personal key
    you have not set, or whose key is withheld over Tor, are skipped and not
    sent the query. When a case is active the query is logged to it.

    Exit 0: every source answered or was skipped; 2: some sources errored;
    1: no source answered.

    Examples:
        openfoia search "Acme Corp"
        openfoia search "Acme Corp" -s sec -s opencorporates --limit 5
        openfoia search "Acme Corp" --egress tor --json --yes > hits.json
        openfoia search "water contamination" -s documentcloud,regulations --since 2020-01-01
    """
    from . import search as search_mod  # lazy: startup time
    from .config import load_config  # lazy: startup time
    from .net import TorUnavailableError  # lazy: startup time

    def fail(message: str) -> None:
        _echo(message, err=True)
        raise typer.Exit(1)

    names = [n for raw in source or [] for n in raw.split(",") if n.strip()] or None
    try:
        search_mod.parse_since(since)
        cfg = load_config()
        policy = search_mod.egress_policy(egress.lower() if egress else None, cfg)
        plan = search_mod.plan_search(
            query, names, policy=policy, api_data_gov_key=cfg.records.api_data_gov_key
        )
    except ValueError as e:
        fail(str(e))

    # Fail closed before asking anything: no point confirming a Tor search
    # that cannot go over Tor.
    if plan.adapters:
        try:
            asyncio.run(search_mod.ensure_route(policy))
        except TorUnavailableError as e:
            fail(str(e))

    for line in plan.warning_lines():
        typer.secho(search_mod.clean(line), err=True, fg="yellow")
    # A warning you cannot answer is not consent.
    confirm = "Send this query to the sources listed above?"
    if plan.adapters and not yes and not typer.confirm(confirm, err=True):
        typer.echo("Aborted. Nothing left your machine.", err=True)
        raise typer.Exit(0)

    try:
        report = asyncio.run(
            search_mod.run_search(plan, limit=limit, since=since, allow_network=True)
        )
    except TorUnavailableError as e:
        fail(str(e))

    status_lines = [_search_status_line(s) for s in report.statuses]
    if json_out:
        for line in status_lines:
            typer.echo(line, err=True)
        # ensure_ascii: upstream text cannot put raw control or C1 bytes on stdout.
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=True, indent=2))
    else:
        typer.echo(f"Sources ({_route_note(policy, *plan.adapters.values())}):")
        for line in status_lines:
            typer.echo(line)
        clean = search_mod.clean
        for row in report.rows:
            typer.echo("")
            typer.echo(clean(f"{row['source']} | {(row['date'] or '-')[:10]} | {row['title']}"))
            if row["url"]:
                typer.echo(clean(f"  {row['url']}"))
            details = " | ".join(x for x in (row["id"] and f"id {row['id']}", row["snippet"]) if x)
            if details:
                typer.echo(clean(f"  {details}"))
        answered = sum(s.status in ("ok", "no_results") for s in report.statuses)
        typer.echo(
            f"\n{len(report.rows)} result(s) from {answered} source(s) that answered, "
            f"egress {report.egress}."
        )
        for note in report.notes:
            _echo(note, err=True)
    if report.exit_code:
        raise typer.Exit(report.exit_code)


# === Cross-Reference Command ===


@app.command()
@_csv_stdout_safe
def crossref(
    request_id: str | None = typer.Option(
        None, "--request", "-r", help="Cross-ref entities from a specific request"
    ),
    document_id: str | None = typer.Option(
        None, "--document", "-d", help="Cross-ref entities from a specific document"
    ),
    sources: str | None = typer.Option(
        None,
        "--sources",
        help="Comma-separated sources (muckrock,opencorporates,sec,opensanctions,"
        "documentcloud; offline: icij,opensanctions-local)",
    ),
    icij_data: Path | None = typer.Option(
        None, "--icij-data", help="Path to downloaded ICIJ CSV data"
    ),
    opensanctions_data: Path | None = typer.Option(
        None,
        "--opensanctions-data",
        help="Directory holding targets.simple.csv from "
        "opensanctions.org/datasets/default (offline screening; data is CC BY-NC 4.0, "
        "free for journalists and non-commercial use). Default: the file downloaded by "
        "'openfoia records opensanctions-data --download', when present",
    ),
    output: Path | None = typer.Option(None, "--output", "-o", help="Save report to file"),
    ftm: Path | None = typer.Option(
        None, "--ftm", help="Export results as FollowTheMoney JSON-lines"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the network confirmation prompt"),
    tor: bool | None = typer.Option(
        None,
        "--tor/--no-tor",
        help="Route lookups through Tor to hide your IP from these APIs "
        "(default: config, see 'openfoia egress-status')",
    ),
    csv_path: str | None = typer.Option(
        None, "--csv", help="Write one row per hit as CSV to this file ('-' for stdout)"
    ),
):
    """Cross-reference extracted entities against external databases.

    Checks every person and organization from your documents against
    MuckRock, OpenCorporates, SEC EDGAR, DocumentCloud, OpenSanctions,
    and ICIJ Offshore Leaks. Flags entities that appear in multiple sources.

    This is the free, local version of what Maltego charges $999/year for.

    Examples:
        openfoia crossref                           # all entities in database
        openfoia crossref -r REQ-20260322-ABC123    # from one request
        openfoia crossref --icij-data ./icij-csvs/  # include Offshore Leaks
        openfoia crossref --sources opensanctions-local --opensanctions-data ./os/
        openfoia crossref --ftm results.ftm.json    # export as FollowTheMoney
        openfoia crossref --tor                     # hide your IP from the APIs
        openfoia crossref --csv hits.csv            # one row per hit, for spreadsheets
    """
    from .config import load_config  # lazy: startup time
    from .crossref import OFFLINE_SOURCES, crossref_entities  # lazy: startup time
    from .db import get_db_path, get_session  # lazy: startup time
    from .extraction_runs import current_entities  # lazy: startup time
    from .net import TorUnavailableError, describe_egress  # lazy: startup time
    from .records.opensanctions import OpenSanctionsAdapter, local_status  # lazy: startup time

    source_list = sources.split(",") if sources else None

    if opensanctions_data and opensanctions_data.is_file():
        rprint("[red]--opensanctions-data: pass the directory containing targets.simple.csv.[/red]")
        raise typer.Exit(1)
    if opensanctions_data is None and local_status()["present"]:
        # Downloaded with `records opensanctions-data --download`: screen offline too.
        opensanctions_data = Path(local_status()["path"]).parent

    # A requested offline source with no data would silently drop out of the
    # run and read as "screened, clean".
    for offline_src, data_dir, flag in (
        ("icij", icij_data, "--icij-data"),
        ("opensanctions-local", opensanctions_data, "--opensanctions-data"),
    ):
        if source_list and offline_src in source_list and not (data_dir and data_dir.exists()):
            rprint(
                f"[red]--sources {offline_src} needs {flag} pointing at the downloaded data.[/red]"
            )
            if offline_src == "opensanctions-local":
                rprint(
                    "[dim]Download it with: openfoia records opensanctions-data --download[/dim]"
                )
            raise typer.Exit(1)

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    # Gather entities
    with get_session() as session:
        query = current_entities(session.query(Entity))

        if document_id:
            query = query.filter(Entity.document_id == document_id)
        elif request_id:
            query = query.join(Document).filter(
                (Document.request_id == request_id)
                | (
                    Document.request_id.in_(
                        session.query(RequestModel.id).filter(
                            RequestModel.request_number == request_id
                        )
                    )
                )
            )

        db_entities = query.all()

        if not db_entities:
            rprint("[yellow]No entities found. Run 'openfoia analyze extract' first.[/yellow]")
            raise typer.Exit(1)

        # Convert to extraction format for crossref engine
        from .pipeline.extract import ExtractedEntity  # lazy: startup time

        entities = []
        for e in db_entities:
            entities.append(
                ExtractedEntity(
                    entity_type=e.entity_type,
                    raw_text=e.raw_text,
                    normalized_text=e.normalized_text,
                    confidence=e.confidence,
                    context=e.context or "",
                )
            )

    # Warn about network activity — crossref sends entity names to external APIs
    network_sources = [
        s
        for s in (
            source_list
            or [
                "muckrock",
                "opencorporates",
                "sec",
                "documentcloud",
                "usaspending",
                "nonprofits",
                "govinfo",
                "fec",
                "regulations",
                "opensanctions",
            ]
        )
        if s not in OFFLINE_SOURCES
    ]
    cfg = load_config()
    policy = _egress_policy_from(cfg, tor=tor)
    not_contacted: dict[str, str] = {}
    if "opensanctions" in network_sources:
        # No usable key (or it is withheld over Tor): the adapter sends nothing,
        # so do not list it as receiving the names.
        reason = OpenSanctionsAdapter(egress=policy, mode="api").unavailable_reason()
        if reason:
            network_sources.remove("opensanctions")
            not_contacted["opensanctions"] = reason

    for name, reason in not_contacted.items():
        rprint(f"[dim]  {name}: not contacted ({_safe(reason)}); reported as errored.[/dim]")

    if network_sources:
        # Fail closed before asking the user to confirm anything — no point
        # walking through the leak report if Tor was requested and isn't
        # actually there to protect the request that follows.
        _check_tor_or_exit(policy)

        rprint(
            "\n[yellow]WARNING: Cross-reference will send entity names to external APIs:[/yellow]"
        )
        rprint(f"[yellow]  {', '.join(network_sources)}[/yellow]")
        rprint(
            "[yellow]  The names of the people and organizations you are "
            "investigating will leave this machine.[/yellow]"
        )

        egress_info = describe_egress(policy)
        if policy.is_tor:
            rprint(
                "[cyan]  Egress: Tor — the destination servers will NOT see your real IP "
                f"(stream isolation: {egress_info['stream_isolation']}).[/cyan]"
            )
        else:
            rprint(
                "[yellow]  Egress: direct — the destination servers WILL see your real IP.[/yellow]"
            )
        rprint(
            "[dim]  Either way, the query itself (the subject names above) still reaches "
            "each endpoint — Tor hides who is asking, not what is asked. A global "
            "adversary watching both ends of the connection can still correlate timing.[/dim]"
        )
        rprint(
            "[dim]  Use --sources icij,opensanctions-local for offline-only "
            "(requires --icij-data / --opensanctions-data downloads; get the OpenSanctions "
            "file with 'openfoia records opensanctions-data --download')[/dim]"
        )
        rprint(
            "[dim]  Use --tor to hide your IP from these endpoints (requires Tor running).[/dim]"
        )
        rprint("")

        # A warning you cannot answer is not consent. Confirm before leaking.
        if not yes and not typer.confirm("Send these names to the sources listed above?"):
            rprint("[green]Aborted. Nothing left your machine.[/green]")
            raise typer.Exit(0)

    rprint("[bold]Cross-referencing entities...[/bold]")

    def _progress(event: str, msg: str) -> None:
        if event == "start" or event == "entity":
            rprint(f"[dim]  {_safe(msg)}[/dim]")

    try:
        report = asyncio.run(
            crossref_entities(
                entities,
                sources=source_list,
                icij_data_dir=str(icij_data) if icij_data else None,
                opensanctions_data_dir=str(opensanctions_data) if opensanctions_data else None,
                on_progress=_progress,
                allow_network=True,  # user was warned and confirmed above
                egress=policy,
                api_data_gov_key=cfg.records.api_data_gov_key,
            )
        )
    except TorUnavailableError:
        _report_tor_unavailable()

    # Display results
    rprint("\n[bold]Cross-Reference Report[/bold]")
    rprint(f"  Entities checked: {report.total_entities}")
    rprint(f"  Sources used: {', '.join(report.sources_used)}")
    rprint(f"  Total hits: {report.total_hits}")
    rprint(f"  Entities flagged: {report.total_flagged}")
    source_errors = getattr(report, "source_errors", {})
    if source_errors:
        details = ", ".join(f"{source} ({error})" for source, error in source_errors.items())
        rprint(f"  [red]Sources errored: {len(source_errors)} — {_safe(details)}[/red]")
    rprint("=" * 60)

    for result in report.results:
        if not result.hits:
            continue

        color = (
            "red"
            if any(h.extra.get("is_sanctioned") or h.extra.get("is_pep") for h in result.hits)
            else "yellow"
        )
        rprint(
            f"\n  [{color}]FLAGGED: {_safe(result.entity_name)}[/{color}] "
            f"({_safe(result.entity_type)})"
        )

        for hit in result.hits:
            source_color = {
                "muckrock": "cyan",
                "opencorporates": "blue",
                "sec": "green",
                "icij": "red",
                "opensanctions": "magenta",
                "opensanctions-local": "magenta",
            }.get(hit.source, "white")

            rprint(
                f"    [{source_color}]{_safe(hit.source)}[/{source_color}] "
                f"{_safe(f'[{hit.match_type}]')} {_safe(hit.details)}"
            )
            if hit.url:
                rprint(f"      {_safe(hit.url)}")

    if not report.total_flagged:
        if source_errors:
            rprint(
                "\n  [yellow]No cross-reference hits found among completed source checks; "
                "report is incomplete.[/yellow]"
            )
        else:
            rprint("\n  [green]No cross-reference hits found.[/green]")

    if csv_path:
        hits = Table()
        for column in ("Entity", "Type", "Source", "Match", "Details", "URL"):
            hits.add_column(column)
        for result in report.results:
            for hit in result.hits:
                _row(
                    hits,
                    result.entity_name,
                    result.entity_type,
                    hit.source,
                    hit.match_type,
                    hit.details,
                    hit.url or "",
                )
        _emit_table(hits, csv_path)
        if source_errors:
            rprint("[yellow]CSV is incomplete: some sources errored (see above).[/yellow]")
            raise typer.Exit(1)

    # Export as FollowTheMoney
    if ftm:
        from .ftm import export_ftm  # lazy: startup time

        count = export_ftm(entities, [], ftm)
        rprint(
            f"\n[green]Exported {count} entities to {_safe(ftm)} (FollowTheMoney format)[/green]"
        )

    # Save report
    if output:
        report_data = {
            "total_entities": report.total_entities,
            "total_hits": report.total_hits,
            "total_flagged": report.total_flagged,
            "sources": report.sources_used,
            "source_errors": source_errors,
            "results": [
                {
                    "entity": r.entity_name,
                    "type": r.entity_type,
                    "source_statuses": getattr(r, "source_statuses", {}),
                    "hits": [
                        {
                            "source": h.source,
                            "match": h.match_type,
                            "details": h.details,
                            "url": h.url,
                        }
                        for h in r.hits
                    ],
                }
                for r in report.results
                if r.hits
                or any(status != "checked" for status in getattr(r, "source_statuses", {}).values())
            ],
        }
        from .exports import record_export  # lazy: startup time

        record_export(output)
        output.write_text(json.dumps(report_data, indent=2))
        rprint(f"\n[green]Report saved to {_safe(output)}[/green]")


# === Export Command ===


@analyze_app.command("export")
def analyze_export(
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path (default: exports/entities.ftm.json in the data directory)",
    ),
    request_id: str | None = typer.Option(
        None, "--request", "-r", help="Export from specific request"
    ),
):
    """Export entities as FollowTheMoney JSON-lines.

    Produces a .ftm.json file compatible with Aleph, OpenAleph,
    OpenSanctions, and other investigative journalism tools.

    Examples:
        openfoia analyze export
        openfoia analyze export -o investigation.ftm.json -r REQ-20260322-ABC
    """
    from .db import get_db_path, get_session  # lazy: startup time
    from .extraction_runs import current_entities  # lazy: startup time
    from .ftm import export_ftm  # lazy: startup time
    from .pipeline.extract import ExtractedEntity  # lazy: startup time

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = current_entities(session.query(Entity))
        if request_id:
            query = query.join(Document).filter(
                (Document.request_id == request_id)
                | (
                    Document.request_id.in_(
                        session.query(RequestModel.id).filter(
                            RequestModel.request_number == request_id
                        )
                    )
                )
            )

        db_entities = query.all()
        if not db_entities:
            rprint("[yellow]No entities to export.[/yellow]")
            return

        entities = [
            ExtractedEntity(
                entity_type=e.entity_type,
                raw_text=e.raw_text,
                normalized_text=e.normalized_text,
                confidence=e.confidence,
                context=e.context or "",
            )
            for e in db_entities
        ]

        # Relationships between exported entities only (not earlier runs' or other scopes')
        exported_ids = {e.id for e in db_entities}
        links = [
            lnk
            for lnk in session.query(entity_links).all()
            if lnk.source_id in exported_ids and lnk.target_id in exported_ids
        ]
        # The FtM writer joins relationship ends to entities by name, not database id.
        names = {e.id: e.normalized_text for e in db_entities}
        relationships = [
            {
                "source": names[lnk.source_id],
                "target": names[lnk.target_id],
                "relation": lnk.link_type or "related_to",
            }
            for lnk in links
        ]

    from .exports import resolve_output  # lazy: startup time

    output = resolve_output(output, "entities.ftm.json")
    count = export_ftm(entities, relationships, output)
    rprint(f"[green]Exported {count} entities to {output} (FollowTheMoney format)[/green]")
    rprint("[dim]Compatible with Aleph, OpenAleph, OpenSanctions, and ICIJ tools.[/dim]")


@analyze_app.command("import")
def analyze_import(
    file: Path = typer.Argument(..., help="FtM JSON-lines file to import"),
    tag: str | None = typer.Option(None, "--tag", "-t", help="Tag for this import batch"),
):
    """Import entities from a FollowTheMoney JSON-lines file.

    Reads .ftm.json files produced by Aleph, OpenAleph, OpenSanctions,
    or any FtM-compatible tool and imports them into your local database.

    Imported entities are deduplicated against existing data and can be
    cross-referenced, graphed, and purged like any other entity.

    Use cases:
    - Export from Aleph → import → work offline
    - Download OpenSanctions dump → import → crossref locally
    - Colleague sends you FtM data → import → analyze → purge when done

    Examples:
        openfoia analyze import investigation.ftm.json
        openfoia analyze import sanctions.ftm.json --tag opensanctions
    """
    from .ftm_import import import_ftm_to_db, parse_ftm_file  # lazy: startup time

    if not file.exists():
        rprint(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    # Preview first
    entities, relationships = parse_ftm_file(file)
    rprint(f"\n[cyan]File: {_safe(file)}[/cyan]")
    rprint(f"  Entities: {len(entities)}")
    rprint(f"  Relationships: {len(relationships)}")

    if not entities:
        rprint("[yellow]No entities found in file.[/yellow]")
        return

    # Show schema breakdown
    schemas = Counter(e["schema"] for e in entities)
    for schema, count in schemas.most_common(10):
        rprint(f"  {_safe(schema)}: {count}")  # schema names come from the imported file

    rprint("\n[cyan]Importing...[/cyan]")
    ent_count, rel_count = import_ftm_to_db(file, tag=tag)

    rprint(f"\n[green]Imported {ent_count} entities, {rel_count} relationships[/green]")
    if tag:
        rprint(f"[dim]Tagged as: {_safe(tag)}[/dim]")
    rprint("[dim]Run 'openfoia crossref' to cross-reference imported entities.[/dim]")
    rprint("[dim]Run 'openfoia analyze graph --view' to visualize.[/dim]")


# === MCP Server ===

mcp_app = typer.Typer(help="Local MCP server for agent clients (stdio only, no network port)")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve(
    allow_network: bool = typer.Option(
        False,
        "--allow-network",
        help="Let the openfoia_search tool send queries to public-records sources (after the "
        "user confirms each search). Off by default: every other tool is offline.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Detailed logs on stderr. These can include queries and document text, and MCP "
        "clients keep a server's stderr in log files.",
    ),
):
    """Serve OpenFOIA to an MCP client (Claude Desktop, Claude Code...) over stdin/stdout.

    The client starts this command itself; you do not run it by hand. It never
    listens on a network port. Tools: openfoia_search (network; off unless
    --allow-network, and each search needs the user's confirmation) and
    read-only local tools over your cases, stored documents, entities and
    requests. No tool deletes, purges, sends requests, email, fax or mail, or
    changes config. Everything a tool returns goes to the client's model
    provider. See docs/GUIDE.md (MCP server) for client configuration.

    Examples:
        openfoia mcp tools
        openfoia mcp serve
        openfoia mcp serve --allow-network
    """
    from .mcp_server import MCPUnavailable, serve  # lazy: startup time

    if sys.stdin.isatty():
        typer.echo(
            "openfoia mcp serve speaks MCP on stdin/stdout; an MCP client starts it. "
            "See 'openfoia mcp tools'. Ctrl-C to exit.",
            err=True,
        )
    try:
        serve(allow_network=allow_network, verbose=verbose)
    except MCPUnavailable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


@mcp_app.command("tools")
def mcp_tools(
    allow_network: bool = typer.Option(
        False, "--allow-network", help="Describe the tools as 'mcp serve --allow-network' would"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the tools/list entries as JSON"),
):
    """List the tools 'openfoia mcp serve' offers an agent. Works without the mcp extra."""
    from .mcp_server import ServerOptions, tool_list  # lazy: startup time

    tools = tool_list(ServerOptions(allow_network=allow_network))
    if as_json:
        typer.echo(json.dumps(tools, indent=2))
        return
    for t in tools:
        kind = "network" if t["annotations"]["openWorldHint"] else "local, read-only"
        typer.echo(f"{t['name']:<24} {kind}")
    if not allow_network:
        typer.echo("\nopenfoia_search sends nothing unless the server runs with --allow-network.")


# === Main Entry Point ===


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
