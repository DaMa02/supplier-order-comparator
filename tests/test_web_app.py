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

# Il listino Noce in .xls lo costruisce gia' il collaudo della pipeline:
# un OLE2 vero con dentro record BIFF8 veri, non un file finto.
import test_schema_pipeline as pipeline  # noqa: E402

SERVER_SPEC = importlib.util.spec_from_file_location("compara_ordini_web_server", SERVER_PATH)
if SERVER_SPEC is None or SERVER_SPEC.loader is None:
    raise RuntimeError(f"Impossibile importare {SERVER_PATH}")
SERVER = importlib.util.module_from_spec(SERVER_SPEC)
SERVER_SPEC.loader.exec_module(SERVER)

ReviewStore = SERVER.ReviewStore
# Lo stesso modulo che usa il servizio: le date del rinvio le calcola lui.
ORDER_HISTORY = SERVER.order_history
SnapshotError = SERVER.SnapshotError
# «Un lavoro per volta»: la stessa eccezione che il servizio traduce in 409.
LavoroGiaInCorso = SERVER.LavoroGiaInCorso
supplier_label = SERVER.supplier_label
# Lo stesso `consegna` che il servizio ha caricato: le costanti dei tipi da
# consegnare stanno li', e una seconda importazione darebbe un altro modulo.
consegna = SERVER.consegna

SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import registro as registro_adattatori  # noqa: E402


def importa_launcher() -> Any:
    """Il lanciatore come modulo isolato, allo stesso modo degli altri test qui."""

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
                        # Come lo scrive `display_offer`: il prezzo al pezzo in
                        # `unitPriceNet`, i pezzi dell'espositore in
                        # `quantityFactor`, l'espositore intero in
                        # `orderUnitPriceNet`. 442,1196 / 96 = 4,6054.
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
    """I sei casi della frase che l'utente legge a compilazione finita.

    Era una sessantina di righe innestate in sei rami dentro `compile`, che e'
    la funzione piu' lunga del programma: si potevano guardare solo compilando
    davvero, cioe' scrivendo file veri con Node. Adesso e' una funzione pura, e
    i sei casi si provano con una tabella.

    ⚠ La frase non e' solo quello che si legge a schermo: finisce anche
    nell'audit della cartella, campo `messaggio`, che e' quello che resta fra
    una settimana quando nessuno ricorda piu' com'era andata.
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
        """⚠ «Non ci ha provato nessuno» e «ci ha provato e non ne e' uscito

        niente» sono due cose diverse, e a separarle e' la configurazione, non
        il fatto che ci sia un motivo da dire. La prima estrazione le
        confondeva, e a chi la configurazione ce l'ha gia' completa faceva
        leggere «completa la configurazione di scrittura» — nella pagina e
        nell'audit della cartella, dove resta. Trovato dalla verifica
        avversariale del 20 agosto 2026.
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
        """La pagina li elenca gia' da `writerIssues`: ripeterli qui li farebbe

        comparire due volte sotto un titolo che li smentisce. Il numero resta,
        perche' lo storico delle compilazioni ha il messaggio e non l'elenco."""

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
        """Quando NON e' stata creata nessuna copia il motivo sono le copie

        scartate. Gli avvisi del writer dicono «la quantita' e' stata scritta
        lo stesso», e dentro una frase che dichiara che non e' stato creato
        niente si contraddicono a vicenda."""

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
    """Ogni autosalvataggio ricostruiva il confronto due volte da zero.

    `save_state` chiamava `validate_snapshot`, che costruisce il confronto per
    sapere quali prodotti esistono; poi, scritto lo stato, lo ricostruiva. E
    `base_review` rilegge e riparsa `review_data.json` per intero — 2,4 MB sul
    confronto vero — piu' `upload_profiles.json`, il catalogo e gli sconti. La
    pagina si autosalva 450 ms dopo ogni modifica: due volte per ogni quantita'
    toccata, e il primo dei due risultati si buttava via.

    ⚠ Qui si conta, non si cronometra: un test sul tempo passerebbe o
    fallirebbe a seconda di quanto e' carica la macchina che lo esegue.
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
        """Quante volte `base_review` ha riletto il confronto da disco."""

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
        """⚠ Il primo salvataggio no, e non e' una dimenticanza: senza un

        `state.json` sul disco lo stato non dichiara nessuna run, e il
        confronto va rifatto (vedi la prova qui sotto). Quello che conta e' il
        salvataggio di regime, che la pagina fa 450 ms dopo ogni modifica."""

        primo = self.store.save_state(self.snapshot)

        volte, esito = self.conta_le_costruzioni(lambda: self.store.save_state(
            {**self.snapshot, "stateVersion": primo["stateVersion"]}
        ))

        self.assertTrue(esito["ok"])
        self.assertEqual(volte, 1)

    def test_anche_l_anteprima_dello_spostamento_lo_costruisce_una_volta(self) -> None:
        """Qui vale sempre: l'anteprima non scrive niente, quindi il confronto

        costruito dalla convalida e' letteralmente quello che si otterrebbe
        rifacendolo."""

        volte, _ = self.conta_le_costruzioni(lambda: self.store.move_preview({
            **self.snapshot,
            "from": "larice",
        }))

        self.assertEqual(volte, 1)

    def test_con_uno_stato_di_un_altra_run_il_confronto_si_ricostruisce(self) -> None:
        """L'unico caso in cui riusare il confronto cambierebbe qualcosa.

        `apply_match_overrides` — le risposte date ai candidati — si applica
        solo se lo stato sul disco dichiara la stessa run del confronto. Con
        uno stato che ne dichiara un'altra, quello costruito prima della
        scrittura le salta e quello di dopo no: qui si pretende che il
        programma se ne accorga e ricostruisca, invece di rispondere con un
        confronto a cui manca qualcosa.
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
        """Se il confronto riusato non fosse quello che si sarebbe ricostruito,

        il secondo salvataggio direbbe numeri diversi dal primo."""

        primo = self.store.save_state(self.snapshot)
        secondo = self.store.save_state({**self.snapshot, "stateVersion": primo["stateVersion"]})

        self.assertEqual(primo["orderSummary"], secondo["orderSummary"])
        self.assertEqual(primo["promotionStates"], secondo["promotionStates"])

    def test_il_confronto_riusato_dice_quello_che_direbbe_uno_ricostruito(self) -> None:
        """La proprieta' che rende lecita la scorciatoia, provata invece che

        dichiarata: fra la lettura e la scrittura non cambia niente da cui il
        confronto dipenda, quindi i numeri devono essere gli stessi.

        ⚠ Lo stato di partenza porta le chiavi da cui il confronto dipende —
        uno sconto di testata e un prodotto aggiunto a mano — e non e' un
        ornamento: la prima versione girava su uno stato che non ne aveva
        nessuna, cioe' su un confronto in cui non c'era niente da perdere.
        Trovato dalla verifica avversariale del 20 agosto 2026, che ha fatto
        notare come il difetto chiuso subito dopo fosse stato trovato
        rileggendo il codice e non da questa prova.
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
        # Lo stato vive in una sottocartella "run" perche' lo storico degli ordini
        # viene ricavato da state_path.parent.parent/history/orders.json: con lo
        # stato nella radice della cartella temporanea le compile() dei test
        # scriverebbero fuori da essa, in %TEMP%\history\orders.json.
        self.run_dir = self.root / "run-corrente"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "review_state.json"
        self.upload_dir = self.root / "uploads"
        self.output_dir = self.root / "outputs"
        # Fase 6d: ogni compilazione ha la sua cartella datata qui dentro. Il
        # piano non sta piu' in `outputs` con un nome fisso, ed e' il motivo per
        # cui tutte le prove qui sotto lo cercano passando per `piano_di`.
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

    # -- Fase 6d: dove sono finiti i file di una compilazione ----------------

    def cartelle(self) -> list[Path]:
        """Le cartelle delle compilazioni presenti, in ordine di nome."""

        if not self.orders_dir.is_dir():
            return []
        return sorted(item for item in self.orders_dir.iterdir() if item.is_dir())

    def cartella_di(self, esito: dict[str, Any]) -> Path:
        """La cartella della compilazione appena fatta, presa dalla sua risposta."""

        return self.orders_dir / esito["cartella"]

    def piano_di(self, esito: dict[str, Any]) -> dict[str, Any]:
        """Il piano di quella compilazione, letto dalla sua cartella datata."""

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
        # Il piano non si scrive: quindi non deve nascere nemmeno la cartella
        # che lo avrebbe contenuto. Una cartella vuota comparirebbe nell'elenco
        # come una compilazione avvenuta e senza file.
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
        # L'espositore consegna i suoi pezzi, non se stesso: 2 espositori da 96
        # sono 192 pezzi a 4,6054 l'uno. Il totale non cambia, perche' si fattura
        # l'espositore intero.
        self.assertEqual(order["quantity_factor"], 96.0)
        self.assertEqual(order["delivered_pieces"], 192)
        self.assertEqual(order["unit_price_net"], 4.6054)
        self.assertEqual(order["order_unit_price_net"], 442.1196)
        self.assertEqual(order["line_total_net"], 884.2392)
        self.assertNotIn(order["supplier_source_row"], {199, 200})

    def test_standard_product_quantity_is_colli_with_no_rounding(self) -> None:
        # Nuova regola commerciale: l'utente inserisce direttamente i colli da
        # ordinare, non piu' i pezzi desiderati. 10 colli restano 10 colli:
        # niente arrotondamento e niente eccedenza, anche se il collo contiene
        # 6 pezzi (quantityFactor dell'offerta larice).
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
        # Stesso principio della nuova regola isolato dal calcolo del prezzo:
        # 3 colli restano 3 colli sia che il collo contenga 6 pezzi (larice)
        # sia che ne contenga 97 (fornitore con un fattore molto diverso).
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

        # Prodotto normale (larice, 6 pezzi/collo) + espositore (larice, 1
        # pezzo/collo: l'espositore stesso e' l'unita' d'ordine).
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

        # Noce: il fattore e' il moltiplicatore d'ordine del campo "unit"
        # del listino, esattamente come per l'offerta EAN normale qui sopra.
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
        # scripts/write_supplier_orders.mjs e la compilazione Noce in
        # app/server.py leggono queste chiavi per nome dal piano compilato: se
        # sparissero, la scrittura dei listini si romperebbe senza che nessun
        # altro test se ne accorga.
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
        """Una quantità toccata dall'utente non deve tornare 'gestionale' dopo un
        ricaricamento: altrimenti il comando che azzera le sole quantità
        predefinite cancellerebbe una scelta deliberata."""

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
            # ⚠ `adapterId` e `schemaState` non sono decorazione: sono i due
            # campi con cui il catalogo sceglie il lettore, gli stessi su cui
            # decide la catena. Una review vera li porta sempre — li scrive
            # `build_review_data.manifest_files` — e senza di loro questa
            # fixture descriveva una review che non esiste.
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
        # Nuova regola: anche per un prodotto aggiunto manualmente dal
        # catalogo l'unita' di quantita' e' il collo, non il pezzo.
        self.assertEqual(manual["quantityLabel"], "colli")

    def test_catalog_ranks_suppliers_by_price_per_piece_not_by_total_per_carton(self) -> None:
        # La regressione piu' pericolosa della nuova regola, riprodotta anche
        # nel percorso "aggiungi dal catalogo": cipresso ha il totale per
        # collo piu' basso (12.00 EUR) ma NON e' il piu' conveniente al pezzo
        # (2.00 EUR/pezzo contro 1.00 EUR/pezzo di noce, che vende colli
        # da 24 pezzi invece che da 6). Deve vincere noce.
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

        # Precondizione del trabocchetto: cipresso vince sul totale in colli...
        self.assertLess(offers["cipresso"]["orderUnitPriceNet"], offers["noce"]["orderUnitPriceNet"])
        # ...ma noce vince sul prezzo al pezzo, il solo criterio valido.
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
            # La run del confronto attivo: senza, la compilazione si ferma
            # prima di guardare qualsiasi altra cosa, ed e' giusto cosi'.
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
        # Il piano sta nella cartella datata e **non** in `outputs`: e' il
        # cambiamento della 6d, ed e' quello che impedisce alla compilazione
        # successiva di sovrascriverlo.
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
        # Nemmeno nella cartella della compilazione: il writer non e' partito
        # affatto, quindi li' dentro c'e' solo il piano con il suo audit.
        self.assertFalse(any(self.cartella_di(result).glob("*.xlsx")))

    def test_launcher_recognizes_cipresso_only_with_verified_mapping(self) -> None:
        launcher_path = SKILL_ROOT / "app" / "launcher.py"
        launcher_spec = importlib.util.spec_from_file_location("compara_ordini_launcher_test", launcher_path)
        if launcher_spec is None or launcher_spec.loader is None:
            self.fail("Impossibile importare il launcher")
        launcher = importlib.util.module_from_spec(launcher_spec)
        sys.modules[launcher_spec.name] = launcher
        launcher_spec.loader.exec_module(launcher)
        # ⚠ Copia congelata del registro: `references/adapters.json` lo riscrive
        # il programma quando impara uno schema confermato, e questa prova
        # poggia su quello che l'adattatore CIPRESSO **consegnato** dichiara.
        # Vedi la nota in `tests/test_registro_impronte.py`.
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
                # ⚠ Noce c'e' anche qui, come nella configurazione vera che
                # `prepare_writer_config` scrive: un fornitore ordinato ci sta
                # sempre. Prima mancava, e il writer lo saltava perche' si
                # chiamava «noce» — cioe' era questo banco a tenere in piedi
                # il nome cablato. Adesso a saltarlo e' la **procedura** che la
                # sua regola dichiara, e un fornitore ordinato senza listino
                # configurato viene detto invece che ignorato.
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
        # ⚠ Il writer Node non produce **niente** per Noce: a loro si
        # rimanda il loro `.xls`, compilato in posizione da `app/xls_writer.py`.
        # Il piano qui sopra porta una riga Noce apposta, cosi' questa prova
        # fallisce se il ramo tornasse con un nome che dica «noce».  Un
        # artefatto con un nome del tutto diverso non lo vedrebbe: e' la guardia
        # piu' larga che si possa scrivere senza elencare nomi che non esistono.
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
        # La riga Noce del piano non produce piu' niente **qui**: la sua
        # copia la scrive il servizio locale dentro il loro `.xls`, e il writer
        # Node si limita a contarla nel proprio riepilogo.
        # Il writer stampa anche righe di servizio del motore fogli: il
        # riepilogo e' l'ultimo oggetto JSON dell'uscita.
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
        """⚠ Il lucchetto dei dati non basta a impedirlo, e il risultato inganna.

        `compile` prende `self.lock`; la catena lo prende solo nell'istante in
        cui sostituisce il confronto vivo. Per tutte le nove fasi prima di
        quello, compilare e' legittimo e produce i listini del confronto
        **precedente** — i prezzi della settimana scorsa — mentre in pagina 1
        una barra dice che il confronto si sta aggiornando. Basta far partire
        il ricalcolo e passare alla pagina 3.
        """

        vero = self.store.pipeline_jobs.in_corso
        self.store.pipeline_jobs.in_corso = lambda: True
        try:
            with self.assertRaises(LavoroGiaInCorso) as fermato:
                self.store.compile(self.snapshot(1, accept_below_threshold=True))
        finally:
            self.store.pipeline_jobs.in_corso = vero

        # La frase e' quella che legge chi ordina, e dice perche' non adesso.
        self.assertIn("si sta aggiornando", str(fermato.exception))
        # E nessuna cartella: un ordine non partito non lascia niente in giro.
        self.assertEqual(self.cartelle(), [])

        # La controprova: finito il ricalcolo, lo stesso comando compila.
        self.store.compile(self.snapshot(1, accept_below_threshold=True))
        self.assertEqual(len(self.cartelle()), 1)

    def test_una_compilazione_rifiutata_non_lascia_nessuna_cartella(self) -> None:
        """Il fratello della prova qui sopra, sul difetto che la 6d introduce.

        Creare la cartella prima delle convalide sarebbe comodo e lascerebbe
        dietro una compilazione fantasma per ogni ordine vuoto o sotto soglia:
        l'elenco delle compilazioni precedenti si riempirebbe di cartelle che
        non contengono niente e che nessuno ha chiesto.
        """

        # La radice esiste gia' (la crea il costruttore), ed e' vuota.
        self.assertTrue(self.orders_dir.is_dir())
        self.assertEqual(self.cartelle(), [])

        with self.assertRaises(SnapshotError):
            self.store.compile(self.snapshot(0, accept_below_threshold=True))
        self.assertEqual(self.cartelle(), [])

        with self.assertRaises(SnapshotError):
            self.store.compile(self.snapshot(1, accept_below_threshold=False))
        self.assertEqual(self.cartelle(), [])

        # E la prova che il conteggio non e' vuoto per un altro motivo: una
        # compilazione accettata la cartella la crea eccome.
        self.store.compile(self.snapshot(1, accept_below_threshold=True))
        self.assertEqual(len(self.cartelle()), 1)

    # -- Fase 9b: l'ingresso del listino Noce in Excel 97-2003 ----------

    def carica(self, nome: str, contenuto: bytes, role: str = "suppliers") -> dict[str, Any]:
        return self.store.upload({"files": [{
            "name": nome,
            "data": base64.b64encode(contenuto).decode("ascii"),
            "role": role,
        }]})

    def test_il_listino_xls_di_noce_si_carica_e_viene_riconosciuto(self) -> None:
        """Era il difetto più semplice e il più bloccante: «Formato non accettato: .xls»."""
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
        # ⚠ Seguiva «Codex deve confermarne la struttura prima del ricalcolo»:
        # un nome che chi usa il programma non conosce, per un permesso che dal
        # cantiere R6 non serve piu'.
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
        """Rilievo [41]: il taglio a 180 caratteri veniva DOPO il controllo
        sull'estensione. Un nome piu' lungo di 180 caratteri superava il
        controllo e poi perdeva il punto e l'estensione nel taglio: il nome
        risultante non era piu' quello che il controllo aveva approvato.
        """

        nome_lunghissimo = "A" * 250 + ".xlsx"

        tagliato = SERVER.safe_upload_name(nome_lunghissimo)

        self.assertEqual(len(tagliato), 180)
        self.assertEqual(Path(tagliato).suffix, ".xlsx")

    def test_un_listino_con_nome_lunghissimo_arriva_sul_disco_ancora_xlsx(self) -> None:
        """La conseguenza vera non e' un messaggio d'errore: e' un listino che
        sparisce dal confronto senza che nessuno lo dica. Il caricamento
        riesce (il lettore sceglie dal contenuto, non dal nome), ma
        `candidate_files` di `scripts/inspect_sources.py` filtra per suffisso
        quando la catena rilancia l'inventario sulla cartella: un file senza
        estensione non entra e il fornitore sparisce dal confronto in
        silenzio.
        """

        sorgente = listino_finto(self.root / "sorgente.xlsx", "LARICE")
        nome_lunghissimo = "A" * 250 + ".xlsx"

        esito = self.carica(nome_lunghissimo, sorgente.read_bytes())

        self.assertTrue(esito["ok"])
        # `upload_profiles.json` vive nella stessa cartella dei documenti
        # caricati: si guarda solo la copia appena scritta, presa dalla
        # risposta, non tutto quello che c'e' dentro `upload_dir`.
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
        """Un .xls rinominato .csv si legge lo stesso, ma va detto: le colonne non torneranno."""
        listino = pipeline.scrivi_listino_noce(
            self.root / "vero.xls", pipeline.RIGHE_DI_PROVA, pipeline.RIGHE_IN_CODA,
        )

        esito = self.carica("listino_noce.csv", listino.read_bytes())

        self.assertEqual(esito["files"][0]["contentFormat"], "xls")
        self.assertIn("«listino_noce.csv» dentro è un Excel 97-2003 (.xls)", esito["message"])
        self.assertIn("letto per quello che è", esito["message"])

    def test_un_fornitore_non_letto_diventa_un_avviso_e_non_toglie_gli_altri(self) -> None:
        """Il catalogo che si assottiglia in silenzio è peggio di uno che non si carica."""
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
        # Lo stesso file non viene segnalato due volte con parole diverse.
        doppioni = [item for item in prodotta["warnings"] if item.get("code") == "CONDIZIONI_COMMERCIALI_NON_LETTE"]
        self.assertEqual(doppioni, [])
        # E l'altro fornitore e' rimasto in piedi.
        self.assertEqual(store.search_products("tovaglioli")["count"], 1)

    def test_le_condizioni_di_un_listino_rovinato_non_fanno_fallire_la_pagina(self) -> None:
        """Il lettore delle soglie Larice apriva il file senza rete: bastava un
        listino rovinato perché `GET /api/review` non rispondesse più."""
        rotto = self.root / "larice.xlsx"
        rotto.write_bytes(b"questo non e' un foglio di calcolo")
        servizio = SERVER.PromotionService()
        review = {"files": [{"supplierId": "larice", "sourcePath": str(rotto)}], "products": []}

        self.assertEqual(servizio.detect(review), [])
        self.assertEqual(len(servizio.load_errors), 1)
        self.assertIn("larice.xlsx", servizio.load_errors[0]["message"])
        self.assertEqual(servizio.load_errors[0]["supplier"], "larice")

        # La seconda lettura non riapre il file, ma il motivo lo dice lo stesso.
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
        """Chi ricarica un listino e legge «acquisito» crede di averlo aggiornato."""
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
        """«Gia' presente» deve voler dire che la copia c'e'.

        ⚠ Il vicolo cieco del 15 agosto 2026, con le parole di chi ci e' finito
        dentro: «ho cancellato i listini dalla cartella dell'app e ora, provando
        a caricarli, mi segnala che sono gia' presenti, ma io non li vedo e non
        mi fa nemmeno confrontare i listini».  Il registro dei profili teneva la
        scheda di un documento che sul disco non c'era piu': il caricamento la
        leggeva come «duplicato» e non scriveva niente, mentre la pagina — che i
        profili senza copia li filtra gia' — non mostrava nessuna scheda da
        eliminare per uscirne.
        """

        contenuto = b"ean;prodotto\n8000000000001;PRIMO\n"
        self.carica("larice-nuovo.csv", contenuto)
        # Cancellato da Esplora risorse, non dall'app: l'app non lo sa.
        (self.upload_dir / "larice-nuovo.csv").unlink()

        secondo = self.carica("larice-nuovo.csv", contenuto)

        self.assertEqual(secondo["files"][0]["status"], "profiled")
        self.assertIn("caricato e letto", secondo["message"])
        self.assertTrue((self.upload_dir / "larice-nuovo.csv").exists())
        profili = json.loads((self.upload_dir / "upload_profiles.json").read_text(encoding="utf-8"))
        nomi = [item.get("file_name") for item in profili["profiles"]]
        # Una copia sola, una scheda sola: due schede sullo stesso percorso
        # sarebbero due verita' sullo stesso file.
        self.assertEqual(nomi.count("larice-nuovo.csv"), 1)
        # E la pagina lo rivede: e' la prova che il giro si chiude.
        self.assertTrue(any(
            item.get("uploadName") == "larice-nuovo.csv" for item in self.store.review()["files"]
        ))

    def test_una_scheda_che_parla_di_un_altro_file_non_vale_per_questo(self) -> None:
        """Il profilo dichiara un percorso: se non e' questa copia, non e' suo.

        ⚠ Controprova rimasta VERDE al primo giro: togliendo il confronto fra
        percorso dichiarato e copia vera, i test non se ne accorgevano — tutti
        passavano dal ramo «il file non c'e'», che si ferma prima.  Questo e' il
        caso che quel confronto serve a fermare: una scheda che parla
        dell'originale sul Desktop mentre negli upload c'e' un file omonimo.
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
        """Il registro descrive la cartella, e la cartella è l'unica verità."""

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
        """«Eliminando un listino questo continuava a comparire nel confronto».

        Il difetto segnalato il 14 agosto 2026, con le parole di chi lo ha
        subito.  La cancellazione toglieva la scheda del file e lasciava intatto
        tutto il resto: il fornitore restava fra le offerte di ogni prodotto,
        restava quello scelto e restava nel totale in euro del riepilogo.

        Le offerte non spariscono — restano visibili, perche' l'utente deve
        capire perche' il prezzo di prima non c'e' piu' — ma smettono di essere
        ordinabili, ed e' scritto perche'.
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
        """«Non lo so» non e' «e' stato tolto», e solo il secondo spegne le offerte.

        Il confronto vivo di Daniele ha quattro fornitori con offerte e un solo
        documento nell'elenco: se bastasse l'assenza dall'elenco per spegnere un
        fornitore, aprendo il programma troverebbe tutto non ordinabile senza
        che nessuno abbia eliminato niente.
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
        # I prodotti restano: non si potano dal confronto, e nessuno lo chiede.
        self.assertEqual(len(dopo["products"]), len(review["products"]))
        # ⚠ Ma le offerte del fornitore eliminato smettono di essere ordinabili.
        # Fino al 14 agosto 2026 questa prova si fermava alla riga qui sopra, e
        # quel solo conteggio **codificava il difetto come atteso**: il
        # fornitore restava nelle offerte, restava quello scelto e restava nel
        # totale del riepilogo.  La scheda qui dichiara solo `supplier`, senza
        # `supplierId`: e' la forma che i confronti piu' vecchi hanno, ed e' il
        # motivo per cui la chiave si ricava da tutte e due.
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
        # Eliminare il gestionale e' una funzione voluta — serve quando se ne
        # carica uno sbagliato — ma i prodotti del confronto vengono da li' e
        # restano in pagina, con «Continua» attivo.  La conseguenza va detta,
        # altrimenti si preparano gli ordini della settimana su un elenco che
        # nel programma non c'e' piu'.
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
        """Le copie che il programma non conosce si accumulano e nessuno le vede."""
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
        """Il catalogo legge il listino, il motore delle promozioni no: due guasti diversi."""
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
    """⚠ La fascia c'era, la pagina sapeva leggerla, e non si e' mai vista.

    `cambiamentoDocumenti` legge `pipeline.stato.cambiamento` e ha cinque prove
    che la coprono, ma **nessuno scriveva quel campo**: `input_modificato`
    metteva soltanto un messaggio, e il messaggio in pagina non compare (la
    barra delle fasi tace su IN_ATTESA, apposta). Chi cancellava il listino
    della settimana scorsa e caricava quello nuovo si ritrovava le pagine 2 e 3
    con i prezzi di prima, e niente che glielo dicesse.

    Verificato il 22 agosto 2026 con una ricerca su tutto il codice: la stringa
    `"cambiamento"` compariva **solo nei file di prova**. E' la forma piu'
    insidiosa di prova verde — prova che chi legge funziona, e nessuno prova
    che qualcuno lo alimenti.
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
        """⚠ Visto in pagina il 23 agosto 2026, con il servizio vero.

        Caricando insieme l'elenco e un listino, la fascia diceva «Hai caricato
        i listini BETULLA e FORNITORE dopo l'ultimo confronto». L'elenco del
        gestionale ha un adattatore come tutti (`gestionale_v1`) ma non ha un
        `supplier_id`, e `supplier_label("")` risponde «FORNITORE» — il ripiego
        generico del registro. La guardia `if etichetta` non poteva bastare:
        «FORNITORE» è una stringa piena.
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
        # Il documento si nomina; il fornitore inventato no.
        self.assertEqual(cambiamento.get("fornitori") or [], [])

    def test_eliminarne_uno_lo_dichiara(self) -> None:
        self.carica("listino-vecchio.csv", b"ean;prodotto\n8000000000001;PRIMO\n")

        esito = self.store.delete_upload({"name": "listino-vecchio.csv"})

        cambiamento = esito["pipeline"].get("cambiamento") or {}
        self.assertEqual(cambiamento.get("tipo"), "eliminato")
        self.assertIn("listino-vecchio.csv", cambiamento.get("documenti") or [])

    def test_e_il_campo_finisce_sul_disco_cosi_sopravvive_al_riavvio(self) -> None:
        """⚠ Nella sua prima stesura questa prova rimase VERDE togliendo il
        salvataggio: leggeva lo stato dalla **memoria**, e un riavvio del
        programma avrebbe fatto sparire la fascia. Qui si guarda il file, che e'
        quello che il servizio rilegge quando riparte.
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
    """Un fornitore che manca va detto prima che l'utente scelga, non dopo.

    Fino alla Fase 9b gli avvisi della review comparivano soltanto nel riquadro
    del passo 3: chi sceglieva prodotti e fornitori al passo 2 non sapeva che un
    listino era rimasto fuori dal confronto.

    ⚠ Queste sono prove **a sottostringa**: guardano il sorgente, non il
    comportamento.  La regola vera — quali avvisi entrano nel riquadro e quali
    restano sulla scheda del prodotto — la prova
    `tests/test_interfaccia_pagina1.py`, che esegue `app.js` in Node.  Qui
    resta soltanto il cablaggio: il riquadro esiste e arriva a tutte e due le
    pagine.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.app_js, f"manca la funzione {name}")
        return self.app_js.split(marker)[1].split("\n}\n")[0]

    def test_gli_avvisi_dei_documenti_hanno_un_riquadro_proprio(self) -> None:
        # La scelta di quali avvisi mostrare sta in `avvisiDelConfronto`, che è
        # anche il posto da cui il riquadro del ricalcolo scopre quali avvisi
        # sono già stampati e non li ripete.
        selezione = self.body("avvisiDelConfronto")
        self.assertIn("state.review?.warnings", selezione)
        # Gli avvisi legati a un prodotto restano nella scheda del prodotto.
        self.assertIn("!issue.productId", selezione)
        corpo = self.body("renderSourceWarnings")
        self.assertIn("avvisiDelConfronto()", corpo)
        # Il riquadro non chiama piu' `renderAlert` da se': passa da
        # `renderAvvisi`, che stampa i singoli e conta quelli che si ripetono.
        # Che gli avvisi arrivino davvero in pagina lo provano le prove
        # eseguite in `test_interfaccia_pagina1.py`.
        self.assertIn("renderAvvisi(warnings", corpo)

    def test_ogni_pagina_chiede_i_suoi_avvisi(self) -> None:
        """Lo stesso riquadro con lo stesso titolo stava in cima a tutt'e due,
        con dentro le stesse frasi. Adesso la pagina dice quale mazzo vuole:
        1 i documenti, 2 i prodotti. Che il taglio sia quello giusto lo provano
        le prove eseguite in `test_interfaccia_pagina1.py`."""

        self.assertIn("renderSourceWarnings(1)", self.body("renderUploadStep"))
        self.assertIn("renderSourceWarnings(2)", self.body("renderQuantityStep"))


