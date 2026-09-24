from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_review_data import (  # noqa: E402
    CAUSE_DI_SCARTO,
    anomalie_listino_summary,
    build_display_products,
    build_products,
    decisioni_ai_scartate,
    display_offer,
    senza_offerta_summary,
    suggested_quantity,
    suspect_reject_summary,
    suspect_reject_warnings,
)
from merge_match_decisions import SOGLIA_RIFIUTO_SOSPETTO  # noqa: E402


def supplier_match(
    *,
    unit_price_net: float,
    pieces_per_carton: float | None = None,
    order_multiplier: float | None = None,
    ean: str,
    description: str,
    source_row: int,
) -> dict[str, object]:
    selected: dict[str, object] = {
        "unit_price_net": unit_price_net,
        "usable": True,
        "description": description,
        "ean": ean,
        "source_row": source_row,
    }
    if order_multiplier is not None:
        selected["order_multiplier"] = order_multiplier
    if pieces_per_carton is not None:
        selected["pieces_per_carton"] = pieces_per_carton
    return {
        "status": "EAN_ESATTO",
        "method": "EAN",
        "confidence": "CERTA",
        "requires_user_confirmation": False,
        "selected": selected,
    }


class SuggestedQuantityHelperTests(unittest.TestCase):
    """Suggested cartons from the management software's "Colli" column: no
    conceptual rounding here, just validating an integer >= 0 (or None)."""

    def test_valid_values_are_rounded_to_the_nearest_integer(self) -> None:
        self.assertEqual(suggested_quantity("4"), 4)
        self.assertEqual(suggested_quantity(4.6), 5)
        self.assertEqual(suggested_quantity(0), 0)

    def test_negative_and_absent_values_are_rejected(self) -> None:
        self.assertIsNone(suggested_quantity(-1))
        self.assertIsNone(suggested_quantity(None))
        self.assertIsNone(suggested_quantity(""))


class SuggestedQuantityPropagationTests(unittest.TestCase):
    """product.suggestedQuantity / product.quantitySource must come from the
    management software's "Colli" column (master.suggested_colli), not from a
    computed default or a pieces-to-cartons rounding."""

    def test_gestionale_colli_column_prefills_quantity_and_source(self) -> None:
        resolved = [
            {
                "gestionale": {
                    "source_row": 5,
                    "ean": "8000000000005",
                    "description": "PRODOTTO CON COLLI SUGGERITI",
                    "suggested_colli": "4",
                },
                "suppliers": {
                    "larice": supplier_match(
                        unit_price_net=1.0,
                        pieces_per_carton=6,
                        ean="8000000000005",
                        description="PRODOTTO CON COLLI SUGGERITI",
                        source_row=50,
                    )
                },
            },
            {
                "gestionale": {
                    "source_row": 6,
                    "ean": "8000000000006",
                    "description": "PRODOTTO SENZA COLLI SUGGERITI",
                    "suggested_colli": None,
                },
                "suppliers": {
                    "larice": supplier_match(
                        unit_price_net=1.0,
                        pieces_per_carton=6,
                        ean="8000000000006",
                        description="PRODOTTO SENZA COLLI SUGGERITI",
                        source_row=51,
                    )
                },
            },
        ]

        products = build_products(resolved, ["larice"])
        with_hint = next(item for item in products if item["sourceRow"] == 5)
        without_hint = next(item for item in products if item["sourceRow"] == 6)

        self.assertEqual(with_hint["suggestedQuantity"], 4)
        self.assertEqual(with_hint["quantitySource"], "gestionale")
        self.assertEqual(with_hint["quantity"], 4)
        self.assertEqual(with_hint["quantityLabel"], "colli")

        self.assertIsNone(without_hint["suggestedQuantity"])
        self.assertEqual(without_hint["quantitySource"], "utente")
        self.assertEqual(without_hint["quantity"], 0)


