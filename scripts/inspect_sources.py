#!/usr/bin/env python3
"""Profile candidate XLSX/XLS/CSV inputs without changing them.

The output is intentionally descriptive.  A Codex/LLM preflight must review the
deterministic hints and complete ``ai_preflight`` before parsing a run.

Il formato lo decidono i primi byte del file, non l'estensione: i .xls di
Noce passano dal lettore Excel 97-2003 di ``app/xls_reader.py``, i .xlsx da
openpyxl, tutto il resto dal lettore CSV.
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

# Il lettore dei file Excel 97-2003 sta nella cartella dell'applicazione e usa
# soltanto la libreria standard: un listino .xls si apre senza installare
# niente sul computer dell'utente.  Il registro degli schemi sta invece qui
# accanto, e la sua cartella si aggiunge lo stesso: chi importa questo modulo
# ce l'ha gia' in cammino, ma dipenderne in silenzio vorrebbe dire un errore di
# importazione al primo consumatore che se ne dimentica.
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

# I primi byte di un file dicono che cosa e' davvero.  L'estensione no: un
# listino puo' arrivare rinominato, e aprire un .xls con il lettore dei .xlsx
# (o viceversa) darebbe all'utente un errore incomprensibile.
FIRMA_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # Excel 97-2003
FIRMA_ZIP = b"PK\x03\x04"  # Excel 2007 e successivi: e' un archivio


def container_format(path: Path) -> str:
    """Dice con quale lettore va aperto il file, guardando i suoi primi byte."""
    with path.open("rb") as stream:
        testa = stream.read(8)
    if testa.startswith(FIRMA_OLE2):
        return "xls"
    if testa.startswith(FIRMA_ZIP):
        return "xlsx"
    return "csv"


def normalized(value: Any) -> str:
    """Il token normalizzato di un'intestazione, come lo calcola il registro.

    Il nome resta perche' lo usa il resto del modulo, ma il calcolo e' uno
    solo: due implementazioni che si allontanano di un carattere vorrebbero
    dire un listino riconosciuto qui e non ritrovato nel registro, e nessuno
    saprebbe perche'.
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
    """Le righe che somigliano a un'intestazione secondo un elenco di parole note.

    Resta perche' e' quello che una persona legge nel manifest per capire a
    colpo d'occhio dov'e' l'intestazione e quanto ci somiglia.  Il
    riconoscimento pero' non passa piu' di qui: l'elenco qui sotto descrive i
    fornitori di oggi, e un fornitore nuovo non ci comparirebbe.  Quello che il
    registro legge sono le righe di `prime_righe_non_vuote`.
    """

    keywords = {
        "ean", "codice", "codart", "descrizione", "descrcommerciale", "colli",
        "quantita", "prezzo", "sconto", "iva", "totale", "totali", "ordine",
        "pzct", "cessione", "product", "packaging", "availability", "unit",
        # Intestazioni del listino Noce in formato Excel 97-2003.
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


# Quante righe si consegnano al registro perche' ci cerchi l'intestazione.  Le
# intestazioni misurate sui listini veri stanno fra la riga 1 e la riga 6:
# venti righe lasciano margine a un fornitore piu' prolisso senza appesantire
# il manifest, che una persona deve poter ancora leggere.
RIGHE_PER_IL_REGISTRO = 20


def prime_righe_non_vuote(rows: list[tuple[int, list[Any]]],
                          candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Le righe che il registro puo' leggere per cercarci un'intestazione.

    Sono le prime righe con del contenuto, senza sceglierne nessuna: e' l'unico
    modo perche' il registro veda l'intestazione di un fornitore che nessuno ha
    ancora censito.  `header_candidates` scarta le righe che non contengono una
    parola del suo elenco, e quell'elenco l'ha scritto chi guardava i fornitori
    di oggi; senza righe grezze un listino nuovo non arriverebbe mai al motore
    e la Fase 5 non avrebbe niente da imparare.

    Le righe gia' riconosciute da `header_candidates` si aggiungono anche
    quando cadono piu' in basso della ventesima, perche' prima di questa fase
    erano le uniche che il riconoscimento guardava e arrivano fino alla
    cinquantesima: un fornitore con un preambolo lungo si riconosceva ieri e
    deve continuare a riconoscersi oggi.
    """

    righe = {
        row_number: [display_value(value) for value in values]
        for row_number, values in rows[:RIGHE_PER_IL_REGISTRO]
    }
    for candidato in candidates:
        righe.setdefault(candidato["row"], candidato["values"])
    return [{"row": numero, "values": righe[numero]} for numero in sorted(righe)]


# Un separatore di sezione: una riga con pochissime celle piene in mezzo a
# righe piene.  E' cosi' che un listino scrive «da qui comincia un'altra cosa»
# — `A68 = 'LISTINO'` su QUERCIA, dopo 56 righe di prezzi che sono valorizzazioni
# di omaggi e non prezzi d'acquisto.  Le soglie sono misurate sui listini veri:
# i separatori hanno una o due celle piene, le righe di prodotto ne hanno da
# otto in su, e quattro e' un margine largo per un listino piu' povero.
CELLE_DI_UN_SEPARATORE = 2
CELLE_DI_UNA_RIGA_PIENA = 4
# Solo in testa al documento: piu' in basso una riga stretta e' un'etichetta di
# gruppo dentro i dati, non l'inizio dei dati — il listino LARICE ne ha 625, e
# nessuna dice dove comincia il listino.  Cento righe lasciano margine al
# blocco promozionale piu' lungo misurato (QUERCIA: 61 righe).
SEPARATORI_IN_TESTA = 100
# Le righe attorno a un separatore che il profilo si porta dietro: due prima
# per far vedere che cosa finisce, sei dopo per far vedere che cosa comincia.
RIGHE_PRIMA_DEL_SEPARATORE = 2
RIGHE_DOPO_IL_SEPARATORE = 6
# Il tetto delle righe in piu': un documento che alterna sezioni ogni tre righe
# non deve gonfiare il profilo, che una persona deve poter ancora leggere.
RIGHE_DI_SEZIONE_AL_MASSIMO = 24


def _cella_vuota(valore: Any) -> bool:
    return valore is None or (isinstance(valore, str) and valore.strip() == "")


def separatori_di_sezione(rows: list[tuple[int, list[Any]]]) -> list[dict[str, Any]]:
    """Le righe che dichiarano l'inizio di una sezione, con che cosa c'e' scritto.

    Servono a chi mappa un fornitore nuovo: senza, «i dati cominciano alla riga
    69» e' un numero che si puo' soltanto indovinare aprendo Excel, e la
    settimana dopo sara' un altro numero.  Con questo elenco la pagina puo'
    proporre «i prodotti cominciano dopo la riga 68 («LISTINO»)», che e' una
    regola e non un numero.
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
            # Corto: e' un'etichetta da riconoscere in un elenco, non il testo
            # da leggere — quello sta per intero nelle righe qui accanto.
            "text": display_value(valori[indice - 1], limit=80),
            # Dove comincerebbero i dati tagliando qui: e' il numero che la
            # pagina mostra accanto alla proposta, e quello che il marcatore
            # ricalcolera' da solo la settimana prossima.
            "data_from": rows[posizione + 1][0] if posizione + 1 < len(rows) else numero + 1,
        })
    return trovati


