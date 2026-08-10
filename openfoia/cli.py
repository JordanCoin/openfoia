"""OpenFOIA command-line interface."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from .net import EgressPolicy
from rich import print as rprint
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .models import utcnow as _utcnow

app = typer.Typer(
    name="openfoia",
    help="Crowdsourced FOIA automation with AI-powered document analysis.",
    no_args_is_help=True,
)


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
    from .db import get_data_dir, get_db_path, has_sqlcipher, init_db

    data_dir = get_data_dir()

    rprint("\n[bold green]🔒 OpenFOIA Initialization[/bold green]")
    rprint("─" * 50)

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

    db_path = get_db_path(password=password)

    if (password or duress_password) and not has_sqlcipher():
        rprint("[bold red]ERROR: pysqlcipher3 is not installed.[/bold red]")
        rprint("[red]Cannot create an encrypted database or decoy profile without it.[/red]")
        rprint("[yellow]Install with: openfoia install-extras encryption[/yellow]")
        raise typer.Exit(1)

    if db_path.exists() and not force:
        rprint(f"[cyan]Database already exists:[/cyan] {db_path}")
        rprint("[dim]Use --force to re-initialize (WARNING: loses data)[/dim]")

        # Show stats
        from .db import get_session
        from .models import Agency, Document, Request

        with get_session(password=password) as session:
            agency_count = session.query(Agency).count()
            request_count = session.query(Request).count()
            doc_count = session.query(Document).count()

        rprint("\n[cyan]Current data:[/cyan]")
        rprint(f"  Agencies: {agency_count}")
        rprint(f"  Requests: {request_count}")
        rprint(f"  Documents: {doc_count}")
        return

    if force and db_path.exists():
        rprint("[yellow]Removing existing database...[/yellow]")
        db_path.unlink()

    rprint(f"[cyan]Data directory:[/cyan] {data_dir}")
    rprint(f"[cyan]Database:[/cyan] {db_path}")
    if password and has_sqlcipher():
        rprint("[cyan]Encryption:[/cyan] AES-256 (SQLCipher)")

    # Initialize
    rprint("\n[cyan]Creating tables...[/cyan]")
    init_db(seed=not no_seed, password=password)

    if not no_seed:
        from .db import get_session
        from .models import Agency

        with get_session(password=password) as session:
            count = session.query(Agency).count()
        rprint(f"[green]✓ Seeded {count} federal agencies[/green]")

    # Duress mode setup
    if duress_password:
        from .security import setup_duress_mode

        if duress_password == password:
            rprint(
                "[bold red]Error: duress password must differ from the real password.[/bold red]"
            )
            raise typer.Exit(1)

        decoy_path = setup_duress_mode(duress_password)
        rprint("[green]✓ Duress mode configured[/green]")
        rprint(f"[dim]  Decoy database: {decoy_path}[/dim]")
        rprint(
            "[dim]  When the duress password is used, the decoy DB is opened transparently.[/dim]"
        )

    rprint("\n[bold green]✓ Initialization complete![/bold green]")
    rprint("[dim]Run 'openfoia serve' to start the web interface.[/dim]\n")


# === Server Command ===


@app.command("install-extras")
def install_extras(
    extra: str = typer.Argument(
        ..., help="Extra to install: ner, ocr, fax, mail, cloud-ai, tor, browser, encryption, all"
    ),
):
    """Install optional features after the initial setup.

    The base install is lightweight (~30MB). Add features as you need them.

    Examples:
        openfoia install-extras ner          # GLiNER entity extraction (~2GB)
        openfoia install-extras ocr          # Tesseract OCR support
        openfoia install-extras cloud-ai     # Anthropic + OpenAI
        openfoia install-extras encryption   # SQLCipher encrypted database
        openfoia install-extras all          # everything
    """
    import subprocess
    import sys

    valid = {
        "ner",
        "ner-spacy",
        "ocr",
        "fax",
        "mail",
        "cloud-ai",
        "tor",
        "browser",
        "encryption",
        "all",
    }
    if extra not in valid:
        rprint(f"[red]Unknown extra '{extra}'. Choose from: {', '.join(sorted(valid))}[/red]")
        raise typer.Exit(1)

    size_hints = {
        "ner": "~2GB (PyTorch + GLiNER model)",
        "ner-spacy": "~100MB (+ download a model after)",
        "ocr": "~5MB (also needs system tesseract-ocr + poppler-utils)",
        "fax": "~20MB",
        "mail": "~5MB",
        "cloud-ai": "~30MB",
        "tor": "~1MB",
        "browser": "~50MB (+ playwright install after)",
        "encryption": "~5MB (also needs system libsqlcipher-dev)",
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
        elif extra == "browser":
            rprint("[dim]Also run: playwright install chromium[/dim]")
        elif extra == "encryption":
            rprint(
                "[dim]Also install system package: apt install libsqlcipher-dev (Linux) or brew install sqlcipher (macOS)[/dim]"
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

    Examples:
        openfoia serve                    # Auto-select browser, private mode
        openfoia serve --browser firefox  # Use Firefox
        openfoia serve --tor              # Use Tor Browser or Brave with Tor
        openfoia serve --no-browser       # Just print URL, don't open
    """
    import secrets
    import socket

    from .browser import BrowserType, detect_browsers, launch_browser, print_browser_menu

    # Generate session token for security
    token = secrets.token_urlsafe(16)

    # Find available port if not specified
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

    url = f"http://{host}:{port}/?token={token}"

    rprint("\n[bold green]🔒 OpenFOIA[/bold green]")
    rprint("─" * 50)
    rprint(f"[cyan]Local server:[/cyan] {url}")
    rprint("[cyan]Data stored:[/cyan]  ~/.openfoia/")
    rprint("─" * 50)
    rprint("[dim]Your data never leaves this machine.[/dim]")
    rprint("[dim]Press Ctrl+C to stop the server.[/dim]\n")

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
            rprint(f"[green]Opening {target_browser.name} ({mode} mode)...[/green]\n")
            launch_browser(url, target_browser, private=private, tor_mode=tor)
        else:
            rprint("[yellow]No browser auto-selected. Copy the URL above.[/yellow]\n")

    # Start the server
    from .db import get_data_dir
    from .server import run_server

    run_server(host=host, port=port, token=token, data_dir=get_data_dir())


console = Console()

# Subcommands
request_app = typer.Typer(help="Manage FOIA requests")
docs_app = typer.Typer(help="Process documents")
campaign_app = typer.Typer(help="Manage campaigns")
agency_app = typer.Typer(help="Manage agencies")
analyze_app = typer.Typer(help="Analyze documents")
template_app = typer.Typer(help="Request templates")
records_app = typer.Typer(help="Search public records")

entities_app = typer.Typer(help="Manage custom entity types")
deadline_app = typer.Typer(help="Track FOIA deadlines")
db_app = typer.Typer(help="Database management")

