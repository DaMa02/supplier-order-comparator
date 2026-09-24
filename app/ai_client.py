#!/usr/bin/env python3
"""AI client: asks a model to judge a shortlist of candidates already built upstream.

The deterministic step produces, for every pair (management-software item,
supplier), a handful of candidates with a score. This module asks a model,
through OpenRouter, whether one of those candidates is the same product. The
decision is emitted in the format described by
`references/ai-decision-format.md`, which `scripts/merge_match_decisions.py`
reads.

Two invariants govern this file.

Degradation is a return value, not an exception. No public method raises
for a network, service or response failure: it returns an `EsitoAI` whose
status says what went wrong, with `decisione = None`. Callers translate every
`decisione is None` into `DA_VERIFICARE`. An exception is reserved for API
misuse (a case with no candidates, a missing prompt file).

An empty response is not a rejection. Measured on real calls:
`deepseek/deepseek-v4-flash`, on several compute providers, is a reasoning
model whose reasoning tokens are charged against `max_tokens`. With
`max_tokens` at 400, 11 of 30 responses came back with `finish_reason:
"length"`, empty `content` and no HTTP error: the model had spent the whole
token budget thinking and never wrote the answer. If the code doesn't
recognize this case, it reads as "no candidate matches" and a product
silently drops out of the comparison. Hence the default `max_tokens` of 1500,
the `TRONCATA` status, and its dedicated test.

The HTTP transport is injectable from the constructor, which is how tests
avoid the network. A transport is a callable of this shape:

    transport(url: str, body: dict, headers: dict, timeout: float)
        -> tuple[int, Any]

It returns the HTTP status and the response body already JSON-decoded (or the
raw text, if it wasn't JSON). It raises only for a network failure, which the
client translates into `ERRORE_RETE`. The real transport is `trasporto_urllib`.
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
    # Chosen from a benchmark of seven candidate models, not by preference: it
    # is the only one combining zero non-conforming responses, the fewest
    # missed matches and the highest speed. `deepseek-v4-flash`, the original
    # default, gets twice as many `ALTA` calls wrong. It is also cheaper than
    # `gpt-oss-120b` as measured, even though the list price favors the
    # latter (0.170 $/M output vs 0.600): the other model produces far more
    # reasoning tokens. List price alone isn't enough to choose.
    "model": "openai/gpt-5.6-luna",
    "base_url": "https://openrouter.ai/api/v1",
    "max_tokens": 1500,
    "temperature": 0.0,
    "timeout_secondi": 60.0,
    "tetto_spesa_usd": 3.0,        # the store's per-run spend budget, in USD
    "tetto_chiamate": 4000,
    # Measured on real calls, with the adversarial check on and memory empty
    # each pass: 150 cases take 82.1 s at 8 workers, 22.7 s at 32, 19.0 s at
    # 64. 32 is the knee of the curve: doubling it buys little and doubles
    # the requests a compute provider sees arrive together.
    #
    # This can't be scaled by 948/150 to estimate a real run's duration:
    # memory's cost isn't linear in the number of cases — every save rewrites
    # the whole file, which keeps growing — and parallelism buys nothing on
    # that part, since it runs inside the lock. A full 948-case run, measured
    # end to end: 105 s with memory starting empty. See
    # `RISPOSTE_FRA_DUE_SALVATAGGI`, which corrects for that cost.
    "parallelismo": 32,
    # v3 is the one benchmarked: v1 got 5 ALTA wrong and missed 32% of
    # matches, v2 missed 16% but got 9 ALTA wrong, v3 gets 3 wrong and misses
    # 31%. With the adversarial check above, v3 brings wrong ALTA to zero.
    "versione_prompt": "v3",
    "versione_avversario": "v1",
}


# The statuses. `OK` and `DALLA_MEMORIA` carry a decision; none of the others do.
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
# The compute provider wants the word "json" in the message; retried with it added.
STATO_JSON_RICHIESTO = "JSON_RICHIESTO"

# A single retry, and only for statuses that can change on a second try. An
# `HTTP_400` (wrong model id) or `HTTP_401` (invalid key) won't fix itself by
# repeating.
# Statuses for which continuing the batch makes no sense: the key is rejected,
# the credit is spent, the provider has locked us out. Retrying doesn't fix
# these, and neither does trying the other nine hundred cases: it's the same
# answer nine hundred times, minutes of waiting and a rate-limit risk, only to
# learn at the end what was already known after the first call. `HTTP_429`
# and `HTTP_5xx` stay out, since they're transient, as does `HTTP_400` (wrong
# model id), which is left to run per case since it can be case-specific.
STATI_CHE_FERMANO_L_INFORNATA = frozenset({"HTTP_401", "HTTP_402", "HTTP_403"})

# A case that was never asked because the batch stopped earlier. It isn't a
# per-case failure: nobody evaluated it, and downstream it counts as
# `DA_VERIFICARE` exactly like any other unevaluated case.
STATO_NON_CHIESTO = "NON_CHIESTO"

STATI_RIPETIBILI = frozenset({
    STATO_TRONCATA,
    STATO_CONTENUTO_VUOTO,
    STATO_NON_E_JSON,
    STATO_SCHEMA_NON_CONFORME,
    STATO_ERRORE_RETE,
    # Retryable because the second call differs from the first: it carries
    # the word the provider asked for. Without this, an `HTTP_400` would
    # never be retried — correctly so, since a wrong model id stays wrong no
    # matter how many times it's repeated.
    STATO_JSON_RICHIESTO,
    "HTTP_429",
    "HTTP_5xx",
})

# A transport failure that isn't a network failure: a code defect inside an
# injected transport. It isn't retried — retrying a MemoryError or a
# KeyError is the worst thing to do — and it isn't mixed with genuine network
# failures, since per-status accounting treats them as two different things.
STATO_ERRORE_TRASPORTO = "ERRORE_TRASPORTO"

# An unexpected failure inside the client itself, caught so a single case
# can't take down the whole batch.
STATO_ERRORE_INTERNO = "ERRORE_INTERNO"

# The families that really are network failures. `OSError` covers
# `TimeoutError`, `ConnectionError` and `URLError`; `ValueError` covers a body
# that doesn't decode.
GUASTI_DI_RETE = (OSError, http.client.HTTPException, ValueError)

# What is set aside for an in-flight call until a real cost is observed.
# Measured: a full case costs ~$0.0001. It starts ten times higher, since
# overestimating costs a few fewer calls while underestimating risks
# overshooting the spend cap.
COSTO_ATTESO_INIZIALE = 0.001

# How many new answers accumulate before the memory file is rewritten, when
# running in a batch. See `ClientAI._scrivi_ricordo`: saving means rewriting
# the whole file, and on a real run with over a thousand saves that meant
# ~90 seconds inside the lock. A hundred is the trade-off: about ten writes
# over a 948-case run, and at most a hundred already-paid-for answers lost if
# the process dies partway through.
RISPOSTE_FRA_DUE_SALVATAGGI = 100

# Waits before retrying, but only where waiting actually helps.
PAUSA_RIPETIZIONE_S = 2.0
STATI_CON_PAUSA = frozenset({"HTTP_429", "HTTP_5xx", STATO_ERRORE_RETE})

# Some compute providers require the word "json" in the messages.
#
# This is what broke `qwen/qwen3.5-flash-02-23` in the model benchmark: HTTP
# 400 on all 150 calls, with the error body spelling it out — "'messages'
# must contain the word 'json' in some form, to use 'response_format'". The
# same exact call, with the word added, returns 200 from the same provider.
#
# It matters because OpenRouter chooses the provider, which is the right
# call: pinning one would turn its failure into ours. But it means the
# configured model can land on a provider with this rule tomorrow, and that
# run's AI phase would answer 400 across the board with nobody around to
# understand why.
#
# The word is not always added, for a measured reason. Appending it to the
# system prompt changes the answers, because the model reads a format
# instruction as license to be terse:
#
#   | system prompt             | wrong `ALTA` | missed matches |
#   |----------------------------|--------------|-----------------|
#   | no addition (3 runs)       | 4 · 4 · 5    | 40 · 41 · 43    |
#   | "Reply only with the json  | 7 · 6 · 7    | 36 · 37 · 37    |
#   |  object…" (3 runs)         |              |                 |
#   | "Format: json." (1 run)    | 6            | 41              |
#
# The two bands don't overlap: paying for two extra wrong `ALTA` calls out of
# 400 cases — the only error that causes a wrong purchase — to guard against
# a provider that isn't even in use today is a bad trade.
#
# So the word is added only once that specific provider has already asked
# for it: an `HTTP 400` with this signature becomes retryable once, and the
# retry adds it. The normal path stays exactly as measured, and the defense
# still applies.
FIRMA_JSON_RICHIESTO = "must contain the word 'json'"
ISTRUZIONE_FORMATO = "Formato della risposta: json."


def _chiede_la_parola_json(risposta: Any) -> bool:
    """Recognizes a provider's rejection asking for the word "json".

    Checks the whole serialized response, not just the message: when
    OpenRouter wraps a provider's error, the top level carries a generic
    "Provider returned error" while the real message is nested inside
    `error.metadata.raw`, as a string."""
    try:
        testo = risposta if isinstance(risposta, str) else json.dumps(risposta, ensure_ascii=False)
    except (TypeError, ValueError):
        testo = str(risposta)
    return FIRMA_JSON_RICHIESTO in testo.lower()

AZIONI_AMMESSE = ("ACCEPT", "REJECT", "UNRESOLVED")
CONFIDENZE_AMMESSE = ("ALTA", "MEDIA", "BASSA")
CHIAVI_ATTESE = frozenset({"azione", "source_row", "confidenza", "motivo"})

# The schema version enters the memory key alongside the prompt version:
# changing the response shape must invalidate old answers.
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
    """A shortlist candidate, as the model sees it.

    No EAN and no price: the EAN because the benchmark uses it as ground
    truth and showing it would invalidate the measurement, the price because
    it has nothing to do with product identity.
    """

    source_row: int
    description: str
    score: float


@dataclass(frozen=True)
class CasoValutazione:
    """A pair (management-software item, supplier) with its shortlist."""

    gestionale_source_row: int
    supplier: str
    descrizione: str
    candidati: tuple[Candidato, ...]


@dataclass(frozen=True)
class EsitoAI:
    """The result of an evaluation: always a value, never an exception."""

    stato: str                 # see the statuses above
    decisione: dict | None     # shape of references/ai-decision-format.md, or None
    modello: str
    fornitore_calcolo: str | None
    costo_usd: float
    token_ingresso: int
    token_uscita: int
    tentativi: int
    durata_s: float
    dettaglio: str             # Italian, human-readable, NEVER contains the key


# The case `prova_connessione` uses to verify key, model id and schema with a
# real call: `GET /models` won't do, since it's public and answers without a key too.
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
# A generic safety net beyond substituting the real key: anything shaped like
# a key must not leave this function.
_FORMA_DI_CHIAVE = re.compile(r"sk-[A-Za-z0-9._\-]{8,}")


def oscura(valore: Any, chiave: str | None) -> Any:
    """Strips the key out of a text. Applied to every field of `EsitoAI`.

    Public on purpose: `app/server.py` also uses it to scrub every error
    message sent to the browser. Keeping two regexes in sync elsewhere would
    risk the forgotten one leaking the key; renaming this function breaks the
    server, deliberately."""

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
    """A number, also accepting the Italian decimal comma ("3,0").

    Settings are typed in by a person, and the comma is the natural way to
    write a decimal in Italian. Raises if it isn't a number: this call site
    needs to notice, not silently fall back.
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


