#!/usr/bin/env python3

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from promotions import (  # noqa: E402
    CERTAINTY_HIGH,
    CERTAINTY_REVIEW,
    KIND_AMBIGUOUS,
    KIND_INCLUDED_PACK,
    KIND_NUMERIC_DISCOUNT,
    KIND_THRESHOLD_GIFT,
    STATUS_EARNED,
    STATUS_NEAR,
    STATUS_NOT_REACHED,
    STATUS_REVIEW,
    calculate_effective_price,
    calculate_promotion_state,
    decorate_review_data,
    detect_ambiguous_offer,
    detect_included_pack,
    detect_numeric_discount,
    detect_promotions,
    detect_threshold_gift,
)


def review_data(*, quantity_one=0, quantity_two=0, supplier="noce"):
    return {
        "products": [
            {
                "id": "product:1",
                "ean": "8000000000001",
                "description": "PRODOTTO UNO",
                "quantity": quantity_one,
                "quantityLabel": "colli",
                "selectedSupplierId": supplier,
                "promotionGroups": ["linea-test"],
                "offers": [
                    {
                        "supplierId": "noce",
                        "sourceRow": 1453,
                        "price": 24.36,
                        "unitPriceNet": 2.03,
                        "unitPricePreDiscount": 2.50,
                        "unitsPerOrderUnit": 12,
                    },
                    {
                        "supplierId": "larice",
                        "sourceRow": 136,
                        "price": 24.00,
                        "unitPriceNet": 2.00,
                        "unitPricePreDiscount": 2.00,
                        "unitsPerOrderUnit": 12,
                    },
                ],
            },
            {
                "id": "product:2",
                "ean": "8000000000002",
                "description": "PRODOTTO DUE",
                "quantity": quantity_two,
                "quantityLabel": "colli",
                "selectedSupplierId": supplier,
                "offers": [
                    {
                        "supplierId": "noce",
                        "sourceRow": 1454,
                        "price": 24.36,
                        "unitPriceNet": 2.03,
                        "unitsPerOrderUnit": 12,
                        "promotionGroups": ["linea-test"],
                    }
                ],
            },
        ]
    }


class LaFraseDellaSogliaTests(unittest.TestCase):
    """The exact wording of the threshold message shown on the product card.

    A message that names the wrong product or the wrong quantity source is
    worse than no message: the threshold can count across a group of
    products while the reward is a single unrelated item, and the wording
    must make both facts clear rather than implying a 1-to-1 relationship.
    """

    def soglia(self):
        return detect_threshold_gift(
            supplier="noce",
            source_reference="Canvass!G2:J130",
            source_text=(
                "ACQUISTANDO 10 CT TRA: IN OMAGGIO 1 CT DI "
                "RESALINA SALE LAVASTOVIGLIE KG1"
            ),
            eligible={"products": ["product:1", "product:2"], "mix_allowed": True},
        )

    def test_la_regola_dice_che_cosa_arriva_in_omaggio(self) -> None:
        stato = calculate_promotion_state(self.soglia(), review_data(quantity_one=10))

        self.assertEqual(stato["status"], STATUS_EARNED)
        self.assertIn("1 cartone di RESALINA SALE LAVASTOVIGLIE KG1 in omaggio", stato["message"])

    def test_la_regola_dice_dove_si_contano_i_cartoni(self) -> None:
        """On a single product's card, the message must make clear the
        quantity is a group total, not just that product's own count."""

        stato = calculate_promotion_state(self.soglia(), review_data(quantity_one=10))

        self.assertIn("fra i 2 prodotti dell'offerta", stato["message"])
        self.assertIn("in tutto", stato["message"])

    def test_un_prodotto_solo_non_parla_di_gruppi(self) -> None:
        promozione = detect_threshold_gift(
            supplier="noce",
            source_reference="Canvass!G2:J130",
            source_text="ACQUISTANDO 10 CT IN OMAGGIO 1 CT DI RESALINA SALE LAVASTOVIGLIE KG1",
            eligible={"products": ["product:1"]},
        )

        stato = calculate_promotion_state(promozione, review_data(quantity_one=10))

        self.assertNotIn("dell'offerta", stato["message"])
        self.assertIn("RESALINA SALE LAVASTOVIGLIE KG1", stato["message"])

    def test_anche_quando_manca_poco_si_dice_che_cosa_manca(self) -> None:
        stato = calculate_promotion_state(self.soglia(), review_data(quantity_one=9))

        self.assertEqual(stato["status"], STATUS_NEAR)
        self.assertIn("Ne manca 1 cartone", stato["message"])
        self.assertIn("RESALINA SALE LAVASTOVIGLIE KG1", stato["message"])


