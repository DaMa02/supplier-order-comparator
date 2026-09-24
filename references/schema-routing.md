# Schema routing and error handling

## Principle

```text
profile every file → match against the adapter registry → known adapter, or a person maps it once → deterministic parsing and a numeric sanity check
```

Recognizing a supplier's file layout is deterministic, not AI-assisted. Phase 2 of the pipeline ("Recognition", `RICONOSCIMENTO` in `app/pipeline_jobs.py`) matches every uploaded file's profile against the adapter registry (`references/adapters.json` plus the entries learned locally) and either finds an adapter that reads it or stops the run for a person to map its columns, through the app's guided UI. AI in this codebase does real work elsewhere — matching products that share no EAN (pipeline phase 6) — but plays no part in recognizing a file's layout.

## 1. Profiling

`scripts/inspect_sources.py` inventories every candidate `.xlsx`/`.xls`/`.csv` in the uploads folder, read-only: sheet names, header row, data start row, per-column value types, and a SHA-256 of the file. For each sheet it also calls `registro.riconosci()` (`scripts/registro.py`) and stores the result on the profile as `deterministic_hint`: a `state` (`SCHEMA_NOTO`, `SCHEMA_VARIATO` or `AMBIGUO`), the matched `adapter_id`, a confidence score, and the evidence for it. Filename similarity plays no part in this: a supplier renaming their own price list every week shouldn't become a new supplier.

## 2. Recognition

`_fase_riconoscimento` turns each profile into a decision without ever calling an AI. For each file:

1. a matching entry in `decisioni_schemi.json` wins first, if there is one (§4);
2. otherwise, `deterministic_hint.state == SCHEMA_NOTO` becomes a decision straight from the registry entry it matched — the same field mapping the adapter already declares.

Anything else stops the run with `SCHEMA_SCONOSCIUTO`: a layout the registry doesn't know at all (`AMBIGUO`), a known supplier whose layout doesn't match its stored signature anymore (`SCHEMA_VARIATO`), a document missing exactly one required header, or a file uploaded under the wrong role (a master export loaded as a supplier list, or the reverse). This is the first of the pipeline's three hard stops — the other two are a document that fails to parse and a price list that parses to zero orderable rows, `FORNITORE_SENZA_RIGHE` (§6). When two uploaded files resolve to the same supplier, only the more recently modified one enters the run; the other is reported, not silently dropped.

The surviving decisions are merged with the profiles by `scripts/apply_preflight_decisions.py` into `input_manifest.json`, under the field name `ai_preflight` — a name left over from an earlier design where this merge step was an AI call. Nothing in the current code fills that field with anything but a deterministic or hand-written decision.

## 3. States

- `SCHEMA_NOTO` — `registro.riconosci()` found an adapter whose signature and deterministic checks all pass; its field mapping is used unchanged.
- `SCHEMA_VARIATO` — from two places: `registro.riconosci()` returns it when a candidate identifies the supplier but fails a deterministic check (a shifted or an extra column that breaks position-based reading); the guided-mapping UI (`app/schema_mapping.py`) also writes it by default when the user maps columns for a supplier already in the registry.
- `NUOVO_FORNITORE` — written only by the guided-mapping UI, when the user names a supplier that isn't in the registry yet. `registro.riconosci()` never produces it.
- `AMBIGUO` — `registro.riconosci()`'s result when no adapter signature matches at all. On its own it becomes part of a `SCHEMA_SCONOSCIUTO` stop; a hand-written decision can also declare it directly, to exclude a file from parsing without going through the guided UI.
- `FILE_NON_PERTINENTE` — reachable only by hand, through `decisioni_schemi.json`; no UI path produces it. Has the same effect as `AMBIGUO`: `validate_input_manifest.py` and `prepare_manifest_sources.py` skip the file.

## 4. The manual fallback: `decisioni_schemi.json`

A hand-maintained JSON file (`NOME_DECISIONI_MANUALI` in `pipeline_jobs.py`), read by `_decisioni_manuali()`. Each entry names a `file_name` and, optionally, the `file_sha256` it applies to; without a hash it matches by name alone, the only remedy left once a decision was written by hand rather than confirmed on the page. A decision whose hash doesn't match the uploaded file anymore is set aside and reported, not silently reapplied to a different document. An entry missing a `rationale` gets one filled in automatically, since the validator (§6) requires it on every decision.

A manual decision overrides even a document the registry would otherwise flag as changed; the page then shows `DECISIONE_MANUALE_ATTIVA` until the registry manages to learn the mapping on its own, after which the note disappears. Confirmed `SCHEMA_VARIATO`/`NUOVO_FORNITORE` entries written here queue for learning (§7) exactly like ones confirmed through the guided UI.

## 5. A supplier the registry doesn't recognize

The Import page opens the guided mapping for that document directly; no JSON file is opened and no command is run.

1. the page shows the sheet and a preview of the rows;
2. it proposes the supplier and the columns from the headers, never from the filename — a deterministic best guess scored against the registry's known adapters, not a model call;
3. the user checks the essential columns — product, EAN, price, pieces per carton and order column; supplier code, VAT, unit and availability sit under "Altre colonne" (Other columns), collapsed by default;
4. "Prova le colonne" (try the columns) re-reads the file with the same parser used by the comparison and shows how many rows are actually usable, plus a normalized sample;
5. only after that check does "Conferma e riparti col confronto" (confirm and restart the comparison) become available. The pipeline restarts on its own and, once that run completes, stores the schema in the registry for the following weeks (§7).

