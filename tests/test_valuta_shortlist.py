"""Il passo che accende la Fase 5: `scripts/valuta_shortlist.py`.

Quello che si difende qui e' il contratto con l'orchestratore, non la qualita'
delle risposte del modello — quella la misura il banco di prova.

- **Gli stati senza decisione si omettono dal file, non si inventano**: un
  guasto di rete non deve diventare un `UNRESOLVED` indistinguibile da «il
  modello non ha saputo».
- **I due file si scrivono sempre**, anche quando la fase e' degradata: un
  `ai_decisions.json` della run precedente rimasto sul disco verrebbe letto
  come fresco.
- **Il rapporto e' la sola contabilita' che qualcuno leggera'**, ed e' la fonte
  indipendente di `merge_match_decisions.py --decisions-attese`.
- **Il degrado e' un valore di ritorno**: nessuna chiave, rete giu', tetto
  raggiunto escono con esito 5 e i file al loro posto, non con un'eccezione.
- **L'impronta del caso attraversa i due script**: quella scritta qui dal caso
  mandato al modello e quella ricalcolata dal merge sulla shortlist devono
  coincidere. E' l'unico test che se ne accorge, se i due percorsi divergono.

Nessun test tocca la rete: il trasporto e' iniettato.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
for _cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts"):
    if str(_cartella) not in sys.path:
        sys.path.insert(0, str(_cartella))

import valuta_shortlist  # noqa: E402
from ai_client import Candidato, CasoValutazione, ClientAI  # noqa: E402
from build_semantic_shortlists import impronta_caso  # noqa: E402
from valuta_shortlist import (  # noqa: E402
    USCITA_DEGRADATO,
    USCITA_INGRESSO_NON_UTILIZZABILE,
    USCITA_OK,
    caso_dalla_voce,
    casi_dalle_shortlist,
)


CHIAVE_FINTA = "sk-prova-non-vera-0123456789"


def voce(
    *,
    riga: int = 12,
    fornitore: str = "betulla",
    descrizione: str = "PANTERA SHAMPOO 250ML RICCI NEW",
    candidati: tuple[tuple[int, str, float], ...] = (
        (441, "PANTERA SH.250 RICCI", 0.81),
        (512, "PANTERA BALSAMO 200", 0.42),
    ),
) -> dict[str, Any]:
    """Una voce di `semantic_shortlists.json` come la scrive la catena vera."""

    return {
        "gestionale_source_row": riga,
        "ean": "8009249129239",
        "description": descrizione,
        "supplier": fornitore,
        "reason": "EAN_ASSENTE",
        "candidates": [
            {
                "source_row": r,
                "ean": f"800{r}",
                "description": d,
                # I listini veri portano il prezzo **come stringa**.
                "unit_price_net": "3.9800",
                "score": s,
                "shared_tokens": ["PANTERA"],
                "attribute_conflicts": [],
                "query_attributes": {},
                "candidate_attributes": {},
            }
            for r, d, s in candidati
        ],
    }


def risposta_ok(contenuto: str, *, costo: float = 0.0001) -> tuple[int, dict]:
    return 200, {
        "model": "openai/gpt-5.6-luna",
        "provider": "DeepInfra",
        "choices": [{"finish_reason": "stop", "message": {"content": contenuto}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 40, "cost": costo},
    }


def contenuto(azione: str, source_row: Any, confidenza: str = "ALTA", motivo: str = "prova") -> str:
    return json.dumps(
        {"azione": azione, "source_row": source_row, "confidenza": confidenza, "motivo": motivo},
        ensure_ascii=False,
    )


def trasporto(risposte: dict[str, Any]):
    """Un trasporto che risponde in base all'**articolo cercato**.

    Il valore puo' essere: il contenuto JSON di una risposta buona, una coppia
    `(codice, corpo)` da restituire cosi' com'e', o un'eccezione da sollevare —
    che e' come si prova la rete giu' senza toccarla."""

    def chiamata(url: str, corpo: dict, intestazioni: dict, timeout: float):
        domanda = corpo["messages"][-1]["content"]
        articolo = domanda.splitlines()[0].split(": ", 1)[1]
        reazione = risposte[articolo]
        if isinstance(reazione, BaseException):
            raise reazione
        if isinstance(reazione, tuple):
            return reazione
        return risposta_ok(reazione)

    return chiamata


def righe_mostrate(corpo: dict) -> list[int]:
    return [int(n) for n in re.findall(r"source_row (\d+):", corpo["messages"][-1]["content"])]


def esegui(
    cartella: Path,
    shortlists: list[dict[str, Any]],
    *,
    trasporto_finto=None,
    chiave: str = CHIAVE_FINTA,
    modifiche: dict[str, Any] | None = None,
    crea_client=None,
) -> tuple[int, Any, Any, str]:
    """Lancia `main` in processo, con il client iniettato. Niente rete.

    Restituisce codice d'uscita, decisioni, rapporto e quello che ha stampato.
    `None` al posto di un file vuol dire che quel file non e' stato scritto."""

    percorso_shortlists = cartella / "semantic_shortlists.json"
    percorso_shortlists.write_bytes(
        json.dumps(shortlists, ensure_ascii=False).encode("utf-8")
    )
    decisioni = cartella / "ai_decisions.json"
    rapporto = cartella / "ai_rapporto.json"

    viste: dict[str, Any] = {}

    def crea(configurazione, memoria):
        viste.update(configurazione)
        if crea_client is not None:
            return crea_client(configurazione, memoria)
        return ClientAI(
            {**configurazione, **(modifiche or {})},
            trasporto=trasporto_finto,
            memoria=memoria,
            chiave=chiave,
        )

    schermo = io.StringIO()
    with redirect_stdout(schermo):
        codice = valuta_shortlist.main(
            [
                "--shortlists", str(percorso_shortlists),
                "--output", str(decisioni),
                "--rapporto", str(rapporto),
                # Sempre in una cartella temporanea: la memoria predefinita e'
                # `app/data/memoria_ai.json`, e i test non toccano i dati veri.
                "--memoria", str(cartella / "memoria_ai.json"),
            ],
            crea_client=crea,
        )
    return (
        codice,
        json.loads(decisioni.read_text(encoding="utf-8")) if decisioni.exists() else None,
        json.loads(rapporto.read_text(encoding="utf-8")) if rapporto.exists() else None,
        schermo.getvalue(),
    )


