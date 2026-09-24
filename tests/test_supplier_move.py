"""Tests for the bulk product move preview (moving products from one supplier to another).

The preview is the only place where the user sees the cost of changing
supplier before deciding; if these numbers don't match what the final order
compilation produces, the user decides based on a wrong estimate. These
tests protect that match.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = SKILL_ROOT / "app" / "server.py"

SERVER_SPEC = importlib.util.spec_from_file_location("compara_ordini_move_server", SERVER_PATH)
if SERVER_SPEC is None or SERVER_SPEC.loader is None:
    raise RuntimeError(f"Impossibile importare {SERVER_PATH}")
SERVER = importlib.util.module_from_spec(SERVER_SPEC)
SERVER_SPEC.loader.exec_module(SERVER)

ReviewStore = SERVER.ReviewStore
SnapshotError = SERVER.SnapshotError

RUN_ID = "run-spostamento"


def offer(
    supplier: str,
    *,
    unit_price: float,
    factor: float,
    order_price: float,
    available: bool = True,
    requires_confirmation: bool = False,
    units_per_order_unit: float | None = None,
) -> dict[str, Any]:
    """Build a supplier offer for a product, shaped like the pipeline's output."""

    built = {
        "supplierId": supplier,
        "available": available,
        "description": f"OFFERTA {supplier.upper()}",
        "sourceRow": 100,
        "unitPriceNet": unit_price,
        "quantityFactor": factor,
        "orderUnitPriceNet": order_price,
        "method": "EAN",
        "confidence": "CERTA",
        "requiresConfirmation": requires_confirmation,
    }
    if units_per_order_unit is not None:
        built["unitsPerOrderUnit"] = units_per_order_unit
    return built


def move_review() -> dict[str, Any]:
    """Synthetic comparison built around the per-piece price trap.

    On PASTA the cheapest carton is cipresso's (7.20 EUR) but its per-piece
    price is the highest (1.20 EUR): comparing carton totals recommends
    cipresso, comparing per-piece price recommends betulla.
    """

    return {
        "run": {"id": RUN_ID, "status": "ready", "label": "Prova spostamento"},
        "files": [],
        "suppliers": [
            {"id": "larice", "name": "Larice", "minimumOrder": 100},
            {"id": "betulla", "name": "Betulla", "minimumOrder": 400},
            {"id": "cipresso", "name": "Cipresso", "minimumOrder": 30},
        ],
        "products": [
            {
                "id": "product:10",
                "kind": "PRODUCT",
                "itemType": "product",
                "ean": "8000000000010",
                "name": "PASTA",
                "quantity": 0,
                "selectedSupplierId": "larice",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    offer("larice", unit_price=1.0, factor=12, order_price=12.0),
                    offer("betulla", unit_price=0.9, factor=24, order_price=21.6),
                    offer("cipresso", unit_price=1.2, factor=6, order_price=7.2),
                ],
            },
            {
                "id": "product:11",
                "kind": "PRODUCT",
                "itemType": "product",
                "ean": "8000000000011",
                "name": "OLIO",
                "quantity": 0,
                "selectedSupplierId": "larice",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    offer("larice", unit_price=2.0, factor=6, order_price=12.0),
                    offer("betulla", unit_price=1.5, factor=6, order_price=9.0, requires_confirmation=True),
                ],
            },
            {
                "id": "product:12",
                "kind": "PRODUCT",
                "itemType": "product",
                "ean": "8000000000012",
                "name": "RISO",
                "quantity": 0,
                "selectedSupplierId": "larice",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    offer("larice", unit_price=3.0, factor=4, order_price=12.0),
                ],
            },
            {
                "id": "product:13",
                "kind": "PRODUCT",
                "itemType": "product",
                "ean": "8000000000013",
                "name": "SALE",
                "quantity": 0,
                "selectedSupplierId": "larice",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    offer("larice", unit_price=1.0, factor=10, order_price=10.0),
                    offer("betulla", unit_price=0.8, factor=10, order_price=8.0),
                    # Present in the price list but flagged not orderable: it's
                    # the cheapest of the three and must not be picked.
                    offer("cipresso", unit_price=0.5, factor=10, order_price=5.0, available=False),
                ],
            },
            {
                "id": "display:espositore",
                "kind": "DISPLAY",
                "itemType": "display",
                "ean": "",
                "name": "ESPOSITORE MISTO X 24",
                "quantity": 0,
                "selectedSupplierId": "larice",
                "confirmed": True,
                "requiresConfirmation": False,
                # A display is ordered as displays but delivered as pieces:
                # `factor` is the pieces it contains, `unitPriceNet` is the
                # per-piece price, `orderUnitPriceNet` is the whole display's
                # price. 100 / 24 = 4.1667 and 90 / 24 = 3.75.
                "offers": [
                    offer("larice", unit_price=4.1667, factor=24, order_price=100.0, units_per_order_unit=24),
                    offer("betulla", unit_price=3.75, factor=24, order_price=90.0, units_per_order_unit=24),
                ],
            },
            {
                "id": "product:20",
                "kind": "PRODUCT",
                "itemType": "product",
                "ean": "8000000000020",
                "name": "ZUCCHERO",
                "quantity": 0,
                "selectedSupplierId": "betulla",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    offer("betulla", unit_price=1.0, factor=10, order_price=10.0),
                    offer("larice", unit_price=1.1, factor=10, order_price=11.0),
                ],
            },
            {
                "id": "product:30",
                "kind": "PRODUCT",
                "itemType": "product",
                "ean": "8000000000030",
                "name": "FARINA NON ORDINATA",
                "quantity": 0,
                "selectedSupplierId": "larice",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    offer("larice", unit_price=1.0, factor=8, order_price=8.0),
                    offer("betulla", unit_price=0.5, factor=8, order_price=4.0),
                ],
            },
            {
                "id": "product:31",
                "kind": "PRODUCT",
                "itemType": "product",
                "ean": "8000000000031",
                "name": "CAFFE ESCLUSO",
                "quantity": 0,
                "selectedSupplierId": "larice",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    offer("larice", unit_price=5.0, factor=2, order_price=10.0),
                    offer("betulla", unit_price=4.0, factor=2, order_price=8.0),
                ],
            },
        ],
        "warnings": [],
    }


