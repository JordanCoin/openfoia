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

From that point OpenFOIA keeps its data, config and library caches on the USB and creates nothing in the host's home directory. The operating system can still keep traces (swap, temp files, browser and shell history); see [USB.md](USB.md#security-considerations).

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

Not sure whether your record is federal, state or local, or who holds it?
Read [The Records Ladder](RECORDS_LADDER.md) first.

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

Documents are copied to `~/.openfoia/docs/` and metadata is stripped from the stored copy: image EXIF, PDF author and XMP, DOCX author, company and revision data. With mat2 installed (`apt install mat2`; it comes with Tails), OpenDocument, XLSX, PPTX, EPUB, audio and video are cleaned too. Each file's line says what happened, for example `stripped 4 metadata fields` or `metadata NOT stripped: no metadata stripper for .odt`, and the run ends with a count of stored copies that still carry metadata. The files you point it at are never modified.

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

#### One search, every source: `openfoia search`

```bash
openfoia search "Acme Corp"                                 # every source, configured route
openfoia search "Acme Corp" -s sec -s opencorporates -n 5   # only these, 5 results each
openfoia search "Acme Corp" --egress tor --json --yes > hits.json
openfoia search "water contamination" -s documentcloud,regulations --since 2020-01-01
```

Before anything leaves, it prints the sources that will get the query, the
route, and what the route does not hide (the sources still see the query
itself), then asks. `--yes` skips the question; answering no sends nothing.
With `--json` the warning, the question and the per-source lines go to
stderr and stdout carries one JSON document:

```json
{
  "query": "Acme Corp",
  "egress": "tor",
  "sources": [
    {"source": "documentcloud", "status": "ok", "total": 214, "returned": 10, ...},
    {"source": "muckrock", "status": "skipped", "reason": "personal API token not sent over Tor", ...},
    {"source": "sec", "status": "error", "reason": "HTTP 503", ...}
  ],
  "results": [
    {"source": "documentcloud", "title": "...", "url": "https://...", "snippet": "...",
     "retrieved_at": "2026-11-17T14:02:31Z", "egress": "tor", "id": "23058027",
     "date": "1977-08-03", "type": "DOCUMENT"}
  ]
}
```

- Each source is ok, `no_results` (it answered with nothing), `error` (it
  failed; the reason says how) or `skipped` (it was not sent the query; the
  reason says why). One source failing does not stop the others. Exit 0
  means every source answered or was skipped, 2 that some errored, 1 that
  none answered.
- `--egress tor` or `--egress direct` overrides `network.tor` for one run.
  Tor fails closed: if the Tor proxy is not reachable or `socksio` is not
  installed (`openfoia install-extras tor`), the command stops before the
  warning and nothing is sent.
- Over Tor, personal keys are not sent. MuckRock and NARA cannot work
  without one, so they are skipped. `fec`, `govinfo` and `regulations` use
  the shared DEMO_KEY instead, and the warning lists them.
- Sources are searched four at a time; each keeps its own pacing (SEC and
  NARA wait between requests) and no host gets more than ten requests a
  second.
- There is no relevance score: results are in each source's own order,
  grouped by source.
- Result text comes from third parties. The terminal view strips control
  characters; `--json` escapes everything outside ASCII.
- When a case is active the query is logged to it, as with `records search`.

Every source call prints which third party it contacts before the request
leaves. Current limitation: cia.gov's bot filter blocks non-browser clients,
so `--source cia` search and fetch report "blocked" today rather than
pretending there were no results. Workaround: open the printed URL in a
browser, download the PDF, and `openfoia docs ingest <file.pdf>`. Automating
a real browser against that filter is not planned: it would bypass the egress
policy and put a browser fingerprint on cia.gov from the researcher's machine.

A source that fails (rate limit, timeout, missing token) is reported as an
error and the command exits non-zero; "No results found" means the source
answered and had nothing. MuckRock's API now needs a token: set
`OPENFOIA_MUCKROCK_TOKEN`.

#### NARA Catalog (`--source nara`)

