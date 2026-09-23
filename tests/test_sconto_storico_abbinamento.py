#!/usr/bin/env python3
"""Tre difetti della revisione del 6 settembre 2026, provati dal caso vero.

**R6 — lo sconto riassegnava sul confronto nudo.** `set_supplier_discount`
rifaceva la scelta del fornitore su un confronto ricostruito a mano, senza le
righe scelte a mano e senza guardare chi il fornitore l'aveva scelto lui: uno
sconto di testata spostava l'ordine su un'offerta che in pagina non c'era e su
un fornitore che l'utente aveva appena scartato.

**R10 — lo storico restava modificato se l'audit falliva.** La registrazione
dell'ordine precede la scrittura dell'audit; quando l'audit si ferma — disco
pieno, antivirus — la cartella viene cancellata e lo storico no, e la settimana
dopo il programma chiede «è arrivata?» di merce mai ordinata.

**R1 — una scheda vecchia abbinava due articoli sbagliati.** `productId` e
`sourceRow` sono posizionali: mandati da una scheda ferma al confronto della
settimana prima, sul confronto nuovo indicano altri due articoli, e
l'uguaglianza fra i loro codici a barre resta scritta per sempre.
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

# I due banchi di prova esistenti: il confronto sintetico con il catalogo finto
# (abbinamento a mano) e la compilazione che arriva davvero a FILES_READY con
# lo scrittore finto. Rifarli qui vorrebbe dire due verità sullo stesso banco.
import test_abbinamento_a_mano as banco_abbinamento  # noqa: E402
import test_web_app as banco_web  # noqa: E402


# ---------------------------------------------------------------------------
# R6 — lo sconto riassegna il fornitore
# ---------------------------------------------------------------------------

# Il caso della revisione, coi suoi numeri: LARICE ha a listino una riga
# sbagliata da 1 €/pz, quella giusta costa 5 e l'utente l'ha scelta a mano;
# BETULLA sta a 2 e l'utente ha scelto lui.
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
    """Lo sconto cambia i prezzi: la scelta la rifà su quelli che si vedono."""

    def negozio_dello_sconto(self, decisione: dict[str, Any]) -> Any:
        store = self.negozio(confronto_dello_sconto(), {"larice": list(RIGHE_LARICE)})
        store.state_path.write_text(json.dumps({
            "schemaVersion": 1,
            "runId": "run-sconto",
            "stateVersion": 3,
            "products": [decisione],
        }, ensure_ascii=False), encoding="utf-8")
        # La riga giusta di LARICE, scelta a mano: 5 €/pz al posto di 1.
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
        """`selectedSupplierSource == "utente"` vale anche qui.

        Con l'80% LARICE scende a 1 €/pz e diventa il più conveniente anche sul
        confronto decorato: la scelta dell'utente resta dov'è lo stesso. Lo
        sconto cambia i prezzi, non le decisioni prese a mano.
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
        """La decisione automatica si sposta, ma sui prezzi che si vedono.

        Sul confronto nudo LARICE ha ancora la riga sbagliata da 1 €/pz e
        vincerebbe lui; su quello decorato costa 5, e con l'1% BETULLA sta a 1,98.
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
        # E il prezzo su cui la scelta è stata fatta è quello della riga scelta
        # a mano, non quello della riga sbagliata.
        larice = next(o for o in prodotto["offers"] if o["supplierId"] == "larice")
        self.assertAlmostEqual(larice["unitPriceNet"], 5.0, places=4)


# ---------------------------------------------------------------------------
# R10 — lo storico ordini e l'audit che fallisce
# ---------------------------------------------------------------------------

class LoStoricoTornaComEraSeLAuditFallisce(banco_web.ConsegnaBase):
    """Le due parti restano allineate: o la consegna c'è, o non è successo niente."""

    def ordine_precedente(self) -> bytes:
        """Un ordine LARICE della stessa run, ancora senza risposta.

        È la voce che `record_plan` toglie quando lo stesso fornitore viene
        riconsegnato: se lo storico non torna indietro, quell'ordine — mandato
        davvero — sparisce e al suo posto ne resta uno che nessuno ha mandato.
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
        """Il caso gemello: il file non c'era, e non deve nascere da una
        compilazione che non è arrivata in fondo."""

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
# R1 — l'abbinamento a mano e la run della scheda
# ---------------------------------------------------------------------------

class LAbbinamentoAManoGuardaLaRunDellaScheda(banco_abbinamento.BancoDellAbbinamento):
    """`productId` e `sourceRow` sono posizionali: da soli non dicono la settimana."""

    def test_una_scheda_di_un_altra_run_non_abbina_niente(self) -> None:
        store = self.negozio()

        with self.assertRaises(ValueError) as errore:
            store.abbina_riga_di_listino({
                "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
                "runId": "run-della-settimana-scorsa",
            })

        self.assertIn("ricarica la pagina", str(errore.exception))
        # Niente scritto: né lo stato, né l'uguaglianza fra i due codici, che
        # sarebbe permanente e varrebbe per tutti i fornitori.
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
        """Una pagina rimasta in cache non porta `runId`: non deve rompersi."""

        store = self.negozio()

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["ok"], True)
        self.assertEqual(self.offerte_disponibili(store.review()), {"cipresso": 1.28, "noce": 1.15})


if __name__ == "__main__":
    unittest.main()
