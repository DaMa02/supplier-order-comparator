"""Storico degli ordini: merce ordinata e non ancora ricevuta.

Il problema reale: il fornitore non consegna, la merce non entra nel gestionale e
la settimana dopo lo stesso articolo viene riordinato senza accorgersene.

Il test piu' importante di questo file e' quello sull'abbinamento per EAN: gli
identificativi "product:<riga>" dipendono dalla posizione nell'esportazione
settimanale, quindi product:198 di questa settimana non e' lo stesso articolo di
product:198 della settimana scorsa.  Abbinare per identificativo produrrebbe
avvisi sbagliati in silenzio, cioe' esattamente il difetto che questa funzione
dovrebbe evitare.  Fanno eccezione gli identificativi che nascono dal contenuto
e non dalla posizione — gli espositori, `display:composition:...` — perche' li'
l'identificativo e' l'unica identita' disponibile: un EAN non ce l'hanno.

⚠ Una compilazione entra nello storico SOLO quando le copie dei listini sono
sul disco: per questo le prove qui sotto compilano con `compila()`, che mette
uno scrittore finto al posto del writer.  Un `store.compile()` nudo, senza
copie, non registra piu' niente — ed e' il difetto D1 che questo file fissa.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = SKILL_ROOT / "app" / "server.py"

SERVER_SPEC = importlib.util.spec_from_file_location("compara_ordini_storico_server", SERVER_PATH)
if SERVER_SPEC is None or SERVER_SPEC.loader is None:
    raise RuntimeError(f"Impossibile importare {SERVER_PATH}")
SERVER = importlib.util.module_from_spec(SERVER_SPEC)
SERVER_SPEC.loader.exec_module(SERVER)

ReviewStore = SERVER.ReviewStore
order_history = SERVER.order_history

# Due articoli realmente diversi: l'olio e' quello ordinato la settimana scorsa,
# il tonno e' l'articolo che questa settimana occupa la riga 198.
EAN_OLIO = "8009580477747"
EAN_TONNO = "8004567890123"
EAN_RISO = "8001234567890"

# answeredAt: senza di esso un "non ancora arrivata" non sopravvive a un F5 e la
# domanda ricompare. askAgainAt: quando la domanda torna, deciso dal servizio.
# unit: l'unita' con cui si era ordinato (colli o espositori), che puo' differire
# da quella dello stesso codice nella settimana corrente.
PENDING_SUMMARY_KEYS = {
    "orderId", "supplier", "supplierName", "createdAt", "answeredAt", "askAgainAt", "lineCount", "totalNet",
}
PENDING_PRODUCT_KEYS = {"orderId", "supplier", "supplierName", "orderedAt", "quantity", "unit"}
# L'identificativo dell'espositore nasce dalla composizione, non dalla riga:
# e' l'unico caso in cui confrontare l'identificativo fra due settimane e' lecito.
ID_ESPOSITORE = "display:composition:8000000000001:6|8000000000002:6"


def supplier_offer(
    supplier_id: str,
    ean: str,
    description: str,
    *,
    source_row: int,
    order_price: float,
    factor: int = 6,
) -> dict[str, object]:
    return {
        "supplierId": supplier_id,
        "available": True,
        "description": description,
        "sourceRow": source_row,
        "ean": ean,
        "unitPriceNet": round(order_price / factor, 6),
        "quantityFactor": factor,
        "orderUnitPriceNet": order_price,
        "method": "EAN",
        "confidence": "CERTA",
    }


def gestionale_product(
    product_id: str,
    ean: str,
    name: str,
    *,
    source_row: int,
    offers: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "id": product_id,
        "kind": "PRODUCT",
        "itemType": "product",
        "sourceRow": source_row,
        "ean": ean,
        "name": name,
        "description": name,
        "quantity": 0,
        "selectedSupplierId": "larice",
        "confirmed": True,
        "requiresConfirmation": False,
        "offers": offers,
        "components": [],
    }


def espositore(product_id: str) -> dict[str, object]:
    """Un espositore come lo costruisce il confronto: senza EAN, mai."""

    return {
        "id": product_id,
        "kind": "DISPLAY",
        "itemType": "display",
        "sourceRow": 500,
        "ean": "",
        "name": "SOLBAO ESPO TOP PERFORMER X 96",
        "description": "SOLBAO ESPO TOP PERFORMER X 96",
        "quantity": 0,
        "selectedSupplierId": "larice",
        "confirmed": True,
        "requiresConfirmation": False,
        "offers": [{
            "supplierId": "larice",
            "available": True,
            "description": "SOLBAO ESPO TOP PERFORMER X 96",
            "sourceRow": 500,
            "ean": "",
            "unitPriceNet": 4.6054,
            "quantityFactor": 96,
            "orderUnitPriceNet": 442.1196,
            "method": "COMPOSIZIONE",
            "confidence": "ALTA",
        }],
        "components": [],
    }


def review_document(products: list[dict[str, object]], *, run_id: str) -> dict[str, object]:
    return {
        "run": {"id": run_id, "status": "ready", "label": f"Confronto {run_id}"},
        "files": [],
        "suppliers": [
            {"id": "larice", "name": "Larice", "minimumOrder": 0},
            {"id": "betulla", "name": "Betulla", "minimumOrder": 0},
        ],
        "products": products,
        "warnings": [],
    }


def week_one_review() -> dict[str, object]:
    """Settimana scorsa: l'olio sta sulla riga 198 dell'esportazione."""

    return review_document(
        [
            gestionale_product(
                "product:198",
                EAN_OLIO,
                "OLIO EXTRAVERGINE 1L",
                source_row=198,
                offers=[supplier_offer("larice", EAN_OLIO, "OLIO EXTRAVERGINE 1L", source_row=198, order_price=14.9)],
            ),
        ],
        run_id="run-2026-31",
    )


def week_two_review() -> dict[str, object]:
    """Questa settimana: l'olio è sceso alla riga 42, la 198 è un altro articolo."""

    return review_document(
        [
            gestionale_product(
                "product:42",
                EAN_OLIO,
                "OLIO EXTRAVERGINE 1L",
                source_row=42,
                offers=[supplier_offer("larice", EAN_OLIO, "OLIO EXTRAVERGINE 1L", source_row=42, order_price=14.9)],
            ),
            gestionale_product(
                "product:198",
                EAN_TONNO,
                "TONNO ALL'OLIO 3x80",
                source_row=198,
                offers=[supplier_offer("larice", EAN_TONNO, "TONNO ALL'OLIO 3x80", source_row=198, order_price=9.5)],
            ),
        ],
        run_id="run-2026-32",
    )


def history_entry(
    order_id: str,
    created_at: str,
    *,
    ean: str,
    quantity: int = 12,
    status: str = "in_attesa",
    supplier: str = "larice",
    supplier_name: str = "LARICE",
    answered_at: str | None = None,
) -> dict[str, object]:
    return {
        "orderId": order_id,
        "createdAt": created_at,
        "supplier": supplier,
        "supplierName": supplier_name,
        "runId": order_id.split(":")[0],
        "status": status,
        "answeredAt": answered_at,
        "totalNet": round(quantity * 14.9, 2),
        "lines": [
            {
                "ean": ean,
                "description": "OLIO EXTRAVERGINE 1L",
                "quantity": quantity,
                "orderUnitPriceNet": 14.9,
                "productId": "product:198",
            }
        ],
    }


