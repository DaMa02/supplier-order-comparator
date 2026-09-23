#!/usr/bin/env python3
"""Read every active source row and produce deterministic EAN matching artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

import registro
from detect_displays import analyse_workbook


# ---------------------------------------------------------------------------
# I motivi per cui una riga letta non entra nel confronto, dichiarati e non
# dedotti. `usable` e' una congiunzione: da sola dice che la riga e' fuori, mai
# che cosa le mancava, e uno scarto che non si sa contare non e' una scelta —
# l'utente leggeva «righe non ordinabili: 0» con mezzo listino buttato
# (revisione del 14 agosto 2026). Stanno qui perche' li usano sia i lettori
# cablati (BETULLA, Larice, Noce, gestionale) sia quelli mappati di
# `prepare_manifest_sources`, che importa da questo modulo.
# ---------------------------------------------------------------------------
SENZA_DESCRIZIONE = "senza_descrizione"
SENZA_PREZZO = "senza_prezzo"
SENZA_PEZZI_PER_COLLO = "senza_pezzi_per_collo"
NON_DISPONIBILE = "non_disponibile"
MOTIVO_NON_DICHIARATO = "motivo_non_dichiarato"
# Righe saltate prima ancora di diventare un record: non hanno un `usable` da
# guardare, e senza un contatore sparivano del tutto.
SENZA_EAN = "senza_ean"
NON_E_RIGA_PRODOTTO = "non_e_una_riga_prodotto"
FUORI_DAL_FILTRO = "riga_fuori_dal_filtro"


def conta_non_ordinabili(records: list[dict[str, Any]]) -> dict[str, int]:
    """Ogni riga letta che non si puo' ordinare, col motivo dichiarato.

    Il `row_type` del registro vince sul motivo tecnico: un premio di soglia
    non e' «senza prezzo», e' un premio di soglia.
    """

    return dict(Counter(
        str(record.get("row_type") or record.get("unusable_reason") or MOTIVO_NON_DICHIARATO)
        for record in records
        if isinstance(record, dict) and record.get("usable") is False
    ))


def rapporto_di_lettura(
    *,
    righe_lette: int,
    records: list[dict[str, Any]],
    saltate: Counter[str] | None = None,
) -> dict[str, Any]:
    """Com'e' andata la lettura di un listino a schema noto.

    Le stesse chiavi di `prepare_manifest_sources.reading_report`, perche' la
    pagina e l'orchestratore leggono un formato solo. Fino al 14 agosto 2026
    i quattro lettori cablati non ne producevano nessuno, e il riquadro «cosa
    e' stato buttato» restava muto proprio sui listini piu' grossi.
    """

    saltate = saltate or Counter()
    return {
        "sheet_rows": righe_lette,
        "rows_excluded": dict(saltate),
        "rows_kept": len(records),
        "rows_not_orderable": conta_non_ordinabili(records),
    }


def normalize_ean(value: Any) -> str:
    """Normalize only safe spreadsheet artifacts; never search orphan shared strings."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text


def decimal_value(value: Any, *, italian: bool = False) -> Decimal | None:
    """Il numero che c'e' dentro una cella, oppure niente.

    «Non un numero» e «infinito» sono valori che un foglio di calcolo sa
    produrre da solo (una divisione per zero che si porta dietro), e con cui
    non si fa aritmetica: valgono come dato che non si e' letto. Lasciarli
    passare sarebbe peggio di fermarsi — non fanno saltare niente, si
    moltiplicano e si sommano come gli altri, e un totale «non un numero» in
    fondo a un ordine non lo legge nessuno (revisione del 14 agosto 2026).

    ⚠ Con `italian` acceso il punto e' il separatore delle migliaia: e' la
    convenzione dei listini, e vale per tre dei quattro fornitori spediti. Ma
    un testo con UN punto solo, nessuna virgola e una o due cifre dietro non e'
    un numero scritto cosi': «1.25» sono un euro e venticinque, e toglierlo
    faceva entrare nel confronto un prezzo di 125 euro senza dirlo a nessuno.
    Li' si risponde «non l'ho letto», e la riga finisce fra gli scarti contati
    come SENZA_PREZZO: un prodotto che manca si vede, un prezzo inventato no
    (6 settembre 2026). «1.250» resta ambiguo e resta milleduecentocinquanta.

    Le celle numeriche vere — quelle che il foglio di calcolo tiene come
    numero — non passano di qui e non cambiano.
    """

    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        numero = value
    elif isinstance(value, (int, float)):
        numero = Decimal(str(value))
    else:
        text = str(value).strip()
        if italian:
            if re.fullmatch(r"[-+]?\d+\.\d{1,2}", text):
                return None
            text = text.replace(".", "").replace(",", ".")
        try:
            numero = Decimal(text)
        except InvalidOperation:
            return None
    return numero if numero.is_finite() else None


