#!/usr/bin/env python3
"""Tests for "this price-list row is my product": one click, two effects.

A real case: `LUXA SAPONE LIQ. EROG.250ML` sits in the management-software
export under EAN 4009428623194, which only CIPRESSO uses, at 1.28 EUR/pc.
The same item sits on NOCE's price list under 8729721830575, at 1.15. The
management-software name doesn't say which variant it is (`EROG.` just means
"dispenser"; only the buyer knows whether it's `ORIGINAL` or `SETA`), so no
score can infer it, and the correct row ranked only second in the shortlist.

This file tests what a manual choice by the user must do:

1. Immediately: the chosen offer enters the current comparison.
2. Permanently: the two barcodes stay declared as the same item, so the next
   run matches them on their own, across every supplier, not just the one
   chosen.
3. When one of the two EANs is missing, the permanent effect is not
   possible, and the response must say so rather than imply it happened.
4. A previous "no" on the automatic match must not suppress an offer just
   chosen manually.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import server as server_module  # noqa: E402
from server import ReviewStore  # noqa: E402

EAN_GESTIONALE = "4009428623194"
EAN_NOCE = "8729721830575"

RIGHE_NOCE = [
    {"source_row": 4793, "ean": "8729014462339", "description": "LUXA SAPONE EROGATORE GO FRESH ML.250",
     "pieces_per_carton": "6", "unit_price_net": "1.1500", "usable": True},
    {"source_row": 4794, "ean": EAN_NOCE, "description": "LUXA SAPONE EROGATORE ORIGINAL ML.250",
     "pieces_per_carton": "6", "unit_price_net": "1.1500", "usable": True, "supplier_code": "0000000429063"},
    {"source_row": 5000, "ean": "", "description": "LISTINO", "usable": True},
]


class CatalogoFinto:
    """Fake catalog: rows are declared inline instead of read from disk.

    Exposes only what the service actually calls, and delegates to the real
    `_offer` function so this fake doesn't reimplement the orderability rule
    and end up testing its own logic instead of the module's.
    """

    def __init__(self, righe: dict[str, list[dict[str, Any]]]) -> None:
        self.righe_per_fornitore = righe
        self.load_errors: list[dict[str, str]] = []

    def enrich_review(self, review: dict[str, Any]) -> dict[str, Any]:
        return review

    def invalidate(self) -> None:
        return None

    def offerta_dalla_riga(self, review: dict[str, Any], supplier: str, source_row: Any):
        import catalog_search

        chiave = str(supplier or "").strip().casefold()
        record = next(
            (voce for voce in self.righe_per_fornitore.get(chiave, [])
             if str(voce.get("source_row")) == str(source_row)),
            None,
        )
        if record is None:
            raise ValueError(f"Nel listino {chiave} non c'è nessuna riga {source_row}")
        offerta = catalog_search._offer(chiave, record)
        if offerta is None:
            raise ValueError(f"La riga {source_row} di {chiave} non è ordinabile")
        return dict(record), offerta

    def fornitori_sfogliabili(self, review: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"id": nome, "name": nome.upper(), "righe": len(righe), "ordinabili": len(righe)}
                for nome, righe in sorted(self.righe_per_fornitore.items())]


def confronto(*, ean_prodotto: str = EAN_GESTIONALE) -> dict[str, Any]:
    return {
        "run": {"id": "run-prova", "status": "ready", "label": "Prova"},
        "files": [],
        "suppliers": [
            {"id": "noce", "name": "NOCE", "minimumOrder": 0},
            {"id": "cipresso", "name": "CIPRESSO", "minimumOrder": 0},
        ],
        "products": [{
            "id": "product:330",
            "itemType": "product",
            "ean": ean_prodotto,
            "name": "LUXA SAPONE LIQ. EROG.250ML",
            "description": "LUXA SAPONE LIQ. EROG.250ML",
            "quantity": 1,
            "selectedSupplierId": "cipresso",
            "offers": [
                {"supplierId": "cipresso", "available": True, "description": "LUXA SAPONE LIQUIDO EROGATORE 250",
                 "ean": ean_prodotto, "sourceRow": 900, "unitPriceNet": 1.28, "quantityFactor": 6,
                 "orderUnitPriceNet": 7.68},
                {"supplierId": "noce", "available": False, "status": "NON_TROVATO"},
            ],
            "components": [],
        }],
        "warnings": [],
    }


class BancoDellAbbinamento(unittest.TestCase):
    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.root = Path(temporanea.name)
        self.review_path = self.root / "review_data.json"

    def negozio(self, review: dict[str, Any] | None = None, righe=None) -> ReviewStore:
        self.review_path.write_text(
            json.dumps(review or confronto(), ensure_ascii=False), encoding="utf-8",
        )
        store = ReviewStore(
            self.review_path,
            self.root / "state.json",
            self.root / "uploads",
            self.root / "outputs",
            conferme_path=self.root / "history" / "conferme.db",
        )
        store.catalog = CatalogoFinto(righe or {"noce": list(RIGHE_NOCE)})
        self.addCleanup(self.chiudi, store)
        return store

    @staticmethod
    def chiudi(store: ReviewStore) -> None:
        magazzino = getattr(store, "_conferme", None)
        if magazzino is not None:
            magazzino.chiudi()

    @staticmethod
    def offerte_disponibili(review: dict[str, Any]) -> dict[str, Any]:
        prodotto = review["products"][0]
        return {
            str(offerta.get("supplierId")): offerta.get("unitPriceNet")
            for offerta in prodotto.get("offers") or []
            if offerta.get("available")
        }


class SubitoNelConfrontoDiAdesso(BancoDellAbbinamento):
    def test_la_riga_scelta_diventa_un_offerta(self) -> None:
        store = self.negozio()
        self.assertEqual(self.offerte_disponibili(store.review()), {"cipresso": 1.28})

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["ok"], True)
        self.assertEqual(self.offerte_disponibili(store.review()), {"cipresso": 1.28, "noce": 1.15})

    def test_l_offerta_dice_di_essere_stata_scelta_a_mano(self) -> None:
        """Downstream code needs no special case, but the offer must still say
        it came from a human choice, not from a barcode match."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        offerta = next(
            voce for voce in store.review()["products"][0]["offers"]
            if voce["supplierId"] == "noce"
        )
        self.assertEqual(offerta["matchStatus"], "SCELTO_A_MANO")
        self.assertEqual(offerta["description"], "LUXA SAPONE EROGATORE ORIGINAL ML.250")
        self.assertIs(offerta["sceltaManuale"], True)

    def test_una_riga_che_non_si_puo_ordinare_non_si_abbina(self) -> None:
        """Row 5000 is a separator row: no price, no carton size. Ordering it
        would mean a quantity that can't be computed."""

        store = self.negozio()

        with self.assertRaises(ValueError):
            store.abbina_riga_di_listino({
                "productId": "product:330", "supplierId": "noce", "sourceRow": 5000,
            })

    def test_un_prodotto_che_non_c_e_non_si_abbina(self) -> None:
        store = self.negozio()

        with self.assertRaises(ValueError):
            store.abbina_riga_di_listino({
                "productId": "product:999", "supplierId": "noce", "sourceRow": 4794,
            })

    def test_scegliere_di_nuovo_sostituisce_invece_di_accumulare(self) -> None:
        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4793,
        })

        stato = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        scelte = [voce for voce in stato["manualMatches"] if voce["productId"] == "product:330"]
        self.assertEqual(len(scelte), 1)
        self.assertEqual(scelte[0]["sourceRow"], 4793)

    def test_un_no_dato_prima_non_spegne_l_offerta_appena_scelta(self) -> None:
        """Two human answers on the same product: the newest one wins, and the
        stale rejection is removed rather than left alongside it."""

        store = self.negozio()
        stato = {
            "schemaVersion": 1, "runId": "run-prova",
            "matchOverrides": [{
                "runId": "run-prova", "productId": "product:330", "supplierId": "noce",
                "candidateKey": "abc", "accepted": False,
            }],
        }
        (self.root / "state.json").write_text(json.dumps(stato), encoding="utf-8")

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertEqual(esito["rifiutiTolti"], 1)
        self.assertEqual(self.offerte_disponibili(store.review())["noce"], 1.15)