class ScrittoreFinto:
    """Il writer sostituito da qualcosa che mette davvero le copie sul disco.

    Serve perche' dal 13 agosto 2026 lo storico registra soltanto le
    compilazioni riuscite davvero: senza copie non c'e' ordine.  Le copie sono
    `.xls` come quella di Noce — il confronto cella per cella di
    `scarta_copie_infedeli` guarda solo i `.xlsx` — perche' qui si prova che
    cosa entra nello storico, non la fedelta' della copia, che ha le sue prove.
    """

    def __init__(self, sorgente: Path, *, senza_copia: tuple[str, ...] = ()) -> None:
        self.sorgente = sorgente
        self.senza_copia = {str(item) for item in senza_copia}
        self.cartelle: list[Path] = []

    def __call__(
        self, plan_path: Path, destinazione: Path
    ) -> tuple[list[Path], list[tuple[str, Path, Path]], list[str]]:
        piano = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        fornitori = sorted({
            str(riga.get("supplier") or "")
            for riga in piano.get("orders") or []
            if isinstance(riga, dict) and riga.get("supplier")
        })
        self.cartelle.append(Path(destinazione))
        generati = [Path(plan_path)]
        prodotte: list[tuple[str, Path, Path]] = []
        for fornitore in fornitori:
            if fornitore in self.senza_copia:
                continue
            copia = Path(destinazione) / f"ORDINE_{fornitore.upper()}_{self.sorgente.stem}.xls"
            copia.write_bytes(b"copia del listino compilata")
            generati.append(copia)
            prodotte.append((fornitore, self.sorgente, copia))
        return generati, prodotte, []


class HistoryStoreTestCase(unittest.TestCase):
    """Cartelle separate per ogni run, un solo archivio storico condiviso."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.history_path = self.root / "history" / "orders.json"
        self.listino = self.root / "listino_fornitore.xls"
        self.listino.write_bytes(b"listino del fornitore")
        self.writer_config = self.root / "writer_config.json"
        self.writer_config.write_text(json.dumps({"run_id": "run-2026-31"}), encoding="utf-8")

    def make_store(self, review: dict[str, object], *, run_dir: str = "current", history_path: Path | None = None):
        directory = self.root / run_dir
        directory.mkdir(parents=True, exist_ok=True)
        review_path = directory / "review_data.json"
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        return ReviewStore(
            review_path,
            directory / "state.json",
            directory / "uploads",
            directory / "outputs",
            self.writer_config,
            self.history_path if history_path is None else history_path,
        )

    def compila(self, store, snapshot: dict[str, object], *, senza_copia: tuple[str, ...] = ()) -> dict[str, object]:
        """Una compilazione che arriva fino alle copie sul disco."""

        scrittore = ScrittoreFinto(self.listino, senza_copia=senza_copia)
        with mock.patch.object(store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(store, "run_writer", scrittore):
            return store.compile(snapshot)

    def order_id_of(self, supplier: str = "larice") -> str:
        """L'identificativo dell'unico ordine di quel fornitore nello storico."""

        voci = [item for item in self.read_history_file()["orders"] if item["supplier"] == supplier]
        self.assertEqual(len(voci), 1, voci)
        return str(voci[0]["orderId"])

    @staticmethod
    def snapshot(run_id: str, quantities: dict[str, tuple[str, int]]) -> dict[str, object]:
        return {
            "runId": run_id,
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [
                {"id": product_id, "quantity": quantity, "selectedSupplierId": supplier, "confirmed": True}
                for product_id, (supplier, quantity) in quantities.items()
            ],
        }

    def read_history_file(self) -> dict[str, object]:
        return json.loads(self.history_path.read_text(encoding="utf-8"))

    def write_history_file(self, orders: list[dict[str, object]]) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(
            json.dumps({"schema_version": 1, "orders": orders}, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def product_by_id(review: dict[str, object], product_id: str) -> dict[str, object]:
        return next(item for item in review["products"] if item["id"] == product_id)


class EanMatchingTests(HistoryStoreTestCase):
    """L'abbinamento si fa sull'EAN, mai sull'identificativo di riga."""

    def test_warning_follows_the_ean_when_the_row_number_moves(self) -> None:
        # Settimana scorsa: 12 colli di olio ordinati, l'olio era product:198.
        first_week = self.make_store(week_one_review(), run_dir="settimana-31")
        self.compila(first_week, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))

        stored = self.read_history_file()["orders"][0]
        # La trappola deve essere reale: l'ordine porta con se' product:198...
        self.assertEqual(stored["lines"][0]["productId"], "product:198")
        self.assertEqual(stored["lines"][0]["ean"], EAN_OLIO)

        # Questa settimana l'olio e' su product:42 e product:198 e' il tonno.
        second_week = self.make_store(week_two_review(), run_dir="settimana-32")
        review = second_week.review()
        olio = self.product_by_id(review, "product:42")
        tonno = self.product_by_id(review, "product:198")
        self.assertEqual(olio["ean"], EAN_OLIO)
        self.assertEqual(tonno["ean"], EAN_TONNO)

        # L'avviso deve seguire l'articolo (EAN), non il numero di riga.
        self.assertEqual(len(olio["pendingOrders"]), 1)
        self.assertEqual(tonno["pendingOrders"], [])
        pending = olio["pendingOrders"][0]
        self.assertEqual(set(pending), PENDING_PRODUCT_KEYS)
        self.assertEqual(pending["orderId"], stored["orderId"])
        self.assertEqual(pending["supplier"], "larice")
        self.assertEqual(pending["supplierName"], "LARICE")
        self.assertEqual(pending["orderedAt"], stored["createdAt"])
        # Unita' d'ordine: colli, non pezzi (12 colli da 6 pezzi non fanno 72).
        self.assertEqual(pending["quantity"], 12)

    def test_attach_pending_orders_ignores_the_product_identifier(self) -> None:
        # Stessa trappola senza server: finche' c'e' un EAN, il modulo non deve
        # mai guardare un identificativo di riga (product:<riga>).
        history = {
            "schema_version": 1,
            "orders": [history_entry("run-2026-31:larice", "2026-08-03T09:00:00+00:00", ean=EAN_OLIO)],
        }
        products = [
            {"id": "product:42", "ean": EAN_OLIO},
            {"id": "product:198", "ean": EAN_TONNO},
        ]

        order_history.attach_pending_orders(products, history)

        self.assertEqual(len(products[0]["pendingOrders"]), 1)
        self.assertEqual(products[1]["pendingOrders"], [])

    def test_matching_ignores_surrounding_spaces_in_the_ean(self) -> None:
        history = {
            "schema_version": 1,
            "orders": [history_entry("run-2026-31:larice", "2026-08-03T09:00:00+00:00", ean=f"  {EAN_OLIO} ")],
        }
        products = [{"id": "product:7", "ean": f"{EAN_OLIO}  "}]

        order_history.attach_pending_orders(products, history)

        self.assertEqual(len(products[0]["pendingOrders"]), 1)

    def test_different_ean_never_matches(self) -> None:
        history = {
            "schema_version": 1,
            "orders": [history_entry("run-2026-31:larice", "2026-08-03T09:00:00+00:00", ean=EAN_OLIO)],
        }
        products = [{"id": "product:7", "ean": EAN_RISO}]

        order_history.attach_pending_orders(products, history)

        self.assertEqual(products[0]["pendingOrders"], [])