class LaReidratazioneDelRicalcoloTests(unittest.TestCase):
    """Chi ricarica la pagina deve ritrovare anche l'esito, non solo la barra.

    Il servizio lo stato finale ce l'ha e continua a darlo (lo tiene in
    `pipeline_status.json` e lo rilegge anche dopo un riavvio): era la pagina a
    buttarlo via, perche' all'avvio riprendeva soltanto un ricalcolo `IN_CORSO`.
    Un ricalcolo fermato a meta', o finito con degli avvisi, spariva al primo
    ricaricamento — e fra quegli avvisi c'e' proprio quello che dice che la
    compilazione va ricontrollata prima di creare le copie.

    Sono asserzioni sul testo sorgente, come in `ConsegnaInterfacciaTests`:
    l'unica cosa che un test del servizio non puo' vedere e' la pagina.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.app_js, f"manca la funzione {name}")
        return self.app_js.split(marker)[1].split("\n}\n")[0]

    def reidratazione(self) -> str:
        """Il pezzo che gira all'apertura della pagina, dopo `loadReview()`."""

        marker = "loadReview();"
        self.assertIn(marker, self.app_js)
        blocco = self.app_js.split(marker)[-1]
        self.assertIn("API.pipelineStato", blocco)
        return blocco

    def callback_di_reidratazione(self) -> list[str]:
        """Le righe di CODICE della callback, con gli apici normalizzati.

        ⚠ La prima versione di queste prove cercava il testo esatto della
        guardia vecchia (`!== "IN_CORSO"`): la revisione avversariale del 13
        agosto 2026 ha riscritto il comportamento vecchio con gli apici singoli
        e le tre asserzioni sono passate tutte.  Qui si asserisce sulla
        struttura: gli apici si normalizzano, i commenti non contano, e le
        regole sono «un solo return, quello di IN_ATTESA» e «IN_CORSO compare
        una volta sola, a guardia del polling» — qualunque modo di scartare gli
        esiti finiti ha bisogno o di un secondo return o di un secondo
        IN_CORSO.
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
        # Un solo scarto, quello di «non e' mai partito niente»: ogni altro
        # esito — fermata, avvisi, riuscita — si rimette in pagina.
        con_return = [riga for riga in righe if "return" in riga]
        self.assertEqual(len(con_return), 1, con_return)
        self.assertIn('esito === "IN_ATTESA"', con_return[0])
        corpo = "\n".join(righe)
        self.assertIn("state.pipeline.stato = stato", corpo)
        self.assertIn("state.pipeline.chiesto = true", corpo)
        self.assertIn("render()", corpo)

    def test_si_continua_a_interrogare_solo_una_run_viva(self) -> None:
        """Su una run finita il timer ripeterebbe per sempre la stessa risposta."""

        righe = self.callback_di_reidratazione()
        con_in_corso = [riga for riga in righe if "IN_CORSO" in riga]
        # IN_CORSO compare UNA volta sola: a guardia del polling.  Una seconda
        # occorrenza e' il segno che qualcuno sta di nuovo filtrando gli esiti.
        self.assertEqual(len(con_in_corso), 1, con_in_corso)
        self.assertEqual(
            con_in_corso[0],
            'if (esito === "IN_CORSO") pianificaControlloPipeline();',
        )

    def test_il_riquadro_mostra_la_fermata_e_gli_avvisi(self) -> None:
        """L'altra metà: ripescarli non serve se poi non si vedono."""

        corpo = self.body("renderAvanzamentoPipeline")
        self.assertIn("stato.fermata", corpo)
        self.assertIn("Il confronto si è fermato", corpo)
        self.assertIn("stato.avvisi", corpo)
        self.assertIn("renderAlert", corpo)


class PromotionPanelTests(unittest.TestCase):
    """Fase 8: 188 condizioni lette, 13 che possono cambiare l'ordine.

    Sui listini veri 175 delle 188 sono sconti già compresi nel prezzo al pezzo
    su cui si sceglie il fornitore. Mostrarle insieme alle altre è ciò che
    rendeva il pannello inutilizzabile.
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
        """Una condizione senza prodotti abbinati non dice quanto manca."""

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
        # Le due liste non devono essere la stessa lista.
        self.assertIn("!promotionIsAlreadyInPrice(promotion)", corpo)

    def test_prima_le_offerte_a_cui_manca_meno(self) -> None:
        corpo = self.body("sortPromotionsByReach")
        self.assertIn("promotionRemainingQty", corpo)
        self.assertIn("PROMOTION_STATUS_ORDER", corpo)

    def test_la_ricerca_esiste_e_il_riquadro_resta_aperto_mentre_si_scrive(self) -> None:
        self.assertIn("data-promotion-search", self.app_js)
        self.assertIn('data-focus-key="promotion-search"', self.app_js)
        # Senza memoria dell'apertura il riquadro si richiude a ogni ridisegno e
        # il campo sparisce da sotto le dita.
        self.assertIn("state.promotionCatalogOpen", self.app_js)
        self.assertIn('data-promotion-catalog ${state.promotionCatalogOpen ? "open" : ""}', self.app_js)
        # L'evento toggle di <details> non risale: senza la fase di cattura il
        # listener non viene mai chiamato e l'apertura non si ricorda.
        toggle = self.app_js.split('appElement.addEventListener("toggle"')[1].split("\n\n")[0]
        self.assertTrue(toggle.rstrip().endswith("}, true);"), "il listener toggle deve essere in cattura")


class SupplierMoveInterfaceTests(unittest.TestCase):
    """Fase 7, lato browser: le regole che nessun test del server può difendere.

    Il calcolo dello spostamento sta sul server ed è coperto da
    tests/test_supplier_move.py. L'APPLICAZIONE però avviene nel browser, e tre
    mutazioni ad app.js — ereditare la conferma, riscrivere i colli, rompere
    l'annulla — passavano con la suite tutta verde.
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
        # Una conferma data su un'offerta non vale per un'altra: se si eredita,
        # l'ordine parte senza che nessuno abbia guardato il prodotto.
        self.assertIn("product.confirmed = false;", corpo)

    def test_lo_spostamento_non_tocca_quantita_esclusioni_e_origine(self) -> None:
        corpo = self.body("applySupplierMove")
        for vietato in ("product.quantity =", "product.excluded =", "product.quantitySource ="):
            self.assertNotIn(vietato, corpo, f"lo spostamento non deve scrivere {vietato.strip(' =')}")

    def test_l_annulla_ripristina_fornitore_e_conferma_di_prima(self) -> None:
        applica = self.body("applySupplierMove")
        # Per ripristinare bisogna prima aver conservato entrambi i valori.
        self.assertIn("selectedSupplierId: product.selectedSupplierId,", applica)
        self.assertIn("confirmed: Boolean(product.confirmed),", applica)

        annulla = self.body("undoSupplierMove")
        self.assertIn("product.selectedSupplierId = entry.selectedSupplierId;", annulla)
        self.assertIn("product.confirmed = entry.confirmed;", annulla)

    def test_la_differenza_di_spesa_non_si_mostra_mai_da_sola(self) -> None:
        """Spendere meno ricevendo meno merce non è un risparmio."""

        corpo = self.body("moveDeltaParts")
        self.assertIn("deltaPieces", corpo)
        # "si risparmia" deve stare in un ramo che ha già escluso il calo di merce.
        prima_del_risparmio = corpo.split('words: "si risparmia"')[0]
        self.assertIn("pieces < 0", prima_del_risparmio)
        self.assertIn("si spende meno, ma arriva meno merce", corpo)

    def test_le_soglie_distinguono_chi_un_ordine_non_ce_l_aveva(self) -> None:
        """Un fornitore partito da zero non può "scendere" sotto la soglia."""

        corpo = self.body("moveThresholdNotes")
        # Non basta che hadOrderBefore compaia da qualche parte: deve essere
        # proprio la condizione che decide se la soglia era raggiunta prima.
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
        """Altrimenti l'ordine non si salva e non c'è nessun modo di sbloccarlo."""

        corpo = self.body("renderConfirmation")
        self.assertIn("confirmationRequired(product)", corpo)
        regola = self.body("confirmationRequired")
        self.assertIn("selectedOffer(product)", regola)
        self.assertIn("offer.requiresConfirmation", regola)
        # ⚠ E senza fornitore scelto non c'è niente da confermare: e' la stessa
        # riga di confine del servizio. Senza, rifiutare l'unica offerta lasciava
        # in piedi il bloccante «Conferma richiesta» su un prodotto in cui la
        # casella non compariva da nessuna parte.
        self.assertIn("if (!offer) return false;", regola)
        # L'offerta deve portarsi dietro il campo dal servizio locale.
        self.assertIn("requiresConfirmation: Boolean(offer.requiresConfirmation", self.app_js)


