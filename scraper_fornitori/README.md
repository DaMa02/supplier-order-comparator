# Supplier scraper skeleton

This folder contains no working scraper, and isn't wired into the comparator:
it's the generic half of the job, kept aside for the day a supplier stops
sending its price list and the only way left to get prices is its website.

Today every supplier sends a file — `.xlsx`, `.xls` or `.csv` — and the file
is loaded from step 1 of the page. As long as that holds, nothing here runs.

## What's here, and what isn't

| File | What it is |
|---|---|
| `scraper_fornitore.py` | the engine: an HTTP session with cookies, retries, resuming after an interruption, incremental CSV writing, `metadata.json`. Not touched when adding a supplier |
| `modello_fornitore.py` | the template to copy: the three things that depend on the site |
| `valida_catalogo.py` | looks at the downloaded file and decides whether it's usable |
| `controllo_a_campione.py` | re-fetches a few items from the site and compares them against the CSV |

The three things that depend on the site, and that nobody can write for you:

1. how login works — which form, which fields, and above all what proves
   login succeeded;
2. how many pages there are — read from the site on every run, never a
   hand-written number;
3. how a page is read — which endpoint returns the rows, and which cell
   is which column.

## Adding a supplier

```bash
cd scraper_fornitori
cp modello_fornitore.py scraper_nomefornitore.py
```

Before writing a line of code, open the site in a browser and see how it
actually works:

1. reach the product listing;
2. open dev tools, Network tab, and change pages in the listing. Usually the
   page doesn't reload fully: it calls an endpoint that returns just the
   rows. That is the endpoint to call — driving a browser through two
   hundred pages is slow, fragile and unnecessary;
3. check what's sent to the login, hidden fields included;
4. check what changes in the row endpoint's address when the page changes:
   usually a start and end number, not a page number.

Then fill in the three methods and try on a small scale:

```bash
python scraper_nomefornitore.py --output ./output-prova --max-pagine 2 --reset
```

Once the two test pages are correct, run the full extraction:

```bash
python scraper_nomefornitore.py --output ./output --reset
python scraper_nomefornitore.py --output ./output --riprendi   # after an interruption
```

`--riprendi` resumes from the page after the last completed one, tracked in
`ripresa.json`. `--reset` only clears the files inside the folder passed to
`--output`.

## The checks aren't optional

A scraper that finishes without errors doesn't mean the catalog is good: the
session can expire halfway through, a page can come back empty, the price
list can change while it's being downloaded. Before using the file:

```bash
python valida_catalogo.py \
  ./output/catalogo_nomefornitore.csv \
  ./output/catalogo_pulito.csv \
  ./output/rapporto_qualita.json \
  --metadati ./output/metadata.json

python controllo_a_campione.py ...   # called from your own scraper, see below
```

`valida_catalogo.py` looks at the file: missing pages, rows with no
description or no readable price, duplicates, and the gap between the counts
the site declared before and after. A gap of up to 20 items passes with a
warning — the catalog may have changed during extraction — 21 or more
blocks.

`controllo_a_campione.py` looks at the site: it re-fetches five items spread
across the catalog and expects every one to come back identical. It needs
the site object, so it's called from your own scraper:

```python
from controllo_a_campione import avvia
from scraper_nomefornitore import SitoNomeFornitore

raise SystemExit(avvia(SitoNomeFornitore()))
```

## Credentials

They don't live in the code, in command-line arguments, in a file inside the
repository, or in any output. The skeleton reads them from two environment
variables — `<NOME>_UTENTE` and `<NOME>_PASSWORD`, where `<NOME>` is the
site's `nome` field, uppercased — or prompts for them, with the password
hidden.

The command line ends up in shell history, in Task Scheduler logs, and in
the process list: that's why the password is never passed as an argument.

## If this is ever wired into the comparator

That's a decision to make before writing the code, not after.

The program accepts `.xlsx`, `.xls` and `.csv` from step 1 of the page, and
for CSVs it has a generic reader driven by the adapter registry
(`read_mapped_csv_supplier` in `scripts/prepare_manifest_sources.py`): which
column is the EAN, which is the price, where the data starts, which rows are
discarded. So the file this skeleton produces can be loaded like any other
price list, by declaring an adapter, with no new code in the program.

What's left to decide isn't technical: how much to trust a price list no
supplier signed off on. A file a supplier sends is their word; a file built
by reading their site is our reading of their site, and a page change that
shifts a column becomes a wrong price in a real order. That's why
`valida_catalogo.py` and `controllo_a_campione.py` live here and aren't
optional.

One rule still applies: a supplier's conventions — which columns, which rows
get discarded, which column holds the order — belong in the adapter
registry, not in an `if fornitore == "..."` inside the scraper. The scraper
brings the data; the registry says what it is.

## What this skeleton doesn't do

- No new dependencies. It uses only the standard library, like the rest
  of the project. A site that can only be read with a headless browser is an
  architecture decision to discuss first.
- No parallel downloading. One session, one page at a time: these
  web listing systems keep state in the session, and two requests at once
  step on each other — the result isn't an error but duplicated or skipped
  pages, which is worse. The sample check uses four separate sessions, and
  that's fine because each one restarts from login.
- No hand-written page counts. The catalog grows, and a scraper pinned
  to a fixed number silently stops picking up new pages.
