#!/usr/bin/env python3
"""Parse an AI-reviewed manifest through known or declarative adapters.

Run ``validate_input_manifest.py`` first.  Known unchanged schemas reuse the
tested readers in ``prepare_sources.py``; varied/new schemas require an explicit
field mapping in the manifest and are parsed generically.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

# Il lettore dei .xls e' nella cartella dell'applicazione e usa soltanto la
# libreria standard.
APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from xls_reader import read_workbook  # noqa: E402

import registro
from inspect_sources import container_format
from prepare_sources import (
    FUORI_DAL_FILTRO,
    MOTIVO_NON_DICHIARATO,
    NON_DISPONIBILE,
    NON_E_RIGA_PRODOTTO,
    SENZA_DESCRIZIONE,
    SENZA_PEZZI_PER_COLLO,
    SENZA_PREZZO,
    build_matching,
    conta_non_ordinabili,
    decimal_value,
    fattore_d_ordine,
    larice_discount,
    indice_di_colonna,
    json_decimal,
    normalize_ean,
    read_betulla,
    read_gestionale,
    read_larice,
    read_noce,
    integrate_larice_displays,
    write_json,
)
from detect_displays import analyse_workbook
# La forma del marcatore la verifica il validatore del manifest, non una
# seconda copia scritta qui: chi legge e chi valida devono dire la stessa cosa.
from validate_input_manifest import errori_del_marcatore


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def column_number(value: Any, header: list[Any] | None = None) -> int:
    """Traduce la colonna dichiarata nella mappatura in un numero di colonna.

    Quando il foglio ha una riga di intestazione, il nome dichiarato si cerca
    li' dentro e basta: **niente ripiego sulle lettere**.  «cat» e «Iva» sono
    nomi di colonna veri del listino Noce e insieme riferimenti di colonna
    Excel validi (CAT = 2074, IVA = 6657): con il ripiego, un fornitore che
    rinomina una colonna non produrrebbe un errore ma la lettura silenziosa di
    una colonna vuota, e le 8.292 righe FOOD entrerebbero nel confronto senza
    che nessuno se ne accorga.  Fermarsi dicendo che cosa manca costa un
    messaggio; leggere la colonna sbagliata costa un ordine.

    Le lettere restano ammesse solo dove non c'e' un'intestazione da leggere.
    """
    if isinstance(value, int) and value >= 1:
        return value
    text = str(value or "").strip()
    if header is not None:
        wanted = normalized(text)
        matches = [index for index, item in enumerate(header, start=1) if normalized(item) == wanted]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Intestazione duplicata nella mappatura: {text}")
        letti = [str(item).strip() for item in header if str(item or "").strip()]
        elenco = ", ".join(f"«{nome}»" for nome in letti[:20]) or "nessuna"
        if len(letti) > 20:
            elenco += f" e altre {len(letti) - 20}"
        raise ValueError(
            f"Colonna «{text}» non trovata fra le intestazioni dichiarate: il documento non ha "
            f"la forma attesa e non viene letto a metà. Intestazioni lette: {elenco}. "
            "Per indicare una colonna che non ha un nome si dichiara il suo numero (per esempio 5), "
            "non la lettera."
        )
    if re.fullmatch(r"[A-Za-z]+", text):
        return column_index_from_string(text.upper())
    raise ValueError(f"Colonna non risolta: {value!r}")


def mapped_value(row: tuple[Any, ...] | list[Any], columns: dict[str, int], field: str) -> Any:
    index = columns.get(field)
    return row[index - 1] if index and index <= len(row) else None


def _testo_confrontabile(valore: Any) -> str:
    """Il testo di una cella ridotto a cio' che si puo' confrontare.

    Spazi multipli e maiuscole non fanno un'altra riga: un fornitore che scrive
    «LISTINO » con uno spazio in coda manda lo stesso documento.
    """

    return " ".join(str(valore if valore is not None else "").split()).casefold()


def colonna_del_marcatore(spec: Any) -> int:
    """La colonna in cui cercare il marcatore: un numero, oppure una lettera.

    Qui le lettere sono ammesse — al contrario di `column_number`, dove sono
    vietate perche' «cat» e «Iva» sono nomi di colonna veri di un listino e
    insieme riferimenti Excel validi.  Il marcatore non nomina un campo dei
    dati: sta **fuori** dalla tabella, in una riga che non ha intestazione, e
    l'unico modo di indicarlo e' la posizione.
    """

    if isinstance(spec, bool):
        raise ValueError("data_start_marker.column deve essere una lettera di colonna o il suo numero")
    if isinstance(spec, int) and spec >= 1:
        return spec
    testo = str(spec or "").strip()
    if re.fullmatch(r"[1-9][0-9]*", testo):
        return int(testo)
    if re.fullmatch(r"[A-Za-z]{1,3}", testo):
        return column_index_from_string(testo.upper())
    raise ValueError(
        f"data_start_marker.column: «{spec}» non è una colonna. Indicare la lettera "
        "(per esempio «A») oppure il numero della colonna (per esempio 1)."
    )


def riga_dopo_il_marcatore(rows: list[tuple[Any, ...]], marker: dict[str, Any], nome_file: str) -> int:
    """Dove cominciano i prodotti secondo il marcatore, ricalcolato su QUESTO file.

    ⚠ **Non esiste un ripiego.**  Se il marcatore non si trova, o si trova piu'
    di una volta, la lettura si ferma e dice che cosa cercava e dove.
    Ripiegare sul numero di riga della settimana scorsa e' esattamente il
    difetto che questa chiave chiude: su QUERCIA le righe 7-67 sono un blocco
    promozionale con prezzi che sono valorizzazioni di omaggi, la settimana
    prossima saranno di piu' o di meno, e un «69» congelato porterebbe dentro
    l'ordine righe fantasma — o ne perderebbe di vere — senza una parola.
    """

    problemi = errori_del_marcatore(marker)
    if problemi:
        raise ValueError(f"In «{nome_file}» la regola dell'inizio dei dati non è scritta bene: "
                         + "; ".join(problemi))
    indice = colonna_del_marcatore(marker.get("column"))
    lettera = get_column_letter(indice)
    esatto = str(marker.get("equals") or "").strip()
    contenuto = str(marker.get("contains") or "").strip()
    cercato = esatto or contenuto
    atteso = _testo_confrontabile(cercato)
    scarto = int(marker.get("offset", 1))
    come = "è esattamente" if esatto else "contiene"

    trovate = [
        numero
        for numero, riga in enumerate(rows, start=1)
        if indice <= len(riga) and (
            _testo_confrontabile(riga[indice - 1]) == atteso if esatto
            else atteso in _testo_confrontabile(riga[indice - 1])
        )
    ]
    dove = f"in «{nome_file}», colonna {lettera}"
    if not trovate:
        raise ValueError(
            f"Non trovo dove comincia il listino: {dove} nessuna riga {come} «{cercato}». "
            f"La configurazione di questo fornitore dice che i prodotti cominciano {scarto} "
            f"riga/e dopo quella riga. Il documento non viene letto a metà: un numero di riga "
            f"deciso la volta scorsa taglierebbe l'elenco nel punto sbagliato. Apri «{nome_file}» e "
            f"controlla se quella scritta c'è ancora; se il fornitore l'ha tolta o cambiata, togli il "
            f"documento dal passo 1 «Importa i dati» e ricaricalo corretto."
        )
    if len(trovate) > 1:
        elenco = ", ".join(str(numero) for numero in trovate[:10])
        coda = f" e altre {len(trovate) - 10}" if len(trovate) > 10 else ""
        raise ValueError(
            f"Non so dove comincia il listino: {dove} ci sono {len(trovate)} righe in cui il "
            f"testo {come} «{cercato}» (righe {elenco}{coda}). Il punto in cui cominciano i "
            f"prodotti dev'essere uno solo: serve una scritta che compaia una volta sola, "
            f"oppure la riga esatta. Apri «{nome_file}»: se sono due pagine incollate una sotto "
            f"l'altra, togli la seconda intestazione e ricaricalo dal passo 1 «Importa i dati»."
        )
    riga = trovate[0] + scarto
    if riga > len(rows):
        raise ValueError(
            f"{dove} la riga «{cercato}» è la {trovate[0]}, e {scarto} riga/e più in basso "
            f"il documento è già finito: ha {len(rows)} righe. Dopo quella scritta non c'è "
            "nessun prodotto da leggere."
        )
    return riga


def prima_riga_dei_dati(rows: list[tuple[Any, ...]], mapping: dict[str, Any],
                        header_row: int, nome_file: str) -> int:
    """Da quale riga cominciano i prodotti: la regola vince sul numero.

    Quando la mappatura dichiara un `data_start_marker` si cerca quello, ogni
    volta, su questo documento.  `data_start_row` resta scritto perche' serve a
    chi **scrive** la copia dell'ordine, ma qui non lo si guarda nemmeno: un
    ripiego silenzioso sul numero di ieri e' il difetto da cui nasce questa
    funzione.
    """

    marker = mapping.get("data_start_marker")
    if marker not in (None, "", {}):
        return riga_dopo_il_marcatore(rows, marker, nome_file)
    return int(mapping.get("data_start_row") or (header_row + 1 if header_row else 1))


def selected_sheet(workbook: Any, mapping: dict[str, Any]) -> Any:
    selector = mapping.get("sheet")
    if selector in (None, "", "FIRST"):
        return workbook.worksheets[0]
    if isinstance(selector, int):
        if selector < 0 or selector >= len(workbook.worksheets):
            raise ValueError(f"Indice foglio non valido: {selector}")
        return workbook.worksheets[selector]
    if str(selector) not in workbook.sheetnames:
        raise ValueError(f"Foglio non trovato: {selector}")
    return workbook[str(selector)]


def selected_xls_sheet(path: Path, mapping: dict[str, Any]) -> list[list[tuple[Any, bool]]]:
    """Sceglie il foglio dentro un .xls con le stesse regole di selected_sheet."""
    sheets = read_workbook(path)
    if not sheets:
        raise ValueError(f"Il file {path.name} non contiene fogli di dati")
    selector = mapping.get("sheet")
    if selector in (None, "", "FIRST"):
        return sheets[0].rows
    if isinstance(selector, int):
        if selector < 0 or selector >= len(sheets):
            raise ValueError(f"Indice foglio non valido: {selector}")
        return sheets[selector].rows
    for sheet in sheets:
        if sheet.name == str(selector):
            return sheet.rows
    raise ValueError(f"Foglio non trovato: {selector}")


def mapped_rows(path: Path, mapping: dict[str, Any]) -> tuple[int, dict[str, int], list[tuple[Any, ...]], list[tuple[bool, ...]]]:
    """Legge la griglia della fonte scegliendo il lettore dai byte del file.

    L'estensione non decide niente: un .xls rinominato .xlsx resta un .xls.
    Di ogni cella si tiene anche il grassetto, perche' su un listino Noce
    il prezzo in offerta e' segnalato pure cosi' («i prezzi offerta sono in
    grassetto»): si legge da entrambi i formati, altrimenti basterebbe
    risalvare il listino in .xlsx per perdere il segnale senza un avviso.
    Dal .xls arriva gratis insieme ai valori; da un .xlsx costa dal 5 al 45%
    di tempo in piu', quindi li' si legge soltanto quando la mappatura dice
    che quel segnale le serve (`offer_from_bold`).
    """
    if container_format(path) == "xls":
        griglia = selected_xls_sheet(path, mapping)
        rows: list[tuple[Any, ...]] = [tuple(valore for valore, _grassetto in riga) for riga in griglia]
        bold: list[tuple[bool, ...]] = [tuple(grassetto for _valore, grassetto in riga) for riga in griglia]
    else:
        # Si passa il contenuto e non il nome: openpyxl rifiuta un percorso che
        # finisce per .xls anche quando dentro c'e' davvero un .xlsx.
        with path.open("rb") as stream:
            workbook = load_workbook(stream, read_only=True, data_only=True)
            try:
                sheet = selected_sheet(workbook, mapping)
                if mapping.get("offer_from_bold"):
                    rows = []
                    bold = []
                    for riga in sheet.iter_rows():
                        rows.append(tuple(cella.value for cella in riga))
                        # Le celle mai scritte non hanno nemmeno il carattere:
                        # getattr le fa valere «non in grassetto» invece di far
                        # cadere la lettura di un listino intero.
                        bold.append(tuple(
                            bool(getattr(getattr(cella, "font", None), "bold", False))
                            for cella in riga
                        ))
                else:
                    rows = list(sheet.iter_rows(values_only=True))
                    bold = []
            finally:
                workbook.close()
    header_row = int(mapping.get("header_row") or 0)
    if header_row < 0:
        raise ValueError(
            f"La mappatura dichiara la riga di intestazione {header_row}: deve essere un numero "
            "di riga a partire da 1, oppure 0 se il documento non ha intestazioni."
        )
    if header_row > len(rows):
        # Senza questo controllo uscirebbe «list index out of range», che non
        # dice a nessuno che cosa fare.
        raise ValueError(
            f"Il foglio «{mapping.get('sheet') or 'primo foglio'}» di {path.name} non arriva alla riga "
            f"{header_row}, dove dovrebbero esserci le intestazioni: il file non ha la forma attesa."
        )
    header = list(rows[header_row - 1]) if header_row else None
    raw_columns = mapping.get("columns") or mapping.get("field_mapping") or {}
    columns = {field: column_number(spec, header) for field, spec in raw_columns.items() if spec not in (None, "")}
    data_start = prima_riga_dei_dati(rows, mapping, header_row, path.name)
    return data_start, columns, rows, bold


def last_data_row(rows: list[tuple[Any, ...]], columns: dict[str, int], mapping: dict[str, Any], data_start: int) -> int:
    """Dove finiscono davvero i dati: lo dichiara il file, non il codice.

    Il listino Noce prosegue per centinaia di righe oltre l'ultimo
    prodotto: sono righe che contengono soltanto la formula della colonna
    Importo, lasciata li' da Excel.  Senza un confine diventerebbero prodotti
    fantasma.  La regola dice quali campi identificano un prodotto vero;
    l'ultima riga che ne ha almeno uno e' la fine dei dati.
    """
    fields = (mapping.get("data_end_rule") or {}).get("last_row_with_any")
    if not fields:
        return len(rows)
    for row_number in range(len(rows), data_start - 1, -1):
        row = rows[row_number - 1]
        if any(str(mapped_value(row, columns, field) or "").strip() for field in fields):
            return row_number
    return data_start - 1


def excluded_row_label(row: tuple[Any, ...], columns: dict[str, int], mapping: dict[str, Any]) -> str | None:
    """Righe che il confronto non deve nemmeno vedere, con il motivo dichiarato.

    Per Noce sono le righe alimentari: l'utente non tratta il FOOD, e
    tenerle vorrebbe dire proporgli ottomila articoli che non compra.  Quante
    ne vengono tolte finisce nell'audit: uno scarto silenzioso non e' una
    scelta, e' una perdita di dati.
    """
    for rule in mapping.get("exclude_rows") or []:
        actual = str(mapped_value(row, columns, rule.get("field")) or "").strip()
        if "equals" in rule:
            if actual.casefold() == str(rule["equals"]).strip().casefold():
                return str(rule.get("label") or rule["equals"])
        elif "regex" in rule:
            if re.search(str(rule["regex"]), actual, flags=re.IGNORECASE):
                return str(rule.get("label") or rule["regex"])
        else:
            raise ValueError("exclude_rows deve usare equals o regex")
    return None


def non_e_una_riga_prodotto(row: tuple[Any, ...], columns: dict[str, int], mapping: dict[str, Any]) -> bool:
    """Vero quando sulla riga non c'e' niente che sia merce.

    Niente nome, niente codice a barre e nessun prezzo: non e' un prodotto
    letto male, e' un'altra cosa scritta dentro la tabella — il titolo di una
    sezione, la riga che separa un blocco promozionale dal listino, una nota
    del fornitore.  Il lettore del gestionale scarta queste righe da sempre
    (`read_mapped_master`, stessa etichetta `NON_E_RIGA_PRODOTTO`); quello dei
    fornitori no, e le faceva entrare come prodotti non ordinabili.

    ⚠ **Misurato sul listino QUERCIA vero il 17 agosto 2026**: le sei righe
    separatore (10, 12, 14, 16, 18, 68) hanno descrizione vuota, EAN vuoto e
    prezzo assente, ma il loro testo sta nella colonna «Articolo», cioe' nel
    `supplier_code` — e la vecchia guardia di `supplier_record`, che pretende
    vuoti tutti e tre *compreso* il codice, non scattava.  Entravano fra i
    prodotti letti: 4185 invece di 4179.  Sopra il taglio del listino fa poco
    danno; un separatore **dentro** i prodotti diventa una voce del catalogo
    con la scritta promozionale al posto del codice articolo.

    Il codice del fornitore di proposito **non** conta come merce: da solo non
    permette di ordinare (senza nome la riga e' gia' scartata), non permette di
    abbinare e non ha un prezzo.  E' precisamente la cella in cui i separatori
    scrivono.

    Un prezzo si considera presente se la cella e' valorizzata, anche se il
    valore poi non si legge: un prezzo scritto male e' una riga da segnalare
    (`senza_prezzo`), non una riga che non esiste.
    """

    if str(mapped_value(row, columns, "description") or "").strip():
        return False
    if normalize_ean(mapped_value(row, columns, "ean")):
        return False
    for campo in ("unit_price_net", "unit_price_pre_discount"):
        valore = mapped_value(row, columns, campo)
        if valore is not None and str(valore).strip():
            return False
    return True


def expiry_from_description(description: str, mapping: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Stacca dalla descrizione la scadenza scritta come frammento HTML.

    Noce scrive «PRODOTTO ML.500<br> Scadenza 30/08/2026»: quel pezzo non
    fa parte del nome dell'articolo e sporcherebbe il confronto fra
    descrizioni, quindi si legge a parte e la descrizione resta pulita.
    La data pero' non si crede sulla parola: nel listino vero ce n'e' una al
    2057.  Una data assurda, o impossibile nel calendario, diventa un avviso e
    non un errore che ferma la lettura di diciassettemila righe.
    """
    rule = mapping.get("expiry_from_description")
    if not rule:
        return description, None
    pattern = re.compile(str(rule.get("regex") or ""), re.IGNORECASE)
    if not {"giorno", "mese", "anno"} <= set(pattern.groupindex):
        raise ValueError("expiry_from_description: la regola deve nominare i gruppi giorno, mese e anno")
    match = pattern.search(description or "")
    if not match:
        return description, {"expiry_raw": None, "expiry_date": None, "expiry_plausible": None, "expiry_warning": None}

    cleaned = (description[: match.start()] + description[match.end():]).strip()
    raw = match.group(0).strip()
    try:
        expiry = date(int(match.group("anno")), int(match.group("mese")), int(match.group("giorno")))
    except ValueError:
        return cleaned, {
            "expiry_raw": raw,
            "expiry_date": None,
            "expiry_plausible": False,
            "expiry_warning": f"Scadenza scritta male nella descrizione: «{raw}». Il campo va verificato a mano.",
        }

    # Credibile vuol dire vicina a oggi: si confrontano gli anni, cosi' la
    # regola non dipende dal giorno in cui gira il programma piu' del dovuto.
    window = rule.get("plausible_window_years") or {}
    back = int(window.get("back", 2))
    ahead = int(window.get("ahead", 10))
    today = date.today()
    plausible = today.year - back <= expiry.year <= today.year + ahead
    warning = None
    if not plausible:
        warning = (
            f"Scadenza fuori dal credibile: «{raw}». Il dato è stato letto lo stesso, "
            "ma va verificato sul listino del fornitore."
        )
    return cleaned, {
        "expiry_raw": raw,
        "expiry_date": expiry.isoformat(),
        "expiry_plausible": plausible,
        "expiry_warning": warning,
    }


