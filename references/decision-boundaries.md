# Boundaries between deterministic automation and AI

## General rule

Determinism isn't maximized in the abstract. A deterministic rule is used when an operation has a correct result that's definable, verifiable and repeatable; AI is used when the data requires linguistic interpretation, product knowledge, or commercial judgment.

## Operating matrix

| Activity | Approach | Reason |
| --- | --- | --- |
| Inventory, paths and storage | Deterministic | Avoids data loss or files ending up outside their folder. |
| Recognizing already-known schemas | Deterministic | Headers and columns are documented in the adapter registry. |
| Scanning active rows | Deterministic | Must cover the whole worksheet without depending on previews or tables. |
| EAN normalization and matching | Deterministic | Identifiers don't require semantic interpretation. |
| Prices, discounts, VAT, pack sizes, totals and minimum-order threshold | Deterministic | These are auditable calculations. |
| Duplicate EAN | Hybrid | The engine keeps every active row and computes the attributes; the AI evaluates the variant, since the same code can be shared by commercially different products. |
| Missing EAN | Hybrid | The shortlist and structured incompatibilities are repeatable; commercial and linguistic equivalence requires judgment. |
| New, unknown supplier format | Deterministic, confirmed by the user | A header-alias score proposes the field mapping from a profile of the file (`app/schema_mapping.py`); the user confirms it, and the adapter then becomes deterministic. |
| Display recognition | Deterministic, with user input on incomplete cases | The engine reconciles structure, quantity and prices; the user resolves cases it can't complete on its own. |
| Display comparison | Deterministic when the composition is complete, otherwise human | An EAN+quantity fingerprint proves equivalence; similar names aren't enough. |
| Order-quantity writing | Deterministic | Uses the approved source row, without redoing the matching. |
| Quantity, supplier choice and thresholds | Local UI + deterministic checks | The user works on a simple page; the backend recomputes and validates the snapshot. |
| Sending the order | Human | Never performed by the tool. |

## Escalation criterion

An AI decision can always end in `RIFIUTATO` (rejected) or `DA_VERIFICARE` (to be checked); it's never forced to pick a product. Incompatible brand, format, quantity, form or variant override textual similarity. Accepted semantic decisions stay visible in the comparison and require confirmation before the order is prepared.