class UnProdottoSenzaOfferteTests(unittest.TestCase):
    """The management software requests an item that no price list covers: it
    stays visible and the page explains why.

    A quantity requested by the management software but with no usable offer
    is a valid state, not a validation error: `validate_snapshot` accepts
    quantity > 0 with zero usable offers. The quantity keeps the value from
    the "Colli" column instead of being zeroed out, because zeroing it would
    erase the only known fact about that row — how many are needed — and the
    item still matters downstream: it enters the "Prodotti da reperire" list
    produced at delivery time with its requested quantity.

    The product stays, the quantity stays, and its card explains why it can't
    be ordered.
    """

    def caso(
        self, *, colli, ordinabile: bool, da_confermare: bool = False,
    ) -> list[dict[str, object]]:
        fornitore = supplier_match(
            unit_price_net=1.0 if ordinabile else 0.0,
            pieces_per_carton=6,
            ean="8000000000007",
            description="PRODOTTO CHIESTO",
            source_row=70,
        )
        if not ordinabile:
            fornitore["selected"]["usable"] = False
        if da_confermare:
            fornitore["requires_user_confirmation"] = True
            fornitore["rationale"] = "Nomi simili, codice a barre diverso."
        return [{
            "gestionale": {
                "source_row": 7,
                "ean": "8000000000007",
                "description": "PRODOTTO CHIESTO",
                "suggested_colli": colli,
            },
            "suppliers": {"larice": fornitore},
        }]

    def prodotto(self, *, colli, ordinabile: bool, da_confermare: bool = False) -> dict[str, object]:
        return build_products(
            self.caso(colli=colli, ordinabile=ordinabile, da_confermare=da_confermare), ["larice"],
        )[0]

    def test_senza_offerta_i_colli_del_gestionale_si_applicano_lo_stesso(self) -> None:
        """Quantity doesn't depend on having a usable supplier offer.

        At 0, the "Prodotti da reperire" list would end up empty — only items
        with quantity > 0 enter it — and a product the management software
        requested would silently disappear from every sheet.
        """

        prodotto = self.prodotto(colli="1", ordinabile=False)

        self.assertEqual(prodotto["quantity"], 1)
        self.assertIsNone(prodotto["selectedSupplierId"])
        self.assertFalse(prodotto["confirmed"])

    def test_la_quantita_senza_fornitore_resta_marcata_gestionale(self) -> None:
        """Whoever wrote the value decides who may overwrite it.

        Marking it "utente" would make it untouchable: `_ripulisci_stato`
        re-reads from the price list only values marked "gestionale", so a
        recompute after a management-software update with a different carton
        count could no longer refresh it.
        """

        prodotto = self.prodotto(colli="4", ordinabile=False)

        self.assertEqual(prodotto["quantity"], 4)
        self.assertEqual(prodotto["quantitySource"], "gestionale")

    def test_senza_colli_e_senza_fornitore_la_quantita_resta_zero(self) -> None:
        """Never invents a quantity the management software never requested."""

        prodotto = self.prodotto(colli=None, ordinabile=False)

        self.assertEqual(prodotto["quantity"], 0)
        self.assertIsNone(prodotto["suggestedQuantity"])
        self.assertEqual(prodotto["quantitySource"], "utente")

    def test_un_abbinamento_da_confermare_non_e_un_prodotto_senza_offerta(self) -> None:
        """A match pending user confirmation is not unavailability.

        The offer exists and is usable; only the user's "yes, same product"
        confirmation is missing. Treating it as unavailable would put it in
        the "to be sourced" list while a supplier actually carries it.
        """

        prodotto = self.prodotto(colli="2", ordinabile=True, da_confermare=True)

        self.assertEqual(prodotto["selectedSupplierId"], "larice")
        self.assertTrue(prodotto["requiresConfirmation"])
        self.assertFalse(prodotto["confirmed"])
        self.assertEqual(prodotto["quantity"], 2)
        self.assertTrue(prodotto["offers"][0]["available"])
        self.assertEqual(
            [a for a in prodotto["warnings"] if a["code"] == "SENZA_OFFERTA_UTILIZZABILE"], [],
        )
        self.assertEqual(senza_offerta_summary([prodotto]), [])

    def test_il_prodotto_resta_nel_confronto_e_dice_di_non_avere_offerte(self) -> None:
        prodotto = self.prodotto(colli="1", ordinabile=False)

        self.assertEqual(prodotto["name"], "PRODOTTO CHIESTO")
        avvisi = [a["code"] for a in prodotto["warnings"]]
        self.assertIn("SENZA_OFFERTA_UTILIZZABILE", avvisi)

    def test_l_avviso_dice_quanti_ne_chiedeva_il_gestionale(self) -> None:
        avviso = next(
            a for a in self.prodotto(colli="3", ordinabile=False)["warnings"]
            if a["code"] == "SENZA_OFFERTA_UTILIZZABILE"
        )

        self.assertIn("3 colli", avviso["message"])
        self.assertIn("listini caricati", avviso["message"])

    def test_i_colli_chiesti_restano_scritti_e_adesso_anche_applicati(self) -> None:
        """The requested count must survive intact for whoever looks up the row."""

        prodotto = self.prodotto(colli="3", ordinabile=False)

        self.assertEqual(prodotto["suggestedQuantity"], 3)
        self.assertEqual(prodotto["quantity"], 3)

    def test_con_un_offerta_buona_i_colli_si_applicano_come_prima(self) -> None:
        prodotto = self.prodotto(colli="3", ordinabile=True)

        self.assertEqual(prodotto["quantity"], 3)
        self.assertEqual(prodotto["quantitySource"], "gestionale")
        self.assertEqual(
            [a for a in prodotto["warnings"] if a["code"] == "SENZA_OFFERTA_UTILIZZABILE"], [],
        )

    def test_in_cima_alla_pagina_si_dice_quanti_sono_e_come_trovarli(self) -> None:
        """The page only surfaces product warnings where there's a quantity to
        order, and the "Nessuno ce l'ha" filter follows the same rule."""

        prodotti = build_products(self.caso(colli="2", ordinabile=False), ["larice"])

        riassunto = senza_offerta_summary(prodotti)

        self.assertEqual(len(riassunto), 1)
        self.assertEqual(riassunto[0]["code"], "PRODOTTI_SENZA_OFFERTA")
        self.assertEqual(riassunto[0]["count"], 1)
        self.assertIn("«Nessuno ce l’ha»", riassunto[0]["message"])
        # The number sits after the verb; a phrase like "I 1 che il gestionale
        # chiede" breaks grammatically for a singular count.
        self.assertIn("Di questi il gestionale ne chiede 1", riassunto[0]["message"])
        self.assertIn("lo trovi con il filtro", riassunto[0]["message"])
        self.assertNotIn("I 1 ", riassunto[0]["message"])
        self.assertIn("Prodotti da reperire", riassunto[0]["message"])
        # The message must not claim the quantity wasn't applied: it is
        # applied, and saying otherwise would misrepresent what the user sees.
        self.assertNotIn("non è stata", riassunto[0]["message"])

    def test_se_nessuno_li_chiede_il_riassunto_non_promette_il_filtro(self) -> None:
        """The "Nessuno ce l'ha" filter hides items at zero quantity; claiming
        it finds them all would be false when the quantity is missing."""

        prodotti = build_products(self.caso(colli=None, ordinabile=False), ["larice"])

        riassunto = senza_offerta_summary(prodotti)

        self.assertEqual(riassunto[0]["count"], 1)
        self.assertIn("Nessuno di questi ha una quantità da ordinare", riassunto[0]["message"])

    def test_senza_prodotti_orfani_nessun_riassunto(self) -> None:
        prodotti = build_products(self.caso(colli="2", ordinabile=True), ["larice"])

        self.assertEqual(senza_offerta_summary(prodotti), [])


