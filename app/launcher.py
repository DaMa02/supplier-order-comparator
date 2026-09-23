#!/usr/bin/env python3
"""Avvia il comparatore locale con percorsi e controlli sicuri per Windows."""

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


# Sta accanto a questo file, quindi si importa senza toccare `sys.path`: il
# lanciatore e' lo script principale e la sua cartella e' gia' la prima.  Serve
# per chiedere «il server acceso sta eseguendo questi sorgenti?».
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
# Programma di navigazione richiesto: Chrome, non il predefinito di Windows.
# La variabile d'ambiente serve per le prove e per installazioni fuori standard.
CHROME_ENV_OVERRIDE = "COMPARATORE_CHROME"
CHROME_RELATIVE_PATH = Path("Google") / "Chrome" / "Application" / "chrome.exe"
# Quali fornitori si sanno compilare non e' scritto qui: lo dichiara il
# registro, con `order_write` dentro l'adattatore (vedi `references/adapters.json`
# e `references/schema-routing.md`).  Finche' era una tupla in questo file, un
# fornitore imparato non poteva diventare compilabile senza toccare il codice —
# ed era il contrario della regola su cui poggia tutta la Fase 4.
ADAPTERS_PATH = SKILL_ROOT / "references" / "adapters.json"
DATA_DIR = APP_DIR / "data"
CURRENT_DIR = DATA_DIR / "current"
REVIEW_PATH = CURRENT_DIR / "review_data.json"
STATE_PATH = CURRENT_DIR / "state.json"
UPLOAD_DIR = CURRENT_DIR / "uploads"
OUTPUT_DIR = CURRENT_DIR / "outputs"
WRITER_CONFIG_PATH = CURRENT_DIR / "writer_config.json"
WRITER_SCRIPT_PATH = SKILL_ROOT / "scripts" / "write_supplier_orders.mjs"
# La libreria che legge e scrive gli `.xlsx`, copiata dentro il progetto: e'
# quella che il writer cerca per prima.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_ATTEMPTS = 20

