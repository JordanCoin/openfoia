"""OpenFOIA command-line interface."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

app = typer.Typer(
    name="openfoia",
    help="Crowdsourced FOIA automation with AI-powered document analysis.",
    no_args_is_help=True,
)


# === Init Command ===


@app.command()
def init(
    force: bool = typer.Option(False, "--force", "-f", help="Re-initialize even if database exists"),
    no_seed: bool = typer.Option(False, "--no-seed", help="Don't seed agency data"),
):
    """Initialize the OpenFOIA database.
    
    Creates ~/.openfoia/ directory and initializes the SQLite database
    with tables and seed data (federal agencies).
    
    Examples:
        openfoia init                # Initialize with agency data
        openfoia init --no-seed      # Initialize without seed data
        openfoia init --force        # Re-initialize (WARNING: loses data)
    """
    from .db import get_data_dir, get_db_path, init_db, seed_agencies, get_engine
    
    data_dir = get_data_dir()
    db_path = get_db_path()
    
    rprint("\n[bold green]🔒 OpenFOIA Initialization[/bold green]")
    rprint("─" * 50)
    
    if db_path.exists() and not force:
        rprint(f"[cyan]Database already exists:[/cyan] {db_path}")
        rprint("[dim]Use --force to re-initialize (WARNING: loses data)[/dim]")
        
        # Show stats
        from .db import get_session
        from .models import Agency, Request, Document
        
        with get_session() as session:
            agency_count = session.query(Agency).count()
            request_count = session.query(Request).count()
            doc_count = session.query(Document).count()
        
        rprint(f"\n[cyan]Current data:[/cyan]")
        rprint(f"  Agencies: {agency_count}")
        rprint(f"  Requests: {request_count}")
        rprint(f"  Documents: {doc_count}")
        return
    
    if force and db_path.exists():
        rprint(f"[yellow]Removing existing database...[/yellow]")
        db_path.unlink()
    
    rprint(f"[cyan]Data directory:[/cyan] {data_dir}")
    rprint(f"[cyan]Database:[/cyan] {db_path}")
    
    # Initialize
    rprint("\n[cyan]Creating tables...[/cyan]")
    init_db(seed=not no_seed)
    
    if not no_seed:
        from .db import get_session
        from .models import Agency
        
        with get_session() as session:
            count = session.query(Agency).count()
        rprint(f"[green]✓ Seeded {count} federal agencies[/green]")
    
    rprint("\n[bold green]✓ Initialization complete![/bold green]")
    rprint("[dim]Run 'openfoia serve' to start the web interface.[/dim]\n")


# === Server Command ===


@app.command()
def serve(
    port: int = typer.Option(0, "--port", "-p", help="Port to run on (0 = random)"),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to"),
    browser: Optional[str] = typer.Option(None, "--browser", "-b", help="Browser to open (safari/firefox/chrome/brave/tor)"),
    private: bool = typer.Option(True, "--private/--no-private", help="Open in private/incognito mode"),
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
    
    from .browser import detect_browsers, launch_browser, print_browser_menu, BrowserType
    
    # Generate session token for security
    token = secrets.token_urlsafe(16)
    
    # Find available port if not specified
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
    
    url = f"http://{host}:{port}/?token={token}"
    
    rprint("\n[bold green]🔒 OpenFOIA[/bold green]")
    rprint("─" * 50)
    rprint(f"[cyan]Local server:[/cyan] {url}")
    rprint(f"[cyan]Data stored:[/cyan]  ~/.openfoia/")
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
                for bt in [BrowserType.BRAVE, BrowserType.FIREFOX, BrowserType.SAFARI, BrowserType.CHROME]:
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
    from .server import run_server
    from .db import get_data_dir

    run_server(host=host, port=port, token=token, data_dir=get_data_dir())

console = Console()

# Subcommands
request_app = typer.Typer(help="Manage FOIA requests")
docs_app = typer.Typer(help="Process documents")
campaign_app = typer.Typer(help="Manage campaigns")
agency_app = typer.Typer(help="Manage agencies")
analyze_app = typer.Typer(help="Analyze documents")
template_app = typer.Typer(help="Request templates")

deadline_app = typer.Typer(help="Track FOIA deadlines")
db_app = typer.Typer(help="Database management")

app.add_typer(request_app, name="request")
app.add_typer(docs_app, name="docs")
app.add_typer(campaign_app, name="campaign")
app.add_typer(agency_app, name="agency")
app.add_typer(analyze_app, name="analyze")
app.add_typer(template_app, name="template")
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

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config()
    alembic_cfg.set_main_option(
        "script_location", str(Path(__file__).parent / "migrations")
    )
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(alembic_cfg, revision)

    rprint("[bold green]Database upgraded successfully.[/bold green]\n")


# === Configuration ===


@app.command()
def config(
    init: bool = typer.Option(False, "--init", help="Initialize configuration"),
    show: bool = typer.Option(False, "--show", help="Show current configuration"),
):
    """Manage OpenFOIA configuration."""
    config_path = Path.home() / ".openfoia" / "config.json"
    
    if init:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        rprint("[bold]OpenFOIA Configuration Setup[/bold]\n")
        
        # Collect configuration
        config_data = {}
        
        # Email settings
        rprint("[cyan]Email Configuration (for sending requests)[/cyan]")
        config_data['email'] = {
            'smtp_host': typer.prompt("SMTP host", default="smtp.gmail.com"),
            'smtp_port': int(typer.prompt("SMTP port", default="587")),
            'smtp_user': typer.prompt("SMTP username (email)"),
            'from_name': typer.prompt("Your name"),
        }
        
        # Optional: Twilio for fax
        if typer.confirm("Configure Twilio for fax sending?", default=False):
            config_data['twilio'] = {
                'account_sid': typer.prompt("Twilio Account SID"),
                'from_number': typer.prompt("Twilio fax number"),
            }
        
        # Optional: Lob for mail
        if typer.confirm("Configure Lob for physical mail?", default=False):
            config_data['lob'] = {
                'return_address': {
                    'name': typer.prompt("Return address name"),
                    'address_line1': typer.prompt("Address line 1"),
                    'address_city': typer.prompt("City"),
                    'address_state': typer.prompt("State (2 letter)"),
                    'address_zip': typer.prompt("ZIP code"),
                },
            }
        
        # AI settings
        rprint("\n[cyan]AI Configuration (for document analysis)[/cyan]")
        ai_provider = typer.prompt("AI provider", default="anthropic")
        config_data['ai'] = {
            'provider': ai_provider,
            'model': typer.prompt("Model", default="claude-sonnet-4-20250514"),
        }
        
        # OCR settings
        rprint("\n[cyan]OCR Configuration[/cyan]")
        config_data['ocr'] = {
            'backend': typer.prompt("OCR backend (tesseract/google/aws)", default="tesseract"),
        }
        
        # Save
        config_path.write_text(json.dumps(config_data, indent=2))
        rprint(f"\n[green]Configuration saved to {config_path}[/green]")
        
    elif show:
        if config_path.exists():
            config_data = json.loads(config_path.read_text())
            rprint(json.dumps(config_data, indent=2))
        else:
            rprint("[yellow]No configuration found. Run 'openfoia config --init' to create one.[/yellow]")
    else:
        rprint("Use --init to create configuration or --show to display it.")


# === Request Commands ===


@request_app.command("new")
def request_new(
    agency: str = typer.Option(..., "--agency", "-a", help="Target agency name or ID"),
    subject: str = typer.Option(..., "--subject", "-s", help="Request subject"),
    body: Optional[str] = typer.Option(None, "--body", "-b", help="Request body (or use --file)"),
    body_file: Optional[Path] = typer.Option(None, "--file", "-f", help="File containing request body"),
    method: str = typer.Option("email", "--method", "-m", help="Delivery method (email/fax/mail)"),
    name: str = typer.Option(..., "--name", "-n", help="Your full name"),
    email_addr: str = typer.Option(..., "--email", "-e", help="Your email address"),
):
    """Create a new FOIA request."""
    from uuid import uuid4
    from .db import get_db_path, get_session, init_db
    from .models import Agency as AgencyModel, Request as RequestModel, User, RequestStatus, DeliveryMethod

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
        found = session.query(AgencyModel).filter(
            (AgencyModel.abbreviation.ilike(agency)) | (AgencyModel.name.ilike(f"%{agency}%"))
        ).first()

        if not found:
            rprint(f"[red]Agency '{agency}' not found. Run 'openfoia agency search {agency}' to search.[/red]")
            raise typer.Exit(1)

        # Get or create user
        user = session.query(User).filter(User.email == email_addr).first()
        if not user:
            user = User(id=str(uuid4()), email=email_addr, name=name)
            session.add(user)
            session.flush()

        # Create request
        req_num = f"REQ-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"

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

    rprint(f"\n[green]Request saved to database.[/green]")
    rprint(f"[cyan]Use 'openfoia request send --agency {agency} --subject \"{subject}\" --name \"{name}\" --email {email_addr}' to send.[/cyan]")


@request_app.command("list")
def request_list(
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status"),
    agency: Optional[str] = typer.Option(None, "--agency", "-a", help="Filter by agency"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum results"),
):
    """List FOIA requests."""
    from .db import get_session, get_db_path
    from .models import Request as RequestModel, Agency as AgencyModel, RequestStatus

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
                raise typer.Exit(1)

        if agency:
            query = query.filter(
                (AgencyModel.abbreviation.ilike(agency)) |
                (AgencyModel.name.ilike(f"%{agency}%"))
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
    from .db import get_session, get_db_path
    from .models import Request as RequestModel, Agency as AgencyModel, TimelineEvent

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        request = session.query(RequestModel).filter(
            (RequestModel.request_number == request_id) | (RequestModel.id == request_id)
        ).first()

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
        info_table.add_row("Delivery Method", request.delivery_method.value.replace("_", " ").title())
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
        events = session.query(TimelineEvent).filter(
            TimelineEvent.request_id == request.id
        ).order_by(TimelineEvent.occurred_at).all()

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
    body: Optional[str] = typer.Option(None, "--body", "-b", help="Request body text"),
    body_file: Optional[Path] = typer.Option(None, "--file", "-f", help="File containing request body"),
    template: Optional[str] = typer.Option(None, "--template", "-t", help="Use template (standard/self)"),
    name: str = typer.Option(..., "--name", "-n", help="Your full name"),
    email: str = typer.Option(..., "--email", "-e", help="Your email address"),
    method: str = typer.Option("email", "--method", "-m", help="Delivery method (email/fax/mail)"),
    to_address: Optional[str] = typer.Option(None, "--to", help="Override recipient address (email, fax number, or mailing address)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent without sending"),
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
    from .models import Agency
    from .gateways.base import DeliveryPayload

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
            found = session.query(Agency).filter(
                (Agency.abbreviation.ilike(agency)) | (Agency.name.ilike(f"%{agency}%"))
            ).first()
            if found:
                agency_name = found.name
                agency_email = found.foia_email
                agency_fax = getattr(found, 'foia_fax', None)
                agency_address = getattr(found, 'foia_address', None)

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
        from .templates import standard_request, records_about_self, RequesterInfo, RequestDetails

        requester = RequesterInfo(name=name, email=email)
        details = RequestDetails(subject=subject, description=subject)

        if template == "standard":
            body = standard_request(requester=requester, agency_name=agency_name, details=details)
        elif template == "self":
            body = records_about_self(requester=requester, agency_name=agency_name, record_type=subject)
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
    config_path = Path.home() / ".openfoia" / "config.json"
    import os
    import json

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            pass

    # Build and send via the appropriate gateway
    if method == "email":
        from .gateways.email import EmailGateway

        smtp_user = os.environ.get('OPENFOIA_SMTP_USER')
        smtp_password = os.environ.get('OPENFOIA_SMTP_PASSWORD')

        if not smtp_user or not smtp_password:
            smtp_config = config.get('email', {})
            smtp_user = smtp_user or smtp_config.get('smtp_user')
            smtp_password = smtp_password or smtp_config.get('smtp_password')

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

        account_sid = os.environ.get('OPENFOIA_TWILIO_ACCOUNT_SID')
        auth_token = os.environ.get('OPENFOIA_TWILIO_AUTH_TOKEN')
        from_number = os.environ.get('OPENFOIA_TWILIO_FROM_NUMBER')

        if not account_sid or not auth_token:
            fax_config = config.get('fax', {})
            account_sid = account_sid or fax_config.get('account_sid') or fax_config.get('_account_sid')
            auth_token = auth_token or fax_config.get('auth_token') or fax_config.get('_auth_token')
            from_number = from_number or fax_config.get('from_number')

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
            media_base_url=os.environ.get('OPENFOIA_MEDIA_BASE_URL'),
        )

    elif method == "mail":
        from .gateways.mail import LobMailGateway

        lob_api_key = os.environ.get('OPENFOIA_LOB_API_KEY')

        if not lob_api_key:
            mail_config = config.get('mail', {})
            lob_api_key = lob_api_key or mail_config.get('api_key') or mail_config.get('_api_key')

        if not lob_api_key:
            rprint("[red]Lob API key not configured for physical mail delivery.[/red]")
            rprint("[dim]Set environment variable: OPENFOIA_LOB_API_KEY[/dim]")
            rprint("[dim]Or run 'openfoia config --init'[/dim]")
            raise typer.Exit(1)

        # Return address from config or environment
        return_addr = config.get('mail', {}).get('return_address', {})
        if not return_addr:
            rprint("[red]Return address not configured for physical mail.[/red]")
            rprint("[dim]Add to ~/.openfoia/config.json under mail.return_address:[/dim]")
            rprint('[dim]  {"name": "...", "address_line1": "...", "address_city": "...", "address_state": "...", "address_zip": "..."}[/dim]')
            raise typer.Exit(1)

        gateway = LobMailGateway(
            api_key=lob_api_key,
            return_address=return_addr,
        )

    rprint(f"\n[cyan]Sending via {method_labels[method]}...[/cyan]")
    result = asyncio.run(gateway.send(payload))

    if result.success:
        rprint(f"[bold green]Request sent![/bold green]")
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

        rprint(f"\n[dim]Save this reference to track your request.[/dim]")
    else:
        rprint(f"[bold red]Failed to send[/bold red]")
        rprint(f"  Error: {result.error_message}")
        raise typer.Exit(1)


# === Document Commands ===


@docs_app.command("ingest")
def docs_ingest(
    path: Path = typer.Argument(..., help="File or directory to ingest"),
    request_id: Optional[str] = typer.Option(None, "--request", "-r", help="Associate with request"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Recurse into directories"),
    ocr: bool = typer.Option(False, "--ocr", help="Run OCR after ingestion"),
    keep_metadata: bool = typer.Option(False, "--keep-metadata", help="Skip automatic metadata stripping"),
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
                result = asyncio.run(ingester.ingest_file(
                    path, request_id=request_id, strip_metadata=not keep_metadata,
                ))
                results.append(result)
                stripped = result.metadata.get("metadata_stripped") or {}
                if stripped.get("stripped"):
                    rprint(f"[green]✓[/green] {path.name} → {result.document_id[:8]}... (stripped {len(stripped['stripped'])} metadata fields)")
                else:
                    rprint(f"[green]✓[/green] {path.name} → {result.document_id[:8]}...")
            except Exception as e:
                rprint(f"[red]✗[/red] {path.name}: {e}")
        else:
            # Directory
            patterns = ['*.pdf', '*.PDF', '*.doc', '*.docx', '*.txt', '*.jpg', '*.png']
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
                    result = asyncio.run(ingester.ingest_file(
                        file, request_id=request_id, strip_metadata=not keep_metadata,
                    ))
                    results.append(result)
                except Exception as e:
                    rprint(f"[red]✗[/red] {file.name}: {e}")
                progress.advance(task)
    
    # Summary
    rprint(f"\n[green]✓ Ingested {len(results)} documents[/green]")
    
    total_pages = sum(r.page_count or 0 for r in results)
    total_size = sum(r.file_size for r in results)
    rprint(f"  Total pages: {total_pages}")
    rprint(f"  Total size: {total_size / 1024 / 1024:.1f} MB")
    rprint(f"  Storage: {storage_path}")
    
    # Run OCR if requested
    if ocr and results:
        rprint("\n[cyan]Running OCR...[/cyan]")
        from .pipeline.ocr import OCREngine
        engine = OCREngine(backend="tesseract")
        
        for result in results:
            if result.mime_type == 'application/pdf':
                rprint(f"  OCR: {result.filename}...")
                try:
                    ocr_result = asyncio.run(engine.process_pdf(result.file_path))
                    rprint(f"    [green]✓[/green] {ocr_result.page_count} pages, {ocr_result.confidence:.1%} confidence")
                except Exception as e:
                    rprint(f"    [red]✗[/red] OCR failed: {e}")


@docs_app.command("ocr")
def docs_ocr(
    file_path: Path = typer.Argument(..., help="PDF file to OCR"),
    backend: str = typer.Option("tesseract", "--backend", "-b", help="OCR backend (tesseract/google/aws)"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output text file"),
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
    
    if file_path.suffix.lower() != '.pdf':
        rprint(f"[yellow]Warning: OCR works best on PDF files[/yellow]")
    
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
            raise typer.Exit(1)
        except Exception as e:
            rprint(f"[red]OCR failed: {e}[/red]")
            raise typer.Exit(1)
        
        progress.update(task, description="Detecting redactions...")
        redactions = asyncio.run(detector.analyze(result.text, file_path))
    
    # Results
    rprint(f"\n[bold green]✓ OCR Complete[/bold green]")
    rprint("─" * 50)
    
    table = Table(show_header=False, box=None)
    table.add_column("Metric", style="cyan", width=20)
    table.add_column("Value")
    
    table.add_row("Pages", str(result.page_count))
    table.add_row("Confidence", f"{result.confidence:.1%}")
    table.add_row("Characters", f"{len(result.text):,}")
    table.add_row("Backend", backend)
    
    if redactions['exemptions_cited']:
        exemptions = ", ".join(e['code'] for e in redactions['exemptions_cited'])
        table.add_row("Exemptions Found", exemptions)
    
    console.print(table)
    
    # Output
    if output:
        output.write_text(result.text)
        rprint(f"\n[green]Text saved to {output}[/green]")
    else:
        rprint("\n[dim]Use --output to save extracted text[/dim]")
    
    # Show redaction details if found
    if redactions['exemptions_cited']:
        rprint("\n[yellow]⚠️  Exemptions cited in document:[/yellow]")
        for ex in redactions['exemptions_cited']:
            rprint(f"  • {ex['code']}: {ex['description']} ({ex['count']}x)")


# === Agency Commands ===


@agency_app.command("list")
def agency_list(
    level: Optional[str] = typer.Option(None, "--level", "-l", help="Filter by level (federal/state/local)"),
    state: Optional[str] = typer.Option(None, "--state", "-s", help="Filter by state (2-letter code)"),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum results"),
):
    """List agencies in the database."""
    from .db import get_session, get_db_path
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
                raise typer.Exit(1)
        
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
    from .db import get_session, get_db_path
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
            .filter(
                (Agency.name.ilike(search_term)) | 
                (Agency.abbreviation.ilike(search_term))
            )
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
    from .db import get_session, get_db_path
    from .models import Agency
    
    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)
    
    with get_session() as session:
        # Try abbreviation first, then name
        agency = (
            session.query(Agency)
            .filter(
                (Agency.abbreviation.ilike(agency_id)) |
                (Agency.name.ilike(f"%{agency_id}%"))
            )
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
            table.add_row("Fee Waiver", agency.fee_waiver_criteria[:100] + "..." if len(agency.fee_waiver_criteria) > 100 else agency.fee_waiver_criteria)
        
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
    organization: Optional[str] = typer.Option(None, "--org", help="Your organization"),
    journalist: bool = typer.Option(False, "--journalist", "-j", help="You are a journalist"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file (default: stdout)"),
    no_fee_waiver: bool = typer.Option(False, "--no-fee-waiver", help="Don't include fee waiver request"),
    expedited: bool = typer.Option(False, "--expedited", help="Request expedited processing"),
):
    """Generate a FOIA request from a template.
    
    Examples:
        openfoia template generate standard -a FBI -s "Records on X" -n "Jane Doe" -e jane@example.com
        openfoia template generate standard -a EPA -s "Pollution data" -n "John Smith" -e john@example.com -j
    """
    from .templates import standard_request, appeal_denial, records_about_self, RequesterInfo, RequestDetails
    
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
            found = session.query(Agency).filter(
                (Agency.abbreviation.ilike(agency)) | (Agency.name.ilike(f"%{agency}%"))
            ).first()
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
        rprint(f"[red]Unknown template '{template_name}'. Use 'openfoia template list' to see options.[/red]")
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
        ("b(1)", "National Security", "Classified information regarding national defense or foreign policy"),
        ("b(2)", "Internal Personnel Rules", "Related solely to internal personnel rules and practices"),
        ("b(3)", "Statutory Exemption", "Specifically exempted by another statute"),
        ("b(4)", "Trade Secrets", "Trade secrets and confidential commercial/financial information"),
        ("b(5)", "Deliberative Process", "Inter/intra-agency memos that are pre-decisional and deliberative"),
        ("b(6)", "Personal Privacy", "Personnel, medical, or similar files where disclosure would invade privacy"),
        ("b(7)(A)", "Law Enforcement - Interference", "Could interfere with enforcement proceedings"),
        ("b(7)(B)", "Law Enforcement - Fair Trial", "Would deprive a person of a fair trial"),
        ("b(7)(C)", "Law Enforcement - Privacy", "Could constitute unwarranted invasion of privacy"),
        ("b(7)(D)", "Law Enforcement - Confidential Source", "Could reveal a confidential source"),
        ("b(7)(E)", "Law Enforcement - Techniques", "Would disclose investigation techniques"),
        ("b(7)(F)", "Law Enforcement - Safety", "Could endanger life or physical safety"),
        ("b(8)", "Financial Institutions", "Examination/operating reports of financial institutions"),
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
    from .db import get_session, get_db_path
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
    from .db import get_session, get_db_path
    from .models import Campaign, RequestStatus

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(
            Campaign.id.like(f"{campaign_id}%")
        ).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{campaign_id}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]")
            raise typer.Exit(1)

        campaign = matches[0]

        requests = campaign.requests
        total = len(requests)
        responded = sum(1 for r in requests if r.status in (
            RequestStatus.PARTIAL_RESPONSE, RequestStatus.COMPLETE, RequestStatus.DENIED,
        ))
        denied = sum(1 for r in requests if r.status == RequestStatus.DENIED)
        docs_count = sum(len(r.documents) for r in requests)
        total_pages = sum(
            d.page_count or 0
            for r in requests
            for d in r.documents
        )

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
    from .db import get_session, get_db_path, init_db
    from .models import Campaign, User

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(
            Campaign.id.like(f"{campaign_id}%")
        ).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{campaign_id}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]")
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
    from .db import get_session, get_db_path
    from .models import (
        Campaign, Agency as AgencyModel, Request as RequestModel,
        RequestStatus, DeliveryMethod, User,
    )

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(
            Campaign.id.like(f"{campaign_id}%")
        ).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{campaign_id}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]")
            raise typer.Exit(1)

        campaign = matches[0]

        if not campaign.is_active:
            rprint(f"[red]Campaign '{campaign.name}' is no longer active.[/red]")
            raise typer.Exit(1)

        participants = list(campaign.participants)
        if not participants:
            rprint(f"[red]No participants in campaign '{campaign.name}'. Use 'openfoia campaign join' first.[/red]")
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

            req_num = f"REQ-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
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
        rprint(f"[dim]Participants can send their requests with 'openfoia request send'.[/dim]")


@campaign_app.command("progress")
def campaign_progress(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)"),
):
    """Show per-participant, per-agency status grid for a campaign."""
    from .db import get_session, get_db_path
    from .models import Campaign, Agency as AgencyModel, Request as RequestModel, User

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        matches = session.query(Campaign).filter(
            Campaign.id.like(f"{campaign_id}%")
        ).all()

        if len(matches) == 0:
            rprint(f"[red]Campaign '{campaign_id}' not found.[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            rprint(f"[red]Ambiguous campaign ID '{campaign_id}' matches {len(matches)} campaigns. Use full ID.[/red]")
            raise typer.Exit(1)

        campaign = matches[0]
        requests = campaign.requests
        participants = list(campaign.participants)

        if not participants:
            rprint(f"[yellow]No participants in campaign '{campaign.name}'.[/yellow]")
            return

        if not requests:
            rprint(f"[yellow]No requests distributed yet. Run 'openfoia campaign distribute {campaign_id}'.[/yellow]")
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
        rprint(f"Participants: {len(participants)} | Agencies: {len(agencies)} | Requests: {len(requests)}")

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

        rprint(f"\n  Sent: {sent_count}/{total} | Complete: {complete_count}/{total} | Denied: {denied_count}/{total}")


# === Analyze Commands ===


@analyze_app.command("extract")
def analyze_extract(
    document_id: str = typer.Argument(..., help="Document ID to analyze"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file"),
    force: bool = typer.Option(False, "--force", help="Re-extract even if already done"),
):
    """Extract entities from a document."""
    from .db import get_session, get_db_path
    from .models import Document, Entity

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        doc = session.query(Document).filter(
            (Document.id == document_id) | (Document.id.like(f"{document_id}%"))
        ).first()

        if not doc:
            rprint(f"[red]Document '{document_id}' not found.[/red]")
            raise typer.Exit(1)

        if not doc.extracted_text:
            rprint("[yellow]Document has no extracted text. Run OCR first:[/yellow]")
            rprint(f"[dim]openfoia docs ocr {doc.file_path}[/dim]")
            raise typer.Exit(1)

        if doc.entities_extracted and not force:
            rprint("[yellow]Entities already extracted for this document. Use --force to re-extract.[/yellow]")
            return

        if doc.entities_extracted and force:
            # Remove existing entities before re-extracting
            session.query(Entity).filter(Entity.document_id == doc.id).delete()

        rprint(f"[cyan]Extracting entities from {doc.filename}...[/cyan]")

        # Run extraction
        import asyncio
        from .pipeline.extract import EntityExtractor

        extractor = EntityExtractor()
        try:
            result = asyncio.run(extractor.extract(doc.extracted_text))
        except Exception as e:
            rprint(f"[red]Extraction failed: {e}[/red]")
            rprint("[dim]Ensure AI provider is configured: openfoia config --init[/dim]")
            raise typer.Exit(1)

        if not result.entities:
            rprint("[yellow]No entities found in document.[/yellow]")
            return

        # Save to database
        from uuid import uuid4
        for ent in result.entities:
            entity = Entity(
                id=str(uuid4()),
                document_id=doc.id,
                entity_type=ent.entity_type,
                raw_text=ent.raw_text,
                normalized_text=ent.normalized_text,
                confidence=ent.confidence,
                context=ent.context,
                page_number=ent.page_number,
            )
            session.add(entity)

        doc.entities_extracted = True

        rprint(f"[green]Extracted {len(result.entities)} entities[/green]")

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
            output.write_text(json_mod.dumps(
                [{"type": e.entity_type.value, "text": e.normalized_text, "confidence": e.confidence} for e in result.entities],
                indent=2,
            ))
            rprint(f"\n[green]Entities saved to {output}[/green]")


@analyze_app.command("graph")
def analyze_graph(
    request_id: Optional[str] = typer.Option(None, "--request", "-r", help="Analyze single request"),
    campaign_id: Optional[str] = typer.Option(None, "--campaign", "-c", help="Analyze entire campaign"),
    output: Path = typer.Option("graph.json", "--output", "-o", help="Output file for JSON"),
    view: bool = typer.Option(False, "--view", "-v", help="Open interactive HTML visualization in browser"),
):
    """Build entity relationship graph from extracted entities.

    Exports graph data as JSON. Use --view to generate and open an interactive
    HTML visualization with nodes colored by entity type.
    """
    from .db import get_session, get_db_path
    from .models import Entity, Document, Request as RequestModel, entity_links

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = session.query(Entity)

        if request_id:
            query = query.join(Document).filter(Document.request_id == request_id)
        elif campaign_id:
            query = query.join(Document).join(RequestModel).filter(
                RequestModel.campaign_id.like(f"{campaign_id}%")
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

        graph_data = {"nodes": nodes, "edges": edges}

        import json as json_mod
        output.write_text(json_mod.dumps(graph_data, indent=2))

        rprint(f"[green]Graph exported to {output}[/green]")
        rprint(f"  Entities: {len(nodes)}")
        rprint(f"  Relationships: {len(edges)}")

        if view:
            html_path = output.with_suffix(".html")
            _generate_graph_html(graph_data, html_path)
            rprint(f"[green]Visualization exported to {html_path}[/green]")

            import webbrowser
            webbrowser.open(f"file://{html_path.resolve()}")
            rprint("[cyan]Opened in browser.[/cyan]")


def _generate_graph_html(graph_data: dict, output_path: Path) -> None:
    """Generate a standalone HTML file with an interactive entity graph visualization.

    Uses a canvas-based d3-force-style simulation. Nodes are colored by entity type,
    edges are labeled with relationship type, and clicking a node shows its
    occurrences across documents.
    """
    import json as json_mod

    graph_json = json_mod.dumps(graph_data)

    # The HTML is a self-contained page with inline JS for the force graph.
    # All entity data from the user's own local database is embedded as JSON.
    # The escapeHtml() function in the JS sanitizes all text before display.
    html_content = _GRAPH_HTML_TEMPLATE.replace("__GRAPH_DATA__", graph_json)
    output_path.write_text(html_content)


_GRAPH_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenFOIA Entity Graph</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #e0e0e0; overflow: hidden; }
  #graph { width: 100vw; height: 100vh; }
  #info-panel {
    position: fixed; top: 16px; right: 16px; width: 340px;
    background: rgba(20, 20, 20, 0.95); border: 1px solid #333;
    border-radius: 8px; padding: 16px; display: none;
    max-height: 80vh; overflow-y: auto; z-index: 10;
  }
  #info-panel h2 { font-size: 16px; margin-bottom: 8px; color: #fff; }
  #info-panel .label { color: #888; font-size: 12px; text-transform: uppercase; margin-top: 10px; }
  #info-panel .value { font-size: 14px; margin-bottom: 4px; }
  #info-panel .occurrence { background: #1a1a2e; border-radius: 4px; padding: 8px; margin: 4px 0; font-size: 13px; }
  #info-panel .close { position: absolute; top: 8px; right: 12px; cursor: pointer; color: #888; font-size: 18px; }
  #legend {
    position: fixed; bottom: 16px; left: 16px;
    background: rgba(20, 20, 20, 0.95); border: 1px solid #333;
    border-radius: 8px; padding: 12px 16px; z-index: 10;
  }
  #legend h3 { font-size: 13px; margin-bottom: 8px; color: #aaa; }
  .legend-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; }
  .legend-dot { width: 12px; height: 12px; border-radius: 50%; }
  #title {
    position: fixed; top: 16px; left: 16px;
    background: rgba(20, 20, 20, 0.95); border: 1px solid #333;
    border-radius: 8px; padding: 12px 16px; z-index: 10;
  }
  #title h1 { font-size: 16px; color: #fff; }
  #title p { font-size: 12px; color: #888; margin-top: 4px; }
</style>
</head>
<body>
<div id="title">
  <h1>OpenFOIA Entity Graph</h1>
  <p id="stats"></p>
</div>
<div id="legend"></div>
<div id="info-panel">
  <span class="close" onclick="document.getElementById('info-panel').style.display='none'">&times;</span>
  <div id="info-content"></div>
</div>
<canvas id="graph"></canvas>

<script>
(function() {
"use strict";
var graphData = __GRAPH_DATA__;

var TYPE_COLORS = {
  person: '#e74c3c',
  organization: '#3498db',
  location: '#2ecc71',
  date: '#f39c12',
  money: '#9b59b6',
  document_id: '#1abc9c',
  phone: '#e67e22',
  email: '#e91e63',
  address: '#00bcd4'
};

// Stats
var nodeCount = graphData.nodes.length;
var edgeCount = graphData.edges.length;
var typesSet = {};
graphData.nodes.forEach(function(n) { typesSet[n.type] = true; });
var types = Object.keys(typesSet).sort();
document.getElementById('stats').textContent = nodeCount + ' entities, ' + edgeCount + ' relationships';

// Legend
var legendEl = document.getElementById('legend');
var legendHTML = '<h3>Entity Types</h3>';
types.forEach(function(t) {
  var color = TYPE_COLORS[t] || '#999';
  legendHTML += '<div class="legend-item"><div class="legend-dot" style="background:' + color + '"></div>' + t + '</div>';
});
legendEl.innerHTML = legendHTML;

// Build occurrence index: normalized_text -> list of occurrences
var occurrenceIndex = {};
graphData.nodes.forEach(function(n) {
  var key = n.label;
  if (!occurrenceIndex[key]) occurrenceIndex[key] = [];
  occurrenceIndex[key].push(n);
});

// Deduplicate nodes by label for cleaner visualization
var uniqueNodes = {};
graphData.nodes.forEach(function(n) {
  if (!uniqueNodes[n.label] || n.confidence > uniqueNodes[n.label].confidence) {
    var existing = uniqueNodes[n.label];
    uniqueNodes[n.label] = {
      id: n.id, label: n.label, type: n.type, confidence: n.confidence,
      count: (existing ? (existing.count || 1) : 0) + 1
    };
  } else {
    uniqueNodes[n.label].count = (uniqueNodes[n.label].count || 1) + 1;
  }
});
var displayNodes = Object.keys(uniqueNodes).map(function(k) { return uniqueNodes[k]; });
var nodeIdToLabel = {};
graphData.nodes.forEach(function(n) { nodeIdToLabel[n.id] = n.label; });

// Canvas setup
var canvas = document.getElementById('graph');
var ctx = canvas.getContext('2d');
var W, H;
function resize() { W = canvas.width = window.innerWidth; H = canvas.height = window.innerHeight; }
resize();
window.addEventListener('resize', resize);

// Physics simulation
var SIM_NODES = displayNodes.map(function(n) {
  return {
    label: n.label, type: n.type, confidence: n.confidence, count: n.count || 1,
    x: W / 2 + (Math.random() - 0.5) * W * 0.6,
    y: H / 2 + (Math.random() - 0.5) * H * 0.6,
    vx: 0, vy: 0,
    radius: Math.min(8 + (n.count || 1) * 2, 24)
  };
});

// Map edges to display nodes
var labelToIdx = {};
SIM_NODES.forEach(function(n, i) { labelToIdx[n.label] = i; });
var SIM_EDGES = [];
graphData.edges.forEach(function(e) {
  var srcLabel = nodeIdToLabel[e.source];
  var tgtLabel = nodeIdToLabel[e.target];
  if (srcLabel && tgtLabel && labelToIdx[srcLabel] !== undefined && labelToIdx[tgtLabel] !== undefined) {
    SIM_EDGES.push({ source: labelToIdx[srcLabel], target: labelToIdx[tgtLabel], type: e.type || '' });
  }
});

// Force simulation constants
var REPULSION = 3000;
var ATTRACTION = 0.005;
var DAMPING = 0.85;
var CENTER_GRAVITY = 0.01;
var LINK_DISTANCE = 120;

function simulate() {
  var i, j, dx, dy, dist, force, fx, fy, a, b;
  // Repulsion
  for (i = 0; i < SIM_NODES.length; i++) {
    for (j = i + 1; j < SIM_NODES.length; j++) {
      dx = SIM_NODES[j].x - SIM_NODES[i].x;
      dy = SIM_NODES[j].y - SIM_NODES[i].y;
      dist = Math.sqrt(dx * dx + dy * dy) || 1;
      force = REPULSION / (dist * dist);
      fx = (dx / dist) * force; fy = (dy / dist) * force;
      SIM_NODES[i].vx -= fx; SIM_NODES[i].vy -= fy;
      SIM_NODES[j].vx += fx; SIM_NODES[j].vy += fy;
    }
  }
  // Attraction along edges
  for (i = 0; i < SIM_EDGES.length; i++) {
    a = SIM_NODES[SIM_EDGES[i].source]; b = SIM_NODES[SIM_EDGES[i].target];
    dx = b.x - a.x; dy = b.y - a.y;
    dist = Math.sqrt(dx * dx + dy * dy) || 1;
    force = (dist - LINK_DISTANCE) * ATTRACTION;
    fx = (dx / dist) * force; fy = (dy / dist) * force;
    a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
  }
  // Center gravity
  for (i = 0; i < SIM_NODES.length; i++) {
    SIM_NODES[i].vx += (W / 2 - SIM_NODES[i].x) * CENTER_GRAVITY;
    SIM_NODES[i].vy += (H / 2 - SIM_NODES[i].y) * CENTER_GRAVITY;
  }
  // Apply velocity
  for (i = 0; i < SIM_NODES.length; i++) {
    var n = SIM_NODES[i];
    n.vx *= DAMPING; n.vy *= DAMPING;
    n.x += n.vx; n.y += n.vy;
    n.x = Math.max(n.radius, Math.min(W - n.radius, n.x));
    n.y = Math.max(n.radius, Math.min(H - n.radius, n.y));
  }
}

// Pan and zoom state
var offsetX = 0, offsetY = 0, scale = 1;
var isPanning = false, lastMX = 0, lastMY = 0;
var dragNode = null;

canvas.addEventListener('wheel', function(e) {
  e.preventDefault();
  var zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
  var mx = e.clientX, my = e.clientY;
  offsetX = mx - (mx - offsetX) * zoomFactor;
  offsetY = my - (my - offsetY) * zoomFactor;
  scale *= zoomFactor;
});

function screenToWorld(sx, sy) {
  return [(sx - offsetX) / scale, (sy - offsetY) / scale];
}

canvas.addEventListener('mousedown', function(e) {
  var wc = screenToWorld(e.clientX, e.clientY);
  var wx = wc[0], wy = wc[1];
  for (var i = SIM_NODES.length - 1; i >= 0; i--) {
    var n = SIM_NODES[i];
    var ddx = wx - n.x, ddy = wy - n.y;
    if (ddx * ddx + ddy * ddy < n.radius * n.radius * 1.5) {
      dragNode = n;
      return;
    }
  }
  isPanning = true;
  lastMX = e.clientX; lastMY = e.clientY;
});

canvas.addEventListener('mousemove', function(e) {
  if (dragNode) {
    var wc = screenToWorld(e.clientX, e.clientY);
    dragNode.x = wc[0]; dragNode.y = wc[1];
    dragNode.vx = 0; dragNode.vy = 0;
  } else if (isPanning) {
    offsetX += e.clientX - lastMX;
    offsetY += e.clientY - lastMY;
    lastMX = e.clientX; lastMY = e.clientY;
  }
});

canvas.addEventListener('mouseup', function() {
  isPanning = false; dragNode = null;
});

canvas.addEventListener('click', function(e) {
  var wc = screenToWorld(e.clientX, e.clientY);
  var wx = wc[0], wy = wc[1];
  for (var i = SIM_NODES.length - 1; i >= 0; i--) {
    var n = SIM_NODES[i];
    var ddx = wx - n.x, ddy = wy - n.y;
    if (ddx * ddx + ddy * ddy < n.radius * n.radius * 1.5) {
      showNodeInfo(n);
      return;
    }
  }
});

function escapeHtml(text) {
  var div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function showNodeInfo(node) {
  var panel = document.getElementById('info-panel');
  var content = document.getElementById('info-content');
  var color = TYPE_COLORS[node.type] || '#999';
  var occurrences = occurrenceIndex[node.label] || [];

  // Build info panel using DOM methods for safety
  content.textContent = '';

  var h2 = document.createElement('h2');
  h2.style.color = color;
  h2.textContent = node.label;
  content.appendChild(h2);

  var typeLabel = document.createElement('div');
  typeLabel.className = 'label';
  typeLabel.textContent = 'Type';
  content.appendChild(typeLabel);
  var typeValue = document.createElement('div');
  typeValue.className = 'value';
  typeValue.textContent = node.type;
  content.appendChild(typeValue);

  var confLabel = document.createElement('div');
  confLabel.className = 'label';
  confLabel.textContent = 'Confidence';
  content.appendChild(confLabel);
  var confValue = document.createElement('div');
  confValue.className = 'value';
  confValue.textContent = (node.confidence * 100).toFixed(0) + '%';
  content.appendChild(confValue);

  var occLabel = document.createElement('div');
  occLabel.className = 'label';
  occLabel.textContent = 'Occurrences (' + occurrences.length + ')';
  content.appendChild(occLabel);

  occurrences.forEach(function(occ) {
    var occDiv = document.createElement('div');
    occDiv.className = 'occurrence';

    var docInfo = document.createElement('div');
    docInfo.style.fontSize = '11px';
    docInfo.style.color = '#888';
    docInfo.textContent = 'Document: ' + occ.document_id.substring(0, 8) + '...';
    occDiv.appendChild(docInfo);

    if (occ.page_number) {
      var pageInfo = document.createElement('div');
      pageInfo.style.fontSize = '11px';
      pageInfo.style.color = '#888';
      pageInfo.textContent = 'Page: ' + occ.page_number;
      occDiv.appendChild(pageInfo);
    }

    if (occ.context) {
      var ctxInfo = document.createElement('div');
      ctxInfo.style.marginTop = '4px';
      ctxInfo.style.fontSize = '12px';
      ctxInfo.style.color = '#ccc';
      ctxInfo.textContent = '"' + occ.context.substring(0, 200) + '"';
      occDiv.appendChild(ctxInfo);
    }

    content.appendChild(occDiv);
  });

  panel.style.display = 'block';
}

function draw() {
  simulate();
  ctx.clearRect(0, 0, W, H);
  ctx.save();
  ctx.translate(offsetX, offsetY);
  ctx.scale(scale, scale);

  // Draw edges
  ctx.lineWidth = 1;
  var i, e, a, b, mx, my, n, color;
  for (i = 0; i < SIM_EDGES.length; i++) {
    e = SIM_EDGES[i];
    a = SIM_NODES[e.source]; b = SIM_NODES[e.target];
    ctx.strokeStyle = 'rgba(255,255,255,0.15)';
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    if (e.type) {
      mx = (a.x + b.x) / 2; my = (a.y + b.y) / 2;
      ctx.fillStyle = 'rgba(255,255,255,0.35)';
      ctx.font = '9px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(e.type, mx, my - 3);
    }
  }

  // Draw nodes
  for (i = 0; i < SIM_NODES.length; i++) {
    n = SIM_NODES[i];
    color = TYPE_COLORS[n.type] || '#999';
    ctx.beginPath();
    ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.85;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = 'rgba(255,255,255,0.3)';
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = '#fff';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    var lbl = n.label.length > 20 ? n.label.substring(0, 18) + '...' : n.label;
    ctx.fillText(lbl, n.x, n.y + n.radius + 14);
  }

  ctx.restore();
  requestAnimationFrame(draw);
}

draw();
})();
</script>
</body>
</html>"""


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
    from .db import get_session, get_db_path
    from .models import Request as RequestModel, Agency as AgencyModel, RequestStatus

    db_path = get_db_path()
    if not db_path.exists():
        rprint("[yellow]Database not initialized. Run 'openfoia init' first.[/yellow]")
        raise typer.Exit(1)

    with get_session() as session:
        query = session.query(RequestModel).join(AgencyModel)

        if not show_all:
            query = query.filter(RequestModel.status.in_([
                RequestStatus.SENT,
                RequestStatus.ACKNOWLEDGED,
                RequestStatus.PROCESSING,
                RequestStatus.FEE_ESTIMATE,
            ]))

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
                days_over = (datetime.utcnow() - r.due_date).days
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
                days_left = (r.due_date - datetime.utcnow()).days
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
    from .db import get_session, get_db_path
    from .models import Request as RequestModel, RequestStatus

    db_path = get_db_path()
    if not db_path.exists():
        return

    with get_session() as session:
        requests = session.query(RequestModel).filter(
            RequestModel.status.in_([
                RequestStatus.SENT,
                RequestStatus.ACKNOWLEDGED,
                RequestStatus.PROCESSING,
            ]),
            RequestModel.sent_at.isnot(None),
        ).all()

        overdue_count = 0
        for r in requests:
            if not r.due_date and r.sent_at:
                r.due_date = _foia_due_date(r.sent_at)
            if r.is_overdue():
                overdue_count += 1
                days_over = (datetime.utcnow() - r.due_date).days
                rprint(f"[red]OVERDUE:[/red] {r.request_number} — {r.subject} (+{days_over} days)")

        if overdue_count:
            rprint(f"\n[red]{overdue_count} overdue request(s). Run 'openfoia deadlines list' for details.[/red]")
            raise typer.Exit(1)


