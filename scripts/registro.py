#!/usr/bin/env python3
"""Supplier conventions live in the adapter registry, not in code.

The program is meant to run unattended: when a supplier changes a
convention, `references/adapters.json` gets updated and nothing else. A rule
hard-coded inside a function only covers that week's price list and breaks
silently at the next variation.

This module is the only one that reads — and, from this stage on, writes —
that registry on behalf of everything else. It does two things:

1. applies the row codes an adapter declares;
2. recognizes a document's schema by comparing its observed fingerprint
   against the declared ones, and learns a new adapter without discarding
   the previous one.

The declared shape of row codes is:

    "row_markers": {
      "field": "discount_raw",
      "codes": {
        "TP": {"means": "...", "orderable": true},
        "SM": {"means": "...", "orderable": false, "row_type": "OMAGGIO"}
      },
      "reward_rows": {"means": "...", "orderable": false, "row_type": "OMAGGIO"}
    }

`field` is the field of the normalized record where the code appears, not a
sheet column: that way the same rule works for both a dedicated reader and
one driven by a field mapping.

`reward_rows` is for suppliers who don't mark the free-goods row at all: it's
recognized from the description text alone, using the same judgment the
promotions engine already makes (`promotions.looks_like_reward`), the one
that decides where the bridge closes a block. It's worth having because it
covers exactly the case `applica_codici_di_riga` guards: a supplier's free
"IN OMAGGIO ..." rows can carry a real price and no code, while repeating the
barcode of a full-price item already on the price list. Without this
declaration the comparison would pick the free-goods price.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# The shipped registry: tracked in git, and reset to the GitHub state on
# every startup (`reset --hard`) on the store PC.
REGISTRO = Path(__file__).resolve().parents[1] / "references" / "adapters.json"

# The learned registry: what the program writes on its own when it
# recognizes a new supplier or someone confirms a changed schema.
#
# Kept as a separate file, outside git and outside `app/data/`'s reset:
# anything the program learns has to survive the startup `reset --hard` that
# realigns the shipped registry with GitHub, the same way the program's other
# state does — which is also why the backup that saves that state saves this
# file too.
NOME_REGISTRO_IMPARATO = "adattatori_imparati.json"
REGISTRO_IMPARATO = Path(__file__).resolve().parents[1] / "app" / "data" / NOME_REGISTRO_IMPARATO


# Suffix for an adapter learned ON TOP OF one the program ships.
#
# A learned adapter gets its own id, carrying the id it derives from:
# `betulla_v1__locale`. The two coexist; whichever one actually reads the
# document (`riconosci`) wins, and the shipped entry stays in place for the
# day the learned one is no longer needed.
#
# This matters because a learned entry sharing the shipped entry's exact id
# would win ties and hide it — along with everything the guided mapping
# doesn't ask for: commercial terms, header aliases, the expected header on
# the order column, column positions. Recovering from that would mean editing
# `app/data/adattatori_imparati.json` by hand to delete the entry.
SUFFISSO_LOCALE = "__locale"


def id_locale(identificativo: str) -> str:
    """The id under which something learned on top of a shipped adapter is written."""

    return f"{identificativo}{SUFFISSO_LOCALE}"


def adattatore_base(identificativo: Any) -> str:
    """The shipped id a learned adapter derives from, or the id itself.

    Used by every part of the program that decides by identifier: the
    four dedicated readers (`lettore_dedicato`), the columns that can be
    hand-corrected (`colonne_corrette`), and the Larice displays. Without
    it, a `larice_v1__locale` would lose Larice's reader and, with it, its
    displays and free-goods thresholds.
    """

    testo = str(identificativo or "")
    if testo.endswith(SUFFISSO_LOCALE):
        return testo[: -len(SUFFISSO_LOCALE)]
    return testo


def identificativo_da_scrivere(dichiarato: str, percorso: Path | None = None) -> str:
    """The id under which a learned entry actually enters the registry.

    A supplier the program ships gets the `__locale` suffix, so confirming
    the guided mapping doesn't overwrite the shipped entry and everything it
    carries that the guided mapping doesn't ask for — commercial terms,
    aliases, the expected header on the order column, column positions.

    A supplier learned from scratch, absent from the shipped registry, keeps
    updating its own entry as before: there's nothing shipped underneath to
    protect.

    Lives here rather than in `impara_adattatore` because the guided mapping
    isn't the only path that writes to the registry: moving the order column
    from the page writes here too, and both must derive the id the same way.
    """

    identificativo = str(dichiarato or "").strip()
    if identificativo in identificativi_spediti(percorso):
        return id_locale(identificativo)
    return identificativo


def voce_in_uso(identificativo: Any, voci: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The entry that actually applies to a decision recorded under that id.

    A decision recorded last week can say `betulla_v1`; since then someone
    may have moved the order column from the page, which writes
    `betulla_v1__locale`. Looking up the bare id would still find the
    shipped entry, with the old column, and write the order where nobody
    wants it anymore.

    Same rule as `_leggi_registro`'s merge of the two files, and as
    `riconosci`'s tie-break: between two versions of the same thing, the more
    recent one wins — the one someone gave with the document in front of
    them.

    Returns `{}` when there's nothing: a registry that doesn't know that
    decision isn't a failure to raise here.
    """

    cercato = str(identificativo or "").strip()
    if not cercato:
        return {}
    per_id = {str(voce.get("id") or ""): voce for voce in voci if isinstance(voce, dict)}
    locale = per_id.get(id_locale(adattatore_base(cercato)))
    if locale is not None:
        return locale
    return per_id.get(cercato) or {}


