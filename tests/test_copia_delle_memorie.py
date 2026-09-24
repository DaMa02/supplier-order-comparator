#!/usr/bin/env python3
"""Backup copy of the state that recomputing the pipeline cannot regenerate.

`app/data/` (state, confirmations, order history, AI answer cache) is
git-ignored, because those files change while the program runs and starting
the store PC restores tracked files to their committed state. Without an
external backup, those files would live only on the store's disk.

This module tests the copy that backs them up, and the three properties that
make it a safety net rather than another way to fail:

1. the archive includes whatever memory files exist, found recursively;
2. it never raises: an unwritable destination folder must not block startup;
3. old copies don't accumulate forever, and pruning never deletes the most
   recent one.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import launcher  # noqa: E402


class BancoDelleCopie(unittest.TestCase):
    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)
        self.dati = self.radice / "data"
        self.copie = self.radice / "copie"
        (self.dati / "current").mkdir(parents=True)
        (self.dati / "history").mkdir(parents=True)

    def scrivi_le_memorie(self, *, tutte: bool = True) -> None:
        (self.dati / "current" / "state.json").write_text('{"stateVersion": 145}', encoding="utf-8")
        (self.dati / "memoria_ai.json").write_text('{"voci": []}', encoding="utf-8")
        if tutte:
            (self.dati / "history" / "conferme.db").write_bytes(b"SQLite format 3\x00")
            (self.dati / "history" / "orders.json").write_text("[]", encoding="utf-8")
            (self.dati / "adattatori_imparati.json").write_text('{"adapters": []}', encoding="utf-8")

    def copia(self, quando: str = "2026-08-19") -> Path | None:
        return launcher.copia_le_memorie(dati=self.dati, destinazione=self.copie, quando=quando)


class LoZipContieneLeMemorie(BancoDelleCopie):
    def test_ci_sono_tutte_e_cinque_col_loro_percorso(self) -> None:
        self.scrivi_le_memorie()

        percorso = self.copia()

        self.assertIsNotNone(percorso)
        with zipfile.ZipFile(percorso) as archivio:
            self.assertEqual(sorted(archivio.namelist()), [
                "adattatori_imparati.json",
                "current/state.json",
                "history/conferme.db",
                "history/orders.json",
                "memoria_ai.json",
            ])
            self.assertEqual(archivio.read("current/state.json"), b'{"stateVersion": 145}')

    def test_quelle_che_non_ci_sono_ancora_non_sono_un_errore(self) -> None:
        """A fresh install has no orders or confirmations yet: the backup still
        runs, with whatever exists."""

        self.scrivi_le_memorie(tutte=False)

        percorso = self.copia()

        self.assertIsNotNone(percorso)
        with zipfile.ZipFile(percorso) as archivio:
            self.assertEqual(sorted(archivio.namelist()), ["current/state.json", "memoria_ai.json"])

    def test_senza_nessuna_memoria_non_si_scrive_niente(self) -> None:
        self.assertIsNone(self.copia())
        self.assertFalse(self.copie.exists())

    def test_il_nome_e_la_data_e_lo_stesso_giorno_si_sovrascrive(self) -> None:
        self.scrivi_le_memorie()

        primo = self.copia("2026-08-19")
        (self.dati / "current" / "state.json").write_text('{"stateVersion": 146}', encoding="utf-8")
        secondo = self.copia("2026-08-19")

        self.assertEqual(primo, secondo)
        self.assertEqual(secondo.name, "2026-08-19.zip")
        with zipfile.ZipFile(secondo) as archivio:
            self.assertEqual(archivio.read("current/state.json"), b'{"stateVersion": 146}')
        self.assertEqual([voce.name for voce in self.copie.iterdir()], ["2026-08-19.zip"],
                         "il temporaneo non deve restare lì")


class UnaCopiaNonSpegneIlProgramma(BancoDelleCopie):
    def test_una_cartella_che_non_si_scrive_non_solleva(self) -> None:
        """A backup failure must never be a new way to prevent the program from starting."""

        self.scrivi_le_memorie()
        self.copie.mkdir()
        self.copie.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.copie.chmod, stat.S_IRWXU)

        if os.access(self.copie, os.W_OK):  # pragma: no cover - girando da root
            self.skipTest("qui si scrive comunque: i permessi non valgono per questo utente")

        self.assertIsNone(self.copia())

    def test_una_memoria_che_sparisce_mentre_si_copia_non_solleva(self) -> None:
        """The program keeps running: between checking a file exists and
        reading it, a recompute can replace it."""

        self.scrivi_le_memorie()
        vera = launcher.MEMORIE_DA_COPIARE

        def sparisci(*argomenti: object, **parametri: object) -> None:
            raise OSError("il file non c'è più")

        originale = zipfile.ZipFile.write
        zipfile.ZipFile.write = sparisci
        try:
            self.assertIsNone(self.copia())
        finally:
            zipfile.ZipFile.write = originale
        self.assertEqual(launcher.MEMORIE_DA_COPIARE, vera)
        self.assertEqual(list(self.copie.glob("*.zip")), [],
                         "un archivio monco non deve restare al posto di una copia")


class LeCopieVecchieNonSiAccumulano(BancoDelleCopie):
    def test_ne_restano_dieci_e_sono_le_piu_recenti(self) -> None:
        self.scrivi_le_memorie()

        for giorno in range(1, 15):
            self.copia(f"2026-08-{giorno:02d}")

        rimaste = sorted(voce.name for voce in self.copie.glob("*.zip"))
        self.assertEqual(len(rimaste), 10)
        self.assertEqual(rimaste[0], "2026-08-05.zip")
        self.assertEqual(rimaste[-1], "2026-08-14.zip")


class DoveFinisconoLeCopie(unittest.TestCase):
    """The backup path depends on the OS; both must be tested since the store
    runs Windows but development and CI don't always."""

    def test_sta_fuori_dal_progetto(self) -> None:
        """The backup must live outside the repo, or it hits the same problem
        it exists to solve: `.gitignore` ignores `app/data/`, and startup
        restores tracked files to their committed state."""

        cartella = launcher.cartella_delle_copie()

        self.assertNotIn(RADICE, cartella.parents)
        self.assertEqual(cartella.name, "copie")
        self.assertEqual(cartella.parent.name, "ComparaOrdini")

    def test_su_windows_segue_localappdata(self) -> None:
        """The path taken on the store's own OS; without this test it would be
        the one branch never exercised."""

        cartella = launcher.cartella_delle_copie(
            sistema="nt", ambiente={"LOCALAPPDATA": "/tmp/AppData/Local"},
        )

        self.assertEqual(cartella, Path("/tmp/AppData/Local/ComparaOrdini/copie"))

    def test_su_windows_appdata_e_il_secondo_tentativo(self) -> None:
        cartella = launcher.cartella_delle_copie(
            sistema="nt", ambiente={"APPDATA": "/tmp/AppData/Roaming"},
        )

        self.assertEqual(cartella, Path("/tmp/AppData/Roaming/ComparaOrdini/copie"))

    def test_senza_localappdata_c_e_un_ripiego(self) -> None:
        """Without this fallback, the program would have no backup path at all
        on a machine with unusual environment configuration."""

        cartella = launcher.cartella_delle_copie(sistema="nt", ambiente={})

        self.assertEqual(cartella, Path.home() / ".compara-ordini-copie")

    def test_su_linux_segue_xdg(self) -> None:
        cartella = launcher.cartella_delle_copie(
            sistema="posix", piattaforma="linux", ambiente={"XDG_DATA_HOME": "/tmp/dati"},
        )

        self.assertEqual(cartella, Path("/tmp/dati/ComparaOrdini/copie"))

    def test_su_macos_sta_in_application_support(self) -> None:
        cartella = launcher.cartella_delle_copie(sistema="posix", piattaforma="darwin", ambiente={})

        self.assertEqual(
            cartella, Path.home() / "Library" / "Application Support" / "ComparaOrdini" / "copie",
        )