# Le memorie che il ricalcolo non rifa': se si perdono, non si rigenerano
# premendo un pulsante. Dal commit 6007574 (18 agosto 2026) stanno fuori da
# git — che e' giusto, perche' cambiano mentre il programma gira e l'avvio del
# PC del negozio riporta indietro i file tracciati — ma da quel giorno vivono
# SOLO sul disco del negozio, senza nessuna copia da nessuna parte. Il
# commit ae2e214 le aveva messe sotto git proprio perche' «se muore il disco
# non si recuperano»: quella protezione e' stata tolta e non sostituita.
#
# Quello che manca da un avvio all'altro non e' un errore: un'installazione
# nuova non ha ancora ne' ordini ne' memoria AI.
MEMORIE_DA_COPIARE = (
    Path("current") / "state.json",
    # ⚠ Il ponte a mano per i listini che il programma non e' riuscito a
    # imparare.  Non era nell'elenco, e invece e' una memoria come le altre:
    # sopravvive al ricalcolo e a «Inizia nuova comparazione», e per un
    # fornitore che non e' stato imparato e' l'unica cosa che rende ancora
    # leggibile il suo listino.  Perderla vuol dire riscriverla a mano senza
    # sapere che cosa c'era scritto.
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
    # ⚠ Basta Node.  Fino al 5 settembre 2026 qui c'era anche `node_modules`,
    # la cartella con `@oai/artifact-tool`: le copie `.xlsx` adesso le scrive
    # `scripts/lib/xlsx_in_posizione.mjs` con la sola libreria standard di Node.
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
    """Il primo Node 18+ che si trova, nell'ordine in cui vale la pena cercarlo.

    Il runtime portabile dell'app prima di tutto; poi quelli che il runtime di
    sviluppo di Codex si porta dietro, perche' sul PC del negozio sono stati a
    lungo l'unico Node presente; infine quello nel PATH.  Non serve nessuna
    libreria accanto: il writer usa solo quello che Node ha in casa.
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
    """Il motore del registro, importato quando serve.

    Tardivo di proposito: il lanciatore parte anche da solo, e non deve
    fallire all'avvio se la cartella `scripts` non e' al suo posto.  Leggere il
    registro qui dentro con `json.loads` sarebbe una seconda verita' sullo
    stesso file: `scripts/registro.py` e' l'unico che lo legge.
    """

    cartella = str(SKILL_ROOT / "scripts")
    if cartella not in sys.path:
        sys.path.insert(0, cartella)
    import registro  # noqa: PLC0415 - import tardivo voluto

    return registro


def nome_leggibile(supplier: Any) -> str:
    """Come si chiama questo fornitore nelle frasi, secondo il registro.

    ⚠ Qui c'era `supplier.upper()` in cinque frasi: un fornitore imparato
    compariva come `NUOVO_FORNITORE_1`, underscore compresi, mentre il registro
    il suo nome ce l'aveva gia' — e il `display_name` che questo stesso modulo
    mette dentro la regola di scrittura veniva proprio di li' (revisione di
    regressione del 14 agosto 2026).

    Se il registro non e' raggiungibile resta il ripiego di prima: una frase con
    l'identificativo e' meglio di un lanciatore che non parte.
    """

    try:
        return _registro_degli_adattatori().nome_del_fornitore(supplier, ADAPTERS_PATH)
    except Exception:  # noqa: BLE001 - il nome non vale un avvio mancato
        return str(supplier or "fornitore").upper()


def adattatori_compilabili(percorso: Path | None = None) -> dict[str, dict[str, Any]]:
    """I fornitori di cui il registro dichiara **come** si scrive l'ordine.

    La compilabilita' e' una proprieta' dichiarata, non un elenco nel codice:
    un adattatore che porta `order_write` si sa compilare, uno che non ce l'ha
    no — e questo vale identico per i sei nativi e per qualunque schema
    imparato dopo.  Finche' l'elenco era una tupla in questo file, un fornitore
    imparato non sarebbe mai diventato compilabile e nessuno lo diceva:
    misurato il 12 agosto 2026 con 102 prodotti assegnati a ACERO e gli
    avvertimenti vuoti.
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
    """L'adattatore con cui si scrive l'ordine dentro QUESTO documento.

    ⚠ Lo dice la **decisione** con cui il documento e' stato letto, non il nome
    del fornitore. E' la stessa lezione della quinta volta che un
    `if supplier == "..."` e' stato tolto da questo progetto — chi legge un
    listino lo decide la decisione — e vale identica sul lato della scrittura,
    dove costa di piu': un ordine scritto nella colonna sbagliata esce dal
    programma e va al fornitore.

    Due casi in cui prendere il primo adattatore di quel fornitore sbaglia, e
    tutti e due esistono dal 22 agosto 2026:

    - **CIPRESSO ha due schemi**, e solo uno dei due dichiara che sopra la
      colonna dell'ordine deve esserci scritto ORDINE. Misurato: prendendo la
      voce per fornitore la regola esce senza `expected_header`, cioe' senza il
      controllo — ed e' esattamente la difesa che a BETULLA era stata tolta da
      una voce imparata, il 21 agosto.
    - **la colonna d'ordine spostata dalla pagina** scrive una voce `__locale`
      con la colonna nuova. Chi cerca per fornitore ritrova quella spedita e
      scrive dove non vuole piu' nessuno.

    Il ripiego sul fornitore resta per i confronti vecchi, le cui schede non
    dichiarano nessun adattatore: meglio la voce di quel fornitore che niente.
    """

    registro = _registro_degli_adattatori()
    dichiarato = str((entry or {}).get("adapterId") or (entry or {}).get("adapter_id") or "").strip()
    if dichiarato:
        voce = registro.voce_in_uso(dichiarato, registro.adattatori(percorso or ADAPTERS_PATH))
        if voce:
            return voce
    return per_fornitore.get(supplier) or {}


def fornitori_compilabili(percorso: Path | None = None) -> tuple[str, ...]:
    """Solo i nomi, dal piu' lungo al piu' corto.

    L'ordine conta perche' il riconoscimento e' per sottostringa: con
    «cedi» prima di «noce» un documento Noce finirebbe al fornitore
    sbagliato, e sarebbe un ordine mandato a chi non lo aspetta.
    """

    return tuple(sorted(adattatori_compilabili(percorso), key=lambda nome: (-len(nome), nome)))


