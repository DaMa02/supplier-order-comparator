#!/usr/bin/env python3
"""Profile candidate XLSX/XLS/CSV inputs without changing them.

The output is intentionally descriptive: a later step matches each profile
against the adapter registry, or a person maps it through the guided UI, and
fills in ``ai_preflight`` — a legacy field name for a decision that's always
deterministic or hand-written, never an AI call — before parsing a run.

The format is decided by the file's first bytes, not its extension: legacy
Excel 97-2003 files go through the reader in ``app/xls_reader.py``, .xlsx
through openpyxl, everything else through the CSV reader.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

# The legacy Excel 97-2003 reader lives in the application folder and uses
# only the standard library: an .xls price list opens with nothing extra
# installed on the user's machine. The schema registry lives next to it, and
# its folder is added too — importers of this module already have it on the
# path, but relying on that silently would mean an import error for the
# first caller that forgets to set it up.
SCRIPTS_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPTS_DIR.parent / "app"
for cartella in (SCRIPTS_DIR, APP_DIR):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import registro  # noqa: E402
from xls_reader import read_workbook  # noqa: E402


SUPPORTED_SUFFIXES = {".xlsx", ".xls", ".csv"}
SAMPLE_LIMIT = 12
EXAMPLE_LIMIT = 5

# A file's first bytes say what it really is. The extension doesn't: a
# price list can arrive renamed, and opening an .xls with the .xlsx reader
# (or the reverse) would give the user an error they can't make sense of.
FIRMA_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # Excel 97-2003
FIRMA_ZIP = b"PK\x03\x04"  # Excel 2007 e successivi: e' un archivio


def container_format(path: Path) -> str:
    """Says which reader a file needs, by inspecting its first bytes."""
    with path.open("rb") as stream:
        testa = stream.read(8)
    if testa.startswith(FIRMA_OLE2):
        return "xls"
    if testa.startswith(FIRMA_ZIP):
        return "xlsx"
    return "csv"


def normalized(value: Any) -> str:
    """A header's normalized token, computed the same way the registry does.

    Kept as a local name because the rest of the module uses it, but the
    computation itself has a single implementation: two versions drifting
    apart by even one character would mean a price list recognized here and
    not found by the registry, with no visible reason why.
    """

    return registro.normalizza(value)


def display_value(value: Any, limit: int = 180) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = str(value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def type_name(value: Any, data_type: str | None = None) -> str:
    if data_type == "f" or (isinstance(value, str) and value.startswith("=")):
        return "formula"
    if value is None or value == "":
        return "blank"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "text"


def header_candidates(rows: list[tuple[int, list[Any]]]) -> list[dict[str, Any]]:
    """The rows that look like a header, matched against a list of known words.

    Kept because it's what a person reads in the manifest to see at a glance
    where the header is and how confident the match looks. This isn't what
    drives recognition: the keyword list below describes today's suppliers,
    and a new supplier wouldn't show up in it. What the registry actually
    reads are the rows from `prime_righe_non_vuote`.
    """

    keywords = {
        "ean", "codice", "codart", "descrizione", "descrcommerciale", "colli",
        "quantita", "prezzo", "sconto", "iva", "totale", "totali", "ordine",
        "pzct", "cessione", "product", "packaging", "availability", "unit",
        # Headers from a legacy Excel 97-2003 price list.
        "codiceabarre", "descrizionearticolo", "pezzixcartone",
        "offerta", "importo", "cat", "ragionesociale", "variato",
    }
    candidates = []
    for row_number, values in rows:
        tokens = [normalized(value) for value in values if value not in (None, "")]
        matches = sorted({token for token in tokens if token in keywords})
        text_count = sum(isinstance(value, str) and value.strip() != "" for value in values)
        score = len(matches) * 3 + min(text_count, 8)
        if matches:
            candidates.append({
                "row": row_number,
                "score": score,
                "matched_keywords": matches,
                "values": [display_value(value) for value in values],
            })
    return sorted(candidates, key=lambda item: (-item["score"], item["row"]))[:5]


# How many rows are handed to the registry to search for a header. Measured
# headers in real price lists sit between row 1 and row 6; twenty rows leave
# margin for a wordier supplier without bloating the manifest past what a
# person can still read.
RIGHE_PER_IL_REGISTRO = 20


def prime_righe_non_vuote(rows: list[tuple[int, list[Any]]],
                          candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows the registry can read to look for a header.

    These are simply the first rows with content, none pre-selected: it's
    the only way for the registry to see the header of a supplier it hasn't
    catalogued yet. `header_candidates` discards rows with no word from its
    fixed keyword list, so without these raw rows a new supplier's price
    list would never reach the recognition engine and the learning step
    would have nothing to learn from.

    Rows `header_candidates` already recognized are added even when they
    fall past row twenty, up to row fifty: a supplier with a long preamble
    needs its header row included too, not just the leading rows.
    """

    righe = {
        row_number: [display_value(value) for value in values]
        for row_number, values in rows[:RIGHE_PER_IL_REGISTRO]
    }
    for candidato in candidates:
        righe.setdefault(candidato["row"], candidato["values"])
    return [{"row": numero, "values": righe[numero]} for numero in sorted(righe)]


