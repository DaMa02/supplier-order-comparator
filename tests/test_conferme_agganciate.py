#!/usr/bin/env python3
"""Tests for the confirmation store wired into the service.

Before this wiring, confirmations lived only inside `state.json`, keyed to
the product's row number in the management-software export. Measured on real
exports: of the 457 ids present in both, 449 carry a different item. An
answer given one week would either get lost, or worse, reapply to an item
that was never actually confirmed.

This module tests exactly what the wiring has to guarantee, nothing more:

1. answering "yes" writes to the store;
2. the answer is keyed to the item, so it survives a new export and a new row
   number;
3. a price-list row carrying a different item inherits nothing;
4. unchecking a confirmation revokes it, while an expiry does not — the page
   sends both as the same `confirmed: false` payload;
5. a store that fails to open does not block saving, and says so.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import server as SERVER  # noqa: E402
from conferme import MagazzinoConferme, MagazzinoNonUtilizzabile, impronta_prodotto  # noqa: E402
from server import ReviewStore  # noqa: E402


def offerta(supplier: str, *, ean: str, descrizione: str, riga: int, prezzo: float = 12.0) -> dict[str, Any]:
    """An offer that requires confirmation: the case the store exists for."""

    return {
        "supplierId": supplier,
        "available": True,
        "description": descrizione,
        "sourceRow": riga,
        "ean": ean,
        "unitPriceNet": round(prezzo / 6, 6),
        "quantityFactor": 6,
        "orderUnitPriceNet": prezzo,
        "method": "NOME",
        "confidence": "MEDIA",
        "requiresConfirmation": True,
    }


def prodotto(product_id: str, nome: str, ean: str, offerte: list[dict[str, Any]], riga: int = 10) -> dict[str, Any]:
    return {
        "id": product_id,
        "kind": "PRODUCT",
        "itemType": "product",
        "sourceRow": riga,
        "ean": ean,
        "name": nome,
        "description": nome,
        "quantity": 0,
        "selectedSupplierId": offerte[0]["supplierId"] if offerte else "",
        "confirmed": False,
        "requiresConfirmation": False,
        "offers": offerte,
        "components": [],
    }


def confronto(prodotti: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run": {"id": "run-sintetica", "status": "ready", "label": "Prova"},
        "files": [],
        "suppliers": [{"id": "larice", "name": "Larice", "minimumOrder": 0}],
        "products": prodotti,
        "warnings": [],
    }


class BancoDelleConferme(unittest.TestCase):
    """A real store instance, with the confirmation store in its own folder."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.root = Path(temporanea.name)
        self.run_dir = self.root / "run-corrente"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.review_path = self.root / "review_data.json"
        self.conferme_path = self.root / "history" / "conferme.db"

    def negozio(self, review: dict[str, Any]) -> ReviewStore:
        self.review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        store = ReviewStore(
            self.review_path,
            self.run_dir / "review_state.json",
            self.root / "uploads",
            self.root / "outputs",
            conferme_path=self.conferme_path,
        )
        self.addCleanup(self.chiudi, store)
        return store

    @staticmethod
    def chiudi(store: ReviewStore) -> None:
        magazzino = getattr(store, "_conferme", None)
        if magazzino is not None:
            magazzino.chiudi()

    @staticmethod
    def scelte(product_id: str, *, fornitore: str, quantita: int, confermato: bool,
               versione: int | None = None) -> dict[str, Any]:
        istantanea: dict[str, Any] = {
            "runId": "run-sintetica",
            "currentStep": 2,
            "acceptBelowThreshold": True,
            "products": [{
                "id": product_id,
                "quantity": quantita,
                "selectedSupplierId": fornitore,
                "confirmed": confermato,
            }],
        }
        if versione is not None:
            istantanea["stateVersion"] = versione
        return istantanea

    def conferme(self) -> list[dict[str, Any]]:
        magazzino = MagazzinoConferme(self.conferme_path)
        try:
            return magazzino.esporta()
        finally:
            magazzino.chiudi()


