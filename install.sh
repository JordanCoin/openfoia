#!/usr/bin/env bash
set -euo pipefail

# OpenFOIA installer
# Usage: curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/install.sh | bash
#
# Portable USB install (note `-s --`: without it bash eats the flag and you
# get a NON-portable install that writes data to the host machine):
#   cd /Volumes/MY_USB
#   curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/install.sh | bash -s -- --portable

REPO="JordanCoin/openfoia"

info()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m%s\033[0m\n' "$*"; }
die()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; exit 1; }

# --- Detect portable mode ---
PORTABLE=false
MINIMAL=false
SKIP_VERIFY=false
for arg in "$@"; do
    case "$arg" in
        --portable) PORTABLE=true ;;
        --minimal)  MINIMAL=true ;;
        --insecure-skip-verify) SKIP_VERIFY=true ;;
    esac
done
if [ -f ".openfoia-portable" ] || [ -n "${OPENFOIA_DATA_DIR:-}" ]; then
    PORTABLE=true
fi

# --- Set paths based on mode ---
if [ "$PORTABLE" = true ]; then
    # Everything on the USB / current directory
    BASE_DIR="$(pwd)"
    INSTALL_DIR="${BASE_DIR}/bin"
    VENV_DIR="${BASE_DIR}/.openfoia-venv"
    DATA_DIR="${BASE_DIR}/openfoia-data"

    # Create portable marker
    touch "${BASE_DIR}/.openfoia-portable"
else
    # Standard install — home directory
    BASE_DIR="$HOME"
    INSTALL_DIR="${OPENFOIA_INSTALL_DIR:-$HOME/.local/bin}"
    VENV_DIR="${OPENFOIA_VENV_DIR:-$HOME/.openfoia-venv}"
    DATA_DIR="$HOME/.openfoia"
fi

# --- Detect platform ---
detect_platform() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"

    case "$os" in
        Linux)  os="linux" ;;
        Darwin) os="macos" ;;
        MINGW*|MSYS*|CYGWIN*) os="windows" ;;
        *) die "Unsupported OS: $os" ;;
    esac

    case "$arch" in
        x86_64|amd64)  arch="x86_64" ;;
        aarch64|arm64) arch="aarch64" ;;
        *) die "Unsupported architecture: $arch" ;;
    esac

    echo "${os}-${arch}"
}

# --- Download binary from latest release ---
download_binary() {
    local platform="$1"
    local name="pdf-extract-${platform}"
    [ "$platform" = "windows-x86_64" ] && name="${name}.exe"

    info "Downloading pdf-extract for ${platform}..."

    local url
    # Check all releases for binaries (they live on whichever release the
    # glyph-api CI pushed them to — not necessarily the latest release).
    #
    # Match the EXACT asset name: anchor on the leading '/' and the closing
    # quote. Releases also carry a "<name>.sha256" asset, and an unanchored
    # substring match hits both — with JSON asset order not guaranteed, the
    # installer could download the checksum file, chmod +x it, and install
    # that as the binary. The API lists releases newest-first, so head -1
    # still picks the most recent release carrying this platform's binary.
    local name_re
    name_re=$(printf '%s' "$name" | sed 's/[][\.*^$/]/\\&/g')
    url=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases" \
        | grep -Eo "\"browser_download_url\"[[:space:]]*:[[:space:]]*\"[^\"]*/${name_re}\"" \
        | head -1 \
        | cut -d '"' -f 4) || true

    if [ -z "$url" ]; then
        warn "No pdf-extract binary found for ${platform} in latest release."
        warn "PDF text extraction will be unavailable. OCR (tesseract) will be used instead."
        return 1
    fi

    mkdir -p "$INSTALL_DIR"
    curl -fsSL "$url" -o "${INSTALL_DIR}/pdf-extract"

    # Verify SHA256 checksum if .sha256 file exists in the release
    local sha_url="${url}.sha256"
    local expected_checksum
    expected_checksum=$(curl -fsSL "$sha_url" 2>/dev/null | awk '{print $1}') || true

    if [ -n "$expected_checksum" ]; then
        local actual_checksum
        if command -v sha256sum &>/dev/null; then
            actual_checksum=$(sha256sum "${INSTALL_DIR}/pdf-extract" | awk '{print $1}')
        elif command -v shasum &>/dev/null; then
            actual_checksum=$(shasum -a 256 "${INSTALL_DIR}/pdf-extract" | awk '{print $1}')
        elif [ "$SKIP_VERIFY" = true ]; then
            warn "No sha256sum or shasum found — verification skipped (--insecure-skip-verify)."
            actual_checksum="$expected_checksum"
        else
            rm -f "${INSTALL_DIR}/pdf-extract"
            die "No sha256sum or shasum available to verify the download. \
This binary runs with your environment, including OPENFOIA_DB_PASSWORD. \
Install coreutils, or re-run with --insecure-skip-verify to accept the risk."
        fi

        if [ "$actual_checksum" != "$expected_checksum" ]; then
            rm -f "${INSTALL_DIR}/pdf-extract"
            die "Checksum mismatch for pdf-extract! Expected ${expected_checksum}, got ${actual_checksum}. Aborting."
        fi
        ok "Checksum verified (SHA256)."
    elif [ "$SKIP_VERIFY" = true ]; then
        warn "No .sha256 in release — verification skipped (--insecure-skip-verify)."
    else
        rm -f "${INSTALL_DIR}/pdf-extract"
        die "No .sha256 published for this release, so the download cannot be verified. \
Re-run with --insecure-skip-verify to accept the risk, or skip the optional \
pdf-extract binary entirely (OpenFOIA falls back to pure-Python extraction)."
    fi

    chmod +x "${INSTALL_DIR}/pdf-extract"
    ok "Installed pdf-extract to ${INSTALL_DIR}/pdf-extract"
}

