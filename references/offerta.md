# An offer: the fields, who writes them, who reads them

## Purpose

An offer is the row through which a supplier states the price it brings for an item. It lives inside `review_data.json`, under `products[].offers[]`, and is written by `display_offer` in `scripts/build_review_data.py`.

This document exists because an offer's fields are read in more than thirty places across the service, in the form `x.get("camelCase") or x.get("snake_case")`, and every alias tolerated beyond what's needed is one more name someone will write in place of the right one tomorrow. The names below are the closed list: none are added beyond it.

## Who answers questions about an offer

`app/offerta.py`, and no one else. Three questions, three functions:

| Question | Function |
|---|---|
| Whose offer is this | `offer_supplier_id(offer)` |
| Can it be ordered | `offer_is_available(offer)` |
| What does it cost per piece and per carton | `offer_pricing(offer)` |

Plus `find_offer(product, supplier)`, which returns a supplier's offer on a product — the first one, if the price list repeats it.

None of the three is ever rewritten by hand, not even for a seven-character line such as `offer.get("available") is not False`. A hand-written copy of one of these checks — for instance inside `pipeline_jobs._ripulisci_stato`, the function that decides which quantities to zero out after a recompute — inevitably drifts from this module the day one of the two changes and the other doesn't. The symptom is a quantity that stays on an offer the compilation step then rejects: it surfaces a week later, somewhere else.

## The fields

| Canonical field | Tolerated aliases | What it is |
|---|---|---|
| `supplierId` | `supplier_id` | the supplier making the offer |
| `available` | — | `false` = not orderable. Absent means available: older price lists never write it |
| `quantityFactor` | `unitsPerOrderUnit`, `units_per_order_unit` | how many pieces are in the order unit. For a display, this is the pieces it contains — not 1 |
| `unitPriceNet` | `pricePerPiece`, `price_per_piece` | net price per piece — the number different suppliers are compared on |
| `orderUnitPriceNet` | `price`, `netPrice` | net price of the carton (or of the whole display) |
| `sourceRow` | — | the price-list row the offer comes from, and the one the quantity will be written to |
| `matchStatus` | — | how the match was found, in Italian and meant for the user |
| `requiresConfirmation` | `requires_confirmation` | the user must confirm by hand before it can be ordered |

One of the two prices is enough: `offer_pricing` derives the other by multiplying or dividing by `quantityFactor`, which defaults to 1 when absent. If neither price is present, or the carton price is negative, the offer has no usable price and `offer_pricing` returns `None`.

An offer also carries other fields — `supplierName`, `supplierCode`, `status`, `method`, `confidence`, `rationale`, `unitPricePreDiscount`, `declaredUnits`, `components`, `compositionStatus` — that explain it on the page and don't decide anything. The true, complete list is whatever `display_offer` writes: if this document and that function disagree, the function is right, and this document needs fixing.

## Who still reads `available` on its own, and why

Two reads don't go through `offer_is_available`, and that's not an oversight: they answer a different question — "which offer to preselect" — not "can it be ordered".

- `scripts/build_review_data.py`, `build_products` and its twin for displays, `build_display_products`: `[offer for offer in offers if offer.get("available")]`. This is truthy, not `is not False`, and the two rules diverge on `available: 0` and `available: ""` — the producer would preselect "not available" where `offer_is_available` says "available". This doesn't bite today for a specific reason: `available` is written only by these two producers, and only as a boolean. Whoever one day writes a number there needs to know the two answers then part ways.
- `app/static/app.js`: `offer.available !== false` to rebuild the offer on the page, then `offer.available` truthy to filter it. Same divergence, same reason it doesn't bite today. The page can't import `app/offerta.py`: if the rule grows, it has to grow here too, by hand, and be written down here.

The test that guards against hand-written copies (`tests/test_offerta.py`, `test_nessun_altro_file_riscrive_la_regola_a_mano`) checks only Python, and only the two forms `is False` / `is not False`. It doesn't see the truthy checks or the page — this document is what keeps those two lines honest.

## The two known traps

A legitimate `0` is mistaken for an absent field. `offer_pricing`'s `or` chains discard zero along with `None`. This doesn't bite today for a reason that lives elsewhere: both producers of offers already discard prices that aren't `> 0` before they get here — `app/catalog_search.py`, `_offer`, and `scripts/build_review_data.py`, `display_offer`, both guarding against the same failure ("a price read as 0.00 wins the comparison, because the lowest one goes first"). The guard is upstream, not at the point that reads the value: whoever removes either of those two filters needs to know that.

`available` absent means available, and that's not an accident. It's the only field whose missing value doesn't mean "unknown" but "yes": price lists read before the field existed never wrote it, and treating them as not-orderable would empty out an old comparison.

## Where to look when something doesn't add up

- who produces the fields: `scripts/build_review_data.py`, `display_offer`;
- who decides that a price-list row becomes an offer: `app/catalog_search.py`, `_offer`;
- who reads them for the comparison and for compiling the order: `app/server.py`;
- who reads them to zero out quantities after a recompute: `app/pipeline_jobs.py`, `_ripulisci_stato`.