app.add_typer(request_app, name="request")
app.add_typer(docs_app, name="docs")
app.add_typer(campaign_app, name="campaign")
app.add_typer(agency_app, name="agency")
app.add_typer(analyze_app, name="analyze")
app.add_typer(template_app, name="template")
app.add_typer(records_app, name="records")
app.add_typer(entities_app, name="entities")
app.add_typer(deadline_app, name="deadlines")
app.add_typer(db_app, name="db")


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
    from .db import get_db_path, run_migrations

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

    Creates an encrypted copy of the current database and swaps it in place.
    A backup of the original plaintext database is saved as data.db.bak.

    Requires pysqlcipher3: pip install 'openfoia[encryption]'

    Examples:
        openfoia db encrypt --password SECRET
        openfoia db encrypt   # will prompt for password
    """
    from .db import encrypt_database, get_db_path, has_sqlcipher

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
        rprint(f"[bold red]Encryption failed:[/bold red] {e}")
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
    from .db import get_data_dir

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
            from .config import redact_secrets

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
    from uuid import uuid4

    from .db import get_db_path, get_session, init_db
    from .models import (
        Agency as AgencyModel,
    )
    from .models import (
        DeliveryMethod,
        RequestStatus,
        User,
    )
    from .models import (
        Request as RequestModel,
    )

    if body_file:
        body = body_file.read_text()
    elif not body:
        rprint("[yellow]Enter request body (Ctrl+D when done):[/yellow]")
        import sys

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
def request_list(
    status: str | None = typer.Option(None, "--status", "-s", help="Filter by status"),
    agency: str | None = typer.Option(None, "--agency", "-a", help="Filter by agency"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum results"),
):
    """List FOIA requests."""
    from .db import get_db_path, get_session
    from .models import Agency as AgencyModel
    from .models import Request as RequestModel
    from .models import RequestStatus

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
                rprint(f"[red]Invalid status '{status}'.[/red]")
                raise typer.Exit(1) from None

        if agency:
            query = query.filter(
                (AgencyModel.abbreviation.ilike(agency)) | (AgencyModel.name.ilike(f"%{agency}%"))
            )

        requests = query.order_by(RequestModel.created_at.desc()).limit(limit).all()

        if not requests:
            rprint("[yellow]No requests found.[/yellow]")
            return

        table = Table(title=f"FOIA Requests ({len(requests)} results)")
        table.add_column("Request #", style="cyan")
        table.add_column("Agency")
        table.add_column("Subject")
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
            table.add_row(
                r.request_number,
                r.agency.abbreviation or r.agency.name,
                r.subject[:40] + "..." if len(r.subject) > 40 else r.subject,
                f"[{color}]{r.status.value}[/{color}]",
                sent_str,
                days,
            )

        console.print(table)


@request_app.command("status")
def request_status(
    request_id: str = typer.Argument(..., help="Request ID or number"),
):
    """Check status of a FOIA request."""
    from .db import get_db_path, get_session
    from .models import Request as RequestModel
    from .models import TimelineEvent

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
            rprint(f"[red]Request '{request_id}' not found.[/red]")
            raise typer.Exit(1)

        rprint(f"\n[bold cyan]{request.request_number}[/bold cyan]")
        rprint("=" * 50)

        info_table = Table(show_header=False, box=None)
        info_table.add_column("Field", style="cyan", width=20)
        info_table.add_column("Value")

        info_table.add_row("Agency", request.agency.name)
        info_table.add_row("Subject", request.subject)
        info_table.add_row("Status", request.status.value.replace("_", " ").title())
        info_table.add_row(
            "Delivery Method", request.delivery_method.value.replace("_", " ").title()
        )
        info_table.add_row("Created", request.created_at.strftime("%Y-%m-%d %H:%M"))
        if request.sent_at:
            info_table.add_row("Sent", request.sent_at.strftime("%Y-%m-%d %H:%M"))
        if request.acknowledged_at:
            info_table.add_row("Acknowledged", request.acknowledged_at.strftime("%Y-%m-%d %H:%M"))
        if request.due_date:
            overdue = " [red](OVERDUE)[/red]" if request.is_overdue() else ""
            info_table.add_row("Due Date", request.due_date.strftime("%Y-%m-%d") + overdue)
        if request.agency_tracking_number:
            info_table.add_row("Tracking #", request.agency_tracking_number)
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
                timeline_table.add_row(
                    event.event_type,
                    event.occurred_at.strftime("%Y-%m-%d %H:%M"),
                    event.description,
                )
            console.print(timeline_table)

        rprint("")


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
    import asyncio

    from .db import get_db_path, get_session
    from .gateways.base import DeliveryPayload
    from .models import Agency

    if method not in ("email", "fax", "mail"):
        rprint(f"[red]Unknown method '{method}'. Use: email, fax, mail[/red]")
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
            rprint(f"[red]No FOIA email found for '{agency}'. Specify with --to.[/red]")
            raise typer.Exit(1)
    elif method == "fax":
        recipient = to_address or agency_fax
        if not recipient:
            rprint(f"[red]No FOIA fax number found for '{agency}'. Specify with --to.[/red]")
            rprint("[dim]Example: --to '+12025551234'[/dim]")
            raise typer.Exit(1)
    elif method == "mail":
        recipient = to_address or agency_address
        if not recipient:
            rprint(f"[red]No FOIA mailing address found for '{agency}'. Specify with --to.[/red]")
            rprint("[dim]Format: 'Name\\nStreet\\nCity, ST ZIP' (use \\n for newlines)[/dim]")
            raise typer.Exit(1)
        # Handle escaped newlines from CLI
        recipient = recipient.replace("\\n", "\n")

    # Get body content
    if template:
        from .templates import RequestDetails, RequesterInfo, records_about_self, standard_request

        requester = RequesterInfo(name=name, email=email)
        details = RequestDetails(subject=subject, description=subject)

        if template == "standard":
            body = standard_request(requester=requester, agency_name=agency_name, details=details)
        elif template == "self":
            body = records_about_self(
                requester=requester, agency_name=agency_name, record_type=subject
            )
        else:
            rprint(f"[red]Unknown template '{template}'. Use: standard, self[/red]")
            raise typer.Exit(1)
    elif body_file:
        body = body_file.read_text()
    elif not body:
        rprint("[yellow]Enter request body (Ctrl+D when done):[/yellow]")
        import sys

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
    rprint(f"[cyan]To:[/cyan] {recipient}")
    rprint(f"[cyan]Subject:[/cyan] FOIA Request: {subject}")
    rprint(f"[cyan]From:[/cyan] {name} <{email}>")

    # Show cost estimate for paid methods
    if method == "fax":
        from .gateways.fax import TwilioFaxGateway

        est_pages = max(1, len(body) // 3000 + 1) + (1 if payload.cover_page else 0)
        est_cost = est_pages * TwilioFaxGateway.COST_PER_PAGE_CENTS
        rprint(f"[cyan]Est. Cost:[/cyan] ${est_cost / 100:.2f} ({est_pages} pages @ $0.07/page)")
    elif method == "mail":
        from .gateways.mail import LobMailGateway

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
    from .db import get_data_dir

    config_path = get_data_dir() / "config.json"
    import json
    import os

    config = {}
    if config_path.exists():
        with contextlib.suppress(json.JSONDecodeError):
            config = json.loads(config_path.read_text())

    # Build and send via the appropriate gateway
    if method == "email":
        from .gateways.email import EmailGateway

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
        from .gateways.fax import TwilioFaxGateway

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
        from .gateways.mail import LobMailGateway

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

    rprint(f"\n[cyan]Sending via {method_labels[method]}...[/cyan]")
    result = asyncio.run(gateway.send(payload))

    if result.success:
        rprint("[bold green]Request sent![/bold green]")
        rprint(f"  Reference: {result.reference_id}")
        rprint(f"  Sent at: {result.sent_at}")
        if result.cost_cents:
            rprint(f"  Estimated cost: ${result.cost_cents / 100:.2f}")

        # Show method-specific details
        if result.metadata:
            if method == "fax" and result.metadata.get("estimated_pages"):
                rprint(f"  Estimated pages: {result.metadata['estimated_pages']}")
            if method == "mail":
                if result.metadata.get("tracking_number"):
                    rprint(f"  Tracking #: {result.metadata['tracking_number']}")
                if result.metadata.get("expected_delivery_date"):
                    rprint(f"  Expected delivery: {result.metadata['expected_delivery_date']}")

        # Update the matching Request in the DB if one exists
        from datetime import timedelta

        from .db import get_db_path, get_session
        from .models import Agency as AgencyModel
        from .models import Request as RequestModel
        from .models import RequestStatus

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
                    # Auto-set due date
                    response_days = req.agency.typical_response_days if req.agency else 20
                    current = req.sent_at
                    days_added = 0
                    while days_added < response_days:
                        current += timedelta(days=1)
                        if current.weekday() < 5:
                            days_added += 1
                    req.due_date = current

        rprint("\n[dim]Save this reference to track your request.[/dim]")
    else:
        rprint("[bold red]Failed to send[/bold red]")
        rprint(f"  Error: {result.error_message}")
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
    Automatically strips sensitive metadata (EXIF, author info, etc.)
    unless --keep-metadata is specified.
    Supports PDF, DOCX, TXT, and image files.

    Examples:
        openfoia docs ingest ./response.pdf
        openfoia docs ingest ./foia-docs/ --ocr
        openfoia docs ingest ./evidence/ -r REQ-2026-001
        openfoia docs ingest ./doc.pdf --keep-metadata
    """
    import asyncio

    from .db import get_data_dir, get_db_path, init_db
    from .pipeline.ingest import DocumentIngester

    # Ensure database exists
    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Initializing database...[/yellow]")
        init_db()

    storage_path = get_data_dir() / "docs"
    ingester = DocumentIngester(storage_path=storage_path)

    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        if path.is_file():
            task = progress.add_task(f"Ingesting {path.name}...", total=None)
            try:
                result = asyncio.run(
                    ingester.ingest_file(
                        path,
                        request_id=request_id,
                        strip_metadata=not keep_metadata,
                    )
                )
                results.append(result)
                stripped = result.metadata.get("metadata_stripped") or {}
                if stripped.get("stripped"):
                    rprint(
                        f"[green]✓[/green] {path.name} → {result.document_id[:8]}... (stripped {len(stripped['stripped'])} metadata fields)"
                    )
                else:
                    rprint(f"[green]✓[/green] {path.name} → {result.document_id[:8]}...")
            except Exception as e:
                rprint(f"[red]✗[/red] {path.name}: {e}")
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
                rprint(f"[yellow]No supported files found in {path}[/yellow]")
                return

            task = progress.add_task("Ingesting...", total=len(files))

            for file in files:
                progress.update(task, description=f"Ingesting {file.name}...")
                try:
                    result = asyncio.run(
                        ingester.ingest_file(
                            file,
                            request_id=request_id,
                            strip_metadata=not keep_metadata,
                        )
                    )
                    results.append(result)
                except Exception as e:
                    rprint(f"[red]✗[/red] {file.name}: {e}")
                progress.advance(task)

    # Persist Document rows to database
    if results:
        from uuid import uuid4

        from .db import get_session
        from .models import Document, DocumentType

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
                    from .models import DeliveryMethod, Request, RequestStatus, User

                    user = session.query(User).first()
                    if not user:
                        user = User(
                            id=str(uuid4()), email="local@openfoia.local", name="Local User"
                        )
                        session.add(user)
                        session.flush()
                    from .models import Agency

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

    # Summary
    rprint(f"\n[green]✓ Ingested {len(results)} documents[/green]")

    total_pages = sum(r.page_count or 0 for r in results)
    total_size = sum(r.file_size for r in results)
    rprint(f"  Total pages: {total_pages}")
    rprint(f"  Total size: {total_size / 1024 / 1024:.1f} MB")
    rprint(f"  Storage: {storage_path}")

    if not keep_metadata:
        stripped_count = sum(
            1 for r in results if (r.metadata.get("metadata_stripped") or {}).get("stripped")
        )
        if stripped_count:
            rprint(f"  Metadata stripped: {stripped_count} files")

    # Run OCR if requested
    if ocr and results:
        rprint("\n[cyan]Running OCR...[/cyan]")
        from .pipeline.ocr import OCREngine

        engine = OCREngine(backend="tesseract")

        for result in results:
            if result.mime_type == "application/pdf":
                rprint(f"  OCR: {result.filename}...")
                try:
                    ocr_result = asyncio.run(engine.process_pdf(result.file_path))
                    rprint(
                        f"    [green]✓[/green] {ocr_result.page_count} pages, {ocr_result.confidence:.1%} confidence"
                    )
                except Exception as e:
                    rprint(f"    [red]✗[/red] OCR failed: {e}")


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
    import asyncio

    from .pipeline.ocr import OCREngine, RedactionDetector

    if not file_path.exists():
        rprint(f"[red]File not found: {file_path}[/red]")
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
            rprint(f"[red]Missing dependency: {e}[/red]")
            rprint("[dim]Install with: pip install pytesseract pdf2image[/dim]")
            rprint("[dim]Also need: brew install tesseract poppler (macOS)[/dim]")
            raise typer.Exit(1) from None
        except Exception as e:
            rprint(f"[red]OCR failed: {e}[/red]")
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
        output.write_text(result.text)
        rprint(f"\n[green]Text saved to {output}[/green]")
    else:
        rprint("\n[dim]Use --output to save extracted text[/dim]")

    # Show redaction details if found
    if redactions["exemptions_cited"]:
        rprint("\n[yellow]⚠️  Exemptions cited in document:[/yellow]")
        for ex in redactions["exemptions_cited"]:
            rprint(f"  • {ex['code']}: {ex['description']} ({ex['count']}x)")


