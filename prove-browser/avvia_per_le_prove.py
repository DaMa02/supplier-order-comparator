#!/usr/bin/env python3
"""Start the program on a synthetic comparison, for tests that click through the UI.

Touches nothing real: the run, state and folders live in a temporary
directory that's removed on exit. The starting comparison is the test
suite's `synthetic_review()` — the same base, so it can't drift into two
different truths — with a second supplier added here: NOCE, cheaper
than LARICE on the standard product.

That second supplier is the point: without two prices to compare, neither
supplier selection nor the header-level discount can be exercised, and both
are behaviors that real regressions have slipped through before.
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
    """The test suite's base review plus NOCE, cheaper on `product-standard`."""

    review = deepcopy(synthetic_review())
    review["suppliers"].append({"id": "noce", "name": "Noce", "minimumOrder": 0})
    for prodotto in review["products"]:
        if prodotto["id"] != "product-standard":
            continue
        offerta_larice = prodotto["offers"][0]
        noce = deepcopy(offerta_larice)
        noce.update({
            "supplierId": "noce",
            # Below LARICE's 1.50, so NOCE is the cheapest and it's
            # immediately visible whether automatic selection picks it up.
            "unitPriceNet": 1.0,
            "orderUnitPriceNet": 6.0,
            "price": 6.0,
            "pricePerPiece": 1.0,
        })
        prodotto["offers"].append(noce)
        prodotto["selectedSupplierId"] = "noce"

    # A third product that REQUIRES confirmation: without one, the page never
    # renders the "Sì, è lo stesso" / "non è lo stesso articolo" controls, so
    # the sequence that once left the card a version behind can't be
    # exercised. Kept separate on purpose: the other two products stay
    # stable for tests that check supplier selection.
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

    # A fourth product with a PROPOSED ROW from a supplier the automatic
    # match rejected: the card once showed two contradictory things about
    # LARICE at the same time — its row with code and price above, and
    # "not in the current price list" in the table below. Without a product
    # shaped like this, that inconsistency can't be reproduced and the
    # warning box never renders.
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
        # Starts at step 2, the comparison. Without this, the page opens on
        # "Importa i dati", which is right for a real weekly run but useless
        # for a test that clicks through the comparison. No decision is
        # pre-saved: `products` is empty, so the supplier shown is whatever
        # the comparison itself picks, not one this fixture decided.
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
