# Changelog

All notable changes to OpenFOIA are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries before 4.0.0 are backfilled from git history and are summaries, not
exhaustive lists.

## [Unreleased]

### Added

- `install.sh --with-fonts` fetches the PDF engine's open-source font pack
  (`fonts.tar.gz`, 12.6MB download, ~24MB on disk, checksum-verified, from
  the public pdf-to-text release) into `~/.local/share/glyph-api/fonts`, or
  next to the binary in portable mode, and the extractor is pointed at it.
  It is opt-in because it rarely matters: with engine 0.8.0 on 175 real
  PDFs, unresolved glyph totals are identical with and without it (one file
  differed by 2 glyphs). It feeds the engine's fingerprint stage, which only
  engages for embedded fonts with no usable character map. `uninstall.sh`
  removes it and `openfoia init` says whether it is present. A font dir you
  set yourself (`GLYPH_API_FONTS_DIR` or `GLYPH_FONTS_DIR`) still wins; a
  pack next to the binary wins over one in the user data dir, so a portable
  install uses its own. A failed download or checksum skips the fonts and
  the rest of the install continues.

- NARA Catalog works the way NARA's own bulk-download scripts do.
  `records search -s nara --ancestor <NAID>` finds every record under a series
  or file unit (the search term is optional), and `--all` walks every page up
  to `--max` hits (default 1000), saying first how many quota-counted queries
  it may take. `records fetch <NAID> -s nara --list-objects` previews a
  record's scans and PDFs without downloading; `--objects` downloads them from
  NARA's storage on Amazon S3 into `<data dir>/nara/<NAID>/`, skips files
  already there so a re-run resumes, and OCRs them into the active case next
  to the record's text. The key can also be set as `CATALOG_API_KEY`, the name
  NARA's scripts use.

### Changed

- PDFs read by an older `pdf-extract` that has no `--json` output (so no
  per-page verdicts) are now checked for text quality: if the text averages
  under 20 characters a page, or under 20% of its non-space characters are
  letters, the whole document goes to OCR. If OCR is not installed or finds
  nothing, the extracted text is kept and `openfoia install-extras ocr` is
  suggested once. With the current `pdf-extract` the only change is that the
  "too little text, OCR everything" check counts non-space characters:
  scanned and garbled pages are still OCR'd one by one, and tables and FOIA
  logs keep their exact text-layer numbers. Tune the gates with `OPENFOIA_PDF_EXTRACT_MIN_CHARS`
  (default 50), `OPENFOIA_PDF_EXTRACT_MIN_CHARS_PER_PAGE` (default 20) and
  `OPENFOIA_PDF_EXTRACT_MIN_ALPHA_RATIO` (default 0.20), or the matching
  `pdf_extract` keys in `config.json`. `docs ocr` now uses these settings too.

## [4.5.0] - 2026-10-14

### Added

- All 50 states and DC now carry sourced statute, clock, fee, residency and
  appeal data for their public-records law, read from the Reporters Committee
  for Freedom of the Press Open Government Guide
  (https://www.rcfp.org/open-government-guide/); Tennessee is partial (its
  response clock was not in the fetched text). `openfoia jurisdiction list`
  ends with a verified/unverified count. `scripts/rcfp_jurisdictions.py`
  drafts a jurisdiction file from a saved guide page, so the data can be
  rebuilt without retyping it; it fetches nothing itself.
