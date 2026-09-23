"""Due difetti della revisione del 6 settembre 2026, R3 e R9.

1. **Uno zero del gestionale e' del gestionale.**  Questa settimana l'elenco
   chiede 0 colli di un articolo; la settimana dopo ne chiede 4.  Se lo zero e'
   marcato «utente» il ricalcolo non lo rilegge — `_ripulisci_stato` rilegge
   dall'elenco solo cio' che porta il marchio «gestionale» — e in pagina resta
   0: l'articolo non entra nell'ordine e nessuno lo dice.  Dal 7 settembre
   2026 c'e' un terzo lato: gli stati gia' salvati con l'etichetta sbagliata,
   che si sanano da soli al primo ricalcolo — ma solo dove l'elenco di allora
   dice che quello zero veniva davvero da lui.
2. **La ripulitura dello stato non deve sopravvivere a una pubblicazione
   fallita.**  La fase finale pota `state.json` sulla base del confronto nuovo
   e solo dopo lo rende vivo.  Se la sostituzione non riesce — su Windows
   `os.replace` fallisce mentre un antivirus, un backup o OneDrive tengono
   aperto `review_data.json` — resterebbe in pagina il confronto di prima con
   le decisioni gia' tolte.
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
    """L'elenco di questa settimana: la stessa riga, con i colli che chiede oggi."""

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
    """R3, il lato del confronto: chi ha scritto il numero decide chi lo riscrive."""

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
        """Zero e' un numero che il gestionale ha detto, non una scelta di nessuno.

        Marcato «utente» diventa intoccabile: il ricalcolo della settimana dopo
        non lo rilegge piu' e i 4 colli dell'elenco nuovo non arrivano in pagina.
        """

        prodotto = self.prodotto("0")

        self.assertEqual(prodotto["suggestedQuantity"], 0)
        self.assertEqual(prodotto["quantity"], 0)
        self.assertEqual(prodotto["quantitySource"], "gestionale")

    def test_senza_colli_nell_elenco_la_sorgente_resta_utente(self) -> None:
        """Colonna vuota: il gestionale non ha detto niente, non c'e' niente da
        rileggere, e la quantita' e' di chi la scrivera'."""

        prodotto = self.prodotto(None)

        self.assertIsNone(prodotto["suggestedQuantity"])
        self.assertEqual(prodotto["quantity"], 0)
        self.assertEqual(prodotto["quantitySource"], "utente")


class UnoZeroDelGestionaleSiRileggeAlRicalcolo(banco_catena.BancoPipeline):
    """R3, il lato della catena: che cosa fa `_ripulisci_stato` con quello zero."""

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
        """E' il difetto per intero: 0 la settimana scorsa, 4 questa."""

        self._decisione()

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 4)
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 1)

    def test_uno_zero_scritto_a_mano_resta_zero(self) -> None:
        """«Non ordinarne nessuno» detto dall'utente e' una decisione sua."""

        self._decisione(quantitySource="utente")

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 0)

    def test_uno_zero_che_resta_zero_non_avvisa_e_non_azzera_niente(self) -> None:
        """Il marchio nuovo non deve far comparire avvisi dove non succede niente."""

        self._decisione()

        esito = self.esegui(confronto_nuovo(0))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(stato["products"][0]["selectedSupplierId"], "betulla")
        codici = {avviso["code"] for avviso in esito["avvisi"]}
        self.assertNotIn("SCELTE_NON_PIU_VALIDE", codici)
        self.assertNotIn("DECISIONI_SCOLLEGATE", codici)


class LaRipulituraNonSopravviveAUnaPubblicazioneFallita(banco_catena.BancoPipeline):
    """R9: o cambiano tutti e due, o non cambia nessuno dei due."""

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
            # Il caso vero: un altro processo tiene aperto `review_data.json` e
            # su Windows `os.replace` non riesce.
            # `resolve()` da tutte e due le parti: su macOS la cartella
            # temporanea arriva qui come `/private/var/...` e il confronto
            # con `/var/...` non tornerebbe mai.
            if Path(percorso).resolve() == self.review.resolve():
                raise OSError(32, "Il file è in uso da un altro processo")
            return originale(percorso, contenuto, **extra)

        with mock.patch.object(scrittura_sicura, "scrivi_bytes", il_file_e_in_uso):
            esito = self.esegui(esecutore)

        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)
        self.assertEqual(self.review.read_bytes(), confronto_prima)
        self.assertEqual(self.stato.read_bytes(), stato_prima)


class UnoZeroUtenteSiSanaSoloSeEraLoZeroDelGestionale(banco_catena.BancoPipeline):
    """R3, il terzo lato: le decisioni gia' salvate con l'etichetta sbagliata.

    Chi non riparte da «Inizia nuova comparazione» si porta dietro uno
    `state.json` scritto prima del 7 settembre 2026, dove lo zero dell'elenco
    e' marcato «utente». Ma non tutti gli zeri «utente» sono quel difetto:
    c'e' anche lo zero che l'utente ha scritto a mano sopra un suggerimento,
    e rileggerlo dall'elenco ordinerebbe merce che aveva tolto. A dire quale
    e' quale e' il confronto di prima, che porta il numero del gestionale.
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
        """Il confronto vivo sul disco: quanti colli chiedeva l'elenco di allora."""

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
        """Il difetto per intero, sullo stato gia' salvato: 0 allora, 4 adesso."""

        self._decisione_a_zero()
        self._elenco_di_prima(0)

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 4)
        self.assertEqual(stato["products"][0]["quantitySource"], "gestionale")
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 1)

    def test_uno_zero_scritto_sopra_un_suggerimento_resta_dell_utente(self) -> None:
        """L'elenco ne chiedeva 3 e lui ha scritto 0: «non ordinarne».

        Rietichettare questo zero ordinerebbe i 4 colli di oggi al posto suo —
        merce che aveva tolto a mano. Peggio del difetto.
        """

        self._decisione_a_zero()
        self._elenco_di_prima(3)

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(stato["products"][0]["quantitySource"], "utente")
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 0)

    def test_con_la_colonna_dei_colli_vuota_lo_zero_resta_dell_utente(self) -> None:
        """`suggestedQuantity` nullo: il gestionale non aveva detto niente, e
        quello zero non puo' che essere di chi lo ha scritto."""

        self._decisione_a_zero()
        self._elenco_di_prima(None)

        esito = self.esegui(confronto_nuovo(4))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(stato["products"][0]["quantitySource"], "utente")
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 0)

    def test_lo_zero_che_resta_zero_si_riprende_comunque_il_marchio(self) -> None:
        """Zero allora, zero adesso: non cambia nessun numero, ma l'etichetta
        va corretta sul disco lo stesso — altrimenti la settimana in cui
        l'elenco chiedera' 4 il difetto sara' ancora li'.
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
