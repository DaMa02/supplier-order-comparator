#!/usr/bin/env python3
"""«Questa riga del listino è il mio prodotto»: un clic, due effetti.

Il caso, misurato sul confronto vero `2026-08-17_1746`. `LUXA SAPONE LIQ.
EROG.250ML` sta nel gestionale col codice 4009428623194, che **solo CIPRESSO**
usa, a 1,28 €/pz. Lo stesso articolo sta su NOCE sotto 8729721830575, a
1,15. Il nome del gestionale non dice la variante — `EROG.` sta per erogatore, e
che l'erogatore sia l'`ORIGINAL` e non il `SETA` lo sa chi compra — quindi
nessun punteggio può dedurlo, e infatti la shortlist di BETULLA aveva la riga
giusta soltanto seconda.

Qui si prova che cosa deve succedere quando l'utente la sceglie a mano:

1. **subito**, l'offerta entra nel confronto di adesso;
2. **per sempre**, i due codici a barre restano dichiarati lo stesso articolo,
   ed è quello che al prossimo ricalcolo li fa incontrare da soli — su tutti i
   fornitori, non solo su quello scelto;
3. quando uno dei due codici **non c'è**, il secondo effetto non è possibile, e
   va detto invece di lasciarlo credere;
4. un «no» dato prima all'analisi automatica non torna a spegnere l'offerta
   appena scelta.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import server as server_module  # noqa: E402
from server import ReviewStore  # noqa: E402

EAN_GESTIONALE = "4009428623194"
EAN_NOCE = "8729721830575"

RIGHE_NOCE = [
    {"source_row": 4793, "ean": "8729014462339", "description": "LUXA SAPONE EROGATORE GO FRESH ML.250",
     "pieces_per_carton": "6", "unit_price_net": "1.1500", "usable": True},
    {"source_row": 4794, "ean": EAN_NOCE, "description": "LUXA SAPONE EROGATORE ORIGINAL ML.250",
     "pieces_per_carton": "6", "unit_price_net": "1.1500", "usable": True, "supplier_code": "0000000429063"},
    {"source_row": 5000, "ean": "", "description": "LISTINO", "usable": True},
]


class CatalogoFinto:
    """Il catalogo vero apre i listini dal disco: qui le righe sono dichiarate.

    Espone soltanto quello che il servizio gli chiede davvero, e `_offer` è la
    funzione vera del modulo — la regola su che cosa è ordinabile non si
    riscrive qui, altrimenti la prova direbbe di sé più di quanto sa.
    """

    def __init__(self, righe: dict[str, list[dict[str, Any]]]) -> None:
        self.righe_per_fornitore = righe
        self.load_errors: list[dict[str, str]] = []

    def enrich_review(self, review: dict[str, Any]) -> dict[str, Any]:
        return review

    def invalidate(self) -> None:
        return None

    def offerta_dalla_riga(self, review: dict[str, Any], supplier: str, source_row: Any):
        import catalog_search

        chiave = str(supplier or "").strip().casefold()
        record = next(
            (voce for voce in self.righe_per_fornitore.get(chiave, [])
             if str(voce.get("source_row")) == str(source_row)),
            None,
        )
        if record is None:
            raise ValueError(f"Nel listino {chiave} non c'è nessuna riga {source_row}")
        offerta = catalog_search._offer(chiave, record)
        if offerta is None:
            raise ValueError(f"La riga {source_row} di {chiave} non è ordinabile")
        return dict(record), offerta

    def fornitori_sfogliabili(self, review: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"id": nome, "name": nome.upper(), "righe": len(righe), "ordinabili": len(righe)}
                for nome, righe in sorted(self.righe_per_fornitore.items())]


def confronto(*, ean_prodotto: str = EAN_GESTIONALE) -> dict[str, Any]:
    return {
        "run": {"id": "run-prova", "status": "ready", "label": "Prova"},
        "files": [],
        "suppliers": [
            {"id": "noce", "name": "NOCE", "minimumOrder": 0},
            {"id": "cipresso", "name": "CIPRESSO", "minimumOrder": 0},
        ],
        "products": [{
            "id": "product:330",
            "itemType": "product",
            "ean": ean_prodotto,
            "name": "LUXA SAPONE LIQ. EROG.250ML",
            "description": "LUXA SAPONE LIQ. EROG.250ML",
            "quantity": 1,
            "selectedSupplierId": "cipresso",
            "offers": [
                {"supplierId": "cipresso", "available": True, "description": "LUXA SAPONE LIQUIDO EROGATORE 250",
                 "ean": ean_prodotto, "sourceRow": 900, "unitPriceNet": 1.28, "quantityFactor": 6,
                 "orderUnitPriceNet": 7.68},
                {"supplierId": "noce", "available": False, "status": "NON_TROVATO"},
            ],
            "components": [],
        }],
        "warnings": [],
    }


class BancoDellAbbinamento(unittest.TestCase):
    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.root = Path(temporanea.name)
        self.review_path = self.root / "review_data.json"

    def negozio(self, review: dict[str, Any] | None = None, righe=None) -> ReviewStore:
        self.review_path.write_text(
            json.dumps(review or confronto(), ensure_ascii=False), encoding="utf-8",
        )
        store = ReviewStore(
            self.review_path,
            self.root / "state.json",
            self.root / "uploads",
            self.root / "outputs",
            conferme_path=self.root / "history" / "conferme.db",
        )
        store.catalog = CatalogoFinto(righe or {"noce": list(RIGHE_NOCE)})
        self.addCleanup(self.chiudi, store)
        return store

    @staticmethod
    def chiudi(store: ReviewStore) -> None:
        magazzino = getattr(store, "_conferme", None)
        if magazzino is not None:
            magazzino.chiudi()

    @staticmethod
    def offerte_disponibili(review: dict[str, Any]) -> dict[str, Any]:
        prodotto = review["products"][0]
        return {
            str(offerta.get("supplierId")): offerta.get("unitPriceNet")
            for offerta in prodotto.get("offers") or []
            if offerta.get("available")
        }


class SubitoNelConfrontoDiAdesso(BancoDellAbbinamento):
    def test_la_riga_scelta_diventa_un_offerta(self) -> None:
        store = self.negozio()
        self.assertEqual(self.offerte_disponibili(store.review()), {"cipresso": 1.28})

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["ok"], True)
        self.assertEqual(self.offerte_disponibili(store.review()), {"cipresso": 1.28, "noce": 1.15})

    def test_l_offerta_dice_di_essere_stata_scelta_a_mano(self) -> None:
        """A valle non serve nessun caso speciale, ma chi guarda deve poter
        risalire: un'offerta entrata per una decisione umana non è una trovata
        dal codice a barre."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        offerta = next(
            voce for voce in store.review()["products"][0]["offers"]
            if voce["supplierId"] == "noce"
        )
        self.assertEqual(offerta["matchStatus"], "SCELTO_A_MANO")
        self.assertEqual(offerta["description"], "LUXA SAPONE EROGATORE ORIGINAL ML.250")
        self.assertIs(offerta["sceltaManuale"], True)

    def test_una_riga_che_non_si_puo_ordinare_non_si_abbina(self) -> None:
        """La riga 5000 è il separatore del listino: niente prezzo, niente
        confezione. Metterla in ordine vorrebbe dire una quantità che non si sa
        calcolare."""

        store = self.negozio()

        with self.assertRaises(ValueError):
            store.abbina_riga_di_listino({
                "productId": "product:330", "supplierId": "noce", "sourceRow": 5000,
            })

    def test_un_prodotto_che_non_c_e_non_si_abbina(self) -> None:
        store = self.negozio()

        with self.assertRaises(ValueError):
            store.abbina_riga_di_listino({
                "productId": "product:999", "supplierId": "noce", "sourceRow": 4794,
            })

    def test_scegliere_di_nuovo_sostituisce_invece_di_accumulare(self) -> None:
        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4793,
        })

        stato = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        scelte = [voce for voce in stato["manualMatches"] if voce["productId"] == "product:330"]
        self.assertEqual(len(scelte), 1)
        self.assertEqual(scelte[0]["sourceRow"], 4793)

    def test_un_no_dato_prima_non_spegne_l_offerta_appena_scelta(self) -> None:
        """Due risposte umane sullo stesso prodotto: vince la più recente, e si
        toglie invece di lasciarle convivere."""

        store = self.negozio()
        stato = {
            "schemaVersion": 1, "runId": "run-prova",
            "matchOverrides": [{
                "runId": "run-prova", "productId": "product:330", "supplierId": "noce",
                "candidateKey": "abc", "accepted": False,
            }],
        }
        (self.root / "state.json").write_text(json.dumps(stato), encoding="utf-8")

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertEqual(esito["rifiutiTolti"], 1)
        self.assertEqual(self.offerte_disponibili(store.review())["noce"], 1.15)


