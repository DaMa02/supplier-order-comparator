#!/usr/bin/env python3
"""Il ponte fra i listini e il motore delle promozioni.

Qui si prova quello che il motore da solo non puo' provare: che cosa il
lettore di un listino riconosce come condizione commerciale, che cosa perde
per strada e se quello che perde lascia una traccia che l'utente puo' vedere.

⚠ Fino al 15 agosto 2026 il lettore conosceva **un fornitore solo**: andava a
prendere `larice_v1` per nome e scriveva «LARICE» nei messaggi.  Da qui in
avanti chi viene letto lo dice il registro, e le prove che contano di piu'
sono due: quella sul listino LARICE vero — 13 condizioni su 13, come prima — e
quella su un fornitore inventato che il codice non nomina da nessuna parte.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "scripts"))
sys.path.insert(0, str(RADICE / "app"))

import promotion_bridge  # noqa: E402
from promotions import KIND_AMBIGUOUS, KIND_INCLUDED_PACK, KIND_THRESHOLD_GIFT  # noqa: E402


# Le colonne del listino Larice usate dal lettore: G descrizione, J nome del
# premio, P codice dello sconto, R codice a barre.
COLONNA_G = 7
COLONNA_J = 10
COLONNA_P = 16
COLONNA_R = 18


def scrivi_listino(
    percorso: Path,
    righe: list[dict[str, object]],
    colonne: dict[str, int] | None = None,
) -> Path:
    """Costruisce un listino Larice finto con le sole colonne che contano.

    `colonne` serve a spostarle: e' l'unico modo per provare che il lettore
    segue il registro invece della posizione che si era imparato a memoria.
    """

    posizioni = colonne or {"g": COLONNA_G, "j": COLONNA_J, "p": COLONNA_P, "r": COLONNA_R}
    libro = Workbook()
    foglio = libro.active
    foglio.title = "Canvass di prova"
    for numero, contenuto in enumerate(righe, start=1):
        for chiave, colonna in posizioni.items():
            valore = contenuto.get(chiave)
            if valore not in (None, ""):
                foglio.cell(row=numero, column=colonna, value=valore)
    libro.save(percorso)
    libro.close()
    return percorso


def riga_merce(nome: str, ean: str) -> dict[str, object]:
    return {"g": nome, "p": "TP", "r": ean}


class LetturaDeiBlocchiLarice(unittest.TestCase):
    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="ponte-promozioni-"))
        self.servizio = promotion_bridge.PromotionService()

    def leggi(self, righe: list[dict[str, object]]) -> list[dict]:
        percorso = scrivi_listino(self.cartella / "larice.xlsx", righe)
        promozioni, errore = self.servizio.condizioni_di("larice", percorso)
        self.assertIsNone(errore)
        return promozioni

    def test_l_ean_della_riga_premio_entra_nel_contratto(self):
        """L'EAN del premio era gia' letto due righe sopra e buttato via.

        Senza, `reward.ean` resta nullo su tutte le soglie e non c'e' modo di
        risalire all'articolo regalato quando arriva la merce.
        """

        promozioni = self.leggi([
            {"g": "ACQUISTANDO 2 CT TRA"},
            riga_merce("BIOPUNTO LATTE CORPO 75ML", "8059651466354"),
            riga_merce("BIOPUNTO LATTE CORPO 200ML", "8059373360282"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "BIOPUNTO SET MARE", "p": "SM", "r": "8050507999999"},
        ])

        self.assertEqual(len(promozioni), 1)
        self.assertEqual(promozioni[0]["kind"], KIND_THRESHOLD_GIFT)
        self.assertEqual(promozioni[0]["reward"]["ean"], "8050507999999")
        self.assertEqual(promozioni[0]["reward"]["description"], "BIOPUNTO SET MARE")
        # La riga premio non e' merce che fa raggiungere la soglia.
        self.assertEqual(promozioni[0]["eligible"]["source_rows"], [2, 3])

    def test_una_grafia_reale_del_verbo_apre_comunque_il_blocco(self):
        """«ACQUISTANO» (senza la D) sta alla riga 978 del listino del 3-6
        agosto: il ponte teneva una copia piu' povera della regola e quella
        condizione, con tre righe di merce, spariva senza una parola."""

        promozioni = self.leggi([
            {"g": "ACQUISTANO 5 CT TRA"},
            riga_merce("DENT. SENSODENT 80+20ML FRESH CLEAN", "5059833580611"),
            riga_merce("DENT. SENSODENT 80+20ML COMPLEX", "5059905473735"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "DENT. SENSODENT 15 ML", "p": "SM", "r": "5059187155220"},
        ])

        self.assertEqual(len(promozioni), 1)
        self.assertEqual(promozioni[0]["kind"], KIND_THRESHOLD_GIFT)
        self.assertEqual(promozioni[0]["threshold"], {"qty": 5, "unit": "cartoni"})

    def test_un_blocco_che_non_si_calcola_resta_da_verificare(self):
        """Il testo c'e', la merce c'e', ma la soglia non e' calcolabile.

        Prima il blocco spariva in silenzio: merce con una condizione
        commerciale che nessuno avrebbe piu' visto.
        """

        promozioni = self.leggi([
            {"g": "ACQUISTANDO 3 CT TRA"},
            riga_merce("PRODOTTO UNO", "8000000000001"),
            {"g": "IN OMAGGIO", "j": "", "p": "SM"},
        ])

        self.assertEqual(len(promozioni), 1)
        self.assertEqual(promozioni[0]["kind"], KIND_AMBIGUOUS)
        self.assertFalse(promozioni[0]["confirmed"])
        self.assertFalse(promozioni[0]["economic_effect"]["affects_total"])
        self.assertIn("non ricomposta", promozioni[0]["source_text"])
        self.assertEqual(promozioni[0]["eligible"]["source_rows"], [2])

    def test_una_unita_mai_vista_apre_comunque_il_blocco(self):
        """Il riconoscimento dell'intestazione e' apposta piu' largo del
        calcolo: se pretendesse anche l'unita', un giorno che il fornitore
        scrive «SCATOLE» il blocco non si aprirebbe nemmeno e la condizione
        sparirebbe senza traccia invece di finire fra quelle da verificare."""

        promozioni = self.leggi([
            {"g": "ACQUISTANDO 10 SCATOLE TRA"},
            riga_merce("PRODOTTO UNO", "8000000000001"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "PREMIO", "p": "SM", "r": "8000000009999"},
        ])

        self.assertEqual(len(promozioni), 1)
        self.assertEqual(promozioni[0]["kind"], KIND_AMBIGUOUS)
        self.assertIn("ACQUISTANDO 10 SCATOLE", promozioni[0]["source_text"])
        self.assertEqual(promozioni[0]["eligible"]["source_rows"], [2])

    def test_un_blocco_troppo_lungo_lascia_una_traccia(self):
        """Oltre cinquecento righe il blocco si abbandona: giusto, ma non in
        silenzio."""

        righe: list[dict[str, object]] = [{"g": "ACQUISTANDO 4 CT TRA"}]
        righe.extend(riga_merce(f"PRODOTTO {n}", f"800000000{n:04d}") for n in range(3))
        # Righe senza EAN: sono quelle che fanno scattare il conto della
        # distanza dall'intestazione.
        righe.extend({"g": ""} for _ in range(520))
        righe.append({"g": "IN OMAGGIO 1 CT DI", "j": "PREMIO", "p": "SM", "r": "8000000009999"})

        promozioni = self.leggi(righe)

        tracce = [p for p in promozioni if p["kind"] == KIND_AMBIGUOUS]
        # Due perdite distinte: l'intestazione abbandonata e, piu' avanti, la
        # riga premio che si ritrova senza intestazione. Entrambe si vedono.
        self.assertEqual(len(tracce), 2)
        abbandono = next(p for p in tracce if "500 righe" in p["source_text"])
        self.assertEqual(abbandono["eligible"]["source_rows"], [2, 3, 4])
        self.assertTrue(any("manca l'intestazione" in p["source_text"] for p in tracce))
        self.assertEqual([p["kind"] for p in promozioni], [KIND_AMBIGUOUS, KIND_AMBIGUOUS])

    def test_una_seconda_intestazione_non_cancella_la_prima_in_silenzio(self):
        promozioni = self.leggi([
            {"g": "ACQUISTANDO 2 CT TRA"},
            riga_merce("PRODOTTO UNO", "8000000000001"),
            {"g": "ACQUISTANDO 6 CT TRA"},
            riga_merce("PRODOTTO DUE", "8000000000002"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "PREMIO", "p": "SM", "r": "8000000009999"},
        ])

        tipi = [p["kind"] for p in promozioni]
        self.assertEqual(tipi.count(KIND_THRESHOLD_GIFT), 1)
        self.assertEqual(tipi.count(KIND_AMBIGUOUS), 1)
        traccia = next(p for p in promozioni if p["kind"] == KIND_AMBIGUOUS)
        self.assertIn("ACQUISTANDO 2 CT TRA", traccia["source_text"])
        self.assertIn("intestazione successiva", traccia["source_text"])

    def test_un_blocco_aperto_a_fine_foglio_lascia_una_traccia(self):
        promozioni = self.leggi([
            {"g": "ACQUISTANDO 2 CT TRA"},
            riga_merce("PRODOTTO UNO", "8000000000001"),
        ])

        self.assertEqual(len(promozioni), 1)
        self.assertEqual(promozioni[0]["kind"], KIND_AMBIGUOUS)
        self.assertIn("finisce prima", promozioni[0]["source_text"])

    def test_una_riga_premio_senza_intestazione_lascia_una_traccia(self):
        promozioni = self.leggi([
            riga_merce("PRODOTTO UNO", "8000000000001"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "PREMIO", "p": "SM", "r": "8000000009999"},
        ])

        self.assertEqual(len(promozioni), 1)
        self.assertEqual(promozioni[0]["kind"], KIND_AMBIGUOUS)
        self.assertIn("manca l'intestazione", promozioni[0]["source_text"])

    def test_le_condizioni_non_ricomposte_sono_contate(self):
        """Il numero serve a chi guarda il servizio: non si contano a mano."""

        percorso = scrivi_listino(self.cartella / "larice.xlsx", [
            {"g": "ACQUISTANDO 2 CT TRA"},
            riga_merce("PRODOTTO UNO", "8000000000001"),
        ])
        review = {"files": [{"supplierId": "larice", "sourcePath": str(percorso)}], "products": []}

        promozioni = self.servizio.detect(review)

        self.assertEqual(self.servizio.condizioni_da_verificare["larice"], 1)
        self.assertEqual(len(promozioni), 1)
        # Il conteggio sopravvive alla seconda lettura, che usa la cache.
        self.servizio.detect(review)
        self.assertEqual(self.servizio.condizioni_da_verificare["larice"], 1)

    def test_gli_sconti_gia_nel_prezzo_non_entrano_nell_elenco(self):
        """⚠ 165 condizioni su 166 erano «Sconto numerico 10%», una per riga.

        Con le parole di chi le leggeva: «LARICE fa una INFINITA di sconti,
        mantieni solo offerte come quella del tostapane, della bistecchiera,
        del sale lavastoviglie e simili».  Sepolte in mezzo agli sconti, le
        soglie con omaggio non si vedevano.  Non si perde nessun calcolo: uno
        sconto gia' compreso nel prezzo non ha mai prodotto un prezzo.
        """

        percorso = scrivi_listino(self.cartella / "larice.xlsx", [
            {"g": "ACQUISTANDO 2 CT TRA"},
            riga_merce("PRODOTTO UNO", "8000000000001"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "SALE LAVASTOVIGLIE KG1", "p": "SM", "r": "8000000009999"},
        ])
        review = {
            "files": [{"supplierId": "larice", "sourcePath": str(percorso)}],
            "products": [
                {
                    "id": f"product:{numero}",
                    "ean": f"800000000{numero:04d}",
                    "offers": [{
                        "supplierId": "larice",
                        "sourceRow": numero,
                        "available": True,
                        "discountRate": 0.1,
                    }],
                }
                for numero in range(1, 6)
            ],
        }

        promozioni = self.servizio.detect(review)

        self.assertEqual([p["kind"] for p in promozioni], [KIND_THRESHOLD_GIFT])
        self.assertEqual(self.servizio.sconti_gia_nel_prezzo, 5)

    def test_uno_sconto_che_il_prezzo_non_contiene_resta(self):
        """Quello cambia il totale: toglierlo sarebbe nascondere un numero."""

        promozione = promotion_bridge.detect_numeric_discount(
            supplier="larice",
            source_reference="larice:riga:1",
            source_text="Sconto numerico 10%",
            discount_value=0.1,
            eligible={"products": ["product:1"]},
            already_applied=False,
        )

        tenute = self.servizio._solo_quelle_che_cambiano_qualcosa([promozione])

        self.assertEqual(len(tenute), 1)
        self.assertEqual(self.servizio.sconti_gia_nel_prezzo, 0)

    def test_un_listino_senza_perdite_non_inventa_tracce(self):
        promozioni = self.leggi([
            {"g": "ACQUISTANDO 2 CT TRA"},
            riga_merce("PRODOTTO UNO", "8000000000001"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "PREMIO", "p": "SM", "r": "8000000009999"},
        ])

        self.assertEqual([p["kind"] for p in promozioni], [KIND_THRESHOLD_GIFT])


class ConUnRegistroFinto(unittest.TestCase):
    """Sostituisce il registro degli adattatori per la durata di un test.

    Si parte sempre da quello vero e si cambia una sola dichiarazione: un
    adattatore porta anche altro — i codici di riga di Larice dicono che `SM`
    marca il premio e non merce acquistabile — e una copia piu' povera
    proverebbe qualcosa che nel programma non succede mai.
    """

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="registro-finto-"))
        self.registro_vero = promotion_bridge.registro.REGISTRO

    def tearDown(self) -> None:
        promotion_bridge.registro.REGISTRO = self.registro_vero

    def registro_finto(self, modifica) -> None:
        documento = json.loads(self.registro_vero.read_text(encoding="utf-8"))
        modifica(documento)
        percorso = self.cartella / "adapters.json"
        percorso.write_text(json.dumps(documento, ensure_ascii=False), encoding="utf-8", newline="")
        promotion_bridge.registro.REGISTRO = percorso

    @staticmethod
    def adattatore(documento: dict, identificativo: str) -> dict:
        return next(voce for voce in documento["adapters"] if voce["id"] == identificativo)


class ColonneDalRegistro(ConUnRegistroFinto):
    """Dove stanno le colonne del listino Larice lo dice il registro.

    Finche' le posizioni stavano scritte dentro il lettore, il giorno che
    Larice ne sposta una — o che l'utente conferma una variazione di schema, e
    il registro impara la mappatura nuova — i blocchi non si formavano piu' e
    le soglie con omaggio sparivano dal riepilogo senza un avviso: chi ordina
    4 cartoni invece di 5 perde il cartone in omaggio e non lo sa.
    """

    SOGLIA = [
        {"g": "ACQUISTANDO 5 CT TRA"},
        riga_merce("DENT. SENSODENT 80+20ML FRESH CLEAN", "5059833580611"),
        {"g": "IN OMAGGIO 1 CT DI", "j": "DENT. SENSODENT 15 ML", "p": "SM", "r": "5059187155220"},
    ]

    # Le colonne di oggi, per i test che ne spostano o ne tolgono una sola.
    COLONNE_DI_OGGI = {
        "description": "G", "reward_description": "J", "discount": "P", "ean": "R",
    }

    def setUp(self) -> None:
        super().setUp()
        self.servizio = promotion_bridge.PromotionService()

    def registro_con(self, colonne_di_larice: dict[str, object]) -> None:
        """Un registro uguale a quello vero, tranne le colonne di Larice."""

        def sostituisci(documento: dict) -> None:
            self.adattatore(documento, "larice_v1")["column_map"] = colonne_di_larice

        self.registro_finto(sostituisci)

    def senza(self, *colonne: str) -> dict[str, object]:
        """Le colonne di oggi meno quelle che il test vuole far mancare."""

        return {campo: dove for campo, dove in self.COLONNE_DI_OGGI.items() if campo not in colonne}

    def test_una_colonna_spostata_nel_registro_sposta_anche_il_lettore(self):
        """Il listino ha una colonna in piu' in testa e il registro lo sa gia'.

        Con le posizioni scritte nel codice qui non si formava nessun blocco:
        openpyxl non solleva niente se si legge la colonna accanto, e la
        condizione commerciale spariva senza lasciare traccia.
        """

        self.registro_con({
            "description": "H", "reward_description": "K", "discount": "Q", "ean": "S",
        })
        percorso = scrivi_listino(
            self.cartella / "larice.xlsx", self.SOGLIA,
            colonne={"g": 8, "j": 11, "p": 17, "r": 19},
        )

        promozioni, errore = self.servizio.condizioni_di("larice", percorso)

        self.assertIsNone(errore)
        self.assertEqual(len(promozioni), 1)
        self.assertEqual(promozioni[0]["kind"], KIND_THRESHOLD_GIFT)
        self.assertEqual(promozioni[0]["threshold"], {"qty": 5, "unit": "cartoni"})
        self.assertEqual(promozioni[0]["reward"]["description"], "DENT. SENSODENT 15 ML")
        self.assertEqual(promozioni[0]["reward"]["ean"], "5059187155220")
        self.assertTrue(promozioni[0]["confirmed"])
        # La riga premio non conta fra la merce che fa raggiungere la soglia:
        # il codice `SM` continua a dirlo anche con le colonne spostate.
        self.assertEqual(promozioni[0]["eligible"]["source_rows"], [2])
        # Il riferimento manda l'utente nelle colonne di oggi, non in quelle di ieri.
        self.assertEqual(promozioni[0]["source_reference"], "Canvass di prova!H1:K3")

    def test_una_colonna_che_il_registro_non_dichiara_ferma_la_lettura(self):
        """Non si indovina: leggere la colonna accanto vuol dire zero soglie e
        nessuno che se ne accorga. Meglio fermarsi e dirlo."""

        self.registro_con(self.senza("ean"))
        percorso = scrivi_listino(self.cartella / "larice.xlsx", self.SOGLIA)
        review = {"files": [{"supplierId": "larice", "sourcePath": str(percorso)}], "products": []}

        self.assertEqual(self.servizio.detect(review), [])
        self.assertEqual(len(self.servizio.load_errors), 1)
        self.assertEqual(self.servizio.load_errors[0]["supplier"], "larice")
        self.assertEqual(self.servizio.load_errors[0]["supplierName"], "LARICE")
        self.assertEqual(
            self.servizio.load_errors[0]["message"],
            "Non riesco a leggere le condizioni commerciali di LARICE: non so più dove il "
            "listino tiene il codice a barre.",
        )

    def test_il_nome_dell_articolo_in_omaggio_vale_come_le_altre_colonne(self):
        """Non e' un di piu' che si puo' tirare a indovinare.

        Senza quel nome la soglia si ricompone lo stesso, ma esce «da
        verificare» invece che confermata: l'utente perderebbe l'omaggio in un
        altro modo, e nessun avviso glielo direbbe.  Finche' il registro non la
        dichiarava, qui restava scritta la posizione storica.
        """

        self.registro_con(self.senza("reward_description"))
        percorso = scrivi_listino(self.cartella / "larice.xlsx", self.SOGLIA)

        promozioni, errore = self.servizio.condizioni_di("larice", percorso)

        self.assertEqual(promozioni, [])
        self.assertEqual(
            errore["message"],
            "Non riesco a leggere le condizioni commerciali di LARICE: non so più dove il "
            "listino tiene il nome dell'articolo in omaggio.",
        )

    def test_due_colonne_che_mancano_si_dicono_come_le_direbbe_una_persona(self):
        self.registro_con(self.senza("description", "ean"))
        percorso = scrivi_listino(self.cartella / "larice.xlsx", self.SOGLIA)

        _promozioni, errore = self.servizio.condizioni_di("larice", percorso)

        self.assertEqual(
            errore["message"],
            "Non riesco a leggere le condizioni commerciali di LARICE: non so più dove il "
            "listino tiene le descrizioni e il codice a barre.",
        )

    def test_una_colonna_dichiarata_in_un_modo_che_non_si_legge_non_si_tira_a_indovinare(self):
        """«9» e' un nome di intestazione dappertutto nel programma, e il
        listino Larice di intestazioni non ne ha: leggerlo come «colonna 9»
        vorrebbe dire scegliere una colonna che nessun altro lettore sceglie."""

        self.registro_con({**self.COLONNE_DI_OGGI, "ean": "9"})
        percorso = scrivi_listino(self.cartella / "larice.xlsx", self.SOGLIA)

        promozioni, errore = self.servizio.condizioni_di("larice", percorso)

        self.assertEqual(promozioni, [])
        self.assertIn("il codice a barre", errore["message"])


class IlListinoCheNonSiRisolve(unittest.TestCase):
    """Il documento dichiarato dalla review che non si apre piu'.

    Spostato sul Desktop, tolto dai caricamenti dopo il ricalcolo, su un
    percorso di rete caduto: il lettore tornava una lista vuota, cioe' la
    stessa risposta di «questo fornitore non ha condizioni commerciali».
    """

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="listino-sparito-"))
        self.servizio = promotion_bridge.PromotionService()

    def test_un_listino_che_non_si_apre_piu_diventa_un_avviso(self):
        sparito = self.cartella / "larice.xlsx"
        review = {"files": [{"supplierId": "larice", "sourcePath": str(sparito)}], "products": []}

        self.assertEqual(self.servizio.detect(review), [])

        self.assertEqual(len(self.servizio.load_errors), 1)
        self.assertEqual(self.servizio.load_errors[0]["supplier"], "larice")
        self.assertEqual(self.servizio.load_errors[0]["supplierName"], "LARICE")
        self.assertEqual(
            self.servizio.load_errors[0]["message"],
            "Non riesco a leggere le condizioni commerciali di LARICE: il listino non è fra "
            "i documenti caricati.",
        )

    def test_un_fornitore_che_la_review_non_dichiara_non_diventa_un_avviso(self):
        """Un avviso che compare sempre non lo legge piu' nessuno: senza un
        listino Larice fra i documenti non c'e' nessuna perdita da segnalare."""

        self.assertEqual(self.servizio.detect({"files": [], "products": []}), [])
        self.assertEqual(self.servizio.load_errors, [])

    def test_un_listino_che_si_apre_non_segnala_niente(self):
        """La stessa review con il documento al suo posto resta muta: e' la
        prova che l'avviso nuovo dipende dal file e non dal fornitore."""

        percorso = scrivi_listino(self.cartella / "larice.xlsx", [
            {"g": "ACQUISTANDO 2 CT TRA"},
            riga_merce("PRODOTTO UNO", "8000000000001"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "PREMIO", "p": "SM", "r": "8000000009999"},
        ])
        review = {"files": [{"supplierId": "larice", "sourcePath": str(percorso)}], "products": []}

        promozioni = self.servizio.detect(review)

        self.assertEqual([p["kind"] for p in promozioni], [KIND_THRESHOLD_GIFT])
        self.assertEqual(self.servizio.load_errors, [])