- Request clocks come from the agency's jurisdiction: `openfoia request mark
  <id> acknowledged|extended|tolled --until <date>|received|denied|appealed|
  still-interested --due <date>|replied|closed [--on <date>]` records the
  event in the timeline and recomputes every clock from the law each time it
  is shown: the response deadline (business days skip federal holidays),
  the extension, tolls paused in the clock's own unit, the appeal window
  (federal 90 calendar days, Michigan 180) and the "still interested" reply
  date. An appeal retires the response clock; only federal FOIA has an
  appeal-decision clock (20 business days). `request status`, `deadlines
  list` and `deadlines check` cover all of them, overdue first, and a law
  with no statutory clock is reported as "no statutory deadline" instead of
  an invented date, even for requests sent before this release. The agent's
  send tool uses the same code path as the CLI. Agencies with no
  jurisdiction fall back to federal FOIA and say so. Database migration
  `007`.
- Jurisdictions: federal FOIA is no longer the only law openfoia knows. One
  JSON file per public-records law lives in `openfoia/jurisdictions/`
  (`us` plus all 50 states and DC) with the statute, response clock, fee,
  residency and appeal rules; a null field means unknown, never "none", and
  stubs say `verified: false`. `openfoia jurisdiction list|show <id>|search
  <text>` reads them, each with its sources.
- Local public bodies: `openfoia agency add --name ... --jurisdiction us-mi
  --level school_district [--email --address --portal --custodian-title
  --response-days]` adds a school district, city, county or university with
  its state's law and clock. `agency list --jurisdiction us-mi --level
  school_district` filters; `agency info` shows the law and the custodian.
  Database migration `006` (nullable columns only; existing agencies are
  marked federal).
- Jurisdiction-aware request letters: `openfoia template generate standard
  --jurisdiction us-mi` (or any agency added with a jurisdiction) writes a
  public-records letter under that state's law instead of federal FOIA. It
  cites the statute and exemptions section when the jurisdiction file knows
  them and leaves `[CITE STATUTE]` otherwise, states the response clock only
  when it is known, asks for an itemized fee estimate before work with a
  `[FEE CAP]` placeholder, an exemption citation for every withheld record,
  and the retention schedule plus destruction log for any record reported
  destroyed. Federal letters are unchanged. `template show <name>
  --jurisdiction <id>` prints the skeleton; `template list` marks which
  templates take a jurisdiction.
- `school-program` template: asks a school district or state education agency
  for program records (board minutes, plans, screening instruments, vendor
  contracts, evaluations, grant files) and aggregate counts, and states that
  individual student records under FERPA (20 U.S.C. 1232g) are not requested
  and give no ground to deny the request as a whole.
- National Archives Catalog source (`--source nara`): `records search`
  queries catalog.archives.gov's API v2 and `records fetch <NAID> -s nara`
  stores the record's OCR text page by page (or its description when the
  catalog has no text) in the active case. Needs a free key in
  `OPENFOIA_NARA_API_KEY` (email Catalog_API@nara.gov); queries are counted
  locally against the 10,000/month quota with a warning at 80 percent and a
  refusal at 100 percent, and sent one per second. `--all-sources` reports
  `skipped (no OPENFOIA_NARA_API_KEY)` instead of an error without a key.
- `OPENFOIA_GOVINFO_API_KEY` supplies a GovInfo (api.data.gov) key; a 403 or
  429 from GovInfo now names it.

- `docs/RECORDS_LADDER.md`: which rung of the US public-records system holds
  a record (the public body, the state law, state agencies and archives,
  federal FOIA, non-FOIA paths such as NARA and CREST), how to ask at each,
  what goes wrong, a worked Detroit example, and what openfoia covers today.

### Internal

- Text-pulling adapters share one storage path (`openfoia/records/store.py`),
  so DocumentCloud and NARA documents are stored, re-fetched and attached to
  cases the same way.

## [4.4.0] - 2026-10-14

Cases get update and delete: rename, re-describe, remove, and move documents
and findings between cases as an investigation changes shape. No schema
change; the `pdf-extract` 0.7.0 binaries on the 4.3.0 release still apply.

### Added

- Cases are now fully editable: `openfoia case edit [--name <new>] [--desc
  <text>]` renames a case (the slug follows) or changes its description,
  `openfoia case rm <name> [--yes]` deletes a case with its notes and query
  log after confirmation while keeping its documents in the database,
  detached, and `openfoia case note edit <id> [new text] [--tier --page
  --quote --doc]` changes a finding in place. `case note edit <id> --to
  <case>` moves a finding to another case, and `openfoia case move <doc-id>
  --to <case>` moves a document (with the documents fetched alongside it, and
  the notes that cite them) to another case.

## [4.3.1] - 2026-10-14

Plugin and fixes found by running the 4.3.0 investigation loop end to end.
No schema change; the `pdf-extract` 0.7.0 binaries on the 4.3.0 release
still apply and `install.sh` picks them up.

### Fixed

- `analyze graph --mentions A B` now also checks the stored page text, so a
  name the extractor never tagged still counts as a mention and the pages are
  listed. `docs grep --save` records its notes as tier `says` (they are
  verbatim passages), so they carry the tier in `case notes` and exports.

### Changed

- Claude Code plugin 0.2.0: the `openfoia` skill and `/foia-investigate` now
  teach the case loop (`case new`, ranked `records search --json`,
  `records fetch --from`, `docs grep`, cited `case note`, scoped graph,
  `case export`) with the three evidence tiers, instead of the old
  search-download-OCR-extract sequence. `docs/GUIDE.md` no longer suggests
  automating a browser against cia.gov's bot filter.

## [4.3.0] - 2026-10-13

The deep-research release: cases, per-page text with offline full-text
search, citations, batch fetch, a CIA reading-room source, and per-page
OCR routing driven by the `pdf-extract` 0.7.0 engine's `--json` verdicts.
The engine binaries for this release are built separately and attached to
this release page. Known limit: cia.gov's bot filter rejects non-browser
requests to the reading room, so `--source cia` reports "blocked" today; the
documented workaround is a browser download plus `openfoia docs ingest`.

### Added

- Case notes: `openfoia case note "<text>" [--doc <id> --page N --quote "<text>"
  --tier says|infer|speculation]` records a finding on the active case,
  `case notes [--json]` lists them with citations, `case note rm <id>` removes
  one. `docs grep --save` attaches every printed hit (document, page, passage)
  to the active case; `--note "<text>"` saves them with a note. While a case
  is active `docs grep` searches only that case's documents (`--all-docs`
  for everything), `--save` never stores hits from other cases and skips
  duplicates. Database migration `004`.
- Citation export: `case export` (Markdown and JSON) now has a Findings
  section (tier, note, quoted passage, then title, DocumentCloud or other
  source id, CREST number found in the text, page with the `*` caveat when
  fewer pages are stored than the source has, retrieval date) and a Sources
  list with URLs. `case show` prints the note count.
- Batch fetch: `records fetch <id> <id> ...` and `records fetch --from
  <results.json>` (the output of `records search --json` or `--raw`) fetch
  several documents from one source, skip ids already stored unless
  `--refresh`, continue past failures and end with a fetched / skipped /
  failed summary (exit 2 when some failed, 1 when all did).
- `analyze graph --mentions "<A>" "<B>"` lists the documents in scope where
  both entity labels occur, with the pages, and the shortest path between
  them in the saved `--name` graph. Nothing is exported.
- `records search --rank child,school,gifted` scores each result by keyword
  hits in its title and snippet, sorts by the score and adds it to the table
  and to `--json` output (also with `--all-sources`).
- CIA FOIA Electronic Reading Room source (`--source cia`): `records search`
  scrapes the public site's search (title, document URL, ESDN/CREST number,
  snippet) and `records fetch <CREST | MORI | ESDN | readingroom URL> --source
  cia` downloads the PDF under the data directory, runs the normal ingest
  (text, OCR, pages) and attaches it to the active case. All requests go
  through the egress client at one request per second, print the usual
  pre-contact notice, and a bot-filter or CAPTCHA page is reported as
  "blocked", never as zero results. No new dependencies.
- Cases: `openfoia case new|list|use|show|export|close` groups one
  investigation. While a case is active, `records search` logs each query to
  it, `records fetch` and `records download --ingest` attach their documents
  to it, and `analyze graph` graphs only that case (`--case <name>` to pick
  one, `--all` for everything) and prints which scope it used. `case export`
  lists each document (title, source URL, DocumentCloud id or filename, pages,
  retrieved date) and the query log as Markdown or JSON. The active case is
  stored by id only in `active_case.json` under the data directory. Database
  migration `002`.
- An existing database is upgraded to the current schema automatically the
  first time any command opens it ("Upgrading database schema 001 -> 002" on
  stderr); no need to run `openfoia db upgrade` first.
- `records search --json` prints one compact object per result (id, title,
  source, pages, created, url, 200-character snippet), one per line.
- `records search --all-sources` queries every registered source and prints
  a status line for each: `ok N`, `error: ...`, or `skipped` when a
  `--jurisdiction`/`--type` filter does not apply to that source. Exit code
  0 when nothing errored, 2 when some sources errored, 1 when none succeeded.
- `OPENFOIA_MUCKROCK_TOKEN` supplies a MuckRock API token.
- **Page-aware document text.** Every stored document now keeps its text per
  page. `records fetch` pulls DocumentCloud's per-page text (one request per
  page, four at a time) and falls back to the full text as a single page if
  any page cannot be fetched. Locally ingested PDFs keep the page breaks from
  the PDF extractor or OCR. Documents stored before this release are kept as
  one page; re-fetch or re-ingest them for real page numbers.
- `openfoia docs grep "<query>"` — offline keyword-in-context search over
  every stored page, printing document id, title, page number and the passage
  around each match. Uses a local SQLite FTS5 index (phrases, `OR`, `NEAR`);
  on a SQLite build without FTS5 (some SQLCipher builds) it falls back to a
  plain substring scan and says so. `--context`, `--limit`, `--doc`, `--json`.
- `openfoia docs show <id> [--page N]` prints a stored document's text and
  page map; `openfoia docs list` lists stored documents with page counts,
  source and text length. Both take `--json`.
- `openfoia analyze extract --tier gliner|spacy|regex|llm` forces one
  extraction backend and fails clearly if it is not installed. The local
  tiers never call an LLM, so they also skip the slow LLM validation step.
- **Per-page extraction verdicts.** PDF text extraction now uses the
  extractor's `--json` output, which reports each page as `text_layer`,
  `garbled` or `image_only`. Only garbled and image-only pages are OCR'd
  (one page at a time, with tesseract); readable pages keep the extracted
  text. The verdict is stored per page (`ocr:<original>` once a page was
  OCR'd) and shown by `docs list` and `docs show`. Older extractors without
  `--json` still work through the form-feed path. Database migration `005`.

### Changed

- GLiNER input is split into sentence-aligned windows under its 384-word
  limit instead of 4,000-character chunks the model silently truncated, so
  names late in a long passage are no longer missed.
- Database schema migration `003` adds the `document_pages` table and its
  full-text index, applied automatically the first time a command opens the
  database.
- PDF extraction no longer gives up after a fixed 30 seconds: the limit is
  10 s plus 1 s per MB (never under 30 s, max 600 s), and hitting it prints a warning naming
  the file before falling back to OCR.

### Fixed

- GLiNER entity extraction now loads the model from the local Hugging Face
  cache without contacting huggingface.co. Only a missing cache triggers the
  one-time download, and a warning says so first.
- `records search --limit` above 25 now pages DocumentCloud (up to 100 per
  request) instead of silently stopping at 25. If a later page fails, the
  results already retrieved are still shown and the command exits non-zero.
- A MuckRock HTTP 401/403 is now an error ("MuckRock API requires an API
  token ... Set OPENFOIA_MUCKROCK_TOKEN.") instead of "No results found"; MuckRock 429 and 5xx responses are
  errors too.
- A single-source `records search` that fails (including DocumentCloud
  timeouts and rate limits) now exits non-zero in every output mode, not only
  with `--raw`.
- `template generate` printed a literal `{requester.name}` under
  "Sincerely," in standard and records-about-yourself letters.
- `OPENFOIA_AI_PROVIDER=none` now disables the LLM even when an API key is
  stored in the config file.
- `OPENFOIA_AI_PROVIDER=none` now wins over `OPENFOIA_ANTHROPIC_API_KEY`,
  `OPENFOIA_OPENAI_API_KEY` and `OPENFOIA_OLLAMA_BASE_URL`; before, setting any
  of those silently switched the LLM provider back on.
- Re-fetching a DocumentCloud document whose per-page text cannot be fetched
  keeps the pages already stored instead of collapsing them into one page.

### Internal

- Releases are automated. Merging a `release/X.Y.Z` branch into `main` tags
  the merge commit and publishes the GitHub release, with notes taken from
  this file. A check on the release pull request fails first if the branch
  name, `pyproject.toml`, `__version__` and this changelog disagree, and the
  test suite re-runs on the exact commit before it is tagged. See
  `docs/RELEASING.md`.
- `scripts/` is now covered by CI lint and format checks and the pre-commit
  hook.

## [4.2.0] - 2026-09-26

Public-records and cross-reference release (merged via PRs #73–#78). The
theme is honesty about what a search actually covered: a failed source, an
empty data directory or a coverage gap no longer reads as a clean "no
results".

### Added

- `openfoia records filings <ticker|CIK>` — a company's SEC filing history
  in reverse chronological order, read from EDGAR's submissions API including
  archived shards. Filters: `--since YYYY-MM-DD`, `--forms 10-K,10-Q`,
  `--limit`. Before anything leaves the machine it warns that the ticker/CIK
  goes to SEC, states whether egress is direct or Tor (`--tor/--no-tor`), and
  asks for confirmation (`--yes` to skip). It retrieves metadata only and never
  downloads filing documents. Remote metadata is validated; a suspicious
  `primaryDocument` falls back to the filing's index URL rather than being
  interpolated into a link. Requests are paced to stay under SEC's fair-access
  limit, and archive shards dated entirely before `--since` are not fetched.
- Cross-reference reports record a status per source per entity (`matched`,
  `checked`, `ERRORED(<kind>)`, `skipped(rate-limited)`) and a report-level
  `source_errors` map, in both the terminal summary and the `--output` JSON.

### Changed

- `records search --help` and the README list all nine searchable sources
  (previously three). The README now states plainly that each search sends its
  query to the selected third party without a confirmation prompt.
- An empty SEC search now says EDGAR full-text search can be incomplete and
  that zero results is not proof no filing exists, and shows any `--type`
  filter that was applied.

### Fixed

- **Cross-reference presented failed sources as clean no-matches.** Every
  checker swallowed errors and returned no hits, so an API outage, a rate limit
  or an unreadable ICIJ file produced the same green "No cross-reference hits
  found" as a genuine clean result. Failures are now reported per source, the
  summary lists every source that errored, and a no-hit result with errored
  sources is labelled incomplete instead of clean.
  - A partially failed source keeps the hits it found before failing: one
    unreadable ICIJ CSV no longer discards matches from the files that opened.
  - The failure kind is reported (`HTTPStatusError`, `ConnectError`, …)
    instead of a generic `RuntimeError` for every remote source.
  - An ICIJ data directory containing no CSV files is reported as
    `NoICIJData` rather than as searched-and-clean.
- `records search --raw` output is now parseable JSON only: no spinner or Rich
  markup, and control characters in upstream data are escaped. An adapter
  error or crash goes to stderr with exit code 1, so a script can tell an API
  failure from an empty result instead of receiving `[]`.

### Security

- Cross-reference warnings no longer log the entity name being looked up or
  upstream error text, which could echo it via a URL. Exported source statuses
  carry only the exception name.

### Known issue

- `records search` still sends the query to the chosen source with no warning,
  no confirmation and no Tor option, unlike `records filings` and `crossref`.
  This is documented in the README; closing it will change CLI behavior for
  existing scripts and is tracked as a separate decision.

## [4.1.0] - 2026-08-11

Additive release on top of the 4.0.0 security hardening. No security
regressions: the 4.0.0 network egress choke point, graph escaping, CDN
removal, and fail-closed installer verification are all preserved.

### Added

- A Claude Code plugin (`plugin/`) — an `openfoia` skill plus four slash
  commands (`/foia-install`, `/foia-search`, `/foia-investigate`,
  `/foia-graph`). Install with
  `/plugin marketplace add JordanCoin/openfoia` then
  `/plugin install openfoia@openfoia`. Every documented `openfoia ...`
  invocation is validated against the live 4.0.0 command tree, including the
  new `egress-status` command.
- `openfoia --version` (also `-V`), sourced from `openfoia.__version__`.
- `openfoia analyze graph --no-text` — export the entity graph without
  embedding document bodies. When text *is* included, the command now warns
  that the export is plaintext and lives outside the encrypted database.
- `CHANGELOG.md` (this file) and a web-UI section in `docs/AIRGAP.md`.

### Fixed

- **Installer could mistake a checksum file for the binary.** `install.sh`
  matched release assets by unanchored substring, so `pdf-extract-<platform>`
  also matched `pdf-extract-<platform>.sha256`. With no guaranteed asset
  ordering, the installer could have downloaded the checksum file and installed
  it as the extractor. The match is now anchored to the exact asset name. This
  sits on top of 4.0.0's fail-closed checksum verification.
- The benchmark's graph writer (`tests/benchmark_extraction.py`) spliced raw
  JSON into an inline `<script>` via `JSON.parse('...')`, which both broke on
  the apostrophes and quotes FOIA data is full of (`O'Brien`,
  `Prince George's County`) and re-opened the same injection vector 4.0.0
  closed in the main renderer. It now uses `escape_json_for_script`.