class LAvvisoDelleAnomalieDiceCheCosaEDoveTests(unittest.TestCase):
    """A vague "anomalies recorded" message doesn't say what happened, what
    the user should do, or the one thing that matters before placing an
    order: whether the price on that row might be wrong.
    """

    SCONTO = [
        {"source": "larice", "source_row": 2476, "ean": "8009580477747",
         "warning": "Codice sconto testuale inatteso: **"},
        {"source": "larice", "source_row": 2477, "ean": "8009585116115",
         "warning": "Codice sconto testuale inatteso: **"},
        {"source": "larice", "source_row": 2478, "ean": "8009478053473",
         "warning": "Codice sconto testuale inatteso: **"},
    ]

    def test_senza_anomalie_nessun_avviso(self) -> None:
        self.assertEqual(anomalie_listino_summary([]), [])

    def test_il_titolo_dice_se_il_prezzo_puo_essere_sbagliato(self) -> None:
        """A discount the program can't parse is treated as zero, meaning
        those rows carry the full list price. The title must say so on its
        own, without the user opening anything.
        """

        avviso = anomalie_listino_summary(self.SCONTO)[0]

        self.assertEqual(avviso["code"], "ANOMALIE_LISTINO")
        self.assertEqual(avviso["count"], 3)
        self.assertEqual(
            avviso["title"], "LARICE: su 3 righe il prezzo può essere più alto del vero",
        )
        self.assertNotIn("anomalie", avviso["title"].lower())
        self.assertNotIn("audit", avviso["message"])

    def test_il_messaggio_dice_quante_righe_di_che_tipo_e_quali(self) -> None:
        avviso = anomalie_listino_summary(self.SCONTO)[0]

        self.assertIn("3 righe del listino LARICE", avviso["message"])
        self.assertIn("colonna dello sconto", avviso["message"])
        self.assertIn("senza sconto", avviso["message"])
        # Row numbers are what the user needs to open the price list and check.
        self.assertIn("2476, 2477 e 2478", avviso["message"])

    def test_una_riga_sola_si_dice_al_singolare(self) -> None:
        avviso = anomalie_listino_summary(self.SCONTO[:1])[0]

        self.assertIn("1 riga del listino LARICE ha ", avviso["message"])
        self.assertIn("È la riga 2476 del suo file.", avviso["message"])
        self.assertNotIn("hanno", avviso["message"])

    def test_le_righe_citate_sono_poche_e_il_resto_si_conta(self) -> None:
        """Sixty row numbers would be a wall of text that drowns the rest of the message."""

        tante = [
            {"source": "larice", "source_row": numero, "warning": "Codice sconto testuale inatteso: **"}
            for numero in range(100, 112)
        ]

        avviso = anomalie_listino_summary(tante)[0]

        self.assertIn("100, 101, 102, 103, 104, 105", avviso["message"])
        self.assertIn("ce ne sono altre 6", avviso["message"])
        self.assertNotIn("106", avviso["message"])

    def test_una_scadenza_strana_non_tocca_il_prezzo_e_lo_dice(self) -> None:
        scadenze = [
            {"source": "betulla", "source_row": 12,
             "warning": "Scadenza fuori dal credibile: «31/12/2099». Il dato è stato letto lo stesso."},
        ]

        avviso = anomalie_listino_summary(scadenze)[0]

        self.assertEqual(
            avviso["title"], "BETULLA: 1 riga con un dato incerto, che non tocca il prezzo",
        )
        self.assertIn("il prezzo non cambia", avviso["message"])

    def test_piu_fornitori_insieme_e_il_titolo_non_ne_nomina_uno_solo(self) -> None:
        avviso = anomalie_listino_summary(self.SCONTO + [
            {"source": "betulla", "source_row": 12, "warning": "Scadenza scritta male nella descrizione: «32/13/26»."},
        ])[0]

        self.assertEqual(
            avviso["title"], "Su 3 righe di listino il prezzo può essere più alto del vero",
        )
        self.assertIn("del listino LARICE", avviso["message"])
        self.assertIn("del listino BETULLA", avviso["message"])
        self.assertEqual(avviso["count"], 4)

    def test_un_motivo_mai_visto_si_riporta_come_e_scritto(self) -> None:
        """No invented consequence: if it's unknown whether the price is
        affected, the title makes no claim and the text states the real reason."""

        avviso = anomalie_listino_summary([
            {"source": "quercia", "source_row": 5, "warning": "Roba mai vista in questo listino"},
        ])[0]

        self.assertEqual(avviso["title"], "QUERCIA: 1 riga letta con una riserva")
        self.assertIn("Roba mai vista in questo listino", avviso["message"])

    def test_una_voce_che_non_e_un_dizionario_non_fa_saltare_l_avviso(self) -> None:
        avviso = anomalie_listino_summary([*self.SCONTO, "riga sbagliata"])

        self.assertEqual(len(avviso), 1)
        self.assertIn("3 righe del listino LARICE", avviso[0]["message"])


class NoceQuantityFactorTests(unittest.TestCase):
    def test_factor_is_the_order_multiplier_not_pieces_per_carton(self) -> None:
        # For this supplier, offer.quantityFactor is the order multiplier
        # parsed from the price list's "unit" field (e.g. "x 6"), never the
        # pieces_per_carton field even if present in the same record.
        resolved = [
            {
                "gestionale": {"source_row": 7, "ean": "8000000000007", "description": "PRODOTTO NOCE"},
                "suppliers": {
                    "noce": supplier_match(
                        unit_price_net=1.4,
                        order_multiplier=6,
                        pieces_per_carton=999,
                        ean="8000000000007",
                        description="PRODOTTO NOCE",
                        source_row=70,
                    )
                },
            }
        ]

        products = build_products(resolved, ["noce"])
        offer = products[0]["offers"][0]

        self.assertEqual(offer["quantityFactor"], 6)
        self.assertEqual(offer["quantityFactorLabel"], "unità")
        self.assertEqual(offer["orderUnitPriceNet"], round(1.4 * 6, 4))


