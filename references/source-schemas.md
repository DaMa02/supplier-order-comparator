# Source schemas

## Index

1. Common rules
2. Management-software export
3. BETULLA
4. Larice
4-bis. Larice — the new canvass
5. Larice displays
6. Cipresso
7. Noce CSV
8. Noce price list (`.xls`)
8-bis. OFFERTE (header-less promotional sheet)
9. Writing the orders

## 1. Common rules

- Treat every input as read-only.
- A file's format is decided by its first bytes, not its extension: `D0 CF 11 E0` is Excel 97-2003 and is read with `app/xls_reader.py`; `PK\x03\x04` is Excel 2007+ and is read with `openpyxl`; everything else goes through the CSV reader. A renamed price list must not produce an unreadable error.
- Read the whole active sheet; never limit the scan to a formatted table's range, a filter, or a preview.
- Only values attached to live cells count. Never look for an EAN directly inside the workbook's internal XML or in `sharedStrings.xml`.
- Keep EANs and item codes as text, including leading zeros and non-standard codes.
- Never deduplicate a price list on the EAN alone.

## 2. Management-software export

Expected signature on sheet `Foglio1`:

- an opening document row with type `T`;
- a header row containing `Codice`, `Descrizione`, `Colli`, `Quantità`, `Prezzo`, `Sconto`, `IVA`, `Totale`;
- product rows with type `C` in column A.

Mapping:

| Field | Column |
| --- | --- |
| EAN, source of truth | B `Codice` |
| Description | D |
| Unit | E |
| Exported cartons | F, reused as the default carton quantity (`suggested_colli` → `product.suggestedQuantity`) |
| Exported quantity | G, ignored for the new order |
| Last net price paid | H `Prezzo` |
| Informational discount | I |
| VAT | J |
| Historical total | K |

In the web comparator the user enters the number of cartons to order directly: there is no rounding and no surplus. Column `Colli` (F) prefills every product's quantity field (`quantitySource: "gestionale"`); editing it switches the product to `quantitySource: "utente"`. Column `Quantità` (G) is never reused for the order. Each offer's line total is `total = cartons * price per carton` (equivalent to `cartons * pieces per carton * price per piece`).

## 3. BETULLA

Expected signature: `EAN`, `CodArt`, `ORDINE`, `Descr.Commerciale`, `PzCt`, `Cessione`, `Pedana`, `Iva`, `TOTALI`.

| Field | Column |
| --- | --- |
| EAN | A |
| Item code | B |
| Order quantity (cartons) | C |
| Description | D |
| Pieces per carton | E |
| Net unit price | F `Cessione` |
| Pallet | G |
| VAT | H |
| Line total | I |

The line-total formula is `ORDINE * PzCt * Cessione`. The last row carries the grand total.

## 4. Larice

The sheet name carries the canvass's number and dates and changes every week. Use the first commercial sheet and validate its column shape; never hard-code the current run's sheet name. The sheet can extend well past a formatted table's range — scan to the worksheet's actual last row.

| Field | Column |
| --- | --- |
| Family / promotion | A, often hidden |
| Indicator | B |
| Item code | C |
| Order quantity (cartons) | D |
| Pieces per carton | E |
| Pallet | F |
| Description | G |
| Previous-price note | M |
| Pre-discount price | O |
| Discount percentage or code | P |
| VAT | Q |
| EAN | R |

The post-discount price is computed deterministically:

```text
if P is numeric: post_price = O * (1 - P)
if P is text:     post_price = O
```

Numeric values are Excel percentages, e.g. `0.10 = 10%`. The known text codes in column P are `TP` (transit price: no discount, an ordinary orderable row) and `SM` (the reward row of a threshold-gift promotion — it carries a real code, EAN and description, but is not purchasable on its own). Any other text code is flagged as anomalous rather than silently treated as "no discount"; a few rows in the reference file also carry the code `**`.

Compare and total using the post-discount price. Keep the pre-discount price and the value of P in the comparison data too, for audit.

## 4-bis. Larice — the new canvass (`larice_canvass_v1`)

Larice also sends a second canvass, `New Larice N°37(v.0)`, headed "Larice Ingrosso Srl", whose layout has nothing in common with §4. §4 stays as it is: the supplier can revert to the old format from one week to the next, and the two adapters coexist under the same `supplier_id`.