The browser never sends file paths. It identifies the run and the profile the pipeline already produced; the server re-checks that the run is still the one that stopped, that every file belongs to the uploads folder, and that its SHA-256 matches what was shown in the preview. A column with no header is saved by number; one with a unique header is saved by name, so the registry can recognize it in future runs.

If the price list doesn't yet have an order column, the page lets the user pick the first empty column after the data. That choice becomes `order_write`; before creating the copy, the program re-checks the file's fingerprint, sheet, row count, that the header cell is still empty, and that the column has no stray text or formulas. The original file is never modified.

## 6. Validation and parsing

`validate_input_manifest.py` (phase 3, VALIDAZIONE) checks the manifest before anything is read at scale: a known `state` (§3), a `rationale`, the file still on disk with a matching hash, and, for `SCHEMA_VARIATO`/`NUOVO_FORNITORE`, a `field_mapping` complete enough to read with (`incomplete_mapping()`): for the master export, `ean`, `description` and `last_unit_price`; for a supplier, `description`, a price column, an order multiplier (a column or an explicit default), and, for each of EAN, supplier code, availability and VAT, either a column or an explicit flag saying it doesn't apply. Every role also needs the sheet (unless the file is a CSV) and where data starts; a supplier `.xlsx` also needs `order_column`. The registry-learning step (§7) reuses this same function rather than a second copy of the rules. Errors here stop the run (`MANIFEST_NON_VALIDO`); anything else is a non-blocking warning.

`prepare_manifest_sources.py` (phase 4, PARSING) does the actual read: `FILE_NON_PERTINENTE`/`AMBIGUO` files are skipped, `SCHEMA_NOTO` uses the registry's own reader, `SCHEMA_VARIATO`/`NUOVO_FORNITORE` are read generically from the confirmed `field_mapping`. A supplier the manifest expected that parses to zero orderable rows stops the run (`FORNITORE_SENZA_RIGHE`) instead of quietly dropping out of the comparison.

After parsing, a deterministic check that only warns (`_post_check` in `pipeline_jobs.py`) compares this run's row counts and median prices against the previous run, supplier by supplier: it warns when a supplier appeared or vanished, when its row count moved by more than 15%, or when its median price moved by more than 10%. A supplier with no usable median price at all is flagged separately and more loudly (`PREZZI_A_ZERO`), since it would otherwise win every line at €0.00.

## 7. Persistence: learned adapters

Every run keeps an `input_manifest.json` with the classification, the observed signature, the chosen adapter, the mapping, the user confirmation and the post-check results. Once parsing and the comparison build have actually exercised the columns, the orchestrator learns from the run and saves `adattatori_imparati.json` in the run's folder, after a dry run that writes nowhere. The manual decision is then removed automatically, but only for the documents actually learned.

What enters the registry:

- only entries whose `ai_preflight.state` is `SCHEMA_VARIATO` or `NUOVO_FORNITORE` and whose `user_confirmation.status == "CONFIRMED"`: the page proposes a mapping, the user approves it, and nothing unapproved is learned;
- only with a `field_mapping` that's complete according to `validate_input_manifest.incomplete_mapping` — the same checks the validator itself uses, not a second copy of them;
- only if re-reading the document with the registry as just written classifies it as `SCHEMA_NOTO` with the learned adapter: an adapter that doesn't even recognize the file it was learned from is worse than none.

The fingerprint is computed from the profile stored in the manifest, not by reopening the file: the manifest is the auditable record, and it's what the user actually saw when confirming. The entry carries `learned_at`, `learned_from` (the file's name and SHA-256), `confirmed_by: "utente"`, and an incremented `schema_version`; the previous version is never lost, it moves to `previous_versions`. Supplier rules that don't live in the mapping — for example Larice's `row_markers`, which say that `SM` isn't purchasable merchandise — stay where they are.

Every entry that wasn't learned is reported with its reason, in Italian. The exit code is `0` when there's no error and `2` when an entry that should have been learned was rejected: a silent failure here would mean a supplier quietly going back to unknown with nothing to explain why. `--prova` computes and prints everything without writing anything.

The HTML comparison page derives its suppliers from the manifest and the results, so it isn't hardcoded to a fixed set. Writing a new `.xlsx` stays blocked until the order column and the copy rules have been confirmed and tested.

## 8. Writing the order: `order_write`

An adapter that carries `order_write` declares that the tool knows how to build an order copy for that supplier; one without it declares the opposite, and the absence is stated: a supplier that reaches the comparison without this declaration produces the `FORNITORE_NON_COMPILABILE` warning, instead of silently dropping out of the writer's configuration.

Fields, all optional except `order_column`:

- `order_column` — the letter of the column the order quantity is written into. Mandatory: without it there's nowhere to write.
- `from_field_mapping` — `true` when the sheet and rows are already declared by the adapter's own `field_mapping` (Cipresso, Noce). In that case the mapping must declare the same order column; two different columns would mean writing into a cell nobody has verified, and the supplier stays unactivated.
- `sheet`, `header_row`, `data_start_row` — for adapters with a dedicated reader that has no `field_mapping` (Betulla, Larice). `"sheet": "FIRST"` here means "the workbook's first sheet", because it's declared by the registry, i.e. by someone who has actually looked at that price list; `"sheet": "FIRST"` inside a `field_mapping` means instead that the sheet hasn't been identified, which only works if the document has just one.
- `expected_header` — the text that must be in that column's header cell. It's actually read from the document before writing is enabled: a price list with shifted columns would otherwise write the order into an arbitrary column.
- `required_columns` — the fields the mapping must declare for writing to be enabled.
- `mode` — `patch_xls_in_posizione` for Noce's `.xls`, which is returned as the supplier's own file rather than converted: the copy is patched in place, four bytes per cell.