def supplier_key(file_entry: dict[str, Any], compilabili: tuple[str, ...] | None = None) -> str:
    """Il fornitore di questo documento, se è uno che si sa compilare."""

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
    """Il fornitore che il documento dichiara, compilabile o no.

    Serve a dire l'assenza: un listino che non si sa compilare deve avere un
    nome nell'avviso, altrimenti l'avviso non si puo' nemmeno scrivere.
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
    """I listini del confronto per cui il registro non dice come si scrive l'ordine.

    Non e' un guasto: e' una cosa che l'utente deve sapere **prima** di
    assegnare mezzo ordine a quel fornitore, perche' alla fine di quella strada
    non c'e' nessuna copia da mandare.  Un fornitore che sparisce dall'elenco
    dei compilabili senza una parola e' esattamente il buco misurato il 12
    agosto 2026.
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
    """Ogni fornitore del confronto per cui OGGI non nascerebbe una copia, col perche'.

    Due cause diverse, stessa conseguenza.  O il registro non dichiara
    `order_write` (il fornitore non e' mai compilabile), oppure lo dichiara ma
    il documento di questa settimana non combacia con la dichiarazione —
    un'intestazione cambiata, un foglio sparito, righe fuori dal documento.
    La seconda famiglia moriva dentro il messaggio di `prepare_writer_config`,
    che il server butta via quando la scrittura riesce: BETULLA con
    l'intestazione cambiata spariva dalla configurazione senza una parola in
    pagina, e lo si scopriva alla compilazione con una frase che non diceva la
    causa (revisione avversariale del 13 agosto 2026).  Chi vuole avvisare
    PRIMA che la merce sia assegnata chiama questa, non il solo registro.

    `percorso` esiste perche' l'orchestratore lavora su un registro
    configurabile (nelle prove e' una copia): guardarne uno e avvisare
    sull'altro direbbe bugie in tutte e due le direzioni.  Con un registro
    che non si apre risponde vuoto: quella e' un'altra frase, la dice
    `registro.motivo_registro_illeggibile` e tocca al chiamante chiederla.
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
    """I fornitori che il confronto usa davvero, presi dalle offerte.

    Non da `review["files"]`: quello e' l'elenco dei documenti, e le due liste
    divergono appena un listino viene eliminato o sostituito.  Chi deve dire
    «per questo fornitore non nascera' nessuna copia» deve partire da qui.
    """

    nomi: list[str] = []
    for voce in review.get("suppliers") or []:
        if isinstance(voce, dict):
            nome = str(voce.get("id") or "").strip().casefold()
            if nome and nome not in nomi:
                nomi.append(nome)
    if nomi:
        return sorted(nomi)
    # Un confronto piu' vecchio puo' non portare l'elenco: si ricava dalle offerte.
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
    """I fornitori su cui si sta per ordinare che oggi non produrrebbero una copia.

    La differenza con `fornitori_senza_copia` non e' cosmetica, ed e' la
    ragione per cui questa funzione esiste.  Quella parte dall'elenco dei
    **documenti** (`review["files"]`); questa parte dai **fornitori** su cui
    l'utente ha messo una quantita'.  Le due liste possono divergere — un
    listino eliminato o sostituito lascia il fornitore dentro le offerte e lo
    toglie dai documenti — e in quel caso la prima non ha niente da dire,
    perche' i suoi avvisi nascono **dentro** il ciclo sui documenti trovati:
    zero documenti risolti significa zero avvisi.

    Misurato il 14 agosto 2026 sul dataset vivo: quattro fornitori con offerte
    e quantita' assegnate, un solo documento nell'elenco, il suo percorso non
    piu' esistente.  `writer_readiness` rispondeva «Writer XLSX pronto» con
    zero regole e zero avvisi, e la compilazione produceva il piano e nessuna
    copia da mandare.

    Chi decide se si puo' compilare chiama questa, e la chiama con i fornitori
    che stanno davvero nell'ordine.
    """

    richiesti = [str(nome or "").strip().casefold() for nome in fornitori]
    richiesti = sorted({nome for nome in richiesti if nome})
    if not richiesti:
        return {}

    # Un registro che non si apre non e' un registro che «non dichiara»: non
    # sappiamo niente di nessuno, quindi non si compila nessuno.  Fallire
    # chiusi qui e' l'unica risposta onesta.
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
    # ⚠ Non si usano `review_supplier_sources`/`review_supplier_entries`: quelle
    # chiamano `fornitori_compilabili()` senza percorso, cioe' leggono sempre il
    # registro predefinito.  L'orchestratore lavora su un registro
    # configurabile — nelle prove e' una copia — e guardarne uno mentre si
    # avvisa sull'altro dice bugie in tutte e due le direzioni: un fornitore
    # dichiarato solo nel registro di lavoro risulterebbe «senza documento»
    # anche col suo listino al posto giusto.
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
            # Il caso che nessuno diceva.  Il fornitore e' compilabile in
            # astratto, ma il documento di questa settimana non c'e' — mai
            # caricato, eliminato dopo il confronto, o con un percorso che non
            # si risolve piu'.
            voce = voci.get(nome)
            if voce is None and not (review.get("files") or []):
                # ⚠ «Non lo so» non e' «non c'e'».  Un confronto che non porta
                # affatto l'elenco dei documenti — le prove sintetiche, i
                # formati piu' vecchi — non autorizza a dire che manchino: si
                # tace, come si tace in `base_review` per lo stesso motivo.
                # Quando l'elenco c'e' ed e' quel fornitore a non esserci,
                # allora si', ed e' la divergenza che si vuole scoprire.
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
    """«xls», «xlsx» o «csv», guardato nei primi byte del file.

    Il formato lo decidono i byte e non l'estensione — lo dice gia' la nota di
    Noce nel registro — e chi sa leggerli e' `scripts/inspect_sources.py`.
    Qui si importa il suo invece di tenerne una seconda copia: due elenchi di
    firme divergono, e quello che si dimentica di aggiornare e' sempre quello
    che poi apre il file con il lettore sbagliato.  Se quella cartella non c'e'
    resta l'estensione, che e' meno di niente ma non e' un guasto.
    """

    cartella = str(SKILL_ROOT / "scripts")
    if cartella not in sys.path:
        sys.path.insert(0, cartella)
    try:
        from inspect_sources import container_format  # noqa: PLC0415 - import tardivo voluto
    except Exception:  # noqa: BLE001 - senza `scripts` si ripiega, non si muore
        return path.suffix.casefold().lstrip(".") or "csv"
    return container_format(path)


def numero_di_colonna(lettere: str) -> int:
    """«G» → 7.  Quale colonna verificare lo dichiara il registro, non il codice."""

    indice = 0
    for lettera in lettere.upper():
        indice = indice * 26 + (ord(lettera) - ord("A") + 1)
    return indice


def _intero(valore: Any) -> int | None:
    return valore if isinstance(valore, int) and not isinstance(valore, bool) else None


def regola_noce(source: Path, mapping: dict[str, Any], order_column: str, header_row: int,
                    data_start_row: int, etichetta: str) -> tuple[dict[str, Any] | None, str | None]:
    """Il listino `.xls` si compila in posizione, quattro byte per cella.

    La colonna dell'EAN resta dichiarata **per nome**: `cat` e `Iva` sono nomi
    di colonna veri di questo listino e insieme riferimenti Excel validi, e un
    ripiego sulle lettere leggerebbe in silenzio la colonna sbagliata.  Chi
    scrive la risolve leggendo la riga di intestazione del file.
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
        # Il documento si compila in posizione, non passando da Node: e' scritto
        # qui perche' chi legge la configurazione lo sappia senza dedurlo
        # dall'estensione.
        "compilazione": "patch_xls_in_posizione",
    }, None


