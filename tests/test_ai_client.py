"""Tests the AI client without touching the network, one case at a time.

The HTTP transport is injected through the constructor: every test replays a
hand-written script of responses. No test opens a socket or reads
`app/data/secrets.json`; the key the tests pass is fake.

`test_02_finish_reason_length_e_troncata` is the test that matters most.
Measured with `max_tokens` at 400: 11 responses out of 30 came back with
`finish_reason: "length"`, empty `content`, and no HTTP error — the model had
spent the whole budget reasoning. Read as "no candidate is good enough", that
response silently drops a product from the comparison. If this test
disappears, the defect comes back.
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

# The seven keys from `references/ai-decision-format.md`, the ones
# `scripts/merge_match_decisions.py` expects to read.
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
    """A 200 response shaped like a real OpenRouter one."""

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
    """Replays a scripted list of responses and counts calls. Never touches the network."""

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
    """The eighteen required cases of the contract, plus a few extra safeguards."""

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
        """The measured trap: the token budget runs out on reasoning, not a rejection."""

        troncata = risposta("", finish_reason="length", token_uscita=400)
        trasporto = TrasportoFinto([troncata, troncata])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "TRONCATA")
        self.assertIsNone(esito.decisione)
        # And, above all, it did not turn into a rejection.
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
        segreta = "chiave-segretissima-di-esempio-0123456789"
        # The 401 echoes the header, straddling the 300-character point where the
        # message gets truncated: if truncation happened before the key is
        # masked, the first part of it would leak.
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
        # Not even a fragment of the key: truncating before masking would leave
        # its start exposed, and half a key is still a leaked key.
        pezzi = {segreta[i:i + 12] for i in range(len(segreta) - 11)}
        self.assertEqual(sorted(pezzo for pezzo in pezzi if pezzo in tutto), [])
        self.assertIn(ai_client.NASCOSTO, esito.dettaglio)
        self.assertNotIn(segreta, json.dumps(client.contabilita, ensure_ascii=False))

    # -------------------------------------------------------- presidi in più

    def test_19_la_chiave_sta_nel_campo_annidato_di_secrets(self):
        """Looking for it at the top level gives None and then a 401: a known trap."""

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
            # The file can be missing: that is the common case, not an error.
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
        """Never the EAN (the test's ground truth) and never the price."""

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
        """The prompt changes by measurement, not by rewriting it on a hunch."""

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

        # The prompt actually sent is the one for the configured version, and
        # the default is now a newer, measured one. This checks that the
        # client sends the file for the version it declares, not a string
        # baked into the code.
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])
        client = ClientAI(configurazione(versione_prompt="v1"), trasporto=trasporto, chiave=CHIAVE_FINTA)
        client.valuta_candidati(caso())
        inviato = trasporto.chiamate[0]["corpo"]
        self.assertEqual(inviato["messages"][0]["content"], atteso)
        self.assertEqual(inviato["max_tokens"], 1500)
        self.assertNotIn("provider", inviato, "il fornitore di calcolo lo sceglie OpenRouter")


    def test_24_prova_connessione_chiama_davvero_e_non_usa_la_memoria(self):
        """Confirms a bad key or model id right away: reading from memory would prove nothing."""

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


# Guards for a round of review that found twenty-nine defects with the suite
# green: the green suite, on its own, did not see them. What follows defends
# each correction individually, so a rewrite that removed one turns something
# red.


def _scrivi_memoria(percorso: Path, voci: dict) -> None:
    """A memory file written by hand, as if a run had left it there."""

    percorso.write_bytes(
        json.dumps({"versione": 1, "voci": voci}, ensure_ascii=False, indent=2).encode("utf-8")
    )


def _voci_sul_disco(percorso: Path) -> dict:
    return json.loads(percorso.read_bytes().decode("utf-8"))["voci"]


def chiave_di(caso_dato: CasoValutazione, *, versione_prompt: str | None = None) -> str:
    """The key the client uses to address that case, with the default model.

    The prompt version follows the default: hardcoding it here would mean
    that changing the prompt stops these tests from talking about the real
    memory. It carries the fingerprint of the prompt's text, like the client
    does: sealing only the version label would keep a rewritten prompt's old
    responses valid."""

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
    """A decision in the consumer format, as it is stored in memory."""

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
    """Raises if called; confirms that it never is."""

    def __init__(self) -> None:
        self.quante = 0

    def __call__(self, url, corpo, intestazioni, timeout):
        self.quante += 1
        raise AssertionError("questo caso non doveva costare una chiamata")


