#!/usr/bin/env python3
"""CLI that turns a shortlist into an AI decision: the pipeline's AI-evaluation step.

Like every other script in the pipeline, this runs as a subprocess of the
orchestrator, and can equally be tested standalone:

    scripts/valuta_shortlist.py --shortlists <run>/dati/semantic_shortlists.json
                                --output     <run>/dati/ai_decisions.json
                                --rapporto   <run>/dati/ai_rapporto.json

Four properties this script enforces, decided elsewhere.

1. Cases without a decision are omitted from the output file, never faked.
`merge_match_decisions.py` already treats a pair with no decision as
`DA_VERIFICARE`. Writing a fake `UNRESOLVED` in place of a network failure
would erase the difference between "the model couldn't tell" and "the
question was never asked" — and the second is fixed by rerunning, the first
isn't.

2. The report is as mandatory as the decisions file. Making `--decisions`
optional would make an AI phase that never ran indistinguishable from one
that decided nothing; an optional `--rapporto` would reopen the same problem
from another angle, since the count `merge_match_decisions.py
--decisions-attese` expects must come from here — from the accounting of
whoever actually called the model — not from counting the file being
reconciled, or the check becomes a tautology.

3. Degradation is a return value, never an exception. Same contract as the
client, and it holds for this CLI too: no key, spend cap hit, network down —
all exit with both files written, an honest report, and an exit code that
distinguishes "couldn't" from "decided everything". The pipeline still moves
forward: "if OpenRouter doesn't answer, the program keeps going" is a
deliberate choice, and without the decisions file `build_review_data.py`
would produce an incomplete comparison.

4. No test touches the network. The client accepts an injected transport,
and `main()` accepts `crea_client` for the same reason.

Every decision carries the fingerprint of the case it was made on, and
`merge_match_decisions.py` verifies it. Its guards compare the shortlist and
price list of the current run only: against a decisions file from another
run they're blind by construction, because the management-software export is
the same file week to week and the `(row, supplier)` pairs mostly overlap.

What the fingerprint proves, and what it doesn't: it proves the evaluated
case is identical to today's — same item, same candidates, same order, same
scores. If a supplier's price list doesn't change from one week to the next,
the case really is the same and last week's decision passes — correctly so,
since that's exactly what the answer memory is for. What the fingerprint does
not say is what that decision was made with: for that, every row also
carries `ai_modello`, `ai_versione_prompt` and `ai_versione_avversario`, and
comparing those against the live configuration is the orchestrator's job.

Exit codes, a contract with the orchestrator:

| Code | Meaning |
|---|---|
| 0 | evaluated everything there was to evaluate |
| 2 | usage error: unreadable/malformed input, or outputs not writable |
| 5 | degraded: both files exist, but some cases weren't evaluated |

5 is not an error: it tells the user the run is degraded without stopping it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Sequence


CARTELLA_SCRIPT = Path(__file__).resolve().parent
RADICE_SKILL = CARTELLA_SCRIPT.parent
for _cartella in (RADICE_SKILL / "app", CARTELLA_SCRIPT):
    if str(_cartella) not in sys.path:
        sys.path.insert(0, str(_cartella))

from ai_client import (  # noqa: E402
    PERCORSO_MEMORIA,
    Candidato,
    CasoValutazione,
    ClientAI,
    carica_configurazione,
    leggi_chiave,
    oscura,
)

# The fingerprint is defined where shortlists are written, and two things
# use it: this script, which stamps it onto every decision, and
# `merge_match_decisions.py`, which verifies it. Imported, not duplicated —
# two implementations of a fingerprint risk drifting apart, and the
# forgotten one is the one that fails to recognize an old file.
from build_semantic_shortlists import impronta_caso  # noqa: E402


USCITA_OK = 0
USCITA_INGRESSO_NON_UTILIZZABILE = 2
USCITA_DEGRADATO = 5


def uscita_in_utf8() -> None:
    """Forces stdout and stderr to UTF-8, regardless of the console's code page.

    Same reason as `merge_match_decisions.py`: on Windows a process writing
    to a pipe uses the system encoding (cp1252 here), and real product
    descriptions flow through this stream — an accented character is enough
    to hand the orchestrator bytes that aren't valid UTF-8."""

    for flusso in (sys.stdout, sys.stderr):
        try:
            flusso.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover - streams replaced in tests
            pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shortlists", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--rapporto",
        type=Path,
        required=True,
        help=(
            "Dove scrivere la contabilità della fase AI. Obbligatorio: è la "
            "fonte indipendente da cui l'orchestratore prende il numero per "
            "«merge_match_decisions.py --decisions-attese»."
        ),
    )
    parser.add_argument(
        "--memoria",
        type=Path,
        default=PERCORSO_MEMORIA,
        help=(
            "La memoria delle risposte già ottenute, indirizzata dal contenuto: "
            "un caso identico non si paga due volte. Predefinita: "
            "app/data/memoria_ai.json."
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


# ----------------------------------------------------------------------------
# From input to cases
# ----------------------------------------------------------------------------


def numero_di_riga(valore: Any, dove: str) -> int:
    """An integer row number. Accepts `"441"`, rejects `441.5` and `True`.

    Not pedantry: `int(441.9)` silently gives 441, and an off-by-one row is a
    real, hard-to-notice class of bug this guards against."""

    if isinstance(valore, bool) or valore is None:
        raise ValueError(f"{dove}: {valore!r} non è un numero di riga")
    if isinstance(valore, int):
        return valore
    try:
        numero = float(str(valore).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{dove}: {valore!r} non è un numero di riga") from None
    # `Infinity` and `NaN` are valid JSON as far as Python is concerned, and
    # `int()` on an infinite value raises `OverflowError`, which `main`
    # doesn't catch: left unguarded, this would produce a traceback and
    # neither output file written.
    if not math.isfinite(numero):
        raise ValueError(f"{dove}: {valore!r} non è un numero di riga")
    if numero != int(numero):
        raise ValueError(f"{dove}: {valore!r} non è un numero di riga intero")
    return int(numero)


def testo_non_vuoto(valore: Any, dove: str) -> str:
    """The text as-is, as long as it isn't empty.

    Returned without normalizing it: this is what the model will read, and
    what the fingerprint is computed over. Stripping whitespace here but not
    in `merge_match_decisions.py` would break the fingerprint comparison on
    every case, invalidating a whole run."""

    if not isinstance(valore, str) or not valore.strip():
        raise ValueError(f"{dove}: {valore!r} non è un testo utilizzabile")
    return valore


def punteggio(valore: Any, dove: str) -> float:
    if isinstance(valore, bool) or valore is None:
        raise ValueError(f"{dove}: punteggio {valore!r} non utilizzabile")
    try:
        return float(valore)
    except (TypeError, ValueError):
        raise ValueError(f"{dove}: punteggio {valore!r} non utilizzabile") from None


def caso_dalla_voce(voce: Any, posizione: int) -> CasoValutazione:
    """Turns one entry of `semantic_shortlists.json` into a case for the client.

    A malformed entry is a failure, not a case to skip: the shortlist is
    written by `build_semantic_shortlists.py`, so a broken entry means
    something upstream is broken, and silently continuing would drop
    products out of the comparison without saying so.

    `Candidato` carries no EAN and no price, deliberately: the EAN is the
    benchmark's ground truth and showing it would invalidate any
    measurement; the price has nothing to do with product identity. Don't
    add them."""

    dove = f"voce {posizione}"
    if not isinstance(voce, dict):
        raise ValueError(f"{dove}: non è un oggetto ma {type(voce).__name__}")
    riga = numero_di_riga(voce.get("gestionale_source_row"), dove)
    fornitore = testo_non_vuoto(voce.get("supplier"), f"{dove}: supplier")
    descrizione = testo_non_vuoto(voce.get("description"), f"{dove}: description")

    grezzi = voce.get("candidates")
    if grezzi is None:
        grezzi = []
    if not isinstance(grezzi, list):
        raise ValueError(f"{dove}: «candidates» non è una lista ma {type(grezzi).__name__}")

    candidati = []
    for indice, grezzo in enumerate(grezzi):
        qui = f"{dove}, candidato {indice}"
        if not isinstance(grezzo, dict):
            raise ValueError(f"{qui}: non è un oggetto ma {type(grezzo).__name__}")
        candidati.append(
            Candidato(
                source_row=numero_di_riga(grezzo.get("source_row"), qui),
                description=testo_non_vuoto(grezzo.get("description"), f"{qui}: description"),
                score=punteggio(grezzo.get("score"), qui),
            )
        )

    return CasoValutazione(
        gestionale_source_row=riga,
        supplier=fornitore,
        descrizione=descrizione,
        candidati=tuple(candidati),
    )


def casi_dalle_shortlist(
    shortlists: Any,
) -> tuple[list[CasoValutazione], list[CasoValutazione]]:
    """The cases to evaluate, and the ones with no candidate at all, separated.

    The latter do occur in practice and are never sent to the model: asking
    it to choose among nothing is API misuse, and `ClientAI` raises for it.
    They're counted in the report and end up as `DA_VERIFICARE`, like every
    pair without a decision.

    Duplicate pairs are caught here. Left unchecked, they'd become two
    decisions for the same pair further down the pipeline, and
    `merge_match_decisions.py` would exit with a "duplicate decision" error —
    pointing whoever debugs it at the wrong file."""

    if not isinstance(shortlists, list):
        raise ValueError(f"il file delle shortlist deve essere una lista, non {type(shortlists).__name__}")

    casi: list[CasoValutazione] = []
    senza_candidati: list[CasoValutazione] = []
    viste: set[tuple[int, str]] = set()
    for posizione, voce in enumerate(shortlists):
        caso = caso_dalla_voce(voce, posizione)
        coppia = (caso.gestionale_source_row, caso.supplier)
        if coppia in viste:
            raise ValueError(
                f"voce {posizione}: la coppia riga {coppia[0]} / {coppia[1]} compare più di una volta"
            )
        viste.add(coppia)
        (casi if caso.candidati else senza_candidati).append(caso)
    return casi, senza_candidati


# ----------------------------------------------------------------------------
# From outcomes to decisions
# ----------------------------------------------------------------------------


def impronta_del_caso(caso: CasoValutazione) -> str:
    """The fingerprint computed on what was actually sent to the model.

    Takes the case, not the entry it came from: if this script ever
    transformed something between the two — a discarded candidate, a
    shortened description — the fingerprint must reflect what the model saw,
    not what was in the file."""

    return impronta_caso(
        caso.gestionale_source_row,
        caso.supplier,
        caso.descrizione,
        [(c.source_row, c.description, c.score) for c in caso.candidati],
    )


def righe_decisioni(
    casi: Sequence[CasoValutazione],
    esiti: Sequence[Any],
    configurazione: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The decisions in the format of `references/ai-decision-format.md`.

    `valuta_molti_con_verifica` returns outcomes in the same order as the
    input cases, and this pairing relies on that. If that guarantee ever
    broke, every decision would land on the wrong item, `ALTA` confidence and
    all — so this checks the pairing instead of trusting it, and a mismatch
    stops everything: zero decisions is better than hundreds attributed at
    random."""

    if len(esiti) != len(casi):
        raise RuntimeError(
            f"il client ha restituito {len(esiti)} esiti per {len(casi)} casi: "
            "non si può accoppiarli"
        )
    righe: list[dict[str, Any]] = []
    for caso, esito in zip(casi, esiti):
        decisione = esito.decisione
        if not decisione:
            continue
        sua = (decisione.get("gestionale_source_row"), decisione.get("supplier"))
        if sua != (caso.gestionale_source_row, caso.supplier):
            raise RuntimeError(
                f"esito fuori posto: decisione per {sua} sul caso "
                f"{(caso.gestionale_source_row, caso.supplier)}"
            )
        righe.append({
            **decisione,
            # What the model saw: the fingerprint is what the merge step
            # verifies, the description is for anyone reading a report or a
            # file by hand.
            "ai_impronta_caso": impronta_del_caso(caso),
            "ai_articolo_mostrato": caso.descrizione,
            # Who saw it. The fingerprint proves the case is the same, and
            # when a supplier's price list doesn't change week to week the
            # case really is the same, so an old decision legitimately
            # passes — that's what the answer memory is for. But a decision
            # made with an outdated, less accurate prompt version must not
            # be able to enter today's order unnoticed. Comparing this
            # against the live configuration is the orchestrator's job: this
            # is only where the data gets recorded, since this is the only
            # point where it exists.
            **provenienza(configurazione),
        })
    return righe


def provenienza(configurazione: dict[str, Any] | None) -> dict[str, Any]:
    """Which model and which prompt a decision was made with."""

    configurazione = configurazione or {}
    return {
        "ai_modello": configurazione.get("model"),
        "ai_versione_prompt": configurazione.get("versione_prompt"),
        "ai_versione_avversario": configurazione.get("versione_avversario"),
    }


def costruisci_rapporto(
    *,
    casi: Sequence[CasoValutazione],
    senza_candidati: Sequence[CasoValutazione],
    esiti: Sequence[Any],
    righe: Sequence[dict[str, Any]],
    contabilita: dict[str, Any],
    configurazione: dict[str, Any],
    durata: float,
    guasto: str,
) -> dict[str, Any]:
    """The phase's accounting, and the one thing anyone will actually read.

    The finished program runs unattended: what wasn't evaluated has to be
    counted here, since nobody reads the logs.

    Two counts by status, deliberately. `per_stato` has one entry per case;
    `per_stato_incluse_verifiche` comes from the client's own accounting and
    also includes the adversarial second pass, which runs on every `ACCEPT`
    — so its total is larger than the number of cases, and that's expected."""

    per_stato = Counter(esito.stato for esito in esiti)
    mancanti = Counter(esito.stato for esito in esiti if not esito.decisione)
    per_azione = Counter(str(riga.get("action")) for riga in righe)
    non_decisi = len(casi) - len(righe)
    # There was work ahead and none of it could even be attempted. Not a
    # corner case: an adapter change or a price list missing prices is
    # enough for `build_semantic_shortlists.py` to write hundreds of entries
    # with `candidates: []` — without this check the report would claim
    # everything was evaluated and exit 0.
    niente_da_fare = len(casi) == 0 and len(senza_candidati) > 0

    if guasto:
        motivo = f"guasto imprevisto della fase AI: {guasto}"
    elif niente_da_fare:
        motivo = (
            f"nessuno dei {len(senza_candidati)} casi ricevuti aveva un candidato da "
            "valutare: il passo deterministico non ha prodotto nessuna shortlist "
            "utilizzabile, e di solito vuol dire che un listino non si è caricato."
        )
    elif non_decisi > 0:
        dettaglio = ", ".join(f"{stato} {quanti}" for stato, quanti in mancanti.most_common())
        motivo = (
            f"{non_decisi} casi su {len(casi)} senza decisione"
            + (f": {dettaglio}" if dettaglio else ".")
        )
    else:
        motivo = ""

    rapporto = {
        "generato_il": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "casi_ricevuti": len(casi) + len(senza_candidati),
        "casi_valutabili": len(casi),
        "casi_senza_candidati": len(senza_candidati),
        # The number the orchestrator will pass to
        # `merge_match_decisions.py --decisions-attese`.
        "casi_decisi": len(righe),
        "casi_senza_decisione": non_decisi,
        "per_stato": dict(sorted(per_stato.items())),
        "per_stato_incluse_verifiche": dict(sorted(contabilita.get("per_stato", {}).items())),
        "per_azione": dict(sorted(per_azione.items())),
        "chiamate": contabilita.get("chiamate", 0),
        "costo_usd": contabilita.get("costo_usd", 0.0),
        "dalla_memoria": contabilita.get("dalla_memoria", 0),
        "durata_s": round(float(durata), 3),
        "parallelismo": configurazione.get("parallelismo"),
        "model": configurazione.get("model"),
        "versione_prompt": configurazione.get("versione_prompt"),
        "versione_avversario": configurazione.get("versione_avversario"),
        "degradato": bool(guasto) or non_decisi > 0 or niente_da_fare,
        "motivo_degrado": motivo,
    }
    if senza_candidati:
        rapporto["coppie_senza_candidati"] = [
            [caso.gestionale_source_row, caso.supplier] for caso in senza_candidati[:10]
        ]
    return rapporto


def scrivi_json(percorso: Path, documento: Any) -> None:
    """Written in binary, since on Windows `write_text` turns newlines into CRLF."""

    percorso.parent.mkdir(parents=True, exist_ok=True)
    testo = json.dumps(documento, ensure_ascii=False, indent=2) + "\n"
    percorso.write_bytes(testo.encode("utf-8"))


# ----------------------------------------------------------------------------
# The CLI
# ----------------------------------------------------------------------------


PREFISSO_AVANZAMENTO = "AVANZAMENTO "
# One line every ten cases, plus the first and the last: on a run of
# hundreds of cases that's around a hundred lines, enough for a progress bar
# to move and few enough not to turn `stderr` into a log file.
CASI_FRA_DUE_ANNUNCI = 10


def _annuncia_avanzamento(client: Any) -> Callable[[int, int], None]:
    """Prints batch progress to `stderr`, in a machine-readable form.

    The format is a single line — `AVANZAMENTO {json}` — because the reader
    is another program streaming this output as it arrives, and a multi-line
    JSON value has no clear end marker for a line-based reader.
    """

    def annuncia(fatti: int, totali: int) -> None:
        if fatti != totali and fatti % CASI_FRA_DUE_ANNUNCI:
            return
        contabilita = getattr(client, "contabilita", {}) or {}
        evento = {
            "fatti": fatti,
            "totali": totali,
            "chiamate": contabilita.get("chiamate"),
            "costo_usd": contabilita.get("costo_usd"),
        }
        print(
            PREFISSO_AVANZAMENTO + json.dumps(evento, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )

    return annuncia


def main(
    argv: Sequence[str] | None = None,
    *,
    crea_client: Callable[[dict[str, Any], Path], Any] | None = None,
) -> int:
    uscita_in_utf8()
    args = parse_args(argv)

    try:
        shortlists = json.loads(args.shortlists.read_text(encoding="utf-8"))
        casi, senza_candidati = casi_dalle_shortlist(shortlists)
    except (OSError, ValueError) as errore:
        print(f"Ingresso non utilizzabile: {errore}")
        return USCITA_INGRESSO_NON_UTILIZZABILE

    configurazione = carica_configurazione()
    print(
        f"{len(casi)} casi da valutare"
        + (f" ({len(senza_candidati)} senza candidati, esclusi)" if senza_candidati else "")
        + f" — modello {configurazione['model']},"
        f" parallelismo {configurazione['parallelismo']}.",
        flush=True,
    )

    # From here on, both output files are always written. An unexpected
    # failure must not leave the orchestrator with a traceback and no
    # artifacts: the previous run's `resolved_matches.json` would still be on
    # disk, ready to be read as if it were fresh.
    client = None
    esiti: list[Any] = []
    righe: list[dict[str, Any]] = []
    guasto = ""
    inizio = time.monotonic()
    try:
        client = (
            crea_client(configurazione, args.memoria)
            if crea_client is not None
            else ClientAI(configurazione, memoria=args.memoria)
        )
        # Progress goes to `stderr`, not `stdout`: `stdout` carries the final
        # report, which the orchestrator reads as JSON, and interleaving
        # status lines into it would make that unparseable.
        try:
            client.avanzamento = _annuncia_avanzamento(client)
        except AttributeError:  # pragma: no cover - a stub client with no such attribute
            pass
        esiti = list(client.valuta_molti_con_verifica(casi))
        righe = righe_decisioni(casi, esiti, configurazione)
    except Exception as errore:  # noqa: BLE001 — degradation is a return value
        # `oscura` here too: an error message echoing the called URL or a
        # header could carry the key along with it. The real key is passed
        # in rather than `None`, since exact substitution is the strong
        # defense and the `sk-…` regex is only the fallback net.
        guasto = f"{type(errore).__name__}: {oscura(str(errore), leggi_chiave())}"
        righe = []
    durata = time.monotonic() - inizio

    contabilita = client.contabilita if client is not None else {}
    rapporto = costruisci_rapporto(
        casi=casi,
        senza_candidati=senza_candidati,
        esiti=esiti,
        righe=righe,
        contabilita=contabilita,
        configurazione=configurazione,
        durata=durata,
        guasto=guasto,
    )

    try:
        # The previous run's report is removed first: if the write stops
        # halfway, whoever reads it should find no report — and notice —
        # rather than yesterday's report next to today's decisions.
        args.rapporto.unlink(missing_ok=True)
        # Decisions before the report: a report being present means the
        # decisions were written completely.
        scrivi_json(args.output, righe)
        scrivi_json(args.rapporto, rapporto)
    except OSError as errore:
        print(f"Uscite non scrivibili: {errore}")
        return USCITA_INGRESSO_NON_UTILIZZABILE

    print(json.dumps(rapporto, ensure_ascii=False, indent=2))
    return USCITA_DEGRADATO if rapporto["degradato"] else USCITA_OK


if __name__ == "__main__":
    raise SystemExit(main())
