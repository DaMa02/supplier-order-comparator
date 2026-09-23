#!/usr/bin/env python3
"""Ricontrolla sul sito un pugno di articoli sparsi nel catalogo scaricato.

`valida_catalogo.py` guarda il file: dice se e' coerente con se' stesso e con
quello che il sito dichiarava. Questo modulo guarda **il sito**: ripesca
qualche articolo e verifica che sia ancora identico a com'e' stato scritto nel
CSV. E' il controllo che si accorge di una sessione scaduta a meta', di una
pagina slittata o di un listino cambiato sotto i piedi.

Il campione e' riproducibile — stesso seme, stesse pagine — e distribuito: una
pagina per ogni fetta di catalogo di uguale ampiezza, e un prodotto a caso da
quella pagina. Si pretende che **tutti** gli articoli del campione tornino: un
solo scarto e' un motivo per guardare, non un margine di tolleranza.

Come `scraper_fornitore.py`, non sa nulla di nessun sito: gli si passa
l'oggetto `SitoFornitore` dello scraper che ha prodotto il CSV.
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
    """Una pagina a caso per ogni fetta di catalogo di uguale ampiezza."""

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
    """Riapre una sessione e ricontrolla i bersagli assegnati a questo gruppo."""

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
    """Esegue il controllo e scrive esiti e riepilogo. Le credenziali restano in memoria."""

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
        # Un campione vuoto passerebbe da solo: «zero controlli, zero falliti»
        # non e' un catalogo verificato, e va detto invece di essere contato.
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
    """Il `main` del controllo per uno scraper concreto."""

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
