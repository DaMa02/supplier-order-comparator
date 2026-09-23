"""Il client AI provato senza toccare la rete, un caso alla volta.

Il trasporto HTTP e' iniettato dal costruttore: qui dentro e' sempre un copione
di risposte scritte a mano. Nessun test apre una presa di rete, nessuno legge
`app/data/secrets.json`: la chiave la passano i test, ed e' finta.

Il test che conta piu' di tutti e' `test_02_finish_reason_length_e_troncata`.
L'11 agosto 2026, con `max_tokens` a 400, 11 risposte su 30 tornavano con
`finish_reason: "length"`, `content` vuoto e nessun errore HTTP: il modello
aveva speso tutto il tetto a ragionare. Letta come «nessun candidato va bene»,
quella risposta fa sparire un prodotto dal confronto senza dire niente. Se quel
test sparisce, il difetto torna.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from dataclasses import asdict
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import ai_client  # noqa: E402
from ai_client import Candidato, CasoValutazione, ClientAI  # noqa: E402


CHIAVE_FINTA = "chiave-di-prova-non-vera"

# Le sette chiavi di references/ai-decision-format.md, quelle che
# scripts/merge_match_decisions.py si aspetta di leggere.
CHIAVI_DECISIONE = {
    "gestionale_source_row",
    "supplier",
    "action",
    "source_row",
    "confidence",
    "rationale",
    "requires_user_confirmation",
}


def caso(
    *,
    riga: int = 12,
    fornitore: str = "betulla",
    descrizione: str = "BAGNOSCHIUMA VIDOR 500 ML TALCO",
    candidati: tuple[tuple[int, str, float], ...] = (
        (441, "VIDOR BAGNO TALCO 500 ML", 0.87),
        (512, "VIDOR BAGNO 500 ML MUSCHIO BIANCO", 0.62),
    ),
) -> CasoValutazione:
    return CasoValutazione(
        gestionale_source_row=riga,
        supplier=fornitore,
        descrizione=descrizione,
        candidati=tuple(
            Candidato(source_row=r, description=d, score=s) for r, d, s in candidati
        ),
    )


def configurazione(**modifiche) -> dict:
    conf = dict(ai_client.CONFIGURAZIONE_PREDEFINITA)
    conf.update(modifiche)
    return conf


def risposta(
    contenuto,
    *,
    finish_reason: str = "stop",
    costo: float = 0.0001,
    fornitore_calcolo: str = "DeepInfra",
    token_ingresso: int = 210,
    token_uscita: int = 48,
) -> tuple[int, dict]:
    """Una risposta 200 come quelle vere di OpenRouter."""

    return 200, {
        "id": "gen-prova",
        "model": "deepseek/deepseek-v4-flash",
        "provider": fornitore_calcolo,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": contenuto},
            }
        ],
        "usage": {
            "prompt_tokens": token_ingresso,
            "completion_tokens": token_uscita,
            "cost": costo,
        },
    }


def decisione_del_modello(
    azione: str = "ACCEPT",
    source_row=441,
    confidenza: str = "ALTA",
    motivo: str = "Marca, formato e variante coincidono.",
) -> str:
    return json.dumps(
        {"azione": azione, "source_row": source_row, "confidenza": confidenza, "motivo": motivo},
        ensure_ascii=False,
    )


class TrasportoFinto:
    """Risponde da un copione e conta le chiamate. Non tocca la rete."""

    def __init__(self, copione) -> None:
        self.copione = list(copione)
        self.chiamate: list[dict] = []

    def __call__(self, url, corpo, intestazioni, timeout):
        self.chiamate.append(
            {"url": url, "corpo": corpo, "intestazioni": intestazioni, "timeout": timeout}
        )
        if not self.copione:
            raise AssertionError("Il trasporto finto ha finito le risposte del copione.")
        prossima = self.copione.pop(0)
        if isinstance(prossima, BaseException):
            raise prossima
        return prossima

    @property
    def quante(self) -> int:
        return len(self.chiamate)


class ProvaClientAI(unittest.TestCase):
    """I diciotto casi obbligatori del contratto, piu' qualche presidio."""

    # ------------------------------------------------------------------ 1
    def test_01_risposta_valida_da_una_decisione_nel_formato_del_consumatore(self):
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(set(esito.decisione), CHIAVI_DECISIONE)
        self.assertEqual(esito.decisione["gestionale_source_row"], 12)
        self.assertEqual(esito.decisione["supplier"], "betulla")
        self.assertEqual(esito.decisione["action"], "ACCEPT")
        self.assertEqual(esito.decisione["source_row"], 441)
        self.assertEqual(esito.decisione["confidence"], "ALTA")
        self.assertEqual(esito.decisione["rationale"], "Marca, formato e variante coincidono.")
        self.assertIs(esito.decisione["requires_user_confirmation"], True)
        self.assertEqual(esito.tentativi, 1)
        self.assertEqual(esito.fornitore_calcolo, "DeepInfra")
        self.assertEqual(esito.token_ingresso, 210)
        self.assertEqual(esito.token_uscita, 48)
        self.assertAlmostEqual(esito.costo_usd, 0.0001)

    # ------------------------------------------------------------------ 2
    def test_02_finish_reason_length_e_troncata(self):
        """La trappola misurata: tetto dei token finito a ragionare, non un rifiuto."""

        troncata = risposta("", finish_reason="length", token_uscita=400)
        trasporto = TrasportoFinto([troncata, troncata])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "TRONCATA")
        self.assertIsNone(esito.decisione)
        # E soprattutto: non e' diventata un rifiuto.
        self.assertNotIn("REJECT", json.dumps(asdict(esito), ensure_ascii=False))
        self.assertEqual(trasporto.quante, 2, "una troncatura si ritenta una volta")

    # ------------------------------------------------------------------ 3
    def test_03_contenuto_vuoto_con_finish_reason_stop(self):
        vuota = risposta("   ", finish_reason="stop")
        trasporto = TrasportoFinto([vuota, vuota])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "CONTENUTO_VUOTO")
        self.assertIsNone(esito.decisione)

    # ------------------------------------------------------------------ 4
    def test_04_prosa_al_posto_del_json(self):
        prosa = risposta("Direi che il primo candidato è lo stesso prodotto.")
        trasporto = TrasportoFinto([prosa, prosa])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "NON_E_JSON")
        self.assertIsNone(esito.decisione)

    # ------------------------------------------------------------------ 5
    def test_05_azione_fuori_dall_insieme(self):
        fuori = risposta(decisione_del_modello(azione="FORSE"))
        trasporto = TrasportoFinto([fuori, fuori])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "SCHEMA_NON_CONFORME")
        self.assertIsNone(esito.decisione)
        self.assertIn("FORSE", esito.dettaglio)

    # ------------------------------------------------------------------ 6
    def test_06_accept_su_una_riga_che_non_era_fra_i_candidati(self):
        trasporto = TrasportoFinto([risposta(decisione_del_modello(source_row=999))])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "RIGA_FUORI_SHORTLIST")
        self.assertIsNone(esito.decisione)
        self.assertEqual(trasporto.quante, 1, "una riga inventata non si ritenta")

    # ------------------------------------------------------------------ 7
    def test_07_reject_con_source_row_valorizzata_viene_azzerata(self):
        trasporto = TrasportoFinto(
            [risposta(decisione_del_modello(azione="REJECT", source_row=441, confidenza="MEDIA"))]
        )
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(esito.decisione["action"], "REJECT")
        self.assertIsNone(esito.decisione["source_row"])

    # ------------------------------------------------------------------ 8
    def test_08_primo_tentativo_troncato_secondo_valido(self):
        trasporto = TrasportoFinto(
            [
                risposta("", finish_reason="length"),
                risposta(decisione_del_modello()),
            ]
        )
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(esito.tentativi, 2)
        self.assertEqual(esito.decisione["source_row"], 441)
        self.assertEqual(trasporto.quante, 2)

    # ------------------------------------------------------------------ 9
    def test_09_http_400_non_si_ripete(self):
        corpo = {"error": {"message": "deepseek/inesistente is not a valid model ID", "code": 400}}
        trasporto = TrasportoFinto([(400, corpo), (400, corpo)])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "HTTP_400")
        self.assertIsNone(esito.decisione)
        self.assertEqual(trasporto.quante, 1, "un model id sbagliato non si aggiusta ripetendo")
        self.assertIn("valid model ID", esito.dettaglio)

    # ------------------------------------------------------------------ 10
    def test_10_http_429_si_ripete_una_volta_sola(self):
        corpo = {"error": {"message": "Rate limit exceeded", "code": 429}}
        trasporto = TrasportoFinto([(429, corpo), (429, corpo), (429, corpo)])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        with mock.patch.object(ai_client, "PAUSA_RIPETIZIONE_S", 0.0):
            esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "HTTP_429")
        self.assertEqual(esito.tentativi, 2)
        self.assertEqual(trasporto.quante, 2, "un solo tentativo di ripetizione, non tre")

    # ------------------------------------------------------------------ 11
    def test_11_errore_di_rete_non_diventa_un_eccezione(self):
        guasto = urllib.error.URLError("[Errno 11001] getaddrinfo failed")
        trasporto = TrasportoFinto([guasto, guasto])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        with mock.patch.object(ai_client, "PAUSA_RIPETIZIONE_S", 0.0):
            esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "ERRORE_RETE")
        self.assertIsNone(esito.decisione)
        self.assertEqual(esito.tentativi, 2)

    # ------------------------------------------------------------------ 12
    def test_12_senza_chiave_non_si_chiama(self):
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave="")

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "SENZA_CHIAVE")
        self.assertIsNone(esito.decisione)
        self.assertEqual(trasporto.quante, 0, "senza chiave non si chiama nessuno")
        self.assertEqual(client.contabilita["chiamate"], 0)

    # ------------------------------------------------------------------ 13
    def test_13_tetto_di_spesa_superato_non_si_chiama(self):
        trasporto = TrasportoFinto(
            [risposta(decisione_del_modello(), costo=0.5), risposta(decisione_del_modello())]
        )
        client = ClientAI(
            configurazione(tetto_spesa_usd=0.4), trasporto=trasporto, chiave=CHIAVE_FINTA
        )

        primo = client.valuta_candidati(caso(riga=1))
        secondo = client.valuta_candidati(caso(riga=2))

        self.assertEqual(primo.stato, "OK")
        self.assertEqual(secondo.stato, "TETTO_SPESA")
        self.assertIsNone(secondo.decisione)
        self.assertEqual(trasporto.quante, 1, "oltre il tetto non parte nessuna chiamata")
        self.assertAlmostEqual(client.contabilita["costo_usd"], 0.5)

    # ------------------------------------------------------------------ 14
    def test_14_tetto_di_chiamate_superato(self):
        trasporto = TrasportoFinto(
            [risposta(decisione_del_modello()), risposta(decisione_del_modello())]
        )
        client = ClientAI(
            configurazione(tetto_chiamate=1), trasporto=trasporto, chiave=CHIAVE_FINTA
        )

        primo = client.valuta_candidati(caso(riga=1))
        secondo = client.valuta_candidati(caso(riga=2))

        self.assertEqual(primo.stato, "OK")
        self.assertEqual(secondo.stato, "TETTO_CHIAMATE")
        self.assertIsNone(secondo.decisione)
        self.assertEqual(trasporto.quante, 1)

    # ------------------------------------------------------------------ 15
    def test_15_un_caso_gia_visto_non_si_paga_due_volte(self):
        trasporto = TrasportoFinto([risposta(decisione_del_modello(), costo=0.0002)])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        primo = client.valuta_candidati(caso())
        secondo = client.valuta_candidati(caso())

        self.assertEqual(primo.stato, "OK")
        self.assertEqual(secondo.stato, "DALLA_MEMORIA")
        self.assertEqual(secondo.costo_usd, 0.0)
        self.assertEqual(secondo.decisione, primo.decisione)
        self.assertEqual(trasporto.quante, 1, "il secondo caso identico non chiama")
        self.assertEqual(client.contabilita["dalla_memoria"], 1)

    # ------------------------------------------------------------------ 16
    def test_16_cambiare_versione_del_prompt_cambia_la_chiave_della_memoria(self):
        cartella = Path(tempfile.mkdtemp(prefix="prova_prompt_"))
        self.addCleanup(_ripulisci, cartella)
        (cartella / "valuta_candidati.v1.md").write_bytes(b"prompt uno\n")
        (cartella / "valuta_candidati.v2.md").write_bytes(b"prompt due, diverso\n")
        memoria = cartella / "memoria_ai.json"

        chiave_v1 = ai_client.chiave_memoria("modello/x", "v1", ai_client.VERSIONE_SCHEMA, caso())
        chiave_v2 = ai_client.chiave_memoria("modello/x", "v2", ai_client.VERSIONE_SCHEMA, caso())
        self.assertNotEqual(chiave_v1, chiave_v2)

        with mock.patch.object(ai_client, "CARTELLA_PROMPT", cartella):
            primo_trasporto = TrasportoFinto([risposta(decisione_del_modello())])
            primo = ClientAI(
                configurazione(versione_prompt="v1"),
                trasporto=primo_trasporto,
                memoria=memoria,
                chiave=CHIAVE_FINTA,
            )
            self.assertEqual(primo.valuta_candidati(caso()).stato, "OK")

            secondo_trasporto = TrasportoFinto([risposta(decisione_del_modello())])
            secondo = ClientAI(
                configurazione(versione_prompt="v2"),
                trasporto=secondo_trasporto,
                memoria=memoria,
                chiave=CHIAVE_FINTA,
            )
            esito = secondo.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(
            secondo_trasporto.quante,
            1,
            "col prompt cambiato la risposta vecchia non vale più: si richiede",
        )

    # ------------------------------------------------------------------ 17
    def test_17_valuta_molti_torna_i_risultati_nell_ordine_dei_casi(self):
        casi = [
            caso(riga=i, descrizione=f"ARTICOLO {i}", candidati=((100 + i, f"CANDIDATO {i}", 0.7),))
            for i in range(6)
        ]

        class TrasportoSfalsato:
            """Risponde in tempi diversi, apposta: il primo caso e' il piu' lento."""

            def __init__(self) -> None:
                self.lucchetto = threading.Lock()
                self.quante = 0

            def __call__(self, url, corpo, intestazioni, timeout):
                testo = corpo["messages"][1]["content"]
                riga = int(testo.split("source_row ")[1].split(":")[0])
                indice = riga - 100
                with self.lucchetto:
                    self.quante += 1
                time.sleep(0.02 * (len(casi) - indice))
                return risposta(decisione_del_modello(source_row=riga))

        trasporto = TrasportoSfalsato()
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esiti = client.valuta_molti(casi)

        self.assertEqual(len(esiti), len(casi))
        self.assertEqual(trasporto.quante, len(casi))
        self.assertEqual(
            [esito.decisione["source_row"] for esito in esiti],
            [100 + i for i in range(6)],
        )
        self.assertEqual(
            [esito.decisione["gestionale_source_row"] for esito in esiti], list(range(6))
        )

    # ------------------------------------------------------------------ 18
    def test_18_la_chiave_non_esce_mai_dal_client(self):
        segreta = "chiave-segretissima-di-daniele-0123456789"
        # Il 401 riecheggia l'header, e lo fa proprio a cavallo dei 300 caratteri
        # a cui il messaggio viene accorciato: se si accorciasse prima di
        # nascondere la chiave, ne resterebbe fuori il primo pezzo.
        riempimento = "controlla le credenziali del servizio; " * 6
        corpo = {
            "error": {
                "message": f"No auth credentials found. {riempimento}Authorization: Bearer {segreta}",
                "code": 401,
            }
        }
        trasporto = TrasportoFinto([(401, corpo)])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=segreta)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "HTTP_401")
        self.assertEqual(trasporto.quante, 1, "una chiave non valida non si aggiusta ripetendo")
        tutto = json.dumps(asdict(esito), ensure_ascii=False) + repr(esito)
        self.assertNotIn(segreta, tutto)
        self.assertNotIn(segreta, esito.dettaglio)
        # Nemmeno un pezzo di chiave: accorciare prima di nascondere ne lascerebbe
        # fuori l'inizio, e mezza chiave è comunque una chiave uscita.
        pezzi = {segreta[i:i + 12] for i in range(len(segreta) - 11)}
        self.assertEqual(sorted(pezzo for pezzo in pezzi if pezzo in tutto), [])
        self.assertIn(ai_client.NASCOSTO, esito.dettaglio)
        self.assertNotIn(segreta, json.dumps(client.contabilita, ensure_ascii=False))

    # -------------------------------------------------------- presidi in più

    def test_19_la_chiave_sta_nel_campo_annidato_di_secrets(self):
        """Cercarla al primo livello dà None e poi 401: trappola già pagata."""

        cartella = Path(tempfile.mkdtemp(prefix="prova_secrets_"))
        self.addCleanup(_ripulisci, cartella)
        percorso = cartella / "secrets.json"
        percorso.write_bytes(
            json.dumps({"openrouter": {"api_key": "chiave-annidata"}}).encode("utf-8")
        )

        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("OPENROUTER_API_KEY", None)
            self.assertEqual(ai_client.leggi_chiave(percorso), "chiave-annidata")

            piatto = cartella / "piatto.json"
            piatto.write_bytes(json.dumps({"api_key": "chiave-al-primo-livello"}).encode("utf-8"))
            self.assertIsNone(ai_client.leggi_chiave(piatto))

            self.assertIsNone(ai_client.leggi_chiave(cartella / "che-non-esiste.json"))

    def test_20_la_configurazione_ha_la_precedenza_dichiarata(self):
        cartella = Path(tempfile.mkdtemp(prefix="prova_impostazioni_"))
        self.addCleanup(_ripulisci, cartella)
        mancante = cartella / "impostazioni_ai.json"

        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("OPENROUTER_MODEL", None)
            # Il file può non esistere: è il caso normale oggi, non un errore.
            predefinita = ai_client.carica_configurazione(mancante)
            self.assertEqual(predefinita, dict(ai_client.CONFIGURAZIONE_PREDEFINITA))

            mancante.write_bytes(
                json.dumps({"model": "dal/file", "max_tokens": 900, "ignoto": 1}).encode("utf-8")
            )
            dal_file = ai_client.carica_configurazione(mancante)
            self.assertEqual(dal_file["model"], "dal/file")
            self.assertEqual(dal_file["max_tokens"], 900)
            self.assertNotIn("ignoto", dal_file)

            os.environ["OPENROUTER_MODEL"] = "dall/ambiente"
            self.assertEqual(ai_client.carica_configurazione(mancante)["model"], "dall/ambiente")

    def test_21_un_caso_senza_candidati_e_un_errore_d_uso(self):
        trasporto = TrasportoFinto([])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)
        vuoto = CasoValutazione(
            gestionale_source_row=7, supplier="betulla", descrizione="X", candidati=()
        )

        with self.assertRaises(ValueError):
            client.valuta_candidati(vuoto)
        with self.assertRaises(ValueError):
            client.valuta_molti([caso(), vuoto])
        self.assertEqual(trasporto.quante, 0, "i casi si controllano prima di partire")

    def test_22_il_modello_vede_solo_descrizione_riga_e_punteggio(self):
        """Mai l'EAN (è la verità del banco di prova) e mai il prezzo."""

        testo = ai_client.caso_come_testo(caso())

        self.assertEqual(
            testo,
            "Articolo cercato: BAGNOSCHIUMA VIDOR 500 ML TALCO\n"
            "\n"
            "Candidati del fornitore:\n"
            "- source_row 441: VIDOR BAGNO TALCO 500 ML (punteggio 0.87)\n"
            "- source_row 512: VIDOR BAGNO 500 ML MUSCHIO BIANCO (punteggio 0.62)",
        )

    def test_23_il_prompt_v1_e_quello_misurato_alla_lettera(self):
        """Il prompt si cambia misurando (5b), non riscrivendolo a occhio."""

        atteso = (
            "Sei un buyer esperto di prodotti per la casa e la persona.\n"
            "Devi dire se uno dei candidati del listino di un fornitore e' lo stesso identico\n"
            "prodotto dell'articolo cercato.\n"
            "\n"
            "Come si ragiona:\n"
            "- descrizioni scritte in ordine diverso o abbreviate sono lo stesso prodotto: e'\n"
            "  normale che i fornitori le scrivano a modo loro;\n"
            "- marca, formato (ml, g, pezzi, rotoli, lavaggi), moltiplicatore e variante\n"
            "  (profumo, colore, tipo) devono coincidere: se uno di questi differisce non e'\n"
            "  lo stesso prodotto;\n"
            "- una parola in piu' che descrive il materiale o la linea non cambia il prodotto;\n"
            "- se due candidati sono ugualmente plausibili, non scegliere: rispondi UNRESOLVED.\n"
            "\n"
            "Rispondi ACCEPT con la source_row del candidato solo se sei convinto che sia lo\n"
            "stesso articolo; REJECT se nessuno lo e'; UNRESOLVED se non riesci a decidere.\n"
            "La confidenza e' ALTA solo quando non hai dubbi.\n"
            "Il campo motivo e' una riga sola, meno di venti parole."
        )

        self.assertEqual(ai_client.leggi_prompt("v1"), atteso)

        # Il prompt che parte davvero e' quello della versione configurata, e il
        # predefinito non e' piu' v1: la 5b l'ha misurato e sostituito. Qui si
        # verifica che il client mandi il file della versione che dichiara, non
        # un testo scritto dentro il codice.
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])
        client = ClientAI(configurazione(versione_prompt="v1"), trasporto=trasporto, chiave=CHIAVE_FINTA)
        client.valuta_candidati(caso())
        inviato = trasporto.chiamate[0]["corpo"]
        self.assertEqual(inviato["messages"][0]["content"], atteso)
        self.assertEqual(inviato["max_tokens"], 1500)
        self.assertNotIn("provider", inviato, "il fornitore di calcolo lo sceglie OpenRouter")


    def test_24_prova_connessione_chiama_davvero_e_non_usa_la_memoria(self):
        """Serve a scoprire una chiave o un model id sbagliati: se leggesse dalla
        memoria non proverebbe niente."""

        trasporto = TrasportoFinto(
            [risposta(decisione_del_modello(source_row=1)), risposta(decisione_del_modello(source_row=1))]
        )
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        primo = client.prova_connessione()
        secondo = client.prova_connessione()

        self.assertEqual(primo.stato, "OK")
        self.assertEqual(secondo.stato, "OK")
        self.assertEqual(trasporto.quante, 2)
        self.assertEqual(client.contabilita["dalla_memoria"], 0)

        senza_chiave = ClientAI(configurazione(), trasporto=TrasportoFinto([]), chiave="")
        self.assertEqual(senza_chiave.prova_connessione().stato, "SENZA_CHIAVE")


