# 🔒 OpenFOIA

**Crowdsourced FOIA automation with AI-powered document analysis.**

Your data never leaves your machine. Transparency is patriotic. 🇺🇸

> ⚠️ **v0.1.0 — Early Development**
> 
> This is a working scaffold. The CLI structure exists, the web UI renders, but most features are stubs. PRs welcome!

---

## What Is This?

OpenFOIA is a **local-first, privacy-focused** toolkit for filing and tracking Freedom of Information Act requests. It's designed for journalists, researchers, and citizens who want to hold government accountable—without trusting a third-party service with their sensitive investigations.

**The problem:** FOIA is powerful but painful. Agencies delay, deny, and obfuscate. Existing tools are either proprietary SaaS (your requests go through them) or manual spreadsheets.

**The solution:** A self-hosted tool that will:
- File requests via fax, mail, or email
- Track deadlines and auto-remind you to follow up
- OCR scanned response PDFs
- Extract entities (people, orgs, money, dates) using AI
- Build relationship graphs across documents
- Coordinate crowdsourced campaigns (100 people FOIA the same thing)

## Current Status

| Feature | Status |
|---------|--------|
| CLI structure | ✅ Working |
| Web UI shell | ✅ Working (htmx + Tailwind) |
| Request drafting | 🚧 Scaffold |
| Email/fax/mail sending | 🚧 Scaffold |
| Document ingestion | 🚧 Scaffold |
| OCR pipeline | 🚧 Scaffold |
| Entity extraction | 🚧 Scaffold |
| SQLite database | 🚧 Schema only |
| Campaign coordination | 🚧 Scaffold |

## Quick Start

```bash
# Clone the repo
git clone https://github.com/JordanCoin/openfoia.git
cd openfoia

# Install in development mode
pip install -e .

# Start local server
openfoia serve

# Opens in your browser (private mode by default)
# All data stays in ~/.openfoia/
```

> **Note:** Not on PyPI yet. Install from source for now.

## Configuration

Copy the example config and customize:

```bash
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

Or use cloud APIs:

```bash
export OPENFOIA_ANTHROPIC_API_KEY="sk-ant-..."
export OPENFOIA_OPENAI_API_KEY="sk-..."
```

### Gateway Adapters (Optional)

Only needed if you want to **send** requests (vs just tracking):

| Gateway | Provider | Cost | Config Key |
|---------|----------|------|------------|
| **Fax** | Twilio | $0.07/page | `OPENFOIA_TWILIO_ACCOUNT_SID` |
| **Mail** | Lob | ~$1/letter | `OPENFOIA_LOB_API_KEY` |
| **Email** | SMTP | Free | `gateways.email` in config |

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
      },
      {
        "name": "CASE_NUMBER",
        "pattern": "\\b\\d{2}-cv-\\d{4,}\\b",
        "description": "Federal court case numbers"
      }
    ]
  }
}
```

## Privacy First

| Feature | OpenFOIA | Cloud Services |
|---------|----------|----------------|
| Data storage | Your machine | Their servers |
| Who sees your requests | Only you | The service provider |
| Works offline | Yes | No |
| Open source | Yes (AGPL) | Rarely |
| Cost | Free | Paid tiers |

**For sensitive investigations:** Use `openfoia serve --tor` to open in Tor Browser or Brave with Tor.

## Planned Features

### 📝 Request Management
- Pre-loaded agency database (federal + state FOIA contacts)
- Smart request templates that actually work
- Automatic deadline tracking
- Fee waiver request generation
- Appeal templates for denials

### 📄 Document Processing
- Drag-and-drop PDF ingestion
- OCR for scanned documents (Tesseract, or cloud APIs)
- Automatic redaction detection
- FOIA exemption identification (b(6), b(7)(A), etc.)

### 🔍 Entity Extraction
- AI-powered entity recognition (people, orgs, dates, money)
- Cross-document linking
- Relationship graph visualization
- Evidence chain building with source citations

### 👥 Crowdsourced Campaigns
- Coordinate multiple requesters
- Template distribution with variations
- Staggered sending to avoid pattern detection
- Aggregate response tracking

## CLI Commands

```bash
# Server
openfoia serve                    # Start web interface
openfoia serve --tor              # Open in Tor Browser
openfoia serve --no-browser       # Just print URL

# Requests (scaffold)
openfoia request new --agency "FBI" --subject "Records on X"
openfoia request list
openfoia request status REQ-001

# Documents (scaffold)
openfoia docs ingest ./folder/
openfoia docs ocr DOC-001

# Analysis (scaffold)
openfoia analyze extract DOC-001
openfoia analyze graph

# Campaigns (scaffold)
openfoia campaign create --name "Project X" --template ./req.txt
openfoia campaign join ABC123
openfoia campaign status ABC123

# Configuration
openfoia config --init            # Interactive setup
openfoia config --show            # Show current config
```

## Browser Support

OpenFOIA auto-detects installed browsers and prefers privacy-focused options:

1. **Brave** (recommended - has built-in Tor)
2. **Firefox** (private window)
3. **Safari** (private window, no extension risk)
4. **Tor Browser** (maximum privacy)
5. **Chrome** (incognito, if nothing else)

```bash
openfoia serve --browser firefox  # Force specific browser
openfoia serve --private          # Incognito mode (default)
openfoia serve --tor              # Tor mode
```

## Architecture

```
~/.openfoia/
├── data.db          # SQLite database (requests, entities, etc.)
├── docs/            # Ingested documents
├── exports/         # Generated reports
└── config.json      # Your settings
```

Everything runs locally. The server binds to `127.0.0.1` only. A random session token prevents other local apps from accessing your data.

## Why Open Source?

FOIA exists to make government transparent. The tools we use to exercise that right should be transparent too.

This is licensed under **AGPL-3.0** — if you modify and deploy it, you must share your changes. Transparency all the way down.

## Contributing

This is early development — PRs welcome for:
- [ ] Agency database (federal + state contacts)
- [ ] Request templates that work
- [ ] OCR pipeline implementation
- [ ] Entity extraction with local models
- [ ] Web UI improvements
- [ ] Documentation
- [ ] Tests

See the code — most features are marked `# TODO`. Pick one and build it.

## Credits

Built by people who love journalists and believe in freedom of information — not journalists ourselves, just folks who want to support them with free tech.

Inspired by the work of [MuckRock](https://www.muckrock.com/), [DocumentCloud](https://www.documentcloud.org/), and the [Reporters Committee for Freedom of the Press](https://www.rcfp.org/).

*Free as in freedom, free as in beer.* 🍺

## License

AGPL-3.0 — Keep it open.

---

*"Democracy dies in darkness. FOIA is how we turn on the lights."*