# A section break: a row with very few filled cells sandwiched between rows
# that are mostly full. This is how a price list marks "something different
# starts here" — a single marker cell after a promotional block of prices
# that are gift valuations, not purchase prices. The thresholds are measured
# on real price lists: a break has one or two filled cells, a product row
# has eight or more, and four is a wide margin for a sparser price list.
CELLE_DI_UN_SEPARATORE = 2
CELLE_DI_UNA_RIGA_PIENA = 4
# Only near the top of the document: further down, a narrow row is a group
# label inside the data, not the start of the data — LARICE's price list has
# 625 such rows, none of which mark where the list actually begins. A
# hundred rows leave margin for the longest measured promotional block
# (QUERCIA: 61 rows).
SEPARATORI_IN_TESTA = 100
# The rows around a section break that the profile carries along: two
# before to show what ends, six after to show what begins.
RIGHE_PRIMA_DEL_SEPARATORE = 2
RIGHE_DOPO_IL_SEPARATORE = 6
# The cap on these extra rows: a document alternating sections every three
# rows must not bloat the profile past what a person can still read.
RIGHE_DI_SEZIONE_AL_MASSIMO = 24


def _cella_vuota(valore: Any) -> bool:
    return valore is None or (isinstance(valore, str) and valore.strip() == "")


def separatori_di_sezione(rows: list[tuple[int, list[Any]]]) -> list[dict[str, Any]]:
    """The rows that mark the start of a section, with their marker text.

    Used when mapping a new supplier: without this, "data starts at row 69"
    is a number that can only be guessed by opening Excel, and next week
    it'll be a different number. With this list the page can instead propose
    "products start after the row that says 'LISTINO'" — a rule, not a
    number.
    """

    piene = [sum(1 for valore in valori if not _cella_vuota(valore)) for _numero, valori in rows]
    trovati: list[dict[str, Any]] = []
    for posizione in range(min(len(rows), SEPARATORI_IN_TESTA)):
        if not 0 < piene[posizione] <= CELLE_DI_UN_SEPARATORE:
            continue
        prima = piene[posizione - 1] if posizione else 0
        dopo = piene[posizione + 1] if posizione + 1 < len(piene) else 0
        if prima < CELLE_DI_UNA_RIGA_PIENA or dopo < CELLE_DI_UNA_RIGA_PIENA:
            continue
        numero, valori = rows[posizione]
        indice = next((indice for indice, valore in enumerate(valori, start=1)
                       if not _cella_vuota(valore)), None)
        if indice is None:
            continue
        trovati.append({
            "row": numero,
            "column": indice,
            "letter": get_column_letter(indice),
            # Short on purpose: a label to recognize in a list, not the text
            # to read — the full text is in the surrounding rows.
            "text": display_value(valori[indice - 1], limit=80),
            # Where data would start if cut here: the number the page shows
            # next to the proposal, which the marker recomputes on its own
            # on every later run.
            "data_from": rows[posizione + 1][0] if posizione + 1 < len(rows) else numero + 1,
        })
    return trovati