The National Archives Catalog holds descriptions of records that agencies have
transferred to NARA, and OCR text for many digitised ones (record groups such
as RG 263, CIA). It needs a free API key: there is no signup form, you email
`Catalog_API@nara.gov` and set `OPENFOIA_NARA_API_KEY` (`CATALOG_API_KEY`, the
name NARA's own scripts use, works too). The default key allows
10,000 queries a month. openfoia counts your queries locally in
`nara_quota.json` under the data directory, warns at 80 percent and refuses at
100 percent (`OPENFOIA_NARA_IGNORE_QUOTA=1` overrides if NARA raised your
tier). Requests go out one per second. `OPENFOIA_NARA_API_BASE` is a
test-only knob: it accepts only `https://catalog.archives.gov/...` or a
localhost server, so the key is never sent anywhere else.

```bash
openfoia records search "Gittinger" -s nara -n 50 --json
openfoia records fetch 7283224 -s nara     # NAID from the search or the catalog URL
openfoia records search -s nara --ancestor 7283224 --all --max 500 --json   # every child record
openfoia records fetch 7283224 -s nara --list-objects   # preview scans/PDFs, download nothing
openfoia records fetch 7283224 -s nara --objects        # download + OCR them into the case
```

These follow NARA's own bulk-download scripts. `--ancestor` limits a search to
records under a series or file unit; `--all` walks every page (100 hits per
query, each counted against the quota, capped by `--max`, default 1000) and
says how many queries it may take first. `--objects` saves each digital object
to `<data dir>/nara/<NAID>/<original filename>`, skips files already there (so
re-running resumes), and ingests the rest (PDFs through text extraction and
OCR, images through OCR) under the same record as the text fetch. Objects come
from NARA's storage on Amazon S3 (`nara-media-001.s3.amazonaws.com`), a second
host the command names before downloading; they are not catalog queries and do
not count against the quota. Objects hosted anywhere else are listed but not
downloaded.

Fetch stores the record's OCR text page by page when the catalog has it, so
`docs grep` cites real pages; when it has none, the catalog description is
stored instead and `docs show` says so. NARA's API terms ask users not to
cache or scrape the catalog, so openfoia stores only the records you fetch by
NAID, nothing from search results, and prints the required notice: this
product uses the National Archives Catalog API but is not endorsed or
certified by the National Archives and Records Administration.

GovInfo (`--source govinfo`) works without a key on the shared DEMO_KEY, which
is rate limited; set `OPENFOIA_GOVINFO_API_KEY` from https://api.data.gov/signup/
for your own limit.

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

### Searching by meaning (optional, experimental)

`docs grep` finds the words you type. A page that says "funds transferred to a
sibling of the city executive" never matches "payments to the mayor's
brother". `docs grep --semantic` ranks pages by similarity of meaning instead.
It is an optional extra and runs on this machine:

```bash
openfoia install-extras semantic        # model2vec + sqlite-vec, ~90 MB (PyPI)
openfoia docs embed-model               # asks first, then ~31 MB from huggingface.co, once
openfoia docs embed                     # embed stored pages, offline (~1.4 ms a page)
openfoia docs grep --semantic "payments to the mayor's brother"
openfoia docs embed --status            # what is indexed, which search path runs
```

- **One network action.** `docs embed-model` is the only step that goes
  online, and it says what it sends before asking: your IP address (the Tor
  exit's with `--tor`) and which model files you fetch, nothing from your
  documents. Files are pinned and hash-checked, and stored in the data
  directory's `models/` (portable mode respected). Embedding and searching are
  offline; `grep --semantic` embeds any new pages itself before searching.
- **Models.** `potion-base-8M` (English, default, ~150 MB RAM),
  `potion-retrieval-32M` (English, ~131 MB, ~350 MB RAM),
  `potion-multilingual-128M` (101 languages, 531 MB, ~1.9 GB peak RAM: too
  much for many cheap laptops). Pick one with `--model`. Keyword `docs grep`
  already works in every language.
- **Read the results; do not count them.** Every page gets a score, best first,
  one passage per page. On our generated 1,000-page test set the default model
  put a reworded passage in its top 10 for 3 of 10 queries, where keyword grep
  found none of them; words that appear in the query still pull unrelated pages
  up. It finds leads; it cannot tell you a document is not there. Exact names
  and numbers: use plain `docs grep`.
- **Search path.** sqlite-vec when it loads (it does inside an encrypted
  database); otherwise numpy brute force, for example on macOS's system
  Python, which cannot load SQLite extensions. The command says which ran.
- **Safety.** Embeddings can leak the text they came from. They are stored in
  the database (encrypted with it), deleted with their page or document, and
  removed by `openfoia docs embed --drop` or `purge`. See
  [Threat Model](THREAT_MODEL.md#semantic-search-embeddings).

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

### Extraction runs

Every extraction is kept as a run: which backend and model produced it, when,
and how many entities it found. Re-extracting with `--force` adds a run and
keeps the earlier ones. The graph, crossref, exports, the web UI and the agent
use one run per document: the newest, unless you pick another.

```bash
openfoia analyze runs <document-id>                            # list runs; "yes" marks the one in use
openfoia analyze extract <document-id> --prefer-run <run-id>   # use an earlier run instead
openfoia analyze runs <document-id> --prune                    # delete every other run and its entities
```

Earlier runs stay in the database until you prune them or run `openfoia purge`.
`--prune` overwrites the freed database pages, but it cannot reach copies made
before it ran: graph and FtM exports, backups, or old blocks on an SSD.

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

## AI agents over MCP

`openfoia mcp serve` runs OpenFOIA as an [MCP](https://modelcontextprotocol.io)
server, so an MCP client (Claude Desktop, Claude Code, or any other) can use it
as tools. The tools run the same code as the CLI: `openfoia_search` is
`openfoia search`, with the same warning, the same Tor rules and the same JSON
as `openfoia search --json`; `openfoia_docs_grep` is `docs grep`.

```bash
openfoia install-extras mcp     # the MCP Python SDK; the core install does not need it
openfoia mcp tools              # what an agent would see (works without the extra)
```

The client starts the server itself and talks to it over stdin and stdout.
There is no network port: nothing on your network can connect to it.

**Claude Code:**

```bash
claude mcp add openfoia -- openfoia mcp serve
```

**Claude Desktop:** add this to `claude_desktop_config.json` (Settings →
Developer → Edit Config), then restart Claude Desktop. If Claude Desktop cannot
find `openfoia`, use the full path that `which openfoia` prints.

```json
{
  "mcpServers": {
    "openfoia": {
      "command": "openfoia",
      "args": ["mcp", "serve"]
    }
  }
}
```

To let the agent search public-records sources, add `"--allow-network"` to
`args` (or `claude mcp add openfoia -- openfoia mcp serve --allow-network`).
For portable mode or a second data directory, add
`"env": {"OPENFOIA_DATA_DIR": "/path/to/data"}`.

| Tool | What it does | Network |
|---|---|---|
| `openfoia_search` | `openfoia search`: one query to every records source, per-source status, provenance on every row | Yes, only with `--allow-network`, only after a confirmed call |
| `openfoia_docs_grep` | `docs grep` over stored pages (FTS5 keyword, or `mode=semantic` when the semantic extra and model are installed; the reply names the engine) | No |
| `openfoia_read_page` | One stored page with its citation | No |
| `openfoia_list_cases` | Cases, document counts, which is active | No |
| `openfoia_case_summary` | `case show` / `case export --format json`: documents, notes, logged queries | No |
| `openfoia_list_requests` | Your FOIA requests and their status | No |
| `openfoia_entities` | Extracted entities and a summary of their relationship graph, for a document or a case | No |

The local tools use the active case unless the agent passes `case` or
`all_docs`, like the CLI. No tool takes a file path, and no tool deletes,
purges, sends FOIA requests, sends email, fax or mail, ingests files or changes
config: those stay CLI commands you run yourself.

**How a search is gated.** Three things must all hold before a query leaves
the machine: you started the server with `--allow-network` (the model cannot
turn it on); the agent called `openfoia_search` once without `confirmed` and got
the warning back (which sources, Tor or direct); and it called again with
`confirmed: true`. The agent is told to show you the warning and to confirm
only after you agree, but the model sets `confirmed`, not you, so also keep
your client's approval prompt on for `openfoia_search`. When Tor is configured
(`network.tor`), a call asking for `egress: direct` is refused, and if Tor is
not running nothing is sent.

**What the client and its model provider see.** The server runs on your
machine; the model usually does not. Everything a tool returns (passages, page
text, case names and notes, the queries logged to a case, entity names, your
FOIA request subjects) goes to the client, and the client sends it to its model
provider (Anthropic, for Claude Desktop and Claude Code) as part of the
conversation. If a document must not leave the machine, do not let an agent
read it, or use a client with a local model. See `docs/THREAT_MODEL.md`.

**Logs.** The server writes nothing to stdout except protocol messages. Logs go
to stderr, which MCP clients keep in log files, so by default they hold only
the message and the error type, never a query or document text. `--verbose`
logs details (and so can log content). Stray output from libraries goes to the
null device.

**Encrypted database.** The server cannot ask for a password (stdin is the
protocol), so it reads the key the same way the CLI does without a prompt:
`OPENFOIA_DB_PASSWORD` in its environment, or the config file. Putting
`OPENFOIA_DB_PASSWORD` in the client's config file stores your database
password in plaintext there.

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
openfoia init --encrypt
# or encrypt an existing database:
openfoia db encrypt
```

Both prompt for the passphrase. Requires SQLCipher: `pip install openfoia[encryption]`

Later commands read the passphrase from `OPENFOIA_DB_PASSWORD`. Set it
with `read` so it is typed without echo and never appears on a command line:

```bash
read -rs OPENFOIA_DB_PASSWORD && export OPENFOIA_DB_PASSWORD   # bash, zsh
read -s -x OPENFOIA_DB_PASSWORD                                # fish
```

The variable lasts until the terminal closes and is readable by programs
running as your user.

Never type a passphrase as a command-line argument (`--password VALUE`,
`OPENFOIA_DB_PASSWORD=VALUE openfoia ...`): it is saved in your shell history
and is visible in the process list while the command runs. If you already
did, delete those lines from `~/.bash_history` / `~/.zsh_history` (fish:
`history delete --contains openfoia`), and treat that passphrase as exposed.
`openfoia purge --secure` also removes lines that mention `openfoia` or
`run.sh`, but only when you purge.

### Duress mode

If you're forced to unlock your device, a second password opens a decoy database with harmless-looking data:

```bash
openfoia init --encrypt --duress   # prompts for both passphrases
```

The real database must be encrypted; `init` refuses to put a decoy next to a
plaintext one (for an existing database: `openfoia db encrypt`, then
`openfoia init --duress`). The real password opens your investigation. The duress password opens weather data and meeting minutes.

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

With `--tor`, Chromium sends hostnames to Tor's SOCKS proxy (Tor does the DNS
lookup), and the browser's own DNS resolver is switched off for the session,
so a name that somehow bypassed the proxy fails instead of being looked up on
your network. The browser gets a throwaway home directory inside the data
directory, removed when it exits.

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
openfoia search "query"               # every source, asks first
openfoia records search "query" --source muckrock
openfoia records search "query" --source opencorporates
openfoia records search "query" --source sec
openfoia records search "query" --source cia
openfoia records fetch <CREST|MORI> --source cia  # download + ingest a CIA PDF
openfoia records download <ID>        # Download FOIA docs

# Cross-reference
openfoia crossref                     # Check all entities against all sources

# AI agents (MCP, stdio only)
openfoia mcp tools                    # List the tools an agent gets
openfoia mcp serve                    # Started by the MCP client; offline tools only
openfoia mcp serve --allow-network    # Also let openfoia_search ask the records sources

# Entity types
openfoia entities add                 # Add custom pattern
openfoia entities import file.csv     # Import from spreadsheet
openfoia entities test -t "text"      # Test patterns
openfoia entities list                # Show configured types

# Safety
openfoia db encrypt                   # Encrypt database (prompts)
openfoia purge --secure --yes         # Destroy all data
openfoia browse <url> --tor           # Anonymous browsing
```
