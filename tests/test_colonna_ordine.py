#!/usr/bin/env python3
"""La colonna in cui si scrivono le quantità ordinate si cambia quando serve.

Prima del 18 agosto 2026 quella colonna si sceglieva **una volta sola**, nella
mappatura guidata, che si apre soltanto quando il programma non riconosce le
colonne di un documento. Per i fornitori conosciuti stava nel registro e la
pagina la mostrava e basta: per spostarla bisognava aprire un file JSON.

Le tre famiglie che questo lavoro deve trattare, e sono diverse davvero — le
differenze sono misurate sui listini veri della settimana del 17 agosto 2026:

* **BETULLA** ha un lettore dedicato e una riga di intestazione: la colonna C si
  chiama «ORDINE», e quel titolo è quello che il writer verifica;
* **CIPRESSO** dichiara `from_field_mapping`, cioè registro e mappatura
  confermata devono dire la **stessa** colonna, e la sua G non ha titolo (la
  cella è vuota su tutte e 3372 le righe);
* **LARICE** non ha **nessuna** riga di intestazione: sopra la colonna non c'è
  nessuna cella, e dichiararne una vuota lo bloccherebbe.

E i due rifiuti che contano: una colonna che il programma già legge (l'ordine
la azzera e la riscrive) e una colonna di formule (scriverci sopra le cancella
— la I di BETULLA ne ha 6380, misurate).
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from openpyxl import Workbook

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import colonna_ordine  # noqa: E402
import launcher  # noqa: E402
import registro  # noqa: E402
import schema_mapping  # noqa: E402
from inspect_sources import profile_file  # noqa: E402
from pipeline_jobs import LavoroGiaInCorso  # noqa: E402
from server import ReviewStore  # noqa: E402


# --------------------------------------------------------------------------
# Le regole di che cosa si dichiara, senza toccare né disco né registro.
# --------------------------------------------------------------------------
class CheCosaSiDichiara(unittest.TestCase):
    def test_una_colonna_con_titolo_diventa_l_intestazione_attesa(self) -> None:
        """`expected_header` è una misura del documento, non una preferenza."""

        nuova = colonna_ordine.dichiarazione_aggiornata(
            {"order_column": "C", "expected_header": "ORDINE"}, "K", "QUANTITA"
        )

        self.assertEqual(nuova["order_column"], "K")
        self.assertEqual(nuova["expected_header"], "QUANTITA")
        self.assertNotIn("allow_blank_header_if_confirmed", nuova)

    def test_una_colonna_senza_titolo_si_dichiara_vuota_e_confermata(self) -> None:
        """Senza questa dichiarazione la colonna verrebbe scritta **senza
        nessun controllo**: `expected_header` assente vuol dire «niente da
        verificare», quindi scegliere una colonna senza titolo spegnerebbe la
        difesa invece di accenderla."""

        nuova = colonna_ordine.dichiarazione_aggiornata(
            {"order_column": "C", "expected_header": "ORDINE"}, "J", ""
        )

        self.assertNotIn("expected_header", nuova)
        self.assertIs(nuova["allow_blank_header_if_confirmed"], True)
        self.assertIs(nuova["order_header_blank_confirmed"], True)

    def test_senza_riga_di_intestazione_non_si_dichiara_nessuna_cella_vuota(self) -> None:
        """Il caso LARICE. Sopra la colonna non c'è nessuna cella da guardare:
        dichiararla vuota non rende il controllo più severo, lo rende
        impossibile — `source_rule` pretende una riga di intestazione per farlo
        e si fermerebbe accusando il registro di una cosa che il documento non
        ha."""

        nuova = colonna_ordine.dichiarazione_aggiornata(
            {"order_column": "D"}, "S", "", c_e_intestazione=False
        )

        self.assertEqual(nuova["order_column"], "S")
        self.assertNotIn("expected_header", nuova)
        self.assertNotIn("allow_blank_header_if_confirmed", nuova)
        self.assertNotIn("order_header_blank_confirmed", nuova)

    def test_la_procedura_di_scrittura_resta_dov_era(self) -> None:
        """Il `.xls` di Noce si scrive in posizione: cambiare colonna non
        cambia il modo in cui quel documento si tocca."""

        nuova = colonna_ordine.dichiarazione_aggiornata(
            {
                "order_column": "I",
                "from_field_mapping": True,
                "mode": "patch_xls_in_posizione",
                "required_columns": ["ean"],
            },
            "N",
            "",
        )

        self.assertEqual(nuova["order_column"], "N")
        self.assertIs(nuova["from_field_mapping"], True)
        self.assertEqual(nuova["mode"], "patch_xls_in_posizione")
        self.assertEqual(nuova["required_columns"], ["ean"])

    def test_la_mappatura_dice_la_stessa_cosa_del_registro(self) -> None:
        registro_nuovo = colonna_ordine.dichiarazione_aggiornata(
            {"order_column": "G", "from_field_mapping": True}, "H", "ORDINI"
        )
        mappatura = colonna_ordine.mappatura_aggiornata(
            {"order_column": "G", "order_header_blank_confirmed": True}, "H", "ORDINI"
        )

        self.assertEqual(registro_nuovo["order_column"], mappatura["order_column"])
        self.assertEqual(mappatura["order_header_expected"], "ORDINI")
        self.assertNotIn("order_header_blank_confirmed", mappatura)

    def test_la_colonna_si_indica_per_lettera_o_per_numero(self) -> None:
        """La pagina manda il numero, il registro scrive la lettera: la
        traduzione sta in un posto solo, altrimenti i due modi divergono su
        «AA» contro 27."""

        self.assertEqual(colonna_ordine.indice_scelto("C"), 3)
        self.assertEqual(colonna_ordine.indice_scelto("c"), 3)
        self.assertEqual(colonna_ordine.indice_scelto(3), 3)
        self.assertEqual(colonna_ordine.indice_scelto("3"), 3)
        self.assertEqual(colonna_ordine.indice_scelto("AA"), 27)
        self.assertEqual(colonna_ordine.lettera_di_indice(27), "AA")
        for scarto in (None, "", 0, -2, "3C", True):
            self.assertIsNone(colonna_ordine.indice_scelto(scarto), scarto)


# --------------------------------------------------------------------------
# Le colonne fra cui si sceglie.
# --------------------------------------------------------------------------
class ColonneFraCuiScegliere(unittest.TestCase):
    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)

    def profilo(self, righe: list[list[Any]], nome: str = "listino.xlsx") -> dict[str, Any]:
        percorso = self.radice / nome
        libro = Workbook()
        pagina = libro.active
        pagina.title = "Sheet1"
        for riga in righe:
            pagina.append(riga)
        libro.save(percorso)
        libro.close()
        return profile_file(percorso)

    def test_offre_anche_la_prima_colonna_libera_in_fondo(self) -> None:
        """Un listino che una colonna d'ordine non ce l'ha ancora deve poterne
        ricevere una: se si offrissero soltanto le colonne scritte, non ci
        sarebbe nessun posto dove metterla."""

        profilo = self.profilo([["EAN", "Descrizione"], ["8000000000001", "SAPONE"]])

        colonne = schema_mapping.colonne_del_foglio(profilo, "Sheet1", 1, 2)

        self.assertEqual([voce["lettera"] for voce in colonne], ["A", "B", "C"])
        self.assertEqual(colonne[2]["valori"], 0)

    def test_conta_le_formule_di_ogni_colonna(self) -> None:
        """Il conto non è un campione: `inspect_sources` attraversa ogni riga.
        È il numero che decide, perché scrivere l'ordine sopra una colonna di
        formule le cancella."""

        profilo = self.profilo([
            ["EAN", "PzCt", "TOTALI"],
            ["8000000000001", 6, "=B2*2"],
            ["8000000000002", 4, "=B3*2"],
        ])

        colonne = {voce["lettera"]: voce for voce in
                   schema_mapping.colonne_del_foglio(profilo, "Sheet1", 1, 2)}

        self.assertEqual(colonne["C"]["formule"], 2)
        self.assertEqual(colonne["B"]["formule"], 0)
        self.assertEqual(colonne["B"]["esempio"], "6")

    def test_una_colonna_d_ordine_oltre_l_ultima_scritta_resta_raggiungibile(self) -> None:
        """Il caso CIPRESSO: la G è vuota su tutte le righe, quindi nel profilo
        non compare proprio. Senza `fino_a`, l'unica colonna nuova che si
        potrebbe scegliere sarebbe quella accanto a quella già in uso."""

        profilo = self.profilo([["COD", "DES"], ["019654", "SPAZZOLA"]])

        senza = schema_mapping.colonne_del_foglio(profilo, "Sheet1", 1, 2)
        con = schema_mapping.colonne_del_foglio(profilo, "Sheet1", 1, 2, fino_a=4)

        self.assertEqual([voce["lettera"] for voce in senza], ["A", "B", "C"])
        self.assertEqual([voce["lettera"] for voce in con], ["A", "B", "C", "D", "E"])

    def test_le_colonne_occupate_si_vedono_marcate_non_tolte(self) -> None:
        """Nasconderle farebbe sembrare che il documento abbia meno colonne di
        quante ne ha, e chi cerca «quella dopo il prezzo» conta quelle che
        vede."""

        effettiva = {
            "columns": [{"campo": "unit_price_net", "etichetta": "Prezzo", "colonna": 2}],
            "orderColumn": {"colonna": 3, "lettera": "C"},
        }
        colonne = colonna_ordine.colonne_per_la_scelta(effettiva, [
            {"colonna": 1, "lettera": "A"},
            {"colonna": 2, "lettera": "B"},
            {"colonna": 3, "lettera": "C"},
        ])

        self.assertEqual([voce["lettera"] for voce in colonne], ["A", "B", "C"])
        self.assertEqual(colonne[1]["occupataDa"], "Prezzo")
        self.assertIs(colonne[1]["scegliibile"], False)
        self.assertIs(colonne[2]["attuale"], True)


# --------------------------------------------------------------------------
# Il listino che non è un .xlsx: si legge, ma l'ordine non ci si scrive.
# --------------------------------------------------------------------------
class UnListinoChePerScriverciDentroNonVaBene(unittest.TestCase):
    """Un `.xls` arriva fin qui, e fino al 4 settembre 2026 esplodeva.

    `source_rule` apre il documento con openpyxl, che di un Excel 97-2003 non
    sa niente: l'utente riceveva la frase inglese della libreria — «openpyxl
    does not support the old .xls file format, please use xlrd to read this
    file» — dentro «Non riesco a leggere il listino LARICE», e la leggeva come
    «lo legge ma non ci sa scrivere le quantità». Il rimedio è una riga di
    Excel, e va detto.
    """

    # I primi otto byte di un Excel 97-2003. Il formato lo decidono i byte e
    # non l'estensione: è la stessa firma che legge `inspect_sources`.
    FIRMA_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)

    def test_un_xls_senza_procedura_dichiarata_dice_che_cosa_fare(self) -> None:
        sorgente = self.radice / "New Larice.xls"
        sorgente.write_bytes(self.FIRMA_OLE2 + b"\x00" * 512)

        regola, motivo = launcher.source_rule(
            "larice", sorgente, {},
            {"id": "prova_v1", "supplier_id": "larice", "order_write": {
                "sheet": "FIRST", "header_row": 11, "data_start_row": 12, "order_column": "D",
            }},
        )

        self.assertIsNone(regola)
        self.assertIn("Excel 97-2003", motivo)
        self.assertIn(".xlsx", motivo)
        self.assertNotIn("openpyxl", motivo)

    def test_l_xls_che_si_compila_in_posizione_non_incontra_la_guardia(self) -> None:
        """Noce il suo `.xls` lo compila davvero, e non deve cambiare:
        quel ramo esce prima, e la guardia non lo vede nemmeno passare."""

        sorgente = self.radice / "formattato.xls"
        sorgente.write_bytes(self.FIRMA_OLE2 + b"\x00" * 512)

        regola, motivo = launcher.source_rule(
            "noce", sorgente,
            {"fieldMapping": {
                "sheet": "Foglio1", "header_row": 1, "data_start_row": 2,
                "order_column": "I", "columns": {"ean": "cat"},
            }},
            {"id": "prova_v1", "supplier_id": "noce", "order_write": {
                "mode": "patch_xls_in_posizione", "from_field_mapping": True,
                "sheet": "Foglio1", "header_row": 1, "data_start_row": 2, "order_column": "I",
            }},
        )

        self.assertIsNone(motivo)
        self.assertEqual(regola["compilazione"], "patch_xls_in_posizione")


# --------------------------------------------------------------------------
# La verifica vera, quella che attiva la compilazione.
# --------------------------------------------------------------------------
class LaVerificaDelDocumento(unittest.TestCase):
    """`source_rule` è la funzione che decide se un listino si compila. Qui si
    prova il ramo nuovo: la conferma della cella vuota dichiarata dentro
    `order_write`, che è l'unico posto in cui può stare per un fornitore con un
    lettore dedicato e nessuna mappatura."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)

    def listino(self, intestazioni: list[Any], righe: list[list[Any]]) -> Path:
        percorso = self.radice / "listino.xlsx"
        libro = Workbook()
        pagina = libro.active
        pagina.title = "Sheet1"
        pagina.append(intestazioni)
        for riga in righe:
            pagina.append(riga)
        libro.save(percorso)
        libro.close()
        return percorso

    @staticmethod
    def adattatore(**scrittura: Any) -> dict[str, Any]:
        base = {
            "sheet": "FIRST",
            "header_row": 1,
            "data_start_row": 2,
            "order_column": "C",
        }
        base.update(scrittura)
        return {"id": "prova_v1", "supplier_id": "betulla", "order_write": base}

    def test_la_conferma_della_cella_vuota_puo_stare_nel_registro(self) -> None:
        sorgente = self.listino(["EAN", "Descrizione", None], [["8000000000001", "SAPONE", None]])

        regola, motivo = launcher.source_rule(
            "betulla", sorgente, {},
            self.adattatore(allow_blank_header_if_confirmed=True, order_header_blank_confirmed=True),
        )

        self.assertIsNone(motivo)
        self.assertIsNotNone(regola)
        self.assertEqual(regola["order_column"], "C")
        self.assertIs(regola["blank_header_confirmed"], True)

    def test_una_cella_che_non_e_piu_vuota_ferma_la_compilazione(self) -> None:
        """La dichiarazione non basta: si va a guardare il documento di oggi."""

        sorgente = self.listino(["EAN", "Descrizione", "PREZZO"], [["8000000000001", "SAPONE", 2]])

        regola, motivo = launcher.source_rule(
            "betulla", sorgente, {},
            self.adattatore(allow_blank_header_if_confirmed=True, order_header_blank_confirmed=True),
        )

        self.assertIsNone(regola)
        self.assertIn("non è più vuota", motivo)

    def test_del_testo_sotto_la_colonna_ferma_la_compilazione(self) -> None:
        sorgente = self.listino(
            ["EAN", "Descrizione", None],
            [["8000000000001", "SAPONE", None], ["8000000000002", "OLIO", "OMAGGIO"]],
        )

        regola, motivo = launcher.source_rule(
            "betulla", sorgente, {},
            self.adattatore(allow_blank_header_if_confirmed=True, order_header_blank_confirmed=True),
        )

        self.assertIsNone(regola)
        self.assertIn("riga 3", motivo)


