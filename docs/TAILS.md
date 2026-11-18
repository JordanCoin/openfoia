# Running OpenFOIA on Tails OS

Tails is a Debian-based live operating system that routes all traffic through
Tor by default. It is designed for privacy-sensitive work and pairs well with
OpenFOIA for journalists and researchers working on sensitive FOIA requests.

## Prerequisites

- A USB stick with [Tails](https://tails.net) installed (8 GB minimum).
- **Persistent Storage** enabled in Tails (Settings > Persistent Storage).
  This is required to keep your database, configuration, and documents across
  reboots.

## Setup

### 1. Enable Persistent Storage

On first boot, open **Settings > Persistent Storage** and enable at least:

- **Personal Data** (stores files in `/home/amnesia/Persistent`)
- **Additional Software** (lets you install Python packages that survive reboot)
- **Dotfiles** (optional, for shell aliases)

Reboot after enabling persistence.

### 2. Install OpenFOIA

Tails ships Python 3 but does not include pip in the default environment.
Install with `--user` to avoid needing root:

```bash
# Install pip if not already available
sudo apt update && sudo apt install -y python3-pip python3-venv

# Create a virtual environment on persistent storage
python3 -m venv ~/Persistent/openfoia-env
source ~/Persistent/openfoia-env/bin/activate

# Install OpenFOIA
pip install openfoia
```

To persist the virtual environment across reboots, the `~/Persistent/` directory
is automatically retained by Tails persistent storage.

### 3. Configure the Data Directory

Point OpenFOIA at your persistent volume so data survives reboot:

```bash
export OPENFOIA_DATA_DIR="$HOME/Persistent/openfoia-data"
```

Add this to `~/.bashrc` (or the Tails dotfiles persistence) so it is set
automatically:

```bash
echo 'export OPENFOIA_DATA_DIR="$HOME/Persistent/openfoia-data"' >> ~/.bashrc
```

### 4. Initialize the Database

```bash
source ~/Persistent/openfoia-env/bin/activate
openfoia init
```

For encrypted-at-rest storage (recommended even on Tails):

```bash
pip install 'openfoia[encryption]'
openfoia init --encrypt          # prompts for the passphrase
```

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

### 5. (Optional) Set Up Duress Mode

```bash
openfoia init --encrypt --duress   # prompts for the real and the duress passphrase
```

Duress mode requires the real database to be encrypted; `init` refuses to set
up a decoy next to a plaintext database. To add a decoy to an existing
database, run `openfoia db encrypt` first, then `openfoia init --duress`.

If compelled to open OpenFOIA, use the duress passphrase. It will show a
decoy database with bland, non-sensitive FOIA requests. It is a decoy, not
deniability: see `docs/THREAT_MODEL.md`.

## Using OpenFOIA on Tails

```bash
source ~/Persistent/openfoia-env/bin/activate
export OPENFOIA_DATA_DIR="$HOME/Persistent/openfoia-data"

# Start the local server (opens Tor Browser by default on Tails)
openfoia serve --browser tor

# Or use the CLI directly
openfoia request list
openfoia browse https://example.com --tor --save
```

## Tor Browsing

Tails routes ALL traffic through Tor automatically, so the `--tor` flag on
`openfoia browse` is redundant on Tails. However, it still applies the
fingerprint hardening (WebGL/WebRTC disabled, common user-agent).

## Security Notes

- **Persistent Storage encryption**: Tails encrypts the persistent volume with
  LUKS. Combined with OpenFOIA's optional SQLCipher encryption, your data has
  two layers of protection.
- **Amnesia**: Without persistent storage, everything is wiped on shutdown.
  This is a feature, not a bug.
- **No swap**: Tails disables swap to prevent sensitive data from being written
  to disk unencrypted.
- **Emergency shutdown**: Pulling the USB stick immediately shuts down Tails
  and wipes RAM. Your persistent data remains encrypted on the USB.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `pip: command not found` | `sudo apt install python3-pip` |
| Database not found after reboot | Ensure `OPENFOIA_DATA_DIR` is set to a persistent path |
| `playwright` errors | Playwright requires a display; use `--headless` on Tails |
| Slow Tor browsing | Expected; Tor adds latency. Use `--headless` to reduce overhead |