class QuantityWheelGuardTests(unittest.TestCase):
    """Fase 3bis: la rotellina non deve poter cambiare una quantità d'ordine."""

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
        # Senza { passive: false } preventDefault() viene ignorato e la protezione
        # non protegge nulla.
        coda = self.app_js.split('appElement.addEventListener("wheel"')[1].split("\n\n")[0]
        self.assertIn("passive: false", coda)


class InterfacciaR5Tests(unittest.TestCase):
    """Le richieste R5 devono restare azioni brevi, non nuovi muri di testo."""

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
        # Il motivo non sta piu' dietro un pieghevole: era l'unica cosa che
        # serve per decidere, ed era l'unica nascosta (revisione 14/8/2026).
        # Il motivo non sta piu' dietro un pieghevole: era l'unica cosa che
        # serve per decidere, ed era l'unica nascosta (revisione 14/8/2026).
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
        # La conferma dice che cosa si perde, non come si chiama dentro il
        # programma: «promemoria» era il nome della struttura, non della cosa.
        conferma = self.body("renderCompilazione")
        self.assertNotIn("e i suoi promemoria", conferma)
        self.assertIn("è arrivata la merce?", conferma)
        self.assertIn("Non si può annullare", conferma)

    def test_l_attesa_di_autosalvataggio_resta_450_millisecondi(self) -> None:
        self.assertIn("window.setTimeout(() => saveState(), 450)", self.body("scheduleSave"))


class LaCompilabilitaVieneDalRegistroTests(unittest.TestCase):
    """Satellite 1 della verifica del 12 agosto 2026.

    I fornitori compilabili erano una tupla dentro `launcher.py`, contro la
    regola per cui la regola sta nel registro: un fornitore imparato non
    sarebbe mai potuto diventare compilabile, e nessuno lo diceva. Misurato:
    102 prodotti assegnati a ACERO e avvertimenti vuoti.
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
        """Nessuna riga di codice: la regola sta nel registro, il codice la applica."""

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
        """Se l'intestazione dichiarata non è lì, l'ordine finirebbe altrove."""

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
        """Il controllo dev'essere lineare, non quadratico.

        Il 15 agosto 2026 l'avvio del programma e' rimasto **muto per cinque
        minuti** e Daniele l'ha dato per piantato.  Non era piantato: su un
        foglio aperto in sola lettura `foglio.cell(r, c)` rilegge il foglio
        dall'inizio a ogni chiamata, e questo controllo ne faceva una per riga
        di listino — misurate 3382 chiamate su CIPRESSO, 5,7 milioni di righe
        analizzate, 474 secondi dentro `prepare_writer_config`.

        Il tetto qui e' larghissimo di proposito: la passata unica sta sotto il
        secondo, quella vecchia su queste righe ci metteva minuti.  Non misura
        la velocita' del computer, misura che l'algoritmo non sia tornato
        quadratico.
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
        """La regola e' cambiata di posto, non di contenuto.

        L'elenco non e' fisso a quattro: e' quello che il registro spedito
        dichiara, e cresce quando si aggiunge un fornitore. `offerte` e' il
        listino promozionale di CIPRESSO, aggiunto il 21 agosto 2026 — arriva
        senza riga di intestazione e si riconosce dalla forma delle colonne.
        Questa prova serve a far notare l'aggiunta di un fornitore
        compilabile, non a impedirla: se il numero cambia, si aggiorna qui
        dopo aver guardato che cosa e' entrato.
        """

        self.assertEqual(sorted(self.launcher.fornitori_compilabili()),
                         ["betulla", "cipresso", "larice", "noce", "offerte"])

    # -- la revisione avversariale del 13 agosto 2026 ----------------------

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
        """Un refuso di una cifra scriverebbe le quantità fuori dai dati, in silenzio."""

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
        """Dati che partono sulla riga dell'intestazione non stanno insieme."""

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["header_row"] = 2
        voce["order_write"]["data_start_row"] = 2

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("non stanno insieme" in avviso for avviso in avvisi), avvisi)

    def test_un_foglio_non_dichiarato_su_un_documento_a_piu_fogli_si_rifiuta(self) -> None:
        """Prendere il primo di tre sarebbe un foglio che nessuno ha guardato."""

        voce = self.adattatore_imparato(con_scrittura=True)
        del voce["order_write"]["sheet"]
        documento = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", "ORDINE"],
                                           fogli_extra=2)

        regole, avvisi = self.avvisi_di(voce, self.confronto_su(documento))

        self.assertEqual(regole, {})
        self.assertTrue(any("il registro non indica quello esatto" in avviso
                            for avviso in avvisi), avvisi)

    def test_first_dichiarato_dal_registro_vale_il_primo_foglio(self) -> None:
        """«FIRST» scritto nel registro è una scelta, anche su più fogli."""

        voce = self.adattatore_imparato(con_scrittura=True)
        documento = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", "ORDINE"],
                                           fogli_extra=2)

        regole, avvisi = self.avvisi_di(voce, self.confronto_su(documento))

        self.assertEqual(regole["acero"]["sheet"], "Listino")
        self.assertEqual(avvisi, [])

    def test_una_procedura_di_scrittura_sconosciuta_si_rifiuta(self) -> None:
        """Un refuso in `mode` ricadeva in silenzio sulla procedura base."""

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["mode"] = "patch_xls_in_posizone"

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("procedura di scrittura sconosciuta" in avviso
                            and "patch_xls_in_posizone" in avviso for avviso in avvisi), avvisi)

    def test_le_colonne_richieste_senza_mappatura_accusano_le_colonne(self) -> None:
        """La frase diceva «le righe» quando il problema erano le colonne."""

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["required_columns"] = ["ean"]

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("chiede le colonne ean" in avviso for avviso in avvisi), avvisi)
        self.assertFalse(any("le righe da cui parte" in avviso for avviso in avvisi), avvisi)

    def test_un_foglio_dichiarato_che_non_esiste_accusa_il_registro(self) -> None:
        """Lo dichiara il registro, non una mappatura: la frase deve dirlo."""

        voce = self.adattatore_imparato(con_scrittura=True)
        voce["order_write"]["sheet"] = "Fantasma"

        regole, avvisi = self.avvisi_di(voce, self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("indicato nel registro" in avviso for avviso in avvisi), avvisi)

    def test_un_registro_rotto_non_diventa_un_non_dichiara(self) -> None:
        """«Non si legge» e «non dichiara» mandano in due posti diversi."""

        percorso = self.root / "adapters.json"
        percorso.write_text("{ rotto", encoding="utf-8")

        with mock.patch.object(self.launcher, "ADAPTERS_PATH", percorso):
            _node, _sorgenti, regole, _mancanti, avvisi = self.launcher.writer_readiness(self.confronto())

        self.assertEqual(regole, {})
        self.assertTrue(any("non si legge" in avviso for avviso in avvisi), avvisi)
        self.assertFalse(any("non dichiara" in avviso for avviso in avvisi), avvisi)

    def test_fornitori_senza_copia_porta_la_causa_vera(self) -> None:
        """L'avviso della catena deve dire perché, non solo che manca."""

        voce = self.adattatore_imparato(con_scrittura=True)
        percorso = self.registro(voce)
        sbagliato = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", "ORDINI"])

        esiti = self.launcher.fornitori_senza_copia(self.confronto_su(sbagliato), percorso)

        self.assertIn("acero", esiti)
        self.assertIn("non contiene ORDINE", esiti["acero"])

        giusto = self.listino_su_misura(intestazioni=["COD.EAN", "DESCRIZIONE", "ORDINE"])
        self.assertEqual(self.launcher.fornitori_senza_copia(self.confronto_su(giusto), percorso), {})


class LauncherBrowserTests(unittest.TestCase):
    """Il comparatore deve aprirsi in Chrome, non nel predefinito di Windows."""

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
# Fase 5d: la pagina Impostazioni
# ---------------------------------------------------------------------------

AI = SERVER.ai_client

# Due chiavi finte, e la differenza fra loro e' il punto di due prove diverse.
# La prima **non ha la forma** di una chiave OpenRouter: la rete di sicurezza a
# espressione regolare non la riconosce, quindi se sparisce da una risposta e'
# perche' il gestore si e' ricordato di quale chiave stava passando. La seconda
# ha la forma giusta e serve al caso opposto: una chiave che il gestore non ha
# mai visto — arrivata per esempio dentro l'errore di un fornitore.
CHIAVE_SENZA_FORMA = "CHIAVE-FINTA-DI-PROVA-0123456789"
CHIAVE_CON_FORMA = "sk-or-v1-0123456789abcdef0123456789abcdef"


def nomi_dei_campi(valore: Any) -> set[str]:
    """Tutti i nomi di campo di una risposta JSON, a qualunque profondita'."""

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
    """Un `EsitoAI` completo: la classe è congelata e non ha valori predefiniti."""

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
    """Un `ClientAI` che non tocca la rete e ricorda la chiave che ha ricevuto."""

    def __init__(self, configurazione: dict[str, Any], chiave: str | None, esito: Any) -> None:
        self.configurazione = configurazione
        self.chiave = chiave
        self._esito = esito

    def prova_connessione(self) -> Any:
        if isinstance(self._esito, Exception):
            raise self._esito
        return self._esito