class PerSempreLUguaglianzaFraCodici(BancoDellAbbinamento):
    def test_i_due_codici_restano_dichiarati_uguali(self) -> None:
        store = self.negozio()

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["uguaglianzaRicordata"], True)
        self.assertEqual(store.uguaglianze_in_vigore(), [[EAN_GESTIONALE, EAN_NOCE]])
        self.assertIn("per tutti i fornitori", esito["message"])

    def test_la_dichiarazione_dice_da_dove_viene(self) -> None:
        """A wrong equivalence causes a wrong order on every run after it, so
        the record must keep saying what row was declared equivalent to what."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        voce = store.uguaglianze_dichiarate()[0]
        self.assertIn("riga 4794", voce["motivo"])
        self.assertIn("LUXA SAPONE EROGATORE ORIGINAL", voce["offerta"])
        self.assertTrue(voce["valida_dal"])

    def test_si_rilegge_in_impostazioni_e_si_toglie(self) -> None:
        """The equivalence list lives in Settings, with names and a search, not
        inside the comparison payload: two thirteen-digit codes with no names
        don't let anyone judge whether a declared equivalence is correct."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })
        self.assertNotIn("uguaglianze", store.review())
        self.assertEqual(len(store.elenco_delle_uguaglianze()["uguaglianze"]), 1)

        esito = store.togli_uguaglianza({"codici": [EAN_GESTIONALE, EAN_NOCE]})

        self.assertIs(esito["tolta"], True)
        self.assertEqual(store.uguaglianze_in_vigore(), [])
        self.assertEqual(store.elenco_delle_uguaglianze()["uguaglianze"], [])

    def test_l_elenco_porta_i_due_nomi_e_il_fornitore(self) -> None:
        """Codes alone don't let anyone judge whether a confirmed equivalence
        is correct; the list must also carry both item names and the supplier."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        voce = store.elenco_delle_uguaglianze()["uguaglianze"][0]

        self.assertEqual(voce["gestionale"]["codice"], EAN_GESTIONALE)
        self.assertIn("LUXA SAPONE LIQ", voce["gestionale"]["nome"])
        self.assertEqual(voce["listino"]["fornitoreId"], "noce")
        self.assertIn("LUXA SAPONE EROGATORE ORIGINAL", voce["listino"]["nome"])
        self.assertEqual(voce["listino"]["codice"], EAN_NOCE)
        self.assertTrue(voce["dal"])

    def test_la_ricerca_guarda_tutto_quello_che_si_vede(self) -> None:
        """A list that keeps growing needs search: scanning it by eye is a way
        of never reviewing the older entries again."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        for cercato in ("luxa", "NOCE", "8729721830575", "riga 4794"):
            with self.subTest(cercato=cercato):
                self.assertEqual(len(store.elenco_delle_uguaglianze(cercato)["uguaglianze"]), 1)
        assente = store.elenco_delle_uguaglianze("sapone di marsiglia")
        self.assertEqual(assente["uguaglianze"], [])
        self.assertEqual(assente["totale"], 1)

    def test_senza_codice_a_barre_vale_solo_per_questo_confronto_e_lo_dice(self) -> None:
        """Some management-software items and LARICE display offers have no
        EAN: there's nothing to declare equivalent, and pretending otherwise
        would be a promise that breaks on the next run."""

        store = self.negozio(review=confronto(ean_prodotto=""))

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["uguaglianzaRicordata"], False)
        # The message must name what's missing, not just say it failed: the
        # reader needs to know the problem is the product's data, not a bug.
        self.assertIn("il prodotto non ha un codice a barre", esito["message"])
        self.assertIn("Non vale per i prossimi", esito["message"])
        self.assertEqual(store.uguaglianze_in_vigore(), [])
        # The match for the current comparison still happens, which is the point.
        self.assertEqual(self.offerte_disponibili(store.review())["noce"], 1.15)

    def test_due_codici_gia_uguali_non_producono_una_dichiarazione(self) -> None:
        store = self.negozio(review=confronto(ean_prodotto=EAN_NOCE))

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["uguaglianzaRicordata"], False)
        self.assertIn("già lo stesso", esito["message"])
        self.assertEqual(store.uguaglianze_in_vigore(), [])


