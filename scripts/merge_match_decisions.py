#!/usr/bin/env python3
"""Merge deterministic exact matches and bounded AI decisions into one audited result.

Two decisions belong here, not in the AI client.

High-confidence (`ALTA`) matches are not confirmed on screen. A human reviewer
still checks the orders before sending them, so the effort goes into making
`ALTA` trustworthy — a tuned prompt plus an adversarial check — rather than
into hundreds of confirmations. On a benchmark of three independent runs of
150 cases, `ALTA` was never wrong. Anything below `ALTA` still asks for
confirmation.

A wrong rejection stays silent, and that is the risk this design accepts. The
adversarial check protects against wrong `ACCEPT`s, not against `REJECT`s: a
rejected product disappears from that supplier's comparison and nothing flags
it. Fixing it would cost more model calls; counting it doesn't. Every
rejection carries its best candidate's score, and callers decide what to
surface.

Four checks close off ways a failure could look like a success.

1. An accepted row is verified against what the model actually saw — the
shortlist. A row number identifies a position, not a product: if the price
list is re-read after the shortlist is built and gains a row at the top, row
441 is a different item, and an `ALTA` decision — which asks nobody — would
put that other item on the order. The full record still comes from
`normalized_sources.json`, since the shortlist doesn't carry the order
multiplier, but it must be the same product: same EAN, same description, same
price.

The comparison is always against the shortlist's candidate, never against
`matching_result.json`'s. `prepare_sources.py` writes both files from the same
objects in the same run, so comparing one against the other only compares the
price list with itself. The shortlist is the only artifact that can be stale —
the only one the model actually saw.

2. The model can't name a row it wasn't shown. Only shortlist rows are
accepted. Ambiguous-EAN candidates never reach the model, so they can't be
accepted either.

3. Absence of decisions is not success. `--decisions-attese` is required: the
caller declares how many decisions the AI phase produced, and the count must
match. Zero is a legitimate value and reflects the commercial decision that
the pipeline proceeds even when OpenRouter doesn't respond.

4. Counting doesn't prove membership. A decisions file from another run can
reconcile perfectly — same declared and found count — while none of its
decisions actually apply. So decisions that don't find their matching pair are
counted too, and a nonzero count here stops the pipeline.

The output file is always written, once the run gets far enough to build it,
with broken pairs degraded to `DA_VERIFICARE`. Skipping the write would leave
the previous run's `resolved_matches.json` on disk, and `build_review_data.py`
would read it as current. A fresh, degraded artifact is honest; a stale one
mistaken for fresh is not. The signal is the exit code and the summary.

The full summary prints only once the run gets far enough to build it.
Failures caught earlier — unreadable input, unreconciled decisions — print
their message alone, since at that point there's nothing to summarize yet.

Every degraded pair carries `ai_decisione_scartata`, the field
`build_review_data.py` uses to count these cases at the top of the page.
Without it, the constraint "what gets discarded must be counted in a visible
summary" would only hold on stdout, which nobody reads.

Two more checks.

5. A decision must declare the case it was made on. The four checks above
compare artifacts from the current run — the shortlist against the price
list, the accepted row against the shortlist — and are blind by construction
to a decisions file from another run: the management-software export is
usually the same file week to week, so most `(row, supplier)` pairs overlap
and the count still reconciles. Hence the fingerprint: `valuta_shortlist.py`
writes `ai_impronta_caso` on every decision, computed from what the model
actually saw, and it's recomputed here from today's shortlist. A mismatch
means the decision isn't from this run, and the pair goes back to the
reviewer.

The fingerprint is required: a decision without one isn't old, it's from a
producer that doesn't write it — accepting it would make the check bypassable
by simply omitting a field, exactly the gap `--decisions-attese` closes. The
two causes stay distinct in the returned data and message, since they point
at different places to look for the problem.

What the fingerprint proves, and what it doesn't. It proves the case is
identical to today's. If a supplier's price list hasn't changed since last
week, the case really is the same and last week's decision still applies —
that's the point of the answer memory. It does not say which model or prompt
produced the decision; for that, every row also carries `ai_modello`,
`ai_versione_prompt` and `ai_versione_avversario`, read here but not judged —
comparing them against the live configuration is the orchestrator's job, since
it's the orchestrator that holds that configuration.

6. A semantic pair without a shortlist is work nobody attempted. The
`--decisions-attese` count comes from the AI phase's own report, but that
report only counts the cases in the file it received: if there are 500
shortlists for 948 pairs, the phase evaluates 500, declares 500, this script
finds 500, and everything reconciles — because both sides are counting the
same missing work. `coda_semantica` and `coda_valutabile` are both reported
side by side in the summary so a reader can catch the gap; the script itself
does not compare them.

7. An accepted match carries its barcode to other suppliers. Case in point: the
management software identifies an item by one EAN, while two suppliers use a
different, shared EAN for the same item, with no EAN link between them; the
model accepted one supplier's row and rejected the other's (written with an
abbreviated description), so only one match went through on its own. But the
accepted row states a barcode, and the other supplier had a row with that same
barcode.

After decisions are made, for each product: if the model accepted a row at
supplier A with an EAN different from the product's own, then for every other
supplier S with no match and no row matching the product's own EAN, the one
usable row at S with that same EAN is proposed. Always pending confirmation,
regardless of A's confidence — the evidence comes from the model, not a
person, and a "no" already recorded at S (by row fingerprint) stays a no. This
never overrides an already-chosen row, a discarded decision, or an ambiguous
EAN; it does override an AI rejection at S, since that's the case it exists
for. Sources are snapshotted before anything is changed, so there are no
propagation chains and no dependency on supplier order.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


CARTELLA_SCRIPT = Path(__file__).resolve().parent
if str(CARTELLA_SCRIPT) not in sys.path:
    sys.path.insert(0, str(CARTELLA_SCRIPT))

# The fingerprint is defined by whoever writes the shortlists. Imported, not
# duplicated: two implementations of the same fingerprint would risk one
# silently drifting and accepting a file from another run.
from build_semantic_shortlists import impronta_caso  # noqa: E402


def uscita_in_utf8() -> None:
    """Force stdout and stderr to UTF-8, regardless of the console's codepage.

    On Windows a Python process writing to a pipe uses the system codepage
    (cp1252 here). The summary carries real product descriptions, and a
    single accented character is enough to produce bytes that aren't UTF-8
    for whatever reads the pipe. The output needs to be consistent across
    consoles for the orchestrator reading it downstream.
    """

    for flusso in (sys.stdout, sys.stderr):
        try:
            flusso.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover - stream already replaced
            pass


# Confidence level that goes on an order line with no on-screen confirmation.
CONFIDENZA_SENZA_CONFERMA = "ALTA"

# Method tag for rows entered because a different supplier's AI-accepted row
# shares the same barcode (rule 7 in the module docstring).
METODO_STESSO_CODICE = "EAN_DA_ALTRO_FORNITORE"
# Valid EAN/GTIN lengths. A short internal code or one made of zeros proves
# nothing and must not carry rows across price lists.
LUNGHEZZE_EAN = frozenset({8, 12, 13, 14})

# Above this score a rejection is worth a second look. Measured on a
# benchmark (150 cases, with adversarial verification): at 0.65 it flags 5 of
# the 13 wrong rejections and 5 of the 87 correct ones; at 0.60 it still
# flags the same 5 but noise rises to 12; at 0.80 only 3 of 13 remain. This
# is the point where the signal is still at least half of what gets shown.
SOGLIA_RIFIUTO_SOSPETTO = 0.65

# Exit codes, so the caller (`app/pipeline_jobs.py` or a shell) can tell
# "done" from "couldn't" without parsing text. They're a contract, pinned by a
# test.
USCITA_OK = 0
USCITA_INGRESSO_NON_UTILIZZABILE = 2
USCITA_DECISIONI_NON_RICONCILIATE = 3
USCITA_LISTINO_DISALLINEATO = 4
USCITA_RIGA_INVENTATA = 5


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def coppia(riga: Any, fornitore: Any) -> tuple[Any, str]:
    """The key that links matching, shortlist and decisions together.

    `"12"` and `12` are the same row. Without normalizing the row number here,
    a row written as text by one file and as a number by another would make
    every decision look "unmatched" and force a healthy run to redo the paid
    AI phase.

    A value that isn't an integer is left as-is: a pair that fails to link —
    visibly — is safer than two different rows silently collapsing into one.
    """

    if not isinstance(riga, bool):
        try:
            numero = float(str(riga).strip())
            if numero == int(numero):
                riga = int(numero)
        except (TypeError, ValueError, OverflowError):
            pass
    return (riga, str(fornitore))


def impronta_attesa(shortlist: dict[str, Any]) -> str:
    """The fingerprint a decision from this run must carry.

    Recomputed from today's shortlist with the same function that wrote it
    (`build_semantic_shortlists.impronta_caso`), passing it the three fields
    the model reads for each candidate.
    """

    return impronta_caso(
        shortlist.get("gestionale_source_row"),
        shortlist.get("supplier"),
        shortlist.get("description"),
        [
            (candidato.get("source_row"), candidato.get("description"), candidato.get("score"))
            for candidato in shortlist.get("candidates") or []
            if isinstance(candidato, dict)
        ],
    )


def prezzo_confrontabile(valore: Any) -> float | None:
    """The price as a number, from whatever shape it arrives in. `None` if it isn't one.

    `prepare_sources.py` writes prices as strings (`json_decimal` serializes
    with `format(..., "f")`, giving `"2.1000"`), so this must accept strings
    as well as numbers — otherwise the price component of `identita()` never
    matches anything.

    The reverse direction matters just as much: if one artifact carries `1.0`
    and another `"1.0"`, two spellings of the same number must not become two
    different products.
    """

    if isinstance(valore, bool) or valore is None:
        return None
    if isinstance(valore, (int, float)):
        return round(float(valore), 6)
    try:
        return round(float(str(valore).strip().replace(",", ".")), 6)
    except (TypeError, ValueError):
        return None


def identita(record: dict[str, Any]) -> tuple[str, str, float | None]:
    """What proves two rows are the same product.

    Not the row number, which is a position and shifts when the supplier adds
    a row above it. EAN, description and price: the three fields both the
    shortlist and the normalized price list carry.

    Price is included because the row that ends up on the order is the row
    the price is taken from, and it must be the same row. The model never
    sees the price, correctly so — it has no bearing on product identity.
    Among usable rows (the only ones that can enter a shortlist or an order),
    duplicate identities are rare in real price lists and none carry
    different prices; the field is included at negligible cost, as a
    safeguard in case a supplier ever prices two lots of the same item
    differently.

    `or ""`, not `get(field, "")`: in real price lists a missing EAN arrives
    from JSON as `null`, not as an absent key, and `null` against `""` would
    be a false mismatch that stops a good run.
    """

    return (
        str(record.get("ean") or "").strip(),
        " ".join(str(record.get("description") or "").split()).upper(),
        prezzo_confrontabile(record.get("unit_price_net")),
    )


def leggibile(chi: tuple[str, str, float | None] | None) -> str | None:
    """The identity in human-readable form, for the summary."""

    if chi is None:
        return None
    return chi[1] or chi[0] or "(riga senza descrizione né EAN)"


def miglior_punteggio(shortlist: dict[str, Any]) -> float | None:
    """The score of the best candidate shown to the AI.

    Does not read `candidates[0]`: ordering is a property of whoever wrote the
    shortlist, and this function shouldn't depend on it.
    """
    punteggi = [
        candidato.get("score")
        for candidato in shortlist.get("candidates") or []
        if isinstance(candidato.get("score"), (int, float)) and not isinstance(candidato.get("score"), bool)
    ]
    return float(max(punteggi)) if punteggi else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matching", type=Path, required=True)
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--shortlists", type=Path, required=True)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument(
        "--decisions-attese",
        type=int,
        required=True,
        help=(
            "Quante decisioni la fase AI dichiara di aver prodotto. Obbligatorio: "
            "facoltativo bastava dimenticarlo per far passare una fase AI mai "
            "eseguita. Zero è legittimo e vuol dire «la fase AI è degradata e "
            "lo sa»."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def carica_decisioni(args: argparse.Namespace) -> tuple[list[Any] | None, int, str]:
    """The decisions, or the reason processing can't continue."""

    if args.decisions is None:
        return [], USCITA_OK, ""
    if not args.decisions.exists():
        # A declared path that doesn't exist is a failure, not an empty file.
        return None, USCITA_DECISIONI_NON_RICONCILIATE, f"File delle decisioni non trovato: {args.decisions}"
    try:
        decisioni = load(args.decisions)
    except (OSError, ValueError) as errore:
        return None, USCITA_INGRESSO_NON_UTILIZZABILE, f"File delle decisioni illeggibile: {errore}"
    if not isinstance(decisioni, list):
        return None, USCITA_INGRESSO_NON_UTILIZZABILE, (
            "Il file delle decisioni deve essere una lista, non "
            f"{type(decisioni).__name__}."
        )
    return decisioni, USCITA_OK, ""


