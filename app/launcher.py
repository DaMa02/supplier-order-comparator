#!/usr/bin/env python3
"""Launch the local comparator with safe paths and checks, for Windows."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


# These live next to this file, so they import without touching `sys.path`
# (the launcher's own directory is already first). They check whether a
# running server is executing the current sources.
import scrittura_sicura  # noqa: E402
import versione_del_codice  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
SKILL_ROOT = APP_DIR.parent
SERVER_PATH = APP_DIR / "server.py"
INSPECTOR_PATH = SKILL_ROOT / "scripts" / "inspect_sources.py"
REQUIRED_SOURCE_PATHS = (
    SERVER_PATH,
    APP_DIR / "catalog_search.py",
    APP_DIR / "promotion_bridge.py",
    INSPECTOR_PATH,
    SKILL_ROOT / "scripts" / "promotions.py",
)
# Required browser: Chrome, not the Windows default.
# The env var is for tests and non-standard installs.
CHROME_ENV_OVERRIDE = "COMPARATORE_CHROME"
CHROME_RELATIVE_PATH = Path("Google") / "Chrome" / "Application" / "chrome.exe"
# Which suppliers can be compiled isn't listed here: the registry declares it
# via `order_write` on the adapter (see `references/adapters.json` and
# `references/schema-routing.md`). A hardcoded list here would keep a newly
# learned supplier from ever becoming compilable without a code change.
ADAPTERS_PATH = SKILL_ROOT / "references" / "adapters.json"
DATA_DIR = APP_DIR / "data"
CURRENT_DIR = DATA_DIR / "current"
REVIEW_PATH = CURRENT_DIR / "review_data.json"
STATE_PATH = CURRENT_DIR / "state.json"
UPLOAD_DIR = CURRENT_DIR / "uploads"
OUTPUT_DIR = CURRENT_DIR / "outputs"
WRITER_CONFIG_PATH = CURRENT_DIR / "writer_config.json"
WRITER_SCRIPT_PATH = SKILL_ROOT / "scripts" / "write_supplier_orders.mjs"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_ATTEMPTS = 20

# State the recompute doesn't regenerate: lost, it isn't recreated by a
# button press. These paths are intentionally outside git — they change
# while the program runs, and the store PC's startup resets tracked files —
# so they exist only on the store's disk, with no other copy anywhere.
#
# A missing file between installs isn't a bug: a fresh install has neither
# orders nor AI memory yet.
MEMORIE_DA_COPIARE = (
    Path("current") / "state.json",
    # The manual bridge for price lists the app failed to learn. Easy to
    # miss here, but it's a memory like the others: it survives a recompute
    # and "Inizia nuova comparazione" (start new comparison), and for an
    # unlearned supplier it's the only thing that keeps its price list
    # readable. Losing it means rewriting it by hand with no record of what
    # was there.
    Path("current") / "decisioni_schemi.json",
    Path("history") / "conferme.db",
    Path("history") / "orders.json",
    Path("memoria_ai.json"),
    Path("adattatori_imparati.json"),
)
COPIE_DA_TENERE = 10

EMPTY_REVIEW: dict[str, Any] = {
    "run": {
        "id": "current",
        "status": "awaiting_files",
        "createdAt": "",
        "label": "Nuovo confronto",
    },
    "files": [],
    "suppliers": [],
    "products": [],
    "warnings": [],
}


@dataclass(frozen=True)
class NodeRuntime:
    # Node alone is enough: `.xlsx` copies are written by
    # `scripts/lib/xlsx_in_posizione.mjs` using only Node's standard library,
    # no `node_modules`.
    executable: Path
    version: str
    label: str


@dataclass(frozen=True)
class WriterSetup:
    config_path: Path | None
    node_runtime: NodeRuntime | None
    message: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Avvia il comparatore ordini nel browser, solo su questo computer."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Controlla runtime, sintassi e percorsi senza avviare server o browser.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Avvia il server senza aprire automaticamente il browser.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Prima porta locale da provare (predefinita: {DEFAULT_PORT}).",
    )
    return parser.parse_args()


def fail(message: str, *, detail: str | None = None) -> int:
    print(f"\n[ERRORE] {message}", file=sys.stderr)
    if detail:
        print(f"         {detail}", file=sys.stderr)
    return 1


def validate_python() -> None:
    if sys.version_info < (3, 10):
        raise RuntimeError(
            f"serve Python 3.10 o successivo; è stato trovato {sys.version.split()[0]}"
        )
    if importlib.util.find_spec("openpyxl") is None:
        raise RuntimeError(
            "il runtime Python non contiene openpyxl. "
            "Non è stata eseguita alcuna installazione: usa il runtime incluso in Codex."
        )


def validate_source(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"file richiesto non trovato: {path}")
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")


def validate_review(path: Path, *, may_be_missing: bool) -> None:
    if not path.exists():
        if may_be_missing:
            return
        raise RuntimeError(f"file dati non trovato: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"review_data.json non è leggibile: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("review_data.json deve contenere un oggetto JSON")


def read_review(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("review_data.json deve contenere un oggetto JSON")
    return value


def node_version(executable: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = (result.stdout or result.stderr or "").strip()
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+).*", version)
    if result.returncode != 0 or not match or int(match.group(1)) < 18:
        return None
    return version.removeprefix("v")


def find_node_runtime() -> NodeRuntime | None:
    """Return the first Node 18+ found, in the order worth checking.

    The app's bundled runtime first; then the ones Codex's dev runtime
    carries, since on the store PC they were long the only Node present;
    finally the one on PATH. No extra library is needed: the writer only
    uses Node's own standard library.
    """

    user_profile = Path(os.environ.get("USERPROFILE") or Path.home())
    local_value = os.environ.get("LOCALAPPDATA")
    local_app_data = Path(local_value) if local_value else user_profile / "AppData" / "Local"
    primary_root = (
        user_profile
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
    )

    candidates: list[tuple[Path, str]] = [
        (APP_DIR / "runtime" / "node.exe", "runtime portabile dell'app"),
        (primary_root / "bin" / "node.exe", "runtime Node incluso in Codex"),
    ]

    cua_root = local_app_data / "OpenAI" / "Codex" / "runtimes" / "cua_node"
    if cua_root.is_dir():
        cua_nodes = list(cua_root.glob("*/bin/node.exe"))
        cua_nodes.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        candidates.extend((executable, "runtime Node Codex in AppData") for executable in cua_nodes)

    path_node = shutil.which("node")
    if path_node:
        candidates.append((Path(path_node), "node nel PATH"))

    seen: set[str] = set()
    for executable, label in candidates:
        key = str(executable).casefold()
        if key in seen or not executable.is_file():
            continue
        seen.add(key)
        version = node_version(executable)
        if version:
            return NodeRuntime(executable=executable.resolve(), version=version, label=label)
    return None


def _registro_degli_adattatori() -> Any:
    """Import and return the adapter registry engine, lazily.

    Deferred on purpose: the launcher must still start on its own even if
    the `scripts` folder is missing. Reading the registry here with
    `json.loads` would create a second source of truth for the same file:
    `scripts/registro.py` is the only reader of it.
    """

    cartella = str(SKILL_ROOT / "scripts")
    if cartella not in sys.path:
        sys.path.insert(0, cartella)
    import registro  # noqa: PLC0415 - import tardivo voluto

    return registro


def nome_leggibile(supplier: Any) -> str:
    """Return this supplier's name for user-facing messages, per the registry.

    A raw `supplier.upper()` would show a learned supplier as
    `NUOVO_FORNITORE_1`, underscore included, even though the registry
    already has its proper name — the same name this module's
    `display_name` uses in the write rule.

    Falls back to the identifier itself if the registry can't be reached: a
    message with the raw id is better than a launcher that won't start.
    """

    try:
        return _registro_degli_adattatori().nome_del_fornitore(supplier, ADAPTERS_PATH)
    except Exception:  # noqa: BLE001 - a name isn't worth a failed startup
        return str(supplier or "fornitore").upper()


def adattatori_compilabili(percorso: Path | None = None) -> dict[str, dict[str, Any]]:
    """Suppliers the registry declares it knows how to write orders for.

    Compilability is a declared property, not a list in code: an adapter
    that carries `order_write` is compilable, one without it isn't — equally
    true for the six built-in suppliers and any schema learned later. A
    hardcoded tuple here would silently leave a learned supplier
    uncompilable: measured with 102 products assigned to acero and no
    warning raised.
    """

    registro = _registro_degli_adattatori()
    trovati: dict[str, dict[str, Any]] = {}
    for voce in registro.adattatori(percorso or ADAPTERS_PATH):
        fornitore = str(voce.get("supplier_id") or "").strip().casefold()
        if fornitore and registro.scrittura_ordine(voce):
            trovati.setdefault(fornitore, voce)
    return trovati


def adattatore_del_documento(entry: dict[str, Any], supplier: str,
                             per_fornitore: dict[str, dict[str, Any]],
                             percorso: Path | None = None) -> dict[str, Any]:
    """The adapter that writes the order into THIS document.

    It's decided by which adapter read the document, not by the supplier's
    name: picking the first adapter for a supplier can be wrong in two
    cases.

    - A supplier can have more than one schema (e.g. only one of two
      variants declares that the order column header must read ORDINE).
      Picking by supplier alone can silently drop that header check.
    - The order column can be reassigned from the UI, which stores a
      `__locale` entry with the new column. Looking up by supplier instead
      finds the shipped entry and writes to the wrong column.

    Falls back to the supplier lookup for older comparisons, whose entries
    don't declare an adapter: that entry is better than nothing.
    """

    registro = _registro_degli_adattatori()
    dichiarato = str((entry or {}).get("adapterId") or (entry or {}).get("adapter_id") or "").strip()
    if dichiarato:
        voce = registro.voce_in_uso(dichiarato, registro.adattatori(percorso or ADAPTERS_PATH))
        if voce:
            return voce
    return per_fornitore.get(supplier) or {}


def fornitori_compilabili(percorso: Path | None = None) -> tuple[str, ...]:
    """Return supplier names only, longest first.

    Order matters because matching is by substring: matching a shorter name
    before a longer name that contains it would route the document to the
    wrong supplier, and the order would end up sent to whoever isn't
    expecting it.
    """

    return tuple(sorted(adattatori_compilabili(percorso), key=lambda nome: (-len(nome), nome)))


def supplier_key(file_entry: dict[str, Any], compilabili: tuple[str, ...] | None = None) -> str:
    """This document's supplier, if it's one the registry can compile for."""

    values = [
        file_entry.get("supplierId"),
        file_entry.get("supplier_id"),
        file_entry.get("adapterId"),
        file_entry.get("adapter_id"),
        file_entry.get("supplier"),
    ]
    text = " ".join(str(value or "") for value in values).casefold()
    for fornitore in (fornitori_compilabili() if compilabili is None else compilabili):
        if fornitore in text:
            return fornitore
    return ""


def supplier_declared(file_entry: dict[str, Any]) -> str:
    """The supplier this document declares, compilable or not.

    Needed to name the gap: a price list the registry can't compile for
    still needs a name in the warning, or the warning can't be written.
    """

    for chiave in ("supplierId", "supplier_id"):
        valore = str(file_entry.get(chiave) or "").strip()
        if valore:
            return valore.casefold()
    return ""


def resolve_source_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = Path(os.path.expandvars(value.strip())).expanduser()
    candidates = [raw] if raw.is_absolute() else [
        CURRENT_DIR / raw,
        SKILL_ROOT / raw,
        SKILL_ROOT.parent / raw,
    ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.suffix.casefold() in {".xlsx", ".xls"}:
            return resolved
    return None


def review_supplier_sources(review: dict[str, Any]) -> dict[str, Path]:
    compilabili = fornitori_compilabili()
    result: dict[str, Path] = {}
    for entry in review.get("files") or []:
        if not isinstance(entry, dict):
            continue
        supplier = supplier_key(entry, compilabili)
        if not supplier or supplier in result:
            continue
        source = resolve_source_path(
            entry.get("sourcePath")
            or entry.get("source_path")
            or entry.get("originalPath")
            or entry.get("original_path")
            or entry.get("path")
        )
        if source:
            result[supplier] = source
    return result


def review_supplier_entries(review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    compilabili = fornitori_compilabili()
    result: dict[str, dict[str, Any]] = {}
    for entry in review.get("files") or []:
        if not isinstance(entry, dict):
            continue
        supplier = supplier_key(entry, compilabili)
        if supplier and supplier not in result:
            result[supplier] = entry
    return result


def fornitori_non_compilabili(review: dict[str, Any]) -> list[str]:
    """Price lists in the comparison for which the registry declares no write rule.

    Not a failure: the user needs to know this before assigning quantities
    to that supplier, since no order copy will ever be produced for it. A
    supplier silently missing from the compilable list is exactly the gap
    this guards against.
    """

    compilabili = fornitori_compilabili()
    mancanti: list[str] = []
    for entry in review.get("files") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("role") or "").strip().casefold() == "master":
            continue
        dichiarato = supplier_declared(entry)
        if not dichiarato or supplier_key(entry, compilabili):
            continue
        if dichiarato not in mancanti:
            mancanti.append(dichiarato)
    return mancanti


def fornitori_senza_copia(review: dict[str, Any],
                          percorso: Path | None = None) -> dict[str, str]:
    """Every supplier in the comparison that would produce no copy today, with why.

    Two causes, same effect: either the registry declares no `order_write`
    (never compilable), or it does but this week's document doesn't match
    the declaration — a changed header, a missing sheet, rows outside the
    document. The second kind is easy to miss: it's swallowed by
    `prepare_writer_config`'s message, which the server discards once
    writing succeeds, so the failure only surfaces at compile time with no
    stated cause. Call this to warn before quantities are assigned, not the
    registry alone.

    `percorso` exists because the orchestrator can run against a configurable
    registry copy (used in tests): reading one registry while warning about
    another would be wrong in both directions. An unreadable registry
    returns empty; that case is reported separately by
    `registro.motivo_registro_illeggibile`, which the caller must check.
    """

    if _registro_degli_adattatori().motivo_registro_illeggibile(percorso or ADAPTERS_PATH):
        return {}
    adattatori = adattatori_compilabili(percorso)
    compilabili = tuple(sorted(adattatori, key=lambda nome: (-len(nome), nome)))
    esiti: dict[str, str] = {}
    valutati: set[str] = set()
    for entry in review.get("files") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("role") or "").strip().casefold() == "master":
            continue
        dichiarato = supplier_declared(entry)
        chiave = supplier_key(entry, compilabili)
        if not chiave:
            if dichiarato and dichiarato not in esiti:
                esiti[dichiarato] = (
                    f"{dichiarato.upper()} non si può compilare: il registro degli adattatori "
                    "non dichiara come si scrive l'ordine dentro il suo listino."
                )
            continue
        if chiave in valutati:
            continue
        source = resolve_source_path(
            entry.get("sourcePath")
            or entry.get("source_path")
            or entry.get("originalPath")
            or entry.get("original_path")
            or entry.get("path")
        )
        if source is None:
            continue
        valutati.add(chiave)
        rule, warning = source_rule(chiave, source, entry, adattatori.get(chiave))
        if rule is None and warning:
            esiti[chiave] = warning
    return esiti


def fornitori_del_confronto(review: dict[str, Any]) -> list[str]:
    """Suppliers the comparison actually uses, taken from the offers.

    Not from `review["files"]`, which lists documents: the two lists
    diverge as soon as a price list is deleted or replaced. Anything that
    needs to say "no copy will be produced for this supplier" must start
    from here.
    """

    nomi: list[str] = []
    for voce in review.get("suppliers") or []:
        if isinstance(voce, dict):
            nome = str(voce.get("id") or "").strip().casefold()
            if nome and nome not in nomi:
                nomi.append(nome)
    if nomi:
        return sorted(nomi)
    # An older comparison may not carry this list: derive it from the offers.
    for prodotto in review.get("products") or []:
        if not isinstance(prodotto, dict):
            continue
        for offerta in prodotto.get("offers") or []:
            if not isinstance(offerta, dict):
                continue
            nome = str(offerta.get("supplierId") or offerta.get("supplier_id") or "").strip().casefold()
            if nome and nome not in nomi:
                nomi.append(nome)
    return sorted(nomi)


def fornitori_ordinati_senza_copia(
    review: dict[str, Any],
    fornitori: Iterable[str],
    percorso: Path | None = None,
) -> dict[str, str]:
    """Suppliers about to be ordered from that would produce no copy today.

    Differs from `fornitori_senza_copia` on purpose: that one iterates
    documents (`review["files"]`); this one iterates suppliers the user has
    assigned a quantity to. The two lists can diverge — a deleted or
    replaced price list stays in the offers but drops out of the documents —
    and in that case the document-based check has nothing to say, since its
    warnings are only raised inside the loop over resolved documents.

    Measured on a live dataset: four suppliers with offers and assigned
    quantities, one listed document whose path had disappeared.
    `writer_readiness` reported ready with zero rules and zero warnings, and
    compiling produced a plan with no copy to send.

    Whatever decides if compiling is possible should call this, with the
    suppliers actually in the order.
    """

    richiesti = [str(nome or "").strip().casefold() for nome in fornitori]
    richiesti = sorted({nome for nome in richiesti if nome})
    if not richiesti:
        return {}

    # A registry that fails to open isn't a registry that "declares
    # nothing": we know nothing about anyone, so nothing compiles. Failing
    # closed here is the only honest answer.
    motivo_registro = _registro_degli_adattatori().motivo_registro_illeggibile(
        percorso or ADAPTERS_PATH
    )
    if motivo_registro:
        return {
            nome: (
                f"{nome.upper()}: non si riesce a leggere il registro degli adattatori, "
                f"quindi non si può preparare nessuna copia. {motivo_registro}"
            )
            for nome in richiesti
        }

    adattatori = adattatori_compilabili(percorso)
    # Not using `review_supplier_sources`/`review_supplier_entries` here:
    # those call `fornitori_compilabili()` without a path, always reading
    # the default registry. The orchestrator can run against a configurable
    # registry (a copy, in tests), and reading one while warning on the
    # other would be wrong in both directions.
    compilabili = tuple(sorted(adattatori, key=lambda nome: (-len(nome), nome)))
    sorgenti: dict[str, Path] = {}
    voci: dict[str, dict[str, Any]] = {}
    for entry in review.get("files") or []:
        if not isinstance(entry, dict):
            continue
        chiave = supplier_key(entry, compilabili)
        if not chiave or chiave in voci:
            continue
        voci[chiave] = entry
        trovata = resolve_source_path(
            entry.get("sourcePath")
            or entry.get("source_path")
            or entry.get("originalPath")
            or entry.get("original_path")
            or entry.get("path")
        )
        if trovata is not None:
            sorgenti[chiave] = trovata

    esiti: dict[str, str] = {}
    for nome in richiesti:
        dichiarazione = adattatori.get(nome)
        if dichiarazione is None:
            esiti[nome] = (
                f"{nome.upper()} non si può compilare: il registro degli adattatori non "
                "dichiara come si scrive l'ordine dentro il suo listino."
            )
            continue
        source = sorgenti.get(nome)
        if source is None:
            # An easy-to-miss case: the supplier is compilable in
            # the abstract, but this week's document is missing — never
            # uploaded, deleted after the comparison, or with a path that no
            # longer resolves.
            voce = voci.get(nome)
            if voce is None and not (review.get("files") or []):
                # "Unknown" isn't "missing". A comparison that carries no
                # document list at all (synthetic test data, older formats)
                # doesn't license saying suppliers are missing: stay silent,
                # the same way `base_review` does for the same reason. When
                # the list exists and this supplier is absent from it,
                # that's the real divergence this check is meant to catch.
                continue
            if voce is None:
                esiti[nome] = (
                    f"{nome.upper()}: il suo listino non è fra i documenti caricati, quindi "
                    "non c'è niente su cui scrivere l'ordine. Ricarica il listino e rifai il "
                    "confronto."
                )
            else:
                dichiarato = str(
                    voce.get("sourcePath")
                    or voce.get("source_path")
                    or voce.get("originalPath")
                    or voce.get("original_path")
                    or voce.get("path")
                    or ""
                )
                dove = f" ({dichiarato})" if dichiarato else ""
                esiti[nome] = (
                    f"{nome.upper()}: il file del suo listino non si trova più{dove}. "
                    "Ricaricalo e rifai il confronto."
                )
            continue
        _regola, avviso = source_rule(nome, source, voci.get(nome, {}), dichiarazione)
        if _regola is None:
            esiti[nome] = avviso or (
                f"{nome.upper()}: il suo listino non combacia con quello che il registro "
                "dichiara, quindi la copia dell'ordine non si può preparare."
            )
    return esiti


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workbook_sheet_names(path: Path) -> list[str]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def formato_del_contenitore(path: Path) -> str:
    """Return "xls", "xlsx" or "csv", read from the file's first bytes.

    The format is decided by the bytes, not the extension — as noted for
    noce in the registry — and `scripts/inspect_sources.py` knows how to
    read them. Imported here rather than duplicated, since two signature
    lists would drift apart, and the one that isn't updated is always the
    one that opens the file with the wrong reader. Falls back to the
    extension if that folder is missing: worse than nothing, but not a
    failure.
    """

    cartella = str(SKILL_ROOT / "scripts")
    if cartella not in sys.path:
        sys.path.insert(0, cartella)
    try:
        from inspect_sources import container_format  # noqa: PLC0415 - import tardivo voluto
    except Exception:  # noqa: BLE001 - fall back without `scripts`, don't crash
        return path.suffix.casefold().lstrip(".") or "csv"
    return container_format(path)


def numero_di_colonna(lettere: str) -> int:
    """`"G"` -> `7`. Which column to check is declared by the registry, not the code."""

    indice = 0
    for lettera in lettere.upper():
        indice = indice * 26 + (ord(lettera) - ord("A") + 1)
    return indice


def _intero(valore: Any) -> int | None:
    return valore if isinstance(valore, int) and not isinstance(valore, bool) else None


def regola_noce(source: Path, mapping: dict[str, Any], order_column: str, header_row: int,
                    data_start_row: int, etichetta: str) -> tuple[dict[str, Any] | None, str | None]:
    """The `.xls` price list is patched in place, four bytes per cell.

    The EAN column stays declared by name: `cat` and `Iva` are real column
    names in this price list that are also valid Excel column references,
    so falling back to letters would silently read the wrong column. The
    writer resolves it by reading the file's header row.
    """

    colonne = mapping.get("columns")
    nome_ean = colonne.get("ean") if isinstance(colonne, dict) else None
    if not nome_ean:
        return None, f"{etichetta} non attivato: il registro non dichiara dove sta l'EAN"
    return {
        "sheet": str(mapping.get("sheet") or "") or None,
        "order_column": order_column,
        "data_start_row": data_start_row,
        "header_row": header_row,
        "ean_column_name": nome_ean,
        "source_sha256": sha256_file(source),
        # The document is compiled in place, not via Node: stated here so
        # whatever reads the config knows without inferring it from the
        # extension.
        "compilazione": "patch_xls_in_posizione",
    }, None


def _colonna_dichiarata(dichiarata: Any, intestazione: list[Any]) -> str | None:
    """Return the column letter for a registry declaration.

    The registry states a field's position in one of three ways depending
    on the supplier — letter, number, or header name — all equally valid.
    A name can only be resolved against the document's own header: without
    it, this doesn't guess and returns `None`.
    """

    # Deferred like the other openpyxl imports in this file: the launcher
    # must be able to start and report that it's missing (see the check at
    # the top), not die with an ImportError before it can explain.
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    if dichiarata in (None, ""):
        return None
    if isinstance(dichiarata, bool):
        return None
    if isinstance(dichiarata, (int, float)) and float(dichiarata).is_integer() and int(dichiarata) >= 1:
        return get_column_letter(int(dichiarata))
    testo = str(dichiarata).strip()
    if re.fullmatch(r"\d+", testo) and int(testo) >= 1:
        return get_column_letter(int(testo))
    if re.fullmatch(r"[A-Za-z]{1,3}", testo) and not intestazione:
        return testo.upper()
    atteso = _registro_degli_adattatori().normalizza(testo)
    for indice, valore in enumerate(intestazione, start=1):
        if valore in (None, ""):
            continue
        if _registro_degli_adattatori().normalizza(valore) == atteso:
            return get_column_letter(indice)
    # No matching header: if the declaration was already a letter, use it;
    # otherwise it's unknown, and nothing is guessed.
    if re.fullmatch(r"[A-Za-z]{1,3}", testo):
        return testo.upper()
    return None


def _colonne_da_verificare(
    adattatore: dict[str, Any] | None,
    mappatura: dict[str, Any] | None,
    intestazione: list[Any],
) -> dict[str, str]:
    """Return where the writer can verify a row is the right one.

    Only what the registry (or the confirmed mapping) already declares: the
    EAN identifies the item exactly, the description merely recognizes it.
    Two checks with two different severities, known to whoever uses them.
    """

    motore = _registro_degli_adattatori()
    trovate: dict[str, str] = {}
    for campo, chiave in (("ean", "ean_column"), ("description", "description_column")):
        dichiarata = motore.posizione_del_campo(adattatore or {}, mappatura or {}, campo)
        colonna = _colonna_dichiarata(dichiarata, intestazione)
        if colonna:
            trovate[chiave] = colonna
    return trovate


def _riga_dell_intestazione(
    source: Path, sheet_name: str, order_column: str, atteso: str
) -> int | None:
    """Return the row where the order column carries the expected text, if unique.

    Returns `None` when it's absent or found twice: in either case, guessing
    would mean writing quantities into a spot nobody looked at, so the
    caller keeps the declared numbers and lets the check below reject.

    Only the first rows are scanned: a header sits at the top, and reading
    the whole sheet read-only costs the same five-minute stall that once
    made startup look hung.
    """

    from openpyxl import load_workbook  # noqa: PLC0415

    cercato = " ".join(str(atteso).split()).casefold()
    indice = numero_di_colonna(order_column)
    if not cercato or not indice:
        return None
    trovate: list[int] = []
    try:
        workbook = load_workbook(source, read_only=True, data_only=True)
        try:
            foglio = workbook[sheet_name]
            for numero, riga in enumerate(
                foglio.iter_rows(min_row=1, max_row=40, values_only=True), start=1,
            ):
                valore = riga[indice - 1] if len(riga) >= indice else None
                if " ".join(str(valore or "").split()).casefold() == cercato:
                    trovate.append(numero)
        finally:
            workbook.close()
    except Exception:  # noqa: BLE001 - non sapere dov'e' non e' un guasto
        return None
    return trovate[0] if len(trovate) == 1 else None


def source_rule(supplier: str, source: Path, entry: dict[str, Any],
                adattatore: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """The rule for writing an order into this supplier's price list.

    Every number and letter comes from the registry — `order_write` on the
    adapter, completed by `field_mapping` when the adapter has one — never
    from an `if supplier == "..."` branch. Hardcoding a supplier's column in
    code would keep a learned supplier from ever becoming compilable. This
    function adds only what the registry can't know: today's actual sheet
    name, its fingerprint, and proof that the declared header sits where
    it's declared.

    "FIRST" means two different things depending on who states it, on
    purpose. In `order_write` it's the registry's choice — someone who
    looked at that price list — meaning "the document's first sheet". In a
    `field_mapping` it means the sheet wasn't identified, so the document
    must have exactly one: picking the first of several would write the
    order into a sheet nobody looked at.
    """

    # All-caps like other messages in this program: `capitalize()` turned
    # "Noce" into "Noce", a spelling that belongs to no one.
    etichetta = nome_leggibile(supplier)
    if not isinstance(adattatore, dict):
        adattatore = adattatori_compilabili().get(supplier) or {}
    dichiarazione = adattatore.get("order_write")
    if not isinstance(dichiarazione, dict) or not dichiarazione:
        return None, (
            f"{etichetta} non si può compilare: il registro degli adattatori non dichiara "
            "come si scrive l'ordine dentro il suo listino."
        )

    da_mappatura = bool(dichiarazione.get("from_field_mapping"))
    incompleta = (f"{etichetta} non attivato: la mappatura verificata deve indicare foglio, righe, "
                  f"campi e colonna {str(dichiarazione.get('order_column') or '').strip().upper()}")
    # This is the comparison's mapping, not the registry's: it's what the
    # user actually saw, measured against today's document. Falling back to
    # the registry's mapping would write the order against a different
    # document than the one being compiled.
    mapping = entry.get("fieldMapping") or entry.get("field_mapping")
    if not isinstance(mapping, dict):
        mapping = {}
    if da_mappatura and not mapping:
        return None, f"{etichetta} non attivato: manca la mappatura confermata della colonna ordine"

    dichiarata = str(dichiarazione.get("order_column") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", dichiarata):
        return None, f"{etichetta} non attivato: il registro non dichiara la colonna d'ordine del suo listino"
    if da_mappatura and str(mapping.get("order_column") or mapping.get("orderColumn") or "").strip().upper() != dichiarata:
        # The registry declares the column; the confirmed mapping must agree.
        # If they name different columns the order would land in a cell no
        # one verified.
        return None, incompleta
    order_column = dichiarata

    # Rows and sheet: declared by whatever reads the document. For adapters
    # with a mapping, that's the mapping the user confirmed; for adapters
    # with a dedicated reader, it's `order_write`.
    origine = mapping if da_mappatura else dichiarazione
    header_row = _intero(origine.get("header_row") or origine.get("headerRow"))
    data_start_row = _intero(origine.get("data_start_row") or origine.get("dataStartRow"))
    colonne = mapping.get("columns") if isinstance(mapping.get("columns"), dict) else {}
    mancanti = [str(nome) for nome in (dichiarazione.get("required_columns") or []) if nome not in colonne]
    if mancanti:
        # The real cause is missing columns, not rows: an earlier message
        # pointed at `data_start_row` when the actual problem was a
        # different declaration.
        if da_mappatura:
            return None, incompleta
        return None, (f"{etichetta} non attivato: il registro chiede le colonne "
                      + ", ".join(sorted(mancanti))
                      + " ma il documento non ha una mappatura confermata che le porti")
    atteso = str(dichiarazione.get("expected_header") or "").strip()
    # The confirmation "that cell is blank, and that's fine" can come from
    # two places, both routes to the same human decision: the confirmed
    # mapping in the preview (suppliers with `from_field_mapping`), or
    # `order_write` itself, when the order column was chosen from the UI for
    # a supplier with a dedicated reader and no mapping. Without the second
    # route, moving that supplier's order column onto an untitled column
    # would disable the check instead of enabling it: a missing
    # `expected_header` means "nothing to verify", so the quantity could
    # land in a cell no one looked at. Either way, what's declared is still
    # checked against the document below: the cell must actually still be
    # blank, with no text or formulas beneath it.
    intestazione_vuota_confermata = (
        dichiarazione.get("allow_blank_header_if_confirmed") is True
        and (
            mapping.get("order_header_blank_confirmed") is True
            or dichiarazione.get("order_header_blank_confirmed") is True
        )
    )
    if data_start_row is None or data_start_row < 2 or (
        (atteso or intestazione_vuota_confermata) and (header_row is None or header_row < 1)
    ):
        if da_mappatura:
            return None, incompleta
        return None, (f"{etichetta} non attivato: il registro non dichiara le righe da cui parte "
                      "l'ordine nel suo listino")

    procedura = str(dichiarazione.get("mode") or "").strip()
    if procedura == "patch_xls_in_posizione":
        if header_row is None or header_row < 1:
            return None, f"{etichetta} non attivato: la mappatura non dichiara le righe di intestazione e dati"
        return regola_noce(source, mapping, order_column, header_row, data_start_row, etichetta)
    if procedura:
        # Without this, a typo in the declared procedure would silently
        # fall through to the default path: visible by chance on an `.xls`
        # (openpyxl refuses it), invisible on an `.xlsx`.
        return None, (f"{etichetta} non attivato: il registro dichiara una procedura di "
                      f"scrittura sconosciuta («{procedura}»)")

    # From here on the document opens with openpyxl, which knows nothing
    # about `.xls`. Without this guard the user would see openpyxl's raw
    # error message embedded in "can't read the price list", which reads as
    # "it opens fine but can't be written to". The real fix is one Excel
    # step, stated here: `.xls` only compiles in-place where the registry
    # declares that procedure, which requires an order column that's already
    # fully numeric (as with noce).
    formato = formato_del_contenitore(source)
    if formato != "xlsx":
        com_e = {"xls": "un Excel 97-2003 (.xls)"}.get(formato, "un file che non è un .xlsx")
        return None, (
            f"{etichetta} non attivato: il suo listino è {com_e} e l'ordine si può scrivere "
            "solo dentro un .xlsx. Aprilo con Excel e salvalo come «Cartella di lavoro di "
            "Excel (.xlsx)», poi ricaricalo."
        )

    try:
        sheet_names = workbook_sheet_names(source)
    except Exception as exc:
        return None, f"Non riesco a leggere il listino {nome_leggibile(supplier)}: {exc}"
    if not sheet_names:
        return None, f"Il listino {nome_leggibile(supplier)} non contiene fogli utilizzabili"

    richiesto = str(origine.get("sheet") or origine.get("sheet_name") or "").strip()
    if not richiesto or richiesto.casefold() == "first":
        # "FIRST" declared in the registry is a deliberate choice by
        # whoever reviewed that price list, meaning "the first sheet". A
        # sheet name that's simply absent is not a choice: picking the
        # first of several would write the order into a sheet nobody looked
        # at. From a mapping the single-sheet rule always applies, since
        # there "FIRST" marks an unidentified sheet.
        scelto_dal_registro = bool(richiesto) and not da_mappatura
        if not scelto_dal_registro and len(sheet_names) != 1:
            chi = "la mappatura" if da_mappatura else "il registro"
            return None, (f"{etichetta} non attivato: il listino ha più fogli e "
                          f"{chi} non indica quello esatto")
        sheet_name = sheet_names[0]
    else:
        sheet_name = richiesto
    if not sheet_name or sheet_name not in sheet_names:
        dove = "nella mappatura" if da_mappatura else "nel registro"
        return None, (f"{etichetta} non attivato: il foglio indicato {dove} "
                      "non coincide con il listino")

    # Locating the header when the registry says to search for it instead
    # of declaring its row. Needed for price lists without a real header
    # row: above the offers there are blank rows and just the word ORDINE,
    # and how many blank rows there are varies month to month. Reading
    # already tracks this via a `data_start_marker` recalculated each run;
    # without matching that here, writing would use a stale frozen row
    # number.
    #
    # If the word isn't found, or appears more than once, this doesn't
    # guess: it keeps the declared numbers and lets the check below reject,
    # which is the correct outcome.
    if dichiarazione.get("expected_header_search") is True and atteso:
        trovata = _riga_dell_intestazione(source, sheet_name, order_column, atteso)
        if trovata is not None:
            scarto = data_start_row - header_row if (header_row and data_start_row) else 1
            header_row = trovata
            data_start_row = trovata + max(1, scarto)

    regola: dict[str, Any] = {
        "sheet": sheet_name,
        "order_column": order_column,
        "data_start_row": data_start_row,
        "source_sha256": sha256_file(source),
        # The display name travels with the rule: the Node writer never
        # reads the registry — the config is its only declared input — so
        # without this it would print the raw identifier, underscore
        # included, in messages and order file names.
        "display_name": _registro_degli_adattatori().nome_del_fornitore(supplier, ADAPTERS_PATH),
    }
    if header_row is not None and header_row >= 1:
        regola["header_row"] = header_row
    if header_row is not None and header_row >= 1 and data_start_row <= header_row:
        return None, (f"{etichetta} non attivato: le righe dichiarate non stanno insieme "
                      f"(intestazione alla riga {header_row}, dati dalla riga {data_start_row})")

    # The document is opened even when there's no header to verify: the
    # declared rows come from a file a person edits by hand, and a single
    # typo'd digit would silently write quantities outside the data range.
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(source, read_only=True, data_only=False)
        try:
            foglio = workbook[sheet_name]
            ultima_riga = foglio.max_row
            numero_colonna_ordine = numero_di_colonna(order_column)
            # On a sheet opened read-only, `foglio.cell(r, c)` isn't a plain
            # access: openpyxl re-reads the sheet from the start on every
            # call. Calling it once per price-list row measured 3382 calls
            # on cipresso — 5.7 million rows scanned — and the launch hung
            # silently for five minutes. Now the sheet is walked exactly
            # once, taking what's needed along the way.
            intestazione = None
            valori_intestazione: list[Any] = []
            contenuto_inatteso = None
            serve_intestazione = header_row is not None and header_row >= 1
            ultima_da_leggere = ultima_riga if intestazione_vuota_confermata else (
                header_row if serve_intestazione else 0
            )
            for numero_riga, riga in enumerate(
                foglio.iter_rows(min_row=1, max_row=ultima_da_leggere or 1, values_only=True),
                start=1,
            ):
                if serve_intestazione and numero_riga == header_row:
                    valori_intestazione = list(riga)
                    if atteso or intestazione_vuota_confermata:
                        intestazione = (
                            riga[numero_colonna_ordine - 1]
                            if len(riga) >= numero_colonna_ordine else None
                        )
                if not intestazione_vuota_confermata or numero_riga < data_start_row:
                    continue
                valore = (
                    riga[numero_colonna_ordine - 1]
                    if len(riga) >= numero_colonna_ordine else None
                )
                if valore in (None, "") or (
                    isinstance(valore, (int, float)) and not isinstance(valore, bool)
                ):
                    continue
                contenuto_inatteso = numero_riga
                break
        finally:
            workbook.close()
    except Exception as exc:
        return None, (f"{etichetta} non attivato: non riesco a verificare il foglio "
                      f"{sheet_name} ({exc})")
    # Where the writer can verify that the destination row really carries
    # the planned product, generalizing a check available elsewhere only
    # for noce; nothing is invented here, only what the registry already
    # declares.
    verifica = _colonne_da_verificare(adattatore, mapping, valori_intestazione)
    if verifica:
        regola["verify"] = verifica
    if isinstance(ultima_riga, int) and ultima_riga >= 1:
        if header_row is not None and header_row > ultima_riga:
            return None, (f"{etichetta} non attivato: l'intestazione dichiarata alla riga "
                          f"{header_row} sta fuori dal foglio, che finisce alla riga "
                          f"{ultima_riga}")
        if data_start_row > ultima_riga:
            return None, (f"{etichetta} non attivato: i dati dichiarati dalla riga "
                          f"{data_start_row} stanno fuori dal foglio, che finisce alla "
                          f"riga {ultima_riga}")
    if intestazione_vuota_confermata:
        if str(intestazione or "").strip():
            return None, (
                f"{etichetta} non attivato: la cella {order_column} dell'intestazione "
                "non è più vuota come nella mappatura confermata"
            )
        if contenuto_inatteso is not None:
            return None, (
                f"{etichetta} non attivato: la colonna d'ordine contiene testo o formule "
                f"alla riga {contenuto_inatteso} e non può essere sovrascritta"
            )
        regola["blank_header_confirmed"] = True
        return regola, None
    if not atteso:
        return regola, None
    if " ".join(str(intestazione or "").replace(" ", " ").split()).upper() != atteso.upper():
        return None, (f"{etichetta} non attivato: la cella {order_column} dell'intestazione non "
                      f"contiene {atteso}")
    regola["expected_header"] = atteso
    return regola, None


def writer_readiness(
    review: dict[str, Any],
) -> tuple[NodeRuntime | None, dict[str, Path], dict[str, dict[str, Any]], list[str], list[str]]:
    node = find_node_runtime()
    sources = review_supplier_sources(review)
    entries = review_supplier_entries(review)
    rules: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    missing: list[str] = []
    if node is None:
        missing.append("Node 18+")
    if not WRITER_SCRIPT_PATH.is_file():
        missing.append(str(WRITER_SCRIPT_PATH))
    # Price lists in the comparison the registry can't compile: reported
    # here, where it's decided who enters the write configuration. Without
    # this they'd disappear before ever being named.
    motivo_registro = _registro_degli_adattatori().motivo_registro_illeggibile(ADAPTERS_PATH)
    if motivo_registro:
        # A registry that fails to open isn't one that "declares nothing":
        # a per-supplier message here would send someone looking for a
        # declaration inside a broken file.
        warnings.append(
            "Il registro degli adattatori non si legge, quindi nessun fornitore "
            "risulta compilabile e nessuna copia d'ordine nascerà. " + motivo_registro
        )
    else:
        for fornitore in fornitori_non_compilabili(review):
            warnings.append(
                f"{nome_leggibile(fornitore)} non si può compilare: il registro degli adattatori non dichiara "
                "come si scrive l'ordine dentro il suo listino. Il confronto lo tiene, ma per quel "
                "fornitore non nascerà nessuna copia da mandare."
            )
    adattatori = adattatori_compilabili()
    usable_sources: dict[str, Path] = {}
    for supplier, source in sources.items():
        entry = entries.get(supplier, {})
        rule, warning = source_rule(supplier, source, entry,
                                    adattatore_del_documento(entry, supplier, adattatori))
        if rule is None:
            if warning:
                warnings.append(warning)
            continue
        usable_sources[supplier] = source
        rules[supplier] = rule

    # The warnings above are raised inside the loop over resolved sources.
    # A supplier whose price list has gone missing — never uploaded,
    # deleted after the comparison, an unresolvable path — never enters
    # that loop, so nothing names it: zero sources means zero warnings.
    # Measured on a live dataset: four suppliers with offers, one listed
    # document, and `writer_readiness` reporting zero rules and zero
    # warnings.
    #
    # Start from the suppliers the comparison actually uses, not from the
    # documents.
    mancanti = [
        fornitore for fornitore in fornitori_del_confronto(review)
        if fornitore not in sources
    ]
    if mancanti:
        for _fornitore, motivo in sorted(
            fornitori_ordinati_senza_copia(review, mancanti).items()
        ):
            if motivo not in warnings:
                warnings.append(motivo)

    return node, usable_sources, rules, missing, warnings


def prepare_writer_config(
    review: dict[str, Any], *, write: bool, destinazione: Path | None = None
) -> WriterSetup:
    """Build the write configuration from the live comparison.

    `destinazione` exists for the orchestrator: after a recompute, price
    lists are different files with different names and rows, and a stale
    config would compile last week's list using today's row numbers. The
    launcher writes it where it always has; the orchestrator writes it
    wherever the server asks, which in tests isn't the real folder.
    """

    percorso = Path(destinazione) if destinazione is not None else WRITER_CONFIG_PATH
    node, sources, rules, missing, warnings = writer_readiness(review)
    if missing:
        return WriterSetup(
            config_path=None,
            node_runtime=node,
            message="Writer XLSX non attivato; manca: " + ", ".join(missing),
        )

    assert node is not None
    run_id = str((review.get("run") or {}).get("id") or "")
    config = {
        "schema_version": 1,
        "managed_by": "app/launcher.py",
        "run_id": run_id,
        "node_executable": str(node.executable),
        "writer_script": str(WRITER_SCRIPT_PATH.resolve()),
        "working_directory": str((SKILL_ROOT / "scripts").resolve()),
        "supplier_files": {supplier: str(source) for supplier, source in sources.items()},
        "supplier_write_rules": rules,
    }
    if write:
        # Two different writers touch this file, not two threads: the
        # launcher process, which passes through on every startup — even
        # while the service is already running a recompute — and the
        # service's own pipeline, when it reconfigures writing at the end of
        # a recompute. With a fixed temporary name the two could overlap,
        # letting the published file take `run_id` from one write and
        # `supplier_files` from the other: the guard that compares run ids
        # would then approve exactly the mismatch it exists to catch, and
        # the writer would apply today's row numbers to last week's price
        # list. Triggered by double-clicking the launcher while a recompute
        # is already in progress.
        scrittura_sicura.scrivi_json(percorso, config)

    # "Ready" with zero rules isn't ready: it's a configuration that will
    # compile nothing, and calling it ready hid that gap for a while. The
    # count of writable suppliers is stated explicitly, so a zero is
    # visible.
    if rules:
        quanti = len(rules)
        testa = (
            f"Writer XLSX pronto con {node.label} {node.version}: "
            f"{quanti} {'listino' if quanti == 1 else 'listini'} da compilare"
        )
    else:
        testa = (
            f"Writer XLSX avviabile con {node.label} {node.version}, ma nessun listino "
            "risulta compilabile: non nascerà nessuna copia d'ordine"
        )
    return WriterSetup(
        config_path=percorso if write else None,
        node_runtime=node,
        message=testa + (". " + " ".join(warnings) if warnings else ""),
    )


def ensure_local_layout() -> None:
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not REVIEW_PATH.exists():
        scrittura_sicura.scrivi_json(REVIEW_PATH, EMPTY_REVIEW)


def application_health(port: int, *, timeout: float = 0.35) -> bool:
    return salute(port, timeout=timeout) is not None


def salute(port: int, *, timeout: float = 0.35) -> dict[str, Any] | None:
    """Return `/api/health`'s response, or `None` if the app doesn't respond.

    Kept separate from `application_health` because knowing that something
    responds isn't enough: it matters which program it is, and
    `firmaDelCodice` says so. A server older than this signature won't have
    it, and its absence is itself the answer: it's definitely old.
    """

    request = urllib.request.Request(
        f"http://{HOST}:{port}/api/health",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read().decode("utf-8"))
            if not isinstance(body, dict) or body.get("ok") is not True:
                return None
            return body
    except (OSError, ValueError, urllib.error.URLError):
        return None


def e_lo_stesso_programma(risposta: dict[str, Any], firma_del_disco: str) -> bool:
    """Is the responding server running today's sources?

    A server without `firmaDelCodice` predates this check: treated as
    different, which is both the cautious and the correct answer.
    """

    dichiarata = risposta.get("firmaDelCodice")
    return isinstance(dichiarata, str) and bool(dichiarata) and dichiarata == firma_del_disco


def chiedi_di_spegnersi(port: int, *, timeout: float = 6.0) -> tuple[bool, str]:
    """Ask the server to stop and wait until it stops responding.

    Returns `(stopped, reason)`. The reason matters: if a run is in
    progress the service refuses, and the caller must report that instead
    of retrying — killing a pipeline mid-run throws away AI work already
    paid for.
    """

    request = urllib.request.Request(
        f"http://{HOST}:{port}/api/spegni",
        data=b"{}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            corpo = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, "SENZA_SPEGNIMENTO"
        return False, "RIFIUTATO"
    except (OSError, ValueError, urllib.error.URLError):
        return False, "IRRAGGIUNGIBILE"

    if isinstance(corpo, dict) and corpo.get("spento") is not True:
        return False, str(corpo.get("motivo") or "RIFIUTATO")

    scadenza = time.monotonic() + timeout
    while time.monotonic() < scadenza:
        if salute(port, timeout=0.3) is None:
            return True, ""
        time.sleep(0.15)
    return False, "NON_SI_E_FERMATO"


def port_is_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((HOST, port))
        return True
    except OSError:
        return False


def select_port(first_port: int) -> tuple[int, bool]:
    """Return the port to use, and whether an existing server can be reused.

    A healthy server alone isn't enough to be reused: it must also be
    running today's sources. If responding were the only check, reopening
    the launcher after an update would silently reattach to the old server
    instead of restarting it.

    If the signature doesn't match, the server is asked to stop and the same
    port is reused, never the next one: running on the next port would leave
    two live services on the same data, which is worse than stale code —
    stale code is at least consistent with itself.
    """

    if not 1 <= first_port <= 65535:
        raise RuntimeError("la porta deve essere compresa tra 1 e 65535")
    firma_attuale = versione_del_codice.firma_del_disco()
    last_port = min(65535, first_port + PORT_ATTEMPTS - 1)
    for port in range(first_port, last_port + 1):
        risposta = salute(port)
        if risposta is not None:
            if e_lo_stesso_programma(risposta, firma_attuale):
                return port, True
            print(
                "[AVVIO] Il programma era acceso con una versione precedente: "
                "lo spengo e lo riavvio."
            )
            spento, motivo = chiedi_di_spegnersi(port)
            if spento:
                return port, False
            if motivo == "RUN_IN_CORSO":
                print(
                    "[AVVISO] C'è un confronto in corso: si continua con il programma "
                    "già acceso. Quando ha finito, chiudi e riapri per avere la "
                    "versione nuova."
                )
                return port, True
            raise RuntimeError(
                "c'è un programma più vecchio acceso sulla porta "
                f"{port} e non si è potuto spegnere ({motivo}). Chiudi la finestra "
                "del comparatore, oppure termina il processo, e riprova"
            )
        if port_is_free(port):
            return port, False
    raise RuntimeError(
        f"nessuna porta libera tra {first_port} e {last_port}; chiudi un'app locale e riprova"
    )


def server_command(port: int, writer_config: Path | None) -> list[str]:
    command = [
        sys.executable,
        str(SERVER_PATH),
        "--review",
        str(REVIEW_PATH),
        "--state",
        str(STATE_PATH),
        "--uploads",
        str(UPLOAD_DIR),
        "--output-dir",
        str(OUTPUT_DIR),
        # Compiled order files: passed explicitly so the destination is
        # stated here rather than left to the service's own default.
        "--orders-dir",
        str(CURRENT_DIR / "ordini"),
        "--host",
        HOST,
        "--port",
        str(port),
    ]
    if writer_config is not None:
        command.extend(["--writer-config", str(writer_config)])
    return command


def chrome_candidates() -> list[Path]:
    roots = (
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    )
    return [Path(root) / CHROME_RELATIVE_PATH for root in roots if root]


def find_chrome() -> Path | None:
    """Return Chrome's path, or None if it isn't installed on this machine."""

    override = os.environ.get(CHROME_ENV_OVERRIDE, "").strip()
    if override:
        chosen = Path(override)
        return chosen if chosen.is_file() else None
    for candidate in chrome_candidates():
        if candidate.is_file():
            return candidate
    located = shutil.which("chrome")
    return Path(located) if located else None


def open_application(url: str) -> None:
    # Opens in Chrome because that's the user's chosen browser: the Windows
    # default here is Edge. Falls back to the system default instead of
    # blocking startup if Chrome is missing: a page open elsewhere beats no
    # page at all.
    chrome = find_chrome()
    if chrome is not None:
        print(f"Apro Chrome: {url}")
        try:
            subprocess.Popen(
                [str(chrome), url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError as exc:
            print(f"[AVVISO] Chrome non si è aperto ({exc}): provo con il programma predefinito.")
    else:
        print("[AVVISO] Chrome non è stato trovato: apro il programma di navigazione predefinito.")

    print(f"Apro il browser predefinito: {url}")
    try:
        opened = webbrowser.open(url, new=2)
    except webbrowser.Error as exc:
        opened = False
        print(f"[AVVISO] Il browser non è stato aperto automaticamente: {exc}")
    if not opened:
        print(f"[AVVISO] Apri manualmente questo indirizzo: {url}")


def wait_until_ready(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"il server si è chiuso durante l'avvio (codice {return_code})"
            )
        if application_health(port, timeout=0.5):
            return
        time.sleep(0.15)
    raise RuntimeError("il server non ha risposto entro 12 secondi")


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run_check() -> int:
    try:
        validate_python()
        validate_source(Path(__file__).resolve())
        for source_path in REQUIRED_SOURCE_PATHS:
            validate_source(source_path)
        validate_review(REVIEW_PATH, may_be_missing=True)
        review = read_review(REVIEW_PATH) if REVIEW_PATH.is_file() else EMPTY_REVIEW
        writer_setup = prepare_writer_config(review, write=False)
    except RuntimeError as exc:
        return fail("Controllo non superato.", detail=str(exc))

    print("[OK] Controllo avvio completato; server e browser non sono stati aperti.")
    print(f"     Python: {sys.executable} ({sys.version.split()[0]})")
    print(f"     Server: {SERVER_PATH}")
    print(f"     Dati:   {REVIEW_PATH}")
    print(f"     Stato:  {STATE_PATH}")
    print(f"     Upload: {UPLOAD_DIR}")
    print(f"     Output: {OUTPUT_DIR}")
    print(f"     Writer: {writer_setup.message}")
    print(f"     Copie:  {cartella_delle_copie()}")
    for avviso in avvisi_del_controllo(writer_setup):
        print(f"[AVVISO] {avviso}")
    return 0


def avvisi_del_controllo(writer_setup: WriterSetup) -> list[str]:
    """Things that don't block startup but do block the work.

    Without these, `--check` prints `[OK]` and exits zero even when price
    lists can't be compiled, the port is taken by another program, or the
    backup folder isn't writable. This is the command suggested to anyone
    who "can't get it to start": saying everything is fine on a machine
    where the work can't be finished is worse than not answering.

    These stay warnings, not failures — startup still succeeds — so the
    exit code doesn't change; what changes is that they're visible.
    """

    avvisi: list[str] = []

    if writer_setup.config_path is None:
        avvisi.append(
            f"la compilazione dei listini oggi non e' pronta: {writer_setup.message}. "
            "Se un confronto non c'e' ancora e' normale — si rifa' da sola al primo "
            "ricalcolo; se un confronto c'e', vuol dire che a compilare non ci si arriva."
        )

    risposta = salute(DEFAULT_PORT)
    if risposta is not None:
        avvisi.append(
            f"sulla porta {DEFAULT_PORT} c'e' gia' un comparatore acceso. All'avvio "
            "verra' riusato, oppure spento e riavviato se il codice e' cambiato."
        )
    elif not port_is_free(DEFAULT_PORT):
        avvisi.append(
            f"la porta {DEFAULT_PORT} e' occupata da un altro programma: il comparatore "
            f"partira' su una porta successiva (fino alla {DEFAULT_PORT + PORT_ATTEMPTS - 1})."
        )

    copie = cartella_delle_copie()
    try:
        copie.mkdir(parents=True, exist_ok=True)
        prova = copie / ".prova-di-scrittura"
        prova.write_bytes(b"")
        prova.unlink()
    except OSError as guasto:
        avvisi.append(
            f"la cartella delle copie non e' scrivibile ({type(guasto).__name__}: {guasto}): "
            "conferme, ordini e schemi imparati resterebbero senza copia di sicurezza."
        )

    # Imported here, not at the top: the launcher must still start on a
    # machine where the AI part can't be imported, and this is the only
    # line that needs it.
    import ai_client  # noqa: PLC0415

    stato = ai_client.stato_chiave()
    if not stato.get("presente"):
        avvisi.append(
            "nessuna chiave OpenRouter: la fase che confronta le descrizioni con l'aiuto "
            "dell'AI non parte, e i prodotti dubbi finiscono da verificare a mano. "
            "Si incolla da Impostazioni."
        )

    return avvisi


def cartella_delle_copie(
    sistema: str | None = None,
    piattaforma: str | None = None,
    ambiente: dict[str, str] | None = None,
) -> Path:
    """Return where to keep safety copies, outside the project.

    Outside is the requirement, not a detail: inside the repository would
    bring back the original problem, since the store PC's startup resets
    tracked files and `.gitignore` excludes all of `app/data/`.

    The path is computed here rather than stored as a constant because the
    program runs on both Windows (the store) and macOS (development):
    `%LOCALAPPDATA%` doesn't exist on macOS, and a branch depending on it
    would always be skipped on the test machine.

    The three parameters exist for one reason: testing the Windows branch
    on a machine that isn't Windows. They're never passed in normal use.
    Overriding `os.name` directly, the other option, breaks `pathlib`
    mid-test.
    """

    nome = sistema if sistema is not None else os.name
    sistema_operativo = piattaforma if piattaforma is not None else sys.platform
    variabili = ambiente if ambiente is not None else os.environ

    if nome == "nt":
        base = variabili.get("LOCALAPPDATA") or variabili.get("APPDATA")
        if base:
            return Path(base) / "ComparaOrdini" / "copie"
        return Path.home() / ".compara-ordini-copie"
    if sistema_operativo == "darwin":
        return Path.home() / "Library" / "Application Support" / "ComparaOrdini" / "copie"
    base = variabili.get("XDG_DATA_HOME")
    radice = Path(base) if base else Path.home() / ".local" / "share"
    return radice / "ComparaOrdini" / "copie"


def copia_le_memorie(dati: Path | None = None, destinazione: Path | None = None,
                     quando: str | None = None,
                     su_guasto: Callable[[str], None] | None = None) -> Path | None:
    """Return a dated zip of non-regenerable state, or `None` if none was made.

    Called after the server has started, never before: `memoria_ai.json`
    alone weighs 2.1 MB, and compressing it before the page opens would
    delay the one thing the user is waiting for.

    Never raises. A backup is a safety net, not a precondition for working:
    a full disk or an unwritable folder must leave the program running, not
    stop it.

    Failing silently is a separate problem, though. Returning `None` both
    when there's nothing to back up and when the backup failed, with the
    caller only printing a line on success, would hide an unwritable
    folder, a full disk, or antivirus locking the temp file behind a
    startup that looks normal while the safety net silently isn't there.
    It's the only backup `conferme.db` has, since no recompute can
    regenerate that state. `su_guasto` receives the reason, in Italian, and
    the caller decides where to print it.
    """

    cartella_dati = Path(dati or DATA_DIR)
    cartella_copie = Path(destinazione or cartella_delle_copie())
    presenti = [voce for voce in MEMORIE_DA_COPIARE if (cartella_dati / voce).is_file()]
    if not presenti:
        return None

    giorno = quando or time.strftime("%Y-%m-%d")
    try:
        cartella_copie.mkdir(parents=True, exist_ok=True)
        percorso = cartella_copie / f"{giorno}.zip"
        # Write to a temp file beside it, then `os.replace`: if compression
        # is interrupted midway, yesterday's copy is still good instead of
        # being replaced by a truncated archive.
        temporaneo = cartella_copie / f".{giorno}.zip.parziale"
        with zipfile.ZipFile(temporaneo, "w", compression=zipfile.ZIP_DEFLATED) as archivio:
            for voce in presenti:
                archivio.write(cartella_dati / voce, arcname=str(voce).replace(os.sep, "/"))
        os.replace(temporaneo, percorso)
    except (OSError, zipfile.BadZipFile) as guasto:
        if su_guasto is not None:
            su_guasto(f"{type(guasto).__name__}: {guasto}")
        return None

    _tieni_le_ultime_copie(cartella_copie)
    return percorso


def _tieni_le_ultime_copie(cartella: Path, quante: int = COPIE_DA_TENERE) -> None:
    """Delete the oldest backups: the filename carries the date, so
    alphabetical order is already chronological order."""

    try:
        archivi = sorted(cartella.glob("*.zip"))
    except OSError:
        return
    for vecchio in archivi[:-quante] if len(archivi) > quante else []:
        try:
            vecchio.unlink()
        except OSError:
            # An old backup that fails to delete harms no one.
            continue


def main() -> int:
    args = parse_args()
    if args.check:
        return run_check()

    print("\nCompara ordini fornitori")
    print("========================")
    try:
        validate_python()
        for source_path in REQUIRED_SOURCE_PATHS:
            validate_source(source_path)
        ensure_local_layout()
        validate_review(REVIEW_PATH, may_be_missing=False)
        review = read_review(REVIEW_PATH)
        writer_setup = prepare_writer_config(review, write=True)
        port, already_running = select_port(args.port)
    except RuntimeError as exc:
        return fail("Avvio non riuscito.", detail=str(exc))

    url = f"http://{HOST}:{port}/"
    if already_running:
        print(f"[OK] Il comparatore è già attivo su {url}")
        if not args.no_browser:
            open_application(url)
        return 0

    if port != args.port:
        print(f"[AVVISO] La porta {args.port} è occupata; userò la porta {port}.")

    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    print(f"Runtime Python: {sys.executable}")
    print(f"Dati locali:    {CURRENT_DIR}")
    print(f"Writer ordini:  {writer_setup.message}")
    print("Gli originali non saranno modificati e il server resterà su 127.0.0.1.")

    try:
        process = subprocess.Popen(
            server_command(port, writer_setup.config_path),
            cwd=APP_DIR,
            env=environment,
        )
    except OSError as exc:
        return fail("Il processo server non può essere avviato.", detail=str(exc))
    try:
        wait_until_ready(process, port)
        print(f"[OK] Comparatore pronto: {url}")
        copia = copia_le_memorie(su_guasto=lambda motivo: print(
            f"[AVVISO] Copia delle memorie non riuscita ({motivo}). Il programma funziona "
            f"lo stesso, ma oggi conferme, ordini e schemi imparati non hanno una copia: "
            f"controlla che {cartella_delle_copie()} sia scrivibile e che ci sia spazio."
        ))
        if copia is not None:
            print(f"Copia delle memorie: {copia}")
        if not args.no_browser:
            open_application(url)
        print("Per fermare il comparatore, torna in questa finestra e premi Ctrl+C.")
        # Ctrl+C doesn't go through `/api/spegni`, which refuses to stop
        # while the pipeline is running: it just kills the process. The
        # reason for that refusal still applies here — a run killed midway
        # leaves an orphaned folder and AI answers already paid for that
        # would need redoing — so it's stated here, the one line read
        # before pressing it.
        print("            Se sta ricalcolando, aspetta che finisca: fermarlo adesso")
        print("            butta via il lavoro di quel confronto.")
        return_code = process.wait()
        if return_code != 0:
            return fail(
                "Il server si è chiuso in modo inatteso.",
                detail=f"codice di uscita {return_code}",
            )
        return 0
    except KeyboardInterrupt:
        print("\nArresto del comparatore in corso…")
        stop_process(process)
        print("Comparatore arrestato.")
        return 0
    except RuntimeError as exc:
        stop_process(process)
        return fail("Avvio del server non riuscito.", detail=str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