class PerSempreLUguaglianzaFraCodici(BancoDellAbbinamento):
    def test_i_due_codici_restano_dichiarati_uguali(self) -> None:
        store = self.negozio()

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["uguaglianzaRicordata"], True)
        self.assertEqual(store.uguaglianze_in_vigore(), [[EAN_GESTIONALE, EAN_NOCE]])
        self.assertIn("per tutti i fornitori", esito["message"])

    def test_la_dichiarazione_dice_da_dove_viene(self) -> None:
        """Un'uguaglianza sbagliata è un ordine sbagliato ogni lunedì: deve
        restare scritto guardando che cosa qualcuno l'ha dichiarata."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        voce = store.uguaglianze_dichiarate()[0]
        self.assertIn("riga 4794", voce["motivo"])
        self.assertIn("LUXA SAPONE EROGATORE ORIGINAL", voce["offerta"])
        self.assertTrue(voce["valida_dal"])

    def test_si_rilegge_in_impostazioni_e_si_toglie(self) -> None:
        """⚠ Dal 18 agosto 2026 l'elenco **non** viaggia più col confronto: sta
        in Impostazioni, con i nomi e una ricerca. Ci stava perché doveva
        essere visibile senza cercarla, ma il posto era dentro un prodotto — e
        due numeri di tredici cifre senza i nomi non permettono di giudicare se
        la dichiarazione è giusta."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })
        self.assertNotIn("uguaglianze", store.review())
        self.assertEqual(len(store.elenco_delle_uguaglianze()["uguaglianze"]), 1)

        esito = store.togli_uguaglianza({"codici": [EAN_GESTIONALE, EAN_NOCE]})

        self.assertIs(esito["tolta"], True)
        self.assertEqual(store.uguaglianze_in_vigore(), [])
        self.assertEqual(store.elenco_delle_uguaglianze()["uguaglianze"], [])

    def test_l_elenco_porta_i_due_nomi_e_il_fornitore(self) -> None:
        """È la ragione per cui l'elenco è stato spostato: con i soli codici non
        si può valutare la correttezza di quello che si è confermato."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        voce = store.elenco_delle_uguaglianze()["uguaglianze"][0]

        self.assertEqual(voce["gestionale"]["codice"], EAN_GESTIONALE)
        self.assertIn("LUXA SAPONE LIQ", voce["gestionale"]["nome"])
        self.assertEqual(voce["listino"]["fornitoreId"], "noce")
        self.assertIn("LUXA SAPONE EROGATORE ORIGINAL", voce["listino"]["nome"])
        self.assertEqual(voce["listino"]["codice"], EAN_NOCE)
        self.assertTrue(voce["dal"])

    def test_la_ricerca_guarda_tutto_quello_che_si_vede(self) -> None:
        """Un elenco che cresce di una riga a settimana dopo un anno ne ha
        cinquanta, e cercarle a occhio è il modo di non rileggerle mai."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        for cercato in ("luxa", "NOCE", "8729721830575", "riga 4794"):
            with self.subTest(cercato=cercato):
                self.assertEqual(len(store.elenco_delle_uguaglianze(cercato)["uguaglianze"]), 1)
        assente = store.elenco_delle_uguaglianze("sapone di marsiglia")
        self.assertEqual(assente["uguaglianze"], [])
        self.assertEqual(assente["totale"], 1)

    def test_senza_codice_a_barre_vale_solo_per_questo_confronto_e_lo_dice(self) -> None:
        """6 prodotti su 457 non hanno EAN nel gestionale, e gli espositori
        LARICE non ce l'hanno a listino: lì non c'è niente da dichiarare uguale,
        e lasciarlo sembrare uguale sarebbe una promessa che salta al primo
        ricalcolo."""

        store = self.negozio(review=confronto(ean_prodotto=""))

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["uguaglianzaRicordata"], False)
        # La frase dice **che cosa** manca, non solo che non ha funzionato: il
        # magazzino rifiuterebbe comunque un codice vuoto, ma con un messaggio
        # da magazzino, e chi legge la pagina deve sapere che il problema è il
        # prodotto e non un guasto del programma.
        self.assertIn("il prodotto non ha un codice a barre", esito["message"])
        self.assertIn("Non vale per i prossimi", esito["message"])
        self.assertEqual(store.uguaglianze_in_vigore(), [])
        # L'abbinamento di oggi però c'è, ed è il punto.
        self.assertEqual(self.offerte_disponibili(store.review())["noce"], 1.15)

    def test_due_codici_gia_uguali_non_producono_una_dichiarazione(self) -> None:
        store = self.negozio(review=confronto(ean_prodotto=EAN_NOCE))

        esito = store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertIs(esito["uguaglianzaRicordata"], False)
        self.assertIn("già lo stesso", esito["message"])
        self.assertEqual(store.uguaglianze_in_vigore(), [])


