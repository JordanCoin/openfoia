#!/usr/bin/env bash
set -euo pipefail

# OpenFOIA uninstaller
# Usage: curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/uninstall.sh | bash

info()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m%s\033[0m\n' "$*"; }

DATA_DIR="$HOME/.openfoia"
BIN_DIR="${OPENFOIA_INSTALL_DIR:-$HOME/.local/bin}"

main() {
    info ""
    info "  OpenFOIA Uninstaller"
    info ""

    # Remove Python package
    if command -v pipx &>/dev/null; then
        pipx uninstall openfoia 2>/dev/null && ok "Removed openfoia (pipx)" || true
    fi
    pip3 uninstall openfoia -y 2>/dev/null && ok "Removed openfoia (pip)" || true

    # Remove pdf-extract binary
    if [ -f "${BIN_DIR}/pdf-extract" ]; then
        rm -f "${BIN_DIR}/pdf-extract"
        ok "Removed ${BIN_DIR}/pdf-extract"
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

    echo ""
    ok "OpenFOIA uninstalled."
    info ""
}

main "$@"
