#!/usr/bin/env python3
"""Il client che fa giudicare all'AI una shortlist di candidati gia' prodotta.

Fase 5a. Il passo deterministico produce, per ogni coppia (articolo del
gestionale, fornitore), una manciata di candidati con un punteggio. Qui si
chiede a un modello, tramite OpenRouter, se uno di quei candidati e' lo stesso
identico prodotto. La decisione esce nel formato di
`references/ai-decision-format.md`, che e' quello letto da
`scripts/merge_match_decisions.py`.

Due regole governano tutto il file.

**Il degrado e' un valore di ritorno, non un'eccezione.** Nessun metodo pubblico
solleva per un guasto della rete, del servizio o della risposta: torna un
`EsitoAI` con lo stato che dice cosa e' andato storto e `decisione = None`. Chi
chiama traduce ogni `decisione is None` in `DA_VERIFICARE`. Un'eccezione resta
ammessa solo per un uso sbagliato dell'API (un caso senza candidati, un file di
prompt che non esiste).

**Una risposta vuota non e' un rifiuto.** Misurato l'11 agosto 2026 su chiamate
vere: `deepseek/deepseek-v4-flash` su molti fornitori di calcolo e' un modello
che ragiona, e i token di ragionamento si scalano da `max_tokens`. Con
`max_tokens` a 400, 11 risposte su 30 tornavano con `finish_reason: "length"`,
`content` vuoto e nessun errore HTTP: il modello aveva consumato tutto il tetto
pensando, senza mai scrivere la risposta. Se il codice non riconosce quel caso,
lo legge come «nessun candidato va bene» e un prodotto sparisce dal confronto in
silenzio. Da qui `max_tokens` predefinito a 1500, lo stato `TRONCATA` e il suo
test dedicato.

Il trasporto HTTP e' iniettabile dal costruttore, ed e' cosi' che i test evitano
la rete. Un trasporto e' un chiamabile con questa forma:

    trasporto(url: str, corpo: dict, intestazioni: dict, timeout: float)
        -> tuple[int, Any]

Torna il codice HTTP e il corpo della risposta gia' decodificato da JSON (o il
testo grezzo, se JSON non era). Solleva soltanto per un guasto di rete: il
client lo traduce in `ERRORE_RETE`. Il trasporto vero e' `trasporto_urllib`.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import scrittura_sicura  # noqa: E402


RADICE_APP = Path(__file__).resolve().parent
RADICE_SKILL = RADICE_APP.parent
CARTELLA_PROMPT = RADICE_SKILL / "references" / "prompts"
PERCORSO_SECRETS = RADICE_APP / "data" / "secrets.json"
PERCORSO_IMPOSTAZIONI = RADICE_APP / "data" / "impostazioni_ai.json"
PERCORSO_MEMORIA = RADICE_APP / "data" / "memoria_ai.json"


CONFIGURAZIONE_PREDEFINITA = {
    # Scelto dal confronto fra i sette candidati sul banco di prova, non da una
    # preferenza: e' l'unico che unisce zero risposte non conformi, il minor
    # numero di match mancati e la velocita' piu' alta. `deepseek-v4-flash`, che
    # era il predefinito scritto nel piano, sbaglia il doppio degli `ALTA`.
    # Costa anche meno di `gpt-oss-120b` **misurato**, benche' il listino lo dia
    # a 0,600 $/M in uscita contro 0,170: l'altro produce molti piu' token di
    # ragionamento. Il prezzo di listino non basta a scegliere.
    "model": "openai/gpt-5.6-luna",
    "base_url": "https://openrouter.ai/api/v1",
    "max_tokens": 1500,
    "temperature": 0.0,
    "timeout_secondi": 60.0,
    "tetto_spesa_usd": 3.0,        # ~2,6 € — il budget di Daniele è 2-3 € a run
    "tetto_chiamate": 4000,
    # Misurato il 12 agosto 2026 su chiamate vere, con la verifica avversariale
    # accesa e la memoria vuota a ogni passata: **150 casi** in 82,1 s a 8, in
    # 22,7 s a 32, in 19,0 s a 64. 32 e' il punto in cui la curva si piega:
    # raddoppiarlo toglie poco e moltiplica per due le richieste che un
    # fornitore di calcolo si vede arrivare insieme.
    #
    # ⚠ Non si moltiplica per 948/150 per avere la durata di una run vera, e la
    # revisione della 6b l'ha misurato: il costo della memoria non e' lineare
    # nel numero di casi — ogni salvataggio riscrive tutto il file, che nel
    # frattempo cresce — e su quella parte il parallelismo non compra niente,
    # perche' sta dentro il lucchetto. La run vera dei 948 casi, misurata da
    # capo a fondo: **105 s** con la memoria che partiva vuota. Vedi
    # `RISPOSTE_FRA_DUE_SALVATAGGI`, che e' la correzione di quel costo.
    "parallelismo": 32,
    # v3 e' quella misurata: v1 sbagliava 5 ALTA e perdeva il 32% dei match,
    # v2 ne perdeva il 16% ma sbagliava 9 ALTA, v3 sbaglia 3 e ne perde il 31%.
    # Con la verifica avversariale sopra, v3 porta gli ALTA sbagliati a zero.
    "versione_prompt": "v3",
    "versione_avversario": "v1",
}


# Gli stati. `OK` e `DALLA_MEMORIA` portano una decisione, tutti gli altri no.
STATO_OK = "OK"
STATO_DALLA_MEMORIA = "DALLA_MEMORIA"
STATO_TRONCATA = "TRONCATA"
STATO_CONTENUTO_VUOTO = "CONTENUTO_VUOTO"
STATO_NON_E_JSON = "NON_E_JSON"
STATO_SCHEMA_NON_CONFORME = "SCHEMA_NON_CONFORME"
STATO_RIGA_FUORI_SHORTLIST = "RIGA_FUORI_SHORTLIST"
STATO_ERRORE_RETE = "ERRORE_RETE"
STATO_SENZA_CHIAVE = "SENZA_CHIAVE"
STATO_TETTO_SPESA = "TETTO_SPESA"
STATO_TETTO_CHIAMATE = "TETTO_CHIAMATE"
# Il fornitore vuole la parola «json» nel messaggio: si ripete aggiungendola.
STATO_JSON_RICHIESTO = "JSON_RICHIESTO"

# Un solo tentativo di ripetizione, e solo per gli stati che possono cambiare
# esito. Un `HTTP_400` (model id sbagliato) e un `HTTP_401` (chiave non valida)
# ripetendoli non si aggiustano.
# Gli stati per cui NON ha senso continuare l'infornata: la chiave non e'
# accettata, il credito e' finito, il fornitore ci ha chiusi fuori. Ripetere
# non li aggiusta e nemmeno provare gli altri novecento casi: e' la stessa
# risposta novecento volte, minuti di attesa e un rischio di limitazione, per
# scoprire alla fine quello che si sapeva alla prima. Restano fuori `HTTP_429`
# e `HTTP_5xx`, che sono passeggeri, e `HTTP_400` (model id sbagliato), che
# oggi conviene lasciar correre perche' puo' dipendere dal singolo caso.
STATI_CHE_FERMANO_L_INFORNATA = frozenset({"HTTP_401", "HTTP_402", "HTTP_403"})

# Chi non e' stato chiesto perche' l'infornata si e' fermata prima. Non e' un
# guasto del caso: e' un caso che nessuno ha valutato, e a valle vale come
# `DA_VERIFICARE` esattamente come gli altri non valutati.
STATO_NON_CHIESTO = "NON_CHIESTO"

STATI_RIPETIBILI = frozenset({
    STATO_TRONCATA,
    STATO_CONTENUTO_VUOTO,
    STATO_NON_E_JSON,
    STATO_SCHEMA_NON_CONFORME,
    STATO_ERRORE_RETE,
    # Ripetibile perche' la seconda chiamata e' diversa dalla prima: porta la
    # parola che il fornitore ha chiesto. Senza questo, un `HTTP_400` non si
    # ripeterebbe mai — ed e' giusto, perche' un model id sbagliato ripetuto
    # resta sbagliato.
    STATO_JSON_RICHIESTO,
    "HTTP_429",
    "HTTP_5xx",
})

# Un guasto del trasporto che non e' un guasto di rete: un difetto di codice
# dentro un trasporto iniettato. Non si ripete — ripetere un MemoryError o un
# KeyError e' la cosa peggiore da fare — e non si confonde con la rete che va
# male, perche' nella contabilita' per stato sono due cose diverse.
STATO_ERRORE_TRASPORTO = "ERRORE_TRASPORTO"

# Un guasto imprevisto dentro il client stesso, catturato per non far cadere
# tutta l'infornata insieme a un caso solo.
STATO_ERRORE_INTERNO = "ERRORE_INTERNO"

# Le famiglie che sono davvero un guasto di rete. `OSError` comprende
# `TimeoutError`, `ConnectionError` e `URLError`; `ValueError` copre un corpo
# che non si decodifica.
GUASTI_DI_RETE = (OSError, http.client.HTTPException, ValueError)

# Quanto si mette da parte per una chiamata in volo, finche' non si e' visto un
# costo vero. Misurato l'11 agosto: un caso completo costa ~0,0001 $. Si parte
# dieci volte piu' alti, perche' sbagliare per eccesso costa qualche chiamata in
# meno e sbagliare per difetto costa sforare il tetto.
COSTO_ATTESO_INIZIALE = 0.001

# Quante risposte nuove si accumulano prima di riscrivere il file della memoria,
# quando si lavora a infornate. Vedi `ClientAI._scrivi_ricordo`: salvare vuol
# dire riscrivere tutto il file, e con 1057 salvataggi su una run vera erano
# ~90 secondi dentro il lucchetto. Cento e' il compromesso: dieci scritture su
# una run da 948 casi, e al massimo cento risposte gia' pagate perse se il
# processo muore a meta'.
RISPOSTE_FRA_DUE_SALVATAGGI = 100

# Prima di ripetere si aspetta, ma solo dove aspettare serve davvero.
PAUSA_RIPETIZIONE_S = 2.0
STATI_CON_PAUSA = frozenset({"HTTP_429", "HTTP_5xx", STATO_ERRORE_RETE})

# Alcuni fornitori di calcolo pretendono la parola «json» nei messaggi.
#
# E' quello che ha ucciso `qwen/qwen3.5-flash-02-23` nel confronto fra i
# modelli: HTTP 400 su tutte e 150 le chiamate, con il corpo dell'errore che lo
# dice per esteso — «'messages' must contain the word 'json' in some form, to
# use 'response_format'». La stessa identica chiamata, con la parola aggiunta,
# torna 200 dallo stesso fornitore. Provato.
#
# Conta perche' **il fornitore lo sceglie OpenRouter**, ed e' una decisione
# presa e giusta: inchiodarne uno trasformerebbe un guasto suo in un guasto
# nostro. Ma vuol dire che domani il modello configurato puo' finire su un
# fornitore con questa regola, e la fase AI di quella run risponderebbe 400 in
# blocco. Il programma gira da solo: non ci sara' nessuno a capire perche'.
#
# ⚠ **La parola non si aggiunge sempre**, e la ragione e' misurata. Metterla in
# coda al prompt di sistema cambia le risposte, perche' il modello legge
# un'istruzione sul formato come un invito a essere sbrigativo:
#
#   | prompt di sistema        | `ALTA` sbagliati | match mancati |
#   |--------------------------|------------------|---------------|
#   | senza aggiunte (3 giri)  | **4 · 4 · 5**    | 40 · 41 · 43  |
#   | «Rispondi soltanto con   | **7 · 6 · 7**    | 36 · 37 · 37  |
#   |  l'oggetto json…» (3)    |                  |               |
#   | «Formato: json.» (1)     | 6                | 41            |
#
# Le due fasce non si sovrappongono: pagare due `ALTA` sbagliati su 400 casi —
# il solo errore che fa comprare la cosa sbagliata — per difendersi da un
# fornitore che oggi non ci serve nemmeno e' un cattivo affare.
#
# Quindi la parola entra **solo quando quel fornitore ha gia' risposto che la
# vuole**: un `HTTP 400` con questa firma diventa ripetibile una volta sola, e
# la ripetizione la aggiunge. Il percorso normale resta identico a quello
# misurato, e la difesa c'e' lo stesso.
FIRMA_JSON_RICHIESTO = "must contain the word 'json'"
ISTRUZIONE_FORMATO = "Formato della risposta: json."


def _chiede_la_parola_json(risposta: Any) -> bool:
    """Riconosce il rifiuto del fornitore che vuole la parola «json».

    Si guarda **tutta** la risposta serializzata, non il solo messaggio: quando
    OpenRouter incarta l'errore di un fornitore, in cima resta un generico
    «Provider returned error» e la frase vera sta annidata in
    `error.metadata.raw`, dentro una stringa. Misurato su una risposta vera."""
    try:
        testo = risposta if isinstance(risposta, str) else json.dumps(risposta, ensure_ascii=False)
    except (TypeError, ValueError):
        testo = str(risposta)
    return FIRMA_JSON_RICHIESTO in testo.lower()

AZIONI_AMMESSE = ("ACCEPT", "REJECT", "UNRESOLVED")
CONFIDENZE_AMMESSE = ("ALTA", "MEDIA", "BASSA")
CHIAVI_ATTESE = frozenset({"azione", "source_row", "confidenza", "motivo"})

# La versione dello schema entra nella chiave della memoria insieme a quella del
# prompt: cambiare la forma della risposta deve invalidare le risposte vecchie.
VERSIONE_SCHEMA = "v1"
NOME_SCHEMA = "valutazione_candidati"

SCHEMA_RISPOSTA = {
    "type": "json_schema",
    "json_schema": {
        "name": NOME_SCHEMA,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "azione": {"type": "string", "enum": list(AZIONI_AMMESSE)},
                "source_row": {"type": ["integer", "null"]},
                "confidenza": {"type": "string", "enum": list(CONFIDENZE_AMMESSE)},
                "motivo": {"type": "string", "maxLength": 200},
            },
            "required": ["azione", "source_row", "confidenza", "motivo"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class Candidato:
    """Un candidato della shortlist, come lo vede il modello.

    Niente EAN e niente prezzo: l'EAN perche' il banco di prova lo usa come
    verita' di riferimento e mostrarlo renderebbe falsa la misura, il prezzo
    perche' non c'entra con l'identita' del prodotto.
    """

    source_row: int
    description: str
    score: float


@dataclass(frozen=True)
class CasoValutazione:
    """Una coppia (articolo del gestionale, fornitore) con la sua shortlist."""

    gestionale_source_row: int
    supplier: str
    descrizione: str
    candidati: tuple[Candidato, ...]


@dataclass(frozen=True)
class EsitoAI:
    """Il risultato di una valutazione: sempre un valore, mai un'eccezione."""

    stato: str                 # vedi gli stati qui sopra
    decisione: dict | None     # formato di references/ai-decision-format.md, o None
    modello: str
    fornitore_calcolo: str | None
    costo_usd: float
    token_ingresso: int
    token_uscita: int
    tentativi: int
    durata_s: float
    dettaglio: str             # italiano, leggibile, MAI contiene la chiave


