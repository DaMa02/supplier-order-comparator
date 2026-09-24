#!/usr/bin/env python3
"""Writes user-confirmed schemas into the adapter registry.

Without this step a confirmed schema stays written only in that run's
manifest: the following week the same price list comes back unknown and the
user has to map it again through the guided UI. In unattended runs there is
nobody to copy an entry into ``references/adapters.json`` by hand.

Two rules shape everything else here:

- Only what the user approved enters the registry. The mapping is proposed —
  by the registry's deterministic scoring, or written by hand — and the user
  confirms it: an entry without ``user_confirmation.status == "CONFIRMED"``
  never enters, even when the mapping looks perfect.
- A rejection is never silent. Every entry that isn't learned comes back in
  the report with its reason, and if an entry that should have been learned
  is rejected, the process exits with status 2. A silent failure here would
  mean a supplier stays unknown next week with nobody knowing why.

Usage::

    python scripts/impara_adattatore.py --manifest <input_manifest.json> \\
        --adapters references\\adapters.json [--output <report.json>] [--prova]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The registry engine and the manifest validator live right next to this
# script. Mapping checks reuse the validator's own logic rather than a second
# copy: two divergent rule sets would mean a mapping accepted here and
# rejected there, or the reverse, with nothing to catch it.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import registro  # noqa: E402
from validate_input_manifest import incomplete_mapping  # noqa: E402


# The two states where the user mapped something the registry doesn't know
# yet. `SCHEMA_NOTO` has nothing to learn; `AMBIGUO` and
# `FILE_NON_PERTINENTE` don't even have a mapping to write.
STATI_DA_IMPARARE = ("SCHEMA_VARIATO", "NUOVO_FORNITORE")

CONFERMATO = "CONFIRMED"

# Who approved the schema. A constant, not a field, because this script only
# ever writes what the user confirmed; an automated confirmation would carry
# a different value here, visible in the registry.
CONFERMATO_DA = "utente"


class Rifiuto(Exception):
    """A user-confirmed entry that couldn't be learned.

    An exception rather than a return value because a rejection genuinely
    aborts processing that entry: continuing with a half-built adapter would
    mean writing a registry entry that describes nothing.
    """


def voci_del_manifest(manifest: Any) -> list[dict[str, Any]]:
    """The documents listed in the manifest, under either of its two key names.

    ``apply_preflight_decisions`` writes ``files``, the inspector writes
    ``profiles``; reading both avoids making learning depend on which of the
    two manifests was passed in.
    """

    if not isinstance(manifest, dict):
        raise ValueError("Il manifest deve essere un oggetto JSON")
    voci = manifest.get("files") or manifest.get("profiles") or []
    if not isinstance(voci, list):
        raise ValueError("Il manifest non ha un elenco di file")
    return [voce for voce in voci if isinstance(voce, dict)]


def etichetta(voce: dict[str, Any]) -> str:
    """This document's name in the report."""

    return str(voce.get("file_name") or Path(str(voce.get("path") or "")).name or "file-senza-nome")


def motivo_per_saltare(voce: dict[str, Any]) -> str | None:
    """Why this entry isn't learned, for the two cases that aren't errors.

    Covers the two normal cases: a schema the registry already knows has
    nothing to teach, and a schema the user hasn't confirmed must not enter —
    the mapping is proposed, the user approves it. Both are still reported
    with their reason: nobody reads logs on the user's behalf.
    """

    decisione = voce.get("ai_preflight") or {}
    stato = decisione.get("state")
    if stato not in STATI_DA_IMPARARE:
        return (f"lo stato «{stato}» non chiede di imparare niente: entrano nel registro solo "
                f"gli schemi che l'AI ha proposto come {' o '.join(STATI_DA_IMPARARE)}")
    conferma = (voce.get("user_confirmation") or {}).get("status")
    if conferma != CONFERMATO:
        return (f"l'utente non ha confermato lo schema (user_confirmation.status = «{conferma}»): "
                f"l'AI propone, l'utente approva, e ciò che non è stato approvato non entra")
    return None


