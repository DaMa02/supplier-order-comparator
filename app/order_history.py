#!/usr/bin/env python3
"""History of compiled orders, to remember merchandise not yet received.

The store orders one week, the supplier fails to deliver, the item never
enters the management software: the following week the same item would be
reordered without anyone noticing. This module keeps one entry per supplier
for every successful compilation — successful meaning the price-list copies
are written to disk, anything less isn't an order — and lets the user answer
once for the whole order.

There are three possible answers, always for the whole order, since partial
deliveries don't exist: "received" closes it, "not yet" postpones the
question to next week, "will never arrive" closes it without claiming the
merchandise arrived. An unanswered question expires after 60 days, and the
expiry is reported (`expired_orders`) rather than fading out silently.

The module is self-contained: it knows nothing about the HTTP server or
ReviewStore, so it stays independently testable. The archive lives outside
the current run's folder (app/data/history/orders.json) so it survives the
weekly recompute.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import scrittura_sicura  # noqa: E402


SCHEMA_VERSION = 1
STATUS_PENDING = "in_attesa"
STATUS_RECEIVED = "ricevuto"
STATUS_EXPIRED = "scaduto"
# "Will never arrive": the only way to close a question without claiming the
# merchandise arrived. Without this answer, an undelivered order would keep
# asking about itself every week for sixty days.
STATUS_CLOSED = "non_arrivera"
STATI_NOTI = {STATUS_PENDING, STATUS_RECEIVED, STATUS_EXPIRED, STATUS_CLOSED}
EXPIRY_DAYS = 60
# After a "not yet arrived" answer, the question returns the following week:
# the store places orders once a week, so that's the point where re-asking
# makes sense.
REASK_DAYS = 7
# How many days an order's expiry notice stays on the page. A month: someone
# who works the page once a week sees it at least four times, then it stops
# taking up space with old news.
EXPIRY_NOTICE_DAYS = 30


def empty_history() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "orders": []}


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def to_iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).astimezone(timezone.utc).isoformat()


def parse_moment(value: Any) -> datetime | None:
    """Parse an ISO8601 string; returns None when the date can't be read."""

    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_ean(value: Any) -> str:
    """The barcode with surrounding whitespace stripped: the preferred key."""

    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


# A product identifier counts as an identity only when it's built from
# content, not from sheet position. Of the two display-product id shapes
# (`scripts/build_review_data.py`, build_display_products), only
# `display:composition:<ean>:<pieces>|...` is derived from the composition,
# so it still names the same item next week; `display:unmatched:...` embeds
# the chosen supplier and description and changes when the supplier changes
# — matching on it would silently miss the notice. `product:<row>` never:
# it depends on the row in the weekly export, and today's product:198 isn't
# last week's.
PREFISSI_IDENTITA_STABILE = ("display:composition:",)


def stable_product_id(value: Any) -> str:
    """The identifier, if it's of the kind comparable across weeks."""

    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    return text if text.startswith(PREFISSI_IDENTITA_STABILE) else ""


def match_key(ean: Any, product_id: Any = "") -> str:
    """The key that matches an order line to a product in the comparison.

    EAN first, since that alone identifies an item. When there's no EAN —
    displays never have one in the comparison — the product's identity takes
    over, but only when it's one of the stable ones: without this fallback,
    an ordered display would stay invisible the following week.
    """

    normalized = normalize_ean(ean)
    if normalized:
        return f"ean:{normalized}"
    stable = stable_product_id(product_id)
    return f"prodotto:{stable}" if stable else ""


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _quantity(value: Any) -> float:
    parsed = _number(value) or 0.0
    return int(parsed) if float(parsed).is_integer() else parsed


def _default_supplier_name(supplier_id: str) -> str:
    return str(supplier_id or "fornitore").upper()