# === Agency Commands ===


@agency_app.command("list")
def agency_list(
    level: str | None = typer.Option(
        None, "--level", "-l", help="Filter by level (federal/state/local)"
    ),
    state: str | None = typer.Option(None, "--state", "-s", help="Filter by state (2-letter code)"),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum results"),
):
    """List agencies in the database."""
    from .db import get_db_path, get_session
    from .models import Agency, AgencyLevel

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = session.query(Agency)

        if level:
            try:
                level_enum = AgencyLevel(level.lower())
                query = query.filter(Agency.level == level_enum)
            except ValueError:
                rprint(f"[red]Invalid level '{level}'. Use: federal, state, local, tribal[/red]")
                raise typer.Exit(1) from None

        if state:
            query = query.filter(Agency.state == state.upper())

        agencies = query.order_by(Agency.name).limit(limit).all()

        if not agencies:
            rprint("[yellow]No agencies found.[/yellow]")
            return

        table = Table(title=f"Agencies ({len(agencies)} results)")
        table.add_column("Abbr", style="cyan", width=8)
        table.add_column("Name")
        table.add_column("Level", width=8)
        table.add_column("Contact", width=30)

        for a in agencies:
            contact = a.foia_email or a.foia_portal_url or "—"
            if len(contact) > 28:
                contact = contact[:25] + "..."
            table.add_row(
                a.abbreviation or "—",
                a.name,
                a.level.value,
                contact,
            )

        console.print(table)


