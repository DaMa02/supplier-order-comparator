#!/usr/bin/env python3
"""La regola 7 del merge vista dalla pagina: dal listino fino al «si'» e al «no».

Il 18 settembre 2026 le Linda Seta x18 sono andate a NOCE a 2,31 mentre
LARICE le aveva a 2,25 con lo stesso codice a barre: il gestionale le chiama
8009405394204, tutti e due i listini 8009496220932. L'AI aveva accettato
NOCE e rifiutato LARICE. `test_merge_match_decisions` prova la regola da
sola; qui si prova quello che vede chi ordina, passando per i due script veri e per
il servizio vero, con il suo magazzino delle conferme:

1. la riga LARICE costa meno, quindi viene scelta, e chiede conferma dicendo
   perche';
2. un «no» la spegne e resta spento al confronto dopo; il prodotto torna a
   NOCE e chiede conferma anche li', perche' ha lo stesso codice della
   riga appena rifiutata;
3. un «si'» si ricorda, e al confronto dopo il prodotto nasce confermato.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

from conferme import MagazzinoConferme  # noqa: E402
from merge_match_decisions import METODO_STESSO_CODICE, impronta_attesa  # noqa: E402
from server import ReviewStore  # noqa: E402


GESTIONALE = {"source_row": 273, "description": "LINDA SETA ULTRA LUNGO ALI 18PZ", "ean": "8009405394204",
              "suggested_colli": "1", "last_unit_price": "2.25"}
NOCE = {"source_row": 7463, "ean": "8009496220932", "description": "LINDA SETA ULTRA LUNGO ALI PZ.18",
            "unit_price_net": 2.31, "pieces_per_carton": 12, "usable": True}
LARICE = {"source_row": 1975, "ean": "8009496220932", "description": "ASS. LINDA SETAMORBI X 18 LUNGO",
          "unit_price_net": 2.25, "pieces_per_carton": 12, "usable": True}


def candidato(riga: dict[str, Any], punteggio: float) -> dict[str, Any]:
    return {**{chiave: riga[chiave] for chiave in ("source_row", "ean", "description", "unit_price_net")},
            "score": punteggio}


def scrivi(cartella: Path, nome: str, documento: Any) -> Path:
    percorso = cartella / nome
    percorso.write_text(json.dumps(documento, ensure_ascii=False), encoding="utf-8")
    return percorso


def confronto(cartella: Path, run_id: str) -> dict[str, Any]:
    """Merge e `build_review_data` veri, lanciati come li lancia la catena."""

    cartella.mkdir(parents=True, exist_ok=True)
    matching = [{"gestionale": GESTIONALE, "suppliers": {
        "noce": {"status": "EAN_ASSENTE", "usable_candidates": []},
        "larice": {"status": "EAN_ASSENTE", "usable_candidates": []},
    }}]
    shortlists = [
        {"gestionale_source_row": 273, "supplier": "noce", "description": GESTIONALE["description"],
         "candidates": [candidato(NOCE, 0.84)]},
        {"gestionale_source_row": 273, "supplier": "larice", "description": GESTIONALE["description"],
         "candidates": [candidato(LARICE, 0.54)]},
    ]
    decisioni = [
        {"gestionale_source_row": 273, "supplier": "noce", "action": "ACCEPT", "source_row": 7463,
         "confidence": "ALTA", "rationale": "Stesso prodotto."},
        {"gestionale_source_row": 273, "supplier": "larice", "action": "REJECT", "confidence": "ALTA",
         "rationale": "Formato non chiaro."},
    ]
    for decisione in decisioni:
        voce = next(item for item in shortlists if item["supplier"] == decisione["supplier"])
        decisione["ai_impronta_caso"] = impronta_attesa(voce)
    for comando in (
        ["merge_match_decisions.py",
         "--matching", str(scrivi(cartella, "matching.json", matching)),
         "--normalized", str(scrivi(cartella, "normalized.json", {"noce": [NOCE], "larice": [LARICE]})),
         "--shortlists", str(scrivi(cartella, "shortlists.json", shortlists)),
         "--decisions", str(scrivi(cartella, "decisions.json", decisioni)),
         "--decisions-attese", "2",
         "--output", str(cartella / "resolved.json")],
        ["build_review_data.py", "--resolved", str(cartella / "resolved.json"),
         "--output", str(cartella / "review_data.json"), "--run-id", run_id, "--threshold", "0"],
    ):
        esito = subprocess.run([sys.executable, str(RADICE / "scripts" / comando[0]), *comando[1:]],
                               capture_output=True, text=True, encoding="utf-8")
        if esito.returncode != 0:
            raise AssertionError(f"{comando[0]} è fallito: {esito.stdout}{esito.stderr}")
    return json.loads((cartella / "review_data.json").read_text(encoding="utf-8"))


class LaStessaRigaVistaDaPapaTests(unittest.TestCase):
    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.root = Path(temporanea.name)
        self.conferme_path = self.root / "history" / "conferme.db"

    def negozio(self, run_id: str) -> ReviewStore:
        review = confronto(self.root / run_id, run_id)
        percorso = self.root / "review_data.json"
        percorso.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        store = ReviewStore(percorso, self.root / f"stato-{run_id}.json", self.root / "uploads",
                            self.root / "outputs", conferme_path=self.conferme_path)
        self.addCleanup(store.chiudi)
        return store

    @staticmethod
    def prodotto(store: ReviewStore) -> dict[str, Any]:
        return store.review()["products"][0]

    @staticmethod
    def offerta(prodotto: dict[str, Any], fornitore: str) -> dict[str, Any]:
        return next(offerta for offerta in prodotto["offers"] if offerta["supplierId"] == fornitore)

    def test_la_riga_larice_vince_e_chiede_conferma_dicendo_perche(self) -> None:
        prodotto = self.prodotto(self.negozio("r1"))

        self.assertEqual(prodotto["selectedSupplierId"], "larice")
        self.assertTrue(prodotto["requiresConfirmation"])
        self.assertFalse(prodotto["confirmed"])
        self.assertIn("NOCE", prodotto["confirmationMessage"])
        self.assertIn("8009496220932", prodotto["confirmationMessage"])
        larice = self.offerta(prodotto, "larice")
        self.assertEqual(larice["method"], METODO_STESSO_CODICE)
        self.assertEqual(larice["unitPriceNet"], 2.25)

    def test_il_no_spegne_la_riga_e_resta_al_confronto_dopo(self) -> None:
        store = self.negozio("r1")
        store.rifiuta_l_abbinamento({"runId": "r1", "productId": "product:273",
                                     "supplierId": "larice", "rifiutata": True})
        store.chiudi()

        prodotto = self.prodotto(self.negozio("r2"))

        larice = self.offerta(prodotto, "larice")
        self.assertFalse(larice["available"])
        self.assertEqual(larice["status"], "RIFIUTATO_UTENTE")
        self.assertTrue(self.offerta(prodotto, "noce")["available"])

    def test_dopo_il_no_anche_noce_chiede_conferma(self) -> None:
        """La riga rifiutata e quella NOCE hanno lo stesso codice: se la
        prima non e' le Lines, la seconda e' sospetta. NOCE era un `ALTA`,
        che da solo non chiederebbe niente: la domanda la mette il servizio
        sull'**offerta** (`_riscegli_dopo_il_no`), cosi' vale per qualunque
        strada la faccia scegliere."""

        store = self.negozio("r1")
        store.rifiuta_l_abbinamento({"runId": "r1", "productId": "product:273",
                                     "supplierId": "larice", "rifiutata": True})

        prodotto = self.prodotto(store)

        noce = self.offerta(prodotto, "noce")
        self.assertTrue(noce["requiresConfirmation"])
        self.assertIn("LARICE non è questo articolo", noce["confirmationMessage"])
        self.assertIn("8009496220932", noce["confirmationMessage"])
        _pulito, errori, _review = store.validate_snapshot({
            "runId": "r1",
            "products": [{"id": "product:273", "quantity": 1, "selectedSupplierId": "noce",
                          "confirmed": False, "excluded": False}],
        }, for_compile=True)
        self.assertIn("CONFERMA_MANCANTE", [voce["code"] for voce in errori])

    def test_dopo_il_no_il_prodotto_passa_a_noce(self) -> None:
        store = self.negozio("r1")
        store.rifiuta_l_abbinamento({"runId": "r1", "productId": "product:273",
                                     "supplierId": "larice", "rifiutata": True})

        prodotto = self.prodotto(store)

        self.assertEqual(prodotto["selectedSupplierId"], "noce")
        # La domanda di prodotto era della riga spenta: adesso la fa l'offerta.
        self.assertFalse(prodotto["requiresConfirmation"])

    def test_il_si_su_noce_dopo_il_no_vale_anche_la_settimana_dopo(self) -> None:
        """Trovato dalla revisione del 21 settembre 2026: la settimana dopo il
        confronto nasceva ancora su LARICE (la catena i no non li conosce), il
        si' si cercava su LARICE e quello dato su NOCE non si riapplicava
        mai. Una domanda bloccante ogni settimana."""

        store = self.negozio("r1")
        store.rifiuta_l_abbinamento({"runId": "r1", "productId": "product:273",
                                     "supplierId": "larice", "rifiutata": True})
        store.save_state({
            "runId": "r1", "currentStep": 2, "acceptBelowThreshold": True,
            "products": [{"id": "product:273", "quantity": 1, "selectedSupplierId": "noce", "confirmed": True}],
        })
        store.chiudi()

        store = self.negozio("r2")
        prodotto = self.prodotto(store)

        self.assertEqual(prodotto["selectedSupplierId"], "noce")
        self.assertTrue(prodotto["confirmed"])
        _pulito, errori, _review = store.validate_snapshot({
            "runId": "r2",
            "products": [{"id": "product:273", "quantity": 1, "selectedSupplierId": "noce",
                          "confirmed": True, "excluded": False}],
        }, for_compile=True)
        self.assertNotIn("CONFERMA_MANCANTE", [voce["code"] for voce in errori])

    def test_il_si_si_ricorda_e_al_confronto_dopo_nasce_confermato(self) -> None:
        store = self.negozio("r1")
        store.save_state({
            "runId": "r1", "currentStep": 2, "acceptBelowThreshold": True,
            "products": [{"id": "product:273", "quantity": 1, "selectedSupplierId": "larice", "confirmed": True}],
        })
        store.chiudi()
        magazzino = MagazzinoConferme(self.conferme_path)
        try:
            conferme = magazzino.esporta()
        finally:
            magazzino.chiudi()
        self.assertEqual(
            [(voce.get("fornitore"), voce.get("accettata"), voce.get("motivo")) for voce in conferme],
            [("larice", True, METODO_STESSO_CODICE)],
        )

        prodotto = self.prodotto(self.negozio("r2"))

        self.assertEqual(prodotto["selectedSupplierId"], "larice")
        self.assertTrue(prodotto["confirmed"])



class UnNoSullaRigaSceltaRifaLaSceltaTests(unittest.TestCase):
    """Il caso generale, senza propagazione: una proposta dell'AI rifiutata,
    e un altro fornitore che ha il prodotto per codice a barre. La domanda
    della riga rifiutata non deve restare appesa a quella nuova."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        radice = Path(temporanea.name)
        offerta = lambda fornitore, ean, prezzo, chiede: {  # noqa: E731
            "supplierId": fornitore, "supplierName": fornitore.upper(), "available": True,
            "status": "SEMANTICO_PROPOSTO" if chiede else "EAN_ESATTO",
            "matchStatus": "SEMANTICO_PROPOSTO" if chiede else "EAN_ESATTO",
            "requiresConfirmation": chiede, "confirmed": not chiede, "rationale": "Proposta dall'analisi.",
            "description": f"RIGA {fornitore.upper()}", "ean": ean, "sourceRow": 10,
            "unitPriceNet": prezzo, "orderUnitPriceNet": prezzo * 6, "quantityFactor": 6,
        }
        confronto_ = {
            "run": {"id": "r1"}, "warnings": [], "files": [],
            "suppliers": [{"id": "larice", "name": "LARICE", "minimumOrder": 0},
                          {"id": "betulla", "name": "BETULLA", "minimumOrder": 0}],
            "products": [{
                "id": "product:410", "kind": "PRODUCT", "itemType": "product", "ean": "8009160250531",
                "name": "CERA DI LUNAR", "description": "CERA DI LUNAR", "quantity": 3,
                "selectedSupplierId": "larice", "confirmed": False, "requiresConfirmation": True,
                "confirmationMessage": "Proposta dall'analisi.", "components": [], "warnings": [],
                "offers": [offerta("larice", "8009553130211", 2.1, True),
                           offerta("betulla", "8009160250531", 2.4, False)],
            }],
        }
        percorso = radice / "review_data.json"
        percorso.write_text(json.dumps(confronto_), encoding="utf-8")
        self.store = ReviewStore(percorso, radice / "state.json", radice / "uploads", radice / "out",
                                 conferme_path=radice / "history" / "conferme.db")
        self.addCleanup(self.store.chiudi)

    def test_la_riga_esatta_non_eredita_la_domanda(self) -> None:
        """E non si prende la frase sullo «stesso codice», che per lei e' falsa."""

        self.store.rifiuta_l_abbinamento({"runId": "r1", "productId": "product:410",
                                          "supplierId": "larice", "rifiutata": True})

        prodotto = self.store.review()["products"][0]

        self.assertEqual(prodotto["selectedSupplierId"], "betulla")
        self.assertFalse(prodotto["requiresConfirmation"])
        self.assertEqual(prodotto["confirmationMessage"], "")
        betulla = next(offerta for offerta in prodotto["offers"] if offerta["supplierId"] == "betulla")
        self.assertFalse(betulla["requiresConfirmation"])
        self.assertNotIn("stesso codice", str(betulla.get("confirmationMessage") or ""))


