"""Connect supplier source annotations to the conservative promotion engine."""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from offerta import offer_is_available, offer_supplier_id
import registro
import xls_reader
from promotions import (
    CERTAINTY_REVIEW,
    KIND_AMBIGUOUS,
    decorate_review_data,
    detect_numeric_discount,
    detect_promotions,
    detect_threshold_gift,
    looks_like_reward,
    looks_like_threshold_heading,
    make_promotion,
)


# What counts as a threshold heading is decided by the engine, not this
# bridge: recognizing the wrong subset of heading variants can make an entire
# gift-threshold condition disappear from a supplier's price list, order rows
# included, on a single missed word form.
e_intestazione_di_soglia = looks_like_threshold_heading
e_riga_premio = looks_like_reward


# ---------------------------------------------------------------------------
# Adapter registry vocabulary: where a supplier writes its commercial terms
# ---------------------------------------------------------------------------
#
# The bridge asks the registry a general question — "which suppliers declare
# where they keep their commercial conditions?" — and `references/adapters.json`
# holds the answer, not this file. That keeps the reader from being hardwired
# to a single supplier, with no way to declare another one's conditions
# without a code change.
#
# Checked column by column against real price lists: only `larice` has
# threshold headings and reward rows (13 of each). The other suppliers show
# no such structure, at most an occasional promotional word inside a product
# description. Declaring conditions for those would mean inventing offers
# that don't exist, which is worse than not reading them.

# The roles a condition reader knows how to use, and the word the operator
# knows them by: the label is what says which one is missing without naming a
# spreadsheet column. Order matches how they appear in the message.
_RUOLI_DEL_LETTORE = {
    "text": "le descrizioni",
    "reward": "il nome dell'articolo in omaggio",
    "ean": "il codice a barre",
    "row_code": "i codici delle righe",
}

# The two layouts real price lists use for a commercial condition.
#
# `blocchi` (blocks): the condition spans several rows — the heading with the
# quantity to buy, then the eligible items, then the reward row.
#
# `riga` (row): a single row carries the whole condition in one column. No
# supplier currently declares this layout, but without it a supplier whose
# price list writes conditions this way could never be turned on with just a
# registry entry.
LAYOUT_BLOCCHI = "blocchi"
LAYOUT_RIGA = "riga"

# What each layout requires. A block-form threshold needs all four columns:
# without the reward name the condition still gets reconstructed, but comes
# out "to be checked" instead of confirmed. A single-row condition needs only
# the column it's written in.
_COLONNE_RICHIESTE = {
    LAYOUT_BLOCCHI: ("text", "reward", "ean", "row_code"),
    LAYOUT_RIGA: ("text",),
}


def colonne_richieste_dal_layout(forma: Any) -> tuple[str, ...] | None:
    """Return the columns a layout requires, or `None` if the layout is unknown.

    Used by whoever collects the declaration, before it reaches this module:
    the guided mapping step can reject a layout missing its columns
    immediately, instead of letting the registry save a declaration that
    later produces "non so più dove il listino tiene …" (the declared columns
    can't be found) on every run.
    """

    return _COLONNE_RICHIESTE.get(str(forma or "").strip().casefold())


def nomi_dei_ruoli() -> dict[str, str]:
    """Return the reader's roles keyed by the label the operator sees, for whoever prompts for them."""

    return dict(_RUOLI_DEL_LETTORE)

# Beyond this distance from the heading a block is no longer credible: the
# following rows belong to something else. A defense against a malformed
# price list, not a commercial rule; the registry can override it per adapter
# with `max_block_rows`.
_RIGHE_MASSIME_DI_UN_BLOCCO = 500

# The first eight bytes of an Excel 97-2003 document. Format is decided by
# these bytes, not the extension; `app/xls_reader.py` reads that format.
_FIRMA_XLS = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _codici_non_ordinabili(adattatore: dict[str, Any]) -> set[str]:
    """Return the codes the registry declares non-purchasable for that price list."""

    codici = registro.codici_di_riga(adattatore)
    return {
        str(chiave).strip().upper()
        for chiave, dichiarato in (codici.get("codes") or {}).items()
        if isinstance(dichiarato, dict) and dichiarato.get("orderable") is False
    }


