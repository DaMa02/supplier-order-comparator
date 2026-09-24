# Matching policy

## Contents

1. Order of operations
2. Exact EAN
3. Deterministic shortlist
4. AI evaluation
5. Displays across suppliers
6. State and audit trail

## 1. Order of operations

Applied in this order:

1. deterministic normalization of identifiers;
2. exact EAN match;
3. full collection and structured comparison of duplicate occurrences;
4. deterministic generation of a semantic shortlist;
5. AI evaluation of the shortlist and of the ambiguous duplicate-EAN cases still unresolved;
6. human review of the results that aren't certain.

## 2. Exact EAN

The management-software EAN is the source of truth. Only strip leading/trailing spaces and Excel numeric artifacts such as a trailing `.0`; internal characters are never removed and non-standard codes are never rewritten.

Only active cell values are read. A string that exists solely in the workbook's internal `sharedStrings.xml` table is not a product row in the price list.

States:

- `EAN_ESATTO`: exactly one usable row;
- `EAN_AMBIGUO`: more than one row still valid;
- `EAN_ASSENTE`: no row.

## 3. Deterministic shortlist

For every `EAN_ASSENTE` case, descriptions, brand and quantity/format are normalized. Extracted when present:

- brand and product family;
- volume or weight (`ml`, `l`, `g`, `kg`);
- number of pieces, rolls, washes or nights;
- variant, scent, color, size and target audience;
- product form, e.g. spray, refill, roll-on or gel.

Quantities are read in every form the price lists actually use (`build_semantic_shortlists.attributes`):

- unit after the number: `18PZ`, `500 ML`, `3LT`, `250GR`, `78 MISURINI` (washes);
- unit before the number: `X 18`, `PZ.18`, `ML.500`, `LT.3`, `KG 4`;
- concatenated sums, read as both the base value and the total: `70+8 LAV` is 70 and 78, `PZ.8+2` is 8 and 10;
- a bare `L` after an integer of 10 or more is washes (`COCCOLONE 45L`); `LT` and `LITRI` remain liters;
- with a number immediately before: a unit with a dot attached always belongs to the number that follows it (`X 2 GR.90` is 90 g, `4 IN 1 GR.900` is 900 g); dot-plus-space (`GR. 500`) only when the preceding number counts pieces; a plain space (`PH 3.5 ML 200`) only for volumes and weights, and only if the following number has no unit of its own;
- pieces and weight (or volume) in the same name also count as a total: `GR.250 X 2` is 250 and 500 g.

Deliberately not read: ranges (`11-25 KG` for a child's age, `0-6` months, `KG. 11/25`), sizes (`5°MIS.`), formulas (`2X13=26`), an `X` followed by a number with its own unit (`54 DOSI X 12=648 GR`: the 12 is the grams of a single dose), dimensions (`30 X 40 CM`), an `X` after a number or before a unit with no number of its own (`2 X 250ML` is 250 ML), numbers attached to letters (`2IN1`), ages and protection factors (`45+`, `FP 50+`), codes (`E1`, `N.4`), stray `+` signs. It's better to skip a number than to read it wrong: a false conflict costs the correct row 0.35 in score.

Candidates with an incompatible numeric format are deterministically penalized, and the rest are ranked with a reproducible score based on tokens, string similarity and structured attributes. In the current files, brand has no reliable dedicated column, so the AI is left to read it from the description instead of encoding a rigid rule for it. Only the top candidates and their scores are passed to the AI.

## 4. AI evaluation

The AI evaluates candidates that have already been produced; it never searches the price list freely. It can reject every candidate or leave the case unresolved: textual similarity alone doesn't force a match.

The match is rejected when:

- the brand differs with no documented alias;
- volume, weight, piece count or multipack size are incompatible;
- the variant substantially changes the product;
- the best candidate isn't clearly preferable to the second-best.

An accepted semantic match must carry a short rationale and a confidence level. Medium- or low-confidence matches require user review before the order is placed.

## 5. Displays across suppliers

Two displays are compared as offers of the same item only when their composition is demonstrably equivalent. The primary key is a stable fingerprint built from the ordered `component EAN + quantity` pairs. The display's name, its total piece count, or the brand alone are not enough.

If every EAN and quantity matches, the offers are comparable even when suppliers word the name differently. If EAN or quantity is missing, the comparison is only proposed with a rationale and human confirmation. The per-piece price of two different compositions is never compared automatically.

## 6. State and audit trail

Kept for every product/supplier pair:

- management-software EAN;
- candidate's EAN and row;
- match method;
- deterministic score;
- alternative candidates;
- AI decision and rationale, when applicable;
- user confirmation or correction.

For displays, also kept: parent row, component rows, declared and reconstructed quantity, parent and reconstructed price, confidence, evidence and composition fingerprint.

Matching is never re-run at order-writing time: the approved, persisted source row is used as-is.

### The same code at another supplier (`EAN_DA_ALTRO_FORNITORE`)

`merge_match_decisions.propaga_lo_stesso_codice`: if the AI accepted a row with EAN X (different from the management-software EAN) at supplier A, then at every other supplier S with no match and status `EAN_ASSENTE`, the one usable row carrying EAN X enters with `status: SEMANTICO_PROPOSTO`, `method: EAN_DA_ALTRO_FORNITORE`, confidence `MEDIA`, and it always requires confirmation. It carries `propagato_da` (the source suppliers), `codice_propagato`, and `prima` (the status, method, confidence and rationale it replaces).

- It overrides an AI `REJECT` at S and an undiscarded `DA_VERIFICARE`: that's the scenario it's built for, where the AI rejects an item at one supplier but accepts the same EAN at another.
- It never overrides a row that's already selected, a discarded decision (`ai_decisione_scartata`), or any EAN status other than `EAN_ASSENTE`.
- Two or more candidate rows at S (the same EAN repeated, or two sources with different codes) propose nothing; the case is counted in `stesso_codice_ambiguo`.
- Only codes shaped like a real EAN (8, 12, 13 or 14 digits, not all zeros) are eligible, compared as raw digits the same way the native EAN path does.
- Sources are `SEMANTICO_AI` decisions only: a propagated row never becomes a source itself, and supplier order doesn't affect the result.
- A user "no" on the propagated row discards it by fingerprint like any other rejection; a "yes" is remembered as a confirmation of (supplier, product) with reason `EAN_DA_ALTRO_FORNITORE`. No code equality is ever written on its own.