class LePromozioniDiOggiNonCambiano(unittest.TestCase):
    """Il contratto che il riepilogo mostra oggi, inchiodato campo per campo.

    E' il rischio principale della correzione: far arrivare le colonne dal
    registro non deve cambiare di una virgola quello che l'utente vede con il
    listino e la mappatura di oggi.  L'identificativo compreso, perche' le
    conferme gia' date dall'utente si appoggiano a quello.
    """

    def test_il_contratto_di_una_soglia_e_di_una_condizione_ambigua_e_lo_stesso(self):
        cartella = Path(tempfile.mkdtemp(prefix="promozioni-di-oggi-"))
        percorso = scrivi_listino(cartella / "larice.xlsx", [
            {"g": "ACQUISTANDO 5 CT TRA"},
            riga_merce("DENT. SENSODENT 80+20ML FRESH CLEAN", "5059833580611"),
            riga_merce("DENT. SENSODENT 80+20ML COMPLEX", "5059905473735"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "DENT. SENSODENT 15 ML", "p": "SM", "r": "5059187155220"},
            {"g": "ACQUISTANDO 10 SCATOLE TRA"},
            riga_merce("BIOPUNTO LATTE CORPO 75ML", "8059651466354"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "BIOPUNTO SET MARE", "p": "SM", "r": "8050507999999"},
        ])

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di("larice", percorso)

        self.assertIsNone(errore)
        self.assertEqual(len(promozioni), 2)
        soglia, ambigua = promozioni
        self.assertEqual(soglia["id"], "promo:larice:f43d3a2ac7fd207f")
        self.assertEqual(soglia["source_reference"], "Canvass di prova!G1:J4")
        self.assertEqual(
            soglia["source_text"],
            "ACQUISTANDO 5 CT TRA IN OMAGGIO 1 CT DI DENT. SENSODENT 15 ML",
        )
        self.assertEqual(soglia["kind"], KIND_THRESHOLD_GIFT)
        self.assertEqual(soglia["threshold"], {"qty": 5, "unit": "cartoni"})
        self.assertEqual(soglia["reward"], {
            "kind": "prodotto",
            "description": "DENT. SENSODENT 15 ML",
            "qty": 1,
            "unit": "cartoni",
            "pieces_per_unit": None,
            "ean": "5059187155220",
            "supplier_code": None,
        })
        self.assertEqual(soglia["eligible"], {
            "products": [], "eans": [], "source_rows": [2, 3], "mix_allowed": True, "group": None,
        })
        self.assertEqual(soglia["certainty"], "alta")
        self.assertTrue(soglia["confirmed"])
        self.assertTrue(soglia["repeatable"])
        self.assertEqual(soglia["economic_effect"], {
            "type": "informational_reward",
            "discount_rate": None,
            "deterministic": True,
            "active": True,
            "affects_total": False,
            "affects_supplier_choice": False,
            "base_price_field": "unitPricePreDiscount",
            "already_applied": False,
        })
        self.assertEqual(ambigua["id"], "promo:larice:4c32daca2df97376")
        self.assertEqual(ambigua["source_reference"], "Canvass di prova!G5:J7")
        self.assertEqual(ambigua["kind"], KIND_AMBIGUOUS)
        self.assertEqual(
            ambigua["source_text"],
            "ACQUISTANDO 10 SCATOLE TRA IN OMAGGIO 1 CT DI BIOPUNTO SET MARE "
            "[condizione non ricomposta: il testo del blocco non dice una soglia calcolabile]",
        )
        self.assertEqual(ambigua["eligible"]["source_rows"], [6])
        self.assertFalse(ambigua["confirmed"])


