# Portable Encrypted USB Install

Run OpenFOIA entirely from an encrypted USB drive. No traces are left on the
host machine (assuming no swap and careful use).

## What You Need

- A USB drive (16 GB or larger recommended).
- A host machine with Python 3.11+ and a USB port.
- (Optional) LUKS/VeraCrypt for full-disk encryption of the USB.

## Option A: LUKS-Encrypted USB (Linux)

### 1. Prepare the USB

```bash
# Identify the USB device (e.g., /dev/sdb) -- BE VERY CAREFUL
lsblk

# Create a LUKS-encrypted partition
sudo cryptsetup luksFormat /dev/sdb1
sudo cryptsetup luksOpen /dev/sdb1 openfoia_usb
sudo mkfs.ext4 /dev/mapper/openfoia_usb
sudo mount /dev/mapper/openfoia_usb /mnt/openfoia
sudo chown $USER:$USER /mnt/openfoia
```

### 2. Install OpenFOIA on the USB

```bash
python3 -m venv /mnt/openfoia/venv
source /mnt/openfoia/venv/bin/activate
pip install openfoia
```

### 3. Configure Data Directory

```bash
export OPENFOIA_DATA_DIR="/mnt/openfoia/data"
openfoia init
```

### 4. Run

```bash
source /mnt/openfoia/venv/bin/activate
export OPENFOIA_DATA_DIR="/mnt/openfoia/data"
openfoia serve
```

### 5. Unmount Safely

```bash
sudo umount /mnt/openfoia
sudo cryptsetup luksClose openfoia_usb
```

## Option B: VeraCrypt (Cross-Platform)

VeraCrypt works on Linux, macOS, and Windows.

### 1. Create a VeraCrypt Volume

1. Download and install [VeraCrypt](https://www.veracrypt.fr/).
2. Create an encrypted volume on the USB drive:
   - Select "Create an encrypted file container" or encrypt the entire partition.
   - Choose AES-256 + SHA-512.
   - Set a strong passphrase.

### 2. Mount and Install

Mount the VeraCrypt volume (let's say it mounts at `/Volumes/OPENFOIA` on
macOS or `V:\` on Windows):

```bash
# macOS / Linux
python3 -m venv /Volumes/OPENFOIA/venv
source /Volumes/OPENFOIA/venv/bin/activate
pip install openfoia
export OPENFOIA_DATA_DIR="/Volumes/OPENFOIA/data"
openfoia init
```

```powershell
# Windows
python -m venv V:\venv
V:\venv\Scripts\activate
pip install openfoia
set OPENFOIA_DATA_DIR=V:\data
openfoia init
```

### 3. Dismount

Always dismount the VeraCrypt volume when done. This re-encrypts everything.

## Option C: macOS Encrypted Disk Image

```bash
# Create a 2 GB encrypted sparse image
hdiutil create -size 2g -encryption AES-256 -type SPARSE \
    -fs APFS -volname OPENFOIA ~/openfoia.sparseimage

# Mount it
hdiutil attach ~/openfoia.sparseimage

# Install
python3 -m venv /Volumes/OPENFOIA/venv
source /Volumes/OPENFOIA/venv/bin/activate
pip install openfoia
export OPENFOIA_DATA_DIR="/Volumes/OPENFOIA/data"
openfoia init
```

## Helper Script

Create a `run.sh` on the USB for convenience:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export OPENFOIA_DATA_DIR="$SCRIPT_DIR/data"

source "$SCRIPT_DIR/venv/bin/activate"
openfoia "$@"
```

Then: `chmod +x run.sh` and use `./run.sh serve`, `./run.sh request list`, etc.

## Security Considerations

- **Swap**: Disable swap on the host machine or use encrypted swap. Sensitive
  data in RAM can be written to swap on disk.
  - Linux: `sudo swapoff -a`
  - macOS: Swap is encrypted by default when FileVault is enabled.
- **Temp files**: Python may write temp files to `/tmp`. On Linux, `/tmp` is
  usually tmpfs (RAM-backed). On macOS, set `TMPDIR` to a directory on the
  encrypted USB if concerned.
- **Browser cache**: When using `openfoia browse`, Playwright stores browser
  profiles in a temp directory. Use `--headless` and the profiles are cleaned
  up on exit.
- **USB forensics**: Even with LUKS/VeraCrypt, wear-leveling on flash storage
  means deleted data may persist in spare blocks. For maximum security, use a
  hardware-encrypted USB drive (e.g., Apricorn Aegis, Kingston IronKey).

## Duress Mode on USB

```bash
openfoia init --password REAL_SECRET --duress-password DECOY_SECRET
```

If forced to decrypt the USB and open OpenFOIA, use the duress password.
The decoy database with innocuous data will be shown instead.