def _colonna_dichiarata(dichiarata: Any, intestazione: list[Any]) -> str | None:
    """La lettera di colonna corrispondente a una dichiarazione del registro.

    Il registro dice dove sta un campo in tre modi diversi a seconda del
    fornitore — lettera, numero, nome dell'intestazione — e sono tutti e tre
    legittimi. Il nome si puo' risolvere solo con l'intestazione del documento
    davanti: senza, non si indovina e si torna `None`.
    """

    # Tardivo come gli altri openpyxl di questo file: il lanciatore deve poter
    # partire e *dire* che manca (vedi il controllo in cima), non morire di
    # ImportError prima di arrivare a spiegarlo.
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
    # Nessuna intestazione che corrisponda: se la dichiarazione era gia' una
    # lettera vale quella, altrimenti non si sa e non si inventa.
    if re.fullmatch(r"[A-Za-z]{1,3}", testo):
        return testo.upper()
    return None


def _colonne_da_verificare(
    adattatore: dict[str, Any] | None,
    mappatura: dict[str, Any] | None,
    intestazione: list[Any],
) -> dict[str, str]:
    """Dove il writer puo' controllare che la riga sia quella giusta.

    Solo cio' che il registro (o la mappatura confermata) dichiara gia': l'EAN
    identifica l'articolo in modo esatto, la descrizione lo riconosce e basta.
    Sono due controlli con due severita' diverse, e chi li usa lo sa.
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
    """La riga in cui la colonna d'ordine porta la scritta attesa, se e' una sola.

    `None` quando non c'e' o quando ce ne sono due: in tutti e due i casi
    indovinare vorrebbe dire scrivere le quantita' in un punto che nessuno ha
    guardato, e chi chiama tiene i numeri dichiarati e lascia rifiutare la
    verifica.

    Si guardano solo le prime righe: un'intestazione sta in cima, e leggere
    tutto il foglio in sola lettura costerebbe l'attesa di cinque minuti che il
    15 agosto 2026 ha fatto sembrare piantato l'avvio del programma.
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
    """La regola con cui si scrive l'ordine dentro il listino di questo fornitore.

    Ogni numero e ogni lettera vengono dal registro — `order_write`
    nell'adattatore, completato dalla `field_mapping` quando l'adattatore ne ha
    una — e non da un ramo `if supplier == "..."`.  Prima era il codice a
    sapere che BETULLA si ordina in colonna C: un fornitore imparato non sarebbe
    mai potuto diventare compilabile, e nessuno lo diceva.  Qui si aggiunge
    soltanto cio' che il registro non puo' sapere: il nome vero del foglio del
    documento di oggi, la sua impronta, e la prova che l'intestazione
    dichiarata sta dove e' scritto.

    ⚠ «FIRST» vuol dire due cose diverse a seconda di chi lo dice, ed e'
    voluto.  In `order_write` lo dice il registro, cioe' una persona che quel
    listino l'ha guardato: vuol dire «il primo foglio del documento».  In una
    `field_mapping` vuol dire che il foglio **non e' stato identificato**, e
    allora il documento ne deve avere uno solo — scegliere il primo di tre
    scriverebbe l'ordine in un foglio che nessuno ha guardato.
    """

    # Tutto maiuscolo come nelle altre frasi del programma: `capitalize()`
    # trasformava «Noce» in «Noce», una grafia che non e' di nessuno.
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
    # La mappatura e' quella del confronto, non quella del registro: e' quella
    # che l'utente ha avuto davanti, ed e' misurata sul documento di oggi.
    # Ripiegare su quella del registro vorrebbe dire scrivere l'ordine secondo
    # un documento diverso da quello che si sta compilando.
    mapping = entry.get("fieldMapping") or entry.get("field_mapping")
    if not isinstance(mapping, dict):
        mapping = {}
    if da_mappatura and not mapping:
        return None, f"{etichetta} non attivato: manca la mappatura confermata della colonna ordine"

    dichiarata = str(dichiarazione.get("order_column") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", dichiarata):
        return None, f"{etichetta} non attivato: il registro non dichiara la colonna d'ordine del suo listino"
    if da_mappatura and str(mapping.get("order_column") or mapping.get("orderColumn") or "").strip().upper() != dichiarata:
        # La colonna la dichiara il registro; la mappatura confermata deve dire
        # la stessa cosa. Se dicono due colonne diverse l'ordine finirebbe in
        # una cella che nessuno ha verificato.
        return None, incompleta
    order_column = dichiarata

    # Righe e foglio: dove li dichiara chi legge il documento. Per gli
    # adattatori con una mappatura e' la mappatura (che l'utente ha confermato);
    # per quelli con un lettore dedicato e' `order_write`.
    origine = mapping if da_mappatura else dichiarazione
    header_row = _intero(origine.get("header_row") or origine.get("headerRow"))
    data_start_row = _intero(origine.get("data_start_row") or origine.get("dataStartRow"))
    colonne = mapping.get("columns") if isinstance(mapping.get("columns"), dict) else {}
    mancanti = [str(nome) for nome in (dichiarazione.get("required_columns") or []) if nome not in colonne]
    if mancanti:
        # La causa vera: mancano colonne, non righe.  La frase di prima
        # mandava a cercare `data_start_row` quando il problema era un'altra
        # dichiarazione (revisione avversariale del 13 agosto 2026).
        if da_mappatura:
            return None, incompleta
        return None, (f"{etichetta} non attivato: il registro chiede le colonne "
                      + ", ".join(sorted(mancanti))
                      + " ma il documento non ha una mappatura confermata che le porti")
    atteso = str(dichiarazione.get("expected_header") or "").strip()
    # La conferma «quella cella e' vuota, e va bene cosi'» arriva da due posti,
    # e sono due strade per la stessa risposta umana: la mappatura confermata
    # nell'anteprima (fornitori con `from_field_mapping`) oppure `order_write`
    # stesso, quando la colonna e' stata scelta dalla pagina su un fornitore che
    # ha un lettore dedicato e nessuna mappatura — BETULLA.  Senza la seconda,
    # spostare la colonna d'ordine di BETULLA su una colonna senza titolo
    # **spegnerebbe** ogni verifica invece di accenderla: `expected_header`
    # assente vuol dire «niente da controllare», e la quantita' finirebbe in una
    # cella che nessuno ha guardato.  Quello che si dichiara viene comunque
    # misurato sul documento, qui sotto: la cella dev'essere ancora vuota
    # davvero, e sotto non ci devono essere testo o formule.
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
        # Un refuso nella procedura ricadeva in silenzio su quella base: su un
        # `.xls` si vedeva per caso (openpyxl si rifiuta), su un `.xlsx` no.
        return None, (f"{etichetta} non attivato: il registro dichiara una procedura di "
                      f"scrittura sconosciuta («{procedura}»)")

    # ⚠ Da qui in giu' si apre il documento con openpyxl, che di un `.xls` non
    # sa niente: senza questa guardia l'utente riceveva la frase inglese della
    # libreria — «openpyxl does not support the old .xls file format, please use
    # xlrd to read this file» — incastonata dentro «Non riesco a leggere il
    # listino LARICE», e la leggeva come «lo legge ma non ci sa scrivere».
    # Il rimedio vero e' una riga di Excel, e va detto qui: il `.xls` si compila
    # in posizione solo dove il registro dichiara quella procedura, che pretende
    # una colonna d'ordine gia' tutta numerica (Noce).
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
        # «FIRST» scritto nel registro e' una scelta di chi il listino l'ha
        # guardato, e vuol dire «il primo foglio».  Un foglio ASSENTE invece
        # non e' una scelta: prendere il primo di tre sarebbe scrivere
        # l'ordine in un foglio che nessuno ha guardato.  Da una mappatura
        # vale sempre la regola del foglio unico, perche' li' «FIRST» marca
        # un foglio non identificato.
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

    # ⚠ Dove sta l'intestazione, quando il registro dice di **cercarla** invece
    # di dichiararne la riga. Serve ai listini che non hanno una riga di
    # intestazione vera: nel foglio delle offerte sopra i prodotti ci sono
    # righe vuote e la sola parola ORDINE, e quante siano quelle righe vuote
    # cambia di mese in mese. La lettura lo sa gia' — segue un
    # `data_start_marker` che si ricalcola a ogni giro — e senza questo la
    # scrittura restava indietro con un numero congelato: il confronto mostrava
    # le offerte, la compilazione non produceva nessuna copia per quel
    # fornitore e lo diceva solo con un avviso in mezzo agli altri.
    #
    # Se la parola non c'e', o compare piu' di una volta, non si indovina: si
    # tengono i numeri dichiarati e la verifica qui sotto rifiuta, che e'
    # esattamente quello che deve succedere.
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
        # Il nome leggibile viaggia con la regola: il writer Node non legge il
        # registro — la configurazione e' il suo unico ingresso dichiarato — e
        # senza questo si ritrovava a stampare l'identificativo tecnico, con
        # l'underscore, nei messaggi e nei nomi dei file d'ordine.
        "display_name": _registro_degli_adattatori().nome_del_fornitore(supplier, ADAPTERS_PATH),
    }
    if header_row is not None and header_row >= 1:
        regola["header_row"] = header_row
    if header_row is not None and header_row >= 1 and data_start_row <= header_row:
        return None, (f"{etichetta} non attivato: le righe dichiarate non stanno insieme "
                      f"(intestazione alla riga {header_row}, dati dalla riga {data_start_row})")

    # Il documento si apre anche quando non c'e' un'intestazione da
    # verificare: righe e intestazione dichiarate vengono da un file che una
    # persona modifica a mano, e un refuso di una cifra scriverebbe le
    # quantita' fuori dai dati senza una parola (revisione avversariale del
    # 13 agosto 2026).
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(source, read_only=True, data_only=False)
        try:
            foglio = workbook[sheet_name]
            ultima_riga = foglio.max_row
            numero_colonna_ordine = numero_di_colonna(order_column)
            # ⚠ Su un foglio aperto in **sola lettura** `foglio.cell(r, c)` non
            # e' un accesso: openpyxl rilegge il foglio dall'inizio a ogni
            # chiamata.  Il ciclo qui sotto ne faceva una per riga di listino —
            # 3382 chiamate su CIPRESSO, cioe' 5,7 milioni di righe analizzate
            # — e il 15 agosto 2026 l'avvio del programma e' rimasto muto per
            # **cinque minuti**, con l'utente convinto che si fosse piantato.
            # Il ramo era appena diventato raggiungibile: e' l'intestazione
            # vuota confermata di CIPRESSO a portarci.  Adesso il foglio si
            # attraversa **una volta sola**, e si prende quello che serve.
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
    # Dove il writer puo' controllare che la riga di destinazione porti davvero
    # il prodotto del piano. E' la difesa che il 12 agosto 2026 avrebbe fermato
    # l'ordine finito sulla riga 2600, e finora esisteva solo per Noce.
    # Non si inventa niente: si dichiara solo cio' che il registro dice gia'.
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
    # I listini del confronto che il registro non sa compilare: l'assenza si
    # dice qui, dove si decide chi entra nella configurazione di scrittura.
    # Senza, sparivano prima ancora di essere nominati.
    motivo_registro = _registro_degli_adattatori().motivo_registro_illeggibile(ADAPTERS_PATH)
    if motivo_registro:
        # Un registro che non si apre non e' un registro che «non dichiara»:
        # la frase per-fornitore manderebbe a cercare una dichiarazione dentro
        # un file rotto (revisione avversariale del 13 agosto 2026).
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

    # ⚠ Gli avvisi qui sopra nascono **dentro** il ciclo sulle sorgenti
    # risolte.  Un fornitore il cui listino e' sparito — mai caricato,
    # eliminato dopo il confronto, percorso che non si risolve piu' — non entra
    # in quel ciclo, quindi nessuno lo nomina: zero sorgenti significa zero
    # avvisi.  Misurato il 14 agosto 2026 sul dataset vivo: quattro fornitori
    # con offerte, un solo documento nell'elenco, e `writer_readiness` che
    # rispondeva con zero regole e **zero avvisi**.
    #
    # Si parte dai fornitori che il confronto usa davvero, non dai documenti.
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
    """Costruisce la configurazione di scrittura a partire dal confronto vivo.

    `destinazione` esiste per l'orchestratore: dopo un ricalcolo i listini sono
    altri file, con altri nomi e altre righe, e una configurazione rimasta a
    quella della settimana scorsa farebbe compilare **il listino di prima** con
    i numeri di riga di adesso.  Il lanciatore la scrive dove ha sempre fatto;
    l'orchestratore la scrive dove gliela chiede il server, che nelle prove non
    e' la cartella vera.
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
        # ⚠ Questo file lo scrivono in due, e non due fili: il **processo** del
        # lanciatore, che ci passa a ogni avvio — anche quando il servizio e'
        # gia' acceso e sta ricalcolando — e il filo della catena dentro il
        # servizio, quando riconfigura la scrittura a fine ricalcolo. Con un
        # temporaneo dal nome fisso i due si sovrapponevano, e il file
        # pubblicato poteva prendere il `run_id` da uno e i `supplier_files`
        # dall'altro: la guardia che confronta le due run direbbe di si'
        # proprio a cio' che deve fermare, e il writer scriverebbe i numeri di
        # riga di adesso dentro il listino di prima. Il gesto che lo innesca e'
        # quello che l'utente fa quando la pagina sembra bloccata: doppio clic
        # sull'avvio a ricalcolo in corso. Trovato dalla verifica avversariale
        # del 20 agosto 2026.
        scrittura_sicura.scrivi_json(percorso, config)

    # «Pronto» con zero regole non e' pronto: e' una configurazione che non
    # compilera' niente, e dirlo «pronto» e' la frase che ha coperto il buco
    # fino al 14 agosto 2026.  Il numero dei fornitori scrivibili sta nella
    # frase, cosi' lo zero si vede.
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
    """La risposta di `/api/health`, oppure `None` se non risponde l'app.

    Separata da `application_health` perche' adesso non basta sapere **che**
    qualcuno risponde: serve sapere **quale programma** e', e lo dice
    `firmaDelCodice`.  Un server piu' vecchio della firma non ce l'ha, e
    l'assenza e' essa stessa la risposta: e' vecchio di sicuro.
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
    """Il server che risponde sta eseguendo i sorgenti che ci sono adesso?

    Un server senza `firmaDelCodice` e' precedente a questa difesa: si tratta
    come diverso, che e' la risposta prudente e anche quella vera.
    """

    dichiarata = risposta.get("firmaDelCodice")
    return isinstance(dichiarata, str) and bool(dichiarata) and dichiarata == firma_del_disco