def _normalized_entry(raw: dict[str, Any]) -> dict[str, Any]:
    entry = dict(raw)
    entry["orderId"] = str(entry.get("orderId") or "")
    entry["supplier"] = str(entry.get("supplier") or "")
    entry["supplierName"] = str(entry.get("supplierName") or _default_supplier_name(entry["supplier"]))
    entry["runId"] = str(entry.get("runId") or "")
    entry["createdAt"] = str(entry.get("createdAt") or "")
    status = str(entry.get("status") or "")
    entry["status"] = status if status in STATI_NOTI else STATUS_PENDING
    answered = entry.get("answeredAt")
    entry["answeredAt"] = str(answered) if answered else None
    # When the expiry fired: needed to report it on the page. Archives
    # written before this field existed lack it and stay readable without it.
    expired = entry.get("expiredAt")
    entry["expiredAt"] = str(expired) if expired else None
    entry["totalNet"] = round(_number(entry.get("totalNet")) or 0.0, 2)
    lines = entry.get("lines")
    entry["lines"] = [dict(line) for line in lines if isinstance(line, dict)] if isinstance(lines, list) else []
    return entry


def load_history(path: Path) -> dict[str, Any]:
    """Read the archive; returns an empty history if the file is missing."""

    file_path = Path(path)
    if not file_path.exists():
        return empty_history()
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Lo storico degli ordini non è leggibile: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Lo storico degli ordini non contiene un oggetto")
    orders = raw.get("orders")
    if orders is not None and not isinstance(orders, list):
        raise ValueError("Lo storico degli ordini non contiene un elenco di ordini")
    history = dict(raw)
    history["schema_version"] = int(_number(raw.get("schema_version")) or SCHEMA_VERSION)
    history["orders"] = [_normalized_entry(item) for item in orders or [] if isinstance(item, dict)]
    return history


def save_history(path: Path, history: dict[str, Any]) -> None:
    """Atomic write, confirmed to reach disk: handled by `scrittura_sicura`.

    This is the memory that says what was ordered and hasn't arrived yet:
    finding it empty after a crash means nobody will ever ask "has it
    arrived?" for that merchandise again.
    """

    scrittura_sicura.scrivi_json(Path(path), history)


def expire_pending(
    history: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_days: int = EXPIRY_DAYS,
) -> bool:
    """Expire pending questions older than the limit; True if anything changed."""

    limit = (now or utc_now()) - timedelta(days=max_age_days)
    changed = False
    for entry in history.get("orders") or []:
        if entry.get("status") != STATUS_PENDING:
            continue
        created = parse_moment(entry.get("createdAt"))
        # The sixty days count from the LAST interaction, not from creation:
        # with the question returning every seven days, an order answered
        # "not yet arrived" yesterday would otherwise expire today as if
        # nobody had ever answered it, mislabeling a diligent answer as
        # "no response".
        answered = parse_moment(entry.get("answeredAt"))
        ultima = max(momento for momento in (created, answered) if momento is not None) \
            if (created or answered) else None
        # A missing or unreadable date counts as expired: without a date the
        # question can't be placed in time and would stay pending forever.
        if ultima is not None and ultima >= limit:
            continue
        entry["status"] = STATUS_EXPIRED
        # The expiry moment is recorded, so it can be shown on the page
        # instead of the question silently going quiet.
        entry["expiredAt"] = to_iso(now)
        changed = True
    return changed


