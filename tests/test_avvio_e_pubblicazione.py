"""The scripts that launch the program.

Covers the launcher scripts that live outside `app/` and `scripts/`: the
Windows launcher and the macOS startup script. `AVVIA_COMPARATORE.ps1` isn't
tested here — it requires PowerShell, which isn't available on the
development machine; CI covers it on Windows and at least parses it. This
file covers the testable half: the `sh`/`bash` script.

These tests run real processes (`bash`, `curl`) on local ports. Where one of
those tools is missing, the class is skipped rather than failed, the same
rule this suite already applies to Node.
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
    """An HTTP server that replies whatever it's told to, on a free port."""

    def __init__(self, corpo: bytes | None, stato: int = 200) -> None:
        self.corpo = corpo
        self.stato = stato
        finto = self

        class Gestore(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - required by http.server
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
    """The first thing that must hold: that bash can parse them.

    Mirrors the syntax check CI runs on PowerShell. A syntax error here means
    the program won't start, and no other test would catch it.
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
    """`avvia.command` lives inside the tree that `AVVIA_COMPARATORE.ps1`
    keeps in sync on Windows: on macOS it doesn't run `reset --hard` itself,
    but it's still a file a repository update can rewrite while it's running.
    The safeguard is that the whole script sits inside one function — bash
    must read the entire function definition before it can execute it — with
    an `exit` right after the call, so it never goes back to read the
    changed file.

    This isn't a style check: it's the one thing that holds, and it breaks
    silently if a line is added after the function.
    """

    def _righe_vere(self, nome: str) -> list[str]:
        righe = (AVVIO_MACOS / nome).read_text(encoding="utf-8").splitlines()
        return [r.strip() for r in righe if r.strip() and not r.strip().startswith("#")]

    def test_tutto_sta_in_una_funzione_chiamata_alla_fine(self) -> None:
        # It's not enough for `main` to exist: it must be the only thing in
        # the file. A line of code before the function definition runs while
        # bash is still reading the file, exactly the case this guards
        # against. A weaker version of this test that only checked for
        # `main() {` among the lines would stay green with code outside the
        # function: coverage that isn't there.
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
    """The guard that decides whether to sync isn't "is the port busy".

    Checking only the port would make the same mistake already ruled out for
    a "dirty tree" guard: it would call any program on that port a reason to
    stop, and the folder would stop receiving updates as long as any program
    stayed bound to it.
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