# ============================================================================
# I presidi delle correzioni della Fase 5a.
#
# Le tre revisioni della 5a hanno trovato ventinove difetti con la suite verde:
# la suite verde, da sola, non li vedeva. Quello che segue difende le
# correzioni, una per una, perche' la riscrittura che le togliesse debba far
# diventare rosso qualcosa.
# ============================================================================


def _scrivi_memoria(percorso: Path, voci: dict) -> None:
    """Un file di memoria scritto a mano, come se lo avesse lasciato una run."""

    percorso.write_bytes(
        json.dumps({"versione": 1, "voci": voci}, ensure_ascii=False, indent=2).encode("utf-8")
    )


def _voci_sul_disco(percorso: Path) -> dict:
    return json.loads(percorso.read_bytes().decode("utf-8"))["voci"]


def chiave_di(caso_dato: CasoValutazione, *, versione_prompt: str | None = None) -> str:
    """La chiave con cui il client indirizza quel caso, col modello predefinito.

    La versione del prompt segue il predefinito: cablarla qui vorrebbe dire che
    cambiando prompt questi test smettono di parlare della memoria vera. E
    porta l'impronta del **testo** del prompt, come fa il client: sigillare la
    sola etichetta lasciava valide le risposte di un prompt riscritto."""

    versione = versione_prompt or ai_client.CONFIGURAZIONE_PREDEFINITA["versione_prompt"]
    return ai_client.chiave_memoria(
        ai_client.CONFIGURAZIONE_PREDEFINITA["model"],
        f"{versione}:{ai_client.impronta_prompt(ai_client.leggi_prompt(versione))}",
        ai_client.VERSIONE_SCHEMA,
        caso_dato,
    )


