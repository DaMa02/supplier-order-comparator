#!/usr/bin/env python3
"""Rileva e applica in modo conservativo le promozioni dei fornitori.

Il modulo non modifica i listini e non decide autonomamente un fornitore.  Le
promozioni vengono trasformate in un contratto JSON stabile, valutate rispetto
alle quantità scelte e infine aggiunte ai dati di revisione.

Regole di sicurezza economica:

* omaggi, campioncini e confezioni promozionali non riducono mai il totale;
* una regola ambigua non influisce mai sulla scelta del fornitore;
* un prezzo promozionale viene esposto soltanto per uno sconto numerico,
  deterministico, confermato e attivato;
* la decorazione conserva sempre prezzi e fornitore selezionato originali.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


KIND_NUMERIC_DISCOUNT = "sconto_numerico"
KIND_THRESHOLD_GIFT = "soglia_omaggio"
KIND_INCLUDED_PACK = "confezione_promozionale"
KIND_AMBIGUOUS = "offerta_ambigua"

CERTAINTY_HIGH = "alta"
CERTAINTY_MEDIUM = "media"
CERTAINTY_REVIEW = "da_verificare"

STATUS_EARNED = "ottenuta"
STATUS_NEAR = "vicina"
STATUS_NOT_REACHED = "non_raggiunta"
STATUS_REVIEW = "da_verificare"

KINDS = {
    KIND_NUMERIC_DISCOUNT,
    KIND_THRESHOLD_GIFT,
    KIND_INCLUDED_PACK,
    KIND_AMBIGUOUS,
}
CERTAINTIES = {CERTAINTY_HIGH, CERTAINTY_MEDIUM, CERTAINTY_REVIEW}

_NUMBER = r"\d+(?:[.,]\d+)?"
# I verbi che aprono una soglia stanno scritti una volta sola: quando erano
# ricopiati altrove (il ponte con il listino Larice ne teneva una versione
# piu' povera) una grafia vera come «ACQUISTANO 5 CT» era soglia per un
# rilevatore e testo qualunque per l'altro, e la condizione spariva.
_VERBI_SOGLIA = r"ACQUISTANDO|ACQUISTA(?:NDO|NO|TI)?|ACQUSITA(?:NDO)?|COMPRANDO|COMPRA|OGNI"
# Le unita' d'ordine ammesse in una soglia o in un premio. Vanno scritte per
# esteso, singolare e plurale, **mai** con la lettera finale facoltativa: con
# «COLLI?» il rilevatore si fermava a «COLL» e il premio usciva con un'unita'
# che non esiste, portandosi dietro la descrizione tagliata («O (12 PEZZI) DI
# NEVAL...»). Il 19 agosto 2026 lo stesso difetto era ancora aperto su
# «CARTONI?» e «PEZZI?»: «IN OMAGGIO 1 CARTONE DI NEVAL DEO» dava unita'
# «carton» e descrizione «E DI NEVAL DEO», cioe' non diceva che merce si
# prende in omaggio — che e' l'unica cosa su cui si decide se conviene
# arrivare alla soglia.
_UNITA_ORDINE = r"CT|CARTON[IE]|COLL[IO]|PZ|PEZZ[IO]"
_THRESHOLD_RE = re.compile(
    rf"\b(?:{_VERBI_SOGLIA})"
    rf"\s+(?P<qty>{_NUMBER})\s*(?P<unit>{_UNITA_ORDINE})\b",
    re.IGNORECASE,
)
_REWARD_RE = re.compile(
    rf"(?:IN\s+OMAGGIO|RICEVI(?:\s+IN\s+OMAGGIO)?)\s+"
    # ⚠ Il `\b` dopo il gruppo dell'unita' e' quello che `_THRESHOLD_RE` ha
    # sempre avuto e qui mancava: senza, l'unita' poteva fermarsi a meta' di
    # una parola e il resto finiva nella descrizione del premio.
    rf"(?P<qty>{_NUMBER})\s*(?P<unit>{_UNITA_ORDINE})?\b"
    rf"(?:\s*\((?P<details>[^)]*)\))?\s*(?:OMAGGIO)?\s*(?:DI\s+)?(?P<description>.+)$",
    re.IGNORECASE,
)
_INCLUDED_PACK_RE = re.compile(
    rf"\b(?P<base>{_NUMBER})\s*\+\s*(?P<extra>{_NUMBER})\b.*\b(?:GRATIS|OMAGGIO)\b",
    re.IGNORECASE,
)
_PROMOTION_MARKERS_RE = re.compile(
    rf"\b(?:OMAGGIO|GRATIS|OFFERTA|PROMOZIONE|PROMO|SCONTO|RICEVI|{_VERBI_SOGLIA})\b|\b1\s*\+\s*1\b",
    re.IGNORECASE,
)

# Le unita' di misura che possono governare una coppia «N+M». Quando una di
# queste tocca la coppia, i due numeri sono contenuto (millilitri, grammi,
# metri, lavaggi) e non pezzi in piu' nella confezione. E' l'unico segnale che
# il testo offre: la grandezza dei numeri non separa i due casi, perche'
# «8+2» e' un conteggio giusto sui rasoi e una misura sbagliata sui metri di
# alluminio.
_MISURA = r"(?:ML|CL|LT|L|GR|G|KG|MT|M|CM|MM|LAV(?:AGGI)?|W)"
# «MT.16+4»: l'unita' precede la coppia, eventualmente con il punto.
_MISURA_PRIMA_RE = re.compile(rf"\b{_MISURA}\s*\.?\s*$", re.IGNORECASE)
# «500+100 Omaggio=600 Ml»: l'unita' segue la coppia, eventualmente dopo la
# parola promozionale e dopo il totale ricomposto dal fornitore.
_MISURA_DOPO_RE = re.compile(
    rf"^\s*(?:GRATIS|OMAGGIO|IN\s+OMAGGIO)?\s*(?:=\s*{_NUMBER})?\s*{_MISURA}\b",
    re.IGNORECASE,
)

_UNIT_ALIASES = {
    "ct": "cartoni",
    "cartone": "cartoni",
    "cartoni": "cartoni",
    "collo": "colli",
    "colli": "colli",
    "pz": "pezzi",
    "pezzo": "pezzi",
    "pezzi": "pezzi",
    "unità": "unità",
    "unita": "unità",
    "espositore": "espositori",
    "espositori": "espositori",
}

# Il singolare delle unita' normalizzate: serve soltanto a scrivere un
# messaggio leggibile («1 cartone», non «1 cartoni»).
_UNITA_SINGOLARE = {
    "cartoni": "cartone",
    "colli": "collo",
    "pezzi": "pezzo",
    "unità": "unità",
    "espositori": "espositore",
}

_ORDINALI = {
    2: "secondo",
    3: "terzo",
    4: "quarto",
    5: "quinto",
    6: "sesto",
    7: "settimo",
    8: "ottavo",
    9: "nono",
    10: "decimo",
}


def _plain_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.upper().split())


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(" ", "").replace(",", ".")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _clean_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(float(value), 6)


def normalize_unit(value: Any) -> str | None:
    text = str(value or "").strip().casefold().rstrip(".")
    return _UNIT_ALIASES.get(text, text or None)


_INTESTAZIONE_SOGLIA_RE = re.compile(rf"\b(?:{_VERBI_SOGLIA})\s+{_NUMBER}", re.IGNORECASE)


def looks_like_threshold_heading(text: Any) -> bool:
    """Dice se un testo apre un blocco a soglia («ACQUISTANDO 5 CT ...»).

    Serve a chi legge un listino a blocchi e deve riconoscere la riga di
    intestazione prima di avere il testo completo — il nome del premio sta
    righe piu' sotto e in un'altra colonna.

    E' apposta piu' larga di `_THRESHOLD_RE`: qui basta un verbo d'acquisto
    seguito da un numero, senza pretendere l'unita'. Se pretendesse anche
    quella, un'intestazione con un'unita' mai vista («ACQUISTANDO 10
    SCATOLE») non aprirebbe nessun blocco e sparirebbe senza lasciare
    traccia; larga com'e', il blocco si apre, il rilevatore non lo calcola e
    chi legge se lo ritrova fra le condizioni da verificare. I verbi restano
    quelli del rilevatore: e' la duplicazione di quell'elenco che ha gia'
    fatto sparire una condizione vera.
    """

    return bool(_INTESTAZIONE_SOGLIA_RE.search(_plain_text(text)))


def looks_like_reward(text: Any) -> bool:
    """Dice se un testo e' la riga premio di un blocco («IN OMAGGIO 1 CT DI»).

    Qui il testo e' per forza parziale — il nome del prodotto regalato sta in
    un'altra colonna — quindi il segnale resta la sola formula d'apertura.
    """

    return bool(re.search(r"\b(?:IN\s+OMAGGIO|RICEVI)\b", _plain_text(text)))


def _promotion_id(supplier: str, source_reference: str, kind: str, source_text: str) -> str:
    raw = "\x1f".join((supplier.casefold(), source_reference, kind, _plain_text(source_text)))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"promo:{supplier.casefold()}:{digest}"


def _eligible_contract(eligible: Any = None, group: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "products": [],
        "eans": [],
        "source_rows": [],
        "group": group,
        "mix_allowed": None,
    }
    if isinstance(eligible, Mapping):
        for key in ("products", "eans", "source_rows"):
            raw = eligible.get(key) or []
            if not isinstance(raw, (list, tuple, set)):
                raw = [raw]
            result[key] = [item for item in raw if item not in (None, "")]
        result["group"] = eligible.get("group") or group
        if "mix_allowed" in eligible:
            result["mix_allowed"] = eligible.get("mix_allowed")
    elif isinstance(eligible, (list, tuple, set)):
        result["products"] = [item for item in eligible if item not in (None, "")]
    elif eligible not in (None, ""):
        result["products"] = [eligible]
    return result


def _has_eligibility_anchor(eligible: Mapping[str, Any]) -> bool:
    return any(eligible.get(key) for key in ("products", "eans", "source_rows")) or bool(eligible.get("group"))


def make_promotion(
    *,
    supplier: str,
    source_reference: str,
    source_text: str,
    kind: str,
    threshold_qty: Any = None,
    threshold_unit: str | None = None,
    eligible: Any = None,
    eligible_group: str | None = None,
    reward: Mapping[str, Any] | None = None,
    repeatable: bool | None = False,
    certainty: str = CERTAINTY_REVIEW,
    confirmed: bool = False,
    economic_effect: Mapping[str, Any] | None = None,
    promotion_id: str | None = None,
) -> dict[str, Any]:
    """Crea e valida il contratto JSON di una promozione."""

    supplier = str(supplier or "").strip().casefold()
    source_reference = str(source_reference or "").strip()
    source_text = str(source_text or "").strip()
    if not supplier:
        raise ValueError("supplier obbligatorio")
    if not source_reference:
        raise ValueError("source_reference obbligatorio")
    if kind not in KINDS:
        raise ValueError(f"kind non valido: {kind}")
    if certainty not in CERTAINTIES:
        raise ValueError(f"certainty non valida: {certainty}")

    threshold_value = _number(threshold_qty)
    threshold = {
        "qty": _clean_number(threshold_value),
        "unit": normalize_unit(threshold_unit),
    }
    eligible_value = _eligible_contract(eligible, eligible_group)
    reward_value = {
        "kind": None,
        "description": None,
        "qty": None,
        "unit": None,
        "pieces_per_unit": None,
        "ean": None,
        "supplier_code": None,
    }
    if reward:
        reward_value.update({key: reward.get(key) for key in reward_value})
        reward_value["qty"] = _clean_number(_number(reward_value.get("qty")))
        reward_value["unit"] = normalize_unit(reward_value.get("unit"))
        reward_value["pieces_per_unit"] = _clean_number(_number(reward_value.get("pieces_per_unit")))

    effect = {
        "type": "none",
        "discount_rate": None,
        "deterministic": False,
        "active": False,
        "affects_total": False,
        "affects_supplier_choice": False,
        "base_price_field": "unitPricePreDiscount",
        "already_applied": False,
    }
    if economic_effect:
        effect.update(dict(economic_effect))
    effect["discount_rate"] = _number(effect.get("discount_rate"))
    effect["deterministic"] = bool(effect.get("deterministic"))
    effect["active"] = bool(effect.get("active"))
    effect["affects_total"] = bool(effect.get("affects_total"))
    effect["affects_supplier_choice"] = bool(effect.get("affects_supplier_choice"))
    effect["already_applied"] = bool(effect.get("already_applied"))

    # Solo uno sconto numerico esplicito può avere effetto economico.
    if kind != KIND_NUMERIC_DISCOUNT or certainty == CERTAINTY_REVIEW or not confirmed:
        effect["affects_total"] = False
        effect["affects_supplier_choice"] = False
    if kind != KIND_NUMERIC_DISCOUNT:
        effect["discount_rate"] = None
    if not (effect["deterministic"] and effect["active"] and confirmed):
        effect["affects_total"] = False
        effect["affects_supplier_choice"] = False

    return {
        "id": promotion_id or _promotion_id(supplier, source_reference, kind, source_text),
        "supplier": supplier,
        "source_reference": source_reference,
        "source_text": source_text,
        "kind": kind,
        "threshold": threshold,
        "eligible": eligible_value,
        "reward": reward_value,
        "repeatable": bool(repeatable) if repeatable is not None else None,
        "certainty": certainty,
        "confirmed": bool(confirmed),
        "economic_effect": effect,
    }


def normalize_promotion(value: Mapping[str, Any]) -> dict[str, Any]:
    """Rivalida una promozione proveniente da JSON o da un rilevatore."""

    threshold = value.get("threshold") or {}
    return make_promotion(
        supplier=value.get("supplier") or "",
        source_reference=value.get("source_reference") or "",
        source_text=value.get("source_text") or "",
        kind=value.get("kind") or "",
        threshold_qty=threshold.get("qty"),
        threshold_unit=threshold.get("unit"),
        eligible=value.get("eligible"),
        reward=value.get("reward"),
        repeatable=value.get("repeatable"),
        certainty=value.get("certainty") or CERTAINTY_REVIEW,
        confirmed=bool(value.get("confirmed")),
        economic_effect=value.get("economic_effect"),
        promotion_id=value.get("id"),
    )


def detect_numeric_discount(
    *,
    supplier: str,
    source_reference: str,
    source_text: str,
    discount_value: Any,
    eligible: Any = None,
    confirmed: bool = True,
    already_applied: bool = False,
) -> dict[str, Any] | None:
    """Rileva uno sconto numerico Excel (0,10) o percentuale (10)."""

    rate = _number(discount_value)
    if rate is None or rate <= 0:
        return None
    if rate >= 1:
        rate = rate / 100
    if not 0 < rate < 1:
        return None
    certainty = CERTAINTY_HIGH
    return make_promotion(
        supplier=supplier,
        source_reference=source_reference,
        source_text=source_text,
        kind=KIND_NUMERIC_DISCOUNT,
        eligible=eligible,
        certainty=certainty,
        confirmed=confirmed,
        economic_effect={
            "type": "price_discount",
            "discount_rate": round(rate, 8),
            "deterministic": True,
            "active": bool(confirmed),
            "affects_total": bool(confirmed),
            "affects_supplier_choice": bool(confirmed),
            "base_price_field": "unitPricePreDiscount",
            "already_applied": bool(already_applied),
        },
    )


def detect_threshold_gift(
    *,
    supplier: str,
    source_reference: str,
    source_text: str,
    eligible: Any = None,
    eligible_group: str | None = None,
    reward_ean: str | None = None,
    confirmed: bool | None = None,
) -> dict[str, Any] | None:
    """Interpreta regole italiane del tipo "acquista N colli, ricevi X"."""

    normalized = _plain_text(source_text)
    threshold_match = _THRESHOLD_RE.search(normalized)
    reward_match = _REWARD_RE.search(normalized)
    if not threshold_match or not reward_match:
        return None

    threshold_qty = _number(threshold_match.group("qty"))
    threshold_unit = normalize_unit(threshold_match.group("unit"))
    reward_qty = _number(reward_match.group("qty"))
    reward_unit = normalize_unit(reward_match.group("unit")) or "unità"
    reward_details = reward_match.group("details") or ""
    pieces_match = re.search(rf"(?P<qty>{_NUMBER})\s*(?:PZ|PEZZI?)\b", reward_details, re.IGNORECASE)
    pieces_per_unit = _number(pieces_match.group("qty")) if pieces_match else None
    reward_description = re.sub(r"^[\s;,:.-]+", "", reward_match.group("description") or "").strip()
    eligible_value = _eligible_contract(eligible, eligible_group)
    if eligible_value.get("mix_allowed") is None:
        if re.search(r"\bTRA\b", normalized):
            eligible_value["mix_allowed"] = True
        elif len(eligible_value.get("products") or []) == 1 or len(eligible_value.get("eans") or []) == 1:
            eligible_value["mix_allowed"] = False

    complete = threshold_qty is not None and reward_qty is not None and bool(reward_description)
    certainty = CERTAINTY_HIGH if complete and _has_eligibility_anchor(eligible_value) else CERTAINTY_MEDIUM
    if confirmed is None:
        confirmed = certainty == CERTAINTY_HIGH
    reward_kind = "campioncino" if "CAMPION" in normalized else "prodotto"
    return make_promotion(
        supplier=supplier,
        source_reference=source_reference,
        source_text=source_text,
        kind=KIND_THRESHOLD_GIFT,
        threshold_qty=threshold_qty,
        threshold_unit=threshold_unit,
        eligible=eligible_value,
        reward={
            "kind": reward_kind,
            "description": reward_description,
            "qty": reward_qty,
            "unit": reward_unit,
            "pieces_per_unit": pieces_per_unit,
            "ean": reward_ean,
        },
        # La ripetibilita' non si legge nel listino: e' una condizione del
        # rapporto commerciale, decisa dall'utente e valida per tutte le
        # soglie. Dedurla dalla parola «OGNI» era una regola scritta guardando
        # un solo fornitore: nei listini Larice «OGNI» non compare mai, quindi
        # a soglia 20 con 40 cartoni acquistati il programma dichiarava un
        # omaggio solo invece di due.
        repeatable=True,
        certainty=certainty,
        confirmed=bool(confirmed),
        economic_effect={
            "type": "informational_reward",
            "deterministic": complete,
            "active": bool(confirmed),
            "affects_total": False,
            "affects_supplier_choice": False,
        },
    )


def detect_included_pack(
    *,
    supplier: str,
    source_reference: str,
    source_text: str,
    eligible: Any = None,
) -> dict[str, Any] | None:
    """Rileva confezioni come "11+1 gratis", già incluse nel prezzo."""

    normalized = _plain_text(source_text)
    match = _INCLUDED_PACK_RE.search(normalized)
    if not match:
        return None
    prima = normalized[: match.start("base")]
    dopo = normalized[match.end("extra") :]
    if _MISURA_PRIMA_RE.search(prima) or _MISURA_DOPO_RE.match(dopo):
        # «500+100 Omaggio=600 Ml» sono millilitri, «MT.16+4 GRATIS» sono
        # metri: dichiararli «unita' aggiuntive» con certezza alta sarebbe un
        # numero inventato. Il testo torna al rilevatore delle offerte
        # ambigue, che lo mostra all'utente senza calcolarlo.
        return None
    extra = _number(match.group("extra"))
    return make_promotion(
        supplier=supplier,
        source_reference=source_reference,
        source_text=source_text,
        kind=KIND_INCLUDED_PACK,
        threshold_qty=1,
        threshold_unit="unità",
        eligible=eligible,
        reward={
            "kind": "contenuto_aggiuntivo",
            "description": (
                f"{_clean_number(extra)} unità aggiuntiva già nella confezione"
                if extra == 1
                else f"{_clean_number(extra)} unità aggiuntive già nella confezione"
            ),
            "qty": extra,
            "unit": "pezzi",
        },
        repeatable=True,
        certainty=CERTAINTY_HIGH,
        confirmed=True,
        economic_effect={
            "type": "included_in_pack",
            "deterministic": True,
            "active": True,
            "affects_total": False,
            "affects_supplier_choice": False,
        },
    )


def detect_ambiguous_offer(
    *,
    supplier: str,
    source_reference: str,
    source_text: str,
    eligible: Any = None,
) -> dict[str, Any] | None:
    """Conserva un'offerta testuale che non può essere calcolata."""

    if not _PROMOTION_MARKERS_RE.search(_plain_text(source_text)):
        return None
    return make_promotion(
        supplier=supplier,
        source_reference=source_reference,
        source_text=source_text,
        kind=KIND_AMBIGUOUS,
        eligible=eligible,
        certainty=CERTAINTY_REVIEW,
        confirmed=False,
        economic_effect={
            "type": "none",
            "deterministic": False,
            "active": False,
            "affects_total": False,
            "affects_supplier_choice": False,
        },
    )