def foglio_dichiarato(profilo: dict[str, Any], mappatura: dict[str, Any]) -> dict[str, Any]:
    """The profile's sheet the confirmed mapping says to work on.

    Follows the same selection rule the reader uses (``selected_sheet`` in
    ``prepare_manifest_sources``), exact name included: learning the
    fingerprint from a sheet the reader wouldn't open would produce an
    adapter that's recognized but then unreadable.
    """

    fogli = registro.fogli_del_profilo(profilo)
    if not fogli:
        raise Rifiuto("il profilo nel manifest non contiene nessun foglio")
    scelto = mappatura.get("sheet")
    if scelto in (None, "", "FIRST"):
        return fogli[0]
    if isinstance(scelto, int) and not isinstance(scelto, bool):
        if 0 <= scelto < len(fogli):
            return fogli[scelto]
        raise Rifiuto(f"la mappatura dichiara il foglio numero {scelto}, il documento ne ha {len(fogli)}")
    for foglio in fogli:
        if foglio.get("name") == str(scelto):
            return foglio
    nomi = ", ".join(f"«{foglio.get('name')}»" for foglio in fogli) or "nessuno"
    raise Rifiuto(f"il foglio «{scelto}» dichiarato dalla mappatura non è nel documento: ci sono {nomi}")


def riga_dichiarata(profilo: dict[str, Any], mappatura: dict[str, Any]) -> int:
    """The row the confirmed mapping declares as the header row.

    A missing value defaults to 1 for a CSV and 0 for a spreadsheet — the
    same fallbacks the two readers in ``prepare_manifest_sources`` already
    use. Deducing a different default here would learn a fingerprint that
    describes a row the reader will never actually look at.
    """

    grezzo = mappatura.get("header_row")
    if grezzo in (None, ""):
        return 1 if not isinstance(profilo.get("sheets"), list) else 0
    try:
        return int(grezzo)
    except (TypeError, ValueError):
        raise Rifiuto(f"la mappatura dichiara una riga di intestazione illeggibile: {grezzo!r}") from None


def intestazioni_osservate(foglio: dict[str, Any], riga: int) -> list[Any]:
    """The header row's values, as stored in the manifest's profile.

    The file itself is never reopened: the manifest is the auditable
    document, the one the user actually saw when confirming. Re-reading the
    file would mean learning from content nobody approved.
    """

    if riga < 1:
        raise Rifiuto(
            "la mappatura dichiara che il documento non ha una riga di intestazione "
            "(header_row 0): un'impronta per forma delle colonne si scrive a mano nel "
            "registro, non si impara da un profilo"
        )
    for candidato in registro.righe_di_intestazione(foglio):
        if candidato["row"] == riga:
            return list(candidato["values"])
    raise Rifiuto(
        f"la riga di intestazione {riga} non è fra quelle che il profilo riporta: "
        f"il manifest non la contiene e il documento non si riapre"
    )


def obbligatorie_della_mappatura(mappatura: dict[str, Any], osservate: list[str],
                                 riga: int) -> list[str]:
    """The headers the reader can't work without.

    These are the columns the confirmed mapping actually names: if one of
    them disappears, the document stops being readable with this adapter,
    which is exactly the kind of change that must go back through guided
    mapping rather than pass silently. Columns declared by number are excluded:
    they have no name to look up among the headers.
    """

    colonne = mappatura.get("columns") or mappatura.get("field_mapping") or {}
    per_nome = [valore for valore in colonne.values()
                if valore not in (None, "") and not isinstance(valore, (int, bool))]
    richieste = sorted({registro.normalizza(valore) for valore in per_nome} - {""})
    if not richieste:
        raise Rifiuto(
            "la mappatura non dichiara nessuna colonna per nome: un'impronta per "
            "intestazioni ha bisogno di almeno un nome da ritrovare nel documento"
        )
    mancanti = sorted(set(richieste) - set(osservate))
    if mancanti:
        raise Rifiuto(
            f"la mappatura dichiara colonne che non sono nella riga di intestazione {riga}: "
            + ", ".join(f"«{nome}»" for nome in mancanti)
        )
    return richieste