@agency_app.command("search")
def agency_search(
    query: str = typer.Argument(..., help="Search term"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum results"),
):
    """Search for agencies by name or abbreviation."""
    from .db import get_db_path, get_session
    from .models import Agency

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
            rprint(f"[yellow]No agencies found matching '{query}'.[/yellow]")
            return

        table = Table(title=f"Search results for '{query}' ({len(agencies)} found)")
        table.add_column("Abbr", style="cyan", width=8)
        table.add_column("Name")
        table.add_column("Email/Portal")

        for a in agencies:
            contact = a.foia_email or a.foia_portal_url or "—"
            table.add_row(
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
    from .db import get_db_path, get_session
    from .models import Agency

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
            rprint(f"[red]Agency '{agency_id}' not found.[/red]")
            raise typer.Exit(1)

        rprint(f"\n[bold cyan]{agency.name}[/bold cyan]")
        if agency.abbreviation:
            rprint(f"[dim]({agency.abbreviation})[/dim]")
        rprint("─" * 50)

        table = Table(show_header=False, box=None)
        table.add_column("Field", style="cyan", width=20)
        table.add_column("Value")

        table.add_row("Level", agency.level.value.title())
        if agency.state:
            table.add_row("State", agency.state)

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
        table.add_row("Typical Response", f"{agency.typical_response_days} days")

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
    from .templates import list_templates

    templates = list_templates()

    table = Table(title="Available Templates")
    table.add_column("Name", style="cyan")
    table.add_column("Description")

    for t in templates:
        table.add_row(t["name"], t["description"])

    console.print(table)
    rprint("\n[dim]Use 'openfoia template generate <name>' to create a request.[/dim]")


@template_app.command("generate")
def template_generate(
    template_name: str = typer.Argument(..., help="Template name (standard/appeal/self)"),
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
):
    """Generate a FOIA request from a template.

    Examples:
        openfoia template generate standard -a FBI -s "Records on X" -n "Jane Doe" -e jane@example.com
        openfoia template generate standard -a EPA -s "Pollution data" -n "John Smith" -e john@example.com -j
    """
    from .templates import RequestDetails, RequesterInfo, records_about_self, standard_request

    # Build requester info
    requester = RequesterInfo(
        name=name,
        email=email,
        address=address,
        organization=organization,
        is_journalist=journalist,
    )

    # Get agency name from database if abbreviation
    from .db import get_db_path

    agency_name = agency
    db_path = get_db_path()
    if db_path.exists():
        from .db import get_session
        from .models import Agency

        with get_session() as session:
            found = (
                session.query(Agency)
                .filter((Agency.abbreviation.ilike(agency)) | (Agency.name.ilike(f"%{agency}%")))
                .first()
            )
            if found:
                agency_name = found.name

    # Generate based on template type
    if template_name == "standard":
        details = RequestDetails(subject=subject, description=subject)
        letter = standard_request(
            requester=requester,
            agency_name=agency_name,
            details=details,
            fee_waiver=not no_fee_waiver,
            expedited=expedited,
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
    from uuid import uuid4

    from .db import get_db_path, get_session, init_db
    from .models import Campaign, User

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
    rprint(f"  Name: {name}")
    rprint(f"  Target: {target} requests")
    rprint(f"  Template: {template_file}")


@campaign_app.command("list")
def campaign_list():
    """List all campaigns."""
    from .db import get_db_path, get_session
    from .models import Campaign

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
            table.add_row(
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
    from .db import get_db_path, get_session
    from .models import Campaign, RequestStatus

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(Campaign.id.like(f"{campaign_id}%")).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{campaign_id}' not found.[/red]")
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
    from uuid import uuid4

    from .db import get_db_path, get_session
    from .models import Campaign, User

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(Campaign.id.like(f"{campaign_id}%")).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{campaign_id}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(
                f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]"
            )
            raise typer.Exit(1)

        campaign = matches[0]

        if not campaign.is_active:
            rprint(f"[red]Campaign '{campaign.name}' is no longer active.[/red]")
            raise typer.Exit(1)

        # Get or create user
        user = session.query(User).filter(User.email == email).first()
        if not user:
            user = User(id=str(uuid4()), email=email, name=name)
            session.add(user)
            session.flush()

        # Check if already a participant
        if user in campaign.participants:
            rprint(f"[yellow]You are already a participant in '{campaign.name}'.[/yellow]")
            return

        campaign.participants.append(user)
        campaign_name = campaign.name
        participant_count = len(campaign.participants)

    rprint(f"[green]Joined campaign: {campaign_name}[/green]")
    rprint(f"  Participants now: {participant_count}")


@campaign_app.command("distribute")
def campaign_distribute(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)"),
):
    """Distribute FOIA requests to campaign participants.

    Generates individual requests from the campaign template for each
    target agency and assigns them round-robin to participants.
    """
    from uuid import uuid4

    from .db import get_db_path, get_session
    from .models import (
        Agency as AgencyModel,
    )
    from .models import (
        Campaign,
        DeliveryMethod,
        RequestStatus,
    )
    from .models import (
        Request as RequestModel,
    )

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(Campaign.id.like(f"{campaign_id}%")).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{campaign_id}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(
                f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]"
            )
            raise typer.Exit(1)

        campaign = matches[0]

        if not campaign.is_active:
            rprint(f"[red]Campaign '{campaign.name}' is no longer active.[/red]")
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
            table.add_row(
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
    from .db import get_db_path, get_session
    from .models import Agency as AgencyModel
    from .models import Campaign

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(Campaign.id.like(f"{campaign_id}%")).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{campaign_id}' not found.[/red]")
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
            rprint(f"[yellow]No participants in campaign '{campaign.name}'.[/yellow]")
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

        rprint(f"\n[bold]Campaign: {campaign.name}[/bold]")
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
def analyze_extract(
    document_id: str = typer.Argument(..., help="Document ID to analyze"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output file"),
    force: bool = typer.Option(False, "--force", help="Re-extract even if already done"),
    model: str | None = typer.Option(
        None, "--model", "-m", help="LLM model (e.g. llama3.1:8b, llama3.2:3b)"
    ),
    ensemble: bool = typer.Option(
        False, "--ensemble", help="Run all available NER backends together"
    ),
):
    """Extract entities from a document.

    Pipeline: regex + NER → merge → LLM validation (if available).
    Use --ensemble to run ALL NER backends (GLiNER + spaCy) together.
    """
    from .db import get_db_path, get_session
    from .models import Document, Entity

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
            rprint(f"[red]Document '{document_id}' not found.[/red]")
            raise typer.Exit(1)

        if not doc.extracted_text:
            rprint("[yellow]Document has no extracted text. Run OCR first:[/yellow]")
            rprint(f"[dim]openfoia docs ocr {doc.file_path}[/dim]")
            raise typer.Exit(1)

        if doc.entities_extracted and not force:
            rprint(
                "[yellow]Entities already extracted for this document. Use --force to re-extract.[/yellow]"
            )
            return

        if doc.entities_extracted and force:
            # Remove entity links first (FK constraint), then entities
            existing_ids = [
                e.id for e in session.query(Entity).filter(Entity.document_id == doc.id).all()
            ]
            if existing_ids:
                from .models import entity_links as el_table

                session.execute(
                    el_table.delete().where(
                        el_table.c.source_id.in_(existing_ids)
                        | el_table.c.target_id.in_(existing_ids)
                    )
                )
            session.query(Entity).filter(Entity.document_id == doc.id).delete()

        rprint(f"[cyan]Extracting entities from {doc.filename}...[/cyan]")

        # Run extraction
        import asyncio

        from .pipeline.extract import EntityExtractor

        extractor = EntityExtractor(model=model) if model else EntityExtractor()
        backend = extractor._resolve_backend()

        if backend == "regex":
            rprint("[yellow]Using regex-only extraction (dates, money, emails, phones).[/yellow]")
            rprint("[yellow]For people and organizations, install a NER model:[/yellow]")
            rprint(
                "[cyan]  openfoia install-extras ner        [/cyan] [dim]GLiNER (~2GB, best accuracy)[/dim]"
            )
            rprint(
                "[cyan]  openfoia install-extras ner-spacy  [/cyan] [dim]spaCy (~100MB, good accuracy)[/dim]"
            )
            rprint("[dim]Or run a local LLM: ollama pull llama3.2:3b[/dim]")
            rprint("")

        try:
            result = asyncio.run(extractor.extract(doc.extracted_text, ensemble=ensemble))
        except Exception as e:
            rprint(f"[red]Extraction failed: {e}[/red]")
            rprint("[dim]Ensure AI provider is configured: openfoia config --init[/dim]")
            raise typer.Exit(1) from None

        if not result.entities:
            rprint("[yellow]No entities found in document.[/yellow]")
            return

        # Save entities to database
        from uuid import uuid4

        from .models import entity_links

        entity_id_map: dict[str, str] = {}  # normalized_text.lower() -> entity.id

        for ent in result.entities:
            eid = str(uuid4())
            entity_id_map[ent.normalized_text.lower()] = eid
            entity = Entity(
                id=eid,
                document_id=doc.id,
                entity_type=ent.entity_type,
                raw_text=ent.raw_text,
                normalized_text=ent.normalized_text,
                confidence=ent.confidence,
                context=ent.context,
                page_number=ent.page_number,
            )
            session.add(entity)

        # Flush entities so FKs exist before inserting links
        session.flush()

        # Save relationships to entity_links
        rels_saved = 0
        for rel in result.relationships:
            src = (rel.get("source") or "").lower()
            tgt = (rel.get("target") or "").lower()
            src_id = entity_id_map.get(src)
            tgt_id = entity_id_map.get(tgt)
            if not src_id or not tgt_id or src_id == tgt_id:
                # Try substring match
                for key, eid in entity_id_map.items():
                    if not src_id and src in key:
                        src_id = eid
                    if not tgt_id and tgt in key:
                        tgt_id = eid
            if src_id and tgt_id and src_id != tgt_id:
                try:
                    session.execute(
                        entity_links.insert().values(
                            source_id=src_id,
                            target_id=tgt_id,
                            link_type=rel.get("relation", "related_to"),
                        )
                    )
                    rels_saved += 1
                except Exception:
                    pass  # duplicate link, skip

        doc.entities_extracted = True

        rprint(
            f"[green]Extracted {len(result.entities)} entities, {rels_saved} relationships[/green]"
        )

        table = Table(title="Extracted Entities")
        table.add_column("Type", style="cyan")
        table.add_column("Entity")
        table.add_column("Confidence")

        for ent in result.entities:
            table.add_row(
                ent.entity_type.value,
                ent.normalized_text,
                f"{ent.confidence:.0%}",
            )

        console.print(table)

        if output:
            import json as json_mod

            output.write_text(
                json_mod.dumps(
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
            rprint(f"\n[green]Entities saved to {output}[/green]")


@analyze_app.command("graphs")
def analyze_graphs_list():
    """List saved investigation graphs.

    Shows all named graphs saved in ~/.openfoia/graphs/.

    Examples:
        openfoia analyze graphs
    """
    from .db import get_data_dir

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

        table.add_row(name, " + ".join(types), size_str, modified)

    console.print(table)
    rprint("\n[dim]View a graph: openfoia analyze graph --name <name> --view[/dim]")
    rprint(f"[dim]Graphs stored in: {graphs_dir}[/dim]")


@analyze_app.command("graph")
def analyze_graph(
    request_id: str | None = typer.Option(None, "--request", "-r", help="Analyze single request"),
    campaign_id: str | None = typer.Option(
        None, "--campaign", "-c", help="Analyze entire campaign"
    ),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Save as named graph (stored in ~/.openfoia/graphs/)"
    ),
    output: Path = typer.Option(
        "graph.json", "--output", "-o", help="Output file for JSON (ignored if --name is set)"
    ),
    view: bool = typer.Option(
        False, "--view", "-v", help="Open interactive HTML visualization in browser"
    ),
):
    """Build entity relationship graph from extracted entities.

    Use --name to save graphs by investigation name. Each investigation
    gets its own graph that you can revisit later.

    Examples:
        openfoia analyze graph --view                          # everything, open in browser
        openfoia analyze graph --name defense-contracts --view # save + view
        openfoia analyze graph --request REQ-001 --name epa    # filter + save
        openfoia analyze graphs                                # list saved graphs
    """
    from .db import get_db_path, get_session
    from .models import Document, Entity, entity_links
    from .models import Request as RequestModel

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = session.query(Entity)

        if request_id:
            # Accept either UUID or request number (e.g., REQ-20260322-ABC)
            from .models import Request as ReqModel

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

        entities = query.all()

        if not entities:
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
            import re as re_mod

            from .models import Document as DocModel
            from .models import Request as ReqModel

            for doc in session.query(DocModel).filter(DocModel.id.in_(doc_ids)).all():
                # Derive source URL from request body or filename
                source_url = None
                if doc.request_id:
                    req = session.query(ReqModel).filter(ReqModel.id == doc.request_id).first()
                    if req and req.body:
                        # Extract URL from "Pulled from DocumentCloud: https://..."
                        url_match = re_mod.search(r"https?://\S+", req.body)
                        if url_match:
                            source_url = url_match.group(0)
                # Fallback: derive from filename pattern
                if not source_url and doc.filename:
                    dc_match = re_mod.match(r"documentcloud-(\d+)", doc.filename)
                    if dc_match:
                        source_url = f"https://www.documentcloud.org/documents/{dc_match.group(1)}/"
                    mr_match = re_mod.match(r"muckrock-(\d+)", doc.filename or "")
                    if mr_match:
                        source_url = f"https://www.muckrock.com/foi/{mr_match.group(1)}/"

                documents[doc.id] = {
                    "id": doc.id,
                    "filename": doc.filename or "Unknown",
                    "page_count": doc.page_count,
                    "text": doc.extracted_text or "",
                    "request_id": doc.request_id,
                    "source_url": source_url,
                }

        graph_data = {"nodes": nodes, "edges": edges, "documents": documents}

        import json as json_mod

        # Determine output paths
        if name:
            from .db import get_data_dir

            graphs_dir = get_data_dir() / "graphs"
            graphs_dir.mkdir(parents=True, exist_ok=True)
            safe_name = name.lower().replace(" ", "-").replace("/", "-")
            json_path = graphs_dir / f"{safe_name}.json"
            html_path = graphs_dir / f"{safe_name}.html"
        else:
            json_path = output
            html_path = output.with_suffix(".html")

        json_path.write_text(json_mod.dumps(graph_data, indent=2))

        rprint(f"[green]Graph exported to {json_path}[/green]")
        rprint(f"  Entities: {len(nodes)}")
        rprint(f"  Relationships: {len(edges)}")
        if name:
            rprint(f"  Saved as: [cyan]{name}[/cyan]")

        if view:
            _generate_graph_html(graph_data, html_path)
            rprint(f"[green]Visualization: {html_path}[/green]")

            import webbrowser

            webbrowser.open(f"file://{html_path.resolve()}")
            rprint("[cyan]Opened in browser.[/cyan]")


def _generate_graph_html(graph_data: dict, output_path: Path) -> None:
    """Generate a standalone HTML file with an interactive entity graph visualization."""
    import json as json_mod

    from .graph_template import render

    render(json_mod.dumps(graph_data), output_path)


# === Entity Type Commands ===


def _load_config_data() -> tuple[Path, dict]:
    """Load config.json as raw dict."""
    from .db import get_data_dir

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
        table.add_row(ct.get("name", ""), ct.get("pattern", ""), ct.get("description", ""))

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
    import re

    # Validate the regex
    try:
        re.compile(pattern)
    except re.error as e:
        rprint(f"[red]Invalid regex pattern: {e}[/red]")
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

    rprint(f"[green]Added entity type '{name}'[/green]")
    rprint(f"  Pattern: {pattern}")
    rprint(f"  Description: {description}")


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
        rprint(f"[yellow]Entity type '{name}' not found.[/yellow]")
        raise typer.Exit(1)

    data["entities"]["custom_types"] = custom_types
    _save_config_data(config_path, data)
    rprint(f"[green]Removed entity type '{name}'[/green]")


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


def _llm_map_columns(headers: list[str], sample_rows: list[list[str]]) -> dict[str, int] | None:
    """Use the configured LLM to figure out which columns map to name/pattern/description."""
    from .config import load_config
    from .pipeline.extract import _call_ollama, _llm_available

    cfg = load_config()
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

        import re as _re

        match = _re.search(r"\{[^}]+\}", resp)
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
    from .config import load_config
    from .pipeline.extract import _llm_available

    cfg = load_config()
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
            import urllib.request

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
        import re as _re

        lines = resp.strip().split("\n")
        pattern = lines[0].strip().strip("`\"'{}").strip()

        # Remove JSON artifacts if the LLM wrapped it
        if ":" in pattern and pattern.startswith("{"):
            return None
        if len(pattern) > 100:
            return None  # hallucinated garbage

        if not pattern or not any(c in pattern for c in r"\[]()+*?{}dswb"):
            return None  # doesn't look like regex

        _re.compile(pattern)  # validate

        # Sanity check: try to find an example in the description and see if regex matches
        # Extract things that look like examples (alphanumeric patterns with dashes/digits)
        examples = _re.findall(r"[A-Z]{1,4}[-]?\d{2,}[-]?\d*[-]?[A-Z]?[-]?\d*", description)
        if examples:
            matched_any = any(_re.search(pattern, ex) for ex in examples)
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
    import csv
    import re

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

        rprint(f"[cyan]Columns found: {', '.join(header)}[/cyan]")

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
                    rprint(f"  {field} → column '{header[idx]}' (index {idx})")
            else:
                rprint(
                    "[yellow]No AI available. Using first 3 columns as name, pattern, description.[/yellow]"
                )
        elif len(col_map) >= 2:
            rprint("[green]Auto-mapped columns:[/green]")
            for field, idx in col_map.items():
                rprint(f"  {field} → column '{header[idx]}'")

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
            rprint(f"[cyan]  '{name_val}': pattern looks like English, generating regex...[/cyan]")
            generated = _llm_generate_regex(pattern_val)
            if generated:
                rprint(f"[green]    Generated: {generated}[/green]")
                # Use the original text as description if no description exists
                if not desc_val:
                    desc_val = pattern_val
                pattern_val = generated
                ai_generated += 1
            else:
                rprint(
                    f"[red]  Row {row_num}: '{pattern_val}' is not valid regex and AI couldn't generate one[/red]"
                )
                errors += 1
                continue
        elif not pattern_val:
            rprint(f"[red]  Row {row_num}: no pattern for '{name_val}'[/red]")
            errors += 1
            continue

        # Final validation
        try:
            re.compile(pattern_val)
        except re.error as e:
            rprint(f"[red]  Row {row_num}: invalid regex for '{name_val}': {e}[/red]")
            errors += 1
            continue

        if name_val in existing_names:
            rprint(f"[dim]  Skipped '{name_val}' (already exists)[/dim]")
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
    output: Path = typer.Option("entity_types.csv", "--output", "-o", help="Output CSV file"),
):
    """Export custom entity types to a CSV file."""
    import csv

    _, data = _load_config_data()
    custom_types = data.get("entities", {}).get("custom_types", [])

    if not custom_types:
        rprint("[dim]No custom entity types to export.[/dim]")
        return

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
    import re

    if file:
        text = file.read_text()
    elif not text:
        import sys

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
            table.add_row(name, "[red]INVALID REGEX[/red]", "")
            continue

        unique_matches = sorted(set(matches))
        total_matches += len(unique_matches)
        examples = ", ".join(unique_matches[:5])
        if len(unique_matches) > 5:
            examples += f" ... +{len(unique_matches) - 5} more"

        count_str = f"[green]{len(unique_matches)}[/green]" if unique_matches else "[dim]0[/dim]"
        table.add_row(name, count_str, examples or "[dim]none[/dim]")

    console.print(table)
    rprint(
        f"\n[cyan]{total_matches} total matches across {len(custom_types)} entity type(s)[/cyan]"
    )


# === Deadline Commands ===


def _foia_due_date(sent_date: datetime, business_days: int = 20) -> datetime:
    """Calculate FOIA due date (20 business days from sent date per 5 U.S.C. 552)."""
    from datetime import timedelta

    current = sent_date
    days_added = 0
    while days_added < business_days:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Monday=0 through Friday=4
            days_added += 1
    return current


@deadline_app.command("list")
def deadline_list(
    show_all: bool = typer.Option(False, "--all", help="Include completed/closed requests"),
):
    """Show FOIA deadlines and overdue requests.

    Federal agencies have 20 business days to respond (5 U.S.C. 552).
    This command shows what's due, what's overdue, and what needs follow-up.
    """
    from .db import get_db_path, get_session
    from .models import Agency as AgencyModel
    from .models import Request as RequestModel
    from .models import RequestStatus

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = session.query(RequestModel).join(AgencyModel)

        if not show_all:
            query = query.filter(
                RequestModel.status.in_(
                    [
                        RequestStatus.SENT,
                        RequestStatus.ACKNOWLEDGED,
                        RequestStatus.PROCESSING,
                        RequestStatus.FEE_ESTIMATE,
                    ]
                )
            )

        requests = query.order_by(RequestModel.sent_at).all()

        if not requests:
            rprint("[dim]No active requests with deadlines.[/dim]")
            return

        overdue = []
        upcoming = []
        no_deadline = []

        for r in requests:
            if not r.sent_at:
                no_deadline.append(r)
                continue

            if not r.due_date:
                # Auto-calculate from sent_at
                r.due_date = _foia_due_date(r.sent_at, r.agency.typical_response_days or 20)

            if r.is_overdue():
                overdue.append(r)
            else:
                upcoming.append(r)

        # Show overdue first
        if overdue:
            rprint(f"\n[bold red]OVERDUE ({len(overdue)})[/bold red]")
            table = Table()
            table.add_column("Request #", style="cyan")
            table.add_column("Agency")
            table.add_column("Subject")
            table.add_column("Sent")
            table.add_column("Due")
            table.add_column("Days Over", style="red")

            for r in overdue:
                days_over = (_utcnow() - r.due_date).days
                table.add_row(
                    r.request_number,
                    r.agency.abbreviation or r.agency.name,
                    r.subject[:35] + "..." if len(r.subject) > 35 else r.subject,
                    r.sent_at.strftime("%Y-%m-%d"),
                    r.due_date.strftime("%Y-%m-%d"),
                    f"+{days_over} days",
                )
            console.print(table)

        if upcoming:
            rprint(f"\n[bold yellow]UPCOMING ({len(upcoming)})[/bold yellow]")
            table = Table()
            table.add_column("Request #", style="cyan")
            table.add_column("Agency")
            table.add_column("Subject")
            table.add_column("Sent")
            table.add_column("Due")
            table.add_column("Days Left", style="green")

            for r in upcoming:
                days_left = (r.due_date - _utcnow()).days
                color = "green" if days_left > 5 else "yellow"
                table.add_row(
                    r.request_number,
                    r.agency.abbreviation or r.agency.name,
                    r.subject[:35] + "..." if len(r.subject) > 35 else r.subject,
                    r.sent_at.strftime("%Y-%m-%d"),
                    r.due_date.strftime("%Y-%m-%d"),
                    f"[{color}]{days_left} days[/{color}]",
                )
            console.print(table)

        if no_deadline:
            rprint(f"\n[dim]{len(no_deadline)} draft/unsent requests (no deadline)[/dim]")

        rprint("")


@deadline_app.command("check")
def deadline_check():
    """Check for overdue requests and print warnings.

    Useful for cron jobs or shell startup. Returns exit code 1 if any overdue.

    Example (add to .bashrc):
        openfoia deadlines check 2>/dev/null
    """
    from .db import get_db_path, get_session
    from .models import Request as RequestModel
    from .models import RequestStatus

    db_path = get_db_path()
    if not db_path.exists():
        return

    with get_session() as session:
        requests = (
            session.query(RequestModel)
            .filter(
                RequestModel.status.in_(
                    [
                        RequestStatus.SENT,
                        RequestStatus.ACKNOWLEDGED,
                        RequestStatus.PROCESSING,
                    ]
                ),
                RequestModel.sent_at.isnot(None),
            )
            .all()
        )

        overdue_count = 0
        for r in requests:
            if not r.due_date and r.sent_at:
                r.due_date = _foia_due_date(r.sent_at)
            if r.is_overdue():
                overdue_count += 1
                days_over = (_utcnow() - r.due_date).days
                rprint(f"[red]OVERDUE:[/red] {r.request_number} — {r.subject} (+{days_over} days)")

        if overdue_count:
            rprint(
                f"\n[red]{overdue_count} overdue request(s). Run 'openfoia deadlines list' for details.[/red]"
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
    from .net import EgressMode, EgressPolicy

    use_tor = config.network.tor if tor is None else tor
    return EgressPolicy(
        mode=EgressMode.TOR if use_tor else EgressMode.DIRECT,
        tor_host=config.network.tor_host,
        tor_port=config.network.tor_port,
        isolate_streams=config.network.isolate_streams,
    )


def _check_tor_or_exit(policy: EgressPolicy) -> None:
    """Fail-closed Tor readiness gate.

    If *policy* is DIRECT this is a no-op. If it is TOR, probe the SOCKS
    port before any request is made; if it is not reachable, print a clear
    error and abort (typer.Exit) rather than silently falling through to a
    clearnet request — that silent fallback is exactly the deanonymization
    leak Principle 1 rules out.
    """
    from .net import check_tor

    if not policy.is_tor:
        return

    if not asyncio.run(check_tor(policy)):
        rprint(
            f"[red]Tor egress requested but the SOCKS proxy at "
            f"{policy.tor_host}:{policy.tor_port} is not reachable.[/red]"
        )
        rprint(
            "[yellow]Start Tor (e.g. `tor` / `sudo systemctl start tor`) or drop --tor.[/yellow]"
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
    from .config import load_config
    from .net import check_tor, describe_egress

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
    tor: bool = typer.Option(
        False, "--tor", help="Route traffic through Tor (SOCKS5 localhost:9050)"
    ),
    headless: bool = typer.Option(False, "--headless", help="Run browser without visible window"),
    save: bool = typer.Option(
        False, "--save", help="Extract page content and save to data directory"
    ),
):
    """Browse a URL with optional Tor routing and fingerprint hardening.

    Uses Playwright to launch a hardened Chromium instance. When --tor is
    specified, traffic is routed through the Tor SOCKS5 proxy at localhost:9050
    (the Tor daemon must be running).

    Fingerprint hardening is always applied: WebGL disabled, WebRTC disabled,
    timezone set to UTC, and a common user-agent is used.

    \b
    Examples:
        openfoia browse https://example.com                 # Normal browsing
        openfoia browse https://example.onion --tor         # Tor browsing
        openfoia browse https://example.com --save          # Save page content
        openfoia browse https://example.com --tor --headless --save  # Headless Tor
    """
    import asyncio

    from .tor_browse import browse as _browse

    try:
        result = asyncio.run(
            _browse(
                url,
                use_tor=tor,
                headless=headless,
                save=save,
            )
        )
    except SystemExit:
        raise typer.Exit(1) from None
    except Exception as e:
        rprint(f"[red]Browse failed:[/red] {e}")
        raise typer.Exit(1) from None

    rprint(f"\n[cyan]Title:[/cyan] {result.get('title', 'N/A')}")
    rprint(f"[cyan]URL:[/cyan]   {result.get('url', url)}")
    if result.get("saved_to"):
        rprint(f"[cyan]Saved:[/cyan] {result['saved_to']}")


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

    Removes the database, all ingested documents, exports, config,
    and the entire ~/.openfoia/ directory. This cannot be undone.

    For situations where you need everything off this machine, now.

    \b
    Modes:
      openfoia purge --yes          Fast delete (rm -rf). Not forensic.
      openfoia purge --secure       Overwrite all files 3x, scrub shell history.
      openfoia purge --secure --fill  Also fill free space with random data.
    """
    import shutil

    from openfoia.security import (
        clear_shell_history,
        fill_free_space,
        get_decoy_db_path,
        print_ssd_warning,
        secure_delete_dir,
    )

    from .db import get_data_dir as _get_data_dir

    data_dir = _get_data_dir()

    if not data_dir.exists():
        rprint("[dim]Nothing to purge. No data directory found.[/dim]")
        return

    if fill and not secure:
        rprint("[red]--fill requires --secure.[/red]")
        raise typer.Exit(1)

    decoy_path = get_decoy_db_path()
    has_decoy = decoy_path.exists()

    if not confirm:
        rprint("\n[bold red]This will permanently destroy:[/bold red]")
        rprint(f"  [red]{data_dir}/data.db[/red]     — all requests, entities, tracking")
        if has_decoy:
            rprint(f"  [red]{data_dir}/profile_*.db[/red] — all database profiles")
        rprint(f"  [red]{data_dir}/docs/[/red]       — all ingested documents")
        rprint(f"  [red]{data_dir}/exports/[/red]    — all generated reports")
        rprint(f"  [red]{data_dir}/config.json[/red] — your configuration")
        rprint(f"\n  [bold red]Everything in {data_dir}[/bold red]")

        if secure:
            rprint("\n[yellow]Secure mode: files will be overwritten 3x with random data.[/yellow]")
            print_ssd_warning()
        if fill:
            rprint(
                "[yellow]Fill mode: free disk space will be overwritten. This may take a long time.[/yellow]"
            )

        rprint("\n[dim]This cannot be undone.[/dim]\n")

        answer = typer.prompt("Type PURGE to confirm", default="")
        if answer != "PURGE":
            rprint("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    if secure:
        print_ssd_warning()
        rprint("[bold]Secure-deleting all files (3-pass overwrite)...[/bold]")
        count = secure_delete_dir(data_dir)
        rprint(f"[green]{count} file(s) securely deleted.[/green]")

        rprint("[bold]Scrubbing shell history...[/bold]")
        modified = clear_shell_history()
        if modified:
            rprint(f"[green]Cleaned {len(modified)} history file(s): {', '.join(modified)}[/green]")
        else:
            rprint("[dim]No shell history entries found.[/dim]")

        if fill:
            rprint(
                "[bold]Filling free disk space with random data (this will take a while)...[/bold]"
            )
            fill_dir = data_dir.parent / ".openfoia_fill"
            fill_free_space(fill_dir)
            rprint("[green]Free space filled and cleaned up.[/green]")
    else:
        shutil.rmtree(data_dir)

    rprint(f"\n[green]Data directory removed: {data_dir}[/green]")
    rprint(
        "[dim]Note: swap files, filesystem journals, browser caches, and OS-level caches are NOT affected.[/dim]"
    )
    rprint("[dim]For maximum safety, use full-disk encryption.[/dim]\n")


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
    import asyncio

    from .config import load_config
    from .db import get_data_dir
    from .net import TorUnavailableError
    from .pipeline.web import archive_url

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
        progress.add_task(f"Fetching{mode}: {url}", total=None)

        try:
            result = asyncio.run(
                archive_url(url, storage_path, use_tor=policy.is_tor, egress=policy)
            )
        except TorUnavailableError:
            _report_tor_unavailable()
        except Exception as e:
            rprint(f"[red]Failed to fetch URL: {e}[/red]")
            if policy.is_tor:
                rprint("[dim]Make sure Tor is running: brew install tor && tor[/dim]")
            raise typer.Exit(1) from None

    rprint("\n[bold green]Archived web page[/bold green]")
    rprint("=" * 50)

    table = Table(show_header=False, box=None)
    table.add_column("Field", style="cyan", width=16)
    table.add_column("Value")

    table.add_row("Title", result.title)
    table.add_row("URL", result.url)
    table.add_row("Document ID", result.document_id[:12] + "...")
    table.add_row("Size", f"{result.file_size / 1024:.1f} KB")
    table.add_row("Text length", f"{len(result.text):,} chars")
    table.add_row("HTML saved", result.html_path)
    table.add_row("Text saved", result.text_path)
    table.add_row("Checksum", result.checksum[:16] + "...")
    if policy.is_tor:
        table.add_row("Tor", "Yes")

    console.print(table)

    if output:
        output.write_text(result.text)
        rprint(f"\n[green]Text saved to {output}[/green]")

    rprint(f"\n[dim]Content stored in {storage_path}[/dim]")


# === Public Records Commands ===


@records_app.command("search")
def records_search(
    query: str = typer.Argument(..., help="Search term (company name, keyword, etc.)"),
    source: str = typer.Option(
        "opencorporates",
        "--source",
        "-s",
        help="Data source (muckrock, opencorporates, sec)",
    ),
    jurisdiction: str | None = typer.Option(
        None, "--jurisdiction", "-j", help="Jurisdiction filter (e.g. us_ca, gb)"
    ),
    filing_type: str | None = typer.Option(
        None, "--type", "-t", help="Filing type filter for SEC (e.g. 10-K, 8-K)"
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results to display"),
    raw: bool = typer.Option(False, "--raw", help="Print raw JSON output"),
):
    """Search public records databases.

    Searches external public records APIs and returns normalized entities.
    No API keys needed for basic searches.

    Sources:
      opencorporates  - Company registrations worldwide
      sec             - SEC EDGAR filings (US public companies)

    Examples:
        openfoia records search "Acme Corp" --source opencorporates
        openfoia records search "Acme Corp" --source sec
        openfoia records search "Palantir" --source sec --type 10-K
        openfoia records search "Shell" --source opencorporates -j gb
        openfoia records search "EPA water" --source muckrock
    """
    import asyncio

    from .records import get_adapter, list_sources

    # Validate source
    available = list_sources()
    if source not in available:
        rprint(f"[red]Unknown source '{source}'. Available: {', '.join(available)}[/red]")
        raise typer.Exit(1)

    adapter = get_adapter(source)

    kwargs: dict[str, Any] = {}
    if jurisdiction:
        kwargs["jurisdiction"] = jurisdiction
    if filing_type:
        kwargs["filing_type"] = filing_type

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(f"Searching {source} for '{query}'...", total=None)

        try:
            result = asyncio.run(adapter.search(query, **kwargs))
        except Exception as e:
            rprint(f"[red]Search failed: {e}[/red]")
            raise typer.Exit(1) from None

    if raw:
        rprint(
            json.dumps(
                [e.to_dict() for e in result.entities[:limit]],
                indent=2,
                default=str,
            )
        )
        return

    if not result.entities:
        if result.error:
            rprint(f"[red]Search error ({source}): {result.error}[/red]")
        else:
            rprint(f"[yellow]No results found for '{query}' on {source}.[/yellow]")
        return

    rprint(f"\n[bold]{source}[/bold]: {result.total_results} total results for '{query}'")
    rprint("=" * 60)

    entities = result.entities[:limit]

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
            table.add_row(
                e.identifiers.get("muckrock_id", "-"),
                e.name,
                e.extra_data.get("username", "-"),
                str(e.extra_data.get("files_count", 0)),
                types_str,
                (e.extra_data.get("completed") or e.extra_data.get("submitted") or "-")[:10],
            )
        console.print(table)
        rprint("\n[dim]Download documents: openfoia records download <ID> --source muckrock[/dim]")

    elif source == "documentcloud":
        table = Table(expand=True)
        table.add_column("ID", style="dim", width=10)
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
            table.add_row(
                e.identifiers.get("documentcloud_id", "-")[:10],
                e.name,
                str(e.extra_data.get("pages") or "-"),
                e.extra_data.get("source", "-") or "-",
                highlight_str,
            )
        console.print(table)
        rprint("\n[dim]Fetch full text: openfoia records fetch <ID> --source documentcloud[/dim]")

    elif source == "usaspending":
        table = Table(expand=True)
        table.add_column("Recipient", style="cyan", ratio=2)
        table.add_column("Amount", width=12, justify="right", style="green")
        table.add_column("Agency", ratio=2)
        table.add_column("Type", width=10)
        table.add_column("Description", ratio=3)

        for e in entities:
            table.add_row(
                e.name,
                e.extra_data.get("amount_formatted", "-"),
                e.extra_data.get("awarding_agency", "-") or "-",
                e.extra_data.get("award_type", "-") or "-",
                (e.extra_data.get("description") or "-")[:80],
            )
        console.print(table)
        rprint("\n[dim]View on USAspending.gov: click award URLs in the table[/dim]")

    elif source == "nonprofits":
        table = Table(expand=True)
        table.add_column("EIN", style="dim", width=12)
        table.add_column("Name", style="cyan", ratio=3)
        table.add_column("Location", width=18)
        table.add_column("Type", width=12)

        for e in entities:
            table.add_row(
                e.identifiers.get("strein", "-"),
                e.name,
                e.extra_data.get("location", "-") or "-",
                e.extra_data.get("subsection", "-") or "-",
            )
        console.print(table)
        rprint("\n[dim]View details: openfoia records fetch <EIN> --source nonprofits[/dim]")

    elif source == "govinfo":
        table = Table(expand=True)
        table.add_column("Collection", width=18)
        table.add_column("Title", style="cyan", ratio=3)
        table.add_column("Author", ratio=2)
        table.add_column("Date", width=12)

        for e in entities:
            table.add_row(
                e.extra_data.get("collection", "-"),
                e.name[:80],
                e.extra_data.get("government_author", "-") or "-",
                e.extra_data.get("date_issued", "-") or "-",
            )
        console.print(table)

    elif source == "fec":
        table = Table(expand=True)
        table.add_column("Contributor", style="cyan", ratio=2)
        table.add_column("Amount", width=10, justify="right", style="green")
        table.add_column("To", ratio=2)
        table.add_column("Date", width=12)

        for e in entities:
            table.add_row(
                e.name,
                e.extra_data.get("amount_formatted", "-"),
                e.extra_data.get("committee_name", "-") or "-",
                (e.extra_data.get("date") or "-")[:10],
            )
        console.print(table)
        rprint("\n[dim]View on FEC.gov: openfoia records fetch <name> --source fec[/dim]")

    elif source == "regulations":
        table = Table(expand=True)
        table.add_column("Agency", width=10)
        table.add_column("Title", style="cyan", ratio=3)
        table.add_column("Type", width=16)
        table.add_column("Date", width=12)

        for e in entities:
            table.add_row(
                e.extra_data.get("agency", "-") or "-",
                e.name[:80],
                e.extra_data.get("document_type", "-") or "-",
                e.extra_data.get("posted_date", "-") or "-",
            )
        console.print(table)

    elif source == "opencorporates":
        table = Table()
        table.add_column("Name", style="cyan", max_width=30)
        table.add_column("Jurisdiction", width=12)
        table.add_column("Company #", width=14)
        table.add_column("Status", width=12)
        table.add_column("Type", width=14)
        table.add_column("Officers", max_width=30)

        for e in entities:
            officers = e.extra_data.get("officers", [])
            officer_str = ", ".join(o["name"] for o in officers[:3]) if officers else "-"
            if len(officers) > 3:
                officer_str += f" (+{len(officers) - 3} more)"

            table.add_row(
                e.name,
                e.jurisdiction or "-",
                e.identifiers.get("company_number", "-"),
                e.status or "-",
                e.extra_data.get("company_type", "-"),
                officer_str,
            )
        console.print(table)

    elif source == "sec":
        table = Table()
        table.add_column("Company", style="cyan", max_width=30)
        table.add_column("Filing", width=10)
        table.add_column("Date", width=12)
        table.add_column("CIK", width=12)
        table.add_column("URL", max_width=50)

        for e in entities:
            table.add_row(
                e.name,
                e.extra_data.get("filing_type", "-"),
                e.extra_data.get("filing_date", "-"),
                e.identifiers.get("cik", "-"),
                e.source_url or "-",
            )
        console.print(table)

    else:
        # Generic table for any future adapter
        table = Table()
        table.add_column("Name", style="cyan")
        table.add_column("Type")
        table.add_column("Source")
        table.add_column("Jurisdiction")

        for e in entities:
            table.add_row(e.name, e.entity_type, e.source, e.jurisdiction or "-")
        console.print(table)

    rprint(f"\n[dim]Showing {len(entities)} of {result.total_results} results.[/dim]")


@records_app.command("fetch")
def records_fetch(
    doc_id: str = typer.Argument(..., help="Document ID to fetch"),
    source: str = typer.Option("documentcloud", "--source", "-s", help="Data source"),
):
    """Fetch a document's full text and save it to the local database.

    Pulls pre-extracted text from the source (no local OCR needed for
    DocumentCloud). The document is immediately ready for entity extraction.

    Examples:
        openfoia records fetch 2090186 --source documentcloud
        openfoia records fetch 2090186
    """
    import asyncio

    if source == "documentcloud":
        from .records.documentcloud import DocumentCloudAdapter

        adapter = DocumentCloudAdapter()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task(f"Fetching document {doc_id} from DocumentCloud...", total=None)
            try:
                result_id, text = asyncio.run(adapter.pull_text(doc_id))
            except Exception as e:
                rprint(f"[red]Fetch failed: {e}[/red]")
                raise typer.Exit(1) from None

        if not result_id or not text:
            rprint(f"[red]Could not fetch text for document {doc_id}.[/red]")
            rprint("[dim]The document may not exist or may not have extracted text.[/dim]")
            raise typer.Exit(1)

        rprint(f"\n[green]Saved to database:[/green] {result_id[:8]}...")
        rprint(f"  Text length: {len(text):,} characters")
        rprint(f"\n[dim]Extract entities: openfoia analyze extract {result_id}[/dim]")
    else:
        rprint(
            f"[yellow]Fetch not yet supported for '{source}'. Use 'records download' for MuckRock.[/yellow]"
        )
        raise typer.Exit(1)


@records_app.command("download")
def records_download(
    request_id: str = typer.Argument(..., help="MuckRock request ID"),
    source: str = typer.Option("muckrock", "--source", "-s", help="Data source"),
    output: Path = typer.Option("./downloads", "--output", "-o", help="Output directory"),
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
    import asyncio

    if source != "muckrock":
        rprint(f"[yellow]Download only supported for muckrock (got '{source}')[/yellow]")
        raise typer.Exit(1)

    from .records.muckrock import MuckRockAdapter

    adapter = MuckRockAdapter()

    # First fetch to show what we're downloading
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(f"Fetching request {request_id}...", total=None)
        try:
            entity = asyncio.run(adapter.fetch(request_id))
        except Exception as e:
            rprint(f"[red]Failed to fetch request: {e}[/red]")
            raise typer.Exit(1) from None

    if not entity:
        rprint(f"[red]Request {request_id} not found on MuckRock.[/red]")
        raise typer.Exit(1)

    files = entity.extra_data.get("files", [])
    rprint(f"\n[bold cyan]{entity.name}[/bold cyan]")
    rprint(f"  Status: {entity.status}")
    rprint(f"  Documents: {len(files)}")

    if not files:
        rprint("[yellow]No response documents attached to this request.[/yellow]")
        return

    rprint(f"\n[cyan]Downloading {len(files)} file(s) to {output}/[/cyan]")

    for f in files:
        url = f.get("url", "")
        filename = url.split("/")[-1] if url else "unknown"
        rprint(f"  {filename}")

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
            rprint(f"[red]Download failed: {e}[/red]")
            raise typer.Exit(1) from None

    rprint(f"\n[green]{len(downloaded)} file(s) downloaded to {output}/[/green]")

    for path in downloaded:
        rprint(f"  {path}")

    # Auto-ingest if requested
    if ingest and downloaded:
        rprint("\n[cyan]Ingesting into OpenFOIA...[/cyan]")
        from .db import get_data_dir, get_db_path, init_db
        from .pipeline.ingest import DocumentIngester

        db_path = get_db_path()
        if not db_path.exists():
            init_db()

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
                    f"  [green]Ingested:[/green] {Path(path).name} → {result.document_id[:8]}..."
                )
            except Exception as e:
                rprint(f"  [red]Failed:[/red] {Path(path).name}: {e}")

        # Persist Document rows to database
        if ingest_results:
            from uuid import uuid4

            from .db import get_session
            from .models import DeliveryMethod, Document, DocumentType, Request, RequestStatus, User

            with get_session() as session:
                # Create a placeholder request for downloaded docs
                user = session.query(User).first()
                if not user:
                    user = User(
                        id=str(uuid4()),
                        email="local@openfoia.local",
                        name="Local User",
                    )
                    session.add(user)
                    session.flush()

                from .models import Agency

                agency = session.query(Agency).first()
                dl_req = Request(
                    id=str(uuid4()),
                    request_number=f"MUCKROCK-{request_id}",
                    requester_id=user.id,
                    agency_id=agency.id if agency else user.id,
                    subject=entity.name if entity else f"MuckRock request {request_id}",
                    body="Downloaded from MuckRock",
                    delivery_method=DeliveryMethod.EMAIL,
                    status=RequestStatus.COMPLETE,
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

        rprint(f"\n[green]{ingested} file(s) ingested.[/green]")
        rprint("[dim]Run 'openfoia analyze extract --all' to extract entities.[/dim]")


# === Cross-Reference Command ===


@app.command()
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
        help="Comma-separated sources (muckrock,opencorporates,sec,opensanctions,documentcloud)",
    ),
    icij_data: Path | None = typer.Option(
        None, "--icij-data", help="Path to downloaded ICIJ CSV data"
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
        openfoia crossref --ftm results.ftm.json    # export as FollowTheMoney
        openfoia crossref --tor                     # hide your IP from the APIs
    """
    from .config import load_config
    from .crossref import crossref_entities
    from .db import get_db_path, get_session
    from .models import Document, Entity
    from .models import Request as RequestModel
    from .net import TorUnavailableError, describe_egress

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    # Gather entities
    with get_session() as session:
        query = session.query(Entity)

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
        from .pipeline.extract import ExtractedEntity

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

    source_list = sources.split(",") if sources else None

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
        if s != "icij"
    ]
    cfg = load_config()
    policy = _egress_policy_from(cfg, tor=tor)

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
        rprint("[dim]  Use --sources icij for offline-only (requires downloaded ICIJ CSVs)[/dim]")
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
            rprint(f"[dim]  {msg}[/dim]")

    try:
        report = asyncio.run(
            crossref_entities(
                entities,
                sources=source_list,
                icij_data_dir=str(icij_data) if icij_data else None,
                on_progress=_progress,
                allow_network=True,  # user was warned and confirmed above
                egress=policy,
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
    rprint("=" * 60)

    for result in report.results:
        if not result.hits:
            continue

        color = (
            "red"
            if any(h.extra.get("is_sanctioned") or h.extra.get("is_pep") for h in result.hits)
            else "yellow"
        )
        rprint(f"\n  [{color}]FLAGGED: {result.entity_name}[/{color}] ({result.entity_type})")

        for hit in result.hits:
            source_color = {
                "muckrock": "cyan",
                "opencorporates": "blue",
                "sec": "green",
                "icij": "red",
                "opensanctions": "magenta",
            }.get(hit.source, "white")

            rprint(
                f"    [{source_color}]{hit.source}[/{source_color}] [{hit.match_type}] {hit.details}"
            )
            if hit.url:
                rprint(f"      {hit.url}")

    if not report.total_flagged:
        rprint("\n  [green]No cross-reference hits found.[/green]")

    # Export as FollowTheMoney
    if ftm:
        from .ftm import export_ftm

        count = export_ftm(entities, [], ftm)
        rprint(f"\n[green]Exported {count} entities to {ftm} (FollowTheMoney format)[/green]")

    # Save report
    if output:
        report_data = {
            "total_entities": report.total_entities,
            "total_hits": report.total_hits,
            "total_flagged": report.total_flagged,
            "sources": report.sources_used,
            "results": [
                {
                    "entity": r.entity_name,
                    "type": r.entity_type,
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
            ],
        }
        output.write_text(json.dumps(report_data, indent=2))
        rprint(f"\n[green]Report saved to {output}[/green]")


# === Export Command ===


@analyze_app.command("export")
def analyze_export(
    output: Path = typer.Option("entities.ftm.json", "--output", "-o", help="Output file path"),
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
    from .db import get_db_path, get_session
    from .ftm import export_ftm
    from .models import Document, Entity, entity_links
    from .models import Request as RequestModel
    from .pipeline.extract import ExtractedEntity

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = session.query(Entity)
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

        # Get relationships
        links = session.query(entity_links).all()
        relationships = [
            {
                "source": lnk.source_id,
                "target": lnk.target_id,
                "relation": lnk.link_type or "related_to",
            }
            for lnk in links
        ]

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
    from .ftm_import import import_ftm_to_db, parse_ftm_file

    if not file.exists():
        rprint(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    # Preview first
    entities, relationships = parse_ftm_file(file)
    rprint(f"\n[cyan]File: {file}[/cyan]")
    rprint(f"  Entities: {len(entities)}")
    rprint(f"  Relationships: {len(relationships)}")

    if not entities:
        rprint("[yellow]No entities found in file.[/yellow]")
        return

    # Show schema breakdown
    from collections import Counter

    schemas = Counter(e["schema"] for e in entities)
    for schema, count in schemas.most_common(10):
        rprint(f"  {schema}: {count}")

    rprint("\n[cyan]Importing...[/cyan]")
    ent_count, rel_count = import_ftm_to_db(file, tag=tag)

    rprint(f"\n[green]Imported {ent_count} entities, {rel_count} relationships[/green]")
    if tag:
        rprint(f"[dim]Tagged as: {tag}[/dim]")
    rprint("[dim]Run 'openfoia crossref' to cross-reference imported entities.[/dim]")
    rprint("[dim]Run 'openfoia analyze graph --view' to visualize.[/dim]")


# === Main Entry Point ===


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