class IlDiPiuGiaCompresoNelPrezzo(ConUnRegistroFinto):
    """Chi dichiara che «11+1» e' gia' dentro il prezzo lo dice il registro.

    Prima era un `supplier == "betulla"` scritto nel codice: il giorno che un
    altro fornitore avesse fatto lo stesso patto, dichiararlo nel registro non
    avrebbe cambiato niente, e per quel fornitore il di piu' sarebbe finito
    fra le offerte da verificare invece che fra le confezioni promozionali.
    """

    # Il testo vero di BETULLA, dal listino: «11+1» non e' uno sconto da
    # applicare, il prezzo di listino lo contiene gia'.
    OFFERTA = "LINDA SETA Assorbenti Ultra Con Ali Lunghi 11+1 Gratis Pz"

    def tipi_delle_promozioni(self, fornitore: str) -> list[str]:
        review = {
            "files": [],
            "products": [{
                "id": "prodotto-1",
                "ean": "8000000000010",
                "offers": [{
                    "supplierId": fornitore,
                    "available": True,
                    "sourceRow": 12,
                    "promotionText": self.OFFERTA,
                }],
            }],
        }
        return [p["kind"] for p in promotion_bridge.PromotionService().detect(review)]

    def test_il_fornitore_che_lo_dichiara_oggi_lo_ottiene_ancora(self):
        """Con il registro vero BETULLA resta esattamente com'era."""

        self.assertEqual(self.tipi_delle_promozioni("betulla"), [KIND_INCLUDED_PACK])

    def test_senza_la_dichiarazione_la_regola_non_vale_piu(self):
        """La prova che la regola arriva davvero dal registro: se il registro
        tace, il di piu' torna a essere un'offerta da guardare a mano."""

        def togli(documento: dict) -> None:
            self.adattatore(documento, "betulla_v1")["commercial_rules"].pop(
                "promotion_included_in_product", None
            )

        self.registro_finto(togli)

        self.assertEqual(self.tipi_delle_promozioni("betulla"), [KIND_AMBIGUOUS])

    def test_vale_per_qualunque_fornitore_lo_dichiari(self):
        """La prova che non e' lo stesso confronto con un altro vestito: la
        dichiarazione la si mette su CIPRESSO e ha effetto su CIPRESSO."""

        self.assertEqual(self.tipi_delle_promozioni("cipresso"), [KIND_AMBIGUOUS])

        def dichiara(documento: dict) -> None:
            self.adattatore(documento, "cipresso_v1")["commercial_rules"][
                "promotion_included_in_product"
            ] = True

        self.registro_finto(dichiara)

        self.assertEqual(self.tipi_delle_promozioni("cipresso"), [KIND_INCLUDED_PACK])
        # E BETULLA, che lo dichiarava gia', non ha perso niente per strada.
        self.assertEqual(self.tipi_delle_promozioni("betulla"), [KIND_INCLUDED_PACK])

    def test_una_dichiarazione_che_non_e_un_si_non_vale(self):
        """«true» scritto come testo non e' una dichiarazione: un adattatore
        imparato male non deve poter cambiare di nascosto il prezzo di un
        fornitore."""

        def quasi(documento: dict) -> None:
            self.adattatore(documento, "betulla_v1")["commercial_rules"][
                "promotion_included_in_product"
            ] = "true"

        self.registro_finto(quasi)

        self.assertEqual(self.tipi_delle_promozioni("betulla"), [KIND_AMBIGUOUS])