def indicizza_decisioni(decisioni: list[Any]) -> tuple[dict[tuple[int, str], dict[str, Any]] | None, int, str]:
    indice: dict[tuple[int, str], dict[str, Any]] = {}
    for decisione in decisioni:
        if not isinstance(decisione, dict):
            return None, USCITA_INGRESSO_NON_UTILIZZABILE, "Una decisione non è un oggetto."
        try:
            chiave = coppia(int(decisione["gestionale_source_row"]), decisione["supplier"])
        except (KeyError, TypeError, ValueError) as errore:
            return None, USCITA_INGRESSO_NON_UTILIZZABILE, f"Decisione senza coppia utilizzabile: {errore}"
        if chiave in indice:
            # The last row in the file wins, and an ACCEPT can turn into a
            # REJECT — the product would silently disappear because of row
            # order in the file, not because of a decision anyone made.
            return None, USCITA_DECISIONI_NON_RICONCILIATE, f"Decisione duplicata: {chiave}"
        if decisione.get("action") not in {"ACCEPT", "REJECT", "UNRESOLVED"}:
            return None, USCITA_INGRESSO_NON_UTILIZZABILE, f"Azione AI non valida: {decisione.get('action')}"
        if decisione["action"] == "ACCEPT":
            # An ACCEPT with no row is a malformed decision, not a model that
            # hallucinated a row: the client's schema allows `source_row:
            # null`. The row is normalized to an int the same way as the pair
            # key, since a different producer may write it as text.
            try:
                decisione = {**decisione, "source_row": int(decisione["source_row"])}
            except (KeyError, TypeError, ValueError):
                return None, USCITA_INGRESSO_NON_UTILIZZABILE, (
                    f"ACCEPT senza una riga utilizzabile per {chiave}: "
                    f"{decisione.get('source_row')!r}"
                )
        indice[chiave] = decisione
    return indice, USCITA_OK, ""


