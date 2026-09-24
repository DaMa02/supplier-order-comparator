#!/usr/bin/env python3
"""Two barcodes declared equivalent, and what that does to the pipeline.

The case this is built on: `LUXA SAPONE LIQ. EROG.250ML` sits in the
management software under one EAN, used only by one supplier, at 1.28 per
piece. The same item sits with three other suppliers under a different EAN,
at 1.15, 1.1625 and 1.19.

This is not a defect in the automatic matching: one supplier's shortlist had
the right row in second place, at a score of 0.572 against 0.578 for the
wrong variant. The deciding word is "ORIGINAL" versus "SETA", and it is
missing from the management software's name — "EROG." stands for dispenser.
It cannot be decided from the text, so no scoring and no model will ever
resolve it; a person looking at the price list can.

The two kinds of error do not cost the same, and the tests are built around
that asymmetry:

* not declaring an equivalence costs one product bought at a higher price.
* declaring a wrong one costs a wrong order, at every supplier, every week,
  until someone removes it.

For this reason the most important test here is not that a declared
equivalence works: it is that with no equivalences declared, the pipeline
behaves exactly as it did before, byte for byte.
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
SCRIPTS = RADICE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(RADICE / "app"))

import prepare_sources  # noqa: E402
from prepare_manifest_sources import leggi_uguaglianze  # noqa: E402

EAN_GESTIONALE = "4009428623194"
EAN_FORNITORI = "8729721830575"


def prodotto(source_row: int, ean: str, descrizione: str) -> dict[str, Any]:
    return {"source_row": source_row, "ean": ean, "description": descrizione}


def riga(source_row: int, ean: str, descrizione: str, prezzo: str = "1.15",
         **extra: Any) -> dict[str, Any]:
    return {
        "source_row": source_row,
        "ean": ean,
        "description": descrizione,
        "unit_price_net": prezzo,
        "pieces_per_carton": "6",
        "usable": True,
        **extra,
    }


# The real case in miniature: the management software with one code, three
# suppliers with the other, and one supplier that uses the same code as the
# management software.
MASTER = [prodotto(330, EAN_GESTIONALE, "LUXA SAPONE LIQ. EROG.250ML")]
FORNITORI = {
    "cipresso": [riga(900, EAN_GESTIONALE, "LUXA SAPONE LIQUIDO EROGATORE 250", "1.28")],
    "noce": [riga(4794, EAN_FORNITORI, "LUXA SAPONE EROGATORE ORIGINAL ML.250", "1.15")],
    "betulla": [riga(1751, EAN_FORNITORI, "LUXA Sapone Liquido Original 250 Ml", "1.19")],
    "larice": [riga(1056, EAN_FORNITORI, "SAP. LIQ. LUXA BASE 250 TRADIZ.LE", "1.1625")],
}


def stati(matching: list[dict[str, Any]]) -> dict[str, str]:
    return {nome: voce["status"] for nome, voce in matching[0]["suppliers"].items()}


class SenzaDichiarazioniNienteCambiaTests(unittest.TestCase):
    """Protects the matches that already work today."""

    def test_il_risultato_e_identico_a_quello_di_prima(self) -> None:
        senza_argomento = prepare_sources.build_matching(MASTER, FORNITORI)
        con_none = prepare_sources.build_matching(MASTER, FORNITORI, uguaglianze=None)
        con_vuoto = prepare_sources.build_matching(MASTER, FORNITORI, uguaglianze=[])

        self.assertEqual(stati(senza_argomento[0]), {
            "cipresso": "EAN_ESATTO",
            "noce": "EAN_ASSENTE",
            "betulla": "EAN_ASSENTE",
            "larice": "EAN_ASSENTE",
        })
        self.assertEqual(senza_argomento[0], con_none[0])
        self.assertEqual(senza_argomento[0], con_vuoto[0])
        self.assertEqual(senza_argomento[1], con_vuoto[1])

    def test_un_gruppo_di_un_codice_solo_non_e_una_dichiarazione(self) -> None:
        matching, _, audit = prepare_sources.build_matching(
            MASTER, FORNITORI, uguaglianze=[[EAN_GESTIONALE], []],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_ASSENTE")
        self.assertEqual(audit["declared_equivalences"], 0)


class UnaDichiarazioneValeSuTuttiIFornitoriTests(unittest.TestCase):
    def esegui(self, uguaglianze: Any = ((EAN_GESTIONALE, EAN_FORNITORI),)):
        return prepare_sources.build_matching(
            MASTER, FORNITORI, uguaglianze=[list(gruppo) for gruppo in uguaglianze],
        )

    def test_i_tre_fornitori_diventano_abbinamenti_per_codice_esatto(self) -> None:
        """The whole point: nothing downstream needs a special case, because
        nothing downstream can tell the difference."""

        matching, _, _ = self.esegui()

        self.assertEqual(stati(matching), {
            "cipresso": "EAN_ESATTO",
            "noce": "EAN_ESATTO",
            "betulla": "EAN_ESATTO",
            "larice": "EAN_ESATTO",
        })

    def test_la_riga_dichiara_da_dove_viene(self) -> None:
        """Anyone reviewing a wrong order must be able to trace it back to the
        human decision that produced it: without this, a row matched through a
        declared equivalence is indistinguishable from one found by barcode."""

        matching, _, _ = self.esegui()
        noce = matching[0]["suppliers"]["noce"]

        self.assertEqual(noce["via_uguaglianza"], [EAN_FORNITORI])
        self.assertNotIn("via_uguaglianza", matching[0]["suppliers"]["cipresso"])

    def test_i_tre_escono_dalla_coda_dell_ai(self) -> None:
        """Not just a matched product: three fewer questions for the model, and
        three fewer risks of a wrong answer."""

        _, coda_senza, _ = prepare_sources.build_matching(MASTER, FORNITORI)
        _, coda_con, _ = self.esegui()

        self.assertEqual(len(coda_senza), 3)
        self.assertEqual(coda_con, [])

    def test_il_guadagno_si_conta(self) -> None:
        """An uncounted gain is as much a choice as an uncounted loss: this is
        the only place that shows whether these declarations still pay off."""

        _, _, audit = self.esegui()

        self.assertEqual(audit["matched_by_declared_equivalence"], {"noce": 1, "betulla": 1, "larice": 1})
        self.assertEqual(audit["declared_equivalences"], 1)
        self.assertEqual(audit["exact_unique_usable"]["noce"], 1)

    def test_la_catena_di_dichiarazioni_arriva_fino_in_fondo(self) -> None:
        """A≡B and B≡C: searching for A finds the row carrying C."""

        fornitori = {"noce": [riga(4794, "999", "LUXA SAPONE EROGATORE ORIGINAL ML.250")]}
        matching, _, _ = prepare_sources.build_matching(
            MASTER, fornitori, uguaglianze=[[EAN_GESTIONALE, EAN_FORNITORI, "999"]],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_ESATTO")

    def test_un_codice_scritto_in_un_altro_modo_si_ritrova_lo_stesso(self) -> None:
        """The management software's EANs are typed by hand, and spreadsheets
        read them back as numbers: without normalizing to digits, a
        declaration made one day would silently stop matching the next, with
        no visible reason."""

        fornitori = {"noce": [riga(4794, f" {EAN_FORNITORI} ", "LUXA ORIGINAL")]}
        matching, _, _ = prepare_sources.build_matching(
            MASTER, fornitori, uguaglianze=[[f"{EAN_GESTIONALE}.0", EAN_FORNITORI]],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_ESATTO")

    def test_lo_stesso_codice_ripetuto_non_duplica_la_riga(self) -> None:
        """The row must stay one: counting it twice would turn the match into
        `EAN_AMBIGUO`, an AI question instead of a match.

        `mappa_delle_uguaglianze` guarantees this by dropping repeated codes
        and the product's own code — a price-list row has exactly one EAN.
        This test checks the property itself, not the line of code that
        happens to produce it, so it still holds if the implementation moves.
        """

        fornitori = {"noce": [riga(4794, EAN_FORNITORI, "LUXA ORIGINAL")]}
        matching, _, _ = prepare_sources.build_matching(
            MASTER, fornitori,
            uguaglianze=[
                [EAN_GESTIONALE, EAN_FORNITORI],
                [EAN_GESTIONALE, f" {EAN_FORNITORI} "],
                [EAN_FORNITORI, EAN_FORNITORI],
            ],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_ESATTO")
        self.assertEqual(len(matching[0]["suppliers"]["noce"]["candidates"]), 1)
        atteso = {EAN_GESTIONALE: [EAN_FORNITORI], EAN_FORNITORI: [EAN_GESTIONALE]}
        # A repeated code inside one group, and the same pair declared twice
        # across two groups: the two ways a duplicate can show up.
        self.assertEqual(
            prepare_sources.mappa_delle_uguaglianze([[EAN_GESTIONALE, EAN_FORNITORI, EAN_GESTIONALE]]),
            atteso,
        )
        self.assertEqual(
            prepare_sources.mappa_delle_uguaglianze(
                [[EAN_GESTIONALE, EAN_FORNITORI], [EAN_GESTIONALE, f" {EAN_FORNITORI}"]],
            ),
            atteso,
        )

    def test_due_righe_diverse_restano_una_domanda_da_fare(self) -> None:
        """The right fallback: if the declaration brings two usable rows for the
        same supplier, the pipeline does not choose on its own — it asks."""

        fornitori = {"noce": [
            riga(4794, EAN_FORNITORI, "LUXA SAPONE EROGATORE ORIGINAL ML.250"),
            riga(4795, EAN_GESTIONALE, "LUXA SAPONE EROGATORE SETA ML.250"),
        ]}
        matching, coda, _ = prepare_sources.build_matching(
            MASTER, fornitori, uguaglianze=[[EAN_GESTIONALE, EAN_FORNITORI]],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_AMBIGUO")
        self.assertEqual([voce["reason"] for voce in coda], ["EAN_AMBIGUO"])


class IlFileDelleUguaglianzeTests(unittest.TestCase):
    """How a declaration travels from the service's SQLite store to the script."""

    def scrivi(self, contenuto: Any) -> Path:
        cartella = tempfile.TemporaryDirectory()
        self.addCleanup(cartella.cleanup)
        percorso = Path(cartella.name) / "uguaglianze.json"
        percorso.write_text(json.dumps(contenuto), encoding="utf-8")
        return percorso

    def test_non_chiederlo_e_legittimo(self) -> None:
        """Means this run has none, which is the normal case."""

        self.assertEqual(leggi_uguaglianze(None), [])

    def test_chiederlo_e_non_trovarlo_no(self) -> None:
        """These are human declarations: silently dropping them would revert
        manually matched products to their prior state with no visible sign."""

        with self.assertRaises(ValueError) as errore:
            leggi_uguaglianze(Path("questo-file-non-esiste.json"))

        self.assertIn("abbinati a mano", str(errore.exception))

    def test_si_legge_con_la_chiave_e_senza(self) -> None:
        """A hand-written test file must not fail over a missing wrapper key."""

        con_chiave = self.scrivi({"classi": [[EAN_GESTIONALE, EAN_FORNITORI]]})
        senza = self.scrivi([[EAN_GESTIONALE, EAN_FORNITORI]])

        atteso = [[EAN_GESTIONALE, EAN_FORNITORI]]
        self.assertEqual(leggi_uguaglianze(con_chiave), atteso)
        self.assertEqual(leggi_uguaglianze(senza), atteso)

    def test_i_gruppi_di_uno_non_arrivano_alla_catena(self) -> None:
        percorso = self.scrivi({"classi": [[EAN_GESTIONALE], [EAN_GESTIONALE, EAN_FORNITORI]]})

        self.assertEqual(leggi_uguaglianze(percorso), [[EAN_GESTIONALE, EAN_FORNITORI]])


class LoScriptAccettaLIngressoTests(unittest.TestCase):
    """The argument really exists on the command line, not just in the code."""

    def test_equivalenze_e_un_argomento_dichiarato(self) -> None:
        esito = subprocess.run(
            [sys.executable, str(SCRIPTS / "prepare_manifest_sources.py"), "--help"],
            capture_output=True, text=True, encoding="utf-8",
        )

        self.assertEqual(esito.returncode, 0, esito.stderr)
        self.assertIn("--equivalenze", esito.stdout)


if __name__ == "__main__":
    unittest.main()
