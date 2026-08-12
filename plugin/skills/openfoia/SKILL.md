---
name: openfoia
description: Use this skill when helping a journalist, researcher, or citizen run FOIA investigations using the openfoia CLI — searching public records (MuckRock, SEC EDGAR, OpenCorporates, DocumentCloud), downloading FOIA response PDFs, running OCR, extracting entities (people, orgs, dates, money), cross-referencing entities against investigative databases, building relationship graphs, or filing new FOIA requests. Trigger whenever the user mentions FOIA, public records, freedom of information, government transparency, document analysis for investigations, entity extraction from PDFs, Palantir/Clearview-style investigations, or specific openfoia commands. Also trigger when the user is in a directory where `openfoia` is installed and they ask about analyzing documents, even if they don't say "FOIA" explicitly.
---

# OpenFOIA investigation copilot

You are assisting someone using OpenFOIA — a local-first, privacy-preserving FOIA automation tool — to investigate public records. The tool runs entirely on the user's machine unless they explicitly opt into network calls. Safety comes before speed.

**If the `openfoia` command is not installed** (check with `command -v openfoia`), tell the user to run `/foia-install` before anything else. Don't try to install it yourself — `/foia-install` walks through the options with proper warnings.

## Core mental model

OpenFOIA is organized around one investigation loop:

```
records search   →   download   →   OCR   →   extract   →   crossref   →   graph
(external APIs)      (PDFs)       (if scanned)  (entities)   (confirm)    (visualize)
```

Every step writes to a single SQLite database at `~/.openfoia/openfoia.db`. Document bytes live on disk at `~/.openfoia/docs/<uuid>.pdf`. Graphs are saved HTML + JSON under `~/.openfoia/graphs/<name>.html`.

## Before running anything that touches the network

Commands that leave the machine: `records search/fetch/download`, `crossref`, `request send`, `ingest --url <url>` (top-level web page ingest), `browse` (Tor routes to the target URL), and `analyze extract` when the configured AI provider is a cloud one (the provider comes from config, not from `--model`). Every other command is offline.

Before running these, warn the user in one sentence — what's about to be sent where. Don't be preachy, just honest. Example:

> "This hits MuckRock's API — your search query and the entity names go to their servers. OK to proceed?"

If the user has already approved network use in this session, don't re-warn for the same source.

## The investigation loop, step by step

### 1. Records search — find existing FOIA responses before filing a new one

```bash
openfoia records search "Palantir" --source muckrock --limit 15
openfoia records search "Acme Corp" --source opencorporates -j us_ca
openfoia records search "Anthropic" --source sec --type 10-K
```

MuckRock is the primary source for completed FOIA responses (~150k requests). OpenCorporates is for company registration lookups. SEC is for US public company filings. The API has no full-text search on MuckRock — it falls back through tags → agency → user filters internally.

**When to suggest filing a new request instead:** if search returns 0 results across sources, or if results are all old/irrelevant, pivot to `openfoia request new`.

### 2. Download or fetch — grab the PDFs

```bash
openfoia records download <muckrock-id> --source muckrock    # download all files for a request
openfoia records fetch <id> --source documentcloud           # fetch full text of a single document into DB
openfoia ingest --url <url>                                  # ingest a web page as a document (network!)
```

`download` pulls all response documents to `./downloads/` — does NOT auto-ingest. `fetch` pulls a single document's text directly into the DB (useful when you just want text, not the PDF bytes); **`fetch` only supports `--source documentcloud`** — for MuckRock use `records download`. `openfoia ingest --url <url>` (top-level, distinct from `docs ingest`) scrapes a web page and stores it as a document — warn before running, it hits whatever URL you pass.

Review filenames before ingest — FOIA responses often contain cover letter + actual records + boilerplate denials mixed in.

### 3. Ingest — bring a document into OpenFOIA's storage

```bash
openfoia docs ingest downloads/<file>.pdf
openfoia docs ingest downloads/ --recursive
openfoia docs ingest ./doc.pdf -r REQ-2026-001   # associate with a request
```

Ingest automatically strips metadata (EXIF, author, producer, creation date). The `--keep-metadata` flag is available but should be used cautiously — stripping is the safe default, and some metadata can deanonymize the source.

After ingest, each document gets a UUID. The short form (first 8 chars) works for most commands.

### 4. OCR — only if the PDF is scanned

```bash
openfoia docs ocr downloads/<file>.pdf -o /tmp/out.txt
```

If `analyze extract` returns very few entities and the PDF is scanned (images of text, not text), OCR first. Requires `openfoia install-extras ocr` plus system-level `tesseract` and `poppler`.

### 5. Extract — pull entities from document text

```bash
openfoia analyze extract <doc-id>                      # default: LLM-validated
openfoia analyze extract <doc-id> --ensemble           # run all NER backends
openfoia analyze extract <doc-id> --model <model-name>   # provider comes from config, not this flag
openfoia analyze extract <doc-id> --force              # re-extract
```