def expired_orders(
    history: dict[str, Any],
    *,
    now: datetime | None = None,
    window_days: int = EXPIRY_NOTICE_DAYS,
) -> list[dict[str, Any]]:
    """Recently expired orders, to report on the page.

    An order that expires is a question the program stops asking: it has to
    be reported, or the missing merchandise disappears without anyone
    knowing. Excludes expiries older than the window and entries without an
    `expiredAt` (older archives lack it), which cannot be placed in time.
    """

    limit = (now or utc_now()) - timedelta(days=window_days)
    notices = []
    for entry in history.get("orders") or []:
        if not isinstance(entry, dict) or entry.get("status") != STATUS_EXPIRED:
            continue
        expired = parse_moment(entry.get("expiredAt"))
        if expired is None or expired < limit:
            continue
        notices.append({
            "orderId": str(entry.get("orderId") or ""),
            "supplier": str(entry.get("supplier") or ""),
            "supplierName": str(entry.get("supplierName") or _default_supplier_name(str(entry.get("supplier") or ""))),
            "createdAt": str(entry.get("createdAt") or ""),
            # Whoever builds the message needs the truth: "no response" and
            # "last answered on ..." are two different pieces of news.
            "answeredAt": str(entry.get("answeredAt") or "") or None,
            "expiredAt": str(entry.get("expiredAt") or ""),
            "lineCount": len(entry.get("lines") or []),
            "totalNet": round(_number(entry.get("totalNet")) or 0.0, 2),
        })
    notices.sort(key=lambda item: item["expiredAt"])
    return notices


