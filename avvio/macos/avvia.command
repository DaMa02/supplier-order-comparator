#!/bin/bash
# ---------------------------------------------------------------------------
# Avvio del comparatore su macOS: trova un Python adatto (comune.sh) e avvia
# app/launcher.py dalla radice del repository, sulla porta 8765.
# ---------------------------------------------------------------------------

main() {
    qui="$(cd "$(dirname "$0")" && pwd)"
    # avvio/macos → avvio → la radice del repository
    BASE="$(cd "$qui/../.." && pwd)"

    # shellcheck source=comune.sh
    source "$qui/comune.sh"

    echo "COMPARATORE ORDINI"
    echo ""

    avvia_comparatore "$BASE" 8765 "$@"
}

main "$@"
exit $?
