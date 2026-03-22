#!/usr/bin/env bash
set -euo pipefail

# OpenFOIA uninstaller
# Usage: curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/uninstall.sh | bash
#
# For portable installs: run from the USB directory

info()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m%s\033[0m\n' "$*"; }

# Detect portable mode
PORTABLE=false
if [ -f ".openfoia-portable" ]; then
    PORTABLE=true
fi

if [ "$PORTABLE" = true ]; then
    DATA_DIR="$(pwd)/openfoia-data"
    VENV_DIR="$(pwd)/.openfoia-venv"
    BIN_DIR="$(pwd)/bin"
else
    DATA_DIR="$HOME/.openfoia"
    VENV_DIR="${OPENFOIA_VENV_DIR:-$HOME/.openfoia-venv}"
    BIN_DIR="${OPENFOIA_INSTALL_DIR:-$HOME/.local/bin}"
fi

main() {
    info ""
    if [ "$PORTABLE" = true ]; then
        info "  OpenFOIA Uninstaller (portable — $(pwd))"
    else
        info "  OpenFOIA Uninstaller"
    fi
    info ""

    # Remove Python package (pipx — only for standard installs)
    if [ "$PORTABLE" = false ] && command -v pipx &>/dev/null; then
        pipx uninstall openfoia 2>/dev/null && ok "Removed openfoia (pipx)" || true
    fi

    # Remove venv
    if [ -d "$VENV_DIR" ]; then
        rm -rf "$VENV_DIR"
        ok "Removed venv ${VENV_DIR}"
    fi

    # Remove symlinks and binaries
    for f in "${BIN_DIR}/openfoia" "${BIN_DIR}/pdf-extract"; do
        if [ -f "$f" ] || [ -L "$f" ]; then
            rm -f "$f"
            ok "Removed $f"
        fi
    done

    # Remove bin dir if empty (portable only)
    if [ "$PORTABLE" = true ] && [ -d "$BIN_DIR" ]; then
        rmdir "$BIN_DIR" 2>/dev/null || true
    fi

    # Remove data
    if [ -d "$DATA_DIR" ]; then
        echo ""
        warn "Data directory found: ${DATA_DIR}"
        printf "  Delete all OpenFOIA data (database, documents, config)? [y/N] "
        read -r answer
        if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
            rm -rf "$DATA_DIR"
            ok "Removed ${DATA_DIR}"
        else
            info "Kept ${DATA_DIR}. Remove manually with: rm -rf ${DATA_DIR}"
        fi
    fi

    # Remove portable marker
    if [ "$PORTABLE" = true ] && [ -f ".openfoia-portable" ]; then
        rm -f ".openfoia-portable"
        ok "Removed portable marker"
    fi

    echo ""
    ok "OpenFOIA uninstalled. No traces left."
    info ""
}

main "$@"
