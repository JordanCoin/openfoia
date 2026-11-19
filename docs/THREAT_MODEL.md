# OpenFOIA Threat Model

This document describes what OpenFOIA protects, what it does not, and where
honest limitations exist. Read this before relying on OpenFOIA for sensitive
investigations.

---

## What OpenFOIA Protects

- **Local-first data storage.** Your FOIA requests, ingested documents,
  entities, and analysis live in a SQLite database on your machine
  (`~/.openfoia/data.db`). Nothing is uploaded to a server by default.
- **Offline analysis.** Document ingestion, PDF text extraction, entity
  extraction (GLiNER), and the entity graph all run locally.
- **Tor-routed fetches, crossref, and records lookups.** With `--tor` (or
  `network.tor: true` / `OPENFOIA_TOR=1` in config), `openfoia ingest`,
  `openfoia crossref`, and records lookups route through Tor's SOCKS5 proxy
  with a non-identifying browser User-Agent and a fresh circuit per request
  (stream isolation via unique SOCKS credentials), so the destination server
  does not see your real IP. **This hides who is asking, not what is
  asked**: the request content — the URL you fetch, or the subject names you
  cross-reference — still reaches the destination either way. Run
  `openfoia egress-status` to see the current policy and whether the Tor
  proxy is actually reachable right now.
- **Sending requests over Tor.** In Tor mode `openfoia request send` sends
  email (SMTP through the Tor SOCKS proxy, the server name resolved at the
  exit), fax (Twilio's API) and physical mail (Lob's API) through Tor, or not
  at all: it checks the Tor proxy first and stops if it is down, and a send
  that Tor or the provider refuses fails instead of being retried directly.
  `--direct` sends one request without Tor, and says so. Tor hides your IP
  address from the mail server, Twilio or Lob; see "When Data Leaves Your
  Machine" for what still identifies you.
- **Deleted database content is overwritten.** Deleted rows are zeroed in the
  database file, the full-text index is merged so a deleted page's words go
  too, and SQLite's rollback journal is kept as a file that is overwritten
  with zeros after each session instead of being deleted with the old content
  still in it. See Known Limitations 2 and 6 for what this does not reach.
- **Encrypted database.** `openfoia db encrypt` encrypts the SQLite database at
  rest with a password you choose. It shreds the plaintext original and its
  WAL/journal files in place — no plaintext backup is kept.
  **Note:** this encrypts the *database* (requests, entities, extracted text).
  Ingested source documents under `~/.openfoia/docs/` and archived web pages
  are stored as ordinary files and are **not** encrypted by this command. Use
  full-disk encryption or an encrypted USB for those.
- **Secure delete (best-effort).** `openfoia purge --secure` overwrites files
  3x with random data before deletion. On HDDs this is effective. On SSDs it is
  unreliable due to wear-leveling (see Known Limitations). It covers the whole
  data directory, including `exports/`, where graphs, FollowTheMoney exports,
  entity CSVs and `records download` files go by default, and any file you
  wrote elsewhere with `--output`/`--csv` (those paths are recorded in
  `exports_manifest.json` in the data directory, and the purge prompt lists
  them). A file it cannot overwrite does not stop the purge: it is deleted
  without overwriting if possible, and purge ends by listing every file that
  was not overwritten or could not be removed, and exits with status 1. It
  does not remove the original files you ingested, copies you made or moved
  yourself, or exports that earlier versions wrote to the current
  directory (`graph.json`, `entities.ftm.json`, `./downloads/`). Fast purge
  (`--yes` without `--secure`) only unlinks.
- **Duress / decoy database.** A secondary database can be presented under
  coercion.

---

## What OpenFOIA Does NOT Protect

- **Swap files and virtual memory.** Your OS may write portions of in-memory
  data (documents, extracted text, passwords) to swap/pagefile at any time.
  OpenFOIA cannot prevent or erase this.
- **Metadata in formats it cannot clean.** Ingest strips metadata from the
  stored copy of PDF, DOCX and common image files, and of many more formats
  when mat2 is installed. Anything else (for example legacy `.doc`, Outlook
  `.msg`, or `.odt` without mat2) is stored with the author, device, location
  and timestamps it arrived with; ingest says so for each file. Stripping is
  best effort even where it runs: neither OpenFOIA nor mat2 can promise a file
  is clean. The files you ingest are never modified, and email attachments and
  archived web pages are stored as received.
