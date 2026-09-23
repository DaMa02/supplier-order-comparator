"""I codici di riga dei fornitori stanno nel registro, non nel codice.

Larice marca con `SM` la riga premio di una soglia con omaggio: ha codice, EAN
e descrizione veri, ma non è merce acquistabile. Fino a questa correzione
restava fuori dall'ordine solo perché la cella del prezzo era vuota — una
protezione per caso, che sarebbe caduta il giorno in cui il fornitore ci avesse
scritto il valore dell'omaggio.

Il programma finito gira da solo: la regola deve poter cambiare aggiornando
`references/adapters.json`, senza toccare il codice.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
APP = SKILL_ROOT / "app"
ADAPTERS = SKILL_ROOT / "references" / "adapters.json"
for cartella in (SCRIPTS, APP):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import registro  # noqa: E402
from prepare_sources import larice_discount, read_larice  # noqa: E402
from prepare_manifest_sources import read_mapped_csv_supplier, read_mapped_xlsx_supplier  # noqa: E402
from promotion_bridge import PromotionService  # noqa: E402


# Le colonne del listino Larice, dalla A alla R, come le dichiara l'adattatore.
COLONNA = {"supplier_code": 3, "pieces_per_carton": 5, "pallet": 6, "description": 7,
           "unit_price_pre_discount": 15, "discount": 16, "vat": 17, "ean": 18}


def scrivi_listino_larice(percorso: Path, righe: list[dict[str, object]]) -> Path:
    """Un foglio con la forma del listino Larice: intestazione e poi le righe."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "LISTINO"
    for numero, riga in enumerate(righe, start=1):
        for campo, colonna in COLONNA.items():
            if campo in riga:
                sheet.cell(row=numero, column=colonna, value=riga[campo])
    workbook.save(percorso)
    workbook.close()
    return percorso


PRODOTTO = {
    "supplier_code": "12345", "pieces_per_carton": 6, "description": "BAGNO VIDOR 500 ML TALCO",
    "unit_price_pre_discount": 2.50, "discount": "TP", "vat": 22, "ean": "8000000000011",
}
PREMIO = {
    "supplier_code": "27606", "pieces_per_carton": 6, "description": "IN OMAGGIO 1 CT DI",
    "discount": "SM", "vat": 22, "ean": "8059147044172",
}


class CodiciDiRigaTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_codici_"))
        self.addCleanup(shutil.rmtree, self.radice, True)

    # -- la regola vive nel registro ---------------------------------------

    def test_il_registro_dichiara_che_cosa_significa_ogni_codice(self) -> None:
        codici = registro.codici_di_riga(registro.adattatore("larice_v1"))

        self.assertEqual(codici["field"], "discount_raw")
        self.assertEqual(registro.codici_ammessi(codici), {"TP", "SM"})
        self.assertTrue(codici["codes"]["TP"]["orderable"])
        self.assertFalse(codici["codes"]["SM"]["orderable"])
        self.assertEqual(codici["codes"]["SM"]["row_type"], "OMAGGIO")
        self.assertIn("omaggio", codici["codes"]["SM"]["means"].casefold())

    def test_i_codici_ammessi_li_dice_il_registro_e_non_il_codice(self) -> None:
        """Il giorno in cui Larice spiega che cosa vuol dire «**», si aggiorna il
        registro e basta: nessuno deve rimettere le mani in una funzione."""
        _sconto, _tipo, avviso = larice_discount("**", {"TP", "SM"})
        self.assertEqual(avviso, "Codice sconto testuale inatteso: **")

        _sconto, _tipo, ammesso = larice_discount("**", {"TP", "SM", "**"})
        self.assertIsNone(ammesso)

        _sconto, _tipo, vuoto = larice_discount("", set())
        self.assertIsNone(vuoto)

    # -- la riga premio non è merce ----------------------------------------

    def test_la_riga_premio_non_e_ordinabile(self) -> None:
        listino = scrivi_listino_larice(self.radice / "larice.xlsx", [PRODOTTO, PREMIO])

        records, avvisi = read_larice(listino)

        self.assertEqual(avvisi, [])
        prodotto, premio = records
        self.assertTrue(prodotto["usable"])
        self.assertIsNone(prodotto.get("row_type"))
        self.assertFalse(premio["usable"])
        self.assertEqual(premio["row_type"], "OMAGGIO")
        self.assertIn("non è merce acquistabile", premio["not_orderable_reason"])

    def test_una_riga_premio_con_un_prezzo_resta_non_ordinabile(self) -> None:
        """La prova che conta.

        Prima di questa regola la riga restava fuori solo perché il prezzo
        mancava: bastava che il fornitore ci scrivesse il valore dell'omaggio —
        come fa QUERCIA nella sua sezione promozionale — perché il comparatore
        proponesse di comprare merce che non è in vendita.
        """
        premio_con_prezzo = {**PREMIO, "unit_price_pre_discount": 4.90}
        listino = scrivi_listino_larice(self.radice / "larice.xlsx", [PRODOTTO, premio_con_prezzo])

        records, _avvisi = read_larice(listino)

        premio = records[1]
        # Il prezzo viene letto: non si nasconde un dato che il listino porta.
        self.assertEqual(premio["unit_price_net"], "4.9000")
        # Ma la riga non diventa acquistabile.
        self.assertFalse(premio["usable"])
        self.assertEqual(premio["row_type"], "OMAGGIO")

    def test_senza_la_regola_nel_registro_la_riga_tornerebbe_acquistabile(self) -> None:
        """Controprova: è il registro a decidere, non il codice."""
        premio_con_prezzo = {**PREMIO, "unit_price_pre_discount": 4.90}
        listino = scrivi_listino_larice(self.radice / "larice.xlsx", [PRODOTTO, premio_con_prezzo])
        registro_senza = self.radice / "adapters_senza_regola.json"
        documento = json.loads(ADAPTERS.read_text(encoding="utf-8"))
        for voce in documento["adapters"]:
            voce.pop("row_markers", None)
        registro_senza.write_text(json.dumps(documento), encoding="utf-8")

        originale = registro.REGISTRO
        registro.REGISTRO = registro_senza
        try:
            records, _avvisi = read_larice(listino)
        finally:
            registro.REGISTRO = originale

        self.assertTrue(records[1]["usable"])
        self.assertIsNone(records[1].get("row_type"))

    # -- la stessa regola per un fornitore qualunque ------------------------

    def test_la_stessa_regola_vale_per_un_fornitore_mappato(self) -> None:
        """La regola non è di Larice: è del motore. Un fornitore nuovo la
        dichiara nella propria mappatura e funziona allo stesso modo."""
        percorso = self.radice / "nuovo_fornitore.csv"
        percorso.write_text(
            "ean;descrizione;pezzi;prezzo;codice\n"
            "8000000000011;PRODOTTO NORMALE;6;2,50;\n"
            "8059147044172;IN REGALO 1 CT DI;6;4,90;OM\n",
            encoding="utf-8",
        )
        mappatura = {
            "header_row": 1,
            "delimiter": ";",
            "italian_numbers": True,
            "columns": {
                "ean": "ean", "description": "descrizione",
                "pieces_per_carton": "pezzi", "unit_price_net": "prezzo", "discount": "codice",
            },
            # `field` è il campo del record normalizzato — `discount_raw` —, non
            # il nome della colonna nel file: così la regola non dipende da come
            # il fornitore ha intitolato la colonna.
            "row_markers": {
                "field": "discount_raw",
                "codes": {"OM": {"means": "Riga regalo, non acquistabile.", "orderable": False, "row_type": "OMAGGIO"}},
            },
        }
        lettura: dict[str, object] = {}

        records, _avvisi = read_mapped_csv_supplier(percorso, "nuovo", mappatura, report=lettura)

        self.assertTrue(records[0]["usable"])
        self.assertFalse(records[1]["usable"])
        self.assertEqual(records[1]["row_type"], "OMAGGIO")
        self.assertEqual(records[1]["unit_price_net"], "4.9000")
        self.assertEqual(lettura["rows_not_orderable"], {"OMAGGIO": 1})

    def test_la_stessa_regola_vale_anche_su_un_foglio_di_calcolo(self) -> None:
        """Il formato non c'entra: la regola è del motore, non del lettore."""
        percorso = self.radice / "nuovo_fornitore.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["ean", "descrizione", "pezzi", "prezzo", "codice"])
        sheet.append(["8000000000011", "PRODOTTO NORMALE", 6, 2.50, None])
        sheet.append(["8059147044172", "REGALO DELLA SOGLIA", 6, 4.90, "OM"])
        workbook.save(percorso)
        workbook.close()
        mappatura = {
            "sheet": "FIRST",
            "header_row": 1,
            "data_start_row": 2,
            "columns": {
                "ean": "ean", "description": "descrizione",
                "pieces_per_carton": "pezzi", "unit_price_net": "prezzo", "discount": "codice",
            },
            "row_markers": {
                "field": "discount_raw",
                "codes": {"OM": {"means": "Riga regalo, non acquistabile.", "orderable": False, "row_type": "OMAGGIO"}},
            },
        }
        lettura: dict[str, object] = {}

        records, _avvisi = read_mapped_xlsx_supplier(percorso, "nuovo", mappatura, report=lettura)

        self.assertTrue(records[0]["usable"])
        self.assertFalse(records[1]["usable"])
        self.assertEqual(records[1]["row_type"], "OMAGGIO")
        self.assertEqual(lettura["rows_not_orderable"], {"OMAGGIO": 1})

    # -- il fornitore che la riga premio non la marca affatto ---------------

    def test_una_riga_premio_senza_codice_si_riconosce_dal_testo(self) -> None:
        """Il canvass nuovo di LARICE non marca la riga premio: dice PROMO su
        tutta la merce in promozione, premio compreso. L'unico segnale è il
        testo, e a leggerlo è già il motore delle offerte."""
        percorso = self.radice / "canvass.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["ean", "descrizione", "pezzi", "prezzo", "codice"])
        sheet.append(["8000000000011", "SH. A/ERBAR. 250 LAVANDA", 12, 0.68, "PROMO"])
        sheet.append(["8000000000011", "IN OMAGGIO 1CT SH. A/ERBAR. 250ML LAVANDA", 12, 0.65, "PROMO"])
        workbook.save(percorso)
        workbook.close()
        mappatura = {
            "sheet": "FIRST",
            "header_row": 1,
            "data_start_row": 2,
            "columns": {
                "ean": "ean", "description": "descrizione",
                "pieces_per_carton": "pezzi", "unit_price_net": "prezzo", "discount": "codice",
            },
            "row_markers": {
                "field": "discount_raw",
                "reward_rows": {
                    "means": "Riga premio di una soglia con omaggio.",
                    "orderable": False,
                    "row_type": "OMAGGIO",
                },
            },
        }
        lettura: dict[str, object] = {}

        records, _avvisi = read_mapped_xlsx_supplier(percorso, "nuovo", mappatura, report=lettura)

        self.assertTrue(records[0]["usable"])
        self.assertFalse(records[1]["usable"])
        self.assertEqual(records[1]["row_type"], "OMAGGIO")
        self.assertEqual(lettura["rows_not_orderable"], {"OMAGGIO": 1})

    def test_senza_reward_rows_il_premio_col_prezzo_batte_la_merce(self) -> None:
        """La controprova, che è il danno vero: le due righe portano lo stesso
        EAN e il premio costa meno. Senza la dichiarazione il confronto ha due
        offerte per lo stesso prodotto, e la più bassa è quella del regalo."""
        percorso = self.radice / "canvass_senza_regola.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["ean", "descrizione", "pezzi", "prezzo", "codice"])
        sheet.append(["8000000000011", "SH. A/ERBAR. 250 LAVANDA", 12, 0.68, "PROMO"])
        sheet.append(["8000000000011", "IN OMAGGIO 1CT SH. A/ERBAR. 250ML LAVANDA", 12, 0.65, "PROMO"])
        workbook.save(percorso)
        workbook.close()
        mappatura = {
            "sheet": "FIRST",
            "header_row": 1,
            "data_start_row": 2,
            "columns": {
                "ean": "ean", "description": "descrizione",
                "pieces_per_carton": "pezzi", "unit_price_net": "prezzo", "discount": "codice",
            },
        }

        records, _avvisi = read_mapped_xlsx_supplier(percorso, "nuovo", mappatura)

        ordinabili = [record for record in records if record["usable"]]
        self.assertEqual(len(ordinabili), 2)
        self.assertEqual(min(record["unit_price_net"] for record in ordinabili), "0.6500")

    def test_una_riga_premio_marcata_ordinabile_resta_fuori_lo_stesso(self) -> None:
        """Fra le due risposte vince «non ordinabile».

        Scambiare un prodotto per un premio si vede subito — manca dal
        confronto —; scambiare un premio per un prodotto mette a listino un
        prezzo che non esiste, e si scopre dal fornitore.
        """
        percorso = self.radice / "canvass_marcato.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["ean", "descrizione", "pezzi", "prezzo", "codice"])
        sheet.append(["8000000000011", "IN OMAGGIO 1CT SH. A/ERBAR. 250ML LAVANDA", 12, 0.65, "TP"])
        workbook.save(percorso)
        workbook.close()
        mappatura = {
            "sheet": "FIRST",
            "header_row": 1,
            "data_start_row": 2,
            "columns": {
                "ean": "ean", "description": "descrizione",
                "pieces_per_carton": "pezzi", "unit_price_net": "prezzo", "discount": "codice",
            },
            "row_markers": {
                "field": "discount_raw",
                "codes": {"TP": {"means": "Prezzo di transito: merce normale.", "orderable": True}},
                "reward_rows": {
                    "means": "Riga premio di una soglia con omaggio.",
                    "orderable": False,
                    "row_type": "OMAGGIO",
                },
            },
        }

        records, _avvisi = read_mapped_xlsx_supplier(percorso, "nuovo", mappatura)

        self.assertFalse(records[0]["usable"])
        self.assertEqual(records[0]["row_type"], "OMAGGIO")

    # -- il premio non fa raggiungere la soglia a se stesso -----------------

    def test_il_premio_non_conta_fra_i_prodotti_che_fanno_soglia(self) -> None:
        """Una riga premio il cui testo non dice «in omaggio» è riconoscibile
        solo dal codice: senza la regola verrebbe contata fra i prodotti
        acquistati che portano alla soglia."""
        listino = scrivi_listino_larice(self.radice / "larice.xlsx", [
            {"description": "ACQUISTANDO 3 CT TRA"},
            {**PRODOTTO, "ean": "8000000000011"},
            {**PRODOTTO, "ean": "8000000000012", "description": "BAGNO VIDOR 500 ML VITAMINA C"},
            # Il premio, marcato SM ma con il nome del prodotto regalato.
            {**PREMIO, "description": "RESALINA SALE LAVASTOVIGLIE KG1"},
            {**PREMIO, "description": "IN OMAGGIO 1 CT DI"},
        ])
        servizio = PromotionService()

        promozioni = servizio.detect({
            "files": [{"supplierId": "larice", "sourcePath": str(listino)}],
            "products": [],
        })

        self.assertEqual(len(promozioni), 1)
        ammessi = (promozioni[0].get("eligible") or {}).get("source_rows") or []
        self.assertEqual(sorted(ammessi), [2, 3])