def impronta_della_voce(voce: dict[str, Any]) -> str:
    """Fingerprint of a registry entry, as written in the file.

    Answers one question: is the shipped entry still the one this learned
    entry was learned from? Computed over the whole content, notes included:
    telling a "substantive" change from a "cosmetic" one would mean
    maintaining a key list nobody would keep updated.
    """

    canonico = json.dumps(voce, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()[:16]


def sopra_spedito_di(spedita: dict[str, Any]) -> dict[str, Any]:
    """The stamp a learned entry carries to say which shipped entry it was learned from."""

    return {"id": str(spedita.get("id") or ""), "impronta": impronta_della_voce(spedita)}


def _motivo_del_superamento(imparata: dict[str, Any],
                            spedite_per_id: dict[str, dict[str, Any]]) -> str | None:
    """Why a learned entry no longer applies, or `None` if it still does.

    The rule: an entry learned on top of a shipped one (same id, or with
    `__locale`) applies as long as the shipped entry is the one it was
    learned from. If the shipped entry changes — a correction reaches the
    store through an update — the shipped entry wins and the learned one is
    set aside. Confirming the guided mapping again puts it back in play,
    stamped against the current shipped entry.

    An entry learned from scratch, with no shipped counterpart, is
    unaffected by this rule: there's nothing more recent to compare against.

    An entry sitting on top of a shipped one but carrying no stamp is
    treated as superseded: nothing says which version it was learned from,
    so it can't be trusted to still match.
    """

    base = adattatore_base(imparata.get("id"))
    spedita = spedite_per_id.get(base)
    if spedita is None:
        return None
    timbro = imparata.get("sopra_spedito")
    if not isinstance(timbro, dict) or not str(timbro.get("impronta") or ""):
        return f"la voce non dice su quale versione di «{base}» era stata imparata"
    if str(timbro.get("impronta")) == impronta_della_voce(spedita):
        return None
    return f"«{base}» è cambiato dopo che questa voce era stata imparata"


def _spedite_per_id(spedite: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(voce.get("id") or ""): voce for voce in spedite if str(voce.get("id") or "")}


def _scheda_della_superata(voce: dict[str, Any], motivo: str) -> dict[str, Any]:
    return {
        "id": str(voce.get("id") or ""),
        "base": adattatore_base(voce.get("id")),
        "supplier_id": voce.get("supplier_id"),
        "display_name": voce.get("display_name"),
        "motivo": motivo,
    }


def adattatori_superati(percorso: Path | None = None) -> list[dict[str, Any]]:
    """The learned entries the shipped registry has superseded, without touching anything.

    Returns `[]` when either file can't be read too: `motivo_registro_illeggibile`
    already reports why, and this function answers a different question.
    """

    spedito_percorso = Path(percorso or REGISTRO)
    spedite, errore = _voci_di_un_documento(spedito_percorso, quale="Registro degli adattatori")
    if errore is not None:
        return []
    imparato_percorso = percorso_imparato(spedito_percorso)
    if not imparato_percorso.is_file():
        return []
    imparate, errore = _voci_di_un_documento(imparato_percorso, quale="Registro degli adattatori imparati qui")
    if errore is not None:
        return []
    per_id = _spedite_per_id(spedite)
    esito: list[dict[str, Any]] = []
    for voce in imparate:
        motivo = _motivo_del_superamento(voce, per_id)
        if motivo is not None:
            esito.append(_scheda_della_superata(voce, motivo))
    return esito


def percorso_imparato(registro_spedito: Path | None = None) -> Path:
    """Where the learned registry lives, given where the shipped one is.

    In the real install, the shipped registry lives in `references/` and the
    learned one in `app/data/`, alongside the program's other state. A
    registry living elsewhere — a working copy under test, a temp-folder
    fixture — keeps its learned file next to itself instead: a test that
    wrote into the real install would carry learned adapters across runs.
    """

    base = Path(registro_spedito or REGISTRO)
    if base.parent.name == "references":
        return base.parent.parent / "app" / "data" / NOME_REGISTRO_IMPARATO
    return base.with_name(NOME_REGISTRO_IMPARATO)


def identificativi_spediti(percorso: Path | None = None) -> set[str]:
    """The ids in the shipped registry, excluding the learned one.

    Lets `impara_adattatore` check whether it's about to write over
    something the program ships: those get learned alongside it
    (`id_locale`), not on top of it.
    """

    voci, errore = _voci_di_un_documento(Path(percorso or REGISTRO), quale="Registro degli adattatori")
    if errore is not None:
        return set()
    return {str(voce.get("id") or "") for voce in voci if str(voce.get("id") or "")}


def adattatore(adapter_id: str, percorso: Path | None = None) -> dict[str, Any]:
    """The registry entry with that id, or {} if there isn't one.

    A missing or unreadable registry doesn't stop a price list from being
    read: the optional rules simply don't apply, and whoever truly depends on
    them (the Noce `.xls` reader) reports that on its own.
    """

    voci, _errore = _leggi_registro(percorso)
    for voce in voci:
        if voce.get("id") == adapter_id:
            return voce
    return {}


def mappatura_spedita(adapter_id: str, percorso: Path | None = None) -> dict[str, Any]:
    """The shipped entry's `field_mapping` for that id, or `{}` if there isn't one.

    Shipped, not effective — that distinction matters: the learned mapping is
    written by the program, while a mapping confirmed from the page is built
    from scratch and doesn't carry `exclude_rows`. A check that wants to
    notice a lost rule has to compare against the shipped mapping;
    otherwise the rule is missing from both sides of the comparison and the
    check never fires.

    Lives here rather than being read directly elsewhere, so every read of
    the registry goes through this module.

    A missing or unreadable registry answers `{}`, like `adattatore`: this
    mapping backs an extra check, and a file that won't open shouldn't turn
    off the viewer.
    """

    if not str(adapter_id or ""):
        return {}
    voci, errore = _voci_di_un_documento(
        Path(percorso or REGISTRO), quale="Registro degli adattatori",
    )
    if errore is not None:
        return {}
    for voce in voci:
        if str(voce.get("id") or "") == str(adapter_id):
            mappatura = voce.get("field_mapping")
            return mappatura if isinstance(mappatura, dict) else {}
    return {}


def codici_di_riga(fonte: dict[str, Any]) -> dict[str, Any]:
    """The `row_markers` declared by an adapter or a field mapping."""

    codici = (fonte or {}).get("row_markers")
    if not isinstance(codici, dict):
        return {}
    # A supplier can declare codes, reward rows, or both: requiring `codes`
    # would silently discard a declaration made of reward rows alone.
    if not isinstance(codici.get("codes"), dict) and not isinstance(codici.get("reward_rows"), dict):
        return {}
    return codici


def codici_ammessi(codici: dict[str, Any]) -> set[str]:
    """The codes this supplier actually uses; anything else should be flagged."""

    return {str(chiave).strip().upper() for chiave in (codici.get("codes") or {})}


def codice_della_riga(record: dict[str, Any], codici: dict[str, Any]) -> dict[str, Any] | None:
    """The declared code that marks this row, if there is one."""

    campo = str(codici.get("field") or "discount_raw")
    valore = record.get(campo)
    if isinstance(valore, bool) or isinstance(valore, (int, float)):
        return None
    testo = str(valore or "").strip().upper()
    if not testo:
        return None
    for chiave, dichiarato in (codici.get("codes") or {}).items():
        if str(chiave).strip().upper() == testo and isinstance(dichiarato, dict):
            return dichiarato
    return None


def _riga_premio_dal_testo(record: dict[str, Any]) -> bool:
    """Whether the description alone marks this row as free goods.

    The judgment isn't reimplemented here: it's `promotions.looks_like_reward`,
    the same one `promotion_bridge._blocchi` uses to decide a block has
    ended. Two definitions of the same thing can drift apart, and a row
    would then be a reward to the promotions engine and merchandise to the
    comparison.
    """

    try:
        from promotions import looks_like_reward  # noqa: PLC0415 - deliberate late import
    except Exception:  # noqa: BLE001 - without the promotions engine, only the row codes apply
        return False
    return looks_like_reward(record.get("description"))


def applica_codici_di_riga(records: list[dict[str, Any]], codici: dict[str, Any]) -> Counter[str]:
    """Demote the rows the registry declares not orderable.

    The row isn't dropped: it's information — the free-goods reward of a
    threshold promotion carries a real code, barcode and description — but
    it isn't purchasable merchandise, so it must not be able to enter an
    order or weigh on which supplier wins.

    A supplier can write a real price on a free-goods row and mark it with
    no code at all, while its barcode repeats a full-price item already on
    the list. That's what `reward_rows` is for: a row with no code declares
    itself through the promotions engine reading its description text.
    """

    conteggio: Counter[str] = Counter()
    if not codici:
        return conteggio
    premio = codici.get("reward_rows")
    if not isinstance(premio, dict):
        premio = None
    for record in records:
        dichiarato = codice_della_riga(record, codici)
        if (
            premio is not None
            # A code that already demotes the row is left as-is: it says more
            # than the text alone knows, and overwriting it would lose that.
            and (dichiarato is None or dichiarato.get("orderable") is not False)
            and _riga_premio_dal_testo(record)
        ):
            # "Not orderable" wins between the two answers, and the direction
            # isn't symmetric: mistaking a product for a reward is obvious
            # (it's missing from the comparison), while mistaking a reward
            # for a product puts a price on the list that doesn't exist, and
            # only the supplier finds out.
            dichiarato = premio
        if not dichiarato or dichiarato.get("orderable") is not False:
            continue
        tipo = str(dichiarato.get("row_type") or "NON_ORDINABILE")
        record["usable"] = False
        record["row_type"] = tipo
        record["not_orderable_reason"] = str(dichiarato.get("means") or "")
        conteggio[tipo] += 1
    return conteggio


# Schema memory: fingerprints, recognition, versioned writes.


# The three fields no other part of the program can verify for us: if a
# price turns into text, the comparison between suppliers silently goes
# empty, which is the worst-case change.
CAMPI_NUMERICI = ("unit_price_net", "unit_price_pre_discount", "pieces_per_carton")

# An adapter with a dedicated reader (BETULLA, Larice, the management
# software) declares no field mapping: requiring the same checks for it
# would demote it to `SCHEMA_VARIATO` every week, and every week would cost
# a needless trip through manual remapping.
NON_APPLICABILE = "l'adattatore non dichiara una mappatura: verifica non applicabile"


def normalizza(valore: Any) -> str:
    """Reduce a header to its substance, so it can be compared.

    The same supplier can write "Cod.Art.", "COD ART" and "Cod. Art." across
    different weeks with no warning: comparing headers verbatim would mean
    failing to recognize a price list over one extra period.
    """

    testo = unicodedata.normalize("NFKD", str(valore or ""))
    testo = "".join(carattere for carattere in testo if not unicodedata.combining(carattere))
    return re.sub(r"[^a-z0-9]+", "", testo.casefold())


def impronta_intestazioni(valori: Iterable[Any]) -> list[str]:
    """Normalized tokens of a row: no blanks, no duplicates, sorted.

    Column order isn't identity: a supplier who reorders a column still
    sends the same price list. Sorting also keeps the fingerprint stable
    across reads, which is what makes it comparable.
    """

    return sorted({normalizza(valore) for valore in valori} - {""})


def posizioni_delle_intestazioni(intestazioni: Iterable[Any]) -> dict[str, int]:
    """Where each header sits, in the shape `header_signature.columns` uses.

    This is what makes a signature verifiable: the set of names says the
    document belongs to that supplier, the positions say the reader will
    find the columns where it goes looking. Used by whoever writes a
    signature (`impara_adattatore`), so it writes the same shape
    `_verifica_posizioni` reads back; writing it two different ways would
    mean an adapter learned once and never recognized again.

    A repeated name keeps its last position, matching the verification: on
    ACERO, "COSTO IMPON." appears in both column 9 and column 15, and both
    sides must agree on the same answer.
    """

    posizioni: dict[str, int] = {}
    for posizione, valore in enumerate(intestazioni, start=1):
        token = normalizza(valore)
        if token:
            posizioni[token] = posizione
    return posizioni


def impronta(sheet: str | None, header_row: int | None,
             data_start_row: int | None, intestazioni: Iterable[Any]) -> dict[str, Any]:
    """The observed fingerprint of a schema, with its `hash` for auditing.

    The `hash` is for saying "something changed" in a message and for
    matching two identical reads; it is not the identity criterion, since one
    extra decorative column changes it without changing the schema. Identity
    is decided by whether `required` is a subset of what's observed.
    """

    corpo = {
        "sheet": sheet,
        "header_row": header_row,
        "data_start_row": data_start_row,
        "headers": impronta_intestazioni(intestazioni),
    }
    canonico = json.dumps(corpo, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {**corpo, "hash": hashlib.sha256(canonico.encode("utf-8")).hexdigest()}


def adattatori(percorso: Path | None = None) -> list[dict[str, Any]]:
    """All registry entries, `[]` if the registry is missing or unreadable."""

    voci, _errore = _leggi_registro(percorso)
    return voci


def adattatori_effettivi(percorso: Path | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """The effective registry's entries and the failure reason, in one read.

    For callers that need to behave differently for an empty vs. a broken
    registry, without opening the files twice. `adattatori()` for just the
    list, `motivo_registro_illeggibile()` for just the reason.
    """

    return _leggi_registro(percorso)


_NOMI_IN_MEMORIA: dict[str, tuple[tuple[Any, ...] | None, dict[str, str]]] = {}


def _firma_dei_due_registri(spedito: Path) -> tuple[Any, ...] | None:
    """Just enough to notice that one of the two files has changed.

    `None` means "unknown": a caller caching something against this
    signature must re-read rather than trust it.
    """

    firma: list[Any] = []
    for documento in (spedito, percorso_imparato(spedito)):
        try:
            stato = documento.stat()
        except OSError:
            if documento == spedito:
                return None
            # A missing learned file is the normal case, and must be
            # distinguished from a learned file that exists but is empty.
            firma.append(None)
            continue
        firma.append((stato.st_mtime_ns, stato.st_size))
    return tuple(firma)


def nomi_dichiarati_fra(adattatori_letti: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Same map, but over adapters already read by someone else.

    For a caller that already has the registry open — the guided mapping —
    and shouldn't re-read it from disk just to learn a supplier's name. The
    shortest-name rule lives here, in one place, instead of being
    duplicated at each call site.
    """

    nomi: dict[str, str] = {}
    for voce in adattatori_letti or []:
        if not isinstance(voce, dict):
            continue
        fornitore = str(voce.get("supplier_id") or "").strip().casefold()
        nome = str(voce.get("display_name") or "").strip()
        if not fornitore or not nome:
            continue
        precedente = nomi.get(fornitore)
        if precedente is None or len(nome) < len(precedente):
            nomi[fornitore] = nome
    return nomi


def nome_del_fornitore_fra(supplier_id: Any, adattatori_letti: Iterable[dict[str, Any]]) -> str:
    """`nome_del_fornitore`, without going back to disk: same fallback."""

    identificativo = str(supplier_id or "").strip()
    if not identificativo:
        return "FORNITORE"
    nome = nomi_dichiarati_fra(adattatori_letti).get(identificativo.casefold())
    if nome:
        return nome
    return re.sub(r"[-_]+", " ", identificativo).strip().upper() or "FORNITORE"


def nomi_dei_fornitori(percorso: Path | None = None) -> dict[str, str]:
    """The readable name of each supplier, per the registry.

    A single source of truth, drawn from the same place a supplier is born:
    its adapter's `display_name`.

    When several adapters declare the same `supplier_id` — Noce has both a
    CSV and an Excel adapter — the shortest name wins: the longer one
    describes the document ("Noce Excel 97-2003 price list"), not the
    supplier.

    Cached until the underlying file changes: this map gets requested once
    per product, hundreds of times per page.
    """

    documento = Path(percorso or REGISTRO)
    chiave = str(documento)
    # The signature covers both files: a signature over the shipped file
    # alone would miss a supplier just learned, until an unrelated file
    # happened to change too.
    firma = _firma_dei_due_registri(documento)
    if firma is not None:
        memorizzato = _NOMI_IN_MEMORIA.get(chiave)
        if memorizzato is not None and memorizzato[0] == firma:
            return dict(memorizzato[1])
    nomi = nomi_dichiarati_fra(adattatori(documento))
    if firma is not None:
        _NOMI_IN_MEMORIA[chiave] = (firma, dict(nomi))
    return nomi


def nome_del_fornitore(supplier_id: Any, percorso: Path | None = None) -> str:
    """The readable name of a supplier, with a fallback when it's unknown.

    The fallback isn't the bare id: `nuovo_fornitore` becomes "NUOVO
    FORNITORE", since an underscore in the middle of a sentence reads as a
    program error.
    """

    identificativo = str(supplier_id or "").strip()
    if not identificativo:
        return "FORNITORE"
    nome = nomi_dei_fornitori(percorso).get(identificativo.casefold())
    if nome:
        return nome
    return re.sub(r"[-_]+", " ", identificativo).strip().upper() or "FORNITORE"


def motivo_registro_illeggibile(percorso: Path | None = None) -> str | None:
    """The message to show when the registry won't open, `None` if it does.

    "The registry doesn't declare how to write the order" and "the registry
    can't be read" send the user to two different places: the first to
    declare `order_write`, the second to fix a broken JSON file. Since
    `adattatori()` returns `[]` in both cases, a caller relying on it alone
    would always report the first message, even in front of a file that
    won't open.
    """

    _voci, errore = _leggi_registro(percorso)
    return errore


def _voci_di_un_documento(documento_percorso: Path, *, quale: str) -> tuple[list[dict[str, Any]], str | None]:
    """The entries of ONE registry file, and the reason if it couldn't be opened."""

    try:
        documento = json.loads(documento_percorso.read_text(encoding="utf-8"))
    except OSError as errore:
        return [], f"{quale} non leggibile: {errore}"
    except ValueError as errore:
        return [], f"{quale} non interpretabile: {errore}"
    voci = documento.get("adapters") if isinstance(documento, dict) else None
    if not isinstance(voci, list):
        return [], f"{quale} senza l'elenco «adapters»"
    return [voce for voce in voci if isinstance(voce, dict)], None


def _leggi_registro(percorso: Path | None) -> tuple[list[dict[str, Any]], str | None]:
    """The registry's entries and, when it couldn't be read, the reason.

    The effective registry is made of two files: the shipped one
    (`references/adapters.json`, under git) and the learned one
    (`app/data/adattatori_imparati.json`, outside git). At equal `id`, the
    learned entry wins — it's the more recent answer, given by someone with
    the document in front of them — as long as the shipped entry is the
    one it was learned from. If the shipped entry changed since, the
    shipped entry wins and the learned one is excluded: see
    `_motivo_del_superamento`. `adattatori_superati` reports which entries
    that affects; `metti_da_parte_le_superate` removes them from the file,
    which the pipeline runs at the start of every comparison.

    A missing learned file is normal — a fresh install has learned nothing.
    A broken learned file is reported but doesn't block: recognition
    falls back to the shipped registry alone, and the reason is surfaced
    here, so a failed recognition caused by an unreadable file doesn't look
    like an unknown document.
    """

    spedito_percorso = Path(percorso or REGISTRO)
    spedite, errore = _voci_di_un_documento(spedito_percorso, quale="Registro degli adattatori")
    if errore is not None:
        return [], errore

    imparato_percorso = percorso_imparato(spedito_percorso)
    if not imparato_percorso.is_file():
        return spedite, None
    imparate, errore_imparato = _voci_di_un_documento(
        imparato_percorso, quale="Registro degli adattatori imparati qui",
    )
    if errore_imparato is not None:
        return spedite, errore_imparato

    spedite_per_id = _spedite_per_id(spedite)
    attive = [voce for voce in imparate if _motivo_del_superamento(voce, spedite_per_id) is None]
    per_id = {str(voce.get("id") or ""): voce for voce in attive if str(voce.get("id") or "")}
    if not per_id:
        return spedite, None
    fuse = [per_id.pop(str(voce.get("id") or ""), voce) for voce in spedite]
    # Entries learned from scratch, absent from the shipped registry: appended
    # at the end, in the order they were learned.
    fuse.extend(voce for voce in attive if str(voce.get("id") or "") in per_id)
    return fuse, None


def scrittura_ordine(adattatore: dict[str, Any]) -> dict[str, Any]:
    """How the order is written into this supplier's price list, if known.

    `{}` means "unknown": the supplier still enters and stays in the
    comparison, but no order file will be generated for it — and that must
    be reported rather than discovered at the end.
    """

    dichiarazione = (adattatore or {}).get("order_write")
    return dichiarazione if isinstance(dichiarazione, dict) and dichiarazione else {}


def fornitori_con_scrittura(percorso: Path | None = None) -> set[str]:
    """The suppliers the registry declares fillable.

    The program's single definition of "fillable": used by the launcher, to
    build the write configuration, and by the orchestrator, to warn when a
    supplier in the comparison isn't covered. Two lists drifting apart would
    mean a warning that doesn't match what actually happens.
    """

    return {
        str(voce.get("supplier_id") or "").strip().casefold()
        for voce in adattatori(percorso)
        if scrittura_ordine(voce) and str(voce.get("supplier_id") or "").strip()
    }


def fogli_del_profilo(profilo: dict[str, Any]) -> list[dict[str, Any]]:
    """The sheets to examine, which a CSV doesn't have.

    A CSV profile is flat — no `sheets` — while a spreadsheet profile has one
    per tab. Treating a CSV as a single sheet avoids running two recognition
    engines that could drift apart.
    """

    fogli = (profilo or {}).get("sheets")
    if isinstance(fogli, list):
        return [foglio for foglio in fogli if isinstance(foglio, dict)]
    return [profilo] if isinstance(profilo, dict) else []


def righe_di_intestazione(foglio: dict[str, Any]) -> list[dict[str, Any]]:
    """The sheet's rows that could be a header, as the profile sees them.

    Reads the profile as-is: the inspector already produced it, and reading
    the file a second time here would create a second truth about the same
    document.

    `header_candidates` only keeps rows containing at least one word from a
    list hard-coded in the inspector: a supplier who titles columns their
    own way would never make it this far. So when the profile carries
    `header_rows` — the first non-empty rows, verbatim — those are used
    instead, since they don't depend on any word list.
    """

    righe: list[dict[str, Any]] = []
    for candidato in foglio.get("header_rows") or foglio.get("header_candidates") or []:
        if not isinstance(candidato, dict):
            continue
        valori = list(candidato.get("values") or [])
        token = impronta_intestazioni(valori)
        if token:
            righe.append({"row": candidato.get("row"), "headers": token, "values": valori})
    return righe


def _quota(colonna: dict[str, Any], tipo: str, scarto: int = 0) -> float:
    """The fraction of non-empty cells of that type, as written.

    `scarto` excludes cells that aren't data from the count — in practice the
    header cell, which is text sitting in the same column as the numbers.

    A formula counts as a formula here, which matters: this ratio feeds the
    column-shape fingerprints that decide whose document this is, and how
    a column is written is an identity trait like any other. One supplier's
    price list has no formulas at all; another's has thousands, in the same
    calculated columns. Counting formulas by their computed result would
    make the two fingerprints converge and present one supplier's document
    as a variation of the other's. A fingerprint that wants to accept
    formulas declares that itself: `any_of` reads the type name, and
    "formula" is a type like any other.
    """

    non_vuote = max(1, int(colonna.get("nonempty") or 0) - max(0, scarto))
    return float((colonna.get("types") or {}).get(tipo, 0)) / non_vuote


def _quota_leggibile(colonna: dict[str, Any], tipo: str, scarto: int = 0) -> float:
    """The fraction of cells that evaluate to that type, formulas included.

    A cell written `=SUM(E4*(1-5%))` in a price column evaluates to a number,
    which is what a reader opening the document with `data_only=True` sees.
    The profile opens it with `data_only=False` and finds the formula text
    instead: without this, a price column made entirely of formulas would
    show as 0% numeric, and that supplier would be declassed to
    `SCHEMA_VARIATO` every week — a permanent hand mapping. Two parts of the
    same program would be looking at the same cell and disagreeing about it.

    The cached evaluated value is what
    `inspect_sources.censisci_valori_delle_formule` records in
    `formula_values`. A formula that evaluates to text stays text: the
    check still fails a price column that turned into text, which is the
    reason it exists.
    """

    non_vuote = max(1, int(colonna.get("nonempty") or 0) - max(0, scarto))
    conteggio = float((colonna.get("types") or {}).get(tipo, 0))
    conteggio += float((colonna.get("formula_values") or {}).get(tipo, 0))
    return conteggio / non_vuote


def _colonne_per_indice(foglio: dict[str, Any]) -> dict[int, dict[str, Any]]:
    colonne = {}
    for colonna in foglio.get("columns") or []:
        if isinstance(colonna, dict) and isinstance(colonna.get("index"), int):
            colonne[colonna["index"]] = colonna
    return colonne


def _punteggio_forma(firma: dict[str, Any], foglio: dict[str, Any]) -> tuple[float, list[str]]:
    """Score of a column-shape fingerprint.

    Larice's price list has no header row at all: its columns are recognized
    from how they're populated. Without this path, Larice's rule would have
    to live in code, which is exactly what this schema-registry approach
    replaces.
    """

    intervallo = foglio.get("active_range") or {}
    colonne = _colonne_per_indice(foglio)
    minime = firma.get("min_columns")
    if isinstance(minime, int) and int(intervallo.get("max_column") or 0) < minime:
        return 0.0, []
    richieste = [indice for indice in (firma.get("required_columns") or []) if isinstance(indice, int)]
    if any(indice not in colonne for indice in richieste):
        return 0.0, []
    # The columns that must be empty. Almost any price list in the world
    # has prices, quantities and barcodes; without a negative trait too, a
    # promotions sheet with columns A-F filled, G and H empty and just the
    # word "ORDINE" above them would match a different supplier's price
    # list. An empty column doesn't appear among the profile's columns at
    # all: that's how "nothing is here" gets expressed.
    assenti = [indice for indice in (firma.get("absent_columns") or []) if isinstance(indice, int)]
    if any(indice in colonne for indice in assenti):
        return 0.0, []

    punteggio = 0.0
    prove: list[str] = []
    for verifica in firma.get("checks") or []:
        if not isinstance(verifica, dict):
            continue
        colonna = colonne.get(verifica.get("column"))
        if colonna is None:
            continue
        minimo = verifica.get("min_nonempty")
        if isinstance(minimo, int) and int(colonna.get("nonempty") or 0) < minimo:
            continue
        # The ceiling: for columns identified by carrying just ONE thing. On
        # the promotions sheet column H holds only the word "ORDINE", and a
        # second occurrence would make the start of the product rows
        # undeducible — the reader would raise inside the pipeline, stopping
        # the recompute for every supplier instead of surfacing this one
        # document for review.
        massimo = verifica.get("max_nonempty")
        if isinstance(massimo, int) and int(colonna.get("nonempty") or 0) > massimo:
            continue
        rapporto = verifica.get("type_ratio")
        if isinstance(rapporto, dict):
            somma = sum(_quota(colonna, str(tipo)) for tipo in (rapporto.get("any_of") or []))
            # Strictly `>`, not `>=`: matches the threshold this rule already
            # had before it moved out of code, tuned against real price
            # lists; changing it here would silently move that threshold.
            if not somma > float(rapporto.get("min_exclusive", 0.0)):
                continue
        attesi = verifica.get("examples_include")
        if attesi:
            esempi = {str(valore).strip().upper() for valore in (colonna.get("examples") or [])}
            if not {str(valore).strip().upper() for valore in attesi} <= esempi:
                continue
        punteggio += float(verifica.get("weight") or 0.0)
        if verifica.get("evidence"):
            prove.append(str(verifica["evidence"]))
    return punteggio, prove


def _candidati(voci: list[dict[str, Any]], fogli: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The adapters this document could be, with how well each matches."""

    trovati: list[dict[str, Any]] = []
    for ordine, voce in enumerate(voci):
        firma = voce.get("header_signature")
        if isinstance(firma, dict) and str(firma.get("kind") or "headers") == "headers":
            richieste = sorted({normalizza(token) for token in (firma.get("required") or [])} - {""})
            for foglio in fogli if richieste else []:
                riga = next(
                    (riga for riga in righe_di_intestazione(foglio)
                     if set(richieste) <= set(riga["headers"])),
                    None,
                )
                if riga is None:
                    continue
                trovati.append({
                    "voce": voce, "ordine": ordine, "kind": "headers", "foglio": foglio,
                    "confidence": 0.99, "obbligatorie": len(richieste), "prove": [],
                    "impronta": impronta(
                        foglio.get("name"), riga["row"],
                        (riga["row"] + 1) if isinstance(riga["row"], int) else None,
                        riga["values"],
                    ),
                })
                break
        forma = voce.get("column_shape_signature")
        if isinstance(forma, dict):
            soglia = float(forma.get("min_score") or 0.0)
            massimo = float(forma.get("max_confidence") or 1.0)
            for foglio in fogli:
                punteggio, prove = _punteggio_forma(forma, foglio)
                if punteggio < soglia:
                    continue
                trovati.append({
                    "voce": voce, "ordine": ordine, "kind": "shape", "foglio": foglio,
                    "confidence": round(min(massimo, punteggio), 2),
                    "obbligatorie": len(forma.get("required_columns") or []), "prove": prove,
                    # With no header row, the observed fingerprint stays that
                    # of the sheet: reporting "no header row" is a fact,
                    # inventing one would be a lie.
                    "impronta": impronta(foglio.get("name"), None, None, []),
                })
                break
    return trovati


# How many required headers can be missing from an adapter for the document
# to still count as "its own, minus this one thing" instead of unknown. Two:
# an accidentally emptied cell, or a column the supplier stopped titling.
# From three on, this stops being recognition and becomes guessing.
MASSIMO_OBBLIGATORIE_MANCANTI = 2

# And how many must still be present regardless. Claiming "this is BETULLA's
# price list" having seen two headers out of five would be confidently
# wrong, which is worse than saying "not recognized".
MINIMO_OBBLIGATORIE_PRESENTI = 3


def lettera_di_colonna(numero: Any) -> str:
    """1-based number to letter, as shown in a spreadsheet.

    For talking to someone with the document open: "column C" is found,
    "column 3" is counted.
    """

    try:
        indice = int(numero)
    except (TypeError, ValueError):
        return ""
    if indice < 1:
        return ""
    lettere = ""
    while indice > 0:
        indice, resto = divmod(indice - 1, 26)
        lettere = chr(ord("A") + resto) + lettere
    return lettere


def _nome_leggibile(voce: dict[str, Any], token: str) -> str:
    """This header's name in the document, not in the fingerprint.

    The fingerprint carries normalized tokens ("ordine", "codart"), since
    that's what gets compared. But someone looking for the cell in the
    sheet needs the name they'll actually see, and that's stored elsewhere
    in the same entry: the field mapping, or the expected header on the
    order column. When it's nowhere, the uppercased token is shown instead —
    ugly, but accurate.
    """

    fonti: list[Any] = []
    mappatura = voce.get("field_mapping")
    if isinstance(mappatura, dict):
        colonne = mappatura.get("columns")
        if isinstance(colonne, dict):
            fonti.extend(colonne.values())
    scrittura = voce.get("order_write")
    if isinstance(scrittura, dict):
        fonti.append(scrittura.get("expected_header"))
    for fonte in fonti:
        if isinstance(fonte, str) and normalizza(fonte) == token:
            return " ".join(fonte.split())
    return token.upper()


def _mancato_per_un_pelo(voci: list[dict[str, Any]],
                         fogli: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The adapter this document is almost a match for, if there is one.

    Not a candidate, and never becomes one: the state stays `AMBIGUO` and no
    price list is read with an adapter that's missing a piece. This only
    changes what the program reports knowing.

    A real scenario this guards: a price list can come back `AMBIGUO` because
    someone opened it in Excel and resaved it with one header cell emptied
    by accident — the other required headers are all present, in their
    usual place. The program has everything it needs to say so instead of
    just "no known signature matched", which invites confirming the guided
    mapping by hand and ending up with a redundant learned adapter on top
    of a perfectly good shipped one.

    The defense against a false match here is position: the required
    headers that are present must sit where the registry says they sit. A
    document from a different supplier that happens to share three column
    names almost never has them in the same positions too.
    """

    migliore: dict[str, Any] | None = None
    for voce in voci:
        firma = voce.get("header_signature")
        if not isinstance(firma, dict) or str(firma.get("kind") or "headers") != "headers":
            continue
        richieste = sorted({normalizza(token) for token in (firma.get("required") or [])} - {""})
        if len(richieste) < MINIMO_OBBLIGATORIE_PRESENTI:
            continue
        attese = firma.get("columns") if isinstance(firma.get("columns"), dict) else {}
        for foglio in fogli:
            for riga in righe_di_intestazione(foglio):
                osservate = set(riga["headers"])
                mancanti = [token for token in richieste if token not in osservate]
                presenti = [token for token in richieste if token in osservate]
                if not mancanti or len(mancanti) > MASSIMO_OBBLIGATORIE_MANCANTI:
                    continue
                if len(presenti) < MINIMO_OBBLIGATORIE_PRESENTI:
                    continue
                posizioni = posizioni_delle_intestazioni(riga["values"])
                fuori_posto = [
                    token for token in presenti
                    if token in attese and posizioni.get(token) != attese.get(token)
                ]
                if fuori_posto:
                    continue
                trovato = {
                    "voce": voce, "foglio": foglio, "riga": riga,
                    "mancanti": mancanti, "presenti": presenti, "attese": attese,
                }
                if migliore is None or (len(mancanti), -len(presenti)) < (
                    len(migliore["mancanti"]), -len(migliore["presenti"])
                ):
                    migliore = trovato
                break
    return migliore


def dettagli_del_mancato_per_un_pelo(trovato: dict[str, Any]) -> list[dict[str, str]]:
    """What's missing, shaped so a caller can show it without redoing the work.

    The pipeline and its logs use the sentence built below; the page builds
    its own, with proper accented characters this file doesn't use. So the
    two don't diverge, the underlying data has one source, here.
    """

    voce = trovato["voce"]
    return [
        {
            "header": _nome_leggibile(voce, token),
            "column": lettera_di_colonna(trovato["attese"].get(token)),
        }
        for token in trovato["mancanti"]
    ]


def _frase_del_mancato_per_un_pelo(trovato: dict[str, Any]) -> str:
    """"It's their list, minus this one thing" — said to someone with the file open."""

    voce = trovato["voce"]
    nome = str(voce.get("display_name") or voce.get("supplier_id") or voce.get("id") or "").strip()
    pezzi = []
    for dettaglio in dettagli_del_mancato_per_un_pelo(trovato):
        titolo, lettera = dettaglio["header"], dettaglio["column"]
        pezzi.append(f"«{titolo}» nella colonna {lettera}" if lettera else f"«{titolo}»")
    quali = " e ".join(pezzi)
    quante = len(trovato["presenti"])
    manca = "manca l'intestazione" if len(pezzi) == 1 else "mancano le intestazioni"
    altre = (f"l'altra obbligatoria c'è, ed è al posto giusto"
             if quante == 1 else
             f"le altre {quante} obbligatorie ci sono tutte, e tutte al posto giusto")
    celle = "quella cella può essere stata svuotata" if len(pezzi) == 1 else "quelle celle possono essere state svuotate"
    return (
        f"Sembra il listino di {nome}, ma {manca} {quali}: {altre}. "
        f"Se il file è stato aperto in Excel e risalvato, {celle} senza volerlo: "
        f"ricarica l'originale del fornitore."
    )


def _rango_completo(candidato: dict[str, Any]) -> tuple[Any, ...]:
    """How well a candidate describes the document, in order of importance."""

    return (candidato["confidence"], candidato["senza_guasti"],
            candidato["obbligatorie"], -candidato["ordine"])


def _versione_imparata_a_parita(scelto: dict[str, Any],
                                candidati: list[dict[str, Any]]) -> dict[str, Any]:
    """Between a shipped adapter and one learned ON TOP OF IT, an exact tie goes to the learned one.

    This is the registry's general principle — "at equal `id`, the learned
    entry wins, since it's the more recent answer, given by someone with the
    document in front of them" — applied here too, to candidate selection and
    not just to merging the two files.

    Without this, moving the order column from the page would produce an
    entry that reads the document exactly like the shipped one and only
    changes where the order is written; the two would tie on every score,
    and the newer entry would never actually get picked.

    Applies only within the same family, i.e. between `betulla_v1` and
    its `betulla_v1__locale`. Between two different suppliers' adapters that
    tie, there's no older or newer one — just registry order, which stays as
    it is.

    What this trades away: an old learned entry keeps winning even after the
    shipped one improves, as long as the two still tie. Not on every
    improvement — only one that doesn't change how the checks come out — and
    the fix is deleting the entry from `app/data/adattatori_imparati.json`,
    which loses nothing now that the shipped entry never disappears.
    """

    identificativo = str(scelto["voce"].get("id") or "")
    if identificativo.endswith(SUFFISSO_LOCALE):
        return scelto
    famiglia = adattatore_base(identificativo)
    rango = _rango_completo(scelto)[:3]
    for candidato in candidati:
        suo_id = str(candidato["voce"].get("id") or "")
        if not suo_id.endswith(SUFFISSO_LOCALE):
            continue
        if adattatore_base(suo_id) != famiglia:
            continue
        if _rango_completo(candidato)[:3] == rango:
            return candidato
    return scelto


def riconosci(profilo: dict[str, Any], percorso: Path | None = None) -> dict[str, Any]:
    """Say which adapter matches a document, and why.

    The filename never enters this decision: some suppliers send documents
    named things like `formattato_104233.xls`, which say nothing to anyone,
    and a supplier renaming their own price list shouldn't become a new
    supplier.

    One extra column beyond the declared ones doesn't demote anything by
    itself: it goes into `unknown_headers` and an evidence line. A supplier
    adding a decorative column shouldn't cost a manual remapping every week. What
    can demote is one of the deterministic checks: for a reader that reads
    by position, an extra column shifts everything after it, and
    `posizioni_intestazioni` catches that.
    """

    voci, errore = _leggi_registro(percorso)
    if errore:
        return _nessun_candidato(errore)
    fogli = fogli_del_profilo(profilo)
    candidati = _candidati(voci, fogli)
    if not candidati:
        # "No known signature matched" is true but not the whole story: an
        # adapter can be missing one or two required headers out of five,
        # with the rest exactly where it says they'd be. That's not an
        # unknown document, it's "its own, minus this one thing" — and
        # whoever reads the message should understand that without opening
        # the file and counting columns.
        pelo = _mancato_per_un_pelo(voci, fogli)
        if pelo is None:
            return _nessun_candidato("Nessuna firma nota sufficiente")
        esito = _nessun_candidato(_frase_del_mancato_per_un_pelo(pelo))
        # State stays `AMBIGUO`: report what's missing, don't read the price
        # list with an adapter that's missing a piece.
        quasi = pelo["voce"]
        esito["quasi_adapter_id"] = quasi.get("id")
        esito["quasi_supplier_name"] = str(
            quasi.get("display_name") or quasi.get("supplier_id") or quasi.get("id") or ""
        ).strip()
        esito["quasi_missing"] = dettagli_del_mancato_per_un_pelo(pelo)
        esito["quasi_present"] = len(pelo["presenti"])
        esito["missing_headers"] = list(pelo["mancanti"])
        return esito

    # Checks run on ALL candidates, not just the winner, which is the reason
    # `__locale` entries exist: since a mapping confirmed on a shipped
    # supplier is written alongside it instead of over it, the same document
    # can have two adapters resembling it — the shipped one and the one
    # learned here. Between the two, the one that should win is whichever
    # actually reads the document — the right sheet, the right row, columns
    # in place — not whichever comes first in the registry list. There are at
    # most a handful of candidates, so the cost is worth paying.
    for candidato in candidati:
        candidato["verifiche"] = verifica_deterministica(
            candidato["voce"], candidato["foglio"], candidato["impronta"], fogli,
        )
        candidato["senza_guasti"] = all(verifica["ok"] for verifica in candidato["verifiche"])
    # At equal confidence, whoever passes the checks wins; then whoever
    # declares more required headers, the more specific schema and so the
    # better description of the document. Still tied, registry order: between
    # two DIFFERENT suppliers' adapters that tie on everything, there's
    # nothing better to compare, so the one declared first is the one the
    # program knows it shipped first.
    scelto = max(candidati, key=_rango_completo)
    # Between two versions of the SAME thing the rule is different: see
    # below. Applied after, since it looks at the other competing entries
    # and can't be decided from one candidate alone.
    scelto = _versione_imparata_a_parita(scelto, candidati)
    voce = scelto["voce"]
    foglio = scelto["foglio"]
    osservata = scelto["impronta"]
    firma = voce.get("header_signature") if isinstance(voce.get("header_signature"), dict) else {}

    osservate = set(osservata["headers"])
    richieste = {normalizza(token) for token in (firma.get("required") or [])} - {""}
    dichiarate = richieste | ({normalizza(token) for token in (firma.get("known") or [])} - {""})
    sconosciute = sorted(osservate - dichiarate)
    mancanti = sorted(richieste - osservate)

    verifiche = scelto["verifiche"]
    fallite = [verifica for verifica in verifiche if not verifica["ok"]]

    prove: list[str] = []
    if scelto["kind"] == "headers":
        prove.append(
            f"Impronta per intestazioni di «{voce.get('id')}»: le {len(richieste)} intestazioni "
            f"obbligatorie sono tutte nel foglio «{foglio.get('name')}», riga {osservata['header_row']}"
        )
        if osservata["data_start_row"] is not None:
            prove.append(f"Primi dati alla riga {osservata['data_start_row']}")
    else:
        prove.append(
            f"Impronta per forma delle colonne di «{voce.get('id')}» nel foglio "
            f"«{foglio.get('name')}»: punteggio {scelto['confidence']}"
        )
        prove.extend(scelto["prove"])
    if sconosciute:
        prove.append("Intestazioni non dichiarate, che non declassano lo schema: " + ", ".join(sconosciute))
    if mancanti:
        prove.append("Intestazioni dichiarate obbligatorie e non trovate: " + ", ".join(mancanti))
    for verifica in fallite:
        prove.append(f"Verifica «{verifica['name']}» non superata: {verifica['detail']}")

    if scelto["kind"] == "headers":
        confidenza = 0.99 if not fallite else 0.98
    else:
        confidenza = scelto["confidence"]
    return {
        "state": "SCHEMA_VARIATO" if fallite else "SCHEMA_NOTO",
        "adapter_id": voce.get("id"),
        "confidence": round(confidenza, 2),
        "evidence": prove,
        "signature": osservata,
        "checks": verifiche,
        "unknown_headers": sconosciute,
        "missing_headers": mancanti,
    }


def _nessun_candidato(motivo: str) -> dict[str, Any]:
    """The document is still unresolved: say so, rather than pick at random."""

    return {
        "state": "AMBIGUO",
        "adapter_id": None,
        "confidence": 0.0,
        "evidence": [motivo],
        "signature": None,
        "checks": [],
        "unknown_headers": [],
        "missing_headers": [],
    }


def verifica_deterministica(adattatore: dict[str, Any], foglio: dict[str, Any],
                            impronta_osservata: dict[str, Any],
                            fogli: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The checks that replace a manual review on the fast path.

    A matching fingerprint says the document belongs to that supplier, not
    that the reader can actually work with it: the price column may have
    turned into text, the sheet may have moved, columns may have swapped
    places, the price list may be empty. These are exactly the things that
    would otherwise fail silently.

    `fogli` is used only by the sheet check: `sheet: "FIRST"` means "the
    document's first sheet", and answering that requires seeing them all.
    """

    mappatura = adattatore.get("field_mapping") if isinstance(adattatore.get("field_mapping"), dict) else {}
    firma = adattatore.get("header_signature") if isinstance(adattatore.get("header_signature"), dict) else {}
    if mappatura:
        verifiche = [
            _verifica_colonne_attese(mappatura, foglio, impronta_osservata),
            _verifica_riga_intestazione(mappatura, impronta_osservata),
            _verifica_foglio(mappatura, foglio, fogli),
        ]
    else:
        verifiche = [{"name": nome, "ok": True, "detail": NON_APPLICABILE}
                     for nome in ("colonne_attese", "riga_intestazione", "foglio")]
    # An empty price list matters for dedicated-reader adapters too: it's the
    # only way a supplier can drop out of the comparison without anything
    # actually going wrong.
    verifiche.append(_verifica_righe_dati(mappatura, firma, foglio, impronta_osservata))
    # Types are checked even without a field mapping: `header_aliases` and
    # `column_map` still say where the numeric fields are. Skipping this
    # check for dedicated-reader adapters would exempt exactly the three
    # biggest suppliers in the comparison — where a price turning into text
    # does the most damage.
    verifiche.append(_verifica_tipi(adattatore, mappatura, foglio, impronta_osservata))
    verifiche.append(_verifica_posizioni(firma, foglio, impronta_osservata))
    return verifiche


def _valori_intestazione(foglio: dict[str, Any], impronta_osservata: dict[str, Any]) -> list[Any]:
    """The recognized header row, in the order it appears in the sheet.

    Position is needed, not just the set of tokens: a column declared by
    name is only found in the profile by knowing which column it's in.
    """

    riga = impronta_osservata.get("header_row")
    if riga is None:
        return []
    for candidato in righe_di_intestazione(foglio):
        if candidato["row"] == riga:
            return candidato["values"]
    return []


def _indice_di_colonna(valori: list[Any], nome: Any) -> int | None:
    """The 1-based column that carries this name — or number, or letter.

    Number is tried first because it's already the answer: a mapping
    declares a column by number when that column has no name of its own
    or shares one with another — on ACERO "COSTO IMPON." appears twice, in
    columns 9 and 15, so the price can't be pointed to by name at all. This
    matches the rule the real reader applies
    (`prepare_manifest_sources.column_number`: a true integer >= 1 is simply
    the column number).

    Only true integers count: a digit string ("9", "09") is a column name
    like any other to the real reader, and must be treated the same way
    here — answering "column 9" to a lookup the reader would refuse would
    mean declaring a document verified that will never actually be read.

    Name is tried before letter on purpose: "UM" and "QT" are real headers
    on one supplier's price list and would also be plausible column letters.
    Checking headers first avoids reading column 567 instead of the third.
    """

    if isinstance(nome, bool):
        return None
    if isinstance(nome, int):
        return nome if nome >= 1 else None

    cercato = normalizza(nome)
    if not cercato:
        return None
    for posizione, valore in enumerate(valori, start=1):
        if normalizza(valore) == cercato:
            return posizione
    lettere = str(nome or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", lettere):
        return None
    indice = 0
    for lettera in lettere:
        indice = indice * 26 + (ord(lettera) - ord("A") + 1)
    return indice


def _verifica_colonne_attese(mappatura: dict[str, Any], foglio: dict[str, Any],
                             impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    nome = "colonne_attese"
    colonne = mappatura.get("columns")
    if not isinstance(colonne, dict) or not colonne:
        return {"name": nome, "ok": True, "detail": "la mappatura non dichiara colonne: verifica non applicabile"}
    osservate = set(impronta_osservata.get("headers") or [])
    # A mapping can declare a column by name or, when the column has no
    # name, by number — the form `prepare_manifest_sources` documents, used
    # for almost every order column, which arrives blank. Searching for "6"
    # among the headers would never find it, leaving the supplier
    # permanently unresolved.
    attese: set[str] = set()
    per_numero: list[int] = []
    for valore in colonne.values():
        if isinstance(valore, bool) or valore in (None, ""):
            continue
        if isinstance(valore, int) or str(valore).strip().isdigit():
            per_numero.append(int(str(valore).strip()))
            continue
        token = normalizza(valore)
        if token:
            attese.add(token)
    if attese and not osservate:
        return {"name": nome, "ok": False,
                "detail": "nessuna intestazione osservata: le colonne dichiarate per nome non si possono verificare"}
    mancanti = sorted(attese - osservate)
    if mancanti:
        return {"name": nome, "ok": False, "detail": "colonne dichiarate e non trovate: " + ", ".join(mancanti)}
    larghezza = int((foglio.get("active_range") or {}).get("max_column") or 0)
    presenti = set(_colonne_per_indice(foglio)) | set(range(1, larghezza + 1))
    fuori = sorted(indice for indice in per_numero if indice not in presenti)
    if fuori:
        return {"name": nome, "ok": False,
                "detail": "colonne dichiarate per numero e fuori dal documento, largo "
                          f"{larghezza} colonne: " + ", ".join(str(indice) for indice in fuori)}
    conto = len(attese) + len(per_numero)
    coda = f", di cui {len(per_numero)} per numero" if per_numero else ""
    return {"name": nome, "ok": True, "detail": f"tutte le {conto} colonne dichiarate sono nel documento{coda}"}


def _verifica_riga_intestazione(mappatura: dict[str, Any], impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    nome = "riga_intestazione"
    attesa = mappatura.get("header_row")
    if attesa is None:
        return {"name": nome, "ok": True,
                "detail": "la mappatura non dichiara la riga di intestazione: verifica non applicabile"}
    osservata = impronta_osservata.get("header_row")
    return {
        "name": nome,
        "ok": osservata == attesa,
        "detail": f"intestazioni trovate alla riga {osservata}, la mappatura dichiara la riga {attesa}",
    }


def _verifica_foglio(mappatura: dict[str, Any], foglio: dict[str, Any],
                     fogli: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    nome = "foglio"
    atteso = mappatura.get("sheet")
    riconosciuto = foglio.get("name")
    if atteso is None:
        return {"name": nome, "ok": True, "detail": "la mappatura non dichiara un foglio: verifica non applicabile"}
    if str(atteso).strip().upper() == "FIRST":
        # "FIRST" doesn't mean "the first one that matches": the real reader
        # (`prepare_manifest_sources.selected_sheet`) always opens sheet
        # number one. If the schema was recognized on a later sheet — say the
        # supplier added a cover sheet — the reader would open the cover
        # sheet, not the price list.
        if not fogli:
            return {"name": nome, "ok": True,
                    "detail": "la mappatura vuole il primo foglio del documento, che non è stato osservato"}
        primo = fogli[0].get("name")
        return {
            "name": nome,
            "ok": normalizza(riconosciuto) == normalizza(primo),
            "detail": f"foglio riconosciuto «{riconosciuto}», la mappatura vuole il primo del documento, che è «{primo}»",
        }
    return {
        "name": nome,
        "ok": normalizza(riconosciuto) == normalizza(atteso),
        "detail": f"foglio riconosciuto «{riconosciuto}», la mappatura dichiara «{atteso}»",
    }


def _verifica_righe_dati(mappatura: dict[str, Any], firma: dict[str, Any], foglio: dict[str, Any],
                         impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    nome = "righe_dati"
    intervallo = foglio.get("active_range") or {}
    marcatore = mappatura.get("data_start_marker") or firma.get("data_start_marker")
    if marcatore not in (None, "", {}):
        return _verifica_marcatore_dei_dati(marcatore, foglio)
    inizio = mappatura.get("data_start_row")
    if inizio is None:
        inizio = firma.get("data_start_row")
    if inizio is None:
        inizio = impronta_osservata.get("data_start_row")
    non_vuote = int(intervallo.get("nonempty_rows") or 0)
    if inizio is None:
        return {"name": nome, "ok": non_vuote > 0,
                "detail": f"nessuna riga di dati dichiarata; il foglio ha {non_vuote} righe non vuote"}
    ultima = intervallo.get("max_row")
    ok = isinstance(ultima, int) and ultima >= int(inizio)
    return {"name": nome, "ok": ok,
            "detail": f"ultima riga con contenuto: {ultima}, i dati cominciano alla riga {inizio}"}


def _verifica_marcatore_dei_dati(marcatore: Any, foglio: dict[str, Any]) -> dict[str, Any]:
    """Is the marker that declares where data starts still where it was?

    The profile doesn't carry every row of the document, so "not found here"
    doesn't mean "not there": the real reader searches the whole file, and a
    missing marker stops reading there. This check reports what it can and
    declares the rest inconclusive, rather than pretending to have verified
    something it hasn't.

    One case is decidable right away, and it's the one that matters: if the
    marker text appears more than once in the profiled rows, the start
    of the data is already ambiguous and the document shouldn't be read
    without a human look.
    """

    nome = "righe_dati"
    if not isinstance(marcatore, dict):
        return {"name": nome, "ok": False,
                "detail": "la regola dell'inizio dei dati non è un oggetto leggibile"}
    indice = marcatore.get("column")
    if isinstance(indice, str) and re.fullmatch(r"[A-Z]{1,3}", indice.strip().upper()):
        numero = 0
        for lettera in indice.strip().upper():
            numero = numero * 26 + (ord(lettera) - ord("A") + 1)
        indice = numero
    if isinstance(indice, bool) or not isinstance(indice, int) or indice < 1:
        return {"name": nome, "ok": False,
                "detail": f"la regola dell'inizio dei dati non dice in quale colonna cercare: «{marcatore.get('column')}»"}
    esatto = str(marcatore.get("equals") or "").strip()
    contenuto = str(marcatore.get("contains") or "").strip()
    cercato = esatto or contenuto
    if not cercato:
        return {"name": nome, "ok": False,
                "detail": "la regola dell'inizio dei dati non dice che cosa cercare"}
    atteso = " ".join(cercato.split()).casefold()
    trovate: list[int] = []
    for riga in righe_visibili_del_foglio(foglio):
        valori = riga.get("values") or []
        if indice > len(valori):
            continue
        letto = " ".join(str(valori[indice - 1] if valori[indice - 1] is not None else "").split()).casefold()
        if (letto == atteso) if esatto else (atteso in letto):
            trovate.append(int(riga.get("row") or 0))
    dove = f"colonna {indice}, testo «{cercato}»"
    if len(trovate) > 1:
        return {"name": nome, "ok": False,
                "detail": f"l'inizio dei dati è ambiguo: {dove} compare alle righe "
                          + ", ".join(str(numero) for numero in trovate)}
    if not trovate:
        return {"name": nome, "ok": True,
                "detail": f"i dati cominciano dopo la riga con {dove}: non è fra le righe "
                          "profilate, si cerca sul documento intero quando si legge"}
    return {"name": nome, "ok": True,
            "detail": f"i dati cominciano dopo la riga {trovate[0]} ({dove})"}


def righe_visibili_del_foglio(foglio: dict[str, Any]) -> list[dict[str, Any]]:
    """Every row the profile carries for this sheet, deduplicated.

    Not every row of the document, and not meant to be: just the ones a
    check working off the profile can say something about.
    """

    per_numero: dict[int, dict[str, Any]] = {}
    gruppi = [foglio.get("header_rows") or [], foglio.get("header_candidates") or [],
              foglio.get("section_rows") or []]
    campioni = foglio.get("samples") or {}
    for chiave in ("initial", "middle", "final"):
        gruppi.append(campioni.get(chiave) or [])
    for gruppo in gruppi:
        for voce in gruppo:
            if not isinstance(voce, dict) or not isinstance(voce.get("values"), list):
                continue
            try:
                numero = int(voce.get("row"))
            except (TypeError, ValueError):
                continue
            per_numero[numero] = {"row": numero, "values": voce["values"]}
    return [per_numero[numero] for numero in sorted(per_numero)]


def posizione_del_campo(adattatore: dict[str, Any], mappatura: dict[str, Any], campo: str) -> Any:
    """Where the registry declares a field to be, or `None` if it doesn't say.

    The value comes back as written: a column letter ("R"), a 1-based
    number, or a header name ("Descr.Commerciale"). The caller resolves it
    against the document it has — three ways of saying the same thing, and
    the registry uses all three depending on the supplier.

    Public version of `_dove_sta_il_campo`, which the type check uses: the
    same declaration also tells the writer where to check that the
    destination row carries the right product.
    """

    return _dove_sta_il_campo(adattatore or {}, mappatura or {}, campo)


def indice_della_colonna(valori: list[Any], dichiarata: Any) -> int | None:
    """The 1-based column that declaration points to on this sheet.

    Public version of `_indice_di_colonna`, same rules: a true integer is
    already the column number, a name is looked up among the headers, and
    only last is a letter tried. For callers that need to show where the
    program will read from — the Import page — and must answer exactly what
    the real reader would, not a second rule written elsewhere.
    """

    return _indice_di_colonna(list(valori or []), dichiarata)


def _dove_sta_il_campo(adattatore: dict[str, Any], mappatura: dict[str, Any], campo: str) -> Any:
    """Where the registry says a field is, whichever way it declares it.

    Several suppliers have no `field_mapping` because they use a dedicated
    reader, but the registry still says where their columns are: BETULLA via
    `header_aliases` ("Cessione"), Larice via `column_map` ("O"). Those
    declarations already exist, and using them is the only way the type
    check also covers those suppliers.
    """

    colonne = mappatura.get("columns")
    if isinstance(colonne, dict) and colonne.get(campo):
        return colonne[campo]
    alias = adattatore.get("header_aliases")
    if isinstance(alias, dict):
        for nome in alias.get(campo) or []:
            if nome:
                return nome
    mappa = adattatore.get("column_map")
    if isinstance(mappa, dict) and mappa.get(campo):
        return mappa[campo]
    return None


def _verifica_tipi(adattatore: dict[str, Any], mappatura: dict[str, Any], foglio: dict[str, Any],
                   impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    nome = "tipi_plausibili"
    valori = _valori_intestazione(foglio, impronta_osservata)
    colonne = _colonne_per_indice(foglio)
    misure: list[tuple[str, float]] = []
    for campo in CAMPI_NUMERICI:
        dichiarata = _dove_sta_il_campo(adattatore, mappatura, campo)
        if not dichiarata:
            continue
        indice = _indice_di_colonna(valori, dichiarata)
        colonna = colonne.get(indice) if indice is not None else None
        # A column that can't be found is already flagged by
        # `colonne_attese`: reporting it twice adds nothing for the reader.
        if colonna is None:
            continue
        # The header cell sits in the same column as the prices and is text:
        # counting it lowers the numeric ratio, and on a short price list it
        # can push it below half. A short price list isn't a broken one.
        intestata = indice <= len(valori) and str(valori[indice - 1] or "").strip() != ""
        # A column declared by number reads poorly written verbatim
        # ("unit_price_net (9)"): the reader needs to see that 9 is a column,
        # not a value.
        per_numero = isinstance(dichiarata, int) and not isinstance(dichiarata, bool)
        dove = f"colonna {dichiarata}" if per_numero or str(dichiarata).strip().isdigit() else str(dichiarata)
        misure.append((f"{campo} ({dove})", _quota_leggibile(colonna, "number", int(intestata))))
    if not misure:
        return {"name": nome, "ok": True,
                "detail": "nessuna colonna numerica dichiarata e ritrovata: verifica non applicabile"}
    cattive = [(campo, quota) for campo, quota in misure if not quota > 0.5]
    if cattive:
        return {"name": nome, "ok": False,
                "detail": "colonne dichiarate numeriche ma in maggioranza non numeriche: "
                          + ", ".join(f"{campo} {quota:.0%}" for campo, quota in cattive)}
    return {"name": nome, "ok": True,
            "detail": "colonne numeriche in maggioranza: " + ", ".join(f"{campo} {quota:.0%}" for campo, quota in misure)}


def _verifica_posizioni(firma: dict[str, Any], foglio: dict[str, Any],
                        impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    """Are the headers still where the reader goes to pick them up?

    Several adapters (BETULLA, the management-software export, Larice) use a
    dedicated reader that reads by position: it takes the price from a
    fixed row index, not from the column titled "Cessione". The set of
    headers alone doesn't protect them: with one extra column inserted at
    the front of a real price list, the fast path declared `SCHEMA_NOTO`
    with confidence 0.99 and the reader returned the wrong price entirely,
    with no warning for a whole week of orders.

    So those adapters' signatures also declare where each header sits,
    and that's what this check compares. A native adapter whose mapping
    resolves everything by name can declare nothing here, and the check is
    a no-op. Learned adapters always declare it
    (`impara_adattatore` writes it unconditionally), even when fields
    resolve by name: the column where the order is written stays
    positional, and a column inserted ahead of it would shift it without
    changing any name. The cost is a demotion to `SCHEMA_VARIATO` (one extra
    confirmation) whenever a supplier adds a harmless column — the price
    chosen to close this class of silent mismatch for good.
    """

    nome = "posizioni_intestazioni"
    dichiarate = firma.get("columns")
    if not isinstance(dichiarate, dict) or not dichiarate:
        return {"name": nome, "ok": True,
                "detail": "la firma non dichiara dove stanno le intestazioni: verifica non applicabile"}
    valori = _valori_intestazione(foglio, impronta_osservata)
    if not valori:
        return {"name": nome, "ok": False,
                "detail": "nessuna riga di intestazione osservata: le posizioni dichiarate non si possono verificare"}
    # Same function used by whoever writes the signature: if the two sides
    # counted positions differently, a learned adapter would show as out of
    # place on the very document it was learned from.
    osservate = posizioni_delle_intestazioni(valori)
    fuori_posto: list[str] = []
    malformate: list[str] = []
    for token, attesa in dichiarate.items():
        atteso = normalizza(token)
        if not atteso:
            continue
        if isinstance(attesa, bool) or not isinstance(attesa, int):
            # A position that isn't an integer can't be verified, and
            # skipping it would report "all in place" for a declaration
            # that was never actually checked. `bool` is rejected
            # explicitly because `True` is an `int` in Python.
            malformate.append(f"«{token}» dichiara una posizione che non è un numero intero")
            continue
        trovata = osservate.get(atteso)
        if trovata is None:
            fuori_posto.append(f"«{token}» attesa in colonna {attesa}, non c'è più")
        elif trovata != attesa:
            fuori_posto.append(f"«{token}» attesa in colonna {attesa}, trovata nella {trovata}")
    if malformate:
        return {"name": nome, "ok": False,
                "detail": "la firma dichiara posizioni che non sono numeri interi: "
                          + "; ".join(malformate + fuori_posto)}
    if fuori_posto:
        return {"name": nome, "ok": False,
                "detail": "il lettore di questo fornitore legge per posizione: " + "; ".join(fuori_posto)}
    return {"name": nome, "ok": True,
            "detail": f"tutte le {len(dichiarate)} intestazioni dichiarate sono nella colonna prevista"}


# How long to wait for a turn before writing to the registry, and after how
# long an unreturned turn counts as abandoned. The second number is the one
# that matters: a process killed mid-write (antivirus, window closed, PC
# powered off) would otherwise leave the registry locked forever, and a
# program that silently stops learning is worse than the race this lock
# exists to close.
ATTESA_DEL_TURNO = 5.0
TURNO_ABBANDONATO = 20.0


@contextlib.contextmanager
def _turno_di_scrittura(documento_percorso: Path):
    """One process at a time inside the read-modify-write cycle.

    Writing the file itself has always been atomic — temp file plus
    `os.replace`, so a reader finds the old registry or the new one, never a
    half-written one. What wasn't protected is the whole cycle: two
    overlapping writes read the same starting document, and the second one
    to finish overwrites the first one's entry. Measured directly: ten
    concurrent writes left just one entry in the registry.

    Not a lab-only scenario. The service is a `ThreadingHTTPServer`,
    `impara_adattatore` runs as its own process during the recompute, and
    every comparator started from the same folder writes the same
    `app/data/adattatori_imparati.json` — data paths are chosen per run, the
    registry's path isn't.

    The lock is a file created with `O_EXCL`, the one primitive that behaves
    the same on Windows and macOS. It records who holds it and since when,
    so an abandoned turn can be recognized and cleared instead of blocking
    everything forever. If `ATTESA_DEL_TURNO` elapses without acquiring it
    and it isn't abandoned, the write proceeds anyway: the worst case
    reverts to the pre-lock race, not something worse, and an adapter the
    user just confirmed shouldn't be lost because another process is slow.
    """

    lucchetto = documento_percorso.with_name(documento_percorso.name + ".lock")
    lucchetto.parent.mkdir(parents=True, exist_ok=True)
    preso = False
    scadenza = time.monotonic() + ATTESA_DEL_TURNO
    while True:
        try:
            descrittore = os.open(str(lucchetto), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                eta = time.time() - lucchetto.stat().st_mtime
            except OSError:
                continue  # just disappeared: try to acquire it again right away
            if eta > TURNO_ABBANDONATO:
                # Whoever held it is gone. `missing_ok`: fine if they removed
                # it in the meantime.
                lucchetto.unlink(missing_ok=True)
                continue
            if time.monotonic() >= scadenza:
                break
            time.sleep(0.02)
            continue
        except OSError:
            # Unwritable folder, full disk: not the place to block learning
            # for an adapter.
            break
        with os.fdopen(descrittore, "w", encoding="utf-8") as flusso:
            flusso.write(f"{os.getpid()} {time.time()}\n")
        preso = True
        break
    try:
        yield preso
    finally:
        if preso:
            lucchetto.unlink(missing_ok=True)


def scrivi_adattatore(voce: dict[str, Any], percorso: Path | None = None) -> dict[str, Any]:
    """Write an entry to the registry without discarding the previous one.

    The program runs unattended: when the user confirms a new schema, the
    old one must not disappear. If recognition gets worse the following
    week, the only way to see what changed is to still have the previous
    version to compare against.

    Written via a temp file in the same folder and `os.replace`: an
    interruption mid-write would leave the registry truncated, and a
    truncated registry stops recognizing every supplier.

    Always writes only to the learned registry
    (`app/data/adattatori_imparati.json`), never to the shipped one: that
    one is under git, and the store PC resets it on every startup. Version
    and history (`schema_version`, `previous_versions`) are counted against
    the effective registry, shipped entry included: the first schema
    learned on top of a shipped adapter is its second version, not its
    first, so the history includes the version it started from.
    """

    if not isinstance(voce, dict):
        raise ValueError("La voce da scrivere nel registro deve essere un dizionario")
    identificativo = str(voce.get("id") or "").strip()
    if not identificativo:
        raise ValueError("La voce da scrivere nel registro non ha un «id»")

    documento_percorso = percorso_imparato(percorso)
    # The `with` covers the WHOLE cycle — read, modify, write — not just
    # the file write itself, which was already atomic. See
    # `_turno_di_scrittura`.
    with _turno_di_scrittura(documento_percorso):
        if documento_percorso.exists():
            try:
                documento = json.loads(documento_percorso.read_text(encoding="utf-8"))
            except (OSError, ValueError) as errore:
                # Rewriting a registry that can't be read from scratch would
                # mean discarding every other adapter in it.
                raise ValueError(f"Il registro degli adattatori non è leggibile: {errore}") from errore
            if not isinstance(documento, dict) or not isinstance(documento.get("adapters"), list):
                raise ValueError("Il registro degli adattatori non ha l'elenco «adapters»")
        else:
            documento = {"schema_version": 1, "adapters": []}

        voci = documento["adapters"]
        posizione = next(
            (indice for indice, esistente in enumerate(voci)
             if isinstance(esistente, dict) and esistente.get("id") == identificativo),
            None,
        )
        # The shipped entry this one derives from, if any. Needed twice: as
        # the starting version when the learned entry has nothing yet, and
        # as the stamp — "born on top of this one" — that decides how long
        # the entry stays valid (see `_motivo_del_superamento`). A shipped
        # entry that exists but can't be read blocks the write instead of
        # silently restarting the version count from one: starting over
        # would discard an adapter's history without telling anyone.
        #
        # `adattatore_base`: the first version of a `betulla_v1__locale` is
        # the shipped `betulla_v1`. Looking up the full id would make it
        # start as version one with no history, and rereading the entry a
        # month later would lose the starting point — the one thing that
        # lets anyone see what changed.
        spedita_base: dict[str, Any] = {}
        spedito_percorso = Path(percorso or REGISTRO)
        if spedito_percorso.is_file():
            spedite, errore_spedito = _voci_di_un_documento(
                spedito_percorso, quale="Il registro degli adattatori",
            )
            if errore_spedito is not None:
                raise ValueError(f"Il registro degli adattatori non è leggibile: {errore_spedito}")
            cercato = adattatore_base(identificativo)
            spedita_base = next(
                (esistente for esistente in spedite if esistente.get("id") == cercato), {},
            )
        if posizione is not None:
            precedente = dict(voci[posizione])
        else:
            precedente = dict(spedita_base)

        nuova = dict(voce)
        if spedita_base:
            nuova["sopra_spedito"] = sopra_spedito_di(spedita_base)
        else:
            nuova.pop("sopra_spedito", None)
        if not precedente:
            nuova["schema_version"] = 1
            nuova.pop("previous_versions", None)
            storia: list[Any] = []
        else:
            if precedente.get("supplier_id") != voce.get("supplier_id"):
                raise ValueError(
                    f"L'adattatore «{identificativo}» è del fornitore "
                    f"«{precedente.get('supplier_id')}»: non lo si può riscrivere per "
                    f"«{voce.get('supplier_id')}»"
                )
            storia = list(precedente.pop("previous_versions", None) or [])
            storia.append(precedente)
            nuova["schema_version"] = int(precedente.get("schema_version") or 1) + 1
            nuova["previous_versions"] = storia

        if posizione is None:
            voci.append(nuova)
        else:
            voci[posizione] = nuova

        _scrivi_documento(documento_percorso, documento)
    return {
        "id": identificativo,
        "schema_version": nuova["schema_version"],
        "created": not precedente,
        "previous_versions": len(storia),
    }


def metti_da_parte_le_superate(percorso: Path | None = None,
                               quando: str | None = None) -> list[dict[str, Any]]:
    """Move learned entries the shipped registry has superseded out of `adapters`.

    They aren't deleted: they land in `adapters_messi_da_parte` in the same
    file, with a date and reason, since what the operator mapped by hand is
    information — it shows what they saw in the price list — and should
    stay readable. They do have to leave `adapters`, though, or every
    comparison would repeat the same warning forever.

    Returns cards for the entries moved, `[]` if there was nothing to move
    or either file can't be read: this function moves, it doesn't diagnose.
    """

    spedito_percorso = Path(percorso or REGISTRO)
    documento_percorso = percorso_imparato(spedito_percorso)
    if not documento_percorso.is_file():
        return []
    spedite, errore = _voci_di_un_documento(spedito_percorso, quale="Registro degli adattatori")
    if errore is not None:
        return []
    per_id = _spedite_per_id(spedite)
    with _turno_di_scrittura(documento_percorso):
        try:
            documento = json.loads(documento_percorso.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(documento, dict) or not isinstance(documento.get("adapters"), list):
            return []
        restano: list[Any] = []
        messe: list[dict[str, Any]] = []
        schede: list[dict[str, Any]] = []
        for voce in documento["adapters"]:
            motivo = _motivo_del_superamento(voce, per_id) if isinstance(voce, dict) else None
            if motivo is None:
                restano.append(voce)
                continue
            messe.append({
                **voce,
                "messo_da_parte_il": quando or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "messo_da_parte_perche": motivo,
            })
            schede.append(_scheda_della_superata(voce, motivo))
        if not messe:
            return []
        documento["adapters"] = restano
        documento["adapters_messi_da_parte"] = [
            *(documento.get("adapters_messi_da_parte") or []), *messe,
        ]
        _scrivi_documento(documento_percorso, documento)
    return schede


def _scrivi_documento(documento_percorso: Path, documento: dict[str, Any]) -> None:
    """Replace the registry in one shot, with LF line endings.

    The registry is tracked in git with LF endings: rewriting it in CRLF
    would make the whole file show as changed, and the diff a person has to
    read before accepting a new schema would become unreadable.
    """

    documento_percorso.parent.mkdir(parents=True, exist_ok=True)
    descrittore, temporaneo = tempfile.mkstemp(
        prefix=documento_percorso.name + ".", suffix=".tmp", dir=str(documento_percorso.parent)
    )
    try:
        with os.fdopen(descrittore, "w", encoding="utf-8", newline="\n") as flusso:
            json.dump(documento, flusso, ensure_ascii=False, indent=2)
            flusso.write("\n")
        os.replace(temporaneo, documento_percorso)
    except BaseException:
        Path(temporaneo).unlink(missing_ok=True)
        raise
