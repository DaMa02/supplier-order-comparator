#!/usr/bin/env python3
"""Avvia il programma su un confronto finto, per le prove che premono i pulsanti.

⚠ Non tocca niente di reale: run, stato e cartelle nascono in una cartella
temporanea che sparisce alla chiusura. Il confronto di partenza è
`synthetic_review()` dei collaudi — la stessa base, così se cambia non ci sono
due verità — con **un secondo fornitore aggiunto qui**: NOCE, più
conveniente di LARICE sul prodotto standard.

Quel secondo fornitore è il punto: senza due prezzi da confrontare non si
possono provare né la scelta del fornitore né lo sconto di testata, che sono le
due sequenze che il 26 agosto 2026 hanno prodotto difetti veri.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "tests", RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

from test_web_app import synthetic_review  # noqa: E402


def confronto_con_due_fornitori() -> dict:
    """La base dei collaudi più NOCE, che costa meno su `product-standard`."""

    review = deepcopy(synthetic_review())
    review["suppliers"].append({"id": "noce", "name": "Noce", "minimumOrder": 0})
    for prodotto in review["products"]:
        if prodotto["id"] != "product-standard":
            continue
        offerta_larice = prodotto["offers"][0]
        noce = deepcopy(offerta_larice)
        noce.update({
            "supplierId": "noce",
            # Meno di LARICE (1,50), così il più conveniente è NOCE e si
            # vede subito se la scelta automatica lo prende o lo ignora.
            "unitPriceNet": 1.0,
            "orderUnitPriceNet": 6.0,
            "price": 6.0,
            "pricePerPiece": 1.0,
        })
        prodotto["offers"].append(noce)
        prodotto["selectedSupplierId"] = "noce"

    # Un terzo prodotto che CHIEDE conferma: senza, la pagina non disegna né il
    # «Sì, è lo stesso» né il «non è lo stesso articolo», e la sequenza che il
    # 26 agosto lasciava la scheda indietro di una versione non si può premere.
    # Sta a parte apposta: gli altri due restano stabili per le prove che
    # guardano la scelta del fornitore.
    standard = next(p for p in review["products"] if p["id"] == "product-standard")
    da_confermare = deepcopy(standard)
    da_confermare.update({
        "id": "product-da-confermare",
        "name": "PRODOTTO DA CONFERMARE",
        "description": "PRODOTTO DA CONFERMARE",
        "ean": "8000000000099",
        "quantity": 1,
        "confirmed": False,
        "requiresConfirmation": True,
        "selectedSupplierId": "noce",
    })
    for offerta in da_confermare["offers"]:
        offerta["requiresConfirmation"] = offerta["supplierId"] == "noce"
        offerta["confirmed"] = offerta["supplierId"] != "noce"
    review["products"].append(da_confermare)

    # Un quarto prodotto con una RIGA PROPOSTA da un fornitore che l'analisi
    # automatica ha scartato: e' la scheda del 4 settembre 2026 sul PC del
    # negozio, dove la pagina diceva due cose opposte su LARICE — la sua riga
    # qui sopra con codice e prezzo, e sotto la tabella «non ce l'hanno nel
    # listino di adesso». Senza un prodotto cosi' quella sequenza non si puo'
    # premere: il riquadro giallo non nasce.
    con_proposta = deepcopy(standard)
    candidato = {
        "supplierId": "larice",
        "supplierName": "Larice",
        "candidateKey": "candidato-2pz",
        "description": "PRODOTTO CON RIGA PROPOSTA 2PZ",
        "ean": "8000000000098",
        "supplierCode": "7230",
        "rationale": "Il candidato è una confezione da 2 pezzi, mentre l'articolo cercato è singolo.",
        "unitPriceNet": 3.8,
        "quantityFactor": 6,
        "orderUnitPriceNet": 22.8,
        "price": 22.8,
        "available": True,
    }
    con_proposta.update({
        "id": "product-con-proposta",
        "name": "PRODOTTO CON RIGA PROPOSTA",
        "description": "PRODOTTO CON RIGA PROPOSTA",
        "ean": "8000000000097",
        "quantity": 1,
        "selectedSupplierId": "noce",
        "offers": [
            {**deepcopy(standard["offers"][-1]), "supplierId": "noce", "available": True},
            {"supplierId": "larice", "available": False, "status": "NON_TROVATO",
             "method": "AI_RIFIUTATO", "rejectBestScore": 0.9,
             "rejectedCandidate": candidato},
        ],
        "warnings": [{
            "id": "product-con-proposta-rifiuto-larice",
            "code": "RIFIUTO_CON_CANDIDATO_FORTE",
            "title": "Possibile prodotto Larice",
            "message": "Larice propone «PRODOTTO CON RIGA PROPOSTA 2PZ». Indica se è lo stesso articolo.",
            "technicalMessage": "Il candidato migliore è stato rifiutato con somiglianza 0.90 su 1.",
            "severity": "warning",
            "blocking": False,
            "productId": "product-con-proposta",
            "supplierId": "larice",
            "candidateKey": "candidato-2pz",
            "candidate": candidato,
        }],
    })
    review["products"].append(con_proposta)
    return review


def main() -> int:
    porta = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    cartella = Path(tempfile.mkdtemp(prefix="prove-browser-"))
    try:
        review_path = cartella / "review_data.json"
        review_path.write_text(json.dumps(confronto_con_due_fornitori(), ensure_ascii=False),
                               encoding="utf-8")
        uploads = cartella / "uploads"
        uploads.mkdir()
        # ⚠ Si parte dal passo 2, il confronto. Senza, la pagina apre su
        # «Importa i dati» — giusto per chi comincia la settimana, inutile per
        # una prova che deve premere i pulsanti del confronto. Nessuna decisione
        # salvata: `products` vuoto, così il fornitore che si vede è quello che
        # il confronto sceglie e non uno che ha deciso questa impalcatura.
        (cartella / "state.json").write_text(json.dumps({
            "runId": "run-sintetica",
            "schemaVersion": 1,
            "stateVersion": 0,
            "currentStep": 2,
            "products": [],
        }), encoding="utf-8")
        return subprocess.call([
            sys.executable, str(RADICE / "app" / "server.py"),
            "--review", str(review_path),
            "--state", str(cartella / "state.json"),
            "--uploads", str(uploads),
            "--output-dir", str(cartella / "outputs"),
            "--history", str(cartella / "orders.json"),
            "--port", str(porta),
        ])
    finally:
        shutil.rmtree(cartella, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