class ColonnaOffertaNoce(unittest.TestCase):
    """Il canale delle offerte Noce, chiuso a monte dall'adattatore.

    Nel listino vero del 6 agosto 2026 la colonna «descrizione_offerta» e'
    vuota su tutte le righe, quindi questo percorso si puo' provare soltanto
    con un caso costruito: il collaudo serve a garantire che, quando il
    fornitore la compilera', il testo arrivi fino ai rilevatori.
    """

    INTESTAZIONI = [
        "codice_a_barre", "codice", "descrizione_articolo", "pezzi_x_cartone",
        "cartoni_x_stra", "strati_x_pal", "prezzo", "quantita", "offerta",
        "Importo", "descrizione_reparto", "cat", "ragione_sociale", "variato",
        "descrizione_offerta", "Iva",
    ]

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="noce-offerta-"))
        adattatori = json.loads((RADICE / "references" / "adapters.json").read_text(encoding="utf-8"))
        self.adattatore = next(a for a in adattatori["adapters"] if a["id"] == "noce_xls_v1")

    def _listino(self, testo_offerta: str) -> Path:
        libro = Workbook()
        foglio = libro.active
        foglio.title = "Foglio1"
        foglio.cell(row=4, column=4, value="i prezzi offerta sono in grassetto")
        for indice, nome in enumerate(self.INTESTAZIONI, start=2):
            foglio.cell(row=5, column=indice, value=nome)
        valori = {
            "codice_a_barre": "8000000000001",
            "codice": "0000000449070",
            "descrizione_articolo": "NEVAL DOCCIA MEN BOOST ML.250",
            "pezzi_x_cartone": 12,
            "prezzo": 0.98,
            "quantita": 0,
            "offerta": "SI",
            "cat": "NOFOOD",
            "ragione_sociale": "BEIERSDORF",
            "descrizione_offerta": testo_offerta,
            "Iva": "22",
        }
        for indice, nome in enumerate(self.INTESTAZIONI, start=2):
            if nome in valori:
                foglio.cell(row=6, column=indice, value=valori[nome])
        percorso = self.cartella / "noce.xlsx"
        libro.save(percorso)
        libro.close()
        return percorso

    def test_la_colonna_dell_offerta_arriva_fino_ai_rilevatori(self):
        import prepare_manifest_sources as pms
        from catalog_search import _promotion_text
        from promotions import detect_promotions

        testo = "ACQUISTA 15 COLLI IN OMAGGIO 1 COLLO (12 PEZZI) DI NEVAL DOCCIA MEN BOOST ML.250"
        percorso = self._listino(testo)

        record, _avvisi = pms.read_mapped_xlsx_supplier(
            percorso, "noce", dict(self.adattatore["field_mapping"])
        )

        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]["availability"], testo)
        self.assertTrue(record[0]["usable"], "la colonna dell'offerta non deve rendere la riga inutilizzabile")

        promozioni = detect_promotions(
            supplier="noce",
            source_reference="noce:riga:6",
            source_text=_promotion_text(record[0]),
            eligible={"eans": [record[0]["ean"]]},
        )

        self.assertEqual([p["kind"] for p in promozioni], [KIND_THRESHOLD_GIFT])
        self.assertEqual(promozioni[0]["threshold"], {"qty": 15, "unit": "colli"})
        self.assertEqual(promozioni[0]["reward"]["unit"], "colli")

    def test_una_colonna_offerta_vuota_non_cambia_niente(self):
        import prepare_manifest_sources as pms

        percorso = self._listino("")
        record, _avvisi = pms.read_mapped_xlsx_supplier(
            percorso, "noce", dict(self.adattatore["field_mapping"])
        )

        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]["availability"], "")
        self.assertTrue(record[0]["usable"])