class ImpostazioniHttpTests(unittest.TestCase):
    """La pagina Impostazioni provata dalla parte della rotta.

    Una chiave che esce dal servizio locale non si richiama indietro, e il corpo
    dei 500 rimanda `str(exc)` al programma di navigazione. Questi test guardano
    proprio quella via d'uscita, non il caso che va bene.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.secrets = self.root / "secrets.json"
        self.settings = self.root / "impostazioni_ai.json"

        # OPENROUTER_API_KEY vince su tutto: se e' impostata sul computer di chi
        # lancia la suite, questi test proverebbero un'altra cosa.
        ambiente = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""})
        ambiente.start()
        self.addCleanup(ambiente.stop)

        run_dir = self.root / "run"
        run_dir.mkdir()
        review_path = self.root / "review_data.json"
        review_path.write_text(json.dumps(synthetic_review()), encoding="utf-8")
        self.store = ReviewStore(review_path, run_dir / "state.json", self.root / "uploads", self.root / "outputs")
        # ⚠ Chiude `conferme.db` PRIMA che la cartella temporanea venga
        # cancellata: `addCleanup` esegue in ordine inverso, quindi questa riga
        # va dopo quella della cartella.  Su Windows un file aperto non si
        # cancella, e la prova che scarica le conferme — l'unica qui che il
        # database lo apre davvero — moriva alla pulizia con WinError 32,
        # mentre sul Mac passava: Unix un file aperto lo cancella.
        self.addCleanup(self.store.chiudi)

        # Quello che i due punti di rete restituiscono, deciso dal singolo test.
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
                # Si chiama il vero e se ne cattura l'uscita: quello che finisce
                # nel registro dev'essere quello che scrive lui, non una copia
                # della sua logica riscritta nel test.
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
        """Torna anche il corpo grezzo: la chiave si cerca **nei byte**, non in
        una chiave del dizionario che qualcuno potrebbe aver rinominato."""

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
        # La coda e' quattro caratteri: quello che non deve uscire e' il resto.
        self.assertNotIn(CHIAVE_SENZA_FORMA[:-4], grezzo)

    def test_lo_stato_espone_le_voci_da_cambiare_e_non_la_versione_del_prompt(self) -> None:
        _, corpo, _ = self.get("/api/impostazioni")

        self.assertEqual(set(corpo["impostazioni"]), set(SERVER.VOCI_IMPOSTAZIONI))
        self.assertNotIn("versione_prompt", corpo["impostazioni"])
        self.assertNotIn("versione_avversario", corpo["impostazioni"])

    def test_la_configurazione_ai_non_entra_nella_risposta_piu_letta(self) -> None:
        """`GET /api/review` e' la risposta piu' grande e piu' letta: la
        configurazione della fase AI non deve viaggiarci dentro, e tanto meno
        qualcosa che riguardi la chiave."""

        AI.salva_chiave(CHIAVE_SENZA_FORMA, self.secrets)

        stato, corpo, grezzo = self.get("/api/review")

        self.assertEqual(stato, 200)
        self.assertNotIn(CHIAVE_SENZA_FORMA, grezzo)
        # Si guardano i **nomi dei campi**, a qualunque profondita': cercare le
        # parole nel testo grezzo direbbe di sì anche a un prodotto che si
        # chiama «MODEL», e direbbe di no a una voce annidata in fondo.
        vietati = {*SERVER.VOCI_IMPOSTAZIONI, "versione_prompt", "versione_avversario", "openrouter", "api_key"}
        self.assertEqual(sorted(nomi_dei_campi(corpo) & vietati), [])

    # -- il salvataggio della chiave ----------------------------------------

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
        """Si prova **prima** di salvare: una chiave sbagliata non deve poter
        sostituire quella che funziona."""

        stato, corpo, _ = self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})

        self.assertEqual(stato, 200)
        self.assertTrue(corpo["ok"])
        self.assertEqual([client.chiave for client in self.clienti], [CHIAVE_SENZA_FORMA])
        self.assertFalse(self.secrets.exists(), "la prova ha salvato la chiave")

    def test_senza_chiave_nel_corpo_la_prova_usa_quella_salvata_qui(self) -> None:
        """E dev'essere quella del percorso di **questo** servizio.

        Lasciando cercare la chiave a `ClientAI(chiave=None)` si finirebbe sul
        percorso predefinito del modulo: la prova direbbe com'e' una chiave
        diversa da quella che la pagina mostra due riquadri piu' su.
        """

        AI.salva_chiave(CHIAVE_SENZA_FORMA, self.secrets)

        stato, _, _ = self.post("/api/impostazioni/prova", {})

        self.assertEqual(stato, 200)
        self.assertEqual([client.chiave for client in self.clienti], [CHIAVE_SENZA_FORMA])

    def test_senza_nessuna_chiave_la_prova_lo_dichiara_invece_di_cercarla(self) -> None:
        """La stringa vuota dice al client «la chiave non c'e'», ed e' cosi' che
        si ottiene `SENZA_CHIAVE` invece di una ricerca a sorpresa altrove."""

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
        """`SCHEMA_NON_CONFORME` vuol dire che la chiamata e' partita: la chiave
        non e' stata respinta, ma quel modello non e' utilizzabile. Dire «tutto
        a posto» qui sarebbe la bugia piu' costosa di questa pagina."""

        self.esito = esito_finto("SCHEMA_NON_CONFORME", "Manca «azione»")

        _, corpo, _ = self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})

        self.assertFalse(corpo["ok"])
        self.assertEqual(corpo["tono"], "warning")
        self.assertIn("non è utilizzabile", corpo["messaggio"])

    # -- la trappola numero 4: la chiave dentro un'eccezione -----------------

    def test_uneccezione_che_si_porta_dietro_la_chiave_non_arriva_al_browser(self) -> None:
        """Il corpo dei 500 rimanda `str(exc)`. Un guasto qualunque — urllib, un
        `KeyError` su un dizionario di intestazioni, una libreria di terze parti —
        puo' avere la chiave dentro il messaggio."""

        self.esito = RuntimeError(f"guasto interno chiamando con {CHIAVE_SENZA_FORMA} in corso")

        stato, corpo, grezzo = self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})

        self.assertEqual(stato, 500)
        # La proprieta' che conta: la chiave non compare **da nessuna parte** nel
        # corpo, ne' nel messaggio ne' nel dettaglio tecnico.
        self.assertNotIn(CHIAVE_SENZA_FORMA, grezzo)
        self.assertIn(AI.NASCOSTO, grezzo)
        # L'errore resta visibile: nascondere la chiave non vuol dire nascondere
        # il guasto, che senza messaggio nessuno andrebbe a cercare nei registri.
        # Dal 14 agosto 2026 la frase tecnica sta nel dettaglio, e il messaggio
        # dice all'utente che cosa fare: erano due cose in un campo solo.
        dettaglio = [voce for voce in corpo["errors"] if voce.get("code") == "DETTAGLIO_TECNICO"]
        self.assertEqual(len(dettaglio), 1, corpo["errors"])
        self.assertIn("guasto interno", dettaglio[0]["message"])
        self.assertIn(AI.NASCOSTO, dettaglio[0]["message"])
        self.assertIn("Riprova", corpo["message"])

    # -- le conferme date, come file da salvare -------------------------------

    def scarica(self, percorso: str) -> tuple[int, dict[str, str], bytes]:
        """Anche le intestazioni: qui la metà del lavoro sta lì dentro."""

        richiesta = urllib.request.Request(self.base_url + percorso, method="GET")
        with urllib.request.urlopen(richiesta, timeout=15) as risposta:
            return int(risposta.status), dict(risposta.headers), risposta.read()

    def test_le_conferme_si_scaricano_come_file_col_nome_in_italiano(self) -> None:
        stato, intestazioni, corpo = self.scarica("/api/conferme/esporta")

        self.assertEqual(stato, 200)
        self.assertIn("application/json", intestazioni["Content-Type"])
        disposizione = intestazioni["Content-Disposition"]
        self.assertTrue(disposizione.startswith("attachment;"), disposizione)
        # ⚠ Il nome porta un em dash e i mesi in italiano, e le intestazioni di
        # `BaseHTTPRequestHandler` sono latin-1: senza la forma RFC 5987 che
        # `consegna` costruisce, la risposta morirebbe mentre scrive
        # l'intestazione, cioe' a corpo gia' promesso.
        self.assertIn("filename*=UTF-8''", disposizione)
        self.assertIn("Conferme", disposizione)
        documento = json.loads(corpo.decode("utf-8"))
        self.assertEqual(documento["conferme"], [])
        self.assertEqual(documento["quante"], 0)
        self.assertIn("esportate_il", documento)

    def test_quello_che_si_scarica_e_quello_che_c_e_dentro(self) -> None:
        """⚠ Le due prove di prima esercitavano solo il caso vuoto: la rotta

        poteva rispondere `[]` invece di leggere il magazzino e restavano
        verdi. Qui dentro il magazzino c'e' roba, e deve uscire — conferme
        **e** uguaglianze, che stanno nello stesso file e sono memoria «per
        sempre** tutt'e due."""

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
        """Aprire Impostazioni non deve lasciarsi dietro un `conferme.db`

        vuoto: SQLite tiene il file aperto finche' la connessione vive, e su
        Windows un file aperto blocca la cartella che lo contiene."""

        stato, _intestazioni, _corpo = self.scarica("/api/conferme/esporta")

        self.assertEqual(stato, 200)
        self.assertFalse(self.store.conferme_path.exists())

    # -- il traceback di un guasto imprevisto --------------------------------

    def stderr_di(self, chiamata) -> str:
        """Che cosa e' finito sulla finestra del programma durante la richiesta.

        `redirect_stderr` sostituisce `sys.stderr` per tutto il processo, e il
        gestore risponde su un altro filo: la richiesta pero' finisce dentro il
        `with`, perche' `urlopen` aspetta la risposta.
        """

        with io.StringIO() as buffer, contextlib.redirect_stderr(buffer):
            esito = chiamata()
            self.ultimo_esito = esito
            return buffer.getvalue()

    def test_un_guasto_imprevisto_lascia_il_suo_traceback_sulla_finestra(self) -> None:
        """In negozio, davanti a un «KeyError», non c'era modo di sapere da

        quale delle 5.200 righe venisse: restava farsi raccontare i passi al
        telefono."""

        self.esito = RuntimeError("guasto interno di prova")

        uscita = self.stderr_di(lambda: self.post("/api/impostazioni/prova", {}))

        stato, corpo, _ = self.ultimo_esito
        self.assertEqual(stato, 500)
        self.assertIn("[GUASTO]", uscita)
        self.assertIn("POST /api/impostazioni/prova", uscita)
        self.assertIn("Traceback (most recent call last)", uscita)
        self.assertIn("RuntimeError: guasto interno di prova", uscita)
        # Il traceback dice il file e la riga: e' l'unica cosa che il messaggio
        # in pagina non puo' dire.
        self.assertIn("server.py", uscita)
        # E la risposta non cambia di una virgola: il traceback e' per chi
        # guarda la finestra, non per chi usa il programma.
        self.assertIn("Riprova", corpo["message"])
        self.assertNotIn("Traceback", json.dumps(corpo))

    def test_la_chiave_non_finisce_nemmeno_nel_traceback(self) -> None:
        """La chiave puo' comparire in un traceback di `urllib`: e' esattamente

        la ragione per cui `_chiave_in_volo` esiste."""

        self.esito = RuntimeError(f"il fornitore ha risposto: Bearer {CHIAVE_CON_FORMA}")

        uscita = self.stderr_di(
            lambda: self.post("/api/impostazioni/prova", {"chiave": CHIAVE_SENZA_FORMA})
        )

        self.assertIn("[GUASTO]", uscita)
        self.assertNotIn(CHIAVE_CON_FORMA, uscita)
        self.assertNotIn(CHIAVE_SENZA_FORMA, uscita)
        self.assertIn(AI.NASCOSTO, uscita)

    def test_anche_la_chiave_gia_salvata_si_nasconde(self) -> None:
        """⚠ Il caso piu' comune di tutti, e fino al 20 agosto era scoperto.

        Chi preme «Prova la connessione» senza reincollare niente manda un
        corpo **senza** `chiave`, e il servizio ripiega su quella salvata.
        Finche' si metteva da parte solo quella del corpo, li' l'unica difesa
        restava la forma `sk-...` — che una chiave presa da
        `OPENROUTER_API_KEY`, mai controllata da nessuno, non e' tenuta ad
        avere. Questa non ce l'ha, e deve sparire lo stesso: dal corpo della
        risposta e dal traceback sulla finestra."""

        AI.salva_chiave(CHIAVE_SENZA_FORMA, self.secrets)
        self.esito = RuntimeError(f"il fornitore ha risposto 401 per {CHIAVE_SENZA_FORMA}")

        uscita = self.stderr_di(lambda: self.post("/api/impostazioni/prova", {}))

        stato, _corpo, grezzo = self.ultimo_esito
        self.assertEqual(stato, 500)
        self.assertNotIn(CHIAVE_SENZA_FORMA, grezzo)
        self.assertNotIn(CHIAVE_SENZA_FORMA, uscita)
        self.assertIn(AI.NASCOSTO, uscita)

    def test_un_browser_che_stacca_a_meta_non_e_un_guasto(self) -> None:
        """⚠ `BrokenPipeError` arriva allo stesso `except Exception` dei guasti

        veri, ma non e' un guasto del programma: e' l'utente che annulla uno
        scaricamento, o la scheda che si chiude su un file grosso. Succede per
        davvero — riprodotto su un file da 60 MB interrotto a meta' — e un
        traceback per ognuno riempirebbe di rumore proprio la finestra in cui
        il giorno del guasto vero bisogna saper guardare."""

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
        """Un 400 e' una risposta, non un guasto: il traceback e' rumore, e

        rumore a ogni campo lasciato vuoto significa che il giorno del guasto
        vero nessuno guarda piu' quella finestra."""

        uscita = self.stderr_di(lambda: self.post("/api/impostazioni/chiave", {"chiave": "   "}))

        self.assertEqual(self.ultimo_esito[0], 400)
        self.assertNotIn("[GUASTO]", uscita)

    def test_una_chiave_mai_vista_dal_gestore_la_ferma_la_forma(self) -> None:
        """La seconda rete: una chiave che arriva da dentro — dall'errore di un
        fornitore, non dal corpo della richiesta — il gestore non ce l'ha e non
        puo' sostituirla per uguaglianza. La forma la riconosce lo stesso."""

        self.esito = RuntimeError(f"il fornitore ha risposto: Authorization: Bearer {CHIAVE_CON_FORMA}")

        stato, corpo, grezzo = self.post("/api/impostazioni/prova", {})

        self.assertEqual(stato, 500)
        self.assertNotIn(CHIAVE_CON_FORMA, grezzo)
        self.assertIn(AI.NASCOSTO, grezzo)

    # -- la trappola numero 3: la chiave nella riga di richiesta -------------

    def test_la_chiave_non_finisce_nel_registro_del_servizio(self) -> None:
        """`log_message` stampa la riga di richiesta a ogni chiamata. Le rotte
        delle impostazioni che portano la chiave sono POST con corpo JSON, e
        nessuna legge la query: se un domani qualcuno ce la mettesse, non
        arriverebbe comunque nel registro."""

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

    # -- il salvataggio delle impostazioni ----------------------------------

    def test_le_impostazioni_si_salvano_e_tornano_lette_dal_file(self) -> None:
        stato, corpo, _ = self.post("/api/impostazioni", {"impostazioni": {"tetto_spesa_usd": 5.5, "parallelismo": 8}})

        self.assertEqual(stato, 200)
        self.assertEqual(corpo["impostazioni"]["tetto_spesa_usd"], 5.5)
        self.assertEqual(corpo["impostazioni"]["parallelismo"], 8)
        self.assertEqual(json.loads(self.settings.read_bytes().decode("utf-8"))["tetto_spesa_usd"], 5.5)

    def test_la_versione_del_prompt_non_si_cambia_da_questa_pagina(self) -> None:
        """E' un componente del programma, scelto misurando quanti `ALTA`
        sbagliati produce, non una preferenza."""

        stato, corpo, _ = self.post("/api/impostazioni", {"impostazioni": {"versione_prompt": "v1"}})

        self.assertEqual(stato, 400)
        self.assertIn("versione_prompt", corpo["message"])
        self.assertFalse(self.settings.exists(), "una voce rifiutata ha scritto il file lo stesso")

    def test_un_numero_con_la_virgola_e_un_errore_spiegato_non_uno_zero_zitto(self) -> None:
        """Il difetto misurato nella 5a: `3,0` azzerava il tetto di spesa e il
        messaggio ripeteva all'utente il numero che credeva di aver impostato."""

        stato, corpo, _ = self.post("/api/impostazioni", {"impostazioni": {"tetto_spesa_usd": "3,0"}})

        self.assertEqual(stato, 400)
        self.assertIn("separatore decimale", corpo["message"])
        self.assertFalse(self.settings.exists())

    # -- l'elenco dei modelli ------------------------------------------------

    def test_lelenco_dei_modelli_arriva_dal_servizio_e_dice_di_non_essere_una_prova(self) -> None:
        """La CSP e' `connect-src 'self'`: il menu' non si puo' popolare dal
        programma di navigazione. E l'elenco e' pubblico, quindi popolarsi non
        dimostra niente sulla chiave — e la risposta lo porta scritto."""

        stato, corpo, _ = self.get("/api/impostazioni/modelli")

        self.assertEqual(stato, 200)
        self.assertTrue(corpo["ok"])
        self.assertEqual([voce["id"] for voce in corpo["modelli"]], ["fornitore/modello-di-prova"])
        self.assertIn("Prova la connessione", corpo["avviso"])
        self.assertIn("anche senza chiave", corpo["avviso"])

    def test_un_elenco_irraggiungibile_non_spegne_la_pagina(self) -> None:
        """L'identificativo si scrive comunque a mano: e' l'unico modo di usare
        un modello uscito dopo l'ultimo aggiornamento dell'elenco."""

        self.modelli = OSError("la rete non risponde")

        stato, corpo, _ = self.get("/api/impostazioni/modelli")

        self.assertEqual(stato, 200)
        self.assertFalse(corpo["ok"])
        self.assertEqual(corpo["modelli"], [])
        self.assertIn("a mano", corpo["messaggio"])

    def test_la_pagina_non_allenta_la_politica_dei_contenuti(self) -> None:
        """Il menu' si popola perche' c'e' una rotta sul servizio locale, non
        perche' qualcuno ha aperto la CSP verso openrouter.ai."""

        richiesta = urllib.request.Request(self.base_url + "/api/health", method="GET")
        with urllib.request.urlopen(richiesta, timeout=15) as risposta:
            politica = risposta.headers.get("Content-Security-Policy") or ""

        self.assertIn("connect-src 'self'", politica)
        self.assertNotIn("openrouter", politica)


class ImpostazioniInterfacciaTests(unittest.TestCase):
    """Le regole della pagina che nessun test del servizio locale può difendere.

    Il valore della chiave non attraversa mai il servizio locale in uscita: la
    sola cosa che può farlo comparire dove non deve è il programma di
    navigazione, cioè app.js.
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
        # Gli altri campi della pagina si ridisegnano con
        # `value="${escapeHtml(...)}"`: per una chiave che si salva quello
        # metterebbe il valore nel sorgente della pagina. Qui il valore arriva
        # come proprieta' del nodo.
        self.assertNotIn("value=\"${escapeHtml(state.impostazioni.nuovaChiave", corpo)
        self.assertNotIn("state.impostazioni.nuovaChiave}", corpo)
        self.assertIn("data-chiave-openrouter", corpo)

    def test_il_valore_incollato_si_rimette_come_proprieta_del_nodo(self) -> None:
        corpo = self.body("render")
        self.assertIn("[data-chiave-openrouter]", corpo)
        self.assertIn("campoChiave.value = state.impostazioni.nuovaChiave", corpo)

    def test_la_chiave_incollata_si_azzera_in_tutti_i_rami_dopo_linvio(self) -> None:
        corpo = self.body("saveApiKey")
        # Nel `finally`, cioe' anche quando il salvataggio fallisce.
        coda = corpo.split("finally")[-1]
        self.assertIn('state.impostazioni.nuovaChiave = ""', coda)
        # E uscendo dalla pagina, salvata o no.
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
        # Nessun indirizzo delle impostazioni porta parametri.
        indirizzi = self.app_js.split("const API = {")[1].split("};")[0]
        self.assertIn('"/api/impostazioni"', indirizzi)
        self.assertNotIn("impostazioni?", indirizzi)

    def test_il_modello_si_puo_sempre_scrivere_a_mano(self) -> None:
        """Gli elenchi invecchiano: un modello uscito ieri non c'e' dentro.

        Il campo dev'essere davvero scrivibile, e quello che ci si scrive deve
        arrivare fino al salvataggio. Un `readonly` sfuggito lascerebbe la
        pagina identica a vedersi e il modello nuovo impossibile da mettere.
        """

        corpo = self.body("renderSettingsModelPanel")
        campo = next(riga for riga in corpo.splitlines() if 'data-impostazione="model"' in riga or 'id="impostazioni-model"' in riga)
        blocco = corpo.split('id="impostazioni-model"')[1].split(">")[0]
        self.assertIn('type="text"', blocco + campo)
        self.assertNotIn("readonly", blocco)
        self.assertNotIn("disabled", blocco)
        # Quello che si scrive finisce nello stato...
        ascolto = self.app_js.split("if (target.dataset.impostazione) {")[1].split("}")[0]
        self.assertIn("state.impostazioni.valori[target.dataset.impostazione] = target.value", ascolto)
        # ...e dallo stato parte al salvataggio, senza passare dall'elenco.
        self.assertIn("model: modelloConfigurato()", self.body("saveSettings"))
        self.assertIn('state.impostazioni.valori?.model || ""', self.body("modelloConfigurato"))
        # Il menu' riempie il campo di testo, che resta il valore vero.
        scelta = self.app_js.split("if (target.dataset.elencoModelli !== undefined)")[1].split("return;")[0]
        self.assertIn("state.impostazioni.valori.model = String(target.value)", scelta)

    def test_la_pagina_non_racconta_che_lelenco_dimostri_la_chiave(self) -> None:
        corpo = self.body("renderSettingsTestPanel")
        self.assertIn("l’unica cosa che dimostra", corpo)
        # E l'avviso che accompagna l'elenco arriva dal servizio locale, dove sta
        # scritto una volta sola.
        self.assertIn("elenco.avviso", self.body("renderSettingsModelPanel"))

    def test_le_impostazioni_non_sono_un_quarto_passo_del_flusso(self) -> None:
        """`currentStep` vale 1..3 qui e sul servizio locale: un quarto valore
        verrebbe tagliato al salvataggio e la pagina tornerebbe da sola alla
        terza schermata."""

        self.assertIn("state.impostazioni.aperta", self.body("render"))
        self.assertNotIn("state.currentStep = 4", self.app_js)
        # ⚠ La barra dei passi mostra una quarta voce mentre le impostazioni
        # sono aperte, ma NON e' un quarto passo: non porta `data-step`, non
        # tocca `currentStep`, e sparisce appena si chiudono. Quello che questa
        # prova protegge e' che `STEPS` resti di tre.
        self.assertIn('{ id: 3, label: "Riepilogo e compilazione" }', self.app_js)
        self.assertNotIn("{ id: 4,", self.app_js)
        # E la quarta voce si disegna a mano dentro `renderStepper`, senza
        # passare da `STEPS`.
        barra = self.body("renderStepper")
        self.assertIn('data-action="chiudi-impostazioni"', barra)
        self.assertNotIn("data-step", barra.split('data-action="chiudi-impostazioni"', 1)[1])

    def test_la_pagina_si_raggiunge_dalla_prima_schermata(self) -> None:
        self.assertIn('data-action="apri-impostazioni"', self.body("renderUploadStep"))

    def test_i_numeri_partono_come_numeri_e_il_resto_lo_spiega_il_servizio(self) -> None:
        """Il servizio locale rifiuta la stringa «3» per un tetto che vale 3.0:
        e' giusto che sia severo, ed e' qui che si converte prima di spedire."""

        corpo = self.body("settingsNumber")
        self.assertIn("Number.isFinite(numero) ? numero : testo", corpo)
        self.assertIn("settingsNumber(", self.body("saveSettings"))


# ---------------------------------------------------------------------------
# Fase 6d: la consegna — una cartella datata per ogni compilazione
# ---------------------------------------------------------------------------

CONSEGNA = SERVER.consegna


def listino_finto(percorso: Path, foglio: str = "LISTINO") -> Path:
    """Un .xlsx vero, non un file finto: le prove lo riaprono con openpyxl."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = foglio
    sheet.append(["EAN", "DESCRIZIONE", "ORDINE"])
    sheet.append(["8000000000010", "PRODOTTO STANDARD", None])
    workbook.save(percorso)
    workbook.close()
    return percorso


class ScrittoreFinto:
    """Il writer Node sostituito da qualcosa che scrive dove gli si dice.

    Il writer vero ha una catena di verifiche tutta sua, gia' provata altrove e
    intoccabile, e su molte macchine Node non c'e' nemmeno. Qui interessa
    soltanto che *qualcuno* metta dei documenti nella cartella della
    compilazione: quello che si prova e' la rinomina ai nomi leggibili, l'audit
    costruito scandendo il disco e le rotte che li consegnano.

    ⚠ La copia e' una **copia del listino di partenza**, non un documento nuovo
    che gli somiglia.  Dal 12 agosto 2026 la compilazione riapre ogni copia e la
    confronta cella per cella con il suo listino (`app/copia_fedele.py`): uno
    scrittore finto che consegnasse un documento senza rapporto con l'originale
    verrebbe fermato dalla guardia, e queste prove non parlerebbero piu' di
    quello di cui vogliono parlare.

    ⚠ E **le quantita' del piano le scrive davvero**, dal 14 agosto 2026.  Prima
    faceva soltanto `shutil.copy2`, cioe' consegnava una copia identica
    all'originale, con la colonna d'ordine vuota: tutte le prove di consegna —
    rinomina, audit, zip — giravano su ordini vuoti senza accorgersene, perche'
    il controllo di fedelta' guardava solo le celle *diverse* e una quantita'
    mai scritta non e' una differenza.  Chiusa quella falla, uno scrittore che
    non scrive niente e' esattamente cio' che la guardia deve fermare.
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
        # Le frasi che il writer vero dichiara nel suo riepilogo (righe scritte
        # senza aver potuto verificare che fossero quelle giuste): qui si
        # possono iniettare, perche' devono arrivare fino alla pagina.
        self.avvisi = list(avvisi)
        self.destinazioni: list[Path] = []

    @staticmethod
    def _scrivi_le_quantita(piano: dict[str, Any], fornitore: str, copia: Path) -> None:
        """Mette i colli del piano nella colonna d'ordine, come fa il writer vero.

        La colonna la si chiede alla stessa regola che usera' il controllo di
        fedelta' (`default_write_rule`): scriverla a mano qui vorrebbe dire
        avere due verita' e scoprirlo il giorno che una delle due cambia.
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
            # Un file che nessuno aspettava: l'audit lo deve dire lo stesso.
            (destinazione / self.sorpresa).write_bytes(b"nessuno mi aspettava")
        return generati, prodotte, list(self.avvisi)