def detect_promotions(
    *,
    supplier: str,
    source_reference: str,
    source_text: str,
    discount_value: Any = None,
    eligible: Any = None,
    eligible_group: str | None = None,
    included_in_product: bool = False,
    numeric_discount_already_applied: bool = False,
) -> list[dict[str, Any]]:
    """Rileva tutte le promozioni presenti in una riga o annotazione fonte."""

    promotions: list[dict[str, Any]] = []
    numeric = detect_numeric_discount(
        supplier=supplier,
        source_reference=source_reference,
        source_text=source_text,
        discount_value=discount_value,
        eligible=eligible,
        already_applied=numeric_discount_already_applied,
    )
    if numeric:
        promotions.append(numeric)

    textual: dict[str, Any] | None = detect_threshold_gift(
        supplier=supplier,
        source_reference=source_reference,
        source_text=source_text,
        eligible=eligible,
        eligible_group=eligible_group,
    )
    if textual is None and included_in_product:
        textual = detect_included_pack(
            supplier=supplier,
            source_reference=source_reference,
            source_text=source_text,
            eligible=eligible,
        )
    if textual is None:
        textual = detect_ambiguous_offer(
            supplier=supplier,
            source_reference=source_reference,
            source_text=source_text,
            eligible=eligible,
        )
    if textual:
        promotions.append(textual)
    return promotions