def main() -> int:
    uscita_in_utf8()
    args = parse_args()
    try:
        matching = load(args.matching)
        normalized = load(args.normalized)
        shortlists = load(args.shortlists)
    except (OSError, ValueError) as errore:
        print(f"Ingresso non utilizzabile: {errore}")
        return USCITA_INGRESSO_NON_UTILIZZABILE

    decisions, codice, messaggio = carica_decisioni(args)
    if decisions is None:
        print(messaggio)
        return codice
    decision_index, codice, messaggio = indicizza_decisioni(decisions)
    if decision_index is None:
        print(messaggio)
        return codice

    shortlist_index = {
        coppia(item["gestionale_source_row"], item["supplier"]): item for item in shortlists
    }

    # The semantic queue is the work the AI phase had ahead of it: every
    # product-supplier pair the EAN match didn't already resolve. `valutabile`
    # drops the cases with no candidate at all, which the client refuses to
    # send to the model — without this count, an otherwise honest summary can
    # look like it's missing work.
    coda_semantica = sum(
        1
        for product in matching
        for match in product["suppliers"].values()
        if match["status"] != "EAN_ESATTO"
    )
    coda_valutabile = sum(1 for item in shortlists if item.get("candidates"))

    if len(decision_index) != args.decisions_attese:
        print(json.dumps({
            "errore": "decisioni non riconciliate",
            "dichiarate": args.decisions_attese,
            "trovate": len(decision_index),
            "coda_semantica": coda_semantica,
            "coda_valutabile": coda_valutabile,
        }, ensure_ascii=False, indent=2))
        return USCITA_DECISIONI_NON_RICONCILIATE

    output = []
    normalized_index = {
        supplier: {record.get("source_row"): record for record in records}
        for supplier, records in normalized.items()
        if supplier != "gestionale" and isinstance(records, list)
    }
    righe_per_codice = indice_per_codice(normalized)
    propagati: list[dict[str, Any]] = []
    stesso_codice_ambiguo: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    # Two counters worth tracking: how many matches the AI put on an order
    # with no confirmation asked, and how many rejections had a candidate that
    # scored high. The first shows how much weight `ALTA` trust carries, the
    # second is the only remaining trace of a possibly wrong rejection.
    accettati_senza_conferma = 0
    rifiuti_sospetti = 0
    # Every accepted row that has stopped being the product the model was shown,
    # and every row the model was never shown at all.
    disallineamenti: list[dict[str, Any]] = []
    righe_inventate: list[dict[str, Any]] = []
    ean_traditi: list[dict[str, Any]] = []
    # Decisions made on a case that has since diverged from today's: a decisions
    # file from another run, or a shortlist rebuilt after the AI phase ran.
    di_un_altra_run: list[dict[str, Any]] = []
    # Semantic pairs for which a shortlist was never written: never
    # evaluated, and nobody even attempted them.
    senza_shortlist: list[dict[str, Any]] = []
    # Decisions that found their matching pair. Ones that don't are a sign
    # the two files aren't from the same run.
    con_riscontro: set[tuple[int, str]] = set()
    # Pairs whose key exists but the EAN match has since resolved on its own.
    # Not a failure: this happens whenever the deterministic steps are rerun
    # after the AI phase, and the result is better than what the AI proposed.
    # Counting these as unmatched would stop a healthy run and force a costly
    # re-run of the AI phase.
    superate_dall_ean: set[tuple[int, str]] = set()
    for product in matching:
        row = product["gestionale"]["source_row"]
        resolved = {"gestionale": product["gestionale"], "suppliers": {}}
        for supplier, match in product["suppliers"].items():
            key = coppia(row, supplier)
            if match["status"] == "EAN_ESATTO":
                if key in decision_index:
                    superate_dall_ean.add(key)
                selected = match["usable_candidates"][0]
                result = {
                    "status": "EAN_ESATTO",
                    "method": "EAN",
                    "selected": selected,
                    "confidence": "CERTA",
                    "requires_user_confirmation": False,
                    "rationale": "EAN gestionale presente in una sola riga utilizzabile.",
                    "alternatives": [],
                }
            else:
                shortlist = shortlist_index.get(key)
                # A semantic pair with no shortlist is work nobody even
                # attempted. It must be tracked separately, not folded into
                # "the AI didn't decide": both `--decisions-attese` and this
                # script's own count derive from the same truncated shortlist
                # file, so counts can reconcile perfectly while a large chunk
                # of the queue was silently dropped upstream.
                if shortlist is None:
                    senza_shortlist.append({"gestionale_source_row": row, "supplier": supplier})
                    shortlist = {"candidates": []}
                mostrati = {
                    candidato.get("source_row"): candidato
                    for candidato in shortlist.get("candidates", [])
                }
                decision = decision_index.get(key)
                # Before checking what the decision says, check what case it
                # was made on: a decision from another case doesn't deserve
                # any of the other checks, and calling it a "hallucinated
                # row" would point the reader at the model instead of the
                # files.
                stantia = False
                senza_impronta = False
                if decision is not None:
                    con_riscontro.add(key)
                    attesa = impronta_attesa(shortlist)
                    dichiarata = decision.get("ai_impronta_caso")
                    stantia = dichiarata != attesa
                    senza_impronta = dichiarata is None
                    if stantia:
                        di_un_altra_run.append({
                            "gestionale_source_row": row,
                            "supplier": supplier,
                            "articolo": shortlist.get("description"),
                            # Only included if it adds information: in the
                            # case this check catches, the searched item is
                            # the same and only the candidates changed, so two
                            # always-equal fields would look like noise.
                            **(
                                {"articolo_della_decisione": decision.get("ai_articolo_mostrato")}
                                if decision.get("ai_articolo_mostrato") != shortlist.get("description")
                                else {}
                            ),
                            "impronta_dichiarata": dichiarata,
                            "impronta_attesa": attesa,
                        })
                if stantia:
                    # Two distinct causes, since they point at different
                    # places to look: a missing fingerprint is a producer
                    # that doesn't write it, a mismatching one is a file from
                    # another run.
                    result = scartata(
                        shortlist,
                        "Decisione AI scartata: non dichiara su quale caso è stata presa."
                        if senza_impronta
                        else "Decisione AI scartata: è stata presa su un caso diverso da "
                        "quello di questa run.",
                        "DECISIONE_SENZA_IMPRONTA" if senza_impronta else "DECISIONE_DI_UNA_ALTRA_RUN",
                    )
                elif decision and decision["action"] == "ACCEPT":
                    source_row = decision.get("source_row")
                    candidate = mostrati.get(source_row)
                    # On an EAN_AMBIGUO case the correct row isn't a matter of
                    # opinion: the supplier has several rows with the
                    # management software's EAN, and the choice must be
                    # among those. The model only sees the shortlist, which is
                    # never EAN-ranked (`build_semantic_shortlists` sorts by
                    # description tokens), so without this check it could pick
                    # an unrelated row, and an `ALTA` decision would go
                    # straight onto the order.
                    ean_ammessi = {
                        c.get("source_row") for c in match.get("usable_candidates", [])
                    } if match["status"] == "EAN_AMBIGUO" else None
                    if ean_ammessi is not None and source_row not in ean_ammessi:
                        ean_traditi.append({
                            "gestionale_source_row": row,
                            "supplier": supplier,
                            "source_row": source_row,
                            "righe_con_l_ean_del_gestionale": sorted(r for r in ean_ammessi if r is not None),
                        })
                        result = scartata(
                            shortlist,
                            f"Decisione AI scartata: la riga {source_row} non ha l'EAN del "
                            "prodotto, e per questa coppia l'EAN lo decide.",
                            "EAN_NON_RISPETTATO",
                        )
                    elif candidate is None:
                        # The model named a row it was never shown. This gets
                        # its own outcome, distinct from a shifted price
                        # list, since the two call for different fixes.
                        righe_inventate.append({
                            "gestionale_source_row": row,
                            "supplier": supplier,
                            "source_row": source_row,
                            "righe_mostrate": sorted(r for r in mostrati if r is not None),
                        })
                        result = scartata(
                            shortlist,
                            f"Decisione AI scartata: la riga {source_row} non è fra quelle "
                            "mostrate al modello.",
                            "RIGA_NON_MOSTRATA",
                        )
                    else:
                        # The full record lives in the normalized price list
                        # — the shortlist doesn't carry the order multiplier
                        # — but it must be the same product the model saw.
                        selected = normalized_index.get(supplier, {}).get(source_row)
                        atteso = identita(candidate)
                        trovato = identita(selected) if selected is not None else None
                        if trovato != atteso:
                            disallineamenti.append({
                                "gestionale_source_row": row,
                                "supplier": supplier,
                                "source_row": source_row,
                                "mostrato_al_modello": leggibile(atteso),
                                "trovato_nel_listino": leggibile(trovato),
                            })
                            result = scartata(
                                shortlist,
                                f"Decisione AI scartata: la riga {source_row} del listino "
                                "non è più il prodotto mostrato al modello.",
                                "LISTINO_DISALLINEATO",
                            )
                        else:
                            confidenza = decision.get("confidence", "BASSA")
                            result = {
                                "status": "SEMANTICO_PROPOSTO" if match["status"] != "EAN_AMBIGUO" else "EAN_AMBIGUO_RISOLTO_AI",
                                "method": "SEMANTICO_AI" if match["status"] != "EAN_AMBIGUO" else "EAN_AI",
                                "selected": selected,
                                "confidence": confidenza,
                                # Decided here, not in the client: this is the
                                # line that implements the ALTA-skips-confirmation
                                # rule described in the module docstring.
                                "requires_user_confirmation": confidenza != CONFIDENZA_SENZA_CONFERMA,
                                "rationale": decision.get("rationale", ""),
                                "alternatives": shortlist.get("candidates", []),
                            }
                            if not result["requires_user_confirmation"]:
                                accettati_senza_conferma += 1
                elif decision and decision["action"] == "REJECT":
                    punteggio = miglior_punteggio(shortlist)
                    result = {
                        "status": "NON_TROVATO",
                        "method": "AI_RIFIUTATO",
                        "selected": None,
                        "confidence": decision.get("confidence", ""),
                        "requires_user_confirmation": False,
                        "rationale": decision.get("rationale", "Nessun candidato equivalente."),
                        "alternatives": shortlist.get("candidates", []),
                        # The score is always included, even when low: the
                        # threshold is applied by the consumer, in one place.
                        # `None` means "shortlist with no scores", distinct
                        # from a score of zero.
                        "ai_reject_best_score": punteggio,
                    }
                else:
                    result = {
                        "status": "DA_VERIFICARE",
                        "method": "REVISIONE",
                        "selected": None,
                        "confidence": decision.get("confidence", "") if decision else "",
                        "requires_user_confirmation": True,
                        "rationale": (
                            decision.get("rationale", "Decisione AI non disponibile o non conclusiva.")
                            if decision
                            else "Nessuna shortlist per questa coppia: i candidati non sono mai stati prodotti."
                            if not shortlist.get("candidates")
                            else "Decisione AI da effettuare."
                        ),
                        "alternatives": shortlist.get("candidates", []),
                    }
            resolved["suppliers"][supplier] = result
        # After all of the product's suppliers have a decision, not during:
        # the source row can belong to a supplier later in the iteration.
        nuovi, ambigui = propaga_lo_stesso_codice(product, resolved, righe_per_codice)
        propagati.extend(nuovi)
        stesso_codice_ambiguo.extend(ambigui)
        for result in resolved["suppliers"].values():
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            # Counted here, not at decision time: a rejection rule 7 has since
            # replaced with a proposal stops counting as a rejection to flag.
            punteggio = result.get("ai_reject_best_score")
            if (
                result.get("method") == "AI_RIFIUTATO"
                and punteggio is not None
                and punteggio >= SOGLIA_RIFIUTO_SOSPETTO
            ):
                rifiuti_sospetti += 1
        output.append(resolved)

    senza_riscontro = sorted(set(decision_index) - con_riscontro - superate_dall_ean)
    riepilogo = {
        "products": len(output),
        "supplier_results": counts,
        "coda_semantica": coda_semantica,
        "coda_valutabile": coda_valutabile,
        "decisioni_lette": len(decision_index),
        "decisioni_con_riscontro": len(con_riscontro),
        "decisioni_superate_dall_ean": len(superate_dall_ean),
        "decisioni_senza_riscontro": len(senza_riscontro),
        "accettati_senza_conferma": accettati_senza_conferma,
        "rifiuti_con_candidato_forte": rifiuti_sospetti,
        "soglia_rifiuto_sospetto": SOGLIA_RIFIUTO_SOSPETTO,
        "decisioni_scartate_per_disallineamento": len(disallineamenti),
        "decisioni_scartate_per_riga_inventata": len(righe_inventate),
        "decisioni_scartate_per_ean_non_rispettato": len(ean_traditi),
        "decisioni_scartate_perche_di_un_altra_run": len(di_un_altra_run),
        "coppie_senza_shortlist": len(senza_shortlist),
        "abbinamenti_per_stesso_codice": len(propagati),
        "stesso_codice_ambiguo": len(stesso_codice_ambiguo),
    }
    # Examples are truncated, counts aren't: ten rows are enough to show what
    # happened, and it's the full count that matters.
    if disallineamenti:
        riepilogo["disallineamenti"] = disallineamenti[:10]
    if righe_inventate:
        riepilogo["righe_inventate"] = righe_inventate[:10]
    if ean_traditi:
        riepilogo["ean_non_rispettato"] = ean_traditi[:10]
    if di_un_altra_run:
        riepilogo["di_un_altra_run"] = di_un_altra_run[:10]
    if senza_shortlist:
        riepilogo["senza_shortlist"] = senza_shortlist[:10]
    if propagati:
        riepilogo["per_stesso_codice"] = propagati[:10]
    if stesso_codice_ambiguo:
        riepilogo["stesso_codice_ambiguo_esempi"] = stesso_codice_ambiguo[:10]
    if senza_riscontro:
        riepilogo["coppie_senza_riscontro"] = [list(chiave) for chiave in senza_riscontro[:10]]

    # The file is always written: broken pairs are already degraded to
    # DA_VERIFICARE, so it's an honest artifact. See the module docstring.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(riepilogo, ensure_ascii=False, indent=2))

    # Ordered from most severe: a row outside the shown set or missing the
    # required EAN means the model answered badly; a misaligned price list
    # means the artifacts came from different reads; unmatched decisions mean
    # the files aren't from the same run.
    if righe_inventate or ean_traditi:
        return USCITA_RIGA_INVENTATA
    if disallineamenti:
        return USCITA_LISTINO_DISALLINEATO
    # A decision made on a different case, one with no matching pair, and a
    # pair that never had a shortlist all say the same thing — the artifacts
    # don't describe the same run — and get the same exit code: the caller
    # needs to redo the candidates and the AI phase, not reread the price
    # list.
    if di_un_altra_run or senza_riscontro or senza_shortlist:
        return USCITA_DECISIONI_NON_RICONCILIATE
    return USCITA_OK


