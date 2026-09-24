"""Guided mapping for documents the adapter registry doesn't recognize.

The service only exposes data already present in the halted run's profile:
no path comes from the browser and no arbitrary file can be read. The
confirmed mapping is then tried with the same reader the pipeline uses, so a
preview that looks good can't turn into an empty price list on the next run.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from openpyxl.utils import column_index_from_string, get_column_letter


APP_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = APP_DIR.parent / "scripts"
for cartella in (APP_DIR, SCRIPTS_DIR):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

from inspect_sources import container_format, file_hash  # noqa: E402
from prepare_manifest_sources import (  # noqa: E402
    read_mapped_csv_supplier,
    read_mapped_master,
    read_mapped_xlsx_supplier,
)
from validate_input_manifest import (  # noqa: E402
    errori_del_marcatore,
    incomplete_mapping,
    parole_della_pagina,
)


CAMPI_FORNITORE = {
    "ean",
    "supplier_code",
    "description",
    "pieces_per_carton",
    "unit_price_net",
    "vat",
    "availability",
    "unit",
    # The three below feed commercial conditions (promotions). Without them
    # a new supplier's price list can be mapped but its promotions can't:
    # `commercial_conditions` had to be written by hand into the registry.
    #
    # `promotion_text` is the column where the supplier writes its offers.
    # It isn't a product field — no reader puts it into records — it just
    # says *where* to look. It can overlap an already-assigned column (an
    # offer embedded in the description or in the item-code column), which
    # is why it's the one field exempt from the "one column, one field" rule.
    "reward_description",
    "discount",
    "promotion_text",
}
# Columns allowed to overlap another: they read the same text under a
# different question, not the same field twice.
CAMPI_SOVRAPPONIBILI = {"promotion_text"}
CAMPI_MASTER = {
    "ean",
    "description",
    "last_unit_price",
    "suggested_colli",
    "unit",
    "vat",
}

# Minimum share of comparable rows for a management-software export to pass
# the read-back check.
#
# A real export is never 100% clean: repeated headers mid-list, department
# totals, separator rows. These are a handful out of hundreds, so a genuine
# export stays between 90% and 100% comparable rows. A column pointed at the
# wrong cell instead leaves almost none. The threshold sits well inside that
# gap, on the tolerant side: a document has to be roughly ten times more
# broken than a merely messy one before this check stops it. Under one row in
# ten isn't a document with a few extra rows — it's the wrong column.
QUOTA_MINIMA_MASTER = 0.10

# Below this match score, no supplier is proposed at all.
#
# `miglior_adattatore` always returns a candidate: if nothing scores, it
# falls back to the first candidate of the right role with 0.0 coverage. The
# page would show it pre-selected in the supplier dropdown, and confirming
# would silently overwrite that supplier's real price list.
#
# Measured against the historical price-list corpus plus the run's own
# documents:
#
#   correct match    BETULLA 1.00 (two files) - CIPRESSO 1.00 (two
#                     files) - NOCE .xls 1.00 - management export 1.00
#   wrong match       LARICE -> noce 0.00 (four files) -
#                     promo sheet -> betulla 0.20 (one header out of five,
#                     and that one is "ORDINE") -
#                     QUERCIA -> management export 0.29 -
#                     GINEPRO -> management export 0.29 -
#                     ACERO -> cipresso 0.33
#
# No document falls between 0.33 and 1.00: the threshold sits in that gap,
# on the safe side at 0.60 — nearly double the highest wrong match — while
# still admitting the case where a match should exist: a supplier renaming
# one column. With five required headers, one renamed still scores 0.80,
# two renamed scores 0.60 and is still proposed; three renamed drops to 0.40,
# below the threshold. Two headers out of five isn't recognition, it's
# coincidence.
SOGLIA_DELLA_PROPOSTA = 0.60
# ...and never on a single matched header, whatever the percentage: an
# adapter with two required headers would score 1.00 by matching both, but
# one with a single required header would score 1.00 on one matched word.
# The sparsest signature today has five required headers, so this floor
# doesn't currently bind; it stays as a guard since the registry is written
# from the mapping page.
INTESTAZIONI_MINIME_DELLA_PROPOSTA = 2

# Fallback aliases for an unrecognized schema. Known headers aren't listed
# here: they come from the registry's own `header_aliases`, the single
# source of truth for recognized suppliers.
ALIAS_GENERICI: dict[str, tuple[str, ...]] = {
    "ean": ("ean", "cod ean", "codice ean", "codice a barre", "barcode", "gtin"),
    "supplier_code": ("cod art", "codice articolo", "codice", "sku", "product code"),
    "description": (
        "descrizione",
        "des articolo",
        "descr commerciale",
        "descrizione articolo",
        "prodotto",
        "product",
    ),
    "pieces_per_carton": (
        "pz ct",
        "pezzi per cartone",
        "pezzi x cartone",
        "pezzi collo",
        "qt",
        "packaging",
        "pack",
    ),
    "unit_price_net": ("cessione", "listino", "prezzo", "prezzo netto", "unit price", "price"),
    "last_unit_price": ("prezzo", "ultimo prezzo", "prezzo unitario"),
    "suggested_colli": ("colli", "cartoni", "quantita ordine", "quantita"),
    "vat": ("iva", "vat"),
    "availability": ("disponibilita", "disponibile", "availability"),
    "unit": ("um", "unita misura", "unit"),
    "order_quantity": ("ordine", "quantita ordine", "order quantity"),
}


def normalizza(valore: Any) -> str:
    testo = unicodedata.normalize("NFKD", str(valore or ""))
    testo = "".join(carattere for carattere in testo if not unicodedata.combining(carattere))
    return re.sub(r"[^a-z0-9]+", "", testo.casefold())


def slug_fornitore(nome: str) -> str:
    testo = unicodedata.normalize("NFKD", nome)
    testo = "".join(carattere for carattere in testo if not unicodedata.combining(carattere))
    return re.sub(r"[^a-z0-9]+", "_", testo.casefold()).strip("_")


def carica_adattatori(percorso: Path) -> list[dict[str, Any]]:
    """The effective registry: shipped adapters plus locally learned ones.

    A learned adapter must show up here too, or the guided mapping proposes
    that supplier as unknown again and the user re-maps by hand a schema the
    program already knows.

    Raises when nothing could be read: someone mapping a schema needs to know
    the registry is broken, rather than seeing an empty supplier list as if
    none were known. A broken learned registry over a good shipped one
    doesn't stop the run — it falls back to the shipped one, and the reason
    is reported by `registro.motivo_registro_illeggibile`.
    """

    from registro import adattatori_effettivi  # noqa: PLC0415 - import tardivo come gli altri

    voci, motivo = adattatori_effettivi(percorso)
    if motivo is not None and not voci:
        raise ValueError("Il registro dei fornitori non si legge.")
    return voci


def nome_dichiarato(supplier_id: str, adattatori: list[dict[str, Any]]) -> str:
    """This supplier's display name, from the adapters already loaded.

    Delegates to `scripts/registro.py`'s tie-breaking rule instead of
    reimplementing it: given the same `supplier_id`, the shortest name wins,
    since the longer one usually describes the document rather than the
    supplier. The registry is already open and is passed in as a parameter.
    """

    from registro import nome_del_fornitore_fra  # noqa: PLC0415 - import tardivo come gli altri

    return nome_del_fornitore_fra(supplier_id, adattatori)


def fogli_del_profilo(profilo: dict[str, Any]) -> list[dict[str, Any]]:
    dettagli = profilo.get("details") or {}
    fogli = dettagli.get("sheets")
    if isinstance(fogli, list):
        return [foglio for foglio in fogli if isinstance(foglio, dict)]
    # A CSV is a single logical sheet. Keeping the same shape simplifies both
    # the page and validation, without inventing a sheet name it doesn't have.
    return [{
        "name": "",
        "active_range": dettagli.get("active_range") or {},
        "header_candidates": dettagli.get("header_candidates") or [],
        "header_rows": dettagli.get("header_rows") or [],
        "section_breaks": dettagli.get("section_breaks") or [],
        "section_rows": dettagli.get("section_rows") or [],
        "samples": dettagli.get("samples") or {},
        "columns": dettagli.get("columns") or [],
    }]


# How many rows the preview can carry. Set above the profile's own maximum
# (20 header + 24 section + 5 middle + 12 final samples), so the rows around
# a section break — the only ones that show where the price list actually
# starts — aren't pushed out by the document's trailing rows. The cap still
# guards against an unbounded response.
RIGHE_DELL_ANTEPRIMA = 64


def righe_visibili(foglio: dict[str, Any]) -> list[dict[str, Any]]:
    per_numero: dict[int, dict[str, Any]] = {}
    gruppi: list[Iterable[Any]] = [
        foglio.get("header_rows") or [],
        foglio.get("header_candidates") or [],
        # Rows around a section break: without them, entering the first
        # product row still shows the promotional block above it instead.
        foglio.get("section_rows") or [],
    ]
    campioni = foglio.get("samples") or {}
    for nome in ("initial", "middle", "final"):
        gruppi.append(campioni.get(nome) or [])
    for gruppo in gruppi:
        for voce in gruppo:
            if not isinstance(voce, dict):
                continue
            try:
                numero = int(voce.get("row"))
            except (TypeError, ValueError):
                continue
            valori = voce.get("values")
            if isinstance(valori, list):
                per_numero[numero] = {"row": numero, "values": valori}
    # The leading rows are the genuinely useful part of the preview. Middle
    # and final samples stay available, but the cap keeps an unusual profile
    # from inflating the response without bound.
    return [per_numero[numero] for numero in sorted(per_numero)[:RIGHE_DELL_ANTEPRIMA]]


def valori_riga(foglio: dict[str, Any], numero: int) -> list[Any]:
    for voce in righe_visibili(foglio):
        if voce["row"] == numero:
            return list(voce["values"])
    return []


def righe_candidate(foglio: dict[str, Any]) -> list[int]:
    numeri: list[int] = []
    for voce in [*(foglio.get("header_candidates") or []), *(foglio.get("header_rows") or [])]:
        if not isinstance(voce, dict):
            continue
        try:
            numero = int(voce.get("row"))
        except (TypeError, ValueError):
            continue
        if numero >= 1 and numero not in numeri:
            numeri.append(numero)
    return numeri or [1]


def compatibile_formato(adattatore: dict[str, Any], profilo: dict[str, Any]) -> bool:
    tipi = {str(voce).casefold() for voce in (adattatore.get("file_types") or [])}
    suffisso = str(profilo.get("declared_suffix") or "").casefold()
    return not tipi or suffisso in tipi


def punteggio_adattatore(
    profilo: dict[str, Any], adattatore: dict[str, Any]
) -> tuple[float, int, int, str] | None:
    firma = adattatore.get("header_signature") or {}
    richieste = {normalizza(voce) for voce in (firma.get("required") or []) if normalizza(voce)}
    if not richieste:
        return None
    migliore: tuple[float, int, int, str] | None = None
    for foglio in fogli_del_profilo(profilo):
        for riga in righe_candidate(foglio):
            osservate = {normalizza(voce) for voce in valori_riga(foglio, riga) if normalizza(voce)}
            trovate = len(richieste & osservate)
            copertura = trovate / len(richieste)
            # Format is only a tie-breaker. Recognition is still based on
            # content: a renamed .xlsx doesn't change which supplier it is.
            candidato = (copertura, trovate, int(compatibile_formato(adattatore, profilo)), str(foglio.get("name") or ""))
            if migliore is None or candidato > migliore:
                migliore = candidato[:3] + (f"{candidato[3]}\n{riga}",)
    return migliore


def miglior_adattatore(
    profilo: dict[str, Any], adattatori: list[dict[str, Any]], *, ruolo: str | None = None,
    supplier_id: str | None = None,
) -> tuple[dict[str, Any] | None, str, int, float]:
    candidati: list[tuple[float, int, int, dict[str, Any], str, int]] = []
    for adattatore in adattatori:
        tipo = "master" if adattatore.get("kind") == "master" else "supplier"
        if ruolo and tipo != ruolo:
            continue
        if supplier_id and str(adattatore.get("supplier_id") or "").casefold() != supplier_id.casefold():
            continue
        punteggio = punteggio_adattatore(profilo, adattatore)
        if punteggio is None:
            continue
        copertura, trovate, formato, posizione = punteggio
        foglio, riga = posizione.rsplit("\n", 1)
        candidati.append((copertura, trovate, formato, adattatore, foglio, int(riga)))
    if not candidati:
        ripiego = next((voce for voce in adattatori if (
            (not ruolo or ("master" if voce.get("kind") == "master" else "supplier") == ruolo)
            and (not supplier_id or str(voce.get("supplier_id") or "").casefold() == supplier_id.casefold())
        )), None)
        foglio = str((fogli_del_profilo(profilo)[0] or {}).get("name") or "")
        return ripiego, foglio, righe_candidate(fogli_del_profilo(profilo)[0])[0], 0.0
    candidati.sort(key=lambda voce: (voce[0], voce[1], voce[2], str(voce[3].get("id") or "")), reverse=True)
    copertura, _trovate, _formato, adattatore, foglio, riga = candidati[0]
    return adattatore, foglio, riga, copertura


def proposta_credibile(
    profilo: dict[str, Any], adattatore: dict[str, Any] | None, copertura: float
) -> bool:
    """Whether this candidate deserves to appear pre-selected in the dropdown.

    Deliberately not applied inside `miglior_adattatore`: that function is
    also used by `adattatore_per_scelta`, which looks up the adapter for a
    supplier the user just picked by hand. There, low coverage is expected —
    the document doesn't resemble anything, which is why guided mapping is
    showing at all — and rejecting it would make it impossible to assign a
    new price list to an already-known supplier. This threshold gates only
    what the program proposes on its own.
    """

    if adattatore is None or copertura < SOGLIA_DELLA_PROPOSTA:
        return False
    punteggio = punteggio_adattatore(profilo, adattatore)
    return punteggio is not None and punteggio[1] >= INTESTAZIONI_MINIME_DELLA_PROPOSTA


def alias_per(adattatore: dict[str, Any] | None, ruolo: str) -> dict[str, set[str]]:
    risultato = {campo: {normalizza(alias) for alias in aliases} for campo, aliases in ALIAS_GENERICI.items()}
    if isinstance(adattatore, dict):
        for campo, aliases in (adattatore.get("header_aliases") or {}).items():
            destinazione = str(campo)
            if destinazione in {"order_quantity_ignored", "order_quantity"}:
                destinazione = "order_quantity"
            if destinazione not in risultato:
                risultato[destinazione] = set()
            if isinstance(aliases, list):
                risultato[destinazione].update(normalizza(alias) for alias in aliases)
    ammessi = CAMPI_MASTER | {"order_quantity"} if ruolo == "master" else CAMPI_FORNITORE | {"order_quantity"}
    return {campo: aliases - {""} for campo, aliases in risultato.items() if campo in ammessi}


def suggerisci_colonne(
    foglio: dict[str, Any], riga: int, adattatore: dict[str, Any] | None, ruolo: str
) -> tuple[dict[str, int], int | None]:
    valori = valori_riga(foglio, riga)
    normali = [normalizza(valore) for valore in valori]
    risultato: dict[str, int] = {}
    for campo, aliases in alias_per(adattatore, ruolo).items():
        indici = [indice for indice, nome in enumerate(normali, start=1) if nome and nome in aliases]
        if len(indici) == 1:
            if campo == "order_quantity":
                continue
            risultato[campo] = indici[0]
    ordine = next((indice for indice, nome in enumerate(normali, start=1)
                   if nome and nome in alias_per(adattatore, ruolo).get("order_quantity", set())), None)
    if ordine is None and isinstance(adattatore, dict):
        dichiarato = str((adattatore.get("order_write") or {}).get("order_column") or "").strip().upper()
        if re.fullmatch(r"[A-Z]{1,3}", dichiarato):
            numero = 0
            for lettera in dichiarato:
                numero = numero * 26 + ord(lettera) - ord("A") + 1
            ordine = numero
    return risultato, ordine


def foglio_per_nome(profilo: dict[str, Any], nome: str) -> dict[str, Any]:
    fogli = fogli_del_profilo(profilo)
    if len(fogli) == 1 and str(fogli[0].get("name") or "") == str(nome or ""):
        return fogli[0]
    for foglio in fogli:
        if str(foglio.get("name") or "") == str(nome or ""):
            return foglio
    raise ValueError(f"Foglio non trovato nel profilo: {nome or 'primo foglio'}")


def colonna_massima(foglio: dict[str, Any]) -> int:
    intervallo = foglio.get("active_range") or {}
    dichiarata = int(intervallo.get("max_column") or 0)
    profilate = max((int(voce.get("index") or 0) for voce in (foglio.get("columns") or []) if isinstance(voce, dict)), default=0)
    visibili = max((len(voce.get("values") or []) for voce in righe_visibili(foglio)), default=0)
    return max(dichiarata, profilate, visibili, 1)


def serializza_foglio(foglio: dict[str, Any]) -> dict[str, Any]:
    attivo = foglio.get("active_range") or {}
    return {
        "name": str(foglio.get("name") or ""),
        "maxRow": int(attivo.get("max_row") or 0),
        "maxColumn": colonna_massima(foglio),
        "headerRows": righe_candidate(foglio),
        "rows": righe_visibili(foglio),
        # Section breaks: what text marks them and which row data resumes
        # from. This is what the page offers instead of a guessed row number.
        "sectionBreaks": deepcopy(foglio.get("section_breaks") or []),
        "columns": deepcopy(foglio.get("columns") or []),
    }


def fornitori_disponibili(adattatori: list[dict[str, Any]]) -> list[dict[str, str]]:
    per_id: dict[str, str] = {}
    for voce in adattatori:
        identificativo = str(voce.get("supplier_id") or "").strip().casefold()
        if not identificativo:
            continue
        etichetta = str(voce.get("display_name") or identificativo).strip()
        # Two adapters for the same supplier (e.g. XLS and CSV variants)
        # don't turn into two identical entries in the page.
        per_id.setdefault(identificativo, etichetta.split(" listino ", 1)[0])
    return [{"id": chiave, "name": per_id[chiave]} for chiave in sorted(per_id)]


# What changed in a document the registry already recognizes, phrased for
# whoever is looking at the file. Internal check names don't belong here —
# they're code identifiers, not user-facing wording.
COSA_E_CAMBIATO = {
    "foglio": "il nome del foglio",
    "riga_intestazione": "la riga delle intestazioni",
    "colonne_attese": "una colonna dichiarata non c'è più",
    "posizioni_intestazioni": "l'ordine delle colonne",
    "tipi_plausibili": "il tipo di dato di una colonna",
    "righe_dati": "le righe di prodotto",
}


def cambiamenti_del_documento(indizio: dict[str, Any]) -> list[str]:
    """Failed checks, in user-facing Italian and without duplicates."""

    fuori: list[str] = []
    for verifica in (indizio.get("checks") or []):
        if not isinstance(verifica, dict) or verifica.get("ok") is not False:
            continue
        frase = COSA_E_CAMBIATO.get(str(verifica.get("name") or ""))
        if frase and frase not in fuori:
            fuori.append(frase)
    return fuori


def _adattatore_per_id(identificativo: str, adattatori: list[dict[str, Any]]) -> dict[str, Any]:
    for voce in adattatori:
        if str(voce.get("id") or "") == identificativo:
            return voce
    return {}


def ruoli_gia_occupati(
    profili: list[dict[str, Any]], richiesti: set[str], adattatori: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Who, in this run, already has a document that skips guided mapping.

    Surfaces before confirmation what would otherwise only be discovered
    after: if the document being mapped is assigned to a supplier that
    already has a price list in this run, `PipelineJobManager.
    _piu_recente_per_ruolo` keeps only the one with the most recent
    `modified_at`, and the other silently drops out of the comparison.

    The key matches that logic: "master", or "supplier:<supplier_id>" taken
    from the adapter the registry recognized. A document the registry
    doesn't recognize occupies nothing — it's one of the ones currently
    going through guided mapping.
    """

    per_id = {str(voce.get("id") or ""): voce for voce in adattatori}
    occupati: list[dict[str, str]] = []
    for profilo in profili:
        nome = str(profilo.get("file_name") or "")
        if nome.casefold() in richiesti:
            continue
        indizio = profilo.get("deterministic_hint") or {}
        if str(indizio.get("state") or "") != "SCHEMA_NOTO":
            continue
        adattatore = per_id.get(str(indizio.get("adapter_id") or ""))
        if adattatore is None:
            continue
        quando = str(profilo.get("modified_at") or "")
        if adattatore.get("kind") == "master":
            occupati.append({
                "role": "master", "supplierId": "",
                "supplierName": "l'elenco del gestionale",
                "fileName": nome, "modifiedAt": quando,
            })
            continue
        supplier_id = str(adattatore.get("supplier_id") or "").strip().casefold()
        if not supplier_id:
            continue
        occupati.append({
            "role": "supplier", "supplierId": supplier_id,
            "supplierName": nome_dichiarato(supplier_id, adattatori),
            "fileName": nome, "modifiedAt": quando,
        })
    # One entry per key, and it must be the one that actually wins: the same
    # rule as `_piu_recente_per_ruolo` — most recently modified file. With
    # two price lists already loaded for the same supplier, the reported
    # entry must match the one that will really be dropped, not just the
    # first one alphabetically.
    per_chiave: dict[str, dict[str, str]] = {}
    for voce in occupati:
        chiave = voce["role"] if voce["role"] == "master" else f"supplier:{voce['supplierId']}"
        vincente = per_chiave.get(chiave)
        if vincente is None or str(voce["modifiedAt"]) >= str(vincente["modifiedAt"]):
            per_chiave[chiave] = voce
    return sorted(per_chiave.values(), key=lambda voce: str(voce.get("fileName") or "").casefold())