def calculate_effective_price(base_price: Any, promotion: Mapping[str, Any]) -> dict[str, Any]:
    """Calcola un prezzo scontato soltanto quando tutte le condizioni sono sicure."""

    base = _number(base_price)
    normalized = normalize_promotion(promotion)
    effect = normalized["economic_effect"]
    rate = _number(effect.get("discount_rate"))
    can_apply = (
        base is not None
        and normalized["kind"] == KIND_NUMERIC_DISCOUNT
        and normalized["certainty"] != CERTAINTY_REVIEW
        and normalized["confirmed"]
        and effect.get("deterministic")
        and effect.get("active")
        and effect.get("affects_total")
        and rate is not None
        and 0 < rate < 1
        and not effect.get("already_applied")
    )
    effective = round(base * (1 - rate), 6) if can_apply else base
    return {
        "base_price": base,
        "effective_price": effective,
        "applied": bool(can_apply),
        "promotion_id": normalized["id"],
    }


def _supplier_offer(product: Mapping[str, Any], supplier: str) -> Mapping[str, Any] | None:
    return next(
        (
            offer
            for offer in product.get("offers") or []
            if str(offer.get("supplierId") or offer.get("supplier") or "").casefold() == supplier.casefold()
        ),
        None,
    )


def _as_strings(values: Iterable[Any]) -> set[str]:
    return {str(value).strip().casefold() for value in values if value not in (None, "")}


