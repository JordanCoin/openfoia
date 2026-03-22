# OpenFOIA

**Local-first FOIA automation for journalists, researchers, and citizens.**

Your data never leaves your machine. Works offline. Works everywhere.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/install.sh | bash
```

That's it. Works on Linux, macOS, and Windows (WSL). Installs the CLI and a fast PDF text extraction engine.

## What It Does

OpenFOIA is a self-hosted toolkit for filing and tracking Freedom of Information Act requests. No accounts, no cloud, no third parties touching your investigation.

- **File requests** via email, fax, or physical mail
- **Track deadlines** with auto-calculated due dates (20 business days per statute)
- **Extract text** from response PDFs (direct extraction + OCR fallback)
- **Find entities** — people, organizations, money, dates — using AI or regex
- **Build relationship graphs** across documents
- **Coordinate campaigns** — 100 people FOIA the same thing

A human can type a CLI command. An AI agent can call the same tool interface. It runs on Linux, Mac, Windows, or the web. Completely local, completely offline.

## Quick Start

```bash
# Initialize database (53 federal agencies pre-loaded)
openfoia init

# Start the web interface (opens in your browser, private mode)
openfoia serve

# File a request
openfoia request new --agency FBI --subject "Records on X" \
  --body "I request all records..." --name "Jane Doe" --email jane@example.com

# Send it
openfoia request send --agency FBI --subject "Records on X" \
  --name "Jane Doe" --email jane@example.com

# Check deadlines
openfoia deadlines list

# Ingest a response PDF and extract entities
openfoia docs ingest ./response.pdf --request REQ-20260322-A1B2C3
openfoia analyze extract <document-id>

# Build a relationship graph and view it
openfoia analyze graph --view
```

## Features

| Feature | How |
|---------|-----|
| **53 federal agencies** | Pre-loaded with FOIA emails, fax numbers, addresses, portals |
| **Request templates** | Standard, appeal, and records-about-self templates with proven legal language |
| **Email gateway** | SMTP or SendGrid |
| **Fax gateway** | Twilio — auto-generates PDF, sends to agency fax number |
| **Mail gateway** | Lob — formats as certified letter with return envelope |
| **PDF text extraction** | Compiled binary, ~3ms/page, lossless on born-digital PDFs |
| **OCR fallback** | Tesseract (local), Google Cloud Vision, or AWS Textract for scanned docs |
| **Entity extraction** | Local LLM (Ollama), Anthropic, or OpenAI — falls back to regex with zero config |
| **Deadline tracking** | Auto-calculates due dates, CLI checker for cron/bashrc |
| **Campaign coordination** | Create campaigns, distribute requests to participants, track progress |
| **Entity graph** | Force-directed HTML visualization, colored by type, clickable nodes |
| **Web UI** | Local htmx interface with agency search, form submission, document upload |
| **Alembic migrations** | Schema versioning for safe upgrades |
| **Purge command** | `openfoia purge --yes` — everything gone, instantly |

## Configuration

```bash
openfoia config --init    # Interactive setup
# Or copy the example:
cp config.example.json ~/.openfoia/config.json
```

### AI Provider (Entity Extraction)

**Local models (recommended for privacy):**

```json
{
  "ai": {
    "provider": "ollama",
    "ollama": {
      "base_url": "http://localhost:11434",
      "model": "llama3.2"
    }
  }
}
```

No AI configured? Entity extraction falls back to regex — catches dates, money, emails, phone numbers, FOIA tracking numbers out of the box.

Cloud APIs if you want them:

```bash
export OPENFOIA_ANTHROPIC_API_KEY="sk-ant-..."
export OPENFOIA_OPENAI_API_KEY="sk-..."
```

### Delivery Gateways

Only needed if you want to **send** requests (vs just tracking):

| Gateway | Provider | Cost | Config |
|---------|----------|------|--------|
| **Email** | SMTP | Free | `openfoia config --init` |
| **Fax** | Twilio | $0.07/page | `OPENFOIA_TWILIO_ACCOUNT_SID` + `OPENFOIA_TWILIO_AUTH_TOKEN` |
| **Mail** | Lob | ~$1/letter | `OPENFOIA_LOB_API_KEY` |

### Custom Entity Types

Add domain-specific entities for your investigation:

```json
{
  "entities": {
    "custom_types": [
      {
        "name": "CONTRACT_NUMBER",
        "pattern": "\\b[A-Z]{2,4}-\\d{4,}-\\d{4,}\\b",
        "description": "Federal contract numbers"
      }
    ]
  }
}
```

## CLI Reference

```bash
# Server
openfoia serve                    # Start web interface
openfoia serve --tor              # Open in Tor Browser
openfoia serve --no-browser       # Just print URL

# Requests
openfoia request new              # Draft a FOIA request
openfoia request send             # Send via email/fax/mail
openfoia request list             # List all requests
openfoia request status REQ-001   # Check status

# Deadlines
openfoia deadlines list           # Show overdue + upcoming
openfoia deadlines check          # One-liner for cron (exits 1 if overdue)

# Documents
openfoia docs ingest ./folder/    # Import documents
openfoia docs ocr DOC-001         # Run OCR on a document

# Analysis
openfoia analyze extract DOC-001  # Extract entities
openfoia analyze graph            # Build relationship graph
openfoia analyze graph --view     # Open interactive visualization

# Campaigns
openfoia campaign create          # Create a crowdsourced campaign
openfoia campaign join <id>       # Join as participant
openfoia campaign distribute <id> # Generate requests for all participants
openfoia campaign progress <id>   # View per-participant status grid

# Database
openfoia db upgrade               # Run migrations

# Danger zone
openfoia purge                    # Destroy all data (asks for confirmation)
openfoia purge --yes              # No confirmation. Everything gone.
```

## Privacy

| | OpenFOIA | Cloud Services |
|--|----------|----------------|
| Data storage | Your machine | Their servers |
| Who sees your requests | Only you | The service provider |
| Works offline | Yes | No |
| Open source | Yes (AGPL) | Rarely |
| Cost | Free | Paid tiers |

Everything runs locally. The server binds to `127.0.0.1` only. A random session token prevents other local apps from accessing your data.

**For sensitive investigations:** `openfoia serve --tor`

## Architecture

```
~/.openfoia/
├── data.db          # SQLite (requests, entities, campaigns, timeline)
├── docs/            # Ingested documents
├── exports/         # Reports, graph exports
└── config.json      # Your settings
```

The CLI and the AI agent interface share the same tool layer. A human types `openfoia request new`. An agent calls `execute_tool("draft_request", {...})`. Same database, same logic, same privacy guarantees.

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/uninstall.sh | bash
```

Or just: `openfoia purge --yes && pip uninstall openfoia`

## Why Open Source?

FOIA exists to make government transparent. The tools we use to exercise that right should be transparent too.

Licensed under **AGPL-3.0** — if you modify and deploy it, you must share your changes. Transparency all the way down.

## Credits

Built by people who believe in freedom of information — not journalists ourselves, just folks who want to support them with free tools.

Inspired by [MuckRock](https://www.muckrock.com/), [DocumentCloud](https://www.documentcloud.org/), and the [Reporters Committee for Freedom of the Press](https://www.rcfp.org/).

*Free as in freedom, free as in beer.*

## License

AGPL-3.0 — Keep it open.

---

*"Democracy dies in darkness. FOIA is how we turn on the lights."*