def prepara_pendenti(
    profili: list[dict[str, Any]], nomi: list[str], adattatori: list[dict[str, Any]], run_id: str,
    motivi: dict[str, Any] | None = None,
) -> dict[str, Any]:
    richiesti = {nome.casefold() for nome in nomi}
    per_motivo = {str(nome).casefold(): str(valore or "") for nome, valore in (motivi or {}).items()}
    documenti = []
    for profilo in profili:
        nome = str(profilo.get("file_name") or "")
        if nome.casefold() not in richiesti:
            continue
        ruolo_caricato = str(
            profilo.get("upload_role") or (profilo.get("ai_preflight") or {}).get("role") or ""
        ).casefold()
        if ruolo_caricato in {"master", "supplier"}:
            candidati = [
                voce for voce in adattatori
                if ("master" if voce.get("kind") == "master" else "supplier") == ruolo_caricato
            ]
        else:
            candidati = adattatori
        adattatore, nome_foglio, riga, copertura = miglior_adattatore(profilo, candidati)
        # Sheet and row are kept even when the match is dropped below: they
        # mark where the few recognized headers actually sit, which beats
        # defaulting to row 1. What's dropped is the supplier's identity —
        # coverage this low isn't a real match. Losing the adapter also loses
        # its `kind`, so the document shows up in the page as a supplier
        # price list rather than as the management export.
        if not proposta_credibile(profilo, adattatore, copertura):
            adattatore = None
        ruolo = ruolo_caricato if ruolo_caricato in {"master", "supplier"} else (
            "master" if (adattatore or {}).get("kind") == "master" else "supplier"
        )
        foglio = foglio_per_nome(profilo, nome_foglio)
        colonne, ordine = suggerisci_colonne(foglio, riga, adattatore, ruolo)
        if ruolo == "supplier" and "description" not in colonne:
            # A schema that doesn't resemble the management export, and
            # with no proposal, still defaults to "supplier price list":
            # the more common case, leaving one field to correct, not two.
            ruolo = "supplier"
        indizio = profilo.get("deterministic_hint") or {}
        # Two distinct states are reported here. `SCONOSCIUTO`: the registry
        # doesn't recognize this document, a new supplier needs configuring.
        # `VARIATO`: the registry recognizes the supplier, but the document
        # itself changed — the dropdowns arrive pre-filled from the registry
        # and usually just need a glance.
        # `NOTO` covers one path only: manually revisiting columns for a
        # price list the program already recognizes correctly. Nothing else
        # sets a reason there, so without this branch a recognized document
        # would show up marked `SCONOSCIUTO`.
        stato_documento = per_motivo.get(nome.casefold()) or (
            "VARIATO" if str(indizio.get("state") or "") == "SCHEMA_VARIATO"
            else "QUASI" if indizio.get("quasi_adapter_id")
            else "NOTO" if str(indizio.get("state") or "") == "SCHEMA_NOTO"
            else "SCONOSCIUTO"
        )
        gia_noto = (
            _adattatore_per_id(str(indizio.get("adapter_id") or ""), adattatori)
            if stato_documento in {"VARIATO", "RUOLO_SBAGLIATO", "NOTO"} else {}
        )
        # `QUASI`: the registry knows which supplier the document belongs to
        # and knows what's missing to read it. It isn't unknown — treating it
        # as "configure a new supplier" risks overwriting a good shipped
        # adapter with a learned one for what's really just a header cell
        # that moved. `registro` is the single source for this data; it's
        # carried to the page here, not recomputed.
        quasi = {
            "supplierName": str(indizio.get("quasi_supplier_name") or ""),
            "missing": [
                {"header": str(voce.get("header") or ""), "column": str(voce.get("column") or "")}
                for voce in (indizio.get("quasi_missing") or [])
                if isinstance(voce, dict)
            ],
            "present": int(indizio.get("quasi_present") or 0),
        } if stato_documento == "QUASI" else {}
        documenti.append({
            "profileId": str(profilo.get("profile_id") or ""),
            "fileName": nome,
            "format": str(profilo.get("content_format") or (profilo.get("details") or {}).get("format") or ""),
            "sizeBytes": int(profilo.get("size_bytes") or 0),
            "modifiedAt": str(profilo.get("modified_at") or ""),
            "sheets": [serializza_foglio(voce) for voce in fogli_del_profilo(profilo)],
            "reason": {
                "state": stato_documento,
                "supplierName": str(gia_noto.get("display_name") or quasi.get("supplierName") or ""),
                "changed": cambiamenti_del_documento(indizio) if stato_documento == "VARIATO" else [],
                "missing": quasi.get("missing") or [],
                "present": quasi.get("present") or 0,
            },
            "suggestion": {
                "role": ruolo,
                "supplierId": str((adattatore or {}).get("supplier_id") or ""),
                "supplierName": str((adattatore or {}).get("display_name") or ""),
                "sheet": nome_foglio,
                "headerRow": riga,
                "dataStartRow": riga + 1,
                "columns": colonne,
                "orderColumn": ordine,
                "match": round(copertura, 3),
            },
        })
    mancanti = richiesti - {str(voce.get("fileName") or "").casefold() for voce in documenti}
    if mancanti:
        raise ValueError("I profili della run non contengono tutti i documenti da configurare.")
    return {
        "ok": True,
        "required": bool(documenti),
        "runId": run_id,
        "suppliers": fornitori_disponibili(adattatori),
        # Who already has a document in this run. The page uses it to warn,
        # at selection time, which price list would be dropped.
        "occupied": ruoli_gia_occupati(profili, richiesti, adattatori),
        "documents": documenti,
    }


