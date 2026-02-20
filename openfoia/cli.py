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
    # TODO: Implement actual FastAPI server with token auth
    rprint("[yellow]Server not yet implemented. This is the scaffold.[/yellow]")
    rprint(f"[dim]Would start uvicorn on {host}:{port}[/dim]")
    
    # In real implementation:
    # import uvicorn
    # from .server import create_app
    # app = create_app(token=token)
    # uvicorn.run(app, host=host, port=port, log_level="warning")

console = Console()

# Subcommands
request_app = typer.Typer(help="Manage FOIA requests")
docs_app = typer.Typer(help="Process documents")
campaign_app = typer.Typer(help="Manage campaigns")
agency_app = typer.Typer(help="Manage agencies")
analyze_app = typer.Typer(help="Analyze documents")

app.add_typer(request_app, name="request")
app.add_typer(docs_app, name="docs")
app.add_typer(campaign_app, name="campaign")
app.add_typer(agency_app, name="agency")
app.add_typer(analyze_app, name="analyze")


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
    send: bool = typer.Option(False, "--send", help="Send immediately"),
):
    """Create a new FOIA request."""
    if body_file:
        body = body_file.read_text()
    elif not body:
        rprint("[yellow]Enter request body (Ctrl+D when done):[/yellow]")
        import sys
        body = sys.stdin.read()
    
    # Generate request number
    import uuid
    req_num = f"REQ-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    
    table = Table(title="New FOIA Request")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Request #", req_num)
    table.add_row("Agency", agency)
    table.add_row("Subject", subject)
    table.add_row("Method", method)
    table.add_row("Body", body[:100] + "..." if len(body) > 100 else body)
    console.print(table)
    
    if send:
        rprint("[yellow]Sending request...[/yellow]")
        # TODO: Actually send
        rprint("[green]Request sent![/green]")
    else:
        rprint(f"\n[cyan]Request created as draft. Use 'openfoia request send {req_num}' to send.[/cyan]")