class ConsegnaBase(unittest.TestCase):
    """Le fondamenta comuni alle prove della 6d: uno store con due fornitori."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        # `.resolve()` non e' un vezzo: la cartella temporanea arriva col
        # percorso NON risolto, e il codice sotto prova lo risolve. Su macOS
        # `/var` e' un collegamento a `/private/var`, su Windows TEMP esce in
        # forma corta (`RUNNER~1`): in tutti e due i casi i confronti fra
        # percorsi fallirebbero, e con loro venticinque prove che non hanno
        # niente che non va.
        self.root = Path(temporanea.name).resolve()
        # `dati` esiste perche' la radice degli ordini deve avere due livelli
        # sopra di se': senza, `/ordini/../../secrets.json` uscirebbe dalla
        # cartella temporanea e la prova del traversal non potrebbe creare
        # davvero il file bersaglio.
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
            # `prepare_writer_config` scrive sempre la run di appartenenza, e la
            # compilazione pretende che sia quella del confronto attivo: una
            # configurazione che non la dichiara e' una configurazione vecchia.
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
        """Una compilazione che produce davvero le copie, senza chiamare Node."""

        scrittore = ScrittoreFinto({nome: self.listini[nome] for nome in fornitori}, sorpresa=sorpresa)
        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer", scrittore):
            esito = self.store.compile(self.snapshot(quantita, fornitori[0]))
        self.assertEqual(scrittore.destinazioni, [self.orders_dir / esito["cartella"]])
        return esito

    def compila_senza_listini(self, quantita: int = 2) -> dict[str, Any]:
        """Il writer non è configurato: resta il solo piano, e va bene così."""

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
        """Un `datetime` che sta sempre alle 14:35 del 12 agosto 2026.

        Serve al caso «due compilazioni nello stesso minuto», che dal vivo
        capita e che con l'orologio libero non si riesce a provocare a comando.
        """

        class Orologio(SERVER.datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:
                return SERVER.datetime(2026, 8, 12, 14, minuto, 7, 421000, tzinfo=tz)

        return mock.patch.object(SERVER, "datetime", Orologio)


class LaCompilazioneRiuscitaNonHaErroriPerFornitoreTests(ConsegnaBase):
    """Il contratto che la pagina traduceva senza che nessuno lo scrivesse.

    Fino al 17 agosto 2026 `app/static/app.js` aveva una funzione che
    traduceva, per ogni fornitore, un `errors: [{code:
    "FORNITORE_SENZA_COPIA", …}]` dentro la risposta di una compilazione
    **riuscita**. Quel campo non esiste: `run_writer` si ferma se una copia
    attesa non c'è, quindi o la compilazione produce tutto, o non risponde
    `ok: True` affatto. Era codice morto tenuto in vita dal suo stesso test.

    Questo test è la difesa dalla parte giusta: inchioda la forma della
    risposta. Il giorno in cui una compilazione dovesse davvero riuscire a
    metà, questo test diventa rosso — ed è il momento in cui va deciso, e
    scritto, che cosa la pagina mostra.
    """

    def test_la_risposta_di_una_compilazione_riuscita_non_porta_errori(self) -> None:
        esito = self.compila_con_listini(2)

        self.assertTrue(esito["ok"])
        self.assertNotIn("errors", esito)
        # Gli avvisi hanno i loro tre campi, e sono quelli che la pagina legge.
        for campo in ("writerIssues", "deliveryIssues", "historyIssues"):
            self.assertIn(campo, esito)

    def test_una_compilazione_senza_nessuna_copia_lo_mette_fra_gli_avvisi(self) -> None:
        """⚠ Il difetto che il contratto morto nascondeva.

        `run_writer` che fallisce non ferma la compilazione — il piano resta
        consegnabile, ed è voluto. Ma il motivo finiva **solo** dentro
        `message`, e `message` da solo non fa niente: la pagina decide se la
        compilazione è riuscita a metà guardando `writerIssues`,
        `deliveryIssues` e `historyIssues`. Con tutti e tre vuoti mostrava il
        riquadro verde e il pulsante primario per una compilazione che non ha
        prodotto **nessuna** copia.

        Ora il motivo sta nel campo che la pagina legge, com'è già per il ramo
        gemello (configurazione di scrittura incompleta).
        """

        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer",
                                  side_effect=ValueError("Il writer non ha creato la copia prevista per LARICE")):
            esito = self.store.compile(self.snapshot(2, "larice"))

        self.assertTrue(esito["ok"])
        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertEqual(len(esito["writerIssues"]), 1)
        self.assertIn("LARICE", esito["writerIssues"][0])
        # E resta detto anche nella frase, come nel ramo della configurazione.
        self.assertIn("LARICE", esito["message"])
        self.assertNotIn("errors", esito)


class ConsegnaCartellaDatataTests(ConsegnaBase):
    """La cartella datata, la rinomina che non sovrascrive e l'audit del disco."""

    def test_due_compilazioni_di_fila_non_si_sovrascrivono(self) -> None:
        """La prova di accettazione n. 1: compilare due volte, due cartelle.

        E' il difetto che la 6d chiude: fino a ieri la seconda compilazione
        scriveva sui listini pronti della prima, con lo stesso nome e nella
        stessa cartella, senza dire niente a nessuno.
        """

        prima = self.compila_con_listini(2)
        seconda = self.compila_con_listini(3)

        self.assertNotEqual(prima["cartella"], seconda["cartella"])
        self.assertEqual(len(self.cartelle()), 2)

        for esito, quantita in ((prima, 2), (seconda, 3)):
            cartella = self.cartella_di(esito)
            piano = json.loads((cartella / "final_order_plan.json").read_text(encoding="utf-8"))
            # Il piano della prima dice ancora 2: se fosse stato sovrascritto
            # direbbe 3, ed e' esattamente il danno da cui si parte.
            self.assertEqual(piano["orders"][0]["quantity"], quantita)
            copia = cartella / self.nome_del_listino(esito)
            self.assertTrue(copia.is_file())
            # «Ancora apribili»: non basta che il file ci sia, deve aprirsi.
            from openpyxl import load_workbook

            libro = load_workbook(copia, read_only=True)
            try:
                # La copia e' il listino di partenza: stesso foglio, stesso nome.
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
        # E gli URL della risposta portano alla cartella, non piu' a /outputs/.
        piano = next(item for item in esito["outputs"] if item["name"] == "final_order_plan.json")
        self.assertEqual(piano["tipo"], "piano")
        self.assertEqual(piano["url"], f"/ordini/{esito['cartella']}/final_order_plan.json")

    def test_le_copie_prendono_il_nome_leggibile(self) -> None:
        with self.orologio_fermo():
            esito = self.compila_con_listini(2, ("larice",))

        cartella = self.cartella_di(esito)
        self.assertTrue((cartella / "Ordine LARICE — 12 agosto 2026.xlsx").is_file())
        # Il nome tecnico del writer non deve restare li' accanto: sarebbe una
        # seconda copia dello stesso listino, e finirebbe nell'audit e nello zip.
        self.assertEqual(list(cartella.glob("ORDINE_*.xlsx")), [])

    def test_la_rinomina_non_sovrascrive_mai_un_documento_gia_presente(self) -> None:
        """La difesa dell'`os.rename`, provata dove si puo' provocare la collisione.

        Spegnendo il controllo, su Windows arriva un `FileExistsError` e su
        Linux il documento di prima sparisce in silenzio: due modi diversi di
        far fallire questa prova, che e' il punto.
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
        # La copia resta consegnabile con il nome che ha: un nome brutto non e'
        # un motivo per buttare via un ordine giusto.
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
        # L'ora locale con lo scarto, non UTC: si legge accanto al nome della
        # cartella, che e' ora locale, e due fusi nello stesso documento sono
        # un errore che aspetta.
        self.assertRegex(audit["creato_il"], r"[+-]\d{2}:\d{2}$")

        # Il punto della prova: l'elenco dell'audit e' quello che c'e' davvero
        # sul disco. `compilazione.json` non c'e' perche' si scrive per ultimo,
        # quando la scansione e' gia' stata fatta.
        sul_disco = set(os.listdir(cartella)) - {"compilazione.json"}
        self.assertEqual({item["nome"] for item in audit["file"]}, sul_disco)

        per_nome = {item["nome"]: item for item in audit["file"]}
        listino = per_nome[self.nome_del_listino(esito)]
        self.assertEqual(listino["fornitore"], "larice")
        self.assertEqual(listino["origine"], str(self.listini["larice"]))
        self.assertEqual(listino["byte"], (cartella / listino["nome"]).stat().st_size)
        self.assertEqual(per_nome["final_order_plan.json"]["tipo"], "piano")
        # Il file che nessuno aspettava c'e', e si dichiara per quello che e'.
        self.assertEqual(per_nome["sorpresa.xlsx"]["tipo"], "altro")
        self.assertNotIn("origine", per_nome["sorpresa.xlsx"])

    def test_l_audit_c_e_anche_quando_il_writer_non_ha_fatto_niente(self) -> None:
        esito = self.compila_senza_listini()
        audit = self.audit_di(esito)

        self.assertEqual(audit["stato"], "PLAN_READY")
        self.assertEqual([item["nome"] for item in audit["file"]], ["final_order_plan.json"])
        # Senza listini non c'e' niente da consegnare: il pulsante non deve
        # comparire, quindi l'URL dello zip e' nullo e non un indirizzo che
        # risponderebbe 404.
        self.assertIsNone(esito["zipUrl"])
        self.assertEqual(esito["zipNome"], CONSEGNA.nome_zip(esito["cartella"]))

    def test_il_writer_riceve_la_cartella_della_compilazione(self) -> None:
        """`--output-dir` e' la cartella datata: il writer scrive li' e basta.

        Se puntasse ancora a `outputs`, fra la scrittura e lo spostamento ci
        sarebbe un istante in cui la compilazione precedente e' gia' perduta.
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

        # Questo writer finto non dichiara nessun riepilogo: il nome della copia
        # resta quello che il servizio ricalcola, e non c'e' niente da dire.
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
        """`os.rename` fallisce e la compilazione deve reggere lo stesso.

        Su Windows basta che qualcuno tenga aperto il file — l'antivirus che lo
        scansiona, OneDrive che lo sincronizza, l'utente che l'ha aperto in
        Excel — perche' `os.rename` alzi `PermissionError`. Prima questo
        diventava un 500: la pagina diceva «Compilazione non riuscita», la
        cartella restava senza audit, e i listini erano li', giusti e
        scaricabili. Adesso il documento tiene il nome brutto e il programma lo
        dice.
        """

        def rename_che_fallisce(sorgente: Any, destinazione: Any) -> None:
            raise PermissionError(32, "Impossibile accedere al file. Il file è in uso")

        with mock.patch.object(SERVER.os, "rename", rename_che_fallisce):
            esito = self.compila_con_listini(2)

        self.assertTrue(esito["ok"])
        self.assertEqual(esito["status"], "FILES_READY")
        self.assertIn("è rimasto con questo nome", esito["message"])
        self.assertIn("Il file è in uso", esito["message"])

        # Il documento c'e', si sa di chi e', ed e' consegnabile: l'unica cosa
        # che manca e' il nome leggibile.
        audit = self.audit_di(esito)
        listini = [riga for riga in audit["file"] if riga["tipo"] == "listino"]
        self.assertEqual(len(listini), 1)
        self.assertEqual(listini[0]["nome"], "ORDINE_LARICE_listino_larice.xlsx")
        self.assertEqual(listini[0]["fornitore"], "larice")
        self.assertEqual(listini[0]["origine"], str(self.listini["larice"].resolve()))
        self.assertTrue(any("è rimasto con questo nome" in avviso for avviso in audit["avvisi"]))
        self.assertEqual(esito["zipUrl"], f"/ordini/{esito['cartella']}/zip")