class EmptyEanTests(HistoryStoreTestCase):
    """Un EAN vuoto non abbina mai nulla: meglio nessun avviso che uno falso."""

    def test_empty_ean_on_both_sides_does_not_match(self) -> None:
        history = {
            "schema_version": 1,
            "orders": [history_entry("run-2026-31:larice", "2026-08-03T09:00:00+00:00", ean="")],
        }
        products = [
            {"id": "display-senza-ean", "ean": ""},
            {"id": "product:9", "ean": "   "},
            {"id": "product:10"},
            {"id": "product:11", "ean": None},
            # Stesso identificativo salvato nella riga d'ordine: senza EAN non
            # deve comunque abbinare nulla.
            {"id": "product:198", "ean": ""},
        ]

        order_history.attach_pending_orders(products, history)

        for product in products:
            self.assertEqual(product["pendingOrders"], [], product["id"])

    def test_product_without_ean_is_never_matched_by_a_valid_order(self) -> None:
        history = {
            "schema_version": 1,
            "orders": [history_entry("run-2026-31:larice", "2026-08-03T09:00:00+00:00", ean=EAN_OLIO)],
        }
        products = [
            {"id": "display-senza-ean", "ean": ""},
            {"id": "product:10"},
            # L'articolo che occupa la riga 198 di questa settimana non ha EAN:
            # nessun avviso, anche se l'ordine ricorda proprio "product:198".
            {"id": "product:198", "ean": ""},
        ]

        order_history.attach_pending_orders(products, history)

        for product in products:
            self.assertEqual(product["pendingOrders"], [], product["id"])

    def test_display_without_ean_is_recorded_but_never_signalled(self) -> None:
        """Un espositore con un identificativo di comodo non abbina niente.

        `display-solbao` non e' un identificativo che nasce dalla composizione:
        senza EAN e senza identita' stabile l'avviso non si puo' dare a nessuno,
        e soprattutto non deve finire su un altro articolo.
        """

        review = week_one_review()
        review["products"].append(espositore("display-solbao"))
        store = self.make_store(review, run_dir="settimana-31")
        self.compila(store, self.snapshot("run-2026-31", {"display-solbao": ("larice", 2)}))

        lines = self.read_history_file()["orders"][0]["lines"]
        self.assertEqual([line["ean"] for line in lines], [""])

        again = self.make_store(week_one_review(), run_dir="settimana-32")
        review_again = again.review()
        # L'espositore senza EAN non deve far comparire avvisi su altri articoli.
        self.assertEqual(self.product_by_id(review_again, "product:198")["pendingOrders"], [])

    def test_un_espositore_ordinato_si_rivede_la_settimana_dopo(self) -> None:
        """D3: l'espositore non ha un EAN, e per questo spariva dagli avvisi.

        Il suo identificativo nasce dalla composizione — non dalla riga del
        foglio — quindi la settimana dopo indica lo stesso espositore ed e'
        l'unica identita' che abbia.
        """

        review = week_one_review()
        review["products"].append(espositore(ID_ESPOSITORE))
        store = self.make_store(review, run_dir="settimana-31")
        self.compila(store, self.snapshot("run-2026-31", {ID_ESPOSITORE: ("larice", 2)}))

        # La settimana dopo l'olio si e' spostato di riga: l'espositore no,
        # perche' il suo nome non dipende dalla posizione.
        settimana_dopo = week_two_review()
        settimana_dopo["products"].append(espositore(ID_ESPOSITORE))
        dopo = self.make_store(settimana_dopo, run_dir="settimana-32")
        prodotti = dopo.review()

        avvisi = self.product_by_id(prodotti, ID_ESPOSITORE)["pendingOrders"]
        self.assertEqual(len(avvisi), 1, "l'espositore ordinato deve restare visibile")
        self.assertEqual(avvisi[0]["quantity"], 2)
        self.assertEqual(avvisi[0]["unit"], "espositori")
        # E nessun altro articolo deve ereditare l'avviso dell'espositore.
        self.assertEqual(self.product_by_id(prodotti, "product:198")["pendingOrders"], [])

    def test_un_identificativo_di_riga_non_abbina_mai_neanche_senza_ean(self) -> None:
        """La trappola dell'olio e del tonno vale anche per le righe senza EAN."""

        history = {
            "schema_version": 1,
            "orders": [{
                "orderId": "cartella:larice",
                "createdAt": "2026-08-03T09:00:00+00:00",
                "supplier": "larice",
                "supplierName": "LARICE",
                "runId": "run-2026-31",
                "status": "in_attesa",
                "answeredAt": None,
                "totalNet": 10.0,
                "lines": [{"ean": "", "description": "ARTICOLO SENZA CODICE", "quantity": 3,
                           "unit": "colli", "orderUnitPriceNet": 10.0, "productId": "product:198"}],
            }],
        }
        products = [{"id": "product:198", "ean": ""}]

        order_history.attach_pending_orders(products, history)

        self.assertEqual(products[0]["pendingOrders"], [])


