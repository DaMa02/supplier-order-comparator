from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import http.client
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from openpyxl import Workbook, load_workbook
from openpyxl.utils import column_index_from_string


SKILL_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = SKILL_ROOT / "app" / "server.py"
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

# The pipeline's own test already builds the Noce .xls file: a real OLE2
# with real BIFF8 records, not a fake one.
import test_schema_pipeline as pipeline  # noqa: E402

SERVER_SPEC = importlib.util.spec_from_file_location("compara_ordini_web_server", SERVER_PATH)
if SERVER_SPEC is None or SERVER_SPEC.loader is None:
    raise RuntimeError(f"Impossibile importare {SERVER_PATH}")
SERVER = importlib.util.module_from_spec(SERVER_SPEC)
SERVER_SPEC.loader.exec_module(SERVER)

ReviewStore = SERVER.ReviewStore
# Same module the service uses: it computes the deferral dates itself.
ORDER_HISTORY = SERVER.order_history
SnapshotError = SERVER.SnapshotError
# One job at a time: the same exception the service translates to a 409.
LavoroGiaInCorso = SERVER.LavoroGiaInCorso
supplier_label = SERVER.supplier_label
# The same `consegna` module the service loaded: the delivery-type constants
# live there, and a second import would give a different module instance.
consegna = SERVER.consegna

SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import registro as registro_adattatori  # noqa: E402


def importa_launcher() -> Any:
    """Import the launcher as an isolated module, like the other tests here."""

    percorso = SKILL_ROOT / "app" / "launcher.py"
    spec = importlib.util.spec_from_file_location("compara_ordini_launcher_prova", percorso)
    if spec is None or spec.loader is None:
        raise AssertionError("Impossibile importare il launcher")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def synthetic_review() -> dict[str, object]:
    return {
        "run": {"id": "run-sintetica", "status": "ready", "label": "Test sintetico"},
        "files": [],
        "suppliers": [
            {"id": "larice", "name": "Larice", "minimumOrder": 1000},
        ],
        "products": [
            {
                "id": "product-standard",
                "kind": "PRODUCT",
                "itemType": "product",
                "sourceRow": 10,
                "ean": "8000000000010",
                "name": "PRODOTTO STANDARD",
                "description": "PRODOTTO STANDARD",
                "quantity": 0,
                "selectedSupplierId": "larice",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    {
                        "supplierId": "larice",
                        "available": True,
                        "description": "PRODOTTO STANDARD",
                        "sourceRow": 10,
                        "ean": "8000000000010",
                        "unitPriceNet": 1.5,
                        "quantityFactor": 6,
                        "orderUnitPriceNet": 9,
                        "method": "EAN",
                        "confidence": "CERTA",
                    },
                ],
                "components": [],
            },
            {
                "id": "display-solbao-96",
                "kind": "DISPLAY",
                "itemType": "display",
                "ean": "",
                "name": "SOLBAO ESPO TOP PERFORMER X 96",
                "description": "SOLBAO ESPO TOP PERFORMER X 96",
                "quantity": 0,
                "selectedSupplierId": "larice",
                "confirmed": True,
                "requiresConfirmation": False,
                "offers": [
                    {
                        "supplierId": "larice",
                        "available": True,
                        "description": "SOLBAO ESPO TOP PERFORMER X 96",
                        "supplierCode": "28246",
                        "sourceRow": 198,
                        "ean": "",
                        # As `display_offer` writes it: price per piece in
                        # `unitPriceNet`, the display's pieces in
                        # `quantityFactor`, the whole display in
                        # `orderUnitPriceNet`. 442.1196 / 96 = 4.6054.
                        "unitPriceNet": 4.6054,
                        "quantityFactor": 96,
                        "unitsPerOrderUnit": 96,
                        "pricePerPiece": 4.6054,
                        "orderUnitPriceNet": 442.1196,
                        "method": "COMPOSIZIONE",
                        "confidence": "ALTA",
                        "requiresConfirmation": False,
                        "components": [
                            {"sourceRow": 199, "ean": "8000000000001", "quantity": 6},
                            {"sourceRow": 200, "ean": "8000000000002", "quantity": 6},
                        ],
                    },
                ],
                "components": [
                    {"sourceRow": 199, "ean": "8000000000001", "quantity": 6},
                    {"sourceRow": 200, "ean": "8000000000002", "quantity": 6},
                ],
            },
        ],
        "warnings": [],
    }


class LaFraseDellaCompilazione(unittest.TestCase):
    """The six branches of the message shown when a compile finishes.

    The message doubles as the audit trail: it also lands in the folder
    audit's `messaggio` field, which is the only record left a week later.
    """

    PIANO = [{"tipo": "piano"}]
    PIANO_E_UN_LISTINO = [{"tipo": "piano"}, {"tipo": "listino"}]

    def frase(self, **campi: Any) -> str:
        parametri: dict[str, Any] = {
            "status": "PLAN_READY",
            "writer_configurato": False,
            "file_audit": self.PIANO,
            "copie_non_create": [],
            "infedeli": [],
            "avvisi_writer": [],
            "problemi_consegna": [],
            "history_issues": [],
        }
        parametri.update(campi)
        return ReviewStore.messaggio_della_compilazione(**parametri)

    def test_senza_writer_configurato_si_dice_che_cosa_manca(self) -> None:
        frase = self.frase()

        self.assertIn("Piano ordini convalidato", frase)
        self.assertIn("configurazione di scrittura", frase)

    def test_le_copie_non_create_portano_il_loro_motivo(self) -> None:
        frase = self.frase(
            writer_configurato=True,
            copie_non_create=["Manca Node.", "Manca il listino di BETULLA."],
        )

        self.assertIn("Non sono state create copie dei listini:", frase)
        self.assertIn("Manca Node.", frase)
        self.assertIn("Manca il listino di BETULLA.", frase)
        self.assertNotIn("configurazione di scrittura", frase)

    def test_col_writer_configurato_non_si_dice_di_configurarlo(self) -> None:
        """'Never tried' and 'tried and got nothing' are different states,
        and only the configuration flag tells them apart, not whether a
        reason is given. A fully configured writer must not read "complete
        the writer configuration".
        """

        senza_motivo = self.frase(writer_configurato=True, copie_non_create=[])

        self.assertIn("Non sono state create copie dei listini", senza_motivo)
        self.assertNotIn("configurazione di scrittura", senza_motivo)

    def test_con_le_copie_create_si_riepiloga_la_cartella(self) -> None:
        frase = self.frase(status="FILES_READY", file_audit=self.PIANO_E_UN_LISTINO)

        self.assertIn("1 copia del listino", frase)
        self.assertIn("nessun ordine è stato inviato", frase)
        self.assertNotIn("Non sono state create", frase)

    def test_qualche_copia_scartata_e_le_altre_no_dice_quale_manca(self) -> None:
        frase = self.frase(
            status="FILES_READY",
            file_audit=self.PIANO_E_UN_LISTINO,
            infedeli=["La copia di LARICE non regge."],
        )

        self.assertIn("1 copia del listino", frase)
        self.assertIn("Attenzione: La copia di LARICE non regge.", frase)

    def test_degli_avvisi_del_writer_resta_il_numero_e_non_il_testo(self) -> None:
        """The page already lists these via `writerIssues`; repeating them here
        would show them twice under a title that contradicts them. Only the
        count stays, since the compile history keeps the message, not the list.
        """

        uno = self.frase(status="FILES_READY", avvisi_writer=["riga 12 sospetta"])
        due = self.frase(status="FILES_READY", avvisi_writer=["riga 12", "riga 40"])

        self.assertIn("Su un listino preparato c'è una segnalazione da leggere.", uno)
        self.assertNotIn("riga 12", uno)
        self.assertIn("ci sono 2 segnalazioni da leggere.", due)

    def test_un_nome_brutto_e_uno_storico_muto_si_dicono_tutt_e_due(self) -> None:
        frase = self.frase(
            status="FILES_READY",
            file_audit=self.PIANO_E_UN_LISTINO,
            problemi_consegna=["LARICE ha tenuto il nome tecnico."],
            history_issues=["Senza copie non c'è nessun ordine da mandare."],
        )

        self.assertIn("LARICE ha tenuto il nome tecnico.", frase)
        self.assertIn("non è stato registrato fra quelli da controllare", frase)
        self.assertIn("Senza copie non c'è nessun ordine da mandare.", frase)
        self.assertLess(
            frase.index("LARICE ha tenuto il nome tecnico."),
            frase.index("non è stato registrato"),
            "sbagliare l'ordine di due `message` è già successo",
        )

    def test_le_copie_scartate_non_si_mescolano_agli_avvisi_del_writer(self) -> None:
        """When NO copy was created, the discarded copies are the reason.

        The writer warnings say "the quantity was written anyway", which
        contradicts a sentence declaring nothing was created.
        """

        frase = self.frase(
            writer_configurato=True,
            copie_non_create=["La copia di LARICE non regge."],
            infedeli=["La copia di LARICE non regge."],
            avvisi_writer=["la riga 12 aveva già un valore"],
        )

        self.assertIn("La copia di LARICE non regge.", frase)
        self.assertNotIn("la riga 12 aveva già un valore", frase)
        self.assertNotIn("segnalazioni da leggere", frase)

class IlConfrontoSiCostruisceUnaVoltaSola(unittest.TestCase):
    """Each autosave must build the comparison exactly once, not twice.

    `save_state` calls `validate_snapshot`, which builds the comparison to
    know which products exist, and would otherwise rebuild it again after
    writing the state. `base_review` re-reads and re-parses all of
    `review_data.json` (2.4 MB on the real comparison), plus
    `upload_profiles.json`, the catalog and the discounts. The page
    autosaves 450 ms after every edit, so a double build on every quantity
    change would throw the first result away for nothing.

    These tests count rebuilds instead of timing them, since a timing test
    would be flaky depending on machine load.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        radice = Path(self.temporary.name)
        run_dir = radice / "run-corrente"
        run_dir.mkdir(parents=True)
        review_path = radice / "review_data.json"
        review_path.write_text(json.dumps(synthetic_review()), encoding="utf-8")
        self.store = ReviewStore(
            review_path, run_dir / "review_state.json", radice / "uploads", radice / "outputs"
        )
        self.snapshot = {
            "runId": "run-sintetica",
            "currentStep": 2,
            "products": [
                {"id": "display-solbao-96", "quantity": 3, "selectedSupplierId": "larice", "confirmed": True},
            ],
        }

    def conta_le_costruzioni(self, chiamata):
        """Count how many times `base_review` re-read the comparison from disk."""

        vero = self.store.base_review
        conteggio: list[int] = []

        def spia():
            conteggio.append(1)
            return vero()

        self.store.base_review = spia
        try:
            esito = chiamata()
        finally:
            self.store.base_review = vero
        return len(conteggio), esito

    def test_un_salvataggio_lo_costruisce_una_volta(self) -> None:
        """The first save is an exception, not an oversight: without a
        `state.json` on disk the state declares no run, so the comparison
        must be rebuilt (see the test below). What matters is the steady-state
        save, which the page does 450 ms after every edit.
        """

        primo = self.store.save_state(self.snapshot)

        volte, esito = self.conta_le_costruzioni(lambda: self.store.save_state(
            {**self.snapshot, "stateVersion": primo["stateVersion"]}
        ))

        self.assertTrue(esito["ok"])
        self.assertEqual(volte, 1)

    def test_anche_l_anteprima_dello_spostamento_lo_costruisce_una_volta(self) -> None:
        """Always true here: the preview writes nothing, so the comparison
        built by validation is literally what a rebuild would produce.
        """

        volte, _ = self.conta_le_costruzioni(lambda: self.store.move_preview({
            **self.snapshot,
            "from": "larice",
        }))

        self.assertEqual(volte, 1)

    def test_con_uno_stato_di_un_altra_run_il_confronto_si_ricostruisce(self) -> None:
        """The one case where reusing the comparison would change something.

        `apply_match_overrides` (the answers given to match candidates)
        applies only when the state on disk declares the same run as the
        comparison. With a state declaring another run, the comparison built
        before the write skips those overrides and the one built after does
        not: the program must notice and rebuild instead of returning a
        comparison missing something.
        """

        SERVER.atomic_json(self.store.state_path, {
            "schemaVersion": 1,
            "runId": "una-run-di-un-altra-settimana",
            "products": [],
        })

        volte, esito = self.conta_le_costruzioni(lambda: self.store.save_state(self.snapshot))

        self.assertTrue(esito["ok"])
        self.assertEqual(volte, 2, "con lo stato di un'altra run il confronto va rifatto")

    def test_due_salvataggi_di_fila_danno_lo_stesso_riepilogo(self) -> None:
        """If the reused comparison weren't what a rebuild would produce,
        the second save would report different numbers than the first.
        """

        primo = self.store.save_state(self.snapshot)
        secondo = self.store.save_state({**self.snapshot, "stateVersion": primo["stateVersion"]})

        self.assertEqual(primo["orderSummary"], secondo["orderSummary"])
        self.assertEqual(primo["promotionStates"], secondo["promotionStates"])

    def test_il_confronto_riusato_dice_quello_che_direbbe_uno_ricostruito(self) -> None:
        """The property that justifies the shortcut, tested rather than assumed:

        nothing the comparison depends on changes between read and write, so
        the numbers must match.

        The starting state carries the keys the comparison depends on (a
        header discount and a manually added product) on purpose: a state
        without any of those would exercise a comparison with nothing to lose.
        """

        SERVER.atomic_json(self.store.state_path, {
            "schemaVersion": 1,
            "runId": "run-sintetica",
            "stateVersion": 3,
            "products": [],
            "supplierDiscounts": {"larice": {"rate": 0.06, "runId": "run-sintetica"}},
            "manualProducts": [{
                "id": "manuale:1",
                "name": "AGGIUNTO A MANO",
                "ean": "8000000009999",
                "quantity": 0,
                "offers": [{
                    "supplierId": "larice", "available": True, "sourceRow": 900,
                    "unitPriceNet": 1.5, "orderUnitPriceNet": 9.0, "quantityFactor": 6,
                }],
            }],
        })
        prima, _stato = self.store.review_with_manual_products()
        scontato = next(
            offerta for prodotto in prima["products"] if prodotto["id"] == "manuale:1"
            for offerta in prodotto["offers"]
        )
        self.assertLess(scontato["unitPriceNet"], 1.5, "lo sconto di testata non è stato applicato")

        esito = self.store.save_state({**self.snapshot, "stateVersion": 3})

        rifatto, _stato = self.store.review_with_manual_products()
        selezioni = {
            str(voce["id"]): voce
            for voce in SERVER.load_json(self.store.state_path, {}).get("products") or []
        }
        self.assertEqual(esito["orderSummary"], self.store.order_summary(rifatto, selezioni))


class WebAppStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.review_path = self.root / "review_data.json"
        # The state lives in a "run" subfolder because the order history is
        # derived from state_path.parent.parent/history/orders.json: with the
        # state at the root of the temp dir, the tests' compile() calls would
        # write outside it, into %TEMP%\history\orders.json.
        self.run_dir = self.root / "run-corrente"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "review_state.json"
        self.upload_dir = self.root / "uploads"
        self.output_dir = self.root / "outputs"
        # Each compile gets its own dated folder in here; the plan lives
        # there, not in `outputs` under a fixed name, which is why every
        # test below looks it up through `piano_di`.
        self.orders_dir = self.root / "ordini"
        self.review_path.write_text(json.dumps(synthetic_review()), encoding="utf-8")
        self.store = ReviewStore(
            self.review_path,
            self.state_path,
            self.upload_dir,
            self.output_dir,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- where a compile's files end up ---------------------------------------

    def cartelle(self) -> list[Path]:
        """The compile folders present, in name order."""

        if not self.orders_dir.is_dir():
            return []
        return sorted(item for item in self.orders_dir.iterdir() if item.is_dir())

    def cartella_di(self, esito: dict[str, Any]) -> Path:
        """The folder of the compile just run, taken from its response."""

        return self.orders_dir / esito["cartella"]

    def piano_di(self, esito: dict[str, Any]) -> dict[str, Any]:
        """That compile's plan, read from its dated folder."""

        return json.loads((self.cartella_di(esito) / "final_order_plan.json").read_text(encoding="utf-8"))

    def audit_di(self, esito: dict[str, Any]) -> dict[str, Any]:
        return json.loads((self.cartella_di(esito) / "compilazione.json").read_text(encoding="utf-8"))

    @staticmethod
    def snapshot(
        quantity: int,
        *,
        current_step: int = 4,
        accept_below_threshold: bool = False,
    ) -> dict[str, object]:
        return {
            "runId": "run-sintetica",
            "currentStep": current_step,
            "acceptBelowThreshold": accept_below_threshold,
            "products": [
                {
                    "id": "display-solbao-96",
                    "quantity": quantity,
                    "selectedSupplierId": "larice",
                    "confirmed": True,
                },
            ],
        }

    def error_codes(self, error: SnapshotError) -> set[str]:
        return {str(item.get("code")) for item in error.errors}

    def test_state_persists_current_step_and_threshold_consent(self) -> None:
        snapshot = self.snapshot(1, current_step=3, accept_below_threshold=True)
        snapshot["summaryGrouping"] = "product"
        result = self.store.save_state(snapshot)

        self.assertTrue(result["ok"])
        self.assertIn("promotionSummary", result)
        self.assertIn("promotionStates", result)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["currentStep"], 3)
        self.assertTrue(saved["acceptBelowThreshold"])
        self.assertEqual(saved["summaryGrouping"], "product")

        reloaded_store = ReviewStore(
            self.review_path,
            self.state_path,
            self.upload_dir,
            self.output_dir,
        )
        review = reloaded_store.review()
        self.assertEqual(review["state"]["currentStep"], 3)
        self.assertTrue(review["state"]["acceptBelowThreshold"])
        self.assertEqual(review["state"]["summaryGrouping"], "product")
        display = next(item for item in review["products"] if item["id"] == "display-solbao-96")
        self.assertEqual(display["quantity"], 1)
        self.assertEqual(display["selectedSupplierId"], "larice")

    def test_una_proposta_rifiutata_diventa_offerta_solo_dopo_il_si_dell_utente(self) -> None:
        review = synthetic_review()
        product = next(item for item in review["products"] if item["id"] == "product-standard")
        candidate = {
            "supplierId": "larice",
            "supplierName": "LARICE",
            "available": True,
            "description": "PRODOTTO STANDARD LARICE 6 PEZZI",
            "ean": "8000000000010",
            "supplierCode": "G-44",
            "sourceRow": 44,
            "unitPriceNet": 1.25,
            "quantityFactor": 6,
            "orderUnitPriceNet": 7.5,
            "pricePerPiece": 1.25,
            "candidateKey": "cand-44",
        }
        product["offers"] = [{
            "supplierId": "larice",
            "available": False,
            "method": "AI_RIFIUTATO",
            "rejectedCandidate": candidate,
        }]
        product["warnings"] = [{
            "code": "RIFIUTO_CON_CANDIDATO_FORTE",
            "supplierId": "larice",
            "candidateKey": "cand-44",
            "candidate": candidate,
        }]
        review["warnings"] = [{"code": "RIFIUTI_CON_CANDIDATO_FORTE"}]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        self.store.save_state(self.snapshot(0, current_step=2, accept_below_threshold=False))

        result = self.store.answer_rejected_candidate({
            "runId": "run-sintetica",
            "productId": "product-standard",
            "supplierId": "larice",
            "candidateKey": "cand-44",
            "accepted": True,
        })

        self.assertTrue(result["ok"])
        refreshed = self.store.review()
        refreshed_product = next(item for item in refreshed["products"] if item["id"] == "product-standard")
        offer = refreshed_product["offers"][0]
        self.assertTrue(offer["available"])
        self.assertEqual(offer["description"], "PRODOTTO STANDARD LARICE 6 PEZZI")
        self.assertEqual(offer["sourceRow"], 44)
        self.assertEqual(offer["candidateDecision"], "accepted")
        self.assertEqual(refreshed_product["warnings"], [])
        self.assertNotIn("RIFIUTI_CON_CANDIDATO_FORTE", {item.get("code") for item in refreshed["warnings"]})

        # L'autosalvataggio successivo non perde la correzione.
        saved = self.store.save_state({
            "runId": "run-sintetica",
            "currentStep": 2,
            "acceptBelowThreshold": True,
            "products": [{
                "id": "product-standard",
                "quantity": 2,
                "selectedSupplierId": "larice",
                "confirmed": True,
            }],
        })
        self.assertTrue(saved["ok"])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["matchOverrides"][0]["candidateKey"], "cand-44")

        compiled = self.store.compile({
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [{
                "id": "product-standard",
                "quantity": 2,
                "selectedSupplierId": "larice",
                "confirmed": True,
            }],
        })
        order = self.piano_di(compiled)["orders"][0]
        self.assertEqual(order["supplier_source_row"], 44)
        self.assertEqual(order["order_unit_price_net"], 7.5)

    def test_un_no_esclude_la_proposta_senza_renderla_ordinabile(self) -> None:
        review = synthetic_review()
        product = next(item for item in review["products"] if item["id"] == "product-standard")
        candidate = {
            "supplierId": "larice",
            "available": True,
            "description": "ALTRO PRODOTTO",
            "sourceRow": 88,
            "unitPriceNet": 1.0,
            "quantityFactor": 6,
            "orderUnitPriceNet": 6.0,
            "candidateKey": "cand-88",
        }
        product["offers"] = [{
            "supplierId": "larice",
            "available": False,
            "method": "AI_RIFIUTATO",
            "rejectedCandidate": candidate,
        }]
        product["warnings"] = [{
            "code": "RIFIUTO_CON_CANDIDATO_FORTE",
            "supplierId": "larice",
            "candidateKey": "cand-88",
        }]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        self.store.save_state(self.snapshot(0, current_step=2))

        self.store.answer_rejected_candidate({
            "runId": "run-sintetica",
            "productId": "product-standard",
            "supplierId": "larice",
            "candidateKey": "cand-88",
            "accepted": False,
        })

        refreshed_product = next(item for item in self.store.review()["products"] if item["id"] == "product-standard")
        self.assertFalse(refreshed_product["offers"][0]["available"])
        self.assertEqual(refreshed_product["offers"][0]["candidateDecision"], "rejected")
        self.assertEqual(refreshed_product["warnings"], [])

    def test_compile_below_threshold_requires_explicit_consent(self) -> None:
        with self.assertRaises(SnapshotError) as raised:
            self.store.compile(
                self.snapshot(1, accept_below_threshold=False)
            )

        self.assertEqual(self.error_codes(raised.exception), {"SOGLIA_NON_CONFERMATA"})
        error = raised.exception.errors[0]
        self.assertEqual(error["supplier"], "larice")
        self.assertEqual(error["total_net"], 442.12)
        self.assertEqual(error["threshold_net"], 1000.0)
        self.assertFalse((self.output_dir / "final_order_plan.json").exists())
        self.assertFalse(self.state_path.exists())
        # The plan isn't written, so its folder must not appear either: an
        # empty folder would look like a compile that ran and produced
        # nothing.
        self.assertEqual(self.cartelle(), [])

    def test_compile_display_writes_one_order_on_parent_source_row(self) -> None:
        result = self.store.compile(
            self.snapshot(2, accept_below_threshold=True)
        )

        self.assertEqual(result["status"], "PLAN_READY")
        self.assertEqual(result["totalsNet"], {"larice": 884.24})
        self.assertEqual(
            result["belowThreshold"],
            [{"supplier": "larice", "total_net": 884.24, "threshold_net": 1000.0}],
        )

        plan = self.piano_di(result)
        self.assertTrue(plan["threshold_override_confirmed"])
        self.assertEqual(len(plan["orders"]), 1)
        order = plan["orders"][0]
        self.assertEqual(order["item_type"], "display")
        self.assertEqual(order["supplier"], "larice")
        self.assertEqual(order["supplier_source_row"], 198)
        self.assertEqual(order["quantity"], 2)
        # The display delivers its pieces, not itself: 2 displays of 96 are
        # 192 pieces at 4.6054 each. The total is unchanged, since the whole
        # display is invoiced.
        self.assertEqual(order["quantity_factor"], 96.0)
        self.assertEqual(order["delivered_pieces"], 192)
        self.assertEqual(order["unit_price_net"], 4.6054)
        self.assertEqual(order["order_unit_price_net"], 442.1196)
        self.assertEqual(order["line_total_net"], 884.2392)
        self.assertNotIn(order["supplier_source_row"], {199, 200})

    def test_standard_product_quantity_is_colli_with_no_rounding(self) -> None:
        # The user enters cartons to order directly, not pieces: 10 cartons
        # stay 10 cartons, with no rounding and no excess, even though the
        # carton holds 6 pieces (larice offer's quantityFactor).
        result = self.store.compile(
            {
                "runId": "run-sintetica",
                "currentStep": 3,
                "acceptBelowThreshold": True,
                "products": [
                    {
                        "id": "product-standard",
                        "quantity": 10,
                        "selectedSupplierId": "larice",
                        "confirmed": True,
                    }
                ],
            }
        )

        self.assertEqual(result["totalsNet"], {"larice": 90.0})
        plan = self.piano_di(result)
        order = plan["orders"][0]
        self.assertEqual(order["desired_quantity"], 10)
        self.assertEqual(order["desired_quantity_unit"], "colli")
        self.assertEqual(order["quantity"], 10)
        self.assertEqual(order["quantity_factor"], 6.0)
        self.assertEqual(order["delivered_pieces"], 60)
        self.assertEqual(order["excess_pieces"], 0)
        self.assertEqual(order["line_total_net"], 90.0)

    def test_no_rounding_three_colli_stay_three_regardless_of_pieces_per_collo(self) -> None:
        # Same principle isolated from the price calculation: 3 cartons stay
        # 3 cartons whether the carton holds 6 pieces (larice) or 97 (a
        # supplier with a very different factor).
        review = synthetic_review()
        review["suppliers"].append({"id": "betulla", "name": "Betulla", "minimumOrder": 0})
        standard = next(item for item in review["products"] if item["id"] == "product-standard")
        standard["offers"].append({
            "supplierId": "betulla",
            "available": True,
            "description": "PRODOTTO STANDARD BETULLA",
            "sourceRow": 40,
            "ean": "8000000000010",
            "unitPriceNet": 1.0,
            "quantityFactor": 97,
            "orderUnitPriceNet": 97.0,
            "method": "EAN",
            "confidence": "CERTA",
        })
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        primo = store.compile({
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [{"id": "product-standard", "quantity": 3, "selectedSupplierId": "larice", "confirmed": True}],
        })
        order_larice = self.piano_di(primo)["orders"][0]

        secondo = store.compile({
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [{"id": "product-standard", "quantity": 3, "selectedSupplierId": "betulla", "confirmed": True}],
        })
        order_betulla = self.piano_di(secondo)["orders"][0]

        self.assertEqual(order_larice["quantity"], 3)
        self.assertEqual(order_larice["quantity_factor"], 6.0)
        self.assertEqual(order_larice["delivered_pieces"], 18)
        self.assertEqual(order_larice["excess_pieces"], 0)

        self.assertEqual(order_betulla["quantity"], 3)
        self.assertEqual(order_betulla["quantity_factor"], 97.0)
        self.assertEqual(order_betulla["delivered_pieces"], 291)
        self.assertEqual(order_betulla["excess_pieces"], 0)

    def test_compile_computes_colli_pieces_and_totals_for_product_display_and_noce(self) -> None:
        review = synthetic_review()
        review["suppliers"].append({"id": "noce", "name": "Noce", "minimumOrder": 0})
        standard = next(item for item in review["products"] if item["id"] == "product-standard")
        standard["offers"].append({
            "supplierId": "noce",
            "available": True,
            "description": "PRODOTTO STANDARD NOCE",
            "sourceRow": 20,
            "ean": "8000000000010",
            "unitPriceNet": 1.4,
            "quantityFactor": 6,
            "orderUnitPriceNet": 8.4,
            "method": "EAN",
            "confidence": "CERTA",
        })
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        # A regular product (larice, 6 pieces/carton) plus a display (larice,
        # 1 piece/carton: the display itself is the order unit).
        misto = store.compile({
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [
                {"id": "product-standard", "quantity": 3, "selectedSupplierId": "larice", "confirmed": True},
                {"id": "display-solbao-96", "quantity": 2, "selectedSupplierId": "larice", "confirmed": True},
            ],
        })
        plan = self.piano_di(misto)
        product_order = next(item for item in plan["orders"] if item["product_id"] == "product-standard")
        display_order = next(item for item in plan["orders"] if item["product_id"] == "display-solbao-96")

        self.assertEqual(product_order["quantity"], 3)
        self.assertEqual(product_order["quantity_factor"], 6.0)
        self.assertEqual(product_order["delivered_pieces"], 18)
        self.assertEqual(product_order["desired_quantity_unit"], "colli")
        self.assertEqual(product_order["line_total_net"], 27.0)

        self.assertEqual(display_order["quantity"], 2)
        self.assertEqual(display_order["quantity_factor"], 96.0)
        self.assertEqual(display_order["delivered_pieces"], 192)
        self.assertEqual(display_order["desired_quantity_unit"], "espositori")
        self.assertEqual(display_order["line_total_net"], 884.2392)

        # Noce: the factor is the order multiplier from the price list's
        # "unit" field, exactly as for the regular EAN offer above.
        mega = store.compile({
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [
                {"id": "product-standard", "quantity": 4, "selectedSupplierId": "noce", "confirmed": True},
            ],
        })
        plan_mega = self.piano_di(mega)
        mega_order = plan_mega["orders"][0]
        self.assertEqual(mega_order["quantity"], 4)
        self.assertEqual(mega_order["quantity_factor"], 6.0)
        self.assertEqual(mega_order["delivered_pieces"], 24)
        self.assertEqual(mega_order["line_total_net"], 33.6)

    def test_order_plan_preserves_keys_required_by_the_writer(self) -> None:
        # scripts/write_supplier_orders.mjs and the Noce compile path in
        # app/server.py read these keys by name from the compiled plan: if
        # they disappeared, writing the price lists would break without any
        # other test noticing.
        esito = self.store.compile(self.snapshot(2, accept_below_threshold=True))
        plan = self.piano_di(esito)
        order = plan["orders"][0]

        required_keys = {
            "product_id", "item_type", "ean", "description",
            "supplier", "supplier_source_row", "supplier_ean", "supplier_description",
            "quantity", "desired_quantity", "desired_quantity_unit",
            "delivered_pieces", "excess_pieces",
            "unit_price_net", "quantity_factor", "order_unit_price_net", "line_total_net",
        }
        missing = required_keys - order.keys()
        self.assertEqual(missing, set())
        self.assertEqual(order["excess_pieces"], 0)
        self.assertEqual(order["quantity"], order["desired_quantity"])

    def test_review_passes_through_suggested_quantity_and_source_from_gestionale(self) -> None:
        review = synthetic_review()
        standard = next(item for item in review["products"] if item["id"] == "product-standard")
        standard["suggestedQuantity"] = 4
        standard["quantitySource"] = "gestionale"
        standard["quantity"] = 4
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        current = store.review()
        product = next(item for item in current["products"] if item["id"] == "product-standard")

        self.assertEqual(product["suggestedQuantity"], 4)
        self.assertEqual(product["quantitySource"], "gestionale")
        self.assertEqual(product["quantityLabel"], "colli")

    def test_quantity_source_survives_save_and_reload(self) -> None:
        """A quantity edited by the user must not revert to 'gestionale' after a
        reload: otherwise the command that resets only default quantities
        would erase a deliberate choice.
        """

        review = synthetic_review()
        standard = next(item for item in review["products"] if item["id"] == "product-standard")
        standard["suggestedQuantity"] = 4
        standard["quantitySource"] = "gestionale"
        standard["quantity"] = 4
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        store.save_state({
            "runId": review["run"]["id"],
            "currentStep": 2,
            "summaryGrouping": "supplier",
            "acceptBelowThreshold": False,
            "products": [
                {
                    "id": "product-standard",
                    "quantity": 7,
                    "selectedSupplierId": "larice",
                    "confirmed": True,
                    "excluded": False,
                    "quantitySource": "utente",
                },
            ],
        })

        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        decision = next(item for item in saved["products"] if item["id"] == "product-standard")
        self.assertEqual(decision["quantitySource"], "utente")

        reloaded = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)
        product = next(
            item for item in reloaded.review()["products"] if item["id"] == "product-standard"
        )
        self.assertEqual(product["quantity"], 7)
        self.assertEqual(product["quantitySource"], "utente")
        self.assertEqual(product["suggestedQuantity"], 4)

    def test_excluded_product_is_saved_with_zero_quantity(self) -> None:
        snapshot = self.snapshot(3, accept_below_threshold=True)
        snapshot["products"][0]["excluded"] = True

        self.store.save_state(snapshot)

        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["products"][0]["quantity"], 0)
        self.assertTrue(saved["products"][0]["excluded"])

    def test_saved_piece_quantity_updates_a_carton_promotion(self) -> None:
        review = synthetic_review()
        standard = next(item for item in review["products"] if item["id"] == "product-standard")
        standard["offers"][0]["promotionText"] = "ACQUISTA 2 CT IN OMAGGIO 1 CT DI PRODOTTO CAMPIONE"
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        result = store.save_state({
            "runId": "run-sintetica",
            "currentStep": 2,
            "acceptBelowThreshold": False,
            "products": [{
                "id": "product-standard",
                "quantity": 12,
                "selectedSupplierId": "larice",
                "confirmed": True,
            }],
        })

        statuses = {state["status"] for state in result["promotionStates"].values()}
        self.assertEqual(statuses, {"ottenuta"})

    def test_search_and_add_product_from_supplier_catalog(self) -> None:
        catalog_path = self.root / "noce.csv"
        with catalog_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=["catalog_page", "ean", "product", "packaging", "availability", "variation", "price", "unit"],
            )
            writer.writeheader()
            writer.writerow({
                "catalog_page": "1",
                "ean": "8000000000099",
                "product": "PRODOTTO NUOVO CON OMAGGIO",
                "packaging": "6 x 10 PZ",
                "availability": "Disponibile ACQUISTA 3 CT IN OMAGGIO 1 CT",
                "variation": "",
                "price": "1,25",
                "unit": "x 6",
            })
        review = synthetic_review()
        review["files"] = [{
            "name": catalog_path.name,
            "sourcePath": str(catalog_path),
            "role": "supplier",
            "supplierId": "noce",
            # `adapterId` and `schemaState` aren't decoration: they're the two
            # fields the catalog uses to pick the reader, the same ones the
            # pipeline decides on. A real review always carries them (written
            # by `build_review_data.manifest_files`); without them this
            # fixture would describe a review that doesn't exist.
            "adapterId": "noce_csv_v1",
            "schemaState": "SCHEMA_NOTO",
        }]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        result = store.search_products("prodotto nuovo")

        self.assertEqual(result["count"], 1)
        found = result["results"][0]
        self.assertEqual(found["ean"], "8000000000099")
        self.assertEqual(found["offers"][0]["quantityFactor"], 6.0)
        self.assertIn("ACQUISTA 3 CT", found["offers"][0]["promotionText"])

        added = store.add_manual_product({"catalogId": found["catalogId"]})

        self.assertTrue(added["ok"])
        current = store.review()
        manual = next(item for item in current["products"] if item.get("addedManually"))
        self.assertEqual(manual["name"], "PRODOTTO NUOVO CON OMAGGIO")
        # Even for a product added manually from the catalog, the quantity
        # unit is the carton, not the piece.
        self.assertEqual(manual["quantityLabel"], "colli")

    def test_catalog_ranks_suppliers_by_price_per_piece_not_by_total_per_carton(self) -> None:
        # The most dangerous regression of this rule, reproduced here for the
        # "add from catalog" path too: cipresso has the lower total per carton
        # (12.00 EUR) but is NOT the cheaper one per piece (2.00 EUR/piece vs.
        # noce's 1.00 EUR/piece, since noce sells cartons of 24 instead of 6).
        # noce must win.
        noce_path = self.root / "noce_trappola.csv"
        with noce_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=["catalog_page", "ean", "product", "packaging", "availability", "variation", "price", "unit"],
            )
            writer.writeheader()
            writer.writerow({
                "catalog_page": "1",
                "ean": "8000000000077",
                "product": "PRODOTTO TRAPPOLA",
                "packaging": "24 x 10 PZ",
                "availability": "Disponibile",
                "variation": "",
                "price": "1,00",
                "unit": "x 24",
            })
        cipresso_path = self.root / "cipresso_trappola.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Listino al 10-08-2026"
        sheet.append(["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"])
        sheet.append(["E-777", "PRODOTTO TRAPPOLA", "PZ", 6, 2.00, "8000000000077", None])
        workbook.save(cipresso_path)
        workbook.close()

        review = synthetic_review()
        review["suppliers"].append({"id": "noce", "name": "Noce", "minimumOrder": 0})
        review["suppliers"].append({"id": "cipresso", "name": "Cipresso", "minimumOrder": 0})
        review["files"] = [
            {
                "name": noce_path.name,
                "sourcePath": str(noce_path),
                "role": "supplier",
                "supplierId": "noce",
                "adapterId": "noce_csv_v1",
                "schemaState": "SCHEMA_NOTO",
            },
            {
                "name": cipresso_path.name,
                "sourcePath": str(cipresso_path),
                "role": "supplier",
                "supplierId": "cipresso",
                "fieldMapping": {
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
                },
            },
        ]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        result = store.search_products("prodotto trappola")

        self.assertEqual(result["count"], 1)
        found = result["results"][0]
        offers = {offer["supplierId"]: offer for offer in found["offers"]}

        self.assertEqual(offers["noce"]["unitPriceNet"], 1.0)
        self.assertEqual(offers["noce"]["quantityFactor"], 24)
        self.assertEqual(offers["noce"]["orderUnitPriceNet"], 24.0)

        self.assertEqual(offers["cipresso"]["unitPriceNet"], 2.0)
        self.assertEqual(offers["cipresso"]["quantityFactor"], 6)
        self.assertEqual(offers["cipresso"]["orderUnitPriceNet"], 12.0)

        # Precondition of the trap: cipresso wins on the total per carton...
        self.assertLess(offers["cipresso"]["orderUnitPriceNet"], offers["noce"]["orderUnitPriceNet"])
        # ...but noce wins on price per piece, the only valid criterion.
        self.assertLess(offers["noce"]["unitPriceNet"], offers["cipresso"]["unitPriceNet"])
        self.assertEqual(found["selectedSupplierId"], "noce")

    def test_search_and_add_product_from_verified_cipresso_catalog(self) -> None:
        catalog_path = self.root / "cipresso.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Listino al 10-08-2026"
        sheet.append(["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"])
        sheet.append(["E-001", "PRODOTTO CIPRESSO NUOVO", "PZ", 6, 1.25, "8000000000098", None])
        workbook.save(catalog_path)
        workbook.close()

        review = synthetic_review()
        review["files"] = [{
            "name": catalog_path.name,
            "sourcePath": str(catalog_path),
            "role": "supplier",
            "supplierId": "cipresso",
            "fieldMapping": {
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
            },
        }]
        review["suppliers"].append({"id": "cipresso", "name": "Cipresso", "minimumOrder": 1000})
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        search = store.search_products("cipresso nuovo")

        self.assertEqual(search["count"], 1)
        found = search["results"][0]
        self.assertEqual(found["ean"], "8000000000098")
        self.assertEqual(found["offers"][0]["supplierId"], "cipresso")
        self.assertEqual(found["offers"][0]["quantityFactor"], 6.0)

        added = store.add_manual_product({"catalogId": found["catalogId"]})

        self.assertTrue(added["ok"])
        manual = next(item for item in store.review()["products"] if item.get("addedManually"))
        self.assertEqual(manual["selectedSupplierId"], "cipresso")

    def test_compile_cipresso_without_verified_writer_returns_only_the_plan(self) -> None:
        review = synthetic_review()
        review["suppliers"].append({"id": "cipresso", "name": "Cipresso", "minimumOrder": 0})
        standard = next(item for item in review["products"] if item["id"] == "product-standard")
        standard["offers"].append({
            "supplierId": "cipresso",
            "available": True,
            "description": "PRODOTTO STANDARD CIPRESSO",
            "sourceRow": 28,
            "ean": "8000000000010",
            "unitPriceNet": 1.25,
            "quantityFactor": 6,
            "orderUnitPriceNet": 7.5,
            "method": "EAN",
            "confidence": "CERTA",
        })
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        writer_config = self.root / "writer_config.json"
        writer_config.write_text(json.dumps({
            # The active comparison's run: without it, compile stops before
            # looking at anything else, which is the right behavior.
            "run_id": "run-sintetica",
            "node_executable": str(self.root / "node-mancante.exe"),
            "writer_script": str(self.root / "writer-mancante.mjs"),
            "supplier_files": {},
            "supplier_write_rules": {},
        }), encoding="utf-8")
        store = ReviewStore(
            self.review_path,
            self.state_path,
            self.upload_dir,
            self.output_dir,
            writer_config,
        )

        result = store.compile({
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [{
                "id": "product-standard",
                "quantity": 6,
                "selectedSupplierId": "cipresso",
                "confirmed": True,
            }],
        })

        self.assertEqual(result["status"], "PLAN_READY")
        self.assertIn("CIPRESSO", result["message"])
        self.assertIn("Non sono state create copie", result["message"])
        self.assertEqual([item["name"] for item in result["outputs"]], ["final_order_plan.json"])
        # The plan lives in the dated folder, NOT in `outputs`: that's what
        # keeps the next compile from overwriting it.
        self.assertTrue((self.cartella_di(result) / "final_order_plan.json").is_file())
        self.assertFalse((self.output_dir / "final_order_plan.json").exists())
        self.assertFalse(any(self.output_dir.glob("*.xlsx")))
        self.assertFalse(any(self.cartella_di(result).glob("*.xlsx")))

    def test_cipresso_missing_rule_prevents_partial_larice_copy(self) -> None:
        node = Path(sys.executable).resolve().parents[1] / "node" / "bin" / "node.exe"
        if not node.is_file():
            self.skipTest("Node non disponibile per verificare il blocco delle copie parziali")
        larice_source = self.root / "larice.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "LARICE"
        for _row in range(1, 199):
            sheet.append([None, None, None, None])
        workbook.save(larice_source)
        workbook.close()

        review = synthetic_review()
        review["suppliers"].append({"id": "cipresso", "name": "Cipresso", "minimumOrder": 0})
        standard = next(item for item in review["products"] if item["id"] == "product-standard")
        standard["offers"].append({
            "supplierId": "cipresso",
            "available": True,
            "description": "PRODOTTO STANDARD CIPRESSO",
            "sourceRow": 28,
            "ean": "8000000000010",
            "unitPriceNet": 1.25,
            "quantityFactor": 6,
            "orderUnitPriceNet": 7.5,
            "method": "EAN",
            "confidence": "CERTA",
        })
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        writer_config = self.root / "writer_config.json"
        writer_config.write_text(json.dumps({
            "run_id": "run-sintetica",
            "node_executable": str(node),
            "writer_script": str(SKILL_ROOT / "scripts" / "write_supplier_orders.mjs"),
            "supplier_files": {"larice": str(larice_source)},
            "supplier_write_rules": {
                "larice": {"sheet": "LARICE", "order_column": "D", "data_start_row": 2},
            },
        }), encoding="utf-8")
        store = ReviewStore(
            self.review_path,
            self.state_path,
            self.upload_dir,
            self.output_dir,
            writer_config,
        )

        result = store.compile({
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [
                {
                    "id": "product-standard",
                    "quantity": 6,
                    "selectedSupplierId": "cipresso",
                    "confirmed": True,
                },
                {
                    "id": "display-solbao-96",
                    "quantity": 1,
                    "selectedSupplierId": "larice",
                    "confirmed": True,
                },
            ],
        })

        self.assertEqual(result["status"], "PLAN_READY")
        self.assertIn("CIPRESSO", result["message"])
        self.assertFalse((self.output_dir / "ORDINE_LARICE_larice.xlsx").exists())
        self.assertFalse(any(self.output_dir.glob("*.xlsx")))
        # Not in the compile folder either: the writer never started, so it
        # holds only the plan and its audit.
        self.assertFalse(any(self.cartella_di(result).glob("*.xlsx")))

    def test_launcher_recognizes_cipresso_only_with_verified_mapping(self) -> None:
        launcher_path = SKILL_ROOT / "app" / "launcher.py"
        launcher_spec = importlib.util.spec_from_file_location("compara_ordini_launcher_test", launcher_path)
        if launcher_spec is None or launcher_spec.loader is None:
            self.fail("Impossibile importare il launcher")
        launcher = importlib.util.module_from_spec(launcher_spec)
        sys.modules[launcher_spec.name] = launcher
        launcher_spec.loader.exec_module(launcher)
        # Frozen copy of the registry: the program rewrites
        # `references/adapters.json` when it learns a confirmed schema, and
        # this test relies on what the shipped CIPRESSO adapter declares.
        # See the note in `tests/test_registro_impronte.py`.
        launcher.ADAPTERS_PATH = SKILL_ROOT / "tests" / "fixtures" / "adapters_nativi.json"

        source = self.root / "cipresso.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Listino Cipresso"
        sheet.append(["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"])
        sheet.append(["E-1", "PRODOTTO", "PZ", 6, 1.25, "8000000000001", None])
        workbook.save(source)
        workbook.close()
        mapping = {
            "sheet": "FIRST",
            "header_row": 1,
            "data_start_row": 2,
            "order_column": "G",
            "columns": {
                "supplier_code": "COD.ART.",
                "description": "DES.ARTICOLO",
                "pieces_per_carton": "QT",
                "unit_price_net": "LISTINO",
                "ean": "COD.EAN",
            },
        }
        review = {"files": [{
            "supplierId": "cipresso",
            "adapterId": "cipresso_v1",
            "sourcePath": str(source),
            "fieldMapping": mapping,
        }]}

        _node, sources, rules, _missing, warnings = launcher.writer_readiness(review)

        self.assertEqual(sources["cipresso"], source.resolve())
        self.assertEqual(rules["cipresso"]["sheet"], "Listino Cipresso")
        self.assertEqual(rules["cipresso"]["order_column"], "G")
        self.assertEqual(rules["cipresso"]["expected_header"], "ORDINE")
        self.assertFalse(warnings)

        mapping["order_column"] = "D"
        _node, sources, rules, _missing, warnings = launcher.writer_readiness(review)
        self.assertNotIn("cipresso", sources)
        self.assertNotIn("cipresso", rules)
        self.assertTrue(any("CIPRESSO non attivato" in warning for warning in warnings))

    def test_writer_cipresso_writes_g28_and_g1746_only_in_a_copy(self) -> None:
        node = Path(sys.executable).resolve().parents[1] / "node" / "bin" / "node.exe"
        if not node.is_file():
            located = shutil.which("node")
            if not located:
                self.skipTest("Node non disponibile per il writer XLSX")
            node = Path(located)
        source = self.root / "listino_cipresso.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Listino verificato"
        sheet.append(["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"])
        for row in range(2, 1747):
            sheet.append([f"E{row:06d}", f"PRODOTTO {row}", "PZ", 6, 1.25, f"800000{row:07d}", None])
        sheet["G28"] = 91
        sheet["G1746"] = 77
        workbook.save(source)
        workbook.close()
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()

        betulla_source = self.root / "listino_betulla.xlsx"
        betulla_book = Workbook()
        betulla_sheet = betulla_book.active
        betulla_sheet.title = "BETULLA"
        betulla_sheet.append(["EAN", "CodArt", "ORDINE", "Descrizione"])
        betulla_sheet.append(["8000000000001", "C-001", None, "PRODOTTO BETULLA"])
        betulla_book.save(betulla_source)
        betulla_book.close()

        larice_source = self.root / "listino_larice.xlsx"
        larice_book = Workbook()
        larice_sheet = larice_book.active
        larice_sheet.title = "LARICE"
        larice_sheet.append(["Famiglia", "Indicatore", "Codice", "Quantità", "Descrizione"])
        larice_sheet.append(["LINEA", "I", "G-001", None, "PRODOTTO LARICE"])
        larice_book.save(larice_source)
        larice_book.close()

        plan = {
            "source_workbook": "test",
            "totals_net": {"betulla": 5.0, "cipresso": 5.0, "larice": 5.0, "noce": 5.0},
            "orders": [
                {"supplier": "betulla", "supplier_source_row": 2, "quantity": 4},
                {"supplier": "cipresso", "supplier_source_row": 28, "quantity": 3},
                {"supplier": "cipresso", "supplier_source_row": 1746, "quantity": 2},
                {"supplier": "larice", "supplier_source_row": 2, "quantity": 5},
                {
                    "supplier": "noce",
                    "supplier_source_row": 9,
                    "supplier_ean": "8000000000009",
                    "supplier_description": "PRODOTTO NOCE",
                    "quantity": 6,
                    "unit_price_net": 1.5,
                    "quantity_factor": 6,
                    "line_total_net": 9.0,
                },
            ],
        }
        plan_path = self.root / "final_order_plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        writer_config = self.root / "writer_config.json"
        writer_config.write_text(json.dumps({
            "supplier_files": {
                "betulla": str(betulla_source),
                "cipresso": str(source),
                "larice": str(larice_source),
                # Noce is here too, as in the real configuration
                # `prepare_writer_config` writes: an ordered supplier is
                # always present. It's the write *procedure* declared by its
                # rule that skips it, and an ordered supplier missing a
                # configured price list is reported instead of ignored.
                "noce": str(source),
            },
            "supplier_write_rules": {
                "noce": {
                    "sheet": "Foglio1",
                    "order_column": "I",
                    "header_row": 5,
                    "data_start_row": 6,
                    "compilazione": "patch_xls_in_posizione",
                },
                "betulla": {
                    "sheet": "BETULLA",
                    "order_column": "C",
                    "header_row": 1,
                    "expected_header": "ORDINE",
                    "data_start_row": 2,
                    "source_sha256": hashlib.sha256(betulla_source.read_bytes()).hexdigest(),
                },
                "cipresso": {
                    "sheet": "Listino verificato",
                    "order_column": "G",
                    "header_row": 1,
                    "expected_header": "ORDINE",
                    "data_start_row": 2,
                    "source_sha256": original_hash,
                },
                "larice": {
                    "sheet": "LARICE",
                    "order_column": "D",
                    "data_start_row": 2,
                    "source_sha256": hashlib.sha256(larice_source.read_bytes()).hexdigest(),
                },
            },
        }), encoding="utf-8")
        environment = os.environ.copy()
        command = [
            str(node),
            str(SKILL_ROOT / "scripts" / "write_supplier_orders.mjs"),
            "--plan", str(plan_path),
            "--config", str(writer_config),
            "--output-dir", str(self.output_dir),
        ]
        result = subprocess.run(
            command,
            cwd=SKILL_ROOT / "scripts",
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        output = self.output_dir / "ORDINE_CIPRESSO_listino_cipresso.xlsx"
        self.assertTrue(output.is_file())
        betulla_output = self.output_dir / "ORDINE_BETULLA_listino_betulla.xlsx"
        larice_output = self.output_dir / "ORDINE_LARICE_listino_larice.xlsx"
        self.assertTrue(betulla_output.is_file())
        self.assertTrue(larice_output.is_file())
        # The Node writer produces NOTHING for Noce: they get their `.xls`
        # patched in place by `app/xls_writer.py` instead. The plan above
        # carries a Noce row on purpose, so this test fails if that branch
        # regressed to producing a file with "noce" in its name. An artifact
        # with a completely different name wouldn't be caught here: this is
        # the widest guard that doesn't have to enumerate names that don't
        # exist.
        roba_noce = sorted(
            voce.name for voce in self.output_dir.iterdir() if "noce" in voce.name.casefold()
        )
        self.assertEqual(roba_noce, [], roba_noce)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
        from openpyxl import load_workbook

        source_book = load_workbook(source, read_only=True, data_only=False)
        try:
            self.assertEqual(source_book["Listino verificato"]["G28"].value, 91)
            self.assertEqual(source_book["Listino verificato"]["G1746"].value, 77)
        finally:
            source_book.close()
        output_book = load_workbook(output, read_only=True, data_only=False)
        try:
            output_sheet = output_book["Listino verificato"]
            self.assertEqual(output_sheet["G1"].value, "ORDINE")
            self.assertEqual(output_sheet["G28"].value, 3)
            self.assertEqual(output_sheet["G1746"].value, 2)
        finally:
            output_book.close()
        betulla_book = load_workbook(betulla_output, read_only=True, data_only=False)
        try:
            self.assertEqual(betulla_book["BETULLA"]["C2"].value, 4)
        finally:
            betulla_book.close()
        larice_book = load_workbook(larice_output, read_only=True, data_only=False)
        try:
            self.assertEqual(larice_book["LARICE"]["D2"].value, 5)
        finally:
            larice_book.close()
        # The plan's Noce row produces nothing HERE: the local service writes
        # its copy into their `.xls`, and the Node writer only counts it in
        # its own summary. The writer also prints spreadsheet-engine status
        # lines, so the summary is the last JSON object in the output.
        riepilogo = json.loads(result.stdout[result.stdout.index('{\n  "supplier_copies"'):])
        self.assertEqual(riepilogo["noce_lines"], 1)
        self.assertEqual(
            [voce["supplier"] for voce in riepilogo["supplier_copies"]],
            ["betulla", "cipresso", "larice"],
        )

    def test_compile_rejects_empty_order_without_writing_outputs(self) -> None:
        with self.assertRaises(SnapshotError) as raised:
            self.store.compile(
                self.snapshot(0, accept_below_threshold=True)
            )

        self.assertEqual(self.error_codes(raised.exception), {"ORDINE_VUOTO"})
        self.assertFalse((self.output_dir / "final_order_plan.json").exists())
        self.assertFalse(self.state_path.exists())

    def test_non_si_compila_mentre_il_confronto_si_sta_aggiornando(self) -> None:
        """The data lock alone doesn't prevent this, and the result is misleading.

        `compile` takes `self.lock`; the pipeline takes it only at the instant
        it swaps in the live comparison. For every stage before that, compile
        is legal and produces the price lists of the PREVIOUS comparison
        (last week's prices) while page 1 shows a bar saying the comparison is
        updating.
        """

        vero = self.store.pipeline_jobs.in_corso
        self.store.pipeline_jobs.in_corso = lambda: True
        try:
            with self.assertRaises(LavoroGiaInCorso) as fermato:
                self.store.compile(self.snapshot(1, accept_below_threshold=True))
        finally:
            self.store.pipeline_jobs.in_corso = vero

        # The message is what whoever orders reads, and says why not now.
        self.assertIn("si sta aggiornando", str(fermato.exception))
        # And no folder: an order that never started leaves nothing behind.
        self.assertEqual(self.cartelle(), [])

        # Counter-proof: once the recalc finishes, the same command compiles.
        self.store.compile(self.snapshot(1, accept_below_threshold=True))
        self.assertEqual(len(self.cartelle()), 1)

    def test_una_compilazione_rifiutata_non_lascia_nessuna_cartella(self) -> None:
        """The sibling of the test above, guarding against a folder-creation bug.

        Creating the folder before validation would be convenient and would
        leave a ghost compile behind for every empty or below-threshold
        order: the list of previous compiles would fill with folders that
        contain nothing and that nobody asked for.
        """

        # The root already exists (the constructor creates it) and is empty.
        self.assertTrue(self.orders_dir.is_dir())
        self.assertEqual(self.cartelle(), [])

        with self.assertRaises(SnapshotError):
            self.store.compile(self.snapshot(0, accept_below_threshold=True))
        self.assertEqual(self.cartelle(), [])

        with self.assertRaises(SnapshotError):
            self.store.compile(self.snapshot(1, accept_below_threshold=False))
        self.assertEqual(self.cartelle(), [])

        # And proof the count isn't empty for some other reason: an accepted
        # compile does create the folder.
        self.store.compile(self.snapshot(1, accept_below_threshold=True))
        self.assertEqual(len(self.cartelle()), 1)

    # -- ingesting the Noce Excel 97-2003 (.xls) price list --------------

    def carica(self, nome: str, contenuto: bytes, role: str = "suppliers") -> dict[str, Any]:
        return self.store.upload({"files": [{
            "name": nome,
            "data": base64.b64encode(contenuto).decode("ascii"),
            "role": role,
        }]})

    def test_il_listino_xls_di_noce_si_carica_e_viene_riconosciuto(self) -> None:
        """The simplest and most blocking bug: "Format not accepted: .xls"."""
        listino = pipeline.scrivi_listino_noce(
            self.root / "formattato_104233.xls", pipeline.RIGHE_DI_PROVA, pipeline.RIGHE_IN_CODA,
        )

        esito = self.carica("formattato_104233.xls", listino.read_bytes())

        self.assertTrue(esito["ok"])
        acquisito = esito["files"][0]
        self.assertEqual(acquisito["status"], "profiled")
        self.assertEqual(acquisito["contentFormat"], "xls")
        self.assertEqual(acquisito["schemaState"], "SCHEMA_NOTO")
        self.assertEqual(acquisito["adapterId"], "noce_xls_v1")
        self.assertTrue((self.upload_dir / "formattato_104233.xls").is_file())
        # The message must not name an internal tool the user has never
        # heard of, for a confirmation step the pipeline doesn't need.
        self.assertEqual(esito["message"].split(".")[0], "1 documento caricato e letto")
        self.assertNotIn("Codex", esito["message"])
        self.assertIn("Confronta i listini", esito["message"])

    def test_un_formato_non_ammesso_dice_quali_sono_ammessi(self) -> None:
        with self.assertRaises(ValueError) as errore:
            self.carica("contratto.pdf", b"%PDF-1.4 ...")

        messaggio = str(errore.exception)
        self.assertIn("Formato non accettato: .pdf", messaggio)
        self.assertIn(".xls", messaggio)
        self.assertFalse(any(self.upload_dir.iterdir()))

    def test_un_nome_lunghissimo_si_taglia_prima_di_perdere_l_estensione(self) -> None:
        """The 180-character truncation must happen BEFORE the extension check.

        Truncating after the check would let a name longer than 180
        characters pass, then lose the dot and extension in the cut: the
        resulting name would differ from the one the check had approved.
        """

        nome_lunghissimo = "A" * 250 + ".xlsx"

        tagliato = SERVER.safe_upload_name(nome_lunghissimo)

        self.assertEqual(len(tagliato), 180)
        self.assertEqual(Path(tagliato).suffix, ".xlsx")

    def test_un_listino_con_nome_lunghissimo_arriva_sul_disco_ancora_xlsx(self) -> None:
        """The real risk isn't an error message: it's a price list that
        silently drops out of the comparison. Upload succeeds (the reader
        picks the format from content, not from the name), but
        `candidate_files` in `scripts/inspect_sources.py` filters by suffix
        when the pipeline re-scans the folder: a file with no extension is
        skipped and the supplier disappears from the comparison silently.
        """

        sorgente = listino_finto(self.root / "sorgente.xlsx", "LARICE")
        nome_lunghissimo = "A" * 250 + ".xlsx"

        esito = self.carica(nome_lunghissimo, sorgente.read_bytes())

        self.assertTrue(esito["ok"])
        # `upload_profiles.json` lives in the same folder as the uploaded
        # documents: only the copy just written, taken from the response, is
        # checked here, not everything inside `upload_dir`.
        nome_salvato = esito["files"][0]["name"]
        self.assertEqual(Path(nome_salvato).suffix, ".xlsx")
        self.assertTrue((self.upload_dir / nome_salvato).is_file())

    def test_un_documento_illeggibile_lo_dice_in_italiano_e_non_lascia_copie(self) -> None:
        with self.assertRaises(ValueError) as errore:
            self.carica("listino.xlsx", b"\x00\x01\x02 niente di leggibile")

        messaggio = str(errore.exception)
        self.assertIn("«listino.xlsx»", messaggio)
        self.assertIn("non è stato riconosciuto", messaggio)
        self.assertIn("Noce manda un .xls", messaggio)
        self.assertFalse(any(self.upload_dir.iterdir()))

    def test_un_documento_con_l_estensione_bugiarda_viene_letto_per_il_contenuto(self) -> None:
        """An .xls renamed to .csv is still read, but this must be reported: the columns won't be the expected ones."""
        listino = pipeline.scrivi_listino_noce(
            self.root / "vero.xls", pipeline.RIGHE_DI_PROVA, pipeline.RIGHE_IN_CODA,
        )

        esito = self.carica("listino_noce.csv", listino.read_bytes())

        self.assertEqual(esito["files"][0]["contentFormat"], "xls")
        self.assertIn("«listino_noce.csv» dentro è un Excel 97-2003 (.xls)", esito["message"])
        self.assertIn("letto per quello che è", esito["message"])

    def test_un_fornitore_non_letto_diventa_un_avviso_e_non_toglie_gli_altri(self) -> None:
        """A catalog that silently thins out is worse than one that fails to load."""
        rotto = self.root / "larice.xlsx"
        rotto.write_bytes(b"questo non e' un foglio di calcolo")
        cipresso = self.root / "cipresso.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Listino al 10-08-2026"
        sheet.append(["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"])
        sheet.append(["E-001", "TOVAGLIOLI CIPRESSO", "PZ", 6, 1.25, "8000000000098", None])
        workbook.save(cipresso)
        workbook.close()
        review = synthetic_review()
        review["files"] = [
            {"supplierId": "larice", "sourcePath": str(rotto), "role": "supplier"},
            {
                "supplierId": "cipresso",
                "sourcePath": str(cipresso),
                "role": "supplier",
                "fieldMapping": {
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
                },
            },
        ]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        prodotta = store.review()

        avvisi = [item for item in prodotta["warnings"] if item.get("code") == "CATALOGO_FORNITORE_NON_LETTO"]
        self.assertEqual(len(avvisi), 1)
        self.assertIn("LARICE", avvisi[0]["title"])
        self.assertIn("larice.xlsx", avvisi[0]["message"])
        self.assertIn("Gli altri fornitori restano nel confronto", avvisi[0]["message"])
        self.assertFalse(avvisi[0]["blocking"])
        # The same file isn't flagged twice with different wording.
        doppioni = [item for item in prodotta["warnings"] if item.get("code") == "CONDIZIONI_COMMERCIALI_NON_LETTE"]
        self.assertEqual(doppioni, [])
        # And the other supplier stayed in.
        self.assertEqual(store.search_products("tovaglioli")["count"], 1)

    def test_le_condizioni_di_un_listino_rovinato_non_fanno_fallire_la_pagina(self) -> None:
        """The Larice threshold reader had no error handling: a corrupted price
        list was enough to make `GET /api/review` stop responding.
        """
        rotto = self.root / "larice.xlsx"
        rotto.write_bytes(b"questo non e' un foglio di calcolo")
        servizio = SERVER.PromotionService()
        review = {"files": [{"supplierId": "larice", "sourcePath": str(rotto)}], "products": []}

        self.assertEqual(servizio.detect(review), [])
        self.assertEqual(len(servizio.load_errors), 1)
        self.assertIn("larice.xlsx", servizio.load_errors[0]["message"])
        self.assertEqual(servizio.load_errors[0]["supplier"], "larice")

        # The second read doesn't reopen the file, but still reports the reason.
        self.assertEqual(servizio.detect(review), [])
        self.assertEqual(len(servizio.load_errors), 1)

    def test_gli_avvisi_del_catalogo_non_si_accumulano_a_ogni_lettura(self) -> None:
        rotto = self.root / "larice.xlsx"
        rotto.write_bytes(b"questo non e' un foglio di calcolo")
        review = synthetic_review()
        review["files"] = [{"supplierId": "larice", "sourcePath": str(rotto), "role": "supplier"}]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        codici = [
            [item.get("code") for item in store.review()["warnings"]]
            for _volta in range(3)
        ]

        self.assertEqual(codici[0].count("CATALOGO_FORNITORE_NON_LETTO"), 1)
        self.assertEqual(codici[0], codici[1])
        self.assertEqual(codici[1], codici[2])

    def test_un_documento_gia_presente_non_viene_dichiarato_acquisito(self) -> None:
        """Someone re-uploading a price list and reading "acquired" would think it was updated."""
        listino = pipeline.scrivi_listino_noce(
            self.root / "formattato_104233.xls", pipeline.RIGHE_DI_PROVA, pipeline.RIGHE_IN_CODA,
        )
        contenuto = listino.read_bytes()
        self.carica("formattato_104233.xls", contenuto)

        secondo = self.carica("formattato_104233.xls", contenuto)

        self.assertEqual(secondo["files"][0]["status"], "duplicate")
        self.assertIn("già presente", secondo["message"])
        self.assertNotIn("caricato e letto", secondo["message"])

    def test_un_listino_cancellato_dalla_cartella_si_ricarica(self) -> None:
        """'Already present' must mean the copy is actually there.

        The dead end this guards against: deleting the price lists from the
        app folder and then, trying to re-upload them, being told they're
        already present while not seeing them and being unable to compare.
        The profile registry can hold the record of a document that isn't on
        disk; the upload read it as a "duplicate" and wrote nothing, while
        the page (which already filters out profiles with no copy) showed no
        card to delete to get out of the deadlock.
        """

        contenuto = b"ean;prodotto\n8000000000001;PRIMO\n"
        self.carica("larice-nuovo.csv", contenuto)
        # Deleted from the file explorer, not from the app: the app doesn't know.
        (self.upload_dir / "larice-nuovo.csv").unlink()

        secondo = self.carica("larice-nuovo.csv", contenuto)

        self.assertEqual(secondo["files"][0]["status"], "profiled")
        self.assertIn("caricato e letto", secondo["message"])
        self.assertTrue((self.upload_dir / "larice-nuovo.csv").exists())
        profili = json.loads((self.upload_dir / "upload_profiles.json").read_text(encoding="utf-8"))
        nomi = [item.get("file_name") for item in profili["profiles"]]
        # One copy, one record: two records for the same path would be two
        # different truths about the same file.
        self.assertEqual(nomi.count("larice-nuovo.csv"), 1)
        # And the page sees it again: proof the loop closes.
        self.assertTrue(any(
            item.get("uploadName") == "larice-nuovo.csv" for item in self.store.review()["files"]
        ))

    def test_una_scheda_che_parla_di_un_altro_file_non_vale_per_questo(self) -> None:
        """A profile declares a path: if this isn't that copy, it isn't its record.

        Guards against a counter-proof that stayed green on the first pass:
        without the comparison between the declared path and the actual
        copy, the tests never noticed, because they all went through the
        "the file isn't there" branch, which stops early. This is the case
        that comparison is meant to catch: a record pointing at the original
        on the Desktop while the uploads folder has a file with the same name.
        """

        contenuto = b"ean;prodotto\n8000000000001;PRIMO\n"
        altrove = self.root / "altrove"
        altrove.mkdir(exist_ok=True)
        (altrove / "listino.csv").write_bytes(contenuto)
        (self.upload_dir / "listino.csv").write_bytes(contenuto)
        (self.upload_dir / "upload_profiles.json").write_text(json.dumps({
            "schema_version": 1,
            "profiles": [{
                "file_name": "listino.csv",
                "path": str(altrove / "listino.csv"),
                "sha256": hashlib.sha256(contenuto).hexdigest(),
                "upload_role": "supplier",
            }],
            "errors": [],
        }), encoding="utf-8")

        esito = self.carica("listino.csv", contenuto)

        self.assertEqual(esito["files"][0]["status"], "profiled")
        profili = json.loads((self.upload_dir / "upload_profiles.json").read_text(encoding="utf-8"))
        percorsi = {item.get("path") for item in profili["profiles"]}
        self.assertNotIn(str(altrove / "listino.csv"), percorsi)

    def test_la_scheda_di_un_documento_sparito_esce_dal_registro(self) -> None:
        """The registry describes the folder, and the folder is the only source of truth."""

        self.carica("larice-nuovo.csv", b"ean;prodotto\n8000000000001;PRIMO\n")
        (self.upload_dir / "larice-nuovo.csv").unlink()

        self.carica("betulla-nuovo.csv", b"ean;prodotto\n8000000000002;SECONDO\n")

        profili = json.loads((self.upload_dir / "upload_profiles.json").read_text(encoding="utf-8"))
        nomi = {item.get("file_name") for item in profili["profiles"]}
        self.assertNotIn("larice-nuovo.csv", nomi)
        self.assertIn("betulla-nuovo.csv", nomi)

    def test_un_listino_caricato_si_puo_eliminare_senza_cambiare_il_confronto_attivo(self) -> None:
        self.carica("larice-nuovo.csv", b"ean;prodotto\n8000000000001;PRIMO\n")
        prima = self.store.review()
        self.assertTrue(any(item.get("uploadName") == "larice-nuovo.csv" for item in prima["files"]))

        result = self.store.delete_upload({"name": "larice-nuovo.csv"})

        self.assertTrue(result["ok"])
        self.assertFalse((self.upload_dir / "larice-nuovo.csv").exists())
        profiles = json.loads((self.upload_dir / "upload_profiles.json").read_text(encoding="utf-8"))
        self.assertNotIn("larice-nuovo.csv", {item.get("file_name") for item in profiles["profiles"]})
        after = self.store.review()
        self.assertFalse(any(item.get("uploadName") == "larice-nuovo.csv" for item in after["files"]))
        self.assertEqual(
            [item["id"] for item in after["products"]],
            [item["id"] for item in prima["products"]],
        )

    def test_eliminare_il_listino_toglie_dal_confronto_le_offerte_di_quel_fornitore(self) -> None:
        """'Deleting a price list still kept it showing up in the comparison.'

        Deletion removed the file's record but left everything else intact:
        the supplier stayed among every product's offers, stayed the
        selected one, and stayed in the summary's euro total.

        Offers don't disappear (they stay visible, so the user understands
        why the previous price is gone) but they stop being orderable, and
        the reason is stated.
        """

        self.carica("larice.csv", b"ean;prodotto\n8000000000001;PRIMO\n")
        confronto = synthetic_review()
        confronto["files"] = [{
            "name": "larice.csv",
            "supplierId": "larice",
            "supplier": "LARICE",
            "role": "supplier",
            "kind": "Listino",
            "sourcePath": str(self.upload_dir / "larice.csv"),
        }]
        self.review_path.write_text(json.dumps(confronto), encoding="utf-8")

        prima = [
            offerta for prodotto in self.store.review()["products"]
            for offerta in prodotto["offers"] if offerta["supplierId"] == "larice"
        ]
        self.assertTrue(prima, "la prova ha senso solo se LARICE è nel confronto")
        self.assertTrue(all(offerta["available"] for offerta in prima))

        self.store.delete_upload({"name": "larice.csv"})

        dopo_review = self.store.review()
        dopo = [
            offerta for prodotto in dopo_review["products"]
            for offerta in prodotto["offers"] if offerta["supplierId"] == "larice"
        ]
        self.assertEqual(len(dopo), len(prima), "le offerte restano visibili")
        self.assertFalse(
            any(offerta["available"] for offerta in dopo),
            "un listino eliminato non si può più ordinare",
        )
        self.assertTrue(all("eliminato" in (offerta.get("warning") or "") for offerta in dopo))
        avvisi = [
            avviso for avviso in dopo_review.get("warnings") or []
            if avviso.get("code") == "LISTINO_ELIMINATO"
        ]
        self.assertEqual(len(avvisi), 1, avvisi)
        self.assertIn("LARICE", avvisi[0]["message"])

    def test_un_fornitore_mai_stato_nell_elenco_resta_ordinabile(self) -> None:
        """'Unknown' is not 'removed', and only the latter disables offers.

        A real comparison can have suppliers with offers but no matching
        document in the file list: if being absent from the list were enough
        to disable a supplier, opening the program would find everything
        non-orderable without anyone having deleted anything.
        """

        confronto = synthetic_review()
        confronto["files"] = []
        self.review_path.write_text(json.dumps(confronto), encoding="utf-8")

        review = self.store.review()
        offerte = [
            offerta for prodotto in review["products"]
            for offerta in prodotto["offers"] if offerta["supplierId"] == "larice"
        ]
        self.assertTrue(offerte)
        self.assertTrue(all(offerta["available"] for offerta in offerte))
        self.assertFalse([
            avviso for avviso in review.get("warnings") or []
            if avviso.get("code") == "LISTINO_ELIMINATO"
        ])

    def test_eliminare_un_file_spegne_l_anteprima_della_run_che_lo_chiedeva(self) -> None:
        self.carica("betulla.csv", b"ean;prodotto\n8000000000001;PRIMO\n")
        stato = self.store.pipeline_jobs._stato_in_attesa()
        stato.update({
            "ok": False,
            "stato": "ERRORE",
            "runId": "run-vecchia",
            "fermata": {"code": "SCHEMA_SCONOSCIUTO", "documenti": ["betulla.csv"]},
        })
        self.store.pipeline_jobs._stato = stato
        self.store.pipeline_jobs._salva_stato()

        esito = self.store.delete_upload({"name": "betulla.csv"})

        self.assertEqual(esito["pipeline"]["stato"], "IN_ATTESA")
        self.assertIsNone(esito["pipeline"]["fermata"])
        with self.assertRaisesRegex(ValueError, "Non ci sono documenti"):
            self.store.schemi_pendenti()

    def test_un_nuovo_caricamento_spegne_l_anteprima_della_run_precedente(self) -> None:
        stato = self.store.pipeline_jobs._stato_in_attesa()
        stato.update({
            "ok": False,
            "stato": "ERRORE",
            "runId": "run-vecchia",
            "fermata": {"code": "SCHEMA_SCONOSCIUTO", "documenti": ["vecchio.xlsx"]},
        })
        self.store.pipeline_jobs._stato = stato
        self.store.pipeline_jobs._salva_stato()

        esito = self.carica("nuovo.csv", b"ean;prodotto\n8000000000001;PRIMO\n")

        self.assertEqual(esito["pipeline"]["stato"], "IN_ATTESA")
        self.assertIsNone(esito["pipeline"]["fermata"])

    def test_un_listino_attivo_esterno_si_rimuove_dall_app_senza_cancellare_l_originale(self) -> None:
        originale = self.root / "larice-settimana-scorsa.xlsx"
        originale.write_bytes(b"originale fuori dagli upload")
        review = synthetic_review()
        review["files"] = [{
            "name": originale.name,
            "role": "supplier",
            "supplier": "LARICE",
            "sourcePath": str(originale),
        }]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")

        prima = self.store.review()
        self.assertTrue(prima["files"][0]["deletable"])
        esito = self.store.delete_upload({"name": originale.name})

        self.assertTrue(esito["ok"])
        self.assertIn("originale non è stato cancellato", esito["message"])
        self.assertTrue(originale.exists())
        dopo = self.store.review()
        self.assertEqual(dopo["files"], [])
        # Products stay: they aren't pruned from the comparison, and nothing
        # asks for that.
        self.assertEqual(len(dopo["products"]), len(review["products"]))
        # But the deleted supplier's offers stop being orderable. The record
        # here declares only `supplier`, not `supplierId`: that's the shape
        # older comparisons have, which is why the key is derived from both.
        offerte = [
            offerta for prodotto in dopo["products"]
            for offerta in prodotto["offers"] if offerta["supplierId"] == "larice"
        ]
        self.assertTrue(offerte)
        self.assertFalse(any(offerta["available"] for offerta in offerte))
        self.assertTrue(any(
            avviso.get("code") == "LISTINO_ELIMINATO" for avviso in dopo.get("warnings") or []
        ))

    def test_la_copia_caricata_e_la_vecchia_scheda_con_lo_stesso_nome_spariscono_insieme(self) -> None:
        nome = "betulla.xlsx"
        originale = self.root / nome
        originale.write_bytes(b"vecchio originale")
        review = synthetic_review()
        review["files"] = [{
            "name": nome,
            "role": "supplier",
            "supplier": "BETULLA",
            "sourcePath": str(originale),
        }]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        self.carica(nome, b"ean;prodotto\n8000000000001;PRIMO\n")

        schede = [voce for voce in self.store.review()["files"] if voce.get("name") == nome]
        self.assertEqual(len(schede), 1)
        self.assertTrue(schede[0]["deletable"])

        self.store.delete_upload({"name": nome})

        self.assertTrue(originale.exists())
        self.assertFalse((self.upload_dir / nome).exists())
        self.assertFalse(any(voce.get("name") == nome for voce in self.store.review()["files"]))

    def test_un_gestionale_attivo_esterno_si_rimuove_senza_cancellare_l_originale(self) -> None:
        originale = self.root / "gestionale.xlsx"
        originale.write_bytes(b"gestionale")
        review = synthetic_review()
        review["files"] = [{
            "name": originale.name,
            "role": "master",
            "sourcePath": str(originale),
        }]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")

        self.assertTrue(self.store.review()["files"][0]["deletable"])
        esito = self.store.delete_upload({"name": originale.name})
        self.assertIn("Elenco rimosso", esito["message"])
        self.assertTrue(originale.exists())
        dopo = self.store.review()
        self.assertEqual(dopo["files"], [])
        # Deleting the management export is intended (it's needed when the
        # wrong one was loaded), but the comparison's products come from it
        # and stay on the page with "Continue" active. The consequence must
        # be stated, otherwise the week's orders get prepared against a list
        # missing from the program.
        self.assertEqual(len(dopo["products"]), len(review["products"]))
        avvisi = [
            avviso for avviso in dopo.get("warnings") or []
            if avviso.get("code") == "GESTIONALE_ELIMINATO"
        ]
        self.assertEqual(len(avvisi), 1, avvisi)
        self.assertIn("rifai il confronto", avvisi[0]["message"])

    def test_il_box_gestionale_mette_l_export_nel_riquadro_giusto_e_lo_rende_eliminabile(self) -> None:
        self.carica(
            "gestionale.csv",
            b"ean;prodotto\n8000000000001;PRIMO\n",
            role="management",
        )

        scheda = next(voce for voce in self.store.review()["files"] if voce.get("name") == "gestionale.csv")
        self.assertEqual(scheda["role"], "master")
        self.assertEqual(scheda["kind"], "Gestionale")
        self.assertEqual(scheda["supplier"], "Gestionale")
        self.assertTrue(scheda["deletable"])

        esito = self.store.delete_upload({"name": "gestionale.csv"})
        self.assertIn("Elenco eliminato", esito["message"])
        self.assertFalse((self.upload_dir / "gestionale.csv").exists())

    def test_ricaricare_lo_stesso_file_nel_box_gestionale_ne_corregge_la_destinazione(self) -> None:
        contenuto = b"ean;prodotto\n8000000000001;PRIMO\n"
        self.carica("export.csv", contenuto, role="suppliers")

        self.carica("export.csv", contenuto, role="management")

        profili = json.loads(self.store.upload_profiles_path.read_text(encoding="utf-8"))["profiles"]
        self.assertEqual(len(profili), 1)
        self.assertEqual(profili[0]["upload_role"], "master")
        scheda = next(voce for voce in self.store.review()["files"] if voce.get("name") == "export.csv")
        self.assertEqual(scheda["role"], "master")
        self.assertEqual(scheda["kind"], "Gestionale")

    def test_un_nome_gia_usato_lo_dice_invece_di_creare_una_copia_muta(self) -> None:
        self.carica("larice.csv", b"ean;prodotto\n8000000000001;PRIMO\n")

        secondo = self.carica("larice.csv", b"ean;prodotto\n8000000000002;SECONDO\n")

        self.assertEqual(secondo["files"][0]["name"], "larice (2).csv")
        self.assertIn("«larice.csv» era già presente: salvato come «larice (2).csv»", secondo["message"])

    def test_un_documento_vuoto_lo_dice_invece_di_parlare_del_limite(self) -> None:
        with self.assertRaises(ValueError) as errore:
            self.carica("vuoto.xls", b"")

        self.assertIn("è vuoto", str(errore.exception))
        self.assertNotIn("60 MB", str(errore.exception))

    def test_se_un_documento_del_gruppo_e_rotto_gli_altri_non_restano_sul_disco(self) -> None:
        """Copies the program doesn't know about pile up unseen."""
        buono = base64.b64encode(b"ean;prodotto\n8000000000001;PRIMO\n").decode("ascii")
        cattivo = base64.b64encode(b"\x00\x01\x02 spazzatura").decode("ascii")

        with self.assertRaises(ValueError):
            self.store.upload({"files": [
                {"name": "buono.csv", "data": buono},
                {"name": "cattivo.xlsx", "data": cattivo},
            ]})

        self.assertEqual(sorted(item.name for item in self.upload_dir.iterdir()), [])

    def test_un_documento_col_nome_giusto_non_viene_accusato_di_estensione_bugiarda(self) -> None:
        listino = pipeline.scrivi_listino_noce(
            self.root / "formattato_104233.xls", pipeline.RIGHE_DI_PROVA, pipeline.RIGHE_IN_CODA,
        )

        esito = self.carica("formattato_104233.xls", listino.read_bytes())

        self.assertNotIn("dentro è", esito["message"])

    def test_quando_anche_le_offerte_di_un_fornitore_saltano_l_avviso_lo_dice(self) -> None:
        rotto = self.root / "larice.xlsx"
        rotto.write_bytes(b"questo non e' un foglio di calcolo")
        review = synthetic_review()
        review["files"] = [{"supplierId": "larice", "sourcePath": str(rotto), "role": "supplier"}]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

        avvisi = [item for item in store.review()["warnings"] if item.get("supplier") == "larice"]

        self.assertEqual(len(avvisi), 1)
        self.assertIn("Mancano anche le sue soglie con omaggio.", avvisi[0]["message"])
        self.assertIn("ricarica il listino dal passo 1", avvisi[0]["message"])

    def test_le_offerte_non_lette_di_un_fornitore_leggibile_hanno_un_avviso_proprio(self) -> None:
        """The catalog reads the price list, the promotion engine doesn't: two different failures."""
        store = self.store
        servizio = store.promotion_service

        def detect_finto(review: dict[str, Any]) -> list[dict[str, Any]]:
            servizio.load_errors = [{
                "supplier": "cipresso",
                "supplierName": "CIPRESSO",
                "message": "Le condizioni commerciali del listino CIPRESSO «cipresso.xlsx» non sono state lette: prova",
            }]
            return []

        servizio.detect = detect_finto

        avvisi = [item for item in store.review()["warnings"] if item.get("code") == "CONDIZIONI_COMMERCIALI_NON_LETTE"]

        self.assertEqual(len(avvisi), 1)
        self.assertEqual(avvisi[0]["supplier"], "cipresso")
        self.assertFalse(avvisi[0]["blocking"])
        self.assertIn("Offerte CIPRESSO non lette", avvisi[0]["title"])
        self.assertIn("mancano le soglie con omaggio", avvisi[0]["message"])

    def test_se_il_motore_delle_promozioni_esplode_la_pagina_si_apre_lo_stesso(self) -> None:
        def decorate_che_esplode(review: dict[str, Any], selections: Any = None) -> dict[str, Any]:
            raise RuntimeError("motore delle promozioni rotto")

        self.store.promotion_service.decorate = decorate_che_esplode

        prodotta = self.store.review()

        codici = [item.get("code") for item in prodotta["warnings"]]
        self.assertIn("PROMOZIONI_NON_CALCOLATE", codici)
        self.assertTrue(prodotta["products"])
        self.assertEqual(self.store.search_products("prodotto standard")["ok"], True)


class LaFasciaDeiDocumentiCambiatiSiAccende(unittest.TestCase):
    """The changed-documents banner existed and the page could read it, but nothing wrote it.

    `cambiamentoDocumenti` reads `pipeline.stato.cambiamento` and had five
    tests covering it, but nothing wrote that field: `input_modificato` only
    set a message, and the message doesn't render on the page (the phase bar
    stays silent on IN_ATTESA, on purpose). Deleting last week's price list
    and uploading the new one left pages 2 and 3 showing the old prices with
    no indication of that.

    A repo-wide search found the string `"cambiamento"` only in test files:
    the tests proved the reader worked, none proved anything fed it.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.review_path = self.root / "review_data.json"
        self.state_path = self.root / "run" / "state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir = self.root / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.review_path.write_text(json.dumps(synthetic_review()), encoding="utf-8")
        self.store = ReviewStore(
            self.review_path, self.state_path, self.upload_dir, self.root / "outputs",
        )
        self.addCleanup(self.store.chiudi)

    def carica(self, nome: str, contenuto: bytes, ruolo: str = "suppliers") -> dict:
        return self.store.upload({"files": [{
            "name": nome, "role": ruolo,
            "data": base64.b64encode(contenuto).decode("ascii"),
        }]})

    def test_caricare_un_documento_lo_dichiara(self) -> None:
        esito = self.carica("listino-nuovo.csv", b"ean;prodotto\n8000000000001;PRIMO\n")

        cambiamento = esito["pipeline"].get("cambiamento") or {}
        self.assertEqual(cambiamento.get("tipo"), "caricato")
        self.assertIn("listino-nuovo.csv", cambiamento.get("documenti") or [])

    def test_l_elenco_del_gestionale_non_e_un_fornitore_che_si_chiama_FORNITORE(self) -> None:
        """The management export isn't a supplier named FORNITORE.

        Uploading the export together with a price list, the banner read
        "You uploaded price lists BETULLA and FORNITORE since the last
        comparison". The management export has an adapter like any other
        (`gestionale_v1`) but no `supplier_id`, and `supplier_label("")`
        returns "FORNITORE", the registry's generic fallback. A guard on
        `if etichetta` wasn't enough: "FORNITORE" is a non-empty string.
        """

        percorso = self.root / "gestionale.xlsx"
        libro = Workbook()
        foglio = libro.active
        foglio.title = "Foglio1"
        foglio.append(["DOCUMENTO N. 1"])
        foglio.append([None, "Codice", "Cod. Int.", "Descrizione", "UM", "Colli",
                       "Quantità", "Prezzo", "Sconto", "IVA", "Totale"])
        foglio.append(["C", "8000000000011", "A-1", "PASTA", "PZ", 2, 12, 0.89, 0, 22, 10.68])
        libro.save(percorso)
        libro.close()

        esito = self.carica("gestionale.xlsx", percorso.read_bytes(), ruolo="management")

        cambiamento = esito["pipeline"].get("cambiamento") or {}
        self.assertIn("gestionale.xlsx", cambiamento.get("documenti") or [])
        # The document is named; the invented supplier is not.
        self.assertEqual(cambiamento.get("fornitori") or [], [])

    def test_eliminarne_uno_lo_dichiara(self) -> None:
        self.carica("listino-vecchio.csv", b"ean;prodotto\n8000000000001;PRIMO\n")

        esito = self.store.delete_upload({"name": "listino-vecchio.csv"})

        cambiamento = esito["pipeline"].get("cambiamento") or {}
        self.assertEqual(cambiamento.get("tipo"), "eliminato")
        self.assertIn("listino-vecchio.csv", cambiamento.get("documenti") or [])

    def test_e_il_campo_finisce_sul_disco_cosi_sopravvive_al_riavvio(self) -> None:
        """Guards against reading in-memory state instead of disk.

        A version of this test that read the state from memory stayed green
        even without persisting it, but a program restart would have made
        the banner disappear. This checks the file, which is what the
        service re-reads on startup.
        """

        self.carica("listino-nuovo.csv", b"ean;prodotto\n8000000000001;PRIMO\n")

        sul_disco = json.loads(
            (self.state_path.parent / "pipeline_status.json").read_text(encoding="utf-8")
        )

        self.assertEqual((sul_disco.get("cambiamento") or {}).get("tipo"), "caricato")
        self.assertIn(
            "listino-nuovo.csv", (sul_disco.get("cambiamento") or {}).get("documenti") or [],
        )


class AvvisiDeiDocumentiTests(unittest.TestCase):
    """A missing supplier must be reported before the user picks it, not after.

    Review warnings must reach step 2, not only the step-3 panel: someone
    choosing products and suppliers there needs to know when a price list
    has been left out of the comparison.

    These are substring tests: they check the source, not the behavior. The
    real rule — which warnings go in the panel and which stay on the
    product card — is checked by `tests/test_interfaccia_pagina1.py`, which
    runs `app.js` under Node. This only checks the wiring: the panel exists
    and reaches both pages.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.app_js, f"manca la funzione {name}")
        return self.app_js.split(marker)[1].split("\n}\n")[0]

    def test_gli_avvisi_dei_documenti_hanno_un_riquadro_proprio(self) -> None:
        # Which warnings to show lives in `avvisiDelConfronto`, which is also
        # where the recalc panel finds which warnings are already printed so
        # it doesn't repeat them.
        selezione = self.body("avvisiDelConfronto")
        self.assertIn("state.review?.warnings", selezione)
        # Warnings tied to a product stay on the product card.
        self.assertIn("!issue.productId", selezione)
        corpo = self.body("renderSourceWarnings")
        self.assertIn("avvisiDelConfronto()", corpo)
        # The panel must go through `renderAvvisi` rather than calling
        # `renderAlert` directly: `renderAvvisi` prints each entry and
        # counts repeats. That warnings actually reach the page is checked
        # by `test_interfaccia_pagina1.py`.
        self.assertIn("renderAvvisi(warnings", corpo)

    def test_ogni_pagina_chiede_i_suoi_avvisi(self) -> None:
        """Each page must show its own warning set, not a shared panel.

        Each page states which set it wants: 1 for documents, 2 for
        products. That the split is the right one is checked by
        `test_interfaccia_pagina1.py`.
        """

        self.assertIn("renderSourceWarnings(1)", self.body("renderUploadStep"))
        self.assertIn("renderSourceWarnings(2)", self.body("renderQuantityStep"))


class LaReidratazioneDelRicalcoloTests(unittest.TestCase):
    """Reloading the page must recover the recalc outcome, not just the progress bar.

    The service keeps the final state (in `pipeline_status.json`, re-read
    after a restart too); it was the page that discarded it, because on
    startup it only resumed a recalc still `IN_CORSO`. A recalc that
    stopped partway, or finished with warnings, disappeared on the next
    reload, including the warning that says the compile needs re-checking
    before creating copies.

    These are assertions on the source text, as in `ConsegnaInterfacciaTests`:
    the one thing a service-level test can't see is the page.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.app_js, f"manca la funzione {name}")
        return self.app_js.split(marker)[1].split("\n}\n")[0]

    def reidratazione(self) -> str:
        """The code that runs when the page opens, after `loadReview()`."""

        marker = "loadReview();"
        self.assertIn(marker, self.app_js)
        blocco = self.app_js.split(marker)[-1]
        self.assertIn("API.pipelineStato", blocco)
        return blocco

    def callback_di_reidratazione(self) -> list[str]:
        """The CODE lines of the callback, with quotes normalized.

        Asserting on exact source text (like the old guard's literal
        `!== "IN_CORSO"`) is fragile: a harmless rewrite using single quotes
        instead of double quotes would pass three false assertions. This
        asserts on structure instead: quotes are normalized, comments don't
        count, and the rules are "exactly one return, the IN_ATTESA one" and
        "IN_CORSO appears exactly once, guarding the polling" — any way of
        discarding finished outcomes needs either a second return or a
        second IN_CORSO.
        """

        blocco = self.reidratazione()
        self.assertIn(".then((stato) => {", blocco)
        callback = blocco.split(".then((stato) => {")[1].split("})")[0]
        righe = []
        for riga in callback.replace("'", '"').splitlines():
            codice = riga.split("//")[0].strip()
            if codice:
                righe.append(codice)
        return righe

    def test_all_apertura_si_riprende_anche_un_ricalcolo_finito(self) -> None:
        righe = self.callback_di_reidratazione()
        # Only one discard case, "nothing ever started": every other outcome
        # (stopped, warnings, success) gets put back on the page.
        con_return = [riga for riga in righe if "return" in riga]
        self.assertEqual(len(con_return), 1, con_return)
        self.assertIn('esito === "IN_ATTESA"', con_return[0])
        corpo = "\n".join(righe)
        self.assertIn("state.pipeline.stato = stato", corpo)
        self.assertIn("state.pipeline.chiesto = true", corpo)
        self.assertIn("render()", corpo)

    def test_si_continua_a_interrogare_solo_una_run_viva(self) -> None:
        """On a finished run, the timer would repeat the same answer forever."""

        righe = self.callback_di_reidratazione()
        con_in_corso = [riga for riga in righe if "IN_CORSO" in riga]
        # IN_CORSO appears exactly ONCE, guarding the polling. A second
        # occurrence means something is filtering outcomes again.
        self.assertEqual(len(con_in_corso), 1, con_in_corso)
        self.assertEqual(
            con_in_corso[0],
            'if (esito === "IN_CORSO") pianificaControlloPipeline();',
        )

    def test_il_riquadro_mostra_la_fermata_e_gli_avvisi(self) -> None:
        """The other half: recovering these is useless if they aren't shown."""

        corpo = self.body("renderAvanzamentoPipeline")
        self.assertIn("stato.fermata", corpo)
        self.assertIn("Il confronto si è fermato", corpo)
        self.assertIn("stato.avvisi", corpo)
        self.assertIn("renderAlert", corpo)


class PromotionPanelTests(unittest.TestCase):
    """Measured on real price lists: 188 conditions read, only 13 can change the order.

    175 of the 188 are discounts already folded into the per-piece price
    that selects the supplier. Mixing them in with the actionable ones is
    what makes a panel unusable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.app_js, f"manca la funzione {name}")
        return self.app_js.split(marker)[1].split("\n}\n")[0]

    def test_gia_nel_prezzo_si_riconosce_dal_contratto_del_servizio(self) -> None:
        corpo = self.body("promotionIsAlreadyInPrice")
        self.assertIn("economic_effect?.already_applied", corpo)

    def test_utile_vuol_dire_non_nel_prezzo_e_collegata_a_prodotti(self) -> None:
        """A condition with no matched products can't tell you how much is missing."""

        corpo = self.body("promotionIsActionable")
        self.assertIn("!promotionIsAlreadyInPrice(promotion)", corpo)
        self.assertIn("promotionMatchedProductIds(promotion).length > 0", corpo)

    def test_il_conteggio_dice_quante_sono_utili_non_solo_quante_sono(self) -> None:
        corpo = self.body("promotionCountsLabel")
        self.assertIn("lette", corpo)
        self.assertIn("utili", corpo)
        self.assertIn("nessuna collegata ai tuoi prodotti", corpo)

    def test_le_condizioni_gia_nel_prezzo_stanno_in_una_sezione_a_parte(self) -> None:
        corpo = self.body("renderPromotionCatalog")
        self.assertIn("promotionIsAlreadyInPrice", corpo)
        self.assertIn("promotion-catalog__included", corpo)
        self.assertIn("Già conteggiate nel prezzo", corpo)
        # The two lists must not be the same list.
        self.assertIn("!promotionIsAlreadyInPrice(promotion)", corpo)

    def test_prima_le_offerte_a_cui_manca_meno(self) -> None:
        corpo = self.body("sortPromotionsByReach")
        self.assertIn("promotionRemainingQty", corpo)
        self.assertIn("PROMOTION_STATUS_ORDER", corpo)

    def test_la_ricerca_esiste_e_il_riquadro_resta_aperto_mentre_si_scrive(self) -> None:
        self.assertIn("data-promotion-search", self.app_js)
        self.assertIn('data-focus-key="promotion-search"', self.app_js)
        # Without remembering whether it's open, the panel closes on every
        # re-render and the field vanishes mid-typing.
        self.assertIn("state.promotionCatalogOpen", self.app_js)
        self.assertIn('data-promotion-catalog ${state.promotionCatalogOpen ? "open" : ""}', self.app_js)
        # The <details> toggle event doesn't bubble: without the capture
        # phase the listener never fires and the open state is lost.
        toggle = self.app_js.split('appElement.addEventListener("toggle"')[1].split("\n\n")[0]
        self.assertTrue(toggle.rstrip().endswith("}, true);"), "il listener toggle deve essere in cattura")


class SupplierMoveInterfaceTests(unittest.TestCase):
    """Browser-side rules no server test can defend.

    The move calculation lives on the server and is covered by
    tests/test_supplier_move.py. Applying it, though, happens in the
    browser: three separate app.js mutations (inheriting the confirmation,
    rewriting the quantity, breaking undo) could slip past a fully green
    suite without this coverage.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.app_js, f"manca la funzione {name}")
        return self.app_js.split(marker)[1].split("\n}\n")[0]

    def test_lo_spostamento_cambia_il_fornitore_e_azzera_la_conferma(self) -> None:
        corpo = self.body("applySupplierMove")
        self.assertIn("product.selectedSupplierId = assignment.toSupplierId;", corpo)
        # A confirmation given for one offer doesn't apply to another: if
        # inherited, the order would go out without anyone having looked at
        # the product.
        self.assertIn("product.confirmed = false;", corpo)

    def test_lo_spostamento_non_tocca_quantita_esclusioni_e_origine(self) -> None:
        corpo = self.body("applySupplierMove")
        for vietato in ("product.quantity =", "product.excluded =", "product.quantitySource ="):
            self.assertNotIn(vietato, corpo, f"lo spostamento non deve scrivere {vietato.strip(' =')}")

    def test_l_annulla_ripristina_fornitore_e_conferma_di_prima(self) -> None:
        applica = self.body("applySupplierMove")
        # Restoring requires both values to have been saved first.
        self.assertIn("selectedSupplierId: product.selectedSupplierId,", applica)
        self.assertIn("confirmed: Boolean(product.confirmed),", applica)

        annulla = self.body("undoSupplierMove")
        self.assertIn("product.selectedSupplierId = entry.selectedSupplierId;", annulla)
        self.assertIn("product.confirmed = entry.confirmed;", annulla)

    def test_la_differenza_di_spesa_non_si_mostra_mai_da_sola(self) -> None:
        """Spending less while receiving less merchandise isn't a saving."""

        corpo = self.body("moveDeltaParts")
        self.assertIn("deltaPieces", corpo)
        # "si risparmia" (saving) must sit in a branch that already excludes
        # the case of receiving less merchandise.
        prima_del_risparmio = corpo.split('words: "si risparmia"')[0]
        self.assertIn("pieces < 0", prima_del_risparmio)
        self.assertIn("si spende meno, ma arriva meno merce", corpo)

    def test_le_soglie_distinguono_chi_un_ordine_non_ce_l_aveva(self) -> None:
        """A supplier starting from zero can't "drop below" the threshold."""

        corpo = self.body("moveThresholdNotes")
        # It's not enough for hadOrderBefore to appear somewhere: it must be
        # exactly the condition deciding whether the threshold was met before.
        self.assertIn("const reachedBefore = row.hadOrderBefore && row.meetsThresholdBefore;", corpo)
        self.assertIn("&& reachedBefore", corpo)
        self.assertNotIn("&& row.meetsThresholdBefore)", corpo)

    def test_i_motivi_di_chi_resta_indietro_sono_quelli_che_il_server_manda(self) -> None:
        tabella = self.app_js.split("const MOVE_LEFT_BEHIND_REASONS = {")[1].split("\n};")[0]
        server = (SKILL_ROOT / "app" / "server.py").read_text(encoding="utf-8")
        emessi = {"NESSUNA_OFFERTA", "PREZZO_NON_DISPONIBILE"}
        for codice in emessi:
            self.assertIn(f'return "{codice}"', server, f"il server non emette più {codice}")
            self.assertIn(codice, tabella, f"il browser non sa tradurre {codice}")

    def test_la_conferma_richiesta_dall_offerta_ha_una_casella_per_darla(self) -> None:
        """Otherwise the order can't be saved and there's no way to unblock it."""

        corpo = self.body("renderConfirmation")
        self.assertIn("confirmationRequired(product)", corpo)
        regola = self.body("confirmationRequired")
        self.assertIn("selectedOffer(product)", regola)
        self.assertIn("offer.requiresConfirmation", regola)
        # With no supplier selected there's nothing to confirm: the same
        # boundary the service applies. Without this, rejecting the only
        # offer left the "Confirmation required" blocker up on a product
        # with no checkbox anywhere to clear it.
        self.assertIn("if (!offer) return false;", regola)
        # The offer must carry this field over from the local service.
        self.assertIn("requiresConfirmation: Boolean(offer.requiresConfirmation", self.app_js)


class QuantityWheelGuardTests(unittest.TestCase):
    """The mouse wheel must not be able to change an order quantity."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def guard(self) -> str:
        marker = 'appElement.addEventListener("wheel"'
        self.assertIn(marker, self.app_js, "manca la protezione contro la rotellina sui campi numerici")
        return self.app_js.split(marker)[1].split("});")[0]

    def test_the_wheel_is_neutralised_on_a_focused_number_field(self) -> None:
        guard = self.guard()
        self.assertIn('input[type="number"]', guard)
        self.assertIn("document.activeElement !== field", guard)
        self.assertIn("event.preventDefault()", guard)
        self.assertIn("field.blur()", guard)

    def test_the_listener_can_actually_cancel_the_event(self) -> None:
        # Without { passive: false }, preventDefault() is ignored and the
        # guard protects nothing.
        coda = self.app_js.split('appElement.addEventListener("wheel"')[1].split("\n\n")[0]
        self.assertIn("passive: false", coda)


class InterfacciaR5Tests(unittest.TestCase):
    """UI review-round changes must stay short actions, not new walls of text."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        cls.css = (SKILL_ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.app_js)
        return self.app_js.split(marker, 1)[1].split("\n}\n", 1)[0]

    def test_il_candidato_scartato_ha_nome_e_due_risposte(self) -> None:
        corpo = self.body("renderProductIssues")
        self.assertIn("candidate.description", corpo)
        self.assertIn('data-action="answer-candidate"', corpo)
        self.assertIn('data-accepted="true"', corpo)
        self.assertIn('data-accepted="false"', corpo)
        self.assertIn("Perché compare?", corpo)
        self.assertNotIn("candidate.score", corpo)

    def test_la_conferma_semantica_mostra_entrambi_i_nomi(self) -> None:
        corpo = self.body("renderConfirmation")
        self.assertIn("Richiesto:", corpo)
        self.assertIn("product.name", corpo)
        self.assertIn("offer.description", corpo)
        self.assertIn("propone:", corpo)
        # The reason must not be hidden behind a collapsible: it's the one
        # thing needed to decide.
        self.assertNotIn("Perché serve?", corpo)
        self.assertNotIn("<details", corpo)
        self.assertIn("confirmationMessageFor(product)", corpo)

    def test_il_confronto_non_ripete_il_calcolo_ovvio_ne_etichetta_la_scelta(self) -> None:
        confronto = self.body("renderOfferGrid")
        scheda = self.body("renderCompactProduct")
        self.assertNotIn("offer-card__fulfillment", confronto)
        self.assertNotIn("Più conveniente al pezzo", confronto)
        self.assertNotIn("saving-hint", scheda)

    def test_i_totali_per_fornitore_restano_in_una_fascia_sottile(self) -> None:
        pagina = self.body("renderQuantityStep")
        fascia = self.body("renderStickySupplierTotals")
        self.assertIn("renderStickySupplierTotals()", pagina)
        self.assertIn("supplierTotals()", fascia)
        self.assertIn("position: sticky", self.css.split(".supplier-totals-strip", 1)[1].split("}", 1)[0])

    def test_il_riepilogo_ha_piu_meno_rimuovi_ed_ean_del_fornitore(self) -> None:
        controlli = self.body("renderSummaryQuantityControls")
        riepilogo_fornitore = self.body("renderSupplierSummary")
        riepilogo_prodotto = self.body("renderProductSummary")
        self.assertIn('data-delta="-1"', controlli)
        self.assertIn('data-delta="1"', controlli)
        self.assertIn('data-action="summary-remove-product"', controlli)
        self.assertIn("EAN fornitore:", riepilogo_fornitore)
        self.assertIn("EAN fornitore:", riepilogo_prodotto)

    def test_compilazioni_e_listini_hanno_cancellazione_individuale(self) -> None:
        self.assertIn('data-action="ask-delete-upload"', self.body("renderFileCard"))
        compilazioni = self.body("renderCompilazioniPrecedenti")
        self.assertIn("<details", compilazioni)
        self.assertIn("data-compilazioni", compilazioni)
        self.assertIn('data-action="ask-delete-compilation"', self.body("renderCompilazione"))
        # The confirmation states what is lost, not the program's internal
        # name for it.
        conferma = self.body("renderCompilazione")
        self.assertNotIn("e i suoi promemoria", conferma)
        self.assertIn("è arrivata la merce?", conferma)
        self.assertIn("Non si può annullare", conferma)

    def test_l_attesa_di_autosalvataggio_resta_450_millisecondi(self) -> None:
        self.assertIn("window.setTimeout(() => saveState(), 450)", self.body("scheduleSave"))


class LaCompilabilitaVieneDalRegistroTests(unittest.TestCase):
    """Which suppliers are compilable must come from the registry, not a hardcoded tuple.

    Hardcoding it in `launcher.py` meant a learned supplier could never
    become compilable, silently. Measured effect: 102 products assigned to
    ACERO with no warning raised.
    """

    def setUp(self) -> None:
        launcher_path = SKILL_ROOT / "app" / "launcher.py"
        spec = importlib.util.spec_from_file_location("compara_ordini_launcher_registro_test", launcher_path)
        if spec is None or spec.loader is None:
            raise AssertionError("Impossibile importare il launcher")
        self.launcher = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.launcher
        spec.loader.exec_module(self.launcher)
        self.root = Path(tempfile.mkdtemp(prefix="launcher-registro-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def registro(self, *voci: dict[str, Any]) -> Path:
        percorso = self.root / "adapters.json"
        percorso.write_bytes((json.dumps({"schema_version": 1, "adapters": list(voci)},
                                         ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return percorso

    @staticmethod
    def adattatore_imparato(*, con_scrittura: bool) -> dict[str, Any]:
        voce: dict[str, Any] = {
            "id": "acero_v1", "schema_version": 1, "kind": "supplier",
            "supplier_id": "acero", "display_name": "ACERO",
            "header_signature": {"kind": "headers", "required": ["codean"], "known": ["codean"]},
            "field_mapping": {"sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                              "columns": {"ean": "COD.EAN"}, "order_column": "C"},
        }
        if con_scrittura:
            voce["order_write"] = {"sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                                   "order_column": "C", "expected_header": "ORDINE"}
        return voce

    def listino(self) -> Path:
        percorso = self.root / "acero.xlsx"
        libro = Workbook()
        foglio = libro.active
        foglio.title = "Listino"
        foglio.append(["COD.EAN", "DESCRIZIONE", "ORDINE"])
        foglio.append(["8000000000001", "PRODOTTO", None])
        libro.save(percorso)
        libro.close()
        return percorso

    def confronto(self) -> dict[str, Any]:
        return {"files": [{"supplierId": "acero", "adapterId": "acero_v1",
                           "role": "supplier", "sourcePath": str(self.listino())}]}

    def test_senza_regola_di_scrittura_il_fornitore_non_e_compilabile_e_lo_si_dice(self) -> None:
        percorso = self.registro(self.adattatore_imparato(con_scrittura=False))

        with mock.patch.object(self.launcher, "ADAPTERS_PATH", percorso):
            self.assertEqual(self.launcher.fornitori_compilabili(), ())
            _node, sorgenti, regole, _mancanti, avvisi = self.launcher.writer_readiness(self.confronto())

        self.assertEqual(sorgenti, {})
        self.assertEqual(regole, {})
        self.assertTrue(any("ACERO" in avviso and "non si può compilare" in avviso
                            for avviso in avvisi), avvisi)

    def test_basta_dichiarare_order_write_perche_diventi_compilabile(self) -> None:
        """No code changes needed: the rule lives in the registry, code just applies it."""

        percorso = self.registro(self.adattatore_imparato(con_scrittura=True))

        with mock.patch.object(self.launcher, "ADAPTERS_PATH", percorso):
            self.assertEqual(self.launcher.fornitori_compilabili(), ("acero",))
            _node, sorgenti, regole, _mancanti, avvisi = self.launcher.writer_readiness(self.confronto())

        self.assertIn("acero", sorgenti)
        self.assertEqual(regole["acero"]["order_column"], "C")
        self.assertEqual(regole["acero"]["sheet"], "Listino")
        self.assertEqual(regole["acero"]["expected_header"], "ORDINE")
        self.assertEqual(avvisi, [])

    def test_la_colonna_dichiarata_si_verifica_sul_documento(self) -> None:
        """If the declared header isn't there, the order would land somewhere else."""

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["order_column"] = "B"
        percorso = self.registro(voce)

        with mock.patch.object(self.launcher, "ADAPTERS_PATH", percorso):
            _node, sorgenti, regole, _mancanti, avvisi = self.launcher.writer_readiness(self.confronto())

        self.assertEqual(regole, {})
        self.assertEqual(sorgenti, {})
        self.assertTrue(any("non contiene ORDINE" in avviso for avviso in avvisi), avvisi)

    def test_una_colonna_vuota_confermata_diventa_scrivibile_solo_sullo_stesso_file(self) -> None:
        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"] = {
            "from_field_mapping": True,
            "order_column": "C",
            "allow_blank_header_if_confirmed": True,
        }
        mappatura = {
            "sheet": "Listino", "header_row": 1, "data_start_row": 2,
            "columns": {"ean": "COD.EAN", "description": "DESCRIZIONE"},
            "order_column": "C", "order_header_blank_confirmed": True,
        }
        documento = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", None])
        confronto = {"files": [{
            "supplierId": "acero", "adapterId": "acero_v1", "role": "supplier",
            "sourcePath": str(documento), "fieldMapping": mappatura,
        }]}

        regole, avvisi = self.avvisi_di(voce, confronto)

        self.assertEqual(avvisi, [])
        self.assertTrue(regole["acero"]["blank_header_confirmed"])
        self.assertEqual(regole["acero"]["source_sha256"], self.launcher.sha256_file(documento))

    def test_una_colonna_vuota_confermata_non_puo_nascondere_testo_o_formule(self) -> None:
        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"] = {
            "from_field_mapping": True,
            "order_column": "C",
            "allow_blank_header_if_confirmed": True,
        }
        mappatura = {
            "sheet": "Listino", "header_row": 1, "data_start_row": 2,
            "columns": {"ean": "COD.EAN", "description": "DESCRIZIONE"},
            "order_column": "C", "order_header_blank_confirmed": True,
        }
        documento = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", None])
        libro = load_workbook(documento)
        libro["Listino"]["C2"] = "NON SOVRASCRIVERE"
        libro.save(documento)
        libro.close()
        confronto = {"files": [{
            "supplierId": "acero", "adapterId": "acero_v1", "role": "supplier",
            "sourcePath": str(documento), "fieldMapping": mappatura,
        }]}

        regole, avvisi = self.avvisi_di(voce, confronto)

        self.assertEqual(regole, {})
        self.assertTrue(any("contiene testo o formule" in avviso for avviso in avvisi), avvisi)

    def test_la_colonna_vuota_si_controlla_in_una_passata_sola(self) -> None:
        """The check must be linear, not quadratic.

        On a sheet opened read-only, `foglio.cell(r, c)` re-reads the sheet
        from the start on every call. Checking this once per price-list row
        made program startup silently hang for minutes: measured 3382 calls
        on CIPRESSO, 5.7 million rows scanned, 474 seconds inside
        `prepare_writer_config`.

        The ceiling here is deliberately generous: the single pass stays
        under a second, the old quadratic version took minutes on these
        rows. It doesn't measure machine speed, it measures that the
        algorithm hasn't gone quadratic again.
        """

        import time

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"] = {
            "from_field_mapping": True,
            "order_column": "C",
            "allow_blank_header_if_confirmed": True,
        }
        mappatura = {
            "sheet": "Listino", "header_row": 1, "data_start_row": 2,
            "columns": {"ean": "COD.EAN", "description": "DESCRIZIONE"},
            "order_column": "C", "order_header_blank_confirmed": True,
        }
        documento = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", None])
        libro = load_workbook(documento)
        foglio = libro["Listino"]
        for numero in range(2000):
            foglio.append([f"800000000{numero:04d}", f"PRODOTTO {numero}", None])
        libro.save(documento)
        libro.close()
        confronto = {"files": [{
            "supplierId": "acero", "adapterId": "acero_v1", "role": "supplier",
            "sourcePath": str(documento), "fieldMapping": mappatura,
        }]}

        inizio = time.monotonic()
        regole, avvisi = self.avvisi_di(voce, confronto)
        durata = time.monotonic() - inizio

        self.assertEqual(avvisi, [])
        self.assertTrue(regole["acero"]["blank_header_confirmed"])
        self.assertLess(durata, 20.0, f"il controllo della colonna ha impiegato {durata:.1f}s")

    def test_i_compilabili_sono_quelli_che_il_registro_spedito_dichiara(self) -> None:
        """The rule moved location, not content.

        The list isn't fixed at any count: it's whatever the shipped
        registry declares, and it grows as suppliers are added. `offerte`
        is CIPRESSO's promotional price list, which has no header row and
        is recognized by the shape of its columns. This test is meant to
        flag the addition of a compilable supplier, not to block it: when
        the count changes, update it here after checking what was added.
        """

        self.assertEqual(sorted(self.launcher.fornitori_compilabili()),
                         ["betulla", "cipresso", "larice", "noce", "offerte"])

    # -- source-text structural checks (quote/comment-agnostic) ------------

    def listino_su_misura(self, *, intestazioni: list[str], fogli_extra: int = 0) -> Path:
        percorso = self.root / f"acero_{len(intestazioni)}_{fogli_extra}.xlsx"
        libro = Workbook()
        foglio = libro.active
        foglio.title = "Listino"
        foglio.append(intestazioni)
        foglio.append(["8000000000001", "PRODOTTO", None])
        for indice in range(fogli_extra):
            extra = libro.create_sheet(f"Note{indice + 1}")
            extra.append(["nota"])
        libro.save(percorso)
        libro.close()
        return percorso

    def confronto_su(self, percorso: Path) -> dict[str, Any]:
        return {"files": [{"supplierId": "acero", "adapterId": "acero_v1",
                           "role": "supplier", "sourcePath": str(percorso)}]}

    def avvisi_di(self, voce: dict[str, Any], confronto: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        percorso = self.registro(voce)
        with mock.patch.object(self.launcher, "ADAPTERS_PATH", percorso):
            _node, _sorgenti, regole, _mancanti, avvisi = self.launcher.writer_readiness(confronto)
        return regole, avvisi

    def test_le_righe_dichiarate_si_confrontano_col_documento(self) -> None:
        """A single-digit typo would silently write quantities outside the data range."""

        voce = self.adattatore_imparato(con_scrittura=True)
        del voce["order_write"]["expected_header"]
        del voce["order_write"]["header_row"]
        voce["order_write"]["data_start_row"] = 999999

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("fuori dal foglio" in avviso and "999999" in avviso
                            for avviso in avvisi), avvisi)

    def test_anche_l_intestazione_dichiarata_fuori_dal_documento_si_dice(self) -> None:
        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["header_row"] = 999998
        voce["order_write"]["data_start_row"] = 999999

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("intestazione dichiarata alla riga 999998" in avviso
                            and "fuori dal foglio" in avviso for avviso in avvisi), avvisi)

    def test_intestazione_e_dati_dichiarati_incoerenti_si_dicono(self) -> None:
        """Data starting on the header row is an inconsistent declaration."""

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["header_row"] = 2
        voce["order_write"]["data_start_row"] = 2

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("non stanno insieme" in avviso for avviso in avvisi), avvisi)

    def test_un_foglio_non_dichiarato_su_un_documento_a_piu_fogli_si_rifiuta(self) -> None:
        """Picking the first of three sheets would be one nobody actually checked."""

        voce = self.adattatore_imparato(con_scrittura=True)
        del voce["order_write"]["sheet"]
        documento = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", "ORDINE"],
                                           fogli_extra=2)

        regole, avvisi = self.avvisi_di(voce, self.confronto_su(documento))

        self.assertEqual(regole, {})
        self.assertTrue(any("il registro non indica quello esatto" in avviso
                            for avviso in avvisi), avvisi)

    def test_first_dichiarato_dal_registro_vale_il_primo_foglio(self) -> None:
        """'FIRST' written in the registry is a deliberate choice, even across multiple sheets."""

        voce = self.adattatore_imparato(con_scrittura=True)
        documento = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", "ORDINE"],
                                           fogli_extra=2)

        regole, avvisi = self.avvisi_di(voce, self.confronto_su(documento))

        self.assertEqual(regole["acero"]["sheet"], "Listino")
        self.assertEqual(avvisi, [])

    def test_una_procedura_di_scrittura_sconosciuta_si_rifiuta(self) -> None:
        """A typo in `mode` must not silently fall back to the default write procedure."""

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["mode"] = "patch_xls_in_posizone"

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("procedura di scrittura sconosciuta" in avviso
                            and "patch_xls_in_posizone" in avviso for avviso in avvisi), avvisi)

    def test_le_colonne_richieste_senza_mappatura_accusano_le_colonne(self) -> None:
        """The message must blame the columns, not "rows", when columns are the problem."""

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["required_columns"] = ["ean"]

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("chiede le colonne ean" in avviso for avviso in avvisi), avvisi)
        self.assertFalse(any("le righe da cui parte" in avviso for avviso in avvisi), avvisi)

    def test_un_foglio_dichiarato_che_non_esiste_accusa_il_registro(self) -> None:
        """The registry declared it, not a field mapping: the message must say so."""

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["sheet"] = "Fantasma"

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("indicato nel registro" in avviso for avviso in avvisi), avvisi)

    def test_un_registro_rotto_non_diventa_un_non_dichiara(self) -> None:
        """'Can't be read' and 'doesn't declare' must lead to two different messages."""

        percorso = self.root / "adapters.json"
        percorso.write_text("{ rotto", encoding="utf-8")

        with mock.patch.object(self.launcher, "ADAPTERS_PATH", percorso):
            _node, _sorgenti, regole, _mancanti, avvisi = self.launcher.writer_readiness(self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("non si legge" in avviso for avviso in avvisi), avvisi)
        self.assertFalse(any("non dichiara" in avviso for avviso in avvisi), avvisi)

    def test_fornitori_senza_copia_porta_la_causa_vera(self) -> None:
        """The pipeline's warning must say why a copy is missing, not just that it is."""

        voce = self.adattatore_imparato(con_scrittura=True)
        percorso = self.registro(voce)
        sbagliato = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", "ORDINI"])

        esiti = self.launcher.fornitori_senza_copia(self.confronto_su(sbagliato), percorso)

        self.assertIn("acero", esiti)
        self.assertIn("non contiene ORDINE", esiti["acero"])

        giusto = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", "ORDINE"])
        self.assertEqual(self.launcher.fornitori_senza_copia(self.confronto_su(giusto), percorso), {})


class LauncherBrowserTests(unittest.TestCase):
    """The comparator must open in Chrome, not in the system's default browser."""

    @classmethod
    def load_launcher(cls) -> Any:
        launcher_path = SKILL_ROOT / "app" / "launcher.py"
        spec = importlib.util.spec_from_file_location("compara_ordini_launcher_browser_test", launcher_path)
        if spec is None or spec.loader is None:
            raise AssertionError("Impossibile importare il launcher")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def setUp(self) -> None:
        self.launcher = self.load_launcher()
        self.root = Path(tempfile.mkdtemp(prefix="launcher-browser-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def fake_chrome(self) -> Path:
        chrome = self.root / "chrome.exe"
        chrome.write_bytes(b"")
        return chrome

    def test_chrome_is_started_directly_and_the_default_browser_is_left_alone(self) -> None:
        chrome = self.fake_chrome()
        avviati: list[list[str]] = []
        with mock.patch.dict(os.environ, {self.launcher.CHROME_ENV_OVERRIDE: str(chrome)}), \
                mock.patch.object(self.launcher.subprocess, "Popen", lambda command, **_: avviati.append(command)), \
                mock.patch.object(self.launcher.webbrowser, "open", side_effect=AssertionError("il predefinito non va usato")):
            self.launcher.open_application("http://127.0.0.1:8765/")

        self.assertEqual(len(avviati), 1)
        self.assertEqual(avviati[0][0], str(chrome))
        self.assertEqual(avviati[0][1], "http://127.0.0.1:8765/")

    def test_without_chrome_the_default_browser_still_opens_the_page(self) -> None:
        aperti: list[str] = []
        with mock.patch.dict(os.environ, {self.launcher.CHROME_ENV_OVERRIDE: str(self.root / "assente.exe")}), \
                mock.patch.object(self.launcher.shutil, "which", return_value=None), \
                mock.patch.object(self.launcher.webbrowser, "open", lambda url, new=0: aperti.append(url) or True):
            self.launcher.open_application("http://127.0.0.1:8765/")

        self.assertEqual(aperti, ["http://127.0.0.1:8765/"])

    def test_a_chrome_that_refuses_to_start_does_not_leave_the_user_without_a_page(self) -> None:
        chrome = self.fake_chrome()
        aperti: list[str] = []

        def esplode(command: list[str], **_: object) -> None:
            raise OSError("avvio non riuscito")

        with mock.patch.dict(os.environ, {self.launcher.CHROME_ENV_OVERRIDE: str(chrome)}), \
                mock.patch.object(self.launcher.subprocess, "Popen", esplode), \
                mock.patch.object(self.launcher.webbrowser, "open", lambda url, new=0: aperti.append(url) or True):
            self.launcher.open_application("http://127.0.0.1:8765/")

        self.assertEqual(aperti, ["http://127.0.0.1:8765/"])


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

AI = SERVER.ai_client

# Two fake keys, and the difference between them is the point of two
# different tests. The first does NOT have the shape of an OpenRouter key:
# the regex-based safety net doesn't recognize it, so if it's missing from
# a response, that's because the handler remembered which key it was
# passing. The second has the right shape and covers the opposite case: a
# key the handler never saw explicitly (arriving, for example, inside a
# supplier's error message).
CHIAVE_SENZA_FORMA = "CHIAVE-FINTA-DI-PROVA-0123456789"
CHIAVE_CON_FORMA = "sk-or-v1-0123456789abcdef0123456789abcdef"


def nomi_dei_campi(valore: Any) -> set[str]:
    """All field names of a JSON response, at any depth."""

    if isinstance(valore, dict):
        trovati = set(valore)
        for dentro in valore.values():
            trovati |= nomi_dei_campi(dentro)
        return trovati
    if isinstance(valore, list):
        trovati: set[str] = set()
        for dentro in valore:
            trovati |= nomi_dei_campi(dentro)
        return trovati
    return set()


def esito_finto(stato: str = "OK", dettaglio: str = "Tutto a posto.") -> Any:
    """A complete `EsitoAI`: the class is frozen and has no default values."""

    return AI.EsitoAI(
        stato=stato,
        decisione=None,
        modello="fornitore/modello-di-prova",
        fornitore_calcolo="prova",
        costo_usd=0.0001,
        token_ingresso=10,
        token_uscita=5,
        tentativi=1,
        durata_s=0.2,
        dettaglio=dettaglio,
    )


class ClientAIFinto:
    """A `ClientAI` stand-in that never touches the network and records the key it received."""

    def __init__(self, configurazione: dict[str, Any], chiave: str | None, esito: Any) -> None:
        self.configurazione = configurazione
        self.chiave = chiave
        self._esito = esito

    def prova_connessione(self) -> Any:
        if isinstance(self._esito, Exception):
            raise self._esito
        return self._esito


class ImpostazioniHttpTests(unittest.TestCase):
    """The Settings page tested from the route's side.

    A key that leaves the local service can't be recalled, and a 500's body
    forwards `str(exc)` to the browser. These tests target exactly that
    escape path, not the happy case.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.secrets = self.root / "secrets.json"
        self.settings = self.root / "impostazioni_ai.json"

        # OPENROUTER_API_KEY overrides everything: if it were set on the
        # machine running the suite, these tests would be testing something
        # else entirely.
        ambiente = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""})
        ambiente.start()
        self.addCleanup(ambiente.stop)

        run_dir = self.root / "run"
        run_dir.mkdir()
        review_path = self.root / "review_data.json"
        review_path.write_text(json.dumps(synthetic_review()), encoding="utf-8")
        self.store = ReviewStore(review_path, run_dir / "state.json", self.root / "uploads", self.root / "outputs")
        # Close `conferme.db` BEFORE the temp dir is deleted: `addCleanup`
        # runs in reverse order, so this line must come after the temp dir's
        # own cleanup. On Windows an open file blocks deletion, so the test
        # that downloads confirmations (the only one here that actually opens
        # the database) would fail cleanup with WinError 32; on Unix an open
        # file can be deleted regardless.
        self.addCleanup(self.store.chiudi)

        # What the two network endpoints return, decided per test.
        self.modelli: Any = [
            {"id": "fornitore/modello-di-prova", "nome": "Modello di prova", "prezzo_ingresso": 0.0000006, "prezzo_uscita": 0.0000017},
        ]
        self.esito: Any = esito_finto()
        self.clienti: list[ClientAIFinto] = []
        self.righe_di_registro: list[str] = []

        servizio = SERVER.ServizioImpostazioni(
            percorso_secrets=self.secrets,
            percorso_impostazioni=self.settings,
            leggi_modelli=self.elenco_finto,
            crea_client=self.client_finto,
        )

        righe = self.righe_di_registro

        class HandlerCheRegistra(SERVER.AppHandler):
            def log_message(self, format_string: str, *args: object) -> None:
                # Call the real implementation and capture its output: what
                # ends up in the log must be exactly what it writes, not a
                # reimplementation of its logic inside the test.
                with io.StringIO() as buffer, contextlib.redirect_stdout(buffer):
                    super().log_message(format_string, *args)
                    righe.append(buffer.getvalue())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), HandlerCheRegistra)
        self.httpd.store = self.store
        self.httpd.impostazioni = servizio
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.ferma_il_servizio)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def ferma_il_servizio(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def elenco_finto(self, base_url: str | None = None) -> list[dict[str, Any]]:
        if isinstance(self.modelli, Exception):
            raise self.modelli
        return self.modelli

    def client_finto(self, configurazione: dict[str, Any], chiave: str | None) -> ClientAIFinto:
        client = ClientAIFinto(configurazione, chiave, self.esito)
        self.clienti.append(client)
        return client

    # -- richieste -----------------------------------------------------------

    def get(self, percorso: str) -> tuple[int, Any, str]:
        richiesta = urllib.request.Request(self.base_url + percorso, method="GET")
        return self.esegui(richiesta)

    def post(self, percorso: str, corpo: Any) -> tuple[int, Any, str]:
        richiesta = urllib.request.Request(
            self.base_url + percorso,
            data=json.dumps(corpo).encode("utf-8"),
            method="POST",
        )
        richiesta.add_header("Content-Type", "application/json; charset=utf-8")
        return self.esegui(richiesta)

    def esegui(self, richiesta: Any) -> tuple[int, Any, str]:
        """Also return the raw body: the key is searched for in the BYTES, not
        in a dict key someone could have renamed.
        """

        try:
            with urllib.request.urlopen(richiesta, timeout=15) as risposta:
                grezzo = risposta.read().decode("utf-8")
                return int(risposta.status), json.loads(grezzo), grezzo
        except urllib.error.HTTPError as errore:
            grezzo = errore.read().decode("utf-8")
            try:
                return int(errore.code), json.loads(grezzo), grezzo
            except ValueError:
                return int(errore.code), None, grezzo

    # -- lo stato ------------------------------------------------------------

    def test_lo_stato_dice_se_la_chiave_ce_ma_non_la_manda_mai(self) -> None:
        AI.salva_chiave(CHIAVE_SENZA_FORMA, self.secrets)

        stato, corpo, grezzo = self.get("/api/impostazioni")

        self.assertEqual(stato, 200)
        self.assertTrue(corpo["chiave"]["presente"])
        self.assertEqual(corpo["chiave"]["origine"], "file")
        self.assertEqual(corpo["chiave"]["coda"], CHIAVE_SENZA_FORMA[-4:])
        self.assertNotIn(CHIAVE_SENZA_FORMA, grezzo)
        # The tail is four characters; the rest must never appear.
        self.assertNotIn(CHIAVE_SENZA_FORMA[:-4], grezzo)

    def test_lo_stato_espone_le_voci_da_cambiare_e_non_la_versione_del_prompt(self) -> None:
        _, corpo, _ = self.get("/api/impostazioni")

        self.assertEqual(set(corpo["impostazioni"]), set(SERVER.VOCI_IMPOSTAZIONI))
        self.assertNotIn("versione_prompt", corpo["impostazioni"])
        self.assertNotIn("versione_avversario", corpo["impostazioni"])

    def test_la_configurazione_ai_non_entra_nella_risposta_piu_letta(self) -> None:
        """`GET /api/review` is the biggest, most frequently read response: the
        AI stage's configuration must not travel inside it, let alone
        anything about the key.
        """

        AI.salva_chiave(CHIAVE_SENZA_FORMA, self.secrets)

        stato, corpo, grezzo = self.get("/api/review")

        self.assertEqual(stato, 200)
        self.assertNotIn(CHIAVE_SENZA_FORMA, grezzo)
        # Checking field NAMES at any depth: a raw-text search would give a
        # false positive on a product literally named "MODEL", and a false
        # negative on a value nested deep inside the response.
        vietati = {*SERVER.VOCI_IMPOSTAZIONI, "versione_prompt", "versione_avversario", "openrouter", "api_key"}
        self.assertEqual(sorted(nomi_dei_campi(corpo) & vietati), [])

    # -- saving the key -------------------------------------------------

    def test_la_chiave_si_salva_e_la_risposta_non_la_riporta_indietro(self) -> None:
        stato, corpo, grezzo = self.post("/api/impostazioni/chiave", {"chiave": CHIAVE_SENZA_FORMA})

        self.assertEqual(stato, 200)
        self.assertTrue(corpo["ok"])
        self.assertNotIn(CHIAVE_SENZA_FORMA, grezzo)
        self.assertEqual(corpo["chiave"]["coda"], CHIAVE_SENZA_FORMA[-4:])
        self.assertEqual(AI.leggi_chiave(self.secrets), CHIAVE_SENZA_FORMA)

    def test_una_chiave_vuota_e_un_errore_scritto_non_un_file_svuotato(self) -> None:
        AI.salva_chiave(CHIAVE_SENZA_FORMA, self.secrets)

        stato, corpo, _ = self.post("/api/impostazioni/chiave", {"chiave": "   "})

        self.assertEqual(stato, 400)
        self.assertFalse(corpo["ok"])
        self.assertIn("vuota", corpo["message"])
        self.assertEqual(AI.leggi_chiave(self.secrets), CHIAVE_SENZA_FORMA, "la chiave buona è stata persa")

    # -- la prova ------------------------------------------------------------

    def test_la_prova_usa_la_chiave_del_corpo_e_non_la_salva(self) -> None:
        """Testing happens BEFORE saving: a wrong key must never be able to
        overwrite one that already works.
        """

        stato, corpo, _ = self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})

        self.assertEqual(stato, 200)
        self.assertTrue(corpo["ok"])
        self.assertEqual([client.chiave for client in self.clienti], [CHIAVE_SENZA_FORMA])
        self.assertFalse(self.secrets.exists(), "la prova ha salvato la chiave")

    def test_senza_chiave_nel_corpo_la_prova_usa_quella_salvata_qui(self) -> None:
        """And it must be the key at THIS service's path.

        Letting `ClientAI(chiave=None)` look up the key would fall back to
        the module's default path: the test would then report the state of
        a different key than the one the settings page shows.
        """

        AI.salva_chiave(CHIAVE_SENZA_FORMA, self.secrets)

        stato, _, _ = self.post("/api/impostazioni/prova", {})

        self.assertEqual(stato, 200)
        self.assertEqual([client.chiave for client in self.clienti], [CHIAVE_SENZA_FORMA])

    def test_senza_nessuna_chiave_la_prova_lo_dichiara_invece_di_cercarla(self) -> None:
        """The empty string tells the client "there is no key", which is how
        `SENZA_CHIAVE` is reached instead of an unexpected lookup elsewhere.
        """

        self.esito = esito_finto("SENZA_CHIAVE", "Nessuna chiave configurata.")

        stato, corpo, _ = self.post("/api/impostazioni/prova", {})

        self.assertEqual(stato, 200)
        self.assertEqual([client.chiave for client in self.clienti], [""])
        self.assertFalse(corpo["ok"])
        self.assertIn("incollala", corpo["messaggio"])

    def test_la_prova_prova_il_modello_scritto_prima_che_sia_salvato(self) -> None:
        stato, _, _ = self.post("/api/impostazioni/prova", {"model": "fornitore/modello-appena-uscito"})

        self.assertEqual(stato, 200)
        self.assertEqual(self.clienti[0].configurazione["model"], "fornitore/modello-appena-uscito")
        self.assertFalse(self.settings.exists(), "la prova ha salvato il modello")

    def test_una_chiave_rifiutata_lo_dice_in_italiano_e_senza_dire_che_va_bene(self) -> None:
        self.esito = esito_finto("HTTP_401", "Unauthorized")

        _, corpo, _ = self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})

        self.assertFalse(corpo["ok"])
        self.assertEqual(corpo["tono"], "danger")
        self.assertIn("401", corpo["messaggio"])

    def test_una_risposta_storta_non_diventa_una_chiave_che_funziona(self) -> None:
        """`SCHEMA_NON_CONFORME` means the call went through: the key wasn't
        rejected, but that model isn't usable. Reporting "all good" here
        would be this page's most expensive lie.
        """

        self.esito = esito_finto("SCHEMA_NON_CONFORME", "Manca «azione»")

        _, corpo, _ = self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})

        self.assertFalse(corpo["ok"])
        self.assertEqual(corpo["tono"], "warning")
        self.assertIn("non è utilizzabile", corpo["messaggio"])

    # -- an exception carrying the key must never reach the browser ---------

    def test_uneccezione_che_si_porta_dietro_la_chiave_non_arriva_al_browser(self) -> None:
        """A 500's body forwards `str(exc)`. Any failure (urllib, a `KeyError`
        on a headers dict, a third-party library) could have the key inside
        its message.
        """

        self.esito = RuntimeError(f"guasto interno chiamando con {CHIAVE_SENZA_FORMA} in corso")

        stato, corpo, grezzo = self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})

        self.assertEqual(stato, 500)
        # The property that matters: the key doesn't appear ANYWHERE in the
        # body, neither in the message nor in the technical detail.
        self.assertNotIn(CHIAVE_SENZA_FORMA, grezzo)
        self.assertIn(AI.NASCOSTO, grezzo)
        # The error stays visible: hiding the key doesn't mean hiding the
        # failure, which nobody would go looking for in the logs without a
        # message. The technical text lives in the detail field, separate
        # from the message that tells the user what to do.
        dettaglio = [voce for voce in corpo["errors"] if voce.get("code") == "DETTAGLIO_TECNICO"]
        self.assertEqual(len(dettaglio), 1, corpo["errors"])
        self.assertIn("guasto interno", dettaglio[0]["message"])
        self.assertIn(AI.NASCOSTO, dettaglio[0]["message"])
        self.assertIn("Riprova", corpo["message"])

    # -- given confirmations, downloadable as a file --------------------------

    def scarica(self, percorso: str) -> tuple[int, dict[str, str], bytes]:
        """Also return the headers: half of what this endpoint does lives there."""

        richiesta = urllib.request.Request(self.base_url + percorso, method="GET")
        with urllib.request.urlopen(richiesta, timeout=15) as risposta:
            return int(risposta.status), dict(risposta.headers), risposta.read()

    def test_le_conferme_si_scaricano_come_file_col_nome_in_italiano(self) -> None:
        stato, intestazioni, corpo = self.scarica("/api/conferme/esporta")

        self.assertEqual(stato, 200)
        self.assertIn("application/json", intestazioni["Content-Type"])
        disposizione = intestazioni["Content-Disposition"]
        self.assertTrue(disposizione.startswith("attachment;"), disposizione)
        # The filename carries an em dash and Italian month names, and
        # `BaseHTTPRequestHandler` headers are latin-1: without the RFC 5987
        # form `consegna` builds, the response would die while writing the
        # header, after the body has already been promised.
        self.assertIn("filename*=UTF-8''", disposizione)
        self.assertIn("Conferme", disposizione)
        documento = json.loads(corpo.decode("utf-8"))
        self.assertEqual(documento["conferme"], [])
        self.assertEqual(documento["quante"], 0)
        self.assertIn("esportate_il", documento)

    def test_quello_che_si_scarica_e_quello_che_c_e_dentro(self) -> None:
        """The two tests above only exercised the empty case: the route could
        respond `[]` instead of reading the store and stay green. Here the
        store has data, and it must come out, both confirmations and
        equivalences, since they live in the same permanent file.
        """

        magazzino = SERVER.MagazzinoConferme(self.store.conferme_path)
        try:
            magazzino.ricorda(
                fornitore="betulla",
                articolo="8000000000010|TONNO MAR BLUE",
                offerta="betulla|8000000000011|1234|TONNO MAR BLUE OLIO OLIVA",
                accettata=True,
                motivo="scelta a mano dal listino BETULLA",
                quando="2026-08-19T10:00:00+00:00",
            )
            magazzino.unisci(
                "4009428623194", "8729721830575",
                motivo="scelta a mano dal listino NOCE",
                quando="2026-08-19T11:00:00+00:00",
            )
        finally:
            magazzino.chiudi()

        _stato, _intestazioni, corpo = self.scarica("/api/conferme/esporta")

        documento = json.loads(corpo.decode("utf-8"))
        self.assertEqual(documento["quante"], 1)
        self.assertEqual(documento["conferme"][0]["fornitore"], "betulla")
        self.assertEqual(documento["conferme"][0]["articolo"], "8000000000010|TONNO MAR BLUE")
        self.assertEqual(documento["quante_uguaglianze"], 1)
        codici = documento["uguaglianze"][0]
        self.assertIn("4009428623194", json.dumps(codici))
        self.assertIn("8729721830575", json.dumps(codici))

    def test_scaricarle_su_un_programma_nuovo_non_crea_il_magazzino(self) -> None:
        """Opening Settings must not leave behind an empty `conferme.db`:

        SQLite keeps the file open as long as the connection lives, and on
        Windows an open file locks the folder that contains it.
        """

        stato, _intestazioni, _corpo = self.scarica("/api/conferme/esporta")

        self.assertEqual(stato, 200)
        self.assertFalse(self.store.conferme_path.exists())

    # -- the traceback of an unexpected failure --------------------------------

    def stderr_di(self, chiamata) -> str:
        """What ended up on the program's window during the request.

        `redirect_stderr` replaces `sys.stderr` process-wide, and the handler
        responds on another thread; the request still completes inside the
        `with` block, because `urlopen` waits for the response.
        """

        with io.StringIO() as buffer, contextlib.redirect_stderr(buffer):
            esito = chiamata()
            self.ultimo_esito = esito
            return buffer.getvalue()

    def test_un_guasto_imprevisto_lascia_il_suo_traceback_sulla_finestra(self) -> None:
        """Without a traceback, a bare "KeyError" gives no way to know which
        line of thousands raised it, forcing a slow back-and-forth to
        reproduce the failure.
        """

        self.esito = RuntimeError("guasto interno di prova")

        uscita = self.stderr_di(lambda: self.post("/api/impostazioni/prova", {}))

        stato, corpo, _ = self.ultimo_esito
        self.assertEqual(stato, 500)
        self.assertIn("[GUASTO]", uscita)
        self.assertIn("POST /api/impostazioni/prova", uscita)
        self.assertIn("Traceback (most recent call last)", uscita)
        self.assertIn("RuntimeError: guasto interno di prova", uscita)
        # The traceback names the file and line: the one thing the on-page
        # message can never say.
        self.assertIn("server.py", uscita)
        # And the response doesn't change at all: the traceback is for
        # whoever watches the window, not for whoever uses the program.
        self.assertIn("Riprova", corpo["message"])
        self.assertNotIn("Traceback", json.dumps(corpo))

    def test_la_chiave_non_finisce_nemmeno_nel_traceback(self) -> None:
        """The key can show up in an `urllib` traceback: that's exactly why
        `_chiave_in_volo` exists.
        """

        self.esito = RuntimeError(f"il fornitore ha risposto: Bearer {CHIAVE_CON_FORMA}")

        uscita = self.stderr_di(
            lambda: self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})
        )

        self.assertIn("[GUASTO]", uscita)
        self.assertNotIn(CHIAVE_CON_FORMA, uscita)
        self.assertNotIn(CHIAVE_SENZA_FORMA, uscita)
        self.assertIn(AI.NASCOSTO, uscita)

    def test_anche_la_chiave_gia_salvata_si_nasconde(self) -> None:
        """The most common case of all: reusing the saved key, not the pasted one.

        Pressing "Test connection" without pasting anything sends a body
        with no `chiave`, and the service falls back to the saved one. If
        only the body's key were masked, the sole defense left would be the
        `sk-...` shape, which a key taken from `OPENROUTER_API_KEY` (never
        validated) isn't guaranteed to have. This key doesn't have it, and
        must still be hidden, both from the response body and from the
        traceback on the window.
        """

        AI.salva_chiave(CHIAVE_SENZA_FORMA, self.secrets)
        self.esito = RuntimeError(f"il fornitore ha risposto 401 per {CHIAVE_SENZA_FORMA}")

        uscita = self.stderr_di(lambda: self.post("/api/impostazioni/prova", {}))

        stato, _corpo, grezzo = self.ultimo_esito
        self.assertEqual(stato, 500)
        self.assertNotIn(CHIAVE_SENZA_FORMA, grezzo)
        self.assertNotIn(CHIAVE_SENZA_FORMA, uscita)
        self.assertIn(AI.NASCOSTO, uscita)

    def test_un_browser_che_stacca_a_meta_non_e_un_guasto(self) -> None:
        """`BrokenPipeError` reaches the same `except Exception` as real failures,
        but isn't a program failure: it's the user canceling a download, or a
        tab closing mid-transfer on a large file (reproduced with a real
        60 MB file cut off mid-download). Logging a traceback for each would
        drown out the window that matters on the day a real failure happens.
        """

        self.esito = BrokenPipeError(32, "Broken pipe")

        uscita = self.stderr_di(lambda: self.post("/api/impostazioni/prova", {}))

        self.assertEqual(self.ultimo_esito[0], 500)
        self.assertNotIn("[GUASTO]", uscita)
        self.assertNotIn("Traceback", uscita)

    def test_una_richiesta_riuscita_non_stampa_niente(self) -> None:
        uscita = self.stderr_di(lambda: self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA}))

        self.assertEqual(self.ultimo_esito[0], 200)
        self.assertEqual(uscita, "")

    def test_un_errore_gia_spiegato_non_stampa_nessun_traceback(self) -> None:
        """A 400 is a response, not a failure: a traceback here is noise, and
        noise on every empty field means nobody watches that window anymore
        by the day a real failure happens.
        """

        uscita = self.stderr_di(lambda: self.post("/api/impostazioni/chiave", {"chiave": "   "}))

        self.assertEqual(self.ultimo_esito[0], 400)
        self.assertNotIn("[GUASTO]", uscita)

    def test_una_chiave_mai_vista_dal_gestore_la_ferma_la_forma(self) -> None:
        """The second net: a key arriving from inside (a supplier's error, not
        the request body) is one the handler never had, so it can't match it
        by equality. Its shape still catches it.
        """

        self.esito = RuntimeError(f"il fornitore ha risposto: Authorization: Bearer {CHIAVE_CON_FORMA}")

        stato, corpo, grezzo = self.post("/api/impostazioni/prova", {})

        self.assertEqual(stato, 500)
        self.assertNotIn(CHIAVE_CON_FORMA, grezzo)
        self.assertIn(AI.NASCOSTO, grezzo)

    # -- the key must never end up in the request-line log -------------------

    def test_la_chiave_non_finisce_nel_registro_del_servizio(self) -> None:
        """`log_message` prints the request line on every call. Settings routes
        carrying the key are POST with a JSON body, and none of them read
        the query string, so even if one did in the future, the key still
        wouldn't reach the log.
        """

        self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})
        self.get(f"/api/impostazioni?chiave={CHIAVE_CON_FORMA}")

        registro = "".join(self.righe_di_registro)
        self.assertTrue(registro.strip(), "il registro non ha scritto niente: la prova non prova nulla")
        self.assertNotIn(CHIAVE_CON_FORMA, registro)
        self.assertNotIn(CHIAVE_SENZA_FORMA, registro)

    def test_le_rotte_che_portano_la_chiave_esistono_solo_in_post(self) -> None:
        for percorso in ("/api/impostazioni/prova", "/api/impostazioni/chiave"):
            stato, _, _ = self.get(percorso)
            self.assertEqual(stato, 404, f"{percorso} risponde anche in GET")

    # -- saving settings -------------------------------------------------

    def test_le_impostazioni_si_salvano_e_tornano_lette_dal_file(self) -> None:
        stato, corpo, _ = self.post("/api/impostazioni", {"impostazioni": {"tetto_spesa_usd": 5.5, "parallelismo": 8}})

        self.assertEqual(stato, 200)
        self.assertEqual(corpo["impostazioni"]["tetto_spesa_usd"], 5.5)
        self.assertEqual(corpo["impostazioni"]["parallelismo"], 8)
        self.assertEqual(json.loads(self.settings.read_bytes().decode("utf-8"))["tetto_spesa_usd"], 5.5)

    def test_la_versione_del_prompt_non_si_cambia_da_questa_pagina(self) -> None:
        """A program component chosen by measuring wrong `ALTA` results, not a user preference."""

        stato, corpo, _ = self.post("/api/impostazioni", {"impostazioni": {"versione_prompt": "v1"}})

        self.assertEqual(stato, 400)
        self.assertIn("versione_prompt", corpo["message"])
        self.assertFalse(self.settings.exists(), "una voce rifiutata ha scritto il file lo stesso")

    def test_un_numero_con_la_virgola_e_un_errore_spiegato_non_uno_zero_zitto(self) -> None:
        """`3,0` must be rejected explicitly, not silently zero the spending cap
        while the error message parrots back the number the user thought
        they had set.
        """

        stato, corpo, _ = self.post("/api/impostazioni", {"impostazioni": {"tetto_spesa_usd": "3,0"}})

        self.assertEqual(stato, 400)
        self.assertIn("separatore decimale", corpo["message"])
        self.assertFalse(self.settings.exists())

    # -- the model list --------------------------------------------------

    def test_lelenco_dei_modelli_arriva_dal_servizio_e_dice_di_non_essere_una_prova(self) -> None:
        """The CSP is `connect-src 'self'`: the menu can't be populated straight
        from the browser. The list is public, so populating it proves
        nothing about the key, and the response states that explicitly.
        """

        stato, corpo, _ = self.get("/api/impostazioni/modelli")

        self.assertEqual(stato, 200)
        self.assertTrue(corpo["ok"])
        self.assertEqual([voce["id"] for voce in corpo["modelli"]], ["fornitore/modello-di-prova"])
        self.assertIn("Prova la connessione", corpo["avviso"])
        self.assertIn("anche senza chiave", corpo["avviso"])

    def test_un_elenco_irraggiungibile_non_spegne_la_pagina(self) -> None:
        """The model id can still be typed by hand: the only way to use a model
        released after the list was last refreshed.
        """

        self.modelli = OSError("la rete non risponde")

        stato, corpo, _ = self.get("/api/impostazioni/modelli")

        self.assertEqual(stato, 200)
        self.assertFalse(corpo["ok"])
        self.assertEqual(corpo["modelli"], [])
        self.assertIn("a mano", corpo["messaggio"])

    def test_la_pagina_non_allenta_la_politica_dei_contenuti(self) -> None:
        """The menu populates through a local-service route, not because the CSP
        was opened up toward openrouter.ai.
        """

        richiesta = urllib.request.Request(self.base_url + "/api/health", method="GET")
        with urllib.request.urlopen(richiesta, timeout=15) as risposta:
            politica = risposta.headers.get("Content-Security-Policy") or ""

        self.assertIn("connect-src 'self'", politica)
        self.assertNotIn("openrouter", politica)


class ImpostazioniInterfacciaTests(unittest.TestCase):
    """Page rules no local-service test can defend.

    The key value never leaves the local service on any outbound response;
    the only thing that could make it show up somewhere it shouldn't is the
    browser side, i.e. app.js.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.app_js, f"manca la funzione {name}")
        return self.app_js.split(marker)[1].split("\n}\n")[0]

    def test_il_campo_della_chiave_e_un_campo_password_senza_valore_nellhtml(self) -> None:
        corpo = self.body("renderSettingsKeyPanel")
        self.assertIn('type="password"', corpo)
        # Other fields on the page re-render with
        # `value="${escapeHtml(...)}"`: for a saved key that would put the
        # value straight into the page source. Here the value arrives as a
        # DOM node property instead.
        self.assertNotIn("value=\"${escapeHtml(state.impostazioni.nuovaChiave", corpo)
        self.assertNotIn("state.impostazioni.nuovaChiave}", corpo)
        self.assertIn("data-chiave-openrouter", corpo)

    def test_il_valore_incollato_si_rimette_come_proprieta_del_nodo(self) -> None:
        corpo = self.body("render")
        self.assertIn("[data-chiave-openrouter]", corpo)
        self.assertIn("campoChiave.value = state.impostazioni.nuovaChiave", corpo)

    def test_la_chiave_incollata_si_azzera_in_tutti_i_rami_dopo_linvio(self) -> None:
        corpo = self.body("saveApiKey")
        # In the `finally` block, so also when the save fails.
        coda = corpo.split("finally")[-1]
        self.assertIn('state.impostazioni.nuovaChiave = ""', coda)
        # And when leaving the page, saved or not.
        self.assertIn('state.impostazioni.nuovaChiave = ""', self.body("closeSettings"))
        self.assertIn('state.impostazioni.nuovaChiave = ""', self.body("goToStep"))

    def test_la_prova_non_azzera_la_chiave_perche_si_prova_prima_di_salvare(self) -> None:
        corpo = self.body("testConnection")
        self.assertNotIn('state.impostazioni.nuovaChiave = ""', corpo)
        self.assertIn("chiave: state.impostazioni.nuovaChiave", corpo)

    def test_la_chiave_viaggia_solo_nel_corpo_di_una_post(self) -> None:
        for nome in ("saveApiKey", "testConnection"):
            corpo = self.body(nome)
            self.assertIn('method: "POST"', corpo)
            self.assertNotIn("?chiave=", corpo)
        # No settings endpoint carries query parameters.
        indirizzi = self.app_js.split("const API = {")[1].split("};")[0]
        self.assertIn('"/api/impostazioni"', indirizzi)
        self.assertNotIn("impostazioni?", indirizzi)

    def test_il_modello_si_puo_sempre_scrivere_a_mano(self) -> None:
        """Lists go stale: a model released yesterday isn't in them.

        The field must be genuinely editable, and what's typed must reach
        the save call. A stray `readonly` would leave the page looking
        identical while making a new model impossible to enter.
        """

        corpo = self.body("renderSettingsModelPanel")
        campo = next(riga for riga in corpo.splitlines() if 'data-impostazione="model"' in riga or 'id="impostazioni-model"' in riga)
        blocco = corpo.split('id="impostazioni-model"')[1].split(">")[0]
        self.assertIn('type="text"', blocco + campo)
        self.assertNotIn("readonly", blocco)
        self.assertNotIn("disabled", blocco)
        # What's typed lands in state...
        ascolto = self.app_js.split("if (target.dataset.impostazione) {")[1].split("}")[0]
        self.assertIn("state.impostazioni.valori[target.dataset.impostazione] = target.value", ascolto)
        # ...and from state it flows to the save call, bypassing the list.
        self.assertIn("model: modelloConfigurato()", self.body("saveSettings"))
        self.assertIn('state.impostazioni.valori?.model || ""', self.body("modelloConfigurato"))
        # The dropdown fills the text field, which remains the source of truth.
        scelta = self.app_js.split("if (target.dataset.elencoModelli !== undefined)")[1].split("return;")[0]
        self.assertIn("state.impostazioni.valori.model = String(target.value)", scelta)

    def test_la_pagina_non_racconta_che_lelenco_dimostri_la_chiave(self) -> None:
        corpo = self.body("renderSettingsTestPanel")
        self.assertIn("l’unica cosa che dimostra", corpo)
        # The warning accompanying the list comes from the local service,
        # where it's written once.
        self.assertIn("elenco.avviso", self.body("renderSettingsModelPanel"))

    def test_le_impostazioni_non_sono_un_quarto_passo_del_flusso(self) -> None:
        """`currentStep` ranges 1..3 both here and on the local service: a fourth
        value would be truncated on save and the page would snap back to
        the third screen on its own.
        """

        self.assertIn("state.impostazioni.aperta", self.body("render"))
        self.assertNotIn("state.currentStep = 4", self.app_js)
        # The step bar shows a fourth entry while settings are open, but it
        # is NOT a fourth step: it carries no `data-step`, never touches
        # `currentStep`, and disappears as soon as settings close. This test
        # protects `STEPS` staying at three entries.
        self.assertIn('{ id: 3, label: "Riepilogo e compilazione" }', self.app_js)
        self.assertNotIn("{ id: 4,", self.app_js)
        # And the fourth entry is drawn by hand inside `renderStepper`,
        # without going through `STEPS`.
        barra = self.body("renderStepper")
        self.assertIn('data-action="chiudi-impostazioni"', barra)
        self.assertNotIn("data-step", barra.split('data-action="chiudi-impostazioni"', 1)[1])

    def test_la_pagina_si_raggiunge_dalla_prima_schermata(self) -> None:
        self.assertIn('data-action="apri-impostazioni"', self.body("renderUploadStep"))

    def test_i_numeri_partono_come_numeri_e_il_resto_lo_spiega_il_servizio(self) -> None:
        """The local service rejects the string "3" for a cap that's meant to be
        3.0, and it's right to be strict: this is where the conversion
        happens before sending.
        """

        corpo = self.body("settingsNumber")
        self.assertIn("Number.isFinite(numero) ? numero : testo", corpo)
        self.assertIn("settingsNumber(", self.body("saveSettings"))


# ---------------------------------------------------------------------------
# Delivery: a dated folder per compile
# ---------------------------------------------------------------------------

CONSEGNA = SERVER.consegna


def listino_finto(percorso: Path, foglio: str = "LISTINO") -> Path:
    """A real .xlsx, not a fake file: these tests reopen it with openpyxl."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = foglio
    sheet.append(["EAN", "DESCRIZIONE", "ORDINE"])
    sheet.append(["8000000000010", "PRODOTTO STANDARD", None])
    workbook.save(percorso)
    workbook.close()
    return percorso


class ScrittoreFinto:
    """A stand-in for the Node writer that writes wherever it's told.

    The real writer has its own verification chain, already tested
    elsewhere and off-limits here, and Node isn't even installed on many
    machines. All that matters here is that *something* puts documents in
    the compile folder: what's under test is the rename to readable names,
    the audit built by scanning disk, and the routes that deliver them.

    The copy must be a copy of the ORIGINAL price list, not an unrelated
    document that merely resembles it: compile reopens every copy and
    compares it cell by cell against its price list
    (`app/copia_fedele.py`), so a fake writer delivering an unrelated
    document would be caught by that guard, defeating the point of these
    tests.

    It also writes the plan's quantities for real. An earlier version only
    did `shutil.copy2`, delivering a copy identical to the original with an
    empty order column: every delivery test (rename, audit, zip) ran on
    empty orders without anyone noticing, because the fidelity check only
    compared cells that DIFFER, and a quantity never written isn't a
    difference. With that gap closed, a writer that writes nothing is
    exactly what the guard is meant to catch.
    """

    def __init__(
        self,
        fornitori: dict[str, Path],
        *,
        sorpresa: str | None = None,
        avvisi: tuple[str, ...] = (),
    ) -> None:
        self.fornitori = fornitori
        self.sorpresa = sorpresa
        # The messages the real writer reports in its summary (rows written
        # without being able to verify they were the right ones): injectable
        # here because they must reach the page.
        self.avvisi = list(avvisi)
        self.destinazioni: list[Path] = []

    @staticmethod
    def _scrivi_le_quantita(piano: dict[str, Any], fornitore: str, copia: Path) -> None:
        """Write the plan's cartons into the order column, like the real writer does.

        The column comes from the same rule the fidelity check will use
        (`default_write_rule`): hardcoding it here would mean two sources of
        truth, and finding out only when one of them changes.
        """

        righe: dict[int, int] = {}
        for riga in piano.get("orders") or []:
            if str(riga.get("supplier") or "").casefold() != fornitore:
                continue
            numero = riga.get("supplier_source_row")
            colli = riga.get("quantity")
            if numero is None or colli is None:
                continue
            righe[int(numero)] = righe.get(int(numero), 0) + int(colli)
        if not righe:
            return
        regola = ReviewStore.default_write_rule(fornitore) or {}
        colonna = column_index_from_string(str(regola.get("order_column") or "C").strip())
        workbook = load_workbook(copia)
        try:
            foglio = workbook.active
            for numero, colli in righe.items():
                foglio.cell(numero, colonna).value = colli
            workbook.save(copia)
        finally:
            workbook.close()

    def __call__(
        self, plan_path: Path, destinazione: Path
    ) -> tuple[list[Path], list[tuple[str, Path, Path]], list[str]]:
        self.destinazioni.append(destinazione)
        piano = json.loads(plan_path.read_text(encoding="utf-8"))
        generati = [plan_path]
        prodotte: list[tuple[str, Path, Path]] = []
        for fornitore, sorgente in sorted(self.fornitori.items()):
            copia = destinazione / f"ORDINE_{fornitore.upper()}_{sorgente.stem}.xlsx"
            shutil.copy2(sorgente, copia)
            self._scrivi_le_quantita(piano, fornitore, copia)
            generati.append(copia)
            prodotte.append((fornitore, sorgente, copia))
        if self.sorpresa:
            # A file nobody expected: the audit must report it anyway.
            (destinazione / self.sorpresa).write_bytes(b"nessuno mi aspettava")
        return generati, prodotte, list(self.avvisi)


class ConsegnaBase(unittest.TestCase):
    """Shared fixture for the delivery tests: a store with two suppliers."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        # `.resolve()` matters: the temp dir comes back unresolved, but the
        # code under test resolves it. On macOS `/var` is a symlink to
        # `/private/var`, and on Windows TEMP can come back in short form
        # (`RUNNER~1`); either way path comparisons would fail otherwise.
        self.root = Path(temporanea.name).resolve()
        # `dati` needs two levels above the orders root, or
        # `/ordini/../../secrets.json` couldn't reach outside the temp dir
        # and the traversal test below couldn't create its target file.
        self.dati = self.root / "dati"
        run_dir = self.dati / "run-corrente"
        run_dir.mkdir(parents=True)
        self.review_path = self.dati / "review_data.json"
        self.review_path.write_text(json.dumps(self.dati_del_confronto()), encoding="utf-8")
        self.output_dir = self.dati / "outputs"
        self.orders_dir = self.dati / "ordini"
        self.listini = {
            "larice": listino_finto(self.root / "listino_larice.xlsx", "LARICE"),
            "betulla": listino_finto(self.root / "listino_betulla.xlsx", "BETULLA"),
        }
        self.writer_config = self.dati / "writer_config.json"
        self.writer_config.write_text(json.dumps({
            # `prepare_writer_config` always writes the owning run, and compile
            # requires it to match the active comparison: a config without it
            # is a stale one.
            "run_id": "run-sintetica",
            "supplier_files": {nome: str(percorso) for nome, percorso in self.listini.items()},
        }), encoding="utf-8")
        self.store = ReviewStore(
            self.review_path,
            run_dir / "review_state.json",
            self.dati / "uploads",
            self.output_dir,
            self.writer_config,
        )
        self.assertEqual(self.store.orders_dir, self.orders_dir.resolve())

    @staticmethod
    def dati_del_confronto() -> dict[str, Any]:
        review = synthetic_review()
        review["suppliers"].append({"id": "betulla", "name": "Betulla", "minimumOrder": 0})
        review["suppliers"].append({"id": "noce", "name": "Noce", "minimumOrder": 0})
        standard = next(item for item in review["products"] if item["id"] == "product-standard")
        for identificativo, prezzo in (("betulla", 1.2), ("noce", 1.4)):
            standard["offers"].append({
                "supplierId": identificativo,
                "available": True,
                "description": "PRODOTTO STANDARD",
                "sourceRow": 20,
                "ean": "8000000000010",
                "unitPriceNet": prezzo,
                "quantityFactor": 6,
                "orderUnitPriceNet": round(prezzo * 6, 2),
                "method": "EAN",
                "confidence": "CERTA",
            })
        return review

    @staticmethod
    def snapshot(quantita: int, fornitore: str = "larice") -> dict[str, Any]:
        return {
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [{
                "id": "product-standard",
                "quantity": quantita,
                "selectedSupplierId": fornitore,
                "confirmed": True,
            }],
        }

    def compila_con_listini(
        self,
        quantita: int = 2,
        fornitori: tuple[str, ...] = ("larice",),
        *,
        sorpresa: str | None = None,
    ) -> dict[str, Any]:
        """Run a compile that produces real copies, without invoking Node."""

        scrittore = ScrittoreFinto({nome: self.listini[nome] for nome in fornitori}, sorpresa=sorpresa)
        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer", scrittore):
            esito = self.store.compile(self.snapshot(quantita, fornitori[0]))
        self.assertEqual(scrittore.destinazioni, [self.orders_dir / esito["cartella"]])
        return esito

    def compila_senza_listini(self, quantita: int = 2) -> dict[str, Any]:
        """Compile with no writer configured: only the plan is produced."""

        with mock.patch.object(self.store, "writer_config", None):
            return self.store.compile(self.snapshot(quantita))

    def cartelle(self) -> list[str]:
        return sorted(item.name for item in self.orders_dir.iterdir() if item.is_dir())

    def cartella_di(self, esito: dict[str, Any]) -> Path:
        return self.orders_dir / esito["cartella"]

    def audit_di(self, esito: dict[str, Any]) -> dict[str, Any]:
        return json.loads((self.cartella_di(esito) / "compilazione.json").read_text(encoding="utf-8"))

    def nome_del_listino(self, esito: dict[str, Any]) -> str:
        listini = [item["nome"] for item in self.audit_di(esito)["file"] if item["tipo"] == "listino"]
        self.assertEqual(len(listini), 1, listini)
        return listini[0]

    @staticmethod
    def orologio_fermo(minuto: int = 35) -> Any:
        """Return a `datetime` frozen at a fixed moment.

        Needed for the "two compilations in the same minute" case: it does
        happen in practice, but isn't reproducible on demand with the real
        clock.
        """

        class Orologio(SERVER.datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:
                return SERVER.datetime(2026, 8, 12, 14, minuto, 7, 421000, tzinfo=tz)

        return mock.patch.object(SERVER, "datetime", Orologio)


class LaCompilazioneRiuscitaNonHaErroriPerFornitoreTests(ConsegnaBase):
    """Pins the shape of a successful compile response.

    A successful compile never carries a per-supplier `errors` field:
    `run_writer` stops as soon as an expected copy is missing, so a compile
    either produces everything or never reports `ok: True` at all. If a
    partial success ever becomes possible, this test goes red, which is the
    point where the response shape needs to be decided again.
    """

    def test_la_risposta_di_una_compilazione_riuscita_non_porta_errori(self) -> None:
        esito = self.compila_con_listini(2)

        self.assertTrue(esito["ok"])
        self.assertNotIn("errors", esito)
        # Warnings live in their own three fields, which the page reads.
        for campo in ("writerIssues", "deliveryIssues", "historyIssues"):
            self.assertIn(campo, esito)

    def test_una_compilazione_senza_nessuna_copia_lo_mette_fra_gli_avvisi(self) -> None:
        """A `run_writer` failure must surface through `writerIssues`, not just `message`.

        A failing `run_writer` doesn't stop the compile — the plan stays
        deliverable, by design — but the page decides whether a compile is
        only half done by checking `writerIssues`, `deliveryIssues` and
        `historyIssues`; `message` alone isn't enough. The reason must land in
        the field the page actually reads, matching the sibling case
        (incomplete writer configuration).
        """

        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer",
                                  side_effect=ValueError("Il writer non ha creato la copia prevista per LARICE")):
            esito = self.store.compile(self.snapshot(2, "larice"))

        self.assertTrue(esito["ok"])
        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertEqual(len(esito["writerIssues"]), 1)
        self.assertIn("LARICE", esito["writerIssues"][0])
        # Also stated in the message, matching the configuration-issue branch.
        self.assertIn("LARICE", esito["message"])
        self.assertNotIn("errors", esito)


class ConsegnaCartellaDatataTests(ConsegnaBase):
    """The dated output folder, the non-overwriting rename, and the disk audit."""

    def test_due_compilazioni_di_fila_non_si_sovrascrivono(self) -> None:
        """Two compiles in a row must produce two separate folders.

        A second compile must not silently overwrite the first compile's
        ready price lists in the same folder under the same name.
        """

        prima = self.compila_con_listini(2)
        seconda = self.compila_con_listini(3)

        self.assertNotEqual(prima["cartella"], seconda["cartella"])
        self.assertEqual(len(self.cartelle()), 2)

        for esito, quantita in ((prima, 2), (seconda, 3)):
            cartella = self.cartella_di(esito)
            piano = json.loads((cartella / "final_order_plan.json").read_text(encoding="utf-8"))
            # The first plan must still say 2: an overwrite would make it say
            # 3, which is exactly the failure this test guards against.
            self.assertEqual(piano["orders"][0]["quantity"], quantita)
            copia = cartella / self.nome_del_listino(esito)
            self.assertTrue(copia.is_file())
            # Must still open, not just exist on disk.
            from openpyxl import load_workbook

            libro = load_workbook(copia, read_only=True)
            try:
                # The copy is the source price list: same sheet name.
                self.assertEqual(libro.sheetnames, ["LARICE"])
            finally:
                libro.close()

    def test_eliminare_una_compilazione_elimina_anche_i_suoi_promemoria(self) -> None:
        esito = self.compila_con_listini(2)
        history_before, _ = SERVER.order_history.read_history(self.store.history_path)
        self.assertTrue(any(
            str(item.get("orderId") or "").startswith(esito["cartella"] + ":")
            for item in history_before["orders"]
        ))

        result = self.store.delete_compilation({"cartella": esito["cartella"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["promemoriaRimossi"], 1)
        self.assertFalse(self.cartella_di(esito).exists())
        history_after, _ = SERVER.order_history.read_history(self.store.history_path)
        self.assertFalse(any(
            str(item.get("orderId") or "").startswith(esito["cartella"] + ":")
            for item in history_after["orders"]
        ))

    def test_un_errore_durante_l_eliminazione_ripristina_cartella_e_promemoria(self) -> None:
        esito = self.compila_con_listini(2)
        history_before = self.store.history_path.read_bytes()

        with mock.patch.object(SERVER.shutil, "rmtree", side_effect=PermissionError("cartella in uso")):
            with self.assertRaises(PermissionError):
                self.store.delete_compilation({"cartella": esito["cartella"]})

        self.assertTrue(self.cartella_di(esito).is_dir())
        self.assertEqual(self.store.history_path.read_bytes(), history_before)

    def test_non_si_puo_eliminare_una_cartella_fuori_dall_elenco(self) -> None:
        outside = self.root / "fuori"
        outside.mkdir()

        for name in ("../fuori", "..\\fuori", "inesistente"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "non trovata"):
                    self.store.delete_compilation({"cartella": name})

        self.assertTrue(outside.is_dir())

    def test_due_compilazioni_nello_stesso_minuto_restano_due_cartelle(self) -> None:
        with self.orologio_fermo():
            prima = self.compila_con_listini(2)
            seconda = self.compila_con_listini(3)

        self.assertEqual(prima["cartella"], "2026-08-12_1435")
        self.assertEqual(seconda["cartella"], "2026-08-12_1435_2")
        self.assertEqual(self.cartelle(), ["2026-08-12_1435", "2026-08-12_1435_2"])
        self.assertNotIn(":", prima["cartella"])

    def test_il_piano_non_sta_piu_in_outputs_ma_nella_cartella_datata(self) -> None:
        esito = self.compila_con_listini()

        self.assertTrue((self.cartella_di(esito) / "final_order_plan.json").is_file())
        self.assertFalse((self.output_dir / "final_order_plan.json").exists())
        # Response URLs point at the dated folder, not at /outputs/.
        piano = next(item for item in esito["outputs"] if item["name"] == "final_order_plan.json")
        self.assertEqual(piano["tipo"], "piano")
        self.assertEqual(piano["url"], f"/ordini/{esito['cartella']}/final_order_plan.json")

    def test_le_copie_prendono_il_nome_leggibile(self) -> None:
        with self.orologio_fermo():
            esito = self.compila_con_listini(2, ("larice",))

        cartella = self.cartella_di(esito)
        self.assertTrue((cartella / "Ordine LARICE — 12 agosto 2026.xlsx").is_file())
        # The writer's technical filename must not linger next to it: it would
        # be a second copy of the same price list, and would leak into the
        # audit and the zip.
        self.assertEqual(list(cartella.glob("ORDINE_*.xlsx")), [])

    def test_la_rinomina_non_sovrascrive_mai_un_documento_gia_presente(self) -> None:
        """Guard the rename against a name collision.

        Without the guard, a rename onto an existing name raises
        `FileExistsError` on Windows and silently replaces the earlier
        document on Linux; either way this test would fail.
        """

        cartella = self.orders_dir / "2026-08-12_1435"
        cartella.mkdir(parents=True)
        momento = SERVER.datetime(2026, 8, 12, 14, 35)
        gia_li = cartella / CONSEGNA.nome_listino("larice", momento)
        gia_li.write_bytes(b"il listino della volta prima")
        prodotta = cartella / "ORDINE_LARICE_listino_larice.xlsx"
        prodotta.write_bytes(b"la copia appena fatta")

        copie, problemi = self.store.rinomina_listini(
            [("larice", self.listini["larice"], prodotta)], momento
        )

        self.assertEqual(gia_li.read_bytes(), b"il listino della volta prima")
        self.assertTrue(prodotta.is_file())
        # The copy stays deliverable under its current name: an ugly name is
        # no reason to discard a valid order.
        self.assertEqual(copie, [("larice", self.listini["larice"], prodotta)])
        self.assertEqual(len(problemi), 1)
        self.assertIn("c'era già un documento", problemi[0])

    def test_l_audit_descrive_il_disco_e_non_le_intenzioni(self) -> None:
        esito = self.compila_con_listini(2, ("larice",), sorpresa="sorpresa.xlsx")
        cartella = self.cartella_di(esito)
        audit = self.audit_di(esito)

        self.assertEqual(audit["schema_audit"], 1)
        self.assertEqual(audit["cartella"], cartella.name)
        self.assertEqual(audit["run_id"], "run-sintetica")
        self.assertEqual(audit["stato"], "FILES_READY")
        self.assertEqual(audit["fornitori"], ["larice"])
        self.assertEqual(audit["totali_netti"], {"larice": 18.0})
        self.assertEqual(audit["totale_netto"], 18.0)
        self.assertEqual(audit["righe"], 1)
        # Local time with UTC offset, not UTC: it's read next to the folder
        # name, which is local time, and mixing timezones in one document
        # invites a bug.
        self.assertRegex(audit["creato_il"], r"[+-]\d{2}:\d{2}$")

        # The audit's file list must match what's actually on disk.
        # `compilazione.json` is absent because it's written last, after the
        # scan already ran.
        sul_disco = set(os.listdir(cartella)) - {"compilazione.json"}
        self.assertEqual({item["nome"] for item in audit["file"]}, sul_disco)

        per_nome = {item["nome"]: item for item in audit["file"]}
        listino = per_nome[self.nome_del_listino(esito)]
        self.assertEqual(listino["fornitore"], "larice")
        self.assertEqual(listino["origine"], str(self.listini["larice"]))
        self.assertEqual(listino["byte"], (cartella / listino["nome"]).stat().st_size)
        self.assertEqual(per_nome["final_order_plan.json"]["tipo"], "piano")
        # The unexpected file is listed too, tagged as what it is.
        self.assertEqual(per_nome["sorpresa.xlsx"]["tipo"], "altro")
        self.assertNotIn("origine", per_nome["sorpresa.xlsx"])

    def test_l_audit_c_e_anche_quando_il_writer_non_ha_fatto_niente(self) -> None:
        esito = self.compila_senza_listini()
        audit = self.audit_di(esito)

        self.assertEqual(audit["stato"], "PLAN_READY")
        self.assertEqual([item["nome"] for item in audit["file"]], ["final_order_plan.json"])
        # Nothing to deliver without price lists: the download button must not
        # appear, so the zip URL is null rather than a link that would 404.
        self.assertIsNone(esito["zipUrl"])
        self.assertEqual(esito["zipNome"], CONSEGNA.nome_zip(esito["cartella"]))

    def test_il_writer_riceve_la_cartella_della_compilazione(self) -> None:
        """`--output-dir` is the dated folder: the writer writes straight there.

        Writing to `outputs` first and moving afterward would leave a window
        where the previous compile's output is already gone.
        """

        node = self.root / "node.exe"
        node.write_bytes(b"")
        script = self.root / "writer.mjs"
        script.write_bytes(b"")
        self.writer_config.write_text(json.dumps({
            "node_executable": str(node),
            "writer_script": str(script),
            "supplier_files": {"larice": str(self.listini["larice"])},
        }), encoding="utf-8")
        cartella = self.orders_dir / "2026-08-12_1435"
        cartella.mkdir(parents=True)
        plan_path = cartella / "final_order_plan.json"
        plan_path.write_text(json.dumps({"orders": [{"supplier": "larice"}]}), encoding="utf-8")
        comandi: list[list[str]] = []

        def esecuzione_finta(command: list[str], **_altro: Any) -> Any:
            comandi.append(command)
            destinazione = Path(command[command.index("--output-dir") + 1])
            listino_finto(destinazione / "ORDINE_LARICE_listino_larice.xlsx")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(SERVER.subprocess, "run", esecuzione_finta):
            generati, prodotte, avvisi = self.store.run_writer(plan_path, cartella)

        # This fake writer reports no summary: the copy's name is whatever
        # the service recomputes, with no warnings.
        self.assertEqual(avvisi, [])
        self.assertEqual(comandi[0][comandi[0].index("--output-dir") + 1], str(cartella))
        self.assertEqual(prodotte, [(
            "larice",
            self.listini["larice"].resolve(),
            cartella / "ORDINE_LARICE_listino_larice.xlsx",
        )])
        self.assertIn(cartella / "ORDINE_LARICE_listino_larice.xlsx", generati)
        self.assertFalse(any(self.output_dir.glob("*.xlsx")))

    def test_una_rinomina_impossibile_non_butta_via_un_ordine_giusto(self) -> None:
        """A failing `os.rename` must not fail the whole compile.

        On Windows, `PermissionError` from `os.rename` can come from an
        antivirus scan, OneDrive sync, or the file being open in Excel. The
        compile must still succeed, keeping the document under its ugly
        technical name and reporting why.
        """

        def rename_che_fallisce(sorgente: Any, destinazione: Any) -> None:
            raise PermissionError(32, "Impossibile accedere al file. Il file è in uso")

        with mock.patch.object(SERVER.os, "rename", rename_che_fallisce):
            esito = self.compila_con_listini(2)

        self.assertTrue(esito["ok"])
        self.assertEqual(esito["status"], "FILES_READY")
        self.assertIn("è rimasto con questo nome", esito["message"])
        self.assertIn("Il file è in uso", esito["message"])

        # The document is there, attributed to its supplier, and deliverable;
        # only the readable name is missing.
        audit = self.audit_di(esito)
        listini = [riga for riga in audit["file"] if riga["tipo"] == "listino"]
        self.assertEqual(len(listini), 1)
        self.assertEqual(listini[0]["nome"], "ORDINE_LARICE_listino_larice.xlsx")
        self.assertEqual(listini[0]["fornitore"], "larice")
        self.assertEqual(listini[0]["origine"], str(self.listini["larice"].resolve()))
        self.assertTrue(any("è rimasto con questo nome" in avviso for avviso in audit["avvisi"]))
        self.assertEqual(esito["zipUrl"], f"/ordini/{esito['cartella']}/zip")



class ElencoDeiProdottiDaReperireTests(ConsegnaBase):
    """Compile also produces the list of items that couldn't be ordered.

    The file is created only if it has at least one row, lives in the same
    dated folder as the compiled price lists, and is downloadable both on its
    own and inside the zip: whoever downloads the zip is preparing the week's
    order, and that list is part of it.
    """

    def con_un_introvabile(self, quantita: int = 4) -> dict[str, Any]:
        """Add to the comparison a product no supplier carries."""

        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        review["products"].append({
            "id": "product:introvabile",
            "kind": "PRODUCT",
            "itemType": "product",
            "sourceRow": 77,
            "ean": "8009171901446",
            "name": "OLIO EXTRAVERGINE 1L",
            "description": "OLIO EXTRAVERGINE 1L",
            "lastUnitPrice": 6.4,
            "quantity": quantita,
            "suggestedQuantity": quantita,
            "selectedSupplierId": "",
            "confirmed": False,
            "requiresConfirmation": False,
            "offers": [{
                "supplierId": "larice",
                "available": False,
                "status": "NON_TROVATO",
                "matchStatus": "NON_TROVATO",
            }],
            "components": [],
        })
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        istantanea = self.snapshot(2)
        istantanea["products"].append({
            "id": "product:introvabile",
            "quantity": quantita,
            "selectedSupplierId": "",
            "confirmed": False,
        })
        return istantanea

    def setUp(self) -> None:
        super().setUp()
        # The real service: the zip and single-download tests must go through
        # the routes, not a filter reimplemented in the test.

        class HandlerMuto(SERVER.AppHandler):
            def log_message(self, format_string: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), HandlerMuto)
        self.httpd.store = self.store
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.ferma_il_servizio)
        self.porta = self.httpd.server_address[1]

    def ferma_il_servizio(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def chiedi(self, percorso: str) -> tuple[int, bytes, Any]:
        connessione = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=15)
        try:
            connessione.request("GET", percorso)
            risposta = connessione.getresponse()
            return int(risposta.status), risposta.read(), risposta.headers
        finally:
            connessione.close()

    def compila(self, istantanea: dict[str, Any]) -> dict[str, Any]:
        with mock.patch.object(self.store, "writer_config", None):
            return self.store.compile(istantanea)

    def nome_dell_elenco(self, esito: dict[str, Any]) -> str:
        nomi = [item["nome"] for item in self.audit_di(esito)["file"]
                if item["tipo"] == "da_reperire"]
        self.assertEqual(len(nomi), 1, self.audit_di(esito)["file"])
        return nomi[0]

    def test_il_file_nasce_e_l_audit_lo_dichiara_per_quello_che_e(self) -> None:
        esito = self.compila(self.con_un_introvabile())

        nome = self.nome_dell_elenco(esito)
        self.assertTrue(nome.startswith("Prodotti da reperire "), nome)
        self.assertTrue((self.cartella_di(esito) / nome).is_file())

    def test_dentro_ci_sono_il_prodotto_i_colli_e_il_motivo(self) -> None:
        esito = self.compila(self.con_un_introvabile(4))

        libro = load_workbook(self.cartella_di(esito) / self.nome_dell_elenco(esito))
        try:
            foglio = libro.active
            intestazioni = [cella.value for cella in foglio[1]]
            riga = [cella.value for cella in foglio[2]]
        finally:
            libro.close()
        self.assertEqual(intestazioni, [
            "EAN", "Descrizione", "Colli richiesti",
            "Ultimo prezzo noto", "Motivo", "Note",
        ])
        self.assertEqual(riga[0], "8009171901446")
        self.assertEqual(riga[1], "OLIO EXTRAVERGINE 1L")
        self.assertEqual(riga[2], 4)
        self.assertEqual(riga[3], 6.4)
        self.assertIn("Nessun fornitore", str(riga[4]))

    def test_chi_non_si_puo_ordinare_non_finisce_nel_piano(self) -> None:
        """No price-list row exists to write a quantity into."""

        esito = self.compila(self.con_un_introvabile())

        piano = json.loads((self.cartella_di(esito) / "final_order_plan.json").read_text(encoding="utf-8"))
        self.assertEqual([voce["product_id"] for voce in piano["orders"]], ["product-standard"])

    def test_senza_prodotti_da_reperire_il_file_non_nasce(self) -> None:
        """No box, no message, no file: the normal case."""

        esito = self.compila(self.snapshot(2))

        tipi = [item["tipo"] for item in self.audit_di(esito)["file"]]
        self.assertNotIn("da_reperire", tipi)
        nomi = [percorso.name for percorso in self.cartella_di(esito).iterdir()]
        self.assertFalse([nome for nome in nomi if nome.startswith("Prodotti da reperire")], nomi)

    def test_non_ordinare_lo_toglie_anche_dall_elenco(self) -> None:
        istantanea = self.con_un_introvabile()
        istantanea["products"][-1]["excluded"] = True

        esito = self.compila(istantanea)

        self.assertNotIn("da_reperire", [item["tipo"] for item in self.audit_di(esito)["file"]])

    def test_se_tutto_e_introvabile_la_compilazione_non_e_un_ordine_vuoto(self) -> None:
        """An unreachable product isn't a zero-quantity order: compile must still succeed."""

        istantanea = self.con_un_introvabile()
        istantanea["products"][0]["quantity"] = 0

        esito = self.compila(istantanea)

        self.assertTrue((self.cartella_di(esito) / self.nome_dell_elenco(esito)).is_file())
        piano = json.loads((self.cartella_di(esito) / "final_order_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(piano["orders"], [])

    def test_soli_introvabili_non_fanno_scattare_le_soglie_dei_fornitori(self) -> None:
        """All-unreachable products reach the threshold check with every supplier at zero.

        None of them can be below threshold: nothing is being ordered from
        anyone.
        """

        istantanea = self.con_un_introvabile()
        istantanea["products"][0]["quantity"] = 0
        istantanea["acceptBelowThreshold"] = False

        esito = self.compila(istantanea)

        self.assertTrue((self.cartella_di(esito) / self.nome_dell_elenco(esito)).is_file())

    def test_un_ordine_davvero_vuoto_resta_rifiutato(self) -> None:
        """The other side: nothing to order and nothing to source means no compile."""

        with self.assertRaises(SnapshotError) as errore:
            self.compila(self.snapshot(0))

        self.assertEqual({voce.get("code") for voce in errore.exception.errors}, {"ORDINE_VUOTO"})

    def test_lo_zip_della_rotta_porta_dentro_l_elenco(self) -> None:
        """Must go through the real route, not a type filter reimplemented in the test.

        Reimplementing the filter and calling `zip_in_memoria` directly would
        still pass if `servi_zip` were narrowed back to price lists only,
        because the test would just be measuring itself.
        """

        esito = self.compila(self.con_un_introvabile())
        atteso = self.nome_dell_elenco(esito)

        stato, corpo, intestazioni = self.chiedi(esito["zipUrl"])

        self.assertEqual(stato, 200, corpo[:200])
        self.assertEqual(intestazioni["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(corpo)) as archivio:
            self.assertIn(atteso, archivio.namelist())
            # Not just the name: the content must match the file on disk.
            self.assertEqual(archivio.read(atteso), (self.cartella_di(esito) / atteso).read_bytes())

    def test_il_singolo_elenco_si_scarica_anche_da_solo(self) -> None:
        esito = self.compila(self.con_un_introvabile())
        nome = self.nome_dell_elenco(esito)

        stato, corpo, _ = self.chiedi(
            f"/ordini/{esito['cartella']}/{urllib.parse.quote(nome, safe='')}"
        )

        self.assertEqual(stato, 200, corpo[:200])
        self.assertEqual(corpo, (self.cartella_di(esito) / nome).read_bytes())

    def test_una_compilazione_di_soli_introvabili_resta_scaricabile(self) -> None:
        """A compile with only unreachable products must still have a download link."""

        istantanea = self.con_un_introvabile()
        istantanea["products"][0]["quantity"] = 0

        voce = consegna.voce(self.cartella_di(self.compila(istantanea)))

        self.assertIsNotNone(voce["zipUrl"])

    def test_l_elenco_non_si_conta_fra_i_listini(self) -> None:
        # The page reads this number for "N price lists"; a list of items
        # nobody has isn't a price list, and counting it would tell the
        # operator there's one more document to send.
        voce = consegna.voce(self.cartella_di(self.compila(self.con_un_introvabile())))

        self.assertEqual(voce["listini"], 0)
        self.assertIsNotNone(voce["zipUrl"])
        self.assertIn("da_reperire", [riga["tipo"] for riga in voce["file"]])


class ConsegnaHttpTests(ConsegnaBase):
    """Delivery routes exercised over real HTTP, not by calling `do_GET` directly.

    The path decoding matters: `route = unquote(...)` turns
    `..%2f..%2fsecrets.json` into `../../secrets.json` before the path
    guards see it. Calling the handler directly would skip that step.
    """

    PAROLA_SEGRETA = "PAROLA-SEGRETA-DA-NON-CONSEGNARE"

    def setUp(self) -> None:
        super().setUp()
        # The traversal targets must exist for real: a missing file would let
        # the test pass even with the guard disabled.
        for percorso in (self.root / "secrets.json", self.dati / "secrets.json"):
            percorso.write_text(json.dumps({"api_key": self.PAROLA_SEGRETA}), encoding="utf-8")

        class HandlerMuto(SERVER.AppHandler):
            def log_message(self, format_string: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), HandlerMuto)
        self.httpd.store = self.store
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.ferma_il_servizio)
        self.porta = self.httpd.server_address[1]

    def ferma_il_servizio(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def chiedi(self, percorso: str) -> tuple[int, bytes, Any]:
        """Issue a GET with the path exactly as given, unnormalized.

        Uses `http.client` rather than `urllib.request`, which normalizes
        `..` segments before sending and would change what the service
        actually receives.
        """

        connessione = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=15)
        try:
            connessione.request("GET", percorso)
            risposta = connessione.getresponse()
            return int(risposta.status), risposta.read(), risposta.headers
        finally:
            connessione.close()

    def invia(self, percorso: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        connessione = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=15)
        try:
            corpo = json.dumps(payload).encode("utf-8")
            connessione.request(
                "POST",
                percorso,
                body=corpo,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            risposta = connessione.getresponse()
            return int(risposta.status), json.loads(risposta.read().decode("utf-8"))
        finally:
            connessione.close()

    def test_api_ordini_elenca_le_compilazioni_dalla_piu_recente(self) -> None:
        prima = self.compila_con_listini(2)
        seconda = self.compila_senza_listini(3)

        stato, corpo, _ = self.chiedi("/api/ordini")
        elenco = json.loads(corpo.decode("utf-8"))

        self.assertEqual(stato, 200)
        self.assertTrue(elenco["ok"])
        self.assertEqual(
            [voce["cartella"] for voce in elenco["compilazioni"]],
            [seconda["cartella"], prima["cartella"]],
        )
        recente, vecchia = elenco["compilazioni"]
        self.assertEqual(recente["listini"], 0)
        self.assertIsNone(recente["zipUrl"])
        self.assertEqual(vecchia["listini"], 1)
        self.assertEqual(vecchia["zipUrl"], f"/ordini/{prima['cartella']}/zip")
        self.assertTrue(vecchia["completa"])
        # The supplier name comes from `supplier_label` in server.py, not
        # from consegna.py's uppercased-id fallback.
        self.assertEqual(vecchia["fornitori"], [{"id": "larice", "nome": "LARICE", "totaleNetto": 18.0}])
        self.assertEqual(vecchia["righe"], 1)

    def test_api_elimina_compilazione_e_promemoria_insieme(self) -> None:
        esito = self.compila_con_listini(2)

        stato, corpo = self.invia("/api/ordini/elimina", {"cartella": esito["cartella"]})

        self.assertEqual(stato, 200)
        self.assertTrue(corpo["ok"])
        self.assertEqual(corpo["promemoriaRimossi"], 1)
        self.assertFalse(self.cartella_di(esito).exists())

    def test_api_elimina_un_solo_listino_caricato(self) -> None:
        self.store.upload({"files": [{
            "name": "fornitore.csv",
            "data": base64.b64encode(b"ean;prodotto\n8000000000001;PRIMO\n").decode("ascii"),
        }]})

        stato, corpo = self.invia("/api/uploads/elimina", {"name": "fornitore.csv"})

        self.assertEqual(stato, 200)
        self.assertTrue(corpo["ok"])
        self.assertFalse((self.store.upload_dir / "fornitore.csv").exists())

    def test_api_registra_la_risposta_alla_proposta_del_fornitore(self) -> None:
        review = self.dati_del_confronto()
        product = next(item for item in review["products"] if item["id"] == "product-standard")
        candidate = {
            "supplierId": "larice",
            "supplierName": "LARICE",
            "available": True,
            "description": "PROPOSTA LARICE",
            "sourceRow": 77,
            "unitPriceNet": 1.3,
            "quantityFactor": 6,
            "orderUnitPriceNet": 7.8,
            "candidateKey": "cand-http-77",
        }
        product["offers"] = [{
            "supplierId": "larice",
            "available": False,
            "method": "AI_RIFIUTATO",
            "rejectedCandidate": candidate,
        }]
        product["warnings"] = [{
            "code": "RIFIUTO_CON_CANDIDATO_FORTE",
            "supplierId": "larice",
            "candidateKey": "cand-http-77",
        }]
        self.review_path.write_text(json.dumps(review), encoding="utf-8")
        self.store.save_state(self.snapshot(0))

        stato, corpo = self.invia("/api/matches/answer", {
            "runId": "run-sintetica",
            "productId": "product-standard",
            "supplierId": "larice",
            "candidateKey": "cand-http-77",
            "accepted": True,
        })

        self.assertEqual(stato, 200)
        self.assertTrue(corpo["ok"])
        refreshed = self.store.review()
        refreshed_product = next(item for item in refreshed["products"] if item["id"] == "product-standard")
        self.assertTrue(refreshed_product["offers"][0]["available"])
        self.assertEqual(refreshed_product["offers"][0]["sourceRow"], 77)

    def test_api_rifiuta_i_percorsi_costruiti_nelle_cancellazioni(self) -> None:
        outside = self.dati / "non-toccare.csv"
        outside.write_bytes(b"resta")

        stato_compilazione, _ = self.invia("/api/ordini/elimina", {"cartella": "../dati"})
        stato_listino, _ = self.invia("/api/uploads/elimina", {"name": "../non-toccare.csv"})

        self.assertEqual(stato_compilazione, 400)
        self.assertEqual(stato_listino, 400)
        self.assertEqual(outside.read_bytes(), b"resta")

    def test_un_documento_della_compilazione_si_scarica(self) -> None:
        esito = self.compila_con_listini()
        cartella = self.cartella_di(esito)
        nome = self.nome_del_listino(esito)

        stato, corpo, intestazioni = self.chiedi(
            f"/ordini/{esito['cartella']}/{urllib.parse.quote(nome, safe='')}"
        )

        self.assertEqual(stato, 200)
        self.assertEqual(corpo, (cartella / nome).read_bytes())

    def test_il_nome_con_l_em_dash_non_uccide_la_risposta(self) -> None:
        """A filename with an em dash can't go straight into `Content-Disposition`.

        `attachment; filename="Ordine LARICE — 12 agosto 2026.xlsx"` isn't
        latin-1-encodable, and `BaseHTTPRequestHandler` headers are latin-1:
        without a fallback the route would fail while writing the header,
        after the body was already promised.
        """

        esito = self.compila_con_listini()
        nome = self.nome_del_listino(esito)
        self.assertIn("—", nome)

        stato, corpo, intestazioni = self.chiedi(
            f"/ordini/{esito['cartella']}/{urllib.parse.quote(nome, safe='')}"
        )

        self.assertEqual(stato, 200)
        self.assertTrue(corpo)
        disposizione = intestazioni["Content-Disposition"]
        self.assertIn("filename*=UTF-8''", disposizione)
        self.assertIn("Ordine%20LARICE%20%E2%80%94", disposizione)
        # The quoted fallback must be ASCII, and the whole header latin-1
        # encodable: proof the response could actually be sent.
        self.assertNotIn("—", disposizione)
        disposizione.encode("latin-1")

    def test_anche_la_vecchia_rotta_outputs_regge_un_nome_non_ascii(self) -> None:
        """`serve_file` is shared: the fix applies to every route."""

        nome = "Richiesta — prova.json"
        (self.output_dir / nome).write_bytes(b'{"ok": true}')

        stato, corpo, intestazioni = self.chiedi(f"/outputs/{urllib.parse.quote(nome, safe='')}")

        self.assertEqual(stato, 200)
        self.assertEqual(corpo, b'{"ok": true}')
        self.assertIn("filename*=UTF-8''", intestazioni["Content-Disposition"])

    def test_un_documento_vero_di_outputs_si_scarica_ancora_con_la_difesa_nuova(self) -> None:
        """`consegna.file_sicuro` must not block a legitimate file.

        Covers the positive case for the containment check on `/outputs/`: a
        file that genuinely exists in the folder must still download.
        """

        nome = "listino_pronto.xlsx"
        contenuto = b"contenuto vero del documento"
        (self.output_dir / nome).write_bytes(contenuto)

        stato, corpo, _ = self.chiedi(f"/outputs/{urllib.parse.quote(nome, safe='')}")

        self.assertEqual(stato, 200)
        self.assertEqual(corpo, contenuto)

    def test_un_collegamento_simbolico_dentro_outputs_non_esce_dalla_cartella(self) -> None:
        """The `/outputs/` escape route is a symlink, not a `../` traversal.

        `.name` alone already strips `../` segments, so classic traversal
        never worked. What's missing without the containment check after
        `resolve()` (which `consegna.file_sicuro` already applies to the
        sibling route `/ordini/<cartella>/<nome>`) is protection against a
        symlink placed inside `outputs/` that resolves outside it.
        """

        bersaglio = self.root / "segreto-fuori-da-outputs.txt"
        bersaglio.write_text("SEGRETO-FUORI-DA-OUTPUTS", encoding="utf-8")
        collegamento = self.output_dir / "link.txt"
        collegamento.symlink_to(bersaglio)

        stato, corpo, _ = self.chiedi("/outputs/link.txt")

        self.assertEqual(stato, 404)
        self.assertNotIn("SEGRETO-FUORI-DA-OUTPUTS", corpo.decode("utf-8"))

    def test_un_percorso_costruito_a_mano_non_esce_dalla_cartella(self) -> None:
        esito = self.compila_con_listini()
        cartella = esito["cartella"]
        # Precondition: the target files exist and are actually readable.
        self.assertIn(self.PAROLA_SEGRETA, (self.root / "secrets.json").read_text(encoding="utf-8"))
        self.assertIn(self.PAROLA_SEGRETA, (self.dati / "secrets.json").read_text(encoding="utf-8"))
        self.assertTrue((self.orders_dir / ".." / ".." / "secrets.json").resolve().is_file())

        percorsi = [
            "/ordini/../../secrets.json",
            "/ordini/..%2f..%2fsecrets.json",
            "/ordini/..%2F..%2Fsecrets.json",
            f"/ordini/{cartella}/../../secrets.json",
            f"/ordini/{cartella}/..%2f..%2fsecrets.json",
            f"/ordini/{cartella}/../secrets.json",
            f"/ordini/{cartella}",
            "/ordini/",
            f"/ordini/{cartella}/",
            f"/ordini/{cartella}/final_order_plan.json/",
            "/ordini/./secrets.json",
        ]
        for percorso in percorsi:
            with self.subTest(percorso=percorso):
                stato, corpo, _ = self.chiedi(percorso)
                self.assertEqual(stato, 404)
                self.assertNotIn(self.PAROLA_SEGRETA, corpo.decode("utf-8"))
                # The error message doesn't echo the requested path: that
                # would confirm to an attacker what they tried.
                self.assertNotIn("secrets", corpo.decode("utf-8"))

    def test_il_flusso_alternativo_ntfs_non_consegna_l_audit(self) -> None:
        esito = self.compila_con_listini()
        cartella = esito["cartella"]

        for coda in (":$DATA", "::$DATA"):
            with self.subTest(coda=coda):
                stato, corpo, _ = self.chiedi(
                    f"/ordini/{cartella}/{urllib.parse.quote('compilazione.json' + coda, safe='')}"
                )
                self.assertEqual(stato, 404)
                self.assertNotIn("schema_audit", corpo.decode("utf-8"))

    def test_una_compilazione_che_non_esiste_e_un_404_senza_dettagli(self) -> None:
        esito = self.compila_con_listini()

        stato, corpo, _ = self.chiedi("/ordini/2000-01-01_0000/final_order_plan.json")
        self.assertEqual(stato, 404)
        self.assertEqual(json.loads(corpo.decode("utf-8"))["message"], "Compilazione non trovata")

        stato, corpo, _ = self.chiedi(f"/ordini/{esito['cartella']}/inventato.xlsx")
        self.assertEqual(stato, 404)
        self.assertEqual(json.loads(corpo.decode("utf-8"))["message"], "Documento non trovato")

    def test_lo_zip_contiene_solo_i_listini_con_i_nomi_leggibili(self) -> None:
        with self.orologio_fermo():
            esito = self.compila_con_listini(2, ("betulla", "larice"), sorpresa="sorpresa.xlsx")

        stato, corpo, intestazioni = self.chiedi(esito["zipUrl"])

        self.assertEqual(stato, 200)
        self.assertEqual(intestazioni["Content-Type"], "application/zip")
        self.assertEqual(int(intestazioni["Content-Length"]), len(corpo))
        self.assertIn("Listini%20pronti%20per%20invio", intestazioni["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(corpo)) as archivio:
            self.assertEqual(
                sorted(archivio.namelist()),
                ["Ordine BETULLA — 12 agosto 2026.xlsx", "Ordine LARICE — 12 agosto 2026.xlsx"],
            )
            # Neither the plan nor the unexpected file is inside.
            for nome in archivio.namelist():
                self.assertEqual(
                    archivio.read(nome),
                    (self.cartella_di(esito) / nome).read_bytes(),
                )

    def test_un_listino_allegato_a_una_mail_non_ferma_la_consegna_degli_altri(self) -> None:
        """One file moved out of the folder (e.g. attached to an email) must not block the rest of the zip.

        If the file list came from the audit rather than a fresh disk scan,
        the first missing document would abort the whole zip, even though the
        other supplier's file is still there and ready to send.
        """

        with self.orologio_fermo():
            esito = self.compila_con_listini(2, ("betulla", "larice"))
        spostato = self.cartella_di(esito) / "Ordine BETULLA — 12 agosto 2026.xlsx"
        self.assertTrue(spostato.is_file(), "il bersaglio dev'esserci prima di spostarlo")
        spostato.rename(self.root / spostato.name)

        stato, corpo, _ = self.chiedi(esito["zipUrl"])

        self.assertEqual(stato, 200)
        with zipfile.ZipFile(io.BytesIO(corpo)) as archivio:
            self.assertEqual(archivio.namelist(), ["Ordine LARICE — 12 agosto 2026.xlsx"])

        # The page must not report two price lists, or link to a document
        # that's no longer there.
        _stato, elenco_grezzo, _intestazioni = self.chiedi("/api/ordini")
        voce = json.loads(elenco_grezzo.decode("utf-8"))["compilazioni"][0]
        self.assertEqual(voce["listini"], 1)
        self.assertEqual(voce["mancanti"], ["Ordine BETULLA — 12 agosto 2026.xlsx"])
        self.assertNotIn(spostato.name, [riga["nome"] for riga in voce["file"]])

    def test_senza_listini_la_rotta_dello_zip_e_un_404_spiegato(self) -> None:
        esito = self.compila_senza_listini()

        self.assertIsNone(esito["zipUrl"])
        stato, corpo, _ = self.chiedi(f"/ordini/{esito['cartella']}/zip")

        self.assertEqual(stato, 404)
        self.assertEqual(
            json.loads(corpo.decode("utf-8"))["message"],
            "In questa compilazione non ci sono listini da scaricare",
        )


class RottePipelineTests(unittest.TestCase):
    """The two recompute routes, exercised over the network.

    The point isn't that they respond: it's that a second start request,
    while the first is still running, gets a 409 instead of launching a
    second pipeline run over the same files.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        review_path = self.root / "review_data.json"
        review_path.write_text(json.dumps(synthetic_review()), encoding="utf-8")
        (self.root / "uploads").mkdir()
        # A file is required: without one the pipeline stops immediately with
        # "no document" and releases the lock before the second request
        # arrives, making the test pass for the wrong reason.
        (self.root / "uploads" / "finto.xlsx").write_bytes(b"non importa")
        self.store = SERVER.ReviewStore(
            review_path, self.root / "state.json", self.root / "uploads", self.root / "outputs"
        )
        self.sblocca = threading.Event()

        def esecutore_bloccato(comando, cartella, avanzamento, **extra):
            self.sblocca.wait(timeout=15)
            raise RuntimeError("questa prova non arriva mai in fondo")

        self.store.pipeline_jobs.esecutore = esecutore_bloccato
        self.addCleanup(self.sblocca.set)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), SERVER.AppHandler)
        self.httpd.store = self.store
        self.httpd.impostazioni = SERVER.ServizioImpostazioni(
            percorso_secrets=self.root / "secrets.json",
            percorso_impostazioni=self.root / "impostazioni.json",
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.ferma)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def ferma(self) -> None:
        self.sblocca.set()
        self.store.pipeline_jobs.attendi(timeout=15)
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def chiedi(self, percorso: str, corpo: bytes | None = None) -> tuple[int, dict]:
        richiesta = urllib.request.Request(
            self.base_url + percorso,
            data=corpo,
            headers={"Content-Type": "application/json"} if corpo is not None else {},
            method="POST" if corpo is not None else "GET",
        )
        try:
            with urllib.request.urlopen(richiesta, timeout=10) as risposta:
                return risposta.status, json.loads(risposta.read().decode("utf-8"))
        except urllib.error.HTTPError as errore:
            return errore.code, json.loads(errore.read().decode("utf-8"))

    def test_lo_stato_iniziale_dice_che_non_c_e_niente_in_corso(self) -> None:
        codice, corpo = self.chiedi("/api/pipeline/stato")
        self.assertEqual(codice, 200)
        self.assertEqual(corpo["stato"], "IN_ATTESA")
        self.assertEqual(len(corpo["fasi"]), 9)

    def test_le_rotte_della_configurazione_schemi_restituiscono_anteprima_e_verifica(self) -> None:
        self.store.schemi_pendenti = lambda: {
            "ok": True, "required": True, "runId": "run-1", "documents": [{"profileId": "uno"}]
        }
        self.store.valida_schemi = lambda payload: {
            "ok": payload.get("runId") == "run-1", "documents": [{"profileId": "uno", "rowsUsable": 10}]
        }

        codice, anteprima = self.chiedi("/api/schemas/pending")
        codice_verifica, verifica = self.chiedi(
            "/api/schemas/validate", json.dumps({"runId": "run-1"}).encode("utf-8")
        )

        self.assertEqual(codice, 200)
        self.assertTrue(anteprima["required"])
        self.assertEqual(codice_verifica, 200)
        self.assertEqual(verifica["documents"][0]["rowsUsable"], 10)

    def test_la_conferma_delle_colonne_risponde_accepted_e_restituisce_la_pipeline(self) -> None:
        self.store.conferma_schemi = lambda payload: {
            "ok": True,
            "validation": {"ok": True},
            "pipeline": {"stato": "IN_CORSO", "runId": "run-2"},
        }

        codice, corpo = self.chiedi(
            "/api/schemas/confirm", json.dumps({"runId": "run-1"}).encode("utf-8")
        )

        self.assertEqual(codice, 202)
        self.assertEqual(corpo["pipeline"]["stato"], "IN_CORSO")

    def test_l_avvio_torna_subito_e_il_secondo_riceve_un_409(self) -> None:
        codice, corpo = self.chiedi("/api/pipeline/avvia", b"{}")
        self.assertEqual(codice, 202)
        self.assertEqual(corpo["stato"], "IN_CORSO")
        codice_due, corpo_due = self.chiedi("/api/pipeline/avvia", b"{}")
        self.assertEqual(codice_due, 409)
        self.assertFalse(corpo_due["ok"])

    def test_le_rotte_del_sito_noce_non_esistono_piu(self) -> None:
        """Pins the removal of the Noce site-scraper routes.

        Without this test, the scraper routes (and the credentials and cart
        logic behind them) could silently reappear.
        """

        codice_stato, _ = self.chiedi("/api/noce/status")
        codice_avvio, _ = self.chiedi("/api/noce/start", b"{}")
        codice_carrello, _ = self.chiedi("/api/noce/carrello/prepara", b"{}")
        self.assertEqual((codice_stato, codice_avvio, codice_carrello), (404, 404, 404))

    def test_lo_stato_finale_resta_ripescabile_a_run_conclusa(self) -> None:
        """The status route must keep reporting the outcome after the run ends.

        The page relies on this to show stops and warnings on reload; if the
        route stopped reporting the last outcome, no page fix could recover it.
        """

        codice, _ = self.chiedi("/api/pipeline/avvia", b"{}")
        self.assertEqual(codice, 202)
        self.sblocca.set()
        self.store.pipeline_jobs.attendi(timeout=15)

        codice, corpo = self.chiedi("/api/pipeline/stato")

        self.assertEqual(codice, 200)
        self.assertEqual(corpo["stato"], "ERRORE")
        self.assertTrue(corpo["fermata"], corpo)
        self.assertTrue(corpo["messaggio"])
        self.assertIn("avvisi", corpo)

        # Also survives a service restart: state lives on disk, not just in
        # the process memory of whichever run produced it.
        riavviato = SERVER.ReviewStore(
            self.root / "review_data.json",
            self.root / "state.json",
            self.root / "uploads",
            self.root / "outputs",
        )
        ripescato = riavviato.stato_pipeline()
        self.assertEqual(ripescato["stato"], "ERRORE")
        self.assertTrue(ripescato["fermata"], ripescato)

    def test_il_lucchetto_dei_lavori_arriva_alla_pipeline_dal_negozio(self) -> None:
        """`ReviewStore` owns the job lock, and the pipeline must receive that same instance.

        A `PipelineJobManager` with its own lock would stop mutually
        excluding with a second job writing `review_data.json`; a
        behavioral test wouldn't catch it as long as there's only one job.
        """

        self.assertIs(self.store.pipeline_jobs.lucchetto_lavori, self.store.lucchetto_lavori)


class LaVersionePubblicataInApiHealthTests(unittest.TestCase):
    """`/api/health` is what the launcher checks to decide whether to reuse the service.

    It must carry `versionePubblicata` even when its value is `null`,
    because the page distinguishes "unknown" from "field absent".
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        review_path = self.root / "review_data.json"
        review_path.write_text(json.dumps(synthetic_review()), encoding="utf-8")
        (self.root / "uploads").mkdir()
        self.store = ReviewStore(
            review_path, self.root / "state.json", self.root / "uploads", self.root / "outputs"
        )

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), SERVER.AppHandler)
        self.httpd.store = self.store
        self.httpd.impostazioni = SERVER.ServizioImpostazioni(
            percorso_secrets=self.root / "secrets.json",
            percorso_impostazioni=self.root / "impostazioni.json",
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.ferma)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def ferma(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def test_la_chiave_ce_anche_quando_non_si_conosce_la_data(self) -> None:
        with mock.patch.object(SERVER.versione_del_codice, "pubblicata", return_value=None):
            with urllib.request.urlopen(self.base_url + "/api/health", timeout=15) as risposta:
                corpo = json.loads(risposta.read().decode("utf-8"))

        self.assertIn("versionePubblicata", corpo)
        self.assertIsNone(corpo["versionePubblicata"])


def listino_noce_finto(percorso: Path, righe: dict[int, str]) -> Path:
    """Build a realistic `.xls`: EAN in B, price in H, order column RK in I.

    Built with the `test_xls_reader` fixture, so it's a real OLE2 container
    with real BIFF8 records; only the content is fake, not the format.
    """

    import test_xls_reader as banco_xls

    celle = banco_xls.label(4, 1, "CodiceABarre") + banco_xls.label(4, 8, "Quantita")
    for riga, ean in righe.items():
        indice = riga - 1
        celle += banco_xls.label(indice, 1, ean)
        celle += banco_xls.label(indice, 3, f"PRODOTTO {ean}")
        celle += banco_xls.rk(indice, 7, banco_xls.rk_intero(2))
        celle += banco_xls.rk(indice, 8, banco_xls.rk_intero(0))
    percorso.write_bytes(banco_xls.costruisci_xls([("Foglio1", celle)]))
    return percorso


class LaProceduraDiScritturaVieneDallaRegolaTests(ConsegnaBase):
    """The write rule decides how to compile, not the supplier's name.

    The choice between the Node writer and an in-place `.xls` patch must
    come from the adapter registry's `order_write.mode`, surfaced by
    `launcher.source_rule` as `compilazione`; hardcoding it against a
    specific supplier name would break for a new supplier that sends `.xls`,
    the exact case the adapter-learning flow promises to support.
    """

    def setUp(self) -> None:
        super().setUp()
        self.listino_xls = listino_noce_finto(
            self.root / "listino_fornitore_nuovo.xls", {20: "8000000000010", 21: "8000000000099"}
        )
        self.regola = {
            "sheet": "Foglio1",
            "order_column": "I",
            "data_start_row": 6,
            "header_row": 5,
            "ean_column_name": "CodiceABarre",
            "source_sha256": hashlib.sha256(self.listino_xls.read_bytes()).hexdigest(),
            "compilazione": "patch_xls_in_posizione",
        }
        self.scrivi_configurazione()

    def scrivi_configurazione(self, **cambiamenti: Any) -> None:
        regola = {**self.regola, **cambiamenti}
        self.writer_config.write_text(json.dumps({
            "run_id": "run-sintetica",
            "supplier_files": {
                **{nome: str(percorso) for nome, percorso in self.listini.items()},
                "nuovo_fornitore": str(self.listino_xls),
            },
            "supplier_write_rules": {"nuovo_fornitore": regola},
        }), encoding="utf-8")

    @staticmethod
    def dati_del_confronto() -> dict[str, Any]:
        review = ConsegnaBase.dati_del_confronto()
        review["suppliers"].append({"id": "nuovo_fornitore", "name": "Fornitore Nuovo", "minimumOrder": 0})
        standard = next(item for item in review["products"] if item["id"] == "product-standard")
        standard["offers"].append({
            "supplierId": "nuovo_fornitore",
            "available": True,
            "description": "PRODOTTO STANDARD",
            "sourceRow": 20,
            "ean": "8000000000010",
            "unitPriceNet": 1.1,
            "quantityFactor": 6,
            "orderUnitPriceNet": 6.6,
            "method": "EAN",
            "confidence": "CERTA",
        })
        return review

    def test_un_fornitore_qualsiasi_che_lo_dichiara_si_compila_in_posizione(self) -> None:
        """Any supplier that declares in-place `.xls` patching gets it, not only a hardcoded name."""

        esito = self.store.compile(self.snapshot(3, "nuovo_fornitore"))

        self.assertEqual(esito["status"], "FILES_READY", esito["message"])
        listini = [voce["name"] for voce in esito["outputs"] if voce["name"].casefold().endswith(".xls")]
        self.assertEqual(len(listini), 1, [voce["name"] for voce in esito["outputs"]])
        # The copy is the supplier's own document, not a converted one: same
        # extension, with the quantity written into the declared column.
        copia = next(
            percorso for percorso in self.cartella_di(esito).iterdir()
            if percorso.suffix.casefold() == ".xls"
        )
        from xls_reader import read_workbook

        foglio = read_workbook(copia)[0]
        self.assertEqual(foglio.rows[19][8][0], 3)

    def test_senza_la_dichiarazione_lo_stesso_xls_accusa_la_configurazione(self) -> None:
        """A rule missing `compilazione` must blame the configuration, not the supplier's file.

        Without `compilazione`, the `.xls` falls through to the Node writer
        (the default path), which can't write to `.xls`. The message must
        point at the configuration, since the supplier's document is the one
        thing the operator can't change; it also states the fix the operator
        can apply directly, saving the file as `.xlsx` in Excel.
        """

        regola = {chiave: valore for chiave, valore in self.regola.items() if chiave != "compilazione"}
        self.writer_config.write_text(json.dumps({
            "run_id": "run-sintetica",
            "supplier_files": {"nuovo_fornitore": str(self.listino_xls)},
            "supplier_write_rules": {"nuovo_fornitore": regola},
        }), encoding="utf-8")

        esito = self.store.compile(self.snapshot(3, "nuovo_fornitore"))

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non dichiara come compilarlo", esito["message"])
        self.assertNotIn("non è disponibile in formato XLSX", esito["message"])
        # The fix the operator can apply without help.
        self.assertIn("salvalo come «Cartella di lavoro di Excel (.xlsx)»", esito["message"])
        self.assertNotIn("va riconfigurato dalla pagina", esito["message"])


class CompilazioneNoceTests(ConsegnaBase):
    """Compiling for Noce: their own document, with only the order column touched.

    Runs the real `app/xls_writer.py` against a real `.xls`, not a stub.
    Verifies that the compiled copy keeps its readable name and correct
    extension, and that both guards — row EAN and fixed-length column layout
    — stop the compile rather than deliver a wrong order.
    """

    def setUp(self) -> None:
        super().setUp()
        self.noce = listino_noce_finto(
            self.root / "formattato_104233.xls", {20: "8000000000010", 21: "8000000000099"}
        )
        self.scrivi_configurazione()

    def scrivi_configurazione(self, **cambiamenti: Any) -> None:
        regola = {
            "sheet": "Foglio1",
            "order_column": "I",
            "data_start_row": 6,
            "header_row": 5,
            "ean_column_name": "CodiceABarre",
            "source_sha256": hashlib.sha256(self.noce.read_bytes()).hexdigest(),
            # It's the rule that decides how to compile, not the supplier
            # name: `launcher.regola_noce` writes this key, and the service
            # reads it. A fixture without it would exercise a code path the
            # real configuration never produces.
            "compilazione": "patch_xls_in_posizione",
        }
        regola.update(cambiamenti)
        self.writer_config.write_text(json.dumps({
            "run_id": "run-sintetica",
            "supplier_files": {
                **{nome: str(percorso) for nome, percorso in self.listini.items()},
                "noce": str(self.noce),
            },
            "supplier_write_rules": {"noce": regola},
        }), encoding="utf-8")

    def compila(self, quantita: int = 3) -> dict[str, Any]:
        return self.store.compile(self.snapshot(quantita, "noce"))

    def test_la_copia_esce_con_il_nome_leggibile_e_resta_un_xls(self) -> None:
        esito = self.compila()
        self.assertEqual(esito["status"], "FILES_READY", esito["message"])
        nomi = [voce["name"] for voce in esito["outputs"]]
        listini = [nome for nome in nomi if nome.casefold().endswith(".xls")]
        self.assertEqual(len(listini), 1, nomi)
        self.assertTrue(listini[0].startswith("Ordine NOCE "))

    def test_la_quantita_arriva_nella_riga_del_piano(self) -> None:
        import xls_reader

        esito = self.compila(quantita=4)
        cartella = self.orders_dir / esito["cartella"]
        copia = next(percorso for percorso in cartella.iterdir() if percorso.suffix == ".xls")
        griglia = xls_reader.read_workbook(copia)[0].rows
        self.assertEqual(griglia[19][8][0], 4)
        self.assertEqual(griglia[20][8][0], 0)

    def test_l_originale_del_fornitore_non_viene_toccato(self) -> None:
        prima = self.noce.read_bytes()
        self.compila()
        self.assertEqual(self.noce.read_bytes(), prima)

    def test_un_ean_diverso_ferma_la_compilazione_e_lo_dice(self) -> None:
        """A row whose EAN no longer matches the expected product must stop the compile."""

        listino_noce_finto(self.noce, {20: "8009999999999", 21: "8000000000099"})
        self.scrivi_configurazione()
        esito = self.compila()
        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("8000000000010", esito["message"])
        cartella = self.orders_dir / esito["cartella"]
        self.assertEqual([p.name for p in cartella.iterdir() if p.suffix == ".xls"], [])

    def test_un_listino_cambiato_dopo_la_verifica_ferma_la_compilazione(self) -> None:
        self.scrivi_configurazione(source_sha256="0" * 64)
        esito = self.compila()
        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("cambiato dopo la verifica", esito["message"])

    def test_una_colonna_d_ordine_non_compilabile_ferma_prima_di_scrivere(self) -> None:
        """A formula in the order column, instead of a fixed-length number, must stop the compile."""

        import test_xls_reader as banco_xls

        celle = banco_xls.label(4, 1, "CodiceABarre") + banco_xls.label(4, 8, "Quantita")
        celle += banco_xls.label(19, 1, "8000000000010")
        celle += banco_xls.rk(19, 7, banco_xls.rk_intero(2))
        celle += banco_xls.formula(19, 8, banco_xls.formula_numero(0.0))
        self.noce.write_bytes(banco_xls.costruisci_xls([("Foglio1", celle)]))
        self.scrivi_configurazione()
        esito = self.compila()
        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non sono numeri a lunghezza fissa", esito["message"])

    def piano_con_riga_noce(self) -> dict[str, Any]:
        """Build an order plan with a Noce row.

        Exercises `writer_configuration_issues` against the Noce price-list
        checks. `run_id` is required because the real program always writes
        it into every plan (from the comparison run that produced it);
        without it, the run guard would stop the compile before reaching the
        checks under test here.
        """
        return {"run_id": "run-sintetica", "orders": [{
            "supplier": "noce",
            "supplier_source_row": 20,
            "supplier_ean": "8000000000010",
            "quantity": 3,
        }]}

    def test_il_controllo_preventivo_vede_l_impronta_diversa(self) -> None:
        """The fingerprint guard runs at two points, each with its own test.

        The pre-write check and the compile-time check report the same
        message; without a test for each, disabling one would leave the
        suite green while that guard was silently open.
        """

        self.scrivi_configurazione(source_sha256="0" * 64)
        problemi = self.store.writer_configuration_issues(self.piano_con_riga_noce())
        self.assertTrue(any("cambiato dopo la verifica" in voce for voce in problemi), problemi)

    def test_il_controllo_preventivo_passa_su_un_listino_intatto(self) -> None:
        self.assertEqual(self.store.writer_configuration_issues(self.piano_con_riga_noce()), [])

    def configurazione_larice(self, impronta: str) -> dict[str, Any]:
        """Build a config with only LARICE, using the given fingerprint."""

        return {
            "run_id": "run-sintetica",
            "supplier_files": {"larice": str(self.listini["larice"])},
            "supplier_write_rules": {"larice": {
                "sheet": "LARICE",
                "order_column": "D",
                "data_start_row": 2,
                "source_sha256": impronta,
            }},
        }

    @staticmethod
    def piano_con_riga_larice() -> dict[str, Any]:
        return {"run_id": "run-sintetica", "orders": [{
            "supplier": "larice",
            "supplier_source_row": 2,
            "quantity": 1,
        }]}

    def test_l_impronta_si_verifica_per_ogni_fornitore_non_solo_per_cipresso(self) -> None:
        """The fingerprint check must run for every supplier, not one hardcoded name.

        Replacing a supplier's file under the same filename, without a
        recompute, leaves the run guard blind: the file path is unchanged
        and the run id is the same. Only the fingerprint (which
        `source_rule` always writes, for every supplier) can catch it; if the
        check were scoped to a single supplier, quantities from an old
        comparison would land on the wrong rows of the new price list and the
        copy would still come out deliverable.
        """

        self.writer_config.write_text(
            json.dumps(self.configurazione_larice("0" * 64)), encoding="utf-8",
        )
        problemi = self.store.writer_configuration_issues(self.piano_con_riga_larice())
        self.assertTrue(
            any("cambiato dopo l'ultimo confronto" in voce for voce in problemi), problemi,
        )
        self.assertTrue(any("LARICE" in voce for voce in problemi), problemi)

    def test_un_larice_intatto_non_viene_fermato_dall_impronta(self) -> None:
        """The other branch: an unchanged file must not trip the fingerprint check.

        Only the fingerprint-related warnings are asserted here: this minimal
        config doesn't declare Node or the writer, so other warnings are
        expected and legitimate; asserting "no warnings" would measure those
        instead of what this test targets.
        """

        impronta = hashlib.sha256(self.listini["larice"].read_bytes()).hexdigest()
        self.writer_config.write_text(
            json.dumps(self.configurazione_larice(impronta)), encoding="utf-8",
        )
        problemi = self.store.writer_configuration_issues(self.piano_con_riga_larice())
        self.assertFalse(
            [voce for voce in problemi if "cambiato dopo l'ultimo confronto" in voce],
            problemi,
        )
        self.assertFalse([voce for voce in problemi if "impronta" in voce.casefold()], problemi)

    def test_senza_la_regola_del_registro_non_si_compila(self) -> None:
        self.writer_config.write_text(json.dumps({
            "run_id": "run-sintetica",
            "supplier_files": {"noce": str(self.noce)},
        }), encoding="utf-8")
        esito = self.compila()
        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("regola di scrittura", esito["message"])

    def test_la_guardia_sulla_run_vale_anche_per_noce(self) -> None:
        """NOCE doesn't go through Node, but reads its price list from the same config.

        Its copy is an in-place `.xls` patch: a stale run leaves the rows
        pointing at another document's data, exactly as for the other
        suppliers. The run guard can't be scoped to writer-based suppliers
        only.
        """

        self.scrivi_configurazione()
        config = json.loads(self.writer_config.read_text(encoding="utf-8"))
        config["run_id"] = "run-della-settimana-scorsa"
        self.writer_config.write_text(json.dumps(config), encoding="utf-8")

        esito = self.compila()

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non appartiene allo stesso confronto", esito["message"])
        self.assertFalse(list(self.cartella_di(esito).glob("*.xls")))


class LaConfigurazioneDiUnAltraRun(ConsegnaBase):
    """Guards against compiling with a stale `writer_config.json` after a recompute.

    A recompute replaces the price lists with different files, names and
    rows. If `writer_config.json` still points at the previous run, the
    compile writes today's quantities into last week's rows, silently: the
    fingerprint check alone can't catch this, since it only verifies a file
    hasn't changed since it was checked, which stays true even for a stale
    but internally consistent file.

    Verifies that the two `run_id` values are compared before any writer
    runs, and that every way this comparison could go unanswered fails
    closed.
    """

    def setUp(self) -> None:
        super().setUp()
        # A complete config: the only thing left that can stop the compile
        # is the run check, which is what these tests exercise.
        self.config = json.loads(self.writer_config.read_text(encoding="utf-8"))
        self.config["node_executable"] = str(self.root / "node.exe")
        self.config["writer_script"] = str(self.root / "writer.mjs")
        (self.root / "node.exe").write_bytes(b"")
        (self.root / "writer.mjs").write_bytes(b"")
        self.scrivi_config()
        self.scrittore = ScrittoreFinto({"larice": self.listini["larice"]})

    def scrivi_config(self, **cambiamenti: Any) -> None:
        config = {**self.config, **cambiamenti}
        for chiave, valore in list(config.items()):
            if valore is None:
                config.pop(chiave)
        self.writer_config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def compila(self) -> dict[str, Any]:
        with mock.patch.object(self.store, "run_writer", self.scrittore):
            return self.store.compile(self.snapshot(2, "larice"))

    def scrivi_confronto(self, run_id: str | None) -> None:
        confronto = self.dati_del_confronto()
        if run_id is None:
            confronto.pop("run")
        else:
            confronto["run"] = {**confronto["run"], "id": run_id}
        self.review_path.write_text(json.dumps(confronto), encoding="utf-8")

    def test_una_configurazione_di_un_altra_run_ferma_prima_di_scrivere(self) -> None:
        self.scrivi_config(run_id="run-della-settimana-scorsa")

        esito = self.compila()

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertEqual(len(esito["writerIssues"]), 1)
        avviso = esito["writerIssues"][0]
        # The message doesn't claim to know which of the two is stale:
        # without tracking order, that would be a fabricated diagnosis.
        self.assertIn("non appartiene allo stesso confronto", avviso)
        # The fix names the user action that regenerates the configuration.
        self.assertIn("Ricalcola il confronto", avviso)
        self.assertIn(avviso, esito["message"])
        # The writer must never be invoked: the guard stops the compile first.
        self.assertEqual(self.scrittore.destinazioni, [])
        self.assertFalse(list(self.cartella_di(esito).glob("*.xlsx")))
        # Also recorded in the compile audit, not just the response.
        self.assertIn(avviso, self.audit_di(esito)["avvisi"])

    def test_una_configurazione_che_non_dichiara_la_run_ferma(self) -> None:
        """Fail closed: a config that doesn't declare its run must stop the compile.

        A config written before this field existed can't answer "does this
        belong to the current comparison?", and an unanswered question means
        no write to a supplier's price list.
        """

        self.scrivi_config(run_id=None)

        esito = self.compila()

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non dice a quale confronto appartiene", esito["writerIssues"][0])
        self.assertEqual(self.scrittore.destinazioni, [])

    def test_un_confronto_senza_run_ferma(self) -> None:
        """The other side of the same question: no recompute has ever finished."""

        self.scrivi_confronto(None)
        self.scrivi_config(run_id="run-sintetica")

        esito = self.compila()

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non dichiara da dove viene", esito["writerIssues"][0])
        self.assertEqual(self.scrittore.destinazioni, [])

    def test_due_run_vuote_non_si_annullano(self) -> None:
        """Two empty strings are equal, which doesn't mean the check should pass."""

        self.scrivi_confronto("")
        self.scrivi_config(run_id="")

        esito = self.compila()

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non dichiara da dove viene", esito["writerIssues"][0])
        self.assertEqual(self.scrittore.destinazioni, [])

    def test_con_la_stessa_run_la_compilazione_va_avanti(self) -> None:
        """A matching run must not be blocked by the guard."""

        self.scrivi_config(run_id="run-sintetica")

        esito = self.compila()

        self.assertEqual(esito["status"], "FILES_READY", esito["message"])
        self.assertEqual(esito["writerIssues"], [])
        self.assertEqual(self.scrittore.destinazioni, [self.cartella_di(esito)])

    def test_il_ricalcolo_che_finisce_durante_la_compilazione_non_apre_la_finestra(self) -> None:
        """A recompute finishing mid-compile must not reopen the stale-run window.

        The plan is built against run A; a recompute that finishes a moment
        later moves both the config and the comparison to run B. A guard
        that re-reads the comparison from disk at check time would then see
        `B == B` and pass, even though the plan it's about to write still
        describes run A — the exact bug this guard exists to prevent, with
        `writerIssues` empty. The guard must compare the config against the
        PLAN, which nothing can change underneath it. The run switch is
        injected at the point where the dated folder is created: the plan is
        already built (against run A) and the guard hasn't read anything yet,
        a window the compile genuinely passes through because the two locks
        don't exclude each other (the server is a `ThreadingHTTPServer`).
        """

        vero = SERVER.consegna.crea_cartella

        def il_ricalcolo_finisce_adesso(radice: Path, momento: Any) -> Path:
            self.scrivi_confronto("run-B")
            self.scrivi_config(run_id="run-B")
            return vero(radice, momento)

        with mock.patch.object(SERVER.consegna, "crea_cartella", il_ricalcolo_finisce_adesso):
            esito = self.compila()

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non appartiene allo stesso confronto", esito["writerIssues"][0])
        self.assertEqual(self.scrittore.destinazioni, [])
        self.assertFalse(list(self.cartella_di(esito).glob("*.xlsx")))

    def test_la_riconfigurazione_che_non_scrive_lo_dice(self) -> None:
        """`prepare_writer_config` returns without writing when a requirement is missing.

        Without checking that outcome, the most likely case — Node missing,
        script missing — would fail silently: no exception, no
        `COMPILAZIONE_DA_RICONFIGURARE` warning, and the stale configuration
        left in place. The `run_id` guard would still keep the compile
        closed, but the operator should be told immediately, not left to hit
        that guard later.
        """

        import launcher as modulo_launcher

        setup_muto = modulo_launcher.WriterSetup(
            config_path=None,
            node_runtime=None,
            message="Writer XLSX non attivato; manca: Node 18+",
        )
        with mock.patch.object(
            modulo_launcher, "prepare_writer_config", return_value=setup_muto
        ) as preparazione:
            with self.assertRaises(RuntimeError) as errore:
                self.store.riconfigura_compilazione(self.dati_del_confronto())

        self.assertIn("manca", str(errore.exception))
        preparazione.assert_called_once()


class NegozioSintetico(unittest.TestCase):
    """Shared fixture for the tests below: a purpose-built synthetic comparison."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.root = Path(temporanea.name)
        self.run_dir = self.root / "run-corrente"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.review_path = self.root / "review_data.json"

    def negozio(self, review: dict[str, Any]) -> Any:
        self.review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        return ReviewStore(
            self.review_path,
            self.run_dir / "review_state.json",
            self.root / "uploads",
            self.root / "outputs",
        )

    @staticmethod
    def offerta(supplier: str, prezzo: float, *, fattore: int = 6, disponibile: bool = True, promozione: str = "") -> dict[str, Any]:
        offerta = {
            "supplierId": supplier,
            "available": disponibile,
            "description": "PRODOTTO",
            "sourceRow": 10,
            "ean": "8000000000010",
            "unitPriceNet": round(prezzo / fattore, 6),
            "quantityFactor": fattore,
            "orderUnitPriceNet": prezzo,
            "method": "EAN",
            "confidence": "CERTA",
        }
        if promozione:
            offerta["promotionText"] = promozione
        return offerta

    @staticmethod
    def prodotto(product_id: str, nome: str, ean: str, offerte: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": product_id,
            "kind": "PRODUCT",
            "itemType": "product",
            "sourceRow": 10,
            "ean": ean,
            "name": nome,
            "description": nome,
            "quantity": 0,
            "selectedSupplierId": offerte[0]["supplierId"] if offerte else "",
            "confirmed": True,
            "requiresConfirmation": False,
            "offers": offerte,
            "components": [],
        }

    @staticmethod
    def confronto(prodotti: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "run": {"id": "run-sintetica", "status": "ready", "label": "Prova"},
            "files": [],
            "suppliers": [
                {"id": "larice", "name": "Larice", "minimumOrder": 0},
                {"id": "betulla", "name": "Betulla", "minimumOrder": 0},
            ],
            "products": prodotti,
            "warnings": [],
        }

    @staticmethod
    def scelte(quantita: dict[str, tuple[str, int]]) -> dict[str, Any]:
        return {
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [
                {"id": pid, "quantity": q, "selectedSupplierId": fornitore, "confirmed": True}
                for pid, (fornitore, q) in quantita.items()
            ],
        }


class ScontoDelFornitoreTests(NegozioSintetico):
    """A flat percentage discount applied across a supplier's whole price list.

    The discount is applied after the price lists load and before the
    operator starts choosing suppliers. It silently reassigns each product to
    the cheapest supplier (the operator is assumed to know what they're
    doing) and silently expires on the next recompute.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store = self.negozio(self.confronto([
            self.prodotto("product:1", "PRIMO", "8000000000001", [
                self.offerta("larice", 12.0),
                self.offerta("betulla", 12.6),
            ]),
            self.prodotto("product:2", "SECONDO", "8000000000002", [
                self.offerta("larice", 10.0),
                self.offerta("betulla", 30.0),
            ]),
        ]))
        self.store.state_path.write_text(json.dumps({
            "runId": "run-sintetica",
            "stateVersion": 3,
            "products": [
                {"id": "product:1", "quantity": 2, "selectedSupplierId": "larice", "confirmed": True},
                {"id": "product:2", "quantity": 1, "selectedSupplierId": "larice", "confirmed": True},
            ],
        }), encoding="utf-8")

    def prezzi(self, product_id: str) -> dict[str, float]:
        prodotto = next(p for p in self.store.review()["products"] if p["id"] == product_id)
        return {o["supplierId"]: o["orderUnitPriceNet"] for o in prodotto["offers"]}

    def test_lo_sconto_scende_su_tutte_le_offerte_del_fornitore(self) -> None:
        self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})

        primo = self.prezzi("product:1")
        self.assertAlmostEqual(primo["betulla"], 11.844, places=4)
        # Other suppliers are untouched.
        self.assertAlmostEqual(primo["larice"], 12.0, places=4)
        self.assertAlmostEqual(self.prezzi("product:2")["betulla"], 28.2, places=4)

    def test_riassegna_anche_i_prodotti_senza_decisione_salvata(self) -> None:
        """Must reassign every product, even one with no saved decision yet.

        Reaching the discount field normally requires a save that writes a
        decision for every product, so the decision map is rarely empty. But
        a failed save can leave it partially empty, and the discount must
        still reassign every affected product rather than silently skipping
        the ones without a saved decision.
        """

        self.store.state_path.write_text(json.dumps({
            "runId": "run-sintetica", "stateVersion": 3, "products": [],
        }), encoding="utf-8")

        esito = self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})

        self.assertEqual(esito["reassigned"], 1)
        scelti = {p["id"]: p["selectedSupplierId"] for p in self.store.review()["products"]}
        self.assertEqual(scelti["product:1"], "betulla")
        self.assertEqual(scelti["product:2"], "larice")

    def test_la_decisione_creata_dice_da_dove_viene_la_quantita(self) -> None:
        """A decision created by the discount must set `quantitySource`.

        Without it, the next recompute stops re-reading the carton count
        from the management-software export and the quantity stays frozen
        (`pipeline_jobs._ripulisci_stato`).
        """

        self.store.state_path.write_text(json.dumps({
            "runId": "run-sintetica", "stateVersion": 3, "products": [],
        }), encoding="utf-8")

        self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})

        stato = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        decisioni = {voce["id"]: voce for voce in stato["products"]}
        self.assertEqual(sorted(decisioni), ["product:1", "product:2"])
        for voce in decisioni.values():
            self.assertIn(voce["quantitySource"], {"gestionale", "utente"})
            self.assertIn("quantity", voce)
            self.assertIs(voce["excluded"], False)

    def test_riassegna_al_piu_conveniente_e_non_lo_annuncia(self) -> None:
        """At 6% off, BETULLA becomes cheapest on product:1, but not on product:2."""

        esito = self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})

        self.assertEqual(esito["reassigned"], 1)
        confronto = self.store.review()
        scelti = {p["id"]: p["selectedSupplierId"] for p in confronto["products"]}
        self.assertEqual(scelti["product:1"], "betulla")
        self.assertEqual(scelti["product:2"], "larice")
        # No warning: applying the discount is a deliberate operator action.
        codici = {voce.get("code") for voce in confronto.get("warnings") or []}
        self.assertNotIn("SCONTO_FORNITORE", codici)

    def test_i_totali_del_servizio_usano_il_prezzo_scontato(self) -> None:
        """The summary and the page must report the same number."""

        self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})

        riepilogo = self.store.review()["orderSummary"]
        betulla = next(voce for voce in riepilogo["suppliers"] if voce["supplierId"] == "betulla")
        # 2 cartons of product:1 at 11.844 = 23.688, rounded to the cent as
        # the page does: 23.69. Without the discount it would be 25.20.
        self.assertAlmostEqual(betulla["totalNet"], 23.69, places=2)

    def test_scade_con_la_run_e_non_lo_dice(self) -> None:
        self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})

        confronto = json.loads(self.review_path.read_text(encoding="utf-8"))
        confronto["run"]["id"] = "run-della-settimana-dopo"
        self.review_path.write_text(json.dumps(confronto, ensure_ascii=False), encoding="utf-8")

        dopo = self.store.review()
        prezzi = {o["supplierId"]: o["orderUnitPriceNet"] for o in dopo["products"][0]["offers"]}
        self.assertAlmostEqual(prezzi["betulla"], 12.6, places=4)
        self.assertEqual(dopo["state"]["supplierDiscounts"], {})
        self.assertFalse(dopo.get("warnings"))

    def test_a_zero_lo_sconto_sparisce(self) -> None:
        self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})

        self.store.set_supplier_discount({"supplierId": "betulla", "percent": 0})

        self.assertAlmostEqual(self.prezzi("product:1")["betulla"], 12.6, places=4)
        stato = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertEqual(stato["supplierDiscounts"], {})

    def test_una_percentuale_impossibile_si_rifiuta(self) -> None:
        for valore in (-1, 100, 250, "sei"):
            with self.assertRaises(ValueError):
                self.store.set_supplier_discount({"supplierId": "betulla", "percent": valore})

    def test_un_fornitore_che_non_c_e_si_rifiuta(self) -> None:
        with self.assertRaises(ValueError):
            self.store.set_supplier_discount({"supplierId": "acero", "percent": 6})

    def test_il_salvataggio_delle_quantita_non_cancella_lo_sconto(self) -> None:
        """`validate_snapshot` rebuilds state from scratch: the discount must be carried over."""

        self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})
        stato = json.loads(self.store.state_path.read_text(encoding="utf-8"))

        self.store.save_state({
            "runId": "run-sintetica",
            "stateVersion": stato["stateVersion"],
            "currentStep": 2,
            "products": [
                {"id": "product:1", "quantity": 2, "selectedSupplierId": "betulla", "confirmed": True},
                {"id": "product:2", "quantity": 1, "selectedSupplierId": "larice", "confirmed": True},
            ],
        })

        dopo = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertEqual(dopo["supplierDiscounts"]["betulla"]["rate"], 0.06)
        self.assertAlmostEqual(self.prezzi("product:1")["betulla"], 11.844, places=4)


class UnaConfermaCheMancaNonSpegneIlSalvataggioTests(NegozioSintetico):
    """A pending match confirmation must not block saving state.

    An unconfirmed candidate match must not make `PUT /api/state` return 422:
    quantity edits, supplier choices and exclusions on other products have to
    reach disk while confirmations are pending. A missing confirmation blocks
    compile, not save.
    """

    def setUp(self) -> None:
        super().setUp()
        prodotto = self.prodotto(
            "product:198",
            "DOPLO PIATTI DESSER 25PZ",
            "8009580477747",
            [self.offerta("larice", 14.9)],
        )
        prodotto["requiresConfirmation"] = True
        prodotto["confirmed"] = False
        self.store = self.negozio(self.confronto([prodotto]))

    def da_confermare(self, quantita: int = 3) -> dict[str, Any]:
        istantanea = self.scelte({"product:198": ("larice", quantita)})
        istantanea["products"][0]["confirmed"] = False
        return istantanea

    def test_lo_stato_si_salva_anche_con_una_conferma_in_sospeso(self) -> None:
        esito = self.store.save_state(self.da_confermare())

        self.assertTrue(esito["ok"])
        salvato = json.loads((self.run_dir / "review_state.json").read_text(encoding="utf-8"))
        voce = salvato["products"][0]
        self.assertEqual(voce["quantity"], 3)
        self.assertFalse(voce["confirmed"])

    def test_la_quantita_cambiata_dopo_arriva_sul_disco(self) -> None:
        """The failure mode is every save after the first, not just the first."""

        self.store.save_state(self.da_confermare(3))
        istantanea = self.da_confermare(7)
        istantanea["stateVersion"] = 1
        self.store.save_state(istantanea)

        salvato = json.loads((self.run_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(salvato["products"][0]["quantity"], 7)

    def test_la_compilazione_resta_ferma_finche_non_si_conferma(self) -> None:
        with self.assertRaises(SnapshotError) as errore:
            self.store.compile(self.da_confermare())

        codici = {voce.get("code") for voce in errore.exception.errors}
        self.assertIn("CONFERMA_MANCANTE", codici)
        self.assertIn("va confermata la corrispondenza", str(errore.exception))

    def test_gli_altri_rifiuti_continuano_a_fermare_il_salvataggio(self) -> None:
        """Only the confirmation gate was removed; other save-time checks still apply."""

        prodotto = self.prodotto(
            "product:199", "OLIO EXTRAVERGINE 1L", "8009614791337",
            [self.offerta("larice", 14.9, disponibile=False)],
        )
        prodotto["requiresConfirmation"] = True
        store = self.negozio(self.confronto([prodotto]))
        istantanea = self.scelte({"product:199": ("larice", 3)})
        istantanea["products"][0]["confirmed"] = False

        with self.assertRaises(SnapshotError) as errore:
            store.save_state(istantanea)

        codici = {voce.get("code") for voce in errore.exception.errors}
        self.assertIn("OFFERTA_NON_VALIDA", codici)
        self.assertNotIn("CONFERMA_MANCANTE", codici)


class ProdottiDaReperireTests(NegozioSintetico):
    """Products the management-software export asks for that no supplier carries.

    A positive quantity with no supplier available is a valid state, not an
    error to correct. That product stays out of the plan and out of every
    supplier's price list — there's no row to write it into — but it isn't
    lost: compile lists it on a separate sheet.

    The distinction these tests protect: "no supplier available" is not the
    same as "no supplier chosen yet".
    """

    def senza_nessuno(self, quantita: int = 3) -> tuple[Any, dict[str, Any]]:
        store = self.negozio(self.confronto([
            self.prodotto("product:198", "OLIO EXTRAVERGINE 1L", "8009580477747",
                          [self.offerta("larice", 14.9, disponibile=False)]),
        ]))
        return store, self.scelte({"product:198": ("", quantita)})

    def test_una_quantita_senza_nessun_fornitore_si_salva(self) -> None:
        store, scelta = self.senza_nessuno()

        esito = store.save_state(scelta)

        self.assertTrue(esito["ok"])
        salvato = json.loads((self.run_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(salvato["products"][0]["quantity"], 3)

    def test_un_fornitore_scelto_male_resta_un_errore(self) -> None:
        """The relaxed rule doesn't apply where a real decision is still needed."""

        store, _ = self.senza_nessuno()

        with self.assertRaises(SnapshotError) as errore:
            store.save_state(self.scelte({"product:198": ("larice", 3)}))

        self.assertEqual({voce["code"] for voce in errore.exception.errors}, {"OFFERTA_NON_VALIDA"})

    def test_con_qualcuno_disponibile_scegliere_resta_obbligatorio(self) -> None:
        """The other side of the boundary: a supplier is available here, and must be chosen.

        Checked at compile time, not save time: `FORNITORE_DA_SCEGLIERE` is a
        pending decision, not a save-blocking error — it happens normally
        after a "not the same item" rejection when another supplier does
        carry it. Blocking the save on this would block every other
        product's save too, and the back button (which saves before
        responding).
        """

        store = self.negozio(self.confronto([
            self.prodotto("product:198", "OLIO EXTRAVERGINE 1L", "8009580477747",
                          [self.offerta("larice", 14.9, disponibile=False),
                           self.offerta("betulla", 15.4)]),
        ]))
        scelta = self.scelte({"product:198": ("", 3)})

        store.save_state(scelta)
        _pulito, errori, _review = store.validate_snapshot(scelta, for_compile=True)

        self.assertEqual({voce["code"] for voce in errori}, {"FORNITORE_DA_SCEGLIERE"})

    def test_un_match_da_verificare_non_e_un_prodotto_introvabile(self) -> None:
        """An offer awaiting confirmation is still an offer, not an absence."""

        offerta = self.offerta("larice", 14.9)
        offerta["requiresConfirmation"] = True
        store = self.negozio(self.confronto([
            self.prodotto("product:198", "OLIO EXTRAVERGINE 1L", "8009580477747", [offerta]),
        ]))

        scelta = self.scelte({"product:198": ("", 3)})
        _pulito, errori, _review = store.validate_snapshot(scelta, for_compile=True)

        self.assertEqual({voce["code"] for voce in errori}, {"FORNITORE_DA_SCEGLIERE"})

    def test_nessuna_conferma_si_pretende_su_chi_non_ha_fornitore(self) -> None:
        """A confirmation checkbox the operator has no way to tick must never be required.

        Checked at compile time: `save_state` already drops
        `CONFERMA_MANCANTE` for a product with no available supplier
        (`app/server.py:1728`), so a save-time test would pass even with the
        compile-time guard disabled and prove nothing.
        """

        prodotto = self.prodotto("product:198", "OLIO EXTRAVERGINE 1L", "8009580477747",
                                 [self.offerta("larice", 14.9, disponibile=False)])
        prodotto["requiresConfirmation"] = True
        # A second, orderable product: gives the compile a real order to
        # produce, so any rejection can only come from the first product.
        ordinabile = self.prodotto("product:199", "PASTA 500G", "8009614791337",
                                   [self.offerta("larice", 9.0)])
        store = self.negozio(self.confronto([prodotto, ordinabile]))
        scelta = self.scelte({"product:198": ("", 3), "product:199": ("larice", 2)})
        scelta["products"][0]["confirmed"] = False

        _clean, errori, _confronto = store.validate_snapshot(scelta, for_compile=True)

        self.assertEqual([voce["code"] for voce in errori], [])

    def test_non_ordinare_lo_toglie_anche_da_qui(self) -> None:
        store, scelta = self.senza_nessuno()
        scelta["products"][0]["excluded"] = True

        store.save_state(scelta)

        salvato = json.loads((self.run_dir / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(salvato["products"][0]["quantity"], 0)


class OffertaNonValidaDiceQualeProdottoTests(NegozioSintetico):
    """The rejection rule is unchanged: what the rejection message says is what matters.

    A generic "snapshot checks failed" is useless against a comparison with
    hundreds of rows; the message must name the product.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store = self.negozio(self.confronto([
            self.prodotto(
                "product:198",
                "OLIO EXTRAVERGINE 1L",
                "8009580477747",
                [self.offerta("larice", 14.9, disponibile=False)],
            ),
        ]))

    def test_il_rifiuto_nomina_il_prodotto_e_il_fornitore(self) -> None:
        with self.assertRaises(SnapshotError) as errore:
            self.store.save_state(self.scelte({"product:198": ("larice", 3)}))

        messaggio = str(errore.exception)
        self.assertIn("OLIO EXTRAVERGINE 1L", messaggio)
        self.assertIn("LARICE", messaggio)
        # States the fix, not just that something's wrong.
        self.assertIn("quantità a zero", messaggio)
        voce = errore.exception.errors[0]
        self.assertEqual(voce["code"], "OFFERTA_NON_VALIDA")
        self.assertEqual(voce["productName"], "OLIO EXTRAVERGINE 1L")
        self.assertEqual(voce["supplierName"], "LARICE")

    def test_senza_fornitore_scelto_lo_dice_diversamente(self) -> None:
        # This product's supplier CAN carry it: a quantity without a chosen
        # supplier is only an error when there's someone to choose. With no
        # supplier available at all, it's a "to be sourced" product instead,
        # a valid state covered by `ProdottiDaReperireTests`.
        store = self.negozio(self.confronto([
            self.prodotto("product:198", "OLIO EXTRAVERGINE 1L", "8009580477747",
                          [self.offerta("larice", 14.9)]),
        ]))
        scelta = self.scelte({"product:198": ("", 3)})

        _pulito, errori, _review = store.validate_snapshot(scelta, for_compile=True)

        messaggio = str(SnapshotError(errori))
        self.assertIn("OLIO EXTRAVERGINE 1L", messaggio)
        self.assertIn("nessun fornitore", messaggio)

    def test_la_regola_di_rifiuto_non_si_allenta(self) -> None:
        """An unavailable offer is still rejected: only the message changed."""

        with self.assertRaises(SnapshotError):
            self.store.save_state(self.scelte({"product:198": ("larice", 3)}))
        self.assertFalse((self.run_dir / "review_state.json").exists())

    def test_molti_rifiuti_non_diventano_un_elenco_infinito(self) -> None:
        prodotti = [
            self.prodotto(f"product:{numero}", f"ARTICOLO {numero}", f"800000000{numero:04d}",
                          [self.offerta("larice", 9.0, disponibile=False)])
            for numero in range(1, 6)
        ]
        store = self.negozio(self.confronto(prodotti))

        with self.assertRaises(SnapshotError) as errore:
            store.save_state(self.scelte({f"product:{numero}": ("larice", 2) for numero in range(1, 6)}))

        messaggio = str(errore.exception)
        self.assertIn("ARTICOLO 1", messaggio)
        self.assertIn("E altri 2 controlli non superati.", messaggio)
        self.assertEqual(len(errore.exception.errors), 5)

    def test_la_rotta_manda_la_frase_nel_campo_che_la_pagina_mostra(self) -> None:
        class HandlerMuto(SERVER.AppHandler):
            def log_message(self, format_string: str, *args: object) -> None:
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), HandlerMuto)
        httpd.store = self.store
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def ferma() -> None:
            httpd.shutdown()
            thread.join(timeout=10)
            httpd.server_close()

        self.addCleanup(ferma)
        richiesta = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/state",
            data=json.dumps(self.scelte({"product:198": ("larice", 3)})).encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with self.assertRaises(urllib.error.HTTPError) as errore:
            urllib.request.urlopen(richiesta, timeout=15)

        self.assertEqual(errore.exception.code, 422)
        corpo = json.loads(errore.exception.read().decode("utf-8"))
        self.assertFalse(corpo["ok"])
        # `body.message` is the only field the page shows: the text must be
        # there, not just in the error codes.
        self.assertIn("OLIO EXTRAVERGINE 1L", corpo["message"])
        self.assertIn("LARICE", corpo["message"])
        self.assertEqual(corpo["errors"][0]["code"], "OFFERTA_NON_VALIDA")


class OmaggiPersiNelloSpostamentoTests(NegozioSintetico):
    """Moving quantity across a threshold can change the count of free-goods items."""

    def setUp(self) -> None:
        super().setUp()
        self.store = self.negozio(self.confronto([
            self.prodotto(
                "product:198",
                "OLIO EXTRAVERGINE 1L",
                "8009580477747",
                [
                    self.offerta("larice", 14.9, promozione="ACQUISTA 2 CT IN OMAGGIO 1 CT DI OLIO"),
                    self.offerta("betulla", 14.5),
                ],
            ),
        ]))

    def preventivo(self, quantita: int = 12) -> dict[str, Any]:
        richiesta = self.scelte({"product:198": ("larice", quantita)})
        richiesta["from"] = "larice"
        return self.store.move_preview(richiesta)

    def test_il_preventivo_dice_quanti_omaggi_si_perdono(self) -> None:
        # 12 cartons with a threshold every 2 cartons: six free items, all on LARICE.
        opzione = self.preventivo()["options"][0]

        self.assertEqual(opzione["giftsBefore"], 6)
        self.assertEqual(opzione["giftsAfter"], 0)
        self.assertEqual(opzione["giftsLost"], 6)
        self.assertEqual(opzione["giftsGained"], 0)

    def test_gli_omaggi_si_contano_anche_fornitore_per_fornitore(self) -> None:
        riga = self.preventivo()["options"][0]["supplierTotalsAfter"][0]

        self.assertEqual(riga["supplierId"], "larice")
        self.assertEqual(riga["giftsBefore"], 6)
        self.assertEqual(riga["giftsAfter"], 0)

    def test_senza_soglie_raggiunte_non_si_perde_niente(self) -> None:
        opzione = self.preventivo(quantita=1)["options"][0]

        self.assertEqual(opzione["giftsBefore"], 0)
        self.assertEqual(opzione["giftsLost"], 0)

    def test_il_valore_dell_omaggio_non_si_calcola_mai(self) -> None:
        """A free-goods item is informational only, never priced into any total."""

        opzione = self.preventivo()["options"][0]

        for chiave in opzione:
            self.assertNotIn("giftValue", chiave)
            self.assertNotIn("valoreOmaggi", chiave)


class ScartoFraTestataERigheTests(NegozioSintetico):
    """The header total and the sum of the rounded line totals don't always match, and the gap must be reported."""

    def setUp(self) -> None:
        super().setUp()
        # Prices with the decimal precision of real price lists: the sum of
        # rows rounded to the cent doesn't equal the total computed on the
        # unrounded prices.
        self.store = self.negozio(self.confronto([
            self.prodotto("product:1", "PRIMO", "8000000000001", [self.offerta("larice", 4.9975)]),
            self.prodotto("product:2", "SECONDO", "8000000000002", [self.offerta("larice", 7.3325)]),
            self.prodotto("product:3", "TERZO", "8000000000003", [self.offerta("larice", 10.125)]),
        ]))
        self.acquisti = {"product:1": ("larice", 3), "product:2": ("larice", 2), "product:3": ("larice", 1)}

    def test_il_riepilogo_dichiara_lo_scarto_da_arrotondamenti(self) -> None:
        esito = self.store.save_state(self.scelte(self.acquisti))

        riepilogo = esito["orderSummary"]
        fornitore = riepilogo["suppliers"][0]
        self.assertEqual(fornitore["supplierId"], "larice")
        self.assertEqual(fornitore["lineCount"], 3)
        # 14.9925 + 14.665 + 10.125 = 39.78 on the unrounded total, 39.79 when
        # summing the rows each already rounded to the cent: a one-cent gap
        # that must be reported even when the rows sum above the header.
        self.assertEqual(fornitore["totalNet"], 39.78)
        self.assertEqual(fornitore["linesTotalNet"], 39.79)
        self.assertEqual(fornitore["roundingDifference"], -0.01)
        self.assertEqual(riepilogo["totalNet"], 39.78)
        self.assertEqual(riepilogo["roundingDifference"], -0.01)

    def test_senza_scarto_la_differenza_e_zero_e_la_pagina_tace(self) -> None:
        store = self.negozio(self.confronto([
            self.prodotto("product:1", "PRIMO", "8000000000001", [self.offerta("larice", 5.0)]),
        ]))

        riepilogo = store.save_state(self.scelte({"product:1": ("larice", 3)}))["orderSummary"]

        self.assertEqual(riepilogo["suppliers"][0]["totalNet"], 15.0)
        self.assertEqual(riepilogo["suppliers"][0]["roundingDifference"], 0.0)
        self.assertEqual(riepilogo["roundingDifference"], 0.0)

    def test_il_riepilogo_c_e_gia_alla_prima_apertura_della_pagina(self) -> None:
        """The rounding note must appear on first load, not only after the first save."""

        self.store.save_state(self.scelte(self.acquisti))

        riepilogo = self.store.review()["orderSummary"]

        self.assertEqual(riepilogo["totalNet"], 39.78)
        self.assertEqual(riepilogo["roundingDifference"], -0.01)

    def test_il_totale_del_riepilogo_e_quello_della_compilazione(self) -> None:
        """The summary and the order plan must never report different totals."""

        riepilogo = self.store.save_state(self.scelte(self.acquisti))["orderSummary"]
        with mock.patch.object(self.store, "writer_config", None):
            esito = self.store.compile(self.scelte(self.acquisti))

        self.assertEqual(esito["totalsNet"]["larice"], riepilogo["suppliers"][0]["totalNet"])


class ScartiDelParserInPaginaTests(NegozioSintetico):
    """Rows the parser discards must be reported all the way to the page."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def test_il_confronto_porta_l_audit_fino_alla_rotta(self) -> None:
        review = self.confronto([
            self.prodotto("product:1", "PRIMO", "8000000000001", [self.offerta("larice", 5.0)]),
        ])
        review["auditSummary"] = {
            "sources": {"betulla": {"rows": 6379, "duplicate_ean_values": 23}},
            "inputs": [{
                "supplier_id": "betulla",
                "role": "supplier",
                "records": 6379,
                "rows_not_orderable": {"OMAGGIO": 8},
                "reading": {"rows_excluded": {"food": 4}, "rows_kept": 6379},
            }],
        }
        store = self.negozio(review)

        uscita = store.review()

        self.assertEqual(uscita["auditSummary"]["inputs"][0]["rows_not_orderable"], {"OMAGGIO": 8})
        self.assertEqual(uscita["auditSummary"]["sources"]["betulla"]["duplicate_ean_values"], 23)

    def test_la_pagina_non_butta_piu_l_audit(self) -> None:
        normalizzazione = self.app_js.split("function normalizeReview(")[1].split("\n}\n")[0]
        self.assertIn("discardedRows: normalizeDiscardedRows(", normalizzazione)
        self.assertIn("payload.auditSummary ?? payload.audit_summary", normalizzazione)

    def test_il_conteggio_distingue_non_ordinabili_ed_esclusi(self) -> None:
        corpo = self.app_js.split("function normalizeDiscardedRows(")[1].split("\n}\n")[0]
        self.assertIn("rows_not_orderable", corpo)
        self.assertIn("rows_excluded", corpo)
        # The numbers come straight from the audit: the page derives none.
        self.assertNotIn("rowsKept -", corpo)

    def test_la_frase_dice_quante_righe_e_perche(self) -> None:
        corpo = self.app_js.split("function discardedRowsText(")[1].split("\n}\n")[0]
        self.assertIn("non ordinabili", corpo)
        self.assertIn("escluse dal filtro", corpo)
        self.assertIn("righe scartate", corpo)

    def test_i_codici_a_barre_ripetuti_si_dicono(self) -> None:
        """`duplicate_ean_values` explains why two seemingly identical products are separate rows."""

        corpo = self.app_js.split("function normalizeDiscardedRows(")[1].split("\n}\n")[0]
        self.assertIn("duplicate_ean_values", corpo)
        riquadro = self.app_js.split("function renderDiscardedRowsPanel(")[1].split("\n}\n")[0]
        self.assertIn("codici a barre ripetuti", riquadro)

    def test_il_riquadro_sta_nella_pagina_dei_documenti(self) -> None:
        passo = self.app_js.split("function renderUploadStep(")[1].split("\n}\n")[0]
        self.assertIn("renderDiscardedRowsPanel()", passo)

    def test_i_dettagli_tecnici_restano_chiusi_finche_non_servono(self) -> None:
        # The panel's open state isn't hardcoded in the markup: `apribile()`
        # writes `open` only if the operator opened it before. It starts
        # closed because that memory starts empty, and stays open across
        # redraws (which run about once per second while the pipeline is
        # active). Behavioral coverage lives in
        # `tests/test_interfaccia_pagina1.py::SezioniCheRestanoAperte`.
        riquadro = self.app_js.split("function renderDiscardedRowsPanel(")[1].split("\n}\n")[0]
        self.assertIn('<details class="panel import-panel audit-disclosure" ${apribile("righe-scartate")}>', riquadro)
        self.assertNotIn('<details open', riquadro)


class LaDomandaDelleConsegneInPaginaTests(unittest.TestCase):
    """Step 2's pending-delivery question: three possible answers, and a question that can resurface."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def test_la_terza_risposta_esiste_e_chiude_l_ordine(self) -> None:
        self.assertIn('data-action="history-never"', self.app_js)
        # The label must state that the action is irreversible.
        self.assertIn("Annullato, non arriva", self.app_js)
        invio = self.app_js.split("async function answerPendingOrder(")[1].split("\n}\n")[0]
        self.assertIn("{ orderId, closed: true }", invio)

    def test_il_ritorno_della_domanda_lo_decide_il_servizio(self) -> None:
        corpo = self.app_js.split("function domandaRimandata(")[1].split("\n}\n")[0]
        self.assertIn("entry.askAgainAt", corpo)
        # Must not fall back to a browser-local "valid for today" rule.
        self.assertNotIn("toDateString()", self.app_js)
        self.assertIn("askAgainAt", self.app_js.split("function normalizePendingOrder(")[1].split("\n}\n")[0])

    def test_lo_scarto_da_arrotondamenti_non_si_stampa_piu_sulla_pagina(self) -> None:
        """A one-cent rounding gap isn't information the operator needs to see.

        `roundingDifference` still comes back in the service response — and
        is covered above — but no line on the page displays it.
        """

        # Checked by the function and class names a rendering would use, not
        # by its text, so the assertion doesn't depend on UI wording.
        self.assertNotIn("roundingNote", self.app_js)
        self.assertNotIn("renderRoundingNote", self.app_js)
        self.assertNotIn("rounding-note", self.app_js)
        self.assertNotIn("rounding-note", (SKILL_ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8"))

    def test_gli_omaggi_persi_si_leggono_nello_spostamento(self) -> None:
        corpo = self.app_js.split("function moveGiftNotes(")[1].split("\n}\n")[0]
        self.assertIn("option?.giftsLost", corpo)
        self.assertIn("omaggi", corpo)
        opzione = self.app_js.split("function renderSupplierMoveOption(")[1].split("\n}\n")[0]
        self.assertIn("moveGiftNotes(option)", opzione)


class IlRegistroEUnaVeritaSolaTests(unittest.TestCase):
    """Supplier display names and write rules come from the adapter registry, not hardcoded in the code.

    A learned supplier's display name and write rule must both come from a
    single source: hardcoding either one elsewhere risks it diverging from
    what the registry declares.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.radice = Path(self.temporary.name)

    def registro_finto(self, adattatori: list[dict[str, Any]]) -> Path:
        percorso = self.radice / "adapters.json"
        percorso.write_text(json.dumps({"adapters": adattatori}, ensure_ascii=False), encoding="utf-8")
        return percorso

    def test_i_quattro_fornitori_di_oggi_si_chiamano_come_prima(self) -> None:
        for identificativo, atteso in (
            ("betulla", "BETULLA"), ("larice", "LARICE"), ("cipresso", "CIPRESSO"), ("noce", "NOCE"),
        ):
            with self.subTest(fornitore=identificativo):
                self.assertEqual(supplier_label(identificativo), atteso)

    def test_un_fornitore_imparato_si_chiama_come_dice_il_registro(self) -> None:
        percorso = self.registro_finto([
            {"id": "nuovo_v1", "supplier_id": "nuovo_fornitore", "display_name": "Alimentari Rossi", "kind": "supplier"},
        ])

        self.assertEqual(registro_adattatori.nome_del_fornitore("nuovo_fornitore", percorso), "Alimentari Rossi")

    def test_senza_dichiarazione_l_underscore_non_arriva_all_utente(self) -> None:
        """A raw identifier like "NUOVO_FORNITORE" in a sentence reads as a bug, not a name."""

        self.assertEqual(supplier_label("nuovo_fornitore"), "NUOVO FORNITORE")
        self.assertEqual(supplier_label(""), "FORNITORE")

    def test_fra_due_adattatori_dello_stesso_fornitore_vince_il_nome_del_fornitore(self) -> None:
        """The shorter, plain supplier name wins over a longer name describing a specific document."""

        percorso = self.registro_finto([
            {"id": "tizio_csv_v1", "supplier_id": "tizio", "display_name": "TIZIO", "kind": "supplier"},
            {"id": "tizio_xls_v1", "supplier_id": "tizio", "display_name": "TIZIO listino Excel 97-2003", "kind": "supplier"},
        ])

        self.assertEqual(registro_adattatori.nome_del_fornitore("tizio", percorso), "TIZIO")

    def test_la_regola_di_scrittura_di_oggi_non_cambia(self) -> None:
        betulla = ReviewStore.default_write_rule("betulla") or {}
        larice = ReviewStore.default_write_rule("larice") or {}

        self.assertEqual(betulla.get("order_column"), "C")
        self.assertEqual(betulla.get("sheet"), "FIRST")
        self.assertEqual(betulla.get("data_start_row"), 2)
        self.assertEqual(larice.get("order_column"), "D")
        self.assertEqual(larice.get("sheet"), "FIRST")
        self.assertEqual(larice.get("data_start_row"), 2)

    def test_chi_scrive_dalla_mappatura_confermata_non_ha_una_regola_predefinita(self) -> None:
        """For CIPRESSO and Noce, sheet and rows come from the operator's mapping, not a default rule."""

        self.assertIsNone(ReviewStore.default_write_rule("cipresso"))
        self.assertIsNone(ReviewStore.default_write_rule("noce"))

    def test_un_fornitore_imparato_eredita_la_regola_che_il_registro_dichiara(self) -> None:
        percorso = self.registro_finto([
            {
                "id": "nuovo_v1", "supplier_id": "nuovo_fornitore", "display_name": "NUOVO", "kind": "supplier",
                "order_write": {"sheet": "FIRST", "data_start_row": 2, "order_column": "F"},
            },
        ])

        regola = ReviewStore.default_write_rule("nuovo_fornitore", percorso)

        self.assertEqual(regola, {"sheet": "FIRST", "data_start_row": 2, "order_column": "F"})

    def test_un_registro_che_non_dichiara_la_scrittura_non_inventa_una_regola(self) -> None:
        percorso = self.registro_finto([
            {"id": "muto_v1", "supplier_id": "muto", "display_name": "MUTO", "kind": "supplier"},
        ])

        self.assertIsNone(ReviewStore.default_write_rule("muto", percorso))


class LaRegolaDiScritturaDiceDoveControllareTests(unittest.TestCase):
    """The writer must be able to verify a row carries the right product.

    Without knowing where EAN and description sit, the writer could compile
    against a mismatched row. The adapter registry already declares those
    positions — by letter for Larice, by header name for BETULLA — and the
    write rule carries that position through to the writer.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.radice = Path(self.temporary.name)
        self.launcher = importa_launcher()

    def listino_betulla(self) -> Path:
        percorso = self.radice / "betulla.xlsx"
        workbook = Workbook()
        foglio = workbook.active
        foglio.append(["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva"])
        foglio.append(["8000000000011", "C-1", None, "PASTA MEZZE MANICHE", 6, 2.5, None, 22])
        workbook.save(percorso)
        workbook.close()
        return percorso

    def listino_larice(self) -> Path:
        percorso = self.radice / "larice.xlsx"
        workbook = Workbook()
        foglio = workbook.active
        for _ in range(2):
            foglio.append([None] * 18)
        workbook.save(percorso)
        workbook.close()
        return percorso

    def test_su_betulla_le_colonne_si_risolvono_dal_nome_dell_intestazione(self) -> None:
        regola, avviso = self.launcher.source_rule("betulla", self.listino_betulla(), {}, registro_adattatori.adattatore("betulla_v1"))

        self.assertIsNone(avviso)
        self.assertEqual(regola["verify"], {"ean_column": "A", "description_column": "D"})

    def test_su_larice_le_colonne_sono_le_lettere_dichiarate_dal_registro(self) -> None:
        """Larice has no header row: the registry declares column letters directly."""

        regola, avviso = self.launcher.source_rule("larice", self.listino_larice(), {}, registro_adattatori.adattatore("larice_v1"))

        self.assertIsNone(avviso)
        self.assertEqual(regola["verify"], {"ean_column": "R", "description_column": "G"})

    def test_la_regola_porta_anche_il_nome_leggibile_del_fornitore(self) -> None:
        """The Node writer never reads the registry: the config is its only input."""

        regola, _avviso = self.launcher.source_rule("betulla", self.listino_betulla(), {}, registro_adattatori.adattatore("betulla_v1"))

        self.assertEqual(regola["display_name"], "BETULLA")

    def test_un_registro_che_non_dice_dove_guardare_non_fa_inventare_una_colonna(self) -> None:
        adattatore = dict(registro_adattatori.adattatore("betulla_v1"))
        adattatore.pop("header_aliases", None)
        adattatore.pop("column_map", None)

        regola, avviso = self.launcher.source_rule("betulla", self.listino_betulla(), {}, adattatore)

        self.assertIsNone(avviso)
        self.assertNotIn("verify", regola)


class IlFornitoreScelloDalProgrammaSiRifaTests(unittest.TestCase):
    """A stale automatic choice must not survive a recompute that made a cheaper supplier available.

    `build_products` selects the cheapest supplier by default; without
    tracking who made a saved choice, a recompute that adds a cheaper
    supplier would keep the order on the old one, since the saved decision
    looks the same whether the operator or the program made it.

    The distinction is inferred rather than asked of the page: if a saved
    choice happens to match the cheapest supplier, the program already made
    that choice on its own, so it can only be the automatic default.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.review_path = self.root / "review_data.json"
        self.state_path = self.root / "review_state.json"
        self.review_path.write_text(json.dumps({
            "run": {"id": "run-sintetica"},
            "suppliers": [{"id": "betulla", "name": "BETULLA"}, {"id": "noce", "name": "NOCE"}],
            "products": [{
                "id": "product:1", "name": "PRODOTTO", "quantity": 3,
                "quantitySource": "gestionale", "excluded": False,
                # The recomputed comparison already picked the cheapest supplier.
                "selectedSupplierId": "noce", "confirmed": True,
                "offers": [
                    {"supplierId": "betulla", "supplierName": "BETULLA", "available": True,
                     "unitPriceNet": 2.0, "quantityFactor": 6, "orderUnitPriceNet": 12.0},
                    {"supplierId": "noce", "supplierName": "NOCE", "available": True,
                     "unitPriceNet": 1.0, "quantityFactor": 6, "orderUnitPriceNet": 6.0},
                ],
            }],
        }), encoding="utf-8")
        self.store = ReviewStore(self.review_path, self.state_path,
                                 self.root / "uploads", self.root / "outputs")

    def _salva_decisione(self, fornitore: str, fonte: str | None) -> None:
        decisione = {"id": "product:1", "quantity": 3, "selectedSupplierId": fornitore,
                     "confirmed": True, "excluded": False, "quantitySource": "gestionale"}
        if fonte is not None:
            decisione["selectedSupplierSource"] = fonte
        self.state_path.write_text(json.dumps({
            "runId": "run-sintetica", "stateVersion": 1, "schemaVersion": 1,
            "products": [decisione],
        }), encoding="utf-8")

    def _avvisi(self, confronto: dict[str, Any]) -> set[str]:
        return {str(voce.get("code")) for voce in confronto.get("warnings") or []}

    def test_il_default_vecchio_lascia_il_posto_al_piu_conveniente(self) -> None:
        self._salva_decisione("betulla", "automatico")

        confronto = self.store.review()

        self.assertEqual(confronto["products"][0]["selectedSupplierId"], "noce")
        self.assertIn("FORNITORE_PIU_CONVENIENTE_RIPRESO", self._avvisi(confronto))

    def test_una_decisione_senza_marchio_vale_automatica(self) -> None:
        """A decision saved before this field existed is treated as automatic."""

        self._salva_decisione("betulla", None)

        confronto = self.store.review()

        self.assertEqual(confronto["products"][0]["selectedSupplierId"], "noce")

    def test_la_scelta_dell_utente_non_si_tocca(self) -> None:
        self._salva_decisione("betulla", "utente")

        confronto = self.store.review()

        self.assertEqual(confronto["products"][0]["selectedSupplierId"], "betulla")
        # No warning: nothing was changed.
        self.assertNotIn("FORNITORE_PIU_CONVENIENTE_RIPRESO", self._avvisi(confronto))

    def test_cambiando_fornitore_la_conferma_di_prima_non_vale_piu(self) -> None:
        """A confirmation covers the previously offered item, which just changed."""

        self._salva_decisione("betulla", "automatico")

        prodotto = self.store.review()["products"][0]

        self.assertEqual(prodotto["selectedSupplierId"], "noce")
        self.assertFalse(prodotto["confirmed"])

    def test_col_fornitore_scelto_a_mano_la_quantita_resta_dell_utente(self) -> None:
        """A manually chosen supplier must not reset the manually entered quantity's source.

        `quantitySource` must be preserved even on the "user chose this
        supplier" branch; otherwise a manually entered quantity would
        re-declare itself as coming from the management-software export on
        the next load, and a command that clears only default quantities
        would wipe it out.
        """

        self.state_path.write_text(json.dumps({
            "runId": "run-sintetica", "stateVersion": 1, "schemaVersion": 1,
            "products": [{
                "id": "product:1", "quantity": 7, "selectedSupplierId": "betulla",
                "selectedSupplierSource": "utente", "confirmed": True, "excluded": False,
                "quantitySource": "utente",
            }],
        }), encoding="utf-8")

        prodotto = self.store.review()["products"][0]

        self.assertEqual(prodotto["selectedSupplierId"], "betulla")
        self.assertEqual(prodotto["quantity"], 7)
        self.assertEqual(prodotto["quantitySource"], "utente")

    def test_il_marchio_lo_mette_il_salvataggio_deducendolo(self) -> None:
        """Choosing the cheapest supplier is tagged automatic; choosing another isn't."""

        istantanea = {
            "runId": "run-sintetica", "stateVersion": 0, "currentStep": 2,
            "products": [{"id": "product:1", "quantity": 3, "selectedSupplierId": "noce",
                          "confirmed": True, "excluded": False, "quantitySource": "gestionale"}],
        }
        self.store.save_state(istantanea)
        salvato = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(salvato["products"][0]["selectedSupplierSource"], "automatico")

        istantanea["stateVersion"] = salvato["stateVersion"]
        istantanea["products"][0]["selectedSupplierId"] = "betulla"
        self.store.save_state(istantanea)
        salvato = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(salvato["products"][0]["selectedSupplierSource"], "utente")


class DueSchedeNonSiCancellanoIlLavoroTests(unittest.TestCase):
    """With two tabs open on the same comparison, the second save must not silently erase the first's work.

    `save_state` rewrites the whole state, and the run check alone doesn't
    distinguish two tabs on the same comparison: without a version check, the
    tab that autosaves last (every 450 ms) would overwrite the other tab's
    quantities with a snapshot that never had them, and neither tab would be
    told.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.review_path = self.root / "review_data.json"
        self.run_dir = self.root / "run-corrente"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "review_state.json"
        self.upload_dir = self.root / "uploads"
        self.output_dir = self.root / "outputs"
        self.orders_dir = self.root / "ordini"
        self.review_path.write_text(json.dumps(synthetic_review()), encoding="utf-8")
        self.store = ReviewStore(self.review_path, self.state_path, self.upload_dir, self.output_dir)

    def cartelle(self) -> list[Path]:
        if not self.orders_dir.is_dir():
            return []
        return sorted(item for item in self.orders_dir.iterdir() if item.is_dir())

    def scheda(self, *, versione: int | None, display: int, standard: int) -> dict[str, Any]:
        """Build the snapshot a browser tab sends: always the full product list."""

        istantanea: dict[str, Any] = {
            "runId": "run-sintetica",
            "currentStep": 2,
            "acceptBelowThreshold": False,
            "products": [
                {"id": "display-solbao-96", "quantity": display, "selectedSupplierId": "larice", "confirmed": True},
                {"id": "product-standard", "quantity": standard, "selectedSupplierId": "larice", "confirmed": True},
            ],
        }
        if versione is not None:
            istantanea["stateVersion"] = versione
        return istantanea

    def quantita_salvate(self) -> dict[str, int]:
        stato = json.loads(self.state_path.read_text(encoding="utf-8"))
        return {str(item["id"]): int(item["quantity"]) for item in stato["products"]}

    def test_la_versione_parte_da_zero_ed_e_dichiarata_alla_pagina(self) -> None:
        self.assertEqual(self.store.review()["state"]["stateVersion"], 0)

    def test_ogni_salvataggio_completo_fa_crescere_la_versione(self) -> None:
        primo = self.store.save_state(self.scheda(versione=0, display=1, standard=0))
        secondo = self.store.save_state(self.scheda(versione=primo["stateVersion"], display=2, standard=0))

        self.assertEqual(primo["stateVersion"], 1)
        self.assertEqual(secondo["stateVersion"], 2)
        self.assertEqual(self.store.review()["state"]["stateVersion"], 2)

    def test_la_seconda_scheda_viene_fermata_invece_di_cancellare_la_prima(self) -> None:
        # Tabs A and B both opened the same page: both start from version 0.
        self.store.save_state(self.scheda(versione=0, display=4, standard=0))

        with self.assertRaises(SnapshotError) as fermata:
            self.store.save_state(self.scheda(versione=0, display=0, standard=7))

        self.assertEqual({str(item["code"]) for item in fermata.exception.errors}, {"STATO_SOVRASCRITTO"})
        # Tab A's work must remain on disk.
        self.assertEqual(self.quantita_salvate(), {"display-solbao-96": 4, "product-standard": 0})

    def test_la_frase_dice_che_cosa_fare_senza_parlare_di_versioni(self) -> None:
        self.store.save_state(self.scheda(versione=0, display=4, standard=0))

        with self.assertRaises(SnapshotError) as fermata:
            self.store.save_state(self.scheda(versione=0, display=0, standard=7))

        messaggio = str(fermata.exception)
        self.assertIn("un'altra scheda", messaggio)
        self.assertIn("Ricarica questa pagina", messaggio)
        self.assertNotIn("stateVersion", messaggio)
        self.assertNotIn("snapshot", messaggio.casefold())

    def test_chi_avanza_la_versione_la_restituisce_a_chi_lo_ha_chiesto(self) -> None:
        """An action that bumps the state version outside `save_state` must return it in its response.

        A supplier discount (like a manual match or a rejection) advances the
        version on disk; if its own response doesn't carry the new version,
        the tab that triggered it falls one version behind, and its very next
        save gets rejected with "another tab saved after you" — even though
        no other tab was involved.
        """

        primo = self.store.save_state(self.scheda(versione=0, display=4, standard=0))

        esito = self.store.set_supplier_discount({"supplierId": "larice", "percent": 6})

        # 1. The response carries the new version, or the tab has no way to
        #    learn it's fallen behind.
        self.assertIn("stateVersion", esito)
        self.assertGreater(int(esito["stateVersion"]), int(primo["stateVersion"]))
        self.assertEqual(int(esito["stateVersion"]), int(self.store.review()["state"]["stateVersion"]))

        # 2. With that version, the same tab can save without being mistaken
        #    for another one.
        salvato = self.store.save_state(
            self.scheda(versione=int(esito["stateVersion"]), display=4, standard=7)
        )
        self.assertTrue(salvato["ok"])
        self.assertEqual(self.quantita_salvate(), {"display-solbao-96": 4, "product-standard": 7})

    def test_la_scheda_che_si_aggiorna_torna_a_poter_salvare(self) -> None:
        """A rejected save isn't a dead end: reloading the tab lets it resume."""

        self.store.save_state(self.scheda(versione=0, display=4, standard=0))
        versione_vista_ricaricando = self.store.review()["state"]["stateVersion"]

        esito = self.store.save_state(self.scheda(versione=versione_vista_ricaricando, display=4, standard=7))

        self.assertTrue(esito["ok"])
        self.assertEqual(self.quantita_salvate(), {"display-solbao-96": 4, "product-standard": 7})

    def test_una_pagina_che_non_dichiara_la_versione_salva_lo_stesso(self) -> None:
        """A page from before this field existed must still be able to save."""

        self.store.save_state(self.scheda(versione=0, display=4, standard=0))

        esito = self.store.save_state(self.scheda(versione=None, display=0, standard=7))

        self.assertTrue(esito["ok"])

    def test_anche_la_compilazione_rifiuta_uno_stato_sorpassato(self) -> None:
        """Compiling from a stale tab would order stale quantities."""

        self.store.save_state(self.scheda(versione=0, display=4, standard=0))

        with self.assertRaises(SnapshotError) as fermata:
            self.store.compile({
                "runId": "run-sintetica",
                "currentStep": 3,
                "acceptBelowThreshold": True,
                "stateVersion": 0,
                "products": [
                    {"id": "display-solbao-96", "quantity": 1, "selectedSupplierId": "larice", "confirmed": True},
                ],
            })

        self.assertEqual({str(item["code"]) for item in fermata.exception.errors}, {"STATO_SOVRASCRITTO"})
        self.assertEqual(self.cartelle(), [], "una compilazione rifiutata non lascia cartelle")

    def test_la_compilazione_riuscita_dichiara_la_versione_nuova(self) -> None:
        """After a successful compile, the tab must be able to save right away."""

        esito = self.store.compile({
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "stateVersion": 0,
            "products": [
                {"id": "display-solbao-96", "quantity": 1, "selectedSupplierId": "larice", "confirmed": True},
            ],
        })

        self.assertEqual(esito["stateVersion"], 1)
        salvato = self.store.save_state(self.scheda(versione=esito["stateVersion"], display=1, standard=2))
        self.assertTrue(salvato["ok"])

    def test_una_risposta_a_un_candidato_non_fa_scadere_le_schede_aperte(self) -> None:
        """An action that touches only part of the state must not bump the version.

        `validate_snapshot` copies `matchOverrides` and `manualProducts`
        forward from disk; those writes must not be lost, and expiring other
        open tabs over them would only reject otherwise-valid saves.
        """

        self.store.save_state(self.scheda(versione=0, display=4, standard=0))
        stato = json.loads(self.state_path.read_text(encoding="utf-8"))
        stato["matchOverrides"] = [{"productId": "product-standard", "accepted": True}]
        self.state_path.write_text(json.dumps(stato), encoding="utf-8")

        esito = self.store.save_state(self.scheda(versione=1, display=4, standard=3))

        self.assertTrue(esito["ok"])
        self.assertEqual(self.quantita_salvate()["product-standard"], 3)


class UnAltroSitoNonComandaIlComparatoreTests(unittest.TestCase):
    """A page from any other site, open in the same browser, must not be able to drive this service.

    A `POST` with `Content-Type: text/plain` is a CORS "simple request": it
    fires without a preflight, and its effect happens even if the site that
    sent it never reads the response. Without an `Origin` check, a request
    like this could trigger a real action on the file system or shut down
    the service.

    These tests cover both directions: a guard that also rejects the real
    launcher page is worse than the hole it closes, and the launcher calls
    `/api/spegni` with urllib, which never sends an `Origin` header at all.
    """

    class StoreFinto:
        """Records calls so a 403 can be distinguished from "ran anyway"."""

        def __init__(self) -> None:
            self.avvii = 0
            self.stati_salvati: list[Any] = []

        def avvia_pipeline(self) -> dict[str, Any]:
            self.avvii += 1
            return {"stato": "IN_CORSO"}

        def save_state(self, istantanea: Any) -> dict[str, Any]:
            self.stati_salvati.append(istantanea)
            return {"ok": True}

    def setUp(self) -> None:
        class HandlerMuto(SERVER.AppHandler):
            def log_message(self, format_string: str, *args: object) -> None:
                pass

        self.store = self.StoreFinto()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), HandlerMuto)
        self.httpd.store = self.store
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.ferma)
        self.porta = self.httpd.server_address[1]

    def ferma(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def chiedi(self, metodo: str, percorso: str, **intestazioni: str) -> tuple[int, dict[str, Any]]:
        connessione = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=15)
        try:
            connessione.request(
                metodo,
                percorso,
                body=json.dumps({"runId": "r1"}).encode("utf-8"),
                headers={"Content-Type": "application/json", **intestazioni},
            )
            risposta = connessione.getresponse()
            return int(risposta.status), json.loads(risposta.read().decode("utf-8"))
        finally:
            connessione.close()

    def test_un_altro_sito_non_fa_partire_la_catena(self) -> None:
        stato, corpo = self.chiedi(
            "POST", "/api/pipeline/avvia",
            Origin="https://sito-cattivo.example",
        )

        self.assertEqual(stato, 403)
        self.assertIn("non arriva dalla pagina del comparatore", corpo["message"])
        # And crucially: the pipeline never started, so no AI credit was spent.
        self.assertEqual(self.store.avvii, 0)

    def test_il_browser_che_dichiara_la_richiesta_estranea_viene_fermato(self) -> None:
        """`Sec-Fetch-Site` alone must be enough: it's present even without `Origin`."""

        stato, _corpo = self.chiedi(
            "POST", "/api/pipeline/avvia",
            **{"Sec-Fetch-Site": "cross-site"},
        )

        self.assertEqual(stato, 403)
        self.assertEqual(self.store.avvii, 0)

    def test_la_pagina_del_comparatore_avvia_la_catena_come_prima(self) -> None:
        stato, _corpo = self.chiedi(
            "POST", "/api/pipeline/avvia",
            Origin=f"http://127.0.0.1:{self.porta}",
            **{"Sec-Fetch-Site": "same-origin"},
        )

        self.assertEqual(stato, 202)
        self.assertEqual(self.store.avvii, 1)

    def test_la_stessa_pagina_aperta_su_localhost_passa(self) -> None:
        """`localhost` and `127.0.0.1` are different origins to the browser; both must be accepted."""

        stato, _corpo = self.chiedi(
            "POST", "/api/pipeline/avvia",
            Origin=f"http://localhost:{self.porta}",
        )

        self.assertEqual(stato, 202)
        self.assertEqual(self.store.avvii, 1)

    def test_il_lanciatore_che_non_manda_nessuna_delle_due_intestazioni_passa(self) -> None:
        """A urllib caller sends no `Origin`, so the guard can't reject on it alone.

        This is the launcher's real usage pattern: it calls `/api/spegni` on
        every startup when the source files changed. If this test went red,
        the store program would stop reopening with the latest code.
        """

        stato, _corpo = self.chiedi("POST", "/api/pipeline/avvia")

        self.assertEqual(stato, 202)
        self.assertEqual(self.store.avvii, 1)

    def test_anche_il_salvataggio_dello_stato_rifiuta_un_altro_sito(self) -> None:
        stato, _corpo = self.chiedi(
            "PUT", "/api/state",
            Origin="https://sito-cattivo.example",
        )

        self.assertEqual(stato, 403)
        self.assertEqual(self.store.stati_salvati, [])


class UnDominioRipuntatoDalDnsRebindingNonLeggeIlComparatoreTests(unittest.TestCase):
    """A domain that DNS rebinding just repointed at 127.0.0.1 must not be able to read this service, even with a plain GET.

    In DNS rebinding, an attacker's real domain gets opened by the operator,
    then its DNS record is switched to point at 127.0.0.1: requests the page
    sends to `http://attacker-domain.example:<port>/...` stay same-origin as
    far as the browser is concerned, so the attacker's page can read the
    response. A plain GET often carries no `Origin` header at all, so the
    origin-based CSRF guard alone isn't enough; the service must also check
    `Host`, which always carries the attacker's domain as shown in the
    address bar, never the IP DNS rebound it to.

    Covers both directions, with the same pattern as the class above: a
    foreign `Host` must block the request, while the legitimate forms — with
    port, without port, absent entirely as in HTTP/1.0 — must keep working,
    or the real page stops loading.
    """

    class StoreFinto:
        """Records calls so a 403 can be distinguished from "ran anyway"."""

        def __init__(self) -> None:
            self.letture_review = 0
            self.avvii = 0
            self.stati_salvati: list[Any] = []

        def review(self) -> dict[str, Any]:
            self.letture_review += 1
            return {"schema_version": 1, "run": {"id": "prova"}, "files": []}

        def avvia_pipeline(self) -> dict[str, Any]:
            self.avvii += 1
            return {"stato": "IN_CORSO"}

        def save_state(self, istantanea: Any) -> dict[str, Any]:
            self.stati_salvati.append(istantanea)
            return {"ok": True}

    def setUp(self) -> None:
        class HandlerMuto(SERVER.AppHandler):
            def log_message(self, format_string: str, *args: object) -> None:
                pass

        self.store = self.StoreFinto()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), HandlerMuto)
        self.httpd.store = self.store
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.ferma)
        self.porta = self.httpd.server_address[1]

    def ferma(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def chiedi(self, metodo: str, percorso: str, **intestazioni: str) -> tuple[int, dict[str, Any]]:
        connessione = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=15)
        try:
            connessione.request(
                metodo,
                percorso,
                body=json.dumps({"runId": "r1"}).encode("utf-8"),
                headers={"Content-Type": "application/json", **intestazioni},
            )
            risposta = connessione.getresponse()
            return int(risposta.status), json.loads(risposta.read().decode("utf-8"))
        finally:
            connessione.close()

    def chiedi_senza_intestazione_host(self, metodo: str, percorso: str) -> tuple[int, dict[str, Any]]:
        """Simulate a client that sends no `Host` header at all, as HTTP/1.0 may."""

        connessione = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=15)
        try:
            corpo = json.dumps({"runId": "r1"}).encode("utf-8")
            connessione.putrequest(metodo, percorso, skip_host=True)
            connessione.putheader("Content-Type", "application/json")
            connessione.putheader("Content-Length", str(len(corpo)))
            connessione.endheaders(corpo)
            risposta = connessione.getresponse()
            return int(risposta.status), json.loads(risposta.read().decode("utf-8"))
        finally:
            connessione.close()

    def test_un_host_estraneo_non_legge_il_confronto(self) -> None:
        stato, corpo = self.chiedi(
            "GET", "/api/review",
            Host="negozio.attaccante.example",
        )

        self.assertEqual(stato, 403)
        self.assertIn("comparatore", corpo["message"])
        # And crucially: the read never happened, so the comparison never left.
        self.assertEqual(self.store.letture_review, 0)

    def test_un_host_estraneo_ferma_la_catena_anche_con_l_origine_giusta(self) -> None:
        """The `Host` check runs before the `Origin` check, and must block on its own."""

        stato, _corpo = self.chiedi(
            "POST", "/api/pipeline/avvia",
            Host="negozio.attaccante.example",
            Origin=f"http://127.0.0.1:{self.porta}",
            **{"Sec-Fetch-Site": "same-origin"},
        )

        self.assertEqual(stato, 403)
        self.assertEqual(self.store.avvii, 0)

    def test_un_host_estraneo_ferma_anche_il_salvataggio_dello_stato(self) -> None:
        stato, _corpo = self.chiedi(
            "PUT", "/api/state",
            Host="negozio.attaccante.example",
        )

        self.assertEqual(stato, 403)
        self.assertEqual(self.store.stati_salvati, [])

    def test_l_host_127_0_0_1_con_la_porta_giusta_passa(self) -> None:
        stato, _corpo = self.chiedi("GET", "/api/review", Host=f"127.0.0.1:{self.porta}")

        self.assertEqual(stato, 200)
        self.assertEqual(self.store.letture_review, 1)

    def test_l_host_localhost_con_la_porta_giusta_passa(self) -> None:
        stato, _corpo = self.chiedi("GET", "/api/review", Host=f"localhost:{self.porta}")

        self.assertEqual(stato, 200)
        self.assertEqual(self.store.letture_review, 1)

    def test_l_host_ipv6_con_la_porta_giusta_passa(self) -> None:
        stato, _corpo = self.chiedi("GET", "/api/review", Host=f"[::1]:{self.porta}")

        self.assertEqual(stato, 200)
        self.assertEqual(self.store.letture_review, 1)

    def test_l_host_senza_porta_passa(self) -> None:
        """Some clients send `Host: 127.0.0.1` without a port; must still be accepted."""

        stato, _corpo = self.chiedi("GET", "/api/review", Host="127.0.0.1")

        self.assertEqual(stato, 200)
        self.assertEqual(self.store.letture_review, 1)

    def test_l_host_assente_del_tutto_passa(self) -> None:
        """HTTP/1.0 may send no `Host` at all; must still be accepted, or the real page stops loading."""

        stato, _corpo = self.chiedi_senza_intestazione_host("GET", "/api/review")

        self.assertEqual(stato, 200)
        self.assertEqual(self.store.letture_review, 1)


class LoSpegnimentoDelServizioNonUccideUnaRunInCorsoTests(unittest.TestCase):
    """`/api/spegni` and its "a comparison is running" guard.

    `_spegni()` must refuse to stop while the pipeline is working
    (`RUN_IN_CORSO`): killing a run mid-flight leaves an orphaned dated
    folder and discards AI work already paid for. Before it does stop, it
    must release the confirmations file via `store.chiudi()`, since an open
    SQLite file can't be renamed on Windows. `app/launcher.py` calls this
    route with `urllib` on every store-program startup, so a regression here
    would kill an in-progress run with no test catching it.

    None of these tests shut down a real service: `_spegni()` triggers the
    real `shutdown()` on its own thread, and calling it for real would stop
    this `ThreadingHTTPServer` and break the tests that run after it in the
    same file. The real method is set aside in `setUp` and replaced with one
    that only records the call; the real one is invoked again at teardown to
    actually stop the thread.
    """

    class StoreFinto:
        """Records calls in the order they arrive."""

        def __init__(self) -> None:
            self._stato = "IN_ATTESA"
            self.chiamate: list[str] = []

        def stato_pipeline(self) -> dict[str, Any]:
            self.chiamate.append("stato_pipeline")
            return {"stato": self._stato}

        def chiudi(self) -> None:
            self.chiamate.append("chiudi")

    def setUp(self) -> None:
        class HandlerMuto(SERVER.AppHandler):
            def log_message(self, format_string: str, *args: object) -> None:
                pass

        self.store = self.StoreFinto()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), HandlerMuto)
        self.httpd.store = self.store

        # The real shutdown() would stop the server for the tests that
        # follow: set it aside and replace it with one that only records the
        # call, without stopping anything.
        self._spegnimento_vero = self.httpd.shutdown
        self.chiamate_a_shutdown = 0

        def shutdown_finto() -> None:
            self.store.chiamate.append("shutdown")
            self.chiamate_a_shutdown += 1

        self.httpd.shutdown = shutdown_finto

        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.ferma)
        self.porta = self.httpd.server_address[1]

    def ferma(self) -> None:
        # Invokes the real method set aside in setUp: the only way to
        # actually stop the thread serving requests.
        self._spegnimento_vero()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def chiedi(self, metodo: str, percorso: str, **intestazioni: str) -> tuple[int, dict[str, Any]]:
        connessione = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=15)
        try:
            connessione.request(
                metodo,
                percorso,
                body=json.dumps({}).encode("utf-8"),
                headers={"Content-Type": "application/json", **intestazioni},
            )
            risposta = connessione.getresponse()
            return int(risposta.status), json.loads(risposta.read().decode("utf-8"))
        finally:
            connessione.close()

    def aspetta_lo_shutdown_finto(self) -> None:
        """Wait for the call: `_spegni()` triggers `shutdown()` on a separate thread."""

        for _ in range(200):
            if self.chiamate_a_shutdown:
                return
            time.sleep(0.01)
        self.fail("shutdown() finto non è mai stato chiamato entro due secondi")

    def test_con_una_run_in_corso_il_servizio_rifiuta_di_spegnersi(self) -> None:
        self.store._stato = "IN_CORSO"

        stato, corpo = self.chiedi("POST", "/api/spegni")

        self.assertEqual(stato, 200)
        self.assertEqual(corpo["ok"], False)
        self.assertEqual(corpo["motivo"], "RUN_IN_CORSO")
        self.assertEqual(self.chiamate_a_shutdown, 0)
        self.assertNotIn("chiudi", self.store.chiamate)

        # The service must still respond right after: it didn't stop.
        stato_dopo, corpo_dopo = self.chiedi("POST", "/api/spegni")
        self.assertEqual(stato_dopo, 200)
        self.assertEqual(corpo_dopo["motivo"], "RUN_IN_CORSO")

    def test_a_riposo_il_servizio_si_spegne(self) -> None:
        stato, corpo = self.chiedi("POST", "/api/spegni")

        self.assertEqual(stato, 200)
        self.assertEqual(corpo["ok"], True)
        self.assertEqual(corpo["spento"], True)

        self.aspetta_lo_shutdown_finto()
        self.assertEqual(self.chiamate_a_shutdown, 1)

    def test_prima_di_fermarsi_ha_chiuso_il_negozio(self) -> None:
        """The part that matters on Windows: an open SQLite file can't be renamed."""

        self.chiedi("POST", "/api/spegni")

        self.assertIn("chiudi", self.store.chiamate)
        self.aspetta_lo_shutdown_finto()
        self.assertLess(
            self.store.chiamate.index("chiudi"),
            self.store.chiamate.index("shutdown"),
        )


class NonELoStessoArticoloTests(unittest.TestCase):
    """A proposed match that's rejected as "not the same item" must have a working way out.

    Confirming would order the wrong item; leaving it unconfirmed blocks
    compile on "confirmation required"; and excluding it zeroes the
    quantity, which makes `compile`'s loop skip it entirely — including from
    the "products to be sourced" list, where it should still appear.
    """

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_rifiuto_"))
        self.addCleanup(shutil.rmtree, self.radice, True)
        for nome in ("uploads", "out", "history"):
            (self.radice / nome).mkdir()
        confronto = {
            "run": {"id": "r1"},
            "suppliers": [{"id": "larice", "name": "LARICE", "minimumOrder": 0}],
            "warnings": [], "files": [],
            "products": [{
                "id": "product:410", "kind": "PRODUCT", "itemType": "product",
                "ean": "8009160250531", "name": "CERA DI LUNAR ANTIRUGHE QUOTIDIANA",
                "description": "CERA DI LUNAR ANTIRUGHE QUOTIDIANA",
                "quantity": 3, "quantityLabel": "colli", "orderUnitLabel": "colli",
                "lastUnitPrice": 2.5, "selectedSupplierId": "larice",
                "confirmed": False, "requiresConfirmation": True,
                "confirmationMessage": "Abbinamento proposto dall'analisi",
                "components": [], "warnings": [],
                "offers": [{
                    "supplierId": "larice", "supplierName": "LARICE", "available": True,
                    "status": "SEMANTICO_PROPOSTO", "matchStatus": "SEMANTICO_PROPOSTO",
                    "requiresConfirmation": True, "confirmed": False,
                    "description": "CERA DI LUNAR VASO 50 ROSA", "ean": "8009553130211",
                    "unitPriceNet": 2.1, "orderUnitPriceNet": 12.6,
                    "quantityFactor": 6, "sourceRow": 100,
                }],
            }],
        }
        percorso = self.radice / "review_data.json"
        percorso.write_text(json.dumps(confronto), encoding="utf-8")
        self.store = ReviewStore(
            percorso, self.radice / "state.json", self.radice / "uploads",
            self.radice / "out", history_path=self.radice / "history",
        )
        self.addCleanup(self.store.chiudi)

    def offerta(self) -> dict:
        return self.store.review()["products"][0]["offers"][0]

    def rifiuta(self, rifiutata: bool = True) -> dict:
        return self.store.rifiuta_l_abbinamento({
            "runId": "r1", "productId": "product:410",
            "supplierId": "larice", "rifiutata": rifiutata,
        })

    def test_l_offerta_rifiutata_esce_dal_confronto(self) -> None:
        self.rifiuta()

        offerta = self.offerta()
        self.assertFalse(offerta["available"])
        self.assertEqual(offerta["status"], "RIFIUTATO_UTENTE")
        self.assertFalse(offerta["requiresConfirmation"])
        self.assertTrue(offerta.get("rifiutata"))

    def test_la_quantita_resta_e_la_compilazione_non_si_ferma_piu(self) -> None:
        """The reason this response exists: compile must not stop on it."""

        self.rifiuta()

        _pulito, errori, _review = self.store.validate_snapshot({
            "runId": "r1",
            "products": [{"id": "product:410", "quantity": 3,
                          "selectedSupplierId": "", "confirmed": False, "excluded": False}],
        }, for_compile=True)

        self.assertEqual([voce["code"] for voce in errori], [])

    def test_l_elenco_da_reperire_dice_che_l_hai_scartato_tu(self) -> None:
        """"No supplier has it" would wrongly blame the supplier for a match the operator rejected."""

        import da_reperire  # noqa: PLC0415

        self.rifiuta()

        self.assertEqual(
            da_reperire.motivo(self.store.review()["products"][0]["offers"]),
            da_reperire.MOTIVO_RIFIUTATO_DA_TE,
        )

    def test_l_avviso_che_passa_dice_il_gesto_non_la_regola(self) -> None:
        """The transient toast (`showToast`, gone in 3.6s) must confirm the action, not restate the rule.

        The full rule about how long a rejection lasts already sits in the
        rejected-supplier panel and under the button before it's pressed;
        the toast needs to say only what just happened.
        """

        esito = self.rifiuta()

        self.assertIn("esce da questo prodotto", esito["message"])
        self.assertIn("La quantità resta", esito["message"])
        self.assertNotIn("settimana prossima", esito["message"])
        self.assertLess(len(esito["message"]), 90, esito["message"])

    def test_il_fornitore_scelto_si_svuota_ma_la_quantita_no(self) -> None:
        self.store.save_state({
            "runId": "r1",
            "products": [{"id": "product:410", "quantity": 3,
                          "selectedSupplierId": "larice", "confirmed": True, "excluded": False}],
        })

        self.rifiuta()

        salvato = json.loads((self.radice / "state.json").read_text(encoding="utf-8"))
        voce = next(item for item in salvato["products"] if item["id"] == "product:410")
        self.assertEqual(voce["selectedSupplierId"], "")
        self.assertFalse(voce["confirmed"])
        self.assertEqual(voce["quantity"], 3)

    def test_si_torna_indietro_e_l_offerta_riappare_com_era(self) -> None:
        self.rifiuta()

        self.rifiuta(False)

        offerta = self.offerta()
        self.assertTrue(offerta["available"])
        self.assertEqual(offerta["status"], "SEMANTICO_PROPOSTO")
        self.assertTrue(offerta["requiresConfirmation"])
        self.assertIsNone(offerta.get("rifiutata"))

    def test_il_no_sopravvive_al_ricalcolo_perche_e_legato_all_articolo(self) -> None:
        """`state.json` is wiped by a recompute; the confirmations store isn't."""

        self.rifiuta()
        (self.radice / "state.json").unlink(missing_ok=True)

        self.assertFalse(self.offerta()["available"])

    def test_un_autosalvataggio_non_cancella_il_no(self) -> None:
        """An autosave normally closes whatever row is in effect; it must not here."""

        self.rifiuta()

        self.store.save_state({
            "runId": "r1",
            "products": [{"id": "product:410", "quantity": 3,
                          "selectedSupplierId": "", "confirmed": False, "excluded": False}],
        })

        self.assertFalse(self.offerta()["available"])

    def test_un_fornitore_che_non_ha_righe_per_questo_prodotto_lo_dice(self) -> None:
        with self.assertRaises(ValueError) as errore:
            self.store.rifiuta_l_abbinamento({
                "runId": "r1", "productId": "product:410",
                "supplierId": "betulla", "rifiutata": True,
            })

        self.assertIn("nessuna riga", str(errore.exception))


class CominciareUnaComparazioneNuova(unittest.TestCase):
    """"Start a new comparison": clears documents and the comparison to open a fresh week.

    Half of these tests check not what the command does, but what it leaves
    untouched, which is the point of the command. Confirmations and learned
    adapters can't be rebuilt by any recompute, and deleting pending orders
    would cause goods already on the way to be reordered — the three things
    that would turn a cleanup command into data loss.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run-corrente"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.review_path = self.root / "review_data.json"
        self.state_path = self.run_dir / "review_state.json"
        self.upload_dir = self.root / "uploads"
        self.output_dir = self.root / "outputs"
        self.orders_dir = self.root / "ordini"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.storico_dir = self.root / "history"
        self.storico_dir.mkdir(parents=True, exist_ok=True)

        # The supplier name is the one shown on the document card — "BETULLA",
        # not the id — which is what ends up in the message, matching what
        # the page shows.
        self.documenti = {
            "gestionale.xlsx": ("master", ""),
            "listino_betulla.xlsx": ("supplier", "BETULLA"),
            "listino_larice.xlsx": ("supplier", "LARICE"),
        }
        for nome in self.documenti:
            (self.upload_dir / nome).write_bytes(b"contenuto finto")
        (self.upload_dir / "upload_profiles.json").write_text(json.dumps({
            "schema_version": 1,
            "profiles": [
                {"file_name": nome, "path": str(self.upload_dir / nome), "role": ruolo}
                for nome, (ruolo, _) in self.documenti.items()
            ],
            "errors": [],
        }), encoding="utf-8")
        self.review_path.write_text(json.dumps({
            "run": {"id": "r1", "status": "ready", "label": "Confronto"},
            "files": [
                {"name": nome, "role": ruolo, "supplier": fornitore or "Gestionale"}
                for nome, (ruolo, fornitore) in self.documenti.items()
            ],
            "suppliers": [{"id": "betulla", "name": "BETULLA"}, {"id": "larice", "name": "LARICE"}],
            "products": [{"id": "p1", "name": "Un prodotto", "offers": []}],
            "warnings": [],
        }), encoding="utf-8")
        self.state_path.write_text(json.dumps({"runId": "r1", "products": [{"id": "p1", "quantity": 4}]}), encoding="utf-8")

        # The stores that must survive, plus one already-compiled order.
        self.conferme = self.storico_dir / "conferme.db"
        self.conferme.write_bytes(b"le conferme di chi ordina")
        self.ordini = self.storico_dir / "orders.json"
        self.ordini.write_text(json.dumps({"orders": [{"orderId": "o1", "supplier": "betulla"}]}), encoding="utf-8")
        self.imparati = self.root / "adattatori_imparati.json"
        self.imparati.write_text(json.dumps({"adapters": [{"id": "quercia_v1__locale"}]}), encoding="utf-8")
        self.compilazione = self.orders_dir / "2026-08-17_2021"
        self.compilazione.mkdir(parents=True, exist_ok=True)
        (self.compilazione / "Ordine BETULLA compilato.xlsx").write_bytes(b"un ordine gia' preparato")

        self.store = ReviewStore(
            self.review_path,
            self.state_path,
            self.upload_dir,
            self.output_dir,
            history_path=self.ordini,
            orders_dir=self.orders_dir,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def documenti_rimasti(self) -> list[str]:
        return sorted(
            voce.name for voce in self.upload_dir.iterdir()
            if voce.is_file() and voce.name != "upload_profiles.json"
        )

    # -- rollback: the branch that runs when something breaks mid-cleanup --

    def test_se_la_pulizia_si_rompe_a_meta_rimette_tutto_a_posto(self) -> None:
        """A failure mid-cleanup must restore everything, not leave a half-done state.

        `nuova_comparazione` first moves everything into quarantine, then
        rewrites the profiles, and only then deletes for real. If something
        fails in between, it must roll back — exactly the path that runs
        when something has already gone wrong, which is the one that can't
        afford to be broken itself.
        """

        prima_documenti = self.documenti_rimasti()
        prima_confronto = self.review_path.read_bytes()
        prima_scelte = self.state_path.read_bytes()
        prima_profili = (self.upload_dir / "upload_profiles.json").read_bytes()

        vero = SERVER.atomic_json

        def non_si_scrive(percorso, valore):
            if Path(percorso).name == "upload_profiles.json":
                raise OSError("disco pieno")
            return vero(percorso, valore)

        SERVER.atomic_json = non_si_scrive
        try:
            with self.assertRaises(OSError):
                self.store.nuova_comparazione({})
        finally:
            SERVER.atomic_json = vero

        # Everything as it was: documents, comparison, choices, profiles.
        self.assertEqual(self.documenti_rimasti(), prima_documenti)
        self.assertEqual(self.review_path.read_bytes(), prima_confronto)
        self.assertEqual(self.state_path.read_bytes(), prima_scelte)
        self.assertEqual((self.upload_dir / "upload_profiles.json").read_bytes(), prima_profili)
        # No leftover quarantine files either.
        quarantena = [voce.name for voce in self.upload_dir.iterdir()
                      if voce.name.startswith(".nuova-comparazione-")]
        self.assertEqual(quarantena, [])

    def test_dopo_un_ripristino_il_comando_si_puo_rifare(self) -> None:
        """A disk failure, not a program bug: once it clears, retrying must succeed."""

        vero = SERVER.atomic_json
        SERVER.atomic_json = lambda percorso, valore: (_ for _ in ()).throw(OSError("disco pieno"))
        try:
            with self.assertRaises(OSError):
                self.store.nuova_comparazione({})
        finally:
            SERVER.atomic_json = vero

        esito = self.store.nuova_comparazione({})

        self.assertTrue(esito["ok"])
        self.assertEqual(self.documenti_rimasti(), [])
        self.assertFalse(self.review_path.exists())

    # -- what the command does -----------------------------------------------

    def test_toglie_i_documenti_il_confronto_e_le_scelte(self) -> None:
        esito = self.store.nuova_comparazione({})

        self.assertTrue(esito["ok"])
        self.assertEqual(self.documenti_rimasti(), [])
        self.assertFalse(self.review_path.exists(), "il confronto di prima non è stato chiuso")
        self.assertFalse(self.state_path.exists(), "le quantità della settimana scorsa sono rimaste")
        profili = json.loads((self.upload_dir / "upload_profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(profili["profiles"], [])

    def test_dice_quanti_documenti_ha_tolto_e_di_chi(self) -> None:
        """The count feeds the page's confirmation line, checkable at a glance against the visible document cards."""

        esito = self.store.nuova_comparazione({})

        self.assertEqual(esito["tolti"], {"elenco": 1, "listini": 2, "documenti": 3})
        self.assertEqual(esito["fornitori"], ["BETULLA", "LARICE"])

    def test_non_lascia_file_di_quarantena_in_giro(self) -> None:
        """Files are moved into quarantine first and deleted after: nothing scaffolding-like should remain."""

        self.store.nuova_comparazione({})

        residui = [voce.name for voce in self.upload_dir.iterdir() if voce.name.startswith(".nuova-comparazione")]
        self.assertEqual(residui, [])

    def test_la_pagina_riparte_da_zero(self) -> None:
        self.store.nuova_comparazione({})
        review = self.store.review()

        self.assertEqual(review.get("products"), [])
        self.assertEqual(review.get("files"), [])

    # -- what the command deliberately leaves untouched -----------------------

    def test_le_conferme_restano(self) -> None:
        """Confirmations must survive: no recompute can rebuild them, and there's no copy off this disk."""

        self.store.nuova_comparazione({})

        self.assertTrue(self.conferme.is_file())
        self.assertEqual(self.conferme.read_bytes(), b"le conferme di chi ordina")

    def test_gli_ordini_in_attesa_restano(self) -> None:
        """Pending orders must survive: deleting the memory of goods already ordered would cause a reorder."""

        self.store.nuova_comparazione({})

        self.assertTrue(self.ordini.is_file())
        self.assertEqual(
            json.loads(self.ordini.read_text(encoding="utf-8"))["orders"],
            [{"orderId": "o1", "supplier": "betulla"}],
        )

    def test_gli_schemi_imparati_restano(self) -> None:
        """Learned adapters must survive: losing them means every price list needs remapping again."""

        self.store.nuova_comparazione({})

        self.assertTrue(self.imparati.is_file())
        self.assertIn("quercia_v1__locale", self.imparati.read_text(encoding="utf-8"))

    def test_le_compilazioni_gia_fatte_restano(self) -> None:
        """Already-compiled orders must survive: deleting them would also silence "did it arrive?" tracking."""

        self.store.nuova_comparazione({})

        self.assertTrue(self.compilazione.is_dir())
        self.assertTrue((self.compilazione / "Ordine BETULLA compilato.xlsx").is_file())

    def test_il_documento_collegato_da_fuori_non_si_cancella(self) -> None:
        """Input documents are read-only: only the application's own copies are removed, never the original."""

        esterno = self.root / "fuori" / "listino_esterno.xlsx"
        esterno.parent.mkdir(parents=True, exist_ok=True)
        esterno.write_bytes(b"il file vero di chi ordina")
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        review["files"].append({"name": esterno.name, "role": "supplier", "supplier": "quercia"})
        self.review_path.write_text(json.dumps(review), encoding="utf-8")

        self.store.nuova_comparazione({})

        self.assertTrue(esterno.is_file())
        self.assertEqual(esterno.read_bytes(), b"il file vero di chi ordina")

    # -- "did it arrive?" reopens immediately, not after its usual delay -----

    def test_la_domanda_rimandata_torna_in_piedi(self) -> None:
        """Starting a new comparison must reopen a snoozed "did it arrive?" question immediately.

        A "not yet" answer normally snoozes the question for several days,
        but starting a new comparison is a stronger signal than that timer:
        it's the moment the operator genuinely needs to know whether last
        week's goods arrived.
        """

        risposto = ORDER_HISTORY.to_iso(ORDER_HISTORY.utc_now() - timedelta(days=2))
        self.ordini.write_text(json.dumps({"schema_version": 1, "orders": [{
            "orderId": "o1", "supplier": "betulla", "supplierName": "BETULLA", "runId": "vecchia",
            "createdAt": risposto, "answeredAt": risposto, "status": "pending", "lines": [],
        }]}), encoding="utf-8")
        storico, _ = ORDER_HISTORY.read_history(self.ordini)
        self.assertNotEqual(ORDER_HISTORY.pending_summary(storico)[0]["askAgainAt"], "",
                            "senza il comando la domanda è rimandata, ed è giusto così")

        esito = self.store.nuova_comparazione({})

        self.assertEqual(esito["domandeRiaperte"], 1)
        storico, _ = ORDER_HISTORY.read_history(self.ordini)
        self.assertEqual(ORDER_HISTORY.pending_summary(storico)[0]["askAgainAt"], "",
                         "la domanda è ancora rimandata dopo il comando")

    def test_riaprire_non_cancella_che_cosa_avevi_risposto(self) -> None:
        """`answeredAt` records what was answered and when; reopening the question must not clear it.

        The snooze is derived from that timestamp, not stored on it, so it's
        cancelled through a separate flag instead.
        """

        risposto = ORDER_HISTORY.to_iso(ORDER_HISTORY.utc_now() - timedelta(days=2))
        self.ordini.write_text(json.dumps({"schema_version": 1, "orders": [{
            "orderId": "o1", "supplier": "betulla", "supplierName": "BETULLA", "runId": "vecchia",
            "createdAt": risposto, "answeredAt": risposto, "status": "pending", "lines": [],
        }]}), encoding="utf-8")

        self.store.nuova_comparazione({})

        voce = json.loads(self.ordini.read_text(encoding="utf-8"))["orders"][0]
        self.assertEqual(voce["answeredAt"], risposto)

    def test_una_risposta_data_dopo_rimette_il_rinvio(self) -> None:
        """The reopen flag is compared against `answeredAt`, not the clock, or the question would reopen on every reload forever."""

        vecchio = ORDER_HISTORY.to_iso(ORDER_HISTORY.utc_now() - timedelta(days=2))
        riaperta = ORDER_HISTORY.to_iso(ORDER_HISTORY.utc_now() - timedelta(days=1))
        adesso = ORDER_HISTORY.to_iso(ORDER_HISTORY.utc_now())
        voce = {"orderId": "o1", "supplier": "betulla", "createdAt": vecchio,
                "answeredAt": adesso, "reaskedAt": riaperta, "status": "pending", "lines": []}

        self.assertNotEqual(ORDER_HISTORY.ask_again_at(voce), "")
        # E al contrario, riaperta dopo la risposta, la domanda è dovuta.
        voce["answeredAt"] = vecchio
        self.assertEqual(ORDER_HISTORY.ask_again_at(voce), "")

    def test_senza_niente_da_riaprire_non_dice_di_averlo_fatto(self) -> None:
        esito = self.store.nuova_comparazione({})

        self.assertEqual(esito["domandeRiaperte"], 0)

    # -- and when it can't start -----------------------------------------------

    def test_non_si_comincia_mentre_il_confronto_gira(self) -> None:
        """Clearing documents while a pipeline run is reading them would corrupt that run.

        Same reason uploads are blocked while a recompute is in progress.
        """

        with mock.patch.object(self.store.pipeline_jobs, "in_corso", return_value=True):
            with self.assertRaises(Exception) as errore:
                self.store.nuova_comparazione({})

        self.assertIn("confronto è in corso", str(errore.exception))
        self.assertEqual(len(self.documenti_rimasti()), 3, "ha svuotato lo stesso")
        self.assertTrue(self.review_path.exists())



class IlControlloDiceQuelloCheFermaIlLavoro(unittest.TestCase):
    """`--check` must report every condition that would stop the program from working, not just print `[OK]`.

    It's the command suggested to an operator who can't get the program to
    start, so it must actually probe the port, the copies folder's
    writability, the AI key, and whether `prepare_writer_config` found any
    price list compilable; a false "everything is fine" is worse than no
    answer at all.
    """

    def setUp(self) -> None:
        self.launcher = importa_launcher()

    def writer(self, config_path: Path | None, messaggio: str = "Writer XLSX pronto"):
        return self.launcher.WriterSetup(config_path=config_path, node_runtime=None, message=messaggio)

    def test_un_writer_spento_si_dice(self) -> None:
        avvisi = self.launcher.avvisi_del_controllo(
            self.writer(None, "nessun listino risulta compilabile"))

        self.assertTrue(any("compilazione dei listini" in voce for voce in avvisi), avvisi)

    def test_una_cartella_delle_copie_non_scrivibile_si_dice(self) -> None:
        with tempfile.TemporaryDirectory() as temporaneo:
            occupata = Path(temporaneo) / "copie"
            # A file where a folder is expected: `mkdir` can't succeed.
            occupata.write_text("non sono una cartella", encoding="utf-8")
            with mock.patch.object(self.launcher, "cartella_delle_copie", lambda: occupata):
                avvisi = self.launcher.avvisi_del_controllo(self.writer(Path("config.json")))

        self.assertTrue(any("copie" in voce for voce in avvisi), avvisi)

    def test_senza_chiave_lo_dice_e_dice_dove_si_mette(self) -> None:
        with tempfile.TemporaryDirectory() as temporaneo:
            copie = Path(temporaneo) / "copie"
            with mock.patch.object(self.launcher, "cartella_delle_copie", lambda: copie), \
                 mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False), \
                 mock.patch.object(SERVER.ai_client, "PERCORSO_SECRETS", Path(temporaneo) / "secrets.json"):
                avvisi = self.launcher.avvisi_del_controllo(self.writer(Path("config.json")))

        chiave = [voce for voce in avvisi if "chiave OpenRouter" in voce]
        self.assertEqual(len(chiave), 1, avvisi)
        self.assertIn("Impostazioni", chiave[0])

    def test_con_tutto_a_posto_non_dice_niente(self) -> None:
        """Negative control: the command doesn't complain regardless of the actual state."""

        with tempfile.TemporaryDirectory() as temporaneo:
            copie = Path(temporaneo) / "copie"
            segreti = Path(temporaneo) / "secrets.json"
            segreti.write_text(json.dumps({"openrouter": {"api_key": "sk-or-finta"}}), encoding="utf-8")
            with mock.patch.object(self.launcher, "cartella_delle_copie", lambda: copie), \
                 mock.patch.object(self.launcher, "salute", lambda porta, **extra: None), \
                 mock.patch.object(self.launcher, "port_is_free", lambda porta: True), \
                 mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
                import ai_client as ai_del_lanciatore
                with mock.patch.object(ai_del_lanciatore, "PERCORSO_SECRETS", segreti):
                    avvisi = self.launcher.avvisi_del_controllo(self.writer(Path("config.json")))

        self.assertEqual(avvisi, [])


if __name__ == "__main__":
    unittest.main()
