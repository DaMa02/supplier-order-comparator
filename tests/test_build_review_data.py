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
    """Colli suggeriti dalla colonna "Colli" del gestionale: nessun arrotondamento
    concettuale qui, solo validazione di un intero >= 0 (o None)."""

    def test_valid_values_are_rounded_to_the_nearest_integer(self) -> None:
        self.assertEqual(suggested_quantity("4"), 4)
        self.assertEqual(suggested_quantity(4.6), 5)
        self.assertEqual(suggested_quantity(0), 0)

    def test_negative_and_absent_values_are_rejected(self) -> None:
        self.assertIsNone(suggested_quantity(-1))
        self.assertIsNone(suggested_quantity(None))
        self.assertIsNone(suggested_quantity(""))


class SuggestedQuantityPropagationTests(unittest.TestCase):
    """product.suggestedQuantity / product.quantitySource devono arrivare dalla
    colonna "Colli" del gestionale (master.suggested_colli), non da un default
    calcolato o da un arrotondamento pezzi->colli."""

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
    """Il gestionale lo chiede, nessun listino lo porta: si vede e si spiega.

    Il 14 agosto 2026 tre prodotti nuovi del gestionale sono nati con
    `quantity: 1` e nessun fornitore.  Il passo 2 si apriva con un errore
    bloccante e **l'autosalvataggio moriva a ogni battuta**: `validate_snapshot`
    rifiutava una quantita' senza offerta utilizzabile, e il rifiuto valeva
    anche per il semplice salvataggio.  Il programma produceva uno stato che il
    programma stesso non accetta, e la toppa fu azzerare la quantita'.

    ⚠ Il 16 agosto 2026 Daniele ha deciso l'altra meta': la quantita' resta
    quella normale del prodotto, quella dei «Colli» del gestionale, anche senza
    fornitore.  Azzerarla cancellava l'unica cosa che si sapeva di quella riga —
    quanti ne servono — e il prodotto non serve piu' a niente a valle: adesso
    con la sua quantita' entra nell'elenco «Prodotti da reperire» prodotto alla
    compilazione.  Il rifiuto di `validate_snapshot` e' stato ristretto nello
    stesso lavoro: quantita' > 0 senza NESSUNA offerta utilizzabile e' uno stato
    valido.

    Il prodotto resta, la quantita' pure, e la scheda dice perche' non si puo'
    ordinarlo.
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
        """La difesa nuova: la quantita' non dipende piu' dal fornitore.

        Se questa prova torna a leggere 0, l'elenco «Prodotti da reperire» nasce
        vuoto — ci entra solo chi ha una quantita' > 0 — e il prodotto che il
        gestionale chiede sparisce da ogni foglio senza dirlo a nessuno.
        """

        prodotto = self.prodotto(colli="1", ordinabile=False)

        self.assertEqual(prodotto["quantity"], 1)
        self.assertIsNone(prodotto["selectedSupplierId"])
        self.assertFalse(prodotto["confirmed"])

    def test_la_quantita_senza_fornitore_resta_marcata_gestionale(self) -> None:
        """Chi l'ha scritta decide chi la puo' riscrivere.

        Marcarla «utente» la renderebbe intoccabile: `_ripulisci_stato` rilegge
        dall'elenco solo cio' che e' marcato «gestionale», e al ricalcolo dopo
        un gestionale con altri colli non riuscirebbe piu' ad aggiornarla.
        """

        prodotto = self.prodotto(colli="4", ordinabile=False)

        self.assertEqual(prodotto["quantity"], 4)
        self.assertEqual(prodotto["quantitySource"], "gestionale")

    def test_senza_colli_e_senza_fornitore_la_quantita_resta_zero(self) -> None:
        """Non si inventa una quantità che il gestionale non ha chiesto."""

        prodotto = self.prodotto(colli=None, ordinabile=False)

        self.assertEqual(prodotto["quantity"], 0)
        self.assertIsNone(prodotto["suggestedQuantity"])
        self.assertEqual(prodotto["quantitySource"], "utente")

    def test_un_abbinamento_da_confermare_non_e_un_prodotto_senza_offerta(self) -> None:
        """⚠ Un match ancora da verificare NON e' un'indisponibilita'.

        L'offerta c'e' e si puo' usare: manca solo che l'utente dica «si', e' lo
        stesso prodotto».  Trattarlo come irreperibile lo manderebbe nell'elenco
        dei prodotti da reperire mentre un fornitore ce l'ha in casa.
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
        """Serve a chi cerca la riga: la richiesta non si perde più per strada."""

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
        """Gli avvisi di prodotto la pagina li raccoglie solo dove c'e' una
        quantita' da ordinare, e il filtro «Nessuno ce l’ha» fa lo stesso."""

        prodotti = build_products(self.caso(colli="2", ordinabile=False), ["larice"])

        riassunto = senza_offerta_summary(prodotti)

        self.assertEqual(len(riassunto), 1)
        self.assertEqual(riassunto[0]["code"], "PRODOTTI_SENZA_OFFERTA")
        self.assertEqual(riassunto[0]["count"], 1)
        self.assertIn("«Nessuno ce l’ha»", riassunto[0]["message"])
        # ⚠ Diceva «I 1 che il gestionale chiede»: l'articolo non regge
        # davanti a un numero qualunque, e con un prodotto solo si leggeva
        # cosi'. Adesso il numero sta dopo il verbo.
        self.assertIn("Di questi il gestionale ne chiede 1", riassunto[0]["message"])
        self.assertIn("lo trovi con il filtro", riassunto[0]["message"])
        self.assertNotIn("I 1 ", riassunto[0]["message"])
        self.assertIn("Prodotti da reperire", riassunto[0]["message"])
        # ⚠ Il messaggio non deve piu' dire che la quantita' non e' stata
        # applicata: adesso lo e', e prometterlo al contrario sarebbe un avviso
        # che mente su cio' che l'utente ha davanti.
        self.assertNotIn("non è stata", riassunto[0]["message"])

    def test_se_nessuno_li_chiede_il_riassunto_non_promette_il_filtro(self) -> None:
        """Il filtro «Nessuno ce l’ha» nasconde chi sta a zero: promettere che li
        trova tutti sarebbe una frase falsa nel caso in cui la quantita' non
        c'e'."""

        prodotti = build_products(self.caso(colli=None, ordinabile=False), ["larice"])

        riassunto = senza_offerta_summary(prodotti)

        self.assertEqual(riassunto[0]["count"], 1)
        self.assertIn("Nessuno di questi ha una quantità da ordinare", riassunto[0]["message"])

    def test_senza_prodotti_orfani_nessun_riassunto(self) -> None:
        prodotti = build_products(self.caso(colli="2", ordinabile=True), ["larice"])

        self.assertEqual(senza_offerta_summary(prodotti), [])


