#!/usr/bin/env python3
"""Browses a price list the way the program read it.

Exercises `catalog_search` directly. Earlier tests of the browser were all
page-level (`app.js` in Node) or service-level against a fake catalog, so
none of them actually ran this module — three targeted mutations (zeroing
the discarded-row count, letting an unorderable row get matched, dropping
the highlight on an already-matched row) stayed green because nothing
exercised that code path.

Rows are declared inline and `_ensure_loaded` is a no-op: reading real price
lists from disk would make the test depend on the last run, which this
project avoids everywhere else too.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

from catalog_search import SupplierCatalog  # noqa: E402

CONFRONTO: dict[str, Any] = {"run": {"id": "run-prova"}, "files": [], "products": []}


def riga(source_row: int, ean: str, descrizione: str, prezzo: str | None = "1.1500",
         pezzi: str | None = "6", **extra: Any) -> dict[str, Any]:
    voce = {
        "source_row": source_row,
        "ean": ean,
        "description": descrizione,
        "unit_price_net": prezzo,
        "pieces_per_carton": pezzi,
        "usable": True,
        **extra,
    }
    return voce


# Four real rows for supplier NOCE, plus a separator row with no price and a
# row priced at zero: the two real ways a row ends up not orderable.
RIGHE = [
    riga(4792, "8729145039080", "LUXA SAPONE EROGATORE CARE&PROTECT 250"),
    riga(4793, "8729014462339", "LUXA SAPONE EROGATORE GO FRESH ML.250", supplier_code="0000429062"),
    riga(4794, "8729721830575", "LUXA SAPONE EROGATORE ORIGINAL ML.250"),
    riga(4795, "8729251046712", "LUXA SAPONE EROGATORE SETA ML.250"),
    riga(5000, "", "LISTINO", prezzo=None, pezzi=None, unusable_reason="SENZA_PREZZO"),
    riga(5001, "8000000000001", "NEVAL SOFT 25ML", prezzo="0"),
]


class CatalogoDichiarato(SupplierCatalog):
    """The real catalog, with rows set by hand instead of read from files."""

    def __init__(self, righe: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__()
        self.righe_per_fornitore = righe

    def _ensure_loaded(self, review: dict[str, Any]) -> None:
        return None


class BancoDelVisualizzatore(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogo = CatalogoDichiarato({
            "noce": [dict(voce) for voce in RIGHE],
            "betulla": [riga(1751, "8729721830575", "LUXA Sapone Liquido Original 250 Ml", "1.1900")],
        })

    def sfoglia(self, **extra):
        return self.catalogo.sfoglia(CONFRONTO, extra.pop("fornitore", "noce"), **extra)


class IlContoDelleRigheEOnesto(BancoDelVisualizzatore):
    def test_le_righe_scartate_si_contano_sempre(self) -> None:
        """A list showing 4 of 6 rows without saying so is hiding data."""

        pagina = self.sfoglia()

        self.assertEqual(pagina["totale"], 6)
        self.assertEqual(pagina["scartate"], 2)

    def test_una_riga_non_ordinabile_si_vede_e_porta_il_motivo(self) -> None:
        pagina = self.sfoglia(query="LISTINO")

        voce = pagina["righe"][0]
        self.assertIs(voce["ordinabile"], False)
        self.assertEqual(voce["motivo"], "SENZA_PREZZO")

    def test_un_prezzo_a_zero_non_e_ordinabile(self) -> None:
        """The same guard the comparison always applies: a zero price passes
        every filter and would otherwise win, since the lowest price ranks first."""

        pagina = self.sfoglia(query="NEVAL")

        self.assertIs(pagina["righe"][0]["ordinabile"], False)

    def test_la_ricerca_dice_quante_ne_ha_trovate_su_quante(self) -> None:
        pagina = self.sfoglia(query="LUXA EROGATORE")

        self.assertEqual(pagina["trovate"], 4)
        self.assertEqual(pagina["totale"], 6)

    def test_si_cerca_anche_per_ean_e_per_codice_del_fornitore(self) -> None:
        self.assertEqual(self.sfoglia(query="8729721830575")["trovate"], 1)
        self.assertEqual(self.sfoglia(query="0000429062")["trovate"], 1)

    def test_le_colonne_sono_quelle_su_cui_si_ordina(self) -> None:
        """Not the sheet's own columns, but the fields a match is decided on.
        If one is misread, it shows misread here, which is the useful signal."""

        voce = self.sfoglia(query="ORIGINAL")["righe"][0]

        self.assertEqual(voce["sourceRow"], 4794)
        self.assertEqual(voce["ean"], "8729721830575")
        self.assertEqual(voce["piecesPerCarton"], 6.0)
        self.assertEqual(voce["unitPriceNet"], 1.15)
        self.assertEqual(voce["orderUnitPriceNet"], 6.9)

    def test_un_fornitore_che_non_c_e_lo_dice(self) -> None:
        with self.assertRaises(ValueError):
            self.sfoglia(fornitore="quercia")


class LaPaginaSiApreDoveServe(BancoDelVisualizzatore):
    def test_il_fuoco_va_sulla_pagina_che_contiene_la_riga(self) -> None:
        """Across eight thousand rows, making the user search by hand for
        something the program already knows defeats the point of the tool."""

        pagina = self.sfoglia(quante=2, riga=4795)

        self.assertEqual(pagina["da"], 2)
        self.assertEqual([voce["sourceRow"] for voce in pagina["righe"]], [4794, 4795])
        self.assertEqual(pagina["rigaCercata"], "4795")

    def test_una_riga_che_la_ricerca_esclude_lo_dice(self) -> None:
        """Instead of leaving the user to search a page where the row isn't."""

        pagina = self.sfoglia(query="NEVAL", riga=4794)

        self.assertIsNone(pagina["rigaCercata"])
        self.assertEqual(pagina["da"], 0)

    def test_senza_riga_si_parte_dall_inizio(self) -> None:
        pagina = self.sfoglia(quante=3)

        self.assertEqual(pagina["da"], 0)
        self.assertEqual(len(pagina["righe"]), 3)

    def test_un_salto_oltre_la_fine_torna_sull_ultima_pagina(self) -> None:
        pagina = self.sfoglia(quante=2, da=999)

        self.assertEqual(pagina["da"], 4)
        self.assertEqual(len(pagina["righe"]), 2)