- **Filesystem journals.** Journaling filesystems (ext4, APFS, NTFS) may retain
  metadata or partial file contents in their journal even after deletion.
- **macOS Spotlight and thumbnail caches.** macOS indexes file contents
  (Spotlight) and generates thumbnail previews that persist independently of the
  original files.
- **SSD wear-leveling and TRIM.** On SSDs, secure overwrite is unreliable.
  The drive's firmware may retain old copies of data blocks. Full-disk
  encryption is the only reliable defense.
- **Browser history from `openfoia serve`.** The local web UI runs at
  `http://localhost:...`. Your browser records this in its history, cache,
  and potentially in saved form data.
- **Shell history.** Commands you type (including `openfoia request new ...`)
  are recorded in `~/.bash_history`, `~/.zsh_history`, etc.
  `openfoia purge --secure` removes every line that mentions `openfoia` or runs
  `run.sh` (the docs/USB.md wrapper) from the bash, zsh, sh/ksh and fish
  history files and from `$HISTFILE`, overwriting the old bytes. It cannot
  reach the shell you run it from: that shell keeps the session's commands in
  memory and writes them back when it exits (purge tells you how to stop
  that). Other shells, terminal scrollback, `tmux`/`screen` logs and session
  managers may retain copies. A passphrase typed as an argument
  (`--password VALUE`) is in history until it is removed; use the prompting
  forms (`--encrypt`, `--duress`, `db encrypt`).
- **Network-level surveillance.** Without Tor, your ISP (and every network hop
  in between) sees which FOIA portals, records APIs, and websites you visit,
  from your real IP. **With Tor, the destination endpoint still receives the
  request content** — the URL you fetch, or the subject names sent to
  `crossref` — Tor hides who is asking, not what is asked. A global passive
  adversary able to watch both your connection into Tor and the traffic
  leaving the exit node can still correlate timing to link a request back to
  you; this is a known limitation of Tor generally, not something OpenFOIA
  adds or removes. DNS resolution for `.onion` and plain hostnames happens
  through the SOCKS proxy (not locally) when Tor is used, but that is a
  routing detail, not an anonymity guarantee.

---

## When Data Leaves Your Machine

OpenFOIA is local-first, but certain features make network requests:

| Feature | Destination | What is sent |
|---|---|---|
| `openfoia request send` | Your mail server (SMTP), Twilio (fax) or Lob (mail), and through them the agency | Your FOIA request text and contact info (name, email, return address), and your SMTP login and password or your Twilio or Lob API key. In Tor mode this goes through Tor or is not sent; the provider then sees a Tor exit address, but your login or key (and the billing account behind it) and the name on the request identify you to it whatever the route. Many mail providers refuse Tor; `--direct` sends one request from your real IP |
| `openfoia ingest` (web fetch/archive) | The URL's host | The URL you are fetching. Without `--tor`, from your real IP; the exit node also sees it when `--tor` is used |
| `openfoia search`, `openfoia records search` | The public-records sources searched (DocumentCloud, MuckRock, SEC EDGAR, OpenCorporates, USAspending, NARA and the others in the records registry) | Your search query, from your real IP unless Tor is used (`--egress tor` or `network.tor`). Sources that need your own key (NARA, CourtListener, OCCRP Aleph, UK Companies House) also receive it, which ties the query to your account with that provider whatever the route. `openfoia search` lists the sources and asks first; with Tor it fails closed and does not send personal API keys, so those sources are skipped |
| `openfoia crossref` | MuckRock, OpenCorporates, SEC EDGAR, DocumentCloud, OpenSanctions, USASpending, and other configured record sources | **The names of the people and organizations you are investigating** (the subject names extracted from your documents), from your real IP unless `--tor` is used. CLI warns and asks you to confirm before this happens; `--sources icij,opensanctions-local` stays fully offline. The OpenSanctions API is contacted only with a personal key (never over Tor); without one it is listed as not contacted and reported as errored, not as clean |
| OpenSanctions in `openfoia search`, `records search` and `records fetch` | Nobody when the bulk file has been downloaded; otherwise api.opensanctions.org | With the bulk file on disk nothing is sent: it is not listed as contacted and needs no confirmation. Without it, the name (in a POST body, which OpenSanctions says it does not log; GET URLs are kept 90 days) and your personal API key, which identifies your account; not used over Tor. Each name screened uses one credit on that account |
| `openfoia records opensanctions-data --download` (explicit, asks first) | data.opensanctions.org | Your IP address (the Tor exit node's with `network.tor`) and that you downloaded the sanctions list. No names, no API key |
| `openfoia docs embed-model` (semantic search, opt-in, once) | huggingface.co and the storage host it redirects to | Your IP address (the Tor exit node's with `--tor`) and which model files you download. Nothing from your documents or searches. Embedding and `docs grep --semantic` then run offline |
| First entity extraction with the GLiNER tier (once per model: English about 1.6 GB, multilingual about 1.25 GB) | huggingface.co and the storage host it redirects to | Your IP address and which model files you download, **not through Tor**. Nothing from your documents. A warning is logged first. With `network.tor` on the download is refused and extraction continues without that model; `HF_HUB_OFFLINE=1` also makes an uncached model fail instead. Extraction then runs offline |
| AI entity extraction with a provider not on this machine (opt-in) | The configured LLM API (Anthropic, OpenAI, an OpenAI-compatible server, or Ollama on another host) | Document text, from your real IP. Only `localhost` and loopback addresses count as this machine. OpenFOIA warns, then asks; without a yes (or `ai.allow_remote: true`) it uses the local tiers. Never used in Tor mode, because these requests cannot be routed through Tor |
| `openfoia serve` | `localhost` only | Nothing leaves the machine, but browser records local activity |
| `openfoia mcp serve` (opt-in, optional extra, started by an MCP client) | The MCP client that started it over stdin/stdout, **and through it the client's model provider** (Anthropic for Claude Desktop and Claude Code; whoever runs the model for other clients) | **Everything the tools return**: passages and full page text of stored documents, case names, descriptions and notes, the queries logged to a case, entity names, FOIA request subjects. The local tools themselves make no network request. `openfoia_search` also sends its query to the records sources, exactly as `openfoia search` does, but only when the server was started with `--allow-network` and only on a second, `confirmed` call. See "MCP server" below |
| `install.sh` | GitHub API, GitHub releases | Your IP address; what binary you download |

If you never use `send`, `ingest`, `search`, `crossref`, `docs embed-model`,
cloud AI, an MCP client with a remote model, or a GLiNER model that is not yet
downloaded,
no data leaves your machine during normal operation. `--tor` (or the `network.tor` config
default) changes *who the destination sees* for `ingest`, `crossref`, `search`
and `request send` — it does not change *what* they receive: the URL, the
subject names or the request still arrive at the destination either way.

---

## Known Limitations

1. **SSD secure delete is theater.** On solid-state drives, overwriting a file
   does not guarantee the old data is erased. Use full-disk encryption (LUKS,
   FileVault, BitLocker) instead.
2. **SQLite journal files.** SQLite's rollback journal (`data.db-journal`)
   holds the original content of every page a write changes, so a delete
   copies the deleted text into it. SQLite normally deletes the journal after
   every transaction, leaving that content in the filesystem's free space.
   OpenFOIA instead keeps it as a persistent file (`journal_mode=PERSIST`; it
   never turns on WAL) and, after every database session, overwrites the whole
   file with zeros, flushes it to disk and truncates it, holding an exclusive
   lock so no other process still needs it. Temporary tables, sorts and
   statement journals are kept in memory (`temp_store=MEMORY`), not in temp
   files. With an encrypted database the journal is encrypted too. What
   remains:
   - If OpenFOIA is killed between a write and the scrub, or another process
     (such as `openfoia serve`) is writing at that moment, the old content
     stays in `data.db-journal` until the next session scrubs it.
     `purge --secure` and `db encrypt` shred it either way.
   - Overwriting a file in place does not reach copies the storage keeps
     elsewhere: SSD wear-levelling, copy-on-write filesystems (btrfs, ZFS,
     APFS), filesystem journals that log file data, snapshots and backups.
   - Versions before this one let SQLite delete the journal, so journals
     written by them may still be recoverable from free space.
   Full-disk encryption covers all three.
3. **Python process memory.** Sensitive data (passwords, document text) exists
   in Python process memory while OpenFOIA is running. A memory dump or core
   dump could expose it.
4. **Dependency supply chain.** OpenFOIA installs Python packages from PyPI and
   a Rust binary from GitHub releases. A compromised dependency could exfiltrate
   data. The install script verifies SHA256 checksums for the pdf-extract
   binary, but pip packages are not individually verified beyond PyPI's own
   checks.
5. **No forward secrecy for the database.** If an attacker obtains a copy of
   your encrypted database and later obtains your password, they can decrypt
   everything. There is no per-session key rotation.
6. **Deleted rows inside the database file.** Every connection runs with
   SQLite's `secure_delete` on, so deleted rows (page text, entities, notes,
   embeddings) are overwritten with zeros, in their page and when a page is
   freed. The full-text index keeps a deleted or replaced page's words, with
   their positions (enough to rebuild the text), in older index segments until
   they are merged; after a session that deleted or replaced page text,
   OpenFOIA merges the whole index, which frees and zeroes the old segments.
   The test suite deletes a document and replaces a document's pages in a
   plaintext database and checks that neither the text nor its words remain in
   the bytes of `data.db` or `data.db-journal`. The cost: about 15% slower
   ingest, and deleting or replacing a document's pages took 0.3 s instead of
   0.01 s on a 10,000-page database on the test machine (the index merge grows
   with the size of the index). What remains: rows deleted by earlier versions
   stay in free pages until SQLite reuses them; the file itself is subject to
   the same storage caveats as Limitation 2; and `purge --secure` remains the
   way to destroy everything.
7. **Other accounts on the same machine and the web UI.** `openfoia serve`
   opens the browser at a login link (`/login?nonce=...`). Any local account
   can read a browser's command line, so the link is single-use and expires
   after 60 seconds; the browser swaps it for an HttpOnly, SameSite=Strict
   session cookie, and the session token itself never appears in a URL or on
   a command line. This narrows the window, it does not close it: a program
   already watching the process list can race the browser to the link. If
   that happens the browser shows "Login link already used" and the terminal
   prints a warning: stop the server and start it again. An account that can
   read your memory or attach a debugger to your processes is out of scope.
8. **What portable mode keeps off the host.** With a `.openfoia-portable`
   marker, the database, config, documents, and the caches of optional model
   libraries (Hugging Face, torch, matplotlib, and anything that honours
   `XDG_CACHE_HOME`) live in `openfoia-data/` on the stick, and the browser
   started by `openfoia browse` gets a throwaway home directory there too.
   OpenFOIA is built not to create files in the host's home directory (the
   test suite checks `init`, `docs ingest`, `analyze graph` and `purge`). It
   cannot control the operating system: swap, temp files under `/tmp` or `TMPDIR`
   (Playwright puts its browser profile there), the browser history of
   `openfoia serve`, shell history, and OS indexing (Spotlight, recent-files
   lists) can still record activity on the host. On macOS and Windows, a
   library that ignores `XDG_CACHE_HOME` (pip, for one, uses
   `~/Library/Caches` or `%LOCALAPPDATA%`) may still write there during
   `openfoia install-extras`.