class IlMagazzinoNonSiApreSeNonServe(BancoDellAbbinamento):
    """Reading the comparison must not create the confirmations database file.

    SQLite keeps the file open for the life of the connection, and on
    Windows an open file locks the directory containing it; a temp-directory
    cleanup can fail while every test that checks the file directly still
    passes. Equivalences are read on every read of the comparison, so
    opening the database unconditionally would reproduce that failure, and
    only the full test suite (not a single targeted test) would show it.
    """

    def test_leggere_il_confronto_non_crea_nessun_conferme_db(self) -> None:
        store = self.negozio()

        store.review()
        store.review()

        self.assertFalse(self.conferme_esiste(), "il confronto ha creato il magazzino senza motivo")
        self.assertEqual(store.uguaglianze_in_vigore(), [])
        self.assertEqual(store.uguaglianze_dichiarate(), [])

    def test_ma_dichiarare_un_uguaglianza_lo_crea(self) -> None:
        store = self.negozio()

        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertTrue(self.conferme_esiste())
        self.assertEqual(store.uguaglianze_in_vigore(), [[EAN_GESTIONALE, EAN_NOCE]])

    def conferme_esiste(self) -> bool:
        return (self.root / "history" / "conferme.db").exists()


class IlListinoSiSfoglia(BancoDellAbbinamento):
    def test_la_rotta_restituisce_le_righe_e_i_fornitori(self) -> None:
        store = self.negozio()
        store.catalog.sfoglia = lambda review, fornitore, **extra: {
            "supplier": fornitore, "supplierName": fornitore.upper(),
            "righe": [], "da": 0, "quante": 50, "trovate": 0, "totale": 3, "scartate": 1,
            "rigaCercata": None,
        }

        pagina = store.sfoglia_listino("noce")

        self.assertIs(pagina["ok"], True)
        self.assertEqual(pagina["totale"], 3)
        self.assertEqual([voce["id"] for voce in pagina["fornitori"]], ["noce"])