class SupplierRankingByPiecePriceTests(unittest.TestCase):
    """The dangerous case: a supplier can have the lowest total per carton
    while not being the cheapest per piece, when carton sizes differ between
    suppliers. Supplier ranking must stay on the per-piece price
    (unitPriceNet), never on the per-carton total (orderUnitPriceNet)."""

    def test_supplier_with_lower_total_per_carton_can_lose_the_selection(self) -> None:
        resolved = [
            {
                "gestionale": {"source_row": 10, "ean": "8000000000010", "description": "PRODOTTO TRAPPOLA"},
                "suppliers": {
                    # larice: 6-piece carton at 2.00 EUR/piece -> 12.00 EUR/carton
                    # (the lower total of the two).
                    "larice": supplier_match(
                        unit_price_net=2.0,
                        pieces_per_carton=6,
                        ean="8000000000010",
                        description="PRODOTTO TRAPPOLA LARICE",
                        source_row=100,
                    ),
                    # betulla: 24-piece carton at 1.00 EUR/piece -> 24.00 EUR/carton
                    # (higher carton total, but half the per-piece price).
                    "betulla": supplier_match(
                        unit_price_net=1.0,
                        pieces_per_carton=24,
                        ean="8000000000010",
                        description="PRODOTTO TRAPPOLA BETULLA",
                        source_row=200,
                    ),
                },
            }
        ]

        products = build_products(resolved, ["betulla", "larice"])
        product = products[0]
        offers = {offer["supplierId"]: offer for offer in product["offers"]}

        # Precondition of the trap: larice wins on the carton total...
        self.assertLess(offers["larice"]["orderUnitPriceNet"], offers["betulla"]["orderUnitPriceNet"])
        self.assertEqual(offers["larice"]["orderUnitPriceNet"], 12.0)
        self.assertEqual(offers["betulla"]["orderUnitPriceNet"], 24.0)

        # ...but betulla wins on the per-piece price, the only valid criterion.
        self.assertLess(offers["betulla"]["unitPriceNet"], offers["larice"]["unitPriceNet"])
        self.assertEqual(offers["betulla"]["unitPriceNet"], 1.0)
        self.assertEqual(offers["larice"]["unitPriceNet"], 2.0)

        self.assertEqual(product["selectedSupplierId"], "betulla")


class EspositoriPezziEPrezzoAlPezzoTests(unittest.TestCase):
    """A display is bought whole and delivered by the piece.

    `quantityFactor` is the number of pieces it contains and `unitPriceNet`
    is the single piece's price, exactly as for a carton: this is the
    contract `offer_pricing` reads downstream, and it drives the delivered
    piece count on the order plan, the per-piece price in the history, and
    the "best alternative" choice. Writing `1` and the whole display's price
    instead would make the page and the downloadable plan describe two
    different quantities for the same order, off by the display's piece count.
    """

    @staticmethod
    def espositore(
        *,
        supplier: str,
        net_price: float,
        declared_units: float | None,
        components: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "supplier": supplier,
            "description": f"ESPOSITORE {supplier.upper()}",
            "net_price_per_display": net_price,
            "declared_units": declared_units,
            "confidence": "ALTA",
            "components": components or [],
            "source_row": 100,
        }

    def test_il_fattore_sono_i_pezzi_e_il_prezzo_e_quello_del_pezzo(self) -> None:
        offerta = display_offer(self.espositore(supplier="larice", net_price=60.0, declared_units=144))

        self.assertEqual(offerta["quantityFactor"], 144)
        self.assertEqual(offerta["unitsPerOrderUnit"], 144)
        self.assertEqual(offerta["unitPriceNet"], 0.4167)
        self.assertEqual(offerta["pricePerPiece"], 0.4167)
        self.assertEqual(offerta["pricePerPieceNet"], 0.4167)
        # The whole display's price is still what gets invoiced: the row
        # total is unchanged, which is why total-related tests didn't catch
        # this on their own.
        self.assertEqual(offerta["orderUnitPriceNet"], 60.0)
        self.assertEqual(offerta["price"], 60.0)

    def test_senza_pezzi_dichiarati_l_espositore_vale_un_pezzo(self) -> None:
        """The conservative fallback makes the offer look pricier, never cheaper."""

        offerta = display_offer(self.espositore(supplier="betulla", net_price=60.0, declared_units=None))

        self.assertEqual(offerta["quantityFactor"], 1)
        self.assertEqual(offerta["unitPriceNet"], 60.0)
        self.assertEqual(offerta["orderUnitPriceNet"], 60.0)

    def test_zero_pezzi_dichiarati_non_regala_un_prezzo_al_pezzo_finto(self) -> None:
        offerta = display_offer(self.espositore(supplier="betulla", net_price=60.0, declared_units=0))

        self.assertEqual(offerta["quantityFactor"], 1)
        self.assertEqual(offerta["unitPriceNet"], 60.0)

    def test_un_espositore_gia_dichiarato_non_ordinabile_resta_fuori(self) -> None:
        """Both sides of this must be closed.

        The parser sets `usable: False` on a display whose parent carton
        piece count can't be read. If ranking only looked at price, that
        offer would still be considered, priced from an unknown factor — and
        since that price ends up the lowest, it would win.
        """

        base = self.espositore(supplier="larice", net_price=60.0, declared_units=144)

        ordinabile = display_offer(base)
        scartato = display_offer({**base, "usable": False, "unusable_reason": "senza_pezzi_per_collo"})

        self.assertTrue(ordinabile["available"])
        self.assertFalse(scartato["available"])

    def test_identico_lo_dice_solo_una_riconciliazione_fatta(self) -> None:
        """"Espositore identico" is the result of a check that ran, not the
        absence of a check that failed: `None` means the data to run it was
        missing, and `None is not False` would print "identico" over a check
        that never happened."""

        base = self.espositore(supplier="larice", net_price=60.0, declared_units=144)

        riconciliato = display_offer({**base, "quantity_reconciled": True, "price_reconciled": True})
        mai_fatta = display_offer({**base, "quantity_reconciled": None, "price_reconciled": None})
        meta_fatta = display_offer({**base, "quantity_reconciled": True, "price_reconciled": None})
        smentita = display_offer({**base, "quantity_reconciled": False, "price_reconciled": True})

        self.assertEqual(riconciliato["compositionStatus"], "identical")
        self.assertEqual(riconciliato["matchStatus"], "Espositore identico")
        for offerta in (mai_fatta, meta_fatta, smentita):
            self.assertEqual(offerta["compositionStatus"], "comparable")
            self.assertEqual(offerta["matchStatus"], "Composizione da verificare")

    def test_fra_due_espositori_vince_quello_col_pezzo_piu_economico(self) -> None:
        """Same composition, different declared piece counts: this is dirty
        data, and ranking must not reward it. The BETULLA display costs less
        whole (50 vs 60) and more per piece (1.0417 vs 0.4167): ranking by
        whole price would pick BETULLA — the price that isn't comparable.
        """

        componenti = [{"ean": "8000000000001", "quantity": 6}]
        prodotti = build_display_products(
            [
                self.espositore(supplier="betulla", net_price=50.0, declared_units=48, components=componenti),
                self.espositore(supplier="larice", net_price=60.0, declared_units=144, components=componenti),
            ],
            ["betulla", "larice"],
        )

        self.assertEqual(len(prodotti), 1, "stessa composizione: un espositore solo, due offerte")
        prodotto = prodotti[0]
        offerte = {offerta["supplierId"]: offerta for offerta in prodotto["offers"]}

        # Precondition of the trap: betulla wins on the whole-display price...
        self.assertLess(offerte["betulla"]["orderUnitPriceNet"], offerte["larice"]["orderUnitPriceNet"])
        # ...and loses on the per-piece price, the only valid criterion.
        self.assertLess(offerte["larice"]["unitPriceNet"], offerte["betulla"]["unitPriceNet"])
        self.assertEqual(prodotto["selectedSupplierId"], "larice")

    def test_un_espositore_nasce_a_zero_anche_dopo_la_decisione_sui_colli(self) -> None:
        """The rule that carries over the management software's carton
        quantity doesn't apply to displays.

        A display doesn't exist in the management software's data — it comes
        from supplier price lists — so there's no such quantity to carry over,
        and the user still enters how many to order as before. Giving
        displays their own suggested quantity would be a separate decision,
        not a side effect of this one.
        """

        prodotti = build_display_products(
            [self.espositore(supplier="larice", net_price=60.0, declared_units=144)], ["larice"],
        )

        self.assertEqual(prodotti[0]["quantity"], 0)
        self.assertNotIn("suggestedQuantity", prodotti[0])