def read_history(path: Path, *, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Load the archive, applying the 60-day expiry.

    Returns (history, changed): when changed, the caller should save it back.
    """

    history = load_history(path)
    changed = expire_pending(history, now=now)
    return history, changed


def order_lines_from_plan(plan_lines: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = []
    for line in plan_lines:
        if not isinstance(line, dict):
            continue
        lines.append({
            "ean": normalize_ean(line.get("ean")),
            "description": str(line.get("description") or ""),
            "quantity": _quantity(line.get("quantity")),
            # The unit ordered in: the same code can be a display one week
            # and a regular product the next, and the notice must say
            # "displays" if it was displays.
            "unit": str(line.get("desired_quantity_unit") or "colli"),
            "orderUnitPriceNet": round(_number(line.get("order_unit_price_net")) or 0.0, 6),
            # Product identity in the comparison: for displays, which have
            # no EAN, this is the only way to find them again next week (see
            # `match_key`).
            "productId": str(line.get("product_id") or ""),
        })
    return lines


def _run_key(run_id: Any, created_at: Any) -> str:
    """The week an order belongs to.

    Without a runId (a comparison run with no identifier) falls back to the
    date, so orders from different days don't collapse onto the same key.
    """

    run = str(run_id or "").strip()
    return run or f"senza-run-{str(created_at or '')[:10]}"


def _entry_run_key(entry: dict[str, Any]) -> str:
    return _run_key(entry.get("runId"), entry.get("createdAt"))


def _is_replaceable(entry: dict[str, Any]) -> bool:
    """A question still open: a re-compilation for the same supplier replaces it.

    Open means `in_attesa`, even if the user already answered "not yet
    arrived": that answer was about a compilation a re-compilation has since
    superseded, and leaving it standing next to the new one would produce two
    questions for the same week and double-counted quantities. "Received",
    "will never arrive" and "expired" are closed history: they're never
    touched or reissued.
    """

    return entry.get("status") == STATUS_PENDING


def record_plan(
    history: dict[str, Any],
    plan: dict[str, Any],
    *,
    now: datetime | None = None,
    supplier_name: Callable[[str], str] | None = None,
    order_key: str = "",
    delivered: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Record a pending entry for every supplier the plan actually delivered.

    Identifier: `"<order_key>:<supplier>"`, where `order_key` is the
    compilation's dated folder. Two compilations of the same run are two
    different orders — the user may already have sent the first one — and
    the second must not overwrite it.

    Replacement: only the latest compilation of the same week is kept, and
    only among questions still without an answer. An answer already given
    stays where it is: its question doesn't return and doesn't duplicate.

    `delivered` are the suppliers for which the compilation actually wrote a
    copy: a supplier left without a copy doesn't become an order, and doesn't
    erase what was there before either, since a failed compilation changes
    nothing about what was already sent.
    """

    label = supplier_name or _default_supplier_name
    created_at = to_iso(now)
    run_id = str((plan or {}).get("run_id") or "").strip()
    run_key = _run_key(run_id, created_at)
    order_prefix = str(order_key or "").strip() or run_key

    grouped: dict[str, list[dict[str, Any]]] = {}
    totals: dict[str, float] = {}
    for line in (plan or {}).get("orders") or []:
        if not isinstance(line, dict):
            continue
        # Casefolded on both sides: `run_writer` hands over casefolded
        # identifiers, and a supplier key with different casing in the plan
        # would never match its own delivered copy, silently leaving the
        # order out of the history.
        supplier = str(line.get("supplier") or "").strip().casefold()
        if not supplier:
            continue
        grouped.setdefault(supplier, []).append(line)
        total = _number(line.get("line_total_net"))
        if total is None:
            total = (_number(line.get("quantity")) or 0.0) * (_number(line.get("order_unit_price_net")) or 0.0)
        totals[supplier] = totals.get(supplier, 0.0) + total

    consegnati = ({str(item).strip().casefold() for item in delivered}
                  if delivered is not None else set(grouped))

    orders = history.setdefault("orders", [])
    history.setdefault("schema_version", SCHEMA_VERSION)
    existing = {str(item.get("orderId") or ""): item for item in orders if isinstance(item, dict)}

    recorded = []
    for supplier, plan_lines in grouped.items():
        if supplier not in consegnati:
            continue
        order_id = f"{order_prefix}:{supplier}"
        payload = {
            "orderId": order_id,
            "createdAt": created_at,
            "supplier": supplier,
            "supplierName": str(label(supplier)),
            "runId": run_id,
            "status": STATUS_PENDING,
            "answeredAt": None,
            "totalNet": round(totals.get(supplier, 0.0), 2),
            "lines": order_lines_from_plan(plan_lines),
        }
        entry = existing.get(order_id)
        if entry is None:
            orders.append(payload)
            existing[order_id] = payload
            recorded.append(payload)
            continue
        # Same compilation recorded twice: updated, not duplicated.
        payload["createdAt"] = str(entry.get("createdAt") or created_at)
        if entry.get("answeredAt") or entry.get("status") != STATUS_PENDING:
            # An answer already given to the SAME compilation doesn't revert
            # just because it's being recorded again: this isn't a
            # re-delivery, it's the same entry written twice.
            payload["status"] = str(entry.get("status"))
            payload["answeredAt"] = entry.get("answeredAt")
        entry.update(payload)
        recorded.append(entry)

    # Only the latest compilation of the same week stays standing, and the
    # replacement touches ONLY the suppliers just re-delivered: the earlier
    # question was about a compilation that's now superseded (even if the
    # user had answered "not yet arrived" to it). A supplier absent from the
    # plan is never removed: every history entry is a document that was
    # actually delivered, and removing one because a later compilation
    # didn't include that supplier would drop a real, already-sent order
    # while leaving its merchandise to be reordered.
    registrati = {str(item.get("orderId") or "") for item in recorded}
    superflue = [
        entry for entry in orders
        if isinstance(entry, dict)
        and _entry_run_key(entry) == run_key
        and str(entry.get("orderId") or "") not in registrati
        and _is_replaceable(entry)
        and str(entry.get("supplier") or "").strip().casefold() in consegnati
    ]
    for entry in superflue:
        orders.remove(entry)
    return recorded


def pending_entries(
    history: dict[str, Any],
    *,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Orders still pending, excluding the currently open run.

    Without this exclusion, right after compiling, the user would be asked
    whether merchandise ordered thirty seconds ago has arrived, and every
    item just ordered would show up flagged as "already ordered".
    """

    current = str(exclude_run_id or "").strip()
    entries = [
        entry for entry in history.get("orders") or []
        if isinstance(entry, dict)
        and entry.get("status") == STATUS_PENDING
        and not (current and str(entry.get("runId") or "") == current)
    ]
    entries.sort(key=lambda entry: str(entry.get("createdAt") or ""))
    return entries


def remove_compilation(history: dict[str, Any], order_key: str) -> list[dict[str, Any]]:
    """Remove every entry created by a deleted compilation.

    New entries are keyed as `<folder>:<supplier>`. The full prefix is
    compared, trailing colon included, so deleting `..._2` doesn't also
    remove `..._20`. Received, closed, expired and still-pending entries are
    all removed together: a discarded compilation must leave no reminder or
    commercial trace in the deliveries panel.
    """

    key = str(order_key or "").strip()
    if not key:
        return []
    orders = history.get("orders")
    if not isinstance(orders, list):
        history["orders"] = []
        return []
    prefix = f"{key}:"
    removed = [
        entry for entry in orders
        if isinstance(entry, dict) and str(entry.get("orderId") or "").startswith(prefix)
    ]
    if removed:
        history["orders"] = [entry for entry in orders if entry not in removed]
    return removed


def ask_again_at(entry: dict[str, Any]) -> str:
    """When the question returns after "not yet arrived"; "" if due now.

    Decided by the service, not the browser, like every other rule: the
    browser only compares it against the clock. Not stored on disk: it's
    recomputed from `answeredAt` on every request, so it isn't a value that
    can be hand-edited inside `orders.json`.
    """

    answered = parse_moment(entry.get("answeredAt"))
    if answered is None:
        return ""
    # Starting a new comparison run reinstates the question immediately, and
    # that signal overrides the seven-day delay: opening a new week is a
    # stronger signal than a timer, and it's the point where it makes sense
    # to actually ask whether last week's merchandise arrived.
    # Compared against `answeredAt`, not the clock: an answer given AFTER
    # the reopening restores the delay, or the question would return forever
    # on every reload.
    riaperta = parse_moment(entry.get("reaskedAt"))
    if riaperta is not None and riaperta >= answered:
        return ""
    return to_iso(answered + timedelta(days=REASK_DAYS))


def riapri_le_domande(history: dict[str, Any]) -> int:
    """Clear the delay on every still-pending "has it arrived?" question.

    Doesn't touch `answeredAt`: that's the record of what was answered and
    when, and it's what lets the system answer "what did I decide before".
    The delay is a consequence of that date, not part of it, so it's cleared
    with a separate flag.

    Returns how many questions were reinstated, so callers can avoid
    reporting a count when there was nothing to reinstate.
    """

    adesso = to_iso()
    quante = 0
    for entry in pending_entries(history):
        if not entry.get("answeredAt"):
            # Never answered: the question is already due, nothing to clear.
            continue
        entry["reaskedAt"] = adesso
        quante += 1
    return quante


def pending_summary(
    history: dict[str, Any],
    *,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Compact list for the question at the top of page 2."""

    return [
        {
            "orderId": str(entry.get("orderId") or ""),
            "supplier": str(entry.get("supplier") or ""),
            "supplierName": str(entry.get("supplierName") or _default_supplier_name(str(entry.get("supplier") or ""))),
            "createdAt": str(entry.get("createdAt") or ""),
            # Keeps the same question from being re-asked on every page
            # reload after a "not yet arrived" answer.
            "answeredAt": entry.get("answeredAt") or None,
            # ...and this says when to ask again: a week later, since the
            # merchandise may arrive in the meantime and nothing else would
            # report it.
            "askAgainAt": ask_again_at(entry),
            "lineCount": len(entry.get("lines") or []),
            "totalNet": round(_number(entry.get("totalNet")) or 0.0, 2),
        }
        for entry in pending_entries(history, exclude_run_id=exclude_run_id)
    ]


def answer_order(
    history: dict[str, Any],
    order_id: str,
    received: bool,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Single answer for the whole order: partial deliveries don't exist.

    "Yes" closes the order; "No" leaves it pending and records the answer, so
    the question goes quiet for a week and then returns: as long as the
    merchandise hasn't arrived the question still makes sense, and dropping
    it would amount to claiming it had.
    """

    target = str(order_id or "").strip()
    if not target:
        return None
    for entry in history.get("orders") or []:
        if not isinstance(entry, dict) or str(entry.get("orderId") or "") != target:
            continue
        entry["answeredAt"] = to_iso(now)
        if received:
            entry["status"] = STATUS_RECEIVED
        elif entry.get("status") == STATUS_EXPIRED:
            # A "not yet arrived" answer on an order that's already expired
            # doesn't revive it: it would go back to pending, re-expire on
            # the next read with today's `expiredAt`, and the "expired
            # orders" notice would keep coming back forever. The answer is
            # recorded, the status stays what it was.
            pass
        else:
            entry["status"] = STATUS_PENDING
        return entry
    return None


def close_order(
    history: dict[str, Any],
    order_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Close the question as "will never arrive", without claiming it arrived.

    Needed because "not yet arrived" keeps asking every week: an order the
    supplier will never deliver needs a way to exit, and the only
    alternative would be marking it received, which would be false.
    """

    target = str(order_id or "").strip()
    if not target:
        return None
    for entry in history.get("orders") or []:
        if not isinstance(entry, dict) or str(entry.get("orderId") or "") != target:
            continue
        entry["answeredAt"] = to_iso(now)
        entry["status"] = STATUS_CLOSED
        return entry
    return None


def pending_by_identity(
    history: dict[str, Any],
    *,
    exclude_run_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Index identity -> pending orders, with the item's total quantity.

    Identity is the one from `match_key`: the EAN when there is one,
    otherwise the product's stable identifier (displays).
    """

    index: dict[str, list[dict[str, Any]]] = {}
    for entry in pending_entries(history, exclude_run_id=exclude_run_id):
        quantities: dict[str, float] = {}
        units: dict[str, str] = {}
        for line in entry.get("lines") or []:
            if not isinstance(line, dict):
                continue
            key = match_key(line.get("ean"), line.get("productId"))
            if not key:
                # Neither an EAN nor a stable identity: never matches
                # anything, since a wrong notice is worse than a missing one.
                continue
            quantities[key] = quantities.get(key, 0.0) + (_number(line.get("quantity")) or 0.0)
            units.setdefault(key, str(line.get("unit") or "colli"))
        for key, quantity in quantities.items():
            index.setdefault(key, []).append({
                "orderId": str(entry.get("orderId") or ""),
                "supplier": str(entry.get("supplier") or ""),
                "supplierName": str(entry.get("supplierName") or _default_supplier_name(str(entry.get("supplier") or ""))),
                "orderedAt": str(entry.get("createdAt") or ""),
                "quantity": _quantity(quantity),
                # Unit from when it was ordered, not from this week.
                "unit": units.get(key, "colli"),
            })
    return index


def attach_pending_orders(
    products: Iterable[dict[str, Any]],
    history: dict[str, Any],
    *,
    exclude_run_id: str | None = None,
) -> None:
    """Add "pendingOrders" to every product, matched by identity.

    An EAN can be shared by several products in the comparison — a supplier
    can repeat the same barcode across many rows. Attributing the ordered
    quantity to EVERY product carrying that code would show "already
    ordered" on items nobody actually ordered. The history can't tell apart
    rows that share the code, so when this happens the entry declares it
    (`sharedWith`) instead of inventing an attribution, and the page must say
    so alongside the number.
    """

    elenco = [product for product in products or [] if isinstance(product, dict)]
    condivisi: dict[str, int] = {}
    for product in elenco:
        key = match_key(product.get("ean"), product.get("id"))
        if key:
            condivisi[key] = condivisi.get(key, 0) + 1
    index = pending_by_identity(history, exclude_run_id=exclude_run_id)
    for product in elenco:
        key = match_key(product.get("ean"), product.get("id"))
        matches = index.get(key) or [] if key else []
        quanti = condivisi.get(key, 1)
        product["pendingOrders"] = [
            {**item, **({"sharedWith": quanti} if quanti > 1 else {})}
            for item in matches
        ]