class ElencoDeiProdottiDaReperireTests(ConsegnaBase):
    """La compilazione produce anche l'elenco di quello che non si può ordinare.

    Decisione di Daniele del 16 agosto 2026.  Il file nasce **solo** se c'è
    almeno una riga, sta nella stessa cartella datata dei listini compilati, ed
    esce sia da solo sia dentro lo zip: chi scarica lo zip sta preparando la
    settimana, e quell'elenco è parte del lavoro di quella settimana.
    """

    def con_un_introvabile(self, quantita: int = 4) -> dict[str, Any]:
        """Aggiunge al confronto un prodotto che nessun fornitore porta."""

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
        # Il servizio vero: le prove dello zip e del download singolo devono
        # passare dalle rotte, non da un filtro riscritto nel test.

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
        """Non c'è nessuna riga di listino su cui scrivergli una quantità."""

        esito = self.compila(self.con_un_introvabile())

        piano = json.loads((self.cartella_di(esito) / "final_order_plan.json").read_text(encoding="utf-8"))
        self.assertEqual([voce["product_id"] for voce in piano["orders"]], ["product-standard"])

    def test_senza_prodotti_da_reperire_il_file_non_nasce(self) -> None:
        """Nessun riquadro, nessun messaggio, nessun file: è la settimana normale."""

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
        """Il caso che la decisione prevede espressamente."""

        istantanea = self.con_un_introvabile()
        istantanea["products"][0]["quantity"] = 0

        esito = self.compila(istantanea)

        self.assertTrue((self.cartella_di(esito) / self.nome_dell_elenco(esito)).is_file())
        piano = json.loads((self.cartella_di(esito) / "final_order_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(piano["orders"], [])

    def test_soli_introvabili_non_fanno_scattare_le_soglie_dei_fornitori(self) -> None:
        """Un percorso che prima non si poteva raggiungere: `ORDINE_VUOTO` fermava
        tutto prima.  Ora si arriva al controllo delle soglie con i totali di
        **tutti** i fornitori a zero, e nessuno di loro deve risultare sotto
        soglia — non si sta ordinando niente da nessuno.
        """

        istantanea = self.con_un_introvabile()
        istantanea["products"][0]["quantity"] = 0
        istantanea["acceptBelowThreshold"] = False

        esito = self.compila(istantanea)

        self.assertTrue((self.cartella_di(esito) / self.nome_dell_elenco(esito)).is_file())

    def test_un_ordine_davvero_vuoto_resta_rifiutato(self) -> None:
        """L'altro lato: senza niente da ordinare e niente da reperire non si compila."""

        with self.assertRaises(SnapshotError) as errore:
            self.compila(self.snapshot(0))

        self.assertEqual({voce.get("code") for voce in errore.exception.errors}, {"ORDINE_VUOTO"})

    def test_lo_zip_della_rotta_porta_dentro_l_elenco(self) -> None:
        """⚠ Si passa dalla **rotta**, non dal filtro riscritto nel test.

        La prima versione di questa prova ricostruiva a mano il filtro dei tipi
        e chiamava `zip_in_memoria`: restava verde anche riportando `servi_zip`
        ai soli listini, cioè misurava se stessa.  L'ha trovata una mutazione
        rimasta verde il 17 agosto 2026.
        """

        esito = self.compila(self.con_un_introvabile())
        atteso = self.nome_dell_elenco(esito)

        stato, corpo, intestazioni = self.chiedi(esito["zipUrl"])

        self.assertEqual(stato, 200, corpo[:200])
        self.assertEqual(intestazioni["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(corpo)) as archivio:
            self.assertIn(atteso, archivio.namelist())
            # Non il nome soltanto: il contenuto è quello del file in cartella.
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
        """Senza questo, l'unica uscita di quella settimana non avrebbe un link."""

        istantanea = self.con_un_introvabile()
        istantanea["products"][0]["quantity"] = 0

        voce = consegna.voce(self.cartella_di(self.compila(istantanea)))

        self.assertIsNotNone(voce["zipUrl"])

    def test_l_elenco_non_si_conta_fra_i_listini(self) -> None:
        # La pagina scrive «N listini» leggendo questo numero, e un elenco di
        # prodotti che nessuno ha non è un listino: contarlo lì direbbe a chi
        # guarda che ha un documento in più da spedire.
        voce = consegna.voce(self.cartella_di(self.compila(self.con_un_introvabile())))

        self.assertEqual(voce["listini"], 0)
        self.assertIsNotNone(voce["zipUrl"])
        self.assertIn("da_reperire", [riga["tipo"] for riga in voce["file"]])


class ConsegnaHttpTests(ConsegnaBase):
    """Le rotte della consegna provate **via HTTP**, non chiamando `do_GET`.

    Il punto delicato e' la decodifica del percorso: `route = unquote(...)`
    trasforma `..%2f..%2fsecrets.json` in `../../secrets.json` prima che le
    difese lo vedano. Un test che chiamasse il gestore a mano salterebbe
    proprio il passaggio che si vuole provare.
    """

    PAROLA_SEGRETA = "PAROLA-SEGRETA-DA-NON-CONSEGNARE"

    def setUp(self) -> None:
        super().setUp()
        # I bersagli del traversal esistono **davvero**: con un file che non c'e'
        # la prova passerebbe anche a difesa spenta, ed e' l'errore che questo
        # progetto ha gia' fatto una volta.
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
        """Una GET con il percorso **cosi' com'e**.

        Si usa `http.client` e non `urllib.request` perche' urllib normalizza i
        segmenti `..` prima di spedire: la richiesta che il servizio riceverebbe
        non sarebbe piu' quella che si voleva provare.
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
        # Il nome del fornitore arriva da `supplier_label` di server.py, non
        # dall'id maiuscolo di ripiego di consegna.py.
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
        """Sarebbe rosso senza la correzione del §0bis del contratto.

        `attachment; filename="Ordine LARICE — 12 agosto 2026.xlsx"` non si
        codifica in latin-1, e le intestazioni di BaseHTTPRequestHandler sono
        latin-1: la rotta moriva mentre scriveva l'intestazione, a corpo gia'
        promesso.
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
        # Il ripiego fra virgolette e' ASCII, e l'intestazione intera si scrive
        # in latin-1: e' la prova che la risposta poteva partire.
        self.assertNotIn("—", disposizione)
        disposizione.encode("latin-1")

    def test_anche_la_vecchia_rotta_outputs_regge_un_nome_non_ascii(self) -> None:
        """`serve_file` è condivisa: la correzione vale per tutte le rotte."""

        nome = "Richiesta — prova.json"
        (self.output_dir / nome).write_bytes(b'{"ok": true}')

        stato, corpo, intestazioni = self.chiedi(f"/outputs/{urllib.parse.quote(nome, safe='')}")

        self.assertEqual(stato, 200)
        self.assertEqual(corpo, b'{"ok": true}')
        self.assertIn("filename*=UTF-8''", intestazioni["Content-Disposition"])

    def test_un_documento_vero_di_outputs_si_scarica_ancora_con_la_difesa_nuova(self) -> None:
        """La difesa nuova (`consegna.file_sicuro`) non deve fermare un file vero.

        Rilievo [39]: prima della correzione `/outputs/<nome>` usava solo
        `.name` per difendersi. Questa prova copre il verso «giusto»: un file
        che esiste davvero nella cartella deve continuare a scaricarsi.
        """

        nome = "listino_pronto.xlsx"
        contenuto = b"contenuto vero del documento"
        (self.output_dir / nome).write_bytes(contenuto)

        stato, corpo, _ = self.chiedi(f"/outputs/{urllib.parse.quote(nome, safe='')}")

        self.assertEqual(stato, 200)
        self.assertEqual(corpo, contenuto)

    def test_un_collegamento_simbolico_dentro_outputs_non_esce_dalla_cartella(self) -> None:
        """La fuga verificata per `/outputs/` e' per collegamento simbolico, non per traversal.

        Rilievo [39]: `.name` da solo scarta gia' i segmenti `../`, quindi il
        traversal classico non passava nemmeno prima. Quello che mancava era
        il controllo di contenimento dopo `resolve()` che `consegna.file_sicuro`
        applica gia' alla rotta gemella `/ordini/<cartella>/<nome>`: senza,
        un collegamento simbolico piazzato dentro `outputs/` viene seguito
        fuori dalla cartella.
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
        # Precondizione: i file bersaglio ci sono e si leggono davvero.
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
                # Il messaggio non ripete il percorso chiesto: direbbe a chi
                # prova che cosa ha provato.
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
            # Dentro non ci sono ne' il piano ne' il file che nessuno aspettava.
            for nome in archivio.namelist():
                self.assertEqual(
                    archivio.read(nome),
                    (self.cartella_di(esito) / nome).read_bytes(),
                )

    def test_un_listino_allegato_a_una_mail_non_ferma_la_consegna_degli_altri(self) -> None:
        """Il gesto piu' normale che ci sia, e prima rompeva tutto.

        L'utente allega un listino alla mail per BETULLA e lo sposta sul desktop.
        Con l'elenco costruito dall'audit, `zip_in_memoria` chiedeva anche il
        documento sparito, il primo che mancava alzava, e la rotta rispondeva
        «In questa compilazione non ci sono listini da scaricare» — mentre
        quello di LARICE era li' e doveva ancora essere spedito. L'utente ne
        deduceva che la compilazione fosse vuota e rifaceva il lavoro.
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

        # E la pagina non deve dire che i listini sono ancora due, ne' offrire
        # un collegamento a un documento che non c'e' piu'.
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
    """Le due rotte del ricalcolo, provate dalla parte della rete.

    Il punto non e' che rispondano: e' che la seconda richiesta di avvio,
    mentre la prima lavora, riceva un 409 invece di far partire una seconda
    catena sugli stessi file.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        review_path = self.root / "review_data.json"
        review_path.write_text(json.dumps(synthetic_review()), encoding="utf-8")
        (self.root / "uploads").mkdir()
        # Un documento ci vuole: senza, la catena si ferma al primo passo con
        # «nessun documento» e il lucchetto torna libero prima che la seconda
        # richiesta arrivi — cioe' la prova passerebbe per il motivo sbagliato.
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
        """Smontaggio del 12 agosto 2026: la suite deve inchiodarlo.

        Senza questa prova le tre rotte potrebbero tornare — con lo scraper,
        le credenziali e il carrello dietro — e nessun test se ne accorgerebbe.
        """

        codice_stato, _ = self.chiedi("/api/noce/status")
        codice_avvio, _ = self.chiedi("/api/noce/start", b"{}")
        codice_carrello, _ = self.chiedi("/api/noce/carrello/prepara", b"{}")
        self.assertEqual((codice_stato, codice_avvio, codice_carrello), (404, 404, 404))

    def test_lo_stato_finale_resta_ripescabile_a_run_conclusa(self) -> None:
        """La rotta deve continuare a dire com'e' finita, anche a run finita.

        E' la meta' lato servizio del difetto «chi ricarica la pagina non vede
        piu' ne' fermate ne' avvisi»: se la rotta smettesse di raccontare
        l'ultimo esito, nessuna correzione della pagina potrebbe rimediarci.
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

        # E anche dopo un riavvio del servizio: lo stato sta su disco, non solo
        # nella memoria del processo che ha fatto girare la catena.
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
        """`ReviewStore` possiede il lucchetto e la pipeline deve ricevere LUI.

        Se `PipelineJobManager` si facesse un lucchetto suo, il giorno in cui
        un secondo lavoro tornasse a sostituire `review_data.json` i due non si
        escluderebbero piu' — e nessun test comportamentale distinguerebbe i
        due lucchetti finche' i lavori restano uno.
        """

        self.assertIs(self.store.pipeline_jobs.lucchetto_lavori, self.store.lucchetto_lavori)


class LaVersionePubblicataInApiHealthTests(unittest.TestCase):
    """`/api/health` e' la rotta con cui il lanciatore decide se riusare il
    servizio: deve portare `versionePubblicata` anche quando vale `null`,
    perche' la pagina distingue «non lo so» da «non c'e' il campo».
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
    """Un `.xls` verosimile: EAN in B, prezzo in H, colonna d'ordine RK in I.

    Lo costruisce il banco di `test_xls_reader`, cioe' contenitore OLE2 vero e
    record BIFF8 veri: qui non si finge il formato, si finge solo il contenuto.
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
    """Come si compila lo dice la regola, non il nome del fornitore.

    Fino al 17 agosto 2026 la scelta fra writer Node e patch in posizione era
    `supplier == "noce"`, scritta in due punti di `app/server.py`, mentre il
    registro la dichiara da sempre (`order_write.mode`) e `launcher.source_rule`
    la porta nella regola come `compilazione`. Costava in tutte e due le
    direzioni, e questa classe prova quella che costa di più: un fornitore
    **nuovo** che manda un `.xls` — il caso che il programma promette di saper
    imparare dalla pagina — finiva nel ramo del writer Node.
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
        """Il caso che prima era irraggiungibile: nessun `.xls` che non fosse Noce."""

        esito = self.store.compile(self.snapshot(3, "nuovo_fornitore"))

        self.assertEqual(esito["status"], "FILES_READY", esito["message"])
        listini = [voce["name"] for voce in esito["outputs"] if voce["name"].casefold().endswith(".xls")]
        self.assertEqual(len(listini), 1, [voce["name"] for voce in esito["outputs"]])
        # E la copia è il **loro** documento, non una conversione: stessa
        # estensione, e la quantità scritta nella colonna dichiarata.
        copia = next(
            percorso for percorso in self.cartella_di(esito).iterdir()
            if percorso.suffix.casefold() == ".xls"
        )
        from xls_reader import read_workbook

        foglio = read_workbook(copia)[0]
        self.assertEqual(foglio.rows[19][8][0], 3)

    def test_senza_la_dichiarazione_lo_stesso_xls_accusa_la_configurazione(self) -> None:
        """⚠ L'altra metà del difetto, e la parte che non va persa.

        Senza `compilazione` nella regola, quel `.xls` passa dal writer Node —
        che è la strada normale — e lì non si può scrivere. Il messaggio deve
        mandare a correggere **la configurazione**: prima diceva «Il listino
        configurato per … non è disponibile in formato XLSX», cioè accusava il
        documento del fornitore, che è l'unica cosa che l'utente non può
        cambiare.

        Dal 4 settembre 2026 dice anche l'altra metà, che è quella che l'utente
        può mettere in pratica da solo: salvare il documento in `.xlsx` con
        Excel. Prima mandava alla pagina «Importa i dati», dove non c'è nessun
        comando che converta un formato, e da lì si tornava indietro uguali.
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
        # E il rimedio che l'utente può mettere in pratica da solo.
        self.assertIn("salvalo come «Cartella di lavoro di Excel (.xlsx)»", esito["message"])
        self.assertNotIn("va riconfigurato dalla pagina", esito["message"])


class CompilazioneNoceTests(ConsegnaBase):
    """La compilazione Noce: il loro documento, con la sola colonna d'ordine.

    Non si finge il writer: qui gira `app/xls_writer.py` per davvero, su un
    `.xls` vero. Quello che si prova e' che la copia esca dalla compilazione con
    il nome leggibile e l'estensione giusta, e che le due guardie — EAN della
    riga e colonna ancora tutta a lunghezza fissa — fermino la compilazione
    invece di consegnare un ordine sbagliato.
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
            # ⚠ È la regola a dire come si compila, non il nome del fornitore:
            # `launcher.regola_noce` scrive questa chiave da sempre, e dal
            # 17 agosto 2026 è quella che il servizio guarda. Un banco che non
            # la dichiarava provava un percorso che la configurazione vera non
            # produce mai.
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
        """La riga 2600 che era olio Carapelli: questo è il controllo che l'avrebbe visto."""

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
        """Basta che una settimana Noce ci metta una formula."""

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
        """Un piano d'ordine con dentro una riga Noce.

        E' un piano di **compilazione** (Fase 6e), non ha niente a che vedere
        con il vecchio percorso sul sito: serve a far arrivare
        `writer_configuration_issues` fino ai controlli sul listino Noce.
        Il `run_id` c'e' perche' il programma vero lo scrive in ogni piano
        (dalla run del confronto che l'ha prodotto): senza, la guardia della
        run fermerebbe tutto prima dei controlli che queste prove misurano.
        """
        return {"run_id": "run-sintetica", "orders": [{
            "supplier": "noce",
            "supplier_source_row": 20,
            "supplier_ean": "8000000000010",
            "quantity": 3,
        }]}

    def test_il_controllo_preventivo_vede_l_impronta_diversa(self) -> None:
        """La difesa sull'impronta sta in due punti, e ognuno ha la sua prova.

        Il controllo prima di scrivere e quello dentro la compilazione dicono la
        stessa frase: senza una prova per ciascuno, spegnerne uno lascerebbe la
        suite verde e nessuno saprebbe che una delle due porte e' aperta.
        """

        self.scrivi_configurazione(source_sha256="0" * 64)
        problemi = self.store.writer_configuration_issues(self.piano_con_riga_noce())
        self.assertTrue(any("cambiato dopo la verifica" in voce for voce in problemi), problemi)

    def test_il_controllo_preventivo_passa_su_un_listino_intatto(self) -> None:
        self.assertEqual(self.store.writer_configuration_issues(self.piano_con_riga_noce()), [])

    def configurazione_larice(self, impronta: str) -> dict[str, Any]:
        """Una configurazione con il solo LARICE, e l'impronta che le si passa."""

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
        """«Ho eliminato il listino sbagliato, ho ricaricato quello giusto, ho compilato.»

        Con lo stesso nome di file il percorso non cambia, e senza ricalcolo la
        run e' la stessa: la guardia sulla run non vede niente.  L'unica difesa
        e' l'impronta — che `source_rule` scrive da sempre per **tutti** i
        fornitori, ma che fino al 14 agosto 2026 si controllava soltanto per
        CIPRESSO, dietro un `if supplier != "cipresso": continue`.  Le quantita'
        finivano nelle righe del listino nuovo scelte con i numeri di riga del
        confronto vecchio, e la copia usciva consegnabile.
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
        """L'altro ramo: la severita' nuova non deve fermare una compilazione buona.

        Si guarda **solo** l'impronta: questa configurazione minima non dichiara
        Node ne' lo strumento di scrittura, quindi altri avvisi ci sono e sono
        giusti.  Asserire «nessun avviso» qui misurerebbe quelli invece di
        quello che la prova vuole misurare.
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
        """NOCE non passa da Node, ma prende il listino dalla stessa configurazione.

        La sua copia e' una patch in posizione sul `.xls`: se il percorso e' di
        un'altra settimana, le righe sono quelle di un altro documento esatta-
        mente come per gli altri.  La guardia non puo' valere «per chi passa
        dal writer».
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
    """La difesa decisiva del bloccante del 12 agosto 2026.

    Dopo un ricalcolo i listini sono altri file, con altri nomi e altre righe.
    Se `writer_config.json` resta quello di prima, la compilazione scrive le
    quantita' di oggi nelle righe del listino della settimana scorsa: dal vivo
    la quantita' di un prodotto e' finita sulla riga di un altro, con
    `writerIssues: []`.  L'impronta non se ne accorge — controlla che il file
    non sia cambiato da quando e' stato verificato, ed e' vera anche quando il
    file e' quello sbagliato ma coerente con se stesso — e comunque la si
    guardava solo per due fornitori su quattro.

    Qui si prova che il confronto fra i due `run_id` c'e', che si ferma
    **prima** di invocare qualunque writer, e che fallisce chiuso su ognuno dei
    modi in cui la domanda puo' restare senza risposta.
    """

    def setUp(self) -> None:
        super().setUp()
        # Una configurazione completa: cosi' l'unica cosa che puo' fermare la
        # compilazione e' la run, ed e' quella che le prove muovono.
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
        # La frase non pretende di sapere chi dei due sia rimasto indietro:
        # senza tener traccia dell'ordine sarebbe una diagnosi inventata.
        self.assertIn("non appartiene allo stesso confronto", avviso)
        # Il rimedio dice quale azione dell'utente rigenera la configurazione.
        self.assertIn("Ricalcola il confronto", avviso)
        self.assertIn(avviso, esito["message"])
        # ⚠ Il writer non e' stato nemmeno invocato: ci si ferma prima.
        self.assertEqual(self.scrittore.destinazioni, [])
        self.assertFalse(list(self.cartella_di(esito).glob("*.xlsx")))
        # E resta scritto nell'audit della compilazione, non solo nella risposta.
        self.assertIn(avviso, self.audit_di(esito)["avvisi"])

    def test_una_configurazione_che_non_dichiara_la_run_ferma(self) -> None:
        """Fallire chiuso: una config vecchia non dice a quale ricalcolo appartiene.

        E' il caso di chi aggiorna il programma senza rifare il ricalcolo: la
        domanda «e' del confronto di adesso?» resta senza risposta, e a una
        domanda senza risposta non si scrive dentro il listino di un fornitore.
        """

        self.scrivi_config(run_id=None)

        esito = self.compila()

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non dice a quale confronto appartiene", esito["writerIssues"][0])
        self.assertEqual(self.scrittore.destinazioni, [])

    def test_un_confronto_senza_run_ferma(self) -> None:
        """L'altro lato della stessa domanda: nessun ricalcolo è mai arrivato in fondo."""

        self.scrivi_confronto(None)
        self.scrivi_config(run_id="run-sintetica")

        esito = self.compila()

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non dichiara da dove viene", esito["writerIssues"][0])
        self.assertEqual(self.scrittore.destinazioni, [])

    def test_due_run_vuote_non_si_annullano(self) -> None:
        """Due stringhe vuote sono uguali, e non vuol dire che vada bene."""

        self.scrivi_confronto("")
        self.scrivi_config(run_id="")

        esito = self.compila()

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("non dichiara da dove viene", esito["writerIssues"][0])
        self.assertEqual(self.scrittore.destinazioni, [])

    def test_con_la_stessa_run_la_compilazione_va_avanti(self) -> None:
        """La guardia non deve fermare la settimana buona."""

        self.scrivi_config(run_id="run-sintetica")

        esito = self.compila()

        self.assertEqual(esito["status"], "FILES_READY", esito["message"])
        self.assertEqual(esito["writerIssues"], [])
        self.assertEqual(self.scrittore.destinazioni, [self.cartella_di(esito)])

    def test_il_ricalcolo_che_finisce_durante_la_compilazione_non_apre_la_finestra(self) -> None:
        """Il BLOCCANTE della revisione avversariale del 13 agosto 2026.

        La compilazione costruisce il piano dalla run A; un ricalcolo che
        finisce un attimo dopo porta configurazione E confronto alla run B.
        La prima versione della guardia rilegge il confronto dal disco e trova
        `B == B`: diceva di si' a un piano che parlava di A — il bloccante del
        12 agosto, identico, con `writerIssues` vuoto.  La guardia deve
        confrontare la configurazione con il PIANO, che nessuno puo' cambiare
        sotto i piedi.  Il cambio di run e' iniettato nella creazione della
        cartella datata: il piano e' gia' costruito e parla della run A, la
        guardia non ha ancora letto niente, ed e' un punto che la compilazione
        attraversa davvero (i due lucchetti non si escludono: il server e' un
        `ThreadingHTTPServer`).  Prima l'iniezione stava in
        `record_order_history`, che dal 13 agosto 2026 viene DOPO la scrittura:
        li' non avrebbe piu' provato niente.
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
        """`prepare_writer_config` torna senza scrivere quando manca un requisito.

        Senza il controllo sull'esito, il ramo piu' probabile — Node sparito,
        script mancante — falliva IN SILENZIO: nessuna eccezione, nessun
        avviso `COMPILAZIONE_DA_RICONFIGURARE`, e la configurazione restava
        quella della settimana scorsa (revisione avversariale del 13 agosto
        2026, rilievo 7).  La guardia del run_id terrebbe comunque chiusa la
        compilazione, ma la promessa e' che lo si dica subito.
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
    """Fondamenta comuni alle prove qui sotto: un confronto scritto su misura."""

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
    """«BETULLA potrebbe scontare tutto del 6%»: uno sconto su tutto il listino.

    Deciso il 15 agosto 2026: lo sconto si mette dopo aver caricato i listini e
    prima di cominciare a scegliere, riassegna al piu' conveniente **in
    silenzio** («so benissimo cosa sto facendo»), e **scade al ricalcolo** senza
    dire niente a nessuno.
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
        # Gli altri fornitori non si toccano.
        self.assertAlmostEqual(primo["larice"], 12.0, places=4)
        self.assertAlmostEqual(self.prezzi("product:2")["betulla"], 28.2, places=4)

    def test_riassegna_anche_i_prodotti_senza_decisione_salvata(self) -> None:
        """Il `continue` si appoggiava a un'invariante che nessuno impone.

        Oggi il campo dello sconto non e' raggiungibile senza passare da un
        salvataggio che scrive una decisione per ogni prodotto del confronto,
        quindi la mappa non e' quasi mai vuota. Ma basta che un salvataggio
        fallisca — ed e' successo, il 15 agosto 2026 — perche' lo sconto
        riassegni meta' dei prodotti e taccia sull'altra meta'.
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
        """⚠ Senza `quantitySource`, il ricalcolo successivo smette di
        rileggere i colli dal gestionale e la quantita' resta congelata
        (`pipeline_jobs._ripulisci_stato`): sarebbe una riga scritta apposta
        per rompere la settimana dopo."""

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
        """Col 6% BETULLA passa davanti su product:1, ma non su product:2."""

        esito = self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})

        self.assertEqual(esito["reassigned"], 1)
        confronto = self.store.review()
        scelti = {p["id"]: p["selectedSupplierId"] for p in confronto["products"]}
        self.assertEqual(scelti["product:1"], "betulla")
        self.assertEqual(scelti["product:2"], "larice")
        # Nessun avviso: chi lo mette sa quello che sta facendo.
        codici = {voce.get("code") for voce in confronto.get("warnings") or []}
        self.assertNotIn("SCONTO_FORNITORE", codici)

    def test_i_totali_del_servizio_usano_il_prezzo_scontato(self) -> None:
        """Il riepilogo e la pagina devono dire lo stesso numero."""

        self.store.set_supplier_discount({"supplierId": "betulla", "percent": 6})

        riepilogo = self.store.review()["orderSummary"]
        betulla = next(voce for voce in riepilogo["suppliers"] if voce["supplierId"] == "betulla")
        # 2 colli di product:1 a 11,844 = 23,688, che il riepilogo mostra al
        # centesimo come fa la pagina: 23,69. Senza sconto sarebbero 25,20.
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
        """`validate_snapshot` ricostruisce lo stato da zero: lo sconto va riportato."""

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
    """Il passo 2 si apriva con «Salvataggio non riuscito» e da lì non salvava più.

    Misurato il 15 agosto 2026 sul confronto vero: 8 prodotti avevano la
    quantità del gestionale e una corrispondenza da confermare, e tanto bastava
    perché ogni `PUT /api/state` tornasse 422. Quantità cambiate, fornitori
    scelti, prodotti esclusi: niente arrivava sul disco finché non si rispondeva
    a tutte e otto le proposte.

    La regola giusta è quella che il preventivo di spostamento applicava già:
    una conferma che manca impedisce di **compilare**, non di **salvare**.
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
        """È il danno vero: non il primo salvataggio, ma tutti quelli dopo."""

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
        """Si è tolto un cancello solo, non il controllo."""

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
    """La merce che il gestionale chiede e che nessun fornitore porta.

    Decisione di Daniele del 16 agosto 2026, che **rovescia** quella del 12
    agosto («si lascia com'è»): una quantità positiva senza nessun fornitore
    disponibile non è più un errore da correggere, è uno stato valido.  Quel
    prodotto non entra nel piano né nei listini dei fornitori — non c'è nessuna
    riga su cui scrivere — ma non si perde: alla compilazione esce in un foglio
    a parte.

    Il confine che questi test difendono è uno solo, ed è quello che si può
    sbagliare: **nessuno disponibile** è un'altra cosa da **nessuno scelto**.
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
        """La regola non si allenta dove c'è ancora una decisione da prendere."""

        store, _ = self.senza_nessuno()

        with self.assertRaises(SnapshotError) as errore:
            store.save_state(self.scelte({"product:198": ("larice", 3)}))

        self.assertEqual({voce["code"] for voce in errore.exception.errors}, {"OFFERTA_NON_VALIDA"})

    def test_con_qualcuno_disponibile_scegliere_resta_obbligatorio(self) -> None:
        """L'altro lato del confine: qui un fornitore c'è, e va scelto.

        ⚠ La prova si fa sulla **compilazione**. Dal 21 agosto 2026 questo caso
        ha un codice suo, `FORNITORE_DA_SCEGLIERE`, e non ferma piu' il
        salvataggio: e' una decisione ancora da prendere, e succede normalmente
        dopo un «Non è lo stesso articolo» quando un altro fornitore l'articolo
        ce l'ha. Fermando il salvataggio spegneva quello di TUTTI i prodotti —
        il guasto del 15 agosto — e bloccava anche il pulsante per tornare
        indietro, che salva prima di rispondere. Ferma la compilazione: la
        regola del 12 agosto resta intera.
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
        """Un'offerta che chiede una conferma è un'offerta, non un'assenza."""

        offerta = self.offerta("larice", 14.9)
        offerta["requiresConfirmation"] = True
        store = self.negozio(self.confronto([
            self.prodotto("product:198", "OLIO EXTRAVERGINE 1L", "8009580477747", [offerta]),
        ]))

        scelta = self.scelte({"product:198": ("", 3)})
        _pulito, errori, _review = store.validate_snapshot(scelta, for_compile=True)

        self.assertEqual({voce["code"] for voce in errori}, {"FORNITORE_DA_SCEGLIERE"})

    def test_nessuna_conferma_si_pretende_su_chi_non_ha_fornitore(self) -> None:
        """Una casella che l'utente non può spuntare fermerebbe tutto per sempre.

        ⚠ La prova si fa sulla **compilazione**, non sul salvataggio: `save_state`
        `CONFERMA_MANCANTE` lo scarta comunque (`app/server.py:1728`), quindi una
        prova sul salvataggio resta verde anche a guardia spenta e non prova
        niente.  L'ha trovata una mutazione rimasta verde il 17 agosto 2026.
        """

        prodotto = self.prodotto("product:198", "OLIO EXTRAVERGINE 1L", "8009580477747",
                                 [self.offerta("larice", 14.9, disponibile=False)])
        prodotto["requiresConfirmation"] = True
        # Un secondo prodotto ordinabile: così la compilazione ha un ordine vero
        # da produrre e il rifiuto, se arriva, arriva per il primo.
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
    """La regola di rifiuto non cambia: cambia quello che il rifiuto dice.

    Fino al 13 agosto 2026 la pagina mostrava «Controlli snapshot non
    superati», e davanti a cinquecento righe non c'era modo di sapere quale
    prodotto guardare.
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
        # E dice che cosa fare, non solo che qualcosa non va.
        self.assertIn("quantità a zero", messaggio)
        voce = errore.exception.errors[0]
        self.assertEqual(voce["code"], "OFFERTA_NON_VALIDA")
        self.assertEqual(voce["productName"], "OLIO EXTRAVERGINE 1L")
        self.assertEqual(voce["supplierName"], "LARICE")

    def test_senza_fornitore_scelto_lo_dice_diversamente(self) -> None:
        # ⚠ Il prodotto di questa prova ha un fornitore che PUO' servirlo: dal
        # 16 agosto 2026 una quantità senza fornitore è un errore solo quando
        # c'è qualcuno da scegliere. Se non c'è nessuno è un prodotto da
        # reperire, ed è uno stato valido — lo prova
        # `ProdottiDaReperireTests`.
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
        """L'offerta non disponibile resta rifiutata: cambia solo la frase."""

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
        # `body.message` e' l'unico campo che la pagina mostra: la frase deve
        # stare li' dentro, non solo nei codici.
        self.assertIn("OLIO EXTRAVERGINE 1L", corpo["message"])
        self.assertIn("LARICE", corpo["message"])
        self.assertEqual(corpo["errors"][0]["code"], "OFFERTA_NON_VALIDA")


class OmaggiPersiNelloSpostamentoTests(NegozioSintetico):
    """Spostare la merce fa saltare le soglie: il numero di omaggi si conta."""

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
        # 12 colli con una soglia ogni 2 colli: sei omaggi, tutti su LARICE.
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
        """Decisione commerciale: l'omaggio informa, non entra nei conti."""

        opzione = self.preventivo()["options"][0]

        for chiave in opzione:
            self.assertNotIn("giftValue", chiave)
            self.assertNotIn("valoreOmaggi", chiave)


class ScartoFraTestataERigheTests(NegozioSintetico):
    """I due totali non coincidono sempre, e la differenza va dichiarata."""

    def setUp(self) -> None:
        super().setUp()
        # Prezzi con i decimali dei listini veri: la somma delle righe mostrate
        # al centesimo non fa il totale calcolato sui prezzi interi.
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
        # 14,9925 + 14,665 + 10,125 = 39,78 sul totale, 39,79 sommando le righe
        # arrotondate una per una al centesimo: un centesimo di scarto, e va
        # detto anche quando le righe stanno sopra la testata.
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
        """Senza, la frase comparirebbe solo dopo il primo salvataggio."""

        self.store.save_state(self.scelte(self.acquisti))

        riepilogo = self.store.review()["orderSummary"]

        self.assertEqual(riepilogo["totalNet"], 39.78)
        self.assertEqual(riepilogo["roundingDifference"], -0.01)

    def test_il_totale_del_riepilogo_e_quello_della_compilazione(self) -> None:
        """Il Riepilogo e il piano ordini non possono dire due numeri diversi."""

        riepilogo = self.store.save_state(self.scelte(self.acquisti))["orderSummary"]
        with mock.patch.object(self.store, "writer_config", None):
            esito = self.store.compile(self.scelte(self.acquisti))

        self.assertEqual(esito["totalsNet"]["larice"], riepilogo["suppliers"][0]["totalNet"])


class ScartiDelParserInPaginaTests(NegozioSintetico):
    """Le righe che non entrano nel confronto arrivano fino alla pagina."""

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
        # I numeri arrivano dall'audit: la pagina non ne inventa nessuno.
        self.assertNotIn("rowsKept -", corpo)

    def test_la_frase_dice_quante_righe_e_perche(self) -> None:
        corpo = self.app_js.split("function discardedRowsText(")[1].split("\n}\n")[0]
        self.assertIn("non ordinabili", corpo)
        self.assertIn("escluse dal filtro", corpo)
        self.assertIn("righe scartate", corpo)

    def test_i_codici_a_barre_ripetuti_si_dicono(self) -> None:
        """`duplicate_ean_values` spiega perché due prodotti «uguali» sono righe diverse."""

        corpo = self.app_js.split("function normalizeDiscardedRows(")[1].split("\n}\n")[0]
        self.assertIn("duplicate_ean_values", corpo)
        riquadro = self.app_js.split("function renderDiscardedRowsPanel(")[1].split("\n}\n")[0]
        self.assertIn("codici a barre ripetuti", riquadro)

    def test_il_riquadro_sta_nella_pagina_dei_documenti(self) -> None:
        passo = self.app_js.split("function renderUploadStep(")[1].split("\n}\n")[0]
        self.assertIn("renderDiscardedRowsPanel()", passo)

    def test_i_dettagli_tecnici_restano_chiusi_finche_non_servono(self) -> None:
        # ⚠ Dal 15 agosto 2026 l'apertura non e' piu' inchiodata nel markup: la
        # decide `apribile()`, che scrive `open` solo se l'utente l'ha aperto.
        # Il riquadro nasce chiuso perche' la memoria parte vuota, e resta
        # aperto durante i ridisegni — che finche' il ricalcolo gira sono uno
        # al secondo. Le prove eseguite stanno in
        # `tests/test_interfaccia_pagina1.py::SezioniCheRestanoAperte`.
        riquadro = self.app_js.split("function renderDiscardedRowsPanel(")[1].split("\n}\n")[0]
        self.assertIn('<details class="panel import-panel audit-disclosure" ${apribile("righe-scartate")}>', riquadro)
        self.assertNotIn('<details open', riquadro)


class LaDomandaDelleConsegneInPaginaTests(unittest.TestCase):
    """La pagina 2 dopo il difetto D4: tre risposte e una domanda che torna."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    def test_la_terza_risposta_esiste_e_chiude_l_ordine(self) -> None:
        self.assertIn('data-action="history-never"', self.app_js)
        # «NON ARRIVERÀ PIÙ» diceva l'effetto sulla merce, non sulla domanda:
        # l'azione e' irreversibile e adesso lo dice.
        self.assertIn("Annullato, non arriva", self.app_js)
        invio = self.app_js.split("async function answerPendingOrder(")[1].split("\n}\n")[0]
        self.assertIn("{ orderId, closed: true }", invio)

    def test_il_ritorno_della_domanda_lo_decide_il_servizio(self) -> None:
        corpo = self.app_js.split("function domandaRimandata(")[1].split("\n}\n")[0]
        self.assertIn("entry.askAgainAt", corpo)
        # La vecchia regola del browser — «vale per la giornata» — non c'e' piu'.
        self.assertNotIn("toDateString()", self.app_js)
        self.assertIn("askAgainAt", self.app_js.split("function normalizePendingOrder(")[1].split("\n}\n")[0])

    def test_lo_scarto_da_arrotondamenti_non_si_stampa_piu_sulla_pagina(self) -> None:
        """Un centesimo di scarto non e' un'informazione per chi ordina.

        Stava sotto il totale di ogni fornitore e sotto quello generale, cioe'
        accanto al numero che invece conta. Il campo `roundingDifference`
        resta nella risposta del servizio — e resta provato qui sopra — ma
        nessuna riga della pagina lo mostra.
        """

        # ⚠ La frase si cerca per il nome delle funzioni che la
        # scrivevano, non per il suo testo: il testo resta scritto nel
        # commento che spiega perche' e' stata tolta, ed e' giusto che resti.
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
    """Nomi e regole di scrittura vengono dal registro, non dal codice.

    Erano scritti in tre punti (servizio, writer Node, costruzione del
    confronto) e ne conoscevano quattro: un fornitore imparato compariva come
    «NUOVO_FORNITORE», underscore compreso, nei messaggi e nello storico, e non
    ereditava nessuna regola di scrittura anche quando il registro gliela
    dichiarava.
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
        """«NUOVO_FORNITORE» in mezzo a una frase si legge come un guasto."""

        self.assertEqual(supplier_label("nuovo_fornitore"), "NUOVO FORNITORE")
        self.assertEqual(supplier_label(""), "FORNITORE")

    def test_fra_due_adattatori_dello_stesso_fornitore_vince_il_nome_del_fornitore(self) -> None:
        """Il nome più lungo descrive il documento, non chi lo manda."""

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
        """Per CIPRESSO e Noce foglio e righe li dichiara l'utente, non il registro."""

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
    """Il writer deve poter controllare che la riga porti il prodotto giusto.

    È la difesa che il 12 agosto 2026 avrebbe fermato l'ordine finito sulla riga
    2600, e finora esisteva solo per Noce. Dove stanno EAN e descrizione il
    registro lo dichiara già — a lettere per Larice, per nome dell'intestazione
    per BETULLA — e la regola di scrittura porta quella posizione al writer.
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
        """Larice non ha nessuna riga di intestazione: il registro dice le lettere."""

        regola, avviso = self.launcher.source_rule("larice", self.listino_larice(), {}, registro_adattatori.adattatore("larice_v1"))

        self.assertIsNone(avviso)
        self.assertEqual(regola["verify"], {"ean_column": "R", "description_column": "G"})

    def test_la_regola_porta_anche_il_nome_leggibile_del_fornitore(self) -> None:
        """Il writer Node non legge il registro: la configurazione è il suo unico ingresso."""

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
    """Un default vecchio non deve travestirsi da scelta dell'utente.

    Il 26 agosto 2026: NOCE entra nel confronto con il prezzo più basso, la
    pagina lo mostra, e l'ordine continua ad andare a BETULLA. Il totale NOCE
    era 459 € contro i 2.209 € dell'ordine del 17 agosto. L'unico modo di
    rimetterlo a posto era mettere uno sconto e riportarlo a zero, perché
    `set_supplier_discount` era l'unico punto del programma che rifaceva la
    scelta del fornitore.

    In piedi dal primo commit: `build_products` sceglie il più conveniente,
    `review()` ci passava sopra con la decisione salvata senza chiedersi chi
    l'avesse presa. Diventato raggiungibile il 12 agosto, quando ricalcolare è
    diventato un pulsante e si è potuto aggiungere un listino a metà lavoro.

    La distinzione si deduce e non si chiede alla pagina: il più conveniente il
    programma **l'ha già selezionato da sé**, quindi una decisione che coincide
    col migliore non può essere che il default.
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
                # Il confronto ricalcolato ha già scelto il più conveniente.
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
        """Quelle salvate prima della correzione: il confronto di adesso vince."""

        self._salva_decisione("betulla", None)

        confronto = self.store.review()

        self.assertEqual(confronto["products"][0]["selectedSupplierId"], "noce")

    def test_la_scelta_dell_utente_non_si_tocca(self) -> None:
        self._salva_decisione("betulla", "utente")

        confronto = self.store.review()

        self.assertEqual(confronto["products"][0]["selectedSupplierId"], "betulla")
        # E non si dice niente: non è cambiato niente.
        self.assertNotIn("FORNITORE_PIU_CONVENIENTE_RIPRESO", self._avvisi(confronto))

    def test_cambiando_fornitore_la_conferma_di_prima_non_vale_piu(self) -> None:
        """La conferma copre l'articolo guardato, e l'articolo è cambiato."""

        self._salva_decisione("betulla", "automatico")

        prodotto = self.store.review()["products"][0]

        self.assertEqual(prodotto["selectedSupplierId"], "noce")
        self.assertFalse(prodotto["confirmed"])

    def test_col_fornitore_scelto_a_mano_la_quantita_resta_dell_utente(self) -> None:
        """La combinazione che una prima versione della correzione rompeva.

        Il ramo «l'ha scelto l'utente» usciva dal ciclo con un `continue`, e
        saltava il ripristino di `quantitySource`: la quantità scritta a mano
        tornava a dichiararsi «valore dal gestionale» dopo un ricaricamento, e il
        comando che azzera le sole quantità predefinite se la portava via. Due
        cose entrambe volute dall'utente, e la seconda spariva per via della
        prima.
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
        """Chi sceglie il migliore prende il default; chi sceglie altro, no."""

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
    """Con due schede aperte, la seconda non deve azzerare il lavoro della prima.

    `save_state` riscrive TUTTO lo stato e il controllo della run non separa due
    schede sullo stesso confronto: la scheda rimasta aperta salvava — da sola,
    per l'autosalvataggio a 450 ms — uno snapshot in cui le quantita' dell'altra
    non c'erano mai state, e nessuno diceva niente. Misurato prima della
    correzione: `[('p1',4),('p2',0)]` diventava `[('p1',0),('p2',7)]`.
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
        """Lo snapshot che manda una scheda del browser: l'elenco sempre intero."""

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
        # La scheda A e la scheda B hanno aperto la stessa pagina: partono
        # entrambe dalla versione 0.
        self.store.save_state(self.scheda(versione=0, display=4, standard=0))

        with self.assertRaises(SnapshotError) as fermata:
            self.store.save_state(self.scheda(versione=0, display=0, standard=7))

        self.assertEqual({str(item["code"]) for item in fermata.exception.errors}, {"STATO_SOVRASCRITTO"})
        # E soprattutto: sul disco resta il lavoro della scheda A.
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
        """Il difetto del 26 agosto 2026: rifiutata una conferma ogni due o tre.

        Non c'era nessuna seconda scheda. Lo sconto di un fornitore — come
        l'abbinamento a mano e il rifiuto — avanza la versione sul disco, e la
        sua risposta non la portava indietro: la scheda restava indietro di uno
        e il salvataggio SUCCESSIVO, fatto da lei stessa, si sentiva rispondere
        «un'altra scheda ha salvato dopo di te», mandando l'utente a cercare un
        collega che non esiste. Ricaricare rimetteva a posto, fino al gesto dopo.
        """

        primo = self.store.save_state(self.scheda(versione=0, display=4, standard=0))

        esito = self.store.set_supplier_discount({"supplierId": "larice", "percent": 6})

        # 1. La risposta porta la versione nuova, altrimenti la scheda non ha
        #    nessun modo di sapere di essere rimasta indietro.
        self.assertIn("stateVersion", esito)
        self.assertGreater(int(esito["stateVersion"]), int(primo["stateVersion"]))
        self.assertEqual(int(esito["stateVersion"]), int(self.store.review()["state"]["stateVersion"]))

        # 2. E con quella versione la stessa scheda salva, senza essere scambiata
        #    per un'altra.
        salvato = self.store.save_state(
            self.scheda(versione=int(esito["stateVersion"]), display=4, standard=7)
        )
        self.assertTrue(salvato["ok"])
        self.assertEqual(self.quantita_salvate(), {"display-solbao-96": 4, "product-standard": 7})

    def test_la_scheda_che_si_aggiorna_torna_a_poter_salvare(self) -> None:
        """Il rifiuto non è un vicolo cieco: ricaricata, la scheda riprende."""

        self.store.save_state(self.scheda(versione=0, display=4, standard=0))
        versione_vista_ricaricando = self.store.review()["state"]["stateVersion"]

        esito = self.store.save_state(self.scheda(versione=versione_vista_ricaricando, display=4, standard=7))

        self.assertTrue(esito["ok"])
        self.assertEqual(self.quantita_salvate(), {"display-solbao-96": 4, "product-standard": 7})

    def test_una_pagina_che_non_dichiara_la_versione_salva_lo_stesso(self) -> None:
        """Una pagina vecchia rimasta aperta non si lascia senza salvataggio."""

        self.store.save_state(self.scheda(versione=0, display=4, standard=0))

        esito = self.store.save_state(self.scheda(versione=None, display=0, standard=7))

        self.assertTrue(esito["ok"])

    def test_anche_la_compilazione_rifiuta_uno_stato_sorpassato(self) -> None:
        """Compilare da una scheda vecchia manderebbe un ordine di quantità morte."""

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
        """Chi ha compilato deve poter salvare subito dopo: la pagina lo fa."""

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
        """Chi tocca solo una parte dello stato non muove la versione.

        `validate_snapshot` ricopia `matchOverrides` e `manualProducts` dal
        disco: quelle scritture non si possono perdere, e farle scadere
        significherebbe solo rifiutare salvataggi buoni.
        """

        self.store.save_state(self.scheda(versione=0, display=4, standard=0))
        stato = json.loads(self.state_path.read_text(encoding="utf-8"))
        stato["matchOverrides"] = [{"productId": "product-standard", "accepted": True}]
        self.state_path.write_text(json.dumps(stato), encoding="utf-8")

        esito = self.store.save_state(self.scheda(versione=1, display=4, standard=3))

        self.assertTrue(esito["ok"])
        self.assertEqual(self.quantita_salvate()["product-standard"], 3)


class UnAltroSitoNonComandaIlComparatoreTests(unittest.TestCase):
    """Una pagina qualunque aperta in quel browser non deve poter comandare qui.

    Prima della guardia poteva, e il metodo POST non lo impediva: una POST con
    `Content-Type: text/plain` e' una «simple request» CORS, parte senza
    preflight e l'effetto avviene anche se chi l'ha mandata non legge la
    risposta.  Misurato il 19 agosto 2026 su un servizio vero: `/api/upload`
    con `Origin: https://sito-cattivo.example` rispondeva «1 documento caricato
    e letto» e il file finiva davvero sul disco; subito dopo `/api/spegni`
    rispondeva `{"spento": true}` e il programma si fermava.

    Le prove qui sotto guardano tutte e due i versi, perche' una guardia che
    rifiuta anche la pagina vera e' peggio del buco che chiude: il lanciatore
    chiama `/api/spegni` con urllib, che `Origin` non lo manda affatto.
    """

    class StoreFinto:
        """Registra le chiamate: così un 403 si distingue da «l'ha fatto lo stesso»."""

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
        # E soprattutto: la catena non e' partita, cioe' non ha speso credito.
        self.assertEqual(self.store.avvii, 0)

    def test_il_browser_che_dichiara_la_richiesta_estranea_viene_fermato(self) -> None:
        """`Sec-Fetch-Site` da solo basta: è quello che arriva senza `Origin`."""

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
        """`localhost` e `127.0.0.1` sono due origini diverse per il browser."""

        stato, _corpo = self.chiedi(
            "POST", "/api/pipeline/avvia",
            Origin=f"http://localhost:{self.porta}",
        )

        self.assertEqual(stato, 202)
        self.assertEqual(self.store.avvii, 1)

    def test_il_lanciatore_che_non_manda_nessuna_delle_due_intestazioni_passa(self) -> None:
        """Chi chiama con urllib non manda `Origin`: la guardia non lo puo' escludere.

        E' il caso vero del lanciatore, che chiama `/api/spegni` a ogni avvio
        quando i sorgenti sono cambiati.  Se questa prova diventasse rossa, il
        programma del negozio non si riaprirebbe piu' aggiornato.
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
    """Un dominio che il DNS ha appena ripuntato su 127.0.0.1 non deve poter
    leggere qui, nemmeno con una semplice GET.

    Nel DNS rebinding l'attaccante possiede un dominio vero, lo fa aprire
    all'utente, poi fa scadere il proprio DNS e lo ripunta su 127.0.0.1: da
    quel momento le richieste che la pagina manda a
    `http://dominio-cattivo.example:<porta>/...` restano same-origin per il
    browser (l'origine della pagina non e' cambiata), quindi la pagina cattiva
    LEGGE la risposta. In una GET l'intestazione `Origin` spesso non c'e'
    nemmeno, quindi la guardia anti-CSRF sull'origine (rilievo [35]) da sola
    non basta: serve guardare `Host`, che arriva sempre com'era nella barra
    dell'indirizzo del sito cattivo, mai come l'IP a cui il DNS ha ripuntato.

    Le prove coprono tutti e due i versi, con lo stesso schema della classe
    sopra: un `Host` estraneo deve fermare l'effetto, e le forme legittime —
    con porta, senza porta, assente del tutto come in HTTP/1.0 — devono
    continuare a passare, altrimenti la pagina vera non si apre piu'.
    """

    class StoreFinto:
        """Registra le chiamate: così un 403 si distingue da «l'ha fatto lo stesso»."""

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
        """Simula un client che non manda affatto `Host`, come può fare HTTP/1.0."""

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
        # E soprattutto: la lettura non e' avvenuta, cioe' il confronto non e' uscito.
        self.assertEqual(self.store.letture_review, 0)

    def test_un_host_estraneo_ferma_la_catena_anche_con_l_origine_giusta(self) -> None:
        """La guardia sull'Host viene prima di quella sull'origine: deve bastare da sola."""

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
        """Alcuni client mandano `Host: 127.0.0.1` senza porta: deve passare comunque."""

        stato, _corpo = self.chiedi("GET", "/api/review", Host="127.0.0.1")

        self.assertEqual(stato, 200)
        self.assertEqual(self.store.letture_review, 1)

    def test_l_host_assente_del_tutto_passa(self) -> None:
        """HTTP/1.0 può non mandare `Host`: altrimenti la pagina vera non si aprirebbe più."""

        stato, _corpo = self.chiedi_senza_intestazione_host("GET", "/api/review")

        self.assertEqual(stato, 200)
        self.assertEqual(self.store.letture_review, 1)


class LoSpegnimentoDelServizioNonUccideUnaRunInCorsoTests(unittest.TestCase):
    """`/api/spegni` e la sua guardia «c'e' un confronto in corso» — rilievo [51].

    `_spegni()` ha due regole scritte apposta e nessuna prova le guardava:
    non si ferma se la catena sta lavorando (`RUN_IN_CORSO`), perche' una run
    uccisa a meta' lascia una cartella datata orfana e il lavoro gia' pagato
    all'AI da rifare; e prima di fermarsi restituisce il file delle conferme
    con `store.chiudi()`, perche' su Windows un file SQLite aperto non si
    rinomina. Chi la chiama davvero e' `app/launcher.py`, con `urllib`, a ogni
    avvio del programma nel negozio: se la guardia sparisse in un riordino, il
    lanciatore ucciderebbe una run a meta' e nessuna prova lo direbbe.

    ⚠ Nessuna prova qui spegne un servizio vero. `_spegni()` avvia il vero
    `shutdown()` su un thread proprio, e chiamarlo davvero fermerebbe questo
    `ThreadingHTTPServer` e romperebbe le prove che vengono dopo nello stesso
    file. Il metodo vero viene messo da parte in `setUp` e sostituito con uno
    che registra soltanto la chiamata; a fine prova si richiama quello vero,
    per fermare il thread sul serio.
    """

    class StoreFinto:
        """Registra le chiamate, nell'ordine in cui arrivano."""

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

        # Il vero shutdown() fermerebbe il server sotto le prove che seguono:
        # lo mettiamo da parte e lo sostituiamo con uno che registra soltanto
        # la chiamata, senza fermare niente.
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
        # Qui si richiama il metodo vero, messo da parte in setUp: e' l'unico
        # modo di fermare per davvero il thread che serve le richieste.
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
        """`_spegni()` avvia `shutdown()` su un thread separato: aspetta che arrivi."""

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

        # Il servizio deve rispondere ancora, subito dopo: non si e' fermato.
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
        """È la parte che conta su Windows: un file SQLite aperto non si rinomina."""

        self.chiedi("POST", "/api/spegni")

        self.assertIn("chiudi", self.store.chiamate)
        self.aspetta_lo_shutdown_finto()
        self.assertLess(
            self.store.chiamate.index("chiudi"),
            self.store.chiamate.index("shutdown"),
        )


class NonELoStessoArticoloTests(unittest.TestCase):
    """La risposta che mancava, e le tre uscite che non erano uscite.

    ⚠ Fino al 21 agosto 2026, davanti a un abbinamento proposto che NON e' lo
    stesso articolo, chi ordina aveva tre strade e nessuna funzionava:
    confermare ordina la merce sbagliata; non confermare lascia la compilazione
    ferma su «Conferma richiesta · bloccante»; «Escludi dall'ordine» azzera la
    quantita', e il ciclo di `compile` salta chi ha quantita' zero — quindi il
    prodotto spariva anche dall'elenco «Prodotti da reperire». Misurato su
    `conferme.db`: venti conferme, di cui zero negative, undici scritte in
    trenta secondi.
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
        """È la ragione per cui questa risposta esiste."""

        self.rifiuta()

        _pulito, errori, _review = self.store.validate_snapshot({
            "runId": "r1",
            "products": [{"id": "product:410", "quantity": 3,
                          "selectedSupplierId": "", "confirmed": False, "excluded": False}],
        }, for_compile=True)

        self.assertEqual([voce["code"] for voce in errori], [])

    def test_l_elenco_da_reperire_dice_che_l_hai_scartato_tu(self) -> None:
        """«Nessun fornitore lo ha disponibile» darebbe la colpa al fornitore."""

        import da_reperire  # noqa: PLC0415

        self.rifiuta()

        self.assertEqual(
            da_reperire.motivo(self.store.review()["products"][0]["offers"]),
            da_reperire.MOTIVO_RIFIUTATO_DA_TE,
        )

    def test_l_avviso_che_passa_dice_il_gesto_non_la_regola(self) -> None:
        """L'avviso della pagina sparisce in 3,6 secondi (`showToast`), e diceva
        per intero la regola di quanto dura un no: la stessa frase che sta ferma
        nel riquadro del fornitore rifiutato, e che stava anche sotto il
        pulsante prima di premerlo. Tre copie, nessuna delle quali diceva quello
        che serve subito."""

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
        """`state.json` muore col ricalcolo; il magazzino delle conferme no."""

        self.rifiuta()
        (self.radice / "state.json").unlink(missing_ok=True)

        self.assertFalse(self.offerta()["available"])

    def test_un_autosalvataggio_non_cancella_il_no(self) -> None:
        """`dimentica` chiude la riga in vigore qualunque sia: qui non deve."""

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


# --- «Inizia nuova comparazione», 22 agosto 2026 ----------------------------


class CominciareUnaComparazioneNuova(unittest.TestCase):
    """Il comando che svuota documenti e confronto per aprire la settimana.

    ⚠ Metà di queste prove non guarda quello che il comando **fa**: guarda
    quello che **non tocca**. È il punto della funzione. Le conferme e gli
    schemi imparati nessun ricalcolo li sa rifare, e gli ordini in attesa,
    cancellati, farebbero riordinare merce che sta arrivando: sono le tre cose
    che trasformerebbero un comando di pulizia in un danno.
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

        # Il nome del fornitore è quello che si legge sulla scheda del
        # documento — «BETULLA», non l'identificativo: è quello che finisce nel
        # messaggio, e la prova deve vedere la stessa cosa della pagina.
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

        # Le memorie che devono sopravvivere, e una compilazione già fatta.
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

    # -- il ripristino, cioe' il ramo che serve quando qualcosa va storto -----

    def test_se_la_pulizia_si_rompe_a_meta_rimette_tutto_a_posto(self) -> None:
        """⚠ E' l'unico pezzo della funzione che nessuna prova aveva mai eseguito.

        `nuova_comparazione` sposta prima tutto in quarantena, poi riscrive i
        profili, e solo alla fine cancella davvero. Se qualcosa va storto in
        mezzo rimette indietro — ed e' il ramo che serve **proprio** quando
        qualcosa e' andato storto, cioe' quello che non si puo' permettere di
        essere sbagliato. Segnalato da Daniele stesso in `Lavori aperti` §22,
        punto 2.
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

        # Tutto com'era: i documenti, il confronto, le scelte, i profili.
        self.assertEqual(self.documenti_rimasti(), prima_documenti)
        self.assertEqual(self.review_path.read_bytes(), prima_confronto)
        self.assertEqual(self.state_path.read_bytes(), prima_scelte)
        self.assertEqual((self.upload_dir / "upload_profiles.json").read_bytes(), prima_profili)
        # E nessun file di quarantena rimasto in giro.
        quarantena = [voce.name for voce in self.upload_dir.iterdir()
                      if voce.name.startswith(".nuova-comparazione-")]
        self.assertEqual(quarantena, [])

    def test_dopo_un_ripristino_il_comando_si_puo_rifare(self) -> None:
        """Il guasto era del disco, non del programma: quando passa, si riprova."""

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

    # -- quello che il comando fa -------------------------------------------

    def test_toglie_i_documenti_il_confronto_e_le_scelte(self) -> None:
        esito = self.store.nuova_comparazione({})

        self.assertTrue(esito["ok"])
        self.assertEqual(self.documenti_rimasti(), [])
        self.assertFalse(self.review_path.exists(), "il confronto di prima non è stato chiuso")
        self.assertFalse(self.state_path.exists(), "le quantità della settimana scorsa sono rimaste")
        profili = json.loads((self.upload_dir / "upload_profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(profili["profiles"], [])

    def test_dice_quanti_documenti_ha_tolto_e_di_chi(self) -> None:
        """Il numero serve alla riga di conferma in pagina: «tolgo 3 documenti»
        si può controllare a occhio contro le schede che si vedono."""

        esito = self.store.nuova_comparazione({})

        self.assertEqual(esito["tolti"], {"elenco": 1, "listini": 2, "documenti": 3})
        self.assertEqual(esito["fornitori"], ["BETULLA", "LARICE"])

    def test_non_lascia_file_di_quarantena_in_giro(self) -> None:
        """I file si spostano prima e si cancellano dopo: a fine giro la
        cartella non deve portarsi dietro il ponteggio."""

        self.store.nuova_comparazione({})

        residui = [voce.name for voce in self.upload_dir.iterdir() if voce.name.startswith(".nuova-comparazione")]
        self.assertEqual(residui, [])

    def test_la_pagina_riparte_da_zero(self) -> None:
        self.store.nuova_comparazione({})
        review = self.store.review()

        self.assertEqual(review.get("products"), [])
        self.assertEqual(review.get("files"), [])

    # -- quello che il comando NON tocca, ed è il punto ----------------------

    def test_le_conferme_restano(self) -> None:
        """Nessun ricalcolo le sa rifare, e non hanno una copia fuori da questo
        disco: un comando di pulizia che se le porta via
        fa ricominciare da zero il lavoro di settimane."""

        self.store.nuova_comparazione({})

        self.assertTrue(self.conferme.is_file())
        self.assertEqual(self.conferme.read_bytes(), b"le conferme di chi ordina")

    def test_gli_ordini_in_attesa_restano(self) -> None:
        """È l'unico modo in cui questo comando produrrebbe un ordine
        sbagliato: cancellare la memoria della merce già ordinata e non ancora
        arrivata fa riordinare quella merce."""

        self.store.nuova_comparazione({})

        self.assertTrue(self.ordini.is_file())
        self.assertEqual(
            json.loads(self.ordini.read_text(encoding="utf-8"))["orders"],
            [{"orderId": "o1", "supplier": "betulla"}],
        )

    def test_gli_schemi_imparati_restano(self) -> None:
        """Cancellarli vorrebbe dire che lunedì prossimo il programma non
        riconosce più nessun listino e richiede tutte le colonne di tutti."""

        self.store.nuova_comparazione({})

        self.assertTrue(self.imparati.is_file())
        self.assertIn("quercia_v1__locale", self.imparati.read_text(encoding="utf-8"))

    def test_le_compilazioni_gia_fatte_restano(self) -> None:
        """Sono i documenti che si mandano al fornitore, e la loro
        eliminazione spegne anche la domanda «è arrivata la merce?»: non è
        pulizia, è un'altra decisione."""

        self.store.nuova_comparazione({})

        self.assertTrue(self.compilazione.is_dir())
        self.assertTrue((self.compilazione / "Ordine BETULLA compilato.xlsx").is_file())

    def test_il_documento_collegato_da_fuori_non_si_cancella(self) -> None:
        """Regola 1: i documenti in ingresso sono di sola lettura. Quello che
        sparisce sono le copie dell'applicazione, mai l'originale."""

        esterno = self.root / "fuori" / "listino_di_daniele.xlsx"
        esterno.parent.mkdir(parents=True, exist_ok=True)
        esterno.write_bytes(b"il file vero di chi ordina")
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        review["files"].append({"name": esterno.name, "role": "supplier", "supplier": "quercia"})
        self.review_path.write_text(json.dumps(review), encoding="utf-8")

        self.store.nuova_comparazione({})

        self.assertTrue(esterno.is_file())
        self.assertEqual(esterno.read_bytes(), b"il file vero di chi ordina")

    # -- la domanda «è arrivata?» torna adesso, non fra sette giorni ---------

    def test_la_domanda_rimandata_torna_in_piedi(self) -> None:
        """Chi risponde «no, non ancora» si sente rinviare la domanda di sette
        giorni. Cominciare una comparazione nuova è un segnale più forte del
        timer: è il momento in cui ci si chiede davvero se la merce della
        settimana scorsa è arrivata (Daniele, 22 agosto 2026).

        Misurato quel giorno sui dati veri: quattro ordini risposti «non
        ancora» il 19 erano rimandati al 26, quindi il comando non avrebbe
        chiesto niente.
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
        """`answeredAt` è la memoria di che cosa è stato risposto e quando, ed
        è quella che risponde a «che cosa avevo deciso prima». Il rinvio è una
        conseguenza di quella data, non un dato suo: si annulla con un segno a
        parte."""

        risposto = ORDER_HISTORY.to_iso(ORDER_HISTORY.utc_now() - timedelta(days=2))
        self.ordini.write_text(json.dumps({"schema_version": 1, "orders": [{
            "orderId": "o1", "supplier": "betulla", "supplierName": "BETULLA", "runId": "vecchia",
            "createdAt": risposto, "answeredAt": risposto, "status": "pending", "lines": [],
        }]}), encoding="utf-8")

        self.store.nuova_comparazione({})

        voce = json.loads(self.ordini.read_text(encoding="utf-8"))["orders"][0]
        self.assertEqual(voce["answeredAt"], risposto)

    def test_una_risposta_data_dopo_rimette_il_rinvio(self) -> None:
        """Altrimenti la domanda tornerebbe per sempre a ogni ricaricamento:
        il segno si confronta con `answeredAt`, non con l'orologio."""

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

    # -- e quando non si può --------------------------------------------------

    def test_non_si_comincia_mentre_il_confronto_gira(self) -> None:
        """Svuotare i documenti sotto una catena che li sta leggendo è la
        stessa ragione per cui durante il ricalcolo non si carica niente."""

        with mock.patch.object(self.store.pipeline_jobs, "in_corso", return_value=True):
            with self.assertRaises(Exception) as errore:
                self.store.nuova_comparazione({})

        self.assertIn("confronto è in corso", str(errore.exception))
        self.assertEqual(len(self.documenti_rimasti()), 3, "ha svuotato lo stesso")
        self.assertTrue(self.review_path.exists())



class IlControlloDiceQuelloCheFermaIlLavoro(unittest.TestCase):
    """`--check` diceva `[OK]` e usciva zero su una macchina dove non si lavora.

    Non prova a prendere la porta, non guarda se la cartella delle copie e'
    scrivibile, non dice se la chiave AI c'e', e tornava zero anche quando
    `prepare_writer_config` dichiarava che nessun listino era compilabile. E'
    il comando che si suggerisce a chi «non riesce ad avviare»: rispondere «va
    tutto bene» e' peggio che non rispondere.

    Prove eseguite.
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
            # Un file al posto della cartella: `mkdir` non ci riesce.
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
        """La controprova: non e' un comando che si lamenta comunque."""

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