class UnFornitoreInventatoDichiaraDoveTieneLeSueCondizioni(ConUnRegistroFinto):
    """Il motore legge chiunque lo dichiari, non chi il codice conosce per nome.

    ⚠ Questo e' il difetto che la correzione del 15 agosto 2026 chiude.  Il
    lettore andava a prendere `larice_v1` per nome, leggeva il documento di
    `paths["larice"]` e scriveva «LARICE» nei messaggi: le condizioni di
    chiunque altro non le guardava nessuno, e non c'era modo di dichiararle
    senza rimettere mano al codice.  «BIANCHI & FIGLI» non compare in nessun
    file del programma: se queste prove passano, un fornitore nuovo si accende
    con una voce di registro.
    """

    FORNITORE = "bianchi"

    def adattatore_inventato(self, condizioni: dict | None) -> dict:
        """Un fornitore che esiste solo nel registro, con colonne tutte sue."""

        voce = {
            "id": "bianchi_v1",
            "schema_version": 1,
            "kind": "supplier",
            "supplier_id": self.FORNITORE,
            "display_name": "BIANCHI & FIGLI",
            "file_types": [".xlsx"],
            "column_map": {
                "description": "C",
                "reward_description": "E",
                "discount": "F",
                "ean": "H",
            },
            "row_markers": {
                "field": "discount_raw",
                "codes": {
                    "RG": {
                        "means": "Riga regalo: non è merce acquistabile.",
                        "orderable": False,
                        "row_type": "OMAGGIO",
                    }
                },
            },
        }
        if condizioni is not None:
            voce["commercial_conditions"] = condizioni
        return voce

    def registro_con_bianchi(self, condizioni: dict | None) -> None:
        def aggiungi(documento: dict) -> None:
            documento["adapters"].append(self.adattatore_inventato(condizioni))

        self.registro_finto(aggiungi)

    def scrivi(self, foglio_chiamato: str, righe: list[dict[str, object]]) -> Path:
        """Un listino con le colonne di BIANCHI, non con quelle di Larice."""

        libro = Workbook()
        foglio = libro.active
        foglio.title = foglio_chiamato
        for numero, contenuto in enumerate(righe, start=1):
            for colonna, valore in contenuto.items():
                if valore not in (None, ""):
                    foglio.cell(row=numero, column=colonna, value=valore)
        percorso = self.cartella / "bianchi.xlsx"
        libro.save(percorso)
        libro.close()
        return percorso

    # -- la forma a blocchi, quella di LARICE, su un altro fornitore --------

    A_BLOCCHI = {
        "layout": "blocchi",
        "sheet": "Condizioni",
        "data_start_row": 3,
        "fields": {
            "text": "description",
            "reward": "reward_description",
            "ean": "ean",
            "row_code": "discount",
        },
    }

    LISTINO_A_BLOCCHI = [
        {3: "LISTINO SETTIMANA 34"},
        {3: "codice a barre", 5: "omaggio"},
        {3: "ACQUISTANDO 4 CT TRA"},
        {3: "SAPONE MANI 300 ML", 6: "TP", 8: "8011111111111"},
        {3: "SAPONE MANI 500 ML", 6: "TP", 8: "8011111111112"},
        {3: "IN OMAGGIO 1 CT DI", 5: "TOSTAPANE 750W", 6: "RG", 8: "8011111119999"},
    ]

    def test_una_soglia_di_un_fornitore_che_il_codice_non_nomina(self):
        self.registro_con_bianchi(self.A_BLOCCHI)
        percorso = self.scrivi("Condizioni", self.LISTINO_A_BLOCCHI)

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            self.FORNITORE, percorso
        )

        self.assertIsNone(errore)
        self.assertEqual(len(promozioni), 1)
        soglia = promozioni[0]
        self.assertEqual(soglia["supplier"], "bianchi")
        self.assertEqual(soglia["kind"], KIND_THRESHOLD_GIFT)
        self.assertEqual(soglia["threshold"], {"qty": 4, "unit": "cartoni"})
        self.assertEqual(soglia["reward"]["description"], "TOSTAPANE 750W")
        self.assertEqual(soglia["reward"]["ean"], "8011111119999")
        self.assertTrue(soglia["confirmed"])
        # Le due righe di merce, non la riga regalo: il codice che la marca lo
        # dichiara questo fornitore, con una parola sua («RG», non «SM»).
        self.assertEqual(soglia["eligible"]["source_rows"], [4, 5])
        # Le colonne e il foglio del riferimento sono i suoi, non quelli di Larice.
        self.assertEqual(soglia["source_reference"], "Condizioni!C3:E6")

    # -- il premio scritto nella riga stessa, non in una colonna sua --------

    A_BLOCCHI_IN_UNA_COLONNA_SOLA = {
        "layout": "blocchi",
        "sheet": "Condizioni",
        "data_start_row": 3,
        "fields": {
            "text": "description",
            "reward": "description",
            "ean": "ean",
            "row_code": "discount",
        },
    }

    LISTINO_COL_PREMIO_NEL_TESTO = [
        {3: "LISTINO SETTIMANA 34"},
        {3: "codice a barre"},
        {3: "ACQUISTANDO 4 CT TRA"},
        {3: "SAPONE MANI 300 ML", 6: "TP", 8: "8011111111111"},
        {3: "SAPONE MANI 500 ML", 6: "TP", 8: "8011111111112"},
        {3: "IN OMAGGIO 1 CT DI TOSTAPANE 750W", 6: "RG", 8: "8011111119999"},
    ]

    def test_il_premio_nella_stessa_colonna_del_testo_non_esce_scritto_due_volte(self):
        """Il canvass nuovo di LARICE tiene testo e premio tutt'e due in
        colonna E, e il registro deve poterlo dire.

        Leggendo la stessa colonna per i due ruoli la frase usciva doppia —
        «TOSTAPANE 750W IN OMAGGIO 1 CT DI TOSTAPANE 750W» — cioe' la malattia
        del §17: la soglia era giusta e la frase che l'utente legge no.
        """

        self.registro_con_bianchi(self.A_BLOCCHI_IN_UNA_COLONNA_SOLA)
        percorso = self.scrivi("Condizioni", self.LISTINO_COL_PREMIO_NEL_TESTO)

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            self.FORNITORE, percorso
        )

        self.assertIsNone(errore)
        self.assertEqual(len(promozioni), 1)
        soglia = promozioni[0]
        self.assertEqual(soglia["threshold"], {"qty": 4, "unit": "cartoni"})
        self.assertEqual(soglia["reward"]["description"], "TOSTAPANE 750W")
        self.assertEqual(soglia["reward"]["ean"], "8011111119999")
        self.assertEqual(soglia["eligible"]["source_rows"], [4, 5])

    def test_due_colonne_diverse_continuano_a_leggersi_tutt_e_due(self):
        """La controprova: dove il premio ha una colonna sua non cambia nulla."""

        self.registro_con_bianchi(self.A_BLOCCHI)
        percorso = self.scrivi("Condizioni", self.LISTINO_A_BLOCCHI)

        promozioni, _errore = promotion_bridge.PromotionService().condizioni_di(
            self.FORNITORE, percorso
        )

        self.assertEqual(promozioni[0]["reward"]["description"], "TOSTAPANE 750W")

    def test_la_stessa_soglia_arriva_fino_al_riepilogo(self):
        """La prova che non e' solo il lettore: e' tutta la catena.

        `detect` sceglieva i fornitori da leggere con il nome scritto dentro,
        quindi un fornitore nuovo poteva anche essere leggibile senza che
        nessuno gli chiedesse mai niente.
        """

        self.registro_con_bianchi(self.A_BLOCCHI)
        percorso = self.scrivi("Condizioni", self.LISTINO_A_BLOCCHI)
        servizio = promotion_bridge.PromotionService()

        promozioni = servizio.detect({
            "files": [{"supplierId": self.FORNITORE, "sourcePath": str(percorso)}],
            "products": [],
        })

        self.assertEqual([p["supplier"] for p in promozioni], ["bianchi"])
        self.assertEqual(servizio.load_errors, [])
        # Contato per fornitore: LARICE non e' fra i documenti di questa run,
        # quindi non ha nemmeno un conteggio — e non un conteggio a zero, che
        # si leggerebbe come «letto, niente da verificare».
        self.assertEqual(servizio.condizioni_da_verificare, {"bianchi": 0})

    # -- la forma a riga: una condizione scritta per intero in una colonna --

    A_RIGA = {
        "layout": "riga",
        "sheet": "Condizioni",
        "data_start_row": 2,
        "fields": {"text": "description", "ean": "ean", "row_code": "discount"},
    }

    def test_una_condizione_scritta_per_intero_dentro_una_riga(self):
        """La forma che avrebbero BETULLA e NOCE il giorno che scrivono
        qualcosa: il testo sta tutto in una cella, non su piu' righe."""

        self.registro_con_bianchi(self.A_RIGA)
        percorso = self.scrivi("Condizioni", [
            # La riga di testata: sta sopra `data_start_row` e porta apposta
            # una parola promozionale, cosi' se la prima riga dei dati venisse
            # ignorata questa condizione inesistente si farebbe contare.
            {3: "TABELLA PROMOZIONE SETTIMANALE", 8: "codice a barre"},
            {3: "SAPONE MANI 300 ML", 6: "TP", 8: "8011111111111"},
            {
                3: "ACQUISTA 5 CT IN OMAGGIO 1 CT (12 PEZZI) DI SALE LAVASTOVIGLIE KG1",
                6: "TP",
                8: "8011111111112",
            },
        ])

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            self.FORNITORE, percorso
        )

        self.assertIsNone(errore)
        self.assertEqual(len(promozioni), 1)
        soglia = promozioni[0]
        self.assertEqual(soglia["kind"], KIND_THRESHOLD_GIFT)
        self.assertEqual(soglia["threshold"], {"qty": 5, "unit": "cartoni"})
        self.assertEqual(soglia["reward"]["description"], "SALE LAVASTOVIGLIE KG1")
        self.assertEqual(soglia["reward"]["pieces_per_unit"], 12)
        # Il prodotto che la porta e' quello della sua riga, per riga e per EAN.
        self.assertEqual(soglia["eligible"]["source_rows"], [3])
        self.assertEqual(soglia["eligible"]["eans"], ["8011111111112"])
        self.assertEqual(soglia["source_reference"], "Condizioni!C3")

    def test_a_riga_una_descrizione_qualunque_non_diventa_un_offerta(self):
        """Il rischio della forma a riga: la colonna del testo e' spesso la
        descrizione del prodotto, e trecento nomi di prodotto non devono
        diventare trecento offerte da verificare."""

        self.registro_con_bianchi(self.A_RIGA)
        percorso = self.scrivi("Condizioni", [
            {3: "descrizione"},
            {3: "SAPONE MANI 300 ML", 6: "TP", 8: "8011111111111"},
            {3: "DENTIFRICIO MENTA 75 ML", 6: "TP", 8: "8011111111112"},
        ])

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            self.FORNITORE, percorso
        )

        self.assertIsNone(errore)
        self.assertEqual(promozioni, [])

    def test_a_riga_la_riga_non_acquistabile_resta_fuori(self):
        """Il codice di riga vale in tutte e due le forme: una riga che il
        fornitore dichiara non acquistabile non porta una condizione."""

        self.registro_con_bianchi(self.A_RIGA)
        percorso = self.scrivi("Condizioni", [
            {3: "descrizione"},
            {3: "ACQUISTA 5 CT IN OMAGGIO 1 CT DI TOSTAPANE", 6: "RG", 8: "8011111119999"},
        ])

        promozioni, _errore = promotion_bridge.PromotionService().condizioni_di(
            self.FORNITORE, percorso
        )

        self.assertEqual(promozioni, [])

    # -- che cosa succede se il registro non lo dichiara -------------------

    def test_senza_la_dichiarazione_non_si_legge_e_non_si_avvisa_nessuno(self):
        """La prova che la regola arriva davvero dal registro.

        E anche che il silenzio e' quello giusto: un fornitore che non dichiara
        condizioni non ne ha, e riempire la pagina di «non so leggere le
        offerte di X» a ogni ricalcolo vorrebbe dire un avviso che non chiede
        di fare niente.
        """

        self.registro_con_bianchi(None)
        percorso = self.scrivi("Condizioni", self.LISTINO_A_BLOCCHI)
        servizio = promotion_bridge.PromotionService()

        promozioni = servizio.detect({
            "files": [{"supplierId": self.FORNITORE, "sourcePath": str(percorso)}],
            "products": [],
        })

        self.assertEqual(promozioni, [])
        self.assertEqual(servizio.load_errors, [])

    def test_una_forma_di_scrittura_che_il_motore_non_conosce_si_ferma_e_lo_dice(self):
        """Un registro imparato male non deve leggere «qualcosa comunque»."""

        self.registro_con_bianchi({**self.A_BLOCCHI, "layout": "a fisarmonica"})
        percorso = self.scrivi("Condizioni", self.LISTINO_A_BLOCCHI)

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            self.FORNITORE, percorso
        )

        self.assertEqual(promozioni, [])
        self.assertEqual(
            errore["message"],
            "Non riesco a leggere le condizioni commerciali di BIANCHI & FIGLI: non so "
            "più dove il listino tiene in che modo scrive le sue condizioni.",
        )

    def test_il_nome_nei_messaggi_e_quello_dichiarato_dal_registro(self):
        """«LARICE» stava scritto nel codice accanto a `supplier="larice"`: un
        fornitore nuovo sarebbe comparso negli avvisi con il suo
        identificativo tecnico, o peggio con il nome di un altro."""

        senza_ean = {
            **self.A_BLOCCHI,
            "fields": {k: v for k, v in self.A_BLOCCHI["fields"].items() if k != "ean"},
        }
        self.registro_con_bianchi(senza_ean)
        percorso = self.scrivi("Condizioni", self.LISTINO_A_BLOCCHI)
        servizio = promotion_bridge.PromotionService()

        servizio.detect({
            "files": [{"supplierId": self.FORNITORE, "sourcePath": str(percorso)}],
            "products": [],
        })

        self.assertEqual(len(servizio.load_errors), 1)
        self.assertEqual(servizio.load_errors[0]["supplier"], "bianchi")
        self.assertEqual(servizio.load_errors[0]["supplierName"], "BIANCHI & FIGLI")
        self.assertEqual(
            servizio.load_errors[0]["message"],
            "Non riesco a leggere le condizioni commerciali di BIANCHI & FIGLI: non so "
            "più dove il listino tiene il codice a barre.",
        )

    def test_il_listino_sparito_di_un_fornitore_inventato_e_un_avviso_suo(self):
        self.registro_con_bianchi(self.A_BLOCCHI)
        servizio = promotion_bridge.PromotionService()

        servizio.detect({
            "files": [{"supplierId": self.FORNITORE, "sourcePath": str(self.cartella / "via.xlsx")}],
            "products": [],
        })

        self.assertEqual(len(servizio.load_errors), 1)
        self.assertEqual(
            servizio.load_errors[0]["message"],
            "Non riesco a leggere le condizioni commerciali di BIANCHI & FIGLI: il "
            "listino non è fra i documenti caricati.",
        )

    def test_due_fornitori_che_dichiarano_si_leggono_sempre_nello_stesso_ordine(self):
        """⚠ Trovato da una mutazione rimasta verde.

        Finche' a dichiarare le condizioni e' un fornitore solo, qualunque
        ordine e' lo stesso ordine: togliere l'ordinamento non faceva fallire
        niente.  Ma l'elenco delle promozioni e' quello che la pagina mostra, e
        due letture della stessa run che lo mettono in ordine diverso fanno
        sembrare cambiato un confronto che non e' cambiato.
        """

        self.registro_con_bianchi(self.A_BLOCCHI)
        bianchi = self.scrivi("Condizioni", self.LISTINO_A_BLOCCHI)
        larice = scrivi_listino(self.cartella / "larice.xlsx", [
            {"g": "ACQUISTANDO 2 CT TRA"},
            riga_merce("PRODOTTO UNO", "8000000000001"),
            {"g": "IN OMAGGIO 1 CT DI", "j": "PREMIO", "p": "SM", "r": "8000000009999"},
        ])
        review = {
            "files": [
                {"supplierId": "larice", "sourcePath": str(larice)},
                {"supplierId": self.FORNITORE, "sourcePath": str(bianchi)},
            ],
            "products": [],
        }

        # Nel registro BIANCHI viene dopo LARICE; nell'elenco viene prima,
        # perche' l'ordine e' alfabetico e non quello in cui sono dichiarati.
        letti = [p["supplier"] for p in promotion_bridge.PromotionService().detect(review)]

        self.assertEqual(letti, ["bianchi", "larice"])

    def test_di_due_adattatori_si_usa_quello_del_documento_che_si_ha_in_mano(self):
        """⚠ Trovato da una mutazione rimasta verde.

        Noce ha due adattatori, uno per il `.xls` e uno per il CSV: leggere
        le condizioni con la dichiarazione dell'altro formato vuol dire cercare
        una colonna dove non c'e'.  Nessun test lo copriva, perche' l'unico
        fornitore che dichiara condizioni oggi ha un adattatore solo.
        """

        def due_adattatori(documento: dict) -> None:
            # Il primo dell'elenco e' quello dell'altro formato, con colonne
            # diverse: se il formato non contasse, si userebbe questo.
            csv = self.adattatore_inventato(dict(self.A_RIGA))
            csv["id"] = "bianchi_csv_v1"
            csv["file_types"] = [".csv"]
            csv["column_map"] = {"description": "A", "ean": "B", "discount": "C"}
            documento["adapters"].append(csv)
            documento["adapters"].append(self.adattatore_inventato(self.A_BLOCCHI))

        self.registro_finto(due_adattatori)
        percorso = self.scrivi("Condizioni", self.LISTINO_A_BLOCCHI)

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            self.FORNITORE, percorso
        )

        self.assertIsNone(errore)
        self.assertEqual(len(promozioni), 1)
        self.assertEqual(promozioni[0]["kind"], KIND_THRESHOLD_GIFT)
        self.assertEqual(promozioni[0]["source_reference"], "Condizioni!C3:E6")

    def test_il_foglio_dichiarato_e_quello_che_si_legge(self):
        """Il lettore prendeva sempre il primo foglio del libro.  Un fornitore
        che mette le condizioni nel secondo non aveva modo di dirlo."""

        self.registro_con_bianchi(self.A_BLOCCHI)
        libro = Workbook()
        libro.active.title = "Copertina"
        foglio = libro.create_sheet("Condizioni")
        for numero, contenuto in enumerate(self.LISTINO_A_BLOCCHI, start=1):
            for colonna, valore in contenuto.items():
                if valore not in (None, ""):
                    foglio.cell(row=numero, column=colonna, value=valore)
        percorso = self.cartella / "bianchi.xlsx"
        libro.save(percorso)
        libro.close()

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            self.FORNITORE, percorso
        )

        self.assertIsNone(errore)
        self.assertEqual(len(promozioni), 1)
        self.assertEqual(promozioni[0]["source_reference"], "Condizioni!C3:E6")