# Every config entry has its own converter and minimum. A value that fails to
# convert does not become zero: it falls back to the default, and says so. A
# spend cap of zero is indistinguishable from "never call", and is the worst
# possible failure for this phase, because it's invisible: every case returns
# `TETTO_SPESA`, every product becomes `DA_VERIFICARE`, and the message shown
# to the user just repeats the number they thought they had set.
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
    """Converts and checks every entry, falling back to the default for bad ones.

    Always returns a usable configuration: this is where a badly written
    value is stopped, instead of turning into a silent zero mid-run.
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
    """The status name for an HTTP code the service reported."""

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
# Key, configuration, prompt
# ----------------------------------------------------------------------------


def leggi_chiave(percorso_secrets: Path | None = None) -> str | None:
    """The OpenRouter key: environment first, then `app/data/secrets.json`.

    In the file the key is nested under `openrouter.api_key`. Looking for it
    at the top level returns `None` and later an HTTP 401, while
    `GET /api/v1/models` keeps answering because it's public — an easy trap.
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
    """Whether the key is set and where it comes from, never the value itself.

    The only thing the server can send to the browser: the page must never be
    able to read the key back, not even to prefill a field. The four-
    character tail only lets a person tell two keys apart at a glance, and
    four characters reconstruct nothing."""

    dall_ambiente = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if dall_ambiente:
        return {"presente": True, "origine": "ambiente", "coda": dall_ambiente[-4:]}
    chiave = leggi_chiave(percorso_secrets)
    if chiave:
        return {"presente": True, "origine": "file", "coda": chiave[-4:]}
    return {"presente": False, "origine": "", "coda": ""}


