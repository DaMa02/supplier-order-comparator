#!/usr/bin/env python3
"""Regression tests for three supplier-discount and history bugs.

`set_supplier_discount` re-decides the winning supplier on a comparison
rebuilt from scratch, ignoring manually matched price-list rows and any
supplier the user chose by hand; applying a header discount could move the
order to an offer not shown on the page, or to a supplier the user had just
rejected.

Order-history recording happens before the audit file is written; if the
audit write fails (disk full, antivirus lock), the working folder is rolled
back but the history entry it recorded is not, leaving a phantom order the
system later asks about.

`productId` and `sourceRow` are positional. A stale page still holding last
week's comparison can send matches that point at different items in the
current comparison, and the resulting EAN equality would be written to the
registry permanently.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "tests"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

# Reuse the two existing test fixtures (synthetic comparison with manual
# matching, and the compile flow that reaches FILES_READY with a fake writer)
# instead of duplicating their setup here.
import test_abbinamento_a_mano as banco_abbinamento  # noqa: E402
import test_web_app as banco_web  # noqa: E402


# ---------------------------------------------------------------------------
# Supplier discount reassigns the winning offer
# ---------------------------------------------------------------------------

# larice's price list has a wrong row at 1 EUR/unit; the correct row costs 5
# and was matched by hand. betulla is at 2 and was chosen by the user.
RIGHE_LARICE = [
    {"source_row": 700, "ean": "8000000000001", "description": "LA RIGA GIUSTA DI LARICE",
     "pieces_per_carton": "6", "unit_price_net": "5.0000", "usable": True},
]


def confronto_dello_sconto() -> dict[str, Any]:
    return {
        "run": {"id": "run-sconto", "status": "ready", "label": "Prova"},
        "files": [],
        "suppliers": [
            {"id": "larice", "name": "LARICE", "minimumOrder": 0},
            {"id": "betulla", "name": "BETULLA", "minimumOrder": 0},
        ],
        "products": [{
            "id": "product:1",
            "kind": "PRODUCT",
            "itemType": "product",
            "sourceRow": 10,
            "ean": "8000000000001",
            "name": "PRODOTTO",
            "description": "PRODOTTO",
            "quantity": 1,
            "selectedSupplierId": "larice",
            "confirmed": True,
            "requiresConfirmation": False,
            "offers": [
                {"supplierId": "larice", "available": True, "description": "LA RIGA SBAGLIATA",
                 "ean": "8000000000001", "sourceRow": 100, "unitPriceNet": 1.0,
                 "quantityFactor": 6, "orderUnitPriceNet": 6.0,
                 "method": "EAN", "confidence": "CERTA"},
                {"supplierId": "betulla", "available": True, "description": "LA RIGA DI BETULLA",
                 "ean": "8000000000001", "sourceRow": 200, "unitPriceNet": 2.0,
                 "quantityFactor": 6, "orderUnitPriceNet": 12.0,
                 "method": "EAN", "confidence": "CERTA"},
            ],
            "components": [],
        }],
        "warnings": [],
    }


class LoScontoRiassegnaSulConfrontoCheSiVede(banco_abbinamento.BancoDellAbbinamento):
    """A discount changes prices, so reassignment must use the comparison as
    it is shown on the page, not a freshly rebuilt one."""

    def negozio_dello_sconto(self, decisione: dict[str, Any]) -> Any:
        store = self.negozio(confronto_dello_sconto(), {"larice": list(RIGHE_LARICE)})
        store.state_path.write_text(json.dumps({
            "schemaVersion": 1,
            "runId": "run-sconto",
            "stateVersion": 3,
            "products": [decisione],
        }, ensure_ascii=False), encoding="utf-8")
        # Manually match larice's correct row: 5 EUR/unit instead of 1.
        store.abbina_riga_di_listino({
            "productId": "product:1", "supplierId": "larice", "sourceRow": 700,
        })
        return store

    @staticmethod
    def fornitore_deciso(store: Any) -> str:
        stato = json.loads(store.state_path.read_text(encoding="utf-8"))
        voce = next(item for item in stato["products"] if item["id"] == "product:1")
        return str(voce.get("selectedSupplierId") or "")

    def test_lo_sconto_non_sposta_un_fornitore_scelto_a_mano(self) -> None:
        """Applies even here: `selectedSupplierSource == "utente"` blocks
        reassignment.

        An 80% discount drops larice to 1 EUR/unit, making it the cheapest
        even on the decorated comparison, but the user's manual choice must
        still stand: a discount changes prices, not manual decisions.
        """

        store = self.negozio_dello_sconto({
            "id": "product:1", "quantity": 1, "selectedSupplierId": "betulla",
            "selectedSupplierSource": "utente", "confirmed": True,
            "quantitySource": "gestionale",
        })

        store.set_supplier_discount({"supplierId": "larice", "percent": 80})

        self.assertEqual(self.fornitore_deciso(store), "betulla")
        prodotto = store.review()["products"][0]
        self.assertEqual(prodotto["selectedSupplierId"], "betulla")

    def test_lo_sconto_riassegna_sul_confronto_decorato_non_su_quello_nudo(self) -> None:
        """Automatic reassignment must use the decorated comparison's prices.

        On the raw comparison larice still shows its wrong 1 EUR/unit row and
        would win; on the decorated one it costs 5, and a 1% discount puts
        betulla at 1.98.
        """

        store = self.negozio_dello_sconto({
            "id": "product:1", "quantity": 1, "selectedSupplierId": "larice",
            "confirmed": True, "quantitySource": "gestionale",
        })

        esito = store.set_supplier_discount({"supplierId": "betulla", "percent": 1})

        self.assertEqual(esito["reassigned"], 1)
        self.assertEqual(self.fornitore_deciso(store), "betulla")
        prodotto = store.review()["products"][0]
        self.assertEqual(prodotto["selectedSupplierId"], "betulla")
        # The price the decision was made on is the manually matched row,
        # not the wrong one.
        larice = next(o for o in prodotto["offers"] if o["supplierId"] == "larice")
        self.assertAlmostEqual(larice["unitPriceNet"], 5.0, places=4)


# ---------------------------------------------------------------------------
# Order history vs. a failing audit write
# ---------------------------------------------------------------------------

class LoStoricoTornaComEraSeLAuditFallisce(banco_web.ConsegnaBase):
    """History and the delivered order folder must stay in sync: either the
    delivery happened, or neither side was written."""

    def ordine_precedente(self) -> bytes:
        """A pending larice order from the same run, not yet answered.

        This is the entry `record_plan` would remove when the same supplier
        is delivered again. If history isn't rolled back on a failed write,
        that entry — a real, already-sent order — disappears and nothing
        records it was ever sent.
        """

        storico = {
            "schema_version": 1,
            "orders": [{
                "orderId": "2026-09-05_0900:larice",
                "createdAt": datetime.now(tz=timezone.utc).isoformat(),
                "supplier": "larice",
                "supplierName": "Larice",
                "runId": "run-sintetica",
                "status": "in_attesa",
                "answeredAt": None,
                "totalNet": 12.0,
                "lines": [],
            }],
        }
        self.store.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.history_path.write_text(
            json.dumps(storico, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return self.store.history_path.read_bytes()

    def test_lo_storico_resta_identico_se_l_audit_non_si_scrive(self) -> None:
        prima = self.ordine_precedente()
        scrittore = banco_web.ScrittoreFinto({"larice": self.listini["larice"]})

        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer", scrittore), \
                mock.patch.object(banco_web.consegna, "scrivi_audit",
                                  side_effect=OSError("Il disco è pieno")):
            with self.assertRaises(OSError):
                self.store.compile(self.snapshot(2, "larice"))

        self.assertEqual(self.store.history_path.read_bytes(), prima)
        self.assertEqual(self.cartelle(), [])

    def test_senza_storico_di_prima_non_ne_resta_uno_dopo(self) -> None:
        """Mirror case: if the history file didn't exist, a compile that
        fails partway through must not create one."""

        self.assertFalse(self.store.history_path.exists())
        scrittore = banco_web.ScrittoreFinto({"larice": self.listini["larice"]})

        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer", scrittore), \
                mock.patch.object(banco_web.consegna, "scrivi_audit",
                                  side_effect=OSError("Il disco è pieno")):
            with self.assertRaises(OSError):
                self.store.compile(self.snapshot(2, "larice"))

        self.assertFalse(self.store.history_path.exists())
        self.assertEqual(self.cartelle(), [])


# ---------------------------------------------------------------------------
# Manual matching vs. a stale run id
# ---------------------------------------------------------------------------

class LAbbinamentoAManoGuardaLaRunDellaScheda(banco_abbinamento.BancoDellAbbinamento):
    """`productId` and `sourceRow` are positional and say nothing about which
    run's comparison they were read from."""

    def test_una_scheda_di_un_altra_run_non_abbina_niente(self) -> None:
        store = self.negozio()

        with self.assertRaises(ValueError) as errore:
            store.abbina_riga_di_listino({
                "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
                "runId": "run-della-settimana-scorsa",
            })

        self.assertIn("ricarica la pagina", str(errore.exception))
        # Nothing is written: neither the state file nor an EAN equality,
        # which would be permanent and apply across all suppliers.
        self.assertFalse(store.state_path.exists())
        self.assertEqual(self.offerte_disponibili(store.review()), {"cipresso": 1.28})
        self.assertEqual(store.magazzino_conferme().uguaglianze(), [])

    def test_con_la_run_giusta_abbina_come_sempre(self) -> None:
        store = self.negozio()

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
            "runId": "run-prova",
        })

        self.assertIs(esito["ok"], True)
        self.assertEqual(self.offerte_disponibili(store.review()), {"cipresso": 1.28, "noce": 1.15})

    def test_una_scheda_che_non_dichiara_la_run_abbina_come_sempre(self) -> None:
        """A cached page without a `runId` field must not break matching."""

        store = self.negozio()

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["ok"], True)
        self.assertEqual(self.offerte_disponibili(store.review()), {"cipresso": 1.28, "noce": 1.15})


if __name__ == "__main__":
    unittest.main()