class SoloChiLoDichiaraVieneLetto(unittest.TestCase):
    """Nel registro di oggi lo dichiara LARICE e nessun altro, ed e' misurato.

    ⚠ 15 agosto 2026, i quattro listini veri della settimana, cella per cella:
    LARICE 13 intestazioni di soglia e 13 righe premio in colonna G; BETULLA 12
    testi con una parola promozionale dentro la descrizione, di cui **7 sono
    la parola «Ogni» di «Ogni Superficie»**; CIPRESSO 1, ed e' un nome di
    prodotto che contiene «offerta»; NOCE la colonna
    `descrizione_offerta` **vuota su tutte e 18.074 le righe** e la colonna
    `offerta` che dice `NO` su tutte e 17.148 quelle compilate.

    Dichiarare la colonna della descrizione per BETULLA farebbe comparire dodici
    condizioni di cui nove non sono condizioni: inventare offerte a chi non ne
    ha e' peggio del difetto che si stava correggendo.
    """

    def test_nel_registro_di_oggi_lo_dichiara_solo_larice(self):
        self.assertEqual(
            sorted(promotion_bridge.fornitori_con_condizioni_dichiarate()), ["larice"]
        )

    def test_gli_altri_fornitori_del_confronto_non_producono_ne_offerte_ne_avvisi(self):
        cartella = Path(tempfile.mkdtemp(prefix="senza-condizioni-"))
        listini = []
        for fornitore in ("betulla", "cipresso", "noce"):
            percorso = cartella / f"{fornitore}.xlsx"
            libro = Workbook()
            libro.active.cell(row=1, column=1, value="EAN")
            libro.save(percorso)
            libro.close()
            listini.append({"supplierId": fornitore, "sourcePath": str(percorso)})
        servizio = promotion_bridge.PromotionService()

        promozioni = servizio.detect({"files": listini, "products": []})

        self.assertEqual(promozioni, [])
        self.assertEqual(servizio.load_errors, [])