def verifica_intestazione_ordine(mappatura: dict[str, Any], valori: list[Any], riga: int) -> None:
    """The confirmed order-column state (named header or blank) must match the profile."""

    dichiarata = str(mappatura.get("order_column") or "").strip().upper()
    if not dichiarata:
        return
    if not re.fullmatch(r"[A-Z]{1,3}", dichiarata):
        raise Rifiuto(f"la colonna ordine «{dichiarata}» non è una colonna Excel valida")
    indice = 0
    for lettera in dichiarata:
        indice = indice * 26 + ord(lettera) - ord("A") + 1
    osservata = valori[indice - 1] if indice <= len(valori) else None
    attesa = str(mappatura.get("order_header_expected") or "").strip()
    vuota = mappatura.get("order_header_blank_confirmed") is True
    if attesa and vuota:
        raise Rifiuto("la colonna ordine non può avere insieme un'intestazione attesa e una conferma di cella vuota")
    if attesa and " ".join(str(osservata or "").replace(" ", " ").split()).casefold() != " ".join(attesa.split()).casefold():
        raise Rifiuto(
            f"la cella {dichiarata}{riga} contiene «{osservata}» invece dell'intestazione confermata «{attesa}»"
        )
    if vuota and str(osservata or "").strip():
        raise Rifiuto(
            f"la cella {dichiarata}{riga} contiene «{osservata}» ma la mappatura la conferma vuota"
        )


def identificativo_dell_adattatore(decisione: dict[str, Any]) -> str:
    """The id the adapter is written into the registry under.

    A schema change carries the id of the adapter it's changing, which is
    also how the previous version isn't lost. A new supplier has no id yet:
    one is built from its `supplier_id`, the key the rest of the program
    already uses to refer to it.
    """

    dichiarato = str(decisione.get("adapter_id") or "").strip()
    if dichiarato:
        return dichiarato
    fornitore = str(decisione.get("supplier_id") or "").strip()
    if fornitore:
        # Not `registro.normalizza`: that collapses words together for
        # header comparison. Here the result is a name a person reads in the
        # registry next to other adapter ids, so words stay separated by
        # underscores.
        pulito = re.sub(r"[^a-z0-9]+", "_", fornitore.casefold()).strip("_")
        return f"{pulito or fornitore}_v1"
    raise Rifiuto(
        "non si sa con quale identificativo scrivere l'adattatore: la decisione non "
        "dichiara né «adapter_id» né «supplier_id»"
    )


# `identificativo_da_scrivere` lives in `registro`, not here: guided mapping
# isn't the only path that writes to the registry — moving the order column
# from the page writes to it too — so the id-assignment rule has to be
# shared rather than duplicated per caller.
identificativo_da_scrivere = registro.identificativo_da_scrivere


def _colonne_per_lettera(mappatura: dict[str, Any],
                         posizioni: dict[str, int] | None) -> dict[str, str]:
    """Where each field of the confirmed mapping sits, expressed as a letter.

    `column_map` talks in positions, not names: it's the only form that
    works for reading a price list with no header row. The confirmed mapping
    declares columns sometimes by number, sometimes by name, and a name is
    resolved against the headers measured on this document.
    """

    colonne = mappatura.get("columns")
    if not isinstance(colonne, dict):
        return {}
    # Deferred import: this script also runs standalone, and the column
    # letter is only needed by callers that reach this point.
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    posizioni = posizioni or {}
    per_lettera: dict[str, str] = {}
    for campo, dichiarata in colonne.items():
        nome = str(campo or "").strip()
        if not nome or dichiarata in (None, ""):
            continue
        if isinstance(dichiarata, bool):
            continue
        indice: int | None = None
        if isinstance(dichiarata, (int, float)) and float(dichiarata).is_integer():
            indice = int(dichiarata)
        else:
            testo = str(dichiarata).strip()
            if re.fullmatch(r"\d+", testo):
                indice = int(testo)
            else:
                indice = posizioni.get(registro.normalizza(testo))
        if indice and indice >= 1:
            per_lettera[nome] = get_column_letter(indice)
    return per_lettera