def voce_di_memoria(decisione) -> dict:
    return {
        "decisione": decisione,
        "modello": "deepseek/deepseek-v4-flash",
        "fornitore_calcolo": "DeepInfra",
        "salvato_il": "2026-08-11T09:00:00",
    }


def decisione_salvata(caso_dato: CasoValutazione, **modifiche) -> dict:
    """Una decisione nel formato del consumatore, come sta scritta in memoria."""

    decisione = {
        "gestionale_source_row": caso_dato.gestionale_source_row,
        "supplier": caso_dato.supplier,
        "action": "ACCEPT",
        "source_row": caso_dato.candidati[0].source_row,
        "confidence": "ALTA",
        "rationale": "ripresa dalla memoria",
        "requires_user_confirmation": True,
    }
    decisione.update(modifiche)
    return decisione


class TrasportoVietato:
    """Solleva se lo si chiama: serve a provare che non lo si chiama."""

    def __init__(self) -> None:
        self.quante = 0

    def __call__(self, url, corpo, intestazioni, timeout):
        self.quante += 1
        raise AssertionError("questo caso non doveva costare una chiamata")


class TrasportoLento:
    """Conta sotto lucchetto e ci mette qualche millisecondo: apposta, per i thread."""

    def __init__(self, *, costo: float = 0.0, pausa: float = 0.005) -> None:
        self._lucchetto = threading.Lock()
        self._costo = costo
        self._pausa = pausa
        self.quante = 0

    def __call__(self, url, corpo, intestazioni, timeout):
        with self._lucchetto:
            self.quante += 1
        time.sleep(self._pausa)
        return risposta(decisione_del_modello(), costo=self._costo)


