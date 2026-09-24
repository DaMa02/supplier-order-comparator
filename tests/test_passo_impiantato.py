"""A stuck pipeline step must not hang the whole process.

`esegui_comando` runs each step with a timeout. Without one, a stuck step
holds `lucchetto_lavori` forever: every later "Ricalcola" gets a 409,
`POST /api/spegni` refuses to shut down while a run is active, and the
launcher (which restarts the server when the sources changed) keeps the
stuck one running.

Unlike `test_pipeline_jobs.py`, this file spawns real subprocesses: a timeout
that kills a process can't be exercised against a fake one. The children are
Python processes that sleep; none of them touch the network.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import pipeline_jobs  # noqa: E402
from pipeline_jobs import (  # noqa: E402
    ComandoTroppoLungo,
    ConfigurazionePipeline,
    PipelineJobManager,
    esegui_comando,
)


class IlTettoUccideIlFiglio(unittest.TestCase):
    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.cartella = Path(self.temporanea.name)
        self.nati: list[subprocess.Popen] = []
        popen_vero = subprocess.Popen

        def annota(*argomenti, **parametri):
            processo = popen_vero(*argomenti, **parametri)
            self.nati.append(processo)
            return processo

        subprocess.Popen = annota
        self.addCleanup(lambda: setattr(subprocess, "Popen", popen_vero))

    def test_un_figlio_che_non_risponde_viene_ucciso_e_lo_si_dice(self) -> None:
        """The child sleeps and keeps `stdout` open: this is the real case.

        A timeout on `wait()` alone would never fire, because the caller
        would already be blocked inside `stdout.read()`, which only returns
        at EOF.
        """

        partito = time.monotonic()
        with self.assertRaises(ComandoTroppoLungo) as caduta:
            esegui_comando(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                self.cartella,
                lambda evento: None,
                timeout_secondi=0.5,
            )

        self.assertLess(time.monotonic() - partito, 20, "il tetto non ha fermato niente")
        self.assertEqual(caduta.exception.secondi, 0.5)
        self.assertEqual(len(self.nati), 1)
        self.assertIsNotNone(self.nati[0].poll(), "il figlio è rimasto vivo dopo il tetto")

    def test_un_figlio_che_lascia_un_nipote_attaccato_al_tubo_non_pianta_niente(self) -> None:
        """The case where the timeout defense breaks on its own.

        `kill()` only kills the direct child. If that child left a
        grandchild that inherited `stdout`, the pipe stays open and EOF
        never arrives: the reader thread stays blocked in `read()`, and
        `close()` (which needs that reader's internal lock) never returns.
        Result: `lucchetto_lavori` held forever, every later "Ricalcola"
        gets a 409, `POST /api/spegni` refuses to shut down — the exact
        hang the timeout exists to prevent, reached by another path.

        The grandchild must keep the pipe open past the five-second
        cleanup window: if it dies earlier, the `join` calls alone let it
        finish and the test would pass even without the guard, proving
        nothing. It waits for a file the test writes at the end, with its
        own timeout so a broken run doesn't leave a stray process behind.
        """

        sentinella = self.cartella / "il-nipote-puo-uscire"
        uscito = self.cartella / "il-nipote-e-uscito"
        nipote = self.cartella / "nipote.py"
        nipote.write_text(
            "import os, tempfile, time\n"
            "from pathlib import Path\n"
            # The grandchild inherits its working directory from whoever
            # launches it, which is this test's temp dir. On Windows a
            # directory holding a running process can't be removed, and a
            # few milliseconds pass between the file write below and the
            # process actually exiting: cleanup could start in that window
            # and fail with WinError 32. Leaving the directory removes the
            # race; the pipe, which is what this test measures, stays open
            # regardless.
            "os.chdir(tempfile.gettempdir())\n"
            f"sentinella = Path({str(sentinella)!r})\n"
            "scadenza = time.monotonic() + 60\n"
            "while not sentinella.exists() and time.monotonic() < scadenza:\n"
            "    time.sleep(0.05)\n"
            f"Path({str(uscito)!r}).write_bytes(b'')\n",
            encoding="utf-8",
        )
        figlio = self.cartella / "figlio.py"
        figlio.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(nipote)!r}])\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )

        partito = time.monotonic()
        try:
            with self.assertRaises(ComandoTroppoLungo):
                esegui_comando(
                    [sys.executable, str(figlio)],
                    self.cartella,
                    lambda evento: None,
                    timeout_secondi=0.5,
                )
            durata = time.monotonic() - partito
        finally:
            sentinella.write_bytes(b"")
            # Wait for the grandchild to actually exit before the temp dir
            # is removed: it runs with that dir as its working directory,
            # and on Windows a directory holding someone's running process
            # can't be removed.
            scadenza = time.monotonic() + 30
            while not uscito.exists() and time.monotonic() < scadenza:
                time.sleep(0.05)

        # With the guard: half a second of timeout plus the five-second
        # cleanup window. Without it: doesn't return until the grandchild
        # releases the pipe, i.e. a full minute.
        self.assertLess(durata, 20, "è rimasto piantato a chiudere i tubi")
        self.assertTrue(uscito.exists(), "il nipote è ancora vivo dentro la cartella temporanea")

    def test_senza_tetto_si_aspetta_come_prima(self) -> None:
        risultato = esegui_comando(
            [sys.executable, "-c", "import sys; sys.stdout.write('fatto')"],
            self.cartella,
            lambda evento: None,
        )

        self.assertEqual(risultato.uscita, 0)
        self.assertEqual(risultato.stdout, "fatto")

    def test_un_comando_che_finisce_in_tempo_non_perde_niente(self) -> None:
        """Progress events stream from `stderr` line by line and must keep working.

        The AI step reads these: two minutes of silence there looks like a
        stuck program, so moving `stdout` reading to a background thread
        must not break progress reporting as a side effect.
        """

        eventi: list[dict] = []
        programma = (
            "import sys\n"
            "sys.stderr.write('%s' + '{\"fatti\": 1, \"totali\": 2}' + chr(10))\n"
            "sys.stderr.flush()\n"
            "sys.stdout.write('riepilogo')\n"
            "sys.exit(3)\n"
        ) % pipeline_jobs.PREFISSO_AVANZAMENTO

        risultato = esegui_comando(
            [sys.executable, "-c", programma],
            self.cartella,
            eventi.append,
            timeout_secondi=30,
        )

        self.assertEqual(risultato.uscita, 3)
        self.assertEqual(risultato.stdout, "riepilogo")
        self.assertEqual(eventi, [{"fatti": 1, "totali": 2}])

    def test_un_figlio_ciarliero_non_pianta_nessuno(self) -> None:
        """Full pipes with no reader draining them deadlock both sides."""

        programma = (
            "import sys\n"
            "sys.stderr.write('x' * 200000)\n"
            "sys.stdout.write('y' * 200000)\n"
        )

        risultato = esegui_comando(
            [sys.executable, "-c", programma], self.cartella, lambda evento: None, timeout_secondi=30
        )

        self.assertEqual(risultato.uscita, 0)
        self.assertEqual(len(risultato.stdout), 200000)
        self.assertEqual(len(risultato.stderr), 200000)


class IlTettoMisuraIlSilenzioNonLaDurata(unittest.TestCase):
    """The timeout measures silence between progress events, not total runtime.

    `TETTO_DELLE_FASI` is documented as "how long a step may stay silent
    before it counts as stuck", so `esegui_comando` must reset the timeout
    on every progress event rather than counting from the start. Otherwise a
    healthy but long-running AI step (many cases, a slow machine, a slow
    model) would be killed at the timeout and reported as unresponsive,
    discarding calls that were already paid for.

    Uses real subprocesses.
    """

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.cartella = Path(self.temporanea.name)

    def test_un_passo_che_parla_puo_durare_piu_del_tetto(self) -> None:
        """Six lines half a second apart, with a one-second timeout: three
        seconds of work under a timeout that would have fired if it counted
        from the start."""

        programma = (
            "import sys, time\n"
            "for indice in range(6):\n"
            "    sys.stderr.write('AVANZAMENTO {\"fatti\": %d, \"totali\": 6}\\n' % indice)\n"
            "    sys.stderr.flush()\n"
            "    time.sleep(0.5)\n"
            "sys.stdout.write('finito')\n"
        )
        eventi: list[dict] = []
        inizio = time.monotonic()

        risultato = esegui_comando(
            [sys.executable, "-c", programma], self.cartella, eventi.append, timeout_secondi=1.0
        )

        self.assertEqual(risultato.uscita, 0)
        self.assertEqual(risultato.stdout, "finito")
        self.assertEqual(len(eventi), 6)
        self.assertGreater(time.monotonic() - inizio, 1.0, "non è durato più del tetto")

    def test_un_passo_che_smette_di_parlare_viene_ucciso_lo_stesso(self) -> None:
        """Control case: the timeout wasn't disabled, only rebased on progress."""

        programma = (
            "import sys, time\n"
            "sys.stderr.write('AVANZAMENTO {\"fatti\": 1, \"totali\": 2}\\n')\n"
            "sys.stderr.flush()\n"
            "time.sleep(30)\n"
        )

        with self.assertRaises(ComandoTroppoLungo):
            esegui_comando(
                [sys.executable, "-c", programma], self.cartella, lambda evento: None,
                timeout_secondi=1.0,
            )


class LaCatenaSiFermaEDiceQuale(unittest.TestCase):
    """A fired timeout must reach the page as a named stop, not a silent hang."""

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        radice = Path(self.temporanea.name)
        self.dati = radice / "current"
        self.uploads = self.dati / "uploads"
        self.uploads.mkdir(parents=True)
        # Without a source file the pipeline stops at the first step for a
        # different reason, and the test would pass for the wrong reason.
        (self.uploads / "betulla.xlsx").write_bytes(b"finto")
        self.review = self.dati / "review_data.json"
        self.review.write_bytes(b'{"run": {"id": "vecchia"}, "products": [], "suppliers": []}')
        self.prima = self.review.read_bytes()

    def _gestore(self, esecutore) -> PipelineJobManager:
        return PipelineJobManager(
            ConfigurazionePipeline(
                data_dir=self.dati,
                uploads_dir=self.uploads,
                review_path=self.review,
                state_path=self.dati / "state.json",
            ),
            esecutore=esecutore,
        )

    def test_il_passo_impiantato_ferma_la_catena_col_suo_nome(self) -> None:
        tetti: list[float | None] = []

        def impiantato(comando, cartella, avanzamento, *, timeout_secondi=None):
            tetti.append(timeout_secondi)
            raise ComandoTroppoLungo(secondi=float(timeout_secondi or 0.0))

        gestore = self._gestore(impiantato)
        gestore.avvia()
        esito = gestore.attendi(timeout=30)

        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)
        self.assertEqual((esito.get("fermata") or {}).get("code"), "PROFILAZIONE_TROPPO_LUNGA")
        messaggio = (esito.get("fermata") or {}).get("message") or esito.get("messaggio") or ""
        self.assertIn("non ha risposto entro", messaggio)
        self.assertIn("5 minuti", messaggio)
        self.assertEqual(tetti, [pipeline_jobs.TETTO_PREDEFINITO_DI_FASE])

    def test_il_confronto_di_prima_resta_dov_era(self) -> None:
        def impiantato(comando, cartella, avanzamento, *, timeout_secondi=None):
            raise ComandoTroppoLungo(secondi=float(timeout_secondi or 0.0))

        gestore = self._gestore(impiantato)
        gestore.avvia()
        gestore.attendi(timeout=30)

        self.assertEqual(self.review.read_bytes(), self.prima)

    def test_dopo_la_fermata_si_puo_ricalcolare_di_nuovo(self) -> None:
        """The reason the timeout exists: the job lock is released afterward.

        Without it, every later "Ricalcola" got a 409 and the only way out
        was force-closing the window.
        """

        chiamate: list[int] = []

        def impiantato(comando, cartella, avanzamento, *, timeout_secondi=None):
            chiamate.append(1)
            raise ComandoTroppoLungo(secondi=float(timeout_secondi or 0.0))

        gestore = self._gestore(impiantato)
        for _ in range(2):
            gestore.avvia()
            gestore.attendi(timeout=30)

        self.assertEqual(len(chiamate), 2)

    def test_ogni_fase_ha_un_tetto_e_quella_ai_ce_l_ha_piu_alto(self) -> None:
        tetti = {fase: pipeline_jobs.tetto_della_fase(fase) for fase in pipeline_jobs.FASI}

        self.assertTrue(all(valore > 0 for valore in tetti.values()), tetti)
        # 105s measured for the AI step: the timeout is an order of
        # magnitude wider, since it only needs to distinguish "slow" from
        # "stuck".
        self.assertEqual(tetti["VALUTAZIONE_AI"], max(tetti.values()))
        self.assertGreaterEqual(tetti["VALUTAZIONE_AI"], 105 * 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