class IlMagazzinoNonSiApreSeNonServe(BancoDellAbbinamento):
    """Leggere il confronto non deve creare il file delle conferme.

    ⚠ È una regola che questo progetto ha già pagato una volta: SQLite tiene il
    file aperto finché la connessione vive, su Windows un file aperto blocca la
    cartella che lo contiene, e ventinove prove morirono alla pulizia della
    cartella temporanea mentre i test mirati erano tutti verdi. Le uguaglianze
    si leggono a **ogni** lettura del confronto: aprirle sempre riporterebbe
    quel guasto identico, e lo riporterebbe in una forma che solo la suite
    intera vede.
    """

    def test_leggere_il_confronto_non_crea_nessun_conferme_db(self) -> None:
        store = self.negozio()

        store.review()
        store.review()

        self.assertFalse(self.conferme_esiste(), "il confronto ha creato il magazzino senza motivo")
        self.assertEqual(store.uguaglianze_in_vigore(), [])
        self.assertEqual(store.uguaglianze_dichiarate(), [])

    def test_ma_dichiarare_un_uguaglianza_lo_crea(self) -> None:
        store = self.negozio()

        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.assertTrue(self.conferme_esiste())
        self.assertEqual(store.uguaglianze_in_vigore(), [[EAN_GESTIONALE, EAN_NOCE]])

    def conferme_esiste(self) -> bool:
        return (self.root / "history" / "conferme.db").exists()