class RecordingTests(HistoryStoreTestCase):
    """Registrazione al termine di una compile() riuscita."""

    def test_compile_records_one_pending_order_per_supplier(self) -> None:
        review = week_one_review()
        review["products"].append(
            gestionale_product(
                "product:200",
                EAN_RISO,
                "RISO CARNAROLI 1KG",
                source_row=200,
                offers=[supplier_offer("betulla", EAN_RISO, "RISO CARNAROLI 1KG", source_row=77, order_price=11.0)],
            )
        )
        store = self.make_store(review, run_dir="settimana-31")

        esito = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12), "product:200": ("betulla", 5)}))

        history = self.read_history_file()
        self.assertEqual(history["schema_version"], 1)
        orders = {entry["orderId"]: entry for entry in history["orders"]}
        # L'identificativo nasce dalla cartella della compilazione: due
        # compilazioni della stessa run non si sovrascrivono piu'.
        self.assertEqual(set(orders), {f"{esito['cartella']}:larice", f"{esito['cartella']}:betulla"})
        larice = orders[f"{esito['cartella']}:larice"]
        self.assertEqual(larice["supplier"], "larice")
        self.assertEqual(larice["supplierName"], "LARICE")
        self.assertEqual(larice["runId"], "run-2026-31")
        self.assertEqual(larice["status"], "in_attesa")
        self.assertIsNone(larice["answeredAt"])
        self.assertEqual(larice["totalNet"], 178.8)
        self.assertEqual(len(larice["lines"]), 1)
        line = larice["lines"][0]
        self.assertEqual(line["ean"], EAN_OLIO)
        self.assertEqual(line["quantity"], 12)
        self.assertEqual(line["orderUnitPriceNet"], 14.9)
        self.assertEqual(orders[f"{esito['cartella']}:betulla"]["totalNet"], 55.0)

    def test_second_compile_of_the_same_run_updates_instead_of_duplicating(self) -> None:
        """D2: la seconda compilazione sostituisce la domanda, non la duplica."""

        store = self.make_store(week_one_review(), run_dir="settimana-31")

        self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))
        prima = self.read_history_file()["orders"][0]
        seconda = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 20)}))

        orders = self.read_history_file()["orders"]
        self.assertEqual(len(orders), 1, "Ricompilare non deve creare un secondo ordine in sospeso")
        self.assertEqual(orders[0]["orderId"], f"{seconda['cartella']}:larice")
        self.assertNotEqual(orders[0]["orderId"], prima["orderId"])
        self.assertEqual(orders[0]["lines"][0]["quantity"], 20)
        self.assertEqual(orders[0]["totalNet"], 298.0)
        # I 60 giorni decorrono dalla compilazione che e' rimasta in piedi:
        # e' quella che l'utente ha mandato al fornitore.
        self.assertGreaterEqual(orders[0]["createdAt"], prima["createdAt"])
        # Una sola voce, e visibile soltanto da una run diversa da quella compilata.
        self.assertEqual(store.history_pending()["pending"], [])
        settimana_dopo = self.make_store(week_two_review(), run_dir="settimana-32")
        self.assertEqual(len(settimana_dopo.history_pending()["pending"]), 1)

    def test_una_compilazione_gia_risposta_non_viene_sostituita(self) -> None:
        """D2: la sostituzione vale solo per le domande ancora senza risposta."""

        store = self.make_store(week_one_review(), run_dir="settimana-31")
        prima = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))
        settimana_dopo = self.make_store(week_two_review(), run_dir="settimana-32")
        settimana_dopo.answer_history_order({"orderId": f"{prima['cartella']}:larice", "received": True})

        seconda = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 20)}))

        orders = {entry["orderId"]: entry for entry in self.read_history_file()["orders"]}
        self.assertEqual(
            set(orders),
            {f"{prima['cartella']}:larice", f"{seconda['cartella']}:larice"},
            "l'ordine già ricevuto è storia chiusa e resta dov'è",
        )
        self.assertEqual(orders[f"{prima['cartella']}:larice"]["status"], "ricevuto")
        self.assertEqual(orders[f"{seconda['cartella']}:larice"]["status"], "in_attesa")
        # E la domanda non si duplica: quella chiusa non torna a chiedere.
        pendenti = settimana_dopo.history_pending()["pending"]
        self.assertEqual([voce["orderId"] for voce in pendenti], [f"{seconda['cartella']}:larice"])

    def test_lo_storico_nel_formato_vecchio_resta_leggibile(self) -> None:
        """D2: gli ordini scritti come `<run>:<fornitore>` continuano a valere."""

        self.write_history_file([
            history_entry("run-2026-30:larice", "2026-07-27T09:00:00+00:00", ean=EAN_OLIO),
        ])
        store = self.make_store(week_two_review(), run_dir="settimana-32")

        pendenti = store.history_pending()["pending"]
        self.assertEqual([voce["orderId"] for voce in pendenti], ["run-2026-30:larice"])
        # Si risponde, e la risposta arriva alla voce vecchia senza toccare altro.
        store.answer_history_order({"orderId": "run-2026-30:larice", "received": True})
        self.assertEqual(self.read_history_file()["orders"][0]["status"], "ricevuto")

    def test_una_compilazione_vecchia_della_stessa_run_viene_sostituita(self) -> None:
        """La voce nel formato vecchio non diventa un doppione della domanda."""

        self.write_history_file([
            history_entry("run-2026-31:larice", "2026-08-03T09:00:00+00:00", ean=EAN_OLIO),
        ])
        store = self.make_store(week_one_review(), run_dir="settimana-31")

        esito = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))

        orders = self.read_history_file()["orders"]
        self.assertEqual([entry["orderId"] for entry in orders], [f"{esito['cartella']}:larice"])

    def test_un_fornitore_consegnato_prima_non_sparisce_dalla_ricompilazione(self) -> None:
        """B-1 della revisione R4: l'ordine LARICE gia' mandato non si cancella.

        Si compila LARICE + BETULLA e si mandano i due listini; ci si accorge di
        un errore su BETULLA; si azzera LARICE e si ricompila il solo BETULLA.  La
        vecchia regola toglieva la voce LARICE perche' «non piu' nel piano» —
        ma dopo D1 ogni voce e' un documento consegnato davvero, e la settimana
        dopo il programma non avrebbe chiesto della merce LARICE ne' segnalato
        l'olio come gia' ordinato.
        """

        review = week_one_review()
        review["products"].append(
            gestionale_product(
                "product:200",
                EAN_RISO,
                "RISO CARNAROLI 1KG",
                source_row=200,
                offers=[supplier_offer("betulla", EAN_RISO, "RISO CARNAROLI 1KG", source_row=77, order_price=11.0)],
            )
        )
        store = self.make_store(review, run_dir="settimana-31")
        prima = self.compila(store, self.snapshot(
            "run-2026-31", {"product:198": ("larice", 12), "product:200": ("betulla", 5)}))

        seconda = self.compila(store, self.snapshot("run-2026-31", {"product:200": ("betulla", 7)}))

        orders = {entry["orderId"]: entry for entry in self.read_history_file()["orders"]}
        self.assertEqual(
            set(orders),
            {f"{prima['cartella']}:larice", f"{seconda['cartella']}:betulla"},
            "l'ordine LARICE della prima compilazione deve restare in piedi",
        )
        self.assertEqual(orders[f"{prima['cartella']}:larice"]["status"], "in_attesa")
        settimana_dopo = self.make_store(week_two_review(), run_dir="settimana-32")
        pendenti = {voce["orderId"] for voce in settimana_dopo.history_pending()["pending"]}
        self.assertIn(f"{prima['cartella']}:larice", pendenti)

    def test_una_risposta_non_ancora_arrivata_viene_superata_dalla_riconsegna(self) -> None:
        """M-2/I-5 della revisione R4: dopo un «no», la riconsegna sostituisce.

        La risposta «non ancora arrivata» parlava di una compilazione che la
        riconsegna dello STESSO fornitore ha superato: tenerla accanto alla
        nuova lasciava due domande per la stessa settimana e colli sommati
        due volte.
        """

        store = self.make_store(week_one_review(), run_dir="settimana-31")
        prima = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))
        settimana_dopo = self.make_store(week_two_review(), run_dir="settimana-32")
        settimana_dopo.answer_history_order(
            {"orderId": f"{prima['cartella']}:larice", "received": False})

        seconda = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 20)}))

        orders = self.read_history_file()["orders"]
        self.assertEqual([entry["orderId"] for entry in orders],
                         [f"{seconda['cartella']}:larice"],
                         "la domanda risposta «no» è stata superata dalla riconsegna")
        pendenti = self.make_store(week_two_review(), run_dir="settimana-32").history_pending()["pending"]
        self.assertEqual([(voce["orderId"], voce["totalNet"]) for voce in pendenti],
                         [(f"{seconda['cartella']}:larice", 298.0)],
                         "una domanda sola, con i colli dell'ultima compilazione")

    def test_pending_summary_has_the_fields_shown_to_the_user(self) -> None:
        store = self.make_store(week_one_review(), run_dir="settimana-31")
        esito = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))

        # La run che ha appena compilato non si autosegnala: la domanda comparira'
        # la settimana dopo, quando il confronto caricato e' un altro.
        self.assertEqual(store.history_pending()["pending"], [])

        settimana_dopo = self.make_store(week_two_review(), run_dir="settimana-32")
        pending = settimana_dopo.history_pending()

        self.assertTrue(pending["ok"])
        self.assertEqual(len(pending["pending"]), 1)
        entry = pending["pending"][0]
        self.assertEqual(set(entry), PENDING_SUMMARY_KEYS)
        self.assertEqual(entry["orderId"], f"{esito['cartella']}:larice")
        self.assertEqual(entry["supplierName"], "LARICE")
        self.assertEqual(entry["lineCount"], 1)
        self.assertEqual(entry["totalNet"], 178.8)
        self.assertTrue(entry["createdAt"])
        # Mai risposta: la domanda e' dovuta adesso, non fra una settimana.
        self.assertEqual(entry["askAgainAt"], "")

    def test_history_lives_outside_the_run_folder_and_survives_a_reset(self) -> None:
        store = self.make_store(week_one_review(), run_dir="current")
        self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))
        self.assertTrue(self.history_path.is_file())

        # Reset della run: la cartella di lavoro viene ricostruita da zero.
        shutil.rmtree(self.root / "current")

        self.assertTrue(self.history_path.is_file())
        rebuilt = self.make_store(week_two_review(), run_dir="current")
        self.assertEqual(len(rebuilt.history_pending()["pending"]), 1)
        self.assertEqual(
            len(self.product_by_id(rebuilt.review(), "product:42")["pendingOrders"]),
            1,
        )

    def test_default_history_path_is_derived_from_the_state_folder(self) -> None:
        directory = self.root / "data" / "current"
        directory.mkdir(parents=True, exist_ok=True)
        review_path = directory / "review_data.json"
        review_path.write_text(json.dumps(week_one_review(), ensure_ascii=False), encoding="utf-8")

        store = ReviewStore(review_path, directory / "state.json", directory / "uploads", directory / "outputs")

        self.assertEqual(store.history_path, (self.root / "data" / "history" / "orders.json").resolve())
        self.assertNotIn("current", store.history_path.parts)

    def test_history_write_is_atomic_and_leaves_no_temporary_file(self) -> None:
        store = self.make_store(week_one_review(), run_dir="settimana-31")
        self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))

        leftovers = list(self.history_path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_una_compilazione_fermata_non_diventa_un_ordine(self) -> None:
        """D1: senza copie dei listini non c'e' nessun ordine da ricordare.

        Era il difetto piu' grave dello storico: `record_order_history` stava
        prima della scrittura, e una compilazione fermata dai `writer_issues`
        lasciava comunque la domanda «e' arrivata?» di merce mai ordinata.
        """

        store = self.make_store(week_one_review(), run_dir="settimana-31")

        with mock.patch.object(
            store, "writer_configuration_issues",
            return_value=["Non è disponibile il programma necessario per creare le copie."],
        ):
            esito = store.compile(self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertTrue(esito["writerIssues"])
        self.assertFalse(self.history_path.exists(), "una compilazione fermata non lascia niente nello storico")
        # E nemmeno un avviso sui prodotti la settimana dopo.
        settimana_dopo = self.make_store(week_two_review(), run_dir="settimana-32")
        self.assertEqual(settimana_dopo.history_pending()["pending"], [])
        self.assertEqual(self.product_by_id(settimana_dopo.review(), "product:42")["pendingOrders"], [])

    def test_un_writer_che_non_parte_non_lascia_ordini(self) -> None:
        """D1: il writer che fallisce a metà non è un ordine mandato."""

        store = self.make_store(week_one_review(), run_dir="settimana-31")

        def il_writer_non_parte(plan_path, destinazione):
            raise ValueError("Il listino LARICE è cambiato dopo la verifica.")

        with mock.patch.object(store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(store, "run_writer", il_writer_non_parte):
            esito = store.compile(self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertFalse(self.history_path.exists())

    def test_il_fornitore_senza_copia_non_entra_e_non_cancella(self) -> None:
        """Una copia scartata vale come non consegnata, ma non tocca il passato."""

        review = week_one_review()
        review["products"].append(
            gestionale_product(
                "product:200",
                EAN_RISO,
                "RISO CARNAROLI 1KG",
                source_row=200,
                offers=[supplier_offer("betulla", EAN_RISO, "RISO CARNAROLI 1KG", source_row=77, order_price=11.0)],
            )
        )
        store = self.make_store(review, run_dir="settimana-31")
        ordinati = {"product:198": ("larice", 12), "product:200": ("betulla", 5)}

        prima = self.compila(store, self.snapshot("run-2026-31", ordinati))
        # Seconda compilazione: la copia BETULLA non viene creata.
        seconda = self.compila(store, self.snapshot("run-2026-31", ordinati), senza_copia=("betulla",))

        orders = {entry["orderId"]: entry for entry in self.read_history_file()["orders"]}
        self.assertIn(f"{seconda['cartella']}:larice", orders)
        self.assertNotIn(f"{seconda['cartella']}:betulla", orders)
        # L'ordine BETULLA della prima compilazione era vero e resta.
        self.assertIn(f"{prima['cartella']}:betulla", orders)
        self.assertNotIn(f"{prima['cartella']}:larice", orders)


class AnswerTests(HistoryStoreTestCase):
    """Una sola risposta per l'intero ordine: le consegne parziali non esistono."""

    def setUp(self) -> None:
        super().setUp()
        self.first_week = self.make_store(week_one_review(), run_dir="settimana-31")
        self.compila(self.first_week, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))
        self.second_week = self.make_store(week_two_review(), run_dir="settimana-32")
        self.order_id = self.order_id_of("larice")

    def test_yes_closes_the_order_and_removes_the_product_warnings(self) -> None:
        self.assertEqual(len(self.product_by_id(self.second_week.review(), "product:42")["pendingOrders"]), 1)

        result = self.second_week.answer_history_order({"orderId": self.order_id, "received": True})

        self.assertTrue(result["ok"])
        self.assertEqual(result["pending"], [])
        entry = self.read_history_file()["orders"][0]
        self.assertEqual(entry["status"], "ricevuto")
        self.assertTrue(entry["answeredAt"])
        self.assertEqual(self.second_week.history_pending()["pending"], [])
        self.assertEqual(self.product_by_id(self.second_week.review(), "product:42")["pendingOrders"], [])

    def test_no_keeps_the_order_pending_and_keeps_the_warnings(self) -> None:
        result = self.second_week.answer_history_order({"orderId": self.order_id, "received": False})

        self.assertTrue(result["ok"])
        self.assertEqual([item["orderId"] for item in result["pending"]], [self.order_id])
        entry = self.read_history_file()["orders"][0]
        self.assertEqual(entry["status"], "in_attesa")
        # La risposta viene registrata: la domanda non si ripete subito.
        self.assertTrue(entry["answeredAt"])
        self.assertEqual(len(self.product_by_id(self.second_week.review(), "product:42")["pendingOrders"]), 1)

    def test_dopo_un_no_la_domanda_torna_la_settimana_dopo(self) -> None:
        """D4: «non ancora arrivata» non archivia la domanda, la rimanda."""

        self.second_week.answer_history_order({"orderId": self.order_id, "received": False})

        voce = self.second_week.history_pending()["pending"][0]
        risposta = order_history.parse_moment(voce["answeredAt"])
        ritorno = order_history.parse_moment(voce["askAgainAt"])
        self.assertIsNotNone(ritorno, "il servizio deve dire quando la domanda torna")
        self.assertEqual(ritorno - risposta, timedelta(days=order_history.REASK_DAYS))
        self.assertEqual(order_history.REASK_DAYS, 7)

    def test_non_arrivera_piu_chiude_la_domanda_senza_dire_che_e_arrivata(self) -> None:
        """D4: l'unico modo di chiudere senza scrivere il falso."""

        result = self.second_week.answer_history_order({"orderId": self.order_id, "closed": True})

        self.assertTrue(result["ok"])
        self.assertEqual(result["pending"], [])
        entry = self.read_history_file()["orders"][0]
        self.assertEqual(entry["status"], order_history.STATUS_CLOSED)
        self.assertNotEqual(entry["status"], order_history.STATUS_RECEIVED)
        self.assertTrue(entry["answeredAt"])
        # Chiuso: niente domanda e niente avvisi sui prodotti.
        self.assertEqual(self.second_week.history_pending()["pending"], [])
        self.assertEqual(self.product_by_id(self.second_week.review(), "product:42")["pendingOrders"], [])

    def test_unknown_order_is_refused_without_touching_the_archive(self) -> None:
        before = self.history_path.read_text(encoding="utf-8")

        with self.assertRaises(ValueError):
            self.second_week.answer_history_order({"orderId": "run-inesistente:larice", "received": True})

        self.assertEqual(self.history_path.read_text(encoding="utf-8"), before)

    def test_answer_requires_an_explicit_yes_or_no(self) -> None:
        for payload in (
            {"orderId": self.order_id},
            {"orderId": self.order_id, "received": "si"},
            {"orderId": "", "received": True},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.second_week.answer_history_order(payload)


class CancellazioneCompilazioneTests(unittest.TestCase):
    def test_elimina_tutti_e_soli_i_promemoria_della_cartella(self) -> None:
        history = {
            "schema_version": 1,
            "orders": [
                {"orderId": "2026-08-13_1015:betulla", "status": "in_attesa"},
                {"orderId": "2026-08-13_1015:larice", "status": "ricevuto"},
                {"orderId": "2026-08-13_1015_2:betulla", "status": "in_attesa"},
                {"orderId": "2026-08-13_10150:betulla", "status": "scaduto"},
            ],
        }

        removed = order_history.remove_compilation(history, "2026-08-13_1015")

        self.assertEqual(
            {entry["orderId"] for entry in removed},
            {"2026-08-13_1015:betulla", "2026-08-13_1015:larice"},
        )
        self.assertEqual(
            [entry["orderId"] for entry in history["orders"]],
            ["2026-08-13_1015_2:betulla", "2026-08-13_10150:betulla"],
        )

    def test_una_chiave_vuota_non_cancella_niente(self) -> None:
        history = {"orders": [{"orderId": "2026-08-13_1015:betulla"}]}
        self.assertEqual(order_history.remove_compilation(history, ""), [])
        self.assertEqual(len(history["orders"]), 1)


class ExpiryTests(HistoryStoreTestCase):
    """Dopo 60 giorni la domanda non ha più senso e non va riproposta."""

    def test_expiry_uses_the_given_moment_not_the_wall_clock(self) -> None:
        frozen = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
        self.write_history_file([
            history_entry("run-vecchia:larice", (frozen - timedelta(days=61)).isoformat(), ean=EAN_OLIO),
            history_entry("run-recente:larice", (frozen - timedelta(days=59)).isoformat(), ean=EAN_OLIO),
        ])

        history, changed = order_history.read_history(self.history_path, now=frozen)

        self.assertTrue(changed)
        statuses = {entry["orderId"]: entry["status"] for entry in history["orders"]}
        self.assertEqual(statuses["run-vecchia:larice"], "scaduto")
        self.assertEqual(statuses["run-recente:larice"], "in_attesa")
        self.assertEqual(
            [entry["orderId"] for entry in order_history.pending_summary(history)],
            ["run-recente:larice"],
        )

    def test_exactly_sixty_days_is_still_proposed(self) -> None:
        frozen = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
        self.write_history_file([
            history_entry("run-limite:larice", (frozen - timedelta(days=60)).isoformat(), ean=EAN_OLIO),
        ])

        history, changed = order_history.read_history(self.history_path, now=frozen)

        self.assertFalse(changed)
        self.assertEqual(history["orders"][0]["status"], "in_attesa")

    def test_store_hides_expired_orders_and_writes_the_new_status(self) -> None:
        now = datetime.now(tz=timezone.utc)
        self.write_history_file([
            history_entry("run-vecchia:larice", (now - timedelta(days=61)).isoformat(), ean=EAN_OLIO),
            history_entry("run-recente:larice", (now - timedelta(days=59)).isoformat(), ean=EAN_TONNO),
        ])
        store = self.make_store(week_two_review(), run_dir="settimana-32")

        pending = store.history_pending()["pending"]

        self.assertEqual([entry["orderId"] for entry in pending], ["run-recente:larice"])
        statuses = {entry["orderId"]: entry["status"] for entry in self.read_history_file()["orders"]}
        self.assertEqual(statuses["run-vecchia:larice"], "scaduto")
        self.assertEqual(statuses["run-recente:larice"], "in_attesa")

    def test_expired_order_no_longer_marks_the_products(self) -> None:
        now = datetime.now(tz=timezone.utc)
        self.write_history_file([
            history_entry("run-vecchia:larice", (now - timedelta(days=61)).isoformat(), ean=EAN_OLIO),
            history_entry("run-recente:larice", (now - timedelta(days=59)).isoformat(), ean=EAN_TONNO),
        ])
        store = self.make_store(week_two_review(), run_dir="settimana-32")

        review = store.review()

        self.assertEqual(self.product_by_id(review, "product:42")["pendingOrders"], [])
        self.assertEqual(len(self.product_by_id(review, "product:198")["pendingOrders"]), 1)

    def test_la_scadenza_si_dice_in_pagina_e_non_in_silenzio(self) -> None:
        """D4: un ordine che scade è una domanda che sparisce: va detto."""

        now = datetime.now(tz=timezone.utc)
        self.write_history_file([
            history_entry("run-vecchia:larice", (now - timedelta(days=61)).isoformat(), ean=EAN_OLIO),
        ])
        store = self.make_store(week_two_review(), run_dir="settimana-32")

        review = store.review()

        avvisi = [item for item in review.get("warnings") or [] if item.get("code") == "ORDINI_SCADUTI_SENZA_RISPOSTA"]
        self.assertEqual(len(avvisi), 1, "la scadenza deve avere il suo avviso")
        avviso = avvisi[0]
        self.assertFalse(avviso["blocking"], "è un avviso, non una fermata")
        self.assertIn("LARICE", avviso["message"])
        self.assertIn("60 giorni", avviso["message"])
        # La data dell'ordine sta nella frase, in italiano leggibile.
        ordinato = order_history.parse_moment(self.read_history_file()["orders"][0]["createdAt"])
        self.assertIn(SERVER.consegna.data_leggibile(ordinato.astimezone()), avviso["message"])
        self.assertEqual(avviso["orders"][0]["orderId"], "run-vecchia:larice")

    def test_una_scadenza_vecchia_smette_di_occupare_la_pagina(self) -> None:
        """L'avviso non resta in pagina per sempre: dopo un mese ha detto la sua."""

        now = datetime.now(tz=timezone.utc)
        vecchia = history_entry("run-vecchia:larice", (now - timedelta(days=200)).isoformat(), ean=EAN_OLIO)
        vecchia["status"] = order_history.STATUS_EXPIRED
        vecchia["expiredAt"] = (now - timedelta(days=order_history.EXPIRY_NOTICE_DAYS + 1)).isoformat()
        self.write_history_file([vecchia])
        store = self.make_store(week_two_review(), run_dir="settimana-32")

        codici = {item.get("code") for item in store.review().get("warnings") or []}

        self.assertNotIn("ORDINI_SCADUTI_SENZA_RISPOSTA", codici)

    def test_uno_storico_vecchio_senza_data_di_scadenza_non_inventa_avvisi(self) -> None:
        """Compatibilita': gli archivi scritti prima non hanno `expiredAt`."""

        vecchia = history_entry("run-vecchia:larice", "2026-01-02T09:00:00+00:00", ean=EAN_OLIO)
        vecchia["status"] = order_history.STATUS_EXPIRED
        self.write_history_file([vecchia])
        store = self.make_store(week_two_review(), run_dir="settimana-32")

        codici = {item.get("code") for item in store.review().get("warnings") or []}

        self.assertNotIn("ORDINI_SCADUTI_SENZA_RISPOSTA", codici)


class HistoryFailureTests(HistoryStoreTestCase):
    """Lo storico è un promemoria: se si rompe, la compilazione resta valida."""

    def test_compile_succeeds_even_if_the_history_cannot_be_written(self) -> None:
        store = self.make_store(week_one_review(), run_dir="settimana-31")

        with mock.patch.object(order_history, "save_history", side_effect=OSError("disco non disponibile")):
            result = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))

        # Dalla Fase 6d il piano vive nella cartella datata della compilazione,
        # non piu' in `outputs`: e' la risposta stessa a dire quale.
        plan_path = self.root / "settimana-31" / "ordini" / result["cartella"] / "final_order_plan.json"
        self.assertTrue(plan_path.is_file(), "Il piano ordini deve esistere anche senza storico")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(len(plan["orders"]), 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "FILES_READY")
        self.assertTrue(result["historyIssues"], "Il problema dello storico va segnalato all'utente")
        self.assertIn("non è stato registrato", result["message"])
        self.assertFalse(self.history_path.exists())

    def test_compile_succeeds_when_the_archive_is_unreadable_and_leaves_it_intact(self) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text("{ questo non è JSON", encoding="utf-8")
        store = self.make_store(week_one_review(), run_dir="settimana-31")

        result = self.compila(store, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))

        self.assertTrue(result["ok"])
        self.assertTrue((self.root / "settimana-31" / "ordini" / result["cartella"] / "final_order_plan.json").is_file())
        self.assertTrue(result["historyIssues"])
        # L'archivio danneggiato non viene sovrascritto: nessun dato va perso.
        self.assertEqual(self.history_path.read_text(encoding="utf-8"), "{ questo non è JSON")

    def test_review_stays_usable_when_the_archive_is_unreadable(self) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text("{ questo non è JSON", encoding="utf-8")
        store = self.make_store(week_two_review(), run_dir="settimana-32")

        review = store.review()

        self.assertEqual(self.product_by_id(review, "product:42")["pendingOrders"], [])
        warnings = {str(item.get("code")) for item in review.get("warnings") or []}
        self.assertIn("STORICO_ORDINI_NON_LEGGIBILE", warnings)
        blocking = [item for item in review.get("warnings") or [] if item.get("code") == "STORICO_ORDINI_NON_LEGGIBILE"]
        self.assertFalse(blocking[0].get("blocking"), "L'avviso è informativo, non blocca il lavoro")


class SilentHandler(SERVER.AppHandler):
    def log_message(self, format_string: str, *args: object) -> None:  # noqa: D102
        return


class HistoryHttpTests(HistoryStoreTestCase):
    """Le due rotte usate dalla pagina 2."""

    def setUp(self) -> None:
        super().setUp()
        first_week = self.make_store(week_one_review(), run_dir="settimana-31")
        self.compila(first_week, self.snapshot("run-2026-31", {"product:198": ("larice", 12)}))
        self.ordine = self.order_id_of("larice")
        self.store = self.make_store(week_two_review(), run_dir="settimana-32")
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

    def call(self, method: str, path: str, payload: object | None = None) -> tuple[int, dict[str, object]]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_get_pending_returns_only_the_orders_still_waiting(self) -> None:
        status, payload = self.call("GET", "/api/history/pending")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["pending"]), 1)
        entry = payload["pending"][0]
        self.assertEqual(set(entry), PENDING_SUMMARY_KEYS)
        self.assertEqual(entry["orderId"], self.ordine)
        self.assertEqual(entry["supplierName"], "LARICE")
        self.assertEqual(entry["lineCount"], 1)
        self.assertEqual(entry["totalNet"], 178.8)

    def test_review_route_marks_the_product_with_the_same_ean(self) -> None:
        status, payload = self.call("GET", "/api/review")

        self.assertEqual(status, 200)
        products = {str(item["id"]): item for item in payload["products"]}
        self.assertEqual(len(products["product:42"]["pendingOrders"]), 1)
        self.assertEqual(products["product:198"]["pendingOrders"], [])

    def test_answer_yes_over_http_empties_the_pending_list(self) -> None:
        status, payload = self.call(
            "POST", "/api/history/answer", {"orderId": self.ordine, "received": True}
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pending"], [])
        self.assertEqual(self.call("GET", "/api/history/pending")[1]["pending"], [])

    def test_answer_no_over_http_keeps_the_order_in_the_list(self) -> None:
        status, payload = self.call(
            "POST", "/api/history/answer", {"orderId": self.ordine, "received": False}
        )

        self.assertEqual(status, 200)
        self.assertEqual([item["orderId"] for item in payload["pending"]], [self.ordine])
        self.assertEqual(self.read_history_file()["orders"][0]["status"], "in_attesa")

    def test_answer_with_an_unknown_order_is_rejected_with_a_readable_message(self) -> None:
        status, payload = self.call(
            "POST", "/api/history/answer", {"orderId": "run-che-non-esiste:larice", "received": True}
        )

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertTrue(str(payload["message"]).strip())
        # L'ordine buono resta in elenco: nessuna risposta e' stata registrata.
        self.assertEqual(
            [item["orderId"] for item in self.call("GET", "/api/history/pending")[1]["pending"]],
            [self.ordine],
        )

    def test_answer_without_the_yes_or_no_is_rejected(self) -> None:
        status, payload = self.call("POST", "/api/history/answer", {"orderId": self.ordine})

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])