class UnPrezzoAZeroNonVinceIlConfrontoTests(unittest.TestCase):
    """Zero is not the best price: it's a cell that failed to parse.

    Ranking by lowest price would always pick a 0.00 row: the product would
    be assigned to that supplier with a zero total, and the minimum-order
    threshold would never trigger since zero is below any threshold. The
    existing defense (PREZZI_A_ZERO) looks at the whole price list's median,
    so it catches a misread column across an entire file, not a single row
    inside an otherwise healthy price list.
    """

    def caso(self) -> list[dict[str, object]]:
        return [{
            "gestionale": {"source_row": 10, "ean": "8000000000010", "description": "PRODOTTO"},
            "suppliers": {
                "larice": supplier_match(
                    unit_price_net=0.0, pieces_per_carton=6,
                    ean="8000000000010", description="RIGA CHE NON SI È LETTA", source_row=100,
                ),
                "betulla": supplier_match(
                    unit_price_net=1.5, pieces_per_carton=6,
                    ean="8000000000010", description="RIGA BUONA", source_row=200,
                ),
            },
        }]

    def test_l_offerta_a_zero_non_e_ordinabile(self) -> None:
        prodotto = build_products(self.caso(), ["betulla", "larice"])[0]
        offerte = {offerta["supplierId"]: offerta for offerta in prodotto["offers"]}
        self.assertFalse(offerte["larice"]["available"])
        self.assertTrue(offerte["betulla"]["available"])

    def test_la_scelta_va_al_fornitore_con_un_prezzo_vero(self) -> None:
        prodotto = build_products(self.caso(), ["betulla", "larice"])[0]
        self.assertEqual(prodotto["selectedSupplierId"], "betulla")

    def test_un_fattore_a_zero_non_rende_l_offerta_ordinabile(self) -> None:
        """Same family as the zero-price case: `pieces_per_carton` at zero must not silently become 1."""

        caso = self.caso()
        caso[0]["suppliers"]["larice"] = supplier_match(
            unit_price_net=2.0, pieces_per_carton=0,
            ean="8000000000010", description="SENZA PEZZI PER COLLO", source_row=100,
        )
        prodotto = build_products(caso, ["betulla", "larice"])[0]
        offerte = {offerta["supplierId"]: offerta for offerta in prodotto["offers"]}
        self.assertFalse(offerte["larice"]["available"])
        self.assertEqual(prodotto["selectedSupplierId"], "betulla")


def rejected_match(*, supplier_name: str, score: float | None, method: str = "AI_RIFIUTATO") -> dict[str, object]:
    return {
        "supplierId": "betulla",
        "supplierName": supplier_name,
        "method": method,
        "rejectBestScore": score,
    }