class ConCartellaTemporanea(unittest.TestCase):
    def cartella(self, prefisso: str = "prova_memoria_ai_") -> Path:
        cartella = Path(tempfile.mkdtemp(prefix=prefisso))
        self.addCleanup(_ripulisci, cartella)
        return cartella


class ProvaMemoriaDelClientAI(ConCartellaTemporanea):
    """La memoria vive in app/data/, fuori dal controllo di versione: e' un file
    che si puo' modificare a mano, e da cui non deve poter uscire una decisione
    che il client non avrebbe mai prodotto."""

    def test_48_una_voce_con_decisione_che_non_e_un_oggetto_non_diventa_un_ricordo(self):
        """Il primo dei due strati che difendono la memoria.

        Il secondo — `decisione_non_utilizzabile` — e' provato da test_25, 26 e
        27. Dalla superficie pubblica i due strati convergono: tolto il primo,
        il secondo ferma comunque la voce e non si vede nessuna differenza. Per
        questo il test guarda un metodo privato: e' l'unico modo di accorgersi
        se qualcuno domani semplifica «tanto valida chi chiama» e la difesa in
        profondita' diventa difesa singola senza che nessuno l'abbia deciso.
        """

        percorso = self.cartella() / "memoria_ai.json"
        chiave = chiave_di(caso())
        for forma in ("ACCEPT 441", 441, ["ACCEPT", 441], True, None):
            with self.subTest(forma=forma):
                _scrivi_memoria(percorso, {chiave: voce_di_memoria(forma)})
                client = ClientAI(
                    configurazione(),
                    trasporto=TrasportoVietato(),
                    memoria=percorso,
                    chiave=CHIAVE_FINTA,
                )

                self.assertIsNone(client._leggi_ricordo(chiave))

    def test_25_una_voce_con_decisione_che_non_e_un_oggetto_non_fa_sollevare(self):
        for forma in ("ACCEPT 441", 441, ["ACCEPT", 441], True):
            with self.subTest(forma=forma):
                percorso = self.cartella() / "memoria_ai.json"
                _scrivi_memoria(percorso, {chiave_di(caso()): voce_di_memoria(forma)})
                trasporto = TrasportoFinto([risposta(decisione_del_modello())])
                client = ClientAI(
                    configurazione(), trasporto=trasporto, memoria=percorso, chiave=CHIAVE_FINTA
                )

                esito = client.valuta_candidati(caso())

                self.assertEqual(esito.stato, "OK")
                self.assertEqual(esito.decisione["source_row"], 441)
                self.assertEqual(trasporto.quante, 1, "il caso si richiede al modello")
                self.assertEqual(client.contabilita["dalla_memoria"], 0)

    def test_26_una_voce_manomessa_che_accetta_una_riga_fuori_shortlist_viene_scartata(self):
        """La decisione piu' pericolosa che questo progetto conosca."""

        percorso = self.cartella() / "memoria_ai.json"
        manomessa = decisione_salvata(caso(), source_row=999)
        _scrivi_memoria(percorso, {chiave_di(caso()): voce_di_memoria(manomessa)})
        trasporto = TrasportoFinto([risposta(decisione_del_modello(source_row=441))])
        client = ClientAI(
            configurazione(), trasporto=trasporto, memoria=percorso, chiave=CHIAVE_FINTA
        )

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK", "non DALLA_MEMORIA: la voce è stata buttata")
        self.assertEqual(esito.decisione["source_row"], 441)
        self.assertEqual(esito.decisione["rationale"], "Marca, formato e variante coincidono.")
        self.assertEqual(trasporto.quante, 1)
        self.assertEqual(client.contabilita["dalla_memoria"], 0)

    def test_27_una_voce_con_chiavi_diverse_dalle_sette_viene_scartata(self):
        percorso = self.cartella() / "memoria_ai.json"
        storta = decisione_salvata(caso())
        del storta["requires_user_confirmation"]
        storta["confidenza"] = "ALTA"          # il nome italiano non è quello del formato
        _scrivi_memoria(percorso, {chiave_di(caso()): voce_di_memoria(storta)})
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])
        client = ClientAI(
            configurazione(), trasporto=trasporto, memoria=percorso, chiave=CHIAVE_FINTA
        )

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(set(esito.decisione), CHIAVI_DECISIONE)
        self.assertEqual(esito.decisione["rationale"], "Marca, formato e variante coincidono.")
        self.assertEqual(trasporto.quante, 1)
        self.assertEqual(client.contabilita["dalla_memoria"], 0)

    def test_28_la_memoria_su_disco_risparmia_la_chiamata_alla_run_successiva(self):
        """Il giro completo: un client scrive, quello dopo legge e non chiama."""

        percorso = self.cartella() / "memoria_ai.json"
        primo = ClientAI(
            configurazione(),
            trasporto=TrasportoFinto([risposta(decisione_del_modello(), costo=0.0002)]),
            memoria=percorso,
            chiave=CHIAVE_FINTA,
        )
        atteso = primo.valuta_candidati(caso())
        self.assertEqual(atteso.stato, "OK")
        self.assertTrue(percorso.exists(), "la memoria deve finire su disco")

        vietato = TrasportoVietato()
        secondo = ClientAI(
            configurazione(), trasporto=vietato, memoria=percorso, chiave=CHIAVE_FINTA
        )
        esito = secondo.valuta_candidati(caso())

        self.assertEqual(esito.stato, "DALLA_MEMORIA")
        self.assertEqual(esito.costo_usd, 0.0)
        self.assertEqual(esito.decisione, atteso.decisione)
        self.assertEqual(vietato.quante, 0)
        self.assertEqual(secondo.contabilita["chiamate"], 0)

    def test_29_un_fallimento_non_entra_in_memoria(self):
        """La run dopo deve poterlo ritentare: solo le risposte valide si ricordano."""

        percorso = self.cartella() / "memoria_ai.json"
        troncata = risposta("", finish_reason="length", token_uscita=400)
        trasporto = TrasportoFinto([troncata, troncata, troncata, troncata])
        client = ClientAI(
            configurazione(), trasporto=trasporto, memoria=percorso, chiave=CHIAVE_FINTA
        )

        primo = client.valuta_candidati(caso())
        secondo = client.valuta_candidati(caso())

        self.assertEqual(primo.stato, "TRONCATA")
        self.assertEqual(secondo.stato, "TRONCATA")
        # assertIsNone e non assertFalse: un {} passerebbe per «nessuna decisione»
        # e sarebbe invece una voce di memoria vuota entrata di soppiatto.
        self.assertIsNone(primo.decisione)
        self.assertIsNone(secondo.decisione)
        self.assertEqual(trasporto.quante, 4, "due tentativi per valutazione, due volte")
        self.assertFalse(percorso.exists(), "nessuna voce, quindi nemmeno il file")

    def test_30_una_memoria_troncata_non_impedisce_la_costruzione_del_client(self):
        percorso = self.cartella() / "memoria_ai.json"
        percorso.write_bytes(b'{\n  "versione": 1,\n  "voci": {\n    "abc": {\n      "decisi')
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])

        client = ClientAI(
            configurazione(), trasporto=trasporto, memoria=percorso, chiave=CHIAVE_FINTA
        )
        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(trasporto.quante, 1)

    def test_31_una_memoria_con_radice_inattesa_non_impedisce_la_costruzione_del_client(self):
        percorso = self.cartella() / "memoria_ai.json"
        percorso.write_bytes(b'[{"decisione": {"action": "ACCEPT"}}]')
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])

        client = ClientAI(
            configurazione(), trasporto=trasporto, memoria=percorso, chiave=CHIAVE_FINTA
        )
        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(trasporto.quante, 1)

    def test_32_due_client_sullo_stesso_file_non_si_cancellano_le_voci(self):
        percorso = self.cartella() / "memoria_ai.json"
        caso_uno = caso(riga=1, descrizione="ARTICOLO UNO")
        caso_due = caso(riga=2, descrizione="ARTICOLO DUE")

        # Tutti e due nascono sul file vuoto: il secondo non ha in RAM la voce
        # che il primo scrivera' fra un attimo.
        uno = ClientAI(
            configurazione(),
            trasporto=TrasportoFinto([risposta(decisione_del_modello())]),
            memoria=percorso,
            chiave=CHIAVE_FINTA,
        )
        due = ClientAI(
            configurazione(),
            trasporto=TrasportoFinto([risposta(decisione_del_modello())]),
            memoria=percorso,
            chiave=CHIAVE_FINTA,
        )

        self.assertEqual(uno.valuta_candidati(caso_uno).stato, "OK")
        self.assertEqual(due.valuta_candidati(caso_due).stato, "OK")

        voci = _voci_sul_disco(percorso)
        self.assertEqual(len(voci), 2, "la seconda scrittura non cancella la prima")
        self.assertEqual(set(voci), {chiave_di(caso_uno), chiave_di(caso_due)})

    def test_33_la_memoria_si_scrive_a_capo_unix_e_senza_lasciare_file_temporanei(self):
        """write_text su Windows scriverebbe CRLF, e il file finirebbe in diff."""

        cartella = self.cartella()
        percorso = cartella / "memoria_ai.json"
        client = ClientAI(
            configurazione(),
            trasporto=TrasportoFinto([risposta(decisione_del_modello())]),
            memoria=percorso,
            chiave=CHIAVE_FINTA,
        )

        self.assertEqual(client.valuta_candidati(caso()).stato, "OK")

        contenuto = percorso.read_bytes()
        self.assertNotIn(b"\r\n", contenuto)
        self.assertTrue(contenuto.endswith(b"\n"))
        self.assertEqual(sorted(p.name for p in cartella.glob("*.tmp")), [])
        self.assertEqual(len(_voci_sul_disco(percorso)), 1)


