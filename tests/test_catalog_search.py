"""The supplier catalog: who reads what, with which reader, and who gets left out.

Two defects lived here: a legacy Excel 97-2003 price list was still routed to
the CSV reader, and the exception it raised took down every supplier's
catalog, not just that one's.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook


SKILL_ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts", TESTS):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

from catalog_search import SupplierCatalog  # noqa: E402

# The .xls price-list writer already lives in the pipeline test suite: it
# builds a real OLE2 file with real BIFF8 records.
import test_schema_pipeline as pipeline  # noqa: E402


ADAPTERS = SKILL_ROOT / "references" / "adapters.json"
MAPPATURA_NOCE = next(
    voce for voce in json.loads(ADAPTERS.read_text(encoding="utf-8"))["adapters"]
    if voce["id"] == "noce_xls_v1"
)["field_mapping"]

MAPPATURA_CIPRESSO = {
    "sheet": "FIRST",
    "header_row": 1,
    "data_start_row": 2,
    "columns": {
        "supplier_code": "COD.ART.",
        "description": "DES.ARTICOLO",
        "unit": "UM",
        "pieces_per_carton": "QT",
        "unit_price_net": "LISTINO",
        "ean": "COD.EAN",
    },
    "assume_available": True,
    "vat_unavailable": True,
    "order_column": "G",
}


def scrivi_cipresso(percorso: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Listino al 10-08-2026"
    sheet.append(["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"])
    sheet.append(["E-001", "TOVAGLIOLI CIPRESSO", "PZ", 6, 1.25, "8000000000098", None])
    workbook.save(percorso)
    workbook.close()
    return percorso


def scrivi_noce_csv(percorso: Path) -> Path:
    """The legacy catalog extracted from the website; it must keep working."""
    with percorso.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "catalog_page", "ean", "product", "packaging", "availability", "variation", "price", "unit",
        ])
        writer.writeheader()
        writer.writerow({
            "catalog_page": "1",
            "ean": "8000000000077",
            "product": "PRODOTTO DAL VECCHIO CSV",
            "packaging": "24 x 10 PZ",
            "availability": "Disponibile",
            "variation": "",
            "price": "1,00",
            "unit": "x 24",
        })
    return percorso


class CatalogoFornitoriTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_catalogo_"))
        self.addCleanup(shutil.rmtree, self.radice, True)
        self.listino_xls = pipeline.scrivi_listino_noce(
            self.radice / "formattato_104233.xls", pipeline.RIGHE_DI_PROVA, pipeline.RIGHE_IN_CODA,
        )

    def review(self, *file_voci: dict[str, object]) -> dict[str, object]:
        return {"files": list(file_voci), "products": []}

    # Which adapter the registry uses to recognize each test supplier's price
    # list. Not decoration: `adapterId` and `schemaState` are the two fields
    # the catalog uses to pick a reader, the same ones the pipeline decides
    # on. A real review always carries them — `build_review_data.
    # manifest_files` writes them — so a `voce` without them would describe a
    # review that cannot exist.
    ADATTATORE_PREDEFINITO = {
        "noce": "noce_xls_v1",
        "betulla": "betulla_v1",
        "larice": "larice_v1",
        "cipresso": "cipresso_v1",
    }

    def voce(
        self,
        supplier: str,
        percorso: Path,
        mapping: dict | None = None,
        *,
        adattatore: str | None = None,
        stato: str = "SCHEMA_NOTO",
    ) -> dict[str, object]:
        return {
            "supplierId": supplier,
            "role": "supplier",
            "sourcePath": str(percorso),
            "adapterId": adattatore if adattatore is not None
            else self.ADATTATORE_PREDEFINITO.get(supplier, f"{supplier}_v1"),
            "schemaState": stato,
            "fieldMapping": mapping or {},
        }

    # -- the reader is chosen by the mapping decision, not by the supplier's name --
    #
    # A promotional price list declared "BETULLA" in a guided mapping. The
    # pipeline reads it with the generic reader — `SCHEMA_VARIATO` is not one
    # of the known-schema readers — and this module must not route it to
    # `read_betulla` just because the supplier is named betulla: doing so
    # keeps the supplier in the comparison while making it disappear from
    # the catalog viewer.

    MAPPATURA_A_MANO = {
        "sheet": "FIRST",
        "columns": {"description": 2, "unit_price_net": 5, "pieces_per_carton": 4, "ean": 6},
        "data_start_row": 2,
        "assume_available": True,
        "vat_unavailable": True,
        "order_column": "H",
    }

    def scrivi_senza_intestazioni(self, percorso: Path) -> Path:
        """A sheet that does NOT have the headers `read_betulla` requires."""
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Foglio1"
        sheet.append([None, None, None, None, None, None, None, "ORDINE"])
        sheet.append(["028137", "CHIARY INTIMO 200ML", "PZ", 12, 1.85, "8009467741640", None, None])
        workbook.save(percorso)
        workbook.close()
        return percorso

    def test_un_listino_letto_dal_generico_nella_catena_non_va_al_lettore_dedicato(self) -> None:
        """The regression test: a varied schema confirmed by hand, supplier BETULLA."""
        percorso = self.scrivi_senza_intestazioni(self.radice / "offerte.xlsx")
        catalogo = SupplierCatalog()
        review = self.review(self.voce(
            "betulla", percorso, self.MAPPATURA_A_MANO,
            adattatore="betulla_v1", stato="SCHEMA_VARIATO",
        ))

        self.assertEqual(catalogo.load_errors, [])
        self.assertEqual(
            [voce["id"] for voce in catalogo.fornitori_sfogliabili(review)], ["betulla"],
        )
        pagina = catalogo.sfoglia(review, "betulla")
        self.assertEqual(pagina["totale"], 1)
        self.assertEqual(pagina["righe"][0]["description"], "CHIARY INTIMO 200ML")
        self.assertEqual(pagina["righe"][0]["unitPriceNet"], 1.85)

    def test_una_variazione_imparata_qui_resta_lo_stesso_fornitore(self) -> None:
        """`betulla_v1__locale` is BETULLA, and must still go through `read_betulla`.

        A learned adapter built on top of a shipped one gets its own id. Every
        place that decides by adapter id must resolve back to the base id,
        otherwise a learned variation would route the supplier to the generic
        reader, which reads the same columns but skips the rest. `pallet` is
        read only by `read_betulla`, so it's the tell.
        """

        percorso = self.radice / "betulla-locale.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva"])
        sheet.append(["8000000000055", "C-1", None, "SAPONE BETULLA", 6, 1.5, 120, "Iva 22%"])
        workbook.save(percorso)
        workbook.close()
        catalogo = SupplierCatalog()
        review = self.review(self.voce("betulla", percorso, adattatore="betulla_v1__locale"))

        catalogo._ensure_loaded(review)

        self.assertEqual(catalogo.load_errors, [])
        riga = catalogo.righe_per_fornitore["betulla"][0]
        self.assertEqual(riga["pallet"], 120)

    def test_lo_stesso_fornitore_con_schema_noto_va_al_lettore_dedicato(self) -> None:
        """The counterpart: fixing the defect must not route everything to the generic reader.

        `pallet` is read only by `read_betulla`, by column position: if the
        generic reader were used instead, that field would be missing.
        """
        percorso = self.radice / "betulla.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva"])
        sheet.append(["8000000000055", "C-1", None, "SAPONE BETULLA", 6, 1.5, 120, "Iva 22%"])
        workbook.save(percorso)
        workbook.close()
        catalogo = SupplierCatalog()
        review = self.review(self.voce("betulla", percorso, adattatore="betulla_v1"))

        catalogo._ensure_loaded(review)

        self.assertEqual(catalogo.load_errors, [])
        riga = catalogo.righe_per_fornitore["betulla"][0]
        self.assertEqual(riga["pallet"], 120)
        self.assertEqual(riga["description"], "SAPONE BETULLA")

    def test_un_fornitore_scartato_dice_sempre_perche(self) -> None:
        """Guards against `_read_sources` silently dropping a rejected supplier."""
        percorso = self.scrivi_senza_intestazioni(self.radice / "sconosciuto.xlsx")
        catalogo = SupplierCatalog()
        review = self.review(self.voce("acero", percorso, adattatore="", stato="NUOVO_FORNITORE"))

        self.assertEqual(catalogo.fornitori_sfogliabili(review), [])
        self.assertEqual(len(catalogo.load_errors), 1)
        self.assertIn("ACERO", catalogo.load_errors[0]["message"].upper())

    def test_il_gestionale_non_finisce_fra_i_listini_da_sfogliare(self) -> None:
        """The management-software file must never end up among the browsable catalogs."""
        gestionale = self.scrivi_senza_intestazioni(self.radice / "gestionale.xlsx")
        catalogo = SupplierCatalog()
        review = self.review(
            self.voce("cipresso", scrivi_cipresso(self.radice / "cipresso.xlsx"), MAPPATURA_CIPRESSO),
            {
                "name": gestionale.name,
                "role": "master",
                "supplierId": None,
                "sourcePath": str(gestionale),
                "adapterId": "gestionale_v1",
                "schemaState": "SCHEMA_NOTO",
                "fieldMapping": None,
            },
        )

        self.assertEqual([voce["id"] for voce in catalogo.fornitori_sfogliabili(review)], ["cipresso"])
        self.assertEqual(catalogo.load_errors, [])

    # -- il listino .xls entra davvero -------------------------------------

    def test_il_listino_xls_di_noce_entra_nel_catalogo(self) -> None:
        """With no mapping in the review, it falls back to the adapter registry."""
        catalogo = SupplierCatalog()

        risultati = catalogo.search(self.review(self.voce("noce", self.listino_xls)), "casseruola")

        self.assertEqual(len(risultati), 1)
        offerta = risultati[0]["offers"][0]
        self.assertEqual(offerta["supplierId"], "noce")
        self.assertEqual(offerta["unitPriceNet"], 21.75)
        self.assertEqual(offerta["quantityFactor"], 1.0)
        self.assertEqual(catalogo.load_errors, [])

    def test_le_righe_alimentari_non_entrano_nel_catalogo(self) -> None:
        """The store does not sell food: those rows must not show up at all."""
        catalogo = SupplierCatalog()
        review = self.review(self.voce("noce", self.listino_xls))

        self.assertEqual(catalogo.search(review, "pasta di semola"), [])
        self.assertEqual(catalogo.search(review, "biscotti frollini"), [])
        self.assertEqual(len(catalogo.search(review, "sapone")), 1)

    def test_il_csv_di_noce_continua_a_essere_letto(self) -> None:
        """The format is decided by content, not extension: the legacy CSV must still work."""
        catalogo = SupplierCatalog()
        percorso = scrivi_noce_csv(self.radice / "noce_catalog.csv")

        risultati = catalogo.search(
            self.review(self.voce("noce", percorso, adattatore="noce_csv_v1")), "vecchio csv",
        )

        self.assertEqual(len(risultati), 1)
        self.assertEqual(risultati[0]["offers"][0]["unitPriceNet"], 1.0)
        self.assertEqual(catalogo.load_errors, [])

    # -- un fornitore rotto non ne trascina altri ---------------------------

    def test_il_xls_di_noce_non_fa_cadere_i_cataloghi_degli_altri(self) -> None:
        """One bad `.xls` sent to the CSV reader must not take other suppliers down with it.

        The supplier with the broken file still fails, but the test that
        matters is that the other supplier's catalog still reaches the user.
        """
        catalogo = SupplierCatalog()
        cipresso = scrivi_cipresso(self.radice / "cipresso.xlsx")
        review = self.review(
            self.voce("noce", self.listino_xls),
            self.voce("cipresso", cipresso, MAPPATURA_CIPRESSO),
        )

        self.assertEqual(len(catalogo.search(review, "tovaglioli")), 1)
        self.assertEqual(len(catalogo.search(review, "casseruola")), 1)
        self.assertEqual(catalogo.load_errors, [])

    def test_un_listino_illeggibile_lascia_in_piedi_tutti_gli_altri(self) -> None:
        catalogo = SupplierCatalog()
        rotto = self.radice / "larice.xlsx"
        rotto.write_bytes(b"questo non e' un foglio di calcolo")
        review = self.review(
            self.voce("noce", self.listino_xls),
            self.voce("larice", rotto),
        )

        risultati = catalogo.search(review, "casseruola")

        self.assertEqual(len(risultati), 1)
        self.assertEqual(sorted(catalogo.unique_offers), ["noce"])
        self.assertEqual(len(catalogo.load_errors), 1)
        errore = catalogo.load_errors[0]
        self.assertEqual(errore["supplier"], "larice")
        self.assertEqual(errore["supplierName"], "LARICE")
        self.assertIn("larice.xlsx", errore["message"])
        self.assertTrue(errore["message"].startswith("Il listino LARICE"))

    def test_gli_errori_di_lettura_si_azzerano_quando_il_catalogo_si_ricarica(self) -> None:
        catalogo = SupplierCatalog()
        rotto = self.radice / "larice.xlsx"
        rotto.write_bytes(b"questo non e' un foglio di calcolo")
        catalogo.search(self.review(self.voce("larice", rotto)), "qualsiasi cosa")
        self.assertEqual(len(catalogo.load_errors), 1)

        catalogo.invalidate()
        self.assertEqual(catalogo.load_errors, [])

        catalogo.search(self.review(self.voce("noce", self.listino_xls)), "casseruola")
        self.assertEqual(catalogo.load_errors, [])

    # -- le righe alimentari non entrano da nessuna porta -------------------

    def test_una_mappatura_della_review_senza_esclusione_del_food_viene_rifiutata(self) -> None:
        """The review's mapping takes precedence: if it doesn't exclude FOOD
        rows, thousands of food items would get in without a single error."""
        catalogo = SupplierCatalog()
        senza_esclusione = {chiave: valore for chiave, valore in MAPPATURA_NOCE.items() if chiave != "exclude_rows"}
        review = self.review(self.voce("noce", self.listino_xls, senza_esclusione))

        self.assertEqual(catalogo.search(review, "pasta di semola"), [])
        self.assertEqual(catalogo.search(review, "casseruola"), [])
        self.assertEqual(len(catalogo.load_errors), 1)
        self.assertIn("non scarta le righe alimentari", catalogo.load_errors[0]["message"])

    def test_una_mappatura_della_review_aggiornata_prevale_sull_adattatore(self) -> None:
        """The opposite case: a supplier renames a column, the new mapping gets
        confirmed, and the price list must load despite what the registry says."""
        catalogo = SupplierCatalog()
        intestazioni = tuple(
            "categoria_merceologica" if nome == "cat" else nome
            for nome in pipeline.INTESTAZIONI_NOCE
        )
        listino = pipeline.scrivi_listino_noce(
            self.radice / "noce_rinominato.xls", pipeline.RIGHE_DI_PROVA, pipeline.RIGHE_IN_CODA, intestazioni,
        )
        aggiornata = json.loads(json.dumps(MAPPATURA_NOCE))
        aggiornata["columns"]["category"] = "categoria_merceologica"
        aggiornata["exclude_rows"][0]["field"] = "category"

        risultati = catalogo.search(self.review(self.voce("noce", listino, aggiornata)), "casseruola")

        self.assertEqual(len(risultati), 1)
        self.assertEqual(catalogo.load_errors, [])
        self.assertEqual(catalogo.search(self.review(self.voce("noce", listino, aggiornata)), "pasta di semola"), [])

    # -- gli altri modi di sparire ------------------------------------------

    def test_un_listino_che_non_c_e_piu_viene_detto_e_non_sparisce_in_silenzio(self) -> None:
        """The common case: the file gets moved or renamed on the operator's Desktop."""
        catalogo = SupplierCatalog()
        review = self.review(
            self.voce("noce", self.listino_xls),
            self.voce("betulla", self.radice / "listino_che_non_esiste.xlsx"),
        )

        self.assertEqual(len(catalogo.search(review, "casseruola")), 1)
        self.assertEqual(len(catalogo.load_errors), 1)
        errore = catalogo.load_errors[0]
        self.assertEqual(errore["supplier"], "betulla")
        self.assertIn("non è stato trovato", errore["message"])
        self.assertIn("listino_che_non_esiste.xlsx", errore["message"])

    def test_l_avviso_di_un_listino_mancante_sparisce_quando_il_fornitore_esce_dalla_run(self) -> None:
        catalogo = SupplierCatalog()
        con_mancante = self.review(
            self.voce("noce", self.listino_xls),
            self.voce("betulla", self.radice / "listino_che_non_esiste.xlsx"),
        )
        catalogo.search(con_mancante, "casseruola")
        self.assertEqual(len(catalogo.load_errors), 1)

        catalogo.search(self.review(self.voce("noce", self.listino_xls)), "casseruola")

        self.assertEqual(catalogo.load_errors, [])

    def test_un_listino_senza_righe_ordinabili_non_toglie_il_fornitore_in_silenzio(self) -> None:
        catalogo = SupplierCatalog()
        vuoto = self.radice / "cipresso_vuoto.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Listino al 10-08-2026"
        sheet.append(["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"])
        workbook.save(vuoto)
        workbook.close()

        catalogo.search(self.review(self.voce("cipresso", vuoto, MAPPATURA_CIPRESSO)), "qualsiasi cosa")

        self.assertEqual(len(catalogo.load_errors), 1)
        self.assertEqual(catalogo.load_errors[0]["supplier"], "cipresso")
        self.assertIn("nessuna riga ordinabile", catalogo.load_errors[0]["message"])

    def test_un_listino_cipresso_rinominato_csv_si_legge_lo_stesso(self) -> None:
        """The extension doesn't decide the format for mapped suppliers either."""
        catalogo = SupplierCatalog()
        vero = scrivi_cipresso(self.radice / "vero.xlsx")
        travestito = self.radice / "cipresso_listino.csv"
        travestito.write_bytes(vero.read_bytes())

        risultati = catalogo.search(self.review(self.voce("cipresso", travestito, MAPPATURA_CIPRESSO)), "tovaglioli")

        self.assertEqual(len(risultati), 1)
        self.assertEqual(catalogo.load_errors, [])

    # -- come si chiamano i fornitori --------------------------------------

    def test_il_nome_di_un_fornitore_imparato_viene_dal_registro(self) -> None:
        """The display name declared in the registry reaches the offer.

        This test uses a supplier that the registry actually declares, with a
        display name — "Sapori & Co." — that no fallback rule derived from
        the identifier could invent. A test exercising only the fallback path
        (identical to a hardcoded table) would not catch a registry lookup
        that silently stopped happening.
        """

        import catalog_search

        registro = self.radice / "adapters_di_prova.json"
        registro.write_text(json.dumps({
            "schema_version": 1,
            "adapters": [{
                "id": "sapori_v1",
                "schema_version": 1,
                "kind": "supplier",
                "supplier_id": "sapori_e_co",
                "display_name": "Sapori & Co.",
                "file_types": [".xlsx"],
            }],
        }), encoding="utf-8")
        percorso = scrivi_cipresso(self.radice / "sapori.xlsx")
        catalogo = SupplierCatalog()
        originale = catalog_search.ADAPTERS_PATH
        catalog_search.ADAPTERS_PATH = registro
        try:
            risultati = catalogo.search(
                self.review(self.voce("sapori_e_co", percorso, MAPPATURA_CIPRESSO)), "tovaglioli",
            )
        finally:
            catalog_search.ADAPTERS_PATH = originale

        self.assertEqual(len(risultati), 1, catalogo.load_errors)
        offerta = risultati[0]["offers"][0]
        self.assertEqual(offerta["supplierId"], "sapori_e_co")
        self.assertEqual(offerta["supplierName"], "Sapori & Co.")

    def test_un_fornitore_che_il_registro_non_conosce_si_legge_lo_stesso(self) -> None:
        """The fallback: `nuovo_fornitore_1` doesn't turn into a phrase with underscores."""

        percorso = scrivi_cipresso(self.radice / "ignoto.xlsx")
        catalogo = SupplierCatalog()

        risultati = catalogo.search(
            self.review(self.voce("nuovo_fornitore_1", percorso, MAPPATURA_CIPRESSO)), "tovaglioli",
        )

        self.assertEqual(len(risultati), 1, catalogo.load_errors)
        self.assertEqual(risultati[0]["offers"][0]["supplierName"], "NUOVO FORNITORE 1")

    def test_i_fornitori_di_oggi_si_chiamano_come_prima(self) -> None:
        """The other half of the rule: the registry gives the same supplier names as before.

        A source change that also changed the on-screen names would be a
        correction hiding a regression.
        """

        percorso = scrivi_cipresso(self.radice / "cipresso.xlsx")
        catalogo = SupplierCatalog()

        risultati = catalogo.search(
            self.review(self.voce("cipresso", percorso, MAPPATURA_CIPRESSO)), "tovaglioli",
        )

        self.assertEqual(len(risultati), 1, catalogo.load_errors)
        self.assertEqual(risultati[0]["offers"][0]["supplierName"], "CIPRESSO")

    def test_senza_il_registro_degli_adattatori_il_motivo_nomina_il_registro(self) -> None:
        """A missing registry and an incomplete review are two different failures."""
        import catalog_search

        catalogo = SupplierCatalog()
        originale = catalog_search.ADAPTERS_PATH
        catalog_search.ADAPTERS_PATH = self.radice / "adapters_che_non_esiste.json"
        try:
            catalogo.search(self.review(self.voce("noce", self.listino_xls)), "casseruola")
        finally:
            catalog_search.ADAPTERS_PATH = originale

        self.assertEqual(len(catalogo.load_errors), 1)
        self.assertIn("registro degli adattatori", catalogo.load_errors[0]["message"])

    def test_un_guasto_senza_parole_non_lascia_il_messaggio_a_meta(self) -> None:
        """`str(MemoryError())` is empty: the warning must not end with a trailing colon."""
        import catalog_search

        catalogo = SupplierCatalog()
        originale = SupplierCatalog._read_supplier

        def esplode(supplier, sorgente):
            raise MemoryError()

        SupplierCatalog._read_supplier = staticmethod(esplode)
        try:
            catalogo.search(self.review(self.voce("noce", self.listino_xls)), "casseruola")
        finally:
            SupplierCatalog._read_supplier = staticmethod(originale)

        messaggio = catalogo.load_errors[0]["message"]
        self.assertTrue(messaggio.endswith("MemoryError"), messaggio)
        self.assertEqual(catalog_search._motivo(ValueError("motivo scritto")), "motivo scritto")

    def test_un_listino_illeggibile_non_viene_riaperto_a_ogni_ricerca(self) -> None:
        """Without caching the error, every keystroke would reopen the broken file."""
        catalogo = SupplierCatalog()
        rotto = self.radice / "larice.xlsx"
        rotto.write_bytes(b"questo non e' un foglio di calcolo")
        review = self.review(self.voce("larice", rotto))

        letture = []
        originale = SupplierCatalog._read_supplier

        def conta(supplier, sorgente):
            letture.append(supplier)
            return originale(supplier, sorgente)

        SupplierCatalog._read_supplier = staticmethod(conta)
        try:
            catalogo.search(review, "una cosa")
            catalogo.search(review, "un'altra cosa")
            catalogo.search(review, "una terza cosa")
        finally:
            SupplierCatalog._read_supplier = staticmethod(originale)

        self.assertEqual(letture, ["larice"])
        self.assertEqual(len(catalogo.load_errors), 1)


if __name__ == "__main__":
    unittest.main()
