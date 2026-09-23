"""Il catalogo dei fornitori: chi lo legge, con quale lettore, e chi resta fuori.

Fase 9b. Due difetti stavano qui: il listino Noce in Excel 97-2003 veniva
mandato lo stesso al lettore CSV, e l'eccezione che ne usciva portava giu' i
cataloghi di *tutti* i fornitori, non solo il suo.
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

# Lo scrittore del listino Noce in .xls sta gia' nel collaudo della
# pipeline: costruisce un OLE2 vero con record BIFF8 veri.
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
    """Il vecchio catalogo estratto dal sito: deve continuare a funzionare."""
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

    # Con quale adattatore il registro riconosce il listino di ciascun
    # fornitore di collaudo. Non e' decorazione: `adapterId` e `schemaState`
    # sono i due campi con cui il catalogo sceglie il lettore, gli stessi su
    # cui decide la catena. Una review vera li porta sempre — li scrive
    # `build_review_data.manifest_files` — e una `voce` che non li dichiarava
    # descriveva una review che non esiste.
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

    # -- il lettore lo sceglie la decisione, non il nome del fornitore -------
    #
    # Il difetto del 21 agosto 2026: un listino promozionale dichiarato «BETULLA»
    # nella mappatura guidata. La catena lo leggeva col lettore generico —
    # `SCHEMA_VARIATO` non entra fra i lettori a schema noto — e questo modulo
    # lo mandava a `read_betulla` per il solo fatto che il fornitore si chiamava
    # betulla. BETULLA restava nel confronto e spariva dal visualizzatore.

    MAPPATURA_A_MANO = {
        "sheet": "FIRST",
        "columns": {"description": 2, "unit_price_net": 5, "pieces_per_carton": 4, "ean": 6},
        "data_start_row": 2,
        "assume_available": True,
        "vat_unavailable": True,
        "order_column": "H",
    }

    def scrivi_senza_intestazioni(self, percorso: Path) -> Path:
        """Un foglio che NON ha le intestazioni che `read_betulla` pretende."""
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Foglio1"
        sheet.append([None, None, None, None, None, None, None, "ORDINE"])
        sheet.append(["028137", "CHIARY INTIMO 200ML", "PZ", 12, 1.85, "8009467741640", None, None])
        workbook.save(percorso)
        workbook.close()
        return percorso

    def test_un_listino_letto_dal_generico_nella_catena_non_va_al_lettore_dedicato(self) -> None:
        """LA prova del difetto: schema variato e confermato a mano, fornitore BETULLA."""
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
        """`betulla_v1__locale` e' BETULLA, e deve passare da `read_betulla`.

        ⚠ Dal 22 agosto 2026 quello che si impara sopra un adattatore spedito
        prende un id suo. Tutti i punti che decidono per identificativo devono
        risolvere l'id base, altrimenti imparare una variazione manderebbe il
        fornitore al lettore generico — che legge le stesse colonne ma non fa il
        resto. `pallet` lo legge solo `read_betulla`: e' la spia.
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
        """Il gemello: correggere il difetto non vuol dire mandare tutto al generico.

        `pallet` lo legge soltanto `read_betulla`, per posizione: se comparisse il
        lettore generico quel campo non ci sarebbe.
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
        """La sparizione silenziosa: `_read_sources` scartava senza dire niente."""
        percorso = self.scrivi_senza_intestazioni(self.radice / "sconosciuto.xlsx")
        catalogo = SupplierCatalog()
        review = self.review(self.voce("acero", percorso, adattatore="", stato="NUOVO_FORNITORE"))

        self.assertEqual(catalogo.fornitori_sfogliabili(review), [])
        self.assertEqual(len(catalogo.load_errors), 1)
        self.assertIn("ACERO", catalogo.load_errors[0]["message"].upper())

    def test_il_gestionale_non_finisce_fra_i_listini_da_sfogliare(self) -> None:
        """Restava fuori per un incidente; adesso è una regola che si legge."""
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
        """Senza mappatura nella review: la prende dal registro degli adattatori."""
        catalogo = SupplierCatalog()

        risultati = catalogo.search(self.review(self.voce("noce", self.listino_xls)), "casseruola")

        self.assertEqual(len(risultati), 1)
        offerta = risultati[0]["offers"][0]
        self.assertEqual(offerta["supplierId"], "noce")
        self.assertEqual(offerta["unitPriceNet"], 21.75)
        self.assertEqual(offerta["quantityFactor"], 1.0)
        self.assertEqual(catalogo.load_errors, [])

    def test_le_righe_alimentari_non_entrano_nel_catalogo(self) -> None:
        """L'utente non tratta il FOOD: quelle righe non devono nemmeno comparire."""
        catalogo = SupplierCatalog()
        review = self.review(self.voce("noce", self.listino_xls))

        self.assertEqual(catalogo.search(review, "pasta di semola"), [])
        self.assertEqual(catalogo.search(review, "biscotti frollini"), [])
        self.assertEqual(len(catalogo.search(review, "sapone")), 1)

    def test_il_csv_di_noce_continua_a_essere_letto(self) -> None:
        """Il formato lo dicono i byte: il vecchio CSV non deve essersi rotto."""
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
        """Il difetto di partenza: l'.xls andava al lettore CSV e cadeva tutto.

        Qui il listino Noce si legge, ma la prova vera e' che CIPRESSO
        arrivi comunque all'utente.
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
        """La mappatura della review ha la precedenza: se non scarta il FOOD,
        entrerebbero migliaia di articoli alimentari senza un solo errore."""
        catalogo = SupplierCatalog()
        senza_esclusione = {chiave: valore for chiave, valore in MAPPATURA_NOCE.items() if chiave != "exclude_rows"}
        review = self.review(self.voce("noce", self.listino_xls, senza_esclusione))

        self.assertEqual(catalogo.search(review, "pasta di semola"), [])
        self.assertEqual(catalogo.search(review, "casseruola"), [])
        self.assertEqual(len(catalogo.load_errors), 1)
        self.assertIn("non scarta le righe alimentari", catalogo.load_errors[0]["message"])

    def test_una_mappatura_della_review_aggiornata_prevale_sull_adattatore(self) -> None:
        """Il caso opposto: il fornitore rinomina una colonna, Codex conferma la
        mappatura nuova, e il listino deve entrare nonostante il registro."""
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
        """Il modo più comune: il file viene spostato o rinominato sul Desktop."""
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
        """L'estensione non decide neanche per i fornitori mappati, non solo per Noce."""
        catalogo = SupplierCatalog()
        vero = scrivi_cipresso(self.radice / "vero.xlsx")
        travestito = self.radice / "cipresso_listino.csv"
        travestito.write_bytes(vero.read_bytes())

        risultati = catalogo.search(self.review(self.voce("cipresso", travestito, MAPPATURA_CIPRESSO)), "tovaglioli")

        self.assertEqual(len(risultati), 1)
        self.assertEqual(catalogo.load_errors, [])

    # -- come si chiamano i fornitori --------------------------------------

    def test_il_nome_di_un_fornitore_imparato_viene_dal_registro(self) -> None:
        """Il nome dichiarato nel registro arriva fino all'offerta.

        ⚠ Questo test prima usava un fornitore che nel registro **non c'era**,
        quindi attraversava il solo ripiego — che è identico a una tabella
        scritta a mano, ed è per questo che il revisore dell'integrità dei test
        ha potuto rimettere la tabella cablata lasciando 238 test verdi. Qui il
        registro dichiara «Sapori & Co.», che nessuna regola sull'identificativo
        saprebbe inventare.
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
        """Il ripiego: `nuovo_fornitore_1` non diventa una frase con gli underscore."""

        percorso = scrivi_cipresso(self.radice / "ignoto.xlsx")
        catalogo = SupplierCatalog()

        risultati = catalogo.search(
            self.review(self.voce("nuovo_fornitore_1", percorso, MAPPATURA_CIPRESSO)), "tovaglioli",
        )

        self.assertEqual(len(risultati), 1, catalogo.load_errors)
        self.assertEqual(risultati[0]["offers"][0]["supplierName"], "NUOVO FORNITORE 1")

    def test_i_fornitori_di_oggi_si_chiamano_come_prima(self) -> None:
        """L'altra meta' della regola: il registro dice gli stessi quattro nomi.

        Un cambio di sorgente che cambiasse anche i nomi in pagina sarebbe una
        correzione con una regressione dentro.
        """

        percorso = scrivi_cipresso(self.radice / "cipresso.xlsx")
        catalogo = SupplierCatalog()

        risultati = catalogo.search(
            self.review(self.voce("cipresso", percorso, MAPPATURA_CIPRESSO)), "tovaglioli",
        )

        self.assertEqual(len(risultati), 1, catalogo.load_errors)
        self.assertEqual(risultati[0]["offers"][0]["supplierName"], "CIPRESSO")

    def test_senza_il_registro_degli_adattatori_il_motivo_nomina_il_registro(self) -> None:
        """Registro assente e review incompleta sono due guasti diversi."""
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
        """`str(MemoryError())` è vuoto: l'avviso non deve finire con i due punti."""
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
        """Senza memoria dell'errore, ogni tasto digitato riaprirebbe il file."""
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