class IlListinoSiSfoglia(BancoDellAbbinamento):
    def test_la_rotta_restituisce_le_righe_e_i_fornitori(self) -> None:
        store = self.negozio()
        store.catalog.sfoglia = lambda review, fornitore, **extra: {
            "supplier": fornitore, "supplierName": fornitore.upper(),
            "righe": [], "da": 0, "quante": 50, "trovate": 0, "totale": 3, "scartate": 1,
            "rigaCercata": None,
        }

        pagina = store.sfoglia_listino("noce")

        self.assertIs(pagina["ok"], True)
        self.assertEqual(pagina["totale"], 3)
        self.assertEqual([voce["id"] for voce in pagina["fornitori"]], ["noce"])


class LAutosalvataggioNonPortaViaNiente(BancoDellAbbinamento):
    """Il guasto del 19 agosto 2026, e la regola che gli impedisce di tornare.

    `validate_snapshot` ricostruisce da zero tutto `state.json` a ogni
    salvataggio, e la scheda nello snapshot manda soltanto le quantita': le
    chiavi scritte dalle rotte parziali le deve ricopiare dal disco. Ne
    ricopiava tre su quattro. Il quarto era l'abbinamento a mano, cioe' la
    strada che l'utente prende **quando l'analisi automatica ha gia' fallito**:
    450 ms dopo aver toccato una quantita' spariva, l'offerta scelta tornava
    non disponibile, e siccome una quantita' c'era il salvataggio si spegneva
    con `OFFERTA_NON_VALIDA` — da li' in poi non si salvava piu' niente.
    """

    def salva_le_quantita(self, store: ReviewStore, quantita: int = 1, fornitore: str = "noce") -> dict:
        """Quello che manda la scheda 450 ms dopo un tocco sulla quantita'.

        Gli abbinamenti a mano non ci sono: la scheda non li manda, e non e' un
        difetto suo — li ha scritti una rotta a parte.
        """

        sul_disco = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        return store.save_state({
            "runId": "run-prova",
            "stateVersion": sul_disco.get("stateVersion"),
            "currentStep": 2,
            "products": [{
                "id": "product:330",
                "quantity": quantita,
                "selectedSupplierId": fornitore,
                "confirmed": True,
                "quantitySource": "utente",
            }],
        })

    def stato_sul_disco(self) -> dict:
        return json.loads((self.root / "state.json").read_text(encoding="utf-8"))

    def test_la_riga_scelta_a_mano_e_ancora_li_dopo_il_salvataggio(self) -> None:
        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.salva_le_quantita(store)

        scelte = self.stato_sul_disco()["manualMatches"]
        self.assertEqual([voce["sourceRow"] for voce in scelte], [4794])
        self.assertEqual(
            self.offerte_disponibili(store.review()), {"cipresso": 1.28, "noce": 1.15},
        )

    def test_il_salvataggio_dopo_non_si_spegne(self) -> None:
        """La conseguenza vera: perso l'abbinamento, l'offerta con la quantita'
        sopra non e' piu' utilizzabile e `save_state` alza `SnapshotError` per
        sempre. Due salvataggi di fila bastano a farlo vedere."""

        store = self.negozio()
        store.abbina_riga_di_listino({
            "productId": "product:330", "supplierId": "noce", "sourceRow": 4794,
        })

        self.salva_le_quantita(store, quantita=1)
        self.salva_le_quantita(store, quantita=2)

        self.assertEqual(self.stato_sul_disco()["products"][0]["quantity"], 2)

    def test_le_altre_tre_chiavi_restano_ricopiate(self) -> None:
        """La correzione non doveva togliere quello che già funzionava."""

        store = self.negozio()
        stato = {
            "schemaVersion": 1, "runId": "run-prova", "stateVersion": 0,
            "manualProducts": [{"id": "product:999", "addedManually": True}],
            "matchOverrides": [{
                "runId": "run-prova", "productId": "product:331",
                "supplierId": "noce", "candidateKey": "abc", "accepted": False,
            }],
            "supplierDiscounts": {"noce": 3.5},
        }
        (self.root / "state.json").write_text(json.dumps(stato), encoding="utf-8")

        # Qui non c'è nessun abbinamento a mano: l'unica offerta utilizzabile è
        # quella che l'analisi ha trovato da sé.
        self.salva_le_quantita(store, fornitore="cipresso")

        dopo = self.stato_sul_disco()
        self.assertEqual(dopo["manualProducts"], stato["manualProducts"])
        self.assertEqual(dopo["matchOverrides"], stato["matchOverrides"])
        self.assertEqual(dopo["supplierDiscounts"], stato["supplierDiscounts"])


