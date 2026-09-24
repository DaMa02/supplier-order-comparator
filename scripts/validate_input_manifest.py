#!/usr/bin/env python3
"""Validate an input manifest, deterministic or hand-written, before parsing."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# The adapter registry is read via `registro`, never with a plain `json.load`
# here: it is the merge of the shipped registry and the one learned locally,
# and reading only one of the two hides half the suppliers.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import registro  # noqa: E402


STATES = {"SCHEMA_NOTO", "SCHEMA_VARIATO", "NUOVO_FORNITORE", "FILE_NON_PERTINENTE", "AMBIGUO"}
ROLES = {"master", "supplier", "ignore"}

# The two ways to match the marker text. No regex support: a rule the user
# writes from the page has to stay readable by whoever rereads it later.
CONFRONTI_DEL_MARCATORE = ("equals", "contains")


def errori_del_marcatore(marker: Any) -> list[str]:
    """Validate a "data starts after row X" marker declaration.

    Lives next to the rest of the mapping checks, not in the reader: if the two
    checklists drift apart, a mapping the validator accepts could be rejected
    at read time (or the reverse). The reader calls this same function before
    it searches.

    Expected shape::

        "data_start_marker": {"column": "A", "equals": "LISTINO", "offset": 1}

    ``column`` is a column letter or its 1-based number; ``equals`` matches the
    full cell text, ``contains`` a substring (exactly one of the two); ``offset``
    is how many rows below the marker the products start, defaulting to 1.
    """

    if not isinstance(marker, dict) or not marker:
        return ["data_start_marker deve essere un oggetto con column e equals (oppure contains)"]
    problemi: list[str] = []
    colonna = marker.get("column")
    if isinstance(colonna, bool) or (
        not (isinstance(colonna, int) and colonna >= 1)
        and not re.fullmatch(r"[A-Za-z]{1,3}|[1-9][0-9]*", str(colonna or "").strip())
    ):
        problemi.append("data_start_marker.column deve essere una lettera di colonna o il suo numero")
    dichiarati = [nome for nome in CONFRONTI_DEL_MARCATORE
                  if str(marker.get(nome) or "").strip() != ""]
    if len(dichiarati) != 1:
        problemi.append("data_start_marker deve dichiarare «equals» oppure «contains», uno solo dei due")
    scarto = marker.get("offset", 1)
    if isinstance(scarto, bool) or not isinstance(scarto, int) or scarto < 0:
        problemi.append("data_start_marker.offset deve essere un numero intero da 0 in su")
    return problemi


# Maps each validator declaration to how it's phrased on the page.
#
# Lives here and not in `app/schema_mapping.py` for the same reason as
# `errori_del_marcatore`: if the two lists drift apart, a new validator string
# has no translation on the page, and the user sees the raw validator message
# again.
PAROLE_DELLA_PAGINA: dict[str, str] = {
    "columns.description": "nome prodotto",
    "columns.unit_price_net oppure columns.unit_price_pre_discount": "prezzo",
    "fattore d'ordine in colonna oppure default esplicito": "pezzi per collo",
    "columns.ean oppure ean_unavailable=true": "EAN",
    # The master (management-software) branch requires the bare column name:
    # without this entry, "columns.ean" showed up on the page as "ean".
    "columns.ean": "EAN",
    "columns.supplier_code oppure supplier_code_unavailable=true": "codice fornitore",
    "columns.availability oppure assume_available=true": "disponibilità",
    "columns.vat oppure vat_unavailable=true": "IVA",
    "order_column": "colonna ordine",
    "columns.last_unit_price": "ultimo prezzo",
    "sheet": "foglio da leggere",
    "data_start_row oppure data_start_marker": "prima riga dei prodotti",
    # The two "not even a declaration" cases: the `columns` fallback above
    # keeps the page from reaching them, but if it ever does, the phrase must
    # still tell the user what to fix.
    "field_mapping deve essere un oggetto": "nome prodotto, prezzo, pezzi per collo",
    "columns deve essere un oggetto": "nome prodotto, prezzo, pezzi per collo",
    # The four messages from `errori_del_marcatore`, forwarded by `incomplete_mapping`.
    "data_start_marker deve essere un oggetto con column e equals (oppure contains)":
        "la riga che separa i prodotti",
    "data_start_marker.column deve essere una lettera di colonna o il suo numero":
        "la colonna in cui cercare la riga che separa i prodotti",
    "data_start_marker deve dichiarare «equals» oppure «contains», uno solo dei due":
        "il testo della riga che separa i prodotti",
    "data_start_marker.offset deve essere un numero intero da 0 in su":
        "quante righe dopo il separatore cominciano i prodotti",
}


def parole_della_pagina(mancanti: list[str]) -> str:
    """Render missing declarations using the page's own vocabulary.

    A string not found in the table is never printed as-is: a raw validator
    token means nothing to someone placing orders. It falls back to a phrase
    that at least points at the right area; the real string stays in the
    stop's `dettaglio`, for whoever reads the logs.
    """

    dette: list[str] = []
    for voce in mancanti:
        frase = PAROLE_DELLA_PAGINA.get(voce, "le colonne del documento")
        for pezzo in frase.split(", "):
            if pezzo not in dette:
                dette.append(pezzo)
    return ", ".join(dette)


def incomplete_mapping(mapping: Any, role: str, path: Path) -> list[str]:
    """Return missing semantic declarations for a varied/new schema."""

    if not isinstance(mapping, dict):
        return ["field_mapping deve essere un oggetto"]
    # Not `mapping.get("columns") or …`: an empty dict is falsy, so "the user
    # picked no columns" would fall into the "not an object" branch and report
    # the JSON shape instead of the missing columns. The `field_mapping`
    # fallback still applies, but only when `columns` is truly absent.
    raw_columns = mapping["columns"] if "columns" in mapping else mapping.get("field_mapping")
    if not isinstance(raw_columns, dict):
        return ["columns deve essere un oggetto"]
    columns = {str(field) for field, spec in raw_columns.items() if spec not in (None, "")}
    missing: list[str] = []

    if path.suffix.casefold() != ".csv" and mapping.get("sheet") in (None, ""):
        missing.append("sheet")
    try:
        data_start = int(mapping.get("data_start_row") or 0)
    except (TypeError, ValueError):
        data_start = 0
    # A fixed row number isn't the most robust way to say where the list
    # starts: a promotional block can grow or shrink week to week, and a
    # frozen row number would silently cut the list at the wrong point. A
    # marker ("products start after the row where column A reads LISTINO") is
    # a rule, recomputed on every read.
    marker = mapping.get("data_start_marker")
    if marker not in (None, "", {}):
        missing.extend(errori_del_marcatore(marker))
    elif data_start < 1:
        missing.append("data_start_row oppure data_start_marker")

    if role == "master":
        for field in ("ean", "description", "last_unit_price"):
            if field not in columns:
                missing.append(f"columns.{field}")
        return missing

    if "description" not in columns:
        missing.append("columns.description")
    if not ({"unit_price_net", "unit_price_pre_discount"} & columns):
        missing.append("columns.unit_price_net oppure columns.unit_price_pre_discount")
    if not ({"pieces_per_carton", "order_multiplier"} & columns) and not any(
        mapping.get(field) not in (None, "")
        for field in ("pieces_per_carton_default", "order_multiplier_default")
    ):
        missing.append("fattore d'ordine in colonna oppure default esplicito")
    if "ean" not in columns and mapping.get("ean_unavailable") is not True:
        missing.append("columns.ean oppure ean_unavailable=true")
    if "supplier_code" not in columns and mapping.get("supplier_code_unavailable") is not True:
        missing.append("columns.supplier_code oppure supplier_code_unavailable=true")
    if "availability" not in columns and mapping.get("assume_available") is not True:
        missing.append("columns.availability oppure assume_available=true")
    if "vat" not in columns and mapping.get("vat_unavailable") is not True:
        missing.append("columns.vat oppure vat_unavailable=true")
    if path.suffix.casefold() != ".csv" and not mapping.get("order_column"):
        missing.append("order_column")
    return missing


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def identificativi_ammessi(percorso_adattatori: Path | None) -> set[str]:
    """Return the adapter ids a manifest may declare, from the real, merged registry.

    A locally learned supplier (from the guided mapping, or from moving the
    order column) gets an id absent from the shipped registry (`quercia_v1`
    for a new supplier, `betulla_v1__locale` for one learned on top of a
    shipped adapter). Reading only the shipped document here would reject that
    id as `ADATTATORE_NON_VALIDO` even after the recognition phase correctly
    reports it as `SCHEMA_NOTO`.

    Every id decision must go through the registry (`prepare_manifest_sources`,
    `catalog_search`), which merges both documents; this is the one place that
    decides by id, so it has to read the merged view too.
    """

    voci, _motivo = registro.adattatori_effettivi(percorso_adattatori)
    return {str(voce.get("id") or "") for voce in voci if str(voce.get("id") or "")}


def adattatore_ammesso(identificativo: Any, ammessi: set[str]) -> bool:
    """Return whether an id is admitted, directly or via its base adapter.

    Matching on the base id mirrors what the readers do: `betulla_v1__locale`
    *is* Betulla, and if the learned entry became unreadable between profiling
    and this check, the document would still be readable via the shipped
    entry that `registro.voce_in_uso` falls back to. Rejecting it here would
    discard a comparison over an adapter mismatch that doesn't matter.
    """

    nome = str(identificativo or "")
    if not nome:
        return False
    return nome in ammessi or registro.adattatore_base(nome) in ammessi


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--adapters", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = load_json(args.manifest)
    adapter_ids = identificativi_ammessi(args.adapters)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    masters = []
    supplier_ids = set()

    files = manifest.get("files") or manifest.get("profiles") or []
    if not files:
        errors.append({"code": "NESSUN_FILE", "message": "Il manifest non contiene file."})

    for item in files:
        path = Path(item.get("path") or "")
        decision = item.get("ai_preflight") or {}
        label = item.get("file_name") or path.name or "file-senza-nome"
        state = decision.get("state")
        role = decision.get("role")
        adapter_id = decision.get("adapter_id")

        if state not in STATES:
            errors.append({"file": label, "code": "STATO_AI_MANCANTE", "message": "Completare ai_preflight.state."})
            continue
        if not decision.get("rationale"):
            errors.append({"file": label, "code": "MOTIVAZIONE_AI_MANCANTE", "message": "Completare ai_preflight.rationale."})
        if state in {"FILE_NON_PERTINENTE", "AMBIGUO"}:
            if role not in {None, "ignore"}:
                errors.append({"file": label, "code": "RUOLO_INCOERENTE", "message": f"Lo stato {state} non può entrare nel parsing."})
            continue
        if role not in ROLES - {"ignore"}:
            errors.append({"file": label, "code": "RUOLO_NON_VALIDO", "message": "Il ruolo deve essere master o supplier."})
            continue
        if not path.is_file():
            errors.append({"file": label, "code": "FILE_ASSENTE", "message": str(path)})
        elif item.get("sha256") and sha256(path) != item["sha256"]:
            errors.append({"file": label, "code": "FILE_MODIFICATO", "message": "Hash diverso dal profilo preflight."})

        if state == "SCHEMA_NOTO" and not adattatore_ammesso(adapter_id, adapter_ids):
            errors.append({"file": label, "code": "ADATTATORE_NON_VALIDO", "message": str(adapter_id)})
        if state in {"SCHEMA_VARIATO", "NUOVO_FORNITORE"}:
            mapping = decision.get("field_mapping")
            if not mapping:
                errors.append({"file": label, "code": "MAPPATURA_MANCANTE", "message": "Serve field_mapping confermato o da confermare."})
            else:
                missing_fields = incomplete_mapping(mapping, role, path)
                if missing_fields:
                    errors.append({
                        "file": label,
                        "code": "MAPPATURA_INCOMPLETA",
                        # `message` is what the stop shows on the page
                        # (app/pipeline_jobs.py, MANIFEST_NON_VALIDO), hence
                        # Italian. The raw validator strings stay in `missing`,
                        # shown to no one but readable by anyone.
                        "message": "manca " + parole_della_pagina(missing_fields) + ".",
                        "missing": missing_fields,
                    })
        confirmation = item.get("user_confirmation") or {}
        if confirmation.get("required") is True and confirmation.get("status") != "CONFIRMED":
            errors.append({"file": label, "code": "CONFERMA_UTENTE_MANCANTE", "message": "La mappatura ambigua non è stata confermata."})

        if role == "master":
            masters.append(label)
        if role == "supplier":
            supplier_id = decision.get("supplier_id")
            if not supplier_id:
                errors.append({"file": label, "code": "FORNITORE_MANCANTE", "message": "Completare supplier_id."})
            elif supplier_id in supplier_ids:
                errors.append({"file": label, "code": "FORNITORE_DUPLICATO", "message": supplier_id})
            else:
                supplier_ids.add(supplier_id)

    if len(masters) != 1:
        errors.append({"code": "MASTER_NON_UNIVOCO", "message": f"Atteso un solo gestionale, trovati {len(masters)}: {masters}"})
    if not supplier_ids:
        # Must be an error, not a warning: a warning let the manifest pass
        # validation, and the next phase (`prepare_manifest_sources`) failed
        # with a generic "check the uploaded files" message instead of this
        # specific one.
        errors.append({
            "code": "NESSUN_FORNITORE",
            "message": "Manca il listino di almeno un fornitore: carica i listini di questa settimana e riprova.",
        })

    report = {"valid": not errors, "files": len(files), "master": masters, "suppliers": sorted(supplier_ids), "errors": errors, "warnings": warnings}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
