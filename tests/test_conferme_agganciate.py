#!/usr/bin/env python3
"""Il magazzino delle conferme attaccato al servizio.

`app/conferme.py` esisteva dal 15 agosto 2026, con 48 prove sue, e non era
collegato a niente: le conferme vivevano solo dentro `state.json`, cioe'
appese al **numero di riga** del prodotto nell'export del gestionale. Misurato
sui due export veri: dei 457 identificativi presenti in tutti e due, 449
portano un articolo diverso. Una risposta data lunedi' o si perdeva, o —
peggio — si riapplicava a merce mai vista.

Qui si prova quello che l'aggancio deve garantire, e nient'altro:

1. rispondere «si'» **scrive** nel magazzino;
2. la risposta vale per l'**articolo**, quindi sopravvive all'export nuovo e
   al numero di riga nuovo;
3. una riga di listino che porta un **altro** articolo non eredita niente;
4. togliere la spunta **revoca**, mentre una scadenza non revoca niente —
   sono le due cose che la pagina manda scritte allo stesso modo;
5. un magazzino che non si apre **non ferma il salvataggio** e lo dice.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import server as SERVER  # noqa: E402
from conferme import MagazzinoConferme, MagazzinoNonUtilizzabile, impronta_prodotto  # noqa: E402
from server import ReviewStore  # noqa: E402


def offerta(supplier: str, *, ean: str, descrizione: str, riga: int, prezzo: float = 12.0) -> dict[str, Any]:
    """Un'offerta che **chiede conferma**: è il caso in cui il magazzino serve."""

    return {
        "supplierId": supplier,
        "available": True,
        "description": descrizione,
        "sourceRow": riga,
        "ean": ean,
        "unitPriceNet": round(prezzo / 6, 6),
        "quantityFactor": 6,
        "orderUnitPriceNet": prezzo,
        "method": "NOME",
        "confidence": "MEDIA",
        "requiresConfirmation": True,
    }


def prodotto(product_id: str, nome: str, ean: str, offerte: list[dict[str, Any]], riga: int = 10) -> dict[str, Any]:
    return {
        "id": product_id,
        "kind": "PRODUCT",
        "itemType": "product",
        "sourceRow": riga,
        "ean": ean,
        "name": nome,
        "description": nome,
        "quantity": 0,
        "selectedSupplierId": offerte[0]["supplierId"] if offerte else "",
        "confirmed": False,
        "requiresConfirmation": False,
        "offers": offerte,
        "components": [],
    }


def confronto(prodotti: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run": {"id": "run-sintetica", "status": "ready", "label": "Prova"},
        "files": [],
        "suppliers": [{"id": "larice", "name": "Larice", "minimumOrder": 0}],
        "products": prodotti,
        "warnings": [],
    }


