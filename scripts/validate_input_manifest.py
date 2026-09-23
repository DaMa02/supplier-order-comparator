#!/usr/bin/env python3
"""Validate an AI-reviewed input manifest before deterministic parsing."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# ⚠ Il registro si legge da `registro`, non con un `json.load` di questo file.
# Il registro effettivo e' fatto di DUE documenti — quello spedito e quello che
# il programma impara su questo computer — e chi ne legge uno solo vede meta'
# dei fornitori.  `registro` sta qui accanto, in `scripts/`.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import registro  # noqa: E402


STATES = {"SCHEMA_NOTO", "SCHEMA_VARIATO", "NUOVO_FORNITORE", "FILE_NON_PERTINENTE", "AMBIGUO"}
ROLES = {"master", "supplier", "ignore"}

# I due modi di dire dove comincia il testo cercato dal marcatore.  Non c'e'
# una espressione regolare, e non ci sara': una regola che l'utente scrive
# dalla pagina dev'essere leggibile da chi la rilegge fra sei mesi.
CONFRONTI_DEL_MARCATORE = ("equals", "contains")


def errori_del_marcatore(marker: Any) -> list[str]:
    """Che cosa non va nella dichiarazione «i dati cominciano dopo la riga X».

    Sta qui, accanto alle altre verifiche della mappatura, e non nel lettore:
    due elenchi di controlli che si allontanano di un campo vorrebbero dire una
    mappatura accettata dal validatore e rifiutata alla lettura — o peggio il
    contrario.  Il lettore chiama questa stessa funzione prima di cercare.

    Forma attesa::

        "data_start_marker": {"column": "A", "equals": "LISTINO", "offset": 1}

    ``column`` e' una lettera di colonna oppure il suo numero 1-based;
    ``equals`` vuole il testo intero della cella, ``contains`` un pezzo (uno
    solo dei due); ``offset`` dice quante righe piu' in basso cominciano i
    prodotti, e vale 1 se non c'e'.
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


