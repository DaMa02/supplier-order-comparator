"""Single source of truth for what an offer is: who it belongs to, whether it can
be ordered, and what it costs per piece.

Answering them from two hand-maintained copies — one in
`server._offerta_piu_conveniente`, one in `pipeline_jobs._ripulisci_stato` (the
function that decides which quantities to zero out after a recompute) — is
fragile: a rule added to one and missed in the other produces a recompute that
fails to zero a quantity the compilation then rejects, with the symptom
surfacing elsewhere.

This module is the single authority, following the same pattern as `registro`,
`consegna` and `conferme`. It imports nothing from `server` or `pipeline_jobs`;
they import from it, so no import cycle can form.

Function names are kept as they were in `server.py` (English, unlike the rest
of the project's Italian) since this is a move, not a rewrite: renaming them in
the same commit would bury the one fact worth checking — that the bodies are
unchanged.
"""

from __future__ import annotations

from typing import Any


def numero(value: Any) -> float | None:
    """Parse a value as a number, or return `None` if it isn't one.

    Lives here rather than next to its caller because `offer_pricing` depends
    on it; `server.py` re-exports it as `number`, so the service and the offer
    module share one number parser instead of two that merely look alike.
    `True` is rejected rather than read as 1: a boolean field where a price is
    expected is bad data, not the number one.
    """

    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def offer_supplier_id(offer: Any) -> str:
    if not isinstance(offer, dict):
        return ""
    return str(offer.get("supplierId") or offer.get("supplier_id") or "")


def find_offer(product: Any, supplier_id: str) -> dict[str, Any] | None:
    """Return a supplier's offer on a product, the first one if the price list repeats it."""

    if not isinstance(product, dict) or not supplier_id:
        return None
    for offer in product.get("offers") or []:
        if offer_supplier_id(offer) == supplier_id:
            return offer
    return None


def offer_is_available(offer: Any) -> bool:
    """An offer that is missing or explicitly marked unavailable cannot be ordered.

    A missing field counts as available: older price lists never write it.
    This is the single answer to "can it be ordered" and should be called even
    where inlining the check would be shorter — a second copy is the risk, not
    the line count.
    """

    return isinstance(offer, dict) and offer.get("available") is not False


def offer_pricing(offer: Any) -> dict[str, float] | None:
    """Return an offer's factor and net prices, or `None` if they aren't usable.

    `factor` is the number of pieces contained in the order unit; for a
    display it's the pieces the display contains, not 1 — the order unit is
    the display, but what gets delivered is pieces. `unitPriceNet` is the
    price per piece, `orderUnitPriceNet` the price of the carton (or of the
    whole display).

    This contract is the same for every item type, which is what makes offers
    comparable: picking the "best alternative" orders on `unitPriceNet`, and
    for a display that number must be the per-piece price. These fields are
    produced by `display_offer` in `scripts/build_review_data.py`.

    The `or` chains below treat a legitimate `0` as a missing field. This is
    safe today because `catalog_search._offer` already discards prices `<= 0`
    upstream, before they reach here; accepted and tolerated field names are
    listed in `references/offerta.md`.
    """

    if not isinstance(offer, dict):
        return None
    factor = numero(offer.get("quantityFactor") or offer.get("unitsPerOrderUnit") or offer.get("units_per_order_unit")) or 1.0
    unit_price = numero(offer.get("unitPriceNet") or offer.get("pricePerPiece") or offer.get("price_per_piece"))
    order_price = numero(offer.get("orderUnitPriceNet") or offer.get("price") or offer.get("netPrice"))
    if order_price is None and unit_price is not None:
        order_price = unit_price * factor
    if unit_price is None and order_price is not None:
        unit_price = order_price / factor
    if order_price is None or unit_price is None or order_price < 0:
        return None
    return {"factor": factor, "unitPriceNet": unit_price, "orderUnitPriceNet": order_price}


# Status of an offer the operator rejected as "not the same item". Lives here,
# with the other answers to "can it be ordered", rather than in `server.py`:
# the service WRITES it and `da_reperire.motivo()` READS it, so a single
# source keeps the reason column from reverting to "no supplier has it" on a
# row the operator actually rejected.
#
# Named like the other comparison states (`EAN_ESATTO`, `SEMANTICO_PROPOSTO`,
# `SEMANTICO_CONFERMATO_UTENTE`): the negative reads next to its positive.
STATO_RIFIUTATO_UTENTE = "RIFIUTATO_UTENTE"