class DallIngressoAiCasiTests(unittest.TestCase):
    """La voce della shortlist diventa il caso che il modello vede. Se questa
    traduzione sbaglia, il modello risponde bene alla domanda sbagliata."""

    def test_la_voce_diventa_il_caso_con_i_campi_giusti(self) -> None:
        caso = caso_dalla_voce(voce(), 0)
        self.assertEqual(caso.gestionale_source_row, 12)
        self.assertEqual(caso.supplier, "betulla")
        self.assertEqual(caso.descrizione, "PANTERA SHAMPOO 250ML RICCI NEW")
        self.assertEqual([c.source_row for c in caso.candidati], [441, 512])
        self.assertEqual(caso.candidati[0].description, "PANTERA SH.250 RICCI")
        self.assertAlmostEqual(caso.candidati[0].score, 0.81)

    def test_il_candidato_non_porta_ne_ean_ne_prezzo(self) -> None:
        """Voluto: l'EAN e' la verita' di riferimento del banco di prova e
        mostrarlo renderebbe falsa ogni misura gia' fatta; il prezzo non c'entra
        con l'identita' del prodotto. Se un giorno qualcuno li aggiunge, deve
        rompere questo test e non una misura."""
        campi = set(vars(caso_dalla_voce(voce(), 0).candidati[0]))
        self.assertEqual(campi, {"source_row", "description", "score"})

    def test_le_descrizioni_non_si_normalizzano(self) -> None:
        """Il testo va al modello **e** dentro l'impronta. Toglierci gli spazi
        qui e non nel merge farebbe fallire il confronto su ogni caso, cioe'
        butterebbe una run intera."""
        caso = caso_dalla_voce(voce(descrizione="  PANTERA  250  "), 0)
        self.assertEqual(caso.descrizione, "  PANTERA  250  ")

    def test_un_caso_senza_candidati_non_va_al_modello(self) -> None:
        """`ClientAI` solleva su un caso senza candidati, ed e' giusto: chiedere
        di scegliere fra niente e' un uso sbagliato dell'API. Sono sei su 948
        nella run vera, quindi non e' un caso di scuola."""
        casi, senza = casi_dalle_shortlist([voce(), voce(riga=13, candidati=())])
        self.assertEqual([c.gestionale_source_row for c in casi], [12])
        self.assertEqual([c.gestionale_source_row for c in senza], [13])

    def test_una_coppia_duplicata_si_ferma_qui(self) -> None:
        """Piu' avanti diventerebbe una «decisione duplicata» di
        `merge_match_decisions.py`, cioe' manderebbe a cercare il guasto nel
        file sbagliato."""
        with self.assertRaises(ValueError) as errore:
            casi_dalle_shortlist([voce(), voce()])
        self.assertIn("più di una volta", str(errore.exception))

    def test_gli_ingressi_malformati_sono_un_guasto_non_un_caso_saltato(self) -> None:
        """La shortlist la scrive `build_semantic_shortlists.py`: una voce
        storta vuol dire che qualcosa a monte si e' rotto, e proseguire in
        silenzio toglierebbe prodotti dal confronto senza dirlo."""
        casi_storti = {
            "voce non oggetto": ["non un oggetto"],
            "riga assente": [{**voce(), "gestionale_source_row": None}],
            "riga con la virgola": [{**voce(), "gestionale_source_row": 12.5}],
            "riga booleana": [{**voce(), "gestionale_source_row": True}],
            "fornitore vuoto": [{**voce(), "supplier": "   "}],
            "descrizione assente": [{**voce(), "description": None}],
            "descrizione non testo": [{**voce(), "description": ["PANTERA"]}],
            "candidati non lista": [{**voce(), "candidates": {"source_row": 1}}],
            "candidato non oggetto": [{**voce(), "candidates": ["441"]}],
            "candidato senza riga": [{**voce(), "candidates": [{"description": "X", "score": 0.5}]}],
            "candidato senza descrizione": [{**voce(), "candidates": [{"source_row": 1, "score": 0.5}]}],
            "candidato senza punteggio": [{**voce(), "candidates": [{"source_row": 1, "description": "X"}]}],
            "punteggio non numerico": [
                {**voce(), "candidates": [{"source_row": 1, "description": "X", "score": "molto"}]}
            ],
            # `float(True)` fa 1.0 in silenzio, e un punteggio booleano
            # entrerebbe nell'impronta come punteggio pieno.
            "punteggio booleano": [
                {**voce(), "candidates": [{"source_row": 1, "description": "X", "score": True}]}
            ],
            # `int(float("inf"))` solleva `OverflowError`, che non è fra quelle
            # che `main` cattura: erano un traceback, esito 1 e nessuno dei due
            # file scritto. `NaN` finiva bene per caso.
            "riga infinita": [{**voce(), "gestionale_source_row": float("inf")}],
            "riga non numerabile": [{**voce(), "gestionale_source_row": float("nan")}],
        }
        for nome, shortlists in casi_storti.items():
            with self.subTest(nome), self.assertRaises(ValueError):
                casi_dalle_shortlist(shortlists)

    def test_una_riga_scritta_come_stringa_si_accetta(self) -> None:
        """JSON scritto da un altro programma può portare `"12"`: non e' un
        guasto. `12.5` invece lo e', perche' `int()` lo troncherebbe in
        silenzio e una riga sbagliata di uno e' il difetto della 6a."""
        caso = caso_dalla_voce({**voce(), "gestionale_source_row": "12"}, 0)
        self.assertEqual(caso.gestionale_source_row, 12)

    def test_una_shortlist_che_non_e_una_lista_si_ferma(self) -> None:
        with self.assertRaises(ValueError):
            casi_dalle_shortlist({"gestionale_source_row": 12})


