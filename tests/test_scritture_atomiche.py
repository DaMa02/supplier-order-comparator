"""Due fili che salvano lo stesso file non si rubano il temporaneo.

`state.json` lo scrivono in due: la pagina, che si autosalva 450 ms dopo ogni
modifica (`atomic_json` di `server.py`), e la catena, che lo ripulisce a fine
ricalcolo (`scrivi_json` di `pipeline_jobs.py`).  Fino al 20 agosto 2026 tutti
e due passavano da un temporaneo dal nome fisso — `state.json.tmp` — e i due
nomi erano identici.  Quel che ne usciva non era un errore rumoroso: la
sostituzione del primo pubblicava il contenuto del secondo, cioe' lo stato
salvato non era quello che l'utente aveva appena scritto.

Le prove qui sotto mettono i due fili nell'ordine peggiore invece di sperare
che capiti: senza il nome unico diventano rosse tutt'e due.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import ai_client  # noqa: E402
import order_history  # noqa: E402
import pipeline_jobs  # noqa: E402
import scrittura_sicura  # noqa: E402
import server  # noqa: E402


class ScrittureCheSiIncrociano(unittest.TestCase):
    """Il filo lento arriva per ultimo e vince: è l'ordine peggiore."""

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.percorso = Path(self.temporanea.name) / "state.json"

    def _incrocia(self, scrittore_lento, scrittore_veloce) -> None:
        """Ferma il primo scrittore un istante prima della sostituzione.

        Nel frattempo il secondo scrive e sostituisce per intero.  Con un
        temporaneo condiviso, quando il primo riprende il suo file non esiste
        piu': `os.replace` solleva, e il contenuto pubblicato e' quello del
        secondo.
        """

        replace_vero = os.replace
        in_attesa = threading.Event()
        via_libera = threading.Event()
        primo_arrivato = threading.Event()

        def replace_con_pausa(sorgente, destinazione):
            if not primo_arrivato.is_set():
                primo_arrivato.set()
                in_attesa.set()
                via_libera.wait(timeout=10)
            return replace_vero(sorgente, destinazione)

        guasto: list[BaseException] = []

        def lento() -> None:
            try:
                scrittore_lento()
            except BaseException as errore:  # pragma: no cover - lo dice il test
                guasto.append(errore)

        os.replace = replace_con_pausa
        try:
            filo = threading.Thread(target=lento)
            filo.start()
            self.assertTrue(in_attesa.wait(timeout=10), "il primo scrittore non è mai partito")
            scrittore_veloce()
            via_libera.set()
            filo.join(timeout=10)
        finally:
            os.replace = replace_vero
        self.assertFalse(filo.is_alive())
        self.assertEqual(guasto, [], "la scrittura interrotta a metà ha sollevato")

    def test_la_pagina_che_salva_mentre_la_catena_ripulisce_non_perde_il_suo_stato(self) -> None:
        self._incrocia(
            lambda: server.atomic_json(self.percorso, {"chi": "la pagina", "products": [1, 2, 3]}),
            lambda: pipeline_jobs.scrivi_json(self.percorso, {"chi": "la catena"}),
        )

        salvato = json.loads(self.percorso.read_text(encoding="utf-8"))
        self.assertEqual(salvato["chi"], "la pagina")

    def test_e_al_contrario_la_catena_non_perde_la_sua_ripulitura(self) -> None:
        self._incrocia(
            lambda: pipeline_jobs.scrivi_json(self.percorso, {"chi": "la catena", "products": []}),
            lambda: server.atomic_json(self.percorso, {"chi": "la pagina"}),
        )

        salvato = json.loads(self.percorso.read_text(encoding="utf-8"))
        self.assertEqual(salvato["chi"], "la catena")

    def test_due_fili_diversi_non_si_danno_lo_stesso_temporaneo(self) -> None:
        """Il nome e' unico per chi scrive, non per la singola scrittura.

        Due chiamate dallo stesso filo riusano lo stesso nome, e va bene:
        un filo non puo' incrociare se stesso.  Quello che non deve ripetersi
        e' il nome fra fili diversi, perche' sono loro che si incrociano.
        """

        nomi: list[str] = []
        replace_vero = os.replace

        def annota(sorgente, destinazione):
            nomi.append(Path(sorgente).name)
            return replace_vero(sorgente, destinazione)

        # ⚠ I due fili devono essere vivi nello stesso momento: uno dopo
        # l'altro CPython riassegna lo stesso `get_ident()` al filo nuovo, e
        # la prova passerebbe — o cadrebbe — per un motivo che non c'entra.
        insieme = threading.Barrier(2, timeout=10)

        def dalla_pagina() -> None:
            insieme.wait()
            server.atomic_json(self.percorso, {"chi": "la pagina"})

        def dalla_catena() -> None:
            insieme.wait()
            pipeline_jobs.scrivi_json(self.percorso, {"chi": "la catena"})

        os.replace = annota
        try:
            fili = [threading.Thread(target=lavoro) for lavoro in (dalla_pagina, dalla_catena)]
            for filo in fili:
                filo.start()
            for filo in fili:
                filo.join(timeout=10)
                self.assertFalse(filo.is_alive())
        finally:
            os.replace = replace_vero

        self.assertEqual(len(nomi), 2)
        self.assertNotEqual(nomi[0], nomi[1])
        for nome in nomi:
            self.assertTrue(nome.startswith("state.json."), nome)
            # Resta un `.tmp`: `.gitignore` lo esclude, e `consegna.e_documento`
            # non lo conta fra i documenti di una cartella consegnata.
            self.assertTrue(nome.endswith(".tmp"), nome)
            self.assertIn(str(os.getpid()), nome)

    def test_una_scrittura_fallita_non_lascia_in_giro_il_suo_temporaneo(self) -> None:
        replace_vero = os.replace

        def sempre_rotto(sorgente, destinazione):
            raise OSError("il disco dice di no")

        os.replace = sempre_rotto
        try:
            with self.assertRaises(OSError):
                server.atomic_json(self.percorso, {"chi": "la pagina"})
            with self.assertRaises(OSError):
                pipeline_jobs.scrivi_json(self.percorso, {"chi": "la catena"})
        finally:
            os.replace = replace_vero

        # Con un nome unico nessuno riscrivera' mai piu' quei due file: se non
        # li cancella chi li ha creati, restano li' per sempre.
        self.assertEqual(sorted(p.name for p in self.percorso.parent.glob("*.tmp")), [])