def righe_dei_separatori(rows: list[tuple[int, list[Any]]], separatori: list[dict[str, Any]],
                         gia_presenti: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Le righe attorno ai separatori, quelle che il profilo non porta gia'.

    Senza di loro l'anteprima della mappatura e' cieca proprio dove serve: su
    QUERCIA mandava le righe 1-20, 2094-2098 e 4180-4190, cioe' tutto tranne il
    punto in cui il listino vero comincia.  Chi scriveva 69 in «Prima riga dei
    prodotti» continuava a vedere le righe 3-20 — il blocco promozionale — e il
    taglio restava da indovinare.
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
    # Il file si apre passando il contenuto e non il nome: openpyxl si rifiuta
    # di aprire un percorso che finisce per .xls anche quando dentro c'e' un
    # vero .xlsx, e un listino rinominato e' un caso reale.
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
    """Che cosa **vale** una formula, non soltanto che e' una formula.

    Una cella scritta ``=SUM(E4*(1-5%))`` e' un prezzo come tutte le altre: il
    lettore la apre con ``data_only=True`` e ci trova 1,52.  Il profilo invece
    la apre con ``data_only=False``, ci trova il testo della formula e la conta
    fra i tipi come «formula»: la colonna dei prezzi di GINEPRO risultava **0%
    numerica** su 4132 celle, `tipi_plausibili` la bocciava e quel fornitore
    sarebbe tornato SCHEMA_VARIATO ogni settimana, cioe' una mappatura a mano
    per sempre.  Due parti dello stesso programma guardavano la stessa cella e
    ne dicevano due cose diverse.

    Il valore in cache openpyxl lo espone solo riaprendo il documento, quindi
    il file si legge una seconda volta — **soltanto quando ci sono formule**, e
    in sola lettura, con una passata unica di ``iter_rows``: la seconda lettura
    costa quanto la prima o meno (misurato: +0,4 s su QUERCIA, il piu' pesante dei
    listini veri).  Un documento senza formule non paga niente.

    Il conto finisce in una chiave a parte, `formula_values`: `types` continua
    a dire quante formule c'e', che e' un dato vero e che nessuno deve perdere.
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
                    # Le righe si scorrono una volta sola, con i limiti espliciti
                    # perche' l'indice della colonna sia quello vero: chiamare
                    # `foglio.cell(r, c)` dentro un ciclo su un foglio aperto in
                    # sola lettura rilegge il foglio dall'inizio a ogni cella
                    # (misurato altrove nel progetto: 290 s contro 2,6 s).
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
    except Exception as exc:  # il profilo resta utilizzabile, ma lo dice
        # Senza il valore in cache le colonne calcolate risultano non numeriche
        # e il documento viene declassato: e' il comportamento prudente, ma chi
        # legge il profilo deve sapere perche', invece di vedere una verifica
        # rossa senza causa.
        for numero in formule_per_foglio:
            sheet_profiles[numero]["formula_values_error"] = f"{type(exc).__name__}: {exc}"


def sheet_profile_from_grid(name: str, grid: list[list[tuple[Any, bool]]]) -> dict[str, Any]:
    """Profila un foglio letto da un .xls, dove ogni cella e' (valore, grassetto).

    Il grassetto viene contato colonna per colonna: sui listini Noce il
    prezzo in offerta e' segnalato anche cosi' («i prezzi offerta sono in
    grassetto»), quindi va visto anche nel profilo e non solo nei dati.
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
        # Di un .xls si legge il valore gia' calcolato che Excel ha memorizzato:
        # una cella con formula non si distingue da una scritta a mano, e le
        # celle unite non sono disponibili.  Dichiararlo e' meglio che scrivere
        # uno zero che sembra una misura.
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
    """Profila un Excel 97-2003 con il lettore di sola libreria standard."""
    sheet_profiles = [sheet_profile_from_grid(sheet.name, sheet.rows) for sheet in read_workbook(path)]
    details = {
        "format": "xls",
        "sheet_count": len(sheet_profiles),
        # Riepilogo di tutto il libro: serve a chi legge un solo numero di
        # righe attive senza scorrere foglio per foglio.
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


# In un CSV tutto e' testo: il tipo lo decide come e' scritto il valore.  Senza
# questo, il profilo di un CSV dichiarava «testo» anche la colonna dei prezzi,
# e la verifica dei tipi del registro non poteva che bocciarla: un fornitore
# che manda un CSV non sarebbe mai potuto entrare nel registro.  I prezzi
# arrivano scritti all'italiana — «21,75» — e vanno riconosciuti anche cosi'.
NUMERO_SCRITTO = re.compile(r"[+-]?(?:\d{1,3}(?:[.\s ]\d{3})+|\d+)(?:[.,]\d+)?")


def tipo_del_testo(valore: Any) -> str:
    """Se quel testo e' un numero scritto, dirlo: e' l'unico tipo che un CSV ha.

    Serve solo a descrivere la colonna nel profilo.  Nessun calcolo passa di
    qui: i valori li converte chi legge il listino, con le sue regole.
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
    # Il profilo di un CSV e' piatto: non ha fogli, e il registro lo tratta
    # come se ne avesse uno solo senza nome.  Cosi' anche il CSV passa dallo
    # stesso motore, invece di avere un riconoscimento tutto suo che puo'
    # allontanarsi da quello dei fogli di calcolo senza che nessuno se ne
    # accorga.
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
    # Il lettore lo sceglie il contenuto del file, non il suo nome: un listino
    # rinominato non deve far uscire un errore che l'utente non sa leggere.
    container = container_format(path)
    if container == "xls":
        details = profile_xls(path)
    elif container == "xlsx":
        details = profile_xlsx(path)
    else:
        details = profile_csv(path)
    # Di che fornitore sia il documento lo dice il registro, non questo file.
    # Qui non c'e' piu' nessuna intestazione scritta a mano: un fornitore che
    # cambia listino si segue aggiornando references/adapters.json, che e'
    # l'unica cosa che il programma potra' fare quando girera' da solo.
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
        except Exception as exc:  # keep the complete inventory for AI error handling
            errors.append({"path": str(path.resolve()), "error": f"{type(exc).__name__}: {exc}"})

    # Un documento che il programma non ha riconosciuto, o che ha declassato,
    # va contato qui: nessuno andra' a leggere gli stati uno per uno dentro il
    # manifest, e «10 profilati, 0 errori» su quattro listini da interpretare
    # si legge come un successo pieno.
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
    # Il manifest si scrive a fine riga LF come tutto il resto del progetto:
    # `write_text` su Windows lo convertirebbe in CRLF da solo.
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