class IlFileDelleDecisioniTests(unittest.TestCase):
    """Che cosa finisce nel file, e soprattutto che cosa non ci finisce."""

    def test_una_decisione_per_caso_deciso_nel_formato_del_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        self.assertEqual(codice, USCITA_OK)
        self.assertEqual(len(decisioni), 1)
        riga = decisioni[0]
        self.assertEqual(riga["gestionale_source_row"], 12)
        self.assertEqual(riga["supplier"], "betulla")
        self.assertEqual(riga["action"], "ACCEPT")
        self.assertEqual(riga["source_row"], 441)
        self.assertEqual(riga["confidence"], "ALTA")
        self.assertTrue(riga["rationale"])
        self.assertIn("requires_user_confirmation", riga)
        self.assertEqual(rapporto["casi_decisi"], 1)

    def test_ogni_decisione_porta_l_impronta_del_caso_mandato_al_modello(self) -> None:
        """L'impronta e' l'unica cosa che lega una decisione a cio' che il
        modello aveva davanti: senza, un file di un'altra run passa liscio."""
        una = voce()
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, _rapporto, _schermo = esegui(
                Path(temporanea),
                [una],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        attesa = impronta_caso(
            12,
            "betulla",
            "PANTERA SHAMPOO 250ML RICCI NEW",
            [(c["source_row"], c["description"], c["score"]) for c in una["candidates"]],
        )
        self.assertEqual(decisioni[0]["ai_impronta_caso"], attesa)
        self.assertEqual(decisioni[0]["ai_articolo_mostrato"], "PANTERA SHAMPOO 250ML RICCI NEW")

    def test_gli_stati_senza_decisione_non_producono_nessuna_riga(self) -> None:
        """Scrivere un `UNRESOLVED` finto al posto di un guasto di rete
        cancellerebbe la differenza fra «il modello non ha saputo» e «non ho
        potuto chiedere» — e la seconda si rimedia rilanciando."""
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [
                    voce(descrizione="DECIDE"),
                    voce(riga=13, descrizione="RETE GIU'"),
                    voce(riga=14, descrizione="FUORI SCHEMA"),
                ],
                trasporto_finto=trasporto({
                    "DECIDE": contenuto("REJECT", None),
                    "RETE GIU'": OSError("connessione rifiutata"),
                    "FUORI SCHEMA": "non sono nemmeno json",
                }),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual([r["gestionale_source_row"] for r in decisioni], [12])
        self.assertEqual(rapporto["casi_valutabili"], 3)
        self.assertEqual(rapporto["casi_decisi"], 1)
        self.assertEqual(rapporto["casi_senza_decisione"], 2)

    def test_un_rifiuto_non_porta_nessuna_riga_scelta(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, _rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("REJECT", 441)}
                ),
            )
        self.assertEqual(decisioni[0]["action"], "REJECT")
        self.assertIsNone(decisioni[0]["source_row"])

    def test_il_file_e_una_lista_anche_quando_e_vuota(self) -> None:
        """`merge_match_decisions.py` pretende una lista: un file assente o un
        oggetto lo fanno uscire 2 e 3."""
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [voce(candidati=())], trasporto_finto=trasporto({})
            )
        self.assertEqual(decisioni, [])
        self.assertEqual(rapporto["casi_senza_candidati"], 1)
        self.assertEqual(rapporto["coppie_senza_candidati"], [[12, "betulla"]])
        # E siccome quell'unico caso non si e' potuto nemmeno tentare, la fase
        # e' degradata: vedi la prova qui sotto.
        self.assertEqual(codice, USCITA_DEGRADATO)

    def test_nessun_candidato_da_nessuna_parte_non_e_un_successo(self) -> None:
        """Il caso che la prima versione dichiarava «valutato tutto quello che
        c'era da valutare» uscendo 0, e che la revisione ha smontato eseguendolo.

        Non serve la rete giu' e non serve una chiave sbagliata: basta che un
        adattatore cambi e un listino esca senza prezzi.
        `build_semantic_shortlists.py` scarta quelle righe e scrive centinaia di
        voci con `candidates: []`; la fase AI non valuta niente e, senza questo
        controllo, tutta la catena arrivava in fondo con esito 0."""
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(riga=riga, candidati=()) for riga in range(1, 21)],
                trasporto_finto=trasporto({}),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertTrue(rapporto["degradato"])
        self.assertIn("nessuno dei 20 casi ricevuti", rapporto["motivo_degrado"])
        self.assertIn("un listino non si è caricato", rapporto["motivo_degrado"])

    def test_una_coda_vuota_invece_e_un_successo(self) -> None:
        """La differenza che il controllo qui sopra deve saper fare. Se l'EAN ha
        risolto tutto, `semantic_shortlists.json` e' una **lista vuota**: non
        c'era lavoro, e dichiararsi degradati manderebbe l'orchestratore a
        rifare una fase che non aveva niente da fare."""
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [], trasporto_finto=trasporto({})
            )
        self.assertEqual(codice, USCITA_OK)
        self.assertEqual(decisioni, [])
        self.assertFalse(rapporto["degradato"])
        self.assertEqual(rapporto["casi_ricevuti"], 0)

    def test_gli_esiti_fuori_ordine_non_producono_nessuna_decisione(self) -> None:
        """L'accoppiamento caso-esito si appoggia all'ordine, che il client
        promette. Se quella promessa saltasse, ogni decisione finirebbe
        sull'articolo sbagliato con la sua brava confidenza `ALTA`: meglio zero
        decisioni che 948 attribuite a caso."""

        class ClientCheMescola:
            contabilita = {"chiamate": 2, "costo_usd": 0.0, "per_stato": {}, "dalla_memoria": 0}

            def valuta_molti_con_verifica(self, casi):
                return list(reversed([EsitoFinto(caso) for caso in casi]))

        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(), voce(riga=13, descrizione="ALTRO")],
                crea_client=lambda configurazione, memoria: ClientCheMescola(),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertIn("fuori posto", rapporto["motivo_degrado"])

    def test_un_esito_in_meno_ferma_tutto_invece_di_accoppiare_a_caso(self) -> None:
        """L'ordine non basta: `zip` si ferma al più corto e non dice niente.
        La prova qui sopra passa una lista della stessa lunghezza, quindi il
        controllo che conta gli esiti non l'aveva percorso nessuno — la
        mutazione che lo toglie restava verde. Un client che ne restituisce
        nove su dieci farebbe sparire in silenzio decisioni già pagate."""

        class ClientCheNePerdeUno:
            contabilita = {"chiamate": 1, "costo_usd": 0.0, "per_stato": {}, "dalla_memoria": 0}

            def valuta_molti_con_verifica(self, casi):
                return [EsitoFinto(casi[0])]

        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(), voce(riga=13, descrizione="ALTRO")],
                crea_client=lambda configurazione, memoria: ClientCheNePerdeUno(),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertIn("1 esiti per 2 casi", rapporto["motivo_degrado"])

    def test_ogni_decisione_dichiara_con_che_cosa_e_stata_presa(self) -> None:
        """L'impronta dice che il caso è lo stesso, non con che cosa è stato
        giudicato. Se il listino di un fornitore non cambia, il caso **è**
        identico e una decisione della settimana scorsa passa l'impronta: senza
        questi tre campi, una presa con il prompt `v1` — che sbagliava 5 `ALTA`
        e non aveva la verifica avversariale — entrerebbe in un ordine di oggi
        senza che niente lo dica. Qui si scrivono; a confrontarli con la
        configurazione viva sarà l'orchestratore."""
        with tempfile.TemporaryDirectory() as temporanea, mock.patch.dict(
            os.environ, {"OPENROUTER_MODEL": "prova/modello-di-prova"}
        ):
            _codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        self.assertEqual(decisioni[0]["ai_modello"], "prova/modello-di-prova")
        self.assertEqual(decisioni[0]["ai_versione_prompt"], rapporto["versione_prompt"])
        self.assertEqual(decisioni[0]["ai_versione_avversario"], rapporto["versione_avversario"])