9. **Downloads from links in API responses.** File links that records sources
   return (MuckRock files, DocumentCloud text, NARA, CourtListener, Federal
   Register and CIA PDFs) must be HTTPS to a public host. In direct mode the
   host name is resolved, every address must be public, and the connection is
   made to an address from that same answer, with TLS still verified against
   the host name, so a DNS server that answers a public address to the check
   and 127.0.0.1 to the connection (DNS rebinding) cannot point a download at
   this machine or your network. Not covered: when an `HTTPS_PROXY` or
   `ALL_PROXY` from the environment is in use, the proxy resolves the name;
   in Tor mode nothing is resolved locally and the Tor exit's policy is what
   refuses private addresses.

## Semantic search embeddings

`docs grep --semantic` (optional extra, `openfoia install-extras semantic`)
stores an embedding — a list of numbers computed from the text — for every
40-word passage of every stored page. **Treat embeddings as sensitive as the
text they came from.** They are derived data that can leak content: published
"embedding inversion" attacks reconstruct much of the original text from
transformer embeddings, and the static embeddings used here are close to an
average of word vectors, which reveals which words a passage contains.

What OpenFOIA does about it:
- Embeddings live in the same database file as the page text
  (`document_page_vectors`), so `db encrypt` / SQLCipher encrypts them with
  everything else, and `purge` destroys them with the file. sqlite-vec works
  inside an encrypted database (tested with sqlcipher3-binary); it never needs
  a second, unencrypted index.