class SuspectRejectWarningTests(unittest.TestCase):
    """A wrong AI rejection otherwise leaves no trace: the product silently
    drops out of the comparison for that supplier. This warning is the only
    defense against it that doesn't cost an extra model call."""

    def test_sopra_soglia_l_avviso_c_e(self) -> None:
        avvisi = suspect_reject_warnings(
            "product:12",
            "PANTERA SHAMPOO 250ML RICCI NEW",
            [rejected_match(supplier_name="BETULLA", score=SOGLIA_RIFIUTO_SOSPETTO + 0.1)],
        )
        self.assertEqual(len(avvisi), 1)
        self.assertEqual(avvisi[0]["code"], "RIFIUTO_CON_CANDIDATO_FORTE")
        self.assertEqual(avvisi[0]["productId"], "product:12")
        # Non-blocking: a correct rejection is the normal case, and roughly
        # half of these warnings will be one. Blocking would make the app unusable.
        self.assertIs(avvisi[0]["blocking"], False)
        self.assertIn("BETULLA", avvisi[0]["message"])

    def test_sotto_soglia_nessun_avviso(self) -> None:
        self.assertEqual(
            suspect_reject_warnings(
                "product:12", "PRODOTTO", [rejected_match(supplier_name="BETULLA", score=SOGLIA_RIFIUTO_SOSPETTO - 0.01)]
            ),
            [],
        )

    def test_senza_punteggio_nessun_avviso(self) -> None:
        """`None` means "unknown" and must not become an alert: an older run
        that lacks the field shouldn't fill the page with warnings."""
        self.assertEqual(
            suspect_reject_warnings("product:12", "PRODOTTO", [rejected_match(supplier_name="BETULLA", score=None)]),
            [],
        )

    def test_un_rifiuto_che_non_e_dell_ai_non_produce_avvisi(self) -> None:
        """If a product is missing but a model didn't decide that, there's
        nothing to review: this warning is about a decision, not an absence."""
        self.assertEqual(
            suspect_reject_warnings(
                "product:12", "PRODOTTO", [rejected_match(supplier_name="BETULLA", score=0.99, method="REVISIONE")]
            ),
            [],
        )

    def test_l_avviso_arriva_sul_prodotto_costruito(self) -> None:
        """What actually matters: the warning must land in
        `product["warnings"]`, the field the page reads for the "Da
        verificare" filter. A warning in a field nobody reads is no defense."""
        resolved = [
            {
                "gestionale": {"source_row": 12, "description": "PANTERA SHAMPOO 250ML RICCI NEW", "suggested_colli": 3},
                "suppliers": {
                    "betulla": {
                        "status": "NON_TROVATO",
                        "method": "AI_RIFIUTATO",
                        "selected": None,
                        "confidence": "ALTA",
                        "requires_user_confirmation": False,
                        "rationale": "nessun candidato equivalente",
                        "alternatives": [{
                            "source_row": 441,
                            "description": "PANTERA SHAMPOO RICCI 250 ML",
                            "ean": "8000000000441",
                            "supplier_code": "C-441",
                            "unit_price_net": 2.4,
                            "pieces_per_carton": 6,
                            "packaging": "6 pezzi",
                            "usable": True,
                        }],
                        "ai_reject_best_score": 0.976,
                    }
                },
            }
        ]
        product = build_products(resolved, ["betulla"])[0]
        # The rejection leaves the product with no usable offer, so the card
        # carries two warnings; look up the one this test is about instead
        # of asserting an exact count.
        codici = [avviso["code"] for avviso in product["warnings"]]
        self.assertIn("RIFIUTO_CON_CANDIDATO_FORTE", codici)
        self.assertIn("SENZA_OFFERTA_UTILIZZABILE", codici)
        # The score must reach the offer, or the warning couldn't be
        # reconstructed just from the data.
        self.assertAlmostEqual(product["offers"][0]["rejectBestScore"], 0.976)
        candidate = product["offers"][0]["rejectedCandidate"]
        self.assertEqual(candidate["description"], "PANTERA SHAMPOO RICCI 250 ML")
        self.assertEqual(candidate["ean"], "8000000000441")
        self.assertEqual(candidate["supplierCode"], "C-441")
        self.assertEqual(candidate["sourceRow"], 441)
        self.assertEqual(candidate["orderUnitPriceNet"], 14.4)
        self.assertTrue(candidate["available"])
        self.assertRegex(candidate["candidateKey"], r"^[0-9a-f]{20}$")
        warning = product["warnings"][0]
        self.assertEqual(warning["candidateKey"], candidate["candidateKey"])
        self.assertIn("PANTERA SHAMPOO RICCI 250 ML", warning["message"])
        self.assertNotIn("0.976", warning["message"])
        self.assertIn("0.98 su 1", warning["technicalMessage"])


class SuspectRejectSummaryTests(unittest.TestCase):
    """The page-top count exists for a specific reason: the page only
    surfaces product warnings for items with a quantity to order."""

    def _prodotto(self, identificativo: str, *, quantity: int, con_avviso: bool) -> dict[str, object]:
        return {
            "id": identificativo,
            "quantity": quantity,
            "warnings": [{"code": "RIFIUTO_CON_CANDIDATO_FORTE"}] if con_avviso else [],
        }

    def test_senza_avvisi_non_dice_niente(self) -> None:
        self.assertEqual(suspect_reject_summary([self._prodotto("a", quantity=3, con_avviso=False)]), [])

    def test_dichiara_sia_il_totale_sia_quanti_sono_raggiungibili(self) -> None:
        """The two counts differ and the message must not conflate them:
        promising a filter will show them all when it only shows half would
        be a misleading warning."""
        avvisi = suspect_reject_summary([
            self._prodotto("a", quantity=3, con_avviso=True),
            self._prodotto("b", quantity=0, con_avviso=True),
            self._prodotto("c", quantity=5, con_avviso=False),
        ])
        self.assertEqual(len(avvisi), 1)
        self.assertEqual(avvisi[0]["count"], 2)
        self.assertIn("Su 2 prodotti", avvisi[0]["message"])
        self.assertIn("I 1 con una quantità", avvisi[0]["message"])

    def test_quando_nessuno_e_da_ordinare_lo_dice(self) -> None:
        """When the filter would show none at all, without this sentence the
        user would search an empty list."""
        avvisi = suspect_reject_summary([self._prodotto("a", quantity=0, con_avviso=True)])
        self.assertIn("non compaiono in quel filtro", avvisi[0]["message"])