class RispondereSiSiScriveTests(BancoDelleConferme):
    def test_la_conferma_finisce_nel_magazzino_con_la_sua_data(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO MAR BLUE 3X80", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO MAR BLUE GR.80 X3", riga=42)]),
        ]))

        esito = store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        righe = self.conferme()
        self.assertEqual(len(righe), 1, righe)
        self.assertTrue(righe[0]["accettata"])
        self.assertTrue(righe[0]["in_vigore"])
        self.assertEqual(righe[0]["fornitore"], "larice")
        self.assertEqual(righe[0]["valida_dal"], esito["savedAt"])

    def test_lo_stesso_salvataggio_ripetuto_non_riempie_lo_storico(self) -> None:
        """The page autosaves every 450 ms.

        `ricorda` doesn't rewrite an identical answer, which is what makes it
        safe to call on every save without special-casing: otherwise, sitting
        on the page would write hundreds of history rows describing decisions
        that were never made.
        """

        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))

        for versione in (0, 1, 2):
            store.save_state(self.scelte(
                "product:3", fornitore="larice", quantita=2, confermato=True, versione=versione
            ))

        self.assertEqual(len(self.conferme()), 1)

    def test_senza_conferma_non_si_scrive_niente(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))

        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=False))

        self.assertEqual(self.conferme(), [])

    def test_un_abbinamento_certo_non_diventa_una_conferma(self) -> None:
        """The filter that keeps the store readable.

        The comparison marks `confirmed` even on matches nobody had to
        confirm (the large majority), and the page sends them back that way.
        Without this filter, a 450-row order would write 450 "answers" that
        were never actually given, burying the few real ones.
        """

        certa = offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)
        certa["requiresConfirmation"] = False
        certa["confidence"] = "CERTA"
        certa["method"] = "EAN"
        store = self.negozio(confronto([prodotto("product:3", "TONNO", "8000000000010", [certa])]))

        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        self.assertEqual(self.conferme(), [])


