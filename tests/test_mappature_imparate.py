"""Le quattro cose che la revisione del 6 settembre 2026 ha trovato qui.

Stanno insieme perche' partono tutte dallo stesso punto — che cosa il
programma sa di un fornitore imparato, e chi glielo racconta:

* **R12** chi legge il registro lo legge da `registro`. Il preparatore ne
  apriva un file solo, e per un id imparato (`betulla_v1__locale`) non trovava
  niente; il catalogo si leggeva lo spedito per conto suo;
* **R11** una variante di BETULLA o di LARICE imparata dalla mappatura guidata
  indica le colonne **per nome**, e i lettori dedicati leggono per posizione:
  la settimana dopo il ricalcolo si fermava;
* **R4** la colonna «Disponibilita» si poteva mappare, e nessuno la leggeva:
  le righe con «NO» restavano ordinabili;
* **R5** un prezzo scritto «1.25» diventava 125, senza dirlo a nessuno.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
for cartella in (ROOT / "app", ROOT / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import catalog_search  # noqa: E402
import prepare_manifest_sources  # noqa: E402
import prepare_sources  # noqa: E402
import registro  # noqa: E402
import schema_mapping  # noqa: E402
from inspect_sources import profile_file  # noqa: E402


REGISTRO_VERO = json.loads((ROOT / "references" / "adapters.json").read_text(encoding="utf-8"))
BETULLA_SPEDITO = next(voce for voce in REGISTRO_VERO["adapters"] if voce["id"] == "betulla_v1")
GESTIONALE_SPEDITO = next(voce for voce in REGISTRO_VERO["adapters"] if voce["id"] == "gestionale_v1")

# L'intestazione vera del listino BETULLA: le obbligatorie che il registro
# dichiara (`cessione`, `codart`, `ean`, `ordine`, `pzct`) devono esserci
# davvero, altrimenti a fermare la lettura sarebbe un altro controllo.
INTESTAZIONE_BETULLA = ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva"]
RIGA_BETULLA = ["8000000000001", "1300634", None, "VAPO Emanatore", 12, 3.98, 9.99, 22]

# La mappatura come la scrive la pagina quando l'intestazione e' univoca:
# per NOME, non per lettera (`schema_mapping.specifica_colonna`).
COLONNE_PER_NOME = {
    "ean": "EAN",
    "supplier_code": "CodArt",
    "description": "Descr.Commerciale",
    "pieces_per_carton": "PzCt",
    "unit_price_net": "Cessione",
}


def salva(percorso: Path, righe: list[list[object]], foglio: str = "Listino") -> Path:
    libro = Workbook()
    pagina = libro.active
    pagina.title = foglio
    for riga in righe:
        pagina.append(riga)
    libro.save(percorso)
    libro.close()
    return percorso


def betulla_imparato() -> dict:
    """La voce che `impara_adattatore` scrive confermando una variante di BETULLA."""

    return {
        "id": "betulla_v1__locale",
        "kind": "supplier",
        "supplier_id": "betulla",
        "display_name": "BETULLA",
        "sopra_spedito": registro.sopra_spedito_di(BETULLA_SPEDITO),
        "header_signature": {
            "kind": "headers",
            "sheet": "FIRST",
            "header_row": 1,
            "data_start_row": 2,
            "columns": registro.posizioni_delle_intestazioni(INTESTAZIONE_BETULLA),
            "required": ["cessione", "codart", "ean", "ordine", "pzct"],
            "known": registro.impronta_intestazioni(INTESTAZIONE_BETULLA),
        },
        "field_mapping": {
            "sheet": "Listino",
            "header_row": 1,
            "data_start_row": 2,
            "columns": dict(COLONNE_PER_NOME),
            "order_column": "C",
            "italian_numbers": True,
        },
    }


class IlRegistroSiLeggeDaRegistro(unittest.TestCase):
    """R12 — i due file li fonde `registro`, e nessuno se li apre per conto suo."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spedito = self.root / "adapters.json"

    def scrivi_registri(self, spedite: list[dict], imparate: list[dict] | None = None) -> None:
        self.spedito.write_text(json.dumps({"adapters": spedite}, ensure_ascii=False), encoding="utf-8")
        if imparate is not None:
            registro.percorso_imparato(self.spedito).write_text(
                json.dumps({"adapters": imparate}, ensure_ascii=False), encoding="utf-8",
            )

    def test_la_mappatura_spedita_non_guarda_quella_imparata(self) -> None:
        # E' la ragione per cui il catalogo la vuole: la mappatura confermata
        # in pagina si costruisce da zero e non porta `exclude_rows`.
        self.scrivi_registri(
            [{"id": "noce_xls_v1", "field_mapping": {"columns": {"ean": 1}, "exclude_rows": [{"column": 2}]}}],
            [{"id": "noce_xls_v1", "field_mapping": {"columns": {"ean": 9}}}],
        )

        spedita = registro.mappatura_spedita("noce_xls_v1", self.spedito)

        self.assertEqual(spedita["columns"], {"ean": 1})
        self.assertTrue(spedita.get("exclude_rows"))

    def test_un_registro_che_non_si_legge_non_ha_mappature_spedite(self) -> None:
        # La stessa risposta di prima: un registro rotto non deve trasformare
        # il visualizzatore in una pagina di errori.
        self.assertEqual(registro.mappatura_spedita("betulla_v1", self.root / "manca.json"), {})
        (self.root / "rotto.json").write_text("{", encoding="utf-8")
        self.assertEqual(registro.mappatura_spedita("betulla_v1", self.root / "rotto.json"), {})
        self.scrivi_registri([{"id": "betulla_v1"}])
        self.assertEqual(registro.mappatura_spedita("betulla_v1", self.spedito), {})

    def test_il_catalogo_chiede_la_mappatura_spedita_al_registro(self) -> None:
        chiamate: list[tuple] = []

        def finta(adapter_id, percorso):
            chiamate.append((adapter_id, percorso))
            return {"columns": {"ean": 1}}

        with mock.patch.object(catalog_search, "mappatura_spedita", finta):
            esito = catalog_search._mappatura_spedita("noce_xls_v1")

        self.assertEqual(esito, {"columns": {"ean": 1}})
        self.assertEqual(chiamate, [("noce_xls_v1", catalog_search.ADAPTERS_PATH)])