class IlListinoLariceVero(unittest.TestCase):
    """La non-regressione che i listini sintetici non coprono.

    Misurato prima della correzione e riverificato dopo: 13 condizioni, tutte
    `soglia_omaggio`, tutte complete — soglia con l'unita', premio con nome ed
    EAN, righe di merce risolte, ripetibile, confermata.  Nessuna condizione
    «da verificare».
    """

    LISTINO = RADICE / "app" / "data" / "current" / "uploads" / "33-34.1 07-21 ago.xlsx"

    def test_le_tredici_condizioni_restano_tredici_e_restano_complete(self):
        if not self.LISTINO.is_file():
            self.skipTest(f"il listino LARICE vero non c'è: {self.LISTINO.name}")

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            "larice", self.LISTINO
        )

        self.assertIsNone(errore)
        self.assertEqual([p["kind"] for p in promozioni], [KIND_THRESHOLD_GIFT] * 13)
        for promozione in promozioni:
            with self.subTest(riferimento=promozione["source_reference"]):
                self.assertEqual(promozione["supplier"], "larice")
                self.assertEqual(promozione["certainty"], "alta")
                self.assertTrue(promozione["confirmed"])
                self.assertTrue(promozione["repeatable"])
                self.assertEqual(promozione["threshold"]["unit"], "cartoni")
                self.assertGreater(promozione["threshold"]["qty"], 0)
                self.assertTrue(promozione["reward"]["description"])
                self.assertTrue(promozione["reward"]["ean"])
                self.assertTrue(promozione["eligible"]["source_rows"])
        # I premi che l'utente ha nominato quando ha deciso che cosa tenere.
        premi = {p["reward"]["description"] for p in promozioni}
        self.assertIn("RESALINA SALE LAVASTOVIGLIE KG1", premi)
        self.assertIn("BISTECCHIERA 1000W", premi)
        self.assertIn("TOSTIERA ELETTRICA 750W", premi)