# Come si dice in pagina quello che qui e' scritto per il validatore.
#
# ⚠ Sta qui e non in `app/schema_mapping.py` per la stessa ragione per cui ci
# sta `errori_del_marcatore`: due elenchi che si allontanano di una voce
# vorrebbero dire una stringa nuova nel validatore e nessuna traduzione in
# pagina — cioe' di nuovo «completa columns deve essere un oggetto».
PAROLE_DELLA_PAGINA: dict[str, str] = {
    "columns.description": "nome prodotto",
    "columns.unit_price_net oppure columns.unit_price_pre_discount": "prezzo",
    "fattore d'ordine in colonna oppure default esplicito": "pezzi per collo",
    "columns.ean oppure ean_unavailable=true": "EAN",
    # Il ramo del gestionale chiede le colonne **nude**: senza questa riga
    # «columns.ean» usciva in pagina come «ean».
    "columns.ean": "EAN",
    "columns.supplier_code oppure supplier_code_unavailable=true": "codice fornitore",
    "columns.availability oppure assume_available=true": "disponibilità",
    "columns.vat oppure vat_unavailable=true": "IVA",
    "order_column": "colonna ordine",
    "columns.last_unit_price": "ultimo prezzo",
    "sheet": "foglio da leggere",
    "data_start_row oppure data_start_marker": "prima riga dei prodotti",
    # I due casi «non e' nemmeno una dichiarazione»: dopo la correzione qui
    # sotto non si raggiungono piu' dalla pagina, ma se ci si torna la frase
    # dev'essere quella che dice all'utente che cosa fare.
    "field_mapping deve essere un oggetto": "nome prodotto, prezzo, pezzi per collo",
    "columns deve essere un oggetto": "nome prodotto, prezzo, pezzi per collo",
    # Le quattro di `errori_del_marcatore`, che `incomplete_mapping` rilancia.
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
    """Le dichiarazioni mancanti, dette come si chiamano nella pagina.

    Una stringa che non e' nella tabella **non** si stampa com'e': un token da
    validatore in faccia a chi fa gli ordini non gli dice che cosa toccare. Si
    ripiega su una frase che almeno indica dove guardare, e la stringa vera
    resta nel `dettaglio` della fermata, per chi legge i log.
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
    # ⚠ Non `mapping.get("columns") or …`: un dizionario **vuoto** e' falso, e
    # «l'utente non ha scelto nessuna colonna» finiva sul ramo «non e' un
    # oggetto», cioe' su un messaggio che parla della forma del JSON invece che
    # delle colonne mancanti. Misurato il 21 agosto 2026 su «OFFERTE AGOSTO
    # 4.xlsx»: «completa columns deve essere un oggetto». Il ripiego su
    # `field_mapping` resta, ma solo quando `columns` non c'e' per davvero.
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
    # Il numero non e' l'unico modo di dire dove comincia il listino, e non e'
    # il piu' robusto: su QUERCIA le righe 7-67 sono un blocco promozionale che la
    # settimana prossima sara' piu' corto o piu' lungo, e un 69 congelato
    # taglierebbe l'elenco nel punto sbagliato **in silenzio**.  Un marcatore
    # («i prodotti cominciano dopo la riga in cui la colonna A dice LISTINO»)
    # e' una regola, e si ricalcola a ogni lettura.
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
    """Gli identificativi che un manifest puo' dichiarare, presi dal registro VERO.

    ⚠ Qui c'era un `json.load` del solo documento spedito, e per un giorno
    intero e' stato il difetto piu' grave del programma: un fornitore imparato
    su questo computer — dalla mappatura guidata, oppure spostando la colonna
    d'ordine — prende un identificativo che nel documento spedito non c'e'
    (`quercia_v1` per un fornitore nuovo, `betulla_v1__locale` per uno imparato
    sopra uno spedito).  La settimana dopo il riconoscimento lo dichiara
    giustamente `SCHEMA_NOTO` con QUEL nome, e questo controllo lo bocciava:
    `ADATTATORE_NON_VALIDO`, cioe' `MANIFEST_NON_VALIDO`, cioe' il confronto
    della settimana che non si fa e nessuna via d'uscita dalla pagina.

    La regola e' rispettata dal parser
    (`prepare_manifest_sources`) e dalla ricerca (`catalog_search`): **chi
    decide per identificativo passa dal registro**, che i due documenti li
    fonde.  Questo era l'unico posto che decideva per identificativo leggendo
    un file da solo.
    """

    voci, _motivo = registro.adattatori_effettivi(percorso_adattatori)
    return {str(voce.get("id") or "") for voce in voci if str(voce.get("id") or "")}


def adattatore_ammesso(identificativo: Any, ammessi: set[str]) -> bool:
    """Un identificativo va bene se il registro ce l'ha, o se ha la sua radice.

    Il secondo caso non e' larghezza, e' la stessa cosa che fanno i lettori: un
    `betulla_v1__locale` **e'** Betulla, e se la voce imparata sparisse — file
    illeggibile fra la profilazione e questo controllo — il documento
    resterebbe leggibile con la voce spedita, che e' quella che
    `registro.voce_in_uso` gli darebbe.  Fermare la catena in quel caso
    vorrebbe dire buttare un confronto per un adattatore che non serviva.
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
                        # `message` e' la frase che la fermata mostra in pagina
                        # (app/pipeline_jobs.py, MANIFEST_NON_VALIDO): qui ci va
                        # l'italiano. Le stringhe del validatore restano in
                        # `missing`, che nessuno mostra e tutti possono leggere.
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
        # ⚠ Era un avviso, e la fase dopo moriva.  La validazione dichiarava il
        # manifest valido, `prepare_manifest_sources` sollevava «Nessun
        # fornitore incluso nel manifest» e usciva 1, e in pagina arrivava la
        # frase generica del passo fallito — «il problema e' in uno dei
        # documenti caricati» — a chi aveva caricato un documento solo e giusto.
        # La cosa da fare la sapeva gia' questa riga, una fase prima.
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
