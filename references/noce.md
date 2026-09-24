# Noce: read their file, send back their file

Noce is a supplier like any other: it sends a document, the document is uploaded, compared, and in the end it gets its own document back with the order filled in. Its B2B site plays no part in this: the scraper, the credentials, and preparing the order on the site have all been removed.

## Reading

The price list arrives as an `.xls` (Excel 97-2003, not `.xlsx`) and is uploaded from step 1 of the page like any other price list. It's read by `app/xls_reader.py` with the `noce_xls_v1` adapter from the registry (`references/adapters.json`), which declares the sheet, the header row, the data row, the EAN column — by name, not by letter — and the order column.

FOOD rows are discarded on read: the order only covers non-food items.

## Delivery

Noce gets back its own `.xls`, with only the order column filled in and everything else identical byte for byte. It doesn't go through the Node writer, which imports and exports `.xlsx` and would rebuild the file from scratch; instead `app/xls_writer.py` changes four bytes per cell inside a copy.

Two checks run before writing, at no extra cost since the file has to be read anyway:

1. the row's EAN must match the one in the plan — otherwise the quantity would be written onto another item's row;
2. the order column must still be entirely fixed-length (RK) cells — a single formula or a differently typed cell anywhere in the column is enough to make the in-place patch inapplicable, so the check is repeated on every file.

`RECALCID` is reset to zero: changing a cell's value doesn't mark the formulas that reference it as dirty, and without this reset the quantities would be correct while the totals stayed at their old values until Excel was told to recalculate.

If the patch can't be applied, the Noce order fails and says so: it doesn't fall back to a format the supplier doesn't accept, and it doesn't leave a half-written copy on disk.

Acceptance is checked against the total the file computes on its own, which must equal the plan's total. If it doesn't match, nothing is delivered.