class EsitoFinto:
    """Un esito che porta la decisione del suo caso, per provare l'ordine."""

    stato = "OK"

    def __init__(self, caso: CasoValutazione) -> None:
        self.decisione = {
            "gestionale_source_row": caso.gestionale_source_row,
            "supplier": caso.supplier,
            "action": "REJECT",
            "source_row": None,
            "confidence": "MEDIA",
            "rationale": "finto",
            "requires_user_confirmation": True,
        }


class IlRapportoTests(unittest.TestCase):
    """Il rapporto e' la sola contabilita' che qualcuno leggera': il programma
    finito gira da solo, e i log non li legge nessuno."""

    def test_i_conteggi_tornano_e_casi_decisi_e_la_lunghezza_del_file(self) -> None:
        """`casi_decisi` e' il numero che l'orchestratore passera' a
        `--decisions-attese`: se non fosse la lunghezza del file, la
        riconciliazione fallirebbe su una run sana."""
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE"), voce(riga=14, candidati=())],
                trasporto_finto=trasporto({
                    "UNO": contenuto("ACCEPT", 441),
                    "DUE": contenuto("REJECT", None),
                }),
            )
        self.assertEqual(rapporto["casi_ricevuti"], 3)
        self.assertEqual(rapporto["casi_valutabili"], 2)
        self.assertEqual(rapporto["casi_senza_candidati"], 1)
        self.assertEqual(rapporto["casi_decisi"], len(decisioni))
        self.assertEqual(rapporto["per_azione"], {"ACCEPT": 1, "REJECT": 1})
        self.assertFalse(rapporto["degradato"])

    def test_i_due_conteggi_per_stato_sono_diversi_e_non_e_una_svista(self) -> None:
        """`per_stato` ha una voce per caso; quello del client comprende anche
        il secondo giro avversariale, che parte su ogni `ACCEPT`. Sommare il
        secondo e leggerlo come «casi» darebbe un numero piu' grande del
        lavoro fatto."""
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        self.assertEqual(sum(rapporto["per_stato"].values()), 1)
        self.assertEqual(sum(rapporto["per_stato_incluse_verifiche"].values()), 2)
        self.assertEqual(rapporto["chiamate"], 2)

    def test_la_spesa_e_la_durata_ci_sono(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        self.assertAlmostEqual(rapporto["costo_usd"], 0.0002)
        self.assertGreaterEqual(rapporto["durata_s"], 0.0)

    def test_il_rapporto_dichiara_la_configurazione_viva_non_una_copia_scritta(self) -> None:
        """Se il modello lo decide una variabile d'ambiente, il rapporto deve
        dire quello vero: e' la sola traccia di quale modello ha risposto."""
        with tempfile.TemporaryDirectory() as temporanea, mock.patch.dict(
            os.environ, {"OPENROUTER_MODEL": "prova/modello-di-prova"}
        ):
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("REJECT", None)}
                ),
            )
        self.assertEqual(rapporto["model"], "prova/modello-di-prova")
        self.assertEqual(rapporto["versione_prompt"], "v3")

    def test_il_motivo_del_degrado_dice_quali_stati(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE")],
                trasporto_finto=trasporto({
                    "UNO": OSError("rete giù"),
                    "DUE": OSError("rete giù"),
                }),
            )
        self.assertTrue(rapporto["degradato"])
        self.assertIn("2 casi su 2", rapporto["motivo_degrado"])
        self.assertIn("ERRORE_RETE", rapporto["motivo_degrado"])

    def test_le_decisioni_si_scrivono_prima_del_rapporto(self) -> None:
        """L'ordine stava in un commento e non lo difendeva nessuno: scambiare
        le due righe restava verde. Se il processo muore fra i due,
        l'orchestratore deve trovare un **rapporto mancante** — cioè
        accorgersene — invece di un rapporto che dichiara 942 decisioni accanto
        al file della run scorsa."""
        scritti: list[str] = []
        vero = valuta_shortlist.scrivi_json

        def annota(percorso, documento):
            scritti.append(percorso.name)
            vero(percorso, documento)

        with tempfile.TemporaryDirectory() as temporanea, mock.patch.object(
            valuta_shortlist, "scrivi_json", annota
        ):
            esegui(Path(temporanea), [voce(candidati=())], trasporto_finto=trasporto({}))
        self.assertEqual(scritti, ["ai_decisions.json", "ai_rapporto.json"])

    def test_il_rapporto_della_run_precedente_non_sopravvive(self) -> None:
        """Se la scrittura del rapporto fallisce, quello di ieri non deve
        restare accanto alle decisioni di oggi: il commento diceva «chi legge
        trova un rapporto mancante e se ne accorge», e non era vero."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            vecchio = cartella / "ai_rapporto.json"
            vecchio.write_bytes(b'{"casi_decisi": 999}')

            def esplode(percorso, documento):
                if percorso.name == "ai_decisions.json":
                    raise OSError("disco pieno")

            with mock.patch.object(valuta_shortlist, "scrivi_json", esplode):
                codice, _decisioni, rapporto, _schermo = esegui(
                    cartella, [voce(candidati=())], trasporto_finto=trasporto({})
                )
        self.assertEqual(codice, USCITA_INGRESSO_NON_UTILIZZABILE)
        self.assertIsNone(rapporto)

    def test_il_rapporto_si_stampa_anche_a_schermo(self) -> None:
        """Il merge ha già la sua prova che il riepilogo si stampa sempre. Qui
        non c'era, e «nessuno leggerà i log» vale soprattutto per chi lancia la
        fase a mano: senza questi numeri non sa se ne mancavano due o
        novecento."""
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE")],
                trasporto_finto=trasporto({
                    "UNO": contenuto("ACCEPT", 441),
                    "DUE": OSError("rete giù"),
                }),
            )
        stampato = json.loads(schermo[schermo.index("{"):])
        self.assertEqual(stampato["casi_decisi"], rapporto["casi_decisi"])
        self.assertEqual(stampato["casi_senza_decisione"], 1)
        self.assertEqual(stampato["parallelismo"], rapporto["parallelismo"])

    def test_il_rapporto_non_contiene_mai_la_chiave(self) -> None:
        """La chiave passa nelle intestazioni di ogni chiamata: basta un
        messaggio d'errore che riporti la richiesta perche' finisca in un file
        che poi qualcuno allega a un rapporto."""
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, rapporto, schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto({
                    "PANTERA SHAMPOO 250ML RICCI NEW": (
                        401,
                        {"error": {"message": f"chiave non valida: {CHIAVE_FINTA}"}},
                    )
                }),
            )
        testo = json.dumps(rapporto, ensure_ascii=False) + json.dumps(decisioni) + schermo
        self.assertNotIn(CHIAVE_FINTA, testo)


class IlDegradoEUnValoreDiRitornoTests(unittest.TestCase):
    """«Se OpenRouter non risponde il programma tira dritto» e' una decisione
    presa: la fase degradata esce con i file al loro posto e un codice che lo
    dice, non con un'eccezione e nessun artefatto."""

    def test_senza_chiave_i_file_ci_sono_lo_stesso_ed_esce_cinque(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [voce()], chiave="", trasporto_finto=trasporto({})
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertEqual(rapporto["per_stato"], {"SENZA_CHIAVE": 1})
        self.assertIn("SENZA_CHIAVE", rapporto["motivo_degrado"])

    def test_il_tetto_di_spesa_raggiunto_non_e_un_errore_d_uso(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE")],
                modifiche={"tetto_chiamate": 1, "tetto_spesa_usd": 100.0},
                trasporto_finto=trasporto({
                    "UNO": contenuto("REJECT", None),
                    "DUE": contenuto("REJECT", None),
                }),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(len(decisioni), 1)
        self.assertIn("TETTO_CHIAMATE", rapporto["per_stato"])

    def test_un_guasto_imprevisto_scrive_lo_stesso_i_due_file(self) -> None:
        """Senza questa rete l'orchestratore si troverebbe un traceback e
        nessun artefatto — e il `resolved_matches.json` della run precedente
        resterebbe sul disco a farsi leggere come fresco."""

        def esplode(configurazione, memoria):
            raise RuntimeError("il client non si costruisce")

        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [voce()], crea_client=esplode
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertIn("RuntimeError", rapporto["motivo_degrado"])
        self.assertIn("il client non si costruisce", rapporto["motivo_degrado"])

    def test_un_guasto_imprevisto_non_lascia_uscire_la_chiave(self) -> None:
        def esplode(configurazione, memoria):
            raise RuntimeError(f"Authorization: Bearer {CHIAVE_FINTA}")

        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [voce()], crea_client=esplode
            )
        self.assertNotIn(CHIAVE_FINTA, json.dumps(rapporto, ensure_ascii=False))

    def test_tutto_valutato_esce_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE")],
                trasporto_finto=trasporto({
                    "UNO": contenuto("ACCEPT", 441),
                    "DUE": contenuto("UNRESOLVED", None),
                }),
            )
        self.assertEqual(codice, USCITA_OK)
        self.assertFalse(rapporto["degradato"])
        self.assertEqual(rapporto["motivo_degrado"], "")