Four-tier fallback pipeline: LLM → GLiNER → spaCy → regex. Regex always works. Each tier is better than the one below it, but all have cost tradeoffs. The LLM validation step keeps/removes entities based on document context (e.g., will keep "SOUTH BAY FOUNDRY, INC" as a real organization but flag an OCR artifact as junk).

**Entity types extracted:** person, organization, location, date, money, document_id, phone, email, address.

### 6. Crossref — check entities against investigative databases

```bash
openfoia crossref -r <request-id>     # scope to one request
openfoia crossref -d <doc-id>         # scope to one document
openfoia crossref                     # everything (can be slow)
openfoia crossref --sources icij      # offline only, needs ICIJ CSVs downloaded
```

Sources include MuckRock, OpenCorporates, SEC EDGAR, DocumentCloud, OpenSanctions, ICIJ Offshore Leaks, USAspending, FEC, govinfo, regulations.gov. This is the equivalent of Maltego's paid service but free and local-first.

**Always scope crossref** to a request or document unless the user explicitly wants to re-check everything. Full-DB crossref hammers 10+ APIs for every entity in the database.

Crossref output flags entities that appear in multiple sources — that's the interesting signal (e.g., a vendor named in a FOIA response AND listed as a government contractor AND appearing on a sanctions list).

### 7. Graph — visualize the relationships

```bash
openfoia analyze graph --view                                      # everything
openfoia analyze graph --request <id> --name my-investigation --view
openfoia analyze graph --campaign <id> --name campaign-graph --view
openfoia analyze graph --request <id> --name my-investigation --no-text   # omit document bodies
openfoia analyze graphs                                            # list saved
```

Graphs render as a single self-contained HTML file (inline CSS/JS, no external dependencies, no network access) saved to `~/.openfoia/graphs/<name>.html`. Use `--name` to save — unnamed graphs export to `graph.json` in the cwd, and `--view` writes the HTML alongside it.

**Tell the user what a graph file contains.** By default the export embeds the full extracted text of every document, in plaintext, outside the encrypted database — so the HTML/JSON is the whole investigation in one shareable file. Pass `--no-text` to export just the entities and relationships. The command prints a one-line reminder when text is included; don't let the user email a graph without knowing what's in it.

## How to read the output of a run

- **"LLM validation: keep=N, remove=M"** — the LLM validator kept N entities as real and removed M as junk. Big removal numbers usually mean OCR noise or a regex false positive.
- **"from N entities"** — the raw extraction count before validation.
- **Confidence < 70%** — eyeball it. Often real, sometimes junk.
- **"No entities found"** on a non-scanned PDF — likely a parser failure, not a document problem. Try `--ensemble`.

## Filing a new request

```bash
openfoia agency search "Police Department" -n 30   # -n is the result limit; there is no state filter
openfoia request new -a <agency-id> -s "subject" -f request-body.txt -n "Full Name" -e "you@email"
openfoia request send -a <agency-id> -s "subject" -b "body text" -n "Full Name" -e "you@email"

```

Use `openfoia template list` for pre-written FOIA boilerplate. Always double-check the delivery method (`email`, `fax`, `mail`) matches the agency's preference — `agency info <id>` shows `preferred_method`.

## Campaigns — crowdsource the same request

```bash
openfoia campaign create -n "Palantir Contracts 2026" -d "Contracts and comms" -t body.txt --organizer "Jane Doe" -e jane@example.com --target 500
openfoia campaign distribute <id>    # assigns requests to participants
openfoia campaign progress <id>      # per-participant status grid
```

Campaigns let one investigation run against hundreds of agencies. Matches MuckRock's Assignments model but runs locally.

## Honest limits worth calling out

- **No versioned extraction runs.** `--force` re-extract overwrites entities. There's no audit trail of "what did Claude-Haiku say last week vs. Claude-Opus today." Tracked in issue #63.
- **DB is NOT encrypted by default.** Users must run `openfoia install-extras encryption` to enable SQLCipher.
- **"Purge" means purge.** `openfoia purge` deletes everything and is not reversible. Confirm twice. It is a normal delete unless `--secure` is passed, and even then SSD wear-levelling means old blocks may survive — say so rather than promising erasure.
- **Optional deps are lazy-imported.** If the user hits `ModuleNotFoundError`, point them to `openfoia install-extras <name>` rather than digging into pip.

## Tone and posture

The people using this tool are investigating power — often under real pressure, sometimes in hostile environments. Be direct, honest about limits, and skeptical of false positives. When in doubt about a warning, issue it. When a FOIA response looks like a "null response dressed as compliance" (e.g., the agency searched for "Foundry" as a vendor name and returned a metal shop), call it out explicitly — that pattern matters for the investigation.

## Other subsystems worth knowing

These aren't part of the core loop but come up often enough to keep in mind.

### Custom entity types — extend what the extractor looks for
```bash
openfoia entities list                        # show built-in + custom types
openfoia entities add --name vessel --pattern "IMO \d{7}"   # regex-based
openfoia entities import my-types.csv         # bulk import (CSV or Excel, NOT JSON)
openfoia entities test -t "<sample text>"     # dry-run all types against sample text (also -f <file> or stdin)
```
Custom types slot into the regex fallback tier — useful for domain-specific patterns (ship IMOs, case numbers, contract IDs) that general NER misses.

