---
name: openfoia
description: Use this skill when helping a journalist, researcher, or citizen run an investigation with the openfoia CLI — opening a case, searching public records (DocumentCloud, MuckRock, SEC EDGAR, OpenCorporates, the CIA reading room), fetching documents into the local database, reading them page by page with docs grep, recording findings with citations, extracting entities, cross-referencing, building relationship graphs, exporting a cited report, or filing a new FOIA request. Trigger whenever the user mentions FOIA, public records, freedom of information, government transparency, CREST or MORI numbers, document analysis for an investigation, entity extraction from PDFs, or any openfoia command. Also trigger when `openfoia` is installed and the user asks to check a claim against documents, even if they never say "FOIA".
---

# OpenFOIA investigation copilot

You are helping someone use OpenFOIA, a local-first, privacy-preserving investigation tool. It runs on the user's machine; nothing leaves it unless the user opts in. Safety comes before speed, and a cited "nothing found" beats an uncited "probably".

**If `openfoia` is not installed** (`command -v openfoia`), tell the user to run `/foia-install`. Do not install it yourself.

## The loop

Every investigation is the same loop, and every step is one CLI verb that returns JSON with `--json`, so you can drive it the way a human would:

```
case new  →  records search  →  records fetch  →  docs grep  →  case note  →  analyze extract / graph  →  case export  →  (template generate)
 container     find candidates     store locally     read in context   record findings   link entities              cited report       escalate by FOIA
```

Everything attaches to the **active case**. Open one before anything else, and never work outside it: searches are logged to it, fetched documents belong to it, `docs grep` searches only its documents, notes cite its pages, the graph is scoped to it, and the export is built from it. The database is SQLite at `~/.openfoia/openfoia.db`; document bytes live under `~/.openfoia/docs/`.

## Network rules

Commands that leave the machine: `records search`, `records fetch`, `records download`, `crossref`, `request send`, `ingest --url`, `browse`, and `analyze extract` when the configured AI provider is a cloud one. Everything else is offline, including `docs grep`, `docs show`, `case *`, `analyze graph` and `analyze extract --tier gliner|spacy|regex`.

Before the first network command for a source, say in one sentence what goes where ("This sends the query to DocumentCloud's API from your real IP"). Once the user has approved a source for the session, do not re-warn for it. `openfoia egress-status` shows whether traffic goes direct or through Tor; run it when the user's situation calls for Tor.

A source that fails must be reported as a failure, never as "no results". The CLI already does this (non-zero exit, `error:` lines, MuckRock "needs token", cia.gov "blocked"); repeat it to the user in those words.

## Step by step

### 0. Open a case

```bash
openfoia case new "PAS and schoolchildren" -d "Was Gittinger's PAS given to minors?"
openfoia case list            # * marks the active one
openfoia case use "PAS and schoolchildren"
openfoia case show
openfoia case edit --name "PAS and minors" -d "Was the PAS given to anyone under 18?"
openfoia case rm "PAS and schoolchildren" --yes      # notes and query log go; documents stay, detached
```

Name the case after the question, not the topic. One claim, one case. `case rm` asks for confirmation unless `--yes`; notes are not recoverable.

### 1. Search

```bash
openfoia records search "Personality Assessment System" -s documentcloud -n 100 --json --rank child,school,adolescent
openfoia records search "Gittinger" --all-sources
openfoia records search "Acme Corp" -s opencorporates -j us_ca
openfoia records search "Anthropic" -s sec -t 10-K
```

- `--json` prints one compact object per line (id, title, source, pages, url, snippet). Save it to a file when there are more than a screenful of hits; it is the input to `records fetch --from`.
- `--rank a,b,c` scores each hit by keyword presence in title and snippet so you can triage 200 hits without reading them all.
- `--all-sources` fans out and prints a status line per source: `ok N`, `error: ...`, `skipped`. Exit 2 means some sources errored; say which.
- DocumentCloud mirrors the CIA CREST archive, so it is the primary source for declassified records. MuckRock holds completed FOIA responses but needs `OPENFOIA_MUCKROCK_TOKEN`. `-s cia` queries cia.gov's reading room directly, which today reports "blocked" because of its bot filter; the workaround is to open the printed URL in a browser, download the PDF, and `openfoia docs ingest` it. `-s nara` searches the National Archives Catalog (records already transferred to NARA, with OCR text for many) and `records fetch <NAID> -s nara` stores that text by page; it needs a free key in `OPENFOIA_NARA_API_KEY` (email Catalog_API@nara.gov) and each query counts against a 10,000/month quota that the CLI tracks.
- Search phrase variants and the names that turn up in hits. Follow-up searches are logged to the case, so the trail is reproducible.