class LaVerificaAvversarialeArrivaFinQuiTests(unittest.TestCase):
    """Il secondo giro non e' un dettaglio del client: e' quello che rende
    `ALTA` degno di fiducia, ed e' la ragione per cui `merge_match_decisions.py`
    fa entrare un `ALTA` in ordine senza chiedere niente a nessuno."""

    def test_un_accept_non_confermato_scende_a_unresolved(self) -> None:
        chiamate: list[list[int]] = []

        def chiamata(url, corpo, intestazioni, timeout):
            righe = righe_mostrate(corpo)
            chiamate.append(righe)
            # Il secondo giro mostra un candidato solo: e' quello.
            if len(righe) == 1:
                return risposta_ok(contenuto("REJECT", None, "ALTA", "non è lo stesso formato"))
            return risposta_ok(contenuto("ACCEPT", 441))

        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, _rapporto, _schermo = esegui(
                Path(temporanea), [voce()], trasporto_finto=chiamata
            )
        self.assertEqual(len(chiamate), 2)
        self.assertEqual(decisioni[0]["action"], "UNRESOLVED")
        self.assertIsNone(decisioni[0]["source_row"])
        self.assertIn("verifica avversariale", decisioni[0]["rationale"])


class LaCliVeraTests(unittest.TestCase):
    """Lo script lanciato come lo lancera' l'orchestratore: un sottoprocesso.

    Nessuna di queste prove tocca la rete — o l'ingresso e' malformato, o non
    c'e' nessun caso con candidati da mandare."""

    def lancia(self, argomenti: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "valuta_shortlist.py"), *argomenti],
            capture_output=True,
        )

    def test_i_tre_percorsi_sono_obbligatori(self) -> None:
        """`--rapporto` facoltativo riaprirebbe da un'altra porta il difetto
        che la 6a ha appena chiuso: basterebbe dimenticarlo."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = cartella / "sl.json"
            shortlists.write_bytes(b"[]")
            completi = [
                "--shortlists", str(shortlists),
                "--output", str(cartella / "dec.json"),
                "--rapporto", str(cartella / "rap.json"),
            ]
            for mancante in ("--shortlists", "--output", "--rapporto"):
                with self.subTest(mancante):
                    indice = completi.index(mancante)
                    ridotti = completi[:indice] + completi[indice + 2:]
                    esito = self.lancia(ridotti + ["--memoria", str(cartella / "mem.json")])
                    self.assertEqual(esito.returncode, 2)
                    self.assertIn(mancante, esito.stderr.decode("utf-8", errors="replace"))

    def test_un_ingresso_illeggibile_esce_due_senza_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            esito = self.lancia([
                "--shortlists", str(cartella / "non-c-e.json"),
                "--output", str(cartella / "dec.json"),
                "--rapporto", str(cartella / "rap.json"),
                "--memoria", str(cartella / "mem.json"),
            ])
        self.assertEqual(esito.returncode, USCITA_INGRESSO_NON_UTILIZZABILE)
        self.assertIn("Ingresso non utilizzabile", esito.stdout.decode("utf-8"))
        self.assertNotIn("Traceback", esito.stderr.decode("utf-8"))

    def test_l_uscita_e_utf8_qualunque_sia_la_console(self) -> None:
        """Su Windows un processo che scrive su una pipe usa cp1252, e qui
        passano le descrizioni vere dei prodotti: basta un `CAFFÈ` perche'
        l'orchestratore legga byte che non sono UTF-8. Trovato da un test
        della 6a, non da una run."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = cartella / "sl.json"
            shortlists.write_bytes(
                json.dumps([{**voce(), "description": ["CAFFÈ MISCELA PERÙ"]}], ensure_ascii=False).encode("utf-8")
            )
            esito = self.lancia([
                "--shortlists", str(shortlists),
                "--output", str(cartella / "dec.json"),
                "--rapporto", str(cartella / "rap.json"),
                "--memoria", str(cartella / "mem.json"),
            ])
        self.assertEqual(esito.returncode, USCITA_INGRESSO_NON_UTILIZZABILE)
        self.assertIn("CAFFÈ MISCELA PERÙ", esito.stdout.decode("utf-8"))

    def test_senza_casi_da_valutare_scrive_i_due_file_ed_esce_zero(self) -> None:
        """La catena deve poter proseguire anche quando l'EAN ha risolto tutto:
        `build_review_data.py` senza `--resolved` produce un confronto monco.

        La coda vuota e' una **lista vuota**, non una lista di voci senza
        candidati: quella e' un listino che non si e' caricato, ed esce 5."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = cartella / "sl.json"
            shortlists.write_bytes(b"[]")
            decisioni = cartella / "dec.json"
            rapporto = cartella / "rap.json"
            esito = self.lancia([
                "--shortlists", str(shortlists),
                "--output", str(decisioni),
                "--rapporto", str(rapporto),
                "--memoria", str(cartella / "mem.json"),
            ])
            self.assertEqual(esito.returncode, USCITA_OK, esito.stdout.decode("utf-8", "replace"))
            self.assertEqual(json.loads(decisioni.read_text(encoding="utf-8")), [])
            contabilita = json.loads(rapporto.read_text(encoding="utf-8"))
        self.assertEqual(contabilita["casi_ricevuti"], 0)
        self.assertEqual(contabilita["chiamate"], 0)
        self.assertFalse(contabilita["degradato"])

    def test_un_uscita_non_scrivibile_esce_due_e_non_finge_di_aver_deciso(self) -> None:
        """Esito 2, non 0: i due file non ci sono. Uscire 0 direbbe
        all'orchestratore «valutato tutto quello che c'era da valutare», e il
        `ai_decisions.json` della run precedente resterebbe sul disco a farsi
        leggere come fresco — che è il guasto contro cui è scritta metà di
        questa CLI. La mutazione che sostituiva 2 con 0 restava verde."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = cartella / "sl.json"
            shortlists.write_bytes(json.dumps([voce(candidati=())], ensure_ascii=False).encode("utf-8"))
            # Una cartella al posto del file: `write_bytes` solleva `OSError`.
            impossibile = cartella / "dec.json"
            impossibile.mkdir()
            esito = self.lancia([
                "--shortlists", str(shortlists),
                "--output", str(impossibile),
                "--rapporto", str(cartella / "rap.json"),
                "--memoria", str(cartella / "mem.json"),
            ])
            rapporto_scritto = (cartella / "rap.json").exists()
        self.assertEqual(esito.returncode, USCITA_INGRESSO_NON_UTILIZZABILE)
        self.assertIn("Uscite non scrivibili", esito.stdout.decode("utf-8"))
        self.assertNotIn("Traceback", esito.stderr.decode("utf-8"))
        self.assertFalse(rapporto_scritto)

    def test_la_memoria_predefinita_e_quella_del_programma(self) -> None:
        """Un caso identico non si paga due volte, nemmeno fra una run e
        l'altra: il valore predefinito e' il file vero, non una cartella
        temporanea."""
        args = valuta_shortlist.parse_args([
            "--shortlists", "a.json", "--output", "b.json", "--rapporto", "c.json",
        ])
        self.assertEqual(args.memoria.name, "memoria_ai.json")
        self.assertEqual(args.memoria.parent.name, "data")


