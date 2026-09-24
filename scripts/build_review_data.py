#!/usr/bin/env python3
"""Build the compact, supplier-agnostic view model used by the local web app."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The threshold is owned by the module that scores the match. An AI rejection
# with a close candidate becomes a product warning, surfaced in the "To confirm" filter.
try:
    from merge_match_decisions import SOGLIA_RIFIUTO_SOSPETTO
except ImportError:  # imported from outside the scripts/ folder
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from merge_match_decisions import SOGLIA_RIFIUTO_SOSPETTO

# The readable supplier name comes from the adapter registry, not a separate
# list here: it's the same source that defines the supplier in the first place.
import registro  # noqa: E402


def load(path: Path | None, default: Any) -> Any:
    if path is None or not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


USCITA_INGRESSO_NON_UTILIZZABILE = 2


def load_obbligatorio(path: Path) -> Any:
    """Load a required input; a missing or empty file is a hard failure, not a silent default.

    An earlier version fell back to `load(..., [])` when `--resolved` was missing,
    so the comparison still built with an empty result and the page opened nearly
    blank with no indication why. An empty list produces the identical symptom, so
    both cases are rejected the same way: `merge_match_decisions.py` never writes
    an empty list unless the management-software export itself is empty, and
    `prepare_sources.py` already guards against that, so an empty `[]` here means
    an earlier step failed to do its job.

    Exits with the same status code the rest of this phase uses for "unusable
    input" (2), rather than a generic `SystemExit`."""

    def fermati(motivo: str) -> None:
        print(f"[ERRORE] {motivo}", file=sys.stderr)
        raise SystemExit(USCITA_INGRESSO_NON_UTILIZZABILE)

    if not path.exists():
        fermati(
            f"Il file dei match risolti non esiste: {path}. Il passo precedente "
            "della catena non l'ha prodotto: non si costruisce un confronto senza."
        )
    try:
        contenuto = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as errore:
        fermati(f"Il file dei match risolti non si legge: {path} — {errore}")
    if not isinstance(contenuto, list) or not contenuto:
        fermati(
            f"Il file dei match risolti non contiene nessun prodotto: {path}. "
            "Un confronto vuoto marcato «pronto» è peggio di nessun confronto."
        )
    return contenuto


def load_dichiarato(path: Path | None, che_cosa: str) -> Any:
    """Load an optional input: fine if not requested, a hard failure if requested but missing.

    Not passing a path is legitimate (this run has no displays to report). A path
    that's passed but doesn't exist is not: the caller declared the input
    required, so a silent `load(..., [])` fallback would drop every display from
    the comparison with no warning. An empty file, on the other hand, is still
    legitimate: it means none were found.
    """

    if path is None:
        return []
    if not path.exists():
        print(
            f"[ERRORE] Il file {che_cosa} non esiste: {path}. È stato chiesto, quindi "
            "il passo precedente doveva produrlo: senza, quella merce sparirebbe dal "
            "confronto senza che niente lo dica.",
            file=sys.stderr,
        )
        raise SystemExit(USCITA_INGRESSO_NON_UTILIZZABILE)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as errore:
        print(f"[ERRORE] Il file {che_cosa} non si legge: {path} — {errore}", file=sys.stderr)
        raise SystemExit(USCITA_INGRESSO_NON_UTILIZZABILE)


def number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def money(value: Any) -> float | None:
    parsed = number(value)
    return round(parsed, 4) if parsed is not None else None


def suggested_quantity(value: Any) -> int | None:
    """Suggested cartons from the "Colli" column of the management-software export: int >= 0 or None."""
    parsed = number(value)
    if parsed is None or parsed < 0:
        return None
    return int(round(parsed))


def supplier_name(supplier_id: str) -> str:
    """Return the supplier's readable name, from the adapter registry.

    Same source the server and the writer use, so a learned adapter's supplier
    shows its display name rather than its technical id.
    """

    return registro.nome_del_fornitore(supplier_id)


def normalized_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").upper())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", text).split())


def impronta_articolo(offerta: Any) -> str:
    """Fingerprint the ARTICLE identity of an offer, not its commercial terms.

    Expires a "confirmed, same article" checkbox when a later run matches the
    product to a different price-list row: without this, the checkbox would
    stay checked on an article the user never actually reviewed.

    Only identity goes in: supplier, EAN, article code, normalized name. Price
    and packaging are deliberately excluded — they change every week on the same
    article, and re-asking on every price-list tweak would train users to check
    the box without reading. `rejected_candidate_key` covers the full commercial
    fingerprint, for a different question.
    """

    if not isinstance(offerta, dict):
        return ""
    pezzi = [
        str(offerta.get("supplierId") or offerta.get("supplier_id") or offerta.get("supplier") or ""),
        str(offerta.get("ean") or offerta.get("gtin") or "").strip(),
        str(offerta.get("supplierCode") or offerta.get("supplier_code") or "").strip(),
        normalized_name(offerta.get("description") or offerta.get("name")),
    ]
    return "|".join(pezzi)