class LaRigaSceltaDiventaUnOfferta(BancoDelVisualizzatore):
    def test_una_riga_ordinabile_diventa_un_offerta_completa(self) -> None:
        record, offerta = self.catalogo.offerta_dalla_riga(CONFRONTO, "noce", 4794)

        self.assertEqual(record["description"], "LUXA SAPONE EROGATORE ORIGINAL ML.250")
        self.assertEqual(offerta["unitPriceNet"], 1.15)
        self.assertEqual(offerta["quantityFactor"], 6.0)
        self.assertIs(offerta["available"], True)

    def test_una_riga_senza_prezzo_non_si_abbina(self) -> None:
        """Ordering it would mean a quantity that can't be computed, the same
        rejection the service applies to matches proposed by the AI analysis."""

        with self.assertRaises(ValueError) as errore:
            self.catalogo.offerta_dalla_riga(CONFRONTO, "noce", 5000)

        self.assertIn("non è ordinabile", str(errore.exception))
        self.assertIn("SENZA_PREZZO", str(errore.exception))

    def test_una_riga_che_non_esiste_piu_lo_dice(self) -> None:
        with self.assertRaises(ValueError) as errore:
            self.catalogo.offerta_dalla_riga(CONFRONTO, "noce", 9999)

        self.assertIn("9999", str(errore.exception))

    def test_quali_listini_si_possono_aprire(self) -> None:
        fornitori = self.catalogo.fornitori_sfogliabili(CONFRONTO)

        self.assertEqual(
            [(voce["id"], voce["righe"], voce["ordinabili"]) for voce in fornitori],
            [("betulla", 1, 1), ("noce", 6, 4)],
        )


