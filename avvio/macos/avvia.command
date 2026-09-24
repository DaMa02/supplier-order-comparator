#!/bin/bash
# macOS startup: finds a suitable Python (comune.sh) and launches
# app/launcher.py from the repository root, on port 8765.

main() {
    qui="$(cd "$(dirname "$0")" && pwd)"
    # avvio/macos -> avvio -> repository root
    BASE="$(cd "$qui/../.." && pwd)"

    # shellcheck source=comune.sh
    source "$qui/comune.sh"

    echo "COMPARATORE ORDINI"
    echo ""

    avvia_comparatore "$BASE" 8765 "$@"
}

main "$@"
exit $?