class GliEspositoriChiestiNonSparisconoInSilenzioTests(unittest.TestCase):
    """`--displays` must not fail silently.

    The orchestrator declares this argument mandatory, but if the file were
    missing, falling back to `load(..., [])` would drop every display from
    the comparison without a word.
    """

    def _risolti(self, cartella: Path) -> Path:
        percorso = cartella / "resolved.json"
        percorso.write_text(json.dumps([{
            "gestionale": {"source_row": 12, "description": "PRODOTTO", "ean": "8001"},
            "suppliers": {"betulla": {
                "status": "EAN_ESATTO", "method": "EAN", "confidence": "CERTA",
                "selected": {
                    "unit_price_net": 2.0, "pieces_per_carton": 6, "usable": True,
                    "description": "PRODOTTO BETULLA", "ean": "8001", "source_row": 3,
                },
            }},
        }]), encoding="utf-8")
        return percorso

    def _esegui(self, cartella: Path, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_review_data.py"),
                "--resolved", str(self._risolti(cartella)),
                "--output", str(cartella / "review_data.json"),
                *extra,
            ],
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_un_file_degli_espositori_chiesto_e_assente_ferma_la_fase(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            esito = self._esegui(cartella, "--displays", str(cartella / "che-non-c-e.json"))
            self.assertFalse((cartella / "review_data.json").exists())
        self.assertEqual(esito.returncode, 2)
        self.assertIn("non esiste", esito.stderr)

    def test_non_chiederlo_affatto_resta_legittimo(self) -> None:
        """Means "no displays for this run" and is a legitimate choice."""

        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            esito = self._esegui(cartella)
            self.assertTrue((cartella / "review_data.json").exists(), esito.stderr)
        self.assertEqual(esito.returncode, 0)

    def test_un_file_vuoto_resta_legittimo(self) -> None:
        """Means "none were found", which is a distinct, also legitimate case."""

        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            (cartella / "displays.json").write_text("[]", encoding="utf-8")
            esito = self._esegui(cartella, "--displays", str(cartella / "displays.json"))
            self.assertTrue((cartella / "review_data.json").exists(), esito.stderr)
        self.assertEqual(esito.returncode, 0)


class IlFileDeiMatchRisoltiEObbligatorioTests(unittest.TestCase):
    """A missing resolved-matches file must never fall back to `load(..., [])`:
    the comparison would still build — measured, eight products out of 527
    with two of four suppliers — and exit 0. The page would open nearly
    empty with no explanation, the worst way to fail for a program that runs
    unattended."""

    def test_un_file_assente_non_e_una_lista_vuota(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            esito = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "build_review_data.py"),
                    "--resolved", str(cartella / "che-non-c-e.json"),
                    "--output", str(cartella / "review_data.json"),
                ],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertFalse((cartella / "review_data.json").exists())
        self.assertNotEqual(esito.returncode, 0)
        self.assertIn("non esiste", esito.stderr)

    def test_una_lista_vuota_fa_lo_stesso_danno_e_si_ferma_uguale(self) -> None:
        """Measured: an empty `[]` produces the same symptom as a missing file
        — eight products, displays only, and the page marked "ready". Both
        sides of this must be guarded."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            (cartella / "resolved.json").write_text("[]", encoding="utf-8")
            esito = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "build_review_data.py"),
                    "--resolved", str(cartella / "resolved.json"),
                    "--output", str(cartella / "review_data.json"),
                ],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertFalse((cartella / "review_data.json").exists())
        self.assertEqual(esito.returncode, 2)
        self.assertIn("nessun prodotto", esito.stderr)

    def test_un_file_vero_e_pieno_passa(self) -> None:
        """Positive control: the guard must not stop a good run."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            (cartella / "resolved.json").write_text(json.dumps([{
                "gestionale": {"source_row": 12, "description": "PRODOTTO", "ean": "8001"},
                "suppliers": {"betulla": {
                    "status": "EAN_ESATTO", "method": "EAN", "confidence": "CERTA",
                    "requires_user_confirmation": False,
                    "selected": {"unit_price_net": 1.0, "order_multiplier": 6, "usable": True,
                                 "description": "PRODOTTO", "ean": "8001", "source_row": 100},
                }},
            }]), encoding="utf-8")
            esito = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "build_review_data.py"),
                    "--resolved", str(cartella / "resolved.json"),
                    "--output", str(cartella / "review_data.json"),
                ],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertTrue((cartella / "review_data.json").exists())
        self.assertEqual(esito.returncode, 0)