def specifica_colonna(foglio: dict[str, Any], riga: int, indice: int) -> str | int:
    valori = valori_riga(foglio, riga)
    valore = valori[indice - 1] if indice <= len(valori) else None
    token = normalizza(valore)
    if token and sum(1 for voce in valori if normalizza(voce) == token) == 1:
        return str(valore).strip()
    return indice


def intero_positivo(valore: Any, etichetta: str, *, zero: bool = False) -> int:
    if isinstance(valore, bool):
        raise ValueError(f"{etichetta}: serve un numero intero.")
    try:
        numero = int(valore)
    except (TypeError, ValueError):
        raise ValueError(f"{etichetta}: serve un numero intero.") from None
    minimo = 0 if zero else 1
    if numero < minimo:
        raise ValueError(f"{etichetta}: il numero deve essere almeno {minimo}.")
    return numero


def marcatore_dei_dati(grezzo: Any, foglio: dict[str, Any], riga_dati: int, nome: str) -> dict[str, Any] | None:
    """The rule "products start after the row that says X", if one was set.

    A fixed row number doesn't survive a week: some price lists have a
    promotional block of variable length above the real data, ending with a
    single marker cell (e.g. "LISTINO") right before it. A frozen row number
    would silently cut the list at the wrong point once that block's length
    changes.

    The rule isn't trusted blindly: the declared row must be among the ones
    the preview carries, and the declared text must actually be there, in
    that column. A confirmed-without-checking rule would be worse than the
    row number it replaces.

    Page contract (`dataStartMarker`)::

        {"column": 1, "match": "equals", "text": "LISTINO", "offset": 1}

    `column` is the 1-based column number (a letter is also accepted),
    `match` is "equals" or "contains", `offset` is how many rows below the
    marker the products start (1 when omitted).
    """

    if grezzo in (None, "", {}):
        return None
    if not isinstance(grezzo, dict):
        raise ValueError(f"{nome}: la regola dell'inizio dei prodotti non è leggibile.")
    testo = " ".join(str(grezzo.get("text") or "").split())
    if not testo:
        raise ValueError(f"{nome}: scrivi che cosa c'è scritto nella riga che separa i prodotti.")
    confronto = str(grezzo.get("match") or "equals").strip().casefold()
    if confronto not in {"equals", "contains"}:
        raise ValueError(f"{nome}: la regola dell'inizio dei prodotti può essere «equals» o «contains».")
    scarto = intero_positivo(grezzo.get("offset", 1), f"{nome}, righe dopo il separatore", zero=True)
    grezza_colonna = grezzo.get("column")
    if isinstance(grezza_colonna, str) and re.fullmatch(r"[A-Za-z]{1,3}", grezza_colonna.strip()):
        indice = column_index_from_string(grezza_colonna.strip().upper())
    else:
        indice = intero_positivo(grezza_colonna, f"{nome}, colonna del separatore")
    if indice > colonna_massima(foglio):
        raise ValueError(f"{nome}: la colonna {get_column_letter(indice)} non contiene dati da leggere.")

    riga_marcatore = riga_dati - scarto
    if riga_marcatore < 1:
        raise ValueError(f"{nome}: la riga che separa i prodotti finirebbe sopra l'inizio del foglio.")
    valori = valori_riga(foglio, riga_marcatore)
    if not valori:
        raise ValueError(
            f"{nome}: la riga {riga_marcatore} non è fra quelle dell'anteprima, quindi non posso "
            "verificare che contenga il testo indicato. Scegli una riga che si veda nell'anteprima."
        )
    grezza_cella = valori[indice - 1] if indice <= len(valori) else ""
    letto = " ".join(str(grezza_cella if grezza_cella is not None else "").split())
    trovato = letto.casefold() == testo.casefold() if confronto == "equals" else testo.casefold() in letto.casefold()
    if not trovato:
        quale = "contiene" if confronto == "contains" else "è"
        raise ValueError(
            f"{nome}: nella cella {get_column_letter(indice)}{riga_marcatore} c'è "
            f"«{letto}», non una scritta che {quale} «{testo}». Controlla la riga che separa "
            "i prodotti dal resto."
        )
    marcatore = {"column": get_column_letter(indice), confronto: testo, "offset": scarto}
    problemi = errori_del_marcatore(marcatore)
    if problemi:
        # Unreachable today — the line above always builds a valid marker —
        # but if that ever changes, the error should stay in the page's own
        # wording rather than a raw field-path message.
        raise ValueError(f"{nome}: controlla {parole_della_pagina(problemi)}.")
    return marcatore


