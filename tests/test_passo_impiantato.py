"""Un passo della catena che non risponde piu' non inchioda il programma.

Fino al 20 agosto 2026 `esegui_comando` faceva `processo.wait()` senza tetto.
Un passo impiantato non aveva nessuna via d'uscita: teneva `lucchetto_lavori`
per sempre, ogni «Ricalcola» successivo rispondeva 409, `POST /api/spegni` si
rifiutava di spegnere mentre una run e' in corso, e il lanciatore — che spegne
il server vecchio quando i sorgenti sono cambiati — riusava il vecchio.  Cioe'
si inchiodava anche la sola strada con cui una correzione arriva in negozio.

⚠ Questo file, a differenza di `test_pipeline_jobs.py`, lancia sottoprocessi
veri: un tetto che uccide un processo non si puo' provare su un processo
finto.  Sono figli Python che dormono, e nessuno di loro tocca la rete.
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
        """⚠ Il figlio dorme e tiene aperto `stdout`: e' il caso vero.

        Un tetto sulla sola `wait` non scatterebbe mai, perche' il programma
        sarebbe gia' fermo dentro `stdout.read()`, che torna solo all'EOF.
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
        """⚠ Il caso in cui la difesa del tetto si rompeva da sola.

        `kill()` uccide il figlio diretto. Se quel figlio aveva lasciato un
        discendente che ha ereditato `stdout`, il tubo resta aperto e l'EOF non
        arriva mai: il filo lettore resta dentro `read()`, e `close()` — che
        pretende il lucchetto interno di quel lettore — non torna piu'.
        Risultato: `lucchetto_lavori` in mano per sempre, ogni «Ricalcola» a
        409, `POST /api/spegni` che si rifiuta di spegnere. Cioe' esattamente
        la fermata che il tetto esiste per evitare, arrivata per un'altra
        strada.

        ⚠ Il nipote deve tenere il tubo aperto **oltre** i cinque secondi di
        pulizia: se muore prima, i `join` bastano a farlo finire da solo e la
        prova passa anche senza la guardia — cioe' non prova niente. Aspetta un
        file che il test crea alla fine, con un tetto suo per non lasciare in
        giro un processo se qualcosa va storto.
        """

        sentinella = self.cartella / "il-nipote-puo-uscire"
        uscito = self.cartella / "il-nipote-e-uscito"
        nipote = self.cartella / "nipote.py"
        nipote.write_text(
            "import os, tempfile, time\n"
            "from pathlib import Path\n"
            # ⚠ Il nipote eredita da chi lo lancia la cartella di lavoro, che e'
            # proprio la temporanea di questa prova.  Su Windows una cartella
            # con dentro un processo non si cancella, e fra il file che scrive
            # qui sotto e il momento in cui muore davvero passano dei
            # millisecondi: la pulizia partiva in quella finestra e falliva con
            # WinError 32.  Uscire dalla cartella toglie la corsa; il tubo, che
            # e' la cosa che questa prova misura, resta aperto lo stesso.
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
            # ⚠ E si aspetta che sia uscito davvero, prima che la cartella
            # temporanea venga cancellata. Il nipote ha come cartella di lavoro
            # proprio quella, e su Windows una cartella con dentro il processo
            # di qualcuno non si cancella: e' il guasto che su questo progetto
            # ha gia' fatto morire ventinove prove alla pulizia, con i test
            # mirati tutti verdi.
            scadenza = time.monotonic() + 30
            while not uscito.exists() and time.monotonic() < scadenza:
                time.sleep(0.05)

        # Con la guardia: mezzo secondo di tetto piu' i cinque della pulizia.
        # Senza: non torna finche' il nipote non molla il tubo, cioe' un minuto.
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
        """L'avanzamento passa da `stderr` riga per riga, e deve continuare.

        E' la fase AI a leggerlo: due minuti di silenzio sembrano un programma
        rotto, e chi ha spostato la lettura di `stdout` in un filo poteva
        romperlo senza accorgersene.
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
        """Con i tubi pieni e nessuno che li svuota ci si pianta a vicenda."""

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
    """⚠ La costante diceva una cosa e il codice ne faceva un'altra.

    `TETTO_DELLE_FASI` è documentato come «quanto può stare zitto un passo prima
    che si dichiari impiantato», e `esegui_comando` faceva `wait(timeout=...)`,
    cioè contava dall'inizio. Una fase AI sana e lunga — tanti casi, un computer
    lento, un modello che risponde piano — veniva uccisa al tetto e raccontata
    come «non ha risposto», buttando via le chiamate già pagate.

    Prove eseguite, con sottoprocessi veri.
    """

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.cartella = Path(self.temporanea.name)

    def test_un_passo_che_parla_puo_durare_piu_del_tetto(self) -> None:
        """Sei righe a mezzo secondo l'una, con un tetto di un secondo: tre

        secondi di lavoro sotto un tetto che dall'inizio sarebbe scattato."""

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
        """La controprova: il tetto non è stato spento, si è solo spostato."""

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
    """Il tetto scattato deve arrivare in pagina come una fermata con un nome."""

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        radice = Path(self.temporanea.name)
        self.dati = radice / "current"
        self.uploads = self.dati / "uploads"
        self.uploads.mkdir(parents=True)
        # Senza un documento la catena si ferma al primo passo per un altro
        # motivo, e la prova passerebbe per il motivo sbagliato.
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
        """La ragione per cui il tetto esiste: il lucchetto torna libero.

        Senza, ogni «Ricalcola» successivo rispondeva 409 e l'unica uscita era
        chiudere la finestra a forza.
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
        # 105 secondi misurati per la fase AI: il tetto e' largo di un ordine
        # di grandezza, perche' serve solo a distinguere «lento» da «fermo».
        self.assertEqual(tetti["VALUTAZIONE_AI"], max(tetti.values()))
        self.assertGreaterEqual(tetti["VALUTAZIONE_AI"], 105 * 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