def voce_da_scrivere(voce: dict[str, Any], decisione: dict[str, Any], mappatura: dict[str, Any],
                     precedente: dict[str, Any], identificativo: str, foglio: dict[str, Any],
                     riga: int, richieste: list[str], osservate: list[str],
                     quando: str, posizioni: dict[str, int] | None = None) -> dict[str, Any]:
    """The adapter as it will be written, starting from the existing entry.

    Starting from the existing entry is deliberate: an adapter also carries
    rules that don't live in the mapping — row codes that flag a line as
    non-purchasable, for one supplier — and rewriting from scratch would
    silently drop them. A confirmed schema change updates the schema, not
    the supplier's commercial conventions.
    """

    ruolo = str(decisione.get("role") or "")
    scartate = {"schema_version", "previous_versions"} | ({"supplier_id"} if ruolo == "master" else set())
    base = {chiave: valore for chiave, valore in precedente.items() if chiave not in scartate}

    tipi = list(base.get("file_types") or [])
    suffisso = str(voce.get("declared_suffix") or Path(etichetta(voce)).suffix).casefold()
    if suffisso and suffisso not in tipi:
        tipi.append(suffisso)

    # The measured sheet is stored in `learned_from` because the mapping
    # itself can just say "FIRST"; without it there'd be no way to trace
    # back where the fingerprint actually came from.
    provenienza = {"file_name": etichetta(voce), "sha256": voce.get("sha256")}
    if foglio.get("name"):
        provenienza["sheet"] = foglio.get("name")

    nuova: dict[str, Any] = {"id": identificativo, "kind": "master" if ruolo == "master" else "supplier"}
    if ruolo != "master":
        nuova["supplier_id"] = decisione.get("supplier_id")
    nuova["display_name"] = str(decisione.get("display_name") or base.get("display_name")
                                or decisione.get("supplier_id") or identificativo)
    if tipi:
        nuova["file_types"] = tipi
    nuova["header_signature"] = {
        "kind": "headers",
        "sheet": mappatura.get("sheet"),
        "header_row": riga,
        "data_start_row": mappatura.get("data_start_row"),
        "note": (f"Impronta imparata dal profilo di «{etichetta(voce)}» e confermata "
                 f"dall'utente. Le obbligatorie sono le colonne che la mappatura usa "
                 f"davvero: sono quelle senza cui il lettore non può lavorare."),
        # Recorded positions aren't optional extra data: a learned adapter
        # reads by position whenever the mapping declares a column by
        # number (needed when a header text repeats) and writes the order
        # quantity into a column given by letter. An inserted column ahead
        # of the data leaves the set of header names unchanged while
        # shifting every position after it — without this check a learned
        # adapter had no defense against that shift at all.
        "columns_note": ("Dove stava ogni intestazione nel documento da cui questo schema è "
                         "stato imparato. Una colonna che si sposta declassa a SCHEMA_VARIATO: "
                         "la mappatura indica colonne anche per numero e per lettera, e quelle "
                         "seguono la posizione, non il nome."),
        "columns": dict(posizioni or {}),
        "required": richieste,
        "known": osservate,
    }
    # The data-start rule is stored in the fingerprint alongside the row
    # number, and takes priority over it on every read: `data_start_row`
    # alone is just this week's number, and a preceding promotional block
    # can change length. Whoever reads the adapter back needs to see that
    # the number isn't the rule — it's what the rule resolved to on the day
    # it was confirmed.
    marcatore = mappatura.get("data_start_marker")
    if marcatore not in (None, "", {}):
        nuova["header_signature"]["data_start_marker"] = marcatore
        nuova["header_signature"]["data_start_note"] = (
            "I dati cominciano dove dice «data_start_marker», ricalcolato a ogni lettura: "
            "«data_start_row» è la riga a cui quel marcatore si è risolto quando l'utente "
            "ha confermato lo schema, e serve a chi scrive la copia dell'ordine."
        )
    nuova["field_mapping"] = mappatura
    # `column_map` says WHERE columns sit, and some readers rely on it
    # exclusively — a free-goods-threshold reader, for one supplier whose
    # price list has no header row to resolve a name against. Writing only
    # `field_mapping` on a confirmed change and leaving `column_map` stale
    # would make two parts of the program read two different columns of the
    # same price list, with neither one reporting it.
    #
    # Only what the confirmed mapping declares gets updated here; the rest
    # is kept, since `column_map` also carries entries that aren't part of
    # any mapped field — for one supplier, a group label and a free-item
    # name — and dropping them would break whatever reads them.
    aggiornate = _colonne_per_lettera(mappatura, posizioni)
    if aggiornate:
        nuova["column_map"] = {**(base.get("column_map") or {}), **aggiornate}
    # Commercial conditions enter the registry the same way columns do, and
    # for the same reason: without this, a supplier learned from the page
    # would read and compile fine while its promotions stayed unread by
    # everyone, since the declaration names `column_map` keys, which the
    # line above just updated — conditions track price columns instead of
    # lagging behind them.
    #
    # If the confirmed mapping doesn't declare it, the previous declaration
    # stays as-is (`setdefault` further below): a schema change moves where
    # columns are, not whether that supplier runs promotions.
    condizioni = mappatura.get("commercial_conditions")
    if ruolo == "supplier" and isinstance(condizioni, dict) and condizioni:
        nuova["commercial_conditions"] = condizioni
    if (
        ruolo == "supplier"
        and mappatura.get("order_column")
        and (
            base.get("order_write")
            or mappatura.get("order_header_expected")
            or mappatura.get("order_header_blank_confirmed") is True
        )
    ):
        # The order column was chosen in the same preview as the reading
        # columns. For a new schema this declaration makes it writable; for
        # a schema change it also updates a header the supplier renamed.
        # Any special write procedure a previous version had keeps its own
        # mode and guards.
        scrittura = dict(base.get("order_write") or {})
        scrittura["from_field_mapping"] = True
        scrittura["order_column"] = str(mappatura["order_column"]).strip().upper()
        attesa = str(mappatura.get("order_header_expected") or "").strip()
        if attesa:
            scrittura["expected_header"] = attesa
            scrittura.pop("allow_blank_header_if_confirmed", None)
        elif mappatura.get("order_header_blank_confirmed") is True:
            scrittura.pop("expected_header", None)
            scrittura["allow_blank_header_if_confirmed"] = True
        if not scrittura.get("required_columns"):
            colonne_mappate = set((mappatura.get("columns") or {}).keys())
            scrittura["required_columns"] = sorted(
                colonne_mappate
                & {"ean", "supplier_code", "description", "pieces_per_carton",
                   "order_multiplier", "unit_price_net", "unit_price_pre_discount"}
            )
        nuova["order_write"] = scrittura
    nuova["learned_at"] = quando
    nuova["learned_from"] = provenienza
    nuova["confirmed_by"] = CONFERMATO_DA
    # Everything else from the previous entry is kept as-is: `setdefault`
    # never overwrites anything just decided above.
    for chiave, valore in base.items():
        nuova.setdefault(chiave, valore)
    return nuova