# --- Install Python package ---
install_python() {
    info "Installing openfoia..."

    # Find Python 3
    local python=""
    for candidate in python3 python; do
        if command -v "$candidate" &>/dev/null; then
            version=$("$candidate" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                python="$candidate"
                break
            fi
        fi
    done

    if [ -z "$python" ]; then
        die "Python 3.11+ not found. Install Python first: https://python.org"
    fi

    info "Using $python ($($python --version))"

    # Determine extras
    # Default: core only (~30MB). GLiNER adds ~2.3GB (PyTorch + model).
    # Let the user opt in — don't surprise them with a 2GB download.
    local extras=""
    if [ "$MINIMAL" = false ]; then
        # Check if user explicitly wants NER
        for arg in "$@"; do
            case "$arg" in
                --ner) extras="[ner]"; info "Including GLiNER entity extraction (~2GB download)" ;;
                --all) extras="[all]"; info "Full install (all optional features)" ;;
            esac
        done
    fi
    if [ -z "$extras" ]; then
        info "Core install (~30MB). For entity extraction: openfoia install-extras ner"
    fi

    local pkg="git+https://github.com/${REPO}.git"

    # For non-portable: try pipx first
    if [ "$PORTABLE" = false ] && command -v pipx &>/dev/null; then
        pipx install "${pkg}${extras}"
        ok "Installed via pipx"
        return
    fi

    # Create isolated venv
    info "Creating isolated environment at ${VENV_DIR}..."
    "$python" -m venv "$VENV_DIR"
    "${VENV_DIR}/bin/pip" install --upgrade pip -q
    "${VENV_DIR}/bin/pip" install "${pkg}${extras}"

    # Symlink the openfoia command
    mkdir -p "$INSTALL_DIR"
    ln -sf "${VENV_DIR}/bin/openfoia" "${INSTALL_DIR}/openfoia"

    ok "Installed openfoia CLI (venv at ${VENV_DIR})"
}

# --- Ensure PATH ---
ensure_path() {
    case ":$PATH:" in
        *":${INSTALL_DIR}:"*) return ;;
    esac

    if [ "$PORTABLE" = true ]; then
        warn "Add to your PATH for this session:"
        warn "  export PATH=\"${INSTALL_DIR}:\$PATH\""
        return
    fi

    local shell_rc=""
    if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "zsh" ]; then
        shell_rc="$HOME/.zshrc"
    elif [ -n "${BASH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "bash" ]; then
        shell_rc="$HOME/.bashrc"
    fi

    if [ -n "$shell_rc" ]; then
        echo "export PATH=\"${INSTALL_DIR}:\$PATH\"" >> "$shell_rc"
        warn "Added ${INSTALL_DIR} to PATH in ${shell_rc}. Restart your shell or run:"
        warn "  export PATH=\"${INSTALL_DIR}:\$PATH\""
    fi
}

# --- Main ---
main() {
    info ""
    if [ "$PORTABLE" = true ]; then
        info "  OpenFOIA — PORTABLE install (everything stays here)"
        info "  Location: $(pwd)"
    else
        info "  OpenFOIA — your data never leaves your machine"
    fi
    info ""

    platform=$(detect_platform)
    info "Platform: ${platform}"

    install_python
    download_binary "$platform" || true
    ensure_path

    # Initialize database
    mkdir -p "$DATA_DIR"
    if [ "$PORTABLE" = true ]; then
        export OPENFOIA_DATA_DIR="$DATA_DIR"
    fi
    if command -v "${INSTALL_DIR}/openfoia" &>/dev/null; then
        "${INSTALL_DIR}/openfoia" init 2>/dev/null || true
    elif command -v openfoia &>/dev/null; then
        openfoia init 2>/dev/null || true
    fi

    echo ""
    ok "OpenFOIA installed."
    info ""
    if [ "$PORTABLE" = true ]; then
        info "  PORTABLE MODE — everything is in $(pwd)"
        info ""
        info "  To use:  export PATH=\"${INSTALL_DIR}:\$PATH\""
        info "           openfoia guide"
        info ""
        info "  Unplug the USB and nothing remains on the host."
    else
        info "  openfoia guide          Quickstart walkthrough"
        info "  openfoia serve          Start the local web UI"
        info "  openfoia request new    File a FOIA request"
        info "  openfoia --help         See all commands"
        info ""
        info "  All data stored in ${DATA_DIR}"
    fi
    info ""
    info "  To uninstall: curl -fsSL https://raw.githubusercontent.com/${REPO}/main/uninstall.sh | bash"
    info ""
}

main "$@"
