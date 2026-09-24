"""Whether an offer can be ordered must have exactly one implementation.

`offer_is_available` is the single source of truth: `server`, `pipeline_jobs`
and `promotion_bridge` import it rather than reimplementing the rule. If the
rule were copied by hand in more than one place, the copies could drift and
the mismatch would only surface later, somewhere else in the pipeline.
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
    """`server`, `pipeline_jobs` and `promotion_bridge` must all call the same
    `offerta` functions, never a local copy of the same rule."""

    def test_il_servizio_e_la_catena_chiamano_la_stessa_funzione(self) -> None:
        self.assertIs(server.offer_is_available, offerta.offer_is_available)
        self.assertIs(pipeline_jobs.offer_is_available, offerta.offer_is_available)
        self.assertIs(server.offer_supplier_id, offerta.offer_supplier_id)
        self.assertIs(pipeline_jobs.offer_supplier_id, offerta.offer_supplier_id)
        self.assertIs(server.offer_pricing, offerta.offer_pricing)
        self.assertIs(server.find_offer, offerta.find_offer)

    def test_il_parser_dei_numeri_del_servizio_e_quello_dell_offerta_sono_lo_stesso(self) -> None:
        """`offer_pricing` parses prices with the same number parser as the
        rest of the service, not a separate copy."""

        self.assertIs(server.number, offerta.numero)

    def test_nessun_altro_file_riscrive_la_regola_a_mano(self) -> None:
        """Fails if any file besides `offerta.py` reimplements the
        availability check instead of importing it.

        Checks both `is not False` and `is False` forms, since the rule can
        be written either way (e.g. negated inside `promotion_bridge.detect`,
        which decides which offers count toward promotion thresholds).
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
        """`detect` decides which offers count toward promotion thresholds,
        so it must use the same availability rule as the rest of the service."""

        import promotion_bridge  # noqa: PLC0415 - only needed here

        self.assertIs(promotion_bridge.offer_is_available, offerta.offer_is_available)
        self.assertIs(promotion_bridge.offer_supplier_id, offerta.offer_supplier_id)

    def test_offerta_non_importa_ne_il_servizio_ne_la_catena(self) -> None:
        """`offerta` is a shared module; it must not import its own callers,
        or the import graph would form a cycle."""

        testo = (SKILL_ROOT / "app" / "offerta.py").read_text(encoding="utf-8")
        for vietato in ("import server", "import pipeline_jobs", "from server", "from pipeline_jobs"):
            self.assertNotIn(vietato, testo)


class LeTreDomande(unittest.TestCase):
    """Covers `offer_is_available`, `offer_supplier_id`, `offer_pricing` and
    `find_offer`: the shared helpers other modules import from `offerta`."""

    def test_il_campo_assente_vale_disponibile(self) -> None:
        # Price lists read before the field existed never write it.
        self.assertTrue(offerta.offer_is_available({"supplierId": "betulla"}))
        self.assertTrue(offerta.offer_is_available({"available": True}))
        self.assertFalse(offerta.offer_is_available({"available": False}))
        self.assertFalse(offerta.offer_is_available(None))
        self.assertFalse(offerta.offer_is_available("betulla"))

    def test_un_valore_che_non_e_ne_vero_ne_falso_non_toglie_l_offerta(self) -> None:
        """Checks `is not False`, not truthiness: `bool(...)` would make an
        offer with `available: 0` or `available: ""` non-orderable, turning
        dirty data into a decision."""

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