class LImprontaAttraversaLaCatenaTests(unittest.TestCase):
    """I due percorsi che calcolano l'impronta — qui dal caso mandato al
    modello, nel merge dalla shortlist di oggi — devono dare lo stesso numero.

    E' l'unico test che se ne accorge se divergono: dentro `valuta_shortlist` e
    dentro `merge_match_decisions` ciascuno e' coerente con se stesso."""

    def prepara(
        self,
        cartella: Path,
        shortlists: list[dict[str, Any]],
        matching_in_piu: list[dict[str, Any]] | None = None,
    ) -> tuple[Path, Path, Path]:
        percorso_shortlists = cartella / "semantic_shortlists.json"
        percorso_shortlists.write_bytes(json.dumps(shortlists, ensure_ascii=False).encode("utf-8"))

        matching = [{
            "gestionale": {"source_row": v["gestionale_source_row"], "description": v["description"], "ean": v["ean"]},
            "suppliers": {v["supplier"]: {"status": "EAN_ASSENTE", "usable_candidates": []}},
        } for v in shortlists] + list(matching_in_piu or [])
        percorso_matching = cartella / "matching.json"
        percorso_matching.write_bytes(json.dumps(matching, ensure_ascii=False).encode("utf-8"))

        normalized: dict[str, list[dict[str, Any]]] = {}
        for v in shortlists:
            righe = normalized.setdefault(v["supplier"], [])
            for candidato in v["candidates"]:
                righe.append({
                    "source_row": candidato["source_row"],
                    "ean": candidato["ean"],
                    "description": candidato["description"],
                    "unit_price_net": candidato["unit_price_net"],
                    "order_multiplier": 6,
                    "usable": True,
                })
        percorso_normalized = cartella / "normalized.json"
        percorso_normalized.write_bytes(json.dumps(normalized, ensure_ascii=False).encode("utf-8"))
        return percorso_matching, percorso_normalized, percorso_shortlists

    def merge(
        self,
        cartella: Path,
        shortlists: list[dict[str, Any]],
        decisioni: Path,
        attese: int,
        matching_in_piu: list[dict[str, Any]] | None = None,
    ):
        percorso_matching, percorso_normalized, percorso_shortlists = self.prepara(
            cartella, shortlists, matching_in_piu
        )
        uscita = cartella / "resolved.json"
        esito = subprocess.run(
            [
                sys.executable, str(SKILL_ROOT / "scripts" / "merge_match_decisions.py"),
                "--matching", str(percorso_matching),
                "--normalized", str(percorso_normalized),
                "--shortlists", str(percorso_shortlists),
                "--decisions", str(decisioni),
                "--decisions-attese", str(attese),
                "--output", str(uscita),
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        return esito, json.loads(uscita.read_text(encoding="utf-8"))

    def decidi(self, cartella: Path, shortlists: list[dict[str, Any]]) -> Path:
        codice, decisioni, _rapporto, _schermo = esegui(
            cartella,
            shortlists,
            trasporto_finto=trasporto(
                {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
            ),
        )
        self.assertEqual(codice, USCITA_OK)
        self.assertEqual(len(decisioni), 1)
        return cartella / "ai_decisions.json"

    def giro(self, cartella: Path, shortlists: list[dict[str, Any]], descrizione: str):
        """Un giro intero: `valuta_shortlist` scrive, `merge` ricalcola."""
        codice, decisioni, _rapporto, _schermo = esegui(
            cartella, shortlists, trasporto_finto=trasporto({descrizione: contenuto("ACCEPT", 441)})
        )
        self.assertEqual(codice, USCITA_OK)
        self.assertEqual(len(decisioni), 1)
        return self.merge(cartella, shortlists, cartella / "ai_decisions.json", 1)

    def test_il_punteggio_scritto_come_testo_non_rompe_il_confronto(self) -> None:
        """`voce()` dice già che i listini veri portano il prezzo come stringa.
        Il giorno che un adattatore fa lo stesso col punteggio, i due che
        calcolano l'impronta leggono `0.81` e `"0.81"`: è per questo che
        `_confrontabile` esiste, e nessuna prova ce lo faceva passare."""
        una = voce()
        una["candidates"][0]["score"] = "0.81"
        una["candidates"][1]["score"] = "0.42"
        with tempfile.TemporaryDirectory() as temporanea:
            esito, risolti = self.giro(Path(temporanea), [una], "PANTERA SHAMPOO 250ML RICCI NEW")
        self.assertEqual(esito.returncode, 0, esito.stdout + esito.stderr)
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "SEMANTICO_PROPOSTO")

    def test_gli_spazi_e_le_accentate_non_rompono_il_confronto(self) -> None:
        """`test_le_descrizioni_non_si_normalizzano` prova che di qua non si
        tocca niente; questo prova che di là nemmeno. Sono le due metà della
        stessa regola, e ce n'era una sola."""
        descrizione = "  CAFFÈ  MISCELA  PERÙ  250G  "
        with tempfile.TemporaryDirectory() as temporanea:
            esito, risolti = self.giro(Path(temporanea), [voce(descrizione=descrizione)], descrizione)
        self.assertEqual(esito.returncode, 0, esito.stdout + esito.stderr)
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "SEMANTICO_PROPOSTO")

    def test_la_riga_scritta_come_testo_non_rompe_il_confronto(self) -> None:
        """`numero_di_riga` accetta `"12"`, ma nel merge `shortlist_index` e la
        chiave del ciclo restavano com'erano nel file mentre le decisioni
        venivano normalizzate a intero: la coppia non si legava più, ogni
        decisione diventava «senza riscontro» ed usciva 3 — cioè l'orchestratore
        mandava a ripagare la fase AI su una run sana."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, risolti = self.giro(
                Path(temporanea), [{**voce(), "gestionale_source_row": "12"}],
                "PANTERA SHAMPOO 250ML RICCI NEW",
            )
        self.assertEqual(esito.returncode, 0, esito.stdout + esito.stderr)
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "SEMANTICO_PROPOSTO")

    def test_una_coppia_semantica_senza_shortlist_non_passa_per_valutata(self) -> None:
        """Il secondo dei due rilievi bloccanti. Con 500 shortlist su 948
        coppie, la fase AI ne valuta 500, ne dichiara 500, il merge ne trova
        500 e tutto riconcilia — perché le due parti contano la stessa cosa
        mancante. Quattrocentoquarantotto prodotti sparivano con esito 0."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = [voce()]
            percorso = self.decidi(cartella, shortlists)
            # Il matching dichiara due coppie semantiche, le shortlist una.
            esito, risolti = self.merge(
                cartella, shortlists, percorso, 1,
                matching_in_piu=[{
                    "gestionale": {"source_row": 99, "description": "MAI VALUTATO", "ean": "8009"},
                    "suppliers": {"betulla": {"status": "EAN_ASSENTE", "usable_candidates": []}},
                }],
            )
        self.assertEqual(esito.returncode, 3, esito.stdout + esito.stderr)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["coppie_senza_shortlist"], 1)
        self.assertEqual(riepilogo["senza_shortlist"], [{"gestionale_source_row": 99, "supplier": "betulla"}])
        mancante = [r for r in risolti if r["gestionale"]["source_row"] == 99][0]["suppliers"]["betulla"]
        self.assertEqual(mancante["status"], "DA_VERIFICARE")
        self.assertIn("Nessuna shortlist", mancante["rationale"])

    def test_la_decisione_della_run_corrente_entra_nel_confronto(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = [voce()]
            percorso = self.decidi(cartella, shortlists)
            esito, risolti = self.merge(cartella, shortlists, percorso, 1)
        self.assertEqual(esito.returncode, 0, esito.stdout + esito.stderr)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_perche_di_un_altra_run"], 0)
        match = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(match["status"], "SEMANTICO_PROPOSTO")
        self.assertEqual(match["selected"]["source_row"], 441)

    def test_la_stessa_decisione_su_una_shortlist_di_un_altra_run_viene_buttata(self) -> None:
        """Il gestionale e' lo stesso file di settimana in settimana: la coppia
        combacia, i conteggi riconciliano e tutte le altre guardie confrontano
        fra loro artefatti di oggi. Senza l'impronta questa decisione entrerebbe
        in ordine con confidenza `ALTA` e senza conferma."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            percorso = self.decidi(cartella, [voce()])
            # Stessa coppia, stessa riga 441 nel listino: cambia solo che cosa
            # il modello avrebbe visto, cioe' i candidati della shortlist.
            di_oggi = [voce(candidati=((441, "PANTERA SH.250 RICCI", 0.81), (777, "DASY LAVATRICE 30 LAV", 0.55)))]
            esito, risolti = self.merge(cartella, di_oggi, percorso, 1)
        self.assertEqual(esito.returncode, 3, esito.stdout + esito.stderr)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_perche_di_un_altra_run"], 1)
        match = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(match["status"], "DA_VERIFICARE")
        self.assertEqual(match["ai_decisione_scartata"], "DECISIONE_DI_UNA_ALTRA_RUN")
        self.assertIsNone(match["selected"])


if __name__ == "__main__":
    unittest.main()