class TettoCheSiFaLeggereDaTuttiInsieme:
    """Un tetto che, mentre lo si legge, tiene fermo il thread finche' non sono
    arrivati tutti gli altri.

    Serve a provare la **mutua esclusione**, non il totale. Sotto CPython il
    totale non basta: fra il confronto `self._chiamate >= tetto` e
    `self._chiamate += 1` non c'e' nessuna chiamata ne' nessun salto
    all'indietro, quindi l'interprete non cede quasi mai il turno proprio li' e
    anche senza lucchetto i numeri tornano lo stesso. Misurato: con il lucchetto
    tolto, ottanta casi e tetto dieci danno esattamente dieci chiamate in 120
    esecuzioni su 120. Il lucchetto sparirebbe da una riscrittura senza che
    nessun test se ne accorga.

    Il tetto si legge **dentro** la sezione da proteggere: se il lucchetto c'e',
    nessun altro thread puo' entrarci e la porta scade da sola; se non c'e',
    entrano tutti insieme.
    """

    def __init__(self, valore: int, quanti: int, *, attesa: float = 0.5) -> None:
        self.valore = int(valore)
        self.porta = threading.Barrier(quanti, timeout=attesa)
        self._lucchetto = threading.Lock()
        self.letture = 0
        self.entrati_insieme = 0

    def __int__(self) -> int:
        with self._lucchetto:
            self.letture += 1
            prima = self.letture == 1  # la chiamata di riscaldamento, fatta da sola
        if not prima:
            try:
                self.porta.wait()
            except threading.BrokenBarrierError:
                return self.valore
            with self._lucchetto:
                self.entrati_insieme += 1
        return self.valore


class ProvaTettiSottoIThread(ConCartellaTemporanea):
    """Controllare e poi incrementare, con sedici thread, non e' controllare."""

    def test_47_la_prenotazione_di_una_chiamata_non_ammette_due_thread_insieme(self):
        """Il lucchetto, provato per quello che fa e non per il numero che produce."""

        operai = 8
        trasporto = TrasportoLento(costo=0.0, pausa=0.001)
        client = ClientAI(
            configurazione(tetto_chiamate=2, parallelismo=operai, tetto_spesa_usd=100.0),
            trasporto=trasporto,
            chiave=CHIAVE_FINTA,
        )
        tetto = TettoCheSiFaLeggereDaTuttiInsieme(2, operai)
        # Dopo la costruzione: la convalida della configurazione lo rifiuterebbe.
        client.configurazione["tetto_chiamate"] = tetto
        casi = [caso(riga=i, descrizione=f"ARTICOLO {i}") for i in range(1 + operai)]

        esiti = client.valuta_molti(casi)

        self.assertEqual(
            tetto.entrati_insieme,
            0,
            f"{tetto.entrati_insieme} thread dentro la prenotazione insieme: il controllo "
            "del tetto e l'incremento non stanno piu' nella stessa sezione protetta, e con "
            "N thread il tetto salta di N chiamate",
        )
        self.assertEqual(tetto.letture, 1 + operai, "il tetto si legge una volta per chiamata")
        self.assertEqual(trasporto.quante, 2, "due chiamate, non nove")
        self.assertEqual(client.contabilita["chiamate"], 2)
        self.assertEqual(sum(1 for esito in esiti if esito.stato == "TETTO_CHIAMATE"), operai - 1)
        self.assertEqual(len(esiti), 1 + operai)

    def test_34_il_tetto_delle_chiamate_regge_con_sedici_thread(self):
        casi = [caso(riga=i, descrizione=f"ARTICOLO {i}") for i in range(80)]
        trasporto = TrasportoLento(costo=0.0, pausa=0.005)
        client = ClientAI(
            configurazione(tetto_chiamate=10, parallelismo=16, tetto_spesa_usd=100.0),
            trasporto=trasporto,
            chiave=CHIAVE_FINTA,
        )

        esiti = client.valuta_molti(casi)

        self.assertEqual(trasporto.quante, 10, "dieci chiamate, non undici e non sedici")
        self.assertEqual(client.contabilita["chiamate"], 10)
        self.assertEqual(len(esiti), 80)
        self.assertEqual(sum(1 for esito in esiti if esito.stato == "OK"), 10)
        self.assertEqual(sum(1 for esito in esiti if esito.stato == "TETTO_CHIAMATE"), 70)

    def test_35_il_tetto_di_spesa_regge_con_dieci_thread(self):
        """La prima chiamata si fa da sola apposta: prima di averne vista una non
        si sa quanto costi, e la prenotazione della spesa lavora su una stima."""

        casi = [caso(riga=i, descrizione=f"ARTICOLO {i}") for i in range(10)]
        trasporto = TrasportoLento(costo=0.50, pausa=0.02)
        client = ClientAI(
            configurazione(tetto_spesa_usd=1.0, parallelismo=10, tetto_chiamate=1000),
            trasporto=trasporto,
            chiave=CHIAVE_FINTA,
        )

        esiti = client.valuta_molti(casi)

        speso = client.contabilita["costo_usd"]
        self.assertLessEqual(
            speso, 1.0 + 0.50, f"sforato il tetto di più di una chiamata: {speso} $"
        )
        self.assertEqual(len(esiti), 10)
        self.assertGreaterEqual(trasporto.quante, 1, "qualcosa deve pur partire")
        self.assertGreaterEqual(sum(1 for esito in esiti if esito.stato == "TETTO_SPESA"), 1)