class LaConfermaSopravviveAllExportNuovoTests(BancoDelleConferme):
    """The whole point of the store, tested on the real case.

    The next week's management-software export is a different file: the same
    item sits on a different row, so it gets a different id and its decision
    would otherwise not exist. Before this store, the question came back
    every week; now it doesn't.
    """

    ARTICOLO = ("TONNO MAR BLUE 3X80", "8000000000010")
    OFFERTA = dict(ean="8000000000010", descrizione="TONNO MAR BLUE GR.80 X3", riga=42)

    def settimana_prima(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", *self.ARTICOLO, [offerta("larice", **self.OFFERTA)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))
        self.chiudi(store)
        store._conferme = None
        # A new export deletes the state: that's what a recompute does.
        store.state_path.unlink()

    def test_la_riga_nuova_dello_stesso_articolo_e_gia_confermata(self) -> None:
        self.settimana_prima()

        # Same item, different row number in the management software and a
        # different price-list row: identity is barcode plus name, not row.
        store = self.negozio(confronto([
            prodotto("product:517", *self.ARTICOLO, [offerta("larice", **self.OFFERTA)], riga=517),
        ]))
        prodotti = store.review()["products"]

        self.assertTrue(prodotti[0]["confirmed"])
        self.assertEqual(prodotti[0]["confirmation"]["supplierId"], "larice")
        self.assertTrue(prodotti[0]["confirmation"]["since"])

    def test_e_la_compilazione_non_richiede_di_riconfermare(self) -> None:
        """The full chain, as the page actually walks it.

        The store seeds `product.confirmed` in `review()`; `snapshot()` sends
        that same field back unchanged (`app.js:1586`), and the compile gate
        reads the snapshot. The store is deliberately not consulted again on
        save: if it were, a checkbox the user just unchecked would be put back
        by yesterday's memory in the same save that removed it, meaning
        revocation couldn't work. This test walks the real chain instead of
        stubbing it out.
        """

        self.settimana_prima()
        store = self.negozio(confronto([
            prodotto("product:517", *self.ARTICOLO, [offerta("larice", **self.OFFERTA)], riga=517),
        ]))

        letto = next(voce for voce in store.review()["products"] if voce["id"] == "product:517")
        clean, errori, _confronto = store.validate_snapshot(
            self.scelte(
                "product:517", fornitore="larice", quantita=2,
                confermato=bool(letto["confirmed"]),
            ),
            for_compile=True,
        )

        self.assertEqual([voce["code"] for voce in errori if voce["code"] == "CONFERMA_MANCANTE"], [])
        self.assertTrue(clean["products"][0]["confirmed"])

    def test_una_riga_di_listino_con_un_altro_articolo_non_eredita_niente(self) -> None:
        """The boundary that makes the store safe.

        A confirmation is valid for the match "this item of mine to that
        supplier row". If the price-list row now carries a different item,
        the previous answer doesn't cover it and the question comes back:
        losing a confirmation costs a click, applying a wrong one costs an
        order.
        """

        self.settimana_prima()
        store = self.negozio(confronto([
            prodotto("product:517", *self.ARTICOLO, [
                offerta("larice", ean="8000000000099", descrizione="TONNO MAR BLUE OLIO OLIVA", riga=51),
            ], riga=517),
        ]))

        prodotti = store.review()["products"]
        _clean, errori, _confronto = store.validate_snapshot(
            self.scelte("product:517", fornitore="larice", quantita=2, confermato=False),
            for_compile=True,
        )

        self.assertFalse(prodotti[0]["confirmed"])
        self.assertNotIn("confirmation", prodotti[0])
        self.assertIn("CONFERMA_MANCANTE", [voce["code"] for voce in errori])


class RevocareSiPuoScadereNoTests(BancoDelleConferme):
    """The two cases the page sends as the same payload.

    `confirmed: false` arrives both when the user unchecks the box and when a
    recompute expired the confirmation because the row now carries a
    different item. Treating them the same way would empty the store right
    when it's supposed to help.
    """

    ARTICOLO = ("TONNO MAR BLUE 3X80", "8000000000010")
    OFFERTA = dict(ean="8000000000010", descrizione="TONNO MAR BLUE GR.80 X3", riga=42)

    def setUp(self) -> None:
        super().setUp()
        self.store = self.negozio(confronto([
            prodotto("product:3", *self.ARTICOLO, [offerta("larice", **self.OFFERTA)]),
        ]))
        self.store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

    def test_togliere_la_spunta_toglie_la_conferma(self) -> None:
        versione = json.loads(self.store.state_path.read_text(encoding="utf-8"))["stateVersion"]

        self.store.save_state(self.scelte(
            "product:3", fornitore="larice", quantita=2, confermato=False, versione=versione
        ))

        righe = self.conferme()
        self.assertEqual(len(righe), 1, "la riga non si cancella: si chiude, e resta nell'audit")
        self.assertFalse(righe[0]["in_vigore"])
        self.assertTrue(righe[0]["valida_fino_a"])

    def test_una_conferma_scaduta_dal_ricalcolo_resta_in_vigore(self) -> None:
        """The case a regression here would silently break.

        The state arrives without `confirmedArticle` (what a recompute leaves
        behind when it expires a confirmation) and with `confirmed: false`.
        The user hasn't changed their mind, so the store must not be touched.
        """

        stato = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        for decisione in stato["products"]:
            decisione["confirmed"] = False
            decisione.pop("confirmedArticle", None)
        self.store.state_path.write_text(json.dumps(stato), encoding="utf-8")

        self.store.save_state(self.scelte(
            "product:3", fornitore="larice", quantita=2, confermato=False,
            versione=stato["stateVersion"],
        ))

        righe = self.conferme()
        self.assertEqual(len(righe), 1)
        self.assertTrue(righe[0]["in_vigore"], "una scadenza non è una revoca")


class UnMagazzinoRottoNonFermaIlProgrammaTests(BancoDelleConferme):
    """The file won't open: the app keeps going without memory, and says so.

    Silently reporting "no confirmations" would bring back every question
    with no explanation, and the user would redo them by hand assuming the
    app had never known anything. Saving must not be blocked either: a failed
    `PUT /api/state` wouldn't just lose one answer, it would lose every one
    that comes after.
    """

    def negozio_rotto(self) -> ReviewStore:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))
        self.rotto = mock.patch.object(
            SERVER, "MagazzinoConferme",
            side_effect=MagazzinoNonUtilizzabile("Il file delle conferme non si apre: disco pieno"),
        )
        self.rotto.start()
        self.addCleanup(self.rotto.stop)
        return store

    def test_il_salvataggio_riesce_lo_stesso(self) -> None:
        store = self.negozio_rotto()

        esito = store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        self.assertTrue(esito["ok"])
        self.assertTrue(esito["savedAt"])

    def test_e_il_confronto_lo_dice(self) -> None:
        store = self.negozio_rotto()
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        avvisi = store.review().get("warnings") or []

        voce = next((item for item in avvisi if item.get("code") == "CONFERME_NON_DISPONIBILI"), None)
        self.assertIsNotNone(voce, avvisi)
        self.assertIn("disco pieno", voce["message"])
        self.assertFalse(voce["blocking"])

    def test_il_file_non_si_riapre_a_ogni_richiesta(self) -> None:
        """A failure is reported once; retrying per product would cost one
        open attempt per comparison row."""

        store = self.negozio_rotto()
        store.review()
        store.review()

        self.assertEqual(SERVER.MagazzinoConferme.call_count, 1)


