# Changelog

All notable changes to OpenFOIA are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries before 4.0.0 are backfilled from git history and are summaries, not
exhaustive lists.

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
