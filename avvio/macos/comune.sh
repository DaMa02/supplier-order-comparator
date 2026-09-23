# Parte comune all'avvio per macOS. Non si lancia da solo: lo legge
# avvia.command, che sta in questa stessa cartella.
# Il compito e' trovare un Python adatto e un Chrome, poi passare la mano
# ad app/launcher.py, che e' lo stesso identico file che gira su Windows.

trova_python() {
    # Serve Python 3.10 o successivo, con openpyxl: senza quello il programma
    # non legge i listini. Si prende il primo che soddisfa entrambe le cose.
    # E' lo stesso criterio della sonda di Windows (`AVVIA_COMPARATORE.ps1`).
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
    # Vero se su questa porta risponde IL comparatore, non un programma
    # qualunque che ha preso la porta.
    #
    # ⚠ La differenza e' tutta la guardia. Chi chiama questa funzione la usa
    # per decidere se saltare l'allineamento, e una guardia che dicesse
    # «occupata» per un programma qualsiasi spegnerebbe gli aggiornamenti di
    # questa cartella finche' quel programma resta acceso: e' lo stesso difetto
    # della guardia sull'albero sporco, gia' scartata per questa ragione.
    #
    # `/api/health` risponde `{"ok": true, ...}` e non chiede nessun token: e'
    # la stessa rotta con cui `app/launcher.py` riconosce un server acceso.
    #
    # Nel dubbio si risponde di no, cosi' l'allineamento si fa: se la sonda
    # sbaglia si torna al comportamento che c'era prima, che e' l'errore che
    # costa meno.
    porta="$1"
    command -v curl >/dev/null 2>&1 || return 1
    risposta="$(curl --silent --max-time 2 "http://127.0.0.1:${porta}/api/health" 2>/dev/null)" || return 1
    case "$risposta" in
        *'"ok"'*true*) return 0 ;;
        *) return 1 ;;
    esac
}

avvia_comparatore() {
    # $1 = cartella del programma, $2 = prima porta da provare
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

    # Su Windows il programma cerca chrome.exe in Program Files: qui quel
    # percorso non esiste, quindi glielo diciamo noi. Se Chrome manca, il
    # launcher ripiega da solo sul browser predefinito.
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