class UnDocumentoExcel97(ConUnRegistroFinto):
    """Noce manda **solo** Excel 97-2003, e il suo formato si legge.

    Un motore che dicesse «dichiara pure dove tieni le condizioni, tanto il tuo
    documento non lo apro» sarebbe un altro cablaggio, solo meno visibile.  Il
    listino vero serve anche da misura: la colonna che Noce dichiara per le
    offerte e' vuota su tutte le righe, quindi qui il numero giusto e' zero.
    """

    LISTINO = RADICE / "app" / "data" / "current" / "uploads" / "formattato_104233.xls"

    def test_il_listino_xls_si_legge_e_non_ha_nessuna_condizione(self):
        if not self.LISTINO.is_file():
            self.skipTest(f"il listino NOCE vero non c'è: {self.LISTINO.name}")

        def dichiara(documento: dict) -> None:
            self.adattatore(documento, "noce_xls_v1")["commercial_conditions"] = {
                "layout": "riga",
                "sheet": "Foglio1",
                "data_start_row": 6,
                "fields": {"text": "descrizione_offerta", "ean": "codice_a_barre"},
            }

        self.registro_finto(dichiara)

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            "noce", self.LISTINO
        )

        self.assertIsNone(errore, "il .xls dev'essere leggibile, non un errore")
        self.assertEqual(promozioni, [])

    def test_la_colonna_sbagliata_del_listino_xls_si_legge_lo_stesso(self):
        """La prova che il `.xls` viene letto davvero e non solo aperto: la
        colonna delle descrizioni ha cinque testi «N+M GRATIS», e sono quelli
        che il rilevatore trova quando gli si dice di guardare li'."""

        if not self.LISTINO.is_file():
            self.skipTest(f"il listino NOCE vero non c'è: {self.LISTINO.name}")

        def dichiara(documento: dict) -> None:
            self.adattatore(documento, "noce_xls_v1")["commercial_conditions"] = {
                "layout": "riga",
                "sheet": "Foglio1",
                "data_start_row": 6,
                "fields": {"text": "descrizione_articolo", "ean": "codice_a_barre"},
            }

        self.registro_finto(dichiara)

        promozioni, errore = promotion_bridge.PromotionService().condizioni_di(
            "noce", self.LISTINO
        )

        self.assertIsNone(errore)
        testi = sorted(p["source_text"] for p in promozioni)
        self.assertEqual(len(testi), 5, testi)
        self.assertIn("DANZATRIX PANNO MULTIUSO PZ.3+1 GRATIS", testi)
        self.assertIn("CUKO ALLUMINIO MT.16+4 GRATIS", testi)


class QualeDeiDueAdattatoriDelloStessoFornitore(unittest.TestCase):
    """Un fornitore con due schemi dello **stesso** formato.

    Fino al 4 settembre 2026 a scegliere era la sola estensione, e bastava
    perche' i due adattatori di Noce sono uno `.xls` e uno `.csv`.  LARICE
    adesso ne ha due `.xlsx`: con la vecchia regola avrebbe vinto sempre il
    primo dei due, cioe' le soglie del canvass nuovo sarebbero state cercate
    nelle colonne del vecchio.  Non un errore: zero condizioni, in silenzio.
    """

    VOCI = [
        {"id": "larice_v1", "file_types": [".xlsx"]},
        {"id": "larice_canvass_v1", "file_types": [".xlsx"]},
    ]
    DOCUMENTO = Path("New Larice N°37.xlsx")

    def test_decide_l_identificativo_che_ha_riconosciuto_il_documento(self) -> None:
        scelto = promotion_bridge._adattatore_per_il_documento(
            self.VOCI, self.DOCUMENTO, "larice_canvass_v1"
        )
        self.assertEqual(scelto["id"], "larice_canvass_v1")

    def test_lo_schema_imparato_porta_a_quello_da_cui_deriva(self) -> None:
        """Chi decide per identificativo passa da `registro.adattatore_base`."""

        scelto = promotion_bridge._adattatore_per_il_documento(
            self.VOCI, self.DOCUMENTO, "larice_canvass_v1__locale"
        )
        self.assertEqual(scelto["id"], "larice_canvass_v1")

    def test_senza_identificativo_resta_la_regola_del_formato(self) -> None:
        """La regola di prima non se ne va: serve ancora ai due di Noce."""

        voci = [
            {"id": "noce_csv_v1", "file_types": [".csv"]},
            {"id": "noce_xls_v1", "file_types": [".xls", ".xlsx"]},
        ]
        scelto = promotion_bridge._adattatore_per_il_documento(
            voci, Path("formattato_104233.xls"), None
        )
        self.assertEqual(scelto["id"], "noce_xls_v1")

    def test_un_identificativo_che_non_e_fra_questi_non_si_indovina(self) -> None:
        """Meglio nessuna condizione che le condizioni di un altro documento."""

        scelto = promotion_bridge._adattatore_per_il_documento(
            self.VOCI, self.DOCUMENTO, "qualcosa_di_imparato_v1"
        )
        self.assertEqual(scelto, {})

    def test_con_un_candidato_solo_l_identificativo_non_toglie_niente(self) -> None:
        """Il fornitore che di schemi ne ha uno solo si legge come sempre."""

        scelto = promotion_bridge._adattatore_per_il_documento(
            [{"id": "betulla_v1", "file_types": [".xlsx"]}], Path("betulla.xlsx"), "betulla_v1__imparato"
        )
        self.assertEqual(scelto["id"], "betulla_v1")


if __name__ == "__main__":
    unittest.main()
