#!/usr/bin/env python3
"""Delivery: a dated folder per order compilation, and how it's downloaded.

Writing every compilation into a fixed-name `app/data/current/outputs`
folder let a later compilation silently overwrite the price lists a
previous one had prepared, and recompiling before sending the order could
lose files with no warning. This module provides everything needed to
prevent that: the
folder-naming scheme, its atomic creation, the audit record that describes
it, the listing of compilations built from the real folders on disk, and the
two path defenses that keep a hand-built path from escaping the orders
folder.

Self-contained: it does not import `server` and knows nothing about HTTP
routes. The only route-shaped strings are the `url` and `zipUrl` fields of a
listing entry (`voce`), which the server forwards to the page unchanged
— they're entry data, not protocol knowledge.

Standard library only.
"""

from __future__ import annotations

import io
import json
import os
import re
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote


SCHEMA_AUDIT = 1
NOME_AUDIT = "compilazione.json"
NOME_PIANO = "final_order_plan.json"
MESI = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre")

# Prefix for delivery routes. Lives here rather than in `server.py` because
# the listing entry (`voce`) already carries built URLs: the page never
# hand-assembles a path.
PREFISSO_URL = "/ordini"

# Highest suffix tried when two compilations land in the same minute.
MAX_SUFFISSO = 99

# Characters Windows rejects in a file name. The colon is in this set, which
# is why the time is written as `1435`, not `14:35`.
VIETATI_WINDOWS = '<>:"/\\|?*'

# `YYYY-MM-DD_HHMM`, with an optional `_2`, `_3`, ... suffix.
NOME_CARTELLA_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(?:_(\d+))?$")

# The "products with no supplier" list (item 5). Its file-name prefix and
# `tipo` value live here rather than in `app/da_reperire.py` because this
# module is what needs to recognize the file by name alone when a folder is
# re-read without an audit; `da_reperire` imports these constants rather than
# the other way around, so there's one source of truth instead of two that
# could drift apart silently.
PREFISSO_DA_REPERIRE = "Prodotti da reperire "
TIPO_DA_REPERIRE = "da_reperire"

# The types that count as deliverable: what's counted to decide whether the
# "Download" button appears, and what goes into the zip. The to-be-sourced
# list is deliverable output just like a price list — a compilation made up
# only of to-be-sourced products (nothing could be ordered from any supplier)
# would otherwise have no download link at all.
TIPI_DA_CONSEGNARE = (
    "listino",
    TIPO_DA_REPERIRE,
)


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

def nome_cartella(momento: datetime) -> str:
    """The folder name for a compilation: `2026-08-12_1435`."""
    return momento.strftime("%Y-%m-%d_%H%M")


def data_leggibile(momento: datetime) -> str:
    """`12 agosto 2026`.

    Month names come from `MESI`, not `strftime("%B")`, since that depends on
    the system locale and would give `August` on this machine.
    """
    return f"{momento.day} {MESI[momento.month - 1]} {momento.year}"


def _ripulisci_nome_file(valore: str) -> str:
    """Make `valore` usable as a file-name fragment on Windows.

    Trailing dots and spaces become `_`: Windows silently strips them, so two
    suppliers whose names differ only there would collide into the same file
    without anyone noticing.
    """
    ripulito = "".join(
        "_" if carattere in VIETATI_WINDOWS or ord(carattere) < 32 or ord(carattere) == 127 else carattere
        for carattere in str(valore)
    )
    coda = len(ripulito) - len(ripulito.rstrip(". "))
    if coda:
        ripulito = ripulito[:-coda] + "_" * coda
    return ripulito