def fattore_d_ordine(value: Any, *, italian: bool = False) -> Decimal | None:
    """Quanti pezzi si comprano insieme, ma soltanto se il numero c'e' davvero.

    Un fattore che manca, che vale zero o che non si lascia leggere **non e'
    uno**. Il prezzo di quello che si ordina si ottiene moltiplicando il prezzo
    del pezzo per questo numero: con un uno messo li' per non fermarsi,
    l'offerta costa quanto un pezzo solo, sembra la piu' conveniente di tutte e
    vince il confronto, che sceglie il prezzo piu' basso. Una riga senza questo
    numero si legge lo stesso, ma non si puo' ordinare (revisione del 14 agosto
    2026).

    Quello che non e' un numero lo scarta gia' `decimal_value`: qui resta da
    dire che nemmeno lo zero e i negativi lo sono, per un fattore d'ordine.
    """

    try:
        numero = decimal_value(value, italian=italian)
    except (InvalidOperation, TypeError, ValueError):
        # Un vero/falso finisce nel ramo dei numeri e non si lascia convertire:
        # e' il solo caso in cui `decimal_value` alza le mani invece di dire no.
        return None
    if numero is None or numero <= 0:
        return None
    return numero


def json_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")


def active_rows(path: Path, sheet_name: str | None = None) -> tuple[Any, Iterable[tuple[Any, ...]]]:
    """Le righe del foglio, lette tutte e col documento richiuso.

    Restituire il generatore pigro di openpyxl lasciava il file **aperto** per
    tutta la vita del processo: su Windows quel listino non si puo' piu' ne'
    eliminare ne' sostituire, e `app/catalog_search.py` chiama questi lettori
    dentro il servizio, non in un sottoprocesso.
    """

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[sheet_name] if sheet_name else workbook[workbook.sheetnames[0]]
        return sheet, list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()


# Dove i due lettori cablati vanno a prendere ogni campo, quando nessuno dice
# il contrario. Erano numeri scritti dentro il codice (`row[7]` per il prezzo
# del gestionale, `row[5]` per quello di BETULLA): finche' stavano li', una
# correzione delle colonne fatta in pagina non poteva raggiungerli, e l'unico
# modo di applicarla era mandare il documento al lettore generico — che per
# Larice vuol dire perdere espositori e offerte. Adesso sono un valore
# predefinito che la mappatura confermata puo' sostituire, campo per campo.
COLONNE_PREDEFINITE_GESTIONALE = {
    "ean": 2,
    "description": 4,
    "unit": 5,
    "suggested_colli": 6,
    "source_quantity_ignored": 7,
    "last_unit_price": 8,
    "source_discount": 9,
    "vat": 10,
}
COLONNE_PREDEFINITE_BETULLA = {
    "ean": 1,
    "supplier_code": 2,
    "description": 4,
    "pieces_per_carton": 5,
    "unit_price_net": 6,
    "pallet": 7,
    "vat": 8,
}


def colonne_con_predefinite(
    predefinite: dict[str, int], scelte: dict[str, Any] | None
) -> dict[str, int]:
    """Le colonne da usare: le predefinite, corrette da quelle scelte a mano.

    Una scelta che non si risolve in un numero di colonna **non si ignora**:
    prenderebbe silenziosamente il posto della predefinita e leggerebbe la
    colonna sbagliata senza che nessuno lo veda. Si alza.
    """

    risultato = dict(predefinite)
    for campo, dichiarata in (scelte or {}).items():
        if campo not in predefinite:
            continue
        indice = indice_di_colonna(dichiarata)
        if indice is None:
            raise ValueError(
                f"La colonna indicata per «{campo}» ({dichiarata!r}) non è una colonna valida."
            )
        risultato[campo] = indice
    return risultato


def intestazioni_obbligatorie(adapter_id: str) -> list[str]:
    """Le intestazioni che il REGISTRO dichiara obbligatorie per un adattatore.

    Gia' normalizzate: il registro le scrive cosi', e cosi' le confronta
    `riconosci`.  Un registro assente o senza quella voce restituisce una lista
    vuota, e chi chiama non pretende niente — la stessa scelta di
    `registro.adattatore`, che un registro illeggibile non deve fermare la
    lettura di un listino.
    """

    firma = (registro.adattatore(adapter_id) or {}).get("header_signature")
    richieste = (firma or {}).get("required") if isinstance(firma, dict) else None
    if not isinstance(richieste, list):
        return []
    return [registro.normalizza(nome) for nome in richieste if registro.normalizza(nome)]


