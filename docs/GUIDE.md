# OpenFOIA Guide

A practical guide for journalists, researchers, and anyone investigating power.

No programming experience needed. If you can use a terminal, you can use this.

---

## Where your data lives

Everything is in one folder:

```
~/.openfoia/
├── data.db       # Your database (requests, entities, relationships)
├── docs/         # Ingested documents (PDFs, DOCX, etc.)
├── graphs/       # Saved investigation graphs
├── exports/      # Reports
└── config.json   # Your settings
```

When you uninstall or purge, this folder is all that gets removed. Nothing else on your machine is touched.

### Portable mode (USB stick)

If you're running from a USB drive and don't want ANY data on the host machine:

```bash
cd /Volumes/MY_USB    # or wherever your drive is mounted
openfoia portable     # creates a marker file
openfoia init         # database created ON the USB
```

From that point, everything stays on the USB. Unplug it and there's no trace on the host.

---

## Starting an investigation

### Keep it together: cases

A case groups the searches you run, the documents you fetch and the graph you
build for one investigation. Open one first; while it is active, `records
search` logs each query to it, `records fetch` / `records download --ingest`
attach their documents to it, and `analyze graph` only graphs its documents.

```bash
openfoia case new "CIA PAS" --desc "PAS program records"   # create + make active
openfoia records search "PAS" -s documentcloud -n 200 --json
openfoia records fetch 2090186
openfoia case show                       # documents, entities, queries, last activity
openfoia case export -o pas.md           # document list + query log (md or --format json)
openfoia case list                       # * marks the active case
openfoia case use "other case"           # switch
openfoia case close                      # no active case; nothing is deleted
```

The active case is remembered by id only (`active_case.json` in your data
directory); its name stays inside the database. `case export` writes plaintext
outside the database, like graph exports.

### Option A: File a new FOIA request

```bash
# Search for the right agency
openfoia records search "FBI" --source muckrock

# Draft a request
openfoia request new \
  --agency FBI \
  --subject "Records on Project X" \
  --body "I request all records pertaining to..." \
  --name "Your Name" \
  --email you@email.com

# Send it
openfoia request send \
  --agency FBI \
  --subject "Records on Project X" \
  --name "Your Name" \
  --email you@email.com

# Track deadlines (agencies have 20 business days to respond)
openfoia deadlines list
```

### Option B: Analyze existing documents

Got a stack of PDFs from a FOIA response, a leak, or a document dump?

```bash
# Ingest a folder of documents
openfoia docs ingest ./my-documents/

# Or a single file
openfoia docs ingest ./response.pdf
```

Documents are copied to `~/.openfoia/docs/`, metadata is stripped automatically (EXIF, PDF author, DOCX revision history), and they're ready for analysis.

**How a PDF's text is read.** The `pdf-extract` binary reads each page's
text layer and gives every page a verdict: `text_layer` (readable),
`garbled` (fonts that did not decode cleanly) or `image_only` (a scan with
no text). Only `garbled` and `image_only` pages are sent to tesseract OCR,
one page at a time; readable pages keep the extracted text. OCR'd pages are
stored with verdict `ocr:<original>`. If tesseract is not installed those
pages keep whatever text was extracted and you get one hint to run
`openfoia install-extras ocr`. `docs list` and `docs show` print the per-page
verdict counts. `docs ingest --ocr` still OCRs every page regardless of
verdict. Everything here runs locally; nothing is sent anywhere.

### Option C: Search public databases

```bash
# Search 46k+ completed FOIA requests on MuckRock
openfoia records search "EPA water contamination" --source muckrock

# Download response documents from a specific request
openfoia records download 68490 --ingest

# Search company ownership worldwide
openfoia records search "Shell Corp LLC" --source opencorporates

# Search US corporate filings
openfoia records search "Acme Corp" --source sec

# Search the CIA FOIA Electronic Reading Room (declassified CREST/MORI records)
openfoia records search "annual report" --source cia

# Fetch one by CREST number, MORI number, ESDN or reading room URL:
# downloads the PDF, extracts text or OCRs it, attaches it to the active case
openfoia records fetch CIA-RDP96-00789R002200230001-2 --source cia
openfoia records fetch 144765 --source cia

# Ask every source at once; each reports ok N / error / skipped
openfoia records search "Acme Corp" --all-sources
```

