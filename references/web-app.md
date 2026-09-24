# Local HTML comparator

## Architectural choice

A single-user, local web app:

```text
browser ↔ HTTP on 127.0.0.1 ↔ Python standard-library server ↔ the run's JSON
```

Excel and CSV stay import/export formats, not the working interface. No vendor backend, cloud account, external database, npm or web framework is needed. A run's state is a local JSON file written atomically; SQLite would only be worth adding if the app needed multi-run history, concurrent users, or complex queries.

This keeps operational complexity low: for the user it's three guided pages; the REST boundary keeps the local service replaceable. Streamlit would prototype faster but gives less control over tables; NiceGUI adds dependencies and packaging overhead. For the current, stable flow, plain HTML/CSS/JavaScript with no extra framework is the right call.

The three pages are:

1. separate import of the management-software list and the suppliers' price lists;
2. products to order — quantities, exclusion, offer comparison, and search for additional products;
3. a summary sortable by supplier or product, with subtotals, thresholds, promotions and compilation.

## Contract

- `GET /api/review`: files, suppliers, products/offers, displays and saved state;
- `POST /api/upload`: local base64 copy, read-only profiling and a `PROFILED_AI_PENDING` response;
- `GET /api/schemas/pending`: for a run stopped on `SCHEMA_SCONOSCIUTO`, a preview of just the documents involved, the existing suppliers, and a column proposal derived from the headers;
- `POST /api/schemas/validate`: tries the chosen columns with the real parser, without saving them or triggering a recompute; returns counts and a normalized sample;
- `POST /api/schemas/confirm`: repeats the trial, saves the confirmation atomically, and starts the recompute; responds `202` with the new pipeline state;
- `PUT /api/state`: autosave of quantities, exclusions, supplier choice, confirmations, page, grouping and threshold consent; also returns the updated promotion state;
- `GET /api/products/search?q=<text>`: conservative search across the current catalogs;
- `POST /api/products/add`: local addition of a product picked from the catalogs;
- `POST /api/compile`: final validation and generation of the plan/configured copies;
- `GET /api/history/pending`: compiled orders not yet marked received;
- `POST /api/history/answer`: a single answer for the whole order — `received: true` closes it as received, `received: false` postpones the question by seven days, `closed: true` closes it as "won't arrive" without marking it received;
- `POST /api/matches/answer`: accepts or rejects the price-list row proposed for a product; the decision is bound to the run, the supplier, and the row's fingerprint;
- `POST /api/uploads/elimina`: deletes a single uploaded copy and its profile, only if the name appears among the server's deletable uploads;
- `GET /api/ordini`: past compilations, most recent first, derived by scanning the dated folders;
- `POST /api/ordini/elimina`: deletes a compilation the server lists, and in the same operation every reminder that shares its stable key;
- `GET /ordini/<folder>/<name>`: downloads a document from that compilation;
- `GET /ordini/<folder>/zip`: just that compilation's price lists, as an archive built in memory on the fly;
- `GET /outputs/<name>`: legacy route, kept to serve files earlier runs left on disk; current runs don't write there. A supplier's deliverables live in the compilation's own dated folder instead.

## History of orders not yet received

Every successful compilation records one entry per supplier in `app/data/history/orders.json`, outside the run's own folder so it survives the weekly recompute. The path can be set with `--history`; by default it's derived from `<state folder>/../history/orders.json`.

Rules:

- a new entry's key is `<compilation-folder>:<supplier>`: two compilations are two distinct documents; a re-delivery from the same supplier replaces the still-open question from the same run, instead of duplicating it;
- only a supplier the writer actually left a faithful copy on disk for enters the history. A compilation with no copies is not an order. A supplier omitted from a later compilation doesn't erase the price list already produced earlier, which may already have been sent;
- the run currently open never appears among the pending orders — otherwise it would ask about merchandise ordered moments earlier;
- matching uses the EAN; without one, only a stable identifier built from the display's composition is accepted. The identifiers `product:<row>` and `display:unmatched:` are not stable across weeks;
- when several products share an EAN, the warning states the ambiguity rather than attributing the full ordered quantity to each of them;
- an entry older than 60 days since its last interaction, or with an unreadable date, expires. The expiry is shown on the page for 30 days — it doesn't disappear silently;
- if recording the entry fails, the compilation stays valid, but the warning must appear in the final message: a reminder no one is told about serves no purpose.

Partial deliveries don't exist. The three answers cover the whole order: "received", "not yet", and "won't arrive".

Minimal snapshot:

```json
{
  "runId": "run-id",
  "currentStep": 3,
  "acceptBelowThreshold": false,
  "summaryGrouping": "supplier",
  "products": [
    {"id": "product:12", "quantity": 10, "selectedSupplierId": "larice", "confirmed": true, "excluded": false}
  ]
}
```

The backend recomputes prices and totals from the run's own data: it never trusts a price or total sent by the browser.

## Quick decisions in the interface

On the comparison page, the product name from the management software stays distinct from the description on the row a supplier proposed. A dismissed but plausible proposal shows only the question "Same product?" with "Yes" and "No"; score, method and rationale sit in the collapsed "Why does this show up?" detail. A "Yes" makes the offer usable in the current comparison; a "No" hides it. The decision doesn't survive a changed price-list row.

