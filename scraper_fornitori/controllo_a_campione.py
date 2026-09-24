#!/usr/bin/env python3
"""Re-check a handful of items spread across the downloaded catalog, against the live site.

`valida_catalogo.py` looks at the file: it says whether it's consistent with
itself and with what the site declared. This module looks at the site:
it re-fetches a few items and checks each is still identical to what was
written in the CSV. This is the check that catches a session that expired
halfway through, a page that shifted, or a price list that changed underneath
the download.

The sample is reproducible — same seed, same pages — and spread out: one page
per equal-sized slice of the catalog, and a random product from that page.
Every sampled item is expected to match: a single mismatch is a reason to
look, not a tolerance margin.

Like `scraper_fornitore.py`, it knows nothing about any particular site: it's
given the `SitoFornitore` object of the scraper that produced the CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

from scraper_fornitore import Credenziali, SitoFornitore, leggi_credenziali


SEME = 20260819
CAMPIONE_PREDEFINITO = 5
LAVORATORI_MASSIMI = 4


def _confronto(riga: dict[str, Any], campi: Sequence[str]) -> tuple[str, ...]:
    return tuple(" ".join(str(riga.get(campo, "")).split()) for campo in campi)


def scegli_pagine(pagine_totali: int, quanti: int, sorte: random.Random) -> list[int]:
    """One random page per equal-sized slice of the catalog."""

    if pagine_totali < 1 or quanti < 1:
        return []
    effettivi = min(pagine_totali, quanti)
    pagine: list[int] = []
    for indice in range(effettivi):
        inizio = indice * pagine_totali // effettivi + 1
        fine = (indice + 1) * pagine_totali // effettivi
        pagine.append(sorte.randint(inizio, fine))
    return pagine


def controlla_gruppo(
    sito: SitoFornitore,
    credenziali: Credenziali,
    bersagli: list[dict[str, str]],
    colonna_pagina: str,
) -> list[dict[str, str]]:
    """Reopen a session and re-check the targets assigned to this group."""

    cliente = sito.crea_cliente(30, 3)
    sito.accedi(cliente, credenziali)
    campi = [campo for campo in sito.campi_csv if campo != colonna_pagina]
    esiti: list[dict[str, str]] = []
    for bersaglio in bersagli:
        pagina = int(bersaglio[colonna_pagina])
        righe_fresche = sito.pagina(cliente, pagina)
        atteso = _confronto(bersaglio, campi)
        quante = sum(_confronto(riga, campi) == atteso for riga in righe_fresche)
        esiti.append(
            {
                colonna_pagina: str(pagina),
                "righe_nella_pagina": str(len(righe_fresche)),
                "corrispondenze": str(quante),
                "esito": "PASSA" if quante == 1 else "FALLITO",
                **{campo: str(bersaglio.get(campo, "")) for campo in campi},
            }
        )
    return esiti


def controllo_a_campione(
    sito: SitoFornitore,
    catalogo_csv: Path,
    esiti_csv: Path,
    riepilogo_json: Path,
    *,
    credenziali: Credenziali,
    quanti: int = CAMPIONE_PREDEFINITO,
    seme: int = SEME,
    colonna_pagina: str = "pagina_catalogo",
) -> dict[str, Any]:
    """Run the check and write the outcomes and summary. Credentials stay in memory only."""

    with catalogo_csv.open("r", newline="", encoding="utf-8-sig") as flusso:
        righe = list(csv.DictReader(flusso))
    per_pagina: dict[int, list[dict[str, str]]] = {}
    for riga in righe:
        try:
            pagina = int(riga[colonna_pagina])
        except (KeyError, TypeError, ValueError):
            continue
        per_pagina.setdefault(pagina, []).append(riga)
    if not per_pagina:
        raise ValueError(
            f"il catalogo non ha una colonna «{colonna_pagina}» leggibile: non c'e' niente da ricontrollare"
        )

    sorte = random.Random(seme)
    pagine = scegli_pagine(max(per_pagina), quanti, sorte)
    bersagli = [sorte.choice(per_pagina[pagina]) for pagina in pagine]
    if not bersagli:
        # An empty sample would pass on its own: "zero checks, zero failures"
        # is not a verified catalog, and must be reported as an error rather
        # than counted as a pass.
        raise ValueError(f"campione vuoto: --quanti vale {quanti}, e non c'e' niente da controllare")
    lavoratori = min(LAVORATORI_MASSIMI, len(bersagli))
    gruppi = [bersagli[indice::lavoratori] for indice in range(lavoratori)]

    esiti: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=lavoratori) as esecutore:
        attese = [
            esecutore.submit(controlla_gruppo, sito, credenziali, gruppo, colonna_pagina)
            for gruppo in gruppi
        ]
        for attesa in as_completed(attese):
            esiti.extend(attesa.result())
    esiti.sort(key=lambda riga: int(riga[colonna_pagina]))

    esiti_csv.parent.mkdir(parents=True, exist_ok=True)
    campi = list(esiti[0].keys()) if esiti else [colonna_pagina, "esito"]
    csv_temporaneo = esiti_csv.with_suffix(esiti_csv.suffix + ".tmp")
    with csv_temporaneo.open("w", newline="", encoding="utf-8-sig") as flusso:
        scrittore = csv.DictWriter(flusso, fieldnames=campi)
        scrittore.writeheader()
        scrittore.writerows(esiti)
    os.replace(csv_temporaneo, esiti_csv)

    passati = sum(riga["esito"] == "PASSA" for riga in esiti)
    riepilogo = {
        "seme": seme,
        "metodo": "una pagina a caso per ogni fetta di catalogo, poi un prodotto a caso da quella pagina",
        "controlli_chiesti": quanti,
        "pagine_controllate": pagine,
        "controlli_fatti": len(esiti),
        "passati": passati,
        "falliti": len(esiti) - passati,
        "tutti_passati": passati == len(esiti) and len(esiti) == len(pagine),
        "esiti_csv": str(esiti_csv.resolve()),
    }
    json_temporaneo = riepilogo_json.with_suffix(riepilogo_json.suffix + ".tmp")
    json_temporaneo.write_text(json.dumps(riepilogo, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(json_temporaneo, riepilogo_json)
    return riepilogo


def avvia(sito: SitoFornitore, argv: Sequence[str] | None = None) -> int:
    """The `main` of the check, for a concrete scraper."""

    analizzatore = argparse.ArgumentParser(description=__doc__)
    analizzatore.add_argument("catalogo_csv", type=Path)
    analizzatore.add_argument("esiti_csv", type=Path)
    analizzatore.add_argument("riepilogo_json", type=Path)
    analizzatore.add_argument(
        "--quanti",
        type=int,
        default=CAMPIONE_PREDEFINITO,
        help=f"Articoli distribuiti da ricontrollare (predefinito: {CAMPIONE_PREDEFINITO}).",
    )
    analizzatore.add_argument("--colonna-pagina", default="pagina_catalogo")
    argomenti = analizzatore.parse_args(argv)

    riepilogo = controllo_a_campione(
        sito,
        argomenti.catalogo_csv,
        argomenti.esiti_csv,
        argomenti.riepilogo_json,
        credenziali=leggi_credenziali(sito),
        quanti=argomenti.quanti,
        colonna_pagina=argomenti.colonna_pagina,
    )
    print(json.dumps(riepilogo, ensure_ascii=False))
    return 0 if riepilogo["tutti_passati"] else 1


if __name__ == "__main__":
    print(
        "Questo file e' meta' dello scheletro: gli serve il sito dello scraper che ha\n"
        "prodotto il CSV. Chiamalo dal tuo scraper con `avvia(IlMioSito())`, come mostra\n"
        "il README di questa cartella.",
        file=sys.stderr,
    )
    raise SystemExit(2)