class PromotionDetectionTests(unittest.TestCase):
    def test_larice_threshold_and_gift_are_structured(self):
        promotion = detect_threshold_gift(
            supplier="larice",
            source_reference="Canvass!G135:J142",
            source_text=(
                "ACQUISTANDO 2 CT TRA; IN OMAGGIO 1 CT DI "
                "BIOPUNTO LATTE CORPO D/SOLE 75ML"
            ),
            eligible={"source_rows": [136, 137, 138, 139, 140, 141]},
        )

        self.assertIsNotNone(promotion)
        self.assertEqual(promotion["kind"], KIND_THRESHOLD_GIFT)
        self.assertEqual(promotion["threshold"], {"qty": 2, "unit": "cartoni"})
        self.assertTrue(promotion["eligible"]["mix_allowed"])
        self.assertEqual(promotion["reward"]["qty"], 1)
        self.assertEqual(promotion["reward"]["unit"], "cartoni")
        self.assertIn("BIOPUNTO", promotion["reward"]["description"])
        self.assertEqual(promotion["certainty"], CERTAINTY_HIGH)
        self.assertTrue(promotion["confirmed"])
        self.assertFalse(promotion["economic_effect"]["affects_total"])

    def test_noce_rule_and_typo_acqusita_are_parsed(self):
        chiary = detect_threshold_gift(
            supplier="noce",
            source_reference="csv:1453-1460",
            source_text=(
                "Disponibile CHIARY INTIMO ACQUISTA 5 CT IN OMAGGIO "
                "1 CT (12 PEZZI) DI CHIARY INTIMO GEL MINI ML.50"
            ),
            eligible_group="chiary-intimo",
        )
        robinet = detect_threshold_gift(
            supplier="noce",
            source_reference="csv:robinet",
            source_text=(
                "ROBINET DEO ACQUSITA 6 COLLI E RICEVI 1 CT OMAGGIO "
                "DI ROBINET DEO SPRAY AVENA & ARGAN ML.50"
            ),
            eligible_group="robinet-deo",
        )

        self.assertEqual(chiary["threshold"]["qty"], 5)
        self.assertEqual(chiary["reward"]["description"], "CHIARY INTIMO GEL MINI ML.50")
        self.assertEqual(chiary["reward"]["pieces_per_unit"], 12)
        self.assertEqual(robinet["threshold"], {"qty": 6, "unit": "colli"})
        self.assertIn("ROBINET DEO SPRAY", robinet["reward"]["description"])

    def test_le_soglie_sono_ripetibili_anche_senza_la_parola_ogni(self):
        """Repeatability is a property of the deal, not of a specific word in
        the text: inferring it only from "OGNI" ("every") would miss it in
        price lists that never use that word, capping a threshold that
        should apply repeatedly at a single reward.
        """

        con_ogni = detect_threshold_gift(
            supplier="noce",
            source_reference="csv:2092-2097",
            source_text=(
                "OGNI 5 CT ACQUISTATI IN OMAGGIO 1 PZ DI "
                "LOVEHOME CESTO PORTABIANCHERIA TONDO LT.40"
            ),
            eligible={"products": ["product:1"]},
        )
        senza_ogni = detect_threshold_gift(
            supplier="noce",
            source_reference="Canvass!G2:J130",
            source_text="ACQUISTANDO 5 CT TRA IN OMAGGIO 1 CT DI RESALINA SALE KG1",
            eligible={"products": ["product:1"]},
        )

        self.assertTrue(con_ogni["repeatable"])
        self.assertTrue(senza_ogni["repeatable"])
        for promotion in (con_ogni, senza_ogni):
            state = calculate_promotion_state(promotion, review_data(quantity_one=10))
            self.assertEqual(state["status"], STATUS_EARNED)
            self.assertEqual(state["reward_count"], 2)
            self.assertEqual(state["remaining_qty"], 5)

    def test_il_premio_in_un_collo_intero_non_perde_lettere(self):
        """The unit word "COLLO" must be matched whole, not truncated to
        "COLL" with the reward description then starting from a stray "O"."""

        promotion = detect_threshold_gift(
            supplier="noce",
            source_reference="csv:neval",
            source_text=(
                "Disponibile ACQUISTA 15 COLLI IN OMAGGIO 1 COLLO (12 PEZZI) "
                "DI NEVAL DOCCIA MEN BOOST ML.250"
            ),
            eligible={"products": ["product:1"]},
        )

        self.assertEqual(promotion["threshold"], {"qty": 15, "unit": "colli"})
        self.assertEqual(promotion["reward"]["unit"], "colli")
        self.assertEqual(promotion["reward"]["pieces_per_unit"], 12)
        self.assertEqual(promotion["reward"]["description"], "NEVAL DOCCIA MEN BOOST ML.250")

    def test_il_premio_in_un_cartone_o_in_un_pezzo_non_perde_lettere(self):
        """Same word-truncation risk as "COLLO", checked for "CARTONE" and
        "PEZZO": a truncated unit (e.g. "carton", not a value the program
        recognizes) also truncates the reward description that follows it,
        and the description is what the user reads to judge whether the
        deal is worth reaching.
        """

        for testo, unita in (
            ("ACQUISTANDO 6 CT IN OMAGGIO 1 CARTONE DI NEVAL DEO", "cartoni"),
            ("ACQUISTANDO 6 CT IN OMAGGIO 1 CARTONI DI NEVAL DEO", "cartoni"),
            ("ACQUISTANDO 6 CT IN OMAGGIO 1 PEZZO DI NEVAL DEO", "pezzi"),
            ("ACQUISTANDO 6 CT IN OMAGGIO 1 PEZZI DI NEVAL DEO", "pezzi"),
        ):
            with self.subTest(testo=testo):
                promotion = detect_threshold_gift(
                    supplier="quercia", source_reference="xlsx:neval", source_text=testo,
                )
                self.assertEqual(promotion["reward"]["unit"], unita)
                self.assertEqual(promotion["reward"]["description"], "NEVAL DEO")

    def test_un_premio_senza_unita_resta_senza_unita(self):
        """The `\\b` word-boundary after the unit group must not force a
        unit to be present when the text doesn't declare one."""

        promotion = detect_threshold_gift(
            supplier="quercia", source_reference="xlsx:neval",
            source_text="ACQUISTANDO 6 CT IN OMAGGIO 1 NEVAL DEO",
        )

        self.assertEqual(promotion["reward"]["unit"], "unità")
        self.assertEqual(promotion["reward"]["description"], "NEVAL DEO")

    def test_una_soglia_di_un_cartone_solo_e_una_soglia(self):
        """A singular unit ("OGNI 1 CARTONE") must still match: the pattern
        `CARTONI?` stops at "CARTON", and a word boundary applied right
        after that would fail to match the singular form."""

        promotion = detect_threshold_gift(
            supplier="quercia", source_reference="xlsx:acqua",
            source_text="OGNI 1 CARTONE IN OMAGGIO 1 PZ ACQUA ALLE ROSE",
        )

        self.assertEqual(promotion["threshold"], {"qty": 1, "unit": "cartoni"})
        self.assertEqual(promotion["reward"]["description"], "ACQUA ALLE ROSE")

    def test_una_grafia_reale_del_verbo_e_riconosciuta_ovunque(self):
        """"ACQUISTANO" (missing a D) must be recognized as promotional text
        everywhere it's checked, not only by the threshold detector: the
        general marker that decides whether a text is promotional at all
        must also accept it, or a text with no other promotional keyword
        would be dropped before the detector even runs.
        """

        promotion = detect_threshold_gift(
            supplier="larice",
            source_reference="Canvass!G978:J982",
            source_text="ACQUISTANO 5 CT TRA IN OMAGGIO 1 CT DI DENT. SENSODENT 15 ML",
            eligible={"source_rows": [979, 980, 981]},
        )
        ambigua = detect_ambiguous_offer(
            supplier="larice",
            source_reference="Canvass!G978",
            source_text="ACQUISTANO 5 CT TRA",
            eligible={"source_rows": [979]},
        )

        self.assertEqual(promotion["threshold"], {"qty": 5, "unit": "cartoni"})
        self.assertIsNotNone(ambigua)
        self.assertEqual(ambigua["kind"], KIND_AMBIGUOUS)

    def test_betulla_pack_is_included_and_never_reprices(self):
        promotion = detect_included_pack(
            supplier="betulla",
            source_reference="Sheet1!D3230",
            source_text="LINDA SETA Assorbenti Ultra Con Ali Lunghi 11+1 Gratis Pz",
            eligible={"products": ["product:1"]},
        )

        self.assertEqual(promotion["kind"], KIND_INCLUDED_PACK)
        self.assertEqual(promotion["reward"]["qty"], 1)
        self.assertTrue(promotion["confirmed"])
        self.assertFalse(promotion["economic_effect"]["affects_total"])
        result = calculate_effective_price(10, promotion)
        self.assertFalse(result["applied"])
        self.assertEqual(result["effective_price"], 10)

    def test_una_coppia_governata_da_una_misura_non_diventa_pezzi(self):
        """An "N+M" pair attached to a unit of measure (e.g. "500+100
        Omaggio=600 Ml", "MT.16+4 GRATIS") is a volume or length split, not
        M extra pieces: reporting it as extra units with high certainty
        would present a made-up number to the user as a fact. The magnitude
        of N and M doesn't distinguish the two cases — "8+2" is correct as
        pieces for razors but wrong for aluminum foil — so the unit of
        measure attached to the pair is the only reliable signal.
        """

        misure = [
            "ELIDERMA Bagnodoccia Argan 500+100 Omaggio=600 Ml",
            "ELIDERMA Bagnodoccia Latte Di Mandorla 500+100 Omaggio=600 M",
            "ELIDERMA Bagnodoccia Neutro 500+100 Omaggio=600 Ml",
            "CUKO ALLUMINIO MT.16+4 GRATIS",
            "CUKO ALLUMINIO MT.25+5 GRATIS",
            "CUKO ALLUMINIO MT.8+2 GRATIS",
            "TUC CRACKERS CLASSICO GR.75+25 GRATIS<br> Scadenza 31/12/2026",
        ]
        pezzi = {
            "LINDA SETA Assorbenti Ultra Con Ali Lunghi 11+1 Gratis Pz": 1,
            "LINDA SETA Assorbenti Ultra Notte Con Ali 10+1 Gratis Pz": 1,
            "DANZATRIX PANNO MULTIUSO PZ.3+1 GRATIS": 1,
            "GILLARDO RASOI BLUEVI 8+2 OMAGGIO": 2,
        }

        for testo in misure:
            with self.subTest(testo=testo):
                self.assertIsNone(
                    detect_included_pack(
                        supplier="betulla", source_reference="Sheet1!D1", source_text=testo
                    )
                )
        for testo, extra in pezzi.items():
            with self.subTest(testo=testo):
                promotion = detect_included_pack(
                    supplier="betulla", source_reference="Sheet1!D1", source_text=testo
                )
                self.assertIsNotNone(promotion)
                self.assertEqual(promotion["reward"]["qty"], extra)

    def test_una_misura_scartata_resta_visibile_come_offerta_ambigua(self):
        """Rejecting a measure-governed pair from the included-pack detector
        must not make it disappear from the page: it still needs to surface
        for manual review."""

        promotions = detect_promotions(
            supplier="betulla",
            source_reference="Sheet1!D2451",
            source_text="ELIDERMA Bagnodoccia Argan 500+100 Omaggio=600 Ml",
            eligible={"products": ["product:1"]},
            included_in_product=True,
        )

        self.assertEqual(len(promotions), 1)
        self.assertEqual(promotions[0]["kind"], KIND_AMBIGUOUS)
        self.assertEqual(promotions[0]["certainty"], CERTAINTY_REVIEW)
        self.assertFalse(promotions[0]["confirmed"])
        self.assertNotIn("unità aggiuntive", promotions[0]["reward"]["description"] or "")

    def test_i_formati_di_confezione_non_diventano_pezzi(self):
        """Independent control set: descriptions with an "N+M" pair that the
        promotional-text marker doesn't even flag as promotional, because
        they're packaging formats, not deals. They must keep reading as
        packaging formats even if a supplier later appends "GRATIS" to one.
        """

        formati_di_contenuto = [
            "DOCCIA VENEX 250+50 ML",
            "MORBY LAVATRICE RICARICA POLV. 70+8 LAV.",
            "DENT. SENSODENT 80+20ML FRESH CLEAN",
            "AZ DENTIF.PROTEZ.FAMIGLIA ML.75+10",
            "DIXOR LAVATRICE 52+8 LAVAGGI CLASSICO",
        ]
        formati_a_pezzi = [
            "BORBONE 100+20 CIALDE DECISA",
            "BIX RASOIO 3 ACTION PZ.4+2",
            "FOXA CARTA IGIENICA SETA PROFUMATA 4+2 ROTOLI",
        ]

        for testo in formati_di_contenuto:
            with self.subTest(testo=testo):
                self.assertIsNone(
                    detect_included_pack(
                        supplier="betulla",
                        source_reference="Sheet1!D1",
                        source_text=f"{testo} GRATIS",
                    )
                )
        for testo in formati_a_pezzi:
            with self.subTest(testo=testo):
                self.assertIsNotNone(
                    detect_included_pack(
                        supplier="betulla",
                        source_reference="Sheet1!D1",
                        source_text=f"{testo} GRATIS",
                    )
                )

    def test_lovehome_one_plus_one_is_ambiguous_not_included_pack(self):
        promotions = detect_promotions(
            supplier="noce",
            source_reference="availability:LOVEHOME",
            source_text="Disponibile LOVEHOME 1+1 OMAGGIO",
            eligible_group="lovehome",
            included_in_product=False,
        )

        self.assertEqual(len(promotions), 1)
        self.assertEqual(promotions[0]["kind"], KIND_AMBIGUOUS)
        self.assertEqual(promotions[0]["certainty"], CERTAINTY_REVIEW)
        self.assertFalse(promotions[0]["confirmed"])
        self.assertFalse(promotions[0]["economic_effect"]["affects_supplier_choice"])

    def test_generic_offer_without_terms_is_kept_for_review(self):
        promotion = detect_ambiguous_offer(
            supplier="betulla",
            source_reference="Sheet1!D1849",
            source_text="DURAVOLT Stilo Offerta Speciale 8 Pz",
            eligible={"products": ["product:1"]},
        )

        self.assertEqual(promotion["kind"], KIND_AMBIGUOUS)
        self.assertEqual(promotion["certainty"], CERTAINTY_REVIEW)
        self.assertFalse(promotion["economic_effect"]["affects_total"])

    def test_unrelated_text_does_not_create_promotion(self):
        promotions = detect_promotions(
            supplier="betulla",
            source_reference="Sheet1!D2",
            source_text="VAPO Emanatore Classico 30 Notti",
        )
        self.assertEqual(promotions, [])


