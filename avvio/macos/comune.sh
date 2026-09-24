# Shared macOS startup logic. Not run directly: sourced by avvia.command,
# in this same folder.
# Finds a suitable Python and Chrome, then hands off to app/launcher.py,
# the same file that runs on Windows.

trova_python() {
    # Needs Python 3.10+ with openpyxl: without it the program can't read
    # price lists. Picks the first candidate that satisfies both. Same
    # criteria as the Windows probe (`AVVIA_COMPARATORE.ps1`).
    for candidato in \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        "$(command -v python3 2>/dev/null)"
    do
        [ -x "$candidato" ] || continue
        "$candidato" - <<'PROVA' >/dev/null 2>&1 || continue
import sys
assert sys.version_info >= (3, 10)
import openpyxl
PROVA
        printf '%s' "$candidato"
        return 0
    done
    return 1
}

il_comparatore_risponde() {
    # True if THIS comparator is answering on this port, not just some
    # other program that happens to hold it.
    #
    # The check matters: whatever calls this function uses it to decide
    # whether to skip the sync, and a guard that said "busy" for any
    # program would disable updates to this folder for as long as that
    # program stays running.
    #
    # `/api/health` responds `{"ok": true, ...}` with no token required:
    # it's the same route `app/launcher.py` uses to recognize a running
    # server.
    #
    # When in doubt this answers no, so the sync proceeds: a wrong guess
    # here just falls back to the previous behavior, which is the cheaper
    # mistake.
    porta="$1"
    command -v curl >/dev/null 2>&1 || return 1
    risposta="$(curl --silent --max-time 2 "http://127.0.0.1:${porta}/api/health" 2>/dev/null)" || return 1
    case "$risposta" in
        *'"ok"'*true*) return 0 ;;
        *) return 1 ;;
    esac
}

avvia_comparatore() {
    # $1 = program folder, $2 = first port to try
    cartella="$1"
    porta="$2"
    shift 2

    python_scelto="$(trova_python)" || {
        echo ""
        echo "  [ERRORE] Non trovo un Python 3.10+ con openpyxl su questo Mac."
        echo "           Ho controllato anaconda, l'ambiente tesi-pa, homebrew e il PATH."
        echo "           Non ho installato niente."
        echo ""
        read -r -p "Premi Invio per chiudere"
        exit 1
    }

    # On Windows the program looks for chrome.exe in Program Files: that
    # path doesn't exist here, so it's passed explicitly. If Chrome is
    # missing, the launcher falls back to the default browser on its own.
    chrome_mac="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    [ -f "$chrome_mac" ] && export COMPARATORE_CHROME="$chrome_mac"

    echo "[OK] Uso $python_scelto"
    echo ""
    cd "$cartella" || exit 1
    "$python_scelto" app/launcher.py --port "$porta" "$@"
    esito=$?

    if [ $esito -ne 0 ]; then
        echo ""
        echo "  [ERRORE] Il comparatore non e' partito. Il motivo e' scritto qui sopra."
        echo ""
        read -r -p "Premi Invio per chiudere"
    fi
    exit $esito
}