# Il caso con cui `prova_connessione` verifica chiave, model id e schema con una
# chiamata vera: `GET /models` non serve, e' pubblico e risponde anche senza chiave.
CASO_DI_PROVA = CasoValutazione(
    gestionale_source_row=0,
    supplier="prova",
    descrizione="BAGNOSCHIUMA VIDOR 500 ML TALCO",
    candidati=(
        Candidato(source_row=1, description="VIDOR BAGNO TALCO 500 ML", score=0.82),
        Candidato(source_row=2, description="VIDOR DEO SPRAY UOMO 150 ML", score=0.31),
    ),
)


NASCOSTO = "[chiave nascosta]"
# Rete di sicurezza generica, oltre alla sostituzione della chiave vera: qualunque
# cosa abbia la forma di una chiave non deve uscire da qui.
_FORMA_DI_CHIAVE = re.compile(r"sk-[A-Za-z0-9._\-]{8,}")


def oscura(valore: Any, chiave: str | None) -> Any:
    """Toglie la chiave da un testo. Si applica a ogni campo di `EsitoAI`.

    **Pubblica di proposito.** La usa anche `app/server.py`, che ripulisce da
    qui ogni messaggio d'errore diretto al browser: due espressioni regolari da
    tenere allineate vorrebbero dire che quella dimenticata e' quella che perde
    la chiave. Chi la rinomina rompe il server, ed e' giusto che lo sappia."""

    if not isinstance(valore, str):
        return valore
    if chiave:
        valore = valore.replace(chiave, NASCOSTO)
    return _FORMA_DI_CHIAVE.sub(NASCOSTO, valore)


def _numero(valore: Any, predefinito: float = 0.0) -> float:
    try:
        return float(valore)
    except (TypeError, ValueError):
        return predefinito


def _intero(valore: Any, predefinito: int = 0) -> int:
    try:
        return int(valore)
    except (TypeError, ValueError):
        return predefinito


def _decimale(valore: Any) -> float:
    """Un numero, accettando anche «3,0».

    La virgola decimale e' il modo naturale in cui si scrive un numero in
    italiano, e le impostazioni le scrive una persona. Solleva se non e' un
    numero: qui serve accorgersene, non ripiegare.
    """

    if isinstance(valore, bool):
        raise ValueError("un sì/no non è un numero")
    if isinstance(valore, (int, float)):
        return float(valore)
    return float(str(valore).strip().replace(",", "."))


def _intero_stretto(valore: Any) -> int:
    numero = _decimale(valore)
    if numero != int(numero):
        raise ValueError("non è un numero intero")
    return int(numero)


def _testo_non_vuoto(valore: Any) -> str:
    testo = str(valore).strip()
    if not testo:
        raise ValueError("è vuoto")
    return testo


# Ogni voce della configurazione ha il suo convertitore e il suo minimo. Un
# valore che non si converte **non diventa zero**: torna il predefinito, e lo
# dice. Un tetto di spesa che vale zero e' indistinguibile da «non chiamare
# mai», ed e' il guasto peggiore che questa fase possa avere, perche' non si
# vede: ogni caso torna `TETTO_SPESA`, tutti i prodotti diventano
# `DA_VERIFICARE` e il messaggio all'utente gli ripete il numero che credeva di
# aver impostato.
TIPI_CONFIGURAZIONE: dict[str, tuple[Callable[[Any], Any], Any]] = {
    "model": (_testo_non_vuoto, None),
    "base_url": (_testo_non_vuoto, None),
    "versione_prompt": (_testo_non_vuoto, None),
    "versione_avversario": (_testo_non_vuoto, None),
    "max_tokens": (_intero_stretto, 1),
    "temperature": (_decimale, 0.0),
    "timeout_secondi": (_decimale, 1.0),
    "tetto_spesa_usd": (_decimale, 0.0),
    "tetto_chiamate": (_intero_stretto, 1),
    "parallelismo": (_intero_stretto, 1),
}