class PromotionEconomicTests(unittest.TestCase):
    def test_confirmed_numeric_discount_can_produce_effective_price(self):
        promotion = detect_numeric_discount(
            supplier="larice",
            source_reference="Canvass!P136",
            source_text="Sconto numerico 10%",
            discount_value=0.10,
            eligible={"products": ["product:1"]},
            confirmed=True,
        )

        self.assertEqual(promotion["kind"], KIND_NUMERIC_DISCOUNT)
        self.assertEqual(promotion["economic_effect"]["discount_rate"], 0.1)
        self.assertTrue(promotion["economic_effect"]["affects_supplier_choice"])
        result = calculate_effective_price(2.00, promotion)
        self.assertTrue(result["applied"])
        self.assertEqual(result["effective_price"], 1.8)

    def test_inactive_or_already_applied_discount_is_not_applied_twice(self):
        inactive = detect_numeric_discount(
            supplier="larice",
            source_reference="Canvass!P136",
            source_text="Sconto numerico 10%",
            discount_value=10,
            eligible={"products": ["product:1"]},
            confirmed=False,
        )
        included = detect_numeric_discount(
            supplier="larice",
            source_reference="Canvass!P136",
            source_text="Sconto già incluso",
            discount_value=10,
            eligible={"products": ["product:1"]},
            confirmed=True,
            already_applied=True,
        )

        self.assertFalse(calculate_effective_price(2.00, inactive)["applied"])
        self.assertFalse(calculate_effective_price(2.00, included)["applied"])
        self.assertEqual(calculate_effective_price(2.00, included)["effective_price"], 2.00)