def pretendi_le_intestazioni(
    adapter_id: str, valori: Iterable[Any], path: Path, etichetta: str
) -> dict[str, Any]:
    """Che la riga d'intestazione porti quelle che il registro pretende.

    Restituisce la mappa `intestazione normalizzata -> valore com'era scritto`,
    che serve a chi legge per nome (il CSV di Noce) e che chi legge per
    posizione ignora.

    ⚠ **L'elenco viene dal registro e il confronto passa da
    `registro.normalizza`**, cioe' dalla stessa funzione con cui il documento
    e' stato riconosciuto un momento prima.  Fino al 4 settembre 2026 qui
    c'erano cinque nomi scritti nel codice, confrontati lettera per lettera:
    quel giorno BETULLA ha scritto «Ordine» invece di «ORDINE», il registro l'ha
    riconosciuto lo stesso — `normalizza` la cassa non la guarda — e questo
    controllo ha messo il veto su un listino sano da 6.430 prodotti, fermando
    tutto il confronto con «Schema BETULLA non riconosciuto» cinque volte di
    fila.  Erano due definizioni della stessa cosa, e sono divergute alla prima
    occasione; adesso e' una sola, e sta dove sta la regola.

    Il controllo non sparisce, e non e' un doppione inutile: un lettore
    dedicato legge per POSIZIONE, e non sempre chi lo chiama viene dal
    riconoscimento — una decisione scritta a mano in `decisioni_schemi.json` lo
    accende senza che nessuno abbia guardato le intestazioni.  Quello che
    cambia e' che adesso pretende quello che il registro dichiara, non quello
    che si ricordava un letterale.

    E dice **che cosa** manca: «Schema BETULLA non riconosciuto» da solo non ha
    detto a nessuno che il problema era una parola in minuscolo, ed e' costato
    un pomeriggio.
    """

    presenti: dict[str, Any] = {}
    for valore in valori:
        if valore is None:
            continue
        chiave = registro.normalizza(valore)
        if chiave and chiave not in presenti:
            presenti[chiave] = valore
    mancanti = [nome for nome in intestazioni_obbligatorie(adapter_id) if nome not in presenti]
    if mancanti:
        lette = ", ".join(str(valore).strip() for valore in presenti.values()) or "nessuna"
        raise ValueError(
            f"Schema {etichetta} non riconosciuto in {path}: nella riga d'intestazione "
            f"mancano {', '.join(mancanti)}. Le intestazioni lette sono: {lette}."
        )
    return presenti