class LaRigaColCodiceRifiutatoChiedeSempreTests(unittest.TestCase):
    """Trovato dalla verifica avversariale del 21 settembre 2026. Tre fornitori:
    LARICE propagato (2,25, codice X), NOCE accettato `ALTA` (2,31, codice
    X), BETULLA col codice del gestionale (2,28). Dopo il no su LARICE la domanda
    stava sul prodotto, calcolata sul ripiego senza sconti (BETULLA): con uno
    sconto su NOCE, o spostandolo a mano, la riga col codice appena
    rifiutato andava in ordine senza conferma."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        radice = Path(temporanea.name)

        def offerta(fornitore: str, ean: str, prezzo: float, metodo: str, chiede: bool) -> dict[str, Any]:
            return {
                "supplierId": fornitore, "supplierName": fornitore.upper(), "available": True,
                "status": "EAN_ESATTO" if metodo == "EAN" else "SEMANTICO_PROPOSTO",
                "matchStatus": "EAN_ESATTO" if metodo == "EAN" else "SEMANTICO_PROPOSTO",
                "method": metodo, "requiresConfirmation": chiede, "confirmed": not chiede,
                "rationale": "", "description": f"LINDA {fornitore.upper()}", "ean": ean, "sourceRow": 10,
                "unitPriceNet": prezzo, "pricePerPiece": prezzo, "orderUnitPriceNet": round(prezzo * 12, 4),
                "quantityFactor": 12,
            }

        confronto_ = {
            "run": {"id": "r1"}, "warnings": [], "files": [],
            "suppliers": [{"id": chi, "name": chi.upper(), "minimumOrder": 0} for chi in ("larice", "noce", "betulla")],
            "products": [{
                "id": "product:273", "kind": "PRODUCT", "itemType": "product", "ean": "8009405394204",
                "name": "LINDA SETA ULTRA LUNGO ALI 18PZ", "description": "LINDA SETA ULTRA LUNGO ALI 18PZ",
                "quantity": 1, "selectedSupplierId": "larice", "confirmed": False, "requiresConfirmation": True,
                "confirmationMessage": "NOCE ha questo prodotto con il codice a barre 8009496220932.",
                "components": [], "warnings": [],
                "offers": [
                    offerta("larice", "8009496220932", 2.25, "EAN_DA_ALTRO_FORNITORE", True),
                    offerta("noce", "8009496220932", 2.31, "SEMANTICO_AI", False),
                    offerta("betulla", "8009405394204", 2.28, "EAN", False),
                ],
            }],
        }
        percorso = radice / "review_data.json"
        percorso.write_text(json.dumps(confronto_), encoding="utf-8")
        self.store = ReviewStore(percorso, radice / "state.json", radice / "uploads", radice / "out",
                                 conferme_path=radice / "history" / "conferme.db")
        self.addCleanup(self.store.chiudi)
        self.store.rifiuta_l_abbinamento({"runId": "r1", "productId": "product:273",
                                          "supplierId": "larice", "rifiutata": True})

    def errori_con(self, fornitore: str) -> list[str]:
        _pulito, errori, _review = self.store.validate_snapshot({
            "runId": "r1",
            "products": [{"id": "product:273", "quantity": 1, "selectedSupplierId": fornitore,
                          "confirmed": False, "excluded": False}],
        }, for_compile=True)
        return [voce["code"] for voce in errori]

    def test_noce_scelto_a_mano_chiede_conferma(self) -> None:
        self.assertIn("CONFERMA_MANCANTE", self.errori_con("noce"))

    def test_betulla_col_codice_del_gestionale_non_chiede_niente(self) -> None:
        self.assertEqual(self.errori_con("betulla"), [])

    def test_con_lo_sconto_su_noce_chiede_lo_stesso(self) -> None:
        self.store.set_supplier_discount({"runId": "r1", "supplierId": "noce", "percent": 2})

        prodotto = self.store.review()["products"][0]

        self.assertEqual(prodotto["selectedSupplierId"], "noce")
        self.assertIn("CONFERMA_MANCANTE", self.errori_con("noce"))


if __name__ == "__main__":
    unittest.main()