class IlPreparatoreEIlRegistroImparato(unittest.TestCase):
    """R12 e R11 — la fase 3 su un BETULLA imparato la settimana prima."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def esegui(self, imparato: dict, decisione: dict) -> dict:
        """Fa girare la fase 3 su un gestionale e un BETULLA, e rende l'audit."""

        master = salva(self.root / "gestionale.xlsx", [
            ["C", "8000000000001", "", "Prodotto Alfa", "PZ", 1, 1, "2,50", "", 22],
        ], foglio="Foglio1")
        listino = salva(self.root / "betulla.xlsx", [INTESTAZIONE_BETULLA, RIGA_BETULLA])
        spedito = self.root / "adapters.json"
        spedito.write_text(
            json.dumps({"adapters": [GESTIONALE_SPEDITO, BETULLA_SPEDITO]}, ensure_ascii=False),
            encoding="utf-8",
        )
        registro.percorso_imparato(spedito).write_text(
            json.dumps({"adapters": [imparato]}, ensure_ascii=False), encoding="utf-8",
        )
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"files": [
            {"path": str(master), "file_name": master.name,
             "ai_preflight": {"state": "SCHEMA_NOTO", "role": "master", "adapter_id": "gestionale_v1"}},
            {"path": str(listino), "file_name": listino.name, "ai_preflight": decisione},
        ]}), encoding="utf-8")
        uscita = self.root / "normalizzati"
        argomenti = ["prepare_manifest_sources.py", "--manifest", str(manifest),
                     "--adapters", str(spedito), "--output", str(uscita)]
        with mock.patch.object(sys, "argv", argomenti), redirect_stdout(StringIO()):
            prepare_manifest_sources.main()
        return json.loads((uscita / "normalized_sources.json").read_text(encoding="utf-8"))

    def test_il_preparatore_trova_l_adattatore_imparato(self) -> None:
        # Il fornitore lo dice l'adattatore, non la decisione: leggendo il solo
        # file spedito, `betulla_v1__locale` non c'era e la fase 3 si fermava con
        # «supplier_id mancante» su un documento che il registro conosce.
        imparato = betulla_imparato()
        imparato["field_mapping"]["columns"] = {"unit_price_net": "F", "description": "D"}

        normalizzati = self.esegui(imparato, {
            "state": "SCHEMA_NOTO", "role": "supplier", "adapter_id": "betulla_v1__locale",
            "field_mapping": imparato["field_mapping"],
        })

        self.assertIn("betulla", normalizzati)
        self.assertEqual(normalizzati["betulla"][0]["unit_price_net"], "3.9800")

    def test_una_variante_imparata_di_betulla_si_rilegge_la_settimana_dopo(self) -> None:
        # Le colonne indicate per nome arrivano al lettore dedicato risolte in
        # posizioni: senza, la lettura si fermava con «La colonna indicata per
        # description ("Descr.Commerciale") non è una colonna valida».
        imparato = betulla_imparato()

        normalizzati = self.esegui(imparato, {
            "state": "SCHEMA_NOTO", "role": "supplier", "supplier_id": "betulla",
            "adapter_id": "betulla_v1__locale", "field_mapping": imparato["field_mapping"],
        })

        riga = normalizzati["betulla"][0]
        self.assertEqual(riga["description"], "VAPO Emanatore")
        self.assertEqual(riga["unit_price_net"], "3.9800")
        self.assertEqual(riga["ean"], "8000000000001")