def row_allowed(row: tuple[Any, ...], columns: dict[str, int], mapping: dict[str, Any]) -> bool:
    rule = mapping.get("row_filter") or {}
    if not rule:
        return True
    field = rule.get("field")
    actual = str(mapped_value(row, columns, field) or "").strip()
    if "equals" in rule:
        return actual.casefold() == str(rule["equals"]).strip().casefold()
    if "regex" in rule:
        return re.search(str(rule["regex"]), actual, flags=re.IGNORECASE) is not None
    raise ValueError("row_filter deve usare equals o regex")


def reading_report(
    rows: list[tuple[Any, ...]],
    data_start: int,
    data_end: int,
    excluded: Counter[str],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Racconta com'e' andata la lettura: quanto si e' letto e quanto si e' tolto.

    Serve a chi controlla il risultato: se le righe scartate non compaiono da
    nessuna parte, nessuno si accorge che il listino e' entrato a meta'.
    """
    return {
        "sheet_rows": len(rows),
        "data_start_row": data_start,
        "data_end_row": data_end,
        "rows_ignored_after_data_end": max(0, len(rows) - data_end),
        "rows_excluded": dict(excluded),
        "rows_kept": len(records),
        "expiry_dates_read": sum(1 for record in records if record.get("expiry_date")),
        "expiry_dates_to_check": sum(1 for record in records if record.get("expiry_plausible") is False),
    }


def read_mapped_master(
    path: Path,
    mapping: dict[str, Any],
    report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    data_start, columns, rows, _bold = mapped_rows(path, mapping)
    required = {"ean", "description", "last_unit_price"}
    missing = required - set(columns)
    if missing:
        raise ValueError(f"Mappatura master incompleta: {sorted(missing)}")
    records = []
    excluded: Counter[str] = Counter()
    for source_row, row in enumerate(rows[data_start - 1:], start=data_start):
        if not row_allowed(row, columns, mapping):
            excluded[FUORI_DAL_FILTRO] += 1
            continue
        description = str(mapped_value(row, columns, "description") or "").strip()
        ean = normalize_ean(mapped_value(row, columns, "ean"))
        if not description and not ean:
            excluded[NON_E_RIGA_PRODOTTO] += 1
            continue
        records.append({
            "source": "gestionale",
            "source_row": source_row,
            "ean": ean,
            "description": description,
            "unit": str(mapped_value(row, columns, "unit") or "").strip(),
            "suggested_colli": mapped_value(row, columns, "suggested_colli"),
            "source_quantity_ignored": mapped_value(row, columns, "source_quantity_ignored"),
            "manual_quantity": None,
            "last_unit_price": json_decimal(decimal_value(mapped_value(row, columns, "last_unit_price"), italian=bool(mapping.get("italian_numbers")))),
            "source_discount": mapped_value(row, columns, "source_discount"),
            "vat": mapped_value(row, columns, "vat"),
        })
    if not records:
        raise ValueError(f"Nessuna riga master letta da {path}")
    if report is not None:
        report.update(reading_report(rows, data_start, len(rows), excluded, records))
    return records


def supplier_record(
    supplier_id: str,
    source_row: int,
    row: tuple[Any, ...],
    columns: dict[str, int],
    mapping: dict[str, Any],
    bold: tuple[bool, ...] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    description = str(mapped_value(row, columns, "description") or "").strip()
    description, expiry = expiry_from_description(description, mapping)
    ean = normalize_ean(mapped_value(row, columns, "ean"))
    supplier_code = mapped_value(row, columns, "supplier_code")
    if "supplier_code" in (mapping.get("text_columns") or ()):
        # Il codice articolo Noce ha gli zeri iniziali: se diventasse un
        # numero, «0000000449070» si ridurrebbe a 449070 e non tornerebbe piu'
        # indietro.  normalize_ean toglie solo gli artefatti del foglio di
        # calcolo e lascia il resto com'e' scritto.
        supplier_code = normalize_ean(supplier_code)
    if not description and not ean and supplier_code in (None, ""):
        return None, None

    italian = bool(mapping.get("italian_numbers"))
    direct_price = decimal_value(mapped_value(row, columns, "unit_price_net"), italian=italian)
    pre_price = decimal_value(mapped_value(row, columns, "unit_price_pre_discount"), italian=italian)
    discount_raw = mapped_value(row, columns, "discount")
    discount_rate = Decimal("0")
    discount_type = "none"
    warning = None
    if pre_price is not None:
        if mapping.get("discount_mode") == "numeric_percent_or_text_zero":
            discount_rate, discount_type, warning = larice_discount(
                discount_raw, registro.codici_ammessi(registro.codici_di_riga(mapping))
            )
        elif discount_raw not in (None, ""):
            numeric_discount = decimal_value(discount_raw, italian=italian)
            if numeric_discount is None:
                raise ValueError(f"Sconto non interpretabile alla riga {source_row}: {discount_raw!r}")
            discount_rate = numeric_discount / 100 if numeric_discount > 1 else numeric_discount
            discount_type = "percentuale"
        direct_price = pre_price * (Decimal("1") - discount_rate)

    pieces = decimal_value(mapped_value(row, columns, "pieces_per_carton"), italian=italian)
    multiplier = decimal_value(mapped_value(row, columns, "order_multiplier"), italian=italian)
    # Il valore fisso dichiarato nel profilo del fornitore vale solo se e' un
    # numero maggiore di zero: uno zero, o una parola al posto di un numero,
    # non e' un fattore d'ordine e non deve prenderne il posto.
    if pieces is None:
        pieces = fattore_d_ordine(mapping.get("pieces_per_carton_default"))
    if multiplier is None:
        multiplier = fattore_d_ordine(mapping.get("order_multiplier_default"))
    # Chi decide resta il moltiplicatore quando c'e', esattamente come prima:
    # un moltiplicatore scritto male non si rimpiazza con i pezzi per collo,
    # perche' vorrebbe dire ordinare su un numero che nessuno ha dichiarato.
    factor = fattore_d_ordine(multiplier if multiplier is not None else pieces)
    available_field = mapped_value(row, columns, "availability")
    availability = str(available_field or "").strip()
    available = True
    if mapping.get("available_values"):
        available = availability.casefold() in {str(value).casefold() for value in mapping["available_values"]}

    # Lo stesso `and` di prima, scritto in modo che sappia dire che cosa
    # mancava: e' l'unico dato che rende contabile una riga scartata.
    if not description:
        motivo: str | None = SENZA_DESCRIZIONE
    elif direct_price is None:
        motivo = SENZA_PREZZO
    elif factor is None:
        motivo = SENZA_PEZZI_PER_COLLO
    elif not available:
        motivo = NON_DISPONIBILE
    else:
        motivo = None

    record = {
        "source": supplier_id,
        "source_row": source_row,
        "ean": ean,
        "supplier_code": supplier_code,
        "description": description,
        "pieces_per_carton": json_decimal(pieces),
        "order_multiplier": json_decimal(multiplier),
        "unit_price_pre_discount": json_decimal(pre_price),
        "discount_raw": discount_raw,
        "discount_type": discount_type,
        "discount_rate": json_decimal(discount_rate),
        "unit_price_net": json_decimal(direct_price),
        "pallet": mapped_value(row, columns, "pallet"),
        "packaging": mapped_value(row, columns, "packaging"),
        "availability": availability,
        "unit": str(mapped_value(row, columns, "unit") or "").strip(),
        "vat": mapped_value(row, columns, "vat"),
        "order_column": mapping.get("order_column"),
        "usable": motivo is None,
    }
    if motivo is not None:
        record["unusable_reason"] = motivo
    if "category" in columns:
        # Il campo su cui si scarta va mostrato anche sulle righe rimaste:
        # e' cosi' che l'utente controlla che il filtro abbia fatto la cosa giusta.
        record["category"] = str(mapped_value(row, columns, "category") or "").strip()
    if expiry is not None:
        record.update({field: value for field, value in expiry.items() if field != "expiry_warning"})

    bold_field = mapping.get("offer_from_bold")
    if bold_field or "offer_flag" in columns:
        # Su questo listino l'offerta ha due segnali: la colonna «offerta» e il
        # prezzo scritto in grassetto («i prezzi offerta sono in grassetto»).
        # Basta uno dei due, e nessuno dei due si butta via.
        bold_index = columns.get(str(bold_field)) if bold_field else None
        bold_price = bool(bold_index and bold and bold_index <= len(bold) and bold[bold_index - 1])
        declared = str(mapped_value(row, columns, "offer_flag") or "").strip()
        record["offer_flag"] = declared
        record["offer_price_bold"] = bold_price
        record["offer"] = bold_price or declared.casefold() in {"si", "sì", "s", "true", "1"}

    messages = [text for text in (warning, (expiry or {}).get("expiry_warning")) if text]
    warning_record = (
        {"source": supplier_id, "source_row": source_row, "ean": ean, "warning": " ".join(messages)}
        if messages
        else None
    )
    return record, warning_record


def read_mapped_xlsx_supplier(
    path: Path,
    supplier_id: str,
    mapping: dict[str, Any],
    report: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Legge un listino fornitore da foglio elettronico: .xlsx oppure .xls.

    Restituisce ancora due valori perche' questa funzione la chiama anche
    ``app/catalog_search.py``; i conteggi della lettura (confine dei dati,
    righe scartate) finiscono in ``report``, quando chi chiama lo passa.
    """
    data_start, columns, rows, bold = mapped_rows(path, mapping)
    required = {"description", "unit_price_net"} if "unit_price_net" in columns else {"description", "unit_price_pre_discount"}
    missing = required - set(columns)
    if missing:
        raise ValueError(f"Mappatura fornitore incompleta: {sorted(missing)}")
    if not ({"pieces_per_carton", "order_multiplier"} & set(columns)):
        dichiarati = {"pieces_per_carton_default", "order_multiplier_default"} & set(mapping)
        if not dichiarati:
            raise ValueError("Manca il fattore d'ordine o un default esplicito")
        if not any(fattore_d_ordine(mapping.get(chiave)) is not None for chiave in dichiarati):
            # Un valore fisso che non e' un numero maggiore di zero lascerebbe
            # fuori dall'ordine tutte le righe del listino, una per una: meglio
            # dirlo subito e in un punto solo.
            raise ValueError(
                f"Per {path.name} non è indicata nessuna colonna con i pezzi per collo, e il valore "
                "fisso messo al suo posto non è un numero maggiore di zero: così nessun prodotto di "
                "questo listino potrebbe essere ordinato. Indicare la colonna dei pezzi per collo, "
                "oppure un valore fisso valido (per esempio 6)."
            )
    data_end = last_data_row(rows, columns, mapping, data_start)
    records = []
    warnings = []
    excluded: Counter[str] = Counter()
    for source_row, row in enumerate(rows[data_start - 1:data_end], start=data_start):
        if not row_allowed(row, columns, mapping):
            excluded[FUORI_DAL_FILTRO] += 1
            continue
        label = excluded_row_label(row, columns, mapping)
        if label:
            excluded[label] += 1
            continue
        if non_e_una_riga_prodotto(row, columns, mapping):
            excluded[NON_E_RIGA_PRODOTTO] += 1
            continue
        record, warning = supplier_record(
            supplier_id,
            source_row,
            row,
            columns,
            mapping,
            bold=bold[source_row - 1] if source_row - 1 < len(bold) else None,
        )
        if record:
            records.append(record)
        if warning:
            warnings.append(warning)
    registro.applica_codici_di_riga(records, registro.codici_di_riga(mapping))
    if report is not None:
        report.update(reading_report(rows, data_start, data_end, excluded, records))
        report["rows_not_orderable"] = conta_non_ordinabili(records)
    return records, warnings


def mapped_standalone_displays(
    records: list[dict[str, Any]],
    supplier_id: str,
    mapping: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Separate standalone display rows declared by a reviewed supplier mapping.

    A new supplier can list an already assembled display on a single orderable
    row, without the child rows available in the Larice canvass.  The mapping
    must opt in explicitly: a product is never reclassified merely because its
    name happens to contain a similar word.
    """

    rules = mapping.get("display_detection")
    if not isinstance(rules, dict) or not rules.get("description_regex"):
        return records, [], None

    try:
        description_pattern = re.compile(str(rules["description_regex"]), re.IGNORECASE)
        excluded_pattern = (
            re.compile(str(rules["exclude_description_regex"]), re.IGNORECASE)
            if rules.get("exclude_description_regex")
            else None
        )
        code_pattern = (
            re.compile(str(rules["supplier_code_regex"]), re.IGNORECASE)
            if rules.get("supplier_code_regex")
            else None
        )
        declared_pattern = (
            re.compile(str(rules["declared_units_regex"]), re.IGNORECASE)
            if rules.get("declared_units_regex")
            else None
        )
    except re.error as exc:
        raise ValueError(f"Regola espositore non valida per {supplier_id}: {exc}") from exc

    expected_factor = decimal_value(rules.get("order_factor_equals"), italian=bool(mapping.get("italian_numbers")))
    standard: list[dict[str, Any]] = []
    displays: list[dict[str, Any]] = []
    rejected = 0
    for record in records:
        description = str(record.get("description") or "").strip()
        supplier_code = str(record.get("supplier_code") or "").strip()
        if not description_pattern.search(description):
            standard.append(record)
            continue
        if excluded_pattern and excluded_pattern.search(description):
            rejected += 1
            standard.append(record)
            continue
        if code_pattern and not code_pattern.search(supplier_code):
            rejected += 1
            standard.append(record)
            continue
        # Il fattore d'ordine e' quello della riga da cui viene l'espositore: il
        # moltiplicatore quando c'e', altrimenti i pezzi per collo — la stessa
        # precedenza di `supplier_record`, perche' e' la stessa riga. Passa
        # dalla guardia come tutti gli altri: zero non e' un fattore.
        grezzo = record.get("order_multiplier")
        if grezzo is None:
            grezzo = record.get("pieces_per_carton")
        factor = fattore_d_ordine(grezzo)
        if expected_factor is not None and factor != expected_factor:
            rejected += 1
            standard.append(record)
            continue

        declared_units: Decimal | None = None
        if declared_pattern:
            match = declared_pattern.search(description)
            if match:
                numbers = re.findall(r"[0-9]+(?:[.,][0-9]+)?", match.group(0))
                values = [decimal_value(value, italian=True) for value in numbers]
                if values and all(value is not None for value in values):
                    declared_units = sum((value for value in values if value is not None), Decimal("0"))

        evidence = [
            "Riga singola con nome espositore e codice fornitore dedicato.",
            "Non sono presenti righe componente contigue da riconciliare.",
        ]
        if declared_units is not None:
            evidence.append(f"Quantità dichiarata nel nome: {json_decimal(declared_units)} pezzi.")
        # Un espositore non e' piu' sano della riga da cui viene: se quella non
        # si puo' ordinare — il prezzo non si legge, il fattore manca, e' un
        # premio — non si puo' ordinare nemmeno l'espositore, e il motivo e' lo
        # stesso. Senza questa dichiarazione l'espositore arrivava al confronto
        # senza che nessuno avesse detto se si poteva comprare.
        if record.get("usable") is False:
            motivo = str(record.get("row_type") or record.get("unusable_reason") or MOTIVO_NON_DICHIARATO)
        elif factor is None:
            motivo = SENZA_PEZZI_PER_COLLO
        else:
            motivo = None
        displays.append({
            "supplier": supplier_id,
            "source_row": record.get("source_row"),
            "supplier_code": record.get("supplier_code"),
            "description": description,
            "ean": record.get("ean") or "",
            "unit_price_net": record.get("unit_price_net"),
            "net_price_per_display": record.get("unit_price_net"),
            "declared_units": json_decimal(declared_units),
            "components": [],
            "confidence": str(rules.get("confidence") or "MEDIA").upper(),
            "quantity_reconciled": False,
            "price_reconciled": False,
            "evidence": evidence,
            "usable": motivo is None,
            **({} if motivo is None else {"unusable_reason": motivo}),
        })

    audit = {
        "rows_scanned": len(records),
        "standalone_display_count": len(displays),
        "standard_rows": len(standard),
        "rejected_candidates": rejected,
        "component_rows_available": False,
        # Come per gli espositori di Larice: quello che resta fuori dall'ordine
        # si conta, col motivo dichiarato.
        "display_offers_not_orderable": conta_non_ordinabili(displays),
    }
    return standard, displays, audit


def read_mapped_csv_supplier(
    path: Path,
    supplier_id: str,
    mapping: dict[str, Any],
    report: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Legge un listino fornitore da CSV, con le stesse regole dichiarate del foglio.

    Confine dei dati, righe da scartare e scadenza nella descrizione valgono
    anche qui: una regola che funziona solo su un formato e' una trappola per
    chi verra' dopo.

    `header_row = 0` dichiara che il file non ha intestazioni: le colonne si
    indicano allora per numero o per lettera, come nel ramo dei fogli di
    calcolo.  Senza questa via d'uscita un CSV senza intestazione non sarebbe
    piu' mappabile, perche' la prima riga di dati verrebbe presa per la riga
    dei nomi.

    Se dalla lettura non esce nessuna riga ordinabile ci si ferma e lo si dice:
    un listino aperto con il separatore sbagliato, a guardare il risultato, non
    si distingue da un fornitore che non tratta nessuno dei prodotti cercati.
    """
    encoding = mapping.get("encoding", "utf-8-sig")
    delimiter = mapping.get("delimiter")
    with path.open("r", encoding=encoding, newline="") as stream:
        sample = stream.read(65536)
        stream.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|") if not delimiter else None
        reader = csv.reader(stream, dialect) if dialect else csv.reader(stream, delimiter=delimiter)
        rows = []
        # Il numero di riga e' quello vero del file, non il posto occupato
        # nell'elenco: un testo fra virgolette puo' contenere un a-capo, e da
        # li' in poi i due numeri non coincidono piu'. Chi va a controllare
        # l'ordine sul listino del fornitore troverebbe un altro prodotto.
        # `line_num` dopo un prodotto indica la sua ultima riga: la prima e'
        # quella subito dopo la fine del prodotto precedente.
        righe_fisiche: list[int] = []
        prima_riga_del_prodotto = 1
        for grezza in reader:
            rows.append(grezza)
            righe_fisiche.append(prima_riga_del_prodotto)
            prima_riga_del_prodotto = reader.line_num + 1
    grezzo = mapping.get("header_row")
    header_row = 1 if grezzo in (None, "") else int(grezzo)
    if header_row < 0 or header_row > len(rows):
        raise ValueError(
            f"La mappatura dichiara la riga di intestazione {header_row}, ma {path.name} ha "
            f"{len(rows)} righe: usare 0 se il documento non ha intestazioni."
        )
    header = rows[header_row - 1] if header_row else None
    raw_columns = mapping.get("columns") or mapping.get("field_mapping") or {}
    if header is not None and len(header) <= 1 and len(raw_columns) > 1:
        # Il separatore, quando non e' dichiarato, viene indovinato guardando
        # il testo: se l'ipotesi e' sbagliata il file resta tutto in una colonna
        # e il fornitore risulterebbe senza nessuno dei prodotti cercati.
        raise ValueError(
            f"Il documento {path.name} non si è aperto in colonne: nella riga delle intestazioni "
            f"c'è un campo solo, mentre per questo fornitore ne sono indicate {len(raw_columns)}. "
            "Di solito vuol dire che il separatore delle colonne non è quello previsto: indicare "
            "quello giusto (per esempio «;» oppure «,») nella configurazione del fornitore."
        )
    columns = {field: column_number(spec, header) for field, spec in raw_columns.items() if spec not in (None, "")}
    tuples = [tuple(raw) for raw in rows]
    data_start = prima_riga_dei_dati(tuples, mapping, header_row, path.name)
    data_end = last_data_row(tuples, columns, mapping, data_start)
    records = []
    warnings = []
    excluded: Counter[str] = Counter()
    for posizione, row in enumerate(tuples[data_start - 1:data_end], start=data_start):
        if not row_allowed(row, columns, mapping):
            excluded[FUORI_DAL_FILTRO] += 1
            continue
        label = excluded_row_label(row, columns, mapping)
        if label:
            excluded[label] += 1
            continue
        if non_e_una_riga_prodotto(row, columns, mapping):
            excluded[NON_E_RIGA_PRODOTTO] += 1
            continue
        record, warning = supplier_record(
            supplier_id, righe_fisiche[posizione - 1], row, columns, mapping
        )
        if record:
            records.append(record)
        if warning:
            warnings.append(warning)
    # Il controllo sta qui, prima delle regole del registro: quelle tolgono
    # dall'ordine righe lette benissimo (un premio, un omaggio), e non dicono
    # niente su come e' andata la lettura.
    if not any(record.get("usable") for record in records):
        letto = (
            "non è stata letta nessuna riga di prodotto" if not records
            else f"sono state lette {len(records)} righe, ma nessuna è risultata ordinabile"
        )
        raise ValueError(
            f"Da {path.name} {letto}: il documento non ha la forma attesa, oppure il separatore "
            "delle colonne o la riga delle intestazioni non sono quelli indicati. Conviene "
            "controllare il file e la configurazione di questo fornitore: un listino letto a vuoto "
            "lo farebbe risultare senza nessuno dei prodotti cercati, che è una cosa diversa da "
            "un listino non letto."
        )
    registro.applica_codici_di_riga(records, registro.codici_di_riga(mapping))
    if report is not None:
        report.update(reading_report(tuples, data_start, data_end, excluded, records))
        report["rows_not_orderable"] = conta_non_ordinabili(records)
    return records, warnings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--adapters", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    # I gruppi di codici a barre che l'utente ha dichiarato essere lo stesso
    # articolo. Il file lo scrive il servizio dal magazzino delle conferme: la
    # catena e' fatta di passi che leggono artefatti dichiarati, non di processi
    # che aprono database, ed e' il motivo per cui questa fase si puo' provare.
    parser.add_argument("--equivalenze", type=Path)
    return parser.parse_args()


def leggi_uguaglianze(percorso: Path | None) -> list[list[str]]:
    """I gruppi di codici uguali dichiarati dall'utente, o niente.

    Non chiederlo e' legittimo: vuol dire che per questa run non ce ne sono.
    Chiederlo e non trovare il file **non** lo e' — sono dichiarazioni umane, e
    farle sparire in silenzio riporterebbe i prodotti abbinati a mano allo stato
    di prima senza che niente lo dica, cioe' esattamente il modo peggiore di
    perdere una decisione. Stessa regola di `--displays` in
    `build_review_data.py`, e per la stessa ragione.

    Il documento e' `{"classi": [[codice, codice, …], …]}` oppure direttamente la
    lista dei gruppi: un file scritto a mano per una prova non deve sbagliare per
    una chiave.
    """

    if percorso is None:
        return []
    if not percorso.exists():
        raise ValueError(
            f"Il file delle uguaglianze fra codici non esiste: {percorso}. È stato chiesto, "
            "quindi qualcuno doveva scriverlo: senza, i prodotti abbinati a mano tornerebbero "
            "a non avere quel fornitore, e nessuno lo direbbe."
        )
    contenuto = json.loads(percorso.read_text(encoding="utf-8"))
    gruppi = contenuto.get("classi") if isinstance(contenuto, dict) else contenuto
    if not isinstance(gruppi, list):
        raise ValueError(f"Il file delle uguaglianze {percorso} non contiene una lista di gruppi")
    return [
        [str(codice) for codice in gruppo]
        for gruppo in gruppi
        if isinstance(gruppo, list) and len(gruppo) > 1
    ]


def colonne_corrette(
    decision: dict[str, Any], adapter: dict[str, Any], adapter_id: str
) -> dict[str, Any]:
    """Le colonne che l'utente ha corretto a mano per un documento riconosciuto.

    Solo quelle **della decisione**: la `field_mapping` di un adattatore del
    registro descrive un altro modo di leggere lo stesso fornitore, non una
    correzione, e passarla ai lettori cablati vorrebbe dire scambiare le due
    strade a insaputa di tutti.

    `read_noce` legge un CSV e indica le colonne per nome: non ha niente da
    correggere per posizione e resta fuori.
    """

    # ⚠ Per `adattatore_base`, non per l'id cosi' com'e': un `betulla_v1__locale`
    # — la mappatura confermata qui sopra quella spedita — e' lo stesso lettore
    # e le stesse correzioni. Guardare l'id intero vorrebbe dire che imparare
    # una variazione spegne in silenzio le correzioni che quella variazione
    # serviva proprio a portare.
    base = registro.adattatore_base(adapter_id)
    if base == "noce_csv_v1":
        return {}
    mappatura = decision.get("field_mapping")
    if not isinstance(mappatura, dict) or not mappatura:
        return {}
    colonne = mappatura.get("columns")
    extra: dict[str, Any] = {}
    if isinstance(colonne, dict) and colonne:
        extra["colonne"] = colonne_per_posizione(colonne, adapter, adapter_id)
    if base in {"betulla_v1", "larice_v1"}:
        ordine = mappatura.get("order_column")
        if ordine:
            extra["order_column"] = str(ordine)
    return extra


def colonne_per_posizione(
    colonne: dict[str, Any], adapter: dict[str, Any], adapter_id: str
) -> dict[str, Any]:
    """Le colonne della decisione, con i nomi risolti nelle loro posizioni.

    ⚠ La mappatura confermata in pagina indica una colonna **per nome** ogni
    volta che l'intestazione e' univoca (`schema_mapping.specifica_colonna`), e
    i lettori dedicati leggono per posizione. Finche' i nomi arrivavano loro
    cosi' com'erano, imparare una variante di BETULLA o di LARICE voleva dire un
    ricalcolo che la settimana dopo si fermava con «La colonna indicata per
    description ("Descr.Commerciale") non è una colonna valida» — cioe' il
    fornitore fuori dal confronto proprio per la mappatura che serviva a
    tenercelo dentro (6 settembre 2026).

    Le posizioni non si indovinano dal documento: sono quelle che l'adattatore
    ha misurato il giorno in cui la variante e' stata confermata
    (`header_signature.columns`, per nome normalizzato). Il nome si guarda
    **prima** della lettera, perche' «EAN» e' un'intestazione vera di BETULLA e
    come lettera varrebbe la colonna 3.420.

    Numeri e lettere restano com'erano. Un nome che l'impronta non conosce si
    ferma qui: lasciarlo passare significherebbe leggere la colonna
    predefinita, cioe' dei prezzi che sembrano giusti.
    """

    posizioni = ((adapter or {}).get("header_signature") or {}).get("columns")
    posizioni = posizioni if isinstance(posizioni, dict) else {}
    risolte: dict[str, Any] = {}
    for campo, dichiarata in colonne.items():
        if not isinstance(dichiarata, str):
            risolte[campo] = dichiarata
            continue
        indice = posizioni.get(registro.normalizza(dichiarata))
        if indice is None and indice_di_colonna(dichiarata) is None:
            raise ValueError(
                f"La colonna «{dichiarata}» indicata per «{campo}» non è fra le intestazioni con "
                f"cui lo schema «{adapter_id}» è stato imparato: il listino è cambiato ancora, e "
                "la mappatura di questo fornitore va rifatta dalla pagina Importa."
            )
        risolte[campo] = dichiarata if indice is None else indice
    return risolte


LETTORI_A_SCHEMA_NOTO: dict[str, Callable[..., Any]] = {
    "gestionale_v1": read_gestionale,
    "betulla_v1": read_betulla,
    "larice_v1": read_larice,
    "noce_csv_v1": read_noce,
}


def lettore_dedicato(state: Any, adapter_id: Any) -> Callable[..., Any] | None:
    """Il lettore dedicato per questa decisione, o `None` se si legge generico.

    E' l'**unico** posto in cui si sceglie con quale lettore si apre un
    documento. Il catalogo della ricerca prodotti chiama questa, non un
    `if supplier == "..."` suo: le due strade hanno detto cose diverse fino al
    21 agosto 2026, e il conto l'ha pagato BETULLA — letto dal generico qui,
    mandato a `read_betulla` dal catalogo, sparito dal visualizzatore.

    Il criterio e' la **decisione**, non il fornitore: lo stesso fornitore, in
    due run diverse, puo' arrivare con uno schema riconosciuto o con uno
    variato e confermato a mano, e sono due letture diverse dello stesso nome.
    """

    if str(state or "") != "SCHEMA_NOTO":
        return None
    # `adattatore_base`: un `larice_v1__locale` e' Larice, e deve passare da
    # `read_larice`. Senza questa riga, imparare una variazione su uno dei
    # quattro fornitori con lettore dedicato lo manderebbe al lettore generico
    # — che legge le stesse colonne ma non fa il resto: niente espositori,
    # niente soglie con omaggio, niente marcatore di riga del gestionale.
    return LETTORI_A_SCHEMA_NOTO.get(registro.adattatore_base(adapter_id))


def main() -> int:
    args = parse_args()
    manifest = load_json(args.manifest)
    # ⚠ Il registro EFFETTIVO, non il solo file spedito che la catena passa
    # qui: un adattatore imparato (`betulla_v1__locale`) nello spedito non c'e',
    # e `adapters.get(...)` rispondeva `{}` — cioe' nessun `supplier_id` di
    # ripiego, e nessuna posizione delle colonne per rileggere una variante
    # confermata la settimana prima. E' la stessa trappola chiusa il 22 agosto
    # 2026 in `validate_input_manifest.py`, e la regola 4: chi legge il
    # registro lo legge da `registro` (6 settembre 2026).
    adapters = {str(item.get("id") or ""): item for item in registro.adattatori(args.adapters)}
    files = manifest.get("files") or manifest.get("profiles") or []
    master = None
    sources: dict[str, list[dict[str, Any]]] = {}
    warnings: list[dict[str, Any]] = []
    display_offers: dict[str, list[dict[str, Any]]] = {}
    display_audit: dict[str, dict[str, Any]] = {}
    input_audit = []

    for item in files:
        decision = item.get("ai_preflight") or {}
        state = decision.get("state")
        if state in {"FILE_NON_PERTINENTE", "AMBIGUO"}:
            continue
        path = Path(item["path"])
        role = decision.get("role")
        adapter_id = decision.get("adapter_id")
        supplier_id = decision.get("supplier_id")
        adapter = adapters.get(adapter_id, {})
        mapping = decision.get("field_mapping") or adapter.get("field_mapping") or {}
        if role == "supplier" and not supplier_id:
            supplier_id = adapter.get("supplier_id")
        records: Any
        local_warnings: list[dict[str, Any]] = []
        reading: dict[str, Any] = {}

        lettore = lettore_dedicato(state, adapter_id)
        if lettore is not None:
            # Anche i lettori a schema noto raccontano com'e' andata la lettura:
            # senza `report` il riquadro degli scarti restava muto proprio sui
            # quattro listini piu' grossi (BETULLA, Larice, Noce, gestionale).
            #
            # ⚠ Le colonne corrette a mano arrivano **qui dentro**, non
            # mandando il documento al lettore generico. Il lettore generico
            # legge le stesse colonne ma non fa il resto: per Larice vuol dire
            # niente espositori e niente soglie con omaggio, per il gestionale
            # niente marcatore di riga. Una correzione delle colonne non deve
            # cambiare che cosa il programma sa fare di quel documento.
            extra = colonne_corrette(decision, adapter, adapter_id)
            result = lettore(path, report=reading, **extra)
            if registro.adattatore_base(adapter_id) == "larice_v1":
                records, local_warnings = result
                records, supplier_displays, supplier_display_audit = integrate_larice_displays(records, analyse_workbook(path))
                display_offers[supplier_id or "larice"] = supplier_displays
                display_audit[supplier_id or "larice"] = supplier_display_audit
            else:
                records = result
        elif role == "master":
            records = read_mapped_master(path, mapping, report=reading)
        # Il formato lo dicono i primi byte del file: un foglio di calcolo
        # rinominato .csv resterebbe un foglio di calcolo, e leggerlo riga per
        # riga come testo darebbe un listino vuoto senza dirlo a nessuno.
        elif container_format(path) == "csv":
            records, local_warnings = read_mapped_csv_supplier(path, supplier_id, mapping, report=reading)
        else:
            records, local_warnings = read_mapped_xlsx_supplier(path, supplier_id, mapping, report=reading)

        if role == "supplier" and mapping.get("display_detection"):
            records, supplier_displays, supplier_display_audit = mapped_standalone_displays(records, supplier_id, mapping)
            if supplier_displays:
                display_offers[supplier_id] = supplier_displays
                display_audit[supplier_id] = supplier_display_audit or {}

        if role == "master":
            if master is not None:
                raise ValueError("Manifest con più di un master")
            master = records
        elif role == "supplier":
            if not supplier_id:
                adapter = adapters.get(adapter_id, {})
                supplier_id = adapter.get("supplier_id")
            if not supplier_id:
                raise ValueError(f"supplier_id mancante per {path}")
            if supplier_id in sources:
                raise ValueError(f"Fornitore duplicato: {supplier_id}")
            for record in records:
                record["source"] = supplier_id
            sources[supplier_id] = records
        warnings.extend(local_warnings)
        # Le righe si declassano anche DOPO la lettura — i componenti di un
        # espositore Larice, gli espositori isolati di un fornitore mappato —
        # quindi il conteggio si rifa' qui, a mutazioni finite, ed e' lo stesso
        # numero nei due punti dell'audit.
        non_ordinabili = conta_non_ordinabili(records if isinstance(records, list) else [])
        if reading:
            reading["rows_not_orderable"] = non_ordinabili
        input_audit.append({
            "path": str(path.resolve()),
            "state": state,
            "role": role,
            "supplier_id": supplier_id,
            "adapter_id": adapter_id,
            "records": len(records),
            # Quante righe il programma ha deciso di non far ordinare, e per
            # quale motivo dichiarato: i premi delle soglie, i componenti di un
            # espositore, ma anche il prezzo che non si e' letto e i pezzi per
            # collo che mancano. Uno scarto che non si conta non e' una scelta.
            "rows_not_orderable": non_ordinabili,
            "content_format": container_format(path),
            "reading": reading,
        })

    if master is None:
        raise ValueError("Master gestionale non trovato nel manifest")
    if not sources:
        raise ValueError("Nessun fornitore incluso nel manifest")

    matching, semantic_queue, audit = build_matching(master, sources, uguaglianze=leggi_uguaglianze(args.equivalenze))
    audit["warnings"] = warnings
    audit["inputs"] = input_audit
    audit["displays"] = display_audit
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / "normalized_sources.json", {"gestionale": master, **sources, "display_offers": display_offers})
    write_json(args.output / "display_offers.json", display_offers)
    write_json(args.output / "matching_result.json", matching)
    write_json(args.output / "semantic_queue.json", semantic_queue)
    write_json(args.output / "audit.json", audit)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