class LaChiaveNasceGiaStretta(unittest.TestCase):
    """`secrets.json` e' l'unico file del programma con dei permessi da tenere.

    ⚠ Fino al 20 agosto la chiave toccava il disco con i permessi di umask e
    veniva ristretta un istante dopo, mentre il docstring dichiarava il
    contrario. Qui si guarda il temporaneo **nel momento in cui viene creato**,
    che e' l'unico modo di distinguere le due cose.
    """

    @unittest.skipIf(os.name == "nt", "su Windows il modo di `os.open` non significa granche'")
    def test_il_temporaneo_della_chiave_nasce_a_0600(self) -> None:
        with tempfile.TemporaryDirectory() as cartella:
            percorso = Path(cartella) / "secrets.json"
            visti: list[int] = []
            open_vero = os.open

            def annota(percorso_aperto, flag, modo=0o777, **extra):
                descrittore = open_vero(percorso_aperto, flag, modo, **extra)
                if str(percorso_aperto).endswith(".nuovo"):
                    visti.append(os.fstat(descrittore).st_mode & 0o777)
                return descrittore

            os.open = annota
            try:
                ai_client.salva_chiave("sk-or-v1-finta", percorso)
            finally:
                os.open = open_vero

            self.assertEqual(visti, [0o600], "il temporaneo è nato coi permessi sbagliati")
            self.assertEqual(percorso.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(p.name for p in percorso.parent.glob("*.nuovo")), [])


class IlLucchettoEUnoSolo(unittest.TestCase):
    """La catena e le rotte devono condividere lo stesso oggetto, non due uguali.

    E' la meta' che i collaudi di `pipeline_jobs` non possono vedere: li' il
    lucchetto glielo passa il test.  Se il servizio ne costruisse uno suo, le
    prove dell'attivazione resterebbero verdi e in negozio non escluderebbero
    niente.
    """

    def test_il_servizio_passa_alla_catena_il_proprio_lucchetto(self) -> None:
        with tempfile.TemporaryDirectory() as cartella:
            radice = Path(cartella)
            magazzino = server.ReviewStore(
                radice / "current" / "review_data.json",
                radice / "current" / "state.json",
                radice / "current" / "uploads",
                radice / "outputs",
            )

            self.assertIs(magazzino.pipeline_jobs.lucchetto_dati, magazzino.lock)
            self.assertIs(magazzino.pipeline_jobs.lucchetto_lavori, magazzino.lucchetto_lavori)


