"""Gli script che avviano il programma.

Sono la parte del progetto che **nessun collaudo guardava**: il lanciatore di
Windows e l'avvio per macOS.  Vivono fuori da `app/` e da `scripts/`, e
proprio per questo l'unica prova che avevano era qualcuno che li lanciava a
mano.  Il 20 agosto 2026 quelle prove a mano sono state fatte davvero — e
sono servite, perche' una guardia scritta nel modo ovvio si era rivelata
quella sbagliata — ma una prova fatta a mano non sopravvive alla sessione in
cui e' stata fatta.

Che cosa NON c'e' qui, e perche'.  `AVVIA_COMPARATORE.ps1` non si prova:
richiede PowerShell, che sul Mac di chi sviluppa non c'e'.  Quel file lo copre
la CI, che gira su Windows e almeno lo parsifica.  Qui sta la meta'
provabile: lo script `sh`/`bash`.

⚠ Le prove di questo file eseguono processi veri (`bash`, `curl`) su porte
locali.  Dove uno di quegli strumenti manca, la classe si salta invece di
fallire: e' la stessa regola che questa suite applica gia' a Node.
"""

from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import threading
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
AVVIO_MACOS = SKILL_ROOT / "avvio" / "macos"


def _serve(nome: str) -> str | None:
    return shutil.which(nome)


class ServizioFinto:
    """Un server HTTP che risponde quello che gli si dice, su una porta libera."""

    def __init__(self, corpo: bytes | None, stato: int = 200) -> None:
        self.corpo = corpo
        self.stato = stato
        finto = self

        class Gestore(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - lo impone http.server
                if finto.corpo is None:
                    self.send_error(404)
                    return
                self.send_response(finto.stato)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(finto.corpo)))
                self.end_headers()
                self.wfile.write(finto.corpo)

            def log_message(self, *_argomenti) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gestore)
        self.porta = self.server.server_address[1]
        self.filo = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "ServizioFinto":
        self.filo.start()
        return self

    def __exit__(self, *_argomenti) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.filo.join(timeout=5)


@unittest.skipIf(_serve("bash") is None, "senza bash non si possono eseguire gli avvii per macOS")
class GliScriptDiAvvioSiLeggono(unittest.TestCase):
    """La prima cosa che deve reggere: che bash li sappia leggere.

    E' il gemello del passo che la CI fa su PowerShell.  Un errore di sintassi
    qui vuol dire «il programma non parte», e non lo direbbe nessun'altra prova.
    """

    def test_gli_script_non_hanno_errori_di_sintassi(self) -> None:
        for nome in ("comune.sh", "avvia.command"):
            with self.subTest(script=nome):
                esito = subprocess.run(
                    [_serve("bash"), "-n", str(AVVIO_MACOS / nome)],
                    capture_output=True, text=True,
                )
                self.assertEqual(esito.returncode, 0, esito.stderr)


@unittest.skipIf(_serve("bash") is None, "senza bash non si può eseguire l'avvio per macOS")
class IlFileInEsecuzioneNonSiFaRiscrivereSotto(unittest.TestCase):
    """`avvia.command` sta DENTRO l'albero che `AVVIA_COMPARATORE.ps1` allinea
    su Windows: sul Mac non fa `reset --hard` da solo, ma resta comunque un
    file che un aggiornamento del repository puo' riscrivere mentre gira.
    La difesa e' che tutto stia dentro una funzione — bash deve leggerne la
    definizione per intero prima di poterla eseguire — e che dopo la chiamata
    ci sia un `exit`, cosi' non torna a leggere il file cambiato.

    Non e' una prova di stile: e' la sola cosa che tiene, e si rompe
    silenziosamente se qualcuno aggiunge una riga in fondo.
    """

    def _righe_vere(self, nome: str) -> list[str]:
        righe = (AVVIO_MACOS / nome).read_text(encoding="utf-8").splitlines()
        return [r.strip() for r in righe if r.strip() and not r.strip().startswith("#")]

    def test_tutto_sta_in_una_funzione_chiamata_alla_fine(self) -> None:
        # ⚠ Non basta che `main` ci sia: deve essere l'UNICA cosa che c'e'.
        # Una riga di codice prima della definizione viene eseguita mentre
        # bash sta ancora leggendo il file, ed e' esattamente il caso da cui
        # la funzione protegge.  La prima versione di questa prova cercava
        # solo `main() {` fra le righe, e restava verde con del codice fuori:
        # copertura dichiarata e non esistente.
        righe = self._righe_vere("avvia.command")
        self.assertEqual(righe[0], "main() {", "prima della funzione c'è altro codice")
        self.assertEqual(righe[-3], "}", "dopo la funzione c'è altro codice")
        self.assertEqual(righe[-2], 'main "$@"')
        self.assertEqual(righe[-1], "exit $?")


@unittest.skipIf(
    _serve("bash") is None or _serve("curl") is None,
    "la sonda usa curl dentro bash",
)
class LaSondaChiedeChiRisponde(unittest.TestCase):
    """La guardia che decide se allineare NON e' «la porta e' occupata».

    ⚠ E' la distinzione su cui questa guardia poteva nascere sbagliata, ed e'
    il motivo per cui questa classe esiste.  Se dicesse «occupata» per un
    programma qualsiasi, la cartella smetterebbe di ricevere aggiornamenti
    finche' quel programma resta acceso — che e' lo stesso difetto per cui la
    guardia sull'albero sporco era gia' stata scartata.
    """

    def _chiedi(self, porta: int) -> int:
        comando = f'source "{AVVIO_MACOS / "comune.sh"}"; il_comparatore_risponde {porta}'
        return subprocess.run([_serve("bash"), "-c", comando], capture_output=True).returncode

    def test_il_comparatore_vero_lo_riconosce(self) -> None:
        corpo = json.dumps({"ok": True, "status": "ready", "firmaDelCodice": "abc"}).encode()
        with ServizioFinto(corpo) as servizio:
            self.assertEqual(self._chiedi(servizio.porta), 0)

    def test_un_programma_qualunque_sulla_porta_non_la_ferma(self) -> None:
        with ServizioFinto(None) as servizio:
            self.assertEqual(self._chiedi(servizio.porta), 1)

    def test_una_risposta_json_che_non_e_la_sua_non_la_ferma(self) -> None:
        with ServizioFinto(json.dumps({"nome": "altro programma"}).encode()) as servizio:
            self.assertEqual(self._chiedi(servizio.porta), 1)

    def test_su_una_porta_dove_non_c_e_nessuno_risponde_di_no(self) -> None:
        vuota = ServizioFinto(None)
        porta = vuota.porta
        vuota.server.server_close()
        self.assertEqual(self._chiedi(porta), 1)


if __name__ == "__main__":
    unittest.main()