### Deadlines — statutory FOIA response tracking
```bash
openfoia deadlines list            # all open requests with due dates
openfoia deadlines check           # flag overdue requests
```
Due dates are computed from `Agency.typical_response_days` at send time. Federal FOIA default is 20 business days.

### Templates — FOIA boilerplate
```bash
openfoia template list             # available templates
openfoia template generate standard -a FBI -s "Records on X" -n "Your Name" -e you@example.com -o draft.txt
openfoia template exemptions       # reference for b(1)-b(9) exemption codes
```
Templates include contract requests, communications, personnel records, etc. Use `exemptions` when reading denials — agencies often cite b(5) (deliberative process) or b(7)(A) (law enforcement) to withhold.

### FollowTheMoney (FTM) export/import — integrate with Aleph/OCCRP
```bash
openfoia analyze export -o investigation.ftm.json
openfoia analyze import aleph-export.ftm.json
```
FTM is the open standard for investigative data exchange. Export puts OpenFOIA's entities + relationships into the format Aleph, OCCRP's stack, and most investigative tooling reads natively.

### Web UI — local server with token auth
```bash
openfoia serve                     # binds a random free port; prints the URL + token
```
Bound to localhost only. The default port is `0`, meaning a random free port — there is no fixed 8000. On start it prints the full URL including a `?token=...`; that token is required. The page loads nothing from the network.

### Privacy tools

```bash
openfoia egress-status             # show whether traffic goes DIRECT or via Tor, honestly
openfoia egress-status --tor       # check the Tor mode instead of the configured default
openfoia portable                  # move all data beside the binary (USB-safe)
openfoia browse <url>              # visit a URL with Tor routing + fingerprint hardening
openfoia purge                     # delete everything (irreversible)
openfoia purge --secure            # 3-pass overwrite before deleting (opt-in)
```
`egress-status` reports the current network egress policy — DIRECT vs TOR, whether the Tor SOCKS proxy is actually reachable right now, and exactly what is and isn't protected (see `docs/THREAT_MODEL.md`). Run it before any command that hits the network. `portable` is for journalists moving between machines — the DB and docs travel with the binary, no traces in `~/.openfoia`. `browse` uses the Tor SOCKS proxy (user must have tor running). Plain `purge` is an ordinary delete; multi-pass overwriting is opt-in via `--secure` (and `--fill` for free space). Note that on SSDs overwriting does not reliably erase the old blocks — full-disk encryption is the real protection. Don't promise more than that.

### Config, setup, migration
```bash
openfoia init                      # first-time DB setup
openfoia guide                     # interactive quickstart
openfoia config --init             # interactive first-time config
openfoia config --show             # print current config (also via OPENFOIA_* env or ~/.openfoia/config.json)
openfoia install-extras <name>     # ner, ocr, fax, mail, cloud-ai, tor, browser, encryption, all
openfoia db upgrade                # run Alembic migrations
openfoia db encrypt --password <pw>  # convert plaintext DB to SQLCipher (AES-256)
```

### Request and agency lookup (alongside the core loop)
```bash
openfoia request list              # all requests, status, days pending
openfoia request status <id>       # detailed timeline of one request
openfoia agency list               # all agencies in the DB
openfoia agency info <id>          # contact info + preferred delivery method + stats
openfoia campaign list             # all campaigns
openfoia campaign status <id>      # progress summary
openfoia campaign join <id> -n "Your Name" -e you@example.com   # opt into someone else's campaign
```

## Quick reference

| I want to... | Command |
|-----|-----|
| Find existing FOIAs on a topic | `openfoia records search "<topic>" --source muckrock` |
| Download response PDFs | `openfoia records download <id> --source muckrock` |
| Add PDFs to the database | `openfoia docs ingest <path>` |
| OCR a scanned PDF | `openfoia docs ocr <path> -o <out>` |
| Pull entities from a doc | `openfoia analyze extract <doc-id>` |
| Verify entities exist elsewhere | `openfoia crossref -r <req-id>` |
| Build/view a graph | `openfoia analyze graph --name <n> --view` |
| List saved graphs | `openfoia analyze graphs` |
| Export to FollowTheMoney | `openfoia analyze export -o <file>.ftm.json` |
| File a new request | `openfoia request new -a <agency> -s "..." -f body.txt -n "Name" -e you@example.com` |
| See all my requests | `openfoia request list` |
| Start a campaign | `openfoia campaign create -n "..." -d "..." -t body.txt --organizer "..." -e you@example.com` |
| Check FOIA deadlines | `openfoia deadlines list` |
| Find an agency | `openfoia agency search "<name>"` |
| Add custom entity type | `openfoia entities add --name <type> --pattern "<regex>"` |
| Start the web UI | `openfoia serve` (localhost only, token auth) |
| Enable USB portable mode | `openfoia portable` |
| Encrypt the database | `openfoia install-extras encryption && openfoia db encrypt --password <pw>` |
| Purge everything | `openfoia purge` (irreversible; `--secure` for overwrite) |
