#!/usr/bin/env bash
set -euo pipefail

# OpenFOIA installer
# Usage: curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/install.sh | bash
#
# Portable USB install:
#   cd /Volumes/MY_USB
#   curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/install.sh | bash --portable

REPO="JordanCoin/openfoia"

info()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m%s\033[0m\n' "$*"; }
die()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; exit 1; }

# --- Detect portable mode ---
PORTABLE=false
if [ "${1:-}" = "--portable" ] || [ -f ".openfoia-portable" ] || [ -n "${OPENFOIA_DATA_DIR:-}" ]; then
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
    url=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
        | grep "browser_download_url.*${name}" \
        | head -1 \
        | cut -d '"' -f 4) || true

    if [ -z "$url" ]; then
        warn "No pdf-extract binary found for ${platform} in latest release."
        warn "PDF text extraction will be unavailable. OCR (tesseract) will be used instead."
        return 1
    fi

    mkdir -p "$INSTALL_DIR"
    curl -fsSL "$url" -o "${INSTALL_DIR}/pdf-extract"
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

    # For non-portable: try pipx first
    if [ "$PORTABLE" = false ] && command -v pipx &>/dev/null; then
        pipx install "git+https://github.com/${REPO}.git"
        ok "Installed via pipx"
        return
    fi

    # Create isolated venv
    info "Creating isolated environment at ${VENV_DIR}..."
    "$python" -m venv "$VENV_DIR"
    "${VENV_DIR}/bin/pip" install --upgrade pip -q
    "${VENV_DIR}/bin/pip" install "git+https://github.com/${REPO}.git"

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