class ColonnePerNomeDiUnaVarianteImparata(unittest.TestCase):
    """R11 — dove i nomi diventano posizioni, e che cosa succede se non lo fanno."""

    def test_i_nomi_diventano_le_posizioni_dell_adattatore_imparato(self) -> None:
        decisione = {"field_mapping": {"columns": dict(COLONNE_PER_NOME), "order_column": "C"}}

        esito = prepare_manifest_sources.colonne_corrette(
            decisione, betulla_imparato(), "betulla_v1__locale",
        )

        self.assertEqual(esito["colonne"], {
            "ean": 1, "supplier_code": 2, "description": 4, "pieces_per_carton": 5, "unit_price_net": 6,
        })
        self.assertEqual(esito["order_column"], "C")

    def test_lettere_e_numeri_restano_come_sono(self) -> None:
        decisione = {"field_mapping": {"columns": {"unit_price_net": "G", "description": 4}}}

        esito = prepare_manifest_sources.colonne_corrette(
            decisione, betulla_imparato(), "betulla_v1__locale",
        )

        self.assertEqual(esito["colonne"], {"unit_price_net": "G", "description": 4})

    def test_un_nome_che_l_impronta_non_conosce_ferma_invece_di_indovinare(self) -> None:
        decisione = {"field_mapping": {"columns": {"unit_price_net": "Prezzo di cessione"}}}

        with self.assertRaisesRegex(ValueError, "Prezzo di cessione"):
            prepare_manifest_sources.colonne_corrette(
                decisione, betulla_imparato(), "betulla_v1__locale",
            )

    def test_il_lettore_dedicato_legge_con_le_posizioni_risolte(self) -> None:
        with tempfile.TemporaryDirectory() as temporaneo:
            percorso = salva(Path(temporaneo) / "betulla.xlsx", [INTESTAZIONE_BETULLA, RIGA_BETULLA])
            esito = prepare_manifest_sources.colonne_corrette(
                {"field_mapping": {"columns": dict(COLONNE_PER_NOME)}},
                betulla_imparato(), "betulla_v1__locale",
            )

            righe = prepare_sources.read_betulla(percorso, **esito)

        self.assertEqual(righe[0]["unit_price_net"], "3.9800")
        self.assertEqual(righe[0]["description"], "VAPO Emanatore")

    def test_anche_il_catalogo_rilegge_la_variante_imparata(self) -> None:
        # Il visualizzatore legge con lo stesso criterio della catena: se le
        # posizioni le trova solo la catena, BETULLA resta nel confronto e
        # sparisce dalla ricerca prodotti — il difetto del 21 agosto 2026.
        with tempfile.TemporaryDirectory() as temporaneo:
            root = Path(temporaneo)
            percorso = salva(root / "betulla.xlsx", [INTESTAZIONE_BETULLA, RIGA_BETULLA])
            spedito = root / "adapters.json"
            spedito.write_text(json.dumps({"adapters": [BETULLA_SPEDITO]}, ensure_ascii=False), encoding="utf-8")
            registro.percorso_imparato(spedito).write_text(
                json.dumps({"adapters": [betulla_imparato()]}, ensure_ascii=False), encoding="utf-8",
            )
            sorgente = catalog_search._Sorgente(
                path=percorso,
                mapping=betulla_imparato()["field_mapping"],
                adapter_id="betulla_v1__locale",
                state="SCHEMA_NOTO",
            )

            with mock.patch.object(catalog_search, "ADAPTERS_PATH", spedito):
                righe = catalog_search.SupplierCatalog._read_supplier("betulla", sorgente)

        self.assertEqual(righe[0]["unit_price_net"], "3.9800")