class TrasportoLento:
    """Counts under a lock and takes a few milliseconds, on purpose, for thread tests."""

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
    """The memory file lives in `app/data/`, outside version control: it can be
    edited by hand, and no decision the client would never have produced
    should be able to come out of it."""

    def test_48_una_voce_con_decisione_che_non_e_un_oggetto_non_diventa_un_ricordo(self):
        """The first of two layers defending the memory.

        The second — `decisione_non_utilizzabile` — is covered by test_25, 26
        and 27. From the public surface the two layers converge: remove the
        first and the second still stops the entry, with no visible
        difference. That is why this test looks at a private method: it is
        the only way to notice if someone later decides "the caller validates
        anyway" and defense in depth silently becomes a single check.
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
        """The most dangerous decision this project can make."""

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
        storta["confidenza"] = "ALTA"          # wrong key name, not part of the format
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
        """The full loop: one client writes, the next reads and does not call."""

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
        """The next run must be able to retry it: only valid responses get remembered."""

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
        # assertIsNone rather than assertFalse: a {} would pass for "no decision"
        # while actually being an empty memory entry that slipped through.
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

        # Both start from the empty file: the second has no in-memory record of
        # the entry the first is about to write.
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
        """`write_text` on Windows would write CRLF, and the file would show up in diffs."""

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
    """A cap that, while being read, blocks the thread until all the others
    have arrived too.

    This proves mutual exclusion, not just the final total. Under CPython the
    total alone is not enough: between the `self._chiamate >= tetto` check and
    `self._chiamate += 1` there is no call and no backward jump, so the
    interpreter almost never switches threads right there, and the numbers
    come out right even without a lock. Measured: with the lock removed,
    eighty cases and a cap of ten gave exactly ten calls in 120 out of 120
    runs. A rewrite could drop the lock without any test noticing.

    The cap is read inside the protected section: with the lock, no other
    thread can enter and the barrier times out on its own; without it, they
    all get in together.
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
            prima = self.letture == 1  # the warm-up call, made alone
        if not prima:
            try:
                self.porta.wait()
            except threading.BrokenBarrierError:
                return self.valore
            with self._lucchetto:
                self.entrati_insieme += 1
        return self.valore