def nome_listino(fornitore: str, momento: datetime, estensione: str = ".xlsx") -> str:
    """`Ordine LARICE — 12 agosto 2026.xlsx`, with a spaced em dash (U+2014).

    The extension is decided by the source document, not this function: the
    file sent back to a supplier that ships `.xls` stays `.xls` — renaming it
    to `.xlsx` would deliver a document claiming to be something it isn't.

    The em dash survives: `zipfile` sets the UTF-8 flag (0x800) and Windows
    Explorer reads the name back character for character. What doesn't
    survive is the HTTP header, handled separately by `intestazione_allegato`.
    """
    coda = _ripulisci_nome_file(str(estensione or ".xlsx"))
    if not coda.startswith("."):
        coda = "." + coda
    return f"Ordine {_ripulisci_nome_file(fornitore).upper()} — {data_leggibile(momento)}{coda}"


def _pezzi_nome_cartella(nome: str) -> tuple[datetime, int | None] | None:
    """The timestamp and suffix parsed from the folder name, or `None`."""
    corrispondenza = NOME_CARTELLA_RE.match(str(nome or ""))
    if corrispondenza is None:
        return None
    anno, mese, giorno, ora, minuto, suffisso = corrispondenza.groups()
    try:
        momento = datetime(int(anno), int(mese), int(giorno), int(ora), int(minuto))
    except ValueError:
        # `2026-02-30_1435` has the right shape but isn't a real date.
        return None
    return momento, (int(suffisso) if suffisso is not None else None)


def etichetta(nome_cartella: str) -> str:
    """`12 agosto 2026, 14:35`, or `... (2)` when a suffix is present.

    A name that doesn't parse as a date is returned as-is: a folder someone
    created by hand inside `ordini` must not crash the listing.
    """
    pezzi = _pezzi_nome_cartella(nome_cartella)
    if pezzi is None:
        return str(nome_cartella)
    momento, suffisso = pezzi
    testo = f"{data_leggibile(momento)}, {momento.hour:02d}:{momento.minute:02d}"
    return f"{testo} ({suffisso})" if suffisso is not None else testo


def nome_zip(nome_cartella: str) -> str:
    """`Listini pronti per invio 2026-08-12 14-35.zip`.

    The time's colon becomes a dash because this name ends up as a file the
    user saves to disk.
    """
    pezzi = _pezzi_nome_cartella(nome_cartella)
    if pezzi is None:
        return f"Listini pronti per invio {_ripulisci_nome_file(nome_cartella)}.zip"
    momento, suffisso = pezzi
    testo = momento.strftime("%Y-%m-%d %H-%M")
    if suffisso is not None:
        testo = f"{testo} ({suffisso})"
    return f"Listini pronti per invio {testo}.zip"


# --------------------------------------------------------------------------
# The folder
# --------------------------------------------------------------------------

def crea_cartella(radice: Path, momento: datetime) -> Path:
    """Create the compilation's folder and return it.

    `mkdir()` without `exist_ok`, catching `FileExistsError`, is the only
    atomic way to say "I created this one". An `if not exists(): mkdir()`
    check is a race: two compilations in the same minute would land in the
    same folder and overwrite each other, exactly the failure this module
    exists to prevent.
    """
    radice = Path(radice)
    radice.mkdir(parents=True, exist_ok=True)
    base = nome_cartella(momento)
    for tentativo in range(1, MAX_SUFFISSO + 1):
        candidata = radice / (base if tentativo == 1 else f"{base}_{tentativo}")
        try:
            candidata.mkdir()
        except FileExistsError:
            continue
        return candidata
    raise ValueError("Troppe compilazioni nello stesso minuto")


def ultima(radice: Path) -> Path | None:
    """The most recent compilation folder that has an order plan, or `None`.

    "Most recent folder" alone isn't enough: the app treats `ordini` as the
    user's archive, and it invites the user to create their own subfolders
    there (or Windows leaves a stray "New folder" from a misclick). Such a
    folder would otherwise count as the latest compilation and someone who
    just compiled would be told there are none. A folder with no order plan
    isn't a compilation.
    """
    radice = Path(radice)
    for voce_elenco in elenco(radice):
        candidata = radice / voce_elenco["cartella"]
        if (candidata / NOME_PIANO).is_file():
            return candidata
    return None