class IByteArrivanoSulDiscoPrimaDiSostituire(unittest.TestCase):
    """`os.replace` e' atomico rispetto ai metadati, non rispetto ai dati.

    Garantisce che nessuno veda il file a meta' fra il vecchio e il nuovo; non
    garantisce che i byte del nuovo siano gia' sul disco.  Se il computer del
    negozio va giu' per una mancanza di corrente nell'istante sbagliato, al
    riavvio si trova uno `state.json` o un `orders.json` **presente ma vuoto o
    tronco** — e da quando `app/data/` e' ignorato per intero da git, dentro il
    repository di quelle memorie non resta niente.
    """

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.cartella = Path(self.temporanea.name)

    def sincronizzati(self, scrittura) -> list[int]:
        """I descrittori su cui `fsync` è stato chiamato durante la scrittura."""

        fsync_vero = os.fsync
        visti: list[int] = []

        def annota(descrittore):
            visti.append(descrittore)
            return fsync_vero(descrittore)

        os.fsync = annota
        try:
            scrittura()
        finally:
            os.fsync = fsync_vero
        return visti

    def test_tutti_e_quattro_gli_aiutanti_forzano_i_byte_sul_disco(self) -> None:
        aiutanti = {
            "la pagina che salva": lambda: server.atomic_json(
                self.cartella / "state.json", {"products": []}),
            "la catena": lambda: pipeline_jobs.scrivi_json(
                self.cartella / "pipeline.json", {"fasi": []}),
            "lo storico degli ordini": lambda: order_history.save_history(
                self.cartella / "orders.json", {"schema_version": 1, "orders": []}),
            "la memoria AI": lambda: ai_client.ClientAI._salva_memoria(
                self.cartella / "memoria_ai.json", {}),
        }

        for chi, scrittura in aiutanti.items():
            with self.subTest(chi=chi):
                self.assertEqual(len(self.sincronizzati(scrittura)), 1, chi)

    def test_si_puo_spegnere_per_una_scrittura_sola(self) -> None:
        """La via d'uscita se in negozio il salvataggio diventasse lento: si

        spegne dove serve, non dappertutto."""

        visti = self.sincronizzati(lambda: scrittura_sicura.scrivi_json(
            self.cartella / "senza.json", {"a": 1}, forza_su_disco=False))

        self.assertEqual(visti, [])
        self.assertTrue((self.cartella / "senza.json").is_file())

    def test_quello_che_scrivono_finisce_a_capo_come_tutto_il_resto(self) -> None:
        """In binario e LF: su Windows `write_text` farebbe `\r\n`, e ci sono

        cinque collaudi che pretendono il contrario sui file che il programma
        si riscrive.  `atomic_json` e `save_history` passavano da `write_text`.

        ⚠ **Fuori da Windows questa prova non puo' fallire**: `write_text` non
        traduce niente su POSIX, quindi rimettendo il difetto resta verde. Vale
        sulla CI, che gira su `windows-latest` apposta per somigliare al PC del
        negozio; sul Mac e' un promemoria, non una difesa. Il progetto ha gia'
        lo stesso caso in `test_proprieta_non_provate`, e lo dichiara: qui
        mancava.
        """

        server.atomic_json(self.cartella / "state.json", {"products": [1, 2]})
        order_history.save_history(self.cartella / "orders.json", {"orders": []})

        for nome in ("state.json", "orders.json"):
            with self.subTest(file=nome):
                self.assertNotIn(b"\r\n", (self.cartella / nome).read_bytes())

    def test_una_scrittura_che_non_riesce_non_lascia_il_temporaneo(self) -> None:
        fsync_vero = os.fsync

        def sempre_rotto(descrittore):
            raise OSError("il disco dice di no")

        os.fsync = sempre_rotto
        try:
            with self.assertRaises(OSError):
                scrittura_sicura.scrivi_json(self.cartella / "state.json", {"a": 1})
        finally:
            os.fsync = fsync_vero

        self.assertEqual(sorted(p.name for p in self.cartella.glob("*")), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
