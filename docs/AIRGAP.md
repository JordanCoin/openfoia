# Air-Gapped Deployment

OpenFOIA can run completely offline on an air-gapped machine -- no network
connection required after initial setup. This guide covers the overall approach
and links to platform-specific instructions.

## Why Air-Gap?

An air-gapped deployment means the machine running OpenFOIA has no network
connection. This eliminates:

- Remote exfiltration of your FOIA data
- Network-based surveillance of your research activity
- Accidental data leaks through telemetry, DNS, or background services

It is the strongest operational security posture available.

## Architecture

```
┌─────────────────────────────────────────┐
│         Air-Gapped Machine              │
│                                         │
│  ┌─────────────┐   ┌────────────────┐   │
│  │  OpenFOIA   │   │  SQLite DB     │   │
│  │  CLI/Server │──▶│  (encrypted)   │   │
│  └─────────────┘   └────────────────┘   │
│         │                               │
│         ▼                               │
│  ┌─────────────┐   ┌────────────────┐   │
│  │  OCR/PDF    │   │  Documents     │   │
│  │  Pipeline   │──▶│  (local only)  │   │
│  └─────────────┘   └────────────────┘   │
│                                         │
└─────────────────────────────────────────┘
         │
    USB transfer
    (sneakernet)
         │
         ▼
┌─────────────────────────────────────────┐
│      Online Machine (transfer only)     │
│                                         │
│  Download documents, send requests,     │
│  then transfer via encrypted USB        │
└─────────────────────────────────────────┘
```

## Quick Start

### 1. Prepare an Encrypted USB

See [USB.md](USB.md) for detailed instructions on creating an encrypted USB
with LUKS, VeraCrypt, or a macOS encrypted disk image.

### 2. Install OpenFOIA Offline

On an online machine, download the packages:

```bash
mkdir openfoia-offline
pip download openfoia -d openfoia-offline/
```

Copy the `openfoia-offline/` directory to the encrypted USB.

On the air-gapped machine:

```bash
python3 -m venv /path/to/usb/venv
source /path/to/usb/venv/bin/activate
pip install --no-index --find-links=/path/to/usb/openfoia-offline openfoia
```

### 3. Set the Data Directory

```bash
export OPENFOIA_DATA_DIR="/path/to/usb/data"
openfoia init --password YOUR_SECRET
```

### 4. Transfer Documents via Sneakernet

1. On the online machine, download FOIA response documents.
2. Copy them to the encrypted USB.
3. On the air-gapped machine, ingest them:

```bash
openfoia docs ingest /path/to/usb/incoming/response.pdf
```

### 5. Send Requests via Sneakernet

1. Draft requests on the air-gapped machine:
   ```bash
   openfoia request new --agency FBI --subject "..." --name "..." --email "..."
   ```
2. Export the request text to a file on the USB.
3. On the online machine, send via email/fax/mail.

## Platform-Specific Guides

| Platform | Guide | Notes |
|----------|-------|-------|
| **Tails OS** | [TAILS.md](TAILS.md) | Debian-based live OS with built-in Tor. Ideal for journalist work. |
| **Encrypted USB** | [USB.md](USB.md) | Portable install on LUKS/VeraCrypt/macOS encrypted volume. |
| **Any Linux** | This document | Follow the quick start above. |

## Key Configuration

### OPENFOIA_DATA_DIR

The most important setting for air-gapped deployments. This environment
variable tells OpenFOIA where to store all data (database, documents,
exports, config). Set it to a path on your encrypted USB or persistent
volume:

```bash
export OPENFOIA_DATA_DIR="/mnt/encrypted-usb/openfoia"
```

Without this variable, OpenFOIA defaults to `~/.openfoia/` in the user's
home directory.

### Database Encryption

Even on an encrypted volume, enabling SQLCipher adds a second layer:

```bash
pip install 'openfoia[encryption]'
openfoia init --password YOUR_SECRET
```

### Duress Mode

```bash
openfoia init --password YOUR_SECRET --duress-password INNOCENT_PASSWORD
```

If compelled to open OpenFOIA, use the duress password to reveal only a
decoy database with bland FOIA requests about weather data and park
statistics.

## AI/LLM on Air-Gapped Machines

OpenFOIA supports [Ollama](https://ollama.ai) for local LLM inference.
On an air-gapped machine:

1. Download the Ollama binary and a model (e.g., `llama3.2`) on an online
   machine.
2. Transfer to the air-gapped machine via USB.
3. Run Ollama locally -- no internet required.

```bash
ollama serve &
openfoia config --init  # Select "ollama" as the AI provider
```

## The Web UI Works Fully Offline

`openfoia serve` needs no internet. It binds to `127.0.0.1`, serves a single
self-contained HTML page, and fetches nothing from any external host -- the
stylesheet is hand-written and inlined, there are no web fonts, no CDN
scripts, and no analytics. The only external URL anywhere on the page is a
link to the project's GitHub repo in the footer, which does nothing unless
you click it.

This was not true before v4.0.0: the page loaded Tailwind CSS from
`cdn.tailwindcss.com`, so every page load made a DNS lookup and a TLS request
to a third party, and the UI was close to unreadable without one. If you are
running an older version on an air-gapped machine, expect a broken-looking
interface -- and on a networked machine, expect the request. Upgrade.

Entity graphs (`openfoia analyze graph --view`) are self-contained too: a
single HTML file with inline CSS and JS that opens from `file://` with no
network access at all.

## Security Checklist

- [ ] Air-gapped machine has no WiFi/Ethernet/Bluetooth enabled
- [ ] USB drive is encrypted (LUKS, VeraCrypt, or hardware encryption)
- [ ] `OPENFOIA_DATA_DIR` points to the encrypted volume
- [ ] Database encryption enabled (`--password`)
- [ ] Duress mode configured (`--duress-password`)
- [ ] Swap disabled or encrypted on the air-gapped machine
- [ ] Ollama running locally for AI features (no cloud API keys)
- [ ] Running v4.0.0 or later (earlier versions load CSS from a CDN on every
      `openfoia serve` page load)
- [ ] Physical security of the USB drive when not in use