class DecisioniAiScartateInCimaAllaPaginaTests(unittest.TestCase):
    """The discard count must not live only in `merge_match_decisions.py`'s
    `stdout`, which nobody reads: in the list, a downgraded match is
    otherwise indistinguishable from one the AI never evaluated."""

    def _risolto(self, causa: str | None) -> dict[str, object]:
        match: dict[str, object] = {
            "status": "DA_VERIFICARE", "method": "REVISIONE", "selected": None,
            "requires_user_confirmation": True, "rationale": "",
        }
        if causa:
            match["ai_decisione_scartata"] = causa
        return {"gestionale": {"source_row": 12}, "suppliers": {"betulla": match}}

    def test_senza_scarti_non_compare_nessun_avviso(self) -> None:
        self.assertEqual(decisioni_ai_scartate([self._risolto(None)]), [])

    def test_conta_e_dice_perche(self) -> None:
        avvisi = decisioni_ai_scartate([
            self._risolto("LISTINO_DISALLINEATO"),
            self._risolto("LISTINO_DISALLINEATO"),
            self._risolto("RIGA_NON_MOSTRATA"),
        ])
        self.assertEqual(len(avvisi), 1)
        self.assertEqual(avvisi[0]["count"], 3)
        self.assertEqual(avvisi[0]["byCause"], {"LISTINO_DISALLINEATO": 2, "RIGA_NON_MOSTRATA": 1})
        self.assertIn("2 perché il listino non era più quello", avvisi[0]["message"])

    def test_l_avviso_arriva_davvero_nella_pagina(self) -> None:
        """The function alone isn't enough: if nothing calls it, the count
        stays where it was — on a screen nobody watches."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            (cartella / "resolved.json").write_text(json.dumps([{
                "gestionale": {"source_row": 12, "description": "PRODOTTO", "ean": "8001"},
                "suppliers": {"betulla": {
                    "status": "DA_VERIFICARE", "method": "REVISIONE", "selected": None,
                    "requires_user_confirmation": True, "rationale": "scartata",
                    "ai_decisione_scartata": "LISTINO_DISALLINEATO",
                }},
            }]), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "build_review_data.py"),
                    "--resolved", str(cartella / "resolved.json"),
                    "--output", str(cartella / "review_data.json"),
                ],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
            pagina = json.loads((cartella / "review_data.json").read_text(encoding="utf-8"))
        codici = [avviso["code"] for avviso in pagina["warnings"]]
        self.assertIn("DECISIONI_AI_SCARTATE", codici)

    def test_una_causa_sconosciuta_si_conta_lo_stesso(self) -> None:
        """A new code introduced upstream must still be counted even before
        its Italian-language message exists here."""
        avvisi = decisioni_ai_scartate([self._risolto("QUALCOSA_DI_NUOVO")])
        self.assertEqual(avvisi[0]["count"], 1)
        self.assertIn("QUALCOSA_DI_NUOVO", avvisi[0]["message"])

    def test_le_due_cause_della_6b_si_dicono_a_parole(self) -> None:
        avvisi = decisioni_ai_scartate([
            self._risolto("DECISIONE_DI_UNA_ALTRA_RUN"),
            self._risolto("DECISIONE_SENZA_IMPRONTA"),
        ])
        self.assertIn("un elenco di candidati diverso", avvisi[0]["message"])
        self.assertIn("non dice su quali candidati", avvisi[0]["message"])

    def test_ogni_causa_scritta_dal_merge_ha_la_sua_frase(self) -> None:
        """A single-case test isn't enough: removing an entry from
        `CAUSE_DI_SCARTO` would stay green, since `get(causa, causa)` falls
        back to the raw code and the page would print the code itself
        instead of a sentence. This test guards against the same gap for
        any future cause."""
        sorgente = (SCRIPTS / "merge_match_decisions.py").read_text(encoding="utf-8")
        cause = set(re.findall(r'"([A-Z_]{6,})",\s*\n\s*\)', sorgente))
        self.assertTrue(cause, "nessuna causa trovata: l'espressione non riconosce più il codice")
        self.assertEqual({c for c in cause if c not in CAUSE_DI_SCARTO}, set())


class IlFileDeiMatchRisoltiVaLettoDavveroTests(unittest.TestCase):
    """`load_obbligatorio` checking only that the file exists isn't enough. A
    file that exists but is truncated — interrupted write, full disk — is the
    same failure in a different shape, and the near-empty comparison would
    still exit 0 if the read were made more lenient."""

    def _esegui(self, contenuto: str) -> subprocess.CompletedProcess[str]:
        cartella = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (cartella / "resolved.json").write_text(contenuto, encoding="utf-8")
        esito = subprocess.run(
            [sys.executable, str(SCRIPTS / "build_review_data.py"),
             "--resolved", str(cartella / "resolved.json"),
             "--output", str(cartella / "review_data.json")],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.cartella = cartella
        return esito

    def test_un_file_vuoto_si_ferma_con_l_esito_dichiarato(self) -> None:
        esito = self._esegui("")
        self.assertFalse((self.cartella / "review_data.json").exists())
        self.assertEqual(esito.returncode, 2)
        self.assertNotIn("Traceback", esito.stderr, "un traceback non è un messaggio")

    def test_un_file_troncato_si_ferma_con_l_esito_dichiarato(self) -> None:
        """Interrupted write, full disk: the file exists but doesn't parse.
        Crashing isn't enough — exiting 1 with a traceback would be
        indistinguishable to the orchestrator from an interpreter fault, and
        the contract requires exit code 2."""
        esito = self._esegui('[{"gestionale": {"source_row": 12}')
        self.assertFalse((self.cartella / "review_data.json").exists())
        self.assertEqual(esito.returncode, 2)
        self.assertNotIn("Traceback", esito.stderr, "un traceback non è un messaggio")

    def test_senza_resolved_non_si_costruisce_niente(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            esito = subprocess.run(
                [sys.executable, str(SCRIPTS / "build_review_data.py"), "--output", str(cartella / "review_data.json")],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertFalse((cartella / "review_data.json").exists())
        self.assertNotEqual(esito.returncode, 0)
        # Crashing isn't enough: it must say why. With `--resolved` optional,
        # the crash still happens, but as a traceback on `None.exists()`, and
        # the traceback's source line happens to contain the word "resolved"
        # — so a naive substring check on stderr would pass regardless.
        self.assertNotIn("Traceback", esito.stderr, "un traceback non è un messaggio")
        self.assertIn("--resolved", esito.stderr)


if __name__ == "__main__":
    unittest.main()