def codice_ean(valore: Any) -> str:
    """A barcode as compared here: raw digits, only if shaped like a real EAN.

    Same rule `build_matching` uses on its own EAN path. Empty string
    otherwise.
    """

    codice = str(valore or "").strip()
    if codice.isdigit() and len(codice) in LUNGHEZZE_EAN and codice.strip("0"):
        return codice
    return ""


def indice_per_codice(normalized: Any) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Per supplier, the usable rows for each barcode.

    "Usable" with the same predicate as `build_matching`: a free-goods row, a
    display component or a row with no price is never proposed.
    """

    indice: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if not isinstance(normalized, dict):
        return indice
    for fornitore, righe in normalized.items():
        if fornitore == "gestionale" or not isinstance(righe, list):
            continue
        per_codice: dict[str, list[dict[str, Any]]] = {}
        for riga in righe:
            if not isinstance(riga, dict) or not riga.get("usable", True):
                continue
            codice = codice_ean(riga.get("ean"))
            if codice:
                per_codice.setdefault(codice, []).append(riga)
        indice[fornitore] = per_codice
    return indice


def propaga_lo_stesso_codice(
    prodotto: dict[str, Any],
    risolto: dict[str, Any],
    righe_per_codice: dict[str, dict[str, list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rule 7: an AI-accepted row's barcode also applies at other suppliers.

    Modifies `risolto` in place and returns the changed pairs and the ones
    skipped as ambiguous, for the summary. Never raises: the results file is
    always written.
    """

    codice_prodotto = str((prodotto.get("gestionale") or {}).get("ean") or "").strip()
    stati_ean = prodotto.get("suppliers") or {}
    esiti = risolto.get("suppliers") or {}

    # Sources snapshotted before anything is touched: a propagated row never
    # becomes a source itself, and supplier order doesn't affect the result.
    fonti: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for fornitore, esito in esiti.items():
        selezionata = (esito or {}).get("selected")
        if (esito or {}).get("method") != "SEMANTICO_AI" or not isinstance(selezionata, dict):
            continue
        codice = codice_ean(selezionata.get("ean"))
        if codice and codice != codice_prodotto:
            fonti.setdefault(codice, []).append((fornitore, selezionata))
    if not fonti:
        return [], []

    propagati: list[dict[str, Any]] = []
    ambigui: list[dict[str, Any]] = []
    riga_gestionale = (prodotto.get("gestionale") or {}).get("source_row")
    for fornitore, esito in esiti.items():
        esito = esito or {}
        if (
            esito.get("selected")
            or esito.get("ai_decisione_scartata")
            or (stati_ean.get(fornitore) or {}).get("status") != "EAN_ASSENTE"
        ):
            continue
        trovate = [
            (codice, riga)
            for codice in sorted(fonti)
            for riga in (righe_per_codice.get(fornitore) or {}).get(codice, [])
        ]
        if not trovate:
            continue
        if len(trovate) > 1:
            ambigui.append({
                "gestionale_source_row": riga_gestionale,
                "supplier": fornitore,
                "righe": sorted(str(riga.get("source_row")) for _, riga in trovate),
            })
            continue
        codice, riga = trovate[0]
        da_chi = sorted(chi for chi, _ in fonti[codice])
        # The description from the alphabetically first source, not the
        # first one found: the message must not depend on file order.
        come_la_scrive = dict(fonti[codice])[da_chi[0]].get("description") or ""
        esiti[fornitore] = {
            "status": "SEMANTICO_PROPOSTO",
            "method": METODO_STESSO_CODICE,
            "selected": riga,
            # Never ALTA: the evidence comes from the model on a different
            # price list.
            "confidence": "MEDIA",
            "requires_user_confirmation": True,
            "rationale": (
                f"{' e '.join(chi.upper() for chi in da_chi)} {'ha' if len(da_chi) == 1 else 'hanno'} "
                f"questo prodotto con il codice a "
                f"barre {codice} («{come_la_scrive}»), abbinato dall'analisi automatica. Anche "
                f"{fornitore.upper()} ha una riga con quel codice: controlla che sia lo stesso articolo."
            ),
            "alternatives": esito.get("alternatives", []),
            "propagato_da": da_chi,
            "codice_propagato": codice,
            # What was there before, to trace back who decided what.
            "prima": {
                chiave: esito.get(chiave)
                for chiave in ("status", "method", "confidence", "rationale")
            },
        }
        propagati.append({
            "gestionale_source_row": riga_gestionale,
            "supplier": fornitore,
            "source_row": riga.get("source_row"),
            "codice": codice,
            "da": da_chi,
        })
    return propagati, ambigui


def scartata(shortlist: dict[str, Any], motivo: str, causa: str) -> dict[str, Any]:
    """A pair whose AI decision was discarded: goes back to the reviewer.

    Not `NON_TROVATO`, which means "the AI looked and said no": here the AI
    said yes and the answer couldn't be trusted, which needs a human look, not
    removal from the comparison.

    `ai_decisione_scartata` isn't decoration: it's the field
    `build_review_data.py` uses to count these cases at the top of the page.
    Without it, a degraded product would be indistinguishable in the list
    from one the AI never evaluated.
    """

    return {
        "status": "DA_VERIFICARE",
        "method": "REVISIONE",
        "selected": None,
        "confidence": "",
        "requires_user_confirmation": True,
        "rationale": motivo,
        "alternatives": shortlist.get("candidates", []),
        "ai_decisione_scartata": causa,
    }


if __name__ == "__main__":
    raise SystemExit(main())
