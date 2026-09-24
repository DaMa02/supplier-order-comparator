"""Two invariants around the reorder list and state cleanup.

1. A zero from the management-software export is still from the export. If
   this week's list asks for 0 cartons of an item and next week's asks for
   4, marking that zero "user" would make the recompute skip it —
   `_ripulisci_stato` only re-reads what still carries the "gestionale" tag
   — and the page would stay stuck at 0 with the item silently left out of
   the order. State files saved before this fix carry the wrong tag on that
   same case; they self-heal on the next recompute, but only where the
   list at save time confirms the zero really came from it — an
   already-existing user-written zero must stay untouched.
2. State cleanup must not survive a failed publish. The final phase prunes
   `state.json` against the new comparison and only then makes it live. If
   the replace fails (`os.replace` on Windows can fail while an antivirus, a
   backup tool or OneDrive holds `review_data.json` open), the page must
   keep the previous comparison with its decisions intact, not a pruned
   state pointing at a comparison that was never activated.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts", SKILL_ROOT / "tests"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import pipeline_jobs  # noqa: E402
import scrittura_sicura  # noqa: E402
from build_review_data import build_products  # noqa: E402

import test_build_review_data as banco_confronto  # noqa: E402
import test_pipeline_jobs as banco_catena  # noqa: E402


def confronto_nuovo(quantita: int) -> banco_catena.EsecutoreFinto:
    """This week's reorder list: the same row, with today's requested quantity."""

    return banco_catena.EsecutoreFinto(confronto={
        "run": {"id": "nuova"},
        "files": [],
        "suppliers": [{"id": "betulla", "name": "BETULLA"}],
        "products": [{
            "id": "product:1",
            "name": "Uno",
            "ean": "8000000000001",
            "quantity": quantita,
            "quantitySource": "gestionale",
            "offers": [{"supplierId": "betulla", "available": True}],
        }],
        "warnings": [],
    })


class LoZeroDelGestionalePortaIlMarchioDelGestionale(unittest.TestCase):
    """The comparison side: who wrote the number decides who can rewrite it."""

    def prodotto(self, colli) -> dict[str, object]:
        fornitore = banco_confronto.supplier_match(
            unit_price_net=1.0,
            pieces_per_carton=6,
            ean="8000000000007",
            description="PRODOTTO CHIESTO",
            source_row=70,
        )
        return build_products([{
            "gestionale": {
                "source_row": 7,
                "ean": "8000000000007",
                "description": "PRODOTTO CHIESTO",
                "suggested_colli": colli,
            },
            "suppliers": {"larice": fornitore},
        }], ["larice"])[0]

    def test_zero_colli_nell_elenco_restano_una_quantita_del_gestionale(self) -> None:
        """Zero is a number the management-software export stated, not anyone's choice.

        Tagged "user", it becomes untouchable: next week's recompute stops
        re-reading it, and the new list's 4 cartons never reach the page.
        """

        prodotto = self.prodotto("0")

        self.assertEqual(prodotto["suggestedQuantity"], 0)
        self.assertEqual(prodotto["quantity"], 0)
        self.assertEqual(prodotto["quantitySource"], "gestionale")

    def test_senza_colli_nell_elenco_la_sorgente_resta_utente(self) -> None:
        """Empty column: the export said nothing, there's nothing to
        re-read, and the quantity belongs to whoever writes it in."""

        prodotto = self.prodotto(None)

        self.assertIsNone(prodotto["suggestedQuantity"])
        self.assertEqual(prodotto["quantity"], 0)
        self.assertEqual(prodotto["quantitySource"], "utente")


class UnoZeroDelGestionaleSiRileggeAlRicalcolo(banco_catena.BancoPipeline):
    """The pipeline side: what `_ripulisci_stato` does with that zero."""

    def _decisione(self, **campi) -> None:
        self._scrivi(self.stato, {
            "schemaVersion": 1,
            "runId": "vecchia",
            "products": [{
                "id": "product:1",
                "quantity": 0,
                "selectedSupplierId": "betulla",
                "quantitySource": "gestionale",
                **campi,
            }],
        })

    def test_dallo_zero_di_ieri_ai_quattro_colli_di_oggi(self) -> None:
        """The full scenario: 0 last week, 4 this week."""

        self._decisione()

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 4)
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 1)

    def test_uno_zero_scritto_a_mano_resta_zero(self) -> None:
        """"Order none of it", said by the user, is the user's decision."""

        self._decisione(quantitySource="utente")

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 0)

    def test_uno_zero_che_resta_zero_non_avvisa_e_non_azzera_niente(self) -> None:
        """The new tag must not surface warnings where nothing actually changes."""

        self._decisione()

        esito = self.esegui(confronto_nuovo(0))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(stato["products"][0]["selectedSupplierId"], "betulla")
        codici = {avviso["code"] for avviso in esito["avvisi"]}
        self.assertNotIn("SCELTE_NON_PIU_VALIDE", codici)
        self.assertNotIn("DECISIONI_SCOLLEGATE", codici)