def righe_dei_separatori(rows: list[tuple[int, list[Any]]], separatori: list[dict[str, Any]],
                         gia_presenti: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows around section breaks that the profile doesn't already carry.

    Without them the mapping preview is blind exactly where it matters:
    it would show the leading rows and a couple of sample windows deep in
    the file, everything except the point where the real price list starts.
    Entering the data's first row would still show the promotional block
    above it, leaving the cut to be guessed.
    """

    coperte = {int(voce.get("row") or 0) for voce in gia_presenti}
    posizioni = {numero: posizione for posizione, (numero, _valori) in enumerate(rows)}
    righe: dict[int, list[Any]] = {}
    for separatore in separatori:
        posizione = posizioni[int(separatore["row"])]
        da = max(0, posizione - RIGHE_PRIMA_DEL_SEPARATORE)
        a = min(len(rows), posizione + RIGHE_DOPO_IL_SEPARATORE + 1)
        aggiunte = {numero: valori for numero, valori in rows[da:a]
                    if numero not in coperte and numero not in righe}
        if len(righe) + len(aggiunte) > RIGHE_DI_SEZIONE_AL_MASSIMO:
            break
        for numero, valori in aggiunte.items():
            righe[numero] = [display_value(valore) for valore in valori]
    return [{"row": numero, "values": righe[numero]} for numero in sorted(righe)]


def sample_rows(nonempty_rows: list[tuple[int, list[Any]]]) -> dict[str, list[dict[str, Any]]]:
    if not nonempty_rows:
        return {"initial": [], "middle": [], "final": []}

    def pack(items: Iterable[tuple[int, list[Any]]]) -> list[dict[str, Any]]:
        return [{"row": row, "values": [display_value(value) for value in values]} for row, values in items]

    middle = len(nonempty_rows) // 2
    return {
        "initial": pack(nonempty_rows[:SAMPLE_LIMIT]),
        "middle": pack(nonempty_rows[max(0, middle - 2): middle + 3]),
        "final": pack(nonempty_rows[-SAMPLE_LIMIT:]),
    }


def profile_xlsx(path: Path) -> dict[str, Any]:
    # Opened from the file's content, not its path: openpyxl refuses to open
    # a path ending in .xls even when the bytes are a real .xlsx, and a
    # renamed price list is a real case here.
    with path.open("rb") as stream:
        workbook = load_workbook(stream, read_only=False, data_only=False)
    sheet_profiles = []
    formule_per_foglio: dict[int, set[tuple[int, int]]] = {}
    try:
        for numero_foglio, sheet in enumerate(workbook.worksheets):
            column_stats: dict[int, dict[str, Any]] = {}
            nonempty_rows: list[tuple[int, list[Any]]] = []
            active_min_row = active_min_col = None
            active_max_row = active_max_col = 0
            formula_count = 0
            formule: set[tuple[int, int]] = set()

            for row in sheet.iter_rows():
                last_nonempty = 0
                values: list[Any] = []
                for cell in row:
                    value = cell.value
                    values.append(value)
                    kind = type_name(value, cell.data_type)
                    if kind == "blank":
                        continue
                    last_nonempty = cell.column
                    active_min_row = cell.row if active_min_row is None else min(active_min_row, cell.row)
                    active_min_col = cell.column if active_min_col is None else min(active_min_col, cell.column)
                    active_max_row = max(active_max_row, cell.row)
                    active_max_col = max(active_max_col, cell.column)
                    if kind == "formula":
                        formula_count += 1
                        formule.add((cell.row, cell.column))
                    stats = column_stats.setdefault(cell.column, {"nonempty": 0, "types": Counter(), "examples": []})
                    stats["nonempty"] += 1
                    stats["types"][kind] += 1
                    shown = display_value(value)
                    if shown not in stats["examples"] and len(stats["examples"]) < EXAMPLE_LIMIT:
                        stats["examples"].append(shown)
                if last_nonempty:
                    nonempty_rows.append((row[0].row, values[:last_nonempty]))

            columns = []
            for index in sorted(column_stats):
                stats = column_stats[index]
                columns.append({
                    "index": index,
                    "letter": get_column_letter(index),
                    "nonempty": stats["nonempty"],
                    "types": dict(stats["types"]),
                    "examples": stats["examples"],
                })

            candidati = header_candidates(nonempty_rows[:50])
            intestazione = prime_righe_non_vuote(nonempty_rows, candidati)
            separatori = separatori_di_sezione(nonempty_rows)
            if formule:
                formule_per_foglio[numero_foglio] = formule
            sheet_profiles.append({
                "name": sheet.title,
                "active_range": {
                    "min_row": active_min_row,
                    "min_column": active_min_col,
                    "max_row": active_max_row,
                    "max_column": active_max_col,
                    "nonempty_rows": len(nonempty_rows),
                },
                "formula_count": formula_count,
                "merged_ranges_count": len(sheet.merged_cells.ranges),
                "merged_ranges_sample": [str(item) for item in list(sheet.merged_cells.ranges)[:20]],
                "header_candidates": candidati,
                "header_rows": intestazione,
                "section_breaks": separatori,
                "section_rows": righe_dei_separatori(nonempty_rows, separatori, intestazione),
                "samples": sample_rows(nonempty_rows),
                "columns": columns,
            })
    finally:
        workbook.close()

    if formule_per_foglio:
        censisci_valori_delle_formule(path, sheet_profiles, formule_per_foglio)
    return {"format": "xlsx", "sheet_count": len(sheet_profiles), "sheets": sheet_profiles}


def censisci_valori_delle_formule(path: Path, sheet_profiles: list[dict[str, Any]],
                                  formule_per_foglio: dict[int, set[tuple[int, int]]]) -> None:
    """What a formula cell evaluates to, not just that it's a formula.

    A cell written as a formula is a price like any other: the reader opens
    the file with ``data_only=True`` and gets the computed number. The
    profile instead opens with ``data_only=False``, sees the formula text
    and counts it under the "formula" type — a price column made entirely
    of formulas would then look 0% numeric, fail the plausible-type check,
    and that supplier would be flagged as changed every single run, meaning
    a manual mapping forever. Two parts of the same program were looking at
    the same cell and reporting two different things about it.

    openpyxl only exposes the cached computed value by reopening the file,
    so it's read a second time — only when there are formulas, and read-only,
    in a single ``iter_rows`` pass: measured on the heaviest real price
    list, the second read adds +0.4 s. A document with no formulas pays
    nothing extra.

    The result goes into a separate key, `formula_values`: `types` still
    reports the formula count, which is real data nobody should lose.
    """

    for numero in formule_per_foglio:
        sheet_profiles[numero]["formula_values_read"] = False
    try:
        with path.open("rb") as stream:
            workbook = load_workbook(stream, read_only=True, data_only=True)
            try:
                fogli = workbook.worksheets
                for numero, formule in formule_per_foglio.items():
                    if numero >= len(fogli):
                        continue
                    ultima_riga = max(riga for riga, _colonna in formule)
                    ultima_colonna = max(colonna for _riga, colonna in formule)
                    conteggi: dict[int, Counter] = {}
                    # Rows are scanned once, with explicit bounds so the
                    # column index is correct: calling `sheet.cell(r, c)`
                    # inside a loop on a read-only sheet re-scans the sheet
                    # from the start on every single cell access — measured
                    # elsewhere in this project at 290 s vs 2.6 s.
                    for numero_riga, valori in enumerate(
                        fogli[numero].iter_rows(
                            min_row=1, max_row=ultima_riga,
                            min_col=1, max_col=ultima_colonna, values_only=True,
                        ),
                        start=1,
                    ):
                        for numero_colonna, valore in enumerate(valori, start=1):
                            if (numero_riga, numero_colonna) not in formule:
                                continue
                            conteggi.setdefault(numero_colonna, Counter())[type_name(valore)] += 1
                    for colonna in sheet_profiles[numero].get("columns") or []:
                        conto = conteggi.get(colonna.get("index"))
                        if conto:
                            colonna["formula_values"] = dict(conto)
                    sheet_profiles[numero]["formula_values_read"] = True
            finally:
                workbook.close()
    except Exception as exc:  # the profile stays usable, but says why
        # Without the cached value, formula columns show up as non-numeric
        # and the document gets flagged as changed — the cautious behavior,
        # but whoever reads the profile needs to know why, rather than
        # seeing a failed check with no visible cause.
        for numero in formule_per_foglio:
            sheet_profiles[numero]["formula_values_error"] = f"{type(exc).__name__}: {exc}"


def sheet_profile_from_grid(name: str, grid: list[list[tuple[Any, bool]]]) -> dict[str, Any]:
    """Profiles a sheet read from an .xls, where each cell is (value, bold).

    Bold is counted per column: some suppliers flag a discounted price with
    bold formatting rather than a separate column, so the profile needs to
    surface it too, not just the data.
    """
    column_stats: dict[int, dict[str, Any]] = {}
    nonempty_rows: list[tuple[int, list[Any]]] = []
    active_min_row = active_min_col = None
    active_max_row = active_max_col = 0

    for row_number, row in enumerate(grid, start=1):
        last_nonempty = 0
        values = [value for value, _bold in row]
        for column_number, (value, bold) in enumerate(row, start=1):
            kind = type_name(value)
            if kind == "blank":
                continue
            last_nonempty = column_number
            active_min_row = row_number if active_min_row is None else min(active_min_row, row_number)
            active_min_col = column_number if active_min_col is None else min(active_min_col, column_number)
            active_max_row = max(active_max_row, row_number)
            active_max_col = max(active_max_col, column_number)
            stats = column_stats.setdefault(column_number, {"nonempty": 0, "types": Counter(), "examples": [], "bold": 0})
            stats["nonempty"] += 1
            stats["types"][kind] += 1
            stats["bold"] += int(bold)
            shown = display_value(value)
            if shown not in stats["examples"] and len(stats["examples"]) < EXAMPLE_LIMIT:
                stats["examples"].append(shown)
        if last_nonempty:
            nonempty_rows.append((row_number, values[:last_nonempty]))

    columns = []
    for index in sorted(column_stats):
        stats = column_stats[index]
        columns.append({
            "index": index,
            "letter": get_column_letter(index),
            "nonempty": stats["nonempty"],
            "types": dict(stats["types"]),
            "bold": stats["bold"],
            "examples": stats["examples"],
        })

    candidati = header_candidates(nonempty_rows[:50])
    intestazione = prime_righe_non_vuote(nonempty_rows, candidati)
    separatori = separatori_di_sezione(nonempty_rows)
    return {
        "name": name,
        "active_range": {
            "min_row": active_min_row,
            "min_column": active_min_col,
            "max_row": active_max_row,
            "max_column": active_max_col,
            "nonempty_rows": len(nonempty_rows),
        },
        # For an .xls, only the value Excel already computed and stored is
        # read: a formula cell can't be told apart from a hand-typed one,
        # and merged-cell data isn't available. Reporting these as unknown
        # is better than writing a zero that looks like a real measurement.
        "formula_count": None,
        "merged_ranges_count": None,
        "values_only": True,
        "header_candidates": candidati,
        "header_rows": intestazione,
        "section_breaks": separatori,
        "section_rows": righe_dei_separatori(nonempty_rows, separatori, intestazione),
        "samples": sample_rows(nonempty_rows),
        "columns": columns,
    }


def profile_xls(path: Path) -> dict[str, Any]:
    """Profiles a legacy Excel 97-2003 file with the standard-library-only reader."""
    sheet_profiles = [sheet_profile_from_grid(sheet.name, sheet.rows) for sheet in read_workbook(path)]
    details = {
        "format": "xls",
        "sheet_count": len(sheet_profiles),
        # Whole-workbook summary: lets a reader get a single active-rows
        # figure without scanning sheet by sheet.
        "active_range": {
            "max_row": max((sheet["active_range"]["max_row"] for sheet in sheet_profiles), default=0),
            "max_column": max((sheet["active_range"]["max_column"] for sheet in sheet_profiles), default=0),
            "nonempty_rows": sum(sheet["active_range"]["nonempty_rows"] for sheet in sheet_profiles),
        },
        "sheets": sheet_profiles,
    }
    return details


def sniff_csv(path: Path) -> tuple[str, csv.Dialect]:
    raw = path.read_bytes()[:65536]
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = raw.decode(encoding)
            return encoding, csv.Sniffer().sniff(text, delimiters=",;\t|")
        except (UnicodeDecodeError, csv.Error):
            continue
    raise ValueError("Codifica o separatore CSV non riconosciuto")


# A CSV is all text: the type is inferred from how the value is written.
# Without this, a CSV's price column would show up as "text", and the
# registry's type check would reject it outright — a supplier sending a CSV
# could never enter the registry. Prices arrive written the Italian way
# (e.g. "21,75") and need to be recognized as numbers in that form too.
NUMERO_SCRITTO = re.compile(r"[+-]?(?:\d{1,3}(?:[.\s ]\d{3})+|\d+)(?:[.,]\d+)?")


def tipo_del_testo(valore: Any) -> str:
    """Reports whether this text is a written number — the only type a CSV has.

    Only describes the column in the profile. No conversion happens here:
    the reader that actually parses the price list converts values with its
    own rules.
    """

    testo = str(valore or "").strip()
    if not testo:
        return "blank"
    return "number" if NUMERO_SCRITTO.fullmatch(testo) else "text"


def profile_csv(path: Path) -> dict[str, Any]:
    encoding, dialect = sniff_csv(path)
    rows: list[tuple[int, list[Any]]] = []
    column_stats: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding=encoding, newline="") as stream:
        reader = csv.reader(stream, dialect)
        for row_number, row in enumerate(reader, start=1):
            if any(str(value).strip() for value in row):
                rows.append((row_number, row))
            for index, value in enumerate(row, start=1):
                if not str(value).strip():
                    continue
                stats = column_stats.setdefault(index, {"nonempty": 0, "types": Counter(), "examples": []})
                stats["nonempty"] += 1
                stats["types"][tipo_del_testo(value)] += 1
                shown = display_value(value)
                if shown not in stats["examples"] and len(stats["examples"]) < EXAMPLE_LIMIT:
                    stats["examples"].append(shown)

    columns = [{
        "index": index,
        "letter": get_column_letter(index),
        "nonempty": stats["nonempty"],
        "types": dict(stats["types"]),
        "examples": stats["examples"],
    } for index, stats in sorted(column_stats.items())]
    candidati = header_candidates(rows[:20])
    intestazione = prime_righe_non_vuote(rows, candidati)
    separatori = separatori_di_sezione(rows)
    # A CSV's profile is flat: it has no sheets, and the registry treats it
    # as having exactly one, unnamed. This way a CSV goes through the same
    # recognition engine, instead of its own logic that could quietly drift
    # from how spreadsheets are handled.
    return {
        "format": "csv",
        "encoding": encoding,
        "delimiter": dialect.delimiter,
        "active_range": {"max_row": rows[-1][0] if rows else 0, "max_column": max(column_stats, default=0), "nonempty_rows": len(rows)},
        "header_candidates": candidati,
        "header_rows": intestazione,
        "section_breaks": separatori,
        "section_rows": righe_dei_separatori(rows, separatori, intestazione),
        "samples": sample_rows(rows),
        "columns": columns,
    }


def profile_file(path: Path) -> dict[str, Any]:
    # The reader is chosen from the file's content, not its name: a renamed
    # price list shouldn't produce an error the user can't make sense of.
    container = container_format(path)
    if container == "xls":
        details = profile_xls(path)
    elif container == "xlsx":
        details = profile_xlsx(path)
    else:
        details = profile_csv(path)
    # Which supplier a document belongs to is decided by the registry, not
    # by this file: there's no hardcoded per-supplier detection here. A
    # supplier changing its price list format is handled by updating
    # references/adapters.json, the only thing an unattended run can do.
    hint = registro.riconosci(details)
    digest = file_hash(path)
    return {
        "profile_id": digest[:16],
        "path": str(path.resolve()),
        "file_name": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
        "declared_suffix": path.suffix.casefold(),
        "content_format": container,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "details": details,
        "deterministic_hint": hint,
        "ai_preflight": {
            "state": None,
            "role": None,
            "supplier_id": None,
            "adapter_id": None,
            "confidence": None,
            "rationale": None,
            "field_mapping": None,
        },
        "user_confirmation": {"required": None, "status": "PENDING"},
    }


def candidate_files(paths: list[Path], recursive: bool) -> list[Path]:
    found: set[Path] = set()
    for candidate in paths:
        if candidate.is_file() and candidate.suffix.casefold() in SUPPORTED_SUFFIXES:
            found.add(candidate.resolve())
        elif candidate.is_dir():
            iterator = candidate.rglob("*") if recursive else candidate.glob("*")
            found.update(path.resolve() for path in iterator if path.is_file() and path.suffix.casefold() in SUPPORTED_SUFFIXES)
    return sorted(found, key=lambda item: str(item).casefold())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="File o cartelle candidate")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recursive", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    candidates = candidate_files(args.inputs, args.recursive)
    if not candidates:
        raise SystemExit("Nessun file XLSX/CSV candidato trovato")
    profiles = []
    errors = []
    for path in candidates:
        try:
            profiles.append(profile_file(path))
        except Exception as exc:  # one bad file doesn't hide the rest: every error is collected
            errors.append({"path": str(path.resolve()), "error": f"{type(exc).__name__}: {exc}"})

    # A document the program didn't recognize, or flagged as changed, gets
    # counted here: nobody will read the per-file states one by one inside
    # the manifest, and "10 profiled, 0 errors" would otherwise read as a
    # full success even with several documents still needing review.
    stati = Counter(str((profilo.get("deterministic_hint") or {}).get("state") or "AMBIGUO")
                    for profilo in profiles)
    da_guardare = sum(stati[stato] for stato in ("AMBIGUO", "SCHEMA_VARIATO"))

    result = {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "input_count": len(candidates),
        "profiled_count": len(profiles),
        "states": dict(sorted(stati.items())),
        "profiles": profiles,
        "errors": errors,
        "ai_preflight_status": "PENDING",
        "instruction": "L'AI deve riesaminare profili e hint, compilare ai_preflight e salvare input_manifest.json prima dei parser.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # The manifest is written with LF line endings, like the rest of the
    # project: `write_text` on Windows would otherwise convert them to CRLF.
    with args.output.open("w", encoding="utf-8", newline="\n") as flusso:
        json.dump(result, flusso, ensure_ascii=False, indent=2)
    riepilogo = {
        "output": str(args.output.resolve()),
        "profiled": len(profiles),
        "errors": len(errors),
        "states": dict(sorted(stati.items())),
    }
    if da_guardare:
        nomi = [profilo.get("file_name") for profilo in profiles
                if str((profilo.get("deterministic_hint") or {}).get("state") or "AMBIGUO")
                in ("AMBIGUO", "SCHEMA_VARIATO")]
        riepilogo["attention"] = (
            f"{da_guardare} documenti su {len(profiles)} restano da interpretare: "
            + ", ".join(str(nome) for nome in nomi)
        )
    print(json.dumps(riepilogo, ensure_ascii=False, indent=2))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