def registro_di_lavoro(percorso: Path, cartella: Path) -> Path:
    """A registry copy to trial-write to before writing for real.

    Serves one purpose, and it's the reason this script exists at all: an
    adapter that, once written, still can't recognize the document it was
    learned from is worse than nothing — next week the supplier is unknown
    again and the registry carries a dead entry nobody understands. Writing
    to a copy and reading it back with the same engine that will decide for
    real is the only way to catch that beforehand.
    """

    copia = cartella / "adapters.json"
    if percorso.exists():
        # Binary copy: the registry is tracked in git with LF line endings,
        # and a copy rewritten with CRLF would be a different document.
        copia.write_bytes(percorso.read_bytes())
    # The locally learned registry is copied too, or the trial run would
    # test against a registry that isn't the real one: another learned
    # adapter could win recognition instead of the one just written, and
    # this check wouldn't catch it.
    imparato = registro.percorso_imparato(percorso)
    if imparato.exists():
        registro.percorso_imparato(copia).write_bytes(imparato.read_bytes())
    return copia


def impara(voce: dict[str, Any], copia: Path, adapters: Path, presi: dict[str, str],
           prova: bool, quando: str) -> dict[str, Any]:
    """Learns one manifest entry, or reports why it couldn't."""

    decisione = voce.get("ai_preflight") or {}
    ruolo = str(decisione.get("role") or "")
    if ruolo not in ("master", "supplier"):
        raise Rifiuto(f"il ruolo «{ruolo or None}» non permette di scrivere un adattatore: "
                      f"serve «master» oppure «supplier»")

    profilo = voce.get("details")
    if not isinstance(profilo, dict):
        raise Rifiuto("la voce del manifest non porta il profilo del documento («details»): "
                      "l'impronta si calcola da lì e il documento non si riapre")

    if ruolo == "supplier" and not str(decisione.get("supplier_id") or "").strip():
        raise Rifiuto("la decisione non dichiara il fornitore («supplier_id»): un adattatore "
                      "senza fornitore non lo cercherebbe nessuno")

    mappatura = decisione.get("field_mapping")
    mancanti = incomplete_mapping(mappatura, ruolo, Path(str(voce.get("path") or etichetta(voce))))
    if mancanti:
        raise Rifiuto("la mappatura confermata è incompleta, mancano: " + "; ".join(mancanti))

    dichiarato = identificativo_dell_adattatore(decisione)
    identificativo = identificativo_da_scrivere(dichiarato, copia)
    if identificativo in presi:
        raise Rifiuto(f"l'identificativo «{identificativo}» è già stato imparato in questa "
                      f"esecuzione da «{presi[identificativo]}»: due schemi diversi con lo stesso "
                      f"nome si sovrascriverebbero a vicenda")

    foglio = foglio_dichiarato(profilo, mappatura)
    riga = riga_dichiarata(profilo, mappatura)
    valori = intestazioni_osservate(foglio, riga)
    verifica_intestazione_ordine(mappatura, valori, riga)
    impronta = registro.impronta(foglio.get("name"), riga, riga + 1, valori)
    richieste = obbligatorie_della_mappatura(mappatura, impronta["headers"], riga)

    posizioni = registro.posizioni_delle_intestazioni(valori)
    # The first time an adapter is learned alongside a shipped one, its new
    # id isn't in the registry yet: the base is the shipped entry, or the
    # learned entry would start bare — no commercial conditions, no aliases,
    # none of the supplier's row rules that the mapping itself doesn't name.
    # After that first time, the base is the local entry, which already
    # carries those over.
    precedente = registro.adattatore(identificativo, copia)
    if not precedente and identificativo != dichiarato:
        spedito = registro.adattatore(dichiarato, copia)
        # `scrivi_adattatore`'s own guard against overwriting one supplier's
        # adapter with another's only fires on a matching id. Since the new
        # id here never collides with anything, without this check a
        # supplier could silently inherit another one's rules; starting
        # from the shipped entry keeps its commercial conditions and row
        # codes intact.
        atteso = str(spedito.get("supplier_id") or "").strip()
        chiesto = str(decisione.get("supplier_id") or "").strip()
        if spedito and ruolo == "supplier" and atteso and atteso != chiesto:
            raise Rifiuto(
                f"l'adattatore «{dichiarato}» è del fornitore «{atteso}»: non lo si può "
                f"imparare per «{chiesto}», nemmeno accanto — la voce nuova nascerebbe con le "
                f"regole di «{atteso}» addosso"
            )
        precedente = spedito
    nuova = voce_da_scrivere(voce, decisione, mappatura, precedente, identificativo,
                             foglio, riga, richieste, impronta["headers"], quando, posizioni)
    if identificativo != dichiarato:
        # Recorded in the entry itself, not just in the id's naming
        # convention: whoever reads it back should be able to tell where it
        # came from without knowing the suffix scheme.
        nuova["derivato_da"] = dichiarato

    try:
        esito = registro.scrivi_adattatore(nuova, copia)
    except ValueError as errore:
        raise Rifiuto(f"il registro non ha accettato la voce: {errore}") from errore

    # The check that actually matters: with the registry just written, the
    # same engine that will decide next week must recognize this exact
    # document with this exact adapter. If another adapter wins, or any
    # check fails, what was just written is useless.
    riletto = registro.riconosci(profilo, copia)
    # The shipped adapter is allowed to reclaim the document, if it reads
    # it cleanly: that's a good outcome, not an error — it means there was
    # nothing to learn. It's reported, and nothing is written.
    if riletto["state"] == "SCHEMA_NOTO" and riletto["adapter_id"] == dichiarato != identificativo:
        raise Rifiuto(
            f"con questo documento l'adattatore spedito «{dichiarato}» funziona: il programma lo "
            f"legge già senza bisogno di imparare niente, e una voce in più nel registro "
            f"sarebbe solo una cosa da capire fra un mese"
        )
    if riletto["state"] != "SCHEMA_NOTO" or riletto["adapter_id"] != identificativo:
        raise Rifiuto(
            f"con l'adattatore appena scritto il documento risulta {riletto['state']} "
            f"«{riletto['adapter_id']}» invece di SCHEMA_NOTO «{identificativo}»: "
            + (riletto["evidence"][-1] if riletto["evidence"] else "nessuna spiegazione")
        )

    if not prova:
        esito = registro.scrivi_adattatore(nuova, adapters)
    presi[identificativo] = etichetta(voce)
    return {
        "file": etichetta(voce),
        "adapter_id": identificativo,
        "supplier_id": nuova.get("supplier_id"),
        "schema_version": esito["schema_version"],
        "created": esito["created"],
        "previous_versions": esito["previous_versions"],
        "sheet": foglio.get("name"),
        "header_row": riga,
        "required": richieste,
        "hash": impronta["hash"],
        "learned_from": nuova["learned_from"],
        "scritto": not prova,
    }