class LaRipulituraNonSopravviveAUnaPubblicazioneFallita(banco_catena.BancoPipeline):
    """Either both files change, or neither does."""

    def test_se_il_confronto_nuovo_non_diventa_vivo_lo_stato_torna_com_era(self) -> None:
        self._scrivi(self.stato, {
            "schemaVersion": 1,
            "runId": "vecchia",
            "products": [{
                "id": "product:1",
                "quantity": 0,
                "selectedSupplierId": "betulla",
                "quantitySource": "gestionale",
            }],
        })
        stato_prima = self.stato.read_bytes()
        confronto_prima = self.review.read_bytes()
        esecutore = banco_catena.EsecutoreFinto(confronto={
            "run": {"id": "nuova"},
            "files": [],
            "suppliers": [{"id": "betulla", "name": "BETULLA"}],
            "products": [{
                "id": "product:1",
                "name": "Uno",
                "ean": "8000000000001",
                "quantity": 4,
                "offers": [{"supplierId": "betulla", "available": True}],
            }],
            "warnings": [],
        })
        originale = scrittura_sicura.scrivi_bytes

        def il_file_e_in_uso(percorso, contenuto, **extra):
            # Simulates another process holding `review_data.json` open, so
            # `os.replace` fails on Windows.
            # `resolve()` on both sides: on macOS the temp directory arrives
            # here as `/private/var/...`, and comparing it to `/var/...`
            # would never match.
            if Path(percorso).resolve() == self.review.resolve():
                raise OSError(32, "Il file è in uso da un altro processo")
            return originale(percorso, contenuto, **extra)

        with mock.patch.object(scrittura_sicura, "scrivi_bytes", il_file_e_in_uso):
            esito = self.esegui(esecutore)

        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)
        self.assertEqual(self.review.read_bytes(), confronto_prima)
        self.assertEqual(self.stato.read_bytes(), stato_prima)


class UnoZeroUtenteSiSanaSoloSeEraLoZeroDelGestionale(banco_catena.BancoPipeline):
    """Decisions already saved with the wrong tag.

    Anyone who doesn't restart from "Inizia nuova comparazione" carries over
    a `state.json` written before this fix, where the export's zero is
    tagged "user". But not every "user" zero is that bug: there's also the
    zero the user wrote by hand over a suggestion, and re-reading that one
    from the list would order stock they had removed. The previous
    comparison, which carries the export's own number, tells them apart.
    """

    def _decisione_a_zero(self, **campi) -> None:
        self._scrivi(self.stato, {
            "schemaVersion": 1,
            "runId": "vecchia",
            "products": [{
                "id": "product:1",
                "quantity": 0,
                "selectedSupplierId": "betulla",
                "quantitySource": "utente",
                **campi,
            }],
        })

    def _elenco_di_prima(self, suggerito) -> None:
        """The live comparison on disk: how many cartons that list asked for."""

        self._scrivi(self.review, {
            **banco_catena.CONFRONTO_PRECEDENTE,
            "products": [{
                "id": "product:1",
                "name": "Vecchio",
                "ean": "8000000000001",
                "quantity": 0,
                "suggestedQuantity": suggerito,
                "quantitySource": "utente",
                "offers": [],
            }],
        })

    def test_uno_zero_che_veniva_dall_elenco_torna_a_leggersi_dall_elenco(self) -> None:
        """The full scenario, on already-saved state: 0 then, 4 now."""

        self._decisione_a_zero()
        self._elenco_di_prima(0)

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 4)
        self.assertEqual(stato["products"][0]["quantitySource"], "gestionale")
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 1)

    def test_uno_zero_scritto_sopra_un_suggerimento_resta_dell_utente(self) -> None:
        """The list asked for 3 and the user wrote 0: "order none".

        Retagging this zero would order today's 4 cartons in its place —
        stock they had removed by hand. Worse than the bug it fixes.
        """

        self._decisione_a_zero()
        self._elenco_di_prima(3)

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(stato["products"][0]["quantitySource"], "utente")
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 0)

    def test_con_la_colonna_dei_colli_vuota_lo_zero_resta_dell_utente(self) -> None:
        """`suggestedQuantity` is null: the export said nothing, so that
        zero can only belong to whoever wrote it."""

        self._decisione_a_zero()
        self._elenco_di_prima(None)

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(stato["products"][0]["quantitySource"], "utente")
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 0)

    def test_lo_zero_che_resta_zero_si_riprende_comunque_il_marchio(self) -> None:
        """Zero then, zero now: no number changes, but the tag still needs
        correcting on disk — otherwise the bug is still there the week the
        list asks for 4.
        """

        self._decisione_a_zero()
        self._elenco_di_prima(0)

        esito = self.esegui(confronto_nuovo(0))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantitySource"], "gestionale")
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(stato["products"][0]["selectedSupplierId"], "betulla")
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 0)
        codici = {avviso["code"] for avviso in esito["avvisi"]}
        self.assertNotIn("SCELTE_NON_PIU_VALIDE", codici)
        self.assertNotIn("DECISIONI_SCOLLEGATE", codici)


if __name__ == "__main__":  # pragma: no cover - si esegue con `discover`
    unittest.main()
