"""Regression coverage for the learned-adapter path: what the program knows
about a supplier it has learned, and who tells it.

- Anything that reads the adapter registry reads it through `registro`, not by
  opening a registry file directly; a learned id (e.g. `betulla_v1__locale`)
  only exists in the learned file, not the shipped one.
- A BETULLA/LARICE variant learned from the guided mapping page names its
  columns by header text, while the dedicated readers expect column
  positions; the learned mapping must be resolved to positions before reaching
  them.
- An "Availability" column can be mapped, and its values must be interpreted
  (a row marked unavailable must not stay orderable).
- A price written "1.25" (dot, two decimals) must not be read as 125 by the
  Italian-number parser.
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

# The real BETULLA price-list header: the required columns declared by the
# registry (`cessione`, `codart`, `ean`, `ordine`, `pzct`) must actually be
# present, or a different check would stop the read instead.
INTESTAZIONE_BETULLA = ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva"]
RIGA_BETULLA = ["8000000000001", "1300634", None, "VAPO Emanatore", 12, 3.98, 9.99, 22]

# The mapping as the page writes it when the header is unambiguous: by
# column NAME, not by letter (`schema_mapping.specifica_colonna`).
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
    """The entry `impara_adattatore` writes when a BETULLA variant is confirmed."""

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
    """`registro` merges the shipped and learned registry files; nothing else
    opens either one directly."""

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
        # This is why the catalog wants the shipped mapping specifically: a
        # mapping confirmed in the page is built from scratch and carries no
        # `exclude_rows`.
        self.scrivi_registri(
            [{"id": "noce_xls_v1", "field_mapping": {"columns": {"ean": 1}, "exclude_rows": [{"column": 2}]}}],
            [{"id": "noce_xls_v1", "field_mapping": {"columns": {"ean": 9}}}],
        )

        spedita = registro.mappatura_spedita("noce_xls_v1", self.spedito)

        self.assertEqual(spedita["columns"], {"ean": 1})
        self.assertTrue(spedita.get("exclude_rows"))

    def test_un_registro_che_non_si_legge_non_ha_mappature_spedite(self) -> None:
        # Same fallback as above: a broken registry must not turn the catalog
        # viewer into an error page.
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
    """Pipeline phase 3 reading a BETULLA adapter learned in an earlier run."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def esegui(self, imparato: dict, decisione: dict) -> dict:
        """Run phase 3 on a management export and a BETULLA file, return the audit."""

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
        # The adapter, not the AI decision, names the supplier: reading only
        # the shipped file, `betulla_v1__locale` doesn't exist there, and
        # phase 3 would fail with "missing supplier_id" on a file the
        # registry actually recognizes.
        imparato = betulla_imparato()
        imparato["field_mapping"]["columns"] = {"unit_price_net": "F", "description": "D"}

        normalizzati = self.esegui(imparato, {
            "state": "SCHEMA_NOTO", "role": "supplier", "adapter_id": "betulla_v1__locale",
            "field_mapping": imparato["field_mapping"],
        })

        self.assertIn("betulla", normalizzati)
        self.assertEqual(normalizzati["betulla"][0]["unit_price_net"], "3.9800")

    def test_una_variante_imparata_di_betulla_si_rilegge_la_settimana_dopo(self) -> None:
        # Columns named by header text must reach the dedicated reader already
        # resolved to positions; otherwise the read fails with "the column
        # given for description ('Descr.Commerciale') is not a valid column".
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
    """Where column names get resolved to positions, and what happens when they don't."""

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
        # The catalog viewer must resolve columns the same way the pipeline
        # does; if only the pipeline can, BETULLA stays in the comparison but
        # drops out of product search.
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
    """Mapping an "Availability" column isn't enough without saying what its values mean."""

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
    """"1.25" is neither one thousand two hundred fifty nor 125."""

    def test_la_virgola_resta_il_separatore_dei_decimali(self) -> None:
        self.assertEqual(prepare_sources.decimal_value("1,25", italian=True), Decimal("1.25"))
        self.assertEqual(prepare_sources.decimal_value("12.345,50", italian=True), Decimal("12345.50"))

    def test_un_punto_con_due_cifre_dietro_non_si_legge_invece_di_moltiplicare(self) -> None:
        # Better a row discarded as SENZA_PREZZO than a price of 125 instead
        # of 1.25 that wins the comparison.
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