def _matches_eligibility(product: Mapping[str, Any], promotion: Mapping[str, Any]) -> bool:
    eligible = promotion.get("eligible") or {}
    supplier = str(promotion.get("supplier") or "")
    offer = _supplier_offer(product, supplier)
    product_values = {
        str(product.get("id") or "").strip().casefold(),
        str(product.get("ean") or "").strip().casefold(),
    }
    product_values.discard("")
    wanted_products = _as_strings(eligible.get("products") or [])
    wanted_eans = _as_strings(eligible.get("eans") or [])
    if product_values & (wanted_products | wanted_eans):
        return True

    wanted_rows = _as_strings(eligible.get("source_rows") or [])
    candidate_rows = _as_strings(
        [product.get("sourceRow"), offer.get("sourceRow") if offer else None]
    )
    if wanted_rows & candidate_rows:
        return True

    group = str(eligible.get("group") or "").strip().casefold()
    if group:
        groups: list[Any] = []
        for holder in (product, offer or {}):
            groups.extend(holder.get("promotionGroups") or [])
            groups.extend(holder.get("promotion_groups") or [])
            groups.append(holder.get("promotionGroup"))
            groups.append(holder.get("promotion_group"))
        if group in _as_strings(groups):
            return True
    return False


def _selection_for(
    product: Mapping[str, Any],
    selections: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[float, str]:
    override = (selections or {}).get(str(product.get("id") or ""), {})
    quantity = _number(override.get("quantity", product.get("quantity"))) or 0
    supplier = str(
        override.get("selectedSupplierId")
        or override.get("supplierId")
        or override.get("supplier")
        or product.get("selectedSupplierId")
        or ""
    ).casefold()
    return max(quantity, 0), supplier


def _quantity_in_threshold_unit(
    product: Mapping[str, Any],
    supplier: str,
    quantity: float,
    threshold_unit: str | None,
) -> float | None:
    threshold_unit = normalize_unit(threshold_unit)
    product_unit = normalize_unit(product.get("quantityLabel") or product.get("orderUnitLabel"))
    if threshold_unit in (None, "unità") or threshold_unit == product_unit:
        return quantity
    if threshold_unit in {"cartoni", "colli"} and product_unit in {"cartoni", "colli"}:
        return quantity
    if threshold_unit in {"cartoni", "colli"} and product_unit == "pezzi":
        offer = _supplier_offer(product, supplier)
        factor = _number((offer or {}).get("quantityFactor") or (offer or {}).get("unitsPerOrderUnit"))
        return math.ceil(quantity / factor) if factor is not None and factor > 0 else None
    if threshold_unit == "pezzi":
        if product_unit == "pezzi":
            return quantity
        offer = _supplier_offer(product, supplier)
        factor = _number((offer or {}).get("unitsPerOrderUnit") or (offer or {}).get("quantityFactor"))
        return quantity * factor if factor is not None else None
    return None


def _con_unita(quantity: Any, unit: str | None, *, fallback: str = "unità") -> str:
    """«1 cartone», «6 cartoni»: quantità e unità concordate."""

    value = _clean_number(_number(quantity))
    unit = normalize_unit(unit) or fallback
    if value == 1:
        unit = _UNITA_SINGOLARE.get(unit, unit)
    return f"{value} {unit}" if value is not None else unit


def _premio_per_esteso(reward: Mapping[str, Any]) -> str:
    """«1 cartone di RESALINA SALE LAVASTOVIGLIE KG1», non «1 cartone».

    ⚠ Il 15 agosto 2026, letto sulla scheda di NEVAL SALVIETTE: «dici 10
    cartoni = 1 cartone omaggio, ma non e' vero».  Aveva ragione: l'omaggio non
    era un cartone di quel prodotto, era un cartone di sale per lavastoviglie.
    Una regola che non nomina il premio si legge come «lo stesso prodotto».
    """

    quantita = _con_unita(reward.get("qty") or 1, reward.get("unit"))
    descrizione = " ".join(str(reward.get("description") or "").split())
    return f"{quantita} di {descrizione}" if descrizione else quantita


def _messaggio_soglia_ottenuta(
    *,
    threshold_qty: float | None,
    threshold_unit: str | None,
    progress: float,
    remaining: float | None,
    reward_count: int,
    reward: Mapping[str, Any],
    repeatable: bool,
    prodotti_in_offerta: int = 1,
) -> str:
    """Dice quanto manca al prossimo omaggio, non che l'omaggio e' arrivato.

    Con le soglie ripetibili «omaggio ottenuto» e' l'informazione inutile: chi
    ordina deve sapere se conviene aggiungere qualche cartone per prenderne un
    altro. Il messaggio percio' ripete la regola, la quantita' raggiunta e la
    distanza dal premio successivo.

    ⚠ E dice DOVE si conta.  La soglia di LARICE vale su un gruppo di prodotti
    («ACQUISTANDO 10 CT TRA: …»): sulla scheda di uno solo di quelli, «ne hai
    49» sembrava riferito a quel prodotto, che di cartoni ne aveva uno.
    """

    fra_i_prodotti = (
        f" fra i {prodotti_in_offerta} prodotti dell'offerta" if prodotti_in_offerta > 1 else ""
    )
    regola = (
        f"{_con_unita(threshold_qty, threshold_unit)}{fra_i_prodotti} = "
        f"{_premio_per_esteso(reward)} in omaggio"
    )
    raggiunto = f"ne hai {_clean_number(progress)}{' in tutto' if fra_i_prodotti else ''}"
    if not repeatable:
        return f"{regola}: {raggiunto}, l'omaggio spetta una volta sola."
    quanti = "" if reward_count <= 1 else f", sono {reward_count} omaggi"
    prossimo = _ORDINALI.get(reward_count + 1)
    # ⚠ «per il ottavo». L'unico ordinale di questa tabella che comincia per
    # vocale e' «ottavo», e con sette omaggi gia' presi la frase si leggeva
    # cosi' sulla pagina vera (LARICE, 19 agosto 2026).
    verso = (
        (f"l'{prossimo}" if prossimo[0] in "aeiou" else f"il {prossimo}")
        if prossimo
        else "il prossimo"
    )
    mancano = _clean_number(remaining)
    if mancano is None:
        return f"{regola}: {raggiunto}{quanti}."
    return (
        f"{regola}: {raggiunto}{quanti}, te ne mancano "
        f"{_con_unita(mancano, threshold_unit)} per {verso}."
    )


def calculate_promotion_state(
    promotion: Mapping[str, Any],
    review_data: Mapping[str, Any],
    *,
    selections: Mapping[str, Mapping[str, Any]] | None = None,
    near_ratio: float = 0.8,
) -> dict[str, Any]:
    """Calcola progresso e stato: ottenuta, vicina o da verificare."""

    normalized = normalize_promotion(promotion)
    threshold = normalized["threshold"]
    threshold_qty = _number(threshold.get("qty"))
    threshold_unit = threshold.get("unit")
    matched: list[str] = []
    progress = 0.0
    unit_mismatch = False
    for product in review_data.get("products") or []:
        if not _matches_eligibility(product, normalized):
            continue
        matched.append(str(product.get("id") or ""))
        quantity, selected_supplier = _selection_for(product, selections)
        if selected_supplier != normalized["supplier"] or quantity <= 0:
            continue
        converted = _quantity_in_threshold_unit(product, normalized["supplier"], quantity, threshold_unit)
        if converted is None:
            unit_mismatch = True
        else:
            progress += converted

    progress = round(progress, 6)
    status = STATUS_NOT_REACHED
    remaining: float | None = None
    reward_count = 0

    cannot_evaluate = (
        normalized["kind"] == KIND_AMBIGUOUS
        or normalized["certainty"] == CERTAINTY_REVIEW
        or not normalized["confirmed"]
        or not _has_eligibility_anchor(normalized["eligible"])
        or unit_mismatch
    )
    if cannot_evaluate:
        status = STATUS_REVIEW
    elif normalized["kind"] == KIND_THRESHOLD_GIFT:
        if threshold_qty is None or threshold_qty <= 0:
            status = STATUS_REVIEW
        else:
            remaining = max(threshold_qty - (progress % threshold_qty if normalized["repeatable"] and progress >= threshold_qty else progress), 0)
            if progress >= threshold_qty:
                status = STATUS_EARNED
                reward_count = math.floor(progress / threshold_qty) if normalized["repeatable"] else 1
                if normalized["repeatable"] and progress % threshold_qty == 0:
                    remaining = threshold_qty
            elif progress > 0 and (progress >= threshold_qty * near_ratio or threshold_qty - progress <= 1):
                status = STATUS_NEAR
                remaining = threshold_qty - progress
            else:
                status = STATUS_NOT_REACHED
                remaining = threshold_qty - progress
    else:
        status = STATUS_EARNED if progress > 0 else STATUS_NOT_REACHED
        reward_count = 1 if status == STATUS_EARNED else 0

    reward = normalized.get("reward") or {}
    if status == STATUS_EARNED and normalized["kind"] == KIND_THRESHOLD_GIFT:
        message = _messaggio_soglia_ottenuta(
            threshold_qty=threshold_qty,
            threshold_unit=threshold_unit,
            progress=progress,
            remaining=remaining,
            reward_count=reward_count,
            reward=reward,
            repeatable=bool(normalized["repeatable"]),
            prodotti_in_offerta=len(matched),
        )
    elif status == STATUS_NEAR:
        quanti = _clean_number(remaining)
        message = (
            f"Ne {'manca' if quanti == 1 else 'mancano'} {_con_unita(quanti, threshold_unit)}"
            f"{f' fra i {len(matched)} prodotti dell’offerta' if len(matched) > 1 else ''}"
            f" per avere in omaggio {_premio_per_esteso(reward)}."
        )
    elif status == STATUS_REVIEW:
        message = "Condizioni della promozione da verificare prima dell'ordine."
    elif normalized["kind"] == KIND_NUMERIC_DISCOUNT:
        if status == STATUS_EARNED:
            message = (
                "Sconto già compreso nel prezzo indicato."
                if normalized["economic_effect"].get("already_applied")
                else "Sconto applicabile al prezzo promozionale calcolato."
            )
        else:
            message = "Sconto disponibile ma nessuna quantità selezionata."
    else:
        message = "Promozione disponibile ma soglia non raggiunta."

    return {
        "promotion_id": normalized["id"],
        "supplier": normalized["supplier"],
        "status": status,
        "progress_qty": _clean_number(progress),
        "threshold_qty": _clean_number(threshold_qty),
        "unit": threshold_unit,
        "remaining_qty": _clean_number(remaining),
        "reward_count": reward_count,
        "matched_product_ids": matched,
        "message": message,
    }


def _compact_promotion(promotion: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": promotion["id"],
        "supplier": promotion["supplier"],
        "kind": promotion["kind"],
        "certainty": promotion["certainty"],
        "confirmed": promotion["confirmed"],
        "source_text": promotion["source_text"],
        "threshold": promotion["threshold"],
        "reward": promotion["reward"],
        "economic_effect": promotion["economic_effect"],
        "state": dict(state),
    }


def decorate_offer(
    offer: Mapping[str, Any],
    product: Mapping[str, Any],
    promotions: Sequence[Mapping[str, Any]],
    states: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggiunge promozioni a una singola offerta senza mutarne i prezzi base."""

    decorated = copy.deepcopy(dict(offer))
    supplier = str(offer.get("supplierId") or offer.get("supplier") or "").casefold()
    relevant = [
        promotion
        for promotion in promotions
        if promotion["supplier"] == supplier and _matches_eligibility(product, promotion)
    ]
    decorated["promotions"] = [
        _compact_promotion(promotion, states[promotion["id"]]) for promotion in relevant
    ]

    # Il prezzo promozionale è un campo aggiuntivo. Il prezzo originale non
    # viene mai sovrascritto e non si esegue una nuova scelta del fornitore.
    applied_prices = []
    for promotion in relevant:
        effect = promotion["economic_effect"]
        base_field = str(effect.get("base_price_field") or "unitPricePreDiscount")
        result = calculate_effective_price(offer.get(base_field), promotion)
        if result["applied"]:
            factor = _number(offer.get("unitsPerOrderUnit") or offer.get("quantityFactor")) or 1
            applied_prices.append(
                {
                    **result,
                    "base_price_field": base_field,
                    "effective_order_unit_price": round(result["effective_price"] * factor, 6),
                }
            )
    if applied_prices:
        best = min(applied_prices, key=lambda item: item["effective_order_unit_price"])
        decorated["promotionEffectiveUnitPrice"] = best["effective_price"]
        decorated["promotionEffectiveOrderUnitPrice"] = best["effective_order_unit_price"]
        decorated["promotionPriceSourceId"] = best["promotion_id"]
    return decorated


def decorate_review_data(
    review_data: Mapping[str, Any],
    promotions: Sequence[Mapping[str, Any]],
    *,
    selections: Mapping[str, Mapping[str, Any]] | None = None,
    near_ratio: float = 0.8,
) -> dict[str, Any]:
    """Decora il modello della pagina di confronto senza cambiare scelte o totali."""

    decorated = copy.deepcopy(dict(review_data))
    normalized = [normalize_promotion(value) for value in promotions]
    states = {
        promotion["id"]: calculate_promotion_state(
            promotion,
            review_data,
            selections=selections,
            near_ratio=near_ratio,
        )
        for promotion in normalized
    }

    products = []
    for product in decorated.get("products") or []:
        relevant = [promotion for promotion in normalized if _matches_eligibility(product, promotion)]
        product["promotions"] = [
            _compact_promotion(promotion, states[promotion["id"]]) for promotion in relevant
        ]
        product["offers"] = [
            decorate_offer(offer, product, normalized, states) for offer in product.get("offers") or []
        ]
        products.append(product)
    decorated["products"] = products

    counts = Counter(state["status"] for state in states.values())
    by_supplier: dict[str, Counter[str]] = defaultdict(Counter)
    for promotion in normalized:
        by_supplier[promotion["supplier"]][states[promotion["id"]]["status"]] += 1
    decorated["promotions"] = [
        {**promotion, "state": states[promotion["id"]]} for promotion in normalized
    ]
    decorated["promotionSummary"] = {
        "counts": dict(counts),
        "bySupplier": {supplier: dict(values) for supplier, values in by_supplier.items()},
        "earnedIds": [key for key, state in states.items() if state["status"] == STATUS_EARNED],
        "nearIds": [key for key, state in states.items() if state["status"] == STATUS_NEAR],
        "reviewIds": [key for key, state in states.items() if state["status"] == STATUS_REVIEW],
    }
    return decorated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-data", type=Path, required=True)
    parser.add_argument("--promotions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    review_data = json.loads(args.review_data.read_text(encoding="utf-8"))
    promotions = json.loads(args.promotions.read_text(encoding="utf-8"))
    if isinstance(promotions, Mapping):
        promotions = promotions.get("promotions") or []
    result = decorate_review_data(review_data, promotions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "promotions": len(promotions)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