def adattatore_per_scelta(
    profilo: dict[str, Any], adattatori: list[dict[str, Any]], ruolo: str, supplier_id: str
) -> dict[str, Any] | None:
    adattatore, _foglio, _riga, _copertura = miglior_adattatore(
        profilo, adattatori, ruolo=ruolo, supplier_id=supplier_id or None
    )
    return adattatore


# The mapped field that carries each role of the promotion reader. The
# right-hand names are `column_map` keys — the same form the price reader
# uses — because that's the only form `promotion_bridge` knows how to
# resolve.
CAMPI_DELLE_CONDIZIONI = {
    "text": "promotion_text",
    "reward": "reward_description",
    "ean": "ean",
    "row_code": "discount",
}


def condizioni_commerciali(
    grezza: dict[str, Any], colonne: dict[str, str | int], foglio: dict[str, Any], nome: str
) -> dict[str, Any] | None:
    """Where the supplier writes its promotions, as declared by the mapping.

    Without this field a new supplier's price list reads and compiles, but
    its promotions can't: `commercial_conditions` had to be written by hand
    into `references/adapters.json`, and guided mapping had no field to
    declare it, so `impara_adattatore` never wrote it either.

    Nothing is guessed here. If the mapping doesn't declare the promotion-
    text column, no declaration is produced and that supplier's promotions
    stay unread. If it does, the layout and fields are written, and the same
    engine used for known suppliers reads them.

    The layout lives in the mapping and isn't inferred: `riga` (one row
    carries a whole condition) is the default since most suppliers write it
    that way, but `blocchi` can be declared instead and then requires its own
    columns. Accepting it without them would write a registry declaration
    that fails silently every time it's applied.
    """

    from promotion_bridge import LAYOUT_RIGA, colonne_richieste_dal_layout, nomi_dei_ruoli

    grezze = grezza.get("commercialConditions")
    if not isinstance(grezze, dict):
        return None
    if "promotion_text" not in colonne:
        # Declaring the layout without saying where the text is isn't a
        # declaration: it's an empty checkbox.
        if str(grezze.get("layout") or "").strip():
            raise ValueError(
                f"{nome}: per leggere le offerte di questo fornitore serve la colonna in cui le "
                "scrive. Indicala, oppure lascia le offerte fuori."
            )
        return None

    forma = str(grezze.get("layout") or LAYOUT_RIGA).strip().casefold()
    richieste = colonne_richieste_dal_layout(forma)
    if richieste is None:
        raise ValueError(
            f"{nome}: «{grezze.get('layout')}» non è un modo di scrivere le condizioni che "
            "io sappia leggere."
        )
    etichette = nomi_dei_ruoli()
    campi: dict[str, str] = {}
    mancanti: list[str] = []
    for ruolo, campo in CAMPI_DELLE_CONDIZIONI.items():
        if campo in colonne:
            campi[ruolo] = campo
        elif ruolo in richieste:
            mancanti.append(etichette.get(ruolo, ruolo))
    if mancanti:
        raise ValueError(
            f"{nome}: per leggere le offerte scritte a «{forma}» serve anche "
            + ", ".join(mancanti)
            + ". Indica quelle colonne, oppure scegli un altro modo."
        )
    return {
        "layout": forma,
        "sheet": str(foglio.get("name") or "FIRST"),
        # From row 1: commercial conditions often sit above the product
        # list, in the promotional block, and starting from the first data
        # row would cut all of them out.
        "data_start_row": 1,
        "fields": campi,
    }


