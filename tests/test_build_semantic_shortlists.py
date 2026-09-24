"""Rows with the product's EAN always enter the shortlist.

The score only looks at description tokens, and the two texts are written by
different people. Measured on the 1160 `EAN_ESATTO` pairs of a real run —
where the right row is certain because the EAN identifies it — a
description-only shortlist would have shown it 1014 times out of 1160: wrong
12.6% of the time.

For `EAN_ASSENTE` there is no remedy, and that is the normal case. But on
`EAN_AMBIGUO` the right row is one of the ones with that EAN, and leaving it
out means letting the model choose among the wrong ones — with `ALTA`
confidence and no on-screen confirmation. This is also the assumption
`merge_match_decisions.py` relies on downstream: without the forced inclusion
that rule could never be satisfied.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"


def riga(source_row: int, ean: str, description: str, prezzo: float = 1.0, **extra: Any) -> dict[str, Any]:
    return {
        "source_row": source_row,
        "ean": ean,
        "description": description,
        "unit_price_net": prezzo,
        "usable": True,
        **extra,
    }


def esegui(normalized: dict[str, Any], queue: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as temporanea:
        cartella = Path(temporanea)
        (cartella / "normalized.json").write_text(json.dumps(normalized), encoding="utf-8")
        (cartella / "queue.json").write_text(json.dumps(queue), encoding="utf-8")
        esito = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "build_semantic_shortlists.py"),
                "--normalized", str(cartella / "normalized.json"),
                "--queue", str(cartella / "queue.json"),
                "--output", str(cartella / "shortlists.json"),
                "--top-k", str(top_k),
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        if esito.returncode != 0:
            raise AssertionError(f"lo script è fallito: {esito.stdout}{esito.stderr}")
        return json.loads((cartella / "shortlists.json").read_text(encoding="utf-8"))


class LEanEntraSempreInShortlistTests(unittest.TestCase):
    def test_la_riga_con_lo_stesso_ean_entra_anche_se_la_descrizione_non_somiglia(self) -> None:
        """A real case: `CHANTE BRILL ANTICALCARE ACETO 625ML` versus
        `CHANTEBR. A/CALCARE 625 EXTRARAPIDO` shares no token, so it would not
        even enter the candidate pool by description alone."""
        normalized = {"betulla": [
            riga(100, "8001", "CHANTEBR. A/CALCARE 625 EXTRARAPIDO"),
            *[riga(200 + i, f"999{i}", f"CHANTE BRILL ANTICALCARE ALTRO {i}") for i in range(6)],
        ]}
        shortlists = esegui(normalized, [{
            "gestionale_source_row": 12, "supplier": "betulla", "ean": "8001",
            "description": "CHANTE BRILL ANTICALCARE ACETO 625ML", "reason": "EAN_AMBIGUO",
        }])
        righe = [candidato["source_row"] for candidato in shortlists[0]["candidates"]]
        self.assertIn(100, righe)
        self.assertIs(shortlists[0]["candidates"][0]["stesso_ean"], True)
        self.assertEqual(righe[0], 100, "l'EAN è una prova, il punteggio una somiglianza")

    def test_piu_righe_con_lo_stesso_ean_ci_stanno_tutte_anche_oltre_il_taglio(self) -> None:
        """On `EAN_AMBIGUO` the answer lives inside that group: capping it at
        `top_k` would remove the right answer along with the rest."""
        normalized = {"betulla": [riga(100 + i, "8001", f"STESSO PRODOTTO LOTTO {i}") for i in range(7)]}
        shortlists = esegui(normalized, [{
            "gestionale_source_row": 12, "supplier": "betulla", "ean": "8001",
            "description": "STESSO PRODOTTO", "reason": "EAN_AMBIGUO",
        }], top_k=5)
        candidati = shortlists[0]["candidates"]
        self.assertEqual(len(candidati), 7)
        self.assertTrue(all(candidato["stesso_ean"] for candidato in candidati))

    def test_una_riga_non_utilizzabile_non_entra_nemmeno_con_l_ean_giusto(self) -> None:
        """`EAN_PRESENTE_NON_UTILIZZABILE` exists for this: a row without a
        price or that cannot be ordered is not an offer, and showing it to the
        model would let it pick something that cannot be bought."""
        normalized = {"betulla": [
            riga(100, "8001", "IL PRODOTTO", usable=False),
            riga(101, "9999", "IL PRODOTTO SIMILE"),
        ]}
        shortlists = esegui(normalized, [{
            "gestionale_source_row": 12, "supplier": "betulla", "ean": "8001",
            "description": "IL PRODOTTO", "reason": "EAN_PRESENTE_NON_UTILIZZABILE",
        }])
        righe = [candidato["source_row"] for candidato in shortlists[0]["candidates"]]
        self.assertNotIn(100, righe)

    def test_senza_ean_non_cambia_niente(self) -> None:
        """This is the normal case, and the forced inclusion must not touch it:
        no candidate gets added, and the cut stays at `top_k`."""
        normalized = {"betulla": [riga(100 + i, f"800{i}", f"PRODOTTO SIMILE {i}") for i in range(8)]}
        shortlists = esegui(normalized, [{
            "gestionale_source_row": 12, "supplier": "betulla", "ean": "",
            "description": "PRODOTTO SIMILE", "reason": "EAN_ASSENTE",
        }], top_k=5)
        candidati = shortlists[0]["candidates"]
        self.assertEqual(len(candidati), 5)
        self.assertFalse(any(candidato["stesso_ean"] for candidato in candidati))

    def test_l_ean_del_gestionale_non_si_confonde_con_un_altro(self) -> None:
        normalized = {"betulla": [riga(100, "80010", "PRODOTTO"), riga(101, "8001", "PRODOTTO")]}
        shortlists = esegui(normalized, [{
            "gestionale_source_row": 12, "supplier": "betulla", "ean": "8001",
            "description": "PRODOTTO", "reason": "EAN_AMBIGUO",
        }])
        forzati = [c["source_row"] for c in shortlists[0]["candidates"] if c["stesso_ean"]]
        self.assertEqual(forzati, [101])


class IlCandidatoPortaLaRigaInteraTests(unittest.TestCase):
    """The shortlist's top row can become a proposed offer: it must carry the
    full row.

    When the AI rejects a match but the best candidate scored close, `build_
    review_data.offer_from_match` promotes that row to a proposal for the user
    to accept. Without pieces-per-carton the row comes back `available:
    false`, and the "Yes" button answers "the proposed row has no usable
    price and packaging": on a real comparison run, that meant 48 proposals
    and zero acceptable.
    """

    CAMPI_COMMERCIALI = (
        "supplier_code", "pieces_per_carton", "order_multiplier",
        "packaging", "availability", "unit", "pallet", "usable",
    )

    def shortlist(self) -> dict[str, Any]:
        normalized = {"noce": [riga(
            631, "5419373044415", "AURAPUR ELETTRICO RICARICA FIORI", 2.23,
            supplier_code="0000000429063", pieces_per_carton="9.0000", order_multiplier=None,
            packaging="CARTONE", availability="", unit="PZ", pallet=None,
        )]}
        return esegui(normalized, [{
            "gestionale_source_row": 26, "supplier": "noce", "ean": "8009214635130",
            "description": "AURAPUR ELETTRICO RICARICA FIORI ELEGANTI", "reason": "EAN_ASSENTE",
        }])[0]

    def test_la_confezione_arriva_fino_al_candidato(self) -> None:
        candidato = self.shortlist()["candidates"][0]

        for campo in self.CAMPI_COMMERCIALI:
            self.assertIn(campo, candidato, f"«{campo}» non arriva a chi costruisce l'offerta")
        self.assertEqual(candidato["pieces_per_carton"], "9.0000")
        self.assertEqual(candidato["supplier_code"], "0000000429063")

    def test_e_la_riga_diventa_un_offerta_ordinabile(self) -> None:
        """The test that matters: the same function the real comparison uses."""

        sys.path.insert(0, str(SCRIPTS))
        try:
            import build_review_data
        finally:
            sys.path.pop(0)
        candidato = self.shortlist()["candidates"][0]

        offerta = build_review_data.offer_from_match("noce", {
            "selected": candidato,
            "status": "SEMANTICO_PROPOSTO",
            "method": "CORREZIONE_UTENTE",
            "confidence": "UTENTE",
            "requires_user_confirmation": False,
            "rationale": "",
            "alternatives": [],
        }, None)

        self.assertIs(offerta["available"], True)
        self.assertEqual(offerta["quantityFactor"], 9.0)
        self.assertEqual(offerta["unitPriceNet"], 2.23)
        self.assertEqual(offerta["orderUnitPriceNet"], 20.07)

    def test_il_modello_pero_non_vede_niente_di_nuovo(self) -> None:
        """The fingerprint seals `(row, description, score)`, the same triple the
        model reads. If it changed, every decision already paid for would be
        discarded by `merge_match_decisions.py` as taken on a different list."""

        sys.path.insert(0, str(SCRIPTS))
        try:
            import build_semantic_shortlists
        finally:
            sys.path.pop(0)
        voce = self.shortlist()
        candidato = voce["candidates"][0]

        self.assertEqual(
            build_semantic_shortlists.impronta_caso(
                voce["gestionale_source_row"], voce["supplier"], voce["description"],
                [(candidato["source_row"], candidato["description"], candidato["score"])],
            ),
            build_semantic_shortlists.impronta_caso(
                26, "noce", "AURAPUR ELETTRICO RICARICA FIORI ELEGANTI",
                [(631, "AURAPUR ELETTRICO RICARICA FIORI", candidato["score"])],
            ),
        )



def modulo():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import build_semantic_shortlists
    finally:
        sys.path.pop(0)
    return build_semantic_shortlists


class LeQuantitaNelNomeTests(unittest.TestCase):
    """Quantities are read even with the unit written before the number: "X 18", "PZ.18".

    A real case: the management software writes `LINDA SETA ULTRA LUNGO ALI
    18PZ`, one supplier writes `ASS. LINDA SETAMORBI X 18 LUNGO`. Without
    reading either form, the top of the shortlist holds `X 9`, a different
    product: the right row never gets accepted, and the product goes to a
    pricier supplier instead of the cheaper one with the same barcode.

    The principle these tests defend: better to not read a number than to
    read it wrong, because a false conflict subtracts 0.35 from the correct
    row's score.
    """

    def leggi(self, testo: str) -> dict[str, list[float]]:
        return modulo().attributes(testo)

    def test_le_forme_con_l_unita_davanti(self) -> None:
        attese = {
            "X 18": {"PEZZI": [18.0]},
            "X18": {"PEZZI": [18.0]},
            "PZ.18": {"PEZZI": [18.0]},
            "PZ 18": {"PEZZI": [18.0]},
            "LT.3": {"ML": [3000.0]},
            "ML.500": {"ML": [500.0]},
            "KG4": {"G": [4000.0]},
            "KG 4": {"G": [4000.0]},
            "SALVACAM.ML.150": {"ML": [150.0]},
            "VER.KG.1,5": {"G": [1500.0]},
        }
        for testo, attesa in attese.items():
            with self.subTest(testo=testo):
                self.assertEqual(self.leggi(testo), attesa)

    def test_litri_e_grammi_scritti_attaccati(self) -> None:
        """"3LT" and "250GR" need a word boundary that doesn't fall inside `LT`
        or `GR`, right after the `L`/`G`."""

        self.assertEqual(self.leggi("AXO 3LT"), {"ML": [3000.0]})
        self.assertEqual(self.leggi("PATE 250GR"), {"G": [250.0]})
        self.assertEqual(self.leggi("1,5L"), {"ML": [1500.0]})

    def test_le_somme_valgono_come_base_e_come_totale(self) -> None:
        self.assertEqual(self.leggi("MORBY LAV RIC 70+8 LAV"), {"LAVAGGI": [70.0, 78.0]})
        self.assertEqual(self.leggi("LINDA COTONE PZ.8+2"), {"PEZZI": [8.0, 10.0]})
        self.assertEqual(self.leggi("BAGNO 400+100ML"), {"ML": [400.0, 500.0]})
        self.assertEqual(self.leggi("MORBY SACCO 78 MISURINI"), {"LAVAGGI": [78.0]})

    def test_quello_che_non_e_una_quantita_non_si_legge(self) -> None:
        for testo in (
            "PROSACK 30 X 40 CM", "SACCHETTI 50X70", "TEMPE 3 VELI", "SHAMPOO 2IN1",
            "COLORE 1-1", "CODICE E1", "DEET 50%", "EXCELSIORE CREME N.4", "VIAKOL 470+30",
        ):
            with self.subTest(testo=testo):
                self.assertEqual(self.leggi(testo), {})

    def test_la_x_dopo_un_numero_o_prima_di_un_unita_e_un_per(self) -> None:
        self.assertEqual(self.leggi("BIRRA 2 X 250ML"), {"ML": [250.0]})
        self.assertEqual(self.leggi("AREX ABR. COL. X 10 PZ 1276"), {"PEZZI": [10.0]})
        self.assertEqual(self.leggi("VITAFORZA 7 PZ 70 GR"), {"PEZZI": [7.0], "G": [70.0, 490.0]})

    def test_l_unita_col_punto_attaccato_ha_il_suo_numero(self) -> None:
        """Found on real price lists: reading "X 2 GR.90" as 2 grams gives the
        right row a false conflict against "GR.90 X 2" and drops it from the shortlist."""

        attese = {
            "LUXA SAPONETTA ORIGINAL X 2 GR.90": {"PEZZI": [2.0], "G": [90.0, 180.0]},
            "ROBINET SAPONETTE IDRATANTE X 3 GR.100": {"PEZZI": [3.0], "G": [100.0, 300.0]},
            "LUCE SAPONE BUCATO GIALLO X 2 GR. 500": {"PEZZI": [2.0], "G": [500.0, 1000.0]},
            "CALGOR POLVERE 4 IN 1 GR.900": {"G": [900.0]},
            "WHISKAT GATTO PRAN.CARNI M.+1 GR.50X6": {"G": [50.0]},
            "SPRAY 2 ML.1000": {"ML": [1000.0]},
        }
        for testo, attesa in attese.items():
            with self.subTest(testo=testo):
                self.assertEqual(self.leggi(testo), attesa)
        esito = modulo().score_pair(
            {"description": "LUXA SAPONE GR.90 X 2 ORIGINAL"},
            {"description": "LUXA SAPONETTA ORIGINAL X 2 GR.90"},
        )
        self.assertEqual(esito["attribute_conflicts"], [])

    def test_il_volume_con_l_unita_davanti_e_lo_spazio(self) -> None:
        """One supplier writes "PH 3.5 ML 200": that's 200 ML, not 3.5.
        Found by comparing real price lists (the right row moved from first to
        forty-fourth place). But if the following number carries its own unit,
        the unit stays with the earlier number."""

        self.assertEqual(self.leggi("CHIARY INTIMO PH 3.5 ML 200"), {"ML": [200.0]})
        self.assertEqual(self.leggi("LUXA DEO SPRAY ITA ML 150 FRESH"), {"ML": [150.0]})
        self.assertEqual(self.leggi("SPUGNE 10 PZ 1276"), {"PEZZI": [10.0]})
        self.assertEqual(self.leggi("SACCHI 50 LT 10 PZ")["PEZZI"], [10.0])
        # A number glued to letters is not a quantity: this is 250 ml.
        self.assertEqual(self.leggi("NORD&SHAMPO SH. 250 ML 2IN1 APPLE"), {"ML": [250.0]})
        # A dot and space with a leading number that isn't a piece count: the
        # grams are the earlier ones, "100 PIU'" is part of the name.
        self.assertEqual(self.leggi("OMONE B. ADDITIVO 500 GR. 100 PIU' CLASS"), {"G": [500.0]})
        self.assertIn(50000.0, self.leggi("SACCHI 50 LT 10 PZ")["ML"])

    def test_pezzi_per_peso_vale_anche_il_totale(self) -> None:
        esito = modulo().score_pair(
            {"description": "OVA PERLA SAPONE GR.250 X 2"},
            {"description": "OVA SAPONE PERLA BUCATO X 2 GR.500"},
        )
        self.assertEqual(esito["attribute_conflicts"], [])
        esito = modulo().score_pair(
            {"description": "MR FORZUTO IDRAULICO GEL 1LT 2PZ"},
            {"description": "MR FORZUTO Bipacco 2x1000 2000 Ml"},
        )
        self.assertEqual(esito["attribute_conflicts"], [])

    def test_fasce_taglie_e_formule_non_sono_la_confezione(self) -> None:
        """Adversarial check: diapers print the child's weight range ("11-25
        KG", "KG. 11/25"), and reading it naively picks a different end
        depending on the price list; "5°MIS." is a size, not five scoops; in
        "54 DOSI X 12=648 GR", 12 is the weight of one dose."""

        for testo in (
            "HUGLIES Pannolini Bimbi Unisole Junior Taglia 5 11-25kg 14 P",
            "HUGLIES UNISOLE 5°MIS.11-25KG JUNIOR",
            "PAMPINO Pannolino Protezione Pura Maxi Taglia 4 7-18 Kg 19 P",
            "HUGLIES PANN. TAGLIA 5 X 14 KG. 11/25",
            "PAMPINO 12-17 kg 34 Pez",
            "PAMPINO Kg 2x13=26 P",
            "PEDIGRAN 5-15 Kg MANZO",
        ):
            with self.subTest(testo=testo):
                self.assertNotIn("G", self.leggi(testo))
                self.assertNotIn("LAVAGGI", self.leggi(testo))
        self.assertEqual(self.leggi("Drynite 9 Pz.30-48 KG"), {"PEZZI": [9.0]})
        self.assertEqual(self.leggi("BIO LESTOS Power Caps 54 Dosi x 12=648 Gr"), {"G": [648.0]})
        self.assertEqual(self.leggi("DIXOR Busta Power 44 Dosi x14 616 Gr"), {"G": [616.0]})
        # A following number with no unit of its own does not take the pieces from the X.
        self.assertEqual(self.leggi("FAZZ. TEMPE BOX X 80 4veli"), {"PEZZI": [80.0]})
        self.assertEqual(self.leggi("TOV. VIX CLASS DUEVELI X50 33x33"), {"PEZZI": [50.0]})

    def test_con_lo_spazio_l_unita_passa_solo_a_un_numero_piu_grande(self) -> None:
        self.assertEqual(self.leggi("DENT. DENT-X KIDS 60 ML 0-6 FROZEN"), {"ML": [60.0]})
        self.assertEqual(self.leggi("CHICCA Biberon Fast-Silicone 330 Ml 3 Fori"), {"ML": [330.0]})
        self.assertEqual(self.leggi("GLOBO 2/1 ML 250"), {"ML": [250.0]})

    def test_un_numero_enorme_non_ferma_la_shortlist(self) -> None:
        self.assertEqual(self.leggi("1" * 5000 + "+1 LAV"), {"LAVAGGI": [1.0]})

    def test_eta_e_protezione_non_sono_quantita(self) -> None:
        self.assertEqual(self.leggi("L'AUREL ATTIVA ANTIRUGHE 45+ ML.50"), {"ML": [50.0]})
        self.assertEqual(self.leggi("NEVAL SUN FP 50+ 200ML"), {"ML": [200.0]})
        self.assertEqual(self.leggi("BADELUX DOCCIA FRESH 2IN1 ML.600"), {"ML": [600.0]})

    def test_una_l_sola_dopo_un_intero_grande_sono_lavaggi(self) -> None:
        self.assertEqual(self.leggi("COCCOLONE CONC 952ML 45L"), {"ML": [952.0], "LAVAGGI": [45.0]})
        self.assertEqual(self.leggi("FELCE LIM. AMMORB LT2 40L"), {"ML": [2000.0], "LAVAGGI": [40.0]})
        self.assertEqual(self.leggi("SACCHI 110LT"), {"ML": [110000.0]})

    def test_normalize_text_resta_quello_di_prima(self) -> None:
        """Tokens, the candidate index, and text similarity are unchanged: the
        "+" is kept only for reading quantities."""

        self.assertEqual(modulo().normalize_text("A+B 70+8"), "A B 70 8")

    def test_coppie_vere_con_lo_stesso_ean_non_sono_in_conflitto(self) -> None:
        coppie = (
            ("CHICCA AMMORBIDENTE 750ML TALCO", "CHICCA AMMORB. 750 28L TALCO"),
            ("BADELUX DOCCIA 600ML FRESH 2IN1", "BADELUX DOCCIA FRESH 2IN1 ML.600"),
            ("DOMOBOX SPAZZO 15PZ LAVANDA", "DOMOBOX NETTEZZA LAVANDA LT.28 PZ.15"),
            ("L`AUREL CREMA  ATTIVA ANTIRUGHE 50ML 45+", "L'AUREL ATTIVA ANTIRUGHE 45+ ML.50"),
            ("MANTEGANI BAGNO 400ML ALOE", "BAGNO MANTEGANI 400+100ML ALOE&AVOC"),
            ("MORBY SACCO 78 MISURINI CLASSICO", "MORBY LAV RIC 70+8 LAV CLASSICO"),
            ("LINDA SETA ULTRA LUNGO ALI 18PZ", "ASS. LINDA SETAMORBI X 18 LUNGO"),
        )
        for gestionale, listino in coppie:
            with self.subTest(gestionale=gestionale):
                esito = modulo().score_pair({"description": gestionale}, {"description": listino})
                self.assertEqual(esito["attribute_conflicts"], [])

    def test_formati_diversi_restano_in_conflitto(self) -> None:
        esito = modulo().score_pair(
            {"description": "LINDA SETA ULTRA LUNGO ALI 18PZ"},
            {"description": "ASS. LINDA SETAMORBI X  9 LUNGO ALI"},
        )
        self.assertEqual(esito["attribute_conflicts"], ["PEZZI"])
        esito = modulo().score_pair(
            {"description": "MORVIS DENTIFRICIO CLASSICO 75 ML"},
            {"description": "DENT. MORVIS 85 ML CLASSICO"},
        )
        self.assertEqual(esito["attribute_conflicts"], ["ML"])

    def test_nella_shortlist_delle_lines_la_x_18_viene_prima_della_x_9(self) -> None:
        shortlist = esegui(
            {"larice": [
                riga(8, "8009549285017", "ASS. LINDA SETAMORBI X  9 LUNGO ALI", 1.15),
                riga(1975, "8009496220932", "ASS. LINDA SETAMORBI X 18 LUNGO", 2.25),
            ]},
            [{
                "gestionale_source_row": 273, "supplier": "larice", "ean": "8009405394204",
                "description": "LINDA SETA ULTRA LUNGO ALI 18PZ", "status": "EAN_ASSENTE",
            }],
        )
        candidati = shortlist[0]["candidates"]
        self.assertEqual(candidati[0]["source_row"], 1975)
        self.assertEqual(candidati[1]["attribute_conflicts"], ["PEZZI"])


if __name__ == "__main__":
    unittest.main()