class IlCanvassNuovoDiLariceLettoDavvero(unittest.TestCase):
    """Il listino vero, con l'adattatore che il negozio riceve.

    Le prove qui sopra girano su fogli costruiti apposta. Questa gira sul
    documento che LARICE ha mandato il 4 settembre 2026, perché il difetto che
    la regola chiude si vede solo lì: le sei righe «IN OMAGGIO ...» hanno un
    prezzo vero e nessun codice, e cinque delle sei ripetono l'EAN di un
    articolo già a listino a prezzo pieno.
    """

    LISTINO = SKILL_ROOT / "listini-storici" / "New Larice N°37(v.0).xls"

    def setUp(self) -> None:
        if not self.LISTINO.is_file():
            self.skipTest(f"Manca il listino {self.LISTINO}")
        self.adattatore = registro.adattatore("larice_canvass_v1", ADAPTERS)

    def test_le_sei_righe_premio_restano_fuori_dall_ordine(self) -> None:
        lettura: dict[str, object] = {}

        records, avvisi = read_mapped_xlsx_supplier(
            self.LISTINO, "larice", self.adattatore["field_mapping"], report=lettura
        )

        self.assertEqual(avvisi, [])
        omaggi = [record for record in records if record.get("row_type") == "OMAGGIO"]
        self.assertEqual(len(omaggi), 6)
        self.assertFalse(any(record["usable"] for record in omaggi))
        self.assertEqual(lettura["rows_not_orderable"]["OMAGGIO"], 6)

    def test_il_prezzo_del_regalo_non_diventa_un_offerta(self) -> None:
        """L'EAN 8019580330416 sta a 0,68 come merce e a 0,65 come regalo."""

        records, _avvisi = read_mapped_xlsx_supplier(
            self.LISTINO, "larice", self.adattatore["field_mapping"]
        )

        offerte = [
            record for record in records
            if record["usable"] and record["ean"] == "8019580330416"
        ]
        self.assertEqual(len(offerte), 1)
        self.assertEqual(offerte[0]["unit_price_net"], "0.6800")

    def test_le_sei_soglie_con_omaggio_si_ricompongono_tutte(self) -> None:
        """Sei intestazioni di soglia, sei righe premio, sei condizioni.

        Il nome del premio esce pulito: testo e premio stanno nella stessa
        colonna E, e leggerla due volte lo farebbe uscire scritto due volte.
        """

        promozioni, errore = PromotionService().condizioni_di(
            "larice", self.LISTINO, "larice_canvass_v1"
        )

        self.assertIsNone(errore)
        self.assertEqual(len(promozioni), 6)
        self.assertEqual(
            sorted(promozione["threshold"]["qty"] for promozione in promozioni),
            [2, 2, 2, 3, 5, 10],
        )
        premi = {promozione["reward"]["description"] for promozione in promozioni}
        self.assertIn("SH. A/ERBAR. 250ML LAVANDA", premi)
        self.assertTrue(
            all("IN OMAGGIO" not in nome for nome in premi),
            f"il nome del premio si è portato dietro il testo della riga: {sorted(premi)}",
        )


if __name__ == "__main__":
    unittest.main()