# === Purge Command ===


@app.command()
def purge(
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    secure: bool = typer.Option(False, "--secure", "-s", help="Overwrite files 3x with random data before deletion (forensic)"),
    fill: bool = typer.Option(False, "--fill", help="Fill free disk space with random data after purge (slow)"),
):
    """Destroy all OpenFOIA data. Everything. Gone.

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
        print_ssd_warning,
        secure_delete_dir,
    )

    data_dir = Path.home() / ".openfoia"

    if not data_dir.exists():
        rprint("[dim]Nothing to purge. No data directory found.[/dim]")
        return

    if fill and not secure:
        rprint("[red]--fill requires --secure.[/red]")
        raise typer.Exit(1)

    if not confirm:
        rprint("\n[bold red]This will permanently destroy:[/bold red]")
        rprint(f"  [red]{data_dir}/data.db[/red]     — all requests, entities, tracking")
        rprint(f"  [red]{data_dir}/docs/[/red]       — all ingested documents")
        rprint(f"  [red]{data_dir}/exports/[/red]    — all generated reports")
        rprint(f"  [red]{data_dir}/config.json[/red] — your configuration")
        rprint(f"\n  [bold red]Everything in {data_dir}[/bold red]")

        if secure:
            rprint("\n[yellow]Secure mode: files will be overwritten 3x with random data.[/yellow]")
            print_ssd_warning()
        if fill:
            rprint("[yellow]Fill mode: free disk space will be overwritten. This may take a long time.[/yellow]")

        rprint("\n[dim]This cannot be undone.[/dim]\n")

        answer = typer.prompt("Type PURGE to confirm", default="")
        if answer != "PURGE":
            rprint("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    if secure:
        print_ssd_warning()
        rprint("[bold]Secure-deleting all files (3-pass overwrite)...[/bold]")
        count = secure_delete_dir(data_dir)
        rprint(f"[green]{count} file(s) securely destroyed.[/green]")

        rprint("[bold]Scrubbing shell history...[/bold]")
        modified = clear_shell_history()
        if modified:
            rprint(f"[green]Cleaned {len(modified)} history file(s): {', '.join(modified)}[/green]")
        else:
            rprint("[dim]No shell history entries found.[/dim]")

        if fill:
            rprint("[bold]Filling free disk space with random data (this will take a while)...[/bold]")
            fill_dir = data_dir.parent / ".openfoia_fill"
            fill_free_space(fill_dir)
            rprint("[green]Free space filled and cleaned up.[/green]")
    else:
        shutil.rmtree(data_dir)

    rprint(f"\n[green]{data_dir} destroyed.[/green]")
    rprint("[dim]All OpenFOIA data has been removed from this machine.[/dim]\n")


# === Main Entry Point ===


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
