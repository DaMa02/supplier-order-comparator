#!/usr/bin/env python3
"""La rete sotto le memorie che nessun ricalcolo sa rifare.

Il commit `ae2e214` (18 agosto 2026) aveva messo sotto git stato, conferme,
storico ordini e memoria AI, con la ragione scritta: «se muore il disco non si
recuperano». Il commit `6007574`, la sera dopo, le ha tolte — giustamente,
perche' cambiano mentre il programma gira e l'avvio del PC del negozio riporta
indietro i file tracciati — e non ha messo niente al loro posto. Da quel giorno
al 19 agosto quelle memorie vivevano **solo** sul disco del negozio.

Qui si prova la copia che le rimette al sicuro, e le tre proprieta' che la
rendono una rete e non un altro modo di rompersi:

1. lo zip contiene le memorie che ci sono, e le trova anche in sottocartella;
2. **non solleva mai**: una cartella non scrivibile lascia il programma acceso;
3. le copie vecchie non si accumulano all'infinito, e la piu' recente non e'
   quella che si cancella.
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
        """Un'installazione nuova non ha ne' ordini ne' conferme: la copia si
        fa lo stesso, con quello che c'e'."""

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
        """La proprieta' che conta: se la copia potesse fermare l'avvio,
        sarebbe un modo nuovo di non far partire il programma."""

        self.scrivi_le_memorie()
        self.copie.mkdir()
        self.copie.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.copie.chmod, stat.S_IRWXU)

        if os.access(self.copie, os.W_OK):  # pragma: no cover - girando da root
            self.skipTest("qui si scrive comunque: i permessi non valgono per questo utente")

        self.assertIsNone(self.copia())

    def test_una_memoria_che_sparisce_mentre_si_copia_non_solleva(self) -> None:
        """Il programma gira: fra il momento in cui si guarda che il file c'e'
        e quello in cui lo si legge, un ricalcolo puo' averlo sostituito."""

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
    """Il percorso cambia col sistema, e va provato su tutti e due: il negozio
    e' Windows, ma chi sviluppa e chi fa girare la CI non lo sono sempre."""

    def test_sta_fuori_dal_progetto(self) -> None:
        """La condizione che rende la copia una copia. Dentro il repository
        tornerebbe il problema di partenza: `.gitignore` ignora `app/data/`, e
        l'avvio riporta indietro i file tracciati."""

        cartella = launcher.cartella_delle_copie()

        self.assertNotIn(RADICE, cartella.parents)
        self.assertEqual(cartella.name, "copie")
        self.assertEqual(cartella.parent.name, "ComparaOrdini")

    def test_su_windows_segue_localappdata(self) -> None:
        """Il ramo del negozio, provato da qui: e' l'unico che conta davvero e
        sarebbe l'unico a non essere mai eseguito da nessuna prova."""

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
        """Se saltasse anche il ripiego, il programma resterebbe senza copie
        proprio sulla macchina configurata in modo strano."""

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
    """`decisioni_schemi.json` non era nell'elenco, e invece e' una memoria.

    Ci finiscono le colonne scritte a mano per i listini che il programma non
    e' riuscito a imparare: sopravvive al ricalcolo e a «Inizia nuova
    comparazione», e per quei fornitori e' l'unica cosa che rende il listino
    ancora leggibile. Perderla vuol dire riscriverla senza sapere che cosa
    c'era scritto.
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
        """Il caso normale: nessun listino ha avuto bisogno del ponte a mano."""

        self.scrivi_le_memorie()

        percorso = self.copia()

        with zipfile.ZipFile(percorso) as archivio:
            self.assertNotIn("current/decisioni_schemi.json", archivio.namelist())


class UnaCopiaCheNonRiesceLoDice(BancoDelleCopie):
    """⚠ «Non solleva mai» non vuol dire «non lo dice a nessuno».

    Tornava `None` sia quando non c'era niente da copiare sia quando la copia
    non si era potuta fare, e chi la chiama stampava una riga solo in caso di
    successo: cartella non scrivibile, disco pieno, antivirus sul temporaneo, e
    l'avvio sembrava normale. Si continuava a lavorare credendo che la rete di
    sicurezza ci fosse, e l'unica copia di `conferme.db` e' quella.
    """

    def test_il_motivo_arriva_a_chi_chiama(self) -> None:
        self.scrivi_le_memorie()
        motivi: list[str] = []

        percorso = launcher.copia_le_memorie(
            dati=self.dati,
            destinazione=self.dati / "current" / "state.json",  # non e' una cartella
            quando="2026-08-22",
            su_guasto=motivi.append,
        )

        self.assertIsNone(percorso)
        self.assertEqual(len(motivi), 1, motivi)
        # Il tipo del guasto e la sua frase: chi legge deve poter capire se e'
        # un permesso, uno spazio o un percorso.
        self.assertTrue(motivi[0].strip(), motivi)

    def test_niente_da_copiare_non_e_un_guasto(self) -> None:
        """Un'installazione nuova non ha ancora nessuna memoria: non c'e' niente da dire."""

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