# --------------------------------------------------------------------------
# The audit record
# --------------------------------------------------------------------------

def scrivi_audit(cartella: Path, audit: dict) -> Path:
    """Write `compilazione.json` as bytes, atomically.

    As bytes because `write_text` would turn every `\\n` into `\\r\\n` on
    Windows, and this file is also read back by command-line tools. Temp file
    next to it plus `os.replace`: an audit truncated mid-write would lie
    about the compilation.
    """
    percorso = Path(cartella) / NOME_AUDIT
    testo = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    temporaneo = percorso.with_name(percorso.name + ".tmp")
    temporaneo.write_bytes(testo.encode("utf-8"))
    os.replace(temporaneo, percorso)
    return percorso


def leggi_audit(cartella: Path) -> dict | None:
    """The audit dict, or `None`. Never raises.

    A broken audit is a problem for that one compilation, not for the
    listing of every other one.
    """
    try:
        dati = json.loads((Path(cartella) / NOME_AUDIT).read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return dati if isinstance(dati, dict) else None


def tipo_file(nome: str) -> str:
    """Infer the `tipo` from the file name alone, for folders with no readable audit.

    A name that doesn't match any known pattern returns `"altro"`, shown in
    the listing as "other document". This fallback keeps order folders
    written by earlier versions of the app usable: a file the current version
    doesn't produce must neither hide the folder from the listing nor crash
    the entry.
    """
    if nome == NOME_PIANO:
        return "piano"
    # Checked before the generic extension fallback below: the to-be-sourced
    # list is itself a `.xlsx` file, and without this branch it would be
    # mistaken for a price list and end up in the zip sent to a supplier — a
    # sheet listing exactly what that supplier did *not* sell us.
    ripulito = str(nome or "").casefold()
    if ripulito.startswith(PREFISSO_DA_REPERIRE.casefold()) and ripulito.endswith(".xlsx"):
        return TIPO_DA_REPERIRE
    # `.xls` is here because the file returned to a supplier that ships
    # Excel 97-2003 is a `.xls`; without it, that compiled copy would land in
    # "other" and the page wouldn't count it as a price list to send.
    if nome.casefold().endswith((".xlsx", ".xls")):
        return "listino"
    return "altro"


def e_documento(nome: str) -> bool:
    """Whether a name is a compilation document, as opposed to housekeeping noise.

    Four families are excluded, each for its own reason:

    - names starting with `.`, since the writer works in
      `.ordine-temporaneo-*`;
    - `~$...`, the lock file Excel drops next to a document that's open on
      Windows. Without this filter, opening a copy just to check it would be
      enough to get it included in the zip sent to a supplier, and in a
      folder with no audit it would also be counted as a price list;
    - `.tmp` files, atomic writes caught mid-way;
    - the audit file itself, which describes the folder and isn't
      deliverable content.
    """
    nome = str(nome or "")
    if not nome or nome.startswith(".") or nome.startswith("~$"):
        return False
    if nome == NOME_AUDIT or nome.casefold().endswith(".tmp"):
        return False
    return True


# --------------------------------------------------------------------------
# The listing
# --------------------------------------------------------------------------

def _numero(valore: Any) -> float | None:
    if isinstance(valore, bool) or not isinstance(valore, (int, float)):
        return None
    return float(valore)


def _url_file(nome_della_cartella: str, nome: str) -> str:
    return f"{PREFISSO_URL}/{quote(nome_della_cartella, safe='')}/{quote(nome, safe='')}"


def _tipi_dall_audit(audit: dict) -> dict[str, str]:
    """Document name -> `tipo`, read from the audit."""
    tipi: dict[str, str] = {}
    for riga in audit.get("file") or []:
        if not isinstance(riga, dict):
            continue
        nome = riga.get("nome")
        if not isinstance(nome, str) or not nome:
            continue
        tipo = riga.get("tipo")
        tipi[nome] = tipo if isinstance(tipo, str) and tipo else tipo_file(nome)
    return tipi


def _nomi_sul_disco(cartella: Path) -> list[str]:
    """The documents currently in the folder, sorted by name."""
    try:
        with os.scandir(cartella) as scansione:
            return sorted(
                trovato.name for trovato in scansione
                if trovato.is_file() and e_documento(trovato.name)
            )
    except OSError:
        return []


def voce(cartella: Path, *, etichetta_fornitore: Callable[[str], str] | None = None) -> dict:
    """Build the listing entry for a delivery folder, in camelCase for `app.js`.

    A folder that exists always shows up: if the audit is missing or
    unreadable the entry still exists, with `completa: false`, because
    disappearing from the listing would be the worst way to report a broken
    audit.

    The document list is read from disk, not from the audit — the audit only
    says what type each one is. The audit describes the folder at the moment
    it was written; afterwards the user lives in that folder, attaching a
    price list to an email, moving it, renaming it. A listing built purely
    from the audit would keep counting a document that's no longer there and
    offer a link that returns 404, and the zip endpoint, which requires
    every declared name to exist, would then refuse to deliver even the
    price lists that are still present. Anything the audit names that isn't
    on disk goes into `mancanti` instead of disappearing silently.
    """
    cartella = Path(cartella)
    nome_della_cartella = cartella.name
    audit = leggi_audit(cartella)
    nominatore = etichetta_fornitore if callable(etichetta_fornitore) else (lambda identificativo: identificativo.upper())
    sul_disco = _nomi_sul_disco(cartella)

    if audit is None:
        file_voci = [
            {"nome": nome, "tipo": tipo_file(nome), "url": _url_file(nome_della_cartella, nome)}
            for nome in sul_disco
        ]
        mancanti: list[str] = []
        completa = False
        creato_il: Any = None
        stato = "SCONOSCIUTO"
        fornitori: list[dict] = []
        totale_netto: float | None = None
        righe: Any = None
        avvisi: list[str] = []
    else:
        tipi = _tipi_dall_audit(audit)
        file_voci = [
            {
                # A document that appeared after the audit was written is
                # `altro`: its origin is unknown, so it doesn't count as a
                # price list to deliver to a supplier.
                "nome": nome,
                "tipo": tipi.get(nome, "altro"),
                "url": _url_file(nome_della_cartella, nome),
            }
            for nome in sul_disco
        ]
        mancanti = [nome for nome in sorted(tipi) if nome not in set(sul_disco)]
        completa = True
        creato_il = audit.get("creato_il") if isinstance(audit.get("creato_il"), str) else None
        stato = audit.get("stato") if isinstance(audit.get("stato"), str) and audit.get("stato") else "SCONOSCIUTO"
        totali = audit.get("totali_netti")
        totali = totali if isinstance(totali, dict) else {}
        fornitori = [
            {
                "id": identificativo,
                "nome": nominatore(identificativo),
                "totaleNetto": _numero(totali.get(identificativo)),
            }
            for identificativo in (audit.get("fornitori") or [])
            if isinstance(identificativo, str) and identificativo
        ]
        totale_netto = _numero(audit.get("totale_netto"))
        righe = audit.get("righe") if isinstance(audit.get("righe"), int) and not isinstance(audit.get("righe"), bool) else None
        # Warnings the audit recorded: a discarded copy, a name that stayed
        # ugly, an order that didn't make it into the ones to check.
        # Surfaced in the listing entry so they persist across reloads
        # instead of living only as long as the page that first saw them.
        avvisi = [str(voce).strip() for voce in (audit.get("avvisi") or []) if str(voce).strip()]

    # Price lists are counted from what's actually there, not what the audit
    # declared: it's the number the page shows, and it must be true right now.
    #
    # These are two different questions with two different counts. The page
    # says "2 price lists", and the to-be-sourced list isn't a price list —
    # counting it there would be false. The "Download" button instead asks
    # "is there anything to hand over?", and there the to-be-sourced list
    # does count: a compilation where nothing could be ordered from any
    # supplier produces only that list, and counting price lists alone would
    # leave it with no download link at all.
    listini = sum(1 for riga in file_voci if riga["tipo"] == "listino")
    da_consegnare = sum(1 for riga in file_voci if riga["tipo"] in TIPI_DA_CONSEGNARE)

    return {
        "cartella": nome_della_cartella,
        "etichetta": etichetta(nome_della_cartella),
        "creatoIl": creato_il,
        "stato": stato,
        "fornitori": fornitori,
        "totaleNetto": totale_netto,
        "righe": righe,
        "listini": listini,
        "file": file_voci,
        # Names the audit lists that are no longer on disk. Not a bug — the
        # user moved the document — but it needs to be said, so someone
        # looking for a two-week-old price list knows they moved it
        # themselves rather than that the app lost it.
        "mancanti": mancanti,
        "avvisi": avvisi,
        # `null`, not a URL that would 404, when there's nothing to deliver:
        # the download button must not appear.
        "zipUrl": (f"{PREFISSO_URL}/{quote(nome_della_cartella, safe='')}/zip" if da_consegnare else None),
        "zipNome": nome_zip(nome_della_cartella),
        "completa": completa,
    }


def _chiave_ordine(cartella: Path) -> float:
    """When the compilation happened: from the audit if readable, else from disk."""
    audit = leggi_audit(cartella)
    if isinstance(audit, dict) and isinstance(audit.get("creato_il"), str):
        try:
            return datetime.fromisoformat(audit["creato_il"]).timestamp()
        except (ValueError, OSError, OverflowError):
            pass
    try:
        return cartella.stat().st_mtime
    except OSError:
        return 0.0


def elenco(radice: Path, *, etichetta_fornitore: Callable[[str], str] | None = None) -> list[dict]:
    """List every compilation, most recent first.

    Scans the real folders: a separate index would be one more thing that
    can drift out of sync with the disk.

    Only lists what `cartella_sicura` would accept — exactly what the routes
    know how to serve. An NTFS junction inside `ordini` (an `mklink /J` to a
    network folder, or a badly restored backup) resolves outside the orders
    folder and is rejected: without this filter it would show up as a
    compilation, listing file names from outside and offering links that
    return 404.
    """
    radice = Path(radice)
    try:
        with os.scandir(radice) as scansione:
            nomi = [
                trovata.name for trovata in scansione
                if trovata.is_dir() and not trovata.name.startswith(".")
            ]
    except OSError:
        return []
    cartelle = [c for c in (cartella_sicura(radice, nome) for nome in nomi) if c is not None]
    ordinate = sorted(cartelle, key=lambda c: (_chiave_ordine(c), c.name), reverse=True)
    return [voce(c, etichetta_fornitore=etichetta_fornitore) for c in ordinate]


# --------------------------------------------------------------------------
# Path defenses
# --------------------------------------------------------------------------

def _dentro(figlio: Path, genitore: Path) -> bool:
    """Is `figlio` inside `genitore`? Assumes both paths are already resolved."""
    try:
        figlio.relative_to(genitore)
        return True
    except ValueError:
        return False


def _percorso_sicuro(genitore: Path, nome: str, *, vuole_cartella: bool) -> Path | None:
    """The allowlist check shared by `cartella_sicura` and `file_sicuro`.

    Doesn't filter bad characters — that's a denylist, and denylists always
    miss something. Instead it asks the disk what actually exists and
    accepts only a name that appears identically in that directory scan:
    NTFS alternate data streams (`compilazione.json:$DATA`) and reserved
    names never show up in a scan, so they're rejected without needing to be
    special-cased. Containment is still verified afterward, because a
    symlink shows up in the scan but can resolve anywhere.

    The caller passes a name already run through `unquote(...)`:
    `..%2f..%2fsecrets.json` has already become `../../secrets.json` by this
    point, so this check works on the decoded name.
    """
    if not isinstance(nome, str) or not nome:
        return None
    if nome in (".", ".."):
        return None
    if "/" in nome or "\\" in nome or "\x00" in nome:
        return None

    genitore = Path(genitore)
    try:
        with os.scandir(genitore) as scansione:
            esiste = any(
                trovata.name == nome and (trovata.is_dir() if vuole_cartella else trovata.is_file())
                for trovata in scansione
            )
    except OSError:
        return None
    if not esiste:
        return None

    percorso = genitore / nome
    try:
        risolto = percorso.resolve()
        radice_risolta = genitore.resolve()
    except OSError:
        return None
    if risolto == radice_risolta or not _dentro(risolto, radice_risolta):
        return None
    return percorso


def cartella_sicura(radice: Path, nome: str) -> Path | None:
    """That compilation's folder, or `None` if the name isn't legitimate."""
    return _percorso_sicuro(radice, nome, vuole_cartella=True)


def file_sicuro(cartella: Path, nome: str) -> Path | None:
    """That compilation's file, or `None` if the name isn't legitimate."""
    return _percorso_sicuro(cartella, nome, vuole_cartella=False)


# --------------------------------------------------------------------------
# Actual delivery
# --------------------------------------------------------------------------

def zip_in_memoria(cartella: Path, nomi: Sequence[str]) -> bytes:
    """The bytes of a zip built on the fly, in memory.

    Never written to disk: that would be one more artifact to keep in sync
    with the folder it's meant to describe. Every name is checked through
    `file_sicuro` again even when the caller just read it from the audit —
    the audit is a text file, and this is the last gate before touching disk.
    """
    nomi = list(nomi or [])
    if not nomi:
        raise ValueError("Non ci sono listini da mettere nello zip")

    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", zipfile.ZIP_DEFLATED) as archivio:
        for nome in nomi:
            percorso = file_sicuro(cartella, nome)
            if percorso is None:
                # The requested name isn't echoed back: it would end up in a
                # message to the user and reveal to an attacker what they'd
                # just tried.
                raise ValueError("Uno dei documenti chiesti non è in questa compilazione")
            # `arcname=nome`, not the full path: no folders inside the zip.
            archivio.write(percorso, arcname=nome)
    return memoria.getvalue()


def _ripiego_ascii(nome: str) -> str:
    """The name reduced to ASCII, for the header's `filename=` part."""
    scomposto = unicodedata.normalize("NFKD", str(nome))
    pezzi = []
    for carattere in scomposto:
        if unicodedata.combining(carattere):
            continue  # accent mark already split off by NFKD
        if carattere in '"\\':
            continue  # would close the header's quotes early
        codice = ord(carattere)
        if codice < 32 or codice == 127:
            continue  # a `\r\n` here would be a header-injection attack
        pezzi.append(carattere if codice < 128 else "-")
    ripiego = "".join(pezzi).strip()
    return ripiego or "allegato"


def intestazione_allegato(nome: str) -> str:
    """The `Content-Disposition` header value for a download, per RFC 5987.

    `attachment; filename="Ordine LARICE — 12 agosto 2026.xlsx"` doesn't
    encode in latin-1, and `BaseHTTPRequestHandler` writes headers in
    latin-1: the response would fail with `UnicodeEncodeError` while writing
    the header, after the body had already been promised. Hence a quoted
    ASCII fallback for older readers, plus `filename*=UTF-8''` for
    everything else, which current browsers prefer.
    """
    return f"attachment; filename=\"{_ripiego_ascii(nome)}\"; filename*=UTF-8''{quote(str(nome), safe='')}"