class ConCheCosaSiScriveDentroQuestoDocumento(unittest.TestCase):
    """Chi scrive l'ordine lo decide la DECISIONE, non il nome del fornitore.

    ⚠ E' la quinta lezione dello stesso difetto — un `if supplier == "..."`
    tolto quattro volte da questo progetto, e la quinta il 21 agosto 2026 su
    BETULLA — ma sul lato della **scrittura**, dove costa di piu': un ordine
    scritto nella colonna sbagliata esce dal programma e va al fornitore.

    Due casi in cui il nome del fornitore non basta piu', e tutti e due esistono
    dal 22 agosto 2026: CIPRESSO ha due schemi con due `order_write` diversi, e
    la colonna d'ordine spostata dalla pagina scrive una voce `__locale`.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)
        self.registro = self.radice / "adapters.json"
        self.registro.write_text(json.dumps({"schema_version": 1, "adapters": [
            {"id": "tizio_v1", "supplier_id": "tizio", "kind": "supplier",
             "display_name": "TIZIO",
             "order_write": {"sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                             "order_column": "C", "allow_blank_header_if_confirmed": True}},
            {"id": "tizio_secondo_v1", "supplier_id": "tizio", "kind": "supplier",
             "display_name": "TIZIO",
             "order_write": {"sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                             "order_column": "D", "expected_header": "ORDINE"}},
        ]}, ensure_ascii=False), encoding="utf-8")
        self.vecchio = launcher.ADAPTERS_PATH
        launcher.ADAPTERS_PATH = self.registro
        self.addCleanup(setattr, launcher, "ADAPTERS_PATH", self.vecchio)
        self.registro_di_registro = registro.REGISTRO
        registro.REGISTRO = self.registro
        self.addCleanup(setattr, registro, "REGISTRO", self.registro_di_registro)

    def per_fornitore(self) -> dict[str, Any]:
        return launcher.adattatori_compilabili(self.registro)

    def test_fra_due_schemi_dello_stesso_fornitore_vale_quello_della_decisione(self) -> None:
        """⚠ Prendendo il primo del registro si perdeva `expected_header`,
        cioe' il controllo che sopra la colonna ci sia scritto ORDINE prima di
        scriverci dentro. E' la stessa difesa che una voce imparata aveva
        tolto a BETULLA il 21 agosto 2026.
        """

        scelto = launcher.adattatore_del_documento(
            {"adapterId": "tizio_secondo_v1"}, "tizio", self.per_fornitore(), self.registro,
        )

        self.assertEqual(scelto["id"], "tizio_secondo_v1")
        self.assertEqual(scelto["order_write"]["order_column"], "D")
        self.assertEqual(scelto["order_write"]["expected_header"], "ORDINE")

    def test_la_colonna_spostata_a_mano_vince_su_quella_spedita(self) -> None:
        """La decisione della run dice ancora l'id spedito: cercarlo cosi'
        com'e' scriverebbe l'ordine nella colonna di prima."""

        imparato = registro.percorso_imparato(self.registro)
        # Dal 5 settembre 2026 una voce sopra una spedita vale solo con il
        # timbro della spedita su cui e' nata: senza, sarebbe una voce vecchia
        # da mettere da parte, non la scelta fatta oggi.
        spedita = registro.adattatore("tizio_v1", self.registro)
        imparato.write_text(json.dumps({"schema_version": 1, "adapters": [
            {"id": "tizio_v1__locale", "supplier_id": "tizio", "kind": "supplier",
             "display_name": "TIZIO", "derivato_da": "tizio_v1",
             "sopra_spedito": registro.sopra_spedito_di(spedita),
             "order_write": {"sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                             "order_column": "H", "allow_blank_header_if_confirmed": True}},
        ]}, ensure_ascii=False), encoding="utf-8")

        scelto = launcher.adattatore_del_documento(
            {"adapterId": "tizio_v1"}, "tizio", self.per_fornitore(), self.registro,
        )

        self.assertEqual(scelto["id"], "tizio_v1__locale")
        self.assertEqual(scelto["order_write"]["order_column"], "H")

    def test_una_scheda_senza_adattatore_ripiega_sul_fornitore(self) -> None:
        """I confronti fatti prima non dichiarano nessun adattatore nella
        scheda: meglio la voce di quel fornitore che nessuna voce."""

        scelto = launcher.adattatore_del_documento({}, "tizio", self.per_fornitore(), self.registro)

        self.assertEqual(scelto["id"], "tizio_v1")

    def test_un_adattatore_che_il_registro_non_conosce_ripiega_sul_fornitore(self) -> None:
        scelto = launcher.adattatore_del_documento(
            {"adapterId": "sparito_v1"}, "tizio", self.per_fornitore(), self.registro,
        )

        self.assertEqual(scelto["id"], "tizio_v1")


# --------------------------------------------------------------------------
# Il comando completo, con registro e confronto veri (di prova).
# --------------------------------------------------------------------------
class IlComandoCompleto(unittest.TestCase):
    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)
        self.dati = self.radice / "data"
        self.corrente = self.dati / "current"
        self.esecuzioni = self.corrente / "esecuzioni"
        self.uploads = self.corrente / "uploads"
        self.uploads.mkdir(parents=True)
        (self.esecuzioni / "run-prova").mkdir(parents=True)
        self.registro = self.radice / "adapters.json"
        # ⚠ Il registro di prova va messo anche dove lo cercano le funzioni che
        # lo leggono senza percorso (`fornitori_compilabili`): senza, la prova
        # girerebbe per meta' sul registro vero del progetto.
        self.registro_vero = launcher.ADAPTERS_PATH
        launcher.ADAPTERS_PATH = self.registro
        self.addCleanup(setattr, launcher, "ADAPTERS_PATH", self.registro_vero)
        self.registro_di_registro = registro.REGISTRO
        registro.REGISTRO = self.registro
        self.addCleanup(setattr, registro, "REGISTRO", self.registro_di_registro)

    # -- gli ingredienti ---------------------------------------------------
    def scrivi_listino(self) -> Path:
        percorso = self.uploads / "listino.xlsx"
        libro = Workbook()
        pagina = libro.active
        pagina.title = "Sheet1"
        pagina.append(["EAN", "Descrizione", "ORDINE", "PzCt", "Prezzo", None, "TOTALI"])
        pagina.append(["8000000000001", "SAPONE", None, 6, 1.5, None, "=D2*E2"])
        pagina.append(["8000000000002", "OLIO", None, 4, 2.5, None, "=D3*E3"])
        libro.save(percorso)
        libro.close()
        return percorso

    def scrivi_registro(self, **extra: Any) -> None:
        adattatore = {
            "id": "prova_v1",
            "supplier_id": "betulla",
            "display_name": "BETULLA",
            "kind": "supplier",
            "file_types": [".xlsx"],
            "header_signature": {
                "kind": "headers",
                "sheet": "FIRST",
                "header_row": 1,
                "data_start_row": 2,
                # ⚠ Le posizioni si scrivono con i token delle INTESTAZIONI, non
                # con i nomi dei campi: `_verifica_posizioni` cerca «descrizione»
                # nella riga del documento, e «description» non lo trova mai.
                # Scritte cosi' facevano uscire questo documento SCHEMA_VARIATO
                # sempre, e la prova sul riconoscimento non provava piu' niente.
                "columns": {"ean": 1, "descrizione": 2, "pzct": 4, "prezzo": 5},
                "required": ["EAN", "Descrizione", "PzCt", "Prezzo"],
            },
            "header_aliases": {
                "ean": ["EAN"],
                "description": ["Descrizione"],
                "pieces_per_carton": ["PzCt"],
                "unit_price_net": ["Prezzo"],
            },
            "order_write": {
                "sheet": "FIRST",
                "header_row": 1,
                "data_start_row": 2,
                "order_column": "C",
                "expected_header": "ORDINE",
            },
        }
        adattatore.update(extra)
        self.registro.write_text(
            json.dumps({"schema_version": 1, "adapters": [adattatore]}, ensure_ascii=False),
            encoding="utf-8",
        )

    def scrivi_run(self, sorgente: Path) -> None:
        cartella = self.esecuzioni / "run-prova"
        profilo = profile_file(sorgente)
        profilo["profile_id"] = "prova"
        (cartella / "input_profiles.json").write_text(
            json.dumps({"profiles": [profilo]}, ensure_ascii=False), encoding="utf-8",
        )
        (cartella / "input_manifest.json").write_text(
            json.dumps({"files": [{
                "file_name": sorgente.name,
                "profile_id": "prova",
                "ai_preflight": {
                    "role": "supplier",
                    "supplier_id": "betulla",
                    "adapter_id": "prova_v1",
                },
            }]}, ensure_ascii=False),
            encoding="utf-8",
        )

    def scrivi_confronto(self, sorgente: Path) -> None:
        (self.corrente).mkdir(parents=True, exist_ok=True)
        (self.corrente / "review_data.json").write_text(json.dumps({
            "run": {"id": "run-prova", "pipelineRunId": "run-prova", "status": "ready"},
            "files": [{
                "id": "file:1",
                "name": sorgente.name,
                "role": "supplier",
                "supplier": "BETULLA",
                "supplierId": "betulla",
                "sourcePath": str(sorgente),
                "adapterId": "prova_v1",
            }],
            "suppliers": [{"id": "betulla", "name": "BETULLA", "minimumOrder": 0}],
            "products": [],
            "warnings": [],
        }, ensure_ascii=False), encoding="utf-8")

    def negozio(self) -> ReviewStore:
        sorgente = self.scrivi_listino()
        self.scrivi_registro()
        self.scrivi_run(sorgente)
        self.scrivi_confronto(sorgente)
        store = ReviewStore(
            self.corrente / "review_data.json",
            self.corrente / "state.json",
            self.uploads,
            self.corrente / "outputs",
            self.corrente / "writer_config.json",
            self.dati / "history",
            self.corrente / "ordini",
            conferme_path=self.dati / "history" / "conferme.db",
        )
        store.pipeline_jobs.configurazione = dataclasses.replace(
            store.pipeline_jobs.configurazione, adapters_path=self.registro,
        )
        self.addCleanup(self.chiudi, store)
        return store

    @staticmethod
    def chiudi(store: ReviewStore) -> None:
        magazzino = getattr(store, "_conferme", None)
        if magazzino is not None:
            magazzino.chiudi()

    def voce_del_registro(self, identificativo: str = "prova_v1") -> dict[str, Any]:
        """La voce come la vede il programma: spedito e imparato insieme.

        ⚠ Dal 19 agosto 2026 la colonna d'ordine cambiata qui non torna in
        `references/adapters.json` — che sta sotto git e l'avvio del negozio
        riporta indietro — ma nel registro imparato, fuori da git.

        ⚠ E dal 22 agosto 2026 non ci torna nemmeno con lo stesso `id`: la voce
        nuova e' `prova_v1__locale`, e quella spedita resta dov'era, intera. Si
        chiede `voce_in_uso` e non `adattatore`, che e' la stessa domanda che fa
        il programma quando deve sapere con che cosa scrive l'ordine di oggi.
        """

        letta = registro.voce_in_uso(identificativo, registro.adattatori(self.registro))
        if not letta:
            raise AssertionError(f"«{identificativo}» non è nel registro effettivo")
        return letta

    def scrittura_nel_registro(self) -> dict[str, Any]:
        return self.voce_del_registro()["order_write"]

    # -- le prove ----------------------------------------------------------
    def test_le_colonne_offerte_dicono_che_cosa_contengono(self) -> None:
        store = self.negozio()

        esito = store.colonne_d_ordine("betulla")
        per_lettera = {voce["lettera"]: voce for voce in esito["colonne"]}

        self.assertEqual(esito["attuale"]["lettera"], "C")
        self.assertEqual(per_lettera["B"]["occupataDa"], "Nome del prodotto")
        self.assertIs(per_lettera["B"]["scegliibile"], False)
        self.assertEqual(per_lettera["G"]["formule"], 2)
        self.assertIs(per_lettera["F"]["scegliibile"], True)

    def test_la_colonna_nuova_entra_nel_registro_e_nella_compilazione(self) -> None:
        store = self.negozio()

        esito = store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        self.assertIs(esito["ok"], True)
        self.assertEqual(esito["colonna"], "F")
        self.assertEqual(self.scrittura_nel_registro()["order_column"], "F")
        # Non c'e' titolo sopra la F: si dichiara vuota, e il writer lo
        # ricontrollera' sul documento prima di scrivere.
        self.assertIs(self.scrittura_nel_registro()["allow_blank_header_if_confirmed"], True)
        configurazione = json.loads((self.corrente / "writer_config.json").read_text(encoding="utf-8"))
        self.assertEqual(configurazione["supplier_write_rules"]["betulla"]["order_column"], "F")

    def test_se_la_scrittura_non_si_rifa_lo_dice_invece_di_tacere(self) -> None:
        """⚠ A quel punto la colonna **è già cambiata**: la dichiarazione è
        scritta e ha passato la verifica sul documento. Lasciar uscire
        l'eccezione direbbe che non è successo niente, mentre è successo quasi
        tutto — la stessa mezza verità che mostrava il riquadro verde per una
        compilazione senza nessuna copia."""

        store = self.negozio()

        def non_si_rifa(_review):
            raise RuntimeError("Writer XLSX non attivato; manca: Node 18+")

        store.riconfigura_compilazione = non_si_rifa

        esito = store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        self.assertIs(esito["ok"], True)
        self.assertEqual(self.scrittura_nel_registro()["order_column"], "F")
        self.assertIn("Node 18+", esito["avviso"])
        self.assertIn("Node 18+", esito["message"])
        self.assertNotIn("Vale da subito", esito["message"])

    def test_il_registro_tiene_la_versione_di_prima(self) -> None:
        """Se la settimana dopo la compilazione peggiora, l'unico modo di
        capire che cosa è cambiato è avere ancora sotto gli occhi la versione
        precedente."""

        store = self.negozio()

        store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        voce = self.voce_del_registro()
        self.assertEqual(voce["schema_version"], 2)
        self.assertEqual(voce["previous_versions"][0]["order_write"]["order_column"], "C")

    def test_la_colonna_spostata_non_entra_nel_registro_spedito(self) -> None:
        """Spostare la colonna d'ordine e' una decisione presa in negozio, e
        finiva in un file che l'avvio del PC riporta indietro: chi la spostava
        se la ritrovava dov'era il doppio clic dopo, e la rispostava."""

        store = self.negozio()
        spedito_prima = self.registro.read_bytes()

        store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        self.assertEqual(self.registro.read_bytes(), spedito_prima)
        imparato = registro.percorso_imparato(self.registro)
        self.assertTrue(imparato.is_file())
        self.assertEqual(self.voce_del_registro()["order_write"]["order_column"], "F")

    def test_la_voce_spedita_resta_intera_e_quella_nuova_prende_un_id_suo(self) -> None:
        """⚠ Fino al 22 agosto 2026 spostare la colonna d'ordine scriveva nel
        registro imparato una **fotocopia completa** della voce spedita, con lo
        stesso `id`: da quel momento la spedita spariva sotto, e nessun
        aggiornamento di quell'adattatore arrivava piu' — le condizioni
        commerciali, gli alias delle intestazioni, le posizioni delle colonne.
        E' la stessa malattia di `offerte_v1`, dove l'impronta stretta appena
        scritta e' rimasta spenta sotto una copia imparata.
        """

        store = self.negozio()
        spedita_prima = registro.adattatore("prova_v1", self.registro)

        store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        # La voce nuova ha un id suo, e dice da dove viene.
        imparate = json.loads(
            registro.percorso_imparato(self.registro).read_text(encoding="utf-8")
        )["adapters"]
        self.assertEqual([voce["id"] for voce in imparate], ["prova_v1__locale"])
        self.assertEqual(imparate[0]["derivato_da"], "prova_v1")
        self.assertEqual(imparate[0]["order_write"]["order_column"], "F")

        # E la spedita e' ancora li', identica: non e' stata coperta da niente.
        self.assertEqual(registro.adattatore("prova_v1", self.registro), spedita_prima)
        self.assertEqual(spedita_prima["order_write"]["order_column"], "C")

    def test_la_pagina_mostra_subito_la_colonna_nuova(self) -> None:
        """La decisione della run dice ancora «prova_v1»: chi cercasse quell'id
        cosi' com'e' ritroverebbe la voce spedita, con la colonna di prima, e
        si rispostherebbe una colonna gia' spostata."""

        store = self.negozio()
        store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        documento = store.pipeline_jobs.documento_del_fornitore("betulla")

        self.assertEqual(documento["adattatore"]["id"], "prova_v1__locale")
        self.assertEqual(documento["effettiva"]["orderColumn"]["lettera"], "F")
        self.assertEqual(store.colonne_d_ordine("betulla")["attuale"]["lettera"], "F")

    def test_al_prossimo_ricalcolo_la_colonna_resta_quella_nuova(self) -> None:
        """La prova che dice se la mossa e' servita a qualcosa.

        ⚠ Il prossimo ricalcolo non guarda nessuna decisione vecchia: rifa'
        `riconosci` sul documento, e i candidati sono due — la voce spedita e
        quella locale. Le due leggono il documento **esattamente allo stesso
        modo**, perche' fra loro cambia solo dove si scrive l'ordine: stessa
        confidenza, stesse verifiche, stesse obbligatorie. Finche' a parita'
        piena vinceva la spedita, il lunedi' dopo la colonna tornava dov'era e
        nessuno lo diceva.
        """

        sorgente = self.uploads / "listino.xlsx"
        store = self.negozio()
        store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        esito = registro.riconosci(profile_file(sorgente)["details"], self.registro)

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "prova_v1__locale")
        vincente = registro.adattatore("prova_v1__locale", self.registro)
        self.assertEqual(vincente["order_write"]["order_column"], "F")

    def test_spostarla_due_volte_non_moltiplica_le_voci(self) -> None:
        """Ogni spostamento e' una versione della stessa voce locale, non una
        voce nuova: un registro che cresce a ogni ripensamento diventa
        illeggibile proprio quando serve capirci qualcosa."""

        store = self.negozio()
        store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})
        store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "H"})

        imparate = json.loads(
            registro.percorso_imparato(self.registro).read_text(encoding="utf-8")
        )["adapters"]
        self.assertEqual([voce["id"] for voce in imparate], ["prova_v1__locale"])
        self.assertEqual(imparate[0]["order_write"]["order_column"], "H")
        self.assertEqual(imparate[0]["schema_version"], 3)
        # La storia parte dalla spedita e passa da F.
        storia = [voce["order_write"]["order_column"] for voce in imparate[0]["previous_versions"]]
        self.assertEqual(storia, ["C", "F"])

    def test_una_colonna_di_formule_si_rifiuta(self) -> None:
        store = self.negozio()

        with self.assertRaises(ValueError) as errore:
            store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "G"})

        self.assertIn("formule", str(errore.exception))
        self.assertEqual(self.scrittura_nel_registro()["order_column"], "C")

    def test_una_colonna_che_il_programma_legge_si_rifiuta(self) -> None:
        store = self.negozio()

        with self.assertRaises(ValueError) as errore:
            store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "E"})

        self.assertIn("Prezzo", str(errore.exception))
        self.assertEqual(self.scrittura_nel_registro()["order_column"], "C")

    def test_una_colonna_fuori_dal_foglio_si_rifiuta(self) -> None:
        store = self.negozio()

        with self.assertRaises(ValueError):
            store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "ZZ"})

        self.assertEqual(self.scrittura_nel_registro()["order_column"], "C")

    def test_se_la_verifica_del_documento_non_passa_non_si_scrive_niente(self) -> None:
        """Il cancello vero non è in questo modulo: è `source_rule`, la stessa
        funzione che attiva la compilazione. Qui si prova che quando lei dice di
        no, il registro non viene toccato — altrimenti resterebbe scritta una
        colonna che nessuna settimana potrà usare."""

        store = self.negozio()
        # Una riga di dati **oltre** la fine del foglio: la verifica si ferma.
        self.scrivi_registro(order_write={
            "sheet": "FIRST", "header_row": 1, "data_start_row": 99,
            "order_column": "C", "expected_header": "ORDINE",
        })

        with self.assertRaises(ValueError) as errore:
            store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        self.assertIn("riga 99", str(errore.exception))
        self.assertEqual(self.scrittura_nel_registro()["order_column"], "C")
        self.assertFalse((self.corrente / "writer_config.json").is_file())

    def test_un_fornitore_che_non_e_nel_confronto_lo_dice(self) -> None:
        store = self.negozio()

        with self.assertRaises(ValueError) as errore:
            store.cambia_colonna_d_ordine({"supplierId": "larice", "colonna": "F"})

        self.assertIn("nessun listino di questo fornitore", str(errore.exception))

    def test_durante_un_ricalcolo_non_si_cambia(self) -> None:
        """La configurazione di scrittura si rifà a fine ricalcolo: cambiarla
        mentre gira vorrebbe dire vederla sovrascritta un attimo dopo, senza
        una parola."""

        store = self.negozio()
        store.pipeline_jobs.in_corso = lambda: True

        with self.assertRaises(LavoroGiaInCorso):
            store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        self.assertEqual(self.scrittura_nel_registro()["order_column"], "C")

    def test_per_i_fornitori_con_mappatura_si_cambiano_tutte_e_due(self) -> None:
        """`source_rule` si rifiuta se registro e mappatura confermata dicono
        due colonne diverse: cambiarne una sola lascerebbe il fornitore non
        compilabile, con una frase che accusa la mappatura."""

        store = self.negozio()
        self.scrivi_registro(
            field_mapping={
                "sheet": "Sheet1",
                "header_row": 1,
                "data_start_row": 2,
                "columns": {"ean": "EAN", "description": "Descrizione",
                            "pieces_per_carton": "PzCt", "unit_price_net": "Prezzo"},
                "order_column": "C",
                "order_header_expected": "ORDINE",
            },
            order_write={
                "from_field_mapping": True,
                "order_column": "C",
                "expected_header": "ORDINE",
                "required_columns": ["ean", "description"],
            },
        )
        review = json.loads((self.corrente / "review_data.json").read_text(encoding="utf-8"))
        review["files"][0]["fieldMapping"] = {
            "sheet": "Sheet1", "header_row": 1, "data_start_row": 2,
            "columns": {"ean": "EAN", "description": "Descrizione",
                        "pieces_per_carton": "PzCt", "unit_price_net": "Prezzo"},
            "order_column": "C", "order_header_expected": "ORDINE",
        }
        (self.corrente / "review_data.json").write_text(
            json.dumps(review, ensure_ascii=False), encoding="utf-8")

        store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        voce = self.voce_del_registro()
        self.assertEqual(voce["order_write"]["order_column"], "F")
        self.assertEqual(voce["field_mapping"]["order_column"], "F")
        riletto = json.loads((self.corrente / "review_data.json").read_text(encoding="utf-8"))
        self.assertEqual(riletto["files"][0]["fieldMapping"]["order_column"], "F")

    def test_la_decisione_scritta_a_mano_segue(self) -> None:
        """Una decisione manuale è **sovrana** sul registro: lasciarla indietro
        vorrebbe dire vedere la colonna nuova oggi e ritrovarsi quella vecchia
        al primo ricalcolo, senza una parola."""

        store = self.negozio()
        decisioni = store.pipeline_jobs.configurazione.decisioni_manuali_path
        decisioni.write_text(json.dumps({"decisions": [{
            "file_name": "listino.xlsx",
            "role": "supplier",
            "supplier_id": "betulla",
            "adapter_id": "prova_v1",
            "field_mapping": {"order_column": "C", "order_header_expected": "ORDINE"},
        }]}, ensure_ascii=False), encoding="utf-8")

        store.cambia_colonna_d_ordine({"supplierId": "betulla", "colonna": "F"})

        riletto = json.loads(decisioni.read_text(encoding="utf-8"))
        self.assertEqual(riletto["decisions"][0]["field_mapping"]["order_column"], "F")
        self.assertNotIn("order_header_expected", riletto["decisions"][0]["field_mapping"])


if __name__ == "__main__":
    unittest.main()