class LAvvisoDelleAnomalieDiceCheCosaEDoveTests(unittest.TestCase):
    """«Anomalie di listino registrate — 3 anomalie in LARICE; i valori restano
    visibili nell'audit.» Daniele, 20 agosto 2026: «quindi? Che vuol dire?».

    La frase non diceva che cosa fosse successo, non diceva che cosa deve fare
    lui, e soprattutto taceva l'unica cosa che conta davanti a un ordine: se
    quel prezzo puo' essere sbagliato.
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
        """Il caso vero del 19 agosto 2026: tre righe LARICE con lo sconto «**».

        Uno sconto che il programma non sa leggere vale zero — cioe' quelle
        righe hanno il prezzo di listino pieno. Chi ordina deve saperlo dal
        titolo, senza aprire niente.
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
        # I numeri di riga: e' con quelli che si apre il listino e si controlla.
        self.assertIn("2476, 2477 e 2478", avviso["message"])

    def test_una_riga_sola_si_dice_al_singolare(self) -> None:
        avviso = anomalie_listino_summary(self.SCONTO[:1])[0]

        self.assertIn("1 riga del listino LARICE ha ", avviso["message"])
        self.assertIn("È la riga 2476 del suo file.", avviso["message"])
        self.assertNotIn("hanno", avviso["message"])

    def test_le_righe_citate_sono_poche_e_il_resto_si_conta(self) -> None:
        """Sessanta numeri di riga sono un muro, e il resto della frase non si
        legge piu'."""

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
        """⚠ Non gli si inventa una conseguenza: se non sappiamo se tocca il
        prezzo, il titolo non promette niente e il testo dice il motivo vero."""

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
        # Per Noce offer.quantityFactor resta il moltiplicatore d'ordine
        # ricavato dal campo "unit" del listino (es. "x 6"), mai il campo
        # pieces_per_carton anche se presente in un record misto.
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
    """Il caso piu' pericoloso della nuova regola: un fornitore puo' avere il
    totale per collo piu' basso pur non essendo il piu' conveniente al pezzo,
    quando i pezzi per collo differiscono tra fornitori. La scelta del
    fornitore migliore deve restare sul prezzo al pezzo (unitPriceNet), mai
    sul totale in colli (orderUnitPriceNet)."""

    def test_supplier_with_lower_total_per_carton_can_lose_the_selection(self) -> None:
        resolved = [
            {
                "gestionale": {"source_row": 10, "ean": "8000000000010", "description": "PRODOTTO TRAPPOLA"},
                "suppliers": {
                    # Larice: collo da 6 pezzi a 2,00 EUR/pezzo -> 12,00 EUR/collo
                    # (il totale per collo piu' basso dei due).
                    "larice": supplier_match(
                        unit_price_net=2.0,
                        pieces_per_carton=6,
                        ean="8000000000010",
                        description="PRODOTTO TRAPPOLA LARICE",
                        source_row=100,
                    ),
                    # Betulla: collo da 24 pezzi a 1,00 EUR/pezzo -> 24,00 EUR/collo
                    # (il totale per collo piu' alto, ma il pezzo costa meta').
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

        # Precondizione del trabocchetto: larice vince sul totale in colli...
        self.assertLess(offers["larice"]["orderUnitPriceNet"], offers["betulla"]["orderUnitPriceNet"])
        self.assertEqual(offers["larice"]["orderUnitPriceNet"], 12.0)
        self.assertEqual(offers["betulla"]["orderUnitPriceNet"], 24.0)

        # ...ma betulla vince sul prezzo al pezzo, il solo criterio valido.
        self.assertLess(offers["betulla"]["unitPriceNet"], offers["larice"]["unitPriceNet"])
        self.assertEqual(offers["betulla"]["unitPriceNet"], 1.0)
        self.assertEqual(offers["larice"]["unitPriceNet"], 2.0)

        self.assertEqual(product["selectedSupplierId"], "betulla")


class EspositoriPezziEPrezzoAlPezzoTests(unittest.TestCase):
    """Un espositore si compra intero e si consegna a pezzi.

    `quantityFactor` sono i pezzi contenuti e `unitPriceNet` il prezzo del
    singolo pezzo, esattamente come per un collo: e' il contratto che
    `offer_pricing` legge nel servizio, e da li' nascono i pezzi consegnati del
    piano d'ordine, il prezzo al pezzo dello storico e la scelta della
    «Migliore alternativa». Scrivendo `1` e il prezzo dell'espositore intero —
    com'era fino al 14 agosto 2026 — la pagina e il piano scaricabile
    descrivevano due merci diverse per lo stesso ordine, di un fattore pari ai
    pezzi dell'espositore.
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
        # L'espositore intero resta il prezzo che si fattura: il totale di riga
        # non cambia, ed e' il motivo per cui i test dei totali non vedevano
        # niente.
        self.assertEqual(offerta["orderUnitPriceNet"], 60.0)
        self.assertEqual(offerta["price"], 60.0)

    def test_senza_pezzi_dichiarati_l_espositore_vale_un_pezzo(self) -> None:
        """Il ripiego prudente: fa sembrare l'offerta più cara, non più conveniente."""

        offerta = display_offer(self.espositore(supplier="betulla", net_price=60.0, declared_units=None))

        self.assertEqual(offerta["quantityFactor"], 1)
        self.assertEqual(offerta["unitPriceNet"], 60.0)
        self.assertEqual(offerta["orderUnitPriceNet"], 60.0)

    def test_zero_pezzi_dichiarati_non_regala_un_prezzo_al_pezzo_finto(self) -> None:
        offerta = display_offer(self.espositore(supplier="betulla", net_price=60.0, declared_units=0))

        self.assertEqual(offerta["quantityFactor"], 1)
        self.assertEqual(offerta["unitPriceNet"], 60.0)

    def test_un_espositore_gia_dichiarato_non_ordinabile_resta_fuori(self) -> None:
        """La porta si chiude da tutti e due i lati.

        Il lettore dichiara `usable: False` su un espositore i cui pezzi del
        collo padre non si leggono; qui si guardava solo il prezzo, quindi
        quell'offerta rientrava con un prezzo ricavato da un fattore che nessuno
        conosce — ed è il prezzo più basso di tutti, quindi vinceva.
        """

        base = self.espositore(supplier="larice", net_price=60.0, declared_units=144)

        ordinabile = display_offer(base)
        scartato = display_offer({**base, "usable": False, "unusable_reason": "senza_pezzi_per_collo"})

        self.assertTrue(ordinabile["available"])
        self.assertFalse(scartato["available"])

    def test_identico_lo_dice_solo_una_riconciliazione_fatta(self) -> None:
        """«Espositore identico» è il risultato di una verifica, non l'assenza
        di una smentita: `None` vuol dire che i dati per farla non c'erano, e
        `None is not False` faceva scrivere «identico» sopra un controllo che
        nessuno aveva eseguito."""

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
        """Stessa composizione, pezzi dichiarati diversi: e' un dato sporco.

        Il programma non deve premiarlo. L'espositore BETULLA costa meno intero
        (50 contro 60) e piu' al pezzo (1,0417 contro 0,4167): con il contratto
        vecchio la selezione andava a BETULLA, cioe' al prezzo che non si puo'
        confrontare.
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

        # Precondizione della trappola: betulla vince sull'espositore intero...
        self.assertLess(offerte["betulla"]["orderUnitPriceNet"], offerte["larice"]["orderUnitPriceNet"])
        # ...e perde sul pezzo, che e' il solo criterio valido.
        self.assertLess(offerte["larice"]["unitPriceNet"], offerte["betulla"]["unitPriceNet"])
        self.assertEqual(prodotto["selectedSupplierId"], "larice")

    def test_un_espositore_nasce_a_zero_anche_dopo_la_decisione_sui_colli(self) -> None:
        """La decisione del 16 agosto 2026 non tocca gli espositori.

        Riguarda i colli del gestionale: un espositore nel gestionale non c'e' —
        nasce dai listini dei fornitori — quindi non ha nessuna quantita' da
        conservare, e quanti ordinarne lo scrive l'utente come prima. Se un
        giorno gli espositori nascessero con una quantita' propria, sarebbe una
        decisione da prendere, non un effetto collaterale di questa.
        """

        prodotti = build_display_products(
            [self.espositore(supplier="larice", net_price=60.0, declared_units=144)], ["larice"],
        )

        self.assertEqual(prodotti[0]["quantity"], 0)
        self.assertNotIn("suggestedQuantity", prodotti[0])


class UnPrezzoAZeroNonVinceIlConfrontoTests(unittest.TestCase):
    """Zero non e' il prezzo piu' conveniente: e' una cella che non si e' letta.

    L'ordinamento mette davanti il prezzo piu' basso, quindi una riga a 0,00
    vinceva **sempre**: il prodotto finiva assegnato a quel fornitore, con
    totale zero, e il minimo d'ordine non scattava perche' zero sta sotto
    qualunque soglia. La difesa che c'era (PREZZI_A_ZERO) guarda la mediana
    dell'intero listino, quindi vede la colonna sbagliata su tutto un file e
    non la riga singola dentro un listino sano.
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
        """Stessa famiglia: `pieces_per_carton` a zero diventava 1 in silenzio."""

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
    """Un rifiuto sbagliato dell'AI non lascia traccia da nessuna parte: il
    prodotto sparisce dal confronto presso quel fornitore e niente lo dice.
    Questo avviso e' l'unica difesa che non costa una chiamata in piu'."""

    def test_sopra_soglia_l_avviso_c_e(self) -> None:
        avvisi = suspect_reject_warnings(
            "product:12",
            "PANTERA SHAMPOO 250ML RICCI NEW",
            [rejected_match(supplier_name="BETULLA", score=SOGLIA_RIFIUTO_SOSPETTO + 0.1)],
        )
        self.assertEqual(len(avvisi), 1)
        self.assertEqual(avvisi[0]["code"], "RIFIUTO_CON_CANDIDATO_FORTE")
        self.assertEqual(avvisi[0]["productId"], "product:12")
        # Non blocca: un rifiuto giusto e' il caso normale, e circa la meta' di
        # questi avvisi lo sara'. Bloccare renderebbe il programma inservibile.
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
        """`None` e' «non lo so», e non si trasforma in un allarme: un run
        vecchio, senza il campo, non deve riempire la pagina di avvisi."""
        self.assertEqual(
            suspect_reject_warnings("product:12", "PRODOTTO", [rejected_match(supplier_name="BETULLA", score=None)]),
            [],
        )

    def test_un_rifiuto_che_non_e_dell_ai_non_produce_avvisi(self) -> None:
        """Se il prodotto non c'e' e non l'ha deciso un modello, non c'e'
        niente da rivedere: l'avviso parla di una decisione, non di un'assenza."""
        self.assertEqual(
            suspect_reject_warnings(
                "product:12", "PRODOTTO", [rejected_match(supplier_name="BETULLA", score=0.99, method="REVISIONE")]
            ),
            [],
        )

    def test_l_avviso_arriva_sul_prodotto_costruito(self) -> None:
        """La prova che conta: l'avviso deve trovarsi in `product["warnings"]`,
        che e' il campo che la pagina raccoglie nel filtro «Da verificare».
        Un avviso in un campo che nessuno legge non e' una difesa."""
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
        # ⚠ Il rifiuto lascia il prodotto senza nessuna offerta utilizzabile,
        # quindi da qui in avanti gli avvisi sulla scheda sono due: si cerca
        # quello che questo collaudo riguarda invece di contarli.
        codici = [avviso["code"] for avviso in product["warnings"]]
        self.assertIn("RIFIUTO_CON_CANDIDATO_FORTE", codici)
        self.assertIn("SENZA_OFFERTA_UTILIZZABILE", codici)
        # E il punteggio deve arrivare all'offerta, altrimenti l'avviso non si
        # potrebbe nemmeno ricostruire guardando i dati.
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
    """Il conteggio in cima alla pagina, che esiste per una ragione precisa:
    la pagina raccoglie gli avvisi di prodotto solo per i prodotti con una
    quantita' da ordinare."""

    def _prodotto(self, identificativo: str, *, quantity: int, con_avviso: bool) -> dict[str, object]:
        return {
            "id": identificativo,
            "quantity": quantity,
            "warnings": [{"code": "RIFIUTO_CON_CANDIDATO_FORTE"}] if con_avviso else [],
        }

    def test_senza_avvisi_non_dice_niente(self) -> None:
        self.assertEqual(suspect_reject_summary([self._prodotto("a", quantity=3, con_avviso=False)]), [])

    def test_dichiara_sia_il_totale_sia_quanti_sono_raggiungibili(self) -> None:
        """I due numeri sono diversi, e il messaggio non deve confonderli:
        promettere che si trovano tutti con un filtro che ne mostra la meta'
        e' un avviso che mente."""
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
        """Il caso in cui il filtro non ne mostrerebbe nemmeno uno: senza
        questa frase l'utente cercherebbe in un elenco vuoto."""
        avvisi = suspect_reject_summary([self._prodotto("a", quantity=0, con_avviso=True)])
        self.assertIn("non compaiono in quel filtro", avvisi[0]["message"])


class GliEspositoriChiestiNonSparisconoInSilenzioTests(unittest.TestCase):
    """`--displays` era rimasto sul ripiego silenzioso.

    Chi lancia la fase lo passa apposta — l'orchestratore lo dichiara
    obbligatorio — ma se il file non c'era, `load(..., [])` faceva sparire
    **tutti** gli espositori dal confronto senza una parola.
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
        """Vuol dire «per questa run non ci sono espositori», ed è una scelta."""

        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            esito = self._esegui(cartella)
            self.assertTrue((cartella / "review_data.json").exists(), esito.stderr)
        self.assertEqual(esito.returncode, 0)

    def test_un_file_vuoto_resta_legittimo(self) -> None:
        """Vuol dire «non ne sono stati trovati», ed è un'altra cosa ancora."""

        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            (cartella / "displays.json").write_text("[]", encoding="utf-8")
            esito = self._esegui(cartella, "--displays", str(cartella / "displays.json"))
            self.assertTrue((cartella / "review_data.json").exists(), esito.stderr)
        self.assertEqual(esito.returncode, 0)


class IlFileDeiMatchRisoltiEObbligatorioTests(unittest.TestCase):
    """Passava da `load(..., [])`: se il file non c'era, il confronto si
    costruiva lo stesso — misurato, otto prodotti su 527 e due fornitori su
    quattro — e usciva **0**. La pagina si apriva quasi vuota e niente diceva
    perche'. E' il modo peggiore di fallire per un programma che gira da solo."""

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
        """Misurato: un `[]` produce il sintomo identico al file mancante — otto
        prodotti, i soli espositori, e la pagina marcata «pronta». La porta va
        chiusa da tutti e due i lati."""
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
        """La controprova positiva: la guardia non deve fermare una run buona."""
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
    """Il conteggio degli scarti esisteva solo su `stdout` di
    `merge_match_decisions.py`, e nessuno lo legge. In elenco una coppia
    degradata e' indistinguibile da una che l'AI non ha mai valutato."""

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
        """La funzione da sola non basta: se nessuno la chiama, il conteggio
        resta dove stava — su uno schermo che nessuno guarda."""
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
        """Un codice nuovo introdotto a monte non deve sparire dal conteggio
        solo perche' qui non c'e' ancora la sua frase in italiano."""
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
        """Il caso singolo non basta: togliere una riga da `CAUSE_DI_SCARTO`
        restava verde, perche' `get(causa, causa)` ripiega sul codice grezzo e
        la pagina direbbe «1 perche' DECISIONE_DI_UNA_ALTRA_RUN». Senza questa
        prova la prossima causa nuova si dimentica allo stesso modo."""
        sorgente = (SCRIPTS / "merge_match_decisions.py").read_text(encoding="utf-8")
        cause = set(re.findall(r'"([A-Z_]{6,})",\s*\n\s*\)', sorgente))
        self.assertTrue(cause, "nessuna causa trovata: l'espressione non riconosce più il codice")
        self.assertEqual({c for c in cause if c not in CAUSE_DI_SCARTO}, set())


class IlFileDeiMatchRisoltiVaLettoDavveroTests(unittest.TestCase):
    """`load_obbligatorio` controlla che il file esista e basta. Un file che
    esiste ed e' troncato — scrittura interrotta, disco pieno — e' lo stesso
    guasto di prima con un'altra faccia, e il confronto quasi vuoto uscirebbe
    ancora 0 se qualcuno «addolcisse» la lettura."""

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
        """Scrittura interrotta, disco pieno: il file c'e' e non si legge. Non
        basta che si schianti — uscendo 1 con un traceback l'orchestratore non
        distingue questo da un guasto dell'interprete, e il contratto dice 2."""
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
        # Non basta che si schianti: deve dirlo. Con `--resolved` facoltativo lo
        # schianto arriva lo stesso, ma come traceback su `None.exists()`, e la
        # riga di codice che compare nel traceback contiene la parola «resolved»
        # — cioe' un controllo scritto male passerebbe.
        self.assertNotIn("Traceback", esito.stderr, "un traceback non è un messaggio")
        self.assertIn("--resolved", esito.stderr)


if __name__ == "__main__":
    unittest.main()