The two signatures are deliberately mutually exclusive: §4 requires at least 18 columns (this schema has 16), and this one requires its own header row (§4's sheet has none). Headers sit on row 11, data from row 12; above that is the block with the supplier's data, the customer's data, and the four totals.

| Field | Column | Header |
| --- | --- | --- |
| EAN | B | `CODICE EAN` |
| Item code | C | `CODICE` |
| Order quantity (cartons) | D | `QTA` |
| Description | E | `DESCRIZIONE ARTICOLO` |
| Row marker | F | _none_ (`PROMO`, `NOVITA`) |
| VAT | G | `IVA` |
| Pieces per carton | H | `IMB.` |
| Pre-discount price | I | `LISTINO` |
| Discount percentage | J | `SCONTO` |
| Post-discount price | K | `NETTO` |

The price used for comparison is NETTO (K), read as-is. Unlike §4, nothing is computed here: the supplier has already applied column J's percentage inside K. Declaring `unit_price_pre_discount` on this schema would turn on the branch that recomputes the discount and apply it a second time.

Reward rows are not marked. Column F reads `PROMO` on every promoted item, reward rows included; a reward row such as "IN OMAGGIO 1CT …" is recognized only from its text and must be declared through `row_markers.reward_rows`. Measured on one price list: six reward rows out of 6,537, zero false positives, and five of the six repeat the EAN of an item that is also listed at full price — EAN `8019580330416` appears both at 0.68 as merchandise and at 0.65 as the reward.

Commercial conditions are laid out in blocks as in §4 — a heading with the quantity, the eligible merchandise, the reward row — but all in the same column E, reward name included: `commercial_conditions.fields` declares `description` for both `text` and `reward`. Measured: six thresholds out of six.

Displays: this schema has no parent/component structure (H and I are numeric), so §5 does not apply. If a future canvass introduces one, its columns are declared once that document is available.

## 5. Larice displays

A display is a composed offer, orderable on its parent row. Do not model it as a sequence of independent products.

Typical parent row:

- promotion/display label in A;
- indicator and item code in B/C;
- order quantity in D, pieces per carton in E;
- parent description in G;
- price and discount in O/P;
- parent EAN in R, often absent or not identifying.

Typical component row:

- same label/block in A;
- supplier item code in C absent;
- component quantity in H, description in I;
- component price in O, discount in P;
- component EAN in R, often present.

The absence is specifically of the supplier code, not necessarily of the EAN. Combine name semantics, parent/child structure, block continuity, quantities and prices. Reject negations like `NO ESPO` and lexical false positives like `ESPRESSO`.

Reconciliation:

```text
declared parent quantity = sum of component quantities
gross parent price = sum(component quantity * component gross price)
net display price = gross parent price * (1 - numeric discount)
```

Tolerate small rounding differences and record them in the audit; lower confidence when component quantities or prices are unavailable. Component rows must have `usable = false`; only the parent row carries the order column.

## 6. Cipresso

Cipresso sends two layouts (`cipresso_v1` and `cipresso_con_ordine_v1`), distinguished only by whether column G already holds an `ORDINE` header — never by the sheet name, which normally carries the price list's date and is not a signature.

Expected header row: `COD.ART.`, `DES.ARTICOLO`, `UM`, `QT`, `LISTINO`, `COD.EAN`, `ORDINE`.

| Field | Column |
| --- | --- |
| Item code | A `COD.ART.` |
| Description | B `DES.ARTICOLO` |
| Unit | C `UM` |
| Pieces per carton | D `QT` |
| Net unit price | E `LISTINO` |
| EAN | F `COD.EAN` |
| Order quantity | G `ORDINE` |

`cipresso_v1` has one row of formulas above the header, which sits on row 2 (data from row 3), and no `ORDINE` column of its own: the order column is confirmed by the user and written to G regardless. `cipresso_con_ordine_v1` has the header on row 1 (data from row 2) and already ships an `ORDINE` column at G. Which adapter applies to a given document is decided by matching every candidate against what the document actually contains, not by inspecting the file name — the two schemas can arrive with the same file-name pattern.

The verified price list carries no VAT, discount or availability columns: assume the row is available in the comparison, but never invent a discount. Keep non-standard EANs as text too. Before writing a copy, check the sheet, `G1 = ORDINE`, the original's hash, and the source row.

A Cipresso display can appear on a single orderable row with no child rows. Recognize it only through rules declared in the preflight step (name, code, quantity and price consistent with each other); present it as a complete display and ask for confirmation if there are no EAN+quantity components with which to reconcile its composition.

## 7. Noce CSV

Expected CSV columns:

| Field | CSV column |
| --- | --- |
| Catalog page | `catalog_page` |
| EAN | `ean` |
| Description | `product` |
| Packaging | `packaging` |
| Availability | `availability` |
| Variation | `variation` |
| Unit price | `price` |
| Unit | `unit` |

`packaging` is informational (`Um/Ct/Str`) and must not be automatically converted into pieces per carton. For Noce totals, use only the explicit multiplier in `unit`, e.g. `x 1,0`; if it can't be parsed, ask for confirmation. A row is usable automatically only if it is available, has a valid price, and has a valid order multiplier.

Accept empty or non-standard EANs. Never drop a row just because its EAN is a duplicate.

## 8. Noce price list (`.xls`, `noce_xls_v1`)

Noce also sends the complete price list as an Excel 97-2003 file, under names that say nothing (`formattato_104233.xls`). The signature is the header row, never the file name: `codice_a_barre`, `codice`, `descrizione_articolo`, `pezzi_x_cartone`, `prezzo`, `quantita`, `offerta`, `Importo`, `cat`, `ragione_sociale`. Sheet `Foglio1`, header on row 5, first product on row 6.

| Field | Column |
| --- | --- |
| EAN | B `codice_a_barre` |
| Item code | C `codice`, text, with leading zeros |
| Description | D `descrizione_articolo` |
| Pieces per carton | E `pezzi_x_cartone` |
| Cartons per layer | F, informational |
| Layers per pallet | G, informational |
| Per-piece price | H `prezzo` |
| Order quantity in cartons | I `quantita`, order column |
| Offer | J `offerta` |
| Amount | K, formula `=I*H*E` |
| Department | L |
| Category | M `cat`, `FOOD` or `NO FOOD` |
| Company name | N |
| Changed | O |
| Offer description | P |
| VAT | Q `Iva` |

Rules declared in the adapter:

- Price and quantity. The `Importo` column's formula (`=I*H*E`) says the price is per piece and the order unit is the carton — exactly the two fields the rest of the program already uses (`unit_price_net` and `pieces_per_carton`): no conversion is needed, and none should be invented.
- End of data. Hundreds of rows follow the last product, containing only the `Importo` formula (measured: data through row 17148, then 931 empty rows through row 18079). The boundary is the last row that has at least one of EAN, item code or description — it must not be hard-coded, and must not be taken from `max_row`.
- FOOD rows. The user doesn't handle food items: rows with `cat = FOOD` are excluded from the comparison. How many were removed must be recorded in the audit (`inputs[].reading.rows_excluded`), never dropped silently.
- Expiry date in the description. About a third of the descriptions end with `<br> Scadenza gg/mm/aaaa`. The date is extracted into `expiry_date` and the description is left clean, otherwise the HTML fragment pollutes name comparisons. The date is read but not trusted: outside a plausible window (two years back, ten ahead) or impossible on the calendar, it becomes `expiry_plausible = false` with a warning, not an error.
- Offers. The signal is twofold: the `offerta` column, and the price written in bold ("prezzi offerta sono in grassetto" per a note in D4) — either is enough. The measured file has no active offer, but bold must still be checked: on a price list with offers it would be the only signal.
- Barcodes. Accept empty or non-standard EANs and never deduplicate on EAN alone: the measured file has 55 empty EANs, about 726 shorter than 13 digits, and 46 repeated EANs.

## 8-bis. OFFERTE (header-less promotional sheet, `offerte_v1`)

One price list arrives with no header row at all: above the products there are blank rows and a single cell reading `ORDINE` in column H. With nothing to read by name, the columns are recognized by how they're populated — "there are prices, pieces per carton and barcodes" identifies nothing on its own, since most price lists have those too. What identifies this sheet is three things together: columns A–F populated, G empty, and a single cell in H reading `ORDINE`.

| Field | Column |
| --- | --- |
| Item code | A |
| Description | B |
| Unit | C |
| Pieces per carton | D |
| Net unit price | E |
| EAN | F |
| (empty) | G |
| Order quantity | H, below the `ORDINE` cell |
| Promotion text | K |

The `ORDINE` cell's row moves week to week, so the sheet is searched for that header rather than assuming a fixed row.

## 9. Writing the orders

- Work exclusively on copies.
- BETULLA: clear and rewrite column C.
- Larice: clear and rewrite column D.
- Cipresso: clear and rewrite column G only after verifying the sheet, header and the original's hash.
- Larice display: write the quantity only on the parent row, never on component rows.
- Use the source row persisted in the matching result; never re-run semantic matching while writing.
- Preserve formulas, formatting, sheets, filters and total rows.
- The order is written inside an `.xlsx`. The one exception is Noce, which declares `order_write.mode: patch_xls_in_posizione` and gets back its own `.xls`, changed by four bytes per cell — a path that requires the order column to already be entirely numeric at fixed length. Anyone else's `.xls` is read and compared, but filling it in means saving it as `.xlsx` with Excel: there is no automatic conversion, because the filled-in document goes back to the supplier, and a conversion done in Python would lose formulas, merged cells, drawings and formatting.