class PromotionStateAndDecorationTests(unittest.TestCase):
    def _chiary_promotion(self):
        return detect_threshold_gift(
            supplier="noce",
            source_reference="csv:1453-1460",
            source_text=(
                "CHIARY INTIMO ACQUISTA 5 CT IN OMAGGIO 1 CT "
                "(12 PEZZI) DI CHIARY INTIMO GEL MINI ML.50"
            ),
            eligible={"products": ["product:1", "product:2"], "mix_allowed": True},
        )

    def test_threshold_status_not_reached_near_and_earned(self):
        promotion = self._chiary_promotion()

        not_reached = calculate_promotion_state(promotion, review_data(quantity_one=2))
        near = calculate_promotion_state(promotion, review_data(quantity_one=3, quantity_two=1))
        earned = calculate_promotion_state(promotion, review_data(quantity_one=3, quantity_two=2))

        self.assertEqual(not_reached["status"], STATUS_NOT_REACHED)
        self.assertEqual(not_reached["remaining_qty"], 3)
        self.assertEqual(near["status"], STATUS_NEAR)
        self.assertEqual(near["remaining_qty"], 1)
        self.assertEqual(earned["status"], STATUS_EARNED)
        self.assertEqual(earned["reward_count"], 1)

    def test_il_messaggio_dice_quanto_manca_al_prossimo_omaggio(self):
        """"Reward earned" alone tells the operator nothing actionable.

        For a repeatable threshold, the message must state the rule, the
        quantity already selected, and how much more is needed for the next
        reward, so the operator can decide whether to round the order up.
        """

        promotion = detect_threshold_gift(
            supplier="noce",
            source_reference="Canvass!G2:J130",
            source_text="ACQUISTANDO 10 CT TRA IN OMAGGIO 1 CT DI RESALINA SALE KG1",
            eligible={"products": ["product:1", "product:2"], "mix_allowed": True},
        )

        state = calculate_promotion_state(
            promotion, review_data(quantity_one=22, quantity_two=20)
        )

        self.assertEqual(state["status"], STATUS_EARNED)
        self.assertEqual(state["reward_count"], 4)
        self.assertEqual(state["remaining_qty"], 8)
        self.assertIn("10 cartoni", state["message"])
        self.assertIn("ne hai 42", state["message"])
        self.assertIn("4 omaggi", state["message"])
        self.assertIn("te ne mancano 8 cartoni per il quinto", state["message"])
        self.assertNotIn("Omaggio ottenuto", state["message"])

    def test_l_ottavo_omaggio_non_si_dice_il_ottavo(self):
        """"Ottavo" ("eighth") is the one ordinal in the lookup table that
        starts with a vowel, so the article before it must elide to "l'"
        ("per l'ottavo"), not read "per il ottavo"."""

        promotion = detect_threshold_gift(
            supplier="noce",
            source_reference="Canvass!G2:J130",
            source_text="ACQUISTANDO 10 CT TRA IN OMAGGIO 1 CT DI RESALINA SALE KG1",
            eligible={"products": ["product:1", "product:2"], "mix_allowed": True},
        )

        state = calculate_promotion_state(
            promotion, review_data(quantity_one=40, quantity_two=30)
        )

        self.assertEqual(state["reward_count"], 7)
        self.assertIn("per l'ottavo", state["message"])
        self.assertNotIn("per il ottavo", state["message"])

    def test_una_soglia_non_ripetibile_lo_dice_invece_di_contare(self):
        promotion = self._chiary_promotion()
        promotion["repeatable"] = False

        state = calculate_promotion_state(promotion, review_data(quantity_one=12))

        self.assertEqual(state["reward_count"], 1)
        self.assertIn("una volta sola", state["message"])

    def test_unconfirmed_or_ambiguous_rule_requires_review(self):
        promotion = self._chiary_promotion()
        promotion["confirmed"] = False
        ambiguous = detect_ambiguous_offer(
            supplier="noce",
            source_reference="availability:LOVEHOME",
            source_text="LOVEHOME 1+1 OMAGGIO",
            eligible={"products": ["product:1"]},
        )

        self.assertEqual(
            calculate_promotion_state(promotion, review_data(quantity_one=5))["status"],
            STATUS_REVIEW,
        )
        self.assertEqual(
            calculate_promotion_state(ambiguous, review_data(quantity_one=5))["status"],
            STATUS_REVIEW,
        )

    def test_piece_threshold_uses_supplier_order_multiplier(self):
        promotion = detect_threshold_gift(
            supplier="noce",
            source_reference="synthetic:pieces",
            source_text="ACQUISTA 24 PEZZI IN OMAGGIO 1 PZ DI CAMPIONE",
            eligible={"products": ["product:1"]},
        )

        state = calculate_promotion_state(promotion, review_data(quantity_one=2))
        self.assertEqual(state["progress_qty"], 24)
        self.assertEqual(state["status"], STATUS_EARNED)

    def test_desired_pieces_are_converted_to_full_cartons_for_threshold(self):
        promotion = detect_threshold_gift(
            supplier="noce",
            source_reference="synthetic:cartons",
            source_text="ACQUISTA 2 CARTONI IN OMAGGIO 1 PZ DI CAMPIONE",
            eligible={"products": ["product:1"]},
        )
        source = review_data(quantity_one=10)
        source["products"][0]["quantityLabel"] = "pezzi"

        state = calculate_promotion_state(promotion, source)

        self.assertEqual(state["progress_qty"], 1)
        self.assertEqual(state["status"], STATUS_NEAR)

    def test_group_can_be_matched_on_product_or_offer(self):
        promotion = detect_threshold_gift(
            supplier="noce",
            source_reference="group:test",
            source_text="ACQUISTA 2 COLLI IN OMAGGIO 1 PZ DI PRODOTTO TEST",
            eligible_group="linea-test",
        )

        state = calculate_promotion_state(
            promotion,
            review_data(quantity_one=1, quantity_two=1),
        )
        self.assertEqual(set(state["matched_product_ids"]), {"product:1", "product:2"})
        self.assertEqual(state["status"], STATUS_EARNED)

    def test_decoration_preserves_supplier_prices_and_totals_for_gift(self):
        source = review_data(quantity_one=5)
        before = copy.deepcopy(source)
        promotion = self._chiary_promotion()

        decorated = decorate_review_data(source, [promotion])

        self.assertEqual(source, before, "La funzione non deve mutare i dati di ingresso")
        product = decorated["products"][0]
        self.assertEqual(product["selectedSupplierId"], "noce")
        self.assertEqual(product["offers"][0]["price"], 24.36)
        self.assertEqual(product["offers"][0]["unitPriceNet"], 2.03)
        self.assertNotIn("promotionEffectiveUnitPrice", product["offers"][0])
        self.assertEqual(product["promotions"][0]["state"]["status"], STATUS_EARNED)
        self.assertEqual(decorated["promotionSummary"]["counts"][STATUS_EARNED], 1)

    def test_decoration_exposes_only_safe_effective_price(self):
        source = review_data(quantity_one=1, supplier="larice")
        original_supplier = source["products"][0]["selectedSupplierId"]
        original_price = source["products"][0]["offers"][1]["price"]
        discount = detect_numeric_discount(
            supplier="larice",
            source_reference="Canvass!P136",
            source_text="Sconto numerico 10%",
            discount_value=0.10,
            eligible={"products": ["product:1"]},
        )

        decorated = decorate_review_data(source, [discount])
        larice_offer = decorated["products"][0]["offers"][1]

        self.assertEqual(decorated["products"][0]["selectedSupplierId"], original_supplier)
        self.assertEqual(larice_offer["price"], original_price)
        self.assertEqual(larice_offer["promotionEffectiveUnitPrice"], 1.8)
        self.assertEqual(larice_offer["promotionEffectiveOrderUnitPrice"], 21.6)

    def test_selection_overrides_are_used_without_mutating_review_data(self):
        source = review_data(quantity_one=0)
        promotion = self._chiary_promotion()
        state = calculate_promotion_state(
            promotion,
            source,
            selections={
                "product:1": {"quantity": 5, "selectedSupplierId": "noce"}
            },
        )

        self.assertEqual(state["status"], STATUS_EARNED)
        self.assertEqual(source["products"][0]["quantity"], 0)


if __name__ == "__main__":
    unittest.main()