def chiedi_di_spegnersi(port: int, *, timeout: float = 6.0) -> tuple[bool, str]:
    """Chiede al server di fermarsi e aspetta che smetta di rispondere.

    Restituisce `(spento, motivo)`.  Il motivo non e' decorazione: se una run e'
    in corso il servizio **rifiuta**, e chi chiama deve dirlo invece di
    insistere — una catena uccisa a meta' costa il lavoro gia' pagato all'AI.
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
    """La porta da usare, e se il server c'e' gia' e si puo' riusare.

    ⚠ **Un server sano non basta piu' a farsi riusare: deve anche eseguire i
    sorgenti di adesso.**  Prima bastava che rispondesse, e chi riapriva il
    `.cmd` dopo un aggiornamento tornava sul programma di prima credendo di
    averlo riavviato — il caso vero: server acceso il 15 agosto alle 19:25 e
    quattro consegne dopo ancora quello.

    Se la firma non coincide si chiede al server di spegnersi e si riprende
    **la sua stessa porta**: mai la successiva.  Andare sulla porta dopo
    lascerebbe due servizi vivi sugli stessi dati, che e' peggio del codice
    vecchio — quello almeno e' coerente con se stesso.
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
        # Le compilazioni datate: si passa esplicitamente, cosi' il posto dove
        # finiscono gli ordini pronti sta scritto qui e non dipende da come il
        # servizio deduce un percorso predefinito.
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
    """Percorso di Chrome, oppure None se non è installato su questo computer."""

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
    # Il comparatore si apre in Chrome perché è il programma di navigazione scelto
    # dall'utente: il predefinito di Windows qui è Edge. Se Chrome manca si ripiega
    # sul predefinito invece di fermare l'avvio: meglio la pagina aperta altrove
    # che nessuna pagina.
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
    """Le cose che non fermano l'avvio ma fermano il lavoro.

    ⚠ `--check` diceva `[OK]` e usciva zero anche quando i listini non si
    sarebbero potuti compilare, quando la porta era occupata da un altro
    programma e quando la cartella delle copie non era scrivibile. E' il
    comando che si suggerisce a chi «non riesce ad avviare»: rispondere «va
    tutto bene» a una macchina su cui il lavoro non si puo' finire e' peggio
    che non rispondere.

    Restano avvisi e non guasti — l'avvio ci riesce lo stesso — quindi il
    codice d'uscita non cambia: quello che cambia e' che si leggono.
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

    # Importato qui e non in cima: il lanciatore deve poter partire anche su una
    # macchina in cui la parte AI non si importa, e questa e' l'unica riga che
    # ne ha bisogno.
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
    """Dove tenere le copie di sicurezza, fuori dal progetto.

    Fuori e' la condizione, non un dettaglio: dentro il repository tornerebbe
    il problema di partenza, perche' l'avvio del PC del negozio riporta
    indietro i file tracciati e `.gitignore` ignora tutto `app/data/`.

    Il percorso si sceglie qui e non si scrive in una costante, perche' il
    programma deve girare su Windows (il negozio) e su macOS (dove si
    sviluppa): `%LOCALAPPDATA%` su macOS non esiste, e un ramo che dipende da
    lei salterebbe sempre proprio sulla macchina in cui si prova.

    ⚠ I tre parametri esistono per una ragione sola: **poter provare il ramo
    di Windows su una macchina che Windows non e'**. Non si passano mai
    nell'uso normale. Cambiare `os.name` dall'esterno, che sarebbe l'altra
    strada, rompe `pathlib` a meta' prova.
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
    """Uno zip datato delle memorie non rigenerabili, o `None` se non si e' fatto.

    ⚠ Si chiama DOPO che il server e' partito, mai prima: `memoria_ai.json` da
    solo pesa 2,1 MB, e comprimerlo davanti all'apertura della pagina
    ritarderebbe l'unica cosa che l'utente sta aspettando.

    ⚠ Non solleva mai. Una copia e' una rete di sicurezza, non una condizione
    per lavorare: un disco pieno o una cartella non scrivibile devono lasciare
    il programma acceso, non spegnerlo.

    ⚠ Ma non riuscire in silenzio e' un'altra cosa. Fino al 22 agosto 2026
    questa funzione tornava `None` sia quando non c'era niente da copiare sia
    quando la copia non si era potuta fare, e chi la chiama stampava una riga
    **solo se era andata bene**: cartella non scrivibile, disco pieno,
    antivirus sul temporaneo, e l'avvio sembrava normale. Si continuava a
    lavorare credendo che la rete di sicurezza ci fosse — ed e' l'unica copia
    che `conferme.db` ha, perche' quella memoria nessun ricalcolo la sa rifare.
    `su_guasto` riceve il motivo, in italiano, e chi chiama decide dove
    scriverlo.
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
        # Un temporaneo accanto e poi `os.replace`: se la compressione si
        # interrompe a meta', la copia di ieri e' ancora buona invece di
        # essere stata sostituita da un archivio monco.
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
    """Le copie piu' vecchie si cancellano: il nome porta la data, quindi
    l'ordine alfabetico e' gia' l'ordine del tempo."""

    try:
        archivi = sorted(cartella.glob("*.zip"))
    except OSError:
        return
    for vecchio in archivi[:-quante] if len(archivi) > quante else []:
        try:
            vecchio.unlink()
        except OSError:
            # Una copia vecchia che non si cancella non fa danno a nessuno.
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
        # ⚠ Ctrl+C non passa da `/api/spegni`, che si rifiuta di spegnere mentre
        # la catena lavora: uccide il processo e basta. La ragione di quel
        # rifiuto vale lo stesso — una run uccisa a meta' lascia una cartella
        # orfana e le risposte AI gia' pagate da rifare — quindi qui si dice,
        # perche' questa e' l'unica riga che chi ordina legge prima di premerlo.
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