def _convalida_configurazione(configurazione: dict, *, da_dove: str) -> dict:
    """Converte e controlla ogni voce, e per quelle sbagliate usa il predefinito.

    Torna sempre una configurazione utilizzabile: e' il punto in cui un valore
    scritto male viene fermato, invece di diventare uno zero silenzioso a metà
    di una run.
    """

    pulita = dict(configurazione)
    for nome, (converti, minimo) in TIPI_CONFIGURAZIONE.items():
        if nome not in pulita:
            continue
        predefinito = CONFIGURAZIONE_PREDEFINITA[nome]
        try:
            valore = converti(pulita[nome])
        except (TypeError, ValueError) as errore:
            print(
                f"[AVVISO] {da_dove}: «{nome}» vale {pulita[nome]!r} e non è utilizzabile "
                f"({errore}). Uso il predefinito {predefinito!r}."
            )
            pulita[nome] = predefinito
            continue
        if minimo is not None and valore < minimo:
            print(
                f"[AVVISO] {da_dove}: «{nome}» vale {valore!r}, sotto il minimo {minimo!r}. "
                f"Uso il predefinito {predefinito!r}."
            )
            pulita[nome] = predefinito
            continue
        pulita[nome] = valore
    return pulita


def _stato_http(codice: int) -> str:
    """Il nome dello stato per un codice HTTP dichiarato dal servizio."""

    if 500 <= codice <= 599:
        return "HTTP_5xx"
    return f"HTTP_{codice}"


def _accorcia(testo: Any, quanti: int = 300) -> str:
    testo = str(testo or "").strip()
    testo = " ".join(testo.split())
    if len(testo) <= quanti:
        return testo
    return testo[:quanti] + "…"


# ----------------------------------------------------------------------------
# Chiave, configurazione, prompt
# ----------------------------------------------------------------------------