class LaColonnaDisponibilita(unittest.TestCase):
    """R4 — mappare «Disponibilita» senza dire che cosa significa non basta."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def profilo(self) -> dict:
        percorso = salva(self.root / "fornitore.xlsx", [
            ["Codice", "Nome", "Pezzi", "Prezzo", "Disponibilita"],
            ["1", "Prodotto uno", 6, "2,50", "SI"],
            ["2", "Prodotto due", 6, "1,10", "NO"],
        ])
        return profile_file(percorso)

    def scelta(self, **cambiamenti: object) -> dict:
        grezza = {
            "role": "supplier", "supplierName": "Nuovo fornitore", "sheet": "Listino",
            "headerRow": 1, "dataStartRow": 2,
            "columns": {"supplier_code": 1, "description": 2, "pieces_per_carton": 3,
                        "unit_price_net": 4, "availability": 5},
            "orderColumn": 6,
        }
        grezza.update(cambiamenti)
        return grezza

    def test_i_valori_dichiarati_finiscono_nella_mappatura_e_il_lettore_li_usa(self) -> None:
        profilo = self.profilo()
        decisione = schema_mapping.decisione_da_mappatura(
            profilo, self.scelta(availableValues="SI, S, disponibile"), [],
        )
        mappatura = decisione["field_mapping"]

        self.assertEqual(mappatura["available_values"], ["SI", "S", "disponibile"])
        self.assertNotIn("assume_available", mappatura)

        righe, _avvisi = prepare_manifest_sources.read_mapped_xlsx_supplier(
            Path(profilo["path"]), "nuovo-fornitore", mappatura,
        )
        per_descrizione = {riga["description"]: riga for riga in righe}
        self.assertTrue(per_descrizione["Prodotto uno"]["usable"])
        self.assertFalse(per_descrizione["Prodotto due"]["usable"])
        self.assertEqual(per_descrizione["Prodotto due"]["unusable_reason"],
                         prepare_sources.NON_DISPONIBILE)

    def test_senza_i_valori_la_conferma_si_rifiuta(self) -> None:
        with self.assertRaises(ValueError) as guasto:
            schema_mapping.decisione_da_mappatura(self.profilo(), self.scelta(availableValues="  ,  "), [])

        self.assertIn("indica quali valori della colonna «Disponibilità» significano disponibile",
                      str(guasto.exception))

    def test_senza_la_colonna_resta_il_disponibile_per_scontato(self) -> None:
        scelta = self.scelta()
        scelta["columns"] = {chiave: valore for chiave, valore in scelta["columns"].items()
                             if chiave != "availability"}

        mappatura = schema_mapping.decisione_da_mappatura(self.profilo(), scelta, [])["field_mapping"]

        self.assertTrue(mappatura["assume_available"])
        self.assertNotIn("available_values", mappatura)


class IlPrezzoScrittoColPunto(unittest.TestCase):
    """R5 — «1.25» non e' milleduecentocinquanta, e non e' nemmeno 125."""

    def test_la_virgola_resta_il_separatore_dei_decimali(self) -> None:
        self.assertEqual(prepare_sources.decimal_value("1,25", italian=True), Decimal("1.25"))
        self.assertEqual(prepare_sources.decimal_value("12.345,50", italian=True), Decimal("12345.50"))

    def test_un_punto_con_due_cifre_dietro_non_si_legge_invece_di_moltiplicare(self) -> None:
        # Meglio una riga fra gli scarti contati come SENZA_PREZZO che un
        # prezzo di 125 euro al posto di 1,25 che vince il confronto.
        self.assertIsNone(prepare_sources.decimal_value("1.25", italian=True))
        self.assertIsNone(prepare_sources.decimal_value("1.2", italian=True))

    def test_le_migliaia_restano_le_migliaia(self) -> None:
        self.assertEqual(prepare_sources.decimal_value("1.250", italian=True), Decimal("1250"))

    def test_le_celle_numeriche_vere_non_cambiano(self) -> None:
        self.assertEqual(prepare_sources.decimal_value(1.25, italian=True), Decimal("1.25"))
        self.assertEqual(prepare_sources.decimal_value(Decimal("1.25"), italian=True), Decimal("1.25"))
        self.assertEqual(prepare_sources.decimal_value(1250, italian=True), Decimal("1250"))


if __name__ == "__main__":
    unittest.main()