- They are deleted with their page: database triggers remove a page's
  embeddings when the page is deleted, replaced (re-OCR, re-fetch) or its text
  changes, and when its document is deleted. Foreign keys cascade as a second
  line. `openfoia docs embed --drop` removes every embedding and keeps the text.
- The model is downloaded once, explicitly, by `openfoia docs embed-model`
  (see the table above), with pinned file hashes, into the data directory
  (`models/`, portable mode respected), not into a shared `~/.cache`. Search
  never touches the network.

What it does not do:
- The duress decoy profile has no embeddings, but the `models/` folder sits in
  the shared data directory either way, so its presence shows that semantic
  search was set up on this machine.
- Embeddings deleted outside `--drop` can linger in free space inside the
  database file (Known Limitation 6).
- Semantic search ranks by similarity; it does not prove a page is absent.
  Every page gets a score, and on the project's test corpus it found reworded
  passages in its top ten only some of the time
  (`docs/benchmarks/semantic-search.md`). Do not rely on it to clear a
  document.

---

## MCP server

`openfoia mcp serve` (optional extra, `openfoia install-extras mcp`) lets an
MCP client such as Claude Desktop or Claude Code call OpenFOIA as tools. The
server is local; the model behind the client usually is not.

What it does:
- **stdio only.** The client starts the server as a child process and talks
  over its stdin and stdout. It never listens on a network port, so nothing on
  your network can reach it.
- **Network is the operator's choice.** `openfoia_search` sends nothing unless
  the server was started with `--allow-network`, a command-line flag in the
  client's configuration that no tool can set. With it, the first call returns
  the same warning as `openfoia search` (sources, Tor or direct) and sends
  nothing; the query goes out on a second call with `confirmed: true`. A call
  cannot move a configured Tor route (`network.tor`) to direct, and Tor fails
  closed. These are the same function and the same checks as the agent
  interface's `search_records` tool.
- **Read-only, no paths.** The other tools only read the database. There are no
  tools that delete, purge, send FOIA requests, send email, fax or mail, ingest
  files or change config, and no tool takes a file path, so a prompt-injected
  document cannot make the agent read `~/.ssh` or the config file through
  OpenFOIA. Unknown arguments are refused. The local tools default to the
  active case, as the CLI does.
- **stdout carries only protocol messages.** The server keeps the real stdout
  for the protocol and points the process's own stdout (and stdin) at the null
  device, so stray library output cannot corrupt a message or end up in the
  client's logs. Logs go to stderr with only the message and the error type,
  never the query or document text, unless `--verbose`.
- **No telemetry.** The MCP SDK wraps each request in an OpenTelemetry span
  (method and tool name). OpenFOIA removes that middleware, so a machine with
  OpenTelemetry configured does not export it.

What it does not do:
- **Everything a tool returns goes to the model provider.** Passages, whole
  pages, case notes, your logged queries and the names in your entity graph
  become part of the conversation, which the client sends to its model
  provider and which may be retained under that provider's terms. OpenFOIA
  cannot see or control what happens there. If a document must not leave the
  machine, do not read it through an agent, or use a client with a local model.
- **`confirmed` is set by the model, not by you.** The tool description and the
  skill tell the agent to show you the warning and to confirm only after you
  agree, but OpenFOIA cannot verify that a human agreed. A prompt-injected or
  careless model can send `confirmed: true` on its own. What does not depend on
  the model: network off without `--allow-network`, Tor stays Tor, Tor fails
  closed, no destructive or sending tools. Keep your client's per-tool approval
  prompt on for `openfoia_search`.