class ProvaRisposteDeformi(ConCartellaTemporanea):
    """Una sola risposta deforme non deve poter far saltare l'infornata."""

    def test_36_un_usage_di_forma_sbagliata_non_fa_sollevare(self):
        for uso in ("prompt 210, completion 48", ["prompt_tokens", 210], 7):
            with self.subTest(usage=uso):
                codice, corpo = risposta(decisione_del_modello())
                corpo["usage"] = uso
                trasporto = TrasportoFinto([(codice, corpo)])
                client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

                esito = client.valuta_candidati(caso())

                self.assertEqual(esito.stato, "OK")
                self.assertEqual(esito.costo_usd, 0.0)
                self.assertEqual(esito.token_ingresso, 0)
                self.assertEqual(esito.token_uscita, 0)

    def test_37_un_errore_dichiarato_dentro_un_200_vale_come_il_codice_che_dichiara(self):
        corpo = {
            "id": "gen-prova",
            "model": "deepseek/inesistente",
            "error": {"code": 400, "message": "deepseek/inesistente is not a valid model ID"},
        }
        trasporto = TrasportoFinto([(200, corpo), (200, corpo)])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "HTTP_400")
        self.assertIsNone(esito.decisione)
        self.assertEqual(trasporto.quante, 1, "un model id sbagliato non si aggiusta ripetendo")
        self.assertIn("is not a valid model ID", esito.dettaglio)

    def test_38_un_429_dichiarato_dentro_un_200_si_ripete_una_volta_sola(self):
        corpo = {"id": "gen-prova", "error": {"code": 429, "message": "Rate limit exceeded"}}
        trasporto = TrasportoFinto([(200, corpo), (200, corpo), (200, corpo)])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        with mock.patch.object(ai_client, "PAUSA_RIPETIZIONE_S", 0.0):
            esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "HTTP_429")
        self.assertEqual(esito.tentativi, 2)
        self.assertEqual(trasporto.quante, 2, "una ripetizione sola, non tre")

    def test_39_un_errore_dentro_un_200_vince_sui_choices_pieni(self):
        """Se il servizio dichiara un guasto, l'esito è quel guasto: al massimo
        si paga una ripetizione, che è il verso giusto in cui sbagliare."""

        codice, corpo = risposta(decisione_del_modello())
        corpo["error"] = {"code": 400, "message": "Provider returned error"}
        trasporto = TrasportoFinto([(codice, corpo), (codice, corpo)])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "HTTP_400")
        self.assertIsNone(esito.decisione)
        self.assertEqual(trasporto.quante, 1)

    def test_40_un_guasto_di_rete_e_errore_rete_e_si_ripete_una_volta(self):
        for guasto in (TimeoutError("timed out"), ConnectionResetError("connessione azzerata")):
            with self.subTest(guasto=type(guasto).__name__):
                trasporto = TrasportoFinto([guasto, guasto])
                client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

                with mock.patch.object(ai_client, "PAUSA_RIPETIZIONE_S", 0.0):
                    esito = client.valuta_candidati(caso())

                self.assertEqual(esito.stato, "ERRORE_RETE")
                self.assertIsNone(esito.decisione)
                self.assertEqual(esito.tentativi, 2)
                self.assertEqual(trasporto.quante, 2)

    def test_41_un_difetto_del_trasporto_non_e_la_rete_e_non_si_ripete(self):
        """Ripetere un MemoryError o un KeyError è la cosa peggiore da fare."""

        for guasto in (KeyError("choices"), MemoryError(), AssertionError("copione finito")):
            with self.subTest(guasto=type(guasto).__name__):
                trasporto = TrasportoFinto([guasto, guasto])
                client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

                esito = client.valuta_candidati(caso())

                self.assertEqual(esito.stato, "ERRORE_TRASPORTO")
                self.assertIsNone(esito.decisione)
                self.assertEqual(esito.tentativi, 1)
                self.assertEqual(trasporto.quante, 1)
                per_stato = client.contabilita["per_stato"]
                self.assertEqual(per_stato.get("ERRORE_TRASPORTO"), 1)
                self.assertIsNone(per_stato.get("ERRORE_RETE"), "non si confonde con la rete")

    def test_42_il_costo_dell_esito_somma_i_tentativi(self):
        """Il tentativo troncato ha consumato e fatturato i token del ragionamento."""

        trasporto = TrasportoFinto(
            [
                risposta("", finish_reason="length", costo=0.0004),
                risposta(decisione_del_modello(), costo=0.0001),
            ]
        )
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(esito.tentativi, 2)
        self.assertAlmostEqual(esito.costo_usd, 0.0005, places=10)
        self.assertAlmostEqual(client.contabilita["costo_usd"], 0.0005, places=10)


class ProvaInfornata(ConCartellaTemporanea):
    """Un caso che fallisce non ferma gli altri, e non li fa nemmeno sparire."""

    def test_43_valuta_molti_torna_un_esito_per_caso_anche_con_esiti_misti(self):
        casi = [
            caso(riga=0, descrizione="CASO ZERO"),
            caso(riga=1, descrizione="CASO UNO"),
            caso(riga=2, descrizione="CASO DUE"),
        ]

        class TrasportoMisto:
            def __init__(self) -> None:
                self._lucchetto = threading.Lock()
                self.quante = 0

            def __call__(self, url, corpo, intestazioni, timeout):
                with self._lucchetto:
                    self.quante += 1
                testo = corpo["messages"][1]["content"]
                if "CASO ZERO" in testo:
                    return risposta(decisione_del_modello())
                if "CASO UNO" in testo:
                    return risposta("", finish_reason="length")
                raise TimeoutError("timed out")

        trasporto = TrasportoMisto()
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        with mock.patch.object(ai_client, "PAUSA_RIPETIZIONE_S", 0.0):
            esiti = client.valuta_molti(casi)

        self.assertEqual(len(esiti), len(casi))
        self.assertEqual([esito.stato for esito in esiti], ["OK", "TRONCATA", "ERRORE_RETE"])
        self.assertEqual(esiti[0].decisione["gestionale_source_row"], 0)
        self.assertIsNone(esiti[1].decisione)
        self.assertIsNone(esiti[2].decisione)
        self.assertEqual(trasporto.quante, 5, "una riuscita, più due ripetute")

    def test_43b_una_chiave_rifiutata_ferma_l_infornata_alla_prima(self):
        """⚠ La promessa era gia' scritta e non era mantenuta.

        Il commento del riscaldamento dice che «un modello sbagliato o una
        chiave scaduta si scoprono prima di lanciare 948 chiamate», ma
        `_prenota_chiamata` conta la chiamata PRIMA di farla: dopo la prima,
        anche fallita, il ciclo di riscaldamento finiva e il pool partiva lo
        stesso. Novecento risposte identiche, minuti di attesa, un rischio di
        limitazione, e la notizia alla fine invece che subito.
        """

        casi = [caso(riga=indice, descrizione=f"CASO {indice}") for indice in range(6)]
        corpo = {"error": {"message": "No auth credentials found.", "code": 401}}
        trasporto = TrasportoFinto([(401, corpo)])
        client = ClientAI(configurazione(parallelismo=4), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esiti = client.valuta_molti(casi)

        # Un esito per caso, come sempre: nessuno sparisce.
        self.assertEqual(len(esiti), len(casi))
        self.assertEqual(esiti[0].stato, "HTTP_401")
        self.assertEqual([esito.stato for esito in esiti[1:]], ["NON_CHIESTO"] * 5)
        # E di chiamate ne e' partita una sola.
        self.assertEqual(trasporto.quante, 1)
        # Nessuno di loro porta una decisione: a valle contano come da verificare.
        self.assertTrue(all(esito.decisione is None for esito in esiti))
        # E il dettaglio dice che cosa fare.
        self.assertIn("Impostazioni", esiti[-1].dettaglio)

    def test_43c_un_guasto_passeggero_non_ferma_l_infornata(self):
        """La controprova: un 429 e' passeggero e gli altri casi si tentano."""

        casi = [caso(riga=indice, descrizione=f"CASO {indice}") for indice in range(3)]

        class TrasportoConUnaLimitazione:
            def __init__(self) -> None:
                self._lucchetto = threading.Lock()
                self.quante = 0

            def __call__(self, url, corpo, intestazioni, timeout):
                with self._lucchetto:
                    self.quante += 1
                    prima = self.quante == 1
                if prima:
                    return (429, {"error": {"message": "slow down", "code": 429}})
                return risposta(decisione_del_modello())

        trasporto = TrasportoConUnaLimitazione()
        client = ClientAI(configurazione(parallelismo=2), trasporto=trasporto, chiave=CHIAVE_FINTA)

        with mock.patch.object(ai_client, "PAUSA_RIPETIZIONE_S", 0.0):
            esiti = client.valuta_molti(casi)

        self.assertEqual(len(esiti), 3)
        self.assertNotIn("NON_CHIESTO", [esito.stato for esito in esiti])

    def test_44_un_caso_che_esplode_non_porta_via_l_infornata(self):
        """Senza la rete di sicurezza si torna con un'eccezione al posto di 947
        esiti buoni, già calcolati e già pagati."""

        casi = [
            caso(riga=0, descrizione="CASO BUONO ZERO"),
            caso(riga=1, descrizione="CASO ROTTO"),
            caso(riga=2, descrizione="CASO BUONO DUE"),
        ]

        class TrasportoConUnCasoRotto:
            def __init__(self) -> None:
                self._lucchetto = threading.Lock()
                self.quante = 0

            def __call__(self, url, corpo, intestazioni, timeout):
                with self._lucchetto:
                    self.quante += 1
                if "CASO ROTTO" in corpo["messages"][1]["content"]:
                    return "duecento", {}      # il codice HTTP non è un numero
                return risposta(decisione_del_modello())

        trasporto = TrasportoConUnCasoRotto()
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esiti = client.valuta_molti(casi)

        self.assertEqual(len(esiti), len(casi))
        self.assertEqual(esiti[0].stato, "OK")
        self.assertEqual(esiti[2].stato, "OK")
        self.assertEqual(esiti[0].decisione["gestionale_source_row"], 0)
        self.assertEqual(esiti[2].decisione["gestionale_source_row"], 2)
        self.assertEqual(esiti[1].stato, "ERRORE_INTERNO")
        self.assertIsNone(esiti[1].decisione)


class ProvaConfigurazioneScrittaMale(ConCartellaTemporanea):
    """Un tetto che vale zero spegne tutta la fase senza dirlo: ogni caso torna
    TETTO_SPESA, tutto diventa DA_VERIFICARE e il messaggio all'utente gli
    ripete il numero che credeva di aver impostato."""

    def _impostazioni(self, dati: dict) -> Path:
        percorso = self.cartella("prova_impostazioni_") / "impostazioni_ai.json"
        percorso.write_bytes(json.dumps(dati).encode("utf-8"))
        return percorso

    def test_45_la_virgola_decimale_italiana_nel_tetto_di_spesa_si_accetta(self):
        import os

        percorso = self._impostazioni({"tetto_spesa_usd": "3,0"})

        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("OPENROUTER_MODEL", None)
            conf = ai_client.carica_configurazione(percorso)

        self.assertEqual(conf["tetto_spesa_usd"], 3.0)
        self.assertIsInstance(conf["tetto_spesa_usd"], float)

        # E con quel tetto le chiamate partono davvero, invece di tornare tutte
        # TETTO_SPESA come farebbe uno zero silenzioso.
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])
        client = ClientAI(conf, trasporto=trasporto, chiave=CHIAVE_FINTA)
        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(trasporto.quante, 1)

    def test_46_un_tetto_di_chiamate_illeggibile_torna_al_predefinito_non_a_zero(self):
        import os

        percorso = self._impostazioni({"tetto_chiamate": "molte"})

        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("OPENROUTER_MODEL", None)
            conf = ai_client.carica_configurazione(percorso)

        self.assertEqual(
            conf["tetto_chiamate"], ai_client.CONFIGURAZIONE_PREDEFINITA["tetto_chiamate"]
        )
        self.assertGreater(conf["tetto_chiamate"], 0, "uno zero spegnerebbe la fase in silenzio")