### Internal

- `pyyaml` added to the `dev` extra — `tests/test_plugin.py` parses plugin
  frontmatter and imported it without it being declared.
- `tests/test_plugin.py` resolves every documented plugin invocation against
  the real command tree, and fails if a top-level command is undocumented in
  the skill.
- `tests/test_security.py` pins the graph escaping and the absence of external
  resources in the web UI, complementing the broader 4.0.0 security suite.
- `test_graph.html`, a generated benchmark artifact, is no longer tracked.

## [4.0.0] - 2026-08-09

Security hardening release (merged via PRs #64 and #65). A parallel
security/OPSEC pass; the notes below describe what it changed, factually.

### Security

- **Fail-closed network egress choke point.** A single egress layer
  (`openfoia/net.py`) routes outbound requests and can force everything through
  Tor's SOCKS5 proxy; if the requested policy cannot be honored the request
  fails rather than silently going out direct. Crossref and web archiving were
  routed through it, and the tool's outbound fingerprint was dropped.
- **`egress-status` command.** Reports honestly whether traffic goes DIRECT or
  via Tor, whether the Tor proxy is actually reachable, and what is and is not
  protected.
- **Graph HTML could execute code from document text.** Untrusted document text
  and entity labels were spliced into the graph's `<script>` block; a FOIA
  response containing a literal `</script>` broke out and ran arbitrary
  JavaScript on `file://`. `escape_json_for_script` now escapes `<`, `>`, `&`,
  and U+2028/U+2029. Regenerate any `graph.html` produced before this release.
- **The web UI loaded JavaScript from a CDN.** `openfoia serve` pulled Tailwind
  from `cdn.tailwindcss.com` on every page load, disclosing your IP and the
  timing of your sessions. Replaced with a stylesheet served from disk; the web
  UI now makes zero external requests, sends a restrictive Content-Security-
  Policy header, rejects non-loopback `Host` headers, and keeps the auth token
  out of the URL and the `Referer`.
- **Metadata, resource caps, and at-rest hardening.** Metadata stripping was
  broadened, downloads and extraction gained size/resource caps, and database
  file permissions were tightened. The gateways (email, fax, mail) were
  hardened.
- **Installer verification fails closed.** `install.sh` verifies the
  pdf-extract binary against a published `.sha256` and refuses to proceed when
  it cannot verify, rather than warning and continuing.

## [3.2.2] - 2026-04-10

### Added

- LLM validation now reports keep/remove counts and surfaces errors instead of
  failing quietly.

### Changed

- The extraction warning distinguishes a local AI provider from a cloud one, so
  "AI is running" no longer reads the same whether or not documents are leaving
  the machine.

## [3.2.1] - 2026-04-10

### Added

- Cassette-compatible LLM routing and selectable PDF extraction profiles.
- Multi-backend extraction pipeline with mention merging and an `--ensemble`
  mode.
- LLM used as a validator rather than an extractor, plus junk filtering and
  OCR-aware fuzzy merging (477 → 333 entities on the benchmark).
- `--model` flag and Qwen3 support.

### Fixed

- Crossref rate limiting, deduplication, error handling, progress output, and a
  ProPublica 404.
- Forced re-extraction (`--force`).

## [3.2.0] - 2026-03-24

### Added

- DocumentCloud adapter and an interactive document reader in the graph view.
- Multi-layer MuckRock search with cleaner table display.
- MSG email support and file-type display in search results.

### Fixed

- spaCy auto-download.
- Web upload text extraction. (The upload path was later routed through the safe
  ingest API and CSP-hardened in 4.0.0.)
- Portable-mode config, request-send persistence, and agent draft handling.

## [3.1.1] - 2026-03-23

### Added

- Python CI, a pre-commit hook running ruff lint and format, and `CLAUDE.md`
  with the project's mission and principles.

### Fixed

- ruff lint and format across the codebase.

## [3.1.0] - 2026-03-23

### Security

- Duress mode redesigned: no stored password hash, an encrypted decoy database,
  and opaque filenames.
- Honest security messaging, a written threat model, and install checksums.
- Addressed seven findings from an adversarial review.

### Fixed

- LLM-extracted entities are validated against the source text.
- MuckRock search uses tags (the API has no full-text search).
- The install script searches all releases for the pdf-extract binary.

## [3.0.1] - 2026-03-22

### Changed

- Core install is lightweight; heavy packages are opt-in extras.
- Every missing-dependency error now names the `openfoia install-extras`
  command that fixes it.

### Added

- Portable install — the entire app lives on the USB stick.

### Fixed

- Install uses an isolated venv rather than polluting the system Python.

## [3.0.0] - 2026-03-22

Baseline for this changelog. Earlier tags (`v0.0.1` through `v2.0.0`,
2026-02-19 to 2026-03-22) predate it; see the git history for details.

[4.5.0]: https://github.com/JordanCoin/openfoia/compare/v4.4.0...v4.5.0
[4.4.0]: https://github.com/JordanCoin/openfoia/compare/v4.3.1...v4.4.0
[4.3.1]: https://github.com/JordanCoin/openfoia/compare/v4.3.0...v4.3.1
[4.3.0]: https://github.com/JordanCoin/openfoia/compare/v4.2.0...v4.3.0
[4.2.0]: https://github.com/JordanCoin/openfoia/compare/v4.1.0...v4.2.0
[4.1.0]: https://github.com/JordanCoin/openfoia/compare/v4.0.0...v4.1.0
[4.0.0]: https://github.com/JordanCoin/openfoia/compare/v3.2.2...v4.0.0
[3.2.2]: https://github.com/JordanCoin/openfoia/compare/v3.2.1...v3.2.2
[3.2.1]: https://github.com/JordanCoin/openfoia/compare/v3.2.0...v3.2.1
[3.2.0]: https://github.com/JordanCoin/openfoia/compare/v3.1.1...v3.2.0
[3.1.1]: https://github.com/JordanCoin/openfoia/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/JordanCoin/openfoia/compare/v3.0.1...v3.1.0
[3.0.1]: https://github.com/JordanCoin/openfoia/compare/v3.0.0...v3.0.1
[3.0.0]: https://github.com/JordanCoin/openfoia/releases/tag/v3.0.0
