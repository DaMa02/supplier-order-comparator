# Promotions: integration contract

## Purpose

The engine in `scripts/promotions.py` turns price-list annotations into structured rules. It never modifies the original files, never changes which supplier is chosen, and never subtracts a reward's value from the total.

It handles four cases:

1. `sconto_numerico`: an explicit, computable percentage;
2. `soglia_omaggio`: buying a minimum quantity earns a reward;
3. `confezione_promozionale`: extra content already included in the pack;
4. `offerta_ambigua`: promotional text that needs confirmation.

## A promotion's contract

```json
{
  "id": "promo:noce:...",
  "supplier": "noce",
  "source_reference": "csv:1453-1460",
  "source_text": "ACQUISTA 5 CT IN OMAGGIO 1 CT (12 PEZZI)...",
  "kind": "soglia_omaggio",
  "threshold": {"qty": 5, "unit": "cartoni"},
  "eligible": {
    "products": ["product:1", "product:2"],
    "eans": [],
    "source_rows": [1453, 1454],
    "group": "chiary-intimo",
    "mix_allowed": true
  },
  "reward": {
    "kind": "prodotto",
    "description": "CHIARY INTIMO GEL MINI ML.50",
    "qty": 1,
    "unit": "cartoni",
    "pieces_per_unit": 12,
    "ean": null,
    "supplier_code": null
  },
  "repeatable": true,
  "certainty": "alta",
  "confirmed": true,
  "economic_effect": {
    "type": "informational_reward",
    "discount_rate": null,
    "deterministic": true,
    "active": true,
    "affects_total": false,
    "affects_supplier_choice": false,
    "base_price_field": "unitPricePreDiscount",
    "already_applied": false
  }
}
```

`eligible` must carry at least one concrete reference: product ids, EANs, price-list rows, or a group that exists among the products or offers. Without that link the rule stays `da_verificare` and is never applied.

## Where a supplier writes its conditions: the adapter registry says

The bridge asks the adapter registry "which suppliers declare where they keep their commercial conditions?", and the answer is in `references/adapters.json`.

An adapter that wants to be read adds a `commercial_conditions` entry:

```json
"commercial_conditions": {
  "note": "why this is here, and which price list it was measured on",
  "layout": "blocchi",
  "sheet": "FIRST",
  "data_start_row": 1,
  "max_block_rows": 500,
  "fields": {
    "text": "description",
    "reward": "reward_description",
    "ean": "ean",
    "row_code": "discount"
  }
}
```

- `layout` — the shape the supplier writes in. Two values:
  - `blocchi`: a condition spans several rows — the heading with the quantity to buy, then the eligible merchandise, then the reward row. This is how LARICE writes, and the only shape measured on a real price list so far. It requires all four columns: without the reward's name the threshold is still reconstructed, but comes out `da_verificare`, and the user loses the reward in a different way. `reward` can name the same column as `text`, when the supplier writes the reward's name inside the row itself — as the new LARICE canvass does ("IN OMAGGIO 1CT SH. A/ERBAR. 250ML LAVANDA", all in column E). In that case the column is read once, not twice — reading it twice made the reward's name appear twice in the sentence the user reads.
  - `riga`: a single row carries its whole condition, in one column. It requires only the `text` column. This would be the shape used by BETULLA ("LINDA SETA … 11+1 Gratis Pz", inside the description) and by NOCE (the `descrizione_offerta` column, currently empty for every row).
  A word the engine doesn't recognize stops the read and says so: a badly learned adapter must not read "something anyway".
- `fields` — which adapter column plays `text`, `reward`, `ean`, `row_code`. These are column names, not letters: the position is looked up in `column_map` (which declares columns by letter, as LARICE does) and then in `header_signature.columns` (which declares them by header name, as BETULLA and NOCE do). It is written in one place on purpose: when the user confirms a schema change, the registry rewrites those, and the commercial conditions follow the prices instead of lagging a week behind. A column the registry doesn't declare is never guessed: the reader stops and names the missing one.
- `sheet` — the sheet name, or `FIRST` for the first one. The comparison is the registry's own: punctuation, spaces and case don't matter.
- `data_start_row` — the row data starts on (default 1).
- `max_block_rows` — only for `blocchi`: past this distance from the heading, the block is abandoned and the condition is left `da_verificare`. It's a defense against a malformed price list, not a commercial rule (default 500).
- `row_markers` on the adapter applies in both shapes: a row whose code is declared `orderable: false` never reaches the threshold and never carries a condition of its own. A supplier whose reward row carries no marker of its own declares it through `row_markers.reward_rows`: it is then recognized from its text, with the same judgment applied when closing a block (`promotions.looks_like_reward`). The new LARICE canvass needs this: its marker column reads `PROMO` on every promoted item, reward rows included, and without `reward_rows` the reward — which does carry a price there — would become that product's cheapest offer.

The document's format is decided by its first bytes, not its extension: both `.xlsx` and Excel 97-2003 (`.xls`, NOCE's format) are read.

A supplier that doesn't declare `commercial_conditions` is never read, and produces no warning. That's the difference between "has no commercial conditions" and "I can't read them": a warning that fires on every recompute and asks for nothing gets ignored.

### Who declares it today, and why only them

Measured cell by cell on the week's four real price lists:

| Supplier | Document | Texts with a promotional word | Real conditions |
|---|---|---|---|
| LARICE | `33-34.1 07-21 ago.xlsx` | 28 in column G | 13 threshold headings + 13 reward rows |
| BETULLA | `LISTINO BETULLA … 01-09-26 (1).xlsx` | 12 in the description (D) | 3 `N+M` packs already in the price; 7 of the 12 are the word "Ogni" in "Ogni Superficie" |
| CIPRESSO | `Listino3_34.xlsx` | 1 in the description (B) | none — it's a product name that happens to contain "offerta" |
| NOCE | `formattato_104233.xls` | 5 `N+M GRATIS` in the description (D), 1 of them FOOD | none — `descrizione_offerta` (P) is empty on all 18,074 rows, and `offerta` (J) reads `NO` on all 17,148 filled ones |

LARICE's documents now come in two shapes, and `commercial_conditions` is declared on both: `larice_v1` for the header-less canvass (text in G, reward in J) and `larice_canvass_v1` for the new one (text and reward both in E). On the new one the measurement is 6 thresholds out of 6. Which declaration applies to the document at hand is decided by the identifier that recognized it, not by the file extension: two schemas from the same supplier can share the same format.

For this reason `commercial_conditions` is declared only for LARICE. Turning on BETULLA's description column would surface twelve conditions, nine of which aren't conditions at all — inventing offers for a supplier that has none is worse than the gap it would fix. The day BETULLA writes a real one, or NOCE starts filling in `descrizione_offerta`, an entry is added to the registry — the code itself doesn't change.

## Detection per source

### BETULLA

Call `detect_promotions(..., included_in_product=True)` on the description. For example `Sheet1!D3230`, "11+1 Gratis", produces a promotional pack. The price stays the price-list price: the extra unit is informational only.

An `N+M` pair becomes a promotional pack only if no unit of measure governs it. "ELIDERMA Bagnodoccia 500+100 Omaggio=600 Ml" is milliliters and "CUKO ALLUMINIO MT.16+4 GRATIS" is meters: text like this falls through to `detect_ambiguous_offer` and stays `da_verificare`. The size of the numbers doesn't tell the two cases apart — "8+2" is a correct count on razors and a wrong measurement on aluminum foil — so the discriminant is the unit attached to the pair, before it (`MT.16+4`) or after it (`500+100 Omaggio=600 Ml`).

### Larice

The bridge reads the conditions on its own, following the `commercial_conditions` the registry declares (see above): nothing is hard-coded, and calling the detector directly is only for building a rule by hand.

For a block promotion, join the heading text and the reward-row text, and supply the eligible product rows. For example:

```python
detect_threshold_gift(
    supplier="larice",
    source_reference="Canvass!G135:J142",
    source_text="ACQUISTANDO 2 CT TRA; IN OMAGGIO 1 CT DI BIOPUNTO ...",
    eligible={"source_rows": [136, 137, 138, 139, 140, 141]},
)
```

Numeric discounts in column `P` are deterministic. If the net price was already computed during normalization, set `already_applied=True` to prevent it being applied twice.

### Noce

Group the rows that share the same promotional text in the `availability` field; the group's EANs or rows become `eligible`. The detector also recognizes real-world misspellings such as `ACQUSITA`.

`LOVEHOME 1+1 OMAGGIO` stays deliberately `offerta_ambigua`: the text doesn't say whether merchandise can be mixed, or which item is the reward.

## Computing the state

`calculate_promotion_state(...)` returns:

- `ottenuta`: threshold reached;
- `vicina`: at most one unit short, or at least 80% of the threshold reached;
- `non_raggiunta`: quantity still far off, or zero;
- `da_verificare`: an ambiguous rule, unconfirmed, or not linkable to any product.

`reward_count` says how many times the reward has been earned.

`repeatable` isn't read from the price list: it's a property of the business relationship, set by the user, and applies to every threshold. The detector always sets it to `true`, so `reward_count` is `floor(progress / threshold)`, and the message states how much is left before the next reward rather than claiming the reward was already earned. Inferring it from the word "OGNI" ("every") would have meant reading it off a single price list — "OGNI" never appears in any of the measured Larice price lists.

## Decorating the comparison data

```python
from promotions import decorate_review_data

decorated_review_data = decorate_review_data(
    review_data,
    promotions,
    selections={
        "product:1": {"quantity": 5, "selectedSupplierId": "noce"}
    },
)
```

The function returns a copy and adds:

- `promotions` and `promotionSummary` at the top level;
- `promotions` on every affected product;
- `promotions` on every affected supplier offer;
- `promotionEffectiveUnitPrice` and `promotionEffectiveOrderUnitPrice`, only for a numeric discount that is confirmed, active and not already applied.

The original fields `selectedSupplierId`, `price`, `unitPriceNet` and `orderUnitPriceNet` are never rewritten. Any ranking that wants to consider the promotional price must use the extra field only when `economic_effect.affects_supplier_choice` is `true`.

## Command-line use

```text
python scripts/promotions.py \
  --review-data review_data.json \
  --promotions promotions.json \
  --output review_data_decorata.json
```

This step can run either right after the comparison data is built, or when the user's saved state is read. It requires no changes to the source price lists.