def read_gestionale(
    path: Path,
    report: dict[str, Any] | None = None,
    colonne: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    posizioni = colonne_con_predefinite(COLONNE_PREDEFINITE_GESTIONALE, colonne)
    workbook = load_workbook(path, read_only=True, data_only=True)
    candidate_names = ["Foglio1", *[name for name in workbook.sheetnames if name != "Foglio1"]]
    sheet = None
    for name in candidate_names:
        if name not in workbook.sheetnames:
            continue
        candidate = workbook[name]
        if any(str(candidate.cell(row, 1).value or "").strip().upper() == "C" for row in range(1, min(candidate.max_row, 200) + 1)):
            sheet = candidate
            break
    if sheet is None:
        workbook.close()
        raise ValueError(f"Schema gestionale non riconosciuto in {path}")
    # Come in `active_rows`: si legge tutto e si richiude, altrimenti l'export
    # del gestionale resta bloccato dal processo che l'ha letto.
    rows = list(sheet.iter_rows(values_only=True))
    workbook.close()
    records = []
    righe_lette = 0
    saltate: Counter[str] = Counter()
    for row_number, row in enumerate(rows, start=1):
        righe_lette += 1
        if str(row[0] if len(row) > 0 else "").strip().upper() != "C":
            saltate[NON_E_RIGA_PRODOTTO] += 1
            continue
        records.append(
            {
                "source": "gestionale",
                "source_row": row_number,
                "ean": normalize_ean(cella(row, posizioni["ean"])),
                "description": str(cella(row, posizioni["description"]) or "").strip(),
                "unit": str(cella(row, posizioni["unit"]) or "").strip(),
                "suggested_colli": cella(row, posizioni["suggested_colli"]),
                "source_quantity_ignored": cella(row, posizioni["source_quantity_ignored"]),
                "manual_quantity": None,
                "last_unit_price": json_decimal(decimal_value(cella(row, posizioni["last_unit_price"]), italian=True)),
                "source_discount": cella(row, posizioni["source_discount"]),
                "vat": cella(row, posizioni["vat"]),
            }
        )
    if not records:
        raise ValueError(f"Nessuna riga prodotto gestionale trovata in {path}")
    if report is not None:
        report.update(rapporto_di_lettura(righe_lette=righe_lette, records=records, saltate=saltate))
    return records


def read_betulla(
    path: Path,
    report: dict[str, Any] | None = None,
    colonne: dict[str, Any] | None = None,
    order_column: str | None = None,
) -> list[dict[str, Any]]:
    posizioni = colonne_con_predefinite(COLONNE_PREDEFINITE_BETULLA, colonne)
    sheet, rows = active_rows(path)
    iterator = iter(rows)
    header = next(iterator, ())
    pretendi_le_intestazioni("betulla_v1", header, path, "BETULLA")
    records = []
    righe_lette = 0
    saltate: Counter[str] = Counter()
    for row_number, row in enumerate(iterator, start=2):
        righe_lette += 1
        current_ean = normalize_ean(cella(row, posizioni["ean"]))
        if not current_ean:
            saltate[SENZA_EAN] += 1
            continue
        prezzo = decimal_value(cella(row, posizioni["unit_price_net"]))
        # Il prezzo di BETULLA e' quello del pezzo: quanto costa un collo lo dice
        # PzCt. Senza quel numero la riga usciva ordinabile lo stesso e il collo
        # finiva nell'ordine a totale zero — un totale falso per il fornitore, e
        # la soglia minima d'ordine non scattava, perche' zero sta sotto
        # qualunque soglia.
        pezzi = fattore_d_ordine(cella(row, posizioni["pieces_per_carton"]))
        motivo = SENZA_PREZZO if prezzo is None else (SENZA_PEZZI_PER_COLLO if pezzi is None else None)
        records.append(
            {
                "source": "betulla",
                "source_row": row_number,
                "ean": current_ean,
                "supplier_code": cella(row, posizioni["supplier_code"]),
                "description": str(cella(row, posizioni["description"]) or "").strip(),
                "pieces_per_carton": cella(row, posizioni["pieces_per_carton"]),
                "unit_price_net": json_decimal(prezzo),
                "pallet": cella(row, posizioni["pallet"]),
                "vat": cella(row, posizioni["vat"]),
                "order_column": str(order_column or "C"),
                "usable": motivo is None,
                # Il motivo si dichiara solo quando c'e': un campo sempre
                # presente si legge come «qualcosa non va» anche sulle righe sane.
                **({} if motivo is None else {"unusable_reason": motivo}),
            }
        )
    if report is not None:
        report.update(rapporto_di_lettura(righe_lette=righe_lette, records=records, saltate=saltate))
    return records


def larice_discount(value: Any, ammessi: set[str] | None = None) -> tuple[Decimal, str, str | None]:
    """Lo sconto della colonna P, che a volte e' un numero e a volte un codice.

    Quali codici siano leciti lo dice il registro degli adattatori, non questa
    funzione: un elenco scritto qui varrebbe per il listino di oggi e nessuno
    lo aggiornerebbe il giorno in cui Larice ne aggiunge uno.
    """

    if isinstance(value, (int, float, Decimal)):
        discount = Decimal(str(value))
        if discount > 1 and discount <= 100:
            discount /= 100
        if discount < 0 or discount > 1:
            raise ValueError(f"Percentuale sconto Larice non valida: {value}")
        return discount, "percentuale", None
    text = str(value or "").strip()
    noti = ammessi if ammessi is not None else set()
    warning = None if not text or text.upper() in noti else f"Codice sconto testuale inatteso: {text}"
    return Decimal("0"), "testo_nessuno_sconto", warning


# Le colonne che il lettore Larice usa davvero, col nome che ne darebbe
# l'utente: il registro le dichiara tutte, e quando ne manca una la lettura si
# ferma dicendo quale invece di andare a prendere la colonna di ieri.
COLONNE_DEL_LETTORE_LARICE = {
    "supplier_code": "il codice dell'articolo",
    "pieces_per_carton": "i pezzi per collo",
    "pallet": "la pedana",
    "description": "le descrizioni",
    "unit_price_pre_discount": "i prezzi",
    "discount": "lo sconto",
    "vat": "l'IVA",
    "ean": "il codice a barre",
}


def indice_di_colonna(dichiarata: Any) -> int | None:
    """La colonna dichiarata dal registro, come numero a partire da 1 (A = 1).

    Il listino Larice non ha nessuna riga di intestazione: li' una colonna si
    puo' indicare solo per lettera («R») o per numero (18), e non c'e' niente
    da cercare per nome. Un testo di cifre non vale come numero: altrove
    quella e' l'intestazione di una colonna, e interpretarla qui vorrebbe dire
    leggere in silenzio la colonna sbagliata.
    """

    if isinstance(dichiarata, bool):
        return None
    if isinstance(dichiarata, int):
        return dichiarata if dichiarata >= 1 else None
    lettere = str(dichiarata or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", lettere):
        return None
    indice = 0
    for lettera in lettere:
        indice = indice * 26 + (ord(lettera) - ord("A") + 1)
    return indice


def elenco_in_italiano(voci: list[str]) -> str:
    """«le descrizioni e il codice a barre», non un elenco fra parentesi quadre."""

    if len(voci) < 2:
        return voci[0] if voci else ""
    return ", ".join(voci[:-1]) + " e " + voci[-1]


def colonne_di_larice(adattatore: dict[str, Any], path: Path) -> dict[str, int]:
    """Dove il registro dice che stanno le colonne del listino Larice.

    Finche' le posizioni stavano scritte qui dentro, il giorno che Larice ne
    sposta una — o che l'utente conferma una variazione di schema e il registro
    impara la mappatura nuova — questo lettore continuava a guardare la colonna
    di prima. E' il lettore dei **prezzi**: non ne mancherebbero alcuni, come
    per le soglie; uscirebbe un listino intero di prezzi sbagliati, con l'aria
    di essere giusto (revisione del 14 agosto 2026).

    Una colonna che il registro non dichiara non si indovina: ci si ferma e si
    dice quale manca.
    """

    colonne: dict[str, int] = {}
    mancanti: list[str] = []
    for campo, nome_per_l_utente in COLONNE_DEL_LETTORE_LARICE.items():
        indice = indice_di_colonna(registro.posizione_del_campo(adattatore, {}, campo))
        if indice is None:
            mancanti.append(nome_per_l_utente)
        else:
            colonne[campo] = indice
    if mancanti:
        raise ValueError(
            f"Il listino LARICE «{path.name}» non è stato letto: non so più dove il listino "
            f"tiene {elenco_in_italiano(mancanti)}. Va detto di nuovo dove stanno quelle colonne "
            "prima di rifare il confronto."
        )
    return colonne


def cella(row: tuple[Any, ...], colonna: int) -> Any:
    """Il valore della cella, indicata come la indica il registro (A = 1)."""

    return row[colonna - 1] if len(row) >= colonna else None


def read_larice(
    path: Path,
    report: dict[str, Any] | None = None,
    colonne: dict[str, Any] | None = None,
    order_column: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    adattatore = registro.adattatore("larice_v1")
    codici = registro.codici_di_riga(adattatore)
    ammessi = registro.codici_ammessi(codici)
    # Le colonne del registro restano la regola; una mappatura confermata in
    # pagina le corregge senza far cambiare lettore — Larice letto dal lettore
    # generico perderebbe espositori e soglie con omaggio.
    colonne = colonne_con_predefinite(colonne_di_larice(adattatore, path), colonne)
    sheet, rows = active_rows(path)
    records = []
    warnings = []
    righe_lette = 0
    saltate: Counter[str] = Counter()
    for row_number, row in enumerate(rows, start=1):
        righe_lette += 1
        current_ean = normalize_ean(cella(row, colonne["ean"]))
        if not current_ean or current_ean.upper() in {"EAN", "#N/A"}:
            saltate[SENZA_EAN] += 1
            continue
        pre_price = decimal_value(cella(row, colonne["unit_price_pre_discount"]))
        sconto_grezzo = cella(row, colonne["discount"])
        discount, discount_type, warning = larice_discount(sconto_grezzo, ammessi)
        post_price = pre_price * (Decimal("1") - discount) if pre_price is not None else None
        if warning:
            warnings.append({"source": "larice", "source_row": row_number, "ean": current_ean, "warning": warning})
        descrizione = str(cella(row, colonne["description"]) or "").strip()
        motivo = SENZA_DESCRIZIONE if not descrizione else (SENZA_PREZZO if post_price is None else None)
        records.append(
            {
                "source": "larice",
                "source_row": row_number,
                "ean": current_ean,
                "supplier_code": cella(row, colonne["supplier_code"]),
                "description": descrizione,
                "pieces_per_carton": cella(row, colonne["pieces_per_carton"]),
                "pallet": cella(row, colonne["pallet"]),
                "unit_price_pre_discount": json_decimal(pre_price),
                "discount_raw": sconto_grezzo,
                "discount_type": discount_type,
                "discount_rate": json_decimal(discount),
                "unit_price_net": json_decimal(post_price),
                "vat": cella(row, colonne["vat"]),
                "order_column": str(order_column or "D"),
                "usable": motivo is None,
                **({} if motivo is None else {"unusable_reason": motivo}),
            }
        )
    # Le righe che il registro dichiara non acquistabili — oggi i premi delle
    # soglie con omaggio, marcati SM — smettono di esserlo qui, per quello che
    # sono e non perche' gli manca il prezzo.
    registro.applica_codici_di_riga(records, codici)
    if report is not None:
        report.update(rapporto_di_lettura(righe_lette=righe_lette, records=records, saltate=saltate))
    return records, warnings


def integrate_larice_displays(
    records: list[dict[str, Any]],
    analysis: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Mark component rows non-orderable and normalize detected parent offers."""
    classifications = {item["source_row"]: item for item in analysis.get("row_classifications", [])}
    component_lookup: dict[int, dict[str, Any]] = {}
    for bundle in [*analysis.get("display_offers", []), *analysis.get("rejected_bundle_candidates", [])]:
        for component in bundle.get("components", []):
            component_lookup[int(component["source_row"])] = component

    for record in records:
        classification = classifications.get(record.get("source_row"))
        if not classification or classification.get("row_type") != "COMPONENT":
            continue
        record["row_type"] = "DISPLAY_COMPONENT" if classification.get("display_detected") else "BUNDLE_COMPONENT"
        record["usable"] = False
        record["display_offer_id"] = classification.get("display_offer_id")
        record["display_parent_row"] = classification.get("parent_row")
        component = component_lookup.get(int(record["source_row"]))
        if component:
            record["component_description"] = component.get("description")
            record["component_quantity"] = component.get("quantity")

    offers = []
    for raw in analysis.get("display_offers", []):
        offer = dict(raw)
        # Quanto costa un espositore si sa moltiplicando il prezzo del pezzo per
        # i pezzi del collo padre. Se quel numero non si legge il prezzo
        # dell'espositore non esiste: prima al suo posto ne veniva usato uno, e
        # l'espositore costava quanto un pezzo — cioe' vinceva sempre il
        # confronto (revisione del 14 agosto 2026).
        parent_pack = fattore_d_ordine(raw.get("pieces_per_carton"))
        parent_pre = decimal_value(raw.get("parent_price_pre_discount")) if parent_pack is not None else None
        parent_post = decimal_value(raw.get("parent_price_post_discount")) if parent_pack is not None else None
        if parent_pack is None:
            # Si toglie di mezzo anche il prezzo del pezzo: chi prepara il
            # confronto, quando il prezzo dell'espositore manca, ripiega su
            # quello, e l'offerta tornerebbe conveniente dalla porta di servizio.
            offer["parent_price_pre_discount"] = None
            offer["parent_price_post_discount"] = None
        offer.update(
            {
                "supplier": "larice",
                "supplier_id": "larice",
                "source_row": raw.get("source_rows", {}).get("parent_row"),
                "declared_units": raw.get("declared_quantity"),
                "list_price_per_display": json_decimal(parent_pre * parent_pack if parent_pre is not None else None),
                "net_price_per_display": json_decimal(parent_post * parent_pack if parent_post is not None else None),
                "quantity_reconciled": (
                    raw.get("sum_pieces") == raw.get("declared_quantity")
                    if raw.get("sum_pieces_complete") and raw.get("declared_quantity") is not None
                    else None
                ),
                "price_reconciled": bool(raw.get("component_parent_price_match_basis")) if raw.get("component_price_coverage") else None,
                "order_column": "D",
                "order_multiplier": "1.0000",
                "usable": parent_pack is not None,
                **({} if parent_pack is not None else {"unusable_reason": SENZA_PEZZI_PER_COLLO}),
            }
        )
        offers.append(offer)

    summary = {
        "rows_scanned": analysis.get("rows_scanned"),
        "active_rows": analysis.get("active_rows"),
        "classification_counts": analysis.get("classification_counts"),
        "display_offer_count": len(offers),
        # Un espositore che resta fuori dall'ordine si conta come qualunque
        # altra riga scartata, con lo stesso vocabolario dei motivi.
        "display_offers_not_orderable": conta_non_ordinabili(offers),
        "confidence_counts": dict(Counter(offer.get("confidence") for offer in offers)),
        "component_rows_excluded_from_orderable": len(analysis.get("component_rows_excluded_from_orderable", [])),
        "rejected_bundle_candidates": len(analysis.get("rejected_bundle_candidates", [])),
    }
    return records, offers, summary


def read_noce(path: Path, report: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    records = []
    righe_lette = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        # ⚠ Le colonne si risolvono per NOME NORMALIZZATO, e poi si legge
        # attraverso questa mappa. Rendere tollerante il solo controllo
        # sarebbe stato peggio del difetto: la riga sarebbe passata e tutti i
        # `row.get("ean")` avrebbero risposto `None`, cioe' un listino letto
        # con zero righe utilizzabili e nessuno che lo dice. Un fornitore che
        # sparisce in silenzio e' il guasto peggiore di questo programma.
        colonne_csv = pretendi_le_intestazioni(
            "noce_csv_v1", reader.fieldnames or (), path, "CSV Noce"
        )

        def campo(riga: dict[str, Any], nome: str) -> Any:
            """Il valore di una colonna dichiarata, comunque il fornitore l'abbia scritta.

            `None` quando la colonna non c'e': «variation» compare solo quando
            Noce segnala una variazione, e il registro infatti non la
            dichiara obbligatoria.
            """

            chiave = colonne_csv.get(registro.normalizza(nome))
            return riga.get(chiave) if chiave is not None else None

        # Il numero di riga e' quello **vero** del file, non il posto che il
        # prodotto occupa nell'elenco: basta una descrizione che contiene un
        # a-capo — e nel testo fra virgolette e' lecito — perche' i due numeri
        # smettano di coincidere e tutte le righe successive slittino. Chi
        # controlla l'ordine sul listino del fornitore guarderebbe la riga
        # sbagliata. `line_num` conta le righe del file, e dopo aver letto un
        # prodotto indica la sua ultima riga: la prima e' quella dopo la fine
        # del prodotto precedente, e va tenuta da parte.
        prima_riga_del_prodotto = reader.line_num + 1
        for row in reader:
            row_number = prima_riga_del_prodotto
            prima_riga_del_prodotto = reader.line_num + 1
            righe_lette += 1
            unit_text = str(campo(row, "unit") or "").strip()
            unit_match = re.fullmatch(r"x\s*([0-9]+(?:[.,][0-9]+)?)", unit_text, flags=re.IGNORECASE)
            # «x 0» e' un moltiplicatore letto, non un moltiplicatore valido.
            order_multiplier = fattore_d_ordine(unit_match.group(1), italian=True) if unit_match else None
            availability = str(campo(row, "availability") or "").strip()
            is_available = availability.casefold().startswith("disponibile")
            price = decimal_value(campo(row, "price"), italian=True)
            records.append(
                {
                    "source": "noce",
                    "source_row": row_number,
                    "catalog_page": campo(row, "catalog_page"),
                    "ean": normalize_ean(campo(row, "ean")),
                    "description": str(campo(row, "product") or "").strip(),
                    "packaging": campo(row, "packaging"),
                    "availability": availability,
                    "variation": campo(row, "variation"),
                    "unit_price_net": json_decimal(price),
                    "unit": unit_text,
                    "order_multiplier": json_decimal(order_multiplier),
                    # Noce appends promotion terms to the availability text.
                    # "Disponibile ACQUISTA ..." is still an orderable item and
                    # must not be discarded merely because an offer follows it.
                    "usable": price is not None and order_multiplier is not None and is_available,
                    **(
                        {}
                        if price is not None and order_multiplier is not None and is_available
                        else {"unusable_reason": (
                            SENZA_PREZZO if price is None
                            else SENZA_PEZZI_PER_COLLO if order_multiplier is None
                            else NON_DISPONIBILE
                        )}
                    ),
                }
            )
    if report is not None:
        report.update(rapporto_di_lettura(righe_lette=righe_lette, records=records))
    return records


def source_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    eans = [record["ean"] for record in records if record.get("ean")]
    counts = Counter(eans)
    return {
        "rows": len(records),
        "rows_with_ean": len(eans),
        "distinct_eans": len(counts),
        "duplicate_ean_values": sum(1 for count in counts.values() if count > 1),
    }


def price_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Il prezzo al pezzo tipico di un listino, per confrontarlo con la volta prima.

    Serve al controllo dell'orchestratore che «avvisa e non ferma»: un listino
    che arriva con tutti i prezzi cambiati in blocco — un fornitore che passa
    dal prezzo netto al lordo, una colonna letta male — ha lo stesso numero di
    righe di sempre e non se ne accorgerebbe nessuno.

    Si usa la **mediana** e non la media: bastano tre articoli da mille euro per
    spostare una media di un listino da ottomila righe, e l'avviso partirebbe
    ogni settimana.
    """

    prezzi = sorted(
        valore
        for valore in (decimal_value(record.get("unit_price_net"), italian=False) for record in records)
        if valore is not None and valore > 0
    )
    if not prezzi:
        return {"usable": 0, "median": None}
    meta = len(prezzi) // 2
    mediana = prezzi[meta] if len(prezzi) % 2 else (prezzi[meta - 1] + prezzi[meta]) / 2
    return {"usable": len(prezzi), "median": json_decimal(Decimal(mediana))}


def solo_cifre(valore: Any) -> str:
    """Un codice a barre ridotto a quello che si confronta.

    ⚠ Deve dare lo stesso risultato di `conferme.codice_confrontabile`, che e'
    dove le uguaglianze vengono scritte: la stessa regola in due punti e' un
    debito, e qui si paga perche' gli script della catena non importano `app/`.
    La prova che le due non divergano sta nei test (`test_prepare_sources`).
    """

    testo = str(valore or "").strip()
    if testo.endswith(".0") and testo[:-2].isdigit():
        testo = testo[:-2]
    return "".join(carattere for carattere in testo if carattere.isdigit())


def mappa_delle_uguaglianze(classi: Any) -> dict[str, list[str]]:
    """Da «gruppi di codici uguali» a «per ogni codice, gli altri del gruppo».

    Ogni gruppo arriva come lista di codici dichiarati uguali fra loro. Qui
    diventa la domanda che serve a `build_matching`: dato l'EAN di un prodotto,
    **quali altri codici valgono come lui**. Il codice stesso non entra nella
    risposta — quello lo cerca gia' la strada nativa, e rimetterlo dentro
    farebbe contare due volte le stesse righe.
    """

    mappa: dict[str, list[str]] = {}
    for gruppo in classi or []:
        codici = [solo_cifre(codice) for codice in gruppo or [] if solo_cifre(codice)]
        if len(set(codici)) < 2:
            continue
        for codice in codici:
            altri = mappa.setdefault(codice, [])
            for altro in codici:
                # ⚠ Qui, e in un posto solo, si tolgono i doppioni. Un codice
                # ripetuto — nello stesso gruppo, o in due gruppi che dichiarano
                # la stessa coppia — farebbe raccogliere **due volte la stessa
                # riga** di listino, e un abbinamento certo diventerebbe
                # `EAN_AMBIGUO`: cioe' una domanda all'AI al posto di un
                # abbinamento. La difesa gemella che stava in `build_matching`
                # e' stata tolta perche' una controprova l'ha trovata inerte.
                if altro != codice and altro not in altri:
                    altri.append(altro)
    return mappa


def build_matching(
    master: list[dict[str, Any]],
    sources: dict[str, list[dict[str, Any]]],
    *,
    uguaglianze: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Abbina ogni prodotto del gestionale alle righe dei listini, per EAN.

    `uguaglianze` sono i gruppi di codici a barre che l'utente ha dichiarato
    essere lo stesso articolo (`conferme.MagazzinoConferme.classi`). Senza,
    questa funzione fa **esattamente** quello che faceva prima: e' la ragione
    per cui la ricerca nativa resta sul codice grezzo e le uguaglianze usano un
    secondo indice, invece di normalizzare tutto e cambiare la base dei 933
    abbinamenti che gia' funzionano.

    A che cosa serve, col caso vero: `LUXA SAPONE LIQ. EROG.250ML` sta nel
    gestionale col codice 4009428623194, che solo CIPRESSO usa; NOCE, LARICE
    e BETULLA hanno lo stesso articolo sotto 8729721830575. Nessun punteggio puo'
    dedurlo — nel nome del gestionale la variante non c'e' — ma una volta che
    qualcuno l'ha detto, i tre fornitori entrano come `EAN_ESATTO` nativi e a
    valle non serve nessun caso speciale.
    """

    per_codice = mappa_delle_uguaglianze(uguaglianze)
    indexes: dict[str, dict[str, list[dict[str, Any]]]] = {}
    # Il secondo indice esiste solo per le uguaglianze: le chiavi sono i codici
    # ridotti a cifre, perche' un codice dichiarato a mano e uno letto da un
    # foglio di calcolo non si scrivono quasi mai allo stesso modo.
    indici_per_cifre: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for source_name, records in sources.items():
        index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        per_cifre: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            if record.get("ean"):
                index[record["ean"]].append(record)
                chiave = solo_cifre(record["ean"])
                if chiave:
                    per_cifre[chiave].append(record)
        indexes[source_name] = index
        indici_per_cifre[source_name] = per_cifre

    matching = []
    semantic_queue = []
    exact_presence_counts = Counter()
    exact_unique_usable_counts = Counter()
    equivalence_counts = Counter()
    master_matched_by_presence = set()
    for product in master:
        product_result = {"gestionale": product, "suppliers": {}}
        altri_codici = per_codice.get(solo_cifre(product.get("ean")), []) if product.get("ean") else []
        for source_name, index in indexes.items():
            candidates = index.get(product["ean"], []) if product.get("ean") else []
            # Le righe che arrivano da un'uguaglianza dichiarata, in coda alle
            # native. ⚠ Non c'e' nessuna guardia contro i doppioni, e non e' una
            # dimenticanza: `mappa_delle_uguaglianze` toglie i codici ripetuti
            # dentro il gruppo **e** esclude il codice del prodotto stesso, e una
            # riga di listino ha un solo EAN — quindi nessuna riga puo' arrivare
            # da due strade. La guardia c'era, ed e' stata tolta perche' una
            # controprova l'ha trovata **inerte**: una riga che sembra una difesa
            # e non lo e' e' peggio di una difesa che manca, perche' la prossima
            # persona la conta.
            da_uguaglianza = [
                candidate
                for altro in altri_codici
                for candidate in indici_per_cifre[source_name].get(altro, [])
            ]
            if da_uguaglianza:
                candidates = [*candidates, *da_uguaglianza]
                equivalence_counts[source_name] += 1
                product_result.setdefault("uguaglianze", {})[source_name] = sorted(
                    {solo_cifre(candidate.get("ean")) for candidate in da_uguaglianza}
                )
            usable_candidates = [candidate for candidate in candidates if candidate.get("usable", True)]
            if candidates:
                exact_presence_counts[source_name] += 1
                master_matched_by_presence.add(product["ean"])
            if len(usable_candidates) == 1:
                status = "EAN_ESATTO"
                exact_unique_usable_counts[source_name] += 1
            elif len(usable_candidates) > 1:
                status = "EAN_AMBIGUO"
            elif candidates:
                status = "EAN_PRESENTE_NON_UTILIZZABILE"
            else:
                status = "EAN_ASSENTE"
            voce = {"status": status, "candidates": candidates, "usable_candidates": usable_candidates}
            # Da dove viene l'abbinamento: senza questo, una riga entrata per una
            # dichiarazione umana e' indistinguibile da una trovata dal codice a
            # barre, e chi guarda un ordine sbagliato non ha modo di risalire.
            if (product_result.get("uguaglianze") or {}).get(source_name):
                voce["via_uguaglianza"] = product_result["uguaglianze"][source_name]
            product_result["suppliers"][source_name] = voce
            if status != "EAN_ESATTO":
                semantic_queue.append(
                    {
                        "gestionale_source_row": product["source_row"],
                        "ean": product["ean"],
                        "description": product["description"],
                        "supplier": source_name,
                        "reason": status,
                    }
                )
        matching.append(product_result)

    audit = {
        "master": source_stats(master),
        "sources": {name: source_stats(records) for name, records in sources.items()},
        "price_summary": {name: price_stats(records) for name, records in sources.items()},
        "exact_ean_presence": dict(exact_presence_counts),
        "exact_unique_usable": dict(exact_unique_usable_counts),
        "matched_by_at_least_one_exact_ean": len(master_matched_by_presence),
        "missing_from_all_exact_ean": len(master) - len(master_matched_by_presence),
        # Quante righe sono entrate perche' qualcuno ha dichiarato due codici
        # uguali, e quante dichiarazioni erano in vigore. Un guadagno che non si
        # conta non e' una scelta piu' di uno scarto che non si conta: e' il solo
        # posto da cui si vede se quelle dichiarazioni stanno ancora servendo.
        "matched_by_declared_equivalence": dict(equivalence_counts),
        "declared_equivalences": len([gruppo for gruppo in uguaglianze or [] if len(gruppo or []) > 1]),
    }
    return matching, semantic_queue, audit


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gestionale", type=Path, required=True)
    parser.add_argument("--betulla", type=Path, required=True)
    parser.add_argument("--larice", type=Path, required=True)
    parser.add_argument("--noce", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    master = read_gestionale(args.gestionale)
    larice, warnings = read_larice(args.larice)
    larice, display_offers, display_summary = integrate_larice_displays(larice, analyse_workbook(args.larice))
    sources = {
        "betulla": read_betulla(args.betulla),
        "larice": larice,
        "noce": read_noce(args.noce),
    }
    matching, semantic_queue, audit = build_matching(master, sources)
    audit["warnings"] = warnings
    audit["displays"] = {"larice": display_summary}
    write_json(args.output / "normalized_sources.json", {"gestionale": master, **sources, "display_offers": {"larice": display_offers}})
    write_json(args.output / "display_offers.json", {"larice": display_offers})
    write_json(args.output / "matching_result.json", matching)
    write_json(args.output / "semantic_queue.json", semantic_queue)
    write_json(args.output / "audit.json", audit)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
