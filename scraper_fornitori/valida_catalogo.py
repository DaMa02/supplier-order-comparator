#!/usr/bin/env python3
"""Check and clean the CSV downloaded from a supplier's site.

This is the second, and more important, half of the skeleton: a scraper that
finishes without errors does not mean the catalog is good. The site can
change while it's being downloaded, a session can expire halfway through, a
page can come back empty. This is where it's decided whether the file is
usable.

Columns are read from the CSV header: this module knows nothing about any
particular supplier. It only needs the names of four columns — product,
price, page, EAN — declared on the command line when they aren't the
defaults.

What blocks and what warns, in short:

- blocks on an empty catalog, an extraction declared incomplete, a
  missing page, rows with no description or no readable price, and a count
  discrepancy bigger than the allowed threshold;
- warns and lets through a small discrepancy (the catalog may have
  changed during extraction), identical duplicates removed, extra pages.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


SCARTO_CONTEGGI_PREDEFINITO = 20
COLONNA_PRODOTTO_PREDEFINITA = "prodotto"
COLONNA_PREZZO_PREDEFINITA = "prezzo"
COLONNA_PAGINA_PREDEFINITA = "pagina_catalogo"
COLONNA_EAN_PREDEFINITA = "ean"


def leggi_prezzo(valore: str) -> Decimal | None:
    """Parse a price written in Italian format (1.234,56).

    A price that can't be parsed doesn't become zero: it becomes `None`, and
    the row is discarded. Zero is a valid price, and a price invented from an
    unreadable cell is exactly the kind of error that costs a wrong order.
    """

    # "\u00a0" is the non-breaking space: many sites put it between the
    # number and the currency, and it's indistinguishable by eye from a
    # normal space in the file.
    testo = valore.strip().replace("\u00a0", "").replace(" ", "")
    if not testo:
        return None
    if not re.fullmatch(r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?", testo):
        return None
    try:
        return Decimal(testo.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def testo_pulito(valore: str | None) -> str:
    return " ".join((valore or "").split())


def leggi_metadati(percorso: Path | None) -> dict[str, Any]:
    if percorso is None or not percorso.is_file():
        return {}
    valore = json.loads(percorso.read_text(encoding="utf-8"))
    if not isinstance(valore, dict):
        raise ValueError(f"i metadati non contengono un oggetto JSON: {percorso}")
    return valore


def intero_o_niente(valore: Any) -> int | None:
    if valore in (None, "") or isinstance(valore, bool):
        return None
    try:
        letto = int(valore)
    except (TypeError, ValueError):
        return None
    return letto if letto >= 0 else None


def scrivi_json(percorso: Path, valore: dict[str, Any]) -> None:
    percorso.parent.mkdir(parents=True, exist_ok=True)
    temporaneo = percorso.with_suffix(percorso.suffix + ".tmp")
    temporaneo.write_text(json.dumps(valore, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporaneo, percorso)


def valida_catalogo(
    csv_ingresso: Path,
    csv_uscita: Path,
    rapporto_json: Path,
    *,
    percorso_metadati: Path | None = None,
    scarto_massimo: int = SCARTO_CONTEGGI_PREDEFINITO,
    colonna_prodotto: str = COLONNA_PRODOTTO_PREDEFINITA,
    colonna_prezzo: str = COLONNA_PREZZO_PREDEFINITA,
    colonna_pagina: str = COLONNA_PAGINA_PREDEFINITA,
    colonna_ean: str = COLONNA_EAN_PREDEFINITA,
) -> dict[str, Any]:
    """Clean the catalog and return the report that decides whether to activate it."""

    if scarto_massimo < 0:
        raise ValueError("lo scarto massimo non puo' essere negativo")
    metadati = leggi_metadati(percorso_metadati)
    with csv_ingresso.open("r", newline="", encoding="utf-8-sig") as flusso:
        lettore = csv.DictReader(flusso)
        colonne = list(lettore.fieldnames or [])
        righe_lette = list(lettore)

    mancanti = [
        colonna
        for colonna in (colonna_prodotto, colonna_prezzo, colonna_pagina)
        if colonna not in colonne
    ]
    if mancanti:
        raise ValueError(
            f"il CSV non ha le colonne {', '.join(mancanti)}: "
            f"l'intestazione trovata e' {', '.join(colonne) or '(vuota)'}"
        )

    righe_uscita: list[dict[str, str]] = []
    viste: set[tuple[str, ...]] = set()
    scartate: Counter[str] = Counter()
    righe_per_pagina: Counter[int] = Counter()
    gruppi_ean: dict[str, list[dict[str, str]]] = defaultdict(list)

    for origine in righe_lette:
        riga = {colonna: testo_pulito(origine.get(colonna)) for colonna in colonne}
        if not riga[colonna_prodotto]:
            scartate["descrizione_vuota"] += 1
            continue
        if leggi_prezzo(riga[colonna_prezzo]) is None:
            scartate["prezzo_vuoto_o_illeggibile"] += 1
            continue

        # The page is provenance, not product identity: two rows that are
        # otherwise identical are a duplicate even if the site returned them
        # on two different pages.
        chiave = tuple(riga[colonna].casefold() for colonna in colonne if colonna != colonna_pagina)
        if chiave in viste:
            scartate["duplicato_identico"] += 1
            continue
        viste.add(chiave)

        try:
            pagina = int(riga[colonna_pagina])
        except ValueError:
            scartate["pagina_non_valida"] += 1
            continue
        if pagina < 1:
            scartate["pagina_non_valida"] += 1
            continue
        righe_per_pagina[pagina] += 1
        righe_uscita.append(riga)
        if colonna_ean in riga and riga[colonna_ean]:
            gruppi_ean[riga[colonna_ean]].append(riga)

    pagine_attese = (
        intero_o_niente(metadati.get("pagine_totali_inizio"))
        or intero_o_niente(metadati.get("pagine_totali"))
        or (max(righe_per_pagina) if righe_per_pagina else 0)
    )
    pagine_mancanti = [
        pagina for pagina in range(1, pagine_attese + 1) if pagina not in righe_per_pagina
    ]
    pagine_in_piu = sorted(pagina for pagina in righe_per_pagina if pagina > pagine_attese)

    conteggi = {
        etichetta: valore
        for etichetta, valore in {
            "inizio": intero_o_niente(metadati.get("articoli_dichiarati_inizio")),
            "fine": intero_o_niente(metadati.get("articoli_dichiarati_fine")),
            "righe_pulite": len(righe_uscita),
        }.items()
        if valore is not None
    }
    valori = list(conteggi.values())
    scarto_osservato = max(
        (
            abs(sinistro - destro)
            for indice, sinistro in enumerate(valori)
            for destro in valori[indice + 1 :]
        ),
        default=0,
    )

    ean_ripetuti = {ean: righe for ean, righe in gruppi_ean.items() if len(righe) > 1}
    ean_ripetuti_stessa_descrizione = sum(
        1
        for righe in ean_ripetuti.values()
        if len({riga[colonna_prodotto].casefold() for riga in righe}) < len(righe)
    )

    csv_uscita.parent.mkdir(parents=True, exist_ok=True)
    csv_temporaneo = csv_uscita.with_suffix(csv_uscita.suffix + ".tmp")
    with csv_temporaneo.open("w", newline="", encoding="utf-8-sig") as flusso:
        scrittore = csv.DictWriter(flusso, fieldnames=colonne)
        scrittore.writeheader()
        scrittore.writerows(righe_uscita)
    os.replace(csv_temporaneo, csv_uscita)

    blocchi: list[dict[str, Any]] = []
    avvisi: list[dict[str, Any]] = []
    if not righe_uscita:
        blocchi.append({"codice": "CATALOGO_VUOTO"})
    if metadati and metadati.get("completo") is not True:
        blocchi.append({"codice": "ESTRAZIONE_INCOMPLETA"})
    if pagine_mancanti:
        blocchi.append({"codice": "PAGINE_MANCANTI", "pagine": pagine_mancanti})
    if scartate["descrizione_vuota"]:
        blocchi.append({"codice": "DESCRIZIONI_VUOTE", "quante": scartate["descrizione_vuota"]})
    if scartate["prezzo_vuoto_o_illeggibile"]:
        blocchi.append(
            {"codice": "PREZZI_NON_VALIDI", "quante": scartate["prezzo_vuoto_o_illeggibile"]}
        )
    if scartate["pagina_non_valida"]:
        blocchi.append(
            {"codice": "PAGINA_CATALOGO_NON_VALIDA", "quante": scartate["pagina_non_valida"]}
        )
    if metadati and "inizio" not in conteggi:
        blocchi.append({"codice": "CONTEGGIO_INIZIALE_NON_DISPONIBILE"})
    if scarto_osservato > scarto_massimo:
        blocchi.append(
            {
                "codice": "SCARTO_CONTEGGI_ECCESSIVO",
                "osservato": scarto_osservato,
                "ammesso": scarto_massimo,
                "conteggi": conteggi,
            }
        )
    elif scarto_osservato:
        avvisi.append(
            {
                "codice": "SCARTO_CONTEGGI_ACCETTATO",
                "osservato": scarto_osservato,
                "ammesso": scarto_massimo,
                "conteggi": conteggi,
                "messaggio": "Il catalogo puo' essere cambiato durante l'estrazione.",
            }
        )
    if metadati and metadati.get("verifica_finale_riuscita") is False:
        avvisi.append(
            {
                "codice": "CONTEGGIO_FINALE_NON_DISPONIBILE",
                "messaggio": (
                    "Il controllo finale sul sito non e' riuscito: sono stati usati il "
                    "conteggio iniziale e le righe estratte."
                ),
            }
        )
    if pagine_in_piu:
        avvisi.append({"codice": "PAGINE_AGGIUNTIVE", "pagine": pagine_in_piu})
    if scartate["duplicato_identico"]:
        avvisi.append(
            {"codice": "DUPLICATI_IDENTICI_RIMOSSI", "quante": scartate["duplicato_identico"]}
        )

    senza_ean = sum(not riga.get(colonna_ean) for riga in righe_uscita)
    ean_fuori_standard = sum(
        bool(riga.get(colonna_ean)) and not re.fullmatch(r"\d{8,14}", riga.get(colonna_ean, ""))
        for riga in righe_uscita
    )
    prezzi_a_zero = sum(leggi_prezzo(riga[colonna_prezzo]) == 0 for riga in righe_uscita)
    accettato = not blocchi
    rapporto = {
        "accettato": accettato,
        "esito": "PASSA_CON_AVVISO" if accettato and avvisi else "PASSA" if accettato else "BLOCCATO",
        "scarto_ammesso": scarto_massimo,
        "scarto_osservato": scarto_osservato,
        "conteggi": conteggi,
        "blocchi": blocchi,
        "avvisi": avvisi,
        "righe_lette": len(righe_lette),
        "righe_pulite": len(righe_uscita),
        "righe_scartate": dict(scartate),
        "pagine_attese": pagine_attese,
        "pagine_presenti": len(righe_per_pagina),
        "pagina_minima": min(righe_per_pagina) if righe_per_pagina else None,
        "pagina_massima": max(righe_per_pagina) if righe_per_pagina else None,
        "pagine_mancanti": pagine_mancanti,
        "pagine_in_piu": pagine_in_piu,
        "righe_senza_ean_tenute": senza_ean,
        "ean_fuori_standard_tenuti": ean_fuori_standard,
        "prezzi_a_zero_tenuti": prezzi_a_zero,
        "ean_ripetuti_tenuti_come_prodotti_distinti": len(ean_ripetuti),
        "righe_con_ean_ripetuto": sum(len(righe) for righe in ean_ripetuti.values()),
        "ean_ripetuti_con_stessa_descrizione": ean_ripetuti_stessa_descrizione,
        "colonne": colonne,
        "metadati": str(percorso_metadati.resolve()) if percorso_metadati else None,
        "csv_pulito": str(csv_uscita.resolve()),
    }
    scrivi_json(rapporto_json, rapporto)
    return rapporto


def main() -> int:
    analizzatore = argparse.ArgumentParser(description=__doc__)
    analizzatore.add_argument("csv_ingresso", type=Path)
    analizzatore.add_argument("csv_uscita", type=Path)
    analizzatore.add_argument("rapporto_json", type=Path)
    analizzatore.add_argument(
        "--metadati",
        type=Path,
        help="metadata.json dello scraper; se manca viene cercato accanto al CSV.",
    )
    analizzatore.add_argument(
        "--scarto-massimo",
        type=int,
        default=SCARTO_CONTEGGI_PREDEFINITO,
        help=f"Scarto massimo fra conteggi e righe (predefinito: {SCARTO_CONTEGGI_PREDEFINITO}).",
    )
    analizzatore.add_argument("--colonna-prodotto", default=COLONNA_PRODOTTO_PREDEFINITA)
    analizzatore.add_argument("--colonna-prezzo", default=COLONNA_PREZZO_PREDEFINITA)
    analizzatore.add_argument("--colonna-pagina", default=COLONNA_PAGINA_PREDEFINITA)
    analizzatore.add_argument("--colonna-ean", default=COLONNA_EAN_PREDEFINITA)
    argomenti = analizzatore.parse_args()
    accanto = argomenti.csv_ingresso.parent / "metadata.json"
    percorso_metadati = argomenti.metadati or (accanto if accanto.is_file() else None)
    rapporto = valida_catalogo(
        argomenti.csv_ingresso,
        argomenti.csv_uscita,
        argomenti.rapporto_json,
        percorso_metadati=percorso_metadati,
        scarto_massimo=argomenti.scarto_massimo,
        colonna_prodotto=argomenti.colonna_prodotto,
        colonna_prezzo=argomenti.colonna_prezzo,
        colonna_pagina=argomenti.colonna_pagina,
        colonna_ean=argomenti.colonna_ean,
    )
    print(json.dumps(rapporto, ensure_ascii=False))
    return 0 if rapporto["accettato"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