def leggi_chiave(percorso_secrets: Path | None = None) -> str | None:
    """La chiave OpenRouter: prima l'ambiente, poi `app/data/secrets.json`.

    Nel file la chiave sta **annidata** in `openrouter.api_key`. Cercarla al
    primo livello torna `None` e poi HTTP 401, mentre `GET /api/v1/models`
    continua a rispondere perche' e' pubblico: e' una trappola gia' costata un
    giro di misure.
    """

    dall_ambiente = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if dall_ambiente:
        return dall_ambiente

    percorso = percorso_secrets if percorso_secrets is not None else PERCORSO_SECRETS
    try:
        dati = json.loads(Path(percorso).read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(dati, dict):
        return None
    sezione = dati.get("openrouter")
    if not isinstance(sezione, dict):
        return None
    chiave = str(sezione.get("api_key") or "").strip()
    return chiave or None


def stato_chiave(percorso_secrets: Path | None = None) -> dict:
    """Se la chiave c'e' e da dove arriva, **senza mai restituirne il valore**.

    E' l'unica cosa che il server puo' mandare al browser: la pagina non deve
    poterla rileggere, nemmeno per riempire un campo. La coda di quattro
    caratteri serve solo a distinguere due chiavi diverse a occhio, e quattro
    caratteri non ricostruiscono niente."""

    dall_ambiente = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if dall_ambiente:
        return {"presente": True, "origine": "ambiente", "coda": dall_ambiente[-4:]}
    chiave = leggi_chiave(percorso_secrets)
    if chiave:
        return {"presente": True, "origine": "file", "coda": chiave[-4:]}
    return {"presente": False, "origine": "", "coda": ""}


def salva_chiave(chiave: str, percorso_secrets: Path | None = None) -> None:
    """Scrive la chiave in `app/data/secrets.json` **conservando il resto**.

    Il file esiste gia' e puo' contenere altro: si rilegge e si sostituisce la
    sola voce annidata `openrouter.api_key`. Si scrive prima accanto e poi si
    rinomina, perche' un'interruzione a meta' lascerebbe l'utente senza chiave
    e senza saperlo.

    ⚠ I permessi si stringono **prima** di scriverci dentro, e adesso e' vero:
    il file nasce con `os.open(..., 0o600)`.  Fino al 20 agosto 2026 questa
    riga del docstring diceva il contrario di quello che il codice faceva —
    `write_bytes` e poi `chmod` — cioe' la chiave toccava il disco con i
    permessi di umask e veniva ristretta un istante dopo.  Finestra stretta e
    macchina a utente singolo, ma un docstring che promette una difesa che non
    c'e' e' peggio della difesa mancante: chi legge smette di guardare."""

    valore = str(chiave or "").strip()
    if not valore:
        raise ValueError("La chiave è vuota: non si salva una chiave vuota.")

    percorso = Path(percorso_secrets if percorso_secrets is not None else PERCORSO_SECRETS)
    percorso.parent.mkdir(parents=True, exist_ok=True)
    try:
        dati = json.loads(percorso.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        dati = {}
    if not isinstance(dati, dict):
        dati = {}
    sezione = dati.get("openrouter")
    dati["openrouter"] = {**sezione, "api_key": valore} if isinstance(sezione, dict) else {"api_key": valore}

    # Il temporaneo porta processo e filo come tutte le altre scritture del
    # programma: con un nome fisso due salvataggi in volo si contendono lo
    # stesso file.
    accanto = percorso.with_name(f"{percorso.name}.{os.getpid()}.{threading.get_ident()}.nuovo")
    testo = (json.dumps(dati, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        # `0o600` al momento della creazione: su Windows il modo non significa
        # granche', ma li' non c'e' nemmeno il problema che chiude.
        descrittore = os.open(accanto, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descrittore, "wb") as flusso:
            flusso.write(testo)
            flusso.flush()
            os.fsync(flusso.fileno())
        os.replace(accanto, percorso)
    except Exception:
        try:
            accanto.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def salva_impostazioni(valori: dict, percorso_impostazioni: Path | None = None) -> dict:
    """Scrive `app/data/impostazioni_ai.json` e restituisce la configurazione viva.

    Accetta **solo** i nomi che esistono in `CONFIGURAZIONE_PREDEFINITA`: una
    voce sconosciuta verrebbe ignorata in lettura, e salvarla lascerebbe
    all'utente un file che sembra dire una cosa che il programma non fa.
    In particolare la chiave non passa mai di qui — quella ha il suo posto.

    Ogni valore passa dagli stessi convertitori della lettura, quindi un
    tetto di spesa scritto `3,0` all'italiana non azzera niente: viene
    rifiutato subito, con il ripiego sul predefinito, invece di spegnere la
    fase AI dicendo all'utente il numero che credeva di aver impostato."""

    percorso = Path(percorso_impostazioni if percorso_impostazioni is not None else PERCORSO_IMPOSTAZIONI)
    try:
        gia_scritte = json.loads(percorso.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        gia_scritte = {}
    if not isinstance(gia_scritte, dict):
        gia_scritte = {}

    ignorate = sorted(set(valori) - set(CONFIGURAZIONE_PREDEFINITA))
    if ignorate:
        raise ValueError(
            "Impostazioni sconosciute: " + ", ".join(ignorate)
            + ". Le voci valide sono: " + ", ".join(sorted(CONFIGURAZIONE_PREDEFINITA))
        )

    da_scrivere = {**gia_scritte, **{nome: valore for nome, valore in valori.items() if valore is not None}}

    # Si convalida **prima** di scrivere, e qui un valore rifiutato dev'essere
    # un errore, non un ripiego. `_convalida_configurazione` avvisa e ripiega —
    # ed e' la cosa giusta all'avvio, dove spegnere il programma per una voce
    # storta sarebbe peggio. In salvataggio no: l'utente ha appena scritto quel
    # numero e sta guardando lo schermo. Scriverlo nel file e poi ignorarlo e'
    # il difetto misurato nella 5a — `tetto_spesa_usd: "3,0"` con la virgola
    # italiana azzerava il tetto **dicendo all'utente il numero che credeva di
    # aver impostato**.
    convalidata = _convalida_configurazione(
        {**CONFIGURAZIONE_PREDEFINITA, **da_scrivere}, da_dove="impostazioni AI in salvataggio"
    )
    rifiutate = [
        nome for nome, valore in valori.items()
        if valore is not None and convalidata.get(nome) != valore and str(convalidata.get(nome)) != str(valore)
    ]
    if rifiutate:
        dettagli = ", ".join(
            f"«{nome}»: {valori[nome]!r} non è utilizzabile" for nome in sorted(rifiutate)
        )
        raise ValueError(
            f"{dettagli}. Il valore non è stato salvato. "
            "Attenzione al separatore decimale: i numeri si scrivono con il punto (3.0), non con la virgola."
        )

    # Una versione di prompt che non esiste passa la convalida — e' una stringa
    # non vuota — e poi fa sollevare il client al primo `ClientAI(...)`, cioe'
    # lontano da qui e a fase gia' avviata. Si prova subito: leggerlo costa
    # niente ed e' l'unico momento in cui c'e' qualcuno che guarda.
    for nome in ("versione_prompt", "versione_avversario"):
        if nome not in valori:
            continue
        radice = "valuta_candidati" if nome == "versione_prompt" else "verifica_avversariale"
        try:
            leggi_prompt(str(da_scrivere[nome]), radice)
        except (FileNotFoundError, ValueError) as errore:
            raise ValueError(f"«{nome}»: {errore}") from errore

    scrittura_sicura.scrivi_json(percorso, da_scrivere, a_capo_finale=True)
    return carica_configurazione(percorso)


def elenco_modelli(base_url: str | None = None, timeout: float = 20.0) -> list[dict]:
    """L'elenco dei modelli di OpenRouter, per il menu' della pagina.

    Sola libreria standard, e **GET**: `trasporto_urllib` e' cablato su POST con
    un corpo JSON e qui non serve.

    ⚠ Questa chiamata **e' pubblica**: risponde anche senza chiave, e anche con
    una chiave scaduta. Un menu' che si popola non e' una prova che la chiave
    valga: quella si prova solo chiamando il modello. Chi usa questa funzione
    non deve raccontare il contrario all'utente.

    Gli alias non chiamabili — quelli che nell'elenco hanno la **tilde** davanti
    — restano fuori: `deepseek/deepseek-v4-flash-latest` e' costato un giro di
    misure e due documenti sbagliati proprio perche' era nell'elenco."""

    url = str(base_url or CONFIGURAZIONE_PREDEFINITA["base_url"]).rstrip("/") + "/models"
    richiesta = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(richiesta, timeout=timeout) as risposta:
        documento = json.loads(risposta.read().decode("utf-8"))
    voci = documento.get("data") if isinstance(documento, dict) else None
    modelli = []
    for voce in voci or []:
        if not isinstance(voce, dict):
            continue
        identificativo = str(voce.get("id") or "")
        if not identificativo or identificativo.startswith("~"):
            continue
        prezzi = voce.get("pricing") if isinstance(voce.get("pricing"), dict) else {}
        modelli.append({
            "id": identificativo,
            "nome": str(voce.get("name") or identificativo),
            "prezzo_ingresso": _numero(prezzi.get("prompt"), 0.0),
            "prezzo_uscita": _numero(prezzi.get("completion"), 0.0),
        })
    modelli.sort(key=lambda voce: voce["id"])
    return modelli


def carica_configurazione(percorso_impostazioni: Path | None = None) -> dict:
    """Predefiniti, poi `app/data/impostazioni_ai.json`, poi `OPENROUTER_MODEL`.

    Il file puo' non esistere: e' il caso normale oggi e non e' un errore.
    Nessun model id e' cablato nella logica: sta solo in
    `CONFIGURAZIONE_PREDEFINITA`, in un posto solo.
    """

    configurazione = dict(CONFIGURAZIONE_PREDEFINITA)

    percorso = percorso_impostazioni if percorso_impostazioni is not None else PERCORSO_IMPOSTAZIONI
    percorso = Path(percorso)
    if percorso.exists():
        try:
            dati = json.loads(percorso.read_bytes().decode("utf-8"))
        except (OSError, ValueError) as errore:
            print(f"[AVVISO] impostazioni AI illeggibili — uso i valori predefiniti: {errore}")
            dati = {}
        if isinstance(dati, dict):
            for nome, valore in dati.items():
                if nome in CONFIGURAZIONE_PREDEFINITA and valore is not None:
                    configurazione[nome] = valore

    dall_ambiente = (os.environ.get("OPENROUTER_MODEL") or "").strip()
    if dall_ambiente:
        configurazione["model"] = dall_ambiente

    return _convalida_configurazione(configurazione, da_dove=f"impostazioni AI ({percorso})")


_prompt_in_memoria: dict[tuple[str, str], str] = {}
_lucchetto_prompt = threading.Lock()


def leggi_prompt(versione: str, nome: str = "valuta_candidati") -> str:
    """Il prompt versionato, letto da `references/prompts/`.

    Il file contiene il testo del prompt e nient'altro: quello che ci sta
    dentro e' esattamente quello che il modello riceve. `nome` sceglie la
    famiglia: `valuta_candidati` per il primo giro, `verifica_avversariale`
    per il secondo.
    """

    cartella = Path(CARTELLA_PROMPT)
    chiave_cache = (str(cartella), f"{nome}.{versione}")
    with _lucchetto_prompt:
        gia_letto = _prompt_in_memoria.get(chiave_cache)
    if gia_letto is not None:
        return gia_letto

    percorso = cartella / f"{nome}.{versione}.md"
    try:
        testo = percorso.read_bytes().decode("utf-8").strip()
    except OSError as errore:
        raise FileNotFoundError(
            f"Prompt della versione «{versione}» non trovato in {percorso}: {errore}"
        ) from errore
    if not testo:
        raise ValueError(f"Il prompt {percorso} è vuoto.")

    with _lucchetto_prompt:
        _prompt_in_memoria[chiave_cache] = testo
    return testo


# ----------------------------------------------------------------------------
# Il caso reso testo e la chiave della memoria
# ----------------------------------------------------------------------------


def caso_come_testo(caso: CasoValutazione) -> str:
    """Il caso come lo legge il modello. Misurato l'11 agosto, da non cambiare."""

    righe = [f"Articolo cercato: {caso.descrizione}", "", "Candidati del fornitore:"]
    for candidato in caso.candidati:
        righe.append(
            f"- source_row {candidato.source_row}: {candidato.description}"
            f" (punteggio {_numero(candidato.score):.2f})"
        )
    return "\n".join(righe)


def _caso_serializzato(caso: CasoValutazione) -> str:
    """Il caso in una forma stabile: stessi dati, sempre lo stesso testo."""

    return json.dumps(
        {
            "gestionale_source_row": caso.gestionale_source_row,
            "supplier": caso.supplier,
            "descrizione": caso.descrizione,
            "candidati": [
                {
                    "source_row": candidato.source_row,
                    "description": candidato.description,
                    "score": round(_numero(candidato.score), 6),
                }
                for candidato in caso.candidati
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def impronta_prompt(testo: str) -> str:
    """Le prime dodici cifre dell'impronta del **testo** di un prompt.

    Serve a chiudere un difetto trovato dalla revisione della 6b: la chiave
    della memoria sigillava l'**etichetta** della versione (`v3`), non quello
    che c'e' scritto dentro. Chi corregge `valuta_candidati.v3.md` senza
    rinominarlo continuava a ricevere le risposte del prompt vecchio, e il
    commento qui sotto — «se cambia uno qualunque di questi, la risposta vecchia
    non vale piu'» — era falso proprio sul campo che si cambia piu' spesso.
    Misurato dal revisore: stessa chiave prima e dopo aver riscritto il file."""

    return hashlib.sha256(str(testo).encode("utf-8")).hexdigest()[:12]


def chiave_memoria(
    modello: str,
    versione_prompt: str,
    versione_schema: str,
    caso: CasoValutazione,
) -> str:
    """La memoria e' indirizzata dal contenuto.

    Modello, versione del prompt, versione dello schema e caso: se cambia uno
    qualunque di questi, la risposta vecchia non vale piu'. `versione_prompt`
    porta anche l'impronta del testo del prompt (vedi `impronta_prompt`), quindi
    «versione» qui vuol dire davvero versione e non solo il nome del file.
    """

    impronta = hashlib.sha256()
    impronta.update(str(modello).encode("utf-8"))
    impronta.update(b"\x00")
    impronta.update(str(versione_prompt).encode("utf-8"))
    impronta.update(b"\x00")
    impronta.update(str(versione_schema).encode("utf-8"))
    impronta.update(b"\x00")
    impronta.update(_caso_serializzato(caso).encode("utf-8"))
    return impronta.hexdigest()


# ----------------------------------------------------------------------------
# Il trasporto vero
# ----------------------------------------------------------------------------


def trasporto_urllib(url: str, corpo: dict, intestazioni: dict, timeout: float) -> tuple[int, Any]:
    """La sola parte che tocca la rete: una POST, e niente altro.

    Torna `(codice HTTP, corpo decodificato)`. Un errore HTTP non e'
    un'eccezione: e' un codice di ritorno, perche' il corpo di un 400 o di un
    401 dice cosa e' successo. Un guasto di rete invece solleva, e il client lo
    traduce in `ERRORE_RETE`.
    """

    dati = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
    richiesta = urllib.request.Request(url, data=dati, headers=dict(intestazioni), method="POST")
    try:
        with urllib.request.urlopen(richiesta, timeout=timeout) as risposta:
            testo = risposta.read().decode("utf-8", errors="replace")
            return int(risposta.status), _decodifica(testo)
    except urllib.error.HTTPError as errore:
        testo = errore.read().decode("utf-8", errors="replace")
        return int(errore.code), _decodifica(testo)


def _decodifica(testo: str) -> Any:
    try:
        return json.loads(testo)
    except ValueError:
        return testo


# ----------------------------------------------------------------------------
# Il client
# ----------------------------------------------------------------------------


class ClientAI:
    """Chiede al modello di scegliere fra i candidati, e non si fida di niente.

    `trasporto=None` usa `trasporto_urllib`. `memoria=None` significa nessuna
    persistenza su disco (e' il modo dei test): la memoria in RAM resta attiva,
    perche' un caso gia' visto non si paga due volte nemmeno dentro una run.
    `chiave=None` cerca la chiave con `leggi_chiave()`; `chiave=""` dichiara che
    la chiave non c'e', ed e' come i test provano lo stato `SENZA_CHIAVE`.
    """

    def __init__(
        self,
        configurazione: dict,
        *,
        trasporto: Callable[..., tuple[int, Any]] | None = None,
        memoria: Path | str | None = None,
        chiave: str | None = None,
    ) -> None:
        self.configurazione = _convalida_configurazione(
            {**CONFIGURAZIONE_PREDEFINITA, **dict(configurazione or {})},
            da_dove="configurazione passata al client",
        )
        self._trasporto = trasporto if trasporto is not None else trasporto_urllib
        self._chiave = leggi_chiave() if chiave is None else (chiave or "")
        self._prompt = leggi_prompt(self.configurazione["versione_prompt"])
        # Si accende da se', la prima volta che un fornitore lo pretende, e
        # resta acceso per tutta la vita del client: vedi FIRMA_JSON_RICHIESTO.
        # Non serve un lucchetto — piu' thread possono scriverlo insieme, ma
        # scrivono tutti lo stesso `True`, e leggerlo un istante prima costa
        # una ripetizione, non un errore.
        self._formato_obbligatorio = False
        # Il prompt dell'avversario si legge solo quando serve: chi usa il
        # client per il primo giro soltanto non deve averlo.
        self._prompt_avversario_letto: str | None = None

        self._lucchetto = threading.Lock()
        self._chiamate = 0
        self._costo = 0.0
        self._impegnato = 0.0            # spesa prenotata dalle chiamate in volo
        self._costo_atteso = COSTO_ATTESO_INIZIALE
        self._per_stato: dict[str, int] = {}
        self._dalla_memoria = 0

        self._percorso_memoria = Path(memoria) if memoria is not None else None
        self._voci: dict[str, dict] = {}
        # Quante risposte nuove aspettano di essere scritte su disco, e se
        # siamo dentro un'infornata. Vedi `_scrivi_ricordo`.
        self._da_salvare = 0
        self._in_infornata = False
        # Chi vuole sapere a che punto e' un'infornata mette qui una funzione.
        # Serve alla barra di avanzamento dell'orchestratore: la fase dura due
        # minuti, e un programma che sta zitto per due minuti sembra rotto.
        # Resta `None` per tutti gli altri, e allora non costa niente.
        self.avanzamento: Callable[[int, int], None] | None = None
        if self._percorso_memoria is not None:
            self._voci = self._carica_memoria(self._percorso_memoria)

    # -- superficie pubblica -------------------------------------------------

    def valuta_candidati(self, caso: CasoValutazione) -> EsitoAI:
        """Valuta un caso. Non solleva mai per un guasto: torna uno stato."""

        _verifica_caso(caso)
        return self._registra(self._valuta(caso, usa_memoria=True))

    def valuta_molti(self, casi: Sequence[CasoValutazione]) -> list[EsitoAI]:
        """Valuta molti casi in parallelo, **nell'ordine dei casi in ingresso**.

        Un caso che fallisce non ferma gli altri: il suo fallimento e' un valore
        di ritorno come tutti. I casi malformati si scoprono prima di partire,
        cosi' un errore d'uso dell'API non spegne mezza infornata.
        """

        return self._infornata(casi, self._valuta_senza_sorprese)

    def _infornata(
        self, casi: Sequence[CasoValutazione], valuta: Callable[[CasoValutazione], EsitoAI]
    ) -> list[EsitoAI]:
        casi = list(casi)
        for caso in casi:
            _verifica_caso(caso)
        if not casi:
            return []

        with self._lucchetto:
            self._in_infornata = True
        try:
            return self._infornata_vera(casi, valuta)
        finally:
            with self._lucchetto:
                self._in_infornata = False
                # La coda delle risposte non ancora scritte va sul disco
                # adesso: e' la «fine» a cui `_scrivi_ricordo` si appoggia.
                if self._da_salvare:
                    self._salva_adesso()

    def _conta_mentre_valuta(
        self, casi: list[CasoValutazione], valuta: Callable[[CasoValutazione], EsitoAI]
    ) -> Callable[[CasoValutazione], EsitoAI]:
        """Avvolge la valutazione per dire a che punto siamo, se qualcuno ascolta.

        Il conteggio sta dentro il lucchetto del client perche' i thread sono
        trentadue: senza, `fatti += 1` perde colpi e la barra torna indietro.
        L'avviso all'ascoltatore si fa **fuori** dal lucchetto — chi ascolta
        scrive su disco, e tenerlo dentro serializzerebbe l'infornata.
        """

        ascoltatore = self.avanzamento
        if ascoltatore is None:
            return valuta
        totali = len(casi)
        fatti = 0

        def valuta_e_conta(caso: CasoValutazione) -> EsitoAI:
            nonlocal fatti
            try:
                return valuta(caso)
            finally:
                with self._lucchetto:
                    fatti += 1
                    quanti = fatti
                try:
                    ascoltatore(quanti, totali)
                except Exception:
                    # Un ascoltatore che si rompe non deve far cadere una run
                    # da due minuti: e' una barra di avanzamento.
                    pass

        return valuta_e_conta

    def _infornata_vera(
        self, casi: list[CasoValutazione], valuta: Callable[[CasoValutazione], EsitoAI]
    ) -> list[EsitoAI]:
        valuta = self._conta_mentre_valuta(casi, valuta)
        operai = max(1, min(_intero(self.configurazione["parallelismo"], 1), len(casi)))
        if operai == 1:
            return [valuta(caso) for caso in casi]

        # La prima chiamata vera si fa **da sola**, e non e' una cautela di
        # stile: prima di averne vista una non si sa quanto costi una chiamata
        # con il modello configurato, quindi la prenotazione della spesa lavora
        # su una stima e tutti i thread configurati possono superare insieme il
        # tetto — oggi sono trentadue, e domani sono quelli che c'e' scritto in
        # `parallelismo`, che e' una voce delle impostazioni. Fatta
        # la prima, `_costo_atteso` e' un numero vero e il tetto tiene. Costa un
        # viaggio di rete su una run di minuti, e in cambio un modello sbagliato
        # o una chiave scaduta si scoprono prima di lanciare 948 chiamate.
        esiti: list[EsitoAI] = []
        indice = 0
        while indice < len(casi) and self.contabilita["chiamate"] == 0:
            esiti.append(valuta(casi[indice]))
            indice += 1

        restanti = casi[indice:]
        # ⚠ E se la prima ha detto «questa chiave non la accetto», ci si ferma
        # qui. Il commento qui sopra prometteva gia' che «una chiave scaduta si
        # scopre prima di lanciare 948 chiamate», e la promessa non era
        # mantenuta: `_prenota_chiamata` conta la chiamata **prima** di farla,
        # quindi dopo la prima — anche fallita — il ciclo di riscaldamento
        # finiva e il pool partiva lo stesso. Novecento risposte identiche, un
        # rischio di limitazione, e la notizia alla fine invece che subito.
        if restanti and esiti and esiti[-1].stato in STATI_CHE_FERMANO_L_INFORNATA:
            fermata = esiti[-1]
            esiti.extend(
                self._costruisci(
                    STATO_NON_CHIESTO,
                    dettaglio=(
                        f"Non chiesto: la prima chiamata è tornata {fermata.stato} e "
                        "l'infornata si è fermata lì. Sistema la chiave o il credito in "
                        "Impostazioni e rifai il confronto."
                    ),
                )
                for _ in restanti
            )
            return esiti
        if restanti:
            with ThreadPoolExecutor(max_workers=operai) as esecutore:
                attese = [esecutore.submit(valuta, caso) for caso in restanti]
                esiti.extend(attesa.result() for attesa in attese)
        return esiti

    @property
    def _prompt_avversario(self) -> str:
        if self._prompt_avversario_letto is None:
            self._prompt_avversario_letto = leggi_prompt(
                self.configurazione["versione_avversario"], "verifica_avversariale"
            )
        return self._prompt_avversario_letto

    def _verifica(self, caso: CasoValutazione, decisione: dict) -> EsitoAI:
        """Il secondo giro: un solo candidato, quello proposto, e istruzioni
        rovesciate. Il caso e' un `CasoValutazione` con la sola riga scelta,
        cosi' il controllo della shortlist vale anche qui."""

        scelta = next(
            (c for c in caso.candidati if c.source_row == decisione.get("source_row")), None
        )
        if scelta is None:  # non ci si arriva: il controllo 6 l'ha gia' escluso
            return self._costruisci(
                STATO_RIGA_FUORI_SHORTLIST,
                dettaglio="La riga da verificare non è fra i candidati.",
            )
        solo_quella = CasoValutazione(
            gestionale_source_row=caso.gestionale_source_row,
            supplier=caso.supplier,
            descrizione=caso.descrizione,
            candidati=(scelta,),
        )
        return self._valuta(
            solo_quella,
            usa_memoria=True,
            prompt=self._prompt_avversario,
            etichetta_prompt=f"avversario:{self.configurazione['versione_avversario']}",
        )

    def _verifica_senza_sorprese(self, caso: CasoValutazione) -> EsitoAI:
        try:
            return self.valuta_con_verifica(caso)
        except Exception as errore:  # noqa: BLE001 — vedi _valuta_senza_sorprese
            return self._registra(
                self._costruisci(
                    STATO_ERRORE_INTERNO,
                    dettaglio=(
                        f"Guasto imprevisto verificando la riga {caso.gestionale_source_row} "
                        f"/ {caso.supplier} ({type(errore).__name__}): {self._pulito(errore)}"
                    ),
                )
            )

    def _valuta_senza_sorprese(self, caso: CasoValutazione) -> EsitoAI:
        """La rete di sicurezza dell'infornata: un caso rotto non porta via gli altri.

        Senza questa, un'eccezione imprevista su un solo caso risale da
        `attesa.result()` e butta via **tutti** i risultati gia' calcolati e gia'
        pagati degli altri: su una run da 948 casi si torna con un'eccezione al
        posto di 947 esiti buoni. «Un caso che fallisce non ferma gli altri»
        deve valere anche per un'eccezione, non solo per uno stato.
        """

        try:
            return self.valuta_candidati(caso)
        except Exception as errore:  # noqa: BLE001 — e' esattamente il punto
            return self._registra(
                self._costruisci(
                    STATO_ERRORE_INTERNO,
                    dettaglio=(
                        f"Guasto imprevisto valutando la riga {caso.gestionale_source_row} "
                        f"/ {caso.supplier} ({type(errore).__name__}): {self._pulito(errore)}"
                    ),
                )
            )

    def valuta_con_verifica(self, caso: CasoValutazione) -> EsitoAI:
        """Il primo giro, e su ogni `ACCEPT` un secondo giro che prova a demolirlo.

        E' quello che rende `ALTA` degno di fiducia, ed e' la ragione per cui gli
        `ALTA` si possono accettare senza chiedere niente a schermo. Misurato
        l'11 agosto 2026 su 150 casi del banco, con il prompt `v3`:

        | | ALTA sbagliati | ACCEPT giusti |
        |---|---|---|
        | solo primo giro | 3 | 43 |
        | con la verifica | **0** | 40 |

        Se i due giri non concordano il caso **non diventa una domanda a
        schermo**: la decisione scende a `UNRESOLVED`, cioe' `DA_VERIFICARE`,
        e finisce nell'elenco che il revisore guarda con l'ordine davanti.
        Costa una chiamata in piu' sui soli `ACCEPT`, cioe' meno di un terzo dei
        casi: sul banco, 0,004 $ ogni 150.
        """

        esito = self.valuta_candidati(caso)
        decisione = esito.decisione
        if not decisione or decisione.get("action") != "ACCEPT":
            return esito

        controllo = self._registra(self._verifica(caso, decisione))
        if controllo.stato in (STATO_OK, STATO_DALLA_MEMORIA):
            verdetto = controllo.decisione or {}
            if verdetto.get("action") == "ACCEPT":
                return _con_dettaglio(
                    esito, f"{esito.dettaglio} Verifica avversariale: confermato."
                )
            motivo_contrario = verdetto.get("rationale", "")
        else:
            motivo_contrario = f"la verifica non è arrivata ({controllo.stato})"

        # I due giri non concordano: non si sceglie chi ha ragione, si passa la
        # mano al revisore. Un ACCEPT non confermato vale meno di niente.
        declassata = {
            **decisione,
            "action": "UNRESOLVED",
            "source_row": None,
            "confidence": "MEDIA",
            "rationale": (
                f"{decisione.get('rationale', '')} — ma la verifica avversariale non "
                f"conferma: {motivo_contrario}"
            ).strip(" —"),
            "requires_user_confirmation": True,
        }
        return replace(
            esito,
            decisione=declassata,
            stato=esito.stato,
            costo_usd=round(esito.costo_usd + controllo.costo_usd, 10),
            tentativi=esito.tentativi + controllo.tentativi,
            dettaglio=f"{esito.dettaglio} Verifica avversariale: non confermato, sceso a DA_VERIFICARE.",
        )

    def valuta_molti_con_verifica(self, casi: Sequence[CasoValutazione]) -> list[EsitoAI]:
        """Come `valuta_molti`, ma con la verifica avversariale su ogni ACCEPT."""

        return self._infornata(casi, self._verifica_senza_sorprese)

    def prova_connessione(self) -> EsitoAI:
        """Una chiamata vera su un caso finto: chiave, model id e schema insieme."""

        return self._registra(self._valuta(CASO_DI_PROVA, usa_memoria=False))

    @property
    def contabilita(self) -> dict:
        """Quante chiamate, quanto speso, con quali esiti, quante dalla memoria.

        `chiamate` conta le richieste HTTP davvero partite, ripetizioni comprese:
        e' quello che il tetto deve trattenere.
        """

        with self._lucchetto:
            return {
                "chiamate": self._chiamate,
                "costo_usd": round(self._costo, 8),
                "per_stato": dict(self._per_stato),
                "dalla_memoria": self._dalla_memoria,
            }

    # -- il giro di una valutazione -----------------------------------------

    def _valuta(
        self,
        caso: CasoValutazione,
        *,
        usa_memoria: bool,
        prompt: str | None = None,
        etichetta_prompt: str | None = None,
    ) -> EsitoAI:
        inizio = time.monotonic()
        prompt = prompt if prompt is not None else self._prompt
        etichetta_prompt = etichetta_prompt or str(self.configurazione["versione_prompt"])

        chiave_ricordo = None
        if usa_memoria:
            chiave_ricordo = chiave_memoria(
                self.configurazione["model"],
                # L'etichetta **e** il testo: un prompt corretto senza
                # rinominarlo deve invalidare le risposte vecchie.
                f"{etichetta_prompt}:{impronta_prompt(prompt)}",
                VERSIONE_SCHEMA,
                caso,
            )
            ricordo = self._leggi_ricordo(chiave_ricordo)
            if ricordo is not None:
                ricordata = ricordo.get("decisione")
                problema = decisione_non_utilizzabile(caso, ricordata)
                if problema:
                    # Una voce alterata non deve poter diventare una decisione:
                    # si butta e si richiama, come se non ci fosse mai stata.
                    print(
                        f"[AVVISO] memoria AI: voce scartata per la riga "
                        f"{caso.gestionale_source_row} / {caso.supplier} — {problema}. "
                        "Il caso viene richiesto al modello."
                    )
                    self._dimentica(chiave_ricordo)
                else:
                    return self._costruisci(
                        STATO_DALLA_MEMORIA,
                        decisione=dict(ricordata),
                        modello=ricordo.get("modello"),
                        fornitore=ricordo.get("fornitore_calcolo"),
                        durata=time.monotonic() - inizio,
                        dettaglio="Caso già valutato: risposta ripresa dalla memoria, nessuna chiamata.",
                    )

        if not self._chiave:
            return self._costruisci(
                STATO_SENZA_CHIAVE,
                durata=time.monotonic() - inizio,
                dettaglio=(
                    "Nessuna chiave OpenRouter: né la variabile d'ambiente "
                    "OPENROUTER_API_KEY né il campo openrouter.api_key di "
                    "app/data/secrets.json."
                ),
            )

        ultimo: EsitoAI | None = None
        costo_accumulato = 0.0
        for tentativo in (1, 2):
            trattenuto = self._prenota_chiamata()
            if trattenuto is not None:
                if ultimo is None:
                    return self._costruisci(
                        trattenuto,
                        durata=time.monotonic() - inizio,
                        dettaglio=_motivo_del_tetto(trattenuto, self.configurazione),
                    )
                return _con_dettaglio(
                    ultimo,
                    f"{ultimo.dettaglio} Ripetizione non fatta: {_motivo_del_tetto(trattenuto, self.configurazione)}",
                )

            esito = self._una_chiamata(caso, tentativo=tentativo, inizio=inizio, prompt=prompt)
            # Il primo tentativo si paga anche quando fallisce — una risposta
            # troncata ha consumato e fatturato i token di ragionamento — quindi
            # l'esito porta la somma, non l'ultimo importo: chi somma gli esiti
            # per fare un rapporto deve trovare la spesa vera.
            costo_accumulato += esito.costo_usd
            esito = _con_costo(esito, costo_accumulato)
            if esito.stato == STATO_OK:
                if chiave_ricordo is not None:
                    self._scrivi_ricordo(chiave_ricordo, esito)
                return esito
            ultimo = esito
            if esito.stato not in STATI_RIPETIBILI or tentativo == 2:
                return esito
            if esito.stato in STATI_CON_PAUSA and PAUSA_RIPETIZIONE_S > 0:
                time.sleep(PAUSA_RIPETIZIONE_S)

        return ultimo  # non ci si arriva: il ciclo torna sempre prima

    def _una_chiamata(
        self,
        caso: CasoValutazione,
        *,
        tentativo: int,
        inizio: float,
        prompt: str | None = None,
    ) -> EsitoAI:
        url = str(self.configurazione["base_url"]).rstrip("/") + "/chat/completions"
        intestazioni = {
            "Authorization": f"Bearer {self._chiave}",
            "Content-Type": "application/json",
        }
        corpo = {
            "model": self.configurazione["model"],
            "messages": [
                {
                    "role": "system",
                    "content": (prompt if prompt is not None else self._prompt)
                    + ("\n\n" + ISTRUZIONE_FORMATO if self._formato_obbligatorio else ""),
                },
                {"role": "user", "content": caso_come_testo(caso)},
            ],
            "temperature": self.configurazione["temperature"],
            "max_tokens": self.configurazione["max_tokens"],
            "response_format": SCHEMA_RISPOSTA,
        }

        try:
            codice, risposta = self._trasporto(
                url, corpo, intestazioni, _numero(self.configurazione["timeout_secondi"], 60.0)
            )
        except GUASTI_DI_RETE as errore:
            # Rete, DNS, timeout, corpo indecifrabile: sono guasti, non eccezioni
            # per chi chiama, e ha senso ritentarli.
            self._chiudi_prenotazione(0.0)
            return self._costruisci(
                STATO_ERRORE_RETE,
                tentativi=tentativo,
                durata=time.monotonic() - inizio,
                dettaglio=f"Chiamata non riuscita ({type(errore).__name__}): {self._pulito(errore)}",
            )
        except Exception as errore:  # noqa: BLE001 — vedi STATO_ERRORE_TRASPORTO
            # Un KeyError, un AssertionError, un MemoryError dentro un trasporto
            # iniettato non sono la rete che va male: sono un difetto. Non si
            # ripetono e non si mescolano ai guasti di rete nella contabilita'.
            self._chiudi_prenotazione(0.0)
            return self._costruisci(
                STATO_ERRORE_TRASPORTO,
                tentativi=tentativo,
                durata=time.monotonic() - inizio,
                dettaglio=(
                    f"Guasto imprevisto del trasporto ({type(errore).__name__}): "
                    f"{self._pulito(errore)}. Non è la rete: non si ripete."
                ),
            )

        # La prenotazione va chiusa **comunque**, anche se l'interpretazione
        # solleva o se il codice non e' un numero: una prenotazione che resta
        # aperta non torna mai giu', e dopo qualche caso ogni valutazione
        # comincia a rispondere TETTO_SPESA con il tetto quasi intatto. La run
        # si spegnerebbe da sola, in silenzio, e tutti i prodotti rimasti
        # diventerebbero DA_VERIFICARE senza che niente lo dica.
        esito = None
        try:
            esito = self._interpreta(caso, int(codice), risposta, tentativo=tentativo, inizio=inizio)
            return esito
        finally:
            self._chiudi_prenotazione(esito.costo_usd if esito is not None else 0.0)

    # -- i controlli, nell'ordine del contratto ------------------------------

    def _interpreta(
        self,
        caso: CasoValutazione,
        codice: int,
        risposta: Any,
        *,
        tentativo: int,
        inizio: float,
    ) -> EsitoAI:
        corpo = risposta if isinstance(risposta, dict) else {}
        # `or {}` non basta: copre un usage falso (null, {}, 0) ma non un usage
        # vero di forma sbagliata, e un `.get` su una stringa solleva. Qui una
        # sola risposta deforme farebbe saltare l'intera infornata.
        uso = corpo.get("usage") if isinstance(corpo.get("usage"), dict) else {}
        costo = _numero(uso.get("cost"), 0.0)
        comune = {
            "modello": corpo.get("model"),
            "fornitore": corpo.get("provider"),
            "costo": costo,
            "token_ingresso": _intero(uso.get("prompt_tokens")),
            "token_uscita": _intero(uso.get("completion_tokens")),
            "tentativi": tentativo,
        }

        def esito(stato: str, *, decisione: dict | None = None, dettaglio: str = "") -> EsitoAI:
            return self._costruisci(
                stato,
                decisione=decisione,
                durata=time.monotonic() - inizio,
                dettaglio=dettaglio,
                **comune,
            )

        # 1. HTTP diverso da 200.
        if codice != 200:
            # Il fornitore che pretende la parola «json» nei messaggi: non e' un
            # rifiuto e non e' un model id sbagliato, e' una convenzione sua.
            # Si accende l'aggiunta e si ripete — una volta sola, e da qui in
            # avanti per tutta l'infornata, altrimenti 948 casi pagherebbero
            # 948 primi tentativi buttati.
            if codice == 400 and _chiede_la_parola_json(risposta):
                self._formato_obbligatorio = True
                return esito(
                    STATO_JSON_RICHIESTO,
                    dettaglio=(
                        "Il fornitore di calcolo pretende la parola «json» nel messaggio "
                        "per accettare una risposta strutturata: aggiunta, e la chiamata "
                        "si ripete."
                    ),
                )
            return esito(
                _stato_http(codice),
                dettaglio=f"Il servizio ha risposto {codice}: {self._pulito(_messaggio_di_errore(risposta))}",
            )

        # Un errore dichiarato dentro un 200: capita quando il guasto arriva a
        # risposta gia' aperta. Vale come l'errore HTTP che dichiara, **anche se
        # insieme arriva un `choices` pieno**: se il servizio dichiara un
        # guasto, l'esito e' quel guasto. Nel caso raro in cui il contenuto
        # fosse comunque completo si paga una ripetizione, che e' il verso
        # giusto in cui sbagliare.
        errore_nel_corpo = corpo.get("error")
        if isinstance(errore_nel_corpo, dict):
            codice_interno = _intero(errore_nel_corpo.get("code"), 0)
            stato = _stato_http(codice_interno) if codice_interno else STATO_CONTENUTO_VUOTO
            return esito(
                stato,
                dettaglio=f"Errore dichiarato nel corpo della risposta: {self._pulito(_messaggio_di_errore(corpo))}",
            )

        scelte = corpo.get("choices")
        scelta = scelte[0] if isinstance(scelte, list) and scelte and isinstance(scelte[0], dict) else {}
        motivo_fine = scelta.get("finish_reason") or scelta.get("native_finish_reason")
        messaggio = scelta.get("message") if isinstance(scelta.get("message"), dict) else {}
        contenuto = messaggio.get("content")

        # 2. La trappola misurata: il tetto dei token consumato a ragionare.
        if motivo_fine == "length":
            return esito(
                STATO_TRONCATA,
                dettaglio=(
                    "Risposta troncata (finish_reason «length»): il modello ha "
                    f"esaurito i {self.configurazione['max_tokens']} token del tetto. "
                    "Non è un rifiuto: il caso resta da verificare."
                ),
            )

        # 3. Contenuto assente, non stringa o vuoto: mai un REJECT.
        if not isinstance(contenuto, str) or not contenuto.strip():
            return esito(
                STATO_CONTENUTO_VUOTO,
                dettaglio="Il servizio ha risposto 200 ma senza testo utile: nessuna decisione.",
            )

        # 4. Contenuto non JSON.
        try:
            grezza = json.loads(contenuto)
        except ValueError:
            return esito(
                STATO_NON_E_JSON,
                dettaglio=f"Risposta non in JSON: {self._pulito(contenuto, 160)}",
            )

        # 5. Chiavi o valori fuori schema.
        problema = _fuori_schema(grezza)
        if problema:
            return esito(
                STATO_SCHEMA_NON_CONFORME,
                dettaglio=f"Risposta fuori schema: {problema}",
            )

        azione = grezza["azione"]
        source_row = grezza["source_row"]
        righe_ammesse = {candidato.source_row for candidato in caso.candidati}

        # 6. Una riga che non era fra i candidati: si scarta, non si discute.
        if azione == "ACCEPT" and source_row not in righe_ammesse:
            return esito(
                STATO_RIGA_FUORI_SHORTLIST,
                dettaglio=(
                    f"Proposta la source_row {source_row}, che non è fra i candidati "
                    f"passati ({sorted(righe_ammesse)}): scartata."
                ),
            )

        # 7. Una riga su un rifiuto non vuol dire niente: si azzera.
        if azione != "ACCEPT" and source_row is not None:
            source_row = None

        decisione = {
            "gestionale_source_row": caso.gestionale_source_row,
            "supplier": caso.supplier,
            "action": azione,
            "source_row": source_row,
            "confidence": grezza["confidenza"],
            "rationale": str(grezza["motivo"]),
            # Resta `true`: accettare gli ALTA senza conferma tocca
            # merge_match_decisions.py e build_review_data.py, non la 5a.
            "requires_user_confirmation": True,
        }
        # Ultima rete, la stessa che attraversa una decisione ripresa dalla
        # memoria: cosi' il controllo sta davvero in un posto solo e le due
        # strade non possono divergere.
        problema = decisione_non_utilizzabile(caso, decisione)
        if problema:
            return esito(
                STATO_SCHEMA_NON_CONFORME,
                dettaglio=f"Decisione costruita ma non utilizzabile: {problema}",
            )
        return esito(STATO_OK, decisione=decisione, dettaglio="Risposta valida e validata.")

    # -- contabilita', tetti, memoria ---------------------------------------

    def _prenota_chiamata(self) -> str | None:
        """Verifica i tetti e prenota chiamata **e spesa**. Torna chi trattiene.

        Il controllo e la prenotazione stanno nella stessa sezione protetta: con
        `parallelismo` thread — trentadue, oggi — controllare e poi
        incrementare farebbe sforare il tetto di altrettante chiamate.

        Si prenota anche la spesa, non solo la chiamata: il costo vero si sa
        solo quando la risposta torna, quindi senza prenotazione N thread
        possono superare il controllo tutti insieme prima che uno qualunque
        abbia aggiunto il suo costo, e il tetto salta di N chiamate. Si mette da
        parte il costo piu' alto visto finora (all'inizio una stima prudente) e
        lo si restituisce quando arriva il costo vero.

        Il ripiego dei due tetti e' il **predefinito**, mai zero: un tetto che
        vale zero spegne tutta la fase senza dirlo.
        """

        with self._lucchetto:
            tetto_spesa = _numero(
                self.configurazione["tetto_spesa_usd"],
                float(CONFIGURAZIONE_PREDEFINITA["tetto_spesa_usd"]),
            )
            tetto_chiamate = _intero(
                self.configurazione["tetto_chiamate"],
                int(CONFIGURAZIONE_PREDEFINITA["tetto_chiamate"]),
            )
            if self._costo + self._impegnato >= tetto_spesa:
                return STATO_TETTO_SPESA
            if self._chiamate >= tetto_chiamate:
                return STATO_TETTO_CHIAMATE
            self._chiamate += 1
            self._impegnato += self._costo_atteso
            return None

    def _chiudi_prenotazione(self, costo: float) -> None:
        """Il costo vero prende il posto della stima messa da parte."""

        with self._lucchetto:
            self._impegnato = max(0.0, self._impegnato - self._costo_atteso)
            self._costo += costo
            if costo > self._costo_atteso:
                self._costo_atteso = costo

    def _registra(self, esito: EsitoAI) -> EsitoAI:
        with self._lucchetto:
            self._per_stato[esito.stato] = self._per_stato.get(esito.stato, 0) + 1
            if esito.stato == STATO_DALLA_MEMORIA:
                self._dalla_memoria += 1
        return esito

    def _leggi_ricordo(self, chiave: str) -> dict | None:
        """La voce, solo se ha la forma minima. Chi chiama la valida comunque."""

        with self._lucchetto:
            voce = self._voci.get(chiave)
            if not isinstance(voce, dict) or not isinstance(voce.get("decisione"), dict):
                return None
            return dict(voce)

    def _dimentica(self, chiave: str) -> None:
        """Toglie una voce guasta, così non la si rilegge a ogni giro."""

        with self._lucchetto:
            self._voci.pop(chiave, None)

    def _scrivi_ricordo(self, chiave: str, esito: EsitoAI) -> None:
        """Solo le risposte valide entrano in memoria.

        Un fallimento non si memorizza: la run dopo deve poterlo ritentare.

        ⚠ **Dentro un'infornata il file non si riscrive a ogni risposta.**
        Salvare significa rileggere, fondere e riscrivere **tutto** il file, e
        il costo cresce con la sua dimensione: misurato dalla revisione della
        6b, 948 casi con una memoria di 568 KB pagavano **~90 secondi**
        serializzati dentro il lucchetto — su una run che di rete ne impiega
        105 — e il file cresceva a 1408 KB, cioe' la settimana dopo sarebbe
        costata di piu'. Il parallelismo su quei 90 secondi non compra niente,
        perche' sono tutti dentro la stessa sezione protetta.

        Quindi: in RAM sempre, su disco ogni `RISPOSTE_FRA_DUE_SALVATAGGI` e
        una volta alla fine dell'infornata. Fuori da un'infornata si salva
        subito, com'era: chi valuta un caso solo non ha una «fine» a cui
        appoggiarsi. Il prezzo e' che una run interrotta perde al massimo un
        centinaio di risposte gia' pagate, e la memoria e' una cache: perderne
        un pezzo costa soldi la prossima volta, non correttezza.
        """

        voce = {
            "decisione": dict(esito.decisione or {}),
            "modello": esito.modello,
            "fornitore_calcolo": esito.fornitore_calcolo,
            "salvato_il": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with self._lucchetto:
            self._voci[chiave] = voce
            self._da_salvare += 1
            if self._percorso_memoria is None:
                return
            if self._in_infornata and self._da_salvare < RISPOSTE_FRA_DUE_SALVATAGGI:
                return
            self._salva_adesso()

    def _salva_adesso(self) -> None:
        """Riscrive il file della memoria. **Va chiamata con il lucchetto in mano.**

        Si rilegge il file e ci si fonde dentro: un secondo client (o una
        seconda run) che avesse scritto nel frattempo non deve sparire perche'
        noi riscriviamo la nostra copia in RAM. Le nostre voci vincono sulle sue
        a parita' di chiave, ma non le cancellano."""

        if self._percorso_memoria is None:
            return
        sul_disco = self._carica_memoria(self._percorso_memoria, in_silenzio=True)
        sul_disco.update(self._voci)
        self._voci = sul_disco
        self._salva_memoria(self._percorso_memoria, self._voci)
        self._da_salvare = 0

    @staticmethod
    def _carica_memoria(percorso: Path, *, in_silenzio: bool = False) -> dict[str, dict]:
        """Un file di memoria illeggibile è un avviso, non un errore."""

        if not percorso.exists():
            return {}
        try:
            dati = json.loads(percorso.read_bytes().decode("utf-8"))
        except (OSError, ValueError) as errore:
            if not in_silenzio:
                print(f"[AVVISO] memoria AI illeggibile — riparto da vuoto: {errore}")
            return {}
        voci = dati.get("voci") if isinstance(dati, dict) else None
        if not isinstance(voci, dict):
            if not in_silenzio:
                print("[AVVISO] memoria AI in una forma inattesa — riparto da vuoto.")
            return {}
        return {chiave: voce for chiave, voce in voci.items() if isinstance(voce, dict)}

    @staticmethod
    def _salva_memoria(percorso: Path, voci: dict[str, dict]) -> None:
        """Scrittura atomica, in binario e forzata sul disco: `scrittura_sicura`.

        Lo schema — temporaneo col nome di chi lo scrive, `fsync`, `os.replace`
        — era in quattro copie e questa era la sola col nome unico: adesso ce
        l'hanno tutte, e ce l'hanno una volta sola.  Le chiavi si ordinano
        perche' due salvataggi dello stesso contenuto devono dare lo stesso
        file.

        ⚠ Un guasto del **disco** non ferma niente: si perde la memoria di
        questa run e si ripagano le domande all'AI, che e' meno grave di una run
        che si ferma. Gli altri si', e non e' cambiato con lo spostamento su
        `scrittura_sicura`: `json.dumps` stava fuori dal `try` anche prima,
        quindi una voce non serializzabile propagava allora come adesso. Quello
        che e' cambiato in meglio e' che il temporaneo viene tolto di mezzo per
        qualunque eccezione, non solo per un `OSError`.
        """

        try:
            scrittura_sicura.scrivi_json(
                percorso, {"versione": 1, "voci": voci},
                ordina_le_chiavi=True, a_capo_finale=True,
            )
        except OSError as errore:
            print(f"[AVVISO] memoria AI non salvata: {errore}")
    def _pulito(self, testo: Any, quanti: int = 300) -> str:
        """Prima toglie la chiave, poi accorcia.

        L'ordine conta: accorciare per primo potrebbe tagliare la chiave a meta'
        e lasciarne un pezzo, che la sostituzione esatta non riconoscerebbe piu'.
        """

        return _accorcia(oscura(str(testo or ""), self._chiave), quanti)

    def _costruisci(
        self,
        stato: str,
        *,
        decisione: dict | None = None,
        modello: str | None = None,
        fornitore: str | None = None,
        costo: float = 0.0,
        token_ingresso: int = 0,
        token_uscita: int = 0,
        tentativi: int = 0,
        durata: float = 0.0,
        dettaglio: str = "",
    ) -> EsitoAI:
        """Costruisce l'esito e toglie la chiave da ogni campo, uno per uno."""

        if decisione is not None:
            decisione = {nome: oscura(valore, self._chiave) for nome, valore in decisione.items()}
        return EsitoAI(
            stato=stato,
            decisione=decisione,
            modello=oscura(modello or self.configurazione["model"], self._chiave),
            fornitore_calcolo=oscura(fornitore, self._chiave),
            costo_usd=round(_numero(costo), 8),
            token_ingresso=_intero(token_ingresso),
            token_uscita=_intero(token_uscita),
            tentativi=_intero(tentativi),
            durata_s=round(_numero(durata), 3),
            dettaglio=oscura(dettaglio, self._chiave),
        )


# ----------------------------------------------------------------------------
# Aiutanti liberi
# ----------------------------------------------------------------------------


def _verifica_caso(caso: CasoValutazione) -> None:
    """Un caso senza candidati è un uso sbagliato dell'API, non un guasto."""

    if not isinstance(caso, CasoValutazione):
        raise TypeError("Serve un CasoValutazione.")
    if not caso.candidati:
        raise ValueError(
            f"Caso senza candidati (riga {caso.gestionale_source_row}, fornitore "
            f"{caso.supplier}): non c'è niente da far valutare."
        )


def _fuori_schema(grezza: Any) -> str:
    """Dice perché la risposta non è conforme, o stringa vuota se lo è."""

    if not isinstance(grezza, dict):
        return f"non è un oggetto ma {type(grezza).__name__}"
    presenti = set(grezza)
    if presenti != CHIAVI_ATTESE:
        mancanti = sorted(CHIAVI_ATTESE - presenti)
        in_piu = sorted(presenti - CHIAVI_ATTESE)
        return f"chiavi mancanti {mancanti}, chiavi in più {in_piu}"
    if grezza["azione"] not in AZIONI_AMMESSE:
        return f"azione «{grezza['azione']}» fuori da {list(AZIONI_AMMESSE)}"
    if grezza["confidenza"] not in CONFIDENZE_AMMESSE:
        return f"confidenza «{grezza['confidenza']}» fuori da {list(CONFIDENZE_AMMESSE)}"
    if not isinstance(grezza["motivo"], str):
        return "il motivo non è un testo"
    source_row = grezza["source_row"]
    if source_row is not None and not isinstance(source_row, int):
        return "la source_row non è un intero né null"
    if isinstance(source_row, bool):
        return "la source_row non è un intero né null"
    return ""


CHIAVI_DECISIONE = frozenset({
    "gestionale_source_row",
    "supplier",
    "action",
    "source_row",
    "confidence",
    "rationale",
    "requires_user_confirmation",
})


def decisione_non_utilizzabile(caso: CasoValutazione, decisione: Any) -> str:
    """Dice perche' una decisione non si puo' usare, o stringa vuota se si puo'.

    Vale per **tutte** le decisioni, da qualunque strada arrivino: quella appena
    interpretata e quella ripresa dalla memoria. Sono i controlli 5, 6 e 7 del
    contratto raccolti in un posto solo, cosi' non possono piu' restare indietro
    su una delle due strade. La memoria vive in `app/data/`, fuori dal controllo
    di versione: e' un file che si puo' modificare a mano, che puo' cambiare
    forma fra una versione e l'altra, e da cui non deve mai poter uscire la
    decisione piu' pericolosa che questo progetto conosca — un `ACCEPT` con
    confidenza `ALTA` su una riga che non era fra i candidati.
    """

    if not isinstance(decisione, dict):
        return f"non è un oggetto ma {type(decisione).__name__}"
    presenti = set(decisione)
    if presenti != CHIAVI_DECISIONE:
        mancanti = sorted(CHIAVI_DECISIONE - presenti)
        in_piu = sorted(presenti - CHIAVI_DECISIONE)
        return f"chiavi mancanti {mancanti}, chiavi in più {in_piu}"
    if decisione["action"] not in AZIONI_AMMESSE:
        return f"azione «{decisione['action']}» fuori da {list(AZIONI_AMMESSE)}"
    if decisione["confidence"] not in CONFIDENZE_AMMESSE:
        return f"confidenza «{decisione['confidence']}» fuori da {list(CONFIDENZE_AMMESSE)}"
    if decisione["gestionale_source_row"] != caso.gestionale_source_row:
        return (
            f"è di un altro articolo (riga {decisione['gestionale_source_row']} "
            f"invece di {caso.gestionale_source_row})"
        )
    if decisione["supplier"] != caso.supplier:
        return f"è di un altro fornitore ({decisione['supplier']} invece di {caso.supplier})"
    source_row = decisione["source_row"]
    if decisione["action"] == "ACCEPT":
        righe_ammesse = {candidato.source_row for candidato in caso.candidati}
        if source_row not in righe_ammesse:
            return (
                f"accetta la source_row {source_row!r}, che non è fra i candidati "
                f"passati ({sorted(righe_ammesse)})"
            )
    elif source_row is not None:
        return "non accetta niente ma indica comunque una source_row"
    return ""


def _messaggio_di_errore(risposta: Any) -> str:
    """Il messaggio dichiarato dal servizio, se c'è; altrimenti il corpo."""

    if isinstance(risposta, dict):
        errore = risposta.get("error")
        if isinstance(errore, dict) and errore.get("message"):
            return str(errore["message"])
        if isinstance(errore, str):
            return errore
        return json.dumps(risposta, ensure_ascii=False)
    return str(risposta or "")


def _motivo_del_tetto(stato: str, configurazione: dict) -> str:
    if stato == STATO_TETTO_SPESA:
        return (
            f"raggiunto il tetto di spesa di {configurazione['tetto_spesa_usd']} $: "
            "nessuna chiamata fatta."
        )
    return (
        f"raggiunto il tetto di {configurazione['tetto_chiamate']} chiamate: "
        "nessuna chiamata fatta."
    )


def _con_dettaglio(esito: EsitoAI, dettaglio: str) -> EsitoAI:
    return replace(esito, dettaglio=dettaglio.strip())


def _con_costo(esito: EsitoAI, costo_usd: float) -> EsitoAI:
    """L'esito con la spesa di **tutti** i tentativi, non solo dell'ultimo."""

    return replace(esito, costo_usd=round(costo_usd, 10))
