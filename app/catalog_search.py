"""Searchable, read-only catalogue assembled from the active supplier files."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from detect_displays import analyse_workbook
from inspect_sources import container_format
# No dedicated reader is named here: which reader handles which supplier is
# decided by `prepare_manifest_sources.lettore_dedicato`, the same function
# the recompute pipeline uses. Display integration for Larice
# stays separate, next to the reader rather than inside it, matching the
# pipeline's own structure.
from prepare_sources import integrate_larice_displays
from prepare_manifest_sources import (
    colonne_corrette,
    lettore_dedicato,
    mapped_standalone_displays,
    read_mapped_csv_supplier,
    read_mapped_xlsx_supplier,
)
# The function is imported, not the module: `registro` is already used here
# as a local variable name (in `_signature`), and a module shadowed by an
# assignment is a failure that only surfaces at runtime.
from registro import (
    adattatore_base,
    adattatori_effettivi,
    mappatura_spedita,
    nome_del_fornitore,
    percorso_imparato,
    voce_in_uso,
)


PROMOTION_WORDS = re.compile(r"\b(omaggi|omaggio|gratis|offerta|promoz|acquista|ogni\s+\d+)\b", re.IGNORECASE)
ADAPTERS_PATH = Path(__file__).resolve().parents[1] / "references" / "adapters.json"


_NOMI_DEL_CATALOGO: dict[str, str] = {}


def _supplier_name(supplier: str) -> str:
    """This supplier's display name, according to the adapter registry.

    Cached until the registry changes. `_offer` requests it for every price
    list row — tens of thousands per catalogue — and
    `registro.nome_del_fornitore` does a file `stat` and copies the name map
    on every call: measured, tens of thousands of calls cost several full
    seconds. The file's signature is checked once per catalogue build, not
    once per row.

    The path is always passed explicitly: `nome_del_fornitore` without a
    path reads the default registry, and in this module the registry is the
    one declared by `ADAPTERS_PATH`.
    """

    memorizzato = _NOMI_DEL_CATALOGO.get(supplier)
    if memorizzato is not None:
        return memorizzato
    nome = nome_del_fornitore(supplier, ADAPTERS_PATH)
    _NOMI_DEL_CATALOGO[supplier] = nome
    return nome


def _adapter_mapping(adapter_id: str) -> dict[str, Any]:
    """The column mapping declared in the adapter registry.

    Used when the review doesn't carry its own field_mapping: a known,
    registered adapter's mapping is fetched from the registry rather than
    kept as a second copy in this module, which would eventually drift from
    the real one.

    The three ways this can fail — missing registry, unreadable registry,
    unknown adapter — each get a distinct message: these are application
    failures, not review data problems, and whoever reads the warning needs
    to know where to look.

    The registry read here is the merged one: shipped plus learned. Reading
    only the shipped `references/adapters.json` would make a re-learned
    adapter with the same id invisible to this module while the recompute
    pipeline still sees it.
    """

    if not adapter_id:
        raise ValueError(
            "la review non dice con quale schema questo listino è stato letto: "
            "rifai il confronto dalla pagina Importa"
        )
    if not ADAPTERS_PATH.is_file():
        raise ValueError(f"manca il registro degli adattatori ({ADAPTERS_PATH.name})")
    # There are two registries, and they're treated differently. The shipped
    # one is under version control, and a failure to read it is an
    # application bug. The learned one is written by the program, and a
    # machine powered off mid-write can truncate it:
    # `registro._leggi_registro` still returns the shipped entries along
    # with the failure reason, and the rest of the code keeps working with
    # those. Treating every failure reason as an outright rejection would
    # make a supplier disappear from the viewer over a file that isn't even
    # the one the error message names.
    voci, motivo = adattatori_effettivi(ADAPTERS_PATH)
    voce = next((item for item in voci if str(item.get("id") or "") == adapter_id), {})
    if not voce:
        if motivo:
            raise ValueError(f"{_quale_registro(motivo)} non è leggibile: {motivo}")
        raise ValueError(f"l'adattatore {adapter_id} non c'è nel registro degli adattatori")
    mapping = voce.get("field_mapping")
    if isinstance(mapping, dict) and mapping:
        return mapping
    raise ValueError(f"l'adattatore {adapter_id} non dichiara la mappatura delle colonne")


def _quale_registro(motivo: str) -> str:
    """The name of the file that failed to read, so the error doesn't point elsewhere.

    Naming the wrong registry file in a two-registry error message sends the
    user to check a file that's under version control and perfectly fine.
    """

    imparato = percorso_imparato(ADAPTERS_PATH)
    if "imparat" in motivo.casefold():
        return (
            f"il registro degli adattatori imparati su questo computer ({imparato.name})"
            "; cancellarlo fa ricominciare l'apprendimento da zero e non rompe nient'altro"
        )
    return f"il registro degli adattatori ({ADAPTERS_PATH.name})"


def _mappatura_spedita(adapter_id: str) -> dict[str, Any]:
    """The mapping as it stands in the shipped registry, or `{}` if absent.

    Deliberately the shipped mapping, not the effective one — that
    difference matters: the learned mapping is written by the program, and a
    mapping confirmed in the page's column mapping is built from scratch, without
    carrying over `exclude_rows`. Comparing against the learned mapping
    instead would let a food-row exclusion rule silently stop applying the
    week after someone re-confirmed that supplier's mapping, since both
    sides of the comparison would have lost the rule and the check would
    never trigger (measured: 11 rows read instead of 9, two of them food
    rows). The shipped registry changes only by deliberate decision, so it
    is the reference.

    Read through `registro`, not a local `json.load`: the registry is two
    files, and reading one of them directly risks reading the wrong one.
    """

    return mappatura_spedita(adapter_id, ADAPTERS_PATH)


def _mappatura_attiva(adapter_id: str, mapping: dict[str, Any]) -> dict[str, Any]:
    """Which columns to read with, and the guarantee that exclusions aren't lost.

    A mapping confirmed in the page's column mapping takes precedence over the
    registry's — it's the more recent answer, given by someone who had the
    actual document in front of them — but it can arrive without the
    exclusion rules the registry declares. For at least one supplier that
    rule discards thousands of food-category rows the user doesn't carry:
    reading is refused here rather than silently letting those rows in.

    The rule itself isn't hard-coded here or tied to a specific supplier —
    it's whatever the registry declares, for whichever supplier declares it.
    """

    attiva = mapping or _adapter_mapping(adapter_id)
    di_serie = _mappatura_spedita(adapter_id)
    if _esclude_gli_alimentari(di_serie) and not _esclude_gli_alimentari(attiva):
        raise ValueError(
            "la mappatura delle colonne non scarta le righe alimentari: senza quella "
            "regola entrerebbero nel confronto migliaia di articoli FOOD, che l'utente "
            "non tratta"
        )
    return attiva


def _motivo(exc: BaseException) -> str:
    """The reason for a failure, never empty.

    `str(MemoryError())` is the empty string, and a warning that trails off
    with a colon and nothing after it helps no one: the exception type is
    shown instead in that case.
    """

    return str(exc).strip() or type(exc).__name__


def _esclude_gli_alimentari(mapping: dict[str, Any]) -> bool:
    """True when the mapping actually discards food-category rows.

    The user doesn't carry food items, and in one supplier's price list
    those are 8,292 rows out of 17,143. The rule lives in the mapping, and a
    mapping confirmed on the review page — which takes precedence over the
    shipped one — can arrive without it: this check exists so those rows
    still get excluded either way.
    """

    for rule in mapping.get("exclude_rows") or []:
        if str(rule.get("field") or "") != "category":
            continue
        if str(rule.get("equals") or "").strip().casefold() == "food":
            return True
        if rule.get("regex") and re.search(str(rule["regex"]), "FOOD", re.IGNORECASE):
            return True
    return False


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _search_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^A-Za-z0-9]+", " ", text).casefold().split())


def _catalog_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _promotion_text(record: dict[str, Any]) -> str:
    availability = str(record.get("availability") or "").strip()
    if availability.casefold().startswith("disponibile"):
        availability = availability[len("Disponibile") :].strip()
    description = str(record.get("description") or "").strip()
    if availability:
        return availability
    return description if PROMOTION_WORDS.search(description) else ""


def _offer(supplier: str, record: dict[str, Any]) -> dict[str, Any] | None:
    unit_price = _number(record.get("unit_price_net"))
    factor = _number(record.get("order_multiplier"))
    factor_label = "pezzi/collo"
    if factor is None:
        factor = _number(record.get("pieces_per_carton"))
    if factor is None or factor <= 0:
        factor = 1.0
        factor_label = "pezzo"
    # `> 0`, not `>= 0`. Same guard `build_review_data` applies, needed here
    # too: a price that reads as 0.00 passes every other filter and then
    # wins the comparison, since sorting puts the lowest price first — the
    # product ends up assigned to the supplier whose cell failed to read, at
    # zero total, and the minimum-order threshold never triggers because
    # zero is below any threshold. Catalogue additions, price refreshes and
    # manual matching all go through this function, so this one guard covers
    # all three paths to the same failure.
    if unit_price is None or unit_price <= 0 or not record.get("usable", True):
        return None
    order_price = round(unit_price * factor, 6)
    discount_rate = _number(record.get("discount_rate")) or 0.0
    return {
        "supplierId": supplier,
        "supplierName": _supplier_name(supplier),
        "available": True,
        "status": "CATALOGO_FORNITORE",
        "method": "CATALOGO",
        "confidence": "ALTA",
        "requiresConfirmation": False,
        "confirmed": True,
        "rationale": "Articolo aggiunto dal catalogo del fornitore.",
        "description": str(record.get("description") or "").strip(),
        "ean": str(record.get("ean") or "").strip(),
        "supplierCode": record.get("supplier_code"),
        "sourceRow": record.get("source_row"),
        "unitPriceNet": round(unit_price, 6),
        "quantityFactor": round(factor, 6),
        "quantityFactorLabel": factor_label,
        "orderUnitPriceNet": order_price,
        "price": order_price,
        "unitsPerOrderUnit": round(factor, 6),
        "pricePerPiece": round(unit_price, 6),
        "matchStatus": "CATALOGO_FORNITORE",
        "details": " | ".join(
            str(item)
            for item in (record.get("packaging"), record.get("availability"), record.get("unit"))
            if item not in (None, "")
        ),
        "promotionText": _promotion_text(record),
        "discountRate": round(discount_rate, 6),
        "sourceReference": {"supplier": supplier, "row": record.get("source_row")},
        "alternatives": [],
    }


@dataclass(frozen=True)
class _Sorgente:
    """A price list from the review, plus what's needed to pick its reader.

    `adapter_id` and `state` are the same two fields the recompute pipeline
    decides on (`prepare_manifest_sources.lettore_dedicato`): the review
    already carries them, written by `build_review_data.manifest_files` as
    `adapterId` and `schemaState`. Without them reaching this far, the
    catalogue had to guess the reader from the supplier's name — unreliably.
    """

    path: Path
    mapping: dict[str, Any]
    adapter_id: str
    state: str


class SupplierCatalog:
    """Load supplier sources once and expose conservative search results."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.signature: tuple[tuple[str, str, int], ...] = ()
        self.entries: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.unique_offers: dict[str, dict[str, dict[str, Any]]] = {}
        # Every price list's rows in document order, for the viewer: the
        # catalogue search keeps only orderable rows, but the viewer needs
        # all of them.
        self.righe_per_fornitore: dict[str, list[dict[str, Any]]] = {}
        # Suppliers excluded from the last load, with a human-readable
        # reason: the server turns these into review warnings, since a
        # catalogue that silently shrinks is worse than one that fails to
        # load.
        self.load_errors: list[dict[str, str]] = []

    @staticmethod
    def _source_paths(review: dict[str, Any]) -> tuple[dict[str, "_Sorgente"], list[dict[str, str]]]:
        """The price lists the review declares, and the ones that no longer exist.

        The most common way a supplier drops out of the comparison isn't a
        corrupted file: it's a file moved or renamed on the desktop. Left
        unchecked, that supplier would silently disappear from product
        search with nothing to explain why.
        """

        paths: dict[str, _Sorgente] = {}
        mancanti: list[dict[str, str]] = []
        for item in review.get("files") or []:
            stato = str(item.get("schemaState") or "")
            # The same two states the recompute pipeline skips
            # (`prepare_manifest_sources.main`): those documents never
            # entered the comparison, and the Import page already shows them
            # with their own reason. Surfacing them here too would just
            # duplicate that warning.
            if stato in {"FILE_NON_PERTINENTE", "AMBIGUO"}:
                continue
            # The management-software export isn't a price list to browse,
            # and this is an explicit rule for it: relying on an accidental
            # side effect instead — a null `supplierId`, an empty key, an
            # empty mapping, silently dropped by `_read_sources` — is
            # exactly the kind of bug that also makes real suppliers
            # disappear.
            if str(item.get("role") or "") == "master":
                continue
            supplier = str(item.get("supplierId") or "").casefold()
            raw_path = item.get("sourcePath") or item.get("originalPath") or item.get("path")
            mapping = item.get("fieldMapping") or item.get("field_mapping") or {}
            if not raw_path or not supplier:
                continue
            path = Path(str(raw_path)).resolve()
            if path.is_file():
                paths[supplier] = _Sorgente(
                    path=path,
                    mapping=mapping if isinstance(mapping, dict) else {},
                    adapter_id=str(item.get("adapterId") or item.get("adapter_id") or ""),
                    state=stato,
                )
            elif supplier:
                mancanti.append({
                    "supplier": supplier,
                    "supplierName": _supplier_name(supplier),
                    "message": (
                        f"Il listino {_supplier_name(supplier)} non è stato trovato dove la run "
                        f"lo cerca: {path}. Se è stato spostato o rinominato, va ricaricato."
                    ),
                })
        return paths, mancanti

    @staticmethod
    def _signature(
        paths: dict[str, "_Sorgente"],
        mancanti: list[dict[str, str]] | None = None,
    ) -> tuple[Any, ...]:
        """What causes the catalogue to reload.

        Besides the price lists themselves, this includes the adapter
        registry: at least one supplier's mapping comes from there, so if
        the registry disappears or gets fixed the catalogue must notice,
        rather than staying stuck on a stale error until restart. It also
        includes price lists that were declared but not found: without them
        in the signature, a warning would stay on screen even after that
        supplier left the run.

        The registry is two files, shipped and learned: without the learned
        one in the signature, a re-learned adapter wouldn't trigger a
        catalogue rebuild and the viewer would stay on the old schema until
        restart. And there's `adapter_id` and `state`, which now decide the
        reader: if they change without the signature noticing, the catalogue
        keeps reading with the old choice.
        """

        registro = tuple(
            (str(documento), documento.stat().st_mtime_ns if documento.is_file() else -1)
            for documento in (ADAPTERS_PATH, percorso_imparato(ADAPTERS_PATH))
        )
        assenti = tuple(sorted(str(voce.get("supplier") or "") for voce in mancanti or []))
        return (registro, assenti, *sorted(
            (
                supplier,
                str(sorgente.path),
                sorgente.path.stat().st_mtime_ns,
                sorgente.adapter_id,
                sorgente.state,
                json.dumps(sorgente.mapping, ensure_ascii=False, sort_keys=True, default=str),
            )
            for supplier, sorgente in paths.items()
        ))

    @staticmethod
    def _read_supplier(supplier: str, sorgente: "_Sorgente") -> list[dict[str, Any]]:
        """Reads a price list with the SAME logic as the recompute pipeline.

        The reader is chosen by the decision — schema state and adapter —
        never by the supplier's name, and `prepare_manifest_sources.lettore_dedicato`
        is the single authority on which reader opens which document.

        Hard-coding a reader by supplier name here would risk exactly the
        kind of drift this guards against: a price list the pipeline reads
        with a different reader than the one this function assumes would
        make that supplier stay in the comparison while disappearing from
        the viewer and from product search.
        """

        lettore = lettore_dedicato(sorgente.state, sorgente.adapter_id)
        if lettore is not None:
            # Manually corrected columns are applied inside the dedicated
            # reader, matching the pipeline: routing the document to the
            # generic reader to apply them would lose Larice's displays and
            # its free-goods thresholds.
            #
            # The adapter is passed through, never faked as `{}`: a learned
            # mapping names columns and the positions to re-read them live in
            # its own fingerprint. Without it, the pipeline would read the
            # price list fine while this function failed — the supplier
            # present in the comparison but missing from the viewer, the
            # same class of bug as the hard-coded-reader issue above,
            # resurfacing in a different spot.
            voce = voce_in_uso(sorgente.adapter_id, adattatori_effettivi(ADAPTERS_PATH)[0])
            extra = colonne_corrette({"field_mapping": sorgente.mapping}, voce, sorgente.adapter_id)
            # `adattatore_base`: a variant like `larice_v1__locale` — the
            # learned mapping layered on the shipped one — is still Larice,
            # displays included.
            if adattatore_base(sorgente.adapter_id) == "larice_v1":
                records, _warnings = lettore(sorgente.path, **extra)
                records, _displays, _summary = integrate_larice_displays(
                    records, analyse_workbook(sorgente.path),
                )
                return records
            risultato = lettore(sorgente.path, **extra)
            return risultato[0] if isinstance(risultato, tuple) else risultato

        attiva = _mappatura_attiva(sorgente.adapter_id, sorgente.mapping)
        # The format is decided by the file's first bytes, not its
        # extension: at least one supplier sends a legacy Excel 97-2003 file
        # under what looks like a CSV export, and routing an `.xls` to the
        # CSV reader raises `UnicodeDecodeError` on a perfectly valid file.
        # Legacy `.xls` is read by `app/xls_reader.py`, inside the generic
        # reader path: going through here loses no hand-written reader.
        if container_format(sorgente.path) == "csv":
            records, _warnings = read_mapped_csv_supplier(sorgente.path, supplier, attiva)
        else:
            records, _warnings = read_mapped_xlsx_supplier(sorgente.path, supplier, attiva)
        records, _displays, _audit = mapped_standalone_displays(records, supplier, attiva)
        return records

    @classmethod
    def _read_sources(
        cls,
        paths: dict[str, "_Sorgente"],
    ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
        """Reads price lists one supplier at a time, isolating whichever fails.

        An unreadable price list removes only its own supplier and nothing
        else. Without this per-supplier isolation, one misread `.xls` file
        would take down the whole catalogue, replacing every other
        supplier's results with a raw technical error message.
        """

        sources: dict[str, list[dict[str, Any]]] = {}
        errors: list[dict[str, str]] = []
        # A fixed priority order for a small set of long-standing suppliers,
        # the rest after: on an exact tie in per-piece price, this decides
        # which offer is listed first, and isn't something to change lightly.
        ordine = {"betulla": 0, "larice": 1, "noce": 2}
        # Every supplier that reaches `paths` is either read successfully or
        # explained in `errors`; nothing in this loop drops one silently.
        # Whichever suppliers don't belong in the comparison are filtered
        # out earlier, in `_source_paths`.
        for supplier, sorgente in sorted(paths.items(), key=lambda voce: (ordine.get(voce[0], 3), voce[0])):
            try:
                sources[supplier] = cls._read_supplier(supplier, sorgente)
            except Exception as exc:  # noqa: BLE001 - one broken supplier must not stop the others
                errors.append({
                    "supplier": supplier,
                    "supplierName": _supplier_name(supplier),
                    "message": (
                        f"Il listino {_supplier_name(supplier)} «{sorgente.path.name}» "
                        f"non è stato letto: {_motivo(exc)}"
                    ),
                })
        return sources, errors

    def _ensure_loaded(self, review: dict[str, Any]) -> None:
        # Supplier names are re-read from the registry once per call to this
        # function, not once per price-list row: `_offer` requests one per
        # row, and there are tens of thousands of rows. The registry is
        # already part of `signature`, so the catalogue rebuilds — and the
        # names with it — whenever it changes.
        _NOMI_DEL_CATALOGO.clear()
        paths, mancanti = self._source_paths(review)
        signature = self._signature(paths, mancanti)
        # A failed read still counts as a completed read: without
        # remembering that, an unreadable price list would be reopened on
        # every keystroke in product search.
        if signature == self.signature and (self.entries or self.load_errors):
            return
        sources, errors = self._read_sources(paths)
        usable = {
            supplier: [record for record in records if record.get("usable", True) and _offer(supplier, record)]
            for supplier, records in sources.items()
        }
        # A price list that opens fine but has no orderable rows removes its
        # supplier from the comparison exactly like one that fails to open:
        # reporting only the second case would turn the first into a silent
        # disappearance.
        for supplier, records in usable.items():
            if records:
                continue
            errors.append({
                "supplier": supplier,
                "supplierName": _supplier_name(supplier),
                "message": (
                    f"Il listino {_supplier_name(supplier)} «{paths[supplier].path.name}» è stato "
                    "letto ma non contiene nessuna riga ordinabile."
                ),
            })
        self.load_errors = mancanti + errors
        # Every row is kept per supplier, orderable or not: this is what the
        # viewer shows. Catalogue search works from orderable rows only —
        # correctly so, since it proposes goods to buy — but whoever opens a
        # price list to understand why a product wasn't matched needs to see
        # the discarded row too, with its reason. This doesn't duplicate
        # memory: `usable` holds references to the same dicts, and this just
        # adds the discarded ones alongside.
        self.righe_per_fornitore = {
            supplier: list(records) for supplier, records in sources.items()
        }
        counts = {
            supplier: Counter(str(record.get("ean") or "") for record in records if record.get("ean"))
            for supplier, records in usable.items()
        }
        unique_by_ean: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        ambiguous: list[tuple[str, dict[str, Any]]] = []
        for supplier, records in usable.items():
            for record in records:
                ean = str(record.get("ean") or "").strip()
                if ean and counts[supplier][ean] == 1:
                    unique_by_ean[ean][supplier] = record
                else:
                    ambiguous.append((supplier, record))

        entries: list[dict[str, Any]] = []
        for ean, records in unique_by_ean.items():
            preferred_supplier = next((item for item in ("betulla", "larice", "noce", "cipresso") if item in records), next(iter(records)))
            preferred = records[preferred_supplier]
            offers = [candidate for supplier, record in records.items() if (candidate := _offer(supplier, record))]
            entries.append(self._entry(f"ean:{ean}", preferred, offers, requires_confirmation=False))
        for supplier, record in ambiguous:
            offer = _offer(supplier, record)
            if offer:
                key = f"row:{supplier}:{record.get('source_row')}:{record.get('ean') or ''}"
                entries.append(self._entry(key, record, [offer], requires_confirmation=bool(record.get("ean"))))

        entries.sort(key=lambda item: (_search_text(item["name"]), item["catalogId"]))
        self.entries = entries
        self.by_id = {item["catalogId"]: item for item in entries}
        self.unique_offers = {
            supplier: {
                ean: candidate
                for ean, records in unique_by_ean.items()
                if (record := records.get(supplier)) is not None
                and (candidate := _offer(supplier, record)) is not None
            }
            for supplier in sources
        }
        self.signature = signature

    def enrich_review(self, review: dict[str, Any]) -> dict[str, Any]:
        """Refresh exact supplier offers and attach current promotion text."""

        with self.lock:
            self._ensure_loaded(review)
            for product in review.get("products") or []:
                if str(product.get("itemType") or product.get("kind") or "").casefold() == "display":
                    continue
                ean = str(product.get("ean") or "").strip()
                if not ean:
                    continue
                offers = product.setdefault("offers", [])
                positions = {
                    str(item.get("supplierId") or item.get("supplier_id") or ""): index
                    for index, item in enumerate(offers)
                }
                for supplier, by_ean in self.unique_offers.items():
                    current = by_ean.get(ean)
                    if not current:
                        continue
                    refreshed = deepcopy(current)
                    refreshed.update({
                        "status": "EAN_ESATTO",
                        "method": "EAN",
                        "confidence": "CERTA",
                        "requiresConfirmation": False,
                        "confirmed": True,
                        "rationale": "Codice esatto presente in una sola riga ordinabile del listino aggiornato.",
                        "matchStatus": "EAN_ESATTO",
                    })
                    if supplier in positions:
                        old = offers[positions[supplier]]
                        if old.get("lastPriceDifference") is not None:
                            refreshed["lastPriceDifference"] = old.get("lastPriceDifference")
                        if old.get("lastPriceDifferencePct") is not None:
                            refreshed["lastPriceDifferencePct"] = old.get("lastPriceDifferencePct")
                        offers[positions[supplier]] = refreshed
                    else:
                        offers.append(refreshed)
            return review

    @staticmethod
    def _entry(
        key: str,
        preferred: dict[str, Any],
        offers: list[dict[str, Any]],
        *,
        requires_confirmation: bool,
    ) -> dict[str, Any]:
        catalog_id = _catalog_id(key)
        available = sorted(offers, key=lambda item: (item.get("unitPriceNet") or float("inf"), item["supplierId"]))
        selected = available[0]["supplierId"] if available else None
        name = str(preferred.get("description") or "Articolo senza descrizione").strip()
        ean = str(preferred.get("ean") or "").strip()
        return {
            "catalogId": catalog_id,
            "id": f"manual:{catalog_id}",
            "kind": "PRODUCT",
            "itemType": "product",
            "sourceRow": None,
            "ean": ean,
            "description": name,
            "name": name,
            "lastUnitPrice": None,
            "quantity": 0,
            "quantityLabel": "colli",
            "orderUnitLabel": "colli",
            "selectedSupplierId": selected,
            "confirmed": bool(selected) and not requires_confirmation,
            "requiresConfirmation": requires_confirmation,
            "confirmationMessage": "Codice ripetuto nel catalogo: controllare la variante." if requires_confirmation else "",
            "components": [],
            "warnings": ["Codice ripetuto: controllare la variante."] if requires_confirmation else [],
            "notes": "Aggiunto manualmente",
            "addedManually": True,
            "offers": available,
            "_search": _search_text(" ".join([name, ean, *(str(item.get("supplierCode") or "") for item in offers)])),
        }

    def search(self, review: dict[str, Any], query: str, limit: int = 20) -> list[dict[str, Any]]:
        normalized = _search_text(query)
        if len(normalized) < 2:
            return []
        with self.lock:
            self._ensure_loaded(review)
            existing_eans = {str(item.get("ean") or "") for item in review.get("products") or [] if item.get("ean")}
            tokens = normalized.split()
            results = [item for item in self.entries if all(token in item["_search"] for token in tokens)]
            results.sort(
                key=lambda item: (
                    0 if str(item.get("ean") or "") == query.strip() else 1,
                    0 if item["_search"].startswith(normalized) else 1,
                    min((offer.get("unitPriceNet") or float("inf")) for offer in item["offers"]),
                    item["name"],
                )
            )
            visible = []
            for item in results[: max(1, min(int(limit), 50))]:
                clean = {key: deepcopy(value) for key, value in item.items() if key != "_search"}
                clean["alreadyPresent"] = bool(clean.get("ean") and clean["ean"] in existing_eans)
                visible.append(clean)
            return visible

    # ------------------------------------------------ il listino come si legge

    @staticmethod
    def _riga_di_listino(supplier: str, record: dict[str, Any]) -> dict[str, Any]:
        """A price list row with the columns that ordering later depends on.

        The same fields that decide a match — code, description, pieces per
        carton, price — not the sheet's own columns: this shows what the
        program actually read, which is exactly what's needed when a product
        wasn't matched and it's unclear why. If a column was misread, it
        shows up misread here too.
        """

        offerta = _offer(supplier, record)
        return {
            "sourceRow": record.get("source_row"),
            "ean": str(record.get("ean") or "").strip(),
            "supplierCode": str(record.get("supplier_code") or "").strip(),
            "description": str(record.get("description") or "").strip(),
            "piecesPerCarton": _number(record.get("pieces_per_carton")),
            "orderMultiplier": _number(record.get("order_multiplier")),
            "unitPriceNet": _number(record.get("unit_price_net")),
            # `ordinabile` asks the same question the comparison itself
            # asks: a row that's visible but can't be ordered has to say so,
            # and say why.
            "ordinabile": offerta is not None,
            "motivo": str(record.get("unusable_reason") or record.get("row_type") or ""),
            "orderUnitPriceNet": (offerta or {}).get("orderUnitPriceNet"),
        }

    def fornitori_sfogliabili(self, review: dict[str, Any]) -> list[dict[str, Any]]:
        """Which price lists can be browsed, and how many rows each has."""

        with self.lock:
            self._ensure_loaded(review)
            return [
                {
                    "id": supplier,
                    "name": _supplier_name(supplier),
                    "righe": len(records),
                    "ordinabili": sum(1 for record in records if _offer(supplier, record)),
                }
                for supplier, records in sorted(self.righe_per_fornitore.items())
            ]

    def sfoglia(
        self,
        review: dict[str, Any],
        supplier: str,
        *,
        query: str = "",
        da: int = 0,
        quante: int = 50,
        riga: Any = None,
    ) -> dict[str, Any]:
        """A page of one supplier's price list, exactly as the program read it.

        `riga` is the row number to focus on — the one already matched to
        the product the viewer was opened from. When given, `da` is ignored
        and the page returned is the one containing it: opening an
        eight-thousand-row price list at the start, when the target row is
        already known, would just make the user search by hand for
        something the program already knows.

        Discarded rows are always counted, even when the current page shows
        none of them: a price list with thousands of rows that silently
        shows only most of them would be a viewer that hides data.
        """

        chiave = str(supplier or "").strip().casefold()
        with self.lock:
            self._ensure_loaded(review)
            if chiave not in self.righe_per_fornitore:
                raise ValueError(f"Nessun listino caricato per «{supplier}»")
            tutte = self.righe_per_fornitore[chiave]
            normalizzata = _search_text(query)
            if normalizzata:
                parole = normalizzata.split()
                trovate = [
                    record for record in tutte
                    if all(
                        parola in _search_text(
                            f"{record.get('description') or ''} {record.get('ean') or ''} "
                            f"{record.get('supplier_code') or ''}"
                        )
                        for parola in parole
                    )
                ]
            else:
                trovate = list(tutte)
            quante = max(1, min(int(quante or 50), 200))
            posizione = None
            if riga is not None:
                cercata = str(riga)
                posizione = next(
                    (indice for indice, record in enumerate(trovate) if str(record.get("source_row")) == cercata),
                    None,
                )
            da = (posizione // quante) * quante if posizione is not None else max(0, int(da or 0))
            if da >= len(trovate):
                da = max(0, (len(trovate) - 1) // quante * quante) if trovate else 0
            pagina = trovate[da : da + quante]
            return {
                "supplier": chiave,
                "supplierName": _supplier_name(chiave),
                "righe": [self._riga_di_listino(chiave, record) for record in pagina],
                "da": da,
                "quante": quante,
                "trovate": len(trovate),
                "totale": len(tutte),
                "scartate": sum(1 for record in tutte if not _offer(chiave, record)),
                # Where the requested row ended up: `None` when no row was
                # requested, or when the current search filtered it out — in
                # which case the page must say so instead of leaving the
                # caller searching for nothing.
                "rigaCercata": None if riga is None else (
                    str(riga) if posizione is not None else None
                ),
            }

    def offerta_dalla_riga(
        self, review: dict[str, Any], supplier: str, source_row: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The manually chosen row and the offer built from it.

        Returns `(record, offerta)`. Raises if the row doesn't exist or
        isn't orderable: matching a product to a row with no price or no
        pieces-per-carton would mean ordering a quantity that can't be
        computed — the same rejection the service applies to automatic
        match proposals.
        """

        chiave = str(supplier or "").strip().casefold()
        cercata = str(source_row)
        with self.lock:
            self._ensure_loaded(review)
            if chiave not in self.righe_per_fornitore:
                raise ValueError(f"Nessun listino caricato per «{supplier}»")
            record = next(
                (
                    voce for voce in self.righe_per_fornitore[chiave]
                    if str(voce.get("source_row")) == cercata
                ),
                None,
            )
            if record is None:
                raise ValueError(
                    f"Nel listino {_supplier_name(chiave)} non c'è nessuna riga {source_row}: "
                    "il documento è cambiato, riapri il listino."
                )
            offerta = _offer(chiave, record)
            if offerta is None:
                motivo = str(record.get("unusable_reason") or record.get("row_type") or "")
                raise ValueError(
                    f"La riga {source_row} di {_supplier_name(chiave)} non è ordinabile"
                    + (f": {motivo}" if motivo else " (manca il prezzo o i pezzi per collo).")
                )
            return deepcopy(record), offerta

    def get(self, review: dict[str, Any], catalog_id: str) -> dict[str, Any]:
        with self.lock:
            self._ensure_loaded(review)
            item = self.by_id.get(str(catalog_id))
            if not item:
                raise ValueError("Prodotto non trovato nel catalogo")
            return {key: deepcopy(value) for key, value in item.items() if key != "_search"}

    def invalidate(self) -> None:
        with self.lock:
            self.signature = ()
            self.entries = []
            self.by_id = {}
            self.unique_offers = {}
            self.load_errors = []