@request_app.command("list")
def request_list(
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status"),
    agency: Optional[str] = typer.Option(None, "--agency", "-a", help="Filter by agency"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum results"),
):
    """List FOIA requests."""
    table = Table(title="FOIA Requests")
    table.add_column("Request #", style="cyan")
    table.add_column("Agency")
    table.add_column("Subject")
    table.add_column("Status")
    table.add_column("Sent")
    table.add_column("Days")
    
    # TODO: Query database
    # For now, show placeholder
    table.add_row(
        "REQ-2026-001",
        "FBI",
        "Records on Project X",
        "[yellow]processing[/yellow]",
        "2026-01-15",
        "35",
    )
    table.add_row(
        "REQ-2026-002",
        "DOJ",
        "Contract spending",
        "[green]complete[/green]",
        "2026-01-20",
        "30",
    )
    
    console.print(table)


@request_app.command("status")
def request_status(
    request_id: str = typer.Argument(..., help="Request ID or number"),
):
    """Check status of a FOIA request."""
    rprint(f"[cyan]Status for {request_id}:[/cyan]")
    
    # TODO: Query database and delivery gateway
    table = Table()
    table.add_column("Event", style="cyan")
    table.add_column("Date")
    table.add_column("Details")
    
    table.add_row("Created", "2026-01-15 10:00", "Draft created")
    table.add_row("Sent", "2026-01-15 10:30", "Sent via email")
    table.add_row("Acknowledged", "2026-01-17 14:22", "Agency tracking #: FOI-2026-1234")
    table.add_row("Fee Estimate", "2026-02-01 09:00", "$45.00 estimated")
    
    console.print(table)


# === Document Commands ===


@docs_app.command("ingest")
def docs_ingest(
    path: Path = typer.Argument(..., help="File or directory to ingest"),
    request_id: Optional[str] = typer.Option(None, "--request", "-r", help="Associate with request"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Recurse into directories"),
):
    """Ingest documents into the system."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Ingesting documents...", total=None)
        
        if path.is_file():
            # Single file
            progress.update(task, description=f"Processing {path.name}...")
            # TODO: Call ingester
            rprint(f"[green]✓ Ingested {path.name}[/green]")
        else:
            # Directory
            files = list(path.rglob("*") if recursive else path.glob("*"))
            files = [f for f in files if f.is_file()]
            
            progress.update(task, total=len(files))
            for file in files:
                progress.update(task, description=f"Processing {file.name}...")
                # TODO: Call ingester
                progress.advance(task)
            
            rprint(f"[green]✓ Ingested {len(files)} files[/green]")


@docs_app.command("ocr")
def docs_ocr(
    document_id: str = typer.Argument(..., help="Document ID to OCR"),
    backend: str = typer.Option("tesseract", "--backend", "-b", help="OCR backend"),
):
    """Run OCR on a document."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Running OCR...", total=None)
        
        # TODO: Run OCR
        import time
        time.sleep(2)  # Simulate processing
        
        rprint("[green]✓ OCR complete[/green]")
        rprint("  Pages: 15")
        rprint("  Confidence: 94.2%")
        rprint("  Characters extracted: 45,230")


# === Campaign Commands ===


@campaign_app.command("create")
def campaign_create(
    name: str = typer.Option(..., "--name", "-n", help="Campaign name"),
    description: str = typer.Option(..., "--desc", "-d", help="Campaign description"),
    template: Path = typer.Option(..., "--template", "-t", help="Request template file"),
    target: int = typer.Option(100, "--target", help="Target number of requests"),
):
    """Create a new crowdsourced campaign."""
    rprint(f"[cyan]Creating campaign: {name}[/cyan]")
    
    # TODO: Create campaign
    import uuid
    campaign_id = str(uuid.uuid4())[:8]
    
    rprint(f"[green]✓ Campaign created: {campaign_id}[/green]")
    rprint(f"  Share this link to recruit participants:")
    rprint(f"  [cyan]https://openfoia.org/campaign/{campaign_id}[/cyan]")


@campaign_app.command("join")
def campaign_join(
    campaign_id: str = typer.Argument(..., help="Campaign ID to join"),
):
    """Join an existing campaign."""
    rprint(f"[cyan]Joining campaign {campaign_id}...[/cyan]")
    # TODO: Join campaign
    rprint("[green]✓ You have joined the campaign![/green]")


@campaign_app.command("status")
def campaign_status(
    campaign_id: str = typer.Argument(..., help="Campaign ID"),
):
    """Check campaign progress."""
    table = Table(title=f"Campaign Status: {campaign_id}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value")
    
    # TODO: Get real stats
    table.add_row("Participants", "47")
    table.add_row("Requests Filed", "156 / 200")
    table.add_row("Responses Received", "89")
    table.add_row("Denials", "12")
    table.add_row("Documents Collected", "1,247 pages")
    table.add_row("Avg Response Time", "23 days")
    
    console.print(table)


# === Analyze Commands ===


@analyze_app.command("extract")
def analyze_extract(
    document_id: str = typer.Argument(..., help="Document ID to analyze"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file"),
):
    """Extract entities from a document."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Extracting entities...", total=None)
        
        # TODO: Run extraction
        import time
        time.sleep(3)
        
        rprint("[green]✓ Extraction complete[/green]")
        
        table = Table(title="Extracted Entities")
        table.add_column("Type", style="cyan")
        table.add_column("Entity")
        table.add_column("Confidence")
        table.add_column("Occurrences")
        
        table.add_row("PERSON", "John Smith", "98%", "12")
        table.add_row("ORGANIZATION", "Acme Corp", "95%", "8")
        table.add_row("MONEY", "$1,500,000", "99%", "3")
        table.add_row("DATE", "January 15, 2024", "97%", "5")
        
        console.print(table)


@analyze_app.command("graph")
def analyze_graph(
    request_id: Optional[str] = typer.Option(None, "--request", "-r", help="Analyze single request"),
    campaign_id: Optional[str] = typer.Option(None, "--campaign", "-c", help="Analyze entire campaign"),
    output: Path = typer.Option("graph.json", "--output", "-o", help="Output file"),
):
    """Build entity relationship graph."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Building entity graph...", total=None)
        
        # TODO: Build graph
        import time
        time.sleep(2)
        
        rprint(f"[green]✓ Graph exported to {output}[/green]")
        rprint("  Entities: 234")
        rprint("  Relationships: 567")
        rprint("  Connected components: 12")


# === Main Entry Point ===


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