def rejected_candidate_key(supplier_id: str, candidate: dict[str, Any]) -> str:
    """Fingerprint the row proposed to the user after a suspect AI rejection.

    A human decision on this candidate must expire the moment any commercial
    trait of the row changes: position, name, EAN, code, price or packaging.
    The run itself is validated separately by the server.
    """

    payload = {
        "supplier": str(supplier_id or ""),
        "source_row": candidate.get("source_row"),
        "description": str(candidate.get("description") or ""),
        "ean": str(candidate.get("ean") or ""),
        "supplier_code": str(candidate.get("supplier_code") or ""),
        "unit_price_net": number(candidate.get("unit_price_net")),
        "order_multiplier": number(candidate.get("order_multiplier")),
        "pieces_per_carton": number(candidate.get("pieces_per_carton")),
        "packaging": str(candidate.get("packaging") or ""),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def supplier_ids(resolved: list[dict[str, Any]], display_offers: list[dict[str, Any]]) -> list[str]:
    found = set()
    for product in resolved:
        found.update((product.get("suppliers") or {}).keys())
    found.update(str(item.get("supplier") or item.get("supplier_id") or "") for item in display_offers)
    found.discard("")
    preferred = [supplier for supplier in ("betulla", "larice", "noce", "cipresso") if supplier in found]
    return preferred + sorted(found - set(preferred))


def offer_from_match(supplier_id: str, result: dict[str, Any], last_price: float | None) -> dict[str, Any]:
    selected = result.get("selected") or None
    alternatives = result.get("alternatives") or []
    if not selected:
        offer = {
            "supplierId": supplier_id,
            "supplierName": supplier_name(supplier_id),
            "available": False,
            "status": result.get("status") or "NON_TROVATO",
            "method": result.get("method") or "",
            "confidence": result.get("confidence") or "",
            "requiresConfirmation": bool(result.get("requires_user_confirmation")),
            "confirmed": False,
            "rationale": result.get("rationale") or "",
            "alternatives": alternatives[:3],
            "price": 0,
            "unitsPerOrderUnit": 1,
            "pricePerPiece": 0,
            "matchStatus": result.get("status") or "NON_TROVATO",
            # Similarity score of the best candidate the AI rejected. `None` when
            # the rejection isn't AI-driven or the shortlist had no scores: the
            # page distinguishes "unknown" from "scored low".
            "rejectBestScore": number(result.get("ai_reject_best_score")),
        }
        # A suspect rejection needs an actionable warning, not a dead end. The
        # shortlist's top row becomes the proposal the user can accept or
        # reject; it only turns into a real offer after that decision.
        best = alternatives[0] if alternatives and isinstance(alternatives[0], dict) else None
        score = offer["rejectBestScore"]
        if result.get("method") == "AI_RIFIUTATO" and best and score is not None and score >= SOGLIA_RIFIUTO_SOSPETTO:
            candidate_offer = offer_from_match(
                supplier_id,
                {
                    "selected": best,
                    "status": "SEMANTICO_PROPOSTO",
                    "method": "CORREZIONE_UTENTE",
                    "confidence": "UTENTE",
                    "requires_user_confirmation": False,
                    "rationale": result.get("rationale") or "",
                    "alternatives": [],
                },
                last_price,
            )
            candidate_offer["candidateKey"] = rejected_candidate_key(supplier_id, best)
            candidate_offer["score"] = score
            offer["rejectedCandidate"] = candidate_offer
        return offer

    unit_price = money(selected.get("unit_price_net"))
    factor = number(selected.get("order_multiplier"))
    factor_kind = "unità"
    if factor is None:
        factor = number(selected.get("pieces_per_carton"))
        factor_kind = "pezzi/collo"
    order_price = money(unit_price * factor) if unit_price is not None and factor is not None else None
    difference = money(unit_price - last_price) if unit_price is not None and last_price is not None else None
    difference_pct = round((difference / last_price) * 100, 2) if difference is not None and last_price else None
    details = [selected.get("packaging"), selected.get("availability"), selected.get("unit")]
    if selected.get("pallet") not in (None, ""):
        details.append(f"Pedana: {selected['pallet']}")
    requires_confirmation = bool(result.get("requires_user_confirmation"))
    return {
        "supplierId": supplier_id,
        "supplierName": supplier_name(supplier_id),
        # `> 0`, not `is not None`. A price that reads as 0.00 would pass every
        # filter and then win the comparison, since sorting puts the lowest
        # price first: the product would land on the supplier whose cell failed
        # to parse, at zero total, and the minimum-order threshold wouldn't
        # trigger because zero is below any threshold. The existing safeguard
        # (PREZZI_A_ZERO) checks the median of the whole price list, which
        # catches a misread column across a file, not a single bad row inside
        # an otherwise healthy list.
        "available": (
            unit_price is not None and unit_price > 0
            and factor is not None and factor > 0
            and bool(selected.get("usable", True))
        ),
        "status": result.get("status") or "",
        "method": result.get("method") or "",
        "confidence": result.get("confidence") or "",
        "requiresConfirmation": requires_confirmation,
        "confirmed": not requires_confirmation,
        "rationale": result.get("rationale") or "",
        "description": selected.get("description") or "",
        "ean": selected.get("ean") or "",
        "supplierCode": selected.get("supplier_code"),
        "sourceRow": selected.get("source_row"),
        "unitPriceNet": unit_price,
        "quantityFactor": factor,
        "quantityFactorLabel": factor_kind,
        "orderUnitPriceNet": order_price,
        "price": order_price,
        "unitsPerOrderUnit": factor,
        "pricePerPiece": unit_price,
        "matchStatus": result.get("status") or "",
        "lastPriceDifference": difference,
        "lastPriceDifferencePct": difference_pct,
        "details": " | ".join(str(item) for item in details if item not in (None, "")),
        "alternatives": alternatives[:3],
    }


def suspect_reject_warnings(product_id: str, name: str, offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build warnings for AI rejections that had a close-scoring candidate.

    This is the only safeguard against a wrong rejection, which otherwise
    leaves no trace: the adversarial check protects against wrong `ACCEPT`s,
    not `REJECT`s. It's non-blocking — a correct rejection is the normal case,
    and about half of these warnings will be that — but the product lands in
    the "To confirm" filter instead of disappearing silently."""
    avvisi = []
    for offer in offers:
        punteggio = offer.get("rejectBestScore")
        if offer.get("method") != "AI_RIFIUTATO" or punteggio is None:
            continue
        if punteggio < SOGLIA_RIFIUTO_SOSPETTO:
            continue
        candidate = offer.get("rejectedCandidate") if isinstance(offer.get("rejectedCandidate"), dict) else None
        candidate_name = str((candidate or {}).get("description") or "").strip()
        supplier = str(offer.get("supplierName") or offer.get("supplierId") or "Fornitore")
        avvisi.append({
            "id": f"{product_id}-rifiuto-{offer['supplierId']}",
            "code": "RIFIUTO_CON_CANDIDATO_FORTE",
            "severity": "warning",
            "blocking": False,
            "productId": product_id,
            "supplierId": offer.get("supplierId"),
            "candidateKey": (candidate or {}).get("candidateKey") or "",
            "candidate": candidate,
            "title": f"Possibile prodotto {supplier}",
            "message": (
                f"{supplier} propone «{candidate_name}». Indica se è lo stesso articolo."
                if candidate_name
                else f"{supplier} potrebbe avere questo prodotto, ma il nome della riga proposta non è disponibile."
            ),
            "technicalMessage": (
                f"Prodotto gestionale: {name}. Il candidato migliore della selezione è stato "
                f"rifiutato dall'analisi automatica con somiglianza {punteggio:.2f} su 1."
            ),
        })
    return avvisi


def senza_offerta_warnings(
    product_id: str,
    ordinabile: bool,
    colli_chiesti: int | None,
    offers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flag, on the product's own row, that no price list can supply it.

    The management-software quantity still applies here: the row shows the
    cartons needed with no supplier next to it, and without this warning that
    would look like an unmade choice rather than a genuine dead end. This is
    the only place that explains, on the row itself, why that quantity won't
    turn into an order — at order-compilation time it lands in the "to be
    sourced" list.
    """

    if ordinabile:
        return []
    fornitori = len(offers)
    dove = (
        f"Nessuno dei {fornitori} listini caricati ha una riga utilizzabile per questo prodotto."
        if fornitori
        else "Nessun listino caricato porta questo prodotto."
    )
    chiesti = (
        f" Il gestionale ne chiedeva {colli_chiesti} "
        + ("collo" if colli_chiesti == 1 else "colli")
        + ": va ordinato altrove, oppure serve un listino che lo porti."
        if colli_chiesti
        else ""
    )
    # New strings use accented characters; older ones in the project keep the
    # apostrophe form, by convention.
    return [{
        "id": f"{product_id}-senza-offerta",
        "code": "SENZA_OFFERTA_UTILIZZABILE",
        "severity": "warning",
        "blocking": False,
        "productId": product_id,
        # In this app "offerta" means a supplier's proposal, but in retail
        # usage it means a discount ("in offerta" = "on sale"). A store
        # operator reading "nessuna offerta" would understand "nothing's on
        # sale" rather than "no supplier carries it". This label and the
        # messages that summarize it avoid the word for "proposal" and keep
        # it only for actual discounts, where the user expects it.
        "title": "Nessun fornitore ce l’ha",
        "message": dove + chiesti,
    }]


def senza_offerta_summary(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize the count at the top of the page, and how to find them.

    The page collects per-product warnings only for products with a quantity
    to order, and the "no supplier has it" filter hides anything at zero.
    Products the management export requests a quantity for are findable in
    that filter; the rest — nothing requested — are invisible everywhere
    else, and this summary line is the only place that counts them. Both
    counts are stated explicitly: a summary that claims a filter shows
    everything when it doesn't would be a warning that lies.
    """

    senza = [
        product for product in products
        if any(
            (warning or {}).get("code") == "SENZA_OFFERTA_UTILIZZABILE"
            for warning in product.get("warnings") or []
        )
    ]
    if not senza:
        return []
    # Checks `quantity`, not `suggestedQuantity`: `quantity` is what decides
    # whether the "no supplier has it" filter shows the row (it hides
    # anything at zero), and this message promises exactly that filter. The
    # two fields happen to agree today; if they ever diverged, the promise
    # would still hold.
    chiesti = [
        product for product in senza
        if (product.get("quantity") or 0) > 0
    ]
    # The count goes after the verb so the sentence reads correctly whether
    # it's one item or a thousand (an earlier phrasing broke on a count of 1).
    uno = len(chiesti) == 1
    coda = (
        f" Di questi il gestionale ne chiede {len(chiesti)}: "
        f"{'lo trovi' if uno else 'li trovi'} con il filtro «Nessuno ce l’ha», "
        f"con la {'sua' if uno else 'loro'} quantità. "
        f"{'Va' if uno else 'Vanno'} reperit{'o' if uno else 'i'} altrove, e alla compilazione "
        f"{'finisce' if uno else 'finiscono'} nell'elenco «Prodotti da reperire»."
        if chiesti
        else " Nessuno di questi ha una quantità da ordinare, quindi nel filtro "
        "«Nessuno ce l’ha» non compaiono."
    )
    return [{
        "code": "PRODOTTI_SENZA_OFFERTA",
        "severity": "warning",
        "blocking": False,
        "title": "Prodotti che nessun fornitore ha",
        "message": (
            f"{len(senza)} prodotti dell'elenco non li ha nessuno dei fornitori: nei listini "
            "caricati non c'è una riga utilizzabile per loro." + coda
        ),
        "count": len(senza),
    }]


# A generic "N anomalies, values stay visible in the audit" warning tells the
# store operator nothing actionable: it names neither the problem in their own
# terms nor where to look, and it omits the one thing that matters before
# placing an order — whether the price can be wrong.
#
# These entries state the consequence, not the internal name of the defect.
# They're keyed by the prefix of the text the price-list reader writes: an
# unrecognized warning text falls through to the generic branch below and can
# be added here once it's worth explaining.
SPIEGAZIONI_ANOMALIE: tuple[tuple[str, str, str, bool], ...] = (
    (
        "Codice sconto testuale inatteso",
        "nella colonna dello sconto una sigla che il programma non conosce",
        "il prezzo è stato preso senza sconto, quindi può risultare più alto del vero",
        True,
    ),
    (
        "Scadenza scritta male",
        "la data di scadenza scritta in un modo che non si riesce a leggere",
        "il prezzo non cambia — è la scadenza che va letta sul listino del fornitore",
        False,
    ),
    (
        "Scadenza fuori dal credibile",
        "una data di scadenza troppo lontana per essere vera",
        "il prezzo non cambia — è la scadenza che va letta sul listino del fornitore",
        False,
    ),
)


def _righe_citate(righe: list[Any]) -> str:
    """List the supplier file's row numbers in full up to a cap, then just the count.

    Someone checking this opens the price list and looks the row up: six
    numbers are easy to copy by hand, sixty are a wall of text that drowns
    out the rest of the sentence.
    """

    numeri = [str(riga) for riga in righe if riga not in (None, "")]
    if not numeri:
        return ""
    if len(numeri) == 1:
        return f" È la riga {numeri[0]} del suo file."
    if len(numeri) <= 6:
        return f" Sono le righe {', '.join(numeri[:-1])} e {numeri[-1]} del suo file."
    return f" Le prime sono le righe {', '.join(numeri[:6])} del suo file, e ce ne sono altre {len(numeri) - 6}."


def anomalie_listino_summary(source_warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize what happened, on which price list, and whether a price is affected.

    A bare "3 anomalies" warning leaves the reader to guess the rest. This
    states the supplier, the row count, what was unusual and the consequence
    — and its title answers the question that matters before placing an
    order: can this price be trusted, or does it need checking?
    """

    if not source_warnings:
        return []

    # Grouped by (supplier, anomaly type): that pair decides both the sentence
    # and the rows to cite. A plain `dict` preserves insertion order, so
    # warnings come out in the order the price lists were read, not an
    # unfamiliar alphabetical one.
    gruppi: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for voce in source_warnings:
        if not isinstance(voce, dict):
            continue
        testo = str(voce.get("warning") or "")
        indice = next(
            (n for n, (prefisso, *_) in enumerate(SPIEGAZIONI_ANOMALIE) if testo.startswith(prefisso)),
            len(SPIEGAZIONI_ANOMALIE),
        )
        fornitore = str(voce.get("source") or "fonte").upper()
        gruppi.setdefault((fornitore, indice), []).append(voce)

    frasi: list[str] = []
    righe_sul_prezzo = 0
    righe_senza_spiegazione = 0
    for (fornitore, indice), voci in gruppi.items():
        una = len(voci) == 1
        quante = "1 riga" if una else f"{len(voci)} righe"
        righe = _righe_citate([voce.get("source_row") for voce in voci])
        if indice < len(SPIEGAZIONI_ANOMALIE):
            _, che_cosa, conseguenza, sul_prezzo = SPIEGAZIONI_ANOMALIE[indice]
            frasi.append(
                f"{quante} del listino {fornitore} {'ha' if una else 'hanno'} {che_cosa}: "
                f"{conseguenza}.{righe}"
            )
            if sul_prezzo:
                righe_sul_prezzo += len(voci)
        else:
            # A reason this table doesn't recognize: reported verbatim,
            # without inventing a consequence we don't actually know.
            motivi = sorted({str(voce.get("warning") or "").strip() for voce in voci if voce.get("warning")})
            dettaglio = f" Il motivo scritto in lettura: {'; '.join(motivi)}." if motivi else ""
            frasi.append(
                f"{quante} del listino {fornitore} "
                f"{'è stata letta' if una else 'sono state lette'} con una riserva.{dettaglio}{righe}"
            )
            righe_senza_spiegazione += len(voci)

    # The title answers the question that has a cost: if even one group
    # affects a price, the title says so, and the detail stays in the message.
    fornitori = sorted({fornitore for fornitore, _ in gruppi})
    chi = f"{fornitori[0]}: " if len(fornitori) == 1 else ""
    coda_listino = "" if len(fornitori) == 1 else " di listino"
    totali = len(source_warnings)
    una_sola = totali == 1
    quante_tutte = "1 riga" if una_sola else f"{totali} righe"
    if righe_sul_prezzo:
        quante = "1 riga" if righe_sul_prezzo == 1 else f"{righe_sul_prezzo} righe"
        titolo = f"{chi}su {quante}{coda_listino} il prezzo può essere più alto del vero"
    elif righe_senza_spiegazione:
        letta = "letta" if una_sola else "lette"
        titolo = f"{chi}{quante_tutte}{coda_listino} {letta} con una riserva"
    else:
        titolo = f"{chi}{quante_tutte}{coda_listino} con un dato incerto, che non tocca il prezzo"
    # Without a supplier name in front, the title starts with the sentence
    # itself, so the capitalization is applied here, in one place.
    if not chi:
        titolo = titolo[:1].upper() + titolo[1:]

    return [{
        "code": "ANOMALIE_LISTINO",
        "severity": "warning",
        "blocking": False,
        "title": titolo,
        "message": " ".join(frasi) + " Il resto dei listini non è toccato.",
        "count": len(source_warnings),
    }]


def suspect_reject_summary(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize the same count at the top of the page.

    The page collects per-product warnings only for products with a quantity
    to order, so without this summary a wrongly rejected product left at zero
    wouldn't show up anywhere. Both counts are stated explicitly — claiming a
    filter shows everything when it doesn't would be a warning that lies."""
    con_avviso = [
        product
        for product in products
        if any(
            (warning or {}).get("code") == "RIFIUTO_CON_CANDIDATO_FORTE"
            for warning in product.get("warnings") or []
        )
    ]
    if not con_avviso:
        return []
    da_ordinare = sum(1 for product in con_avviso if (product.get("quantity") or 0) > 0)
    return [{
        "code": "RIFIUTI_CON_CANDIDATO_FORTE",
        "severity": "warning",
        "blocking": False,
        "title": "Prodotti scartati che somigliavano molto",
        "message": (
            f"Su {len(con_avviso)} prodotti l'analisi automatica non ha trovato corrispondenza "
            "presso un fornitore, ma la riga più simile del suo listino somigliava molto. "
            + (
                f"I {da_ordinare} con una quantità da ordinare li trovi con il filtro «Da confermare»."
                if da_ordinare
                else "Nessuno di questi ha una quantità da ordinare, quindi non compaiono in quel filtro."
            )
        ),
        "count": len(con_avviso),
    }]


CAUSE_DI_SCARTO = {
    "LISTINO_DISALLINEATO": "il listino non era più quello su cui il modello aveva deciso",
    "RIGA_NON_MOSTRATA": "il modello ha indicato una riga che non gli era stata mostrata",
    "EAN_NON_RISPETTATO": "il modello ha scelto una riga senza il codice a barre del prodotto",
    "DECISIONE_DI_UNA_ALTRA_RUN": "la proposta era stata presa su un elenco di candidati diverso da quello di oggi",
    "DECISIONE_SENZA_IMPRONTA": "la proposta non dice su quali candidati è stata presa",
}


def decisioni_ai_scartate(resolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the top-of-page warning for AI decisions that were discarded.

    Otherwise this count only shows up in `merge_match_decisions.py`'s stdout,
    and the finished app runs unattended, so nobody reads that stream. In the
    product list a discarded match is indistinguishable from one the AI never
    evaluated — the only difference is the rationale text, visible only by
    opening that product at that supplier. Without this summary, "discarded
    decisions must be counted in a visible summary" would be true only on
    paper."""

    per_causa: dict[str, int] = {}
    for item in resolved:
        for match in (item.get("suppliers") or {}).values():
            causa = (match or {}).get("ai_decisione_scartata")
            if causa:
                per_causa[causa] = per_causa.get(causa, 0) + 1
    if not per_causa:
        return []
    totale = sum(per_causa.values())
    motivi = "; ".join(
        f"{quanti} perché {CAUSE_DI_SCARTO.get(causa, causa)}"
        for causa, quanti in sorted(per_causa.items(), key=lambda voce: -voce[1])
    )
    return [{
        "code": "DECISIONI_AI_SCARTATE",
        "severity": "warning",
        "blocking": False,
        "title": "Abbinamenti proposti dall'analisi e non usati",
        "message": (
            f"{totale} abbinamenti proposti dall'analisi automatica sono stati scartati "
            f"prima di entrare nel confronto: {motivi}. Quei prodotti sono da verificare a "
            "mano presso quel fornitore: non è che non esista una corrispondenza, è che "
            "questa non era affidabile."
        ),
        "count": totale,
        "byCause": per_causa,
    }]


def build_products(resolved: list[dict[str, Any]], suppliers: list[str]) -> list[dict[str, Any]]:
    products = []
    for item in resolved:
        master = item.get("gestionale") or {}
        last_price = money(master.get("last_unit_price"))
        offers = [offer_from_match(supplier, (item.get("suppliers") or {}).get(supplier, {}), last_price) for supplier in suppliers]
        selectable = [offer for offer in offers if offer.get("available")]
        selectable.sort(key=lambda offer: (offer.get("unitPriceNet") is None, offer.get("unitPriceNet") or 0, suppliers.index(offer["supplierId"])))
        selected_supplier = selectable[0]["supplierId"] if selectable else None
        selected_offer = next((offer for offer in offers if offer.get("supplierId") == selected_supplier), None)
        requires_confirmation = bool(selected_offer and selected_offer.get("requiresConfirmation"))
        # `source_colli_ignored` is the field's name from before the switch to
        # ordering by cartons; accepting it lets the comparison rebuild from
        # artifacts of older runs.
        default_quantity = suggested_quantity(
            master.get("suggested_colli", master.get("source_colli_ignored"))
        )
        # The management-software carton count always applies, even when no
        # price list carries the product: it's the product's normal quantity,
        # not a choice, and zeroing it out would discard the only thing the
        # export said about that row.
        #
        # This must stay a valid state: a quantity greater than zero with no
        # usable offer from any supplier. Zeroing it here would sidestep a
        # blocking validation error for products the management export
        # introduces that no price list carries, but the validation rule
        # itself is relaxed instead, so such products don't need to be
        # zeroed to pass it. At order-compilation time they land in the
        # "to be sourced" list, which is what surfaces them.
        #
        # The product stays in the comparison and reports that it has no
        # offers: hiding it would drop a row the management software asked for.
        ordinabile = selected_supplier is not None
        quantita = default_quantity if default_quantity is not None else 0
        product_id = f"product:{master.get('source_row')}"
        products.append({
            "id": product_id,
            "kind": "PRODUCT",
            "itemType": "product",
            "sourceRow": master.get("source_row"),
            "ean": master.get("ean") or "",
            "description": master.get("description") or "",
            "name": master.get("description") or "",
            "lastUnitPrice": last_price,
            "quantity": quantita,
            "suggestedQuantity": default_quantity,
            # The source follows the management export's carton count, not
            # whether a supplier was found: since the quantity now always
            # applies, labeling a number the user never typed as "utente"
            # would make it immune to the next recalculation, which only
            # rereads values still marked "gestionale".
            #
            # Checks `is not None`, not the number's truthiness: a management
            # export value of zero is still a value the export declared, and
            # is reread from there like any other; "utente" is reserved for
            # the genuinely empty column, where there's nothing to reread.
            "quantitySource": "gestionale" if default_quantity is not None else "utente",
            "quantityLabel": "colli",
            "orderUnitLabel": "colli",
            "selectedSupplierId": selected_supplier,
            "confirmed": selected_supplier is not None and not requires_confirmation,
            "requiresConfirmation": requires_confirmation,
            "confirmationMessage": selected_offer.get("rationale") if selected_offer else "",
            "components": [],
            "warnings": (
                suspect_reject_warnings(product_id, master.get("description") or "", offers)
                + senza_offerta_warnings(product_id, ordinabile, default_quantity, offers)
            ),
            "notes": "",
            "offers": offers,
        })
    return products


def flatten_displays(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("display_offers"), list):
        return value["display_offers"]
    if isinstance(value.get("offers"), list):
        return value["offers"]
    flattened = []
    for supplier, offers in value.items():
        if isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    flattened.append({"supplier": supplier, **offer})
    return flattened


def component_fingerprint(offer: dict[str, Any]) -> str:
    explicit = offer.get("composition_fingerprint") or offer.get("fingerprint")
    if explicit:
        return str(explicit)
    components = offer.get("components") or []
    pairs = []
    for component in components:
        ean = str(component.get("ean") or component.get("gtin") or "").strip()
        quantity = number(component.get("quantity") or component.get("units") or component.get("units_per_display"))
        if ean and quantity is not None:
            pairs.append(f"{ean}:{quantity:g}")
    if pairs:
        return "composition:" + "|".join(sorted(pairs))
    description = offer.get("description") or offer.get("display_name") or offer.get("normalized_name")
    declared = offer.get("declared_units") or offer.get("declared_quantity") or offer.get("total_units")
    supplier = offer.get("supplier") or offer.get("supplier_id") or "unknown"
    return f"unmatched:{supplier}:{normalized_name(description)}:{declared}"


def display_offer(offer: dict[str, Any]) -> dict[str, Any]:
    supplier = str(offer.get("supplier") or offer.get("supplier_id") or "")
    pre_price = money(offer.get("list_price_per_display") or offer.get("unit_price_pre_discount") or offer.get("parent_price_pre_discount"))
    net_price = money(offer.get("net_price_per_display") or offer.get("unit_price_net") or offer.get("parent_price_net") or offer.get("parent_price_post_discount"))
    if net_price is None:
        discount = number(offer.get("discount_rate"))
        if pre_price is not None:
            net_price = money(pre_price * (1 - (discount or 0)))
    declared = number(offer.get("declared_units") or offer.get("declared_quantity") or offer.get("total_units") or offer.get("sum_component_units"))
    confidence = str(offer.get("confidence") or offer.get("detection_confidence") or "").upper()
    requires_confirmation = confidence not in {"ALTA", "HIGH", "CERTA"} and not bool(offer.get("auto_confirmed"))
    components = []
    for item in offer.get("components") or []:
        components.append({
            "ean": item.get("ean") or item.get("gtin") or "",
            "description": item.get("description") or item.get("normalized_product") or "",
            "quantity": number(item.get("quantity") or item.get("units") or item.get("units_per_display")),
            "unitPricePreDiscount": money(item.get("unit_price_pre_discount") or item.get("component_unit_price")),
            "sourceRow": item.get("source_row"),
        })
    # A display is an order unit like a carton: `quantityFactor` is the pieces
    # it contains and `unitPriceNet` the price of a single piece, exactly like
    # a regular product (`offer_from_match`, above). `unitPriceNet` must not
    # hold the price of the whole display with a factor of 1 — the order plan
    # and history would then write `delivered_pieces` = number of displays and
    # `unit_price_net` = display price, a different quantity than the page
    # shows (off by a factor of `declaredUnits`), and "move everything to
    # another supplier" would pick the best alternative by whole-display price
    # instead of per-piece price, against the per-piece contract the rest of
    # the app relies on. If the declared piece count is missing, the display
    # counts as one piece — a conservative fallback, since it makes the offer
    # look more expensive rather than cheaper than it is.
    pieces = declared if declared and declared > 0 else 1
    price_per_piece = money(net_price / pieces) if net_price is not None else None
    # "identical" must be the result of an actual check, not the absence of a
    # contradiction. `quantity_reconciled` and `price_reconciled` are `None`
    # when there wasn't data to reconcile against, and `None is not False` is
    # true — so this must check for `True` explicitly, not just falsiness.
    composition_status = (
        "identical"
        if offer.get("quantity_reconciled") is True and offer.get("price_reconciled") is True
        else "comparable"
    )
    raw_evidence = offer.get("evidence") or []
    if isinstance(raw_evidence, list):
        evidence = "; ".join(
            str(item.get("detail") or item.get("code") or item) if isinstance(item, dict) else str(item)
            for item in raw_evidence
        )
    else:
        evidence = str(raw_evidence)
    return {
        "supplierId": supplier,
        "supplierName": supplier_name(supplier),
        # `usable` applies to a display too. Checking price alone would let a
        # display the reader already flagged as not orderable — e.g. because
        # its parent carton's piece count didn't parse — back in through the
        # side door, with a price derived from an unknown factor.
        "available": (
            net_price is not None and net_price > 0
            and bool(offer.get("usable", True))
        ),
        "status": "ESPOSITORE_RILEVATO",
        "method": "COMPOSIZIONE",
        "confidence": confidence or "DA_VERIFICARE",
        "requiresConfirmation": requires_confirmation,
        "confirmed": not requires_confirmation,
        "rationale": evidence,
        "description": offer.get("description") or offer.get("display_name") or "",
        "supplierCode": offer.get("supplier_code"),
        "sourceRow": offer.get("source_row") or offer.get("parent_source_row"),
        "unitPricePreDiscount": pre_price,
        "unitPriceNet": price_per_piece,
        "quantityFactor": pieces,
        "quantityFactorLabel": "espositore",
        "orderUnitPriceNet": net_price,
        "pricePerPieceNet": price_per_piece,
        "price": net_price,
        "unitsPerOrderUnit": pieces,
        "pricePerPiece": price_per_piece,
        "matchStatus": "Espositore identico" if composition_status == "identical" else "Composizione da verificare",
        "compositionStatus": composition_status,
        "declaredUnits": declared,
        "components": components,
        "quantityReconciled": offer.get("quantity_reconciled"),
        "priceReconciled": offer.get("price_reconciled"),
    }


def build_display_products(raw_offers: list[dict[str, Any]], suppliers: list[str]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for offer in raw_offers:
        groups[component_fingerprint(offer)].append(offer)
    products = []
    for fingerprint, offers_raw in sorted(groups.items(), key=lambda item: normalized_name(item[1][0].get("description") or item[1][0].get("display_name"))):
        offers = [display_offer(item) for item in offers_raw]
        first = offers_raw[0]
        description = first.get("description") or first.get("display_name") or "Espositore"
        components = offers[0].get("components") or []
        declared = offers[0].get("declaredUnits")
        selectable = [offer for offer in offers if offer.get("available")]
        selectable.sort(key=lambda offer: (offer.get("unitPriceNet") is None, offer.get("unitPriceNet") or 0))
        selected_supplier = selectable[0]["supplierId"] if selectable else None
        selected_offer = selectable[0] if selectable else None
        requires_confirmation = bool(selected_offer and selected_offer.get("requiresConfirmation"))
        products.append({
            "id": f"display:{fingerprint}",
            "kind": "DISPLAY",
            "itemType": "display",
            "ean": "",
            "description": description,
            "name": description,
            "lastUnitPrice": None,
            # Always zero: a display has no management-software row of its
            # own — it comes from the suppliers' price lists — so it has no
            # carton count to carry over. How many displays to order is
            # written by the user, as before.
            "quantity": 0,
            "quantityLabel": "espositori",
            "orderUnitLabel": "espositori",
            "selectedSupplierId": selected_supplier,
            "confirmed": bool(selected_supplier) and not requires_confirmation,
            "requiresConfirmation": requires_confirmation,
            "confirmationMessage": selected_offer.get("rationale") if selected_offer else "Conferma composizione e offerta dell'espositore.",
            "notes": "",
            "offers": offers,
            "components": components,
            "warnings": [],
            "display": {
                "fingerprint": fingerprint,
                "declaredUnits": declared,
                "componentCount": len(components),
                "components": components,
                "comparison": "IDENTICO" if len({offer["supplierId"] for offer in offers}) > 1 and not fingerprint.startswith("unmatched:") else "SINGOLA_OFFERTA",
            },
        })
    return products


def manifest_files(manifest: dict[str, Any], audit: dict[str, Any]) -> list[dict[str, Any]]:
    audit_by_name = {
        Path(item.get("path") or "").name.casefold(): item
        for item in audit.get("inputs") or []
    }
    result = []
    for item in manifest.get("files") or []:
        ai = item.get("ai_preflight") or {}
        file_name = item.get("file_name") or ""
        state = ai.get("state") or "AMBIGUO"
        status = "ready" if state == "SCHEMA_NOTO" else "warning" if state in {"SCHEMA_VARIATO", "NUOVO_FORNITORE"} else "error"
        role = ai.get("role")
        supplier_id = ai.get("supplier_id")
        audited = audit_by_name.get(file_name.casefold(), {})
        result.append({
            "id": item.get("profile_id"),
            "name": file_name,
            "sourcePath": item.get("path"),
            "role": ai.get("role"),
            "supplierId": ai.get("supplier_id"),
            "adapterId": ai.get("adapter_id"),
            "status": status,
            "confidence": ai.get("confidence"),
            "kind": "Gestionale" if role == "master" else "Listino" if role == "supplier" else "Ignorato",
            "supplier": "Gestionale" if role == "master" else supplier_name(supplier_id) if supplier_id else "Da riconoscere",
            "schemaState": state,
            "rows": audited.get("records") or 0,
            "message": ai.get("rationale") or "",
            "fieldMapping": ai.get("field_mapping") if isinstance(ai.get("field_mapping"), dict) else None,
            "sourceSha256": item.get("sha256") or None,
        })
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--displays", type=Path)
    parser.add_argument("--threshold", type=float, default=1000.0)
    parser.add_argument("--run-id")
    return parser.parse_args()


def main() -> int:
    # Same reason as `merge_match_decisions.py`: on Windows, a process writing
    # to a pipe defaults to cp1252, and these messages contain accented
    # characters. Whatever reads this output must always find UTF-8.
    for flusso in (sys.stdout, sys.stderr):
        try:
            flusso.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    args = parse_args()
    resolved = load_obbligatorio(args.resolved)
    manifest = load(args.manifest, {})
    audit = load(args.audit, {})
    raw_displays = flatten_displays(load_dichiarato(args.displays, "degli espositori"))
    suppliers = supplier_ids(resolved, raw_displays)
    products = build_products(resolved, suppliers)
    products.extend(build_display_products(raw_displays, suppliers))
    warnings = []
    warnings.extend(anomalie_listino_summary(audit.get("warnings") or []))

    warnings.extend(decisioni_ai_scartate(resolved))
    warnings.extend(suspect_reject_summary(products))
    warnings.extend(senza_offerta_summary(products))

    now = datetime.now(tz=timezone.utc).isoformat()
    review = {
        "schemaVersion": 1,
        "run": {
            "id": args.run_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            "status": "ready",
            "generatedAt": now,
            "createdAt": now,
            "label": "Confronto ordini corrente",
            "thresholdNet": args.threshold,
            "productCount": len(products),
            "standardProductCount": len(resolved),
            "displayCount": len(products) - len(resolved),
        },
        "files": manifest_files(manifest, audit),
        "suppliers": [{"id": item, "name": supplier_name(item), "thresholdNet": args.threshold, "minimumOrder": args.threshold} for item in suppliers],
        "products": products,
        "warnings": warnings,
        "auditSummary": audit,
        "ui": {"currentStep": 1, "currency": "EUR", "locale": "it-IT"},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "products": len(products), "displays": review["run"]["displayCount"], "suppliers": suppliers}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
