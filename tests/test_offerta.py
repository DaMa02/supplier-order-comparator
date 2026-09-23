"""«Quest'offerta si puo' ordinare» ha una risposta sola.

Fino al 20 agosto 2026 la regola — `offer.get("available") is not False` —
aveva la sua autorita' in `server.offer_is_available` e **due copie scritte a
mano**: `server._offerta_piu_conveniente` e, la piu' cara,
`pipeline_jobs._ripulisci_stato`, cioe' la funzione che decide quali quantita'
azzerare dopo un ricalcolo.  Il suo docstring dichiara di dover «seguire la
stessa riga di confine» di `validate_snapshot`: un accordo fra copie, tenuto in
piedi a mano.

Il giorno in cui la regola cresce — «un'offerta senza `sourceRow` non e'
utilizzabile» — chi la scrive nel servizio e la dimentica nella catena ottiene
un ricalcolo che NON azzera una quantita' su un'offerta che la compilazione poi
rifiuta, e il sintomo si vede una settimana dopo e altrove.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import offerta  # noqa: E402
import pipeline_jobs  # noqa: E402
import server  # noqa: E402


class UnaAutoritaSola(unittest.TestCase):
    def test_il_servizio_e_la_catena_chiamano_la_stessa_funzione(self) -> None:
        self.assertIs(server.offer_is_available, offerta.offer_is_available)
        self.assertIs(pipeline_jobs.offer_is_available, offerta.offer_is_available)
        self.assertIs(server.offer_supplier_id, offerta.offer_supplier_id)
        self.assertIs(pipeline_jobs.offer_supplier_id, offerta.offer_supplier_id)
        self.assertIs(server.offer_pricing, offerta.offer_pricing)
        self.assertIs(server.find_offer, offerta.find_offer)

    def test_il_parser_dei_numeri_del_servizio_e_quello_dell_offerta_sono_lo_stesso(self) -> None:
        """`offer_pricing` legge prezzi: se leggesse i numeri in modo diverso

        dal resto del servizio, due copie ci sarebbero comunque — sotto un
        altro nome."""

        self.assertIs(server.number, offerta.numero)

    def test_nessun_altro_file_riscrive_la_regola_a_mano(self) -> None:
        """La prova che vale davvero: e' quella che diventa rossa il giorno in

        cui qualcuno ricopia i sette caratteri invece di importare.

        ⚠ Cerca tutt'e due le facce, `is not False` e `is False`. La prima
        versione guardava solo la prima, e non vedeva la copia che stava in
        `promotion_bridge.detect` scritta al negativo — cioe' lo stesso accordo
        fra copie sotto un'altra faccia, dentro la funzione che decide quali
        offerte concorrono alle soglie delle promozioni. Trovata dalla verifica
        avversariale del 20 agosto 2026, con la prova verde.
        """

        regola = re.compile(r"""get\(\s*["']available["']\s*\)\s*is\s+(not\s+)?False""")
        colpevoli = []
        for percorso in sorted((SKILL_ROOT / "app").glob("*.py")) + sorted((SKILL_ROOT / "scripts").glob("*.py")):
            if percorso.name == "offerta.py":
                continue
            for numero_riga, riga in enumerate(percorso.read_text(encoding="utf-8").splitlines(), 1):
                if regola.search(riga):
                    colpevoli.append(f"{percorso.name}:{numero_riga}")

        self.assertEqual(colpevoli, [], "la regola è di nuovo scritta a mano: importala da `offerta`")

    def test_anche_il_ponte_delle_promozioni_chiama_la_stessa_funzione(self) -> None:
        """`detect` decide quali offerte concorrono alle soglie: una regola che

        cresce nel servizio e non li' sbaglia un omaggio."""

        import promotion_bridge  # noqa: PLC0415 - serve solo qui

        self.assertIs(promotion_bridge.offer_is_available, offerta.offer_is_available)
        self.assertIs(promotion_bridge.offer_supplier_id, offerta.offer_supplier_id)

    def test_offerta_non_importa_ne_il_servizio_ne_la_catena(self) -> None:
        """Un modulo condiviso che risalisse a chi lo usa sarebbe un anello."""

        testo = (SKILL_ROOT / "app" / "offerta.py").read_text(encoding="utf-8")
        for vietato in ("import server", "import pipeline_jobs", "from server", "from pipeline_jobs"):
            self.assertNotIn(vietato, testo)


class LeTreDomande(unittest.TestCase):
    def test_il_campo_assente_vale_disponibile(self) -> None:
        # I listini letti prima che il campo esistesse non lo scrivono.
        self.assertTrue(offerta.offer_is_available({"supplierId": "betulla"}))
        self.assertTrue(offerta.offer_is_available({"available": True}))
        self.assertFalse(offerta.offer_is_available({"available": False}))
        self.assertFalse(offerta.offer_is_available(None))
        self.assertFalse(offerta.offer_is_available("betulla"))

    def test_un_valore_che_non_e_ne_vero_ne_falso_non_toglie_l_offerta(self) -> None:
        """`is not False`, non `truthy`: e' la regola scritta, e cambiarla in

        `bool(...)` renderebbe non ordinabile un'offerta con `available: 0`
        o `available: ""` — cioe' un dato sporco diventerebbe una decisione."""

        self.assertTrue(offerta.offer_is_available({"available": 0}))
        self.assertTrue(offerta.offer_is_available({"available": ""}))

    def test_il_fornitore_si_legge_in_tutt_e_due_i_modi(self) -> None:
        self.assertEqual(offerta.offer_supplier_id({"supplierId": "betulla"}), "betulla")
        self.assertEqual(offerta.offer_supplier_id({"supplier_id": "larice"}), "larice")
        self.assertEqual(offerta.offer_supplier_id({}), "")
        self.assertEqual(offerta.offer_supplier_id(None), "")

    def test_dei_due_prezzi_ne_basta_uno(self) -> None:
        dal_pezzo = offerta.offer_pricing({"unitPriceNet": 2.5, "quantityFactor": 6})
        self.assertEqual(dal_pezzo, {"factor": 6.0, "unitPriceNet": 2.5, "orderUnitPriceNet": 15.0})
        dal_collo = offerta.offer_pricing({"orderUnitPriceNet": 15.0, "quantityFactor": 6})
        self.assertEqual(dal_collo, {"factor": 6.0, "unitPriceNet": 2.5, "orderUnitPriceNet": 15.0})
        self.assertIsNone(offerta.offer_pricing({"quantityFactor": 6}))
        self.assertIsNone(offerta.offer_pricing(None))

    def test_la_prima_offerta_del_fornitore_quando_il_listino_la_ripete(self) -> None:
        prodotto = {"offers": [
            {"supplierId": "betulla", "sourceRow": 10},
            {"supplierId": "betulla", "sourceRow": 99},
        ]}

        self.assertEqual(offerta.find_offer(prodotto, "betulla"), {"supplierId": "betulla", "sourceRow": 10})
        self.assertIsNone(offerta.find_offer(prodotto, "larice"))
        self.assertIsNone(offerta.find_offer(prodotto, ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