class LAutosalvataggioNonPortaViaNiente(BancoDellAbbinamento):
    """A save must not drop state that a different route wrote.

    `validate_snapshot` rebuilds all of `state.json` on every save; the
    autosave payload only carries quantities, so keys written by partial
    routes must be copied back from disk. Manual matches are exactly the
    path a user takes once the automatic match has already failed, so
    losing them on the next autosave turns the chosen offer unavailable and
    makes `save_state` raise `OFFERTA_NON_VALIDA`, which then blocks every
    save after it.
    """

    def salva_le_quantita(self, store: ReviewStore, quantita: int = 1, fornitore: str = "noce") -> dict:
        """The debounced autosave payload the client sends after a quantity edit.

        It carries no manual matches by design; those are written by a
        separate route, not by this one.
        """

        sul_disco = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        return store.save_state({
            "runId": "run-prova",
            "stateVersion": sul_disco.get("stateVersion"),
            "currentStep": 2,
            "products": [{
                "id": "product:330",
                "quantity": quantita,
                "selectedSupplierId": fornitore,
                "confirmed": True,
                "quantitySource": "utente",
            }],
        })

    def stato_sul_disco(self) -> dict:
        return json.loads((self.root / "state.json").read_text(encoding="utf-8"))

    def test_la_riga_scelta_a_mano_e_ancora_li_dopo_il_salvataggio(self) -> None:
        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.salva_le_quantita(store)

        scelte = self.stato_sul_disco()["manualMatches"]
        self.assertEqual([voce["sourceRow"] for voce in scelte], [4794])
        self.assertEqual(
            self.offerte_disponibili(store.review()), {"cipresso": 1.28, "noce": 1.15},
        )

    def test_il_salvataggio_dopo_non_si_spegne(self) -> None:
        """The real consequence: if the match is lost, the offer with the
        quantity on it becomes unusable and `save_state` keeps raising
        `SnapshotError`. Two saves in a row are enough to show it."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.salva_le_quantita(store, quantita=1)
        self.salva_le_quantita(store, quantita=2)

        self.assertEqual(self.stato_sul_disco()["products"][0]["quantity"], 2)

    def test_le_altre_tre_chiavi_restano_ricopiate(self) -> None:
        """Regression guard: fixing the missing key must not break the ones already working."""

        store = self.negozio()
        stato = {
            "schemaVersion": 1, "runId": "run-prova", "stateVersion": 0,
            "manualProducts": [{"id": "product:999", "addedManually": True}],
            "matchOverrides": [{
                "runId": "run-prova", "productId": "product:331",
                "supplierId": "noce", "candidateKey": "abc", "accepted": False,
            }],
            "supplierDiscounts": {"noce": 3.5},
        }
        (self.root / "state.json").write_text(json.dumps(stato), encoding="utf-8")

        # No manual match here: the only usable offer is the one the
        # automatic analysis found on its own.
        self.salva_le_quantita(store, fornitore="cipresso")

        dopo = self.stato_sul_disco()
        self.assertEqual(dopo["manualProducts"], stato["manualProducts"])
        self.assertEqual(dopo["matchOverrides"], stato["matchOverrides"])
        self.assertEqual(dopo["supplierDiscounts"], stato["supplierDiscounts"])


class NessunaRottaScriveUnaChiaveCheIlSalvataggioNonConosce(unittest.TestCase):
    """Guards against a new partial route writing a key the autosave doesn't copy back.

    Checks a static property, not a behavior: that two sets match — the keys
    partial routes write into `state`, and the keys `validate_snapshot`
    copies back from disk. A gap between the two is exactly the class of bug
    a per-key test can't catch, since each one only looks at its own key.
    """

    # Routes that write a piece of state on their own, bypassing `save_state`.
    # Add new ones here.
    ROTTE_PARZIALI = (
        "answer_rejected_candidate",
        "abbina_riga_di_listino",
        # The "not the same item" route: writes to the confirmations
        # database and touches only `products` in `state`, a service key the
        # client resends in full on every save. Listed here so this test
        # covers it: if it ever wrote a key of its own, this test would fail
        # instead of letting an uncopied key slip through silently.
        "rifiuta_l_abbinamento",
        "set_supplier_discount",
        "add_manual_product",
    )

    # Keys `validate_snapshot` rebuilds itself on every save, which is
    # correct: they must not be copied back from disk.
    #
    # `products` is the only key a partial route writes that still needs
    # rebuilding: `set_supplier_discount` fills in a default decision for
    # products that don't have one yet, but the client resends every
    # decision on each save (`snapshot()` maps `state.review.products` in
    # full). Copying it from disk would ignore quantities just written.
    CHIAVI_DI_SERVIZIO = frozenset({
        "schemaVersion", "runId", "updatedAt", "stateVersion", "stateVersionOrigin",
        "products",
    })

    @staticmethod
    def chiavi_scritte(metodo: ast.FunctionDef) -> set[str]:
        """Return every key set via `state["x"] = ...` or `state.setdefault("x", ...)`."""

        trovate: set[str] = set()
        for nodo in ast.walk(metodo):
            if isinstance(nodo, ast.Assign):
                for bersaglio in nodo.targets:
                    if (isinstance(bersaglio, ast.Subscript)
                            and isinstance(bersaglio.value, ast.Name)
                            and bersaglio.value.id == "state"
                            and isinstance(bersaglio.slice, ast.Constant)
                            and isinstance(bersaglio.slice.value, str)):
                        trovate.add(bersaglio.slice.value)
            if (isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr == "setdefault"
                    and isinstance(nodo.func.value, ast.Name)
                    and nodo.func.value.id == "state"
                    and nodo.args
                    and isinstance(nodo.args[0], ast.Constant)
                    and isinstance(nodo.args[0].value, str)):
                trovate.add(nodo.args[0].value)
        return trovate

    def test_le_chiavi_delle_rotte_e_quelle_ricopiate_sono_le_stesse(self) -> None:
        sorgente = (RADICE / "app" / "server.py").read_text(encoding="utf-8")
        albero = ast.parse(sorgente)
        metodi = {
            nodo.name: nodo
            for nodo in ast.walk(albero)
            if isinstance(nodo, ast.FunctionDef) and nodo.name in self.ROTTE_PARZIALI
        }
        self.assertEqual(sorted(metodi), sorted(self.ROTTE_PARZIALI), "una rotta parziale è sparita o ha cambiato nome")

        scritte: set[str] = set()
        for nome in self.ROTTE_PARZIALI:
            scritte |= self.chiavi_scritte(metodi[nome])
        self.assertTrue(scritte, "nessuna scrittura trovata: il lettore non legge più il codice")

        self.assertEqual(
            scritte - self.CHIAVI_DI_SERVIZIO,
            set(server_module.CHIAVI_DI_STATO_RICOPIATE),
            "una rotta scrive una chiave che l'autosalvataggio non ricopia (o viceversa): "
            "vedi CHIAVI_DI_STATO_RICOPIATE in app/server.py",
        )


if __name__ == "__main__":
    unittest.main()