Every source call prints which third party it contacts before the request
leaves. Current limitation: cia.gov's bot filter blocks non-browser clients,
so `--source cia` search and fetch report "blocked" today rather than
pretending there were no results. Workaround: open the printed URL in a
browser, download the PDF, and `openfoia docs ingest <file.pdf>`. A Playwright
route through `tor_browse.py` is a possible follow-up.

A source that fails (rate limit, timeout, missing token) is reported as an
error and the command exits non-zero; "No results found" means the source
answered and had nothing. MuckRock's API now needs a token: set
`OPENFOIA_MUCKROCK_TOKEN`.

### From hits to citations

A search is only the start. The loop below turns a pile of hits into findings
you can paste into a report, with every page cited.

```bash
openfoia case new "CIA PAS"

# 1. Search, rank by the words that matter, keep the result list
openfoia records search "PAS" -s documentcloud -n 300 --json --rank child,school,gifted > hits.json

# 2. Fetch every hit into the case (already-stored ids are skipped)
openfoia records fetch --from hits.json

# 3. Read offline: grep every stored page, save the hits you care about
openfoia docs grep '"Hocking Technical College"' --save
openfoia docs grep 'WISC OR WAIS' --note "IQ tests named, no ages given"

# 4. Write findings by hand, pinned to a document and page
openfoia case note "SRI tested the PAS on college volunteers" \
    --doc 3f2a --page 51 --quote "students at Hocking Technical College" --tier says
openfoia case note "The 1952 Denver volunteers may have been minors" --tier speculation

# 5. Which documents mention both names, and how are they linked?
openfoia analyze graph --name pas
openfoia analyze graph --mentions "Gittinger" "Zimbardo" --name pas

# 6. Export: Findings with citations, then Sources, then the query log
openfoia case notes
openfoia case export -o pas.md
```

Tiers keep you honest: `says` means the document states it, `infer` means it
follows from the document, `speculation` is a lead. A page marked `*` comes
from a document with fewer stored pages than the source has, so the number is
not the source's page number; re-fetch it with `records fetch <id> --refresh`
to get per-page text. CREST numbers (`CIA-RDP...`) are picked out of the
document title or text when present. The export is plaintext outside the
encrypted database.

---

## Extracting entities

Once you have documents ingested, extract the people, organizations, money, and dates:

```bash
openfoia analyze extract <document-id>
```

The extractor tries four methods in order:
1. **Local LLM** (if ollama is running) — best quality, finds relationships too
2. **GLiNER** (if installed: `pip install gliner`) — great quality, zero config
3. **spaCy** (if installed: `pip install spacy`) — good for people and orgs
4. **Regex** — always works, catches dates, money, emails, phones

For the best results with no cloud dependency:

```bash
# Install ollama (one time)
# https://ollama.ai
ollama pull llama3.2:3b    # 2GB model, runs on any laptop

# Now extraction uses the local LLM automatically
openfoia analyze extract <document-id>
```

---

## Building investigation graphs

```bash
# Graph the active case (or everything when no case is active)
openfoia analyze graph --view

# Graph one case, or everything regardless of the active case
openfoia analyze graph --case "CIA PAS" --view
openfoia analyze graph --all --view

# Graph a specific investigation and save it
openfoia analyze graph --name defense-contracts --view

# Graph entities from one request only
openfoia analyze graph --request REQ-20260322-ABC --name epa-water --view

# List your saved investigations
openfoia analyze graphs
```

Graphs are interactive HTML files. Nodes are colored by type (red = person, blue = organization, green = location, orange = money). Click a node to see its connections.

---

## Cross-referencing

This is where it gets powerful. One command checks every person and organization in your database against multiple public databases:

```bash
openfoia crossref
```

Sources checked:
- **MuckRock** — are there other FOIA requests about this person/org?
- **OpenCorporates** — do they own companies? Where are they registered?
- **SEC EDGAR** — are they a public company? What have they filed?
- **OpenSanctions** — are they on a sanctions list? Politically exposed?
- **ICIJ Offshore Leaks** — do they appear in the Panama/Pandora Papers? (requires downloaded CSV data)

To include ICIJ Offshore Leaks data (fully offline):
1. Download CSVs from https://offshoreleaks.icij.org/pages/database
2. Extract to a folder
3. `openfoia crossref --icij-data ./icij-csvs/`

