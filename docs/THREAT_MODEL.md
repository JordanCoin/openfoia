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
- **Tor-routed fetches.** When you use `--tor`, web requests are routed through
  Tor's SOCKS5 proxy so the target server does not see your IP.
- **Encrypted database.** `openfoia db encrypt` encrypts the SQLite database at
  rest with a password you choose.
- **Secure delete (best-effort).** `openfoia purge --secure` overwrites files
  3x with random data before deletion. On HDDs this is effective. On SSDs it is
  unreliable due to wear-leveling (see Known Limitations).
- **Duress / decoy database.** A secondary database can be presented under
  coercion.

---

## What OpenFOIA Does NOT Protect

- **Swap files and virtual memory.** Your OS may write portions of in-memory
  data (documents, extracted text, passwords) to swap/pagefile at any time.
  OpenFOIA cannot prevent or erase this.
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
  `openfoia purge --secure` attempts to scrub these, but other shells or
  session managers may retain copies.
- **Network-level surveillance.** Even with Tor, traffic analysis by a global
  adversary may correlate timing. Without Tor, your ISP sees which FOIA portals
  you visit.

---

## When Data Leaves Your Machine

OpenFOIA is local-first, but certain features make network requests:

| Feature | Destination | What is sent |
|---|---|---|
| `openfoia request send` | Agency FOIA portal / email gateway | Your FOIA request text, your contact info |
| `--tor` fetches | Tor network, then target server | The URL you are fetching (visible to exit node) |
| `openfoia analyze crossref` | CrossRef API (`api.crossref.org`) | DOI or bibliographic query terms |
| Cloud AI summarization (opt-in) | Configured LLM API (OpenAI, etc.) | Document text sent to the API endpoint |
| `openfoia serve` | `localhost` only | Nothing leaves the machine, but browser records local activity |
| `install.sh` | GitHub API, GitHub releases | Your IP address; what binary you download |

If you never use `--tor`, send commands, crossref, or cloud AI, no data leaves
your machine during normal operation.

---

## Known Limitations

1. **SSD secure delete is theater.** On solid-state drives, overwriting a file
   does not guarantee the old data is erased. Use full-disk encryption (LUKS,
   FileVault, BitLocker) instead.
2. **SQLite WAL files.** SQLite's write-ahead log (`data.db-wal`) may contain
   recent writes even if the main database is encrypted. `purge --secure`
   handles these, but a crash before purge could leave WAL remnants.
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
- Both profiles encrypted (no plaintext decoy)
- Opaque filenames (profile_0.db, profile_1.db)

What it does NOT provide:
- Protection against forensic analysis (two encrypted files are visible)
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
   openfoia init --password <real-secret>
   ```

3. **Tails OS** (maximum): Boot from Tails USB, which is amnesic — forgets
   everything on shutdown. See `docs/TAILS.md`.
