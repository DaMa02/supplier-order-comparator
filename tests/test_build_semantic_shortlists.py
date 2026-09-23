"""Le righe con l'EAN del prodotto entrano sempre in shortlist.

Il punteggio guarda solo i token della descrizione, e i due testi li scrivono
due persone diverse. Misurato sulle 1160 coppie `EAN_ESATTO` della run vera —
dove la riga giusta e' certa perche' l'EAN la identifica — la shortlist per sola
descrizione l'avrebbe mostrata 1014 volte su 1160: **il 12,6% delle volte no**.

Per un `EAN_ASSENTE` non c'e' rimedio ed e' il caso normale. Ma su un
`EAN_AMBIGUO` la riga giusta e' una di quelle con l'EAN, e lasciarla fuori vuol
dire far scegliere il modello fra le sbagliate — con `ALTA` e senza conferma a
schermo. E' anche il presupposto della regola che `merge_match_decisions.py`
applica a valle: senza la forzatura sarebbe insoddisfabile.
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
        """Il caso vero: `CHANTE BRILL ANTICALCARE ACETO 625ML` contro
        `CHANTEBR. A/CALCARE 625 EXTRARAPIDO` non condivide un token, quindi non
        entra nemmeno nel gruppo dei candidati."""
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
        """Su un `EAN_AMBIGUO` la scelta sta dentro quel gruppo: tagliarlo a
        `top_k` vorrebbe dire togliere di mezzo la risposta giusta."""
        normalized = {"betulla": [riga(100 + i, "8001", f"STESSO PRODOTTO LOTTO {i}") for i in range(7)]}
        shortlists = esegui(normalized, [{
            "gestionale_source_row": 12, "supplier": "betulla", "ean": "8001",
            "description": "STESSO PRODOTTO", "reason": "EAN_AMBIGUO",
        }], top_k=5)
        candidati = shortlists[0]["candidates"]
        self.assertEqual(len(candidati), 7)
        self.assertTrue(all(candidato["stesso_ean"] for candidato in candidati))

    def test_una_riga_non_utilizzabile_non_entra_nemmeno_con_l_ean_giusto(self) -> None:
        """`EAN_PRESENTE_NON_UTILIZZABILE` esiste apposta: una riga senza prezzo
        o non ordinabile non e' un'offerta, e mostrarla al modello lo farebbe
        scegliere qualcosa che non si puo' comprare."""
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
        """E' il caso normale — 948 su 948 nella run vera — e la forzatura non
        deve toccarlo: nessun candidato viene aggiunto, e il taglio resta a
        `top_k`."""
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
    """La prima riga della shortlist puo' diventare un'offerta: deve essere intera.

    Quando l'AI rifiuta un abbinamento ma il candidato migliore somigliava
    molto, `build_review_data.offer_from_match` promuove quella riga a proposta
    da accettare.  Con le sole quattro colonne di prima nasceva senza pezzi per
    collo — quindi `available: false` — e il «Si» della pagina rispondeva «la
    riga proposta non ha prezzo e confezione utilizzabili»: sul confronto vero
    del 17 agosto 2026, **48 proposte e zero accettabili**.
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
        """La prova che conta: la stessa funzione del confronto vero."""

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
        """L'impronta sigilla `(riga, descrizione, punteggio)`: quella che il
        modello legge. Se cambiasse, ogni decisione gia' pagata verrebbe buttata
        da `merge_match_decisions.py` come «presa su un elenco diverso»."""

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
    """Le quantita' si leggono anche con l'unita' davanti: «X 18», «PZ.18».

    Caso vero, 18 settembre 2026: il gestionale scrive `LINDA SETA ULTRA LUNGO
    ALI 18PZ`, LARICE `ASS. LINDA SETAMORBI X 18 LUNGO`. Nessuna delle due forme
    LARICE si leggeva, e in testa alla shortlist c'era `X 9`, un altro prodotto:
    la riga giusta non e' stata accettata e il prodotto e' andato a NOCE a
    2,31 invece che a LARICE a 2,25.

    Il principio che queste prove difendono: meglio non leggere un numero che
    leggerlo sbagliato, perche' un falso conflitto toglie 0,35 alla riga giusta.
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
        """«3LT» e «250GR» prima non si leggevano: il confine di parola dopo `L`
        o `G` cadeva dentro `LT` e `GR`."""

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
        """Trovato dalla revisione del 21 settembre 2026 sui listini veri: in
        «X 2 GR.90» si leggevano 2 grammi, e la riga giusta prendeva un falso
        conflitto contro «GR.90 X 2» e usciva dalla shortlist."""

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
        """ACERO scrive «PH 3.5 ML 200»: 200 ML, non 3,5. Trovato sul banco
        fra listini del 21 settembre 2026 (la riga giusta passava dal primo al
        quarantaquattresimo posto). Se il numero dopo ha un'unita' sua, invece,
        l'unita' resta al numero di prima."""

        self.assertEqual(self.leggi("CHIARY INTIMO PH 3.5 ML 200"), {"ML": [200.0]})
        self.assertEqual(self.leggi("LUXA DEO SPRAY ITA ML 150 FRESH"), {"ML": [150.0]})
        self.assertEqual(self.leggi("SPUGNE 10 PZ 1276"), {"PEZZI": [10.0]})
        self.assertEqual(self.leggi("SACCHI 50 LT 10 PZ")["PEZZI"], [10.0])
        # Un numero attaccato a lettere non e' una quantita': sono 250 ml.
        self.assertEqual(self.leggi("NORD&SHAMPO SH. 250 ML 2IN1 APPLE"), {"ML": [250.0]})
        # Punto e spazio con un numero prima che non conta pezzi: i grammi sono
        # quelli di prima, «100 PIU'» e' il nome.
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
        """Verifica avversariale del 21 settembre 2026: i pannolini scrivono il
        peso del bambino («11-25 KG», «KG. 11/25»), e ogni listino ne leggeva
        un estremo diverso; «5°MIS.» e' una taglia, non cinque misurini; in
        «54 DOSI X 12=648 GR» 12 sono i grammi di una dose."""

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
        # Il numero dopo senza unita' sua non toglie i pezzi alla X.
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
        """Token, indice dei candidati e somiglianza del testo non cambiano:
        il «+» si tiene solo per leggere le quantita'."""

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
