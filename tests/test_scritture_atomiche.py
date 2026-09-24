"""Two threads writing the same file must not race on a shared temp file.

`state.json` is written from two places: the page, which autosaves 450ms
after each change (`atomic_json` in `server.py`), and the pipeline, which
rewrites it at the end of a recompute (`scrivi_json` in `pipeline_jobs.py`).
If both used a temp file with a fixed name, one thread's atomic replace could
publish the other thread's content — silently, with no error, just the wrong
state on disk.

These tests force the two threads into the worst possible interleaving
instead of hoping it occurs naturally: without a unique temp-file name per
writer, both fail.
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
    """The slow thread finishes last and wins the write: the worst-case order."""

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.percorso = Path(self.temporanea.name) / "state.json"

    def _incrocia(self, scrittore_lento, scrittore_veloce) -> None:
        """Pause the first writer right before its atomic replace.

        The second writer runs to completion in the meantime. With a shared
        temp file, the first writer's file is gone by the time it resumes:
        `os.replace` raises, and the published content is the second
        writer's.
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
        """Each writer thread gets its own temp-file name; a single thread may reuse it.

        Two calls from the same thread can reuse the same name, since a
        thread can't race itself. What must differ is the name across
        different threads, since those are the ones that can race.
        """

        nomi: list[str] = []
        replace_vero = os.replace

        def annota(sorgente, destinazione):
            nomi.append(Path(sorgente).name)
            return replace_vero(sorgente, destinazione)

        # The two threads must be alive at the same time: run one after the
        # other and CPython can reuse the same `get_ident()` for the new
        # thread, making the test pass or fail for an unrelated reason.
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
            # Keeps the `.tmp` suffix: `.gitignore` excludes it, and
            # `consegna.e_documento` doesn't count it among a delivered folder's documents.
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

        # With a unique name, nothing will ever write to those two temp files
        # again; if the writer doesn't clean them up, they stay forever.
        self.assertEqual(sorted(p.name for p in self.percorso.parent.glob("*.tmp")), [])


class LaChiaveNasceGiaStretta(unittest.TestCase):
    """`secrets.json` is the only file in the program with permissions to enforce.

    The key file must be created with restrictive permissions from the first
    write, not written with default umask permissions and tightened
    afterward — a window where the key sits world-readable on disk. This
    test inspects the temp file's mode at creation time, the only way to
    tell the two apart.
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
    """The service and the pipeline it drives must share the same lock object, not two equal ones.

    `pipeline_jobs`'s own tests can't catch this, since they get a lock
    injected directly. If the service built its own instead of passing its
    lock down, those tests would stay green while the real deployment had
    no mutual exclusion at all.
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
    """`os.replace` is atomic with respect to metadata, not to data durability.

    It guarantees no reader ever sees a half-written file mid-swap; it does
    not guarantee the new file's bytes have reached disk. A power loss at the
    wrong moment can leave `state.json` or `orders.json` present but empty or
    truncated on restart — and since `app/data/` is entirely git-ignored,
    the repository holds no other copy to recover from.
    """

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.cartella = Path(self.temporanea.name)

    def sincronizzati(self, scrittura) -> list[int]:
        """Return the file descriptors `fsync` was called on during the write."""

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
        """Escape hatch if saving gets slow on the store's hardware: disable
        the fsync for one call site, not everywhere."""

        visti = self.sincronizzati(lambda: scrittura_sicura.scrivi_json(
            self.cartella / "senza.json", {"a": 1}, forza_su_disco=False))

        self.assertEqual(visti, [])
        self.assertTrue((self.cartella / "senza.json").is_file())

    def test_quello_che_scrivono_finisce_a_capo_come_tutto_il_resto(self) -> None:
        """Written files must use LF line endings, in binary, not `write_text`'s
        platform-dependent newline translation.

        On Windows, `write_text` would emit `\r\n`, which other tests reading
        these files back assume never happens. `atomic_json` and
        `save_history` must write bytes directly.

        This test can only fail on Windows: `write_text` doesn't translate
        anything on POSIX, so a regression here stays green on macOS or
        Linux. It's meaningful on CI, which runs `windows-latest` to match
        the store's OS; on other platforms it's not a real check, a gap this
        project also documents in `test_proprieta_non_provate`.
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