# Quantities the user decided: cartons (displays for the display row), and
# never changed by a supplier move.
ORDERED = {
    "product:10": 5,
    "product:11": 3,
    "product:12": 2,
    "product:13": 4,
    "display:espositore": 2,
    "product:20": 6,
    "product:30": 0,
    "product:31": 0,
}

# Order-unit price per supplier, copied by hand from the comparison: an
# independent reference that stays correct even if the server's own price
# reading changes.
ORDER_UNIT_PRICE = {
    "product:10": {"larice": 12.0, "betulla": 21.6, "cipresso": 7.2},
    "product:11": {"larice": 12.0, "betulla": 9.0, "cipresso": 0.6},
    "product:12": {"larice": 12.0},
    "product:13": {"larice": 10.0, "betulla": 8.0},
    "display:espositore": {"larice": 100.0, "betulla": 90.0},
    "product:20": {"betulla": 10.0, "larice": 11.0},
    "product:30": {"larice": 8.0, "betulla": 4.0},
    "product:31": {"larice": 10.0, "betulla": 8.0},
}

ESCLUSI = {"product:31"}


class SilentHandler(SERVER.AppHandler):
    def log_message(self, format_string: str, *args: object) -> None:  # noqa: D102
        return


class MoveTestCase(unittest.TestCase):
    """Shared fixture: synthetic comparison, isolated store, independent totals."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.review_path = self.root / "review_data.json"
        # State lives under a "run" subfolder: order history is derived from
        # state_path.parent.parent, so without this level it would land
        # outside the temp directory.
        self.run_dir = self.root / "run-corrente"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "review_state.json"
        self.upload_dir = self.root / "uploads"
        self.output_dir = self.root / "outputs"
        self.review = move_review()
        self.write_review(self.review)
        self.store = self.make_store()

    def write_review(self, review: dict[str, Any]) -> None:
        self.review = review
        self.review_path.write_text(json.dumps(review), encoding="utf-8")

    def make_store(self) -> Any:
        return ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

    def snapshot(
        self,
        *,
        quantities: dict[str, int] | None = None,
        run_id: str = RUN_ID,
    ) -> dict[str, Any]:
        """Build the same snapshot payload the browser sends to PUT /api/state."""

        quantities = ORDERED if quantities is None else quantities
        products = []
        for product in self.review["products"]:
            product_id = str(product["id"])
            if product_id not in quantities:
                continue
            products.append({
                "id": product_id,
                "quantity": quantities[product_id],
                "selectedSupplierId": product["selectedSupplierId"],
                "confirmed": True,
                "excluded": product_id in ESCLUSI,
                "quantitySource": "gestionale",
            })
        return {
            "runId": run_id,
            "currentStep": 2,
            "acceptBelowThreshold": False,
            "summaryGrouping": "supplier",
            "products": products,
        }

    def payload(self, from_supplier: str = "larice", **kwargs: Any) -> dict[str, Any]:
        request = self.snapshot(**kwargs)
        request["from"] = from_supplier
        return request

    def preview(self, from_supplier: str = "larice", **kwargs: Any) -> dict[str, Any]:
        return self.store.move_preview(self.payload(from_supplier, **kwargs))

    @staticmethod
    def option(result: dict[str, Any], option_id: str) -> dict[str, Any]:
        return next(item for item in result["options"] if item["id"] == option_id)

    @staticmethod
    def assignments_by_product(option: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(item["productId"]): item for item in option["assignments"]}

    def order_total(self, moves: dict[str, str], quantities: dict[str, int] | None = None) -> float:
        """Recompute the whole order's net total row by row, from the price data.

        Uses no server function; this is the independent computation the
        preview result is checked against.
        """

        quantities = ORDERED if quantities is None else quantities
        total = 0.0
        selected = {str(item["id"]): str(item["selectedSupplierId"]) for item in self.review["products"]}
        for product_id, quantity in quantities.items():
            if quantity <= 0 or product_id in ESCLUSI:
                continue
            supplier = moves.get(product_id) or selected[product_id]
            total += quantity * ORDER_UNIT_PRICE[product_id][supplier]
        return round(total, 2)

    def tree_fingerprint(self) -> dict[str, tuple[int, str]]:
        """Fingerprint every file under the working directory by mtime and content hash."""

        fingerprint = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                fingerprint[path.relative_to(self.root).as_posix()] = (
                    path.stat().st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
        return fingerprint


class SupplierMoveTests(MoveTestCase):
    # -- 1. The preview total is the whole order's total. --

    def test_delta_net_coincide_con_la_somma_dei_totali_di_riga(self) -> None:
        result = self.preview()

        # larice 60 + 36 + 24 + 40 + 200, betulla 60.
        self.assertEqual(result["currentNetTotal"], 420.0)
        self.assertEqual(result["currentNetTotal"], self.order_total({}))

        for option in result["options"]:
            moves = {
                str(item["productId"]): str(item["toSupplierId"])
                for item in option["assignments"]
            }
            atteso = round(self.order_total(moves) - self.order_total({}), 2)
            self.assertEqual(
                option["deltaNet"],
                atteso,
                f"L'opzione {option['id']} non dichiara la differenza sull'ordine intero",
            )
            # The same delta must show up in the per-row totals shown to the
            # user: rows that don't move must not change by a cent.
            somma_righe = round(
                sum(item["newLineNet"] for item in option["assignments"])
                - sum(item["previousLineNet"] for item in option["assignments"]),
                2,
            )
            self.assertEqual(option["deltaNet"], somma_righe)

        # Moving everything to betulla costs 11 euros more: must show up even
        # when the number is positive.
        self.assertEqual(self.option(result, "betulla")["deltaNet"], 11.0)
        self.assertEqual(self.option(result, "cipresso")["deltaNet"], -24.0)

    # -- 2. A product with no offer at the destination stays where it is. --

    def test_prodotto_senza_offerta_alla_destinazione_resta_dovera(self) -> None:
        opzione = self.option(self.preview(), "betulla")

        self.assertNotIn("product:12", self.assignments_by_product(opzione))
        rimasti = {str(item["productId"]): item for item in opzione["leftBehind"]}
        self.assertIn("product:12", rimasti)
        self.assertEqual(rimasti["product:12"]["reason"], "NESSUNA_OFFERTA")
        self.assertEqual(rimasti["product:12"]["productName"], "RISO")

        # Not zeroed out: its 2 cartons keep counting toward larice's total
        # even after everything else moves.
        larice = next(item for item in opzione["supplierTotalsAfter"] if item["supplierId"] == "larice")
        self.assertEqual(larice["netTotalAfter"], 24.0)
        self.assertEqual(opzione["movedCount"], 4)
        self.assertEqual(opzione["movableCount"], 5)

    def test_offerta_dichiarata_non_disponibile_non_e_una_destinazione(self) -> None:
        # SALE has the lowest absolute price on cipresso, but that row is
        # flagged not orderable: moving it there would be an impossible order.
        opzione = self.option(self.preview(), "cipresso")

        self.assertEqual(list(self.assignments_by_product(opzione)), ["product:10"])
        rimasti = {str(item["productId"]): str(item["reason"]) for item in opzione["leftBehind"]}
        self.assertEqual(rimasti["product:13"], "NESSUNA_OFFERTA")

    # -- 3. The per-piece price trap. --

    def test_best_sceglie_sul_prezzo_al_pezzo_non_sul_prezzo_del_collo(self) -> None:
        opzione = self.option(self.preview(), "best")
        assegnazione = self.assignments_by_product(opzione)["product:10"]

        # Cipresso's carton is 7.20 against betulla's 21.60: comparing carton
        # totals picks cipresso. But cipresso's carton holds 6 pieces at 1.20
        # each, while betulla's holds 24 at 0.90 each.
        self.assertEqual(assegnazione["toSupplierId"], "betulla")
        self.assertEqual(assegnazione["previousFactor"], 12)
        self.assertEqual(assegnazione["newFactor"], 24)
        self.assertTrue(assegnazione["factorChanged"])
        self.assertEqual(opzione["kind"], "best")
        self.assertEqual(opzione["label"], "Migliore alternativa per ciascun prodotto")

    def test_best_puo_mandare_prodotti_diversi_su_fornitori_diversi(self) -> None:
        # When a product is better off elsewhere, "best" must not flatten
        # everything onto the same supplier.
        review = move_review()
        olio = next(item for item in review["products"] if item["id"] == "product:11")
        olio["offers"].append(offer("cipresso", unit_price=0.1, factor=6, order_price=0.6))
        self.write_review(review)
        self.store = self.make_store()

        assegnazioni = self.assignments_by_product(self.option(self.preview(), "best"))

        self.assertEqual(assegnazioni["product:10"]["toSupplierId"], "betulla")
        self.assertEqual(assegnazioni["product:11"]["toSupplierId"], "cipresso")

    # -- 4. The preview writes nothing to disk. --

    def test_il_preventivo_non_scrive_niente_su_disco(self) -> None:
        self.assertFalse(self.state_path.exists())
        prima = self.tree_fingerprint()

        result = self.preview()

        self.assertTrue(result["ok"])
        self.assertFalse(self.state_path.exists(), "Il preventivo ha creato lo stato salvato")
        self.assertFalse((self.output_dir / "final_order_plan.json").exists())
        self.assertEqual(self.tree_fingerprint(), prima, "Il preventivo ha toccato dei file")

    def test_il_preventivo_non_modifica_uno_stato_gia_salvato(self) -> None:
        self.store.save_state(self.snapshot())
        salvato = json.loads(self.state_path.read_text(encoding="utf-8"))
        # A marker no server write would ever reproduce. Needed because
        # comparing mtime and content isn't enough: two saves close in time
        # can produce an identical file, and a silent rewrite would pass
        # unnoticed.
        salvato["marcatoreDelTest"] = "questo file non va riscritto"
        self.state_path.write_text(json.dumps(salvato), encoding="utf-8")
        prima = self.tree_fingerprint()
        self.assertIn("run-corrente/review_state.json", prima)

        self.preview()

        rimasto = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(rimasto.get("marcatoreDelTest"), "questo file non va riscritto")
        self.assertEqual(self.tree_fingerprint(), prima, "Il preventivo ha riscritto lo stato salvato")

    # -- 5. Per-supplier totals and thresholds. --

    def test_totali_per_fornitore_e_superamento_soglia(self) -> None:
        opzione = self.option(self.preview(), "betulla")
        righe = {str(item["supplierId"]): item for item in opzione["supplierTotalsAfter"]}

        self.assertEqual(set(righe), {"larice", "betulla"})
        self.assertEqual(righe["larice"]["supplierName"], "LARICE")
        self.assertEqual(righe["larice"]["netTotalBefore"], 360.0)
        self.assertEqual(righe["larice"]["netTotalAfter"], 24.0)
        self.assertEqual(righe["larice"]["threshold"], 100.0)
        # Emptying larice loses a threshold it already met: the single most
        # important warning on this screen.
        self.assertTrue(righe["larice"]["meetsThresholdBefore"])
        self.assertFalse(righe["larice"]["meetsThresholdAfter"])

        self.assertEqual(righe["betulla"]["netTotalBefore"], 60.0)
        self.assertEqual(righe["betulla"]["netTotalAfter"], 407.0)
        self.assertEqual(righe["betulla"]["threshold"], 400.0)
        self.assertFalse(righe["betulla"]["meetsThresholdBefore"])
        self.assertTrue(righe["betulla"]["meetsThresholdAfter"])

        # The per-supplier totals after the move must add up to the whole order.
        dopo = self.order_total({
            "product:10": "betulla",
            "product:11": "betulla",
            "product:13": "betulla",
            "display:espositore": "betulla",
        })
        self.assertEqual(dopo, 431.0)
        self.assertEqual(round(righe["larice"]["netTotalAfter"] + righe["betulla"]["netTotalAfter"], 2), dopo)

    def test_un_fornitore_senza_ordine_non_ha_soglia_da_raggiungere(self) -> None:
        # cipresso doesn't appear in the starting order: before the move it's
        # not "below threshold", it simply has no order.
        opzione = self.option(self.preview(), "cipresso")
        righe = {str(item["supplierId"]): item for item in opzione["supplierTotalsAfter"]}

        self.assertEqual(righe["cipresso"]["netTotalBefore"], 0.0)
        self.assertTrue(righe["cipresso"]["meetsThresholdBefore"])
        self.assertEqual(righe["cipresso"]["netTotalAfter"], 36.0)
        self.assertEqual(righe["cipresso"]["threshold"], 30.0)
        self.assertTrue(righe["cipresso"]["meetsThresholdAfter"])

    # -- The rest of the contract. --

    def test_il_numero_di_colli_e_di_espositori_non_cambia_mai(self) -> None:
        assegnazioni = self.assignments_by_product(self.option(self.preview(), "betulla"))

        # The display stays 2 displays: 2 x 100 becomes 2 x 90.
        espositore = assegnazioni["display:espositore"]
        self.assertEqual(espositore["previousLineNet"], 200.0)
        self.assertEqual(espositore["newLineNet"], 180.0)
        # The order unit is the display, but the delivered goods are its
        # pieces: the factor is 24 from both suppliers, so neither the carton
        # count nor the piece count changes.
        self.assertEqual(espositore["previousFactor"], 24)
        self.assertEqual(espositore["newFactor"], 24)
        self.assertFalse(espositore["factorChanged"])
        self.assertEqual(espositore["previousPieces"], 48)
        self.assertEqual(espositore["newPieces"], 48)

        # SALE: same carton count, same factor, only the price changes.
        sale = assegnazioni["product:13"]
        self.assertEqual(sale["previousLineNet"], 40.0)
        self.assertEqual(sale["newLineNet"], 32.0)
        self.assertFalse(sale["factorChanged"])

    def test_la_destinazione_che_richiede_conferma_viene_segnalata(self) -> None:
        assegnazioni = self.assignments_by_product(self.option(self.preview(), "betulla"))

        self.assertTrue(assegnazioni["product:11"]["needsConfirmation"])
        self.assertFalse(assegnazioni["product:13"]["needsConfirmation"])

    def test_prodotti_a_zero_o_esclusi_non_sono_spostabili(self) -> None:
        result = self.preview()
        coinvolti = set()
        for option in result["options"]:
            coinvolti.update(self.assignments_by_product(option))
            coinvolti.update(str(item["productId"]) for item in option["leftBehind"])

        self.assertEqual(result["movableCount"], 5)
        self.assertNotIn("product:30", coinvolti)
        self.assertNotIn("product:31", coinvolti)

    def test_i_prodotti_aggiunti_a_mano_si_spostano_come_gli_altri(self) -> None:
        manuale = {
            "id": "product:99",
            "kind": "PRODUCT",
            "itemType": "product",
            "ean": "8000000000099",
            "name": "PRODOTTO AGGIUNTO A MANO",
            "quantity": 0,
            "selectedSupplierId": "larice",
            "confirmed": True,
            "requiresConfirmation": False,
            "addedManually": True,
            "offers": [
                offer("larice", unit_price=5.0, factor=4, order_price=20.0),
                offer("betulla", unit_price=3.0, factor=5, order_price=15.0),
            ],
        }
        self.state_path.write_text(
            json.dumps({"schemaVersion": 1, "runId": RUN_ID, "products": [], "manualProducts": [manuale]}),
            encoding="utf-8",
        )
        self.review["products"].append(manuale)
        request = self.payload()
        request["products"].append({
            "id": "product:99",
            "quantity": 2,
            "selectedSupplierId": "larice",
            "confirmed": True,
            "excluded": False,
            "quantitySource": "utente",
        })

        result = self.store.move_preview(request)
        assegnazione = self.assignments_by_product(self.option(result, "betulla"))["product:99"]

        self.assertEqual(result["movableCount"], 6)
        self.assertEqual(assegnazione["previousLineNet"], 40.0)
        self.assertEqual(assegnazione["newLineNet"], 30.0)
        self.assertEqual(assegnazione["previousFactor"], 4)
        self.assertEqual(assegnazione["newFactor"], 5)
        self.assertTrue(assegnazione["factorChanged"])

    def test_fornitore_di_partenza_senza_prodotti_non_e_un_errore(self) -> None:
        result = self.preview(from_supplier="cipresso")

        self.assertTrue(result["ok"])
        self.assertEqual(result["from"], "cipresso")
        self.assertEqual(result["fromName"], "CIPRESSO")
        self.assertEqual(result["movableCount"], 0)
        self.assertEqual(result["options"], [])
        self.assertEqual(result["currentNetTotal"], 420.0)

    def test_fornitore_di_partenza_inesistente_non_e_un_errore(self) -> None:
        result = self.preview(from_supplier="fornitore-che-non-esiste")

        self.assertTrue(result["ok"])
        self.assertEqual(result["movableCount"], 0)
        self.assertEqual(result["options"], [])

    def test_opzione_che_non_sposta_niente_non_viene_proposta(self) -> None:
        # With only RISO ordered, there's no valid destination: no options to
        # show, and the product stays ordered from larice.
        result = self.preview(quantities={"product:12": 2})

        self.assertEqual(result["movableCount"], 1)
        self.assertEqual(result["options"], [])
        self.assertEqual(result["currentNetTotal"], 24.0)

    def test_le_opzioni_si_ordinano_sul_prezzo_al_pezzo_non_sul_totale(self) -> None:
        """Options can't be ranked by total spend alone.

        Orders that deliver different quantities of goods aren't comparable
        by total: it's the same trap as comparing offers, applied to the
        list of destinations. CIPRESSO spends less overall but delivers less
        goods and costs more per piece, so it must not rank first.
        """

        result = self.preview()

        self.assertEqual([item["id"] for item in result["options"]], ["best", "betulla", "cipresso"])
        self.assertEqual(self.option(result, "betulla")["label"], "BETULLA")
        self.assertEqual(self.option(result, "betulla")["kind"], "supplier")

        cipresso = self.option(result, "cipresso")
        betulla = self.option(result, "betulla")
        # Proof the ordering isn't arbitrary: cipresso is cheaper by total
        # and more expensive per piece. Ranking by total again would put
        # cipresso back in front.
        self.assertLess(cipresso["deltaNet"], betulla["deltaNet"])
        self.assertGreater(cipresso["deltaCostPerPiece"], betulla["deltaCostPerPiece"])

    def test_ogni_opzione_dichiara_la_merce_consegnata_oltre_alla_spesa(self) -> None:
        """The spend delta alone is misleading; the goods delta must ship with it.

        At the same carton count, delivered pieces differ by supplier.
        Without `deltaPieces`, the UI could label an option as savings when
        it actually delivers less goods.
        """

        result = self.preview()
        cipresso = self.option(result, "cipresso")

        self.assertLess(cipresso["deltaNet"], 0)
        self.assertLess(cipresso["deltaPieces"], 0)
        self.assertEqual(
            cipresso["deliveredPiecesAfter"] - cipresso["deliveredPiecesBefore"],
            cipresso["deltaPieces"],
        )
        # The declared per-piece price must be consistent with the declared spend.
        moved = cipresso["assignments"]
        speso_dopo = round(sum(item["newLineNet"] for item in moved), 2)
        pezzi_dopo = sum(item["newPieces"] for item in moved)
        self.assertAlmostEqual(cipresso["costPerPieceAfter"], speso_dopo / pezzi_dopo, places=2)

    def test_un_fornitore_senza_ordine_dichiara_di_non_averne_avuto_uno(self) -> None:
        """Without `hadOrderBefore` the page would state the opposite of the truth.

        `meets_threshold` follows the order-compilation rule that a supplier
        with no order at all isn't "below threshold", so it also returns
        true for a supplier at zero. Read alone, that true reads as "it used
        to meet the threshold", and the UI would then claim a supplier that
        started at zero dropped below a threshold it never had.
        """

        result = self.preview()
        # Looking at the CIPRESSO option, where cipresso is the destination:
        # it had no ordered product before the move.
        righe = {str(row["supplierId"]): row for row in self.option(result, "cipresso")["supplierTotalsAfter"]}
        cipresso = righe["cipresso"]

        self.assertEqual(cipresso["netTotalBefore"], 0.0)
        self.assertTrue(cipresso["meetsThresholdBefore"], "regola della compilazione: chi non ordina non è sotto soglia")
        self.assertFalse(cipresso["hadOrderBefore"], "cipresso non aveva nessun ordine da cui scendere")

        larice = righe["larice"]
        self.assertGreater(larice["netTotalBefore"], 0.0)
        self.assertTrue(larice["hadOrderBefore"])

    def test_una_conferma_mancante_altrove_non_impedisce_il_preventivo(self) -> None:
        """A preview writes nothing, so it must not be rejected for a missing confirmation elsewhere.

        A missing confirmation on a product belonging to a DIFFERENT
        supplier must not reject the whole preview with a technical message
        and no product name, in a state the application itself allows the
        user to reach.
        """

        request = self.payload()
        for decision in request["products"]:
            if decision["id"] == "product:11":
                # This product's betulla offer requires confirmation; here it
                # isn't given, and the product doesn't belong to larice.
                decision["selectedSupplierId"] = "betulla"
                decision["confirmed"] = False

        result = self.store.move_preview(request)

        self.assertTrue(result["ok"])
        self.assertTrue(result["options"], "il preventivo deve essere calcolato lo stesso")

    def test_snapshot_di_un_altra_run_viene_rifiutato(self) -> None:
        with self.assertRaises(ValueError) as raised:
            self.preview(run_id="run-di-un-altra-settimana")

        self.assertIn("run", str(raised.exception).casefold())

    def test_snapshot_non_valido_viene_rifiutato_come_nel_salvataggio(self) -> None:
        request = self.payload()
        request["products"].append({
            "id": "product:non-esiste",
            "quantity": 1,
            "selectedSupplierId": "larice",
            "confirmed": True,
        })

        with self.assertRaises(SnapshotError) as raised:
            self.store.move_preview(request)

        self.assertEqual({str(item["code"]) for item in raised.exception.errors}, {"PRODOTTO_SCONOSCIUTO"})

    def test_fornitore_di_partenza_obbligatorio(self) -> None:
        with self.assertRaises(ValueError):
            self.store.move_preview(self.snapshot())


class SupplierMoveHttpTests(MoveTestCase):
    """Same contract as `SupplierMoveTests`, exercised through the real HTTP route, plus status codes."""

    def setUp(self) -> None:
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), SilentHandler)
        self.httpd.store = self.store
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop_server(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def call(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        prepared = urllib.request.Request(
            self.base_url + "/api/suppliers/move-preview",
            data=json.dumps(request).encode("utf-8"),
            method="POST",
        )
        prepared.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(prepared, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_la_rotta_risponde_con_il_preventivo_e_non_salva_niente(self) -> None:
        prima = self.tree_fingerprint()

        status, body = self.call(self.payload())

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["fromName"], "LARICE")
        self.assertEqual(body["currentNetTotal"], 420.0)
        self.assertEqual([item["id"] for item in body["options"]], ["best", "betulla", "cipresso"])
        self.assertFalse(self.state_path.exists(), "La rotta ha salvato lo stato")
        self.assertEqual(self.tree_fingerprint(), prima)

    def test_la_rotta_rifiuta_la_run_sbagliata_senza_esplodere(self) -> None:
        status, body = self.call(self.payload(run_id="run-di-un-altra-settimana"))

        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_la_rotta_segnala_lo_snapshot_non_valido_con_i_codici(self) -> None:
        request = self.payload()
        request["products"].append({"id": "product:non-esiste", "quantity": 1, "selectedSupplierId": "larice"})

        status, body = self.call(request)

        self.assertEqual(status, 422)
        self.assertEqual({str(item["code"]) for item in body["errors"]}, {"PRODOTTO_SCONOSCIUTO"})


def espositori_review() -> dict[str, Any]:
    """Two display offers for the same product with a different piece count.

    The same per-piece-price trap as the carton case, but larger: BETULLA's
    display costs less to buy (50 against 60) and more to consume (1.04 per
    piece against 0.4167), because it holds 48 pieces instead of 144.
    Comparing whole displays recommends BETULLA; comparing the goods
    recommends LARICE, which is what the preview page must show.
    """

    return {
        "run": {"id": RUN_ID, "status": "ready", "label": "Prova espositori"},
        "files": [],
        "suppliers": [
            {"id": "larice", "name": "Larice", "minimumOrder": 0},
            {"id": "betulla", "name": "Betulla", "minimumOrder": 0},
            {"id": "cipresso", "name": "Cipresso", "minimumOrder": 0},
        ],
        "products": [
            {
                "id": "display:caramelle",
                "kind": "DISPLAY",
                "itemType": "display",
                "ean": "",
                "name": "ESPOSITORE CARAMELLE",
                "quantity": 0,
                "selectedSupplierId": "cipresso",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    offer("cipresso", unit_price=1.5, factor=40, order_price=60.0),
                    # 60 / 144 = 0.4167 per piece: the most expensive display
                    # of the three, and the cheapest goods.
                    offer("larice", unit_price=0.4167, factor=144, order_price=60.0),
                    # 50 / 48 = 1.0417 per piece: the cheapest display, and
                    # the most expensive goods.
                    offer("betulla", unit_price=1.0417, factor=48, order_price=50.0),
                ],
            },
        ],
        "warnings": [],
    }


class EspositoriPezziEPrezzoTests(MoveTestCase):
    """"Best alternative" must rank by per-piece price for display offers too.

    `display_offer` must not write `quantityFactor: 1` with the whole
    display's price in `unitPriceNet`: that would rank offers by the display
    price, a number that isn't comparable across displays of different
    sizes, and could recommend the opposite of what the comparison page
    itself recommends.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write_review(espositori_review())
        self.store = self.make_store()

    def snapshot(self, *, quantities: dict[str, int] | None = None, run_id: str = RUN_ID) -> dict[str, Any]:
        return {
            "runId": run_id,
            "currentStep": 2,
            "acceptBelowThreshold": False,
            "summaryGrouping": "supplier",
            "products": [{
                "id": "display:caramelle",
                "quantity": 3,
                "selectedSupplierId": "cipresso",
                "confirmed": True,
                "excluded": False,
                "quantitySource": "gestionale",
            }],
        }

    def test_la_migliore_alternativa_e_l_espositore_col_pezzo_piu_economico(self) -> None:
        assegnazione = self.assignments_by_product(
            self.option(self.preview("cipresso"), "best")
        )["display:caramelle"]

        self.assertEqual(assegnazione["toSupplierId"], "larice")
        self.assertEqual(assegnazione["newFactor"], 144)

    def test_l_opzione_col_totale_piu_basso_non_e_quella_consigliata(self) -> None:
        """Regression guard: catches a ranking that falls back to total price.

        BETULLA spends less (150 against 180) and delivers less goods; if
        the ranking went back to the whole display's price, this test would
        fail on its own.
        """

        risultato = self.preview("cipresso")
        betulla = self.option(risultato, "betulla")
        larice = self.option(risultato, "larice")

        self.assertLess(betulla["deltaNet"], larice["deltaNet"])
        self.assertGreater(betulla["deltaCostPerPiece"], larice["deltaCostPerPiece"])
        self.assertEqual([item["id"] for item in risultato["options"]][0], "best")

    def test_i_pezzi_dichiarati_sono_pezzi_e_non_espositori(self) -> None:
        opzione = self.option(self.preview("cipresso"), "larice")

        # 3 displays stay 3 displays; the goods go from 3x40 to 3x144.
        self.assertEqual(opzione["deliveredPiecesBefore"], 120)
        self.assertEqual(opzione["deliveredPiecesAfter"], 432)
        self.assertEqual(opzione["deltaPieces"], 312)
        self.assertEqual(opzione["assignments"][0]["previousPieces"], 120)
        self.assertEqual(opzione["assignments"][0]["newPieces"], 432)

    def test_il_totale_resta_quello_dell_espositore_intero(self) -> None:
        """Piece count changes the comparison, not the invoice: what's bought is the display."""

        opzione = self.option(self.preview("cipresso"), "larice")

        self.assertEqual(opzione["assignments"][0]["previousLineNet"], 180.0)
        self.assertEqual(opzione["assignments"][0]["newLineNet"], 180.0)
        self.assertEqual(opzione["deltaNet"], 0.0)


if __name__ == "__main__":
    unittest.main()