def valori_di_disponibilita(grezzi: Any, nome: str) -> list[str]:
    """Which values of the availability column mean "in stock".

    Without this list the reader defaults to `available = True` and never
    looks at the cell (`prepare_manifest_sources`, `available_values`): the
    column could be mapped while rows marked unavailable stayed orderable
    and could win the comparison on price, for the very reason their price
    looked lower.

    The list is declared by whoever has the price list in hand, not guessed
    here: "SI", "S", "disponibile", "X" are all valid for different
    suppliers, and hardcoding one convention would bake a single supplier's
    wording into the code. If the column is mapped and the list is empty,
    confirmation is refused: reading that column without knowing what it
    means is worse than not reading it.

    Page contract (`availableValues`): comma- or newline-separated values,
    e.g. "SI, S, disponibile".
    """

    valori: list[str] = []
    for pezzo in re.split(r"[,\r\n]", str(grezzi or "")):
        valore = pezzo.strip()
        if valore and valore not in valori:
            valori.append(valore)
    if not valori:
        raise ValueError(
            f"{nome}: indica quali valori della colonna «Disponibilità» significano "
            "disponibile (per esempio: SI), separati da virgola."
        )
    return valori


def decisione_da_mappatura(
    profilo: dict[str, Any], grezza: dict[str, Any], adattatori: list[dict[str, Any]]
) -> dict[str, Any]:
    nome = str(profilo.get("file_name") or "documento")
    ruolo = str(grezza.get("role") or "supplier")
    if ruolo not in {"supplier", "master"}:
        raise ValueError(f"{nome}: scegli se il documento è un listino o l'elenco del gestionale.")
    foglio = foglio_per_nome(profilo, str(grezza.get("sheet") or ""))
    max_colonna = colonna_massima(foglio)
    max_riga = int((foglio.get("active_range") or {}).get("max_row") or 0)
    riga_header = intero_positivo(grezza.get("headerRow"), f"{nome}, riga intestazioni")
    riga_dati = intero_positivo(grezza.get("dataStartRow"), f"{nome}, prima riga dati")
    if max_riga and riga_header > max_riga:
        raise ValueError(f"{nome}: la riga delle intestazioni è oltre la fine del foglio.")
    if riga_dati <= riga_header:
        raise ValueError(f"{nome}: la prima riga dati deve stare sotto le intestazioni.")
    if max_riga and riga_dati > max_riga:
        raise ValueError(f"{nome}: la prima riga dati è oltre la fine del foglio.")

    ammessi = CAMPI_MASTER if ruolo == "master" else CAMPI_FORNITORE
    colonne_grezze = grezza.get("columns")
    if not isinstance(colonne_grezze, dict):
        raise ValueError(f"{nome}: la scelta delle colonne non è completa.")
    colonne: dict[str, str | int] = {}
    indici_usati: dict[int, str] = {}
    for campo, valore in colonne_grezze.items():
        if campo not in ammessi or valore in (None, ""):
            continue
        indice = intero_positivo(valore, f"{nome}, colonna {campo}")
        if indice > max_colonna:
            raise ValueError(f"{nome}: la colonna {get_column_letter(indice)} non contiene dati da leggere.")
        precedente = indici_usati.get(indice)
        if precedente and campo not in CAMPI_SOVRAPPONIBILI and precedente not in CAMPI_SOVRAPPONIBILI:
            raise ValueError(
                f"{nome}: la colonna {get_column_letter(indice)} è assegnata sia a {precedente} sia a {campo}."
            )
        # The column is still marked occupied, for the order-column check
        # below, but the remembered field is the real one, not the
        # overlapping `promotion_text`, which would be a meaningless
        # conflict message.
        if precedente is None or campo not in CAMPI_SOVRAPPONIBILI:
            indici_usati[indice] = campo
        colonne[campo] = specifica_colonna(foglio, riga_header, indice)

    mappatura: dict[str, Any] = {
        "header_row": riga_header,
        "data_start_row": riga_dati,
        "columns": colonne,
        "italian_numbers": True,
    }
    marcatore = marcatore_dei_dati(grezza.get("dataStartMarker"), foglio, riga_dati, nome)
    if marcatore:
        mappatura["data_start_marker"] = marcatore
    if str(profilo.get("content_format") or "") != "csv":
        mappatura["sheet"] = str(foglio.get("name") or "FIRST")
    else:
        dettagli = profilo.get("details") or {}
        mappatura["encoding"] = dettagli.get("encoding") or "utf-8-sig"
        mappatura["delimiter"] = dettagli.get("delimiter") or ";"

    if ruolo == "supplier":
        condizioni = condizioni_commerciali(grezza, colonne, foglio, nome)
        if condizioni:
            mappatura["commercial_conditions"] = condizioni
        if "pieces_per_carton" not in colonne and grezza.get("piecesPerCartonDefault") not in (None, ""):
            try:
                fisso = float(str(grezza["piecesPerCartonDefault"]).replace(",", "."))
            except ValueError:
                raise ValueError(f"{nome}: il valore fisso dei pezzi per collo non è un numero.") from None
            if fisso <= 0:
                raise ValueError(f"{nome}: il valore fisso dei pezzi per collo deve essere maggiore di zero.")
            mappatura["pieces_per_carton_default"] = fisso
        if "ean" not in colonne:
            mappatura["ean_unavailable"] = True
        if "supplier_code" not in colonne:
            mappatura["supplier_code_unavailable"] = True
        if "availability" in colonne:
            mappatura["available_values"] = valori_di_disponibilita(grezza.get("availableValues"), nome)
        else:
            mappatura["assume_available"] = True
        if "vat" not in colonne:
            mappatura["vat_unavailable"] = True
        if "supplier_code" in colonne:
            mappatura["text_columns"] = ["supplier_code"]
        if str(profilo.get("content_format") or "") != "csv":
            ordine = intero_positivo(grezza.get("orderColumn"), f"{nome}, colonna ordine")
            if ordine > max_colonna + 1:
                raise ValueError(
                    f"{nome}: la colonna ordine può essere al massimo {get_column_letter(max_colonna + 1)}."
                )
            if ordine in indici_usati:
                raise ValueError(
                    f"{nome}: la colonna ordine non può essere anche la colonna {indici_usati[ordine]}."
                )
            mappatura["order_column"] = get_column_letter(ordine)
            intestazioni = valori_riga(foglio, riga_header)
            intestazione_ordine = intestazioni[ordine - 1] if ordine <= len(intestazioni) else None
            if str(intestazione_ordine or "").strip():
                mappatura["order_header_expected"] = str(intestazione_ordine).strip()
            else:
                # This consent is scoped to these exact bytes and this
                # position. The writer still re-checks the hash, the empty
                # cell and the column contents before writing to the copy.
                mappatura["order_header_blank_confirmed"] = True

    supplier_id = ""
    display_name = ""
    adattatore: dict[str, Any] | None = None
    stato = "SCHEMA_VARIATO"
    if ruolo == "master":
        adattatore = adattatore_per_scelta(profilo, adattatori, ruolo, "")
        if adattatore is None:
            raise ValueError(f"{nome}: nel registro non esiste ancora l'elenco gestionale da aggiornare.")
    else:
        supplier_id = str(grezza.get("supplierId") or "").strip().casefold()
        if supplier_id:
            adattatore = adattatore_per_scelta(profilo, adattatori, ruolo, supplier_id)
            if adattatore is None:
                raise ValueError(f"{nome}: il fornitore scelto non esiste nel registro.")
            display_name = str(adattatore.get("display_name") or supplier_id)
        else:
            display_name = " ".join(str(grezza.get("supplierName") or "").split())
            if len(display_name) < 2 or len(display_name) > 80:
                # The dropdown can also be left on its placeholder, so this
                # message needs to cover both cases — an empty choice and an
                # invalid name — instead of describing only one.
                raise ValueError(
                    f"{nome}: scegli il fornitore, oppure «Nuovo fornitore…» e scrivi il nome."
                )
            supplier_id = slug_fornitore(display_name)
            esistenti = {str(voce.get("supplier_id") or "").casefold() for voce in adattatori}
            if not supplier_id:
                raise ValueError(f"{nome}: il nome del nuovo fornitore non contiene lettere o numeri.")
            if supplier_id in esistenti:
                raise ValueError(f"{nome}: questo fornitore esiste già; selezionalo dall'elenco.")
            stato = "NUOVO_FORNITORE"

    # `incomplete_mapping` decides "is this a CSV?" from the file suffix. A
    # CSV that arrives named .xlsx (the reader picks the parser from the
    # bytes, so this is accepted) would otherwise be asked for a sheet name
    # and an order column, neither of which that document has. The profile
    # already knows the real format, so it's passed in explicitly.
    percorso_dichiarato = Path(str(profilo.get("path") or nome))
    if str(profilo.get("content_format") or "") == "csv":
        percorso_dichiarato = percorso_dichiarato.with_suffix(".csv")
    mancanti = incomplete_mapping(mappatura, ruolo, percorso_dichiarato)
    if mancanti:
        raise ValueError(f"{nome}: completa {parole_della_pagina(mancanti)}.")

    decisione: dict[str, Any] = {
        "file_name": nome,
        # The decision is valid for this document, not for its file name.
        # Without the hash, deleting a misconfigured price list and
        # re-uploading a different file under the same name would silently
        # re-apply the old mapping, columns included.
        "file_sha256": profilo.get("sha256"),
        "profile_id": profilo.get("profile_id"),
        "state": stato,
        "role": ruolo,
        "confidence": "ALTA",
        "rationale": "Colonne controllate e confermate nella pagina Importa.",
        "field_mapping": mappatura,
        "user_confirmation": {"required": True, "status": "CONFIRMED"},
    }
    if adattatore is not None:
        decisione["adapter_id"] = adattatore.get("id")
    if ruolo == "supplier":
        decisione["supplier_id"] = supplier_id
        decisione["display_name"] = display_name
    return decisione