class IlFileSiCreaSoloSeServeESiRestituisceTests(BancoDelleConferme):
    """Two safeguards that only surfaced by running the whole suite.

    SQLite keeps the file open for as long as the connection lives, and on
    Windows an open file can't be deleted or renamed, including its parent
    folder. In an earlier version of this wiring, the store opened on every
    save, even with nothing to remember: dozens of unrelated tests failed at
    temp-folder cleanup with a file-in-use error, and no single targeted test
    caught it.
    """

    def test_un_programma_senza_conferme_non_crea_nessun_file(self) -> None:
        certa = offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)
        certa["requiresConfirmation"] = False
        store = self.negozio(confronto([prodotto("product:3", "TONNO", "8000000000010", [certa])]))

        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))
        store.review()

        self.assertFalse(self.conferme_path.exists(), "un file che non serve non si crea")

    def test_il_negozio_restituisce_il_file_quando_glielo_si_chiede(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))
        self.assertTrue(self.conferme_path.exists())

        store.chiudi()

        # The file can be moved: on Windows, that's proof nothing holds it open.
        spostato = self.conferme_path.with_suffix(".db.spostato")
        self.conferme_path.rename(spostato)
        self.assertTrue(spostato.is_file())
        # The store reopens on the next request, without complaint.
        store.save_state(self.scelte(
            "product:3", fornitore="larice", quantita=3, confermato=True,
            versione=json.loads(store.state_path.read_text(encoding="utf-8"))["stateVersion"],
        ))
        self.assertTrue(self.conferme_path.exists())

    def test_chiuderlo_due_volte_non_fa_danno(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        store.chiudi()
        store.chiudi()


class LeConfermeSiScaricanoTests(BancoDelleConferme):
    """`conferme.db` is the app's permanent memory, and it isn't human-readable.

    `MagazzinoConferme.esporta` existed from the start for exactly that
    reason, but nothing outside the tests called it. Hence the export from
    Settings.
    """

    def test_quello_che_si_scarica_e_quello_che_c_e_nel_magazzino(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000011", descrizione="TONNO MAR BLUE", riga=42)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        scaricate, uguaglianze = store.esporta_le_conferme()

        self.assertEqual(scaricate, self.conferme())
        self.assertEqual(uguaglianze, [])
        self.assertEqual(scaricate[0]["articolo"], "8000000000010|TONNO")
        self.assertTrue(scaricate[0]["accettata"])
        self.assertEqual(len(scaricate), 1)
        # What was confirmed, not just that something was: without these two
        # fields the export wouldn't let anyone judge anything.
        self.assertIn("fornitore", scaricate[0])
        self.assertIn("articolo", scaricate[0])

    def test_su_un_programma_senza_conferme_si_scarica_un_elenco_vuoto(self) -> None:
        """And no `conferme.db` gets created: opening Settings on a fresh
        install must not create the confirmation store. SQLite keeps the
        file open for as long as the connection lives, and on Windows an
        open file locks its parent folder."""

        store = self.negozio(confronto([]))

        self.assertEqual(store.esporta_le_conferme(), ([], []))
        self.assertFalse(self.conferme_path.exists(), "un file che non serve non si crea")

    def test_un_file_che_non_e_un_database_non_fa_fallire_lo_scarico(self) -> None:
        """Same as for equalities: the reason already surfaces among the
        comparison warnings, and blocking the whole page over one missing
        memory would be disproportionate."""

        self.conferme_path.parent.mkdir(parents=True, exist_ok=True)
        self.conferme_path.write_bytes(b"questo non e' un database SQLite")
        store = self.negozio(confronto([]))

        self.assertEqual(store.esporta_le_conferme(), ([], []))

    def test_un_magazzino_che_si_rompe_mentre_lo_si_legge_dice_perche(self) -> None:
        """The other failure mode: the file opens but the read then fails.
        The reason isn't discarded; it ends up among the comparison
        warnings, same as for equalities."""

        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000011", descrizione="TONNO MAR BLUE", riga=42)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))
        magazzino = store.magazzino_conferme()
        self.assertIsNotNone(magazzino)

        def non_si_legge():
            raise MagazzinoNonUtilizzabile("Il file delle conferme non si legge più: disco assente")

        magazzino.esporta = non_si_legge

        self.assertEqual(store.esporta_le_conferme(), ([], []))
        self.assertIn("non si legge", store._conferme_guasto)


class LImprontaDelProdottoEQuellaDelModuloTests(BancoDelleConferme):
    """The service doesn't recompute item identity on its own.

    Two definitions of "same item" would be two sources of truth over the
    same data, and the day one changes, the store would silently stop matching.
    """

    def test_la_chiave_scritta_e_quella_che_dichiara_conferme_py(self) -> None:
        articolo = prodotto("product:3", "TONNO MAR BLUE 3X80", "8000000000010",
                            [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)])
        store = self.negozio(confronto([articolo]))

        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        self.assertEqual(self.conferme()[0]["articolo"], impronta_prodotto(articolo))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