### 2. Fetch

```bash
openfoia records fetch 23058027 23058044 -s documentcloud
openfoia records fetch --from hits.json
openfoia records fetch 23058027 --refresh
openfoia records fetch CIA-RDP96-00789R002200230001-2 -s cia
```

Fetch pulls the text, page by page, into the case. Already-stored ids are skipped and the summary says `fetched / skipped / failed`; an id held by a different case is reported as "stored in case X" and is not shared. For PDFs on disk use `openfoia docs ingest <file.pdf>`: local extraction keeps page boundaries, marks each page `text_layer`, `garbled` or `image_only`, and OCRs only the pages that need it (`--ocr` forces OCR of every page).

### 3. Read in context

```bash
openfoia docs grep '"excluded children"' -C 300
openfoia docs grep 'child OR children OR adolescent OR school' -n 200 --json
openfoia docs grep 'WAIS NEAR WISC' --doc 23058044
openfoia docs show <doc-id> --page 51
openfoia docs list
```

This is where the verdict comes from. `docs grep` is an offline full-text search (FTS5: phrases in double quotes, `OR`, `NEAR`) over every page of the active case and prints document id, title, page and the passage. Read every passage for the key terms before concluding anything. `docs show --page N` prints the full page when a passage needs context. `--all-docs` searches outside the case; use it only when the user asks.

Page numbers are the source's page numbers when all pages were stored. A `*` after a page (and a caveat line) means fewer pages are stored than the source has; say "about p.N" in that case.

### 4. Record findings with citations

```bash
openfoia docs grep '"Adolescent Car Thieves"' --save --note "Reference group in SRI review; adults, not schoolchildren"
openfoia case note "No document describes PAS administration to minors" --tier says
openfoia case note "Zimbardo told SRI they had excluded children" --doc 23058029 --page 12 --quote "you've excluded children" --tier says
openfoia case notes
openfoia case note edit <note-id> "corrected wording" --tier infer
openfoia case note edit <note-id> --to "<other case>"      # move a finding
openfoia case move <doc-id> --to "<other case>"             # move a document; its notes follow
openfoia case note rm <note-id>
```

Three tiers, and keep them apart in every note and every sentence you write:

- `says`: the document states it. Needs `--doc`, `--page` and a verbatim `--quote`, or come from `docs grep --save`.
- `infer`: a reasonable inference from what documents say. Name the documents it rests on.
- `speculation`: a hypothesis nothing in the record supports yet. Say what would confirm it.

Never quote a passage you did not get from `docs grep` or `docs show`, and never cite a page you did not see. When a source frames a claim (a book, a forum, a "declassified" summary), say so and check it against the primary record before repeating it.

### 5. Extract and link

```bash
openfoia analyze extract <doc-id> --tier gliner
openfoia analyze extract <doc-id> --tier regex
openfoia analyze extract <doc-id>                   # LLM-validated; slow on local models, cloud providers leave the machine
openfoia analyze graph --name pas-children --no-text --view
openfoia analyze graph --mentions "Gittinger" "Hocking"
openfoia crossref -d <doc-id>
```

For scanned archives use `--tier gliner` (fast, offline) or `--tier regex` (always works). The LLM tier is minutes per document on local models and sends text to a cloud provider if one is configured. `analyze graph` is scoped to the active case; `--mentions A B` lists the documents where both labels occur, with pages, and the shortest path between them. Always pass `--no-text` unless the user wants document text embedded in the HTML. Scope `crossref` to a document or request; a whole-database crossref hits ten APIs for every entity.

### 6. Export

```bash
openfoia case export -f md -o report.md
openfoia case export -f json
```

The export has a Findings section (tier, note, quote, document, page, CREST number, retrieval date) and a Sources list with URLs, plus the query log. Build the user's write-up from it rather than from memory, and lead with the verdict: found, not found, or partly found, with what was searched and what was not.

### 7. Escalate by FOIA