class IlPonteAManoStaNellaCopia(BancoDelleCopie):
    """`decisioni_schemi.json` must be backed up along with the other state.

    It holds manually mapped columns for price lists the adapter learner
    couldn't handle on its own. It survives a recompute and "Inizia nuova
    comparazione", and for those suppliers it's the only thing that keeps
    the price list readable; losing it means rebuilding the mapping blind.
    """

    def test_ci_finisce_dentro(self) -> None:
        self.scrivi_le_memorie()
        (self.dati / "current" / "decisioni_schemi.json").write_text(
            '{"decisions": [{"file_name": "quercia.xlsx"}]}', encoding="utf-8")

        percorso = self.copia()

        with zipfile.ZipFile(percorso) as archivio:
            self.assertIn("current/decisioni_schemi.json", archivio.namelist())
            self.assertIn(b"quercia.xlsx", archivio.read("current/decisioni_schemi.json"))

    def test_se_non_c_e_non_cambia_niente(self) -> None:
        """The normal case: no price list needed a manual mapping."""

        self.scrivi_le_memorie()

        percorso = self.copia()

        with zipfile.ZipFile(percorso) as archivio:
            self.assertNotIn("current/decisioni_schemi.json", archivio.namelist())


class UnaCopiaCheNonRiesceLoDice(BancoDelleCopie):
    """Never raising doesn't mean never reporting failure.

    A `None` result must be distinguishable between "nothing to back up" and
    "the backup failed": an unwritable folder, a full disk, or antivirus
    locking the temp file must not look like a normal, silent success, since
    `conferme.db` has no other copy.
    """

    def test_il_motivo_arriva_a_chi_chiama(self) -> None:
        self.scrivi_le_memorie()
        motivi: list[str] = []

        percorso = launcher.copia_le_memorie(
            dati=self.dati,
            destinazione=self.dati / "current" / "state.json",  # not a directory
            quando="2026-08-22",
            su_guasto=motivi.append,
        )

        self.assertIsNone(percorso)
        self.assertEqual(len(motivi), 1, motivi)
        # The failure reason must say enough to tell a permission error from
        # a full disk from a bad path.
        self.assertTrue(motivi[0].strip(), motivi)

    def test_niente_da_copiare_non_e_un_guasto(self) -> None:
        """A fresh install has no memory files yet: there's nothing to report."""

        motivi: list[str] = []

        percorso = launcher.copia_le_memorie(
            dati=self.dati, destinazione=self.copie, quando="2026-08-22", su_guasto=motivi.append,
        )

        self.assertIsNone(percorso)
        self.assertEqual(motivi, [])

    def test_quando_riesce_non_dice_niente(self) -> None:
        self.scrivi_le_memorie()
        motivi: list[str] = []

        percorso = launcher.copia_le_memorie(
            dati=self.dati, destinazione=self.copie, quando="2026-08-22", su_guasto=motivi.append,
        )

        self.assertIsNotNone(percorso)
        self.assertEqual(motivi, [])


if __name__ == "__main__":
    unittest.main()