def _fornitori_con_promozione_inclusa() -> set[str]:
    """Return the suppliers who declare their extra goods as already included in the list price.

    A "buy N get M" deal that a supplier's stated price already reflects is
    not a discount to apply on top; it's a term of the commercial
    relationship with that supplier, declared by the registry rather than
    hardcoded, so a new supplier can adopt the same arrangement without a
    code change.

    Looked up by `supplier_id`, not by adapter name: a supplier can have more
    than one adapter (one per file format), and it's enough that one of them
    declares it.
    """

    fornitori: set[str] = set()
    for voce in registro.adattatori():
        regole = voce.get("commercial_rules")
        if not isinstance(regole, dict) or regole.get("promotion_included_in_product") is not True:
            continue
        fornitore = str(voce.get("supplier_id") or "").strip().casefold()
        if fornitore:
            fornitori.add(fornitore)
    return fornitori


def fornitori_con_condizioni_dichiarate() -> dict[str, list[dict[str, Any]]]:
    """Return, per supplier, the adapters that declare where its conditions live.

    A supplier that doesn't declare this is not read and produces no warning:
    that's the difference between "has no commercial conditions" and "can't
    read them", and conflating the two would fill the page with rows asking
    for no action.

    A supplier can have more than one adapter, one per document format; all
    are kept, and the actual document decides which one to use.
    """

    dichiarati: dict[str, list[dict[str, Any]]] = {}
    for voce in registro.adattatori():
        if not isinstance(voce.get("commercial_conditions"), dict):
            continue
        fornitore = str(voce.get("supplier_id") or "").strip().casefold()
        if fornitore:
            dichiarati.setdefault(fornitore, []).append(voce)
    return dichiarati


def _adattatore_per_il_documento(
    voci: list[dict[str, Any]], path: Path | None, adapter_id: str | None = None
) -> dict[str, Any]:
    """Among a supplier's adapters, return the one matching the document at hand.

    A supplier can have two adapters for the same file extension (e.g. an old
    layout without headers and a new one with a header row): the extension
    alone can't disambiguate them. Which adapter recognized the document is
    recorded by the review (`adapterId`), matched through
    `registro.adattatore_base` because a learned adapter carries the same id
    with `__locale` appended.

    If the id is present but not among the candidates, and there is more than
    one candidate, this doesn't guess: no condition is safer than reading the
    condition of a different document.
    """

    if adapter_id:
        base = registro.adattatore_base(adapter_id)
        for voce in voci:
            if str(voce.get("id") or "") in {adapter_id, base}:
                return voce
        if len(voci) > 1:
            return {}
    if path is not None:
        suffisso = path.suffix.casefold()
        for voce in voci:
            tipi = [str(tipo).casefold() for tipo in (voce.get("file_types") or [])]
            if suffisso in tipi:
                return voce
    return voci[0] if voci else {}