class ProvaImpostazioniSalvate(unittest.TestCase):
    """Le funzioni su cui poggia la pagina Impostazioni (Fase 5d).

    Il rischio qui non e' un errore che si vede: e' un file salvato che il
    programma poi ignora, o una chiave che esce da dove non deve."""

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="prova_impostazioni_5d_"))
        self.addCleanup(_ripulisci, self.cartella)
        self.secrets = self.cartella / "secrets.json"
        self.impostazioni = self.cartella / "impostazioni_ai.json"
        self._ambiente = mock.patch.dict("os.environ", {}, clear=False)
        self._ambiente.start()
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.addCleanup(self._ambiente.stop)

    def test_lo_stato_della_chiave_non_contiene_la_chiave(self):
        segreta = "sk-segretissima-0123456789XYZ"
        self.secrets.write_text(json.dumps({"openrouter": {"api_key": segreta}}), encoding="utf-8")

        stato = ai_client.stato_chiave(self.secrets)

        self.assertTrue(stato["presente"])
        self.assertEqual(stato["origine"], "file")
        self.assertNotIn(segreta, json.dumps(stato))
        # Nemmeno un pezzo che permetta di ricostruirla: solo la coda.
        self.assertNotIn(segreta[:12], json.dumps(stato))
        self.assertEqual(stato["coda"], segreta[-4:])

    def test_senza_chiave_lo_dice_e_non_finge(self):
        self.assertEqual(
            ai_client.stato_chiave(self.secrets),
            {"presente": False, "origine": "", "coda": ""},
        )

    def test_salvare_la_chiave_non_cancella_il_resto_del_file(self):
        """Il file esiste gia' e puo' contenere altro: riscriverlo da zero
        farebbe sparire in silenzio quello che non conosciamo."""
        self.secrets.write_text(
            json.dumps({"openrouter": {"api_key": "sk-vecchia-AAAA", "note": "mia"}, "altro": {"x": 1}}),
            encoding="utf-8",
        )

        ai_client.salva_chiave("sk-nuova-BBBB", self.secrets)

        dati = json.loads(self.secrets.read_text(encoding="utf-8"))
        self.assertEqual(dati["openrouter"]["api_key"], "sk-nuova-BBBB")
        self.assertEqual(dati["openrouter"]["note"], "mia")
        self.assertEqual(dati["altro"], {"x": 1})

    def test_una_chiave_vuota_non_si_salva(self):
        with self.assertRaises(ValueError):
            ai_client.salva_chiave("   ", self.secrets)
        self.assertFalse(self.secrets.exists())

    def test_un_valore_non_utilizzabile_e_un_errore_non_un_ripiego(self):
        """Il difetto misurato nella 5a: `tetto_spesa_usd` scritto «3,0» con la
        virgola italiana azzerava il tetto **dicendo all'utente il numero che
        credeva di aver impostato**. All'avvio il ripiego e' giusto; qui no,
        perche' l'utente ha appena scritto quel numero e sta guardando."""
        with self.assertRaises(ValueError) as errore:
            ai_client.salva_impostazioni({"tetto_spesa_usd": "3,0"}, self.impostazioni)

        self.assertIn("virgola", str(errore.exception))
        self.assertFalse(self.impostazioni.exists(), "niente si scrive quando qualcosa e' rifiutato")

    def test_la_chiave_non_passa_dalle_impostazioni(self):
        """`carica_configurazione` ignorerebbe `api_key` in silenzio: sarebbe un
        file che sembra dire una cosa che il programma non fa."""
        with self.assertRaises(ValueError):
            ai_client.salva_impostazioni({"api_key": "sk-x"}, self.impostazioni)

    def test_una_versione_di_prompt_inesistente_si_ferma_qui(self):
        """Altrimenti passa la convalida — e' una stringa non vuota — e fa
        sollevare il client alla prima chiamata, a fase gia' avviata."""
        with self.assertRaises(ValueError):
            ai_client.salva_impostazioni({"versione_prompt": "v99"}, self.impostazioni)
        self.assertFalse(self.impostazioni.exists())

    def test_quel_che_si_salva_e_quel_che_si_rilegge(self):
        conf = ai_client.salva_impostazioni(
            {"model": "openai/gpt-5.6-luna", "tetto_spesa_usd": 2.5}, self.impostazioni
        )

        self.assertEqual(conf["model"], "openai/gpt-5.6-luna")
        self.assertAlmostEqual(conf["tetto_spesa_usd"], 2.5)
        scritte = json.loads(self.impostazioni.read_text(encoding="utf-8"))
        self.assertEqual(scritte, {"model": "openai/gpt-5.6-luna", "tetto_spesa_usd": 2.5})

    def test_un_secondo_salvataggio_non_perde_il_primo(self):
        ai_client.salva_impostazioni({"model": "openai/gpt-5.6-luna"}, self.impostazioni)
        ai_client.salva_impostazioni({"tetto_spesa_usd": 1.5}, self.impostazioni)

        scritte = json.loads(self.impostazioni.read_text(encoding="utf-8"))
        self.assertEqual(scritte["model"], "openai/gpt-5.6-luna")
        self.assertAlmostEqual(scritte["tetto_spesa_usd"], 1.5)


def errore_json_richiesto():
    """La risposta vera di OpenRouter quando il fornitore vuole la parola «json».

    Copiata da una chiamata vera del 12 agosto 2026 a `qwen/qwen3.5-flash-02-23`:
    la frase che conta **non** sta nel messaggio in cima — li' c'e' un generico
    «Provider returned error» — ma annidata dentro una stringa in
    `error.metadata.raw`. Un riconoscitore che guardasse il solo messaggio non
    la vedrebbe mai."""
    return 400, {
        "error": {
            "message": "Provider returned error",
            "code": 400,
            "metadata": {
                "raw": (
                    'data: {"error":{"code":"invalid_parameter_error","param":null,'
                    "\"message\":\"'messages' must contain the word 'json' in some form, "
                    "to use 'response_format' of type 'json_object'.\","
                    '"type":"invalid_request_error"}}'
                ),
                "provider_name": "Alibaba",
            },
        }
    }


