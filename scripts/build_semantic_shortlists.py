#!/usr/bin/env python3
"""Build deterministic semantic candidate shortlists without deciding matches.

Rows sharing the product's EAN always enter the shortlist. Description
scoring alone is not reliable enough: measured on 1160 cases where the right
row is known for certain because the EAN identifies it, a description-only
shortlist would have surfaced it only 1014 times out of 1160. The 12.6%
misses aren't marginal cases either — two descriptions of the same product
can differ enough in wording and abbreviation to share no useful tokens at
all (e.g. `CHANTE BRILL ANTICALCARE ACETO 625ML` vs `CHANTEBR. A/CALCARE 625
EXTRARAPIDO`).

For a missing EAN there's no remedy, and that's the normal case. But when a
supplier has several usable rows sharing the same EAN and the right one has
to be chosen among them, the correct row is guaranteed to be one of those —
so forcing them all into the shortlist is an avoidable source of error.

This also matters downstream: `merge_match_decisions.py` requires that, for
that ambiguous case, the accepted row carries the product's EAN. Without this
forcing, that rule would be unsatisfiable and every such case would stall."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


STOPWORDS = {
    "A", "AL", "ALLA", "CON", "DA", "DAL", "DE", "DEL", "DELLA", "DI", "E",
    "IL", "IN", "LA", "LE", "LO", "NEW", "PER", "PIU", "THE", "UN", "UNA",
}
ALIASES = {
    "DEODORANTE": "DEO",
    "CANDEG": "CANDEGGINA",
    "CANDEGG": "CANDEGGINA",
    "RIC": "RICARICA",
    "RICAR": "RICARICA",
    "PZ": "PEZZI",
    "PZZ": "PEZZI",
    "LT": "L",
    "LITRO": "L",
    "LITRI": "L",
    "GR": "G",
    "GRAMMI": "G",
}


def _confrontabile(valore: Any) -> Any:
    """A number as a number, everything else as text. Never raises.

    Used only inside the fingerprint, whose two callers read data from
    different sources: `valuta_shortlist.py` passes what it actually sent to
    the model (ints and floats), `merge_match_decisions.py` passes whatever
    it finds in the file. `441` and `"441"` are the same row number and must
    not produce two different fingerprints — and a malformed value must not
    crash a comparison whose whole job is to say "this file is no good".
    """

    if isinstance(valore, bool) or valore is None:
        return str(valore)
    try:
        return round(float(valore if isinstance(valore, (int, float)) else str(valore).strip()), 6)
    except (TypeError, ValueError, OverflowError):
        # `OverflowError` isn't theoretical: a corrupted or foreign-written
        # JSON integer hundreds of digits long can raise it here, and would
        # take down `merge_match_decisions.py` before it wrote
        # `resolved_matches.json` — leaving the previous run's file on disk
        # to be read as fresh, exactly the failure this module exists to
        # avoid. A function whose job is to say "this file is no good" must
        # not crash while saying it.
        return str(valore)


def impronta_caso(
    gestionale_source_row: Any,
    supplier: Any,
    description: Any,
    candidati: Iterable[tuple[Any, Any, Any]],
) -> str:
    """Sixteen hex digits describing exactly what the model saw for one case.

    The merge step's guards compare the accepted row against the shortlist
    and the shortlist against the price list, but both artifacts belong to
    the current run: against a decisions file produced last week they are
    blind by construction, because the management-software export is the
    same file and the `(row, supplier)` pairs mostly overlap. An old
    decision could then apply to a new shortlist, letting a high-confidence
    `ACCEPT` into the order unreviewed.

    The fingerprint closes that gap: `valuta_shortlist.py` stamps it onto
    every decision, `merge_match_decisions.py` recomputes it from today's
    shortlist and discards any decision that doesn't match.

    Covers every candidate, not just the accepted one, because an old
    `REJECT` is dangerous too: it says "none of these match" about a list
    that has since changed, silently dropping the product out of the
    comparison for that supplier.

    `candidati` is a sequence of `(source_row, description, score)` triples —
    exactly the three fields the model reads (`Candidato` carries no EAN or
    price). Order matters, since it's the order the model saw them in."""

    canonico = json.dumps(
        {
            "riga": _confrontabile(gestionale_source_row),
            "fornitore": str(supplier),
            "articolo": str(description),
            "candidati": [
                [_confrontabile(riga), str(descrizione), _confrontabile(punteggio)]
                for riga, descrizione, punteggio in candidati
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()[:16]


def normalize_text(value: Any, *, tieni_il_piu: bool = False) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").upper())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^A-Z0-9.,+]+" if tieni_il_piu else r"[^A-Z0-9.,]+", " ", text)
    return " ".join(text.split())


def tokens(value: Any) -> set[str]:
    result = set()
    for token in normalize_text(value).split():
        normalized = ALIASES.get(token, token)
        if normalized not in STOPWORDS and len(normalized) > 1:
            result.add(normalized)
    return result


# The quantities written into a product name, to compare two formats:
# "18PZ" vs "X 9" must read as "different pack sizes", and a conflict
# subtracts 0.35 from the score.
#
# Forms with the unit written before the number are read too — `X 18`,
# `PZ.18`, `ML.500`, `LT.3`, `KG 4` — alongside the more common
# unit-after-number forms like `3LT` and `250GR`. Without reading both
# directions, two rows for the same product can fail to match because
# neither one's quantity is recognized, and the comparison silently falls
# back to picking a different, cheaper-looking product instead.
#
# Measured against the reorder list ranked against LARICE and NOCE (355 and
# 308 products respectively, where the correct row is known for certain
# because the EAN matches; EAN hidden, ranked by description alone): correct
# row in first place rose from 199 to 215 and from 220 to 240; in the top
# five, from 292 to 302 and from 283 to 284.
#
# The guiding principle: better to not read a number than to misread it. A
# false conflict subtracts 0.35 from exactly the correct row's score; a
# number that isn't recognized just leaves the score unchanged.
#
# This score feeds into both the text sent to the model and the AI memory
# key (`ai_client._caso_serializzato`): changing this parsing logic causes
# the model to be re-asked only for the cases whose score actually changes,
# not for all of them.
_UNITA_LETTE = {
    "ML": "ML", "L": "L", "LT": "L", "LITRI": "L", "LITRO": "L",
    "KG": "KG", "G": "G", "GR": "G",
    "PZ": "PEZZI", "PEZZI": "PEZZI",
    "ROT": "ROTOLI", "ROTOLI": "ROTOLI",
    "LAV": "LAVAGGI", "LAVAGGI": "LAVAGGI", "MIS": "LAVAGGI", "MISURINI": "LAVAGGI",
    "NOTTI": "NOTTI",
}
_NUMERO = r"\d+(?:\.\d+)?"
_UNITA_DOPO = re.compile(
    rf"({_NUMERO}) ?(ML|LT|LITRI|LITRO|L|KG|GR|G|PEZZI|PZ|ROTOLI|ROT|LAVAGGI|LAV|MISURINI|MIS|NOTTI)\b"
)
# Unit before the number: "X 18", "PZ.18", "ML.500", "LT.3", "KG4". A period
# right before the unit is allowed ("SALVACAM.ML.150", "VER.KG.1,5"), a
# letter or digit isn't. The number can't be glued to other letters
# ("SH. 250 ML 2IN1" isn't 2 ml), except for a multiplier "X" ("GR.50X6").
_UNITA_DAVANTI = re.compile(rf"(?<![A-Z0-9])(X|PZ|PEZZI|ML|LT|KG|GR)\.? ?({_NUMERO})(?![\dA-WYZ])")
# A unit right after an "X" means the X is a "times", not a pack count: "2 X
# 250ML", "X 10 PZ". Unless that unit has its own number attached: in
# "X 2 GR.90" the X counts individual bars and the 90 is the grams of each.
_UNITA_DOPO_LA_X = re.compile(
    r" ?(ML|LT|L|KG|GR|G|CL|CM|MM|MT|M|PZ|PEZZI|LAV|LAVAGGI|MIS|MISURINI|NOTTI|ROT|ROTOLI|ANNI|MESI)\b(?!\.? ?\d)"
)
# A unit right after a number: means that number already has its own unit.
_UNITA_SUBITO_DOPO = re.compile(
    r" ?(ML|LT|LITRI|LITRO|L|KG|GR|G|PEZZI|PZ|ROTOLI|ROT|LAVAGGI|LAV|MISURINI|MIS|NOTTI)\b"
)
# Only a "+" glued directly between two numbers: "70+8 LAV", "PZ.8+2",
# "500+250ML". Six digits are enough for any real quantity, and a
# thousands-of-digits number would make `int()` raise and take down the
# whole shortlist step.
_SOMMA = re.compile(r"(?<!\d)(?<!\d\.)(\d{1,6})((?:\+\d{1,6})+)(?!\d)(?!\.\d)")
# "45+" (a cream's age rating), "FP50+", "6+ ANNI": not quantities.
_ETA = re.compile(r"(?<![\d.])\d+\+(?!\d)")
# Measurements that aren't the pack quantity, stripped from the raw text
# before parsing (normalization would turn "-", "/", "°" and "=" into
# spaces, leaving loose numbers behind): weight ranges ("7-18 KG" for a
# child, "KG. 11/25", "0-6" years), sizes ("5°MIS.") and formulas
# ("2X13=26"). Confirmed by an adversarial check against several diaper
# products, which without this were reading a different child weight per
# price list.
# A weight range takes its "KG" with it wherever it appears, before or
# after: left loose, it would attach to the nearest unrelated number
# ("11-25KG 14 P" would misread as 14 kg). Only KG is stripped this way,
# since real weight ranges in these price lists are for children or
# animals, and in "60 ML 0-6" or "2/1 ML 250" the ML belongs to the
# adjacent number, not to the range.
_FASCIA = re.compile(
    r"(?:(?<![A-Z])KG\.?\s*)?"
    r"\d+(?:[.,]\d+)?\s*[-/]\s*\d+(?:[.,]\d+)?"
    r"(?:\s*(?:KG|ANNI|MESI)(?![A-Z])\.?)?"
)
_ORDINALE = re.compile(r"\d+\s*[°ºª]")
_FORMULA = re.compile(r"\d+\s*X\s*\d+\s*=\s*\d+")


def _unita_davanti_girata(trovato: re.Match[str]) -> str:
    """Turns "X 18" into "18 PZ", "ML.500" into "500 ML"; leaves measurements alone.

    A number immediately before means this isn't a unit-before-number form
    but a dimension or a code: "30 X 40 CM", "10 PZ 1276", "25 LT 3". An "X"
    followed by a unit is a "times": "X 250ML" stays 250 ML, not 250 pieces.

    Except for the unit glued with a period, which is always the
    unit-before-number form: in "X 2 GR.90" and "4 IN 1 GR.900" the grams
    are 90 and 900, not 2 and 1. Left unhandled, the general unit-after
    pattern would glue the unit to the preceding number instead and produce
    a false conflict on the correct row. A separator pipe keeps it apart
    from the number before it.
    """

    unita, numero = trovato.group(1), trovato.group(2)
    parole_prima = trovato.string[: trovato.start()].split()
    prima = parole_prima[-1] if parole_prima else ""
    if re.fullmatch(_NUMERO, prima):
        # With a number right before, the unit could belong to it or to the
        # number that follows. Three forms, as seen in real price lists:
        #   "GR.90" (period glued to unit)  -> always belongs to the number
        #       that follows;
        #   "GR. 500" (period, then space)  -> belongs to the following
        #       number only if the number before it counts pieces
        #       ("X 2 GR. 500"); in "ADDITIVO 500 GR. 100 PIU'" the grams
        #       are 500;
        #   "ML 200" (space only)  -> e.g. "PH 3.5 ML 200": belongs to the
        #       following number if it's a volume or weight and that number
        #       has no unit of its own ("50 LT 10 PZ" is 50 liters). Not for
        #       pieces: a "PZ" is often followed by an item code
        #       ("10 PZ 1276").
        dopo_l_unita = trovato.group(0)[len(unita):]
        pezzi_prima = len(parole_prima) > 1 and parole_prima[-2] == "X"
        if unita == "X":
            separa = False
        elif re.match(r"\.\d", dopo_l_unita):
            separa = True
        elif dopo_l_unita.startswith("."):
            separa = pezzi_prima
        else:
            # ...and only if the following number is larger: "PH 3.5 ML 200"
            # yes, "CHICCA BIBERON 330 ML 3 FORI" no (330 ml and 3 openings).
            separa = (
                unita in {"ML", "LT", "KG", "GR"}
                and not _UNITA_SUBITO_DOPO.match(trovato.string, trovato.end())
                and (pezzi_prima or float(numero) > float(prima))
            )
        if separa:
            return f" | {numero} {unita} "
        return trovato.group(0)
    if unita == "X" and _UNITA_DOPO_LA_X.match(trovato.string, trovato.end()):
        return trovato.group(0)
    # E.g. "54 DOSI X 12=648 GR": after the X's number comes another one with
    # its own unit, and that 12 is the grams per dose, not twelve pieces. Not
    # for a bare number without a unit: in "TEMPE BOX X 80 4VELI" that's 80
    # tissues.
    if unita == "X":
        dopo = re.match(rf" ?({_NUMERO})", trovato.string[trovato.end():])
        if dopo and _UNITA_SUBITO_DOPO.match(trovato.string, trovato.end() + dopo.end()):
            return trovato.group(0)
    return f"{numero} {'PZ' if unita == 'X' else unita} "


def attributes(value: Any) -> dict[str, list[float]]:
    # The raw text, not `normalize_text`'s output: the "+" in a sum would be
    # stripped before reaching this point.
    grezzo = str(value or "").upper()
    for misura in (_FORMULA, _FASCIA, _ORDINALE):
        grezzo = misura.sub(" ", grezzo)
    text = normalize_text(grezzo, tieni_il_piu=True).replace(",", ".")
    text = _ETA.sub(" ", text)
    letture = [text]
    if _SOMMA.search(text):
        # A sum is read two ways, as the total and as the base value: the
        # same product appears as "8PZ" in the reorder list and "PZ.8+2" in
        # a price list, or as "78 MISURINI" against "70+8 LAV". An extra
        # reading never creates conflicts, since a single matching pair is
        # enough.
        letture = [
            _SOMMA.sub(lambda somma: str(sum(int(parte) for parte in somma.group(0).split("+"))), text),
            _SOMMA.sub(lambda somma: somma.group(1), text),
        ]
    parsed: dict[str, set[float]] = defaultdict(set)
    for lettura in letture:
        lettura = _UNITA_DAVANTI.sub(_unita_davanti_girata, " ".join(lettura.replace("+", " ").split()))
        for raw_number, raw_unit in _UNITA_DOPO.findall(lettura):
            number = float(raw_number)
            unit = _UNITA_LETTE[raw_unit]
            if raw_unit == "L" and "." not in raw_number and number >= 10:
                # E.g. "COCCOLONE 952ML 45L", "AMMORB LT2 40L": a bare "L"
                # after an integer of 10 or more means washes, not liters.
                # "LT" and "LITRI" still mean liters.
                unit = "LAVAGGI"
            if unit == "L":
                unit, number = "ML", number * 1000
            elif unit == "KG":
                unit, number = "G", number * 1000
            parsed[unit].add(number)
    # Pieces and weight (or volume) in the same name: the total is also
    # valid, since the same product appears as "GR.250 X 2" and as
    # "X 2 GR.500". As with sums, an extra reading removes conflicts and
    # creates none.
    for pezzi in [valore for valore in parsed.get("PEZZI", ()) if 1 < valore <= 100]:
        for unit in ("ML", "G"):
            parsed[unit].update({valore * pezzi for valore in list(parsed.get(unit, ()))})
    return {key: sorted(values) for key, values in parsed.items() if values}


def attribute_comparison(left: dict[str, list[float]], right: dict[str, list[float]]) -> tuple[float, list[str]]:
    common_units = set(left) & set(right)
    if not common_units:
        return 0.5, []
    matches = 0
    conflicts = []
    for unit in sorted(common_units):
        compatible = any(abs(a - b) <= max(0.02 * max(a, b), 0.01) for a in left[unit] for b in right[unit])
        if compatible:
            matches += 1
        else:
            conflicts.append(unit)
    return matches / len(common_units), conflicts


def score_pair(query: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    query_text = normalize_text(query.get("description"))
    candidate_text = normalize_text(candidate.get("description"))
    query_tokens = tokens(query_text)
    candidate_tokens = tokens(candidate_text)
    union = query_tokens | candidate_tokens
    shared = query_tokens & candidate_tokens
    jaccard = len(shared) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, query_text, candidate_text, autojunk=False).ratio()
    query_attributes = attributes(query.get("description"))
    candidate_attributes = attributes(candidate.get("description"))
    attribute_score, conflicts = attribute_comparison(query_attributes, candidate_attributes)
    score = 0.45 * jaccard + 0.35 * sequence + 0.20 * attribute_score
    if conflicts:
        score -= 0.35
    return {
        "score": round(max(0.0, score), 6),
        "shared_tokens": sorted(shared),
        "attribute_conflicts": conflicts,
        "query_attributes": query_attributes,
        "candidate_attributes": candidate_attributes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    normalized = json.loads(args.normalized.read_text(encoding="utf-8"))
    queue = json.loads(args.queue.read_text(encoding="utf-8"))

    token_indexes: dict[str, dict[str, set[int]]] = {}
    ean_indexes: dict[str, dict[str, set[int]]] = {}
    for supplier in {item["supplier"] for item in queue}:
        index: dict[str, set[int]] = defaultdict(set)
        per_ean: dict[str, set[int]] = defaultdict(set)
        for record_index, record in enumerate(normalized[supplier]):
            if (
                not record.get("description")
                or not record.get("unit_price_net")
                or not record.get("usable", True)
                or record.get("row_type") == "DISPLAY_COMPONENT"
            ):
                continue
            for token in tokens(record["description"]):
                index[token].add(record_index)
            ean = str(record.get("ean") or "").strip()
            if ean:
                per_ean[ean].add(record_index)
        token_indexes[supplier] = index
        ean_indexes[supplier] = per_ean

    results = []
    forzati_totali = 0
    for item in queue:
        supplier = item["supplier"]
        query_tokens = tokens(item["description"])
        pool: set[int] = set()
        for token in query_tokens:
            pool.update(token_indexes[supplier].get(token, set()))

        # Rows sharing the same EAN always enter, ahead of everything else:
        # in an ambiguous-EAN case the correct row is guaranteed to be one
        # of these, and leaving it out would force the model to choose among
        # only the wrong ones.
        ean = str(item.get("ean") or "").strip()
        forzati = ean_indexes[supplier].get(ean, set()) if ean else set()
        pool.update(forzati)

        ranked = []
        for record_index in pool:
            candidate = normalized[supplier][record_index]
            scoring = score_pair(item, candidate)
            ranked.append(
                {
                    "source_row": candidate.get("source_row"),
                    "ean": candidate.get("ean"),
                    "description": candidate.get("description"),
                    "unit_price_net": candidate.get("unit_price_net"),
                    # The candidate row travels with its full commercial data,
                    # not just a subset: when a rejection looks suspicious,
                    # the first row of this list becomes the proposal the
                    # user can accept, and `offer_from_match` promotes it to
                    # a real offer. With only a handful of core fields, that
                    # proposal was born without pieces-per-carton and so
                    # `available: false`, making every such proposal
                    # unusable — measured on one comparison: 48 proposals,
                    # zero acceptable. The model itself never sees these
                    # extra fields: `valuta_shortlist.py` builds its
                    # `Candidato` objects from just `source_row`,
                    # `description` and `score`, so the prompt — and the
                    # memory key computed from it — are unaffected.
                    "supplier_code": candidate.get("supplier_code"),
                    "pieces_per_carton": candidate.get("pieces_per_carton"),
                    "order_multiplier": candidate.get("order_multiplier"),
                    "packaging": candidate.get("packaging"),
                    "availability": candidate.get("availability"),
                    "unit": candidate.get("unit"),
                    "pallet": candidate.get("pallet"),
                    "usable": candidate.get("usable", True),
                    "stesso_ean": record_index in forzati,
                    **scoring,
                }
            )
        # Order: EAN match first, then score. The EAN is proof, the score
        # only a similarity signal.
        ranked.sort(key=lambda value: (not value["stesso_ean"], -value["score"], value["source_row"] or 0))
        # The cutoff must never drop a row sharing the correct EAN: if there
        # are more of them than `top_k`, all of them are shown, since the
        # correct choice is guaranteed to be among them.
        quanti = max(args.top_k, sum(1 for value in ranked if value["stesso_ean"]))
        forzati_totali += sum(1 for value in ranked[:quanti] if value["stesso_ean"])
        results.append({**item, "candidates": ranked[:quanti]})

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "queue_items": len(queue),
        "shortlists": len(results),
        "top_k": args.top_k,
        "candidati_con_lo_stesso_ean": forzati_totali,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