def _indice_di_colonna(dichiarata: Any) -> int | None:
    """Return the registry's declared column as a 1-based index.

    Some price lists have no header row, so a column can only be given by
    letter ("R") or by number (18). A digit string is not accepted as a
    number, matching the rule the registry itself applies: elsewhere a digit
    string is a header name, and reading it as "column 9" here would silently
    pick the wrong column.
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


def _dove_sta_la_colonna(adattatore: dict[str, Any], campo: Any) -> int | None:
    """Return where the adapter says the named column is.

    Reads the same declarations the price reader follows — `column_map` for
    adapters that give columns by letter, the header signature's positions for
    those that give them by header name. This keeps prices and conditions
    pointed at the same columns: when the operator confirms a schema change,
    the registry rewrites those declarations, so conditions track prices
    instead of silently falling behind.
    """

    nome = str(campo or "").strip()
    if not nome:
        return None
    mappa = adattatore.get("column_map")
    if isinstance(mappa, dict) and nome in mappa:
        return _indice_di_colonna(mappa.get(nome))
    firma = adattatore.get("header_signature")
    colonne = firma.get("columns") if isinstance(firma, dict) else None
    if isinstance(colonne, dict):
        cercato = registro.normalizza(nome)
        for intestazione, posizione in colonne.items():
            if registro.normalizza(intestazione) == cercato:
                return _indice_di_colonna(posizione)
    return None


def _colonne_delle_condizioni(
    adattatore: dict[str, Any], dichiarazione: dict[str, Any]
) -> tuple[str, dict[str, int], list[str]]:
    """Return the layout, the resolved columns, and the ones still missing.

    Column positions are read from the registry rather than hardcoded, so
    that when a supplier moves a column — or the operator confirms a schema
    change and the registry learns the new mapping — the reader follows it
    instead of silently reading the old column and dropping gift thresholds
    from the summary.

    A column the registry doesn't declare is never guessed: it's returned in
    the missing-columns list instead, so the caller can report it.
    """

    forma = str(dichiarazione.get("layout") or "").strip().casefold()
    campi = dichiarazione.get("fields")
    campi = campi if isinstance(campi, dict) else {}
    if forma not in _COLONNE_RICHIESTE:
        return forma, {}, ["in che modo scrive le sue condizioni"]

    colonne: dict[str, int] = {}
    mancanti: list[str] = []
    for ruolo, nome_per_l_utente in _RUOLI_DEL_LETTORE.items():
        indice = _dove_sta_la_colonna(adattatore, campi.get(ruolo))
        if indice is not None:
            colonne[ruolo] = indice
        elif ruolo in _COLONNE_RICHIESTE[forma]:
            mancanti.append(nome_per_l_utente)
    return forma, colonne, mancanti


def _firma_del_registro() -> int:
    """Return when the registry file was last modified.

    Part of the read cache key: columns are resolved from the registry, so if
    the operator confirms a schema change while the price list file itself is
    unchanged, the cache must still invalidate — otherwise it would keep
    serving blocks read with the old mapping until restart.
    """

    try:
        return registro.REGISTRO.stat().st_mtime_ns
    except OSError:
        return -1


def _cella(row: tuple[Any, ...], colonna: int | None) -> str:
    """Return a cell's text, indexed the way the registry indexes columns (1 = A).

    The `.xls` reader hands back each cell as a `(value, bold)` pair; without
    unwrapping it, a condition's text would become the literal string
    `"('ACQUISTANDO 5 CT TRA', False)"`, which no detector recognizes.
    """

    if colonna is None or len(row) < colonna:
        return ""
    valore = row[colonna - 1]
    if isinstance(valore, tuple) and len(valore) == 2 and isinstance(valore[1], bool):
        valore = valore[0]
    return str(valore or "").strip()


def _elenco_in_italiano(voci: list[str]) -> str:
    """Join items as a natural-language list ("the descriptions and the barcode"), not a Python repr."""

    if len(voci) < 2:
        return voci[0] if voci else ""
    return ", ".join(voci[:-1]) + " e " + voci[-1]


def _errore_di_lettura(fornitore: str, motivo: str) -> dict[str, str]:
    """Build the message the operator sees when a supplier's conditions can't be read.

    The page turns this into an "offers not read" warning and adds on its own
    that prices and the comparison remain valid; this function states only
    what happened, in one line, without naming columns, files or functions.

    The supplier's display name comes from the registry, so a learned
    supplier shows its proper name instead of its internal id.
    """

    nome = registro.nome_del_fornitore(fornitore)
    return {
        "supplier": fornitore,
        "supplierName": nome,
        "message": f"Non riesco a leggere le condizioni commerciali di {nome}: {motivo}.",
    }


def _errore_di_documento(fornitore: str, path: Path, motivo: str) -> dict[str, str]:
    """Build the message for a document that exists but couldn't be read fully."""

    nome = registro.nome_del_fornitore(fornitore)
    return {
        "supplier": fornitore,
        "supplierName": nome,
        "message": (
            f"Le condizioni commerciali del listino {nome} «{path.name}» non sono "
            f"state lette: {motivo}"
        ),
    }