class ProvaFornitoreCheVuoleLaParolaJson(unittest.TestCase):
    """Il fornitore di calcolo lo sceglie OpenRouter, e puo' cambiare da un
    giorno all'altro: uno che pretende la parola «json» nei messaggi
    risponderebbe 400 a tutte e 948 le chiamate di una run. Il programma gira
    da solo e non ci sarebbe nessuno a capire perche'.

    La parola non si aggiunge sempre, perche' misurato su 400 casi peggiora il
    numero che non si negozia: si aggiunge quando quel fornitore l'ha chiesta."""

    def test_il_primo_rifiuto_accende_l_aggiunta_e_la_ripetizione_riesce(self):
        trasporto = TrasportoFinto([errore_json_richiesto(), risposta(decisione_del_modello())])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "OK", "la ripetizione con la parola deve riuscire")
        self.assertEqual(trasporto.quante, 2)
        primo = trasporto.chiamate[0]["corpo"]["messages"][0]["content"]
        secondo = trasporto.chiamate[1]["corpo"]["messages"][0]["content"]
        self.assertNotIn("json", primo.lower(), "la prima chiamata resta quella misurata")
        self.assertIn("json", secondo.lower(), "la seconda porta la parola richiesta")

    def test_resta_acceso_per_le_chiamate_successive(self):
        """Senza memoria, 948 casi pagherebbero 948 primi tentativi buttati."""
        trasporto = TrasportoFinto([
            errore_json_richiesto(),
            risposta(decisione_del_modello()),
            risposta(decisione_del_modello()),
        ])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        client.valuta_candidati(caso())
        esito = client.valuta_candidati(caso(riga=13))

        self.assertEqual(esito.stato, "OK")
        self.assertEqual(trasporto.quante, 3, "il secondo caso non deve ripetere il primo errore")
        self.assertIn("json", trasporto.chiamate[2]["corpo"]["messages"][0]["content"].lower())

    def test_un_400_qualsiasi_non_si_ripete(self):
        """La regola vecchia non si indebolisce: un model id sbagliato ripetuto
        resta sbagliato, e ripeterlo raddoppierebbe soltanto il conto."""
        quattrocento = (400, {"error": {"message": "openai/inesistente is not a valid model ID"}})
        trasporto = TrasportoFinto([quattrocento])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "HTTP_400")
        self.assertEqual(trasporto.quante, 1)


class LaMemoriaSiScriveAOndateTests(unittest.TestCase):
    """Salvare vuol dire riscrivere **tutto** il file, e il file cresce.

    Misurato dalla revisione della Fase 6b: 948 casi con una memoria di 568 KB
    pagavano ~90 secondi serializzati dentro il lucchetto, su una run che di
    rete ne impiega 105 — e il file arrivava a 1408 KB, cioe' la settimana dopo
    sarebbe costata di piu'. Il parallelismo su quei 90 secondi non compra
    niente, perche' stanno tutti nella stessa sezione protetta."""

    def _client(self, percorso: Path, trasporto, **modifiche):
        return ClientAI(
            configurazione(**modifiche), trasporto=trasporto, memoria=percorso, chiave=CHIAVE_FINTA
        )

    def _contando_i_salvataggi(self):
        """Sostituisce `_salva_memoria` con una che conta e poi fa il suo lavoro."""
        vero = ai_client.ClientAI._salva_memoria
        quanti: list[int] = []

        def conta(percorso, voci):
            quanti.append(len(voci))
            vero(percorso, voci)

        return quanti, mock.patch.object(ai_client.ClientAI, "_salva_memoria", staticmethod(conta))

    def test_un_infornata_riscrive_il_file_una_volta_sola(self) -> None:
        casi = [caso(riga=riga) for riga in range(1, 6)]
        trasporto = TrasportoFinto([risposta(decisione_del_modello())] * len(casi))
        quanti, conta = self._contando_i_salvataggi()
        with tempfile.TemporaryDirectory() as temporanea:
            percorso = Path(temporanea) / "memoria_ai.json"
            client = self._client(percorso, trasporto, parallelismo=4)
            with conta:
                esiti = client.valuta_molti(casi)
            voci = json.loads(percorso.read_text(encoding="utf-8"))["voci"]
        self.assertEqual([esito.stato for esito in esiti], ["OK"] * len(casi))
        self.assertEqual(len(quanti), 1, "il file si riscrive a ogni risposta")
        self.assertEqual(len(voci), len(casi), "le risposte dell'infornata devono finire su disco")

    def test_una_valutazione_sola_si_salva_subito(self) -> None:
        """Fuori da un'infornata non c'e' nessuna «fine» a cui appoggiarsi: chi
        valuta un caso per volta deve trovare la sua risposta sul disco."""
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])
        with tempfile.TemporaryDirectory() as temporanea:
            percorso = Path(temporanea) / "memoria_ai.json"
            client = self._client(percorso, trasporto)
            self.assertEqual(client.valuta_candidati(caso()).stato, "OK")
            voci = json.loads(percorso.read_text(encoding="utf-8"))["voci"]
        self.assertEqual(len(voci), 1)

    def test_un_infornata_che_solleva_salva_lo_stesso_quello_che_ha_pagato(self) -> None:
        """Il salvataggio sta in un `finally`: le risposte gia' pagate non si
        buttano perche' qualcosa e' andato storto dopo."""
        casi = [caso(riga=1), caso(riga=2)]
        trasporto = TrasportoFinto([risposta(decisione_del_modello())] * 2)
        with tempfile.TemporaryDirectory() as temporanea:
            percorso = Path(temporanea) / "memoria_ai.json"
            client = self._client(percorso, trasporto, parallelismo=1)
            with mock.patch.object(
                ai_client.ClientAI, "_registra", side_effect=RuntimeError("guasto a valle")
            ), self.assertRaises(RuntimeError):
                client.valuta_molti(casi)
            esiste = percorso.exists()
        self.assertTrue(esiste, "quello che era gia' in memoria non deve sparire")

    def test_un_prompt_riscritto_senza_rinominarlo_invalida_le_risposte_vecchie(self) -> None:
        """La chiave sigillava l'**etichetta** della versione, non il testo:
        chi correggeva `valuta_candidati.v3.md` senza rinominarlo continuava a
        ricevere le risposte del prompt vecchio, e il commento che diceva «se
        cambia uno qualunque di questi, la risposta vecchia non vale piu'» era
        falso proprio sul campo che si cambia piu' spesso."""
        cartella = Path(tempfile.mkdtemp(prefix="prova_prompt_testo_"))
        self.addCleanup(_ripulisci, cartella)
        percorso_prompt = cartella / "valuta_candidati.v9.md"
        memoria = cartella / "memoria_ai.json"

        with mock.patch.object(ai_client, "CARTELLA_PROMPT", cartella):
            percorso_prompt.write_bytes(b"prima stesura del prompt\n")
            ai_client._prompt_in_memoria.clear()
            primo = TrasportoFinto([risposta(decisione_del_modello())])
            self.assertEqual(
                self._client(memoria, primo, versione_prompt="v9").valuta_candidati(caso()).stato,
                "OK",
            )

            # Stesso nome, stessa versione, testo diverso.
            percorso_prompt.write_bytes(b"seconda stesura, corretta, stesso nome\n")
            ai_client._prompt_in_memoria.clear()
            secondo = TrasportoFinto([risposta(decisione_del_modello())])
            esito = self._client(memoria, secondo, versione_prompt="v9").valuta_candidati(caso())

        ai_client._prompt_in_memoria.clear()
        self.assertEqual(esito.stato, "OK")
        self.assertEqual(secondo.quante, 1, "la risposta del prompt vecchio e' stata riusata")


class IlPredefinitoMisuratoTests(unittest.TestCase):
    def test_il_parallelismo_predefinito_e_quello_misurato(self) -> None:
        """32 non e' scelto a occhio: 150 casi in 82,1 s a 8, 22,7 s a 32,
        19,0 s a 64. E' il punto in cui la curva si piega. Cambiarlo senza
        rifare la misura deve fare rumore, anche perche' raddoppiarlo raddoppia
        le richieste che un fornitore di calcolo si vede arrivare insieme."""
        self.assertEqual(ai_client.CONFIGURAZIONE_PREDEFINITA["parallelismo"], 32)


def _ripulisci(cartella: Path) -> None:
    import shutil

    shutil.rmtree(cartella, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