class PendingOrdersInterfaceTests(unittest.TestCase):
    """Controlli minimi sulla pagina: testi italiani, niente window.confirm."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def test_the_two_buttons_are_explicit_and_do_not_use_a_browser_dialog(self) -> None:
        # ⚠ In stile frase come tutti gli altri comandi del programma: erano gli
        # unici tre scritti in maiuscolo, e il pulsante della compilazione era
        # gia' stato tolto dal maiuscolo di proposito, con il commento che lo
        # dice. Quello che conta e' che ci siano e che dicano la risposta per
        # esteso, non che gridino.
        self.assertIn("Sì, ricevuta", self.app_js)
        self.assertIn("No, non ancora", self.app_js)
        self.assertIn('data-action="history-received"', self.app_js)
        self.assertIn('data-action="history-not-received"', self.app_js)
        # Niente finestra del browser: la risposta si da' con due pulsanti grandi.
        card = self.app_js.split("function renderPendingOrderCard")[1].split("\n}\n")[0]
        answer_flow = self.app_js.split("async function answerPendingOrder")[1].split("\n}\n")[0]
        self.assertNotIn("window.confirm(", card)
        self.assertNotIn("window.confirm(", answer_flow)

    def test_the_show_filter_offers_the_already_ordered_choice(self) -> None:
        # Le voci di «Mostra» sono dichiarate in un posto solo e si disegnano da
        # quello: qui si controlla la dichiarazione. Che la voce compaia davvero
        # — e solo quando c'e' un ordine in sospeso — lo prova
        # `IlFiltroMostraSoloQuelloCheServe`, che esegue `app.js` in Node.
        self.assertIn('pending: { etichetta: "Già ordinati"', self.app_js)
        self.assertIn("pendingOrdersFor(product).length > 0", self.app_js)

    def test_the_product_notice_speaks_of_colli_not_pezzi(self) -> None:
        self.assertIn("Già ordinato:", self.app_js)
        self.assertIn("orderUnitLabel", self.app_js)


class HistoryCorrectionTests(unittest.TestCase):
    """Difetti trovati in revisione: ognuno ha qui la sua prova."""

    @staticmethod
    def plan(run_id: str, righe: list[dict[str, object]]) -> dict[str, object]:
        return {"run_id": run_id, "orders": righe}

    @staticmethod
    def riga(supplier: str, ean: str, quantity: int, *, unit: str = "colli") -> dict[str, object]:
        return {
            "supplier": supplier,
            "ean": ean,
            "description": f"ARTICOLO {ean}",
            "quantity": quantity,
            "desired_quantity_unit": unit,
            "order_unit_price_net": 10.0,
            "line_total_net": quantity * 10.0,
            "product_id": "product:1",
        }

    def test_supplier_removed_on_recompile_keeps_the_order_already_created(self) -> None:
        """Omettere un fornitore non cancella il listino gia' creato prima.

        Dopo D1 una voce entra nello storico soltanto quando sul disco esiste
        davvero la copia del listino.  La ricompilazione seguente non puo'
        sapere se quella copia sia gia' stata mandata: cancellarla farebbe
        sparire un ordine reale e riordinare la merce la settimana dopo.
        """

        history = order_history.empty_history()
        order_history.record_plan(history, self.plan("run-A", [
            self.riga("larice", "8000000000001", 12),
            self.riga("betulla", "8000000000002", 5),
        ]))
        self.assertEqual(len(history["orders"]), 2)

        # L'utente sposta tutto su LARICE e ricompila la stessa run. La copia
        # BETULLA della compilazione precedente potrebbe essere gia' stata
        # inviata e deve quindi restare nello storico.
        order_history.record_plan(history, self.plan("run-A", [
            self.riga("larice", "8000000000001", 12),
        ]))

        fornitori = {entry["supplier"] for entry in history["orders"]}
        self.assertEqual(fornitori, {"larice", "betulla"})
        # La merce BETULLA continua correttamente a risultare gia' ordinata.
        prodotti = [{"id": "product:9", "ean": "8000000000002"}]
        order_history.attach_pending_orders(prodotti, history)
        self.assertEqual(len(prodotti[0]["pendingOrders"]), 1)
        self.assertEqual(prodotti[0]["pendingOrders"][0]["supplier"], "betulla")

    def test_current_run_orders_are_not_asked_back_immediately(self) -> None:
        """Appena compilato, l'ordine non deve essere riproposto come domanda."""

        history = order_history.empty_history()
        order_history.record_plan(history, self.plan("run-corrente", [
            self.riga("larice", "8000000000001", 12),
        ]))

        self.assertEqual(order_history.pending_summary(history, exclude_run_id="run-corrente"), [])
        prodotti = [{"id": "product:1", "ean": "8000000000001"}]
        order_history.attach_pending_orders(prodotti, history, exclude_run_id="run-corrente")
        self.assertEqual(prodotti[0]["pendingOrders"], [])

        # Una run diversa vede invece l'ordine della settimana precedente.
        self.assertEqual(len(order_history.pending_summary(history, exclude_run_id="run-successiva")), 1)

    def test_answer_no_is_reported_so_the_question_is_not_repeated(self) -> None:
        history = order_history.empty_history()
        order_history.record_plan(history, self.plan("run-A", [
            self.riga("larice", "8000000000001", 12),
        ]))
        order_id = history["orders"][0]["orderId"]
        order_history.answer_order(history, order_id, False)

        voce = order_history.pending_summary(history, exclude_run_id="altra-run")[0]
        self.assertEqual(voce["orderId"], order_id)
        self.assertTrue(voce["answeredAt"], "answeredAt deve arrivare al programma, altrimenti la domanda si ripete")

    def test_received_order_is_not_resurrected_by_a_recompile(self) -> None:
        history = order_history.empty_history()
        piano = self.plan("run-A", [self.riga("larice", "8000000000001", 12)])
        order_history.record_plan(history, piano)
        order_id = history["orders"][0]["orderId"]
        order_history.answer_order(history, order_id, True)

        order_history.record_plan(history, piano)

        self.assertEqual(history["orders"][0]["status"], order_history.STATUS_RECEIVED)
        self.assertEqual(order_history.pending_summary(history, exclude_run_id="altra-run"), [])

    def test_entry_without_a_readable_date_expires_instead_of_lasting_forever(self) -> None:
        history = {"schema_version": 1, "orders": [
            {"orderId": "x:larice", "supplier": "larice", "supplierName": "LARICE",
             "runId": "x", "status": order_history.STATUS_PENDING, "createdAt": "",
             "answeredAt": None, "totalNet": 10.0, "lines": []},
        ]}
        self.assertTrue(order_history.expire_pending(history))
        self.assertEqual(history["orders"][0]["status"], order_history.STATUS_EXPIRED)

    def test_notice_keeps_the_unit_used_when_the_order_was_placed(self) -> None:
        """Un espositore ordinato resta un espositore, anche se oggi è un prodotto."""

        history = order_history.empty_history()
        order_history.record_plan(history, self.plan("run-A", [
            self.riga("larice", "8000000000001", 2, unit="espositori"),
        ]))
        indice = order_history.pending_by_identity(history)
        self.assertEqual(indice["ean:8000000000001"][0]["unit"], "espositori")