def _source_paths(review: dict[str, Any]) -> tuple[dict[str, Path], set[str], dict[str, str]]:
    """Return the price lists the review declares, and the ones no longer reachable.

    A supplier the review declares whose document can't be resolved is not
    the same as a supplier absent from this run: the former had commercial
    conditions and lost them, and that's worth reporting. Without this
    distinction, "the price list is gone" and "this supplier makes no offers"
    would both surface as silence.

    A supplier the review doesn't declare at all is excluded: warning about
    missing thresholds for suppliers outside the comparison would fire on
    every recompute, and a warning that's always present stops being read.
    """

    result: dict[str, Path] = {}
    senza_documento: set[str] = set()
    # Which adapter recognized that document: needed when a supplier has more
    # than one adapter for the same format, where the extension alone can't
    # say which declaration applies.
    adattatori: dict[str, str] = {}
    for item in review.get("files") or []:
        supplier = str(item.get("supplierId") or "").casefold()
        if not supplier:
            continue
        raw = item.get("sourcePath") or item.get("originalPath") or item.get("path")
        path = Path(str(raw)).resolve() if raw else None
        if path is not None and path.is_file():
            result[supplier] = path
            riconosciuto = str(item.get("adapterId") or item.get("adapter_id") or "").strip()
            if riconosciuto:
                adattatori[supplier] = riconosciuto
        else:
            senza_documento.add(supplier)
    # A supplier can have more than one entry: if at least one document
    # resolves, nothing is missing.
    return result, senza_documento - set(result), adattatori


def _row_ref(supplier: str, offer: dict[str, Any]) -> str:
    return f"{supplier}:riga:{offer.get('sourceRow') or offer.get('source_row') or '?'}"


def _foglio_scelto(nomi: list[str], dichiarato: Any) -> str | None:
    """Return which of the document's sheets the registry declares to read.

    `"FIRST"` is the sentinel the registry already uses elsewhere for "the
    first sheet"; a real sheet name is compared the way the registry compares
    them, ignoring dots, spaces and case.
    """

    if not nomi:
        return None
    voluto = str(dichiarato or "FIRST").strip()
    if not voluto or voluto.upper() == "FIRST":
        return nomi[0]
    cercato = registro.normalizza(voluto)
    for nome in nomi:
        if registro.normalizza(nome) == cercato:
            return nome
    return None


@contextmanager
def _righe_del_documento(
    path: Path, foglio_dichiarato: Any
) -> Iterator[tuple[str, Iterator[tuple[int, tuple[Any, ...]]]]]:
    """Yield the registry-declared sheet row by row, regardless of file format.

    A supplier sending an Excel 97-2003 file is not less readable than one
    sending `.xlsx`; refusing that format would just be another
    single-supplier assumption, less visible than the last one.
    """

    with path.open("rb") as documento:
        firma = documento.read(8)
    if firma == _FIRMA_XLS:
        fogli = xls_reader.read_workbook(path)
        nome = _foglio_scelto([foglio.name for foglio in fogli], foglio_dichiarato)
        if nome is None:
            raise LookupError(f"il foglio «{foglio_dichiarato}» non c'è")
        foglio = next(item for item in fogli if item.name == nome)
        yield nome, enumerate((tuple(riga) for riga in foglio.rows), start=1)
        return

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        nome = _foglio_scelto(list(workbook.sheetnames), foglio_dichiarato)
        if nome is None:
            raise LookupError(f"il foglio «{foglio_dichiarato}» non c'è")
        yield nome, enumerate(workbook[nome].iter_rows(values_only=True), start=1)
    finally:
        workbook.close()


def _riferimento(
    colonne: dict[str, int], foglio: str, prima_riga: int, ultima_riga: int
) -> str:
    """Return where the block sits in the price list, using real column letters.

    This is the operator's only way to find the condition back in the
    supplier's file, and it also feeds into the promotion's id: if columns
    move, this reference must move with them, or it points at an empty
    column.
    """

    prima = get_column_letter(colonne["text"])
    ultima = get_column_letter(colonne.get("reward") or colonne["text"])
    if prima_riga == ultima_riga and prima == ultima:
        return f"{foglio}!{prima}{prima_riga}"
    return f"{foglio}!{prima}{prima_riga}:{ultima}{ultima_riga}"