class ProvaTettiSottoIThread(ConCartellaTemporanea):
    """Check-then-increment, with sixteen threads, stops being a real check."""

    def test_47_la_prenotazione_di_una_chiamata_non_ammette_due_thread_insieme(self):
        """Tests the lock for what it does, not for the number it happens to produce."""

        operai = 8
        trasporto = TrasportoLento(costo=0.0, pausa=0.001)
        client = ClientAI(
            configurazione(tetto_chiamate=2, parallelismo=operai, tetto_spesa_usd=100.0),
            trasporto=trasporto,
            chiave=CHIAVE_FINTA,
        )
        tetto = TettoCheSiFaLeggereDaTuttiInsieme(2, operai)
        # After construction: config validation would otherwise reject this.
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
        """The first call runs alone on purpose: before seeing one, the cost is
        unknown, and spend reservation works off an estimate."""

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
    """One malformed response must not be able to blow up the whole batch."""

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
        """If the service declares a failure, that failure is the outcome: at
        most one retry gets paid for, which is the right way to fail."""

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
        """Retrying a `MemoryError` or a `KeyError` would be the worst thing to do."""

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
        """The truncated attempt still consumed and billed its reasoning tokens."""

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
    """A failing case does not stop the others, and does not make them disappear either."""

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
        """The warm-up promise was written but not kept.

        The warm-up comment claims "a bad model or an expired key is caught
        before launching hundreds of calls", but `_prenota_chiamata` counts
        the call before making it: after the first attempt, even a failed
        one, the warm-up loop was done and the pool started anyway. Hundreds
        of identical failures, minutes of waiting, a rate-limit risk, and the
        failure surfacing at the end instead of immediately.
        """

        casi = [caso(riga=indice, descrizione=f"CASO {indice}") for indice in range(6)]
        corpo = {"error": {"message": "No auth credentials found.", "code": 401}}
        trasporto = TrasportoFinto([(401, corpo)])
        client = ClientAI(configurazione(parallelismo=4), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esiti = client.valuta_molti(casi)

        # One outcome per case, as always: none disappear.
        self.assertEqual(len(esiti), len(casi))
        self.assertEqual(esiti[0].stato, "HTTP_401")
        self.assertEqual([esito.stato for esito in esiti[1:]], ["NON_CHIESTO"] * 5)
        # And only one call went out.
        self.assertEqual(trasporto.quante, 1)
        # None of them carries a decision: downstream they count as needing review.
        self.assertTrue(all(esito.decisione is None for esito in esiti))
        # And the detail says what to do.
        self.assertIn("Impostazioni", esiti[-1].dettaglio)

    def test_43c_un_guasto_passeggero_non_ferma_l_infornata(self):
        """The counter-case: a 429 is transient, so the other cases still get tried."""

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
        """Without this safety net, one bad case would raise instead of returning
        the other outcomes, already computed and already paid for."""

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
                    return "duecento", {}      # HTTP status is not a number
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
    """A cap of zero silently turns off the whole phase: every case comes back
    `TETTO_SPESA`, everything becomes `DA_VERIFICARE`, and the message shown
    to the user just echoes the number they thought they had set."""

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

        # And with that cap, calls actually go out, instead of all coming back
        # TETTO_SPESA as a silent zero would.
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
    """The functions the Settings page relies on.

    The risk here is not a visible error: it is a file that gets saved but
    then ignored by the program, or a key leaking from where it should not."""

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
        # Not even a fragment that would let it be reconstructed: only the tail.
        self.assertNotIn(segreta[:12], json.dumps(stato))
        self.assertEqual(stato["coda"], segreta[-4:])

    def test_senza_chiave_lo_dice_e_non_finge(self):
        self.assertEqual(
            ai_client.stato_chiave(self.secrets),
            {"presente": False, "origine": "", "coda": ""},
        )

    def test_salvare_la_chiave_non_cancella_il_resto_del_file(self):
        """The file already exists and can hold other data: rewriting it from
        scratch would silently drop what we don't know about."""
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
        """The measured defect: `tetto_spesa_usd` written as "3,0" with an
        Italian decimal comma zeroed out the cap while telling the user the
        number they thought they had set. Falling back is fine at startup; not
        here, because the user just typed that number and is watching."""
        with self.assertRaises(ValueError) as errore:
            ai_client.salva_impostazioni({"tetto_spesa_usd": "3,0"}, self.impostazioni)

        self.assertIn("virgola", str(errore.exception))
        self.assertFalse(self.impostazioni.exists(), "niente si scrive quando qualcosa e' rifiutato")

    def test_la_chiave_non_passa_dalle_impostazioni(self):
        """`carica_configurazione` would silently ignore `api_key`: the file
        would look like it does something the program does not do."""
        with self.assertRaises(ValueError):
            ai_client.salva_impostazioni({"api_key": "sk-x"}, self.impostazioni)

    def test_una_versione_di_prompt_inesistente_si_ferma_qui(self):
        """Otherwise it passes validation — it's a non-empty string — and only
        makes the client raise on the first call, once the phase has already started."""
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
    """A real OpenRouter response for when the provider requires the word "json".

    Copied from a real call to `qwen/qwen3.5-flash-02-23`: the phrase that
    matters is not in the top-level message — that one just says a generic
    "Provider returned error" — but nested inside a string in
    `error.metadata.raw`. A detector that only looked at the message would
    never see it."""
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
    """OpenRouter picks the compute provider, and it can change at any time: one
    that requires the word "json" in the messages would answer 400 to every
    call in a run. The program runs unattended, with no one there to notice why.

    The word is not added unconditionally, because measured on 400 cases it
    hurts the metric that matters most: it gets added only once that provider
    has asked for it."""

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
        """Without memory, every case would pay for a wasted first attempt."""
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
        """The old rule still holds: a bad model id stays bad on retry, and
        retrying it would only double the bill."""
        quattrocento = (400, {"error": {"message": "openai/inesistente is not a valid model ID"}})
        trasporto = TrasportoFinto([quattrocento])
        client = ClientAI(configurazione(), trasporto=trasporto, chiave=CHIAVE_FINTA)

        esito = client.valuta_candidati(caso())

        self.assertEqual(esito.stato, "HTTP_400")
        self.assertEqual(trasporto.quante, 1)


class LaMemoriaSiScriveAOndateTests(unittest.TestCase):
    """Saving means rewriting the whole file, and the file keeps growing.

    Measured: 948 cases with a 568 KB memory file paid roughly 90 seconds
    serialized inside the lock, on a run whose network calls took 105 seconds
    total — and the file grew to 1408 KB, meaning the next run would cost even
    more. Parallelism buys nothing over those 90 seconds, because they all sit
    inside the same protected section."""

    def _client(self, percorso: Path, trasporto, **modifiche):
        return ClientAI(
            configurazione(**modifiche), trasporto=trasporto, memoria=percorso, chiave=CHIAVE_FINTA
        )

    def _contando_i_salvataggi(self):
        """Replaces `_salva_memoria` with one that counts calls, then does the real work."""
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
        """Outside a batch there is no "end" to rely on: evaluating one case at
        a time must still leave its answer on disk right away."""
        trasporto = TrasportoFinto([risposta(decisione_del_modello())])
        with tempfile.TemporaryDirectory() as temporanea:
            percorso = Path(temporanea) / "memoria_ai.json"
            client = self._client(percorso, trasporto)
            self.assertEqual(client.valuta_candidati(caso()).stato, "OK")
            voci = json.loads(percorso.read_text(encoding="utf-8"))["voci"]
        self.assertEqual(len(voci), 1)

    def test_un_infornata_che_solleva_salva_lo_stesso_quello_che_ha_pagato(self) -> None:
        """The save runs in a `finally`: responses already paid for do not get
        thrown away because something failed afterward."""
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
        """The key seals the prompt's text, not just its version label: editing
        a prompt file without renaming it must not keep returning the old
        prompt's responses, since any change to the prompt should invalidate
        old answers — on exactly the field that changes most often."""
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
        """32 is not a guess: 150 cases took 82.1 s at 8 workers, 22.7 s at 32,
        19.0 s at 64. It's the knee of the curve. Changing it without
        re-measuring should be noticed, also because doubling it doubles the
        requests a compute provider sees arrive at once."""
        self.assertEqual(ai_client.CONFIGURAZIONE_PREDEFINITA["parallelismo"], 32)


def _ripulisci(cartella: Path) -> None:
    import shutil

    shutil.rmtree(cartella, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