class LaFinestraNonDiventaUnVicoloCieco(unittest.TestCase):
    """A price list that fails to load must not shut the door on the others.

    `sfoglia_listino` must compute the supplier list before calling
    `sfoglia()`, not as part of the same expression: if it computed the
    supplier list after `sfoglia()` and that raised, the supplier list would
    never be built, the route would answer 400, and the "Fornitore" dropdown
    would stay empty with no way to pick a different supplier, even though
    the other price lists load fine.
    """

    def setUp(self) -> None:
        import tempfile, shutil, json  # noqa: PLC0415
        from server import ReviewStore  # noqa: PLC0415

        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_vicolo_"))
        self.addCleanup(shutil.rmtree, self.radice, True)
        (self.radice / "uploads").mkdir()
        (self.radice / "out").mkdir()
        listini = RADICE / "listini-storici"
        for nome in ("OFFERTE AGOSTO 4.xlsx", "Listino3_33.xlsx"):
            if not (listini / nome).is_file():
                self.skipTest(f"Manca il listino {nome}")
        confronto = {
            "run": {"id": "run-prova"},
            "suppliers": [{"id": "betulla", "name": "BETULLA"}, {"id": "cipresso", "name": "CIPRESSO"}],
            "products": [],
            "files": [
                # A real case: a file BETULLA's dedicated reader can't open,
                # attributed to BETULLA anyway.
                {
                    "name": "OFFERTE AGOSTO 4.xlsx", "role": "supplier", "supplierId": "betulla",
                    "sourcePath": str(listini / "OFFERTE AGOSTO 4.xlsx"),
                    "adapterId": "betulla_v1", "schemaState": "SCHEMA_NOTO", "fieldMapping": None,
                },
                {
                    "name": "Listino3_33.xlsx", "role": "supplier", "supplierId": "cipresso",
                    "sourcePath": str(listini / "Listino3_33.xlsx"),
                    "adapterId": "cipresso_v1", "schemaState": "SCHEMA_NOTO", "fieldMapping": None,
                },
            ],
        }
        percorso = self.radice / "review_data.json"
        percorso.write_text(json.dumps(confronto), encoding="utf-8")
        self.store = ReviewStore(
            percorso, self.radice / "state.json", self.radice / "uploads", self.radice / "out",
        )

    def test_un_listino_illeggibile_lascia_gli_altri_nella_tendina(self) -> None:
        esito = self.store.sfoglia_listino("betulla")

        self.assertTrue(esito["ok"])
        self.assertEqual([voce["id"] for voce in esito["fornitori"]], ["cipresso"])
        self.assertEqual(esito["righe"], [])

    def test_il_motivo_vero_arriva_in_finestra(self) -> None:
        """A generic "no list loaded" message says neither what went wrong nor how to fix it."""

        messaggio = (self.store.sfoglia_listino("betulla").get("problema") or {}).get("messaggio", "")

        self.assertIn("Schema BETULLA non riconosciuto", messaggio)
        self.assertIn("tendina", messaggio)

    def test_senza_fornitore_si_apre_il_primo_che_si_puo_sfogliare(self) -> None:
        """The button on a product with no offers doesn't carry any supplier."""

        esito = self.store.sfoglia_listino("")

        self.assertEqual(esito["supplier"], "cipresso")
        self.assertTrue(esito["righe"])
        self.assertEqual(esito.get("problema"), None)

    def test_un_fornitore_che_non_esiste_non_alza(self) -> None:
        esito = self.store.sfoglia_listino("acero")

        self.assertTrue(esito["ok"])
        self.assertIn("non fa parte di questo confronto", (esito.get("problema") or {}).get("messaggio", ""))


if __name__ == "__main__":
    unittest.main()