def scrivi_rapporto(percorso: Path, rapporto: dict[str, Any]) -> None:
    """Saves the report with LF line endings, like the rest of the project."""

    percorso.parent.mkdir(parents=True, exist_ok=True)
    with percorso.open("w", encoding="utf-8", newline="\n") as flusso:
        json.dump(rapporto, flusso, ensure_ascii=False, indent=2)
        flusso.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="Il manifest prodotto da apply_preflight_decisions.py")
    # No default value on purpose: this is the file the script overwrites,
    # and a default would let it be modified by someone who didn't expect it.
    parser.add_argument("--adapters", type=Path, required=True,
                        help="Il registro degli adattatori da aggiornare")
    parser.add_argument("--output", type=Path, help="Dove salvare il rapporto JSON")
    parser.add_argument("--prova", action="store_true",
                        help="Calcola e stampa tutto senza scrivere niente nel registro")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    quando = datetime.now(tz=timezone.utc).isoformat()

    imparati: list[dict[str, Any]] = []
    saltati: list[dict[str, Any]] = []
    presi: dict[str, str] = {}
    rifiutate = 0

    try:
        voci = voci_del_manifest(json.loads(args.manifest.read_text(encoding="utf-8")))
    except (OSError, ValueError) as errore:
        # An unreadable manifest is a failure like any other: it exits with
        # its own reason and status 2, instead of a traceback the report's
        # reader would never see.
        voci = []
        saltati.append({"file": args.manifest.name,
                        "motivo": f"Rifiutato: il manifest non si legge: {errore}"})
        rifiutate = 1

    with tempfile.TemporaryDirectory(prefix="registro_di_prova_") as temporanea:
        copia = registro_di_lavoro(args.adapters, Path(temporanea))
        for voce in voci:
            nome = etichetta(voce)
            motivo = motivo_per_saltare(voce)
            if motivo:
                saltati.append({"file": nome, "motivo": motivo})
                continue
            try:
                imparati.append(impara(voce, copia, args.adapters, presi, args.prova, quando))
            except Rifiuto as rifiuto:
                # The "Rifiutato" prefix marks the distinction that decides
                # the exit status: a skipped entry is a normal case, a
                # rejected one means that supplier stays unknown next week.
                saltati.append({"file": nome, "motivo": f"Rifiutato: {rifiuto}"})
                rifiutate += 1

    rapporto = {"imparati": imparati, "saltati": saltati, "registro": str(args.adapters.resolve())}
    if args.output:
        scrivi_rapporto(args.output, rapporto)
    print(json.dumps(rapporto, ensure_ascii=False, indent=2))
    return 2 if rifiutate else 0


if __name__ == "__main__":
    raise SystemExit(main())