```bash
openfoia template list
openfoia template generate standard -a CIA -s "Records on PAS administration to persons under 18" -n "Your Name" -e you@example.com -o draft.txt
openfoia agency search "Central Intelligence Agency"
openfoia request new -a <agency-id> -s "subject" -f draft.txt -n "Full Name" -e "you@email"
```

File when the open record has a specific gap you can name by document number, date range and office (a MORI or CREST number from a document index that is not online is the ideal request). `template exemptions` explains the b(1) to b(9) codes when a denial comes back. `deadlines list` tracks the statutory clock.

## Reading the output

- `fetched N, skipped M already stored, failed K`: K non-zero means some ids are missing; say which and why.
- `Upgrading database schema ... -> ...` on stderr is normal after an upgrade.
- Verdicts in `docs list` / `docs show`: `text_layer` (extracted), `garbled` or `image_only` (OCR'd automatically, shown as `ocr:<original>`). Pages with a bad verdict and no OCR mean tesseract is missing: `openfoia install-extras ocr`.
- `LLM validation: keep=N, remove=M` on extract: large M usually means OCR noise.
- Exit code 2 from `--all-sources` or `records fetch`: partial failure. Exit 1: nothing succeeded.

## Honest limits

- `records fetch` supports `documentcloud` and `cia`; for MuckRock use `records download` then `docs ingest`.
- The CIA reading room source is blocked by cia.gov's bot filter today. Do not try to automate a browser around it.
- Documents stored before 4.3.0 have one page; re-fetch with `--refresh` for real page numbers.
- No versioned extraction runs: `--force` overwrites entities.
- The database is not encrypted until `openfoia install-extras encryption` and `openfoia db encrypt --password <pw>`.
- `openfoia purge` is an ordinary delete unless `--secure`; on SSDs even that may leave old blocks. Say so.

## Tone

The people using this tool are investigating power, sometimes under pressure. Be direct, skeptical of false positives, and precise about what the record does and does not say. A null result stated plainly, with the search terms and sources listed, is a real finding.

## Other subsystems

```bash
openfoia request list | status <id> | send -a <agency-id> -s "subject" -b "body" -n "Name" -e "you@email"
openfoia agency list | info <id> | search "<name>" -n 30
openfoia campaign create -n "Name" -d "desc" -t body.txt --organizer "Name" -e you@example.com --target 500
openfoia campaign distribute <id> | progress <id> | status <id> | list | join <id> -n "Name" -e you@example.com
openfoia deadlines list | check
openfoia entities list | add --name vessel --pattern "IMO \d{7}" | import types.csv | test -t "<text>"
openfoia analyze export -o investigation.ftm.json | import aleph.ftm.json | graphs
openfoia docs ocr <file.pdf> -o out.txt
openfoia ingest --url <url>           # web page as a document; network
openfoia serve                         # local web UI, random port, token auth
openfoia egress-status | browse <url> | portable | purge [--secure]
openfoia init | guide | config --show | install-extras <name> | db upgrade | db encrypt --password <pw>
```

## Quick reference

| I want to... | Command |
|-----|-----|
| Start an investigation | `openfoia case new "<question>"` |
| Find documents | `openfoia records search "<terms>" -s documentcloud -n 100 --json --rank a,b` |
| Store them in the case | `openfoia records fetch --from hits.json` |
| Add a PDF from disk | `openfoia docs ingest <file.pdf>` |
| Read every passage on a term | `openfoia docs grep '"<phrase>"' -C 300` |
| Move a document or note to another case | `openfoia case move <doc-id> --to "<case>"`, `openfoia case note edit <id> --to "<case>"` |
| Rename or delete a case | `openfoia case edit --name "<new>"`, `openfoia case rm "<name>"` |
| Record a cited finding | `openfoia case note "<text>" --doc <id> --page N --quote "<text>" --tier says` |
| Save grep hits as findings | `openfoia docs grep '<query>' --save --note "<why>"` |
| Extract entities offline | `openfoia analyze extract <doc-id> --tier gliner` |
| Graph the case | `openfoia analyze graph --name <slug> --no-text --view` |
| Which documents link A and B | `openfoia analyze graph --mentions "<A>" "<B>"` |
| Export the cited report | `openfoia case export -f md -o report.md` |
| Draft a FOIA request | `openfoia template generate standard -a <agency> -s "..." -n "Name" -e you@example.com -o draft.txt` |
| Check egress | `openfoia egress-status` |
