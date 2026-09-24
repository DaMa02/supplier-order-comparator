# AI decision format

Decisions are saved as a JSON list. Each element must contain:

```json
{
  "gestionale_source_row": 12,
  "supplier": "betulla",
  "action": "ACCEPT",
  "source_row": 441,
  "confidence": "ALTA",
  "rationale": "Marca e formato coincidono; descrizione abbreviata nel listino.",
  "requires_user_confirmation": true,
  "ai_impronta_caso": "9d3f0c1a7b2e4056",
  "ai_articolo_mostrato": "PANTERA SHAMPOO 250ML RICCI NEW",
  "ai_modello": "openai/gpt-5.6-luna",
  "ai_versione_prompt": "v3",
  "ai_versione_avversario": "v1"
}
```

Allowed values:

- `action`: `ACCEPT`, `REJECT`, `UNRESOLVED`;
- `supplier`: `betulla`, `larice`, `noce`, or any other adapter registered in `references/adapters.json`;
- `confidence`: `ALTA`, `MEDIA`, `BASSA`;
- `source_row`: required only for `ACCEPT`, and it must belong to that pair's shortlist, i.e. to the set of candidates that were actually shown to the model.

## `ai_impronta_caso` — required

The first sixteen hex digits of a hash of the case exactly as the model saw it: the management-software row and supplier, the searched item's description, and the ordered list of candidates with their `source_row`, description and score. `build_semantic_shortlists.impronta_caso` computes it, `scripts/valuta_shortlist.py` writes it, and `scripts/merge_match_decisions.py` verifies it by recomputing it from the current run's shortlist.

It guards against a case no other check sees: a decisions file from a different run. The management-software export is the same file week to week, so `(row, supplier)` pairs mostly overlap, counts reconcile, and the other guards only compare artifacts that all belong to the current run. The fingerprint is the only field that carries what the model actually had in front of it.

It covers every candidate, not only the accepted one: a stale `REJECT` says "none of these match" about a list that has since changed, and that product would silently disappear from the comparison at that supplier.

A decision whose fingerprint doesn't match — or that carries none at all — is not applied: that pair goes back to `DA_VERIFICARE` and `merge_match_decisions.py` exits with 3. The two causes are kept distinct in both the cause code and the message, because they point at different places to look: `DECISIONE_DI_UNA_ALTRA_RUN` for a mismatching fingerprint, `DECISIONE_SENZA_IMPRONTA` for a missing one. Exit 3 isn't a warning the run absorbs: the orchestrator only tolerates exit 4 or 5 and shows those as a banner while the comparison continues; exit 3 means the artifacts don't describe the same run, so the pipeline stops. Accepting a decision with no fingerprint would make the guard trivial to bypass by simply omitting a field.

What the fingerprint proves, and what it doesn't: it proves the case evaluated today is identical to the one the decision was made on. If a supplier's price list doesn't change from one week to the next, the case really is the same: the fingerprint matches and last week's decision is accepted — which is the point, since it's exactly what the answer memory in `app/data/memoria_ai.json` is for. What the fingerprint does not say is what that decision was made with.

## `ai_modello`, `ai_versione_prompt`, `ai_versione_avversario` — required

Provenance. These matter for the case the fingerprint lets through: a decision made with an older prompt version, on a case that hasn't changed since. `scripts/valuta_shortlist.py` writes them from the live configuration.

`merge_match_decisions.py` reads them without judging them: it has no access to today's configuration to compare against. That comparison is the orchestrator's job.

`ai_articolo_mostrato` is not verified; it carries the description of the item the decision was made about, so that anyone reading the file or the summary can tell what was being discussed.

A `source_row` must come from the shortlist shown to the model, because the AI evaluation step builds its cases from the shortlist alone. For an `EAN_AMBIGUO` case it must also carry the management-software EAN, i.e. belong to `usable_candidates` in `matching_result.json`: the shortlist there isn't EAN-ranked, so this second check keeps an `ACCEPT` from landing on an unrelated row with the same description. A `source_row` outside the shortlist, or (for `EAN_AMBIGUO`) without the EAN, makes `merge_match_decisions.py` exit with 5 and degrades that pair to `DA_VERIFICARE`.

Rows, EANs or prices must never be invented. The rationale must cite the attributes that support or rule out the match. When two candidates remain equivalent, `UNRESOLVED` is used.