class NessunaRottaScriveUnaChiaveCheIlSalvataggioNonConosce(unittest.TestCase):
    """La prova che impedisce alla quinta chiave di ripetere la storia.

    Non prova un comportamento: prova che due elenchi coincidono — le chiavi
    che le rotte parziali scrivono dentro `state`, e quelle che
    `validate_snapshot` ricopia dal disco. Il difetto del 19 agosto era
    esattamente uno scarto fra i due, e nessuna prova poteva accorgersene
    perche' ognuna guardava una chiave sola.
    """

    # Le rotte che scrivono un pezzo di stato per conto loro, senza passare da
    # `save_state`. Se ne nasce una nuova, va aggiunta qui.
    ROTTE_PARZIALI = (
        "answer_rejected_candidate",
        "abbina_riga_di_listino",
        # Il «non è lo stesso articolo»: scrive nel magazzino delle conferme, e
        # dello stato tocca solo `products` — cioè una chiave di servizio, che la
        # scheda rimanda per intero a ogni salvataggio. È qui perché il lettore
        # la controlli: il giorno in cui qualcuno le facesse scrivere una chiave
        # sua, questa prova diventerebbe rossa invece di lasciar nascere la
        # quinta chiave che nessuno ricopia.
        "rifiuta_l_abbinamento",
        "set_supplier_discount",
        "add_manual_product",
    )

    # Le chiavi che `validate_snapshot` rifà da sé a ogni salvataggio, ed è
    # giusto che le riscriva invece di ricopiarle dal disco.
    #
    # `products` è l'unica che una rotta parziale scrive **e** che va comunque
    # ricostruita: `set_supplier_discount` crea la decisione dei prodotti che
    # non ne hanno ancora una, ma le decisioni le manda tutte la scheda a ogni
    # salvataggio (`snapshot()` mappa `state.review.products` per intero).
    # Ricopiarla dal disco vorrebbe dire ignorare le quantità appena scritte.
    CHIAVI_DI_SERVIZIO = frozenset({
        "schemaVersion", "runId", "updatedAt", "stateVersion", "stateVersionOrigin",
        "products",
    })

    @staticmethod
    def chiavi_scritte(metodo: ast.FunctionDef) -> set[str]:
        """Ogni `state["x"] = ...` e ogni `state.setdefault("x", ...)`."""

        trovate: set[str] = set()
        for nodo in ast.walk(metodo):
            if isinstance(nodo, ast.Assign):
                for bersaglio in nodo.targets:
                    if (isinstance(bersaglio, ast.Subscript)
                            and isinstance(bersaglio.value, ast.Name)
                            and bersaglio.value.id == "state"
                            and isinstance(bersaglio.slice, ast.Constant)
                            and isinstance(bersaglio.slice.value, str)):
                        trovate.add(bersaglio.slice.value)
            if (isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr == "setdefault"
                    and isinstance(nodo.func.value, ast.Name)
                    and nodo.func.value.id == "state"
                    and nodo.args
                    and isinstance(nodo.args[0], ast.Constant)
                    and isinstance(nodo.args[0].value, str)):
                trovate.add(nodo.args[0].value)
        return trovate

    def test_le_chiavi_delle_rotte_e_quelle_ricopiate_sono_le_stesse(self) -> None:
        sorgente = (RADICE / "app" / "server.py").read_text(encoding="utf-8")
        albero = ast.parse(sorgente)
        metodi = {
            nodo.name: nodo
            for nodo in ast.walk(albero)
            if isinstance(nodo, ast.FunctionDef) and nodo.name in self.ROTTE_PARZIALI
        }
        self.assertEqual(sorted(metodi), sorted(self.ROTTE_PARZIALI), "una rotta parziale è sparita o ha cambiato nome")

        scritte: set[str] = set()
        for nome in self.ROTTE_PARZIALI:
            scritte |= self.chiavi_scritte(metodi[nome])
        self.assertTrue(scritte, "nessuna scrittura trovata: il lettore non legge più il codice")

        self.assertEqual(
            scritte - self.CHIAVI_DI_SERVIZIO,
            set(server_module.CHIAVI_DI_STATO_RICOPIATE),
            "una rotta scrive una chiave che l'autosalvataggio non ricopia (o viceversa): "
            "vedi CHIAVI_DI_STATO_RICOPIATE in app/server.py",
        )


if __name__ == "__main__":
    unittest.main()