class RegoleDellaRevisioneR4Tests(unittest.TestCase):
    """Le regole corrette dopo la revisione avversariale del cantiere R4."""

    ADESSO = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    @staticmethod
    def _voce(order_id: str, *, created: str, answered: str | None = None,
              status: str = "in_attesa", expired: str | None = None) -> dict:
        return {"orderId": order_id, "supplier": "larice", "supplierName": "LARICE",
                "runId": "run-x", "status": status, "createdAt": created,
                "answeredAt": answered, "expiredAt": expired, "totalNet": 10.0,
                "lines": [{"ean": EAN_OLIO, "description": "OLIO", "quantity": 2,
                           "unit": "colli", "orderUnitPriceNet": 5.0, "productId": ""}]}

    def test_record_plan_casefolda_piano_e_consegnati(self) -> None:
        """I-1: un fornitore con una maiuscola non resta fuori dallo storico."""

        history = order_history.empty_history()
        piano = {"run_id": "run-x", "orders": [
            {"supplier": "Larice", "supplier_source_row": 3, "quantity": 5,
             "order_unit_price_net": 2.0, "ean": EAN_OLIO},
        ]}
        registrati = order_history.record_plan(
            history, piano, order_key="2026-08-13_1200", delivered={"LARICE"})

        self.assertEqual([voce["supplier"] for voce in registrati], ["larice"])
        self.assertEqual([voce["orderId"] for voce in registrati], ["2026-08-13_1200:larice"])

    def test_la_scadenza_conta_dall_ultima_risposta(self) -> None:
        """I-3: chi risponde «non ancora» ogni settimana non scade come chi tace."""

        history = {"schema_version": 1, "orders": [
            self._voce("vecchio-risposto:larice",
                       created="2026-05-01T09:00:00+00:00",
                       answered="2026-08-12T09:00:00+00:00"),
            self._voce("vecchio-muto:betulla", created="2026-05-01T09:00:00+00:00"),
        ]}

        cambiato = order_history.expire_pending(history, now=self.ADESSO)

        self.assertTrue(cambiato)
        stati = {voce["orderId"]: voce["status"] for voce in history["orders"]}
        self.assertEqual(stati["vecchio-risposto:larice"], order_history.STATUS_PENDING,
                         "risposto ieri: la domanda è viva")
        self.assertEqual(stati["vecchio-muto:betulla"], order_history.STATUS_EXPIRED)

    def test_l_avviso_degli_scaduti_porta_l_ultima_risposta(self) -> None:
        history = {"schema_version": 1, "orders": [
            self._voce("x:larice", created="2026-05-01T09:00:00+00:00",
                       answered="2026-06-01T09:00:00+00:00",
                       status=order_history.STATUS_EXPIRED,
                       expired="2026-08-01T09:00:00+00:00"),
        ]}

        scaduti = order_history.expired_orders(history, now=self.ADESSO)

        self.assertEqual(scaduti[0]["answeredAt"], "2026-06-01T09:00:00+00:00")

    def test_un_no_su_un_ordine_scaduto_non_lo_resuscita(self) -> None:
        """M-4: la risposta si registra, lo stato resta scaduto, niente avviso nuovo."""

        history = {"schema_version": 1, "orders": [
            self._voce("x:larice", created="2026-05-01T09:00:00+00:00",
                       status=order_history.STATUS_EXPIRED,
                       expired="2026-06-01T09:00:00+00:00"),
        ]}

        voce = order_history.answer_order(history, "x:larice", False, now=self.ADESSO)

        self.assertEqual(voce["status"], order_history.STATUS_EXPIRED)
        self.assertEqual(order_history.expired_orders(history, now=self.ADESSO), [],
                         "la scadenza resta quella vecchia, fuori dalla finestra dell'avviso")
        ricevuto = order_history.answer_order(history, "x:larice", True, now=self.ADESSO)
        self.assertEqual(ricevuto["status"], order_history.STATUS_RECEIVED,
                         "un «si', ricevuta» invece chiude davvero, com'è giusto")

    def test_la_finestra_dell_avviso_e_di_trenta_giorni_di_numero(self) -> None:
        """I-6: trenta giorni scritti come numero, non derivati dalla costante."""

        def scaduto(order_id: str, giorni_fa: int) -> dict:
            momento = (self.ADESSO - timedelta(days=giorni_fa)).isoformat()
            return self._voce(order_id, created="2026-05-01T09:00:00+00:00",
                              status=order_history.STATUS_EXPIRED, expired=momento)

        history = {"schema_version": 1, "orders": [
            scaduto("recente:larice", 29),
            scaduto("vecchio:larice", 31),
        ]}

        visti = [voce["orderId"] for voce in order_history.expired_orders(history, now=self.ADESSO)]

        self.assertEqual(visti, ["recente:larice"],
                         "29 giorni dentro la finestra, 31 fuori: numeri, non la costante")

    def test_l_identita_unmatched_non_abbina(self) -> None:
        """M-7: solo `display:composition:` nasce dal contenuto."""

        self.assertEqual(order_history.match_key("", "display:unmatched:larice:pippo:6"), "")
        self.assertEqual(order_history.match_key("", ID_ESPOSITORE), f"prodotto:{ID_ESPOSITORE}")

    def test_un_ean_condiviso_non_attribuisce_i_colli_a_tutti(self) -> None:
        """I-4: il codice condiviso si dichiara, non si somma su chi non ha ordinato."""

        history = {"schema_version": 1, "orders": [
            self._voce("x:larice", created="2026-08-01T09:00:00+00:00"),
        ]}
        prodotti = [
            {"id": "product:1", "ean": EAN_OLIO},
            {"id": "product:2", "ean": EAN_OLIO},
            {"id": "product:3", "ean": EAN_TONNO},
        ]

        order_history.attach_pending_orders(prodotti, history)

        self.assertEqual(prodotti[0]["pendingOrders"][0]["sharedWith"], 2)
        self.assertEqual(prodotti[1]["pendingOrders"][0]["sharedWith"], 2)
        self.assertEqual(prodotti[2]["pendingOrders"], [])
        # Con un codice tutto suo la voce non porta nessuna condivisione.
        da_solo = [{"id": "product:9", "ean": EAN_OLIO}]
        order_history.attach_pending_orders(da_solo, history)
        self.assertNotIn("sharedWith", da_solo[0]["pendingOrders"][0])


if __name__ == "__main__":
    unittest.main()