def _condizione_da_leggere(
    fornitore: str,
    riferimento: str,
    testo: str,
    righe_ammesse: list[int],
    motivo: str,
) -> dict[str, Any]:
    """Build a promotion for a supplier-declared condition the program can't reconstruct.

    Not a file-reading error: the rows exist and the items can be ordered,
    but the commercial deal doesn't reduce to a calculation. It becomes an
    "ambiguous offer" — shown to the operator but excluded from any total —
    so it's visible and counted instead of silently dropped.
    """

    return make_promotion(
        supplier=fornitore,
        source_reference=riferimento,
        source_text=f"{testo} [condizione non ricomposta: {motivo}]",
        kind=KIND_AMBIGUOUS,
        eligible={"source_rows": list(righe_ammesse), "mix_allowed": True},
        certainty=CERTAINTY_REVIEW,
        confirmed=False,
        economic_effect={
            "type": "none",
            "deterministic": False,
            "active": False,
            "affects_total": False,
            "affects_supplier_choice": False,
        },
    )


class PromotionService:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        # Read state, keyed by supplier: cache key, parsed conditions, and any
        # read error, so a price list is only re-read when its signature
        # changes.
        self.firme_dei_listini: dict[str, tuple[str, int, int]] = {}
        self.condizioni_lette: dict[str, list[dict[str, Any]]] = {}
        self.errori_dei_listini: dict[str, dict[str, str]] = {}
        # How many conditions the last read declared but couldn't reconstruct,
        # per supplier: shown on the page as "to be checked"; this count lets
        # that be reported without counting them by hand.
        self.condizioni_da_verificare: dict[str, int] = {}
        # Suppliers left out of the last read, with the reason. Shown to the
        # operator by the server, same pattern as the catalog's load errors.
        self.load_errors: list[dict[str, str]] = []
        # How many discounts already included in the price the last read
        # dropped from the list: not shown (one per price-list row, pure
        # noise), but kept available as a single-line count.
        self.sconti_gia_nel_prezzo: int = 0

    # -- reading a single supplier --------------------------------------

    def _condizioni_del_fornitore(
        self,
        fornitore: str,
        voci: list[dict[str, Any]],
        path: Path | None,
        documento_sparito: bool = False,
        adapter_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if path is None:
            if documento_sparito:
                # Without this, "no promotions" and "the price list is gone"
                # would be indistinguishable: thresholds vanish from the
                # summary with no warning to explain why.
                self.load_errors.append(
                    _errore_di_lettura(fornitore, "il listino non è fra i documenti caricati")
                )
            return []
        try:
            if not path.is_file():
                self.load_errors.append(
                    _errore_di_lettura(fornitore, "il listino non è fra i documenti caricati")
                )
                return []
            firma = (str(path), path.stat().st_mtime_ns, _firma_del_registro())
        except OSError as exc:
            # A dropped network path or a permission error already fails at
            # `stat`; that's a warning too, not a page that refuses to open.
            nome = registro.nome_del_fornitore(fornitore)
            self.load_errors.append({
                "supplier": fornitore,
                "supplierName": nome,
                "message": f"Il listino {nome} «{path.name}» non è raggiungibile: {exc}",
            })
            return []
        if firma != self.firme_dei_listini.get(fornitore):
            self.firme_dei_listini[fornitore] = firma
            lette, errore = self._leggi_condizioni(
                fornitore, _adattatore_per_il_documento(voci, path, adapter_id), path
            )
            self.condizioni_lette[fornitore] = lette
            self.errori_dei_listini[fornitore] = errore or {}
        errore = self.errori_dei_listini.get(fornitore) or {}
        if errore:
            self.load_errors.append(dict(errore))
        condizioni = self.condizioni_lette.get(fornitore) or []
        self.condizioni_da_verificare[fornitore] = sum(
            1 for promotion in condizioni if promotion.get("kind") == KIND_AMBIGUOUS
        )
        return deepcopy(condizioni)

    def condizioni_di(
        self, fornitore: str, path: Path, adapter_id: str | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        """Return a supplier's commercial conditions, read from its document."""

        voci = fornitori_con_condizioni_dichiarate().get(str(fornitore).casefold()) or []
        return self._leggi_condizioni(
            fornitore, _adattatore_per_il_documento(voci, path, adapter_id), path
        )

    def _leggi_condizioni(
        self, fornitore: str, adattatore: dict[str, Any], path: Path
    ) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        """Return a price list's commercial conditions, or the reason they're missing.

        An unreadable price list costs only its own commercial conditions,
        not the whole page: without this being isolated, a corrupted file
        would fail `GET /api/review` and leave the operator with nothing to
        open.

        Column positions and layout come from the same adapter registry the
        price reader follows, keeping prices and conditions pointed at the
        same columns.
        """

        dichiarazione = adattatore.get("commercial_conditions")
        if not isinstance(dichiarazione, dict):
            return [], None

        forma, colonne, mancanti = _colonne_delle_condizioni(adattatore, dichiarazione)
        if mancanti:
            return [], _errore_di_lettura(
                fornitore, f"non so più dove il listino tiene {_elenco_in_italiano(mancanti)}"
            )

        try:
            with _righe_del_documento(path, dichiarazione.get("sheet")) as (titolo, righe):
                if forma == LAYOUT_BLOCCHI:
                    promozioni = self._blocchi(
                        fornitore, adattatore, dichiarazione, colonne, titolo, righe
                    )
                else:
                    promozioni = self._righe(
                        fornitore, adattatore, dichiarazione, colonne, titolo, righe
                    )
        except Exception as exc:  # noqa: BLE001 - the reason is surfaced to the operator
            return [], _errore_di_documento(fornitore, path, str(exc))
        return promozioni, None

    # -- the two layouts -----------------------------------------------

    def _righe(
        self,
        fornitore: str,
        adattatore: dict[str, Any],
        dichiarazione: dict[str, Any],
        colonne: dict[str, int],
        titolo: str,
        righe: Iterator[tuple[int, tuple[Any, ...]]],
    ) -> list[dict[str, Any]]:
        """Parse the "row" layout: one row carries a whole condition in a single column."""

        prima_riga = _numero_o(dichiarazione.get("data_start_row"), 1)
        non_ordinabili = _codici_non_ordinabili(adattatore)
        gia_compreso = fornitore in _fornitori_con_promozione_inclusa()
        promozioni: list[dict[str, Any]] = []
        for numero, riga in righe:
            if numero < prima_riga:
                continue
            testo = _cella(riga, colonne["text"])
            if not testo:
                continue
            if _cella(riga, colonne.get("row_code")).upper() in non_ordinabili:
                continue
            ean = _cella(riga, colonne.get("ean"))
            promozioni.extend(
                detect_promotions(
                    supplier=fornitore,
                    source_reference=_riferimento(colonne, titolo, numero, numero),
                    source_text=testo,
                    eligible={"source_rows": [numero], "eans": [ean] if ean else []},
                    included_in_product=gia_compreso,
                    numeric_discount_already_applied=True,
                )
            )
        return promozioni

    def _blocchi(
        self,
        fornitore: str,
        adattatore: dict[str, Any],
        dichiarazione: dict[str, Any],
        colonne: dict[str, int],
        titolo: str,
        righe: Iterator[tuple[int, tuple[Any, ...]]],
    ) -> list[dict[str, Any]]:
        """Parse the "blocks" layout: heading, eligible items, then the reward row."""

        prima_riga = _numero_o(dichiarazione.get("data_start_row"), 1)
        massimo = _numero_o(dichiarazione.get("max_block_rows"), _RIGHE_MASSIME_DI_UN_BLOCCO)
        # Which codes mark a row as non-purchasable comes from the adapter
        # registry; needed here so the reward row isn't counted among the
        # items that reach the threshold.
        non_ordinabili = _codici_non_ordinabili(adattatore)

        promozioni: list[dict[str, Any]] = []
        heading_row: int | None = None
        heading_text = ""
        eligible_rows: list[int] = []
        ultima_riga = prima_riga
        for row_number, row in righe:
            if row_number < prima_riga:
                continue
            ultima_riga = row_number
            descrizione = _cella(row, colonne["text"])
            # When a supplier writes the reward name in the same column as
            # the description, the registry declares the same column for both
            # roles; reading it twice would duplicate it in the assembled
            # text. The threshold itself still comes out correct — only the
            # operator-facing text would get noisy. Where the two roles use
            # separate columns, this has no effect.
            nome_del_premio = (
                "" if colonne["reward"] == colonne["text"] else _cella(row, colonne["reward"])
            )
            ean = _cella(row, colonne["ean"])
            discount_code = _cella(row, colonne["row_code"]).upper()

            if e_intestazione_di_soglia(descrizione):
                if heading_row is not None:
                    # A second heading with no reward row in between: the
                    # first condition can no longer be reconstructed.
                    promozioni.append(_condizione_da_leggere(
                        fornitore,
                        _riferimento(colonne, titolo, heading_row, row_number - 1),
                        heading_text, eligible_rows,
                        "la riga con l'omaggio non è arrivata prima dell'intestazione successiva",
                    ))
                heading_row = row_number
                heading_text = descrizione
                eligible_rows = []
                continue
            if heading_row is None:
                if e_riga_premio(descrizione):
                    # A reward row with no heading: the threshold governing it
                    # wasn't recognized, but the item is real.
                    promozioni.append(_condizione_da_leggere(
                        fornitore,
                        _riferimento(colonne, titolo, row_number, row_number),
                        " ".join(item for item in (descrizione, nome_del_premio) if item), [],
                        "manca l'intestazione con la quantità da acquistare",
                    ))
                continue
            if e_riga_premio(descrizione):
                source_text = " ".join(
                    item for item in (heading_text, descrizione, nome_del_premio) if item
                )
                promotion = detect_threshold_gift(
                    supplier=fornitore,
                    source_reference=_riferimento(colonne, titolo, heading_row, row_number),
                    source_text=source_text,
                    eligible={"source_rows": eligible_rows, "mix_allowed": True},
                    # The reward row's EAN is available right here; without
                    # it the promotion's `reward.ean` would be null and no
                    # one could trace it back to the gifted item.
                    reward_ean=ean or None,
                )
                if promotion:
                    promozioni.append(promotion)
                else:
                    # The block is complete but the text doesn't parse into a
                    # calculation: it's still a commercial condition on
                    # purchasable items, so it can't just disappear.
                    promozioni.append(_condizione_da_leggere(
                        fornitore,
                        _riferimento(colonne, titolo, heading_row, row_number),
                        source_text, eligible_rows,
                        "il testo del blocco non dice una soglia calcolabile",
                    ))
                heading_row = None
                heading_text = ""
                eligible_rows = []
                continue
            if ean and discount_code not in non_ordinabili:
                eligible_rows.append(row_number)
            elif row_number - heading_row > massimo:
                promozioni.append(_condizione_da_leggere(
                    fornitore,
                    _riferimento(colonne, titolo, heading_row, row_number),
                    heading_text, eligible_rows,
                    f"nessuna riga con l'omaggio entro {massimo} righe",
                ))
                heading_row = None
                heading_text = ""
                eligible_rows = []
        if heading_row is not None:
            # The sheet ends with a block still open: the same loss, at a
            # point no other check catches.
            promozioni.append(_condizione_da_leggere(
                fornitore,
                _riferimento(colonne, titolo, heading_row, max(ultima_riga, heading_row)),
                heading_text, eligible_rows,
                "il foglio finisce prima della riga con l'omaggio",
            ))
        return promozioni

    # -- the entry point ----------------------------------------------

    def detect(self, review: dict[str, Any]) -> list[dict[str, Any]]:
        self.load_errors = []
        self.condizioni_da_verificare = {}
        grouped_text: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
        promotions: list[dict[str, Any]] = []

        for product in review.get("products") or []:
            for offer in product.get("offers") or []:
                supplier = offer_supplier_id(offer).casefold()
                # The "can it be ordered" rule is answered by `offerta`, not
                # duplicated here: this function decides which offers count
                # toward promotion thresholds, so a rule that grows elsewhere
                # and not here would get a reward wrong.
                if not supplier or not offer_is_available(offer):
                    continue
                text = str(offer.get("promotionText") or "").strip()
                if text:
                    grouped_text[(supplier, " ".join(text.casefold().split()))].append((product, offer))

                discount = offer.get("discountRate")
                if discount not in (None, "", 0, 0.0):
                    numeric = detect_numeric_discount(
                        supplier=supplier,
                        source_reference=_row_ref(supplier, offer),
                        source_text=f"Sconto numerico {float(discount) * 100:g}%",
                        discount_value=discount,
                        eligible={
                            "products": [product.get("id")],
                            "eans": [product.get("ean")],
                            "source_rows": [offer.get("sourceRow")],
                        },
                        already_applied=True,
                    )
                    if numeric:
                        promotions.append(numeric)

        # Read the registry once: opening it per offer group would mean
        # re-reading the same file dozens of times for the same answer.
        gia_compreso_nel_prezzo = _fornitori_con_promozione_inclusa()
        for (supplier, _normalized), holders in grouped_text.items():
            products = [product for product, _offer in holders]
            offers = [offer for _product, offer in holders]
            source_rows = [offer.get("sourceRow") for offer in offers if offer.get("sourceRow") is not None]
            source_reference = (
                f"{supplier}:righe:{min(source_rows)}-{max(source_rows)}"
                if source_rows
                else f"{supplier}:offerta"
            )
            promotions.extend(
                detect_promotions(
                    supplier=supplier,
                    source_reference=source_reference,
                    source_text=str(offers[0].get("promotionText") or ""),
                    eligible={
                        "products": [product.get("id") for product in products],
                        "eans": [product.get("ean") for product in products],
                        "source_rows": source_rows,
                        "mix_allowed": True if len(products) > 1 else False,
                    },
                    eligible_group=f"{supplier}:{source_reference}",
                    included_in_product=supplier in gia_compreso_nel_prezzo,
                    numeric_discount_already_applied=True,
                )
            )

        paths, senza_documento, adattatori_riconosciuti = _source_paths(review)
        # Only suppliers that declare where they keep their conditions get
        # read from their own price list; the list comes from the registry,
        # sorted alphabetically so two reads of the same run produce the same
        # summary.
        for fornitore, voci in sorted(fornitori_con_condizioni_dichiarate().items()):
            promotions.extend(self._condizioni_del_fornitore(
                fornitore, voci, paths.get(fornitore), fornitore in senza_documento,
                adattatori_riconosciuti.get(fornitore),
            ))
        deduplicated = {str(item.get("id")): item for item in promotions if item.get("id")}
        return self._solo_quelle_che_cambiano_qualcosa(list(deduplicated.values()))

    def _solo_quelle_che_cambiano_qualcosa(
        self, promotions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Drop discounts the price already includes; they aren't conditions worth surfacing.

        Measured on a real weekly price list: 165 of 166 declared conditions
        were a flat 10% numeric discount, one per row, all already included
        in the price. Buried among those, the few real gift thresholds became
        impossible to spot, and a condition nobody reads is as good as one
        that doesn't exist.

        What remains is only what changes a decision: a reward in goods
        (`soglia_omaggio`), or a discount the price does NOT already contain
        and therefore moves the total. Nothing calculated is discarded: an
        `already_applied` discount never produced a price in the first place
        — `calculate_effective_price` rejects it — so this only changes what
        gets shown.
        """

        tenute: list[dict[str, Any]] = []
        nel_prezzo = 0
        for promotion in promotions:
            effetto = promotion.get("economic_effect") or {}
            if effetto.get("type") == "price_discount" and effetto.get("already_applied"):
                nel_prezzo += 1
                continue
            tenute.append(promotion)
        self.sconti_gia_nel_prezzo = nel_prezzo
        return tenute

    def decorate(
        self,
        review: dict[str, Any],
        selections: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            promotions = self.detect(review)
            return decorate_review_data(review, promotions, selections=selections or {})


def _numero_o(dichiarato: Any, ripiego: int) -> int:
    """Return a registry-declared number, or the fallback if it isn't one.

    A badly learned registry entry must not be able to stop the read: if
    `data_start_row` arrives as text, reading starts from row one — a bit more
    than necessary — instead of reading nothing at all.
    """

    if isinstance(dichiarato, bool) or not isinstance(dichiarato, int):
        return ripiego
    return dichiarato if dichiarato >= 1 else ripiego