---

## Working with other tools

### Export (share your data)

```bash
# Export as FollowTheMoney JSON-lines
# Compatible with Aleph, OpenAleph, OpenSanctions, ICIJ tools
openfoia analyze export -o investigation.ftm.json
```

### Import (bring in external data)

```bash
# Import from Aleph, OpenSanctions, or any FtM-compatible tool
openfoia analyze import colleague-data.ftm.json --tag aleph-export

# Imported entities are immediately crossref-able and graphable
openfoia crossref
openfoia analyze graph --view
```

---

## Custom entity types

If your investigation involves specific patterns (contract numbers, grant IDs, case numbers), teach OpenFOIA to find them:

```bash
# Add one at a time
openfoia entities add \
  -n CONTRACT_NUMBER \
  -p '\b[A-Z]{2,4}-\d{4,}-\d{4,}\b' \
  -d "Federal contract numbers"

# Import from a spreadsheet (columns can be named anything)
openfoia entities import patterns.csv

# Test your patterns against a document
openfoia entities test -f document.txt

# List what's configured
openfoia entities list
```

If you have ollama running, the CSV import is smart — it'll figure out messy column names and generate regex from plain English descriptions like "looks like GR-2024-00456."

---

## Staying safe

### Encrypt your database

```bash
openfoia init --password <your-secret>
# or encrypt an existing database:
openfoia db encrypt --password <your-secret>
```

Requires SQLCipher: `pip install openfoia[encryption]`

### Duress mode

If you're forced to unlock your device, a second password opens a decoy database with harmless-looking data:

```bash
openfoia init --password real-secret --duress-password fake-secret
```

The real password opens your investigation. The duress password opens weather data and meeting minutes.

### Destroy everything

```bash
# Fast delete
openfoia purge --yes

# Forensically sound (3-pass overwrite, history scrub)
openfoia purge --secure --yes

# Also fill free disk space (slow but thorough)
openfoia purge --secure --fill --yes
```

### Tor browsing

```bash
# Browse a URL through Tor
openfoia browse https://example.gov/records --tor

# Save page content to your pipeline
openfoia browse https://example.gov/records --tor --save
```

Requires Tor service running locally and `pip install openfoia[browser]`.

---

## Delivery methods

Send FOIA requests via three channels:

| Method | Provider | Cost | Setup |
|--------|----------|------|-------|
| Email | SMTP | Free | `openfoia config --init` |
| Fax | Twilio | $0.07/page | Set `OPENFOIA_TWILIO_ACCOUNT_SID` |
| Mail | Lob | ~$1/letter | Set `OPENFOIA_LOB_API_KEY` |

---

## Quick reference

```bash
# Setup
openfoia init                         # Initialize database
openfoia guide                        # This guide (in the terminal)
openfoia config --init                # Interactive configuration
openfoia portable                     # Enable USB/portable mode

# Requests
openfoia request new                  # Draft a request
openfoia request send                 # Send via email/fax/mail
openfoia request list                 # List all requests
openfoia request status REQ-001       # Check status
openfoia deadlines list               # Show deadlines

# Documents
openfoia docs ingest ./folder/        # Import documents
openfoia docs ocr DOC-001             # Run OCR

# Analysis
openfoia analyze extract DOC-001      # Extract entities
openfoia analyze graph --name X --view  # Build + view graph
openfoia analyze graphs               # List saved graphs
openfoia analyze export               # Export as FtM
openfoia analyze import data.ftm.json # Import FtM data

# Data sources
openfoia records search "query" --source muckrock
openfoia records search "query" --source opencorporates
openfoia records search "query" --source sec
openfoia records search "query" --source cia
openfoia records fetch <CREST|MORI> --source cia  # download + ingest a CIA PDF
openfoia records download <ID>        # Download FOIA docs

# Cross-reference
openfoia crossref                     # Check all entities against all sources

# Entity types
openfoia entities add                 # Add custom pattern
openfoia entities import file.csv     # Import from spreadsheet
openfoia entities test -t "text"      # Test patterns
openfoia entities list                # Show configured types

# Safety
openfoia db encrypt --password X      # Encrypt database
openfoia purge --secure --yes         # Destroy all data
openfoia browse <url> --tor           # Anonymous browsing
```