- **The client config is the switch.** Anyone, or any agent with file-editing
  tools (Claude Code has them), who can edit the client's MCP configuration can
  add `--allow-network`. Putting `OPENFOIA_DB_PASSWORD` in that configuration
  stores your database password there in plaintext.
- **Client logs.** MCP clients keep a server's stderr in log files. The default
  log lines carry no content, but `--verbose` can log queries and page text.
- **Document text can address the model.** Tool descriptions label all results
  as untrusted data, never instructions, but a model can still be manipulated
  by text it reads. The gates above are what limit the damage.

---

## Recommendations

- **Use full-disk encryption.** This is the single most important step. It
  protects against swap, journals, caches, and SSD remnants all at once.
  - macOS: FileVault (enabled by default on modern Macs)
  - Linux: LUKS full-disk encryption at install time
  - Windows: BitLocker
- **Run from a Tails USB** for maximum isolation. Tails routes all traffic
  through Tor and uses an amnesic filesystem that forgets everything on
  shutdown. See `docs/TAILS.md`.
- **Use the portable install** on an encrypted USB drive if Tails is not
  practical. See `docs/USB.md`.
- **Do not use cloud AI features** for sensitive documents. Once text is sent to
  an API, you cannot control its retention.
- **Clear your browser history** after using `openfoia serve`, or use a private
  / incognito window.
- **Disable Spotlight indexing** for `~/.openfoia/`:
  ```
  mdutil -i off ~/.openfoia
  ```
  Or add `~/.openfoia` to System Settings > Siri & Spotlight > Privacy.
- **Review shell history** periodically, or configure your shell to not record
  commands prefixed with a space (`HISTCONTROL=ignorespace`).

## Duress Mode — Honest Limitations

OpenFOIA includes a "decoy profile" feature: a second password opens an
innocent-looking database. **This is NOT plausible deniability.** A forensic
examiner can determine that two encrypted database files exist on the device.

What the decoy profile provides:
- Buys time during casual device inspections
- No password hash stored anywhere — SQLCipher verifies the password directly
- Both profiles encrypted (no plaintext decoy). If SQLCipher is unavailable,
  `openfoia init --duress-password` now fails rather than writing a plaintext
  decoy that would offer no protection at all. It also refuses when the real
  database is not encrypted (earlier versions reported success and left the real
  investigation as a readable SQLite file next to an encrypted decoy), and it
  only reports success after checking both files on disk.
- The decoy is built the way `openfoia init` builds a real database: the same
  migrations, full-text index and agency list, owner-only (0600) like the real
  file. Its requests, documents and entities are drawn at random from bland
  topics and spread over the past months, so two decoys differ.
- Opaque filenames (`profile_0.db`, `profile_1.db`). Enabling duress mode
  migrates the real database into slot 0, so neither filename reveals which
  profile is real. Before duress mode is configured the database is the
  ordinary `data.db`.

What it does NOT provide:
- Protection against forensic analysis (two encrypted files are visible)
- A decoy that survives someone who has read the source. The generator
  (`seed_decoy_db` in `openfoia/security.py`) is public, with its topic and
  place lists. An examiner who has the duress passphrase can compare the decoy
  with that code; a small database of exactly those topics says "decoy". The
  decoy's documents are stored under `docs/` as ordinary unencrypted files,
  like real ingested documents, so their text is readable without any
  passphrase. Adding your own innocuous activity under the duress passphrase
  makes it less obvious, not undetectable.
- Protection if the real password is found in RAM, swap, or shell history
- Believable cover under sustained interrogation

**For real coercion resistance**, use one of these approaches:

1. **VeraCrypt hidden volume** (best): Store your real `~/.openfoia/` inside
   a VeraCrypt hidden volume. One password opens the outer volume (decoy),
   another opens the hidden volume (real data). The hidden volume is
   cryptographically indistinguishable from free space.

2. **Encrypted USB + portable mode** (practical): Keep the real investigation
   on an encrypted USB drive using `openfoia portable`. The host machine only
   has the decoy profile. Unplug the USB and the real data is gone.
   ```
   cd /Volumes/ENCRYPTED_USB
   openfoia portable
   openfoia init --encrypt
   ```

3. **Tails OS** (maximum): Boot from Tails USB, which is amnesic — forgets
   everything on shutdown. See `docs/TAILS.md`.