class BancoDelleConferme(unittest.TestCase):
    """Un negozio vero, con il magazzino delle conferme nella sua cartella."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.root = Path(temporanea.name)
        self.run_dir = self.root / "run-corrente"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.review_path = self.root / "review_data.json"
        self.conferme_path = self.root / "history" / "conferme.db"

    def negozio(self, review: dict[str, Any]) -> ReviewStore:
        self.review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        store = ReviewStore(
            self.review_path,
            self.run_dir / "review_state.json",
            self.root / "uploads",
            self.root / "outputs",
            conferme_path=self.conferme_path,
        )
        self.addCleanup(self.chiudi, store)
        return store

    @staticmethod
    def chiudi(store: ReviewStore) -> None:
        magazzino = getattr(store, "_conferme", None)
        if magazzino is not None:
            magazzino.chiudi()

    @staticmethod
    def scelte(product_id: str, *, fornitore: str, quantita: int, confermato: bool,
               versione: int | None = None) -> dict[str, Any]:
        istantanea: dict[str, Any] = {
            "runId": "run-sintetica",
            "currentStep": 2,
            "acceptBelowThreshold": True,
            "products": [{
                "id": product_id,
                "quantity": quantita,
                "selectedSupplierId": fornitore,
                "confirmed": confermato,
            }],
        }
        if versione is not None:
            istantanea["stateVersion"] = versione
        return istantanea

    def conferme(self) -> list[dict[str, Any]]:
        magazzino = MagazzinoConferme(self.conferme_path)
        try:
            return magazzino.esporta()
        finally:
            magazzino.chiudi()


class RispondereSiSiScriveTests(BancoDelleConferme):
    def test_la_conferma_finisce_nel_magazzino_con_la_sua_data(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO MAR BLUE 3X80", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO MAR BLUE GR.80 X3", riga=42)]),
        ]))

        esito = store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        righe = self.conferme()
        self.assertEqual(len(righe), 1, righe)
        self.assertTrue(righe[0]["accettata"])
        self.assertTrue(righe[0]["in_vigore"])
        self.assertEqual(righe[0]["fornitore"], "larice")
        self.assertEqual(righe[0]["valida_dal"], esito["savedAt"])

    def test_lo_stesso_salvataggio_ripetuto_non_riempie_lo_storico(self) -> None:
        """L'autosalvataggio della pagina batte ogni 450 ms.

        `ricorda` non riscrive una risposta identica, ed è la difesa che rende
        possibile chiamarlo a ogni salvataggio senza pensarci: senza,
        rimanere fermi sulla pagina scriverebbe centinaia di righe di storico
        che raccontano decisioni mai prese.
        """

        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))

        for versione in (0, 1, 2):
            store.save_state(self.scelte(
                "product:3", fornitore="larice", quantita=2, confermato=True, versione=versione
            ))

        self.assertEqual(len(self.conferme()), 1)

    def test_senza_conferma_non_si_scrive_niente(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))

        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=False))

        self.assertEqual(self.conferme(), [])

    def test_un_abbinamento_certo_non_diventa_una_conferma(self) -> None:
        """⚠ Il filtro che tiene leggibile il magazzino.

        Il confronto marca `confirmed` anche sugli abbinamenti che nessuno ha
        dovuto confermare — sono la stragrande maggioranza — e la pagina li
        rimanda indietro così. Senza il filtro, un ordine di 450 righe
        scriverebbe 450 «risposte» mai date, e le poche vere non si
        troverebbero più. Una memoria che l'utente non può rileggere non è
        una memoria.
        """

        certa = offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)
        certa["requiresConfirmation"] = False
        certa["confidence"] = "CERTA"
        certa["method"] = "EAN"
        store = self.negozio(confronto([prodotto("product:3", "TONNO", "8000000000010", [certa])]))

        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        self.assertEqual(self.conferme(), [])


class LaConfermaSopravviveAllExportNuovoTests(BancoDelleConferme):
    """Il punto di tutto il magazzino, provato sul caso vero.

    La settimana dopo l'export del gestionale è un altro file: lo stesso
    articolo sta su un'altra riga, quindi ha un altro identificativo e la sua
    decisione non esiste. Prima la domanda tornava; adesso no.
    """

    ARTICOLO = ("TONNO MAR BLUE 3X80", "8000000000010")
    OFFERTA = dict(ean="8000000000010", descrizione="TONNO MAR BLUE GR.80 X3", riga=42)

    def settimana_prima(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", *self.ARTICOLO, [offerta("larice", **self.OFFERTA)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))
        self.chiudi(store)
        store._conferme = None
        # L'export nuovo cancella lo stato: è quello che fa il ricalcolo.
        store.state_path.unlink()

    def test_la_riga_nuova_dello_stesso_articolo_e_gia_confermata(self) -> None:
        self.settimana_prima()

        # Stesso articolo, altro numero di riga nel gestionale **e** altra riga
        # di listino: l'identità è il codice a barre più il nome, non la riga.
        store = self.negozio(confronto([
            prodotto("product:517", *self.ARTICOLO, [offerta("larice", **self.OFFERTA)], riga=517),
        ]))
        prodotti = store.review()["products"]

        self.assertTrue(prodotti[0]["confirmed"])
        self.assertEqual(prodotti[0]["confirmation"]["supplierId"], "larice")
        self.assertTrue(prodotti[0]["confirmation"]["since"])

    def test_e_la_compilazione_non_richiede_di_riconfermare(self) -> None:
        """La catena intera, come la percorre la pagina.

        ⚠ Il magazzino semina `product.confirmed` in `review()`; `snapshot()`
        rimanda indietro **quel** campo senza toccarlo (`app.js:1254`), e il
        cancello della compilazione legge lo snapshot. Il magazzino di
        proposito non viene riguardato al salvataggio: se potesse dire di sì
        anche lì, una spunta appena tolta verrebbe rimessa dal ricordo di ieri
        nello stesso salvataggio in cui l'utente l'ha tolta — cioè la revoca
        non esisterebbe. Questo test percorre la catena vera invece di saltarla.
        """

        self.settimana_prima()
        store = self.negozio(confronto([
            prodotto("product:517", *self.ARTICOLO, [offerta("larice", **self.OFFERTA)], riga=517),
        ]))

        letto = next(voce for voce in store.review()["products"] if voce["id"] == "product:517")
        clean, errori, _confronto = store.validate_snapshot(
            self.scelte(
                "product:517", fornitore="larice", quantita=2,
                confermato=bool(letto["confirmed"]),
            ),
            for_compile=True,
        )

        self.assertEqual([voce["code"] for voce in errori if voce["code"] == "CONFERMA_MANCANTE"], [])
        self.assertTrue(clean["products"][0]["confirmed"])

    def test_una_riga_di_listino_con_un_altro_articolo_non_eredita_niente(self) -> None:
        """⚠ Il confine, ed è quello che rende il magazzino sicuro.

        La conferma vale per la corrispondenza «questo mio articolo ↔ quella
        riga del fornitore». Se la riga di listino porta merce diversa, la
        risposta di prima non la copre e la domanda torna: perdere una
        conferma costa un clic, applicarne una sbagliata costa un ordine.
        """

        self.settimana_prima()
        store = self.negozio(confronto([
            prodotto("product:517", *self.ARTICOLO, [
                offerta("larice", ean="8000000000099", descrizione="TONNO MAR BLUE OLIO OLIVA", riga=51),
            ], riga=517),
        ]))

        prodotti = store.review()["products"]
        _clean, errori, _confronto = store.validate_snapshot(
            self.scelte("product:517", fornitore="larice", quantita=2, confermato=False),
            for_compile=True,
        )

        self.assertFalse(prodotti[0]["confirmed"])
        self.assertNotIn("confirmation", prodotti[0])
        self.assertIn("CONFERMA_MANCANTE", [voce["code"] for voce in errori])


class RevocareSiPuoScadereNoTests(BancoDelleConferme):
    """Le due cose che la pagina manda scritte allo stesso modo.

    `confirmed: false` arriva sia quando l'utente toglie la spunta, sia quando
    il ricalcolo ha fatto scadere la conferma perché la riga porta un altro
    articolo. Trattarle allo stesso modo svuoterebbe il magazzino proprio nella
    settimana in cui deve servire.
    """

    ARTICOLO = ("TONNO MAR BLUE 3X80", "8000000000010")
    OFFERTA = dict(ean="8000000000010", descrizione="TONNO MAR BLUE GR.80 X3", riga=42)

    def setUp(self) -> None:
        super().setUp()
        self.store = self.negozio(confronto([
            prodotto("product:3", *self.ARTICOLO, [offerta("larice", **self.OFFERTA)]),
        ]))
        self.store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

    def test_togliere_la_spunta_toglie_la_conferma(self) -> None:
        versione = json.loads(self.store.state_path.read_text(encoding="utf-8"))["stateVersion"]

        self.store.save_state(self.scelte(
            "product:3", fornitore="larice", quantita=2, confermato=False, versione=versione
        ))

        righe = self.conferme()
        self.assertEqual(len(righe), 1, "la riga non si cancella: si chiude, e resta nell'audit")
        self.assertFalse(righe[0]["in_vigore"])
        self.assertTrue(righe[0]["valida_fino_a"])

    def test_una_conferma_scaduta_dal_ricalcolo_resta_in_vigore(self) -> None:
        """⚠ Il caso che il difetto avrebbe distrutto.

        Lo stato arriva senza `confirmedArticle` — è quello che il ricalcolo
        lascia quando fa scadere una conferma — e con `confirmed: false`. Non è
        l'utente che ha cambiato idea: la memoria non si tocca.
        """

        stato = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        for decisione in stato["products"]:
            decisione["confirmed"] = False
            decisione.pop("confirmedArticle", None)
        self.store.state_path.write_text(json.dumps(stato), encoding="utf-8")

        self.store.save_state(self.scelte(
            "product:3", fornitore="larice", quantita=2, confermato=False,
            versione=stato["stateVersion"],
        ))

        righe = self.conferme()
        self.assertEqual(len(righe), 1)
        self.assertTrue(righe[0]["in_vigore"], "una scadenza non è una revoca")


class UnMagazzinoRottoNonFermaIlProgrammaTests(BancoDelleConferme):
    """Il file non si apre: si continua senza memoria, e si dice.

    Rispondere «nessuna conferma» in silenzio farebbe tornare tutte le domande
    senza spiegare perché, e l'utente le rifarebbe a mano credendo che il
    programma non avesse mai saputo niente. Ma **non** si ferma il salvataggio:
    un `PUT /api/state` che fallisce non perde una risposta, perde tutte
    quelle che vengono dopo.
    """

    def negozio_rotto(self) -> ReviewStore:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))
        self.rotto = mock.patch.object(
            SERVER, "MagazzinoConferme",
            side_effect=MagazzinoNonUtilizzabile("Il file delle conferme non si apre: disco pieno"),
        )
        self.rotto.start()
        self.addCleanup(self.rotto.stop)
        return store

    def test_il_salvataggio_riesce_lo_stesso(self) -> None:
        store = self.negozio_rotto()

        esito = store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        self.assertTrue(esito["ok"])
        self.assertTrue(esito["savedAt"])

    def test_e_il_confronto_lo_dice(self) -> None:
        store = self.negozio_rotto()
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        avvisi = store.review().get("warnings") or []

        voce = next((item for item in avvisi if item.get("code") == "CONFERME_NON_DISPONIBILI"), None)
        self.assertIsNotNone(voce, avvisi)
        self.assertIn("disco pieno", voce["message"])
        self.assertFalse(voce["blocking"])

    def test_il_file_non_si_riapre_a_ogni_richiesta(self) -> None:
        """Un guasto si dice una volta: riprovare a ogni prodotto costerebbe
        un tentativo di apertura per riga di confronto."""

        store = self.negozio_rotto()
        store.review()
        store.review()

        self.assertEqual(SERVER.MagazzinoConferme.call_count, 1)


class IlFileSiCreaSoloSeServeESiRestituisceTests(BancoDelleConferme):
    """⚠ Due difese che solo la **suite intera** ha fatto emergere.

    SQLite tiene il file aperto finché la connessione vive, e su Windows un
    file aperto non si cancella e non si rinomina — compresa la cartella che lo
    contiene. Alla prima versione dell'aggancio il magazzino si apriva a ogni
    salvataggio, anche quando non c'era niente da ricordare: ventinove prove
    morivano alla pulizia della cartella temporanea con «Il file è utilizzato
    da un altro processo», e nessun test mirato lo vedeva.
    """

    def test_un_programma_senza_conferme_non_crea_nessun_file(self) -> None:
        certa = offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)
        certa["requiresConfirmation"] = False
        store = self.negozio(confronto([prodotto("product:3", "TONNO", "8000000000010", [certa])]))

        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))
        store.review()

        self.assertFalse(self.conferme_path.exists(), "un file che non serve non si crea")

    def test_il_negozio_restituisce_il_file_quando_glielo_si_chiede(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))
        self.assertTrue(self.conferme_path.exists())

        store.chiudi()

        # Il file si può spostare: su Windows è la prova che nessuno lo tiene.
        spostato = self.conferme_path.with_suffix(".db.spostato")
        self.conferme_path.rename(spostato)
        self.assertTrue(spostato.is_file())
        # E il negozio riapre da capo alla prima domanda, senza lamentarsi.
        store.save_state(self.scelte(
            "product:3", fornitore="larice", quantita=3, confermato=True,
            versione=json.loads(store.state_path.read_text(encoding="utf-8"))["stateVersion"],
        ))
        self.assertTrue(self.conferme_path.exists())

    def test_chiuderlo_due_volte_non_fa_danno(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        store.chiudi()
        store.chiudi()


class LeConfermeSiScaricanoTests(BancoDelleConferme):
    """`conferme.db` e' la memoria «per sempre» del programma, e non si legge.

    `MagazzinoConferme.esporta` esisteva dal primo giorno, col suo perche'
    scritto nel docstring — «un `.db` non si legge a occhio, e l'utente deve
    poter guardare che cosa ha confermato» — e non la chiamava nessuno fuori
    dai collaudi. Da qui l'esportazione da Impostazioni.
    """

    def test_quello_che_si_scarica_e_quello_che_c_e_nel_magazzino(self) -> None:
        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000011", descrizione="TONNO MAR BLUE", riga=42)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        scaricate, uguaglianze = store.esporta_le_conferme()

        self.assertEqual(scaricate, self.conferme())
        self.assertEqual(uguaglianze, [])
        self.assertEqual(scaricate[0]["articolo"], "8000000000010|TONNO")
        self.assertTrue(scaricate[0]["accettata"])
        self.assertEqual(len(scaricate), 1)
        # Che cosa e' stato confermato, non solo che qualcosa lo e' stato: senza
        # questi due l'esportazione non permetterebbe di giudicare niente, che
        # e' il difetto che le uguaglianze avevano fino al 18 agosto.
        self.assertIn("fornitore", scaricate[0])
        self.assertIn("articolo", scaricate[0])

    def test_su_un_programma_senza_conferme_si_scarica_un_elenco_vuoto(self) -> None:
        """⚠ E soprattutto non nasce nessun `conferme.db`: aprire Impostazioni

        su un'installazione nuova non deve creare la memoria delle conferme.
        SQLite tiene il file aperto finche' la connessione vive, e su Windows
        un file aperto blocca la cartella che lo contiene."""

        store = self.negozio(confronto([]))

        self.assertEqual(store.esporta_le_conferme(), ([], []))
        self.assertFalse(self.conferme_path.exists(), "un file che non serve non si crea")

    def test_un_file_che_non_e_un_database_non_fa_fallire_lo_scarico(self) -> None:
        """Come per le uguaglianze: il motivo sta gia' fra gli avvisi del

        confronto, e una pagina che non si apre per una memoria in meno sarebbe
        sproporzionata."""

        self.conferme_path.parent.mkdir(parents=True, exist_ok=True)
        self.conferme_path.write_bytes(b"questo non e' un database SQLite")
        store = self.negozio(confronto([]))

        self.assertEqual(store.esporta_le_conferme(), ([], []))

    def test_un_magazzino_che_si_rompe_mentre_lo_si_legge_dice_perche(self) -> None:
        """L'altro guasto: il file si apre e poi la lettura non riesce. Il

        motivo non si butta — e' quello che finisce fra gli avvisi del
        confronto, come per le uguaglianze."""

        store = self.negozio(confronto([
            prodotto("product:3", "TONNO", "8000000000010",
                     [offerta("larice", ean="8000000000011", descrizione="TONNO MAR BLUE", riga=42)]),
        ]))
        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))
        magazzino = store.magazzino_conferme()
        self.assertIsNotNone(magazzino)

        def non_si_legge():
            raise MagazzinoNonUtilizzabile("Il file delle conferme non si legge più: disco assente")

        magazzino.esporta = non_si_legge

        self.assertEqual(store.esporta_le_conferme(), ([], []))
        self.assertIn("non si legge", store._conferme_guasto)


class LImprontaDelProdottoEQuellaDelModuloTests(BancoDelleConferme):
    """Il servizio non ricalcola l'identità per conto suo.

    Due definizioni di «stesso articolo» sono due verità sullo stesso dato, e
    il giorno in cui una cambia il magazzino smette di combaciare in silenzio.
    """

    def test_la_chiave_scritta_e_quella_che_dichiara_conferme_py(self) -> None:
        articolo = prodotto("product:3", "TONNO MAR BLUE 3X80", "8000000000010",
                            [offerta("larice", ean="8000000000010", descrizione="TONNO", riga=42)])
        store = self.negozio(confronto([articolo]))

        store.save_state(self.scelte("product:3", fornitore="larice", quantita=2, confermato=True))

        self.assertEqual(self.conferme()[0]["articolo"], impronta_prodotto(articolo))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