def righe_master_confrontabili(righe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The management-export rows that can actually enter the comparison.

    The management export has neither net price nor pieces-per-carton, so
    "usable" means something different than for a supplier price list, and
    is defined by what actually consumes this data:

    * the EAN is the only key the comparison is built on. `build_matching`
      looks up offers with `index.get(product["ean"])`
      (`scripts/prepare_sources.py`); without it a product comes out as
      `EAN_ASSENTE` for every supplier and no offer attaches to it.
    * the description is what the user reads in the comparison and the only
      key the semantic fallback has; a row missing it that reaches the
      semantic queue makes `valuta_shortlist.py` fail, since it requires a
      non-empty description, taking the whole run down with it.

    The last paid price isn't part of the criterion: it feeds the savings
    figure shown in the page, not participation in the comparison, and an
    item never bought before has none by definition.
    """
    return [
        voce for voce in righe
        if str(voce.get("ean") or "").strip() and str(voce.get("description") or "").strip()
    ]


def colonne_master_da_rivedere(righe: list[dict[str, Any]]) -> str:
    """Which column to check, phrased with the same wording as the page."""
    senza_codice = any(not str(voce.get("ean") or "").strip() for voce in righe)
    senza_nome = any(not str(voce.get("description") or "").strip() for voce in righe)
    nomi = [
        etichetta for etichetta, mancante in
        (("Codice EAN", senza_codice), ("Nome prodotto", senza_nome)) if mancante
    ]
    if not nomi:
        nomi = ["Codice EAN", "Nome prodotto"]
    return " e ".join(f"la colonna {etichetta}" for etichetta in nomi)


def prova_decisione(profilo: dict[str, Any], decisione: dict[str, Any]) -> dict[str, Any]:
    percorso = Path(str(profilo.get("path") or ""))
    mappatura = decisione["field_mapping"]
    ruolo = decisione["role"]
    if ruolo == "master":
        righe = read_mapped_master(percorso, mappatura)
        utilizzabili = righe_master_confrontabili(righe)
        if len(utilizzabili) < max(1, len(righe) * QUOTA_MINIMA_MASTER):
            quante = (
                "non risulta nessun prodotto confrontabile" if not utilizzabili
                else f"restano solo {len(utilizzabili)} righe confrontabili su {len(righe)} lette"
            )
            raise ValueError(
                f"{profilo.get('file_name')}: con queste colonne {quante}. "
                f"Controlla soprattutto {colonne_master_da_rivedere(righe)}."
            )
        campione = [{
            "row": voce.get("source_row"),
            "ean": voce.get("ean"),
            "description": voce.get("description"),
            "price": voce.get("last_unit_price"),
        } for voce in utilizzabili[:8]]
        return {
            "ok": True,
            "rowsRead": len(righe),
            "rowsUsable": len(utilizzabili),
            "rowsDiscarded": max(0, len(righe) - len(utilizzabili)),
            "sample": campione,
        }

    rapporto: dict[str, Any] = {}
    if container_format(percorso) == "csv":
        righe, avvisi = read_mapped_csv_supplier(
            percorso, decisione["supplier_id"], mappatura, report=rapporto
        )
    else:
        righe, avvisi = read_mapped_xlsx_supplier(
            percorso, decisione["supplier_id"], mappatura, report=rapporto
        )
    utilizzabili = [voce for voce in righe if voce.get("usable") is True]
    if not utilizzabili:
        raise ValueError(
            f"{profilo.get('file_name')}: con queste colonne non risulta nessun prodotto ordinabile. "
            "Controlla soprattutto prezzo e pezzi per collo."
        )
    campione = [{
        "row": voce.get("source_row"),
        "ean": voce.get("ean"),
        "supplierCode": voce.get("supplier_code"),
        "description": voce.get("description"),
        "piecesPerCarton": voce.get("pieces_per_carton") or voce.get("order_multiplier"),
        "price": voce.get("unit_price_net"),
    } for voce in utilizzabili[:8]]
    return {
        "ok": True,
        "rowsRead": len(righe),
        "rowsUsable": len(utilizzabili),
        "rowsDiscarded": max(0, len(righe) - len(utilizzabili)),
        "warnings": len(avvisi),
        "reading": rapporto,
        "sample": campione,
    }


def valida_mappature(
    profili: list[dict[str, Any]], nomi: list[str], adattatori: list[dict[str, Any]],
    payload: dict[str, Any], run_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if str(payload.get("runId") or "") != run_id:
        raise ValueError("Questa anteprima appartiene a un confronto precedente. Riapri la configurazione.")
    grezze = payload.get("mappings")
    if not isinstance(grezze, list):
        raise ValueError("Le mappature dei documenti non sono presenti.")
    per_id: dict[str, dict[str, Any]] = {}
    for voce in grezze:
        if not isinstance(voce, dict):
            continue
        identificativo = str(voce.get("profileId") or "")
        if not identificativo or identificativo in per_id:
            raise ValueError("Ogni documento deve comparire una sola volta.")
        per_id[identificativo] = voce

    richiesti = {nome.casefold() for nome in nomi}
    profili_richiesti = [voce for voce in profili if str(voce.get("file_name") or "").casefold() in richiesti]
    if set(per_id) != {str(voce.get("profile_id") or "") for voce in profili_richiesti}:
        raise ValueError("Configura tutti e soltanto i documenti indicati dal confronto.")

    decisioni: list[dict[str, Any]] = []
    risultati: list[dict[str, Any]] = []
    fornitori: set[str] = set()
    for profilo in profili_richiesti:
        percorso = Path(str(profilo.get("path") or ""))
        if not percorso.is_file():
            raise ValueError(f"{profilo.get('file_name')}: il documento non è più fra i caricamenti.")
        # The run profiled these exact bytes. If the file was replaced since
        # then, confirming must not apply one document's mapping to another.
        digest = file_hash(percorso)
        if profilo.get("sha256") and digest != profilo.get("sha256"):
            raise ValueError(f"{profilo.get('file_name')}: il documento è cambiato dopo l'anteprima.")
        decisione = decisione_da_mappatura(profilo, per_id[str(profilo.get("profile_id") or "")], adattatori)
        supplier_id = str(decisione.get("supplier_id") or "")
        if supplier_id and supplier_id in fornitori:
            # The display name comes from the registry, as in every other
            # message: falling back to `supplier_id.upper()` would show a
            # slugified identifier instead of the name the supplier declared.
            raise ValueError(
                "Due documenti sono stati assegnati allo stesso fornitore: "
                f"{nome_dichiarato(supplier_id, adattatori)}."
            )
        fornitori.add(supplier_id)
        risultato = prova_decisione(profilo, decisione)
        risultati.append({
            "profileId": profilo.get("profile_id"),
            "fileName": profilo.get("file_name"),
            **risultato,
        })
        decisioni.append(decisione)
    return {"ok": True, "runId": run_id, "documents": risultati}, decisioni


# ---------------------------------------------------------------------------
# What the program reads, column by column
# ---------------------------------------------------------------------------
# Guided mapping (above) only shows column assignment for an unrecognized
# schema. For a recognized document — the common case — the page otherwise
# has no way to show which column became the price, short of opening the
# price list and counting columns by hand.
#
# These positions are never recomputed by guessing from headers.
# `suggerisci_colonne` is the proposer for a new schema, and on an already
# recognized document it can be wrong — a price list with no header row at
# all, or two fields sharing the same alias so the proposal stops on the
# ambiguity. The single source of truth is the registry declaration
# (`header_aliases`, `column_map`) or the confirmed mapping in the decision,
# resolved against the document by the same two functions the recognizer and
# the writer use.

# The fields readers actually put into records, named the way someone
# placing orders would read them. A field the registry declares but no
# reader consumes doesn't appear here: claiming the program "reads" a
# column it then discards would be misleading.
ETICHETTE_DEI_CAMPI: dict[str, str] = {
    "ean": "Codice a barre (EAN)",
    "supplier_code": "Codice articolo del fornitore",
    "description": "Nome del prodotto",
    "unit": "Unità di misura",
    "pieces_per_carton": "Pezzi per collo",
    "unit_price_net": "Prezzo netto",
    "unit_price_pre_discount": "Prezzo prima dello sconto",
    "discount": "Sconto",
    "vat": "IVA",
    "availability": "Disponibilità",
    "pallet": "Pedana",
    "reward_description": "Premio dell'offerta",
    "promotion_text": "Dove il fornitore scrive le sue offerte",
    "last_unit_price": "Ultimo prezzo pagato",
    "suggested_colli": "Colli chiesti dal gestionale",
    "source_quantity_ignored": "Quantità del gestionale (non usata: contano i colli)",
    "source_discount": "Sconto del gestionale",
    "offer_flag": "Segnalazione di offerta",
    "category": "Categoria",
}

CAMPI_LETTI_FORNITORE: tuple[str, ...] = (
    "ean",
    "supplier_code",
    "description",
    "unit",
    "pieces_per_carton",
    "unit_price_net",
    "unit_price_pre_discount",
    "discount",
    "vat",
    "availability",
    "pallet",
    "reward_description",
    "offer_flag",
    "category",
)
CAMPI_LETTI_MASTER: tuple[str, ...] = (
    "ean",
    "description",
    "unit",
    "suggested_colli",
    "last_unit_price",
    "source_quantity_ignored",
    "source_discount",
    "vat",
)


def _etichetta_del_campo(campo: str) -> str:
    return ETICHETTE_DEI_CAMPI.get(campo, campo.replace("_", " "))


def _riga_del_foglio(foglio: dict[str, Any], numero: Any) -> list[Any]:
    try:
        cercata = int(numero)
    except (TypeError, ValueError):
        return []
    for riga in righe_visibili(foglio):
        if int(riga.get("row") or 0) == cercata:
            return list(riga.get("values") or [])
    return []


def _primo_esempio(foglio: dict[str, Any], indice: int, dalla_riga: Any) -> str:
    """A real value from that column, taken from the product rows.

    Answers the question that actually matters when checking a mapping: is
    this really the price column? A header can be misleading; a value isn't.
    """

    try:
        inizio = int(dalla_riga)
    except (TypeError, ValueError):
        inizio = 0
    for riga in righe_visibili(foglio):
        if int(riga.get("row") or 0) < inizio:
            continue
        valori = riga.get("values") or []
        if indice <= len(valori):
            testo = str(valori[indice - 1] if valori[indice - 1] is not None else "").strip()
            if testo:
                return testo[:60]
    return ""


def colonne_del_foglio(
    profilo: dict[str, Any],
    nome_foglio: Any,
    riga_intestazioni: Any,
    riga_dati: Any,
    fino_a: Any = None,
) -> list[dict[str, Any]]:
    """Every column of a sheet, with its header, a sample value and its content.

    `mappatura_effettiva` says which columns the program uses; this says
    which columns exist, which is what someone choosing a column needs. The
    type counts aren't sampled: `inspect_sources` walks every row of every
    sheet, so `formule` is an exact count — and it matters, because writing
    the order quantity into a formula column would overwrite the formula.

    Goes one column past the last one with data: the order column can be the
    first empty one past the end, for a price list that doesn't have one
    yet. `fino_a` extends the range further, for the case where today's
    order column is already past the last column that has any data in the
    profile at all; without it, the one column that couldn't be chosen would
    be the one right next to the one already in use.
    """

    fogli = fogli_del_profilo(profilo)
    foglio = next(
        (voce for voce in fogli if str(voce.get("name") or "") == str(nome_foglio or "")),
        fogli[0] if fogli else {},
    )
    intestazioni = _riga_del_foglio(foglio, riga_intestazioni)
    per_indice: dict[int, dict[str, Any]] = {}
    for voce in foglio.get("columns") or []:
        if not isinstance(voce, dict):
            continue
        try:
            per_indice[int(voce.get("index"))] = voce
        except (TypeError, ValueError):
            continue
    try:
        richiesta = int(fino_a)
    except (TypeError, ValueError):
        richiesta = 1
    ultima = max([*per_indice, len(intestazioni), richiesta, 1])
    prima_riga_dati = riga_dati or ((riga_intestazioni or 0) + 1)
    colonne: list[dict[str, Any]] = []
    for indice in range(1, ultima + 2):
        statistiche = per_indice.get(indice) or {}
        tipi = statistiche.get("types") if isinstance(statistiche.get("types"), dict) else {}
        intestazione = ""
        if indice <= len(intestazioni) and intestazioni[indice - 1] is not None:
            intestazione = str(intestazioni[indice - 1]).strip()
        colonne.append({
            "colonna": indice,
            "lettera": get_column_letter(indice),
            "intestazione": intestazione,
            "esempio": _primo_esempio(foglio, indice, prima_riga_dati),
            "valori": int(statistiche.get("nonempty") or 0),
            "formule": int(tipi.get("formula") or 0),
            "testo": int(tipi.get("text") or 0),
            "numeri": int(tipi.get("number") or 0),
        })
    return colonne


def mappatura_effettiva(
    profilo: dict[str, Any],
    adattatore: dict[str, Any] | None,
    decisione: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The columns the program actually uses to read this document.

    Not a proposal and not a reconstruction: it's the declaration the
    readers follow, resolved against the document with
    `registro.posizione_del_campo` and `registro.indice_della_colonna` — the
    same two functions used by position verification during recognition and
    by the writer before it writes an order quantity. If the registry and
    the reader ever disagreed, this table would show it instead of hiding
    it.

    Always returns something: a document nothing is known about comes back
    with an empty column list and `recognised: False`, which is itself
    useful information for the page.
    """

    from registro import indice_della_colonna, posizione_del_campo  # noqa: PLC0415

    decisione = decisione if isinstance(decisione, dict) else {}
    adattatore = adattatore if isinstance(adattatore, dict) else {}
    mappatura = decisione.get("field_mapping")
    mappatura = mappatura if isinstance(mappatura, dict) else (adattatore.get("field_mapping") or {})
    mappatura = mappatura if isinstance(mappatura, dict) else {}
    firma_registro = adattatore.get("header_signature")
    firma_registro = firma_registro if isinstance(firma_registro, dict) else {}
    firma_osservata = (profilo.get("deterministic_hint") or {}).get("signature")
    firma_osservata = firma_osservata if isinstance(firma_osservata, dict) else {}

    ruolo = str(decisione.get("role") or ("master" if adattatore.get("kind") == "master" else "supplier"))
    # Header row: the confirmed one wins over the one observed during
    # recognition, which wins over the registry's own fallback. The
    # fallback comes last on purpose — `gestionale_v1` declares row 1 even
    # though the real export's header is on row 2.
    riga_intestazioni = _primo_numero(
        mappatura.get("header_row"), firma_osservata.get("header_row"), firma_registro.get("header_row")
    )
    riga_dati = _primo_numero(
        mappatura.get("data_start_row"),
        firma_osservata.get("data_start_row"),
        firma_registro.get("data_start_row"),
        (adattatore.get("order_write") or {}).get("data_start_row"),
    )
    nome_foglio = str(
        mappatura.get("sheet")
        or firma_osservata.get("sheet")
        or ""
    )
    fogli = fogli_del_profilo(profilo)
    foglio = next(
        (voce for voce in fogli if str(voce.get("name") or "") == nome_foglio),
        fogli[0] if fogli else {},
    )
    if not nome_foglio:
        nome_foglio = str(foglio.get("name") or "")
    intestazioni = _riga_del_foglio(foglio, riga_intestazioni)

    campi = CAMPI_LETTI_MASTER if ruolo == "master" else CAMPI_LETTI_FORNITORE
    colonne: list[dict[str, Any]] = []
    for campo in campi:
        dichiarata = posizione_del_campo(adattatore, mappatura, campo)
        if dichiarata in (None, ""):
            continue
        indice = indice_della_colonna(intestazioni, dichiarata)
        if indice is None:
            # Declared but not found: exactly the case the user needs to
            # see, not one to skip silently.
            colonne.append({
                "campo": campo,
                "etichetta": _etichetta_del_campo(campo),
                "colonna": None,
                "lettera": "",
                "intestazione": "",
                "esempio": "",
                "dichiarata": str(dichiarata),
                "trovata": False,
            })
            continue
        intestazione = ""
        if indice <= len(intestazioni) and intestazioni[indice - 1] is not None:
            intestazione = str(intestazioni[indice - 1]).strip()
        colonne.append({
            "campo": campo,
            "etichetta": _etichetta_del_campo(campo),
            "colonna": indice,
            "lettera": get_column_letter(indice),
            "intestazione": intestazione,
            "esempio": _primo_esempio(foglio, indice, riga_dati or ((riga_intestazioni or 0) + 1)),
            "dichiarata": str(dichiarata),
            "trovata": True,
        })
    colonne.sort(key=lambda voce: (voce["colonna"] is None, voce["colonna"] or 0))

    ordine = _colonna_d_ordine(adattatore, mappatura, intestazioni)
    return {
        "profileId": str(profilo.get("profile_id") or ""),
        "fileName": str(profilo.get("file_name") or ""),
        "role": ruolo,
        "adapterId": str(decisione.get("adapter_id") or adattatore.get("id") or ""),
        "sheet": nome_foglio,
        "headerRow": riga_intestazioni,
        "dataStartRow": riga_dati,
        "orderColumn": ordine,
        "columns": colonne,
        # Where this supplier's promotions come from, if anyone declared it.
        # `None` means nobody reads them — the common case — and that's
        # itself information: the difference between "this supplier has no
        # promotions" and "it has them and we're not reading them".
        "commercialConditions": _condizioni_dichiarate(adattatore, mappatura, intestazioni),
        "origin": _origine_della_mappatura(decisione, adattatore),
        "recognised": bool(colonne),
    }


def _condizioni_dichiarate(
    adattatore: dict[str, Any], mappatura: dict[str, Any], intestazioni: list[Any]
) -> dict[str, Any] | None:
    """The commercial-conditions declaration, resolved against the document."""

    from registro import indice_della_colonna  # noqa: PLC0415

    dichiarazione = mappatura.get("commercial_conditions")
    if not isinstance(dichiarazione, dict) or not dichiarazione:
        dichiarazione = adattatore.get("commercial_conditions")
    if not isinstance(dichiarazione, dict) or not dichiarazione:
        return None
    campi = dichiarazione.get("fields")
    campi = campi if isinstance(campi, dict) else {}
    mappa = adattatore.get("column_map")
    mappa = mappa if isinstance(mappa, dict) else {}
    ruoli: dict[str, Any] = {}
    for ruolo, campo in campi.items():
        nome = str(campo or "")
        dichiarata = mappa.get(nome, (mappatura.get("columns") or {}).get(nome))
        indice = indice_della_colonna(intestazioni, dichiarata) if dichiarata not in (None, "") else None
        ruoli[str(ruolo)] = {
            "campo": nome,
            "colonna": indice,
            "lettera": get_column_letter(indice) if indice else "",
        }
    return {
        "layout": str(dichiarazione.get("layout") or ""),
        "sheet": str(dichiarazione.get("sheet") or ""),
        "fields": ruoli,
    }


def _primo_numero(*candidati: Any) -> int | None:
    for valore in candidati:
        if isinstance(valore, bool) or not isinstance(valore, int):
            continue
        if valore >= 1:
            return valore
    return None


def _colonna_d_ordine(
    adattatore: dict[str, Any], mappatura: dict[str, Any], intestazioni: list[Any]
) -> dict[str, Any] | None:
    """The column the writer fills with order quantities, if there is one.

    Reported alongside the other columns rather than inside the list: it
    isn't a column that's read, it's the one column the program writes to,
    on the price list's copy.
    """

    dichiarata = mappatura.get("order_column") or (adattatore.get("order_write") or {}).get("order_column")
    if not dichiarata:
        return None
    from registro import indice_della_colonna  # noqa: PLC0415

    indice = indice_della_colonna(intestazioni, dichiarata)
    if indice is None:
        return {"lettera": str(dichiarata), "colonna": None, "intestazione": "", "trovata": False}
    intestazione = ""
    if indice <= len(intestazioni) and intestazioni[indice - 1] is not None:
        intestazione = str(intestazioni[indice - 1]).strip()
    return {
        "lettera": get_column_letter(indice),
        "colonna": indice,
        "intestazione": intestazione,
        "trovata": True,
    }


def _origine_della_mappatura(decisione: dict[str, Any], adattatore: dict[str, Any]) -> str:
    """Where this assignment comes from.

    Only two values, because only two answers change anything for the
    reader: "confermata" (the user set it in the page, valid for this
    document only) or "registro" (it lives in `references/adapters.json`,
    whether shipped or locally learned). Shipped vs. learned is a real
    distinction but an inert one here — it changes neither what's shown nor
    what can be done.
    """

    conferma = decisione.get("user_confirmation")
    if isinstance(conferma, dict) and str(conferma.get("status") or "") == "CONFIRMED":
        return "confermata"
    if (
        adattatore.get("column_map")
        or adattatore.get("field_mapping")
        or adattatore.get("header_aliases")
        or (isinstance(decisione.get("field_mapping"), dict) and decisione["field_mapping"])
    ):
        return "registro"
    return "sconosciuta"