def salva_chiave(chiave: str, percorso_secrets: Path | None = None) -> None:
    """Writes the key into `app/data/secrets.json`, keeping the rest of the file.

    The file may already exist with other content: it's re-read and only the
    nested `openrouter.api_key` entry is replaced. Written to a sibling
    temp file first, then renamed, so a crash midway can't leave the user
    without a key and without knowing it.

    Permissions are tightened before any content is written: the file is
    created with `os.open(..., 0o600)`, not written first and `chmod`ed
    after — the gap between those two steps would let the key touch disk
    with the process's umask permissions before being restricted."""

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

    # The temp filename carries process id and thread id, like every other
    # write in the program: a fixed name would let two concurrent saves
    # collide on the same file.
    accanto = percorso.with_name(f"{percorso.name}.{os.getpid()}.{threading.get_ident()}.nuovo")
    testo = (json.dumps(dati, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        # `0o600` at creation time; on Windows the mode is mostly meaningless,
        # but there's no equivalent risk to guard against there either.
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
    """Writes `app/data/impostazioni_ai.json` and returns the live configuration.

    Accepts only the names that exist in `CONFIGURAZIONE_PREDEFINITA`: an
    unknown entry would be silently ignored on read, and saving it anyway
    would leave the user a file that claims something the program doesn't
    do. The key itself never passes through here — it has its own path.

    Every value goes through the same converters as on load, so a spend cap
    written as the Italian `3,0` doesn't silently zero out: it's rejected
    immediately, falling back to the default, instead of quietly disabling
    the AI phase while showing the user the number they thought they'd set."""

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

    # Validated before writing, and here a rejected value must be an error,
    # not a silent fallback. `_convalida_configurazione` warns and falls back
    # — the right behavior at startup, where refusing to run over one bad
    # entry would be worse. Not on save: the user just typed that number and
    # is looking at the screen. Writing it to the file and then ignoring it
    # is the failure mode this guards against: `tetto_spesa_usd: "3,0"` with
    # the Italian decimal comma would zero out the cap while showing the user
    # the number they thought they'd set.
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

    # A prompt version that doesn't exist passes validation — it's a
    # non-empty string — and only raises later, at the first `ClientAI(...)`,
    # far from here and after the phase has already started. Checked right
    # away instead: reading it costs nothing, and this is the only moment
    # someone is actually watching.
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
    """The list of OpenRouter models, for the settings page's dropdown.

    Standard library only, and GET: `trasporto_urllib` is wired for POST with
    a JSON body, which isn't needed here.

    This call is public: it answers without a key, and even with an expired
    one. A populated menu is not proof the key works — only an actual model
    call proves that, and callers of this function must not imply otherwise
    to the user.

    Non-callable aliases — the ones prefixed with a tilde in the listing —
    are filtered out: including them led to real, costly confusion before."""

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
    """Defaults, then `app/data/impostazioni_ai.json`, then `OPENROUTER_MODEL`.

    The file may not exist: that's the normal case, not an error. No model
    id is hard-coded in the logic; it lives only in
    `CONFIGURAZIONE_PREDEFINITA`, in one place.
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
    """The versioned prompt, read from `references/prompts/`.

    The file contains the prompt text and nothing else: whatever is in it is
    exactly what the model receives. `nome` selects the family:
    `valuta_candidati` for the first pass, `verifica_avversariale` for the
    adversarial one.
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
# The case rendered as text, and the memory key
# ----------------------------------------------------------------------------


def caso_come_testo(caso: CasoValutazione) -> str:
    """The case as the model reads it. The exact wording was benchmarked; keep it stable."""

    righe = [f"Articolo cercato: {caso.descrizione}", "", "Candidati del fornitore:"]
    for candidato in caso.candidati:
        righe.append(
            f"- source_row {candidato.source_row}: {candidato.description}"
            f" (punteggio {_numero(candidato.score):.2f})"
        )
    return "\n".join(righe)


def _caso_serializzato(caso: CasoValutazione) -> str:
    """The case in a stable form: same data, always the same text."""

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
    """The first twelve hex digits of the fingerprint of a prompt's text.

    Without this, the memory key would seal only the version label (`v3`),
    not the actual prompt content: editing `valuta_candidati.v3.md` without
    renaming it would keep returning answers cached under the old prompt.
    Hashing the text itself ties the memory key to what the model actually saw."""

    return hashlib.sha256(str(testo).encode("utf-8")).hexdigest()[:12]


def chiave_memoria(
    modello: str,
    versione_prompt: str,
    versione_schema: str,
    caso: CasoValutazione,
) -> str:
    """Memory is addressed by content.

    Model, prompt version, schema version and case: if any one of these
    changes, the old answer no longer applies. `versione_prompt` also carries
    the prompt text's fingerprint (see `impronta_prompt`), so "version" here
    really means version, not just a filename.
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
# The real transport
# ----------------------------------------------------------------------------


def trasporto_urllib(url: str, corpo: dict, intestazioni: dict, timeout: float) -> tuple[int, Any]:
    """The only part that touches the network: a POST, and nothing else.

    Returns `(HTTP status, decoded body)`. An HTTP error is not an
    exception, it's a return value, because the body of a 400 or a 401 says
    what happened. A network failure does raise, and the client translates it
    into `ERRORE_RETE`.
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
# The client
# ----------------------------------------------------------------------------


class ClientAI:
    """Asks the model to choose among the candidates, and trusts nothing it says.

    `trasporto=None` uses `trasporto_urllib`. `memoria=None` means no disk
    persistence (the mode tests use): in-RAM memory still applies, so a case
    already seen isn't paid for twice even within a single run.
    `chiave=None` looks up the key via `leggi_chiave()`; `chiave=""` declares
    there is no key, which is how tests exercise the `SENZA_CHIAVE` status.
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
        # Turns on by itself the first time a provider demands it, and stays
        # on for the client's lifetime: see FIRMA_JSON_RICHIESTO. No lock
        # needed — several threads may write it concurrently, but they all
        # write the same `True`, and reading it one instant too early only
        # costs a retry, not a correctness bug.
        self._formato_obbligatorio = False
        # The adversarial prompt is read only when needed: a client used only
        # for the first pass never has to load it.
        self._prompt_avversario_letto: str | None = None

        self._lucchetto = threading.Lock()
        self._chiamate = 0
        self._costo = 0.0
        self._impegnato = 0.0            # spend reserved by in-flight calls
        self._costo_atteso = COSTO_ATTESO_INIZIALE
        self._per_stato: dict[str, int] = {}
        self._dalla_memoria = 0

        self._percorso_memoria = Path(memoria) if memoria is not None else None
        self._voci: dict[str, dict] = {}
        # How many new answers are waiting to be written to disk, and whether
        # we're inside a batch. See `_scrivi_ricordo`.
        self._da_salvare = 0
        self._in_infornata = False
        # Whoever wants batch progress sets a callback here. Used by the
        # orchestrator's progress bar: the phase can run for minutes, and a
        # program that stays silent that long looks stuck. Stays `None` for
        # everyone else, at no cost.
        self.avanzamento: Callable[[int, int], None] | None = None
        if self._percorso_memoria is not None:
            self._voci = self._carica_memoria(self._percorso_memoria)

    # -- public surface -------------------------------------------------

    def valuta_candidati(self, caso: CasoValutazione) -> EsitoAI:
        """Evaluates one case. Never raises for a failure: returns a status."""

        _verifica_caso(caso)
        return self._registra(self._valuta(caso, usa_memoria=True))

    def valuta_molti(self, casi: Sequence[CasoValutazione]) -> list[EsitoAI]:
        """Evaluates many cases in parallel, in the order the cases were given.

        A failing case doesn't stop the others: its failure is a return value
        like any other. Malformed cases are caught before starting, so an API
        misuse doesn't take down half a batch.
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
                # Any answers still queued get flushed to disk now: this is
                # the "end" that `_scrivi_ricordo` relies on.
                if self._da_salvare:
                    self._salva_adesso()

    def _conta_mentre_valuta(
        self, casi: list[CasoValutazione], valuta: Callable[[CasoValutazione], EsitoAI]
    ) -> Callable[[CasoValutazione], EsitoAI]:
        """Wraps the evaluator to report progress, if anyone is listening.

        The counter is updated inside the client's lock because several
        worker threads run concurrently: without it, `fatti += 1` would drop
        increments and the progress bar would go backwards. The listener is
        called outside the lock — it may write to disk, and holding the lock
        there would serialize the whole batch.
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
                    # A broken listener must not take down a multi-minute
                    # run: it's just a progress bar.
                    pass

        return valuta_e_conta

    def _infornata_vera(
        self, casi: list[CasoValutazione], valuta: Callable[[CasoValutazione], EsitoAI]
    ) -> list[EsitoAI]:
        valuta = self._conta_mentre_valuta(casi, valuta)
        operai = max(1, min(_intero(self.configurazione["parallelismo"], 1), len(casi)))
        if operai == 1:
            return [valuta(caso) for caso in casi]

        # The first real call runs alone, and it's not a stylistic caution:
        # until one call has actually completed, the cost of a call with the
        # configured model is unknown, so the spend reservation works off a
        # guess and every configured worker thread could overshoot the cap
        # together — the thread count is a settings value (`parallelismo`).
        # Once the first call is done, `_costo_atteso` is a real number and
        # the cap holds. It costs one extra network round trip on a run that
        # takes minutes, and in exchange a wrong model or an expired key
        # surfaces before hundreds of calls are launched.
        esiti: list[EsitoAI] = []
        indice = 0
        while indice < len(casi) and self.contabilita["chiamate"] == 0:
            esiti.append(valuta(casi[indice]))
            indice += 1

        restanti = casi[indice:]
        # If the first call comes back with "this key is rejected", the batch
        # stops right here instead of launching the pool anyway: since
        # `_prenota_chiamata` reserves a call slot before making it, the
        # warm-up loop above finishes after the first attempt regardless of
        # whether it failed, so this check is what actually keeps a bad key
        # from producing hundreds of identical failures.
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
        """The adversarial pass: only the proposed candidate, with reversed
        instructions. The case is a `CasoValutazione` holding just that one
        row, so the shortlist check applies here too."""

        scelta = next(
            (c for c in caso.candidati if c.source_row == decisione.get("source_row")), None
        )
        if scelta is None:  # unreachable: check 6 already excludes this
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
        except Exception as errore:  # noqa: BLE001 — see _valuta_senza_sorprese
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
        """The batch's safety net: a broken case doesn't take the others down with it.

        Without this, an unexpected exception on a single case propagates up
        from `attesa.result()` and discards every already-computed,
        already-paid-for result for the rest of the batch. "A failing case
        doesn't stop the others" must hold for an exception too, not just for
        a status.
        """

        try:
            return self.valuta_candidati(caso)
        except Exception as errore:  # noqa: BLE001 — that's the whole point here
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
        """The first pass, then a second pass on every `ACCEPT` that tries to refute it.

        This is what makes `ALTA` trustworthy enough to accept without asking
        anything on screen. Measured on a 150-case benchmark, with prompt `v3`:

        | | wrong ALTA | correct ACCEPT |
        |---|---|---|
        | first pass only | 3 | 43 |
        | with the check  | 0 | 40 |

        When the two passes disagree, the case does not turn into a question
        on screen: the decision drops to `UNRESOLVED`, i.e. `DA_VERIFICARE`,
        and lands in the list the reviewer checks against the paper order.
        Costs one extra call, but only on `ACCEPT` cases — under a third of
        the total.
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

        # The two passes disagree: no attempt to decide who is right, the
        # case is handed to the reviewer instead. An unconfirmed ACCEPT is
        # worth less than nothing.
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
        """Like `valuta_molti`, but with the adversarial check on every ACCEPT."""

        return self._infornata(casi, self._verifica_senza_sorprese)

    def prova_connessione(self) -> EsitoAI:
        """A real call on a fixture case: exercises key, model id and schema together."""

        return self._registra(self._valuta(CASO_DI_PROVA, usa_memoria=False))

    @property
    def contabilita(self) -> dict:
        """Calls made, spend, outcomes by status, and how many came from memory.

        `chiamate` counts HTTP requests actually sent, retries included: it's
        what the spend cap has to hold back.
        """

        with self._lucchetto:
            return {
                "chiamate": self._chiamate,
                "costo_usd": round(self._costo, 8),
                "per_stato": dict(self._per_stato),
                "dalla_memoria": self._dalla_memoria,
            }

    # -- a single evaluation round -----------------------------------------

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
                # Label and text both: a corrected prompt, even without
                # renaming it, must invalidate old answers.
                f"{etichetta_prompt}:{impronta_prompt(prompt)}",
                VERSIONE_SCHEMA,
                caso,
            )
            ricordo = self._leggi_ricordo(chiave_ricordo)
            if ricordo is not None:
                ricordata = ricordo.get("decisione")
                problema = decisione_non_utilizzabile(caso, ricordata)
                if problema:
                    # A corrupted entry must never turn into a decision: it's
                    # discarded and re-requested, as if it never existed.
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
            # The first attempt is charged even when it fails — a truncated
            # response still consumed and billed reasoning tokens — so the
            # outcome carries the running total, not just the last amount:
            # anyone summing outcomes for a report needs to see the real spend.
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

        return ultimo  # unreachable: the loop always returns before this

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
            # Network, DNS, timeout, an undecodable body: these are failures,
            # not exceptions as far as the caller sees them, and retrying
            # them makes sense.
            self._chiudi_prenotazione(0.0)
            return self._costruisci(
                STATO_ERRORE_RETE,
                tentativi=tentativo,
                durata=time.monotonic() - inizio,
                dettaglio=f"Chiamata non riuscita ({type(errore).__name__}): {self._pulito(errore)}",
            )
        except Exception as errore:  # noqa: BLE001 — see STATO_ERRORE_TRASPORTO
            # A KeyError, AssertionError or MemoryError inside an injected
            # transport isn't the network failing: it's a defect. These
            # aren't retried, and they aren't mixed with network failures in
            # the per-status accounting.
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

        # The reservation must be released regardless, even if interpreting
        # the response raises or the status code isn't a number: a
        # reservation left open never comes back down, and after a few cases
        # every evaluation starts returning TETTO_SPESA while the actual cap
        # is barely touched. The run would quietly stall, with every
        # remaining product silently turning into DA_VERIFICARE.
        esito = None
        try:
            esito = self._interpreta(caso, int(codice), risposta, tentativo=tentativo, inizio=inizio)
            return esito
        finally:
            self._chiudi_prenotazione(esito.costo_usd if esito is not None else 0.0)

    # -- the checks, in contract order ------------------------------

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
        # `or {}` isn't enough: it covers a falsy usage (null, {}, 0) but not
        # a real usage object of the wrong shape, and `.get` on a string
        # raises. A single malformed response would otherwise crash the
        # whole batch.
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

        # 1. HTTP status other than 200.
        if codice != 200:
            # The provider requiring the word "json" in messages: not a
            # rejection and not a wrong model id, just its own convention.
            # The flag is turned on and the call is retried — once, and then
            # for the rest of the batch, otherwise every case would pay for a
            # wasted first attempt.
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

        # An error declared inside a 200: happens when the failure arrives
        # after the response stream has already opened. Treated as the HTTP
        # error it declares even if a populated `choices` comes along with
        # it: if the service reports a failure, that's the outcome. In the
        # rare case the content was actually complete, this costs one
        # unnecessary retry — the right side to err on.
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

        # 2. The measured trap: the token budget spent entirely on reasoning.
        if motivo_fine == "length":
            return esito(
                STATO_TRONCATA,
                dettaglio=(
                    "Risposta troncata (finish_reason «length»): il modello ha "
                    f"esaurito i {self.configurazione['max_tokens']} token del tetto. "
                    "Non è un rifiuto: il caso resta da verificare."
                ),
            )

        # 3. Content missing, not a string, or empty: never treated as a REJECT.
        if not isinstance(contenuto, str) or not contenuto.strip():
            return esito(
                STATO_CONTENUTO_VUOTO,
                dettaglio="Il servizio ha risposto 200 ma senza testo utile: nessuna decisione.",
            )

        # 4. Content that isn't JSON.
        try:
            grezza = json.loads(contenuto)
        except ValueError:
            return esito(
                STATO_NON_E_JSON,
                dettaglio=f"Risposta non in JSON: {self._pulito(contenuto, 160)}",
            )

        # 5. Keys or values outside the schema.
        problema = _fuori_schema(grezza)
        if problema:
            return esito(
                STATO_SCHEMA_NON_CONFORME,
                dettaglio=f"Risposta fuori schema: {problema}",
            )

        azione = grezza["azione"]
        source_row = grezza["source_row"]
        righe_ammesse = {candidato.source_row for candidato in caso.candidati}

        # 6. A row that wasn't among the candidates: discarded, no debate.
        if azione == "ACCEPT" and source_row not in righe_ammesse:
            return esito(
                STATO_RIGA_FUORI_SHORTLIST,
                dettaglio=(
                    f"Proposta la source_row {source_row}, che non è fra i candidati "
                    f"passati ({sorted(righe_ammesse)}): scartata."
                ),
            )

        # 7. A row on a non-ACCEPT means nothing: cleared.
        if azione != "ACCEPT" and source_row is not None:
            source_row = None

        decisione = {
            "gestionale_source_row": caso.gestionale_source_row,
            "supplier": caso.supplier,
            "action": azione,
            "source_row": source_row,
            "confidence": grezza["confidenza"],
            "rationale": str(grezza["motivo"]),
            # Stays `true`: auto-accepting high-confidence matches without
            # confirmation is a decision for `merge_match_decisions.py` and
            # `build_review_data.py`, not for this module.
            "requires_user_confirmation": True,
        }
        # The same final check a memory-recalled decision goes through: this
        # keeps the validation in one place, so the two paths can't diverge.
        problema = decisione_non_utilizzabile(caso, decisione)
        if problema:
            return esito(
                STATO_SCHEMA_NON_CONFORME,
                dettaglio=f"Decisione costruita ma non utilizzabile: {problema}",
            )
        return esito(STATO_OK, decisione=decisione, dettaglio="Risposta valida e validata.")

    # -- accounting, caps, memory ---------------------------------------

    def _prenota_chiamata(self) -> str | None:
        """Checks the caps and reserves a call and its spend. Returns who's blocking.

        The check and the reservation happen inside the same lock: with
        `parallelismo` worker threads, checking and then incrementing
        separately would let that many calls overshoot the cap together.

        The spend is reserved too, not just the call slot: the real cost is
        only known once the response comes back, so without a reservation N
        threads could all pass the check before any of them has added its
        cost, overshooting the cap by N calls. The highest cost seen so far
        (a conservative guess at first) is set aside and returned once the
        real cost arrives.

        The fallback for both caps is always the default, never zero: a cap
        of zero would silently disable the whole phase.
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
        """The real cost replaces the estimate that was set aside."""

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
        """The entry, only if it has the minimal shape. The caller validates it anyway."""

        with self._lucchetto:
            voce = self._voci.get(chiave)
            if not isinstance(voce, dict) or not isinstance(voce.get("decisione"), dict):
                return None
            return dict(voce)

    def _dimentica(self, chiave: str) -> None:
        """Removes a corrupted entry, so it isn't read back on every pass."""

        with self._lucchetto:
            self._voci.pop(chiave, None)

    def _scrivi_ricordo(self, chiave: str, esito: EsitoAI) -> None:
        """Only valid answers are memorized.

        A failure is never memorized: the next run needs to be able to retry it.

        Inside a batch, the file is not rewritten on every single answer.
        Saving means re-reading, merging and rewriting the whole file, and
        the cost grows with its size: measured on a real 948-case run with a
        568 KB memory file, ~1000 saves cost ~90 s serialized inside the
        lock, against ~105 s of network time for the rest of the run, and the
        file grew to 1408 KB — so the following week's run would cost more
        still. Parallelism buys nothing on that time, since it all runs
        inside the same lock.

        So: always in RAM, flushed to disk every
        `RISPOSTE_FRA_DUE_SALVATAGGI` answers and once at the end of the
        batch. Outside a batch it's saved immediately, as before: evaluating
        a single case has no "end" to defer to. The trade-off is that an
        interrupted run loses at most a hundred already-paid-for answers,
        and memory is a cache: losing a slice of it costs money next time,
        not correctness.
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
        """Rewrites the memory file. Must be called with the lock held.

        The file is re-read and merged into: a second client (or a second
        run) that wrote in the meantime must not lose its entries just
        because this call rewrites its own in-RAM copy. This call's entries
        win on a matching key, but don't erase the others."""

        if self._percorso_memoria is None:
            return
        sul_disco = self._carica_memoria(self._percorso_memoria, in_silenzio=True)
        sul_disco.update(self._voci)
        self._voci = sul_disco
        self._salva_memoria(self._percorso_memoria, self._voci)
        self._da_salvare = 0

    @staticmethod
    def _carica_memoria(percorso: Path, *, in_silenzio: bool = False) -> dict[str, dict]:
        """An unreadable memory file is a warning, not an error."""

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
        """Atomic, binary, fsync'd write, via `scrittura_sicura`.

        Keys are sorted so that two saves of the same content produce the
        same file byte for byte.

        A disk failure doesn't stop anything: this run's memory is lost and
        the AI questions get re-asked, which is less severe than stopping the
        run. A non-serializable entry still propagates as an exception,
        since `json.dumps` runs outside the try block; what the shared
        `scrittura_sicura` helper does add is cleaning up the temp file for
        any exception, not just an `OSError`.
        """

        try:
            scrittura_sicura.scrivi_json(
                percorso, {"versione": 1, "voci": voci},
                ordina_le_chiavi=True, a_capo_finale=True,
            )
        except OSError as errore:
            print(f"[AVVISO] memoria AI non salvata: {errore}")
    def _pulito(self, testo: Any, quanti: int = 300) -> str:
        """Strips the key first, then truncates.

        Order matters: truncating first could cut the key in half and leave
        a fragment the exact-match substitution would no longer recognize.
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
        """Builds the outcome and strips the key from every field, one by one."""

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
# Free-standing helpers
# ----------------------------------------------------------------------------


def _verifica_caso(caso: CasoValutazione) -> None:
    """A case with no candidates is API misuse, not a failure."""

    if not isinstance(caso, CasoValutazione):
        raise TypeError("Serve un CasoValutazione.")
    if not caso.candidati:
        raise ValueError(
            f"Caso senza candidati (riga {caso.gestionale_source_row}, fornitore "
            f"{caso.supplier}): non c'è niente da far valutare."
        )


def _fuori_schema(grezza: Any) -> str:
    """Says why the response doesn't conform, or an empty string if it does."""

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
    """Says why a decision can't be used, or an empty string if it can.

    Applies to every decision, whichever path it took: freshly interpreted
    or recalled from memory. This collects checks 5, 6 and 7 of the contract
    in one place, so the two paths can't drift apart. Memory lives in
    `app/data/`, outside version control — a file that can be hand-edited and
    can change shape between versions — and it must never be able to produce
    the most dangerous decision this project knows: an `ACCEPT` at `ALTA`
    confidence on a row that wasn't among the candidates.
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
    """The message the service declared, if any; otherwise the whole body."""

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
    """The outcome with the spend of every attempt, not just the last one."""

    return replace(esito, costo_usd=round(costo_usd, 10))