While scrolling, a thin bar keeps the running totals per supplier visible. Cards show price per carton, price per piece, pieces per carton and total, without restating the cartons arithmetic. In the summary, the EAN shown is the selected offer's, and the `−`, `+`, `×` controls change the carton count or zero the product out.

All of these edits go through the existing autosave: the snapshot is written atomically about 450 ms after the edit. When the page shows "Tutto salvato" ("all saved"), quantities, supplier choice, confirmations, exclusions and page all survive reopening. The delay is not tuned up or down.

Past compilations and secondary details start collapsed. Deletions require confirmation on the individual row. Removing an uploaded price list doesn't change the live comparison — that only changes after a new recompute completes successfully.

`quantity` always means the number of cartons: no rounding, no surplus. The backend keeps the delivered pieces (`cartons × pieces per carton`) as informational data. For a display, it means the number of complete displays. The snapshot also carries `quantitySource` (`gestionale` or `utente`), distinguishing a quantity prefilled from the `Colli` column from one the user chose.

## Startup

For the current dataset, use `AVVIA_COMPARATORE.cmd`. The launcher looks first for the app's own portable Python runtime, then for `py`/`python`, opens the browser, and keeps data under `app/data/`.

When it also finds Node 18+, it builds `app/data/current/writer_config.json` from the verified `sourcePath`s of the BETULLA/Larice files and enables `.xlsx` copies. If a requirement is missing, the launcher states which one and still leaves the JSON plan available — it never creates a partial configuration.

`.xlsx` copies are written by `scripts/lib/xlsx_in_posizione.mjs` with no library at all: it opens the price list's ZIP container with `node:zlib`, changes only the order column's cells inside the sheet's XML, and closes it again by copying every other part byte for byte. Nothing is reconstructed from scratch — a library that imports a whole workbook and rewrites it can silently drop or corrupt content it doesn't fully understand, which is why the cell-by-cell comparison below exists.

Every copy produced is reopened and compared cell by cell against its source price list (`app/copia_fedele.py`) before delivery: the only differences allowed are in the order column — the plan's quantities, and clearing whatever quantity was there before. The comparison loads both workbooks in full, never `read_only` — a conservative `<dimension>` element or out-of-order rows are legitimate and must not make a correct copy look wrong — and also checks the number format wherever the copy shows a value: a price formatted as `0`, or a quantity hidden behind `;;;`, changes what the supplier reads without changing what's stored. A copy that fails the comparison is discarded, and the reason is added to the compilation's warnings. Deliberately not checked: styles and colors, column widths, images, autofilters, document properties, and cached formula results (formulas themselves are compared as text).

To run a different dataset, start `app/server.py` directly with `--review`, `--state`, `--uploads` and `--output-dir`. The host must stay loopback — never expose the port on the LAN.

The `?demo=1` query loads an in-memory demo only. In normal mode, a missing backend must produce an explicit error — no hidden demo fallback.

## Upload and recompute cycle

Uploading a file doesn't trigger blind automatic parsing. After the upload:

1. the server saves a unique copy and computes its profile — file format, candidate sheets, and a `deterministic_hint` that matches it against the adapter registry (`SCHEMA_NOTO`, `SCHEMA_VARIATO`, `NUOVO_FORNITORE`, or unrecognized);
2. the response lists what was read, any warnings about non-standard reads, and which supplier each file was recognized as;
3. the user reviews the uploaded files and presses "Confronta i listini" ("compare the price lists");
4. that recompute runs the pipeline's own phases — deterministic parsing where the schema is known, targeted AI matching only for items an EAN can't resolve — and rebuilds `review_data.json`.

A run stopped on an unrecognized schema opens the guided column-mapping flow (`references/schema-routing.md`) instead of proceeding blind; the new schema is learned and reused from then on.

## Compilation constraints

Compilation is blocked when the order is empty, a quantity isn't a whole number, the offer is unavailable, a confirmation is missing, or a total below the minimum-order threshold hasn't been explicitly accepted. Changing a quantity or supplier resets the below-threshold consent.

Displays show price per display, price per piece, declared quantity, confidence and an expandable composition. Component rows can't be selected as standalone products.

Confirmed numeric discounts are already reflected in the imported price, or computed into a separate field. Free goods, samples, promotional packs and ambiguous conditions are shown in the offers and in the summary, but never automatically change a total or the chosen supplier.

## Security and output

- listen only on `127.0.0.1`, `localhost` or `::1`;
- accept only `.xlsx`, `.xls` and `.csv` within the configured size limits (`.xls` is the format the Noce price list arrives in);
- sanitize names and paths; never overwrite an upload or an original file;
- apply a CSP and anti-framing headers;
- write state atomically;
- always create `final_order_plan.json` before the `.xlsx` copies, inside that compilation's dated folder (`<data>/ordini/2026-08-12_1435/`): a compilation never overwrites the previous one's ready price lists, and the list of compilations is derived by scanning those folders, not from a separate index that could drift out of sync;
- gate every download path with an allow-list: the requested name must appear, unchanged, in a scan of the folder, and the resolved path must stay inside the orders root;
- show and keep only copies/plans for suppliers with at least one ordered row;
- strip the spreadsheet engine's diagnostic sidecar files from the delivered folder;
- never send orders.
