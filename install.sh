#!/usr/bin/env bash
set -euo pipefail

# OpenFOIA installer
# Usage: curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/install.sh | bash

REPO="JordanCoin/openfoia"
INSTALL_DIR="${OPENFOIA_INSTALL_DIR:-$HOME/.local/bin}"
DATA_DIR="$HOME/.openfoia"

info()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m%s\033[0m\n' "$*"; }
die()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; exit 1; }

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

    if command -v pipx &>/dev/null; then
        pipx install "git+https://github.com/${REPO}.git"
    elif command -v pip3 &>/dev/null; then
        pip3 install --user "git+https://github.com/${REPO}.git"
    elif command -v pip &>/dev/null; then
        pip install --user "git+https://github.com/${REPO}.git"
    else
        die "Python pip not found. Install Python 3.11+ first."
    fi

    ok "Installed openfoia CLI"
}

# --- Ensure PATH ---
ensure_path() {
    case ":$PATH:" in
        *":${INSTALL_DIR}:"*) return ;;
    esac

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
    info "  OpenFOIA — your data never leaves your machine"
    info ""

    platform=$(detect_platform)
    info "Platform: ${platform}"

    install_python
    download_binary "$platform" || true
    ensure_path

    # Initialize database
    mkdir -p "$DATA_DIR"
    if command -v openfoia &>/dev/null; then
        openfoia init 2>/dev/null || true
    fi

    echo ""
    ok "OpenFOIA installed."
    info ""
    info "  openfoia serve          Start the local web UI"
    info "  openfoia request new    File a FOIA request"
    info "  openfoia --help         See all commands"
    info ""
    info "  All data stored in ~/.openfoia/"
    info "  To uninstall: curl -fsSL https://raw.githubusercontent.com/${REPO}/main/uninstall.sh | bash"
    info ""
}

main "$@"
