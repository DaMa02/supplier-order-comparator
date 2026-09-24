#!/usr/bin/env python3
"""Pipeline orchestrator: from the "recompute" button to a ready comparison.

Runs the nine steps in sequence, reading each one's output to decide whether
the next can proceed, so a failure cannot look like a success.

Three rules govern the rest of this module.

1. Each run gets its own dated folder under `<data>/esecuzioni/`, created
   empty. This is not archival convenience: it is what makes "a skipped step
   leaves yesterday's file lying around" impossible. An artifact a step was
   supposed to produce and didn't is a declared failure, not a stale file
   re-read as fresh.
2. The live comparison is replaced at the end, in one atomic step. If a phase
   fails, `review_data.json` stays exactly as it was: the user loses the run,
   not the comparison they were working from.
3. The numeric sanity check warns and never blocks. There are exactly three
   stops, and they cover the cases where the program has no authority to
   decide because the input cannot be used at all: a layout the adapter
   registry doesn't recognize, a document that fails to parse, and a price
   list that yields zero orderable rows (`FORNITORE_SENZA_RIGHE`): a supplier
   deliberately loaded and then silently dropped from the comparison would
   let orders go out without them, at potentially worse prices, while a hard
   stop only costs a re-run — the live comparison stays untouched.

What this module does not do: it never sends orders and never touches the
suppliers' original files. The live comparison changes only at activation;
the only other persistent write is to the adapter registry, and only after a
confirmed mapping has already passed manifest validation, parsing and build.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


APP_DIR = Path(__file__).resolve().parent
SKILL_ROOT = APP_DIR.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
REFERENCES_DIR = SKILL_ROOT / "references"

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import consegna  # noqa: E402
# "Is this offer orderable" has exactly one answer, kept in its own module
# rather than duplicated here, so the rule can evolve in one place.
from offerta import offer_is_available, offer_supplier_id  # noqa: E402
import registro  # noqa: E402
import scrittura_sicura  # noqa: E402
import schema_mapping  # noqa: E402
import versione_del_codice  # noqa: E402
from build_review_data import impronta_articolo  # noqa: E402
from conferme import impronta_prodotto  # noqa: E402


# --------------------------------------------------------------------------
# Contract with the page
# --------------------------------------------------------------------------

FASI: tuple[str, ...] = (
    "PROFILAZIONE",
    "RICONOSCIMENTO",
    "VALIDAZIONE",
    "PARSING",
    "SHORTLIST",
    "VALUTAZIONE_AI",
    "RISOLUZIONE",
    "COSTRUZIONE",
    "ATTIVAZIONE",
)

TITOLI_FASI: dict[str, str] = {
    "PROFILAZIONE": "Lettura dei documenti",
    # These titles show up on the page, especially when something fails:
    # internal names like "schema" or "manifest" tell the user nothing about
    # whether to retry or go check the documents.
    "RICONOSCIMENTO": "Riconoscimento delle colonne",
    "VALIDAZIONE": "Controllo dell'elenco dei documenti",
    "PARSING": "Lettura dei listini",
    "SHORTLIST": "Candidati per i prodotti senza EAN",
    "VALUTAZIONE_AI": "Valutazione dei candidati",
    "RISOLUZIONE": "Unione delle decisioni",
    "COSTRUZIONE": "Costruzione del confronto",
    "ATTIVAZIONE": "Attivazione del confronto",
}

# Seconds a step may stay silent before it's considered stuck. These are
# ceilings, not expected durations: no legitimate run gets close (the AI
# phase, the longest, measured 105s) — they're wide because the store's PC is
# slower than the one used to measure.
#
# The timeout measures silence, not total duration: it resets on every line
# the step prints. A long but healthy AI phase (many cases, a slow machine, a
# slow model) must not be killed and reported as unresponsive just because
# the wall-clock timer started at the beginning. For steps that print
# nothing, the two measures coincide. Without a timeout, a stuck step holds
# `lucchetto_lavori` forever: every following "Ricalcola" gets a 409,
# `POST /api/spegni` refuses to shut down, and the launcher reuses the stale
# server — the only path a code update reaches the store through gets
# blocked too, leaving the user to force-close the window.
TETTO_DELLE_FASI: dict[str, float] = {"VALUTAZIONE_AI": 900.0}
TETTO_PREDEFINITO_DI_FASE = 300.0


def tetto_della_fase(fase: str) -> float:
    """Seconds of silence after which this step is considered stuck."""

    return TETTO_DELLE_FASI.get(fase, TETTO_PREDEFINITO_DI_FASE)


IN_ATTESA = "IN_ATTESA"
IN_CORSO = "IN_CORSO"
COMPLETATO = "COMPLETATO"
ERRORE = "ERRORE"
INTERROTTO = "INTERROTTO"

STATI_IN_CORSO = {IN_CORSO}

NOME_STATO = "pipeline_status.json"
NOME_AUDIT_ESECUZIONE = "esecuzione.json"
NOME_DECISIONI_MANUALI = "decisioni_schemi.json"

# A run folder is not an order compilation: the two roots are kept separate
# so the user's archive ("orders") never mixes with working artifacts that
# nobody needs after a few weeks.
NOME_RADICE_ESECUZIONI = "esecuzioni"

# How many run folders to keep. Every recompute, successful or not, leaves
# one behind (measured at ~13 MB on the real comparison), so this bounds disk
# growth. It's not the space itself: a full disk is what triggers the
# failures this file exists to avoid (pipeline state that can't be written,
# a lock left held, a backup copy that silently fails). Five is more than a
# month of normal use; the two folders still needed — the run in progress and
# the one behind the live comparison — are never counted against this cap.
ESECUZIONI_DA_TENERE = 5

# How much a price list's row count can change from the previous run before
# it's worth mentioning. Not a blocking threshold: just where the warning
# starts showing at the top of the page.
SCARTO_RIGHE_DA_SEGNALARE = 0.15
# The median price is watched more closely: a price list where every price
# shifts by 10% is something worth knowing before an order goes out.
SCARTO_PREZZO_DA_SEGNALARE = 0.10

MASSIMO_AVANZAMENTO_STDERR = 4000

PREFISSO_AVANZAMENTO = "AVANZAMENTO "

# The code on disk is no longer the code running in memory. Not a bug, and
# the user can fix it themselves, so it gets its own code: "retry" would be
# the wrong advice here — the fix is to close and reopen.
CODICE_CAMBIATO_DOPO_L_AVVIO = "CODICE_CAMBIATO_DOPO_L_AVVIO"


class LavoroGiaInCorso(RuntimeError):
    """A second recompute was requested while the first is still running."""


@dataclass
class Fermata(Exception):
    """The pipeline stops because the user needs to act.

    Not a program failure: one of the two cases where the program has no
    authority to decide on its own. Carries the code, the message to show,
    and the documents involved, because "can't continue" without names is an
    obstacle, not a message.
    """

    codice: str
    messaggio: str
    documenti: list[str] = field(default_factory=list)
    dettaglio: str = ""
    # For each document involved, why it's here: "SCONOSCIUTO" (the registry
    # doesn't know it) or "VARIATO" (it knows it, but the document no longer
    # matches the stored signature). `documenti` stays a flat list because
    # that's what the service needs to know which files to configure; the
    # reason is a separate question. Merging the two fields would make the
    # page report unrecognized columns for a supplier the registry knows
    # whose layout has changed.
    motivi: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - only for tracebacks
        return self.messaggio


class ComandoTroppoLungo(Exception):
    """A step didn't respond within its timeout and was killed.

    Doesn't carry the phase name because the command runner doesn't know it;
    `_esegui` attaches it, and is the only place that knows the phase name.
    """

    def __init__(self, secondi: float, stderr: str = "") -> None:
        super().__init__(f"nessuna risposta entro {secondi:g} secondi")
        self.secondi = secondi
        self.stderr = stderr


@dataclass(frozen=True)
class RisultatoComando:
    """What a pipeline step did: exit code, stdout, stderr."""

    uscita: int
    stdout: str
    stderr: str

    @property
    def riepilogo(self) -> dict[str, Any]:
        """The last JSON object printed by the command, or `{}`.

        Every pipeline script ends by printing its own summary; that's where
        the numbers the page shows come from. A command that prints something
        else isn't a failure — the number is simply absent.
        """

        testo = self.stdout.strip()
        if not testo:
            return {}
        inizio = testo.find("{")
        while inizio != -1:
            try:
                valore = json.loads(testo[inizio:])
            except json.JSONDecodeError:
                inizio = testo.find("{", inizio + 1)
                continue
            return valore if isinstance(valore, dict) else {}
        return {}


AscoltaAvanzamento = Callable[[dict[str, Any]], None]
# `...` rather than the full signature: the runner also receives
# `timeout_secondi` as a keyword argument, and `Callable` can't express
# keyword-only parameters.
Esecutore = Callable[..., RisultatoComando]


def esegui_comando(
    comando: Sequence[str],
    cartella: Path,
    avanzamento: AscoltaAvanzamento,
    *,
    timeout_secondi: float | None = None,
) -> RisultatoComando:
    """Run a pipeline step as a subprocess, reading progress as it goes.

    `stdout` is collected in full since it carries the final summary;
    `stderr` is read line by line while the command runs, since that's how
    progress from the AI phase (which alone takes about two minutes) is
    reported. A program silent for two minutes looks broken.

    `PYTHONIOENCODING` is not cosmetic: without it, on Windows the child
    writes to the pipe using the system codepage, and the first Italian
    curly quote in a message crashes it with `UnicodeEncodeError` after the
    work is already done.
    """

    ambiente = dict(os.environ)
    ambiente["PYTHONIOENCODING"] = "utf-8"
    processo = subprocess.Popen(
        list(comando),
        cwd=str(cartella),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=ambiente,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    pezzi_errore: list[str] = []
    # Last sign of life from the child. Makes the timeout measure what its
    # name says — how long a step can stay silent — rather than total
    # duration: `wait(timeout=...)` alone would kill a long but healthy phase
    # and report it as unresponsive. For steps that print nothing the two
    # measures coincide; it matters for the AI phase, which emits a progress
    # line per case.
    ultimo_segno = [time.monotonic()]

    def leggi_errore() -> None:
        assert processo.stderr is not None
        for riga in processo.stderr:
            ultimo_segno[0] = time.monotonic()
            pezzi_errore.append(riga)
            testo = riga.strip()
            if not testo.startswith(PREFISSO_AVANZAMENTO):
                continue
            try:
                evento = json.loads(testo[len(PREFISSO_AVANZAMENTO):])
            except json.JSONDecodeError:
                continue
            if isinstance(evento, dict):
                try:
                    avanzamento(evento)
                except Exception:  # pragma: no cover - l'avanzamento non ferma nulla
                    pass

    pezzi_uscita: list[str] = []

    def leggi_uscita() -> None:
        if processo.stdout is not None:
            pezzi_uscita.append(processo.stdout.read())

    lettore = threading.Thread(target=leggi_errore, name="pipeline-stderr", daemon=True)
    lettore.start()
    # `stdout` is also read on its own thread, not for symmetry: `read()`
    # only returns at EOF, i.e. when the child dies or closes the pipe.
    # Reading it inline would block before `wait` is ever reached, so a
    # timeout on `wait` alone would never fire — a stuck-process guard that
    # looks correct but doesn't work.
    lettore_uscita = threading.Thread(target=leggi_uscita, name="pipeline-stdout", daemon=True)
    lettore_uscita.start()

    def chiudi() -> None:
        """A pipe is closed only once its reader thread has finished.

        `close()` on a `BufferedReader` wants the reader's internal lock; if a
        thread is still inside `read()`, closing blocks forever — holding
        `lucchetto_lavori`, exactly the stall the timeout exists to prevent.
        This is a real case, not theoretical: `kill()` only kills the direct
        child, and if that child had spawned a descendant that inherited the
        pipe, EOF never arrives. In that case the two descriptors stay open
        until the process dies, which is an acceptable cost against a stuck
        program.
        """

        # Five seconds total, not five per thread: this is the time given to
        # cleanup, and doubling it because there are two readers would double
        # the user's wait in front of a dead step too.
        scadenza = time.monotonic() + 5
        lettore.join(timeout=max(0.0, scadenza - time.monotonic()))
        lettore_uscita.join(timeout=max(0.0, scadenza - time.monotonic()))
        if processo.stdout is not None and not lettore_uscita.is_alive():
            processo.stdout.close()
        if processo.stderr is not None and not lettore.is_alive():
            processo.stderr.close()

    def aspetta_finche_da_segni() -> int:
        """Wait for the child, killing it only after too long a silence.

        The timeout resets on every line that arrives: a step that's working
        and saying so can run as long as it needs; one that stops responding
        gets killed after `timeout_secondi` of silence. With no timeout (the
        case in tests), this just waits.
        """

        if timeout_secondi is None:
            return processo.wait()
        while True:
            rimasto = timeout_secondi - (time.monotonic() - ultimo_segno[0])
            if rimasto <= 0:
                raise subprocess.TimeoutExpired(processo.args, timeout_secondi)
            try:
                # At most one second per poll: this is how the clock gets
                # re-checked, not extra waiting.
                return processo.wait(timeout=min(rimasto, 1.0))
            except subprocess.TimeoutExpired:
                continue

    try:
        try:
            codice = aspetta_finche_da_segni()
        except subprocess.TimeoutExpired:
            processo.kill()
            # A timeout here too: SIGKILL itself can't be ignored, but a
            # child stuck in uninterruptible I/O would hang this `wait`,
            # which is the same stall all over again.
            try:
                processo.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise ComandoTroppoLungo(
                secondi=float(timeout_secondi or 0.0), stderr="".join(pezzi_errore)[-4000:]
            ) from None
    finally:
        # In `finally`, not in either branch: whatever happens here — even a
        # `KeyboardInterrupt` — the pipes and threads get closed.
        chiudi()
    return RisultatoComando(
        uscita=codice, stdout="".join(pezzi_uscita), stderr="".join(pezzi_errore)
    )


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigurazionePipeline:
    """Where things live. No path is hardcoded in the code."""

    data_dir: Path
    uploads_dir: Path
    review_path: Path
    state_path: Path
    esecuzioni_dir: Path | None = None
    adapters_path: Path = REFERENCES_DIR / "adapters.json"
    scripts_dir: Path = SCRIPTS_DIR
    python_executable: str = sys.executable
    top_k: int = 5
    soglia_ordine: float = 1000.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        object.__setattr__(self, "uploads_dir", Path(self.uploads_dir).resolve())
        object.__setattr__(self, "review_path", Path(self.review_path).resolve())
        object.__setattr__(self, "state_path", Path(self.state_path).resolve())
        object.__setattr__(self, "adapters_path", Path(self.adapters_path).resolve())
        object.__setattr__(self, "scripts_dir", Path(self.scripts_dir).resolve())
        radice = self.esecuzioni_dir or (self.data_dir / NOME_RADICE_ESECUZIONI)
        object.__setattr__(self, "esecuzioni_dir", Path(radice).resolve())

    @property
    def decisioni_manuali_path(self) -> Path:
        """Manually written decisions for layouts the registry doesn't know."""

        return self.data_dir / NOME_DECISIONI_MANUALI


def _frase_dell_errore(voce: Any) -> str:
    """One manifest error entry, phrased for whoever places the orders.

    A naive `str(voce)` would print the raw Python dict — braces, quotes and
    all — to a user in the UI. The full dict is kept in `dettaglio`, where
    it belongs.
    """

    if not isinstance(voce, dict):
        return str(voce)
    testo = str(voce.get("message") or voce.get("code") or "").strip()
    nome = str(voce.get("file") or "").strip()
    if nome and testo:
        return f"«{nome}»: {testo}"
    return testo or nome or "errore senza descrizione"


def utc_ora() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def json_sicuro(valore: Any) -> Any:
    if isinstance(valore, Path):
        return str(valore)
    if isinstance(valore, Mapping):
        return {str(chiave): json_sicuro(voce) for chiave, voce in valore.items()}
    if isinstance(valore, (list, tuple)):
        return [json_sicuro(voce) for voce in valore]
    if valore is None or isinstance(valore, (str, int, float, bool)):
        return valore
    return str(valore)


def scrivi_json(percorso: Path, valore: Any) -> None:
    """Written in binary with LF line endings, like the rest of the project.

    The write pattern (temp file, `fsync`, `os.replace`) lives in
    `scrittura_sicura`, the single shared implementation. `json_sicuro` stays
    here because this is the one caller that also needs to serialize objects
    JSON doesn't know about (e.g. `Path`).
    """

    scrittura_sicura.scrivi_json(percorso, json_sicuro(valore), a_capo_finale=True)

def leggi_json(percorso: Path, predefinito: Any = None) -> Any:
    try:
        return json.loads(percorso.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return deepcopy(predefinito)


def impronta_file(percorso: Path) -> str:
    digest = hashlib.sha256()
    with percorso.open("rb") as flusso:
        for pezzo in iter(lambda: flusso.read(1024 * 1024), b""):
            digest.update(pezzo)
    return digest.hexdigest()


def _numero(valore: Any) -> float | None:
    if isinstance(valore, bool) or valore is None:
        return None
    if isinstance(valore, (int, float)):
        return float(valore)
    testo = str(valore).strip().replace(",", ".")
    try:
        return float(testo)
    except ValueError:
        return None


def articolo_della_riga(prodotto: Any) -> str:
    """What item this row of the reorder list actually holds, row number aside.

    A product's id is the row number in the management-software export
    (`product:3` is row 3). With a new export, row 3 can be a different item,
    and a decision made on the old row 3 doesn't apply to it. This returns
    the fingerprint that says whether the item is still the same: the
    barcode when there is one, otherwise the normalized name.
    """

    if not isinstance(prodotto, dict):
        return ""
    ean = str(prodotto.get("ean") or prodotto.get("barcode") or "").strip()
    if ean:
        return f"ean:{ean}"
    testo = str(prodotto.get("description") or prodotto.get("name") or "").strip()
    if not testo:
        return ""
    return "nome:" + " ".join(testo.split()).casefold()


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

class PipelineJobManager:
    """Owns a single pipeline run and exposes its status as JSON.

    Doesn't know what an HTTP route is: `avvia()` and `stato()` are plain
    functions returning dicts, and it's the server that puts them behind a URL.
    """

    def __init__(
        self,
        configurazione: ConfigurazionePipeline,
        *,
        lucchetto_lavori: threading.Lock | None = None,
        lucchetto_dati: threading.RLock | None = None,
        esecutore: Esecutore = esegui_comando,
        su_confronto_attivato: Callable[[dict[str, Any]], None] | None = None,
        uguaglianze_dichiarate: Callable[[], list[list[str]]] | None = None,
        rifiuti_dichiarati: Callable[[], dict[tuple[str, str], dict[str, Any]]] | None = None,
    ) -> None:
        self.configurazione = configurazione
        self.esecutore = esecutore
        self.su_confronto_attivato = su_confronto_attivato
        # Who knows which barcodes the user has declared equivalent. A
        # function rather than a path because the confirmations store has a
        # single owner — the service — and two objects opening the same
        # SQLite file is the classic way to discover too late that one of
        # them had it locked. The answer is requested here and written into
        # the run folder, where it stays as a record of which declarations
        # applied.
        self.uguaglianze_dichiarate = uguaglianze_dichiarate
        # Who knows which offers the user rejected with "not the same item".
        # Same shape as the equivalences and for the same reason: the
        # confirmations store has a single owner. This is needed here, not
        # only for display: without it, `_ripulisci_stato` would see the
        # rejected offer as still usable, conclude "someone can still supply
        # this" and zero out the quantity — dropping the product out of
        # "items to source", exactly the dead end that rejection was meant
        # to close.
        self.rifiuti_dichiarati = rifiuti_dichiarati
        # One single lock for every job that touches `review_data.json`: two
        # jobs replacing it at once would leave a comparison half one and
        # half the other.
        self.lucchetto_lavori = lucchetto_lavori or threading.Lock()
        # The same lock the HTTP routes use (`ReviewStore.lock`), not a
        # second one: it keeps the page's own save-state calls out of the
        # one moment the pipeline touches the data the routes serve — the
        # cleaned-up state and the swap of the live comparison. The lock
        # order is always `lucchetto_lavori` first and this one second,
        # never reversed: no route ever takes the jobs lock, so a deadlock
        # has no way to form. An `RLock` because that's what the service
        # uses, and the pipeline only acquires it once.
        self.lucchetto_dati = lucchetto_dati or threading.RLock()
        self._lucchetto = threading.RLock()
        self._filo: threading.Thread | None = None
        # Logged once: state gets rewritten on every phase change and every
        # progress tick, and a console full of the same line helps no one.
        self._stato_non_si_scrive = False
        self._percorso_stato = configurazione.data_dir / NOME_STATO
        configurazione.esecuzioni_dir.mkdir(parents=True, exist_ok=True)

        stato = leggi_json(self._percorso_stato, None)
        self._stato = stato if isinstance(stato, dict) else self._stato_in_attesa()
        if self._stato.get("stato") in STATI_IN_CORSO:
            # The server was shut down while the pipeline was running: that
            # run doesn't resume on its own, and saying so is better than
            # leaving a progress bar on the page that will never move again.
            self._stato.update({
                "ok": False,
                "stato": INTERROTTO,
                "messaggio": (
                    "Il confronto precedente è stato interrotto: quello attivo è "
                    "rimasto quello di prima. Si può rifare."
                ),
                "aggiornatoIl": utc_ora(),
            })
            self._salva_stato()
        elif (
            self._stato.get("stato") == ERRORE
            and (self._stato.get("fermata") or {}).get("code") == "SCHEMA_SCONOSCIUTO"
        ):
            richiesti = [
                str(nome) for nome in ((self._stato.get("fermata") or {}).get("documenti") or [])
                if str(nome).strip()
            ]
            if richiesti and any(
                consegna.file_sicuro(self.configurazione.uploads_dir, nome) is None
                for nome in richiesti
            ):
                self._stato = self._stato_in_attesa()
                self._stato["messaggio"] = "I documenti sono cambiati. Ricalcola il confronto quando sono pronti."
                self._salva_stato()

    # -- lo stato ----------------------------------------------------------

    def _stato_in_attesa(self) -> dict[str, Any]:
        return {
            "ok": True,
            "stato": IN_ATTESA,
            "fase": None,
            "messaggio": "Nessun confronto in corso.",
            "runId": None,
            "cartella": None,
            "iniziatoIl": None,
            "aggiornatoIl": utc_ora(),
            "completatoIl": None,
            "fasi": [self._fase_vuota(nome) for nome in FASI],
            "avanzamento": {"fatte": 0, "totali": len(FASI), "percento": 0.0},
            "numeri": {},
            "avvisi": [],
            "fermata": None,
        }

    @staticmethod
    def _fase_vuota(nome: str) -> dict[str, Any]:
        return {
            "nome": nome,
            "titolo": TITOLI_FASI.get(nome, nome),
            "stato": IN_ATTESA,
            "dettaglio": "",
            "durataSecondi": None,
        }

    def _salva_stato(self) -> None:
        """Write the state to disk; a failure here does not stop the pipeline.

        If `pipeline_status.json` can't be written (full disk, read-only
        folder, antivirus locking the temp file), raising here would surface
        the exception from inside `_lavora`'s own error handler, which calls
        `_segna_fase` and retries the same failed write — the second
        exception would escape before the state ever became `ERRORE`: the
        thread dies, the lock is released, and the page is left with a
        progress bar stuck on "in progress" forever, with recompute and
        uploads both disabled.

        The state that matters to the page is the in-memory one — `stato()`
        reads that — and the file serves only two purposes: surviving a
        service restart, and telling a reopened page "interrupted". Losing it
        is a degradation, not a failure: log it once and move on.
        """

        try:
            scrivi_json(self._percorso_stato, self._stato)
        except OSError as guasto:
            if not self._stato_non_si_scrive:
                self._stato_non_si_scrive = True
                print(
                    f"[AVVISO] Non riesco a scrivere «{self._percorso_stato.name}» "
                    f"({type(guasto).__name__}: {guasto}). Il confronto va avanti lo stesso; "
                    "se il programma viene chiuso adesso, alla riapertura non sapra' dire "
                    "a che punto era."
                )

    def _aggiorna(self, **cambiamenti: Any) -> None:
        with self._lucchetto:
            self._stato.update(json_sicuro(cambiamenti))
            self._stato["aggiornatoIl"] = utc_ora()
            self._salva_stato()

    def _segna_fase(self, nome: str, stato: str, dettaglio: str = "", durata: float | None = None) -> None:
        """`durata` comes from `time.monotonic()`, not the wall clock.

        A wall clock can jump underneath a long-running phase (an NTP
        correction, a DST change), which would write a wrong or negative
        duration here and in the run's audit trail — exactly the number used
        later to judge whether a phase was slow. `time.monotonic()` never
        goes backward and has no timezone: it's a second counter, not a
        date. The other timestamps stay as they are: `utc_ora()` is already
        explicit UTC, and the `datetime.now().astimezone()` that names the
        run folder is meant to stay local time.
        """

        with self._lucchetto:
            for voce in self._stato.get("fasi") or []:
                if voce.get("nome") != nome:
                    continue
                voce["stato"] = stato
                if dettaglio:
                    voce["dettaglio"] = dettaglio
                if durata is not None:
                    voce["durataSecondi"] = round(durata, 2)
            fatte = sum(1 for voce in self._stato.get("fasi") or [] if voce.get("stato") == COMPLETATO)
            self._stato["avanzamento"] = {
                "fatte": fatte,
                "totali": len(FASI),
                "percento": round(fatte * 100 / len(FASI), 1),
            }
            if stato == IN_CORSO:
                self._stato["fase"] = nome
                self._stato["messaggio"] = TITOLI_FASI.get(nome, nome) + "…"
            self._stato["aggiornatoIl"] = utc_ora()
            self._salva_stato()

    def _numeri(self, **valori: Any) -> None:
        with self._lucchetto:
            numeri = dict(self._stato.get("numeri") or {})
            numeri.update(json_sicuro(valori))
            self._stato["numeri"] = numeri
            self._stato["aggiornatoIl"] = utc_ora()
            self._salva_stato()

    def _metti_da_parte_gli_adattatori_superati(self) -> None:
        """Before recognizing any document: the shipped adapter wins if newer.

        Applies `registro._motivo_del_superamento` once per run, so the
        warning fires once instead of on every registry read. Runs before
        profiling because that's where `inspect_sources` calls
        `registro.riconosci`, and an outdated learned entry would otherwise
        claim the document ahead of the shipped one. A registry that can't be
        read doesn't stop here — the phase that actually needs it will
        report that.
        """

        try:
            messe = registro.metti_da_parte_le_superate(self.configurazione.adapters_path)
        except Exception as exc:  # noqa: BLE001 - defensive boundary: proceed with the registry as it is
            print(f"[AVVISO] adattatori superati non messi da parte — {type(exc).__name__}: {exc}")
            return
        for scheda in messe:
            nome = str(scheda.get("display_name") or scheda.get("supplier_id") or scheda.get("base") or "").strip()
            self._avvisa(
                "ADATTATORE_MESSO_DA_PARTE",
                f"{nome.upper()}: vale la versione aggiornata del suo listino",
                f"La disposizione delle colonne di {nome.upper()} imparata su questo computer è "
                "stata messa da parte: con l'aggiornamento del programma ne è arrivata una più "
                "recente, e da adesso vale quella. Se il suo listino non viene riconosciuto, "
                "controlla le colonne nell'anteprima e conferma: verrà imparato di nuovo.",
                adattatore=scheda.get("id"),
                dettaglio=scheda.get("motivo"),
            )

    def _avvisa(self, codice: str, titolo: str, messaggio: str,
                *, severita: str = "warning", **extra: Any) -> None:
        """A warning never blocks anything: warnings never halt the pipeline.

        Goes into both the job's live state — shown while it's running — and,
        at activation, into `review_data.json`, since two minutes later
        nobody is watching the progress bar anymore.

        `severita` exists for the few warnings that must not be skimmed past:
        `"error"` renders them in red at the top of the page and in the
        document, while `blocking` stays `False` — order compilation isn't
        stopped, per the rule that the numeric check warns and never blocks.
        There's no way to mute them: a warning that can be silenced
        eventually gets silenced.
        """

        with self._lucchetto:
            avvisi = list(self._stato.get("avvisi") or [])
            avvisi.append(json_sicuro({
                "code": codice,
                "severity": severita,
                "blocking": False,
                "title": titolo,
                "message": messaggio,
                **extra,
            }))
            self._stato["avvisi"] = avvisi
            self._stato["aggiornatoIl"] = utc_ora()
            self._salva_stato()

    def stato(self) -> dict[str, Any]:
        with self._lucchetto:
            return deepcopy(self._stato)

    def input_modificato(
        self,
        tipo: str = "",
        documenti: list[str] | None = None,
        fornitori: list[str] | None = None,
    ) -> dict[str, Any]:
        """Discard the outcome of a run whose documents have since changed.

        Run folders stay around as an audit trail, but the page must not try
        to configure a file that has since been deleted or replaced. A run
        still in progress keeps its own state instead: if files change while
        it's working, its own checks will stop it safely.

        `cambiamento` is the field that lights up the banner on the page
        saying which supplier's price list changed since the last comparison
        and that the prices currently shown are stale. What isn't declared
        here is left as-is — the state just goes back to idle — because a
        banner that can't say *what* changed helps no one.
        """

        with self._lucchetto:
            if self.in_corso():
                return deepcopy(self._stato)
            self._stato = self._stato_in_attesa()
            self._stato["messaggio"] = "I documenti sono cambiati. Ricalcola il confronto quando sono pronti."
            nomi = [str(voce).strip() for voce in (documenti or []) if str(voce).strip()]
            etichette = [str(voce).strip() for voce in (fornitori or []) if str(voce).strip()]
            if tipo in {"eliminato", "caricato", "colonne"} and (nomi or etichette):
                self._stato["cambiamento"] = {
                    "tipo": tipo,
                    "documenti": nomi,
                    "fornitori": etichette,
                }
            self._salva_stato()
            return deepcopy(self._stato)

    def _contesto_mappatura(self) -> tuple[dict[str, Any], Path, list[dict[str, Any]], list[str]]:
        """Returns only the stopped run that the page can configure."""

        stato = deepcopy(self._stato)
        fermata = stato.get("fermata") or {}
        if stato.get("stato") != ERRORE or fermata.get("code") != "SCHEMA_SCONOSCIUTO":
            raise ValueError("Non ci sono documenti in attesa di configurazione.")
        run_id = str(stato.get("runId") or "")
        cartella = consegna.cartella_sicura(self.configurazione.esecuzioni_dir, run_id)
        if cartella is None:
            raise ValueError("La cartella del confronto da configurare non esiste più.")
        dichiarata = Path(str(stato.get("cartella") or "")).resolve()
        if dichiarata != cartella.resolve():
            raise ValueError("Lo stato del confronto non coincide con la sua cartella.")
        documento = leggi_json(cartella / "input_profiles.json", None)
        profili = documento.get("profiles") if isinstance(documento, dict) else None
        if not isinstance(profili, list):
            raise ValueError("L'anteprima dei documenti non è più disponibile.")
        nomi = [str(nome) for nome in (fermata.get("documenti") or []) if str(nome).strip()]
        if not nomi:
            raise ValueError("Il confronto non indica quali documenti configurare.")
        radice_upload = self.configurazione.uploads_dir.resolve()
        for profilo in profili:
            if not isinstance(profilo, dict):
                raise ValueError("L'anteprima dei documenti non è valida.")
            percorso = Path(str(profilo.get("path") or "")).resolve()
            try:
                percorso.relative_to(radice_upload)
            except ValueError:
                raise ValueError("Un documento dell'anteprima non appartiene ai caricamenti.") from None
        return stato, cartella, profili, nomi

    # --- manual column selector --------------------------------------------
    #
    # The guided-mapping flow opens the selector only when the pipeline stops
    # on a layout the registry doesn't recognize. Recognized suppliers never
    # trigger it, so there was no way to review the columns of an
    # already-known price list — starting with the order-quantity column,
    # which decides where quantities land in the file sent back to the
    # supplier.
    #
    # Same machinery as guided mapping (`schema_mapping`): only the source of
    # the profiles changes (the uploads folder instead of a stopped run's
    # folder), and at the end nothing restarts automatically. The confirmed
    # mapping becomes a stored decision used by the next recompute, which the
    # user triggers themselves — a ten-minute run is never started without
    # being asked for. The page shows the same banner as a changed document,
    # because it is the same situation: the prices currently shown were read
    # with the previous columns.
    CHIAVE_COLONNE_A_MANO = "colonne-a-mano"

    def _profili_dei_caricamenti(self) -> list[dict[str, Any]]:
        documento = leggi_json(self.configurazione.uploads_dir / "upload_profiles.json", None)
        profili = documento.get("profiles") if isinstance(documento, dict) else None
        if not isinstance(profili, list):
            return []
        return [voce for voce in profili if isinstance(voce, dict)]

    def _documento_caricato(self, nome: Any) -> str:
        """The file name, checked against the uploads folder.

        The browser never picks a path directly: it picks among names the
        service has given it, and this checks the name against disk before
        opening anything.

        Two separate checks, both needed: the profiles registry rejects a
        made-up name; a file removed from the folder with Explorer, which
        still has an entry in that registry, is only caught by the disk check.
        """

        nome = str(nome or "").strip()
        if not nome:
            raise ValueError("Indica di quale documento vuoi rivedere le colonne.")
        if consegna.file_sicuro(self.configurazione.uploads_dir, nome) is None:
            raise ValueError("Questo documento non è fra i caricamenti.")
        if not any(
            str(profilo.get("file_name") or "").casefold() == nome.casefold()
            for profilo in self._profili_dei_caricamenti()
        ):
            raise ValueError("Di questo documento non c'è l'anteprima: ricaricalo e riprova.")
        return nome

    def colonne_del_documento(self, nome: Any) -> dict[str, Any]:
        with self._lucchetto:
            if self.in_corso():
                raise ValueError("Il confronto è in corso: le colonne si rivedono appena ha finito.")
            nome = self._documento_caricato(nome)
        adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
        return schema_mapping.prepara_pendenti(
            self._profili_dei_caricamenti(), [nome], adattatori, self.CHIAVE_COLONNE_A_MANO,
        )

    def prova_colonne_del_documento(self, payload: dict[str, Any]) -> dict[str, Any]:
        nome, profili, adattatori = self._contesto_colonne(payload)
        esito, _decisioni = schema_mapping.valida_mappature(
            profili, [nome], adattatori, payload, self.CHIAVE_COLONNE_A_MANO,
        )
        return esito

    def salva_colonne_del_documento(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store the mapping and mark the comparison as stale.

        Doesn't trigger a recompute: a ten-minute run is never started
        without the user asking for it. The next comparison uses the new
        columns, and the page shows the same banner as for a changed
        document — it's the same situation: the prices currently shown were
        read with the previous columns.
        """

        nome, profili, adattatori = self._contesto_colonne(payload)
        esito, decisioni = schema_mapping.valida_mappature(
            profili, [nome], adattatori, payload, self.CHIAVE_COLONNE_A_MANO,
        )
        with self._lucchetto:
            if self.in_corso():
                raise ValueError("Il confronto è partito nel frattempo: riprova quando ha finito.")
            self._salva_decisioni_confermate(decisioni)
        # The supplier name, when the mapping declares it: more informative
        # than the file name, and what the banner leads with.
        fornitori = [
            schema_mapping.nome_dichiarato(str(voce.get("supplier_id") or ""), adattatori)
            for voce in decisioni
            if str(voce.get("supplier_id") or "")
        ]
        stato = self.input_modificato(
            "colonne", documenti=[nome], fornitori=[voce for voce in fornitori if voce],
        )
        return {"ok": True, "validation": esito, "pipeline": stato}

    def _contesto_colonne(self, payload: Any) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida.")
        with self._lucchetto:
            if self.in_corso():
                raise ValueError("Il confronto è in corso: le colonne si rivedono appena ha finito.")
            nome = self._documento_caricato(payload.get("fileName"))
        return nome, self._profili_dei_caricamenti(), schema_mapping.carica_adattatori(
            self.configurazione.adapters_path
        )

    def schemi_pendenti(self) -> dict[str, Any]:
        with self._lucchetto:
            stato, _cartella, profili, nomi = self._contesto_mappatura()
            # The per-document reason comes from the stop itself, not
            # re-derived from the profile: it's the same authority that
            # decided to stop.
            motivi = (stato.get("fermata") or {}).get("motivi") or {}
            adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
            return schema_mapping.prepara_pendenti(
                profili, nomi, adattatori, str(stato.get("runId") or ""), motivi=motivi,
            )

    def colonne_dei_documenti(self) -> dict[str, Any]:
        """Which columns the live comparison used, document by document.

        Doesn't look at the pipeline's own state — it looks at the run that
        produced the comparison currently in use, which can differ: after a
        failed recompute the state describes the last attempt, while the
        prices on the page still come from the previous run, and it's that
        run's columns that need to be shown. The run id comes from the
        comparison itself (`run.pipelineRunId`), the same field the order
        compilation guard uses.

        Doesn't raise when there's nothing to show: a freshly installed
        program, a manually cleaned-up run folder, or a comparison from an
        older version are all normal ways to have no answer, and on the page
        they all read as "columns unknown for this document".
        """

        run_id, motivo, voci = self._documenti_della_run()
        if motivo:
            return {"ok": True, "runId": run_id, "documents": [], "motivo": motivo}
        documenti = [
            schema_mapping.mappatura_effettiva(profilo, adattatore, decisione)
            for profilo, adattatore, decisione in voci
        ]
        return {"ok": True, "runId": run_id, "documents": documenti, "motivo": ""}

    def _documenti_della_run(
        self,
    ) -> tuple[str, str, list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]]]:
        """The documents of the run that produced the comparison in use.

        Returns `(runId, motivo, voci)`: when `motivo` is non-empty, `voci`
        is empty and that message is the reason, read on the page as
        "columns unknown for this document". Each entry is the triple
        `(profile, adapter, decision)` — the three pieces anyone reporting on
        a document needs: the profile carries the sample rows, the adapter is
        what the registry declares, the decision is what the user confirmed.
        """

        review = leggi_json(self.configurazione.review_path, {})
        run_id = ""
        if isinstance(review, dict):
            run_id = str((review.get("run") or {}).get("pipelineRunId") or "")
        if not run_id:
            return "", "Il confronto in uso non dice da quale elaborazione viene.", []
        cartella = consegna.cartella_sicura(self.configurazione.esecuzioni_dir, run_id)
        if cartella is None:
            return run_id, "La cartella di quel confronto non c'è più.", []
        documento = leggi_json(cartella / "input_profiles.json", None)
        profili = documento.get("profiles") if isinstance(documento, dict) else None
        manifest = leggi_json(cartella / "input_manifest.json", None)
        voci = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(profili, list) or not isinstance(voci, list):
            return run_id, "I file di quel confronto non sono più leggibili.", []
        # The decision (supplier, adapter, confirmed mapping) lives in the
        # manifest; the profile carries the sample rows. They're joined on
        # `profile_id`, the identifier both declare, not on the file name.
        per_profilo = {
            str(voce.get("profile_id") or ""): (voce.get("ai_preflight") or {})
            for voce in voci
            if isinstance(voce, dict)
        }
        adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
        risultato = []
        for profilo in profili:
            if not isinstance(profilo, dict):
                continue
            decisione = per_profilo.get(str(profilo.get("profile_id") or "")) or {}
            # `voce_in_uso`, not the raw id: this decision was made at
            # recompute time, and since then the order column may have been
            # moved from the page — which writes a `__locale` entry. Looking
            # up the shipped id directly would return the shipped column, and
            # the page would show a column that's no longer the one in use.
            adattatore = registro.voce_in_uso(decisione.get("adapter_id"), adattatori) or None
            risultato.append((profilo, adattatore, decisione))
        return run_id, "", risultato

    def documento_del_fornitore(self, supplier_id: str) -> dict[str, Any]:
        """This supplier's document in the current comparison, plus its columns.

        Used by the "change order column" flow: the same data the document
        card shows on page 1, plus the full list of sheet columns, which
        isn't needed there.

        The supplier is identified by the run's own decision, not by file
        name: the same supplier can send files with different names week to
        week, and tying the lookup to the name would lose it every time.
        """

        cercato = str(supplier_id or "").strip().casefold()
        run_id, motivo, voci = self._documenti_della_run()
        if motivo:
            raise ValueError(motivo)
        for profilo, adattatore, decisione in voci:
            if str(decisione.get("supplier_id") or "").strip().casefold() != cercato or not cercato:
                continue
            effettiva = schema_mapping.mappatura_effettiva(profilo, adattatore, decisione)
            return {
                "runId": run_id,
                "profilo": profilo,
                "adattatore": adattatore or {},
                "decisione": decisione,
                "effettiva": effettiva,
                "colonne": schema_mapping.colonne_del_foglio(
                    profilo,
                    effettiva.get("sheet"),
                    effettiva.get("headerRow"),
                    effettiva.get("dataStartRow"),
                    # The current order column can sit past the last column
                    # that has any data: a column empty on every row doesn't
                    # show up in the profile at all. Without this, the only
                    # new column choosable would be the one right next to the
                    # one already in use.
                    fino_a=(effettiva.get("orderColumn") or {}).get("colonna"),
                ),
            }
        raise ValueError(
            "Nel confronto in uso non c'è nessun listino di questo fornitore."
        )

    def valida_schemi(self, payload: dict[str, Any]) -> dict[str, Any]:
        # This check can read thousands of rows: it doesn't hold the state
        # lock while doing so. The run id is re-checked before returning.
        with self._lucchetto:
            stato, _cartella, profili, nomi = self._contesto_mappatura()
            run_id = str(stato.get("runId") or "")
        adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
        esito, _decisioni = schema_mapping.valida_mappature(
            profili, nomi, adattatori, payload, run_id
        )
        with self._lucchetto:
            if str(self._stato.get("runId") or "") != run_id:
                raise ValueError("Nel frattempo è partito un altro confronto. Riapri la configurazione.")
        return esito

    def conferma_schemi(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lucchetto:
            stato, _cartella, profili, nomi = self._contesto_mappatura()
            run_id = str(stato.get("runId") or "")
        adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
        esito, decisioni = schema_mapping.valida_mappature(
            profili, nomi, adattatori, payload, run_id
        )
        with self._lucchetto:
            if str(self._stato.get("runId") or "") != run_id:
                raise ValueError("Nel frattempo è partito un altro confronto. Riapri la configurazione.")
            self._salva_decisioni_confermate(decisioni)
            nuovo_stato = self.avvia()
        return {"ok": True, "validation": esito, "pipeline": nuovo_stato}

    def _salva_decisioni_confermate(self, nuove: list[dict[str, Any]]) -> None:
        """Replaces only the decisions for the documents just confirmed."""

        percorso = self.configurazione.decisioni_manuali_path
        documento = leggi_json(percorso, {})
        vecchie = documento.get("decisions") if isinstance(documento, dict) else documento
        if not isinstance(vecchie, list):
            vecchie = []
        nomi = {str(voce.get("file_name") or "").casefold() for voce in nuove}
        profili = {str(voce.get("profile_id") or "") for voce in nuove}
        tenute = [
            voce for voce in vecchie
            if isinstance(voce, dict)
            and str(voce.get("file_name") or "").casefold() not in nomi
            and str(voce.get("profile_id") or "") not in profili
        ]
        scrivi_json(percorso, {"decisions": [*tenute, *nuove]})

    def in_corso(self) -> bool:
        return self._filo is not None and self._filo.is_alive()

    # -- l'avvio -----------------------------------------------------------

    def avvia(self) -> dict[str, Any]:
        """Starts and returns immediately: the caller doesn't wait on the run."""

        with self._lucchetto:
            if self.in_corso():
                raise LavoroGiaInCorso("Un confronto è già in corso")
            if not self.lucchetto_lavori.acquire(blocking=False):
                raise LavoroGiaInCorso("Un altro lavoro sta già aggiornando il confronto")
            # From here until the thread actually starts, the lock is held by
            # no one in particular: anything going wrong in between would
            # otherwise keep it held forever, making every subsequent
            # "Ricalcola" fail and the shutdown route refuse to shut down —
            # the program stuck with nothing left to press on the page.
            # The `finally` below releases it on any failure in this
            # block, and hands it off to the worker thread only once that
            # thread actually exists — it's the thread that releases it when
            # the run finishes.
            partito = False
            try:
                momento = datetime.now().astimezone()
                cartella = consegna.crea_cartella(self.configurazione.esecuzioni_dir, momento)
                tolte = self._ripulisci_le_esecuzioni(cartella)
                if tolte:
                    # Logged, not silent: a silent cleanup is one nobody
                    # notices until they go looking for a folder that's gone.
                    print(f"[PULIZIA] Cartelle di lavoro vecchie rimosse: {tolte} "
                          f"(se ne tengono {ESECUZIONI_DA_TENERE}).")
                self._stato = {
                    "ok": True,
                    "stato": IN_CORSO,
                    "fase": FASI[0],
                    "messaggio": "Confronto avviato.",
                    "runId": cartella.name,
                    "cartella": str(cartella),
                    "iniziatoIl": utc_ora(),
                    "aggiornatoIl": utc_ora(),
                    "completatoIl": None,
                    "fasi": [self._fase_vuota(nome) for nome in FASI],
                    "avanzamento": {"fatte": 0, "totali": len(FASI), "percento": 0.0},
                    "numeri": {},
                    "avvisi": [],
                    "fermata": None,
                }
                self._salva_stato()
                self._filo = threading.Thread(
                    target=self._lavora,
                    args=(cartella,),
                    name=f"pipeline-{cartella.name}",
                    daemon=True,
                )
                self._filo.start()
                partito = True
                return deepcopy(self._stato)
            finally:
                if not partito:
                    try:
                        self.lucchetto_lavori.release()
                    except RuntimeError:  # pragma: no cover - non rilasciarlo due volte
                        pass

    def attendi(self, timeout: float | None = None) -> dict[str, Any]:
        """Convenience for tests and launchers; the page polls `stato()` instead."""

        filo = self._filo
        if filo is not None:
            filo.join(timeout=timeout)
        return self.stato()

    # -- il lavoro ---------------------------------------------------------

    def _verifica_il_codice_in_memoria(self) -> None:
        """A run does not start if the code on disk isn't what's running.

        Python loads top-level modules once, but a module imported inside a
        function is read from disk the first time that line executes — so a
        long-running server process can end up running a mix of old and new
        code after a deploy, and surface a raw Python traceback the user has
        no way to act on from the browser.

        This stops the run rather than warning: a run that's half old code
        and half new code can reach the end and silently write a wrong
        comparison, which is the worst way an unattended program can fail.
        Stopping costs nothing — the active comparison stays where it is —
        and the fix is in the user's hands: close and reopen.
        """

        avviso = versione_del_codice.avviso_del_codice_cambiato()
        if avviso:
            raise Fermata(codice=CODICE_CAMBIATO_DOPO_L_AVVIO, messaggio=avviso)

    def _lavora(self, cartella: Path) -> None:
        registro_artefatti: dict[str, dict[str, Any]] = {}
        try:
            self._verifica_il_codice_in_memoria()
            self._metti_da_parte_gli_adattatori_superati()
            corsa = _Corsa(cartella=cartella, registro_artefatti=registro_artefatti)
            self._fase_profilazione(corsa)
            self._fase_riconoscimento(corsa)
            self._fase_validazione(corsa)
            self._fase_parsing(corsa)
            self._fase_shortlist(corsa)
            self._fase_valutazione_ai(corsa)
            self._fase_risoluzione(corsa)
            self._fase_costruzione(corsa)
            self._fase_attivazione(corsa)
            self._aggiorna(
                ok=True,
                stato=COMPLETATO,
                fase=None,
                messaggio=corsa.messaggio_finale or "Confronto aggiornato.",
                completatoIl=utc_ora(),
            )
        except Fermata as fermata:
            self._segna_fase(self._stato.get("fase") or FASI[0], ERRORE, fermata.messaggio)
            self._aggiorna(
                ok=False,
                stato=ERRORE,
                messaggio=fermata.messaggio,
                fermata={
                    "code": fermata.codice,
                    "message": fermata.messaggio,
                    "documenti": fermata.documenti,
                    "dettaglio": fermata.dettaglio,
                    # Why each of those documents is listed. Readers of this
                    # state — the page, and /api/schemas/pending — have no
                    # other way to know: the profile alone isn't enough,
                    # since a document whose role doesn't match the registry
                    # reads as SCHEMA_NOTO in the profile but VARIATO here.
                    "motivi": fermata.motivi,
                },
                completatoIl=utc_ora(),
            )
        except Exception as exc:  # confine difensivo di un filo di sfondo
            dettaglio = f"{type(exc).__name__}: {exc}"
            self._segna_fase(self._stato.get("fase") or FASI[0], ERRORE, dettaglio)
            # Same check as the entry guard, repeated here: the code can
            # have changed while the run was in progress, in which case the
            # failure isn't a bug but a process that needs restarting.
            # Without this, a deploy that lands mid-run would surface a raw
            # Python traceback with no actionable next step.
            cambiato = versione_del_codice.avviso_del_codice_cambiato()
            codice = CODICE_CAMBIATO_DOPO_L_AVVIO if cambiato else "GUASTO_INATTESO"
            frase = cambiato or (
                "Il confronto non è riuscito. Riprova; se succede ancora, chiudi e riapri "
                "il comparatore."
            )
            self._aggiorna(
                ok=False,
                stato=ERRORE,
                messaggio=(
                    f"{frase} Il confronto precedente è rimasto attivo e non è stato toccato."
                ),
                fermata={
                    "code": codice,
                    "message": frase,
                    "documenti": [],
                    "dettaglio": dettaglio[:2000],
                },
                completatoIl=utc_ora(),
            )
        finally:
            # The audit write happens before the lock is released — with the
            # lock still held, otherwise a new run could start in the middle
            # and `self.stato()` would describe it inside this run's folder —
            # but the release itself sits in its own `finally`:
            # `_scrivi_audit_esecuzione` only catches `OSError`, and any other
            # exception there would otherwise keep the lock held forever, so
            # the release must not depend on anything else succeeding.
            try:
                self._scrivi_audit_esecuzione(cartella, registro_artefatti)
            except Exception as errore:  # noqa: BLE001 - the audit write must not kill the thread
                # The run's own state is already final by this point: a
                # failed audit write changes nothing for the page. What
                # changes is how it's noticed — a raw traceback from a
                # background thread goes unread, while an `[AVVISO]` line
                # sits next to all the others.
                print(f"[AVVISO] audit della run non scritto: {type(errore).__name__}: {errore}")
            finally:
                try:
                    self.lucchetto_lavori.release()
                except RuntimeError:  # pragma: no cover - non rilasciarlo due volte
                    pass

    def _scrivi_audit_esecuzione(
        self, cartella: Path, registro_artefatti: dict[str, dict[str, Any]]
    ) -> None:
        """The run's audit trail, written unconditionally, even on failure.

        This is what the next run compares its own numbers against, and the
        only place recording which artifacts were actually produced and with
        which checksum.
        """

        try:
            scrivi_json(cartella / NOME_AUDIT_ESECUZIONE, {
                **self.stato(),
                "artefatti": registro_artefatti,
            })
        except OSError:  # pragma: no cover - a full disk is not a pipeline error
            pass

    # -- phase helpers -------------------------------------------------------

    def _esegui(
        self,
        corsa: "_Corsa",
        fase: str,
        argomenti: Sequence[str],
        *,
        script: str,
        uscite_ammesse: Sequence[int] = (0,),
    ) -> RisultatoComando:
        comando = [self.configurazione.python_executable, str(self.configurazione.scripts_dir / script), *argomenti]
        tetto = tetto_della_fase(fase)
        try:
            risultato = self.esecutore(
                comando,
                self.configurazione.scripts_dir,
                self._avanzamento_fase(fase),
                timeout_secondi=tetto,
            )
        except ComandoTroppoLungo as troppo:
            # The child process was already killed by the timeout logic. This
            # just names the stop, and that name is what the user reads on
            # the page: the display channel already exists and knows how to
            # show it.
            corsa.comandi.append({
                "fase": fase,
                "script": script,
                "uscita": None,
                "tettoSecondi": tetto,
                "stderr": troppo.stderr[-4000:],
            })
            minuti = troppo.secondi / 60
            raise Fermata(
                codice=f"{fase}_TROPPO_LUNGA",
                messaggio=(
                    f"Il passo «{TITOLI_FASI.get(fase, fase).lower()}» non ha risposto entro "
                    f"{minuti:g} minuti: la catena si è fermata e il confronto di prima è "
                    "intatto. Riprova; se si ferma di nuovo nello stesso punto, il problema è "
                    "in uno dei documenti caricati."
                ),
                dettaglio=(
                    f"Passo «{script}», ucciso dopo {troppo.secondi:g} secondi.\n"
                    + troppo.stderr[-2000:]
                ),
            ) from None
        corsa.comandi.append({
            "fase": fase,
            "script": script,
            "uscita": risultato.uscita,
            # `stderr` is truncated: it needs to explain a failure, not
            # become a second log file.
            "stderr": risultato.stderr[-4000:],
        })
        if risultato.uscita not in uscite_ammesse:
            # The most common stop, and the one the user reads most often.
            # The message stays free of the script's file name and raw exit
            # code — neither helps someone who just needs to decide what to
            # do next. The script name stays available in `dettaglio`, the
            # technical detail panel.
            raise Fermata(
                codice=f"{fase}_NON_RIUSCITA",
                messaggio=(
                    f"Il confronto si è fermato durante: {TITOLI_FASI.get(fase, fase).lower()}. "
                    "Il confronto di prima è intatto. Riprova; se si ferma di nuovo nello stesso "
                    "punto, il problema è in uno dei documenti caricati."
                ),
                dettaglio=(
                    f"Passo «{script}», esito {risultato.uscita}.\n"
                    + (risultato.stderr or risultato.stdout)[-2000:]
                ),
            )
        return risultato

    def _avanzamento_fase(self, fase: str) -> AscoltaAvanzamento:
        def ascolta(evento: dict[str, Any]) -> None:
            fatti = evento.get("fatti")
            totali = evento.get("totali")
            dettaglio = ""
            if isinstance(fatti, int) and isinstance(totali, int) and totali:
                dettaglio = f"{fatti} di {totali}"
                costo = _numero(evento.get("costo_usd"))
                if costo:
                    dettaglio += f" · {costo:.3f} $"
            self._segna_fase(fase, IN_CORSO, dettaglio)
            numeri: dict[str, Any] = {}
            if isinstance(fatti, int):
                numeri["casiValutati"] = fatti
            if isinstance(totali, int):
                numeri["casiDaValutare"] = totali
            if evento.get("chiamate") is not None:
                numeri["chiamateAi"] = evento.get("chiamate")
            if evento.get("costo_usd") is not None:
                numeri["spesaUsd"] = evento.get("costo_usd")
            if numeri:
                self._numeri(**numeri)

        return ascolta

    def _prima_del_passo(self, corsa: "_Corsa", attesi: Sequence[Path]) -> None:
        """None of the artifacts a step is about to produce may already exist.

        A folder created empty can't contain yesterday's file; this check
        covers the remaining case — a step rerun by hand, or two runs landing
        in the same folder — for the cost of a `stat` call.
        """

        for percorso in attesi:
            if percorso.exists():
                raise Fermata(
                    codice="ARTEFATTO_PREESISTENTE",
                    messaggio=(
                        f"«{percorso.name}» esisteva già prima che il passo lo scrivesse: "
                        "questa esecuzione non è pulita e non prosegue."
                    ),
                    documenti=[percorso.name],
                )

    def _dopo_il_passo(self, corsa: "_Corsa", fase: str, attesi: Sequence[Path]) -> None:
        """Every declared artifact must exist now, and its checksum is recorded.

        This is what turns "I wrote it" from a claim into a fact: any later
        reader of an artifact re-checks its checksum, so a file replaced in
        the meantime is caught.
        """

        for percorso in attesi:
            if not percorso.is_file():
                raise Fermata(
                    codice="ARTEFATTO_MANCANTE",
                    messaggio=(
                        f"{TITOLI_FASI.get(fase, fase)}: il passo dice di essere riuscito ma "
                        f"«{percorso.name}» non c'è."
                    ),
                    documenti=[percorso.name],
                )
            corsa.registro_artefatti[self._chiave_artefatto(corsa, percorso)] = {
                "fase": fase,
                "sha256": impronta_file(percorso),
                "byte": percorso.stat().st_size,
                "scritto_il": utc_ora(),
            }

    def _chiave_artefatto(self, corsa: "_Corsa", percorso: Path) -> str:
        try:
            return percorso.relative_to(corsa.cartella).as_posix()
        except ValueError:  # pragma: no cover - every artifact lives inside the run folder
            return percorso.name

    def _leggi_artefatto(self, corsa: "_Corsa", percorso: Path) -> Any:
        """Re-reads an artifact only after re-checking its checksum."""

        chiave = self._chiave_artefatto(corsa, percorso)
        registrato = corsa.registro_artefatti.get(chiave)
        if registrato is None:
            raise Fermata(
                codice="ARTEFATTO_NON_REGISTRATO",
                messaggio=f"«{percorso.name}» non è stato prodotto da questa esecuzione.",
                documenti=[percorso.name],
            )
        if impronta_file(percorso) != registrato.get("sha256"):
            raise Fermata(
                codice="ARTEFATTO_CAMBIATO",
                messaggio=(
                    f"«{percorso.name}» è cambiato dopo essere stato scritto: qualcun altro "
                    "sta lavorando sugli stessi file e questa esecuzione si ferma."
                ),
                documenti=[percorso.name],
            )
        return leggi_json(percorso, None)

    # -- 1. PROFILAZIONE ---------------------------------------------------

    def _fase_profilazione(self, corsa: "_Corsa") -> None:
        fase = "PROFILAZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        uploads = self.configurazione.uploads_dir
        documenti = sorted(
            percorso for percorso in uploads.glob("*")
            if percorso.is_file() and percorso.suffix.casefold() in {".xlsx", ".xls", ".csv"}
        )
        if not documenti:
            raise Fermata(
                codice="NESSUN_DOCUMENTO",
                messaggio=(
                    "Non c'è nessun documento da confrontare: carica prima l'elenco del "
                    "gestionale e i listini dei fornitori."
                ),
            )
        corsa.profili_path = corsa.cartella / "input_profiles.json"
        self._prima_del_passo(corsa, [corsa.profili_path])
        risultato = self._esegui(
            corsa,
            fase,
            [str(uploads), "--output", str(corsa.profili_path), "--recursive"],
            script="inspect_sources.py",
            # Exit code 2 from the inventory means one or more documents
            # failed to parse: the output file is still written, and this
            # function decides whether to stop after reading it, so the
            # message can name which documents.
            uscite_ammesse=(0, 2),
        )
        self._dopo_il_passo(corsa, fase, [corsa.profili_path])
        documento = self._leggi_artefatto(corsa, corsa.profili_path)
        if not isinstance(documento, dict):
            raise Fermata(
                codice="PROFILI_ILLEGGIBILI",
                messaggio="L'inventario dei documenti non è leggibile.",
            )
        errori = documento.get("errors") or []
        if errori:
            nomi = [Path(str(voce.get("path") or "")).name for voce in errori]
            raise Fermata(
                codice="DOCUMENTO_NON_LETTO",
                messaggio=(
                    f"{len(nomi)} documento non si è lasciato leggere e il confronto non "
                    "può proseguire senza sapere che cosa contiene: "
                    if len(nomi) == 1
                    else f"{len(nomi)} documenti non si sono lasciati leggere: "
                ) + ", ".join(f"«{nome}»" for nome in nomi)
                + ". Toglili dai documenti caricati oppure sostituiscili.",
                documenti=nomi,
                dettaglio=str(risultato.riepilogo.get("errors") or ""),
            )
        corsa.profili = list(documento.get("profiles") or [])
        self._numeri(documenti=len(corsa.profili))
        self._segna_fase(
            fase, COMPLETATO,
            f"{len(corsa.profili)} documenti letti",
            time.monotonic() - inizio,
        )

    # -- 2. RICONOSCIMENTO -------------------------------------------------

    def _fase_riconoscimento(self, corsa: "_Corsa") -> None:
        """The fast path, and the first of the three hard stops.

        In the normal case — this week's price lists — the adapter registry
        recognizes everything and nothing else is called here: the decision
        `apply_preflight_decisions.py` expects as manually written is
        generated by the orchestrator itself, because recognition has
        already happened.

        What's left as genuinely manual is only what the registry doesn't
        know — exactly the case this phase stops on. A new supplier is
        learned once, then takes the fast path like everyone else.
        """

        fase = "RICONOSCIMENTO"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        manuali = self._decisioni_di_questi_documenti(self._decisioni_manuali(), corsa.profili)
        decisioni: list[dict[str, Any]] = []
        sconosciuti: list[str] = []
        variati: list[tuple[str, dict[str, Any]]] = []
        # The document is recognized fine: it was just uploaded under the
        # wrong role. Not a changed price list, and reporting it as one would
        # send the user looking for a layout change that isn't there.
        ruolo_sbagliato: list[tuple[str, str]] = []
        # The third case: a document missing exactly one required header,
        # with every other one in the right place. Reporting it as "layout
        # not recognized" sends the user through the new-supplier flow for a
        # near miss instead of the real cause.
        per_un_pelo: list[tuple[str, dict[str, Any]]] = []
        scelti, scartati = self._piu_recente_per_ruolo(corsa.profili, manuali)

        for profilo in scelti:
            nome = str(profilo.get("file_name") or "")
            a_mano = manuali.get(nome.casefold())
            if a_mano is not None:
                decisione = dict(a_mano)
                decisione.setdefault("file_name", nome)
                # `validate_input_manifest.py` requires a rationale on every
                # decision, even ones that say "this file doesn't apply":
                # without one, a manual decision would block the pipeline
                # with `MOTIVAZIONE_AI_MANCANTE`, which explains nothing.
                if not str(decisione.get("rationale") or "").strip():
                    decisione["rationale"] = (
                        f"Decisione scritta a mano in «{NOME_DECISIONI_MANUALI}»."
                    )
                # A manual decision overrides even a document the registry
                # would otherwise flag. Silently skipping the signature check
                # would leave the manual override active forever, invisible
                # to the user.
                stato_a_mano = str((profilo.get("deterministic_hint") or {}).get("state") or "")
                if stato_a_mano != "SCHEMA_NOTO":
                    self._avvisa(
                        "DECISIONE_MANUALE_ATTIVA",
                        f"«{nome}» entra per decisione manuale",
                        f"«{nome}» entra nel confronto per le colonne che hai indicato tu, "
                        f"scritte in «{NOME_DECISIONI_MANUALI}»: su questo documento le "
                        "verifiche della firma del registro non si applicano. Quando il "
                        "programma riesce a memorizzare quelle colonne, questa nota sparisce "
                        "da sola. Non c'è niente da cancellare a mano.",
                    )
                decisioni.append(decisione)
                conferma = decisione.get("user_confirmation") or {}
                if (
                    decisione.get("state") in {"SCHEMA_VARIATO", "NUOVO_FORNITORE"}
                    and conferma.get("status") == "CONFIRMED"
                ):
                    corsa.decisioni_da_imparare.append(nome)
                continue
            indizio = profilo.get("deterministic_hint") or {}
            stato_indizio = str(indizio.get("state") or "")
            ruolo_caricato = str(
                profilo.get("upload_role") or (profilo.get("ai_preflight") or {}).get("role") or ""
            ).casefold()
            adattatore_id = str(indizio.get("adapter_id") or "")
            if stato_indizio == "SCHEMA_NOTO" and ruolo_caricato in {"master", "supplier"} and adattatore_id:
                voce_registro = registro.adattatore(adattatore_id)
                ruolo_registro = "master" if voce_registro.get("kind") == "master" else "supplier"
                if ruolo_registro != ruolo_caricato:
                    ruolo_sbagliato.append((nome, str(voce_registro.get("display_name") or adattatore_id)))
                    continue
            if stato_indizio == "SCHEMA_VARIATO":
                # A VARIATO document isn't unknown: the registry recognizes
                # the supplier, but the document no longer matches the stored
                # signature (an extra column, two swapped). Reporting it as
                # "not recognized" would send the user through the
                # new-supplier flow when the real story is "this supplier's
                # layout changed".
                variati.append((nome, indizio))
                continue
            if stato_indizio != "SCHEMA_NOTO":
                if indizio.get("quasi_adapter_id"):
                    per_un_pelo.append((nome, indizio))
                else:
                    sconosciuti.append(nome)
                continue
            decisioni.append(self._decisione_dal_registro(profilo, indizio))

        if sconosciuti or variati or ruolo_sbagliato or per_un_pelo:
            coinvolti = (
                sconosciuti
                + [nome for nome, _indizio in variati]
                + [nome for nome, _chi in ruolo_sbagliato]
                + [nome for nome, _indizio in per_un_pelo]
            )
            # The reason travels with the stop: a document the registry
            # recognizes with high confidence but whose layout changed must
            # not be reported with the same generic "columns not
            # recognized" wording used for a genuinely unknown supplier.
            motivi = {nome: "SCONOSCIUTO" for nome in sconosciuti}
            motivi.update({nome: "VARIATO" for nome, _indizio in variati})
            motivi.update({nome: "RUOLO_SBAGLIATO" for nome, _chi in ruolo_sbagliato})
            motivi.update({nome: "QUASI" for nome, _indizio in per_un_pelo})
            frasi: list[str] = []
            if sconosciuti:
                frasi.append(
                    "Non riconosco ancora la disposizione delle colonne in "
                    + ", ".join(f"«{nome}»" for nome in sconosciuti)
                    + "."
                )
            if variati:
                nomi_dei_fornitori = []
                for nome, indizio in variati:
                    voce = registro.adattatore(str(indizio.get("adapter_id") or ""))
                    etichetta = str(voce.get("display_name") or "").strip()
                    nomi_dei_fornitori.append(
                        f"{etichetta} («{nome}»)" if etichetta else f"«{nome}»"
                    )
                frasi.append(
                    "È cambiato il listino di "
                    + ", ".join(nomi_dei_fornitori)
                    + ": il fornitore lo conosco, ma il documento non è più come era e non"
                    " voglio leggere i prezzi dal posto sbagliato."
                )
            if per_un_pelo:
                # The message comes from the registry, the only place that
                # knows which header is missing and where it used to be:
                # rewriting it here would mean two versions of the same
                # fact, free to drift apart later.
                frasi.extend(
                    f"«{nome}»: " + str((indizio.get("evidence") or ["non si sa perché"])[0])
                    for nome, indizio in per_un_pelo
                )
            if ruolo_sbagliato:
                frasi.append(
                    "; ".join(
                        f"«{nome}» è il listino di {chi}, ma è stato caricato come elenco del "
                        "gestionale (o viceversa)"
                        for nome, chi in ruolo_sbagliato
                    )
                    + ": cambia il tipo di documento qui sotto."
                )
            frasi.append(
                "Controlla le colonne nell'anteprima qui sotto: controllo i dati prima di"
                " usarli e, dopo la conferma, il confronto riparte da solo."
            )
            raise Fermata(
                codice="SCHEMA_SCONOSCIUTO",
                messaggio=" ".join(frasi),
                documenti=coinvolti,
                motivi=motivi,
            )

        for tenuto, lasciato_fuori in scartati:
            # The message states exactly three things: which file was kept,
            # which was dropped, and what the choice was based on. That last
            # part matters because the choice is the file's modification
            # date, not any validity date written inside the price list — an
            # expired list re-saved today wins, and staying silent about that
            # would hide it.
            messaggio = (
                f"«{lasciato_fuori.get('file_name')}» resta fuori dal confronto: dello stesso "
                f"fornitore c'è anche «{tenuto.get('file_name')}», ed è quello che viene usato. "
                "La scelta è fatta sulla data di modifica dei file nella cartella dei documenti "
                "caricati, non sulla validità scritta dentro i listini: se il listino buono è "
                f"«{lasciato_fuori.get('file_name')}», togli «{tenuto.get('file_name')}» dalla "
                "cartella e rifai il confronto."
            )
            if str(tenuto.get("modified_at") or "") == str(lasciato_fuori.get("modified_at") or ""):
                # With identical dates, the date decided nothing, and saying
                # so avoids crediting a criterion that didn't actually apply
                # — a folder copied with timestamps preserved can produce
                # exact ties.
                messaggio += (
                    " I due file risultano modificati nello stesso momento: a parità di data "
                    "resta l'ultimo in ordine di elenco."
                )
            self._avvisa(
                "DOCUMENTO_PIU_VECCHIO_LASCIATO_FUORI",
                "Due documenti dello stesso fornitore",
                messaggio,
                tenuto=str(tenuto.get("file_name") or ""),
                lasciatoFuori=str(lasciato_fuori.get("file_name") or ""),
            )

        corsa.decisioni_path = corsa.cartella / "preflight_decisions.json"
        corsa.profili_usati_path = corsa.cartella / "input_profiles_usati.json"
        corsa.manifest_path = corsa.cartella / "input_manifest.json"
        self._prima_del_passo(corsa, [corsa.decisioni_path, corsa.profili_usati_path, corsa.manifest_path])
        # The full inventory stays in `input_profiles.json`, the record of
        # what was there. Only the documents entering the comparison go into
        # the manifest: `apply_preflight_decisions` requires a decision for
        # every profile it's given, and a price list superseded by a newer
        # one would otherwise show up on the page as "needs attention" —
        # which it isn't.
        scrivi_json(corsa.profili_usati_path, {"schema_version": 1, "profiles": scelti})
        scrivi_json(corsa.decisioni_path, {"decisions": decisioni})
        for percorso in (corsa.profili_usati_path, corsa.decisioni_path):
            corsa.registro_artefatti[self._chiave_artefatto(corsa, percorso)] = {
                "fase": fase,
                "sha256": impronta_file(percorso),
                "byte": percorso.stat().st_size,
                "scritto_il": utc_ora(),
            }
        self._esegui(
            corsa,
            fase,
            [
                "--profiles", str(corsa.profili_usati_path),
                "--decisions", str(corsa.decisioni_path),
                "--output", str(corsa.manifest_path),
            ],
            script="apply_preflight_decisions.py",
        )
        self._dopo_il_passo(corsa, fase, [corsa.manifest_path])
        fornitori = sorted({
            str(voce.get("supplier_id"))
            for voce in decisioni
            if voce.get("role") == "supplier" and voce.get("supplier_id")
        })
        corsa.fornitori_attesi = fornitori
        self._numeri(fornitori=len(fornitori), fornitoriElenco=fornitori)
        self._segna_fase(
            fase, COMPLETATO,
            f"{len(fornitori)} fornitori riconosciuti, nessuna chiamata AI"
            if not manuali else f"{len(fornitori)} fornitori riconosciuti",
            time.monotonic() - inizio,
        )

    def _decisioni_di_questi_documenti(
        self,
        manuali: dict[str, dict[str, Any]],
        profili: Sequence[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Keeps only the decisions that describe the documents uploaded now.

        Indexing a confirmed decision purely by file name is unsafe: delete
        a misconfigured price list, re-upload the right file under the same
        name, and the recompute would reapply the old mapping to a different
        document — reading quantities from the wrong columns. Each decision
        also carries the checksum of the document it was made on; when it no
        longer matches, the decision is set aside and the user is told —
        a silent drop here would be the same failure mode from the other side.
        """

        if not manuali:
            return {}
        per_nome = {str(profilo.get("file_name") or "").casefold(): profilo for profilo in profili}
        tenute: dict[str, dict[str, Any]] = {}
        for nome, decisione in manuali.items():
            profilo = per_nome.get(nome)
            if profilo is None or self._decisione_e_di_questo_documento(decisione, profilo):
                tenute[nome] = decisione
                continue
            etichetta = str(profilo.get("file_name") or nome)
            self._avvisa(
                "DECISIONE_MANUALE_SCARTATA",
                f"«{etichetta}» non è il documento configurato",
                f"Le colonne confermate per «{etichetta}» erano di un altro documento con lo "
                "stesso nome. Questo file viene riconosciuto da capo: se il confronto si ferma "
                "di nuovo sulle colonne, indicamele un'altra volta.",
            )
        return tenute

    @staticmethod
    def _decisione_e_di_questo_documento(
        decisione: Mapping[str, Any],
        profilo: Mapping[str, Any],
    ) -> bool:
        """Does this confirmed decision actually describe this file?

        Compared by content checksum, not by name: the same supplier's price
        list can keep the same file name every week. A decision with no
        checksum is one written by hand in `decisioni_schemi.json` — the
        documented fallback when automatic recognition isn't enough — and
        that one still applies by name: taking that away would remove the
        one remedy available.
        """

        confermata = str(decisione.get("file_sha256") or "").strip().casefold()
        if not confermata:
            return True
        adesso = str(profilo.get("sha256") or "").strip().casefold()
        # A profile with no checksum isn't proof the document changed: it's
        # not dropped over missing data.
        return not adesso or adesso == confermata

    def _decisioni_manuali(self) -> dict[str, dict[str, Any]]:
        documento = leggi_json(self.configurazione.decisioni_manuali_path, None)
        if isinstance(documento, dict):
            voci = documento.get("decisions")
        else:
            voci = documento
        if not isinstance(voci, list):
            return {}
        risultato: dict[str, dict[str, Any]] = {}
        for voce in voci:
            if not isinstance(voce, dict):
                continue
            nome = str(voce.get("file_name") or "").strip()
            if nome:
                risultato[nome.casefold()] = voce
        return risultato

    @staticmethod
    def _piu_recente_per_ruolo(
        profili: Sequence[dict[str, Any]],
        manuali: Mapping[str, dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
        """One document per supplier: whichever was modified last.

        The uploads folder accumulates week over week, and two price lists
        from the same supplier would otherwise fail the parser with a
        duplicate-supplier error that doesn't tell the user what to do. Only
        one is kept, and which one was dropped is reported — a silent drop
        would be worse than an error.

        "Most recent" here means `modified_at`, i.e. when the file landed in
        the folder — not any validity date written inside the price list
        itself, and the two can disagree: an expired list re-saved today
        wins over yesterday's valid one. This stays the criterion because
        it's the only signal available across every supplier; what changed
        is that the warning now states it plainly instead of implying the
        winner is simply the newer price list. That's also why the (kept,
        dropped) pair is returned, not just the dropped one.

        Documents the registry doesn't recognize are left untouched here:
        their fate is decided by the hard stop, not by this rule.
        """

        manuali = manuali or {}
        per_chiave: dict[str, dict[str, Any]] = {}
        persi_per_chiave: dict[str, list[dict[str, Any]]] = {}
        sconosciuti: list[dict[str, Any]] = []
        for profilo in profili:
            indizio = profilo.get("deterministic_hint") or {}
            adattatore_id = str(indizio.get("adapter_id") or "")
            manuale = manuali.get(str(profilo.get("file_name") or "").casefold())
            ruolo_caricato = str(
                profilo.get("upload_role") or (profilo.get("ai_preflight") or {}).get("role") or ""
            ).casefold()
            if isinstance(manuale, dict) and manuale.get("role") in {"master", "supplier"}:
                chiave = (
                    "master" if manuale.get("role") == "master"
                    else f"supplier:{manuale.get('supplier_id') or manuale.get('adapter_id') or profilo.get('profile_id')}"
                )
            elif ruolo_caricato in {"master", "supplier"}:
                if ruolo_caricato == "master":
                    chiave = "master"
                else:
                    voce = registro.adattatore(adattatore_id) if adattatore_id else {}
                    chiave = f"supplier:{voce.get('supplier_id') or adattatore_id or profilo.get('profile_id')}"
            else:
                if str(indizio.get("state") or "") != "SCHEMA_NOTO" or not adattatore_id:
                    sconosciuti.append(profilo)
                    continue
                voce = registro.adattatore(adattatore_id)
                chiave = (
                    "master" if voce.get("kind") == "master"
                    else f"supplier:{voce.get('supplier_id') or adattatore_id}"
                )
            precedente = per_chiave.get(chiave)
            if precedente is None:
                per_chiave[chiave] = profilo
                continue
            if str(profilo.get("modified_at") or "") >= str(precedente.get("modified_at") or ""):
                per_chiave[chiave] = profilo
                persi_per_chiave.setdefault(chiave, []).append(precedente)
            else:
                persi_per_chiave.setdefault(chiave, []).append(profilo)
        # Pairs are formed only at the end: with three documents from the
        # same supplier, whoever was leading halfway through isn't
        # necessarily the final winner, and naming an intermediate winner
        # would be wrong.
        scartati = sorted(
            ((per_chiave[chiave], perso) for chiave, persi in persi_per_chiave.items() for perso in persi),
            key=lambda coppia: str(coppia[1].get("file_name") or "").casefold(),
        )
        scelti = sorted(
            [*per_chiave.values(), *sconosciuti],
            key=lambda voce: str(voce.get("file_name") or "").casefold(),
        )
        return scelti, scartati

    @staticmethod
    def _decisione_dal_registro(profilo: dict[str, Any], indizio: dict[str, Any]) -> dict[str, Any]:
        """The decision the user would have written by hand, taken from the registry.

        `field_mapping` is always carried over when the registry has one:
        without it, the manifest wouldn't say where the order column is, and
        compilation would skip the supplier claiming a missing confirmed
        mapping the user never actually removed.
        """

        adattatore_id = str(indizio.get("adapter_id") or "")
        voce = registro.adattatore(adattatore_id)
        ruolo = "master" if voce.get("kind") == "master" else "supplier"
        decisione: dict[str, Any] = {
            "file_name": profilo.get("file_name"),
            "profile_id": profilo.get("profile_id"),
            "state": "SCHEMA_NOTO",
            "role": ruolo,
            "adapter_id": adattatore_id,
            "confidence": "ALTA",
            "rationale": (
                f"Schema riconosciuto dal registro ({adattatore_id}, "
                f"confidenza {indizio.get('confidence')}): nessuna chiamata AI."
            ),
            "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
        }
        if ruolo == "supplier":
            decisione["supplier_id"] = voce.get("supplier_id")
        mappatura = voce.get("field_mapping")
        if isinstance(mappatura, dict):
            decisione["field_mapping"] = deepcopy(mappatura)
        return decisione

    # -- 3. VALIDAZIONE ----------------------------------------------------

    def _fase_validazione(self, corsa: "_Corsa") -> None:
        """`prepare_manifest_sources` doesn't read the validation output; this does.

        The parser script runs even on a rejected manifest, and without this
        stop a missing management-export or two suppliers sharing an id
        would turn into a wrong comparison instead of a clear message.
        """

        fase = "VALIDAZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.validazione_path = corsa.cartella / "manifest_validation.json"
        self._prima_del_passo(corsa, [corsa.validazione_path])
        self._esegui(
            corsa,
            fase,
            [
                "--manifest", str(corsa.manifest_path),
                "--adapters", str(self.configurazione.adapters_path),
                "--output", str(corsa.validazione_path),
            ],
            script="validate_input_manifest.py",
            uscite_ammesse=(0, 2),
        )
        self._dopo_il_passo(corsa, fase, [corsa.validazione_path])
        validazione = self._leggi_artefatto(corsa, corsa.validazione_path)
        errori = (validazione or {}).get("errors") or []
        if errori:
            raise Fermata(
                codice="MANIFEST_NON_VALIDO",
                messaggio=(
                    "I documenti caricati non formano un confronto valido: "
                    + "; ".join(_frase_dell_errore(voce) for voce in errori[:5])
                ),
                dettaglio=json.dumps(errori[:20], ensure_ascii=False),
            )
        avvisi = (validazione or {}).get("warnings") or []
        for avviso in avvisi[:10]:
            # The `message` field is a ready-to-read sentence that
            # `validate_input_manifest.py` writes on purpose; the raw dict is
            # only a fallback in case a future warning arrives without one.
            testo = avviso.get("message") if isinstance(avviso, dict) else None
            self._avvisa("MANIFEST_AVVISO", "Avviso sui documenti", str(testo or avviso))
        self._segna_fase(
            fase, COMPLETATO, "manifest valido", time.monotonic() - inizio
        )

    # -- 4. PARSING --------------------------------------------------------

    def _scrivi_le_uguaglianze(self, corsa: "_Corsa") -> Path | None:
        """The active barcode equivalences, written into the run folder.

        Returns the file's path, or `None` when there are none: asking the
        next step for the file and not having it would be a hard stop, and a
        run with no declared equivalences is the normal case.

        Does this also write an empty file when the store can't be opened?
        No — nothing is written in that case and the step runs as if there
        were none. The distinction between "there are none" and "I can't
        read them" is made by `self.uguaglianze_dichiarate`, owned by the
        service; an exception here would stop a recompute over a cache that
        is genuinely optional, so this proceeds and records how many
        equivalences applied in the run's numbers instead.
        """

        if self.uguaglianze_dichiarate is None:
            return None
        try:
            classi = [list(gruppo) for gruppo in self.uguaglianze_dichiarate() or [] if len(gruppo) > 1]
        except Exception as exc:  # noqa: BLE001 - a missing cache must not stop a recompute
            self._numeri(uguaglianzeDichiarate=0, uguaglianzeNonLette=f"{type(exc).__name__}: {exc}")
            return None
        self._numeri(uguaglianzeDichiarate=len(classi))
        if not classi:
            return None
        corsa.dati_dir.mkdir(parents=True, exist_ok=True)
        percorso = corsa.dati_dir / "uguaglianze.json"
        # `write_text` would produce CRLF on Windows, and this file is read
        # back by a later pipeline step: written in binary with LF endings
        # like everything else.
        scrittura_sicura.scrivi_json(percorso, {"classi": classi})
        return percorso

    def _fase_parsing(self, corsa: "_Corsa") -> None:
        fase = "PARSING"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.dati_dir = corsa.cartella / "dati"
        attesi = [
            corsa.dati_dir / nome for nome in (
                "normalized_sources.json", "display_offers.json",
                "matching_result.json", "semantic_queue.json", "audit.json",
            )
        ]
        self._prima_del_passo(corsa, attesi)
        argomenti = [
            "--manifest", str(corsa.manifest_path),
            "--adapters", str(self.configurazione.adapters_path),
            "--output", str(corsa.dati_dir),
        ]
        uguaglianze = self._scrivi_le_uguaglianze(corsa)
        if uguaglianze is not None:
            argomenti += ["--equivalenze", str(uguaglianze)]
        self._esegui(corsa, fase, argomenti, script="prepare_manifest_sources.py")
        self._dopo_il_passo(corsa, fase, attesi)
        audit = self._leggi_artefatto(corsa, corsa.dati_dir / "audit.json") or {}
        corsa.audit = audit if isinstance(audit, dict) else {}
        fonti = corsa.audit.get("sources") or {}
        letti = {str(nome): int((valori or {}).get("rows") or 0) for nome, valori in fonti.items()}
        # A supplier the manifest declared, coming out of the parser with
        # zero rows, isn't a minor detail: it's a supplier that vanished
        # from the comparison.
        mancanti = [nome for nome in corsa.fornitori_attesi if letti.get(nome, 0) <= 0]
        if mancanti:
            # The third hard stop, same family as the second: a price list
            # deliberately loaded that yields not a single row is, in
            # substance, a document that failed to parse. A non-blocking
            # warning here would let the supplier silently drop out of the
            # comparison, and orders would go out without them.
            raise Fermata(
                codice="FORNITORE_SENZA_RIGHE",
                messaggio=(
                    "Il listino di " + ", ".join(nome.upper() for nome in mancanti)
                    + " non ha prodotto nessuna riga ordinabile: il confronto direbbe che quel "
                    "fornitore non ha niente, e non è vero. Il confronto precedente è rimasto "
                    "attivo. Si rimedia così: guarda il suo documento fra i caricamenti — se è "
                    "il file sbagliato o un listino vuoto, sostituiscilo (o togli il fornitore "
                    "dai caricamenti) e rifai il confronto; se invece il fornitore ha cambiato "
                    "la forma del listino, la strada è quella della fermata degli schemi, in "
                    "«references/schema-routing.md»."
                ),
                documenti=mancanti,
            )
        self._controlla_i_prezzi(corsa, letti)
        scartati = {}
        for voce in corsa.audit.get("inputs") or []:
            non_ordinabili = voce.get("rows_not_orderable") or {}
            lettura = voce.get("reading") or {}
            # `prepare_manifest_sources` writes `rows_excluded`; `excluded`
            # is kept as a fallback for audits produced by older versions.
            esclusi = lettura.get("rows_excluded") or lettura.get("excluded") or {}
            if non_ordinabili or esclusi:
                scartati[str(voce.get("supplier_id") or voce.get("role") or "?")] = {
                    "nonOrdinabili": non_ordinabili,
                    "esclusi": esclusi,
                }
        self._numeri(
            righeLette=letti,
            prodottiGestionale=int((corsa.audit.get("master") or {}).get("rows") or 0),
            scartati=scartati,
        )
        self._segna_fase(
            fase, COMPLETATO,
            ", ".join(f"{nome.upper()} {quante}" for nome, quante in sorted(letti.items())),
            time.monotonic() - inizio,
        )

    def _controlla_i_prezzi(self, corsa: "_Corsa", letti: dict[str, int]) -> None:
        """A price list with rows but no usable prices is flagged even on the first run.

        `_post_check` compares against the previous run, so on the first run
        it has nothing to compare and would otherwise miss the worst case
        entirely: a missing median (`usable: 0` in the audit) with no prior
        run to compare against.

        Here the comparison is against zero, which needs no prior run: a
        price list read in full where no row has a price above zero isn't a
        great deal, it's a wrong column. And a supplier priced at zero
        doesn't drop out of the comparison — it wins every row, and drags
        the whole order with it.

        Stays a warning, not a hard stop: the hard stops are reserved for
        cases where the program has no authority to decide because the input
        can't be used at all (an unrecognized layout, a document that fails
        to parse, a price list with no orderable rows). Here the program
        knows exactly what happened and can say so; the numeric check warns
        and never blocks. But it's an error-level warning, shown in red on
        the page and carried into the document, and there's no way to
        silence it.
        """

        prezzi = corsa.audit.get("price_summary")
        if not isinstance(prezzi, dict):
            # "Not measured" isn't "measured as zero": an audit with no price
            # summary (an older format) would otherwise fire PREZZI_A_ZERO
            # on every supplier in the run, and a warning that always fires
            # is a warning nobody reads anymore.
            self._avvisa(
                "PREZZI_NON_MISURATI",
                "Il controllo dei prezzi non si è potuto fare",
                "L'audit di questa run non porta il riepilogo dei prezzi "
                "(price_summary), quindi il controllo che scopre una colonna "
                "dei prezzi sbagliata non ha niente da guardare. Il confronto "
                "di oggi lo scrive sempre: se questo avviso esce, l'audit "
                "viene da un formato più vecchio e conviene rifare il "
                "confronto.",
            )
            return
        senza: list[str] = []
        for nome, righe in sorted(letti.items()):
            if righe <= 0:
                continue
            voce = prezzi.get(nome)
            voce = voce if isinstance(voce, dict) else {}
            mediana = _numero(voce.get("median"))
            if mediana is not None and mediana > 0:
                continue
            usabili = int(_numero(voce.get("usable")) or 0)
            # The message states only what the audit actually proves: a
            # nonzero "usable" count and a missing median describe an
            # inconsistent audit, not "no usable prices".
            if mediana is None and usabili <= 0:
                diagnosi = "nessuna delle quali porta un prezzo al pezzo utilizzabile"
            elif mediana is None:
                diagnosi = (f"un riepilogo dei prezzi incoerente ({usabili} prezzi "
                            "dichiarati utilizzabili ma nessun prezzo mediano)")
            else:
                diagnosi = (f"un prezzo mediano dichiarato di {mediana:g} su "
                            f"{usabili} prezzi dichiarati utilizzabili")
            senza.append(nome)
            self._avvisa(
                "PREZZI_A_ZERO",
                f"{nome.upper()}: nessun prezzo utilizzabile",
                f"Il listino {nome.upper()} porta {righe} righe nel confronto e "
                + diagnosi
                + ". Così quel fornitore comparirebbe a 0,00 € e vincerebbe ogni "
                "riga del confronto. Quasi sempre vuol dire che la colonna del prezzo "
                "dichiarata per questo listino non è quella dei prezzi: controllala "
                "prima di mandare l'ordine.",
                severita="error",
                fornitore=nome,
                righe=righe,
                prezziUsabili=usabili,
            )
        if senza:
            self._numeri(fornitoriSenzaPrezzi=senza)

    # -- 5. SHORTLIST ------------------------------------------------------

    def _fase_shortlist(self, corsa: "_Corsa") -> None:
        fase = "SHORTLIST"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.shortlists_path = corsa.dati_dir / "semantic_shortlists.json"
        self._prima_del_passo(corsa, [corsa.shortlists_path])
        risultato = self._esegui(
            corsa,
            fase,
            [
                "--normalized", str(corsa.dati_dir / "normalized_sources.json"),
                "--queue", str(corsa.dati_dir / "semantic_queue.json"),
                "--output", str(corsa.shortlists_path),
                "--top-k", str(self.configurazione.top_k),
            ],
            script="build_semantic_shortlists.py",
        )
        self._dopo_il_passo(corsa, fase, [corsa.shortlists_path])
        shortlists = self._leggi_artefatto(corsa, corsa.shortlists_path)
        corsa.numero_shortlist = len(shortlists) if isinstance(shortlists, list) else 0
        self._numeri(casiSemantici=corsa.numero_shortlist)
        self._segna_fase(
            fase, COMPLETATO,
            f"{corsa.numero_shortlist} casi da valutare",
            time.monotonic() - inizio,
        )
        corsa.riepiloghi["shortlist"] = risultato.riepilogo

    # -- 6. VALUTAZIONE_AI -------------------------------------------------

    def _fase_valutazione_ai(self, corsa: "_Corsa") -> None:
        """The costliest step in wall-clock time. A degraded run never stops.

        If the AI provider doesn't respond, the pipeline proceeds anyway: the
        cases it would have interpreted stay `DA_VERIFICARE` and reach the
        reviewer, who checks them with the order in front of them.
        """

        fase = "VALUTAZIONE_AI"
        # The detail is set here, at the start, unlike the other phases that
        # only set it at the end. This is the phase that takes the longest,
        # and without an upfront detail the progress line would just read
        # "in progress" while it climbs in jumps and then sits still for the
        # bulk of the recompute — which a non-technical user reads as
        # "stuck", with the natural reaction of closing the program mid-run.
        quanti = corsa.numero_shortlist
        self._segna_fase(fase, IN_CORSO, f"{quanti} {'caso' if quanti == 1 else 'casi'} da valutare")
        inizio = time.monotonic()
        corsa.decisioni_ai_path = corsa.dati_dir / "ai_decisions.json"
        corsa.rapporto_ai_path = corsa.dati_dir / "ai_rapporto.json"
        self._prima_del_passo(corsa, [corsa.decisioni_ai_path, corsa.rapporto_ai_path])
        self._esegui(
            corsa,
            fase,
            [
                "--shortlists", str(corsa.shortlists_path),
                "--output", str(corsa.decisioni_ai_path),
                "--rapporto", str(corsa.rapporto_ai_path),
            ],
            script="valuta_shortlist.py",
            # Exit code 5 is a declared degradation: both files exist, but
            # some cases weren't evaluated. The pipeline continues and
            # reports it.
            uscite_ammesse=(0, 5),
        )
        self._dopo_il_passo(corsa, fase, [corsa.decisioni_ai_path, corsa.rapporto_ai_path])
        rapporto = self._leggi_artefatto(corsa, corsa.rapporto_ai_path)
        if not isinstance(rapporto, dict):
            raise Fermata(
                codice="RAPPORTO_AI_ILLEGGIBILE",
                messaggio="La contabilità della fase AI non è leggibile.",
            )
        corsa.rapporto_ai = rapporto
        self._numeri(
            casiRicevuti=rapporto.get("casi_ricevuti"),
            casiValutabili=rapporto.get("casi_valutabili"),
            casiDecisi=rapporto.get("casi_decisi"),
            chiamateAi=rapporto.get("chiamate"),
            spesaUsd=rapporto.get("costo_usd"),
            modello=rapporto.get("model"),
        )
        if rapporto.get("degradato"):
            self._avvisa(
                "FASE_AI_DEGRADATA",
                "Valutazione automatica incompleta",
                "Una parte dei casi non è stata valutata e resta da verificare a mano. "
                + str(rapporto.get("motivo_degrado") or ""),
            )
        self._segna_fase(
            fase, COMPLETATO,
            f"{rapporto.get('casi_decisi')} decisi su {rapporto.get('casi_valutabili')}"
            f" · {_numero(rapporto.get('costo_usd')) or 0:.3f} $",
            time.monotonic() - inizio,
        )

    # -- 7. RISOLUZIONE ----------------------------------------------------

    def _fase_risoluzione(self, corsa: "_Corsa") -> None:
        fase = "RISOLUZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.risolti_path = corsa.dati_dir / "resolved_matches.json"
        self._prima_del_passo(corsa, [corsa.risolti_path])
        attese = corsa.rapporto_ai.get("casi_decisi")
        if not isinstance(attese, int) or attese < 0:
            raise Fermata(
                codice="RAPPORTO_AI_SENZA_CONTEGGIO",
                messaggio="La fase AI non dichiara quanti casi ha deciso: la catena si ferma.",
            )
        risultato = self._esegui(
            corsa,
            fase,
            [
                "--matching", str(corsa.dati_dir / "matching_result.json"),
                "--normalized", str(corsa.dati_dir / "normalized_sources.json"),
                "--shortlists", str(corsa.shortlists_path),
                "--decisions", str(corsa.decisioni_ai_path),
                "--decisions-attese", str(attese),
                "--output", str(corsa.risolti_path),
            ],
            script="merge_match_decisions.py",
            # Exit codes 4 and 5 write the output file with the broken pairs
            # already downgraded to `DA_VERIFICARE`: an honest artifact, and
            # the pipeline proceeds while reporting it. 2 and 3 don't: there
            # the artifacts don't describe the same work, and the comparison
            # would be a lie.
            uscite_ammesse=(0, 4, 5),
        )
        self._dopo_il_passo(corsa, fase, [corsa.risolti_path])
        riepilogo = risultato.riepilogo
        corsa.riepiloghi["risoluzione"] = riepilogo
        if risultato.uscita in (4, 5):
            self._avvisa(
                "DECISIONI_AI_SCARTATE",
                "Alcune decisioni automatiche sono state scartate",
                "Le coppie interessate tornano da verificare a mano: "
                f"{riepilogo.get('decisioni_scartate_per_disallineamento', 0)} per listino "
                f"disallineato, {riepilogo.get('decisioni_scartate_per_riga_inventata', 0)} per "
                f"riga fuori dai candidati, "
                f"{riepilogo.get('decisioni_scartate_per_ean_non_rispettato', 0)} per EAN non "
                "rispettato.",
            )
        self._giudica_provenienza(corsa)
        self._numeri(
            accettatiSenzaConferma=riepilogo.get("accettati_senza_conferma"),
            rifiutiConCandidatoForte=riepilogo.get("rifiuti_con_candidato_forte"),
            # How many rows matched purely on a shared product code.
            abbinamentiPerStessoCodice=riepilogo.get("abbinamenti_per_stesso_codice"),
        )
        self._segna_fase(
            fase, COMPLETATO,
            f"{riepilogo.get('decisioni_con_riscontro', 0)} decisioni entrate",
            time.monotonic() - inizio,
        )

    def _giudica_provenienza(self, corsa: "_Corsa") -> None:
        """Checks that every decision's declared AI configuration matches this run's.

        Each decision carries the model and the two prompt versions it was
        made with; `merge_match_decisions.py` reads them but has no live
        configuration to compare them against.

        What this actually proves, and what it doesn't: inside a normal
        orchestrated run, the decisions are written by this same run's AI
        phase, with today's configuration — so this check can never find a
        mismatch here, and that's expected. It exists for the remaining
        case, an `ai_decisions.json` supplied from outside the run (the
        script accepts any path via `--decisions`). The comparison against
        the previous run's configuration, which is where a real mismatch
        would show up, lives in `_post_check`.
        """

        decisioni = self._leggi_artefatto(corsa, corsa.decisioni_ai_path)
        righe = decisioni.get("decisions") if isinstance(decisioni, dict) else decisioni
        if not isinstance(righe, list):
            return
        atteso = {
            "ai_modello": corsa.rapporto_ai.get("model"),
            "ai_versione_prompt": corsa.rapporto_ai.get("versione_prompt"),
            "ai_versione_avversario": corsa.rapporto_ai.get("versione_avversario"),
        }
        corsa.provenienza = {chiave: valore for chiave, valore in atteso.items() if valore is not None}
        diverse: dict[str, int] = {}
        for riga in righe:
            if not isinstance(riga, dict):
                continue
            for campo, valore in atteso.items():
                if valore is None or riga.get(campo) == valore:
                    continue
                diverse[campo] = diverse.get(campo, 0) + 1
        if diverse:
            self._avvisa(
                "DECISIONI_DI_UNA_CONFIGURAZIONE_DIVERSA",
                "Decisioni prese con una configurazione diversa",
                "Alcune corrispondenze entrate nel confronto non dichiarano il modello o il "
                "prompt con cui la fase AI di oggi ha lavorato: "
                + ", ".join(f"{campo} {quante}" for campo, quante in sorted(diverse.items()))
                + ". Vanno guardate prima di mandare l'ordine.",
            )

    # -- 8. COSTRUZIONE ----------------------------------------------------

    def _fase_costruzione(self, corsa: "_Corsa") -> None:
        fase = "COSTRUZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.review_path = corsa.cartella / "review_data.json"
        self._prima_del_passo(corsa, [corsa.review_path])
        # `--displays`, `--audit` and `--manifest` are optional flags on the
        # script itself; not here — omitting any of them would silently drop
        # products from the page, and the orchestrator always has all three.
        self._esegui(
            corsa,
            fase,
            [
                "--resolved", str(corsa.risolti_path),
                "--manifest", str(corsa.manifest_path),
                "--audit", str(corsa.dati_dir / "audit.json"),
                "--displays", str(corsa.dati_dir / "display_offers.json"),
                "--threshold", str(self.configurazione.soglia_ordine),
                "--run-id", corsa.cartella.name,
                "--output", str(corsa.review_path),
            ],
            script="build_review_data.py",
        )
        self._dopo_il_passo(corsa, fase, [corsa.review_path])
        confronto = self._leggi_artefatto(corsa, corsa.review_path)
        if not isinstance(confronto, dict):
            raise Fermata(
                codice="CONFRONTO_ILLEGGIBILE",
                messaggio="Il confronto costruito non è leggibile.",
            )
        corsa.confronto = confronto
        self._impara_schemi_confermati(corsa, fase)
        self._numeri(
            prodotti=len(confronto.get("products") or []),
            fornitoriNelConfronto=len(confronto.get("suppliers") or []),
        )
        self._avvisa_chi_non_si_compila(corsa)
        self._post_check(corsa)
        self._segna_fase(
            fase, COMPLETATO,
            f"{len(confronto.get('products') or [])} prodotti",
            time.monotonic() - inizio,
        )

    def _impara_schemi_confermati(self, corsa: "_Corsa", fase: str) -> None:
        """Persists approved mappings to the registry and drops the temporary override.

        Reached only after the manifest, parser and comparison build have
        already used the chosen columns for real. The registry therefore
        learns from a run that has passed every check before activation, not
        from a bare preview.

        A failed learning attempt must never discard the comparison: the
        comparison is already built and already valid by this point, so a
        failure here — a read-only data folder, a registry file locked by an
        antivirus or a sync client — is reported and the pipeline moves on.
        What the registry fails to learn affects next week, not this
        comparison.

        The one exception is `REGISTRO_ADATTATORI_ROVINATO`, which is about
        the suppliers learned on this machine, not about this comparison,
        and is allowed to propagate.
        """

        if not corsa.decisioni_da_imparare:
            return
        try:
            self._prova_a_imparare(corsa, fase)
        except Fermata:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed cache write must not discard a comparison
            motivo = registro.motivo_registro_illeggibile(self.configurazione.adapters_path)
            if motivo:
                raise Fermata(
                    codice="REGISTRO_ADATTATORI_ROVINATO",
                    messaggio=(
                        "Il registro degli adattatori imparati su questo computer non si legge "
                        "più: da adesso valgono solo gli adattatori spediti col programma, "
                        "quindi i fornitori imparati qui possono tornare sconosciuti e per loro "
                        "non nascerebbe nessuna copia d'ordine. Il confronto precedente è "
                        "rimasto attivo e non è stato toccato. Chiudi e riapri il comparatore; "
                        "se si ferma di nuovo qui, "
                        f"«{registro.percorso_imparato(self.configurazione.adapters_path).name}» "
                        "va riparato o eliminato — eliminarlo fa ricominciare l'apprendimento "
                        "da zero e non rompe nient'altro."
                    ),
                    documenti=sorted(corsa.decisioni_da_imparare),
                    dettaglio=motivo,
                ) from exc
            self._avvisa_schemi_non_memorizzati(
                corsa,
                {nome.casefold() for nome in corsa.decisioni_da_imparare},
                {nome.casefold(): f"{type(exc).__name__}: {exc}"
                 for nome in corsa.decisioni_da_imparare},
            )

    def _prova_a_imparare(self, corsa: "_Corsa", fase: str) -> None:
        """`impara_adattatore`'s two steps: dry run, then the real write."""

        prova = corsa.cartella / "adattatori_da_imparare.json"
        rapporto = corsa.cartella / "adattatori_imparati.json"
        self._prima_del_passo(corsa, [prova, rapporto])
        # The first pass is a dry run, not a formality: it writes to its own
        # temp location and never touches the real registry, even on
        # success. A failure here can't have damaged anything, which is why
        # nothing stops the pipeline at this point.
        self._esegui(
            corsa,
            fase,
            [
                "--manifest", str(corsa.manifest_path),
                "--adapters", str(self.configurazione.adapters_path),
                "--output", str(prova),
                "--prova",
            ],
            script="impara_adattatore.py",
            uscite_ammesse=(0, 2),
        )
        self._dopo_il_passo(corsa, fase, [prova])
        documento_prova = self._leggi_artefatto(corsa, prova) or {}
        attesi = {nome.casefold() for nome in corsa.decisioni_da_imparare}
        imparabili = self._nomi_del_rapporto(documento_prova, "imparati") & attesi
        non_memorizzabili = attesi - imparabili
        if non_memorizzabili:
            self._avvisa_schemi_non_memorizzati(
                corsa,
                non_memorizzabili,
                self._motivi_del_rapporto(documento_prova, non_memorizzabili),
            )
        if not imparabili:
            # Nothing to write: the real step doesn't run, and the registry
            # isn't opened for writing for no reason.
            return

        # The second pass writes for real, and the registry is the only
        # thing that could end up worse off than before. Checked before and
        # after, rather than inferred from the exit code.
        motivo_prima = registro.motivo_registro_illeggibile(self.configurazione.adapters_path)
        self._esegui(
            corsa,
            fase,
            [
                "--manifest", str(corsa.manifest_path),
                "--adapters", str(self.configurazione.adapters_path),
                "--output", str(rapporto),
            ],
            script="impara_adattatore.py",
            uscite_ammesse=(0, 2),
        )
        self._dopo_il_passo(corsa, fase, [rapporto])
        documento = self._leggi_artefatto(corsa, rapporto) or {}
        imparati = self._nomi_del_rapporto(documento, "imparati") & attesi
        motivo_dopo = registro.motivo_registro_illeggibile(self.configurazione.adapters_path)
        if motivo_dopo and not motivo_prima:
            imparato = registro.percorso_imparato(self.configurazione.adapters_path)
            raise Fermata(
                codice="REGISTRO_ADATTATORI_ROVINATO",
                messaggio=(
                    "Ho memorizzato le colonne, e subito dopo il registro degli adattatori "
                    "imparati su questo computer non si è più letto: da adesso valgono solo "
                    "gli adattatori spediti col programma, quindi i fornitori imparati qui "
                    "possono tornare sconosciuti e per loro non nascerebbe nessuna copia "
                    "d'ordine. Il confronto precedente è rimasto attivo e non è stato "
                    "toccato. Chiudi e riapri il comparatore; se si ferma di nuovo qui, "
                    f"«{imparato.name}» va riparato o eliminato — eliminarlo fa ricominciare "
                    "l'apprendimento da zero e non rompe nient'altro."
                ),
                documenti=sorted(attesi),
                dettaglio=motivo_dopo,
            )
        rifiutati = imparabili - imparati
        if rifiutati:
            self._avvisa_schemi_non_memorizzati(
                corsa, rifiutati, self._motivi_del_rapporto(documento, rifiutati),
            )
        # The manual override is removed only for documents the registry
        # actually learned. For the rest, that decision is the only thing
        # still making them readable, and removing it here would mean next
        # week's price list stops opening at all.
        self._rimuovi_decisioni_imparate(imparati)

    @staticmethod
    def _nomi_del_rapporto(documento: Mapping[str, Any], chiave: str) -> set[str]:
        """File names listed under one key of the report, case-folded."""

        return {
            str(voce.get("file") or "").casefold()
            for voce in (documento.get(chiave) or [])
            if isinstance(voce, dict)
        }

    @staticmethod
    def _motivi_del_rapporto(documento: Mapping[str, Any], nomi: set[str]) -> dict[str, str]:
        """The reason `impara_adattatore` wrote, keyed by file name.

        Already a ready-to-read sentence; not rewritten here, just passed on
        to the user.
        """

        motivi: dict[str, str] = {}
        for voce in (documento.get("saltati") or []):
            if not isinstance(voce, dict):
                continue
            nome = str(voce.get("file") or "").casefold()
            if nome in nomi and nome not in motivi:
                motivi[nome] = str(voce.get("motivo") or "").strip()
        return motivi

    def _avvisa_schemi_non_memorizzati(
        self,
        corsa: "_Corsa",
        nomi: set[str],
        motivi: Mapping[str, str],
    ) -> None:
        """Reports each document the registry didn't learn, one warning per document.

        One per document rather than a single warning with a list inside:
        the file name is the only thing tying the message to the document
        card the user just configured. The message leads with the fact that
        the comparison itself is intact, since that's the first question a
        reader has: "did I lose my work?"
        """

        per_nome = {nome.casefold(): nome for nome in corsa.decisioni_da_imparare}
        for chiave in sorted(nomi):
            nome = per_nome.get(chiave, chiave)
            motivo = (motivi.get(chiave) or "").removeprefix("Rifiutato:").strip()
            altro = self._fornitore_che_se_lo_prende(motivo)
            if altro:
                # This rejection doesn't mean "not learned". It means that
                # with the current registry, this document now matches
                # another supplier's signature: next week's recompute won't
                # stop and won't ask anything, it will just read it as that
                # other supplier's price list. "Nothing to do" would be the
                # worst possible message here, since there's nothing to do
                # today but everything to do before the next run.
                self._avvisa(
                    "SCHEMA_NON_MEMORIZZATO",
                    f"«{nome}»: da adesso lo leggo come il listino di un altro fornitore",
                    f"Il confronto di adesso è giusto e «{nome}» ci sta dentro con le colonne "
                    f"che hai indicato. Ma il registro non ha potuto tenerlo separato: con lo "
                    f"schema di oggi quel documento combacia con «{altro}», e al prossimo "
                    "confronto verrebbe letto come il suo listino, senza chiedere niente. "
                    "I due sono troppo simili perché li distingua da solo: "
                    "prima del prossimo confronto vanno tenuti separati a mano — carica un "
                    "documento per volta, oppure chiedi al fornitore un listino riconoscibile."
                    + (f" Dettaglio: {motivo}" if motivo else ""),
                    severita="error",
                    documento=nome,
                    motivo=motivo,
                )
                continue
            self._avvisa(
                "SCHEMA_NON_MEMORIZZATO",
                f"«{nome}»: colonne usate adesso, non memorizzate",
                f"Il confronto è aggiornato e «{nome}» ci sta dentro con le colonne che hai "
                "indicato: quel lavoro non è andato perso. Quello che non ho potuto fare è "
                "ricordarmele per i prossimi listini di questo fornitore, quindi la prossima "
                "volta il confronto si fermerà di nuovo sulle colonne e te le richiederà. "
                "Adesso non c'è niente da fare."
                + (f" Motivo: {motivo}" if motivo else ""),
                documento=nome,
                motivo=motivo,
            )

    @staticmethod
    def _fornitore_che_se_lo_prende(motivo: str) -> str:
        """The adapter that claimed the document, when the rejection message says so.

        `impara_adattatore` rejects with a message naming the adapter the
        document now matches instead, when two suppliers export in the same
        layout. It's the only rejection that changes what happens next
        week, and needs a different message than the rest.
        """

        trovato = re.search(
            r"il documento risulta \S+ «([^»]+)» invece di", str(motivo or ""),
        )
        if not trovato:
            return ""
        identificativo = trovato.group(1).strip()
        if not identificativo or identificativo.casefold() == "none":
            return ""
        return registro.nome_del_fornitore(
            str(registro.adattatore(identificativo).get("supplier_id") or identificativo),
        ) or identificativo

    def _rimuovi_decisioni_imparate(self, nomi: set[str]) -> None:
        percorso = self.configurazione.decisioni_manuali_path
        documento = leggi_json(percorso, None)
        voci = documento.get("decisions") if isinstance(documento, dict) else documento
        if not isinstance(voci, list):
            return
        tenute = [
            voce for voce in voci
            if not isinstance(voce, dict)
            or str(voce.get("file_name") or "").casefold() not in nomi
        ]
        if tenute:
            scrivi_json(percorso, {"decisions": tenute})
            return
        try:
            percorso.unlink()
        except FileNotFoundError:
            pass

    def _avvisa_chi_non_si_compila(self, corsa: "_Corsa") -> None:
        """Flags any supplier in the comparison for whom no order file will be written.

        Whether a supplier's order can be written back into their own file
        format is declared by the registry (`order_write` on the adapter). A
        supplier without it stays in the comparison and can still win rows
        and take part of the order — but at the end, where its compiled
        order file is expected, there's nothing.

        Reported here, not at compilation time, because it's still possible
        to act on it now — by the time compilation runs, the merchandise has
        already been assigned.
        """

        motivo = registro.motivo_registro_illeggibile(self.configurazione.adapters_path)
        if motivo:
            # With a broken registry, every supplier would show up as "not
            # declared", sending the user to look for a declaration inside a
            # file that can't even be opened. There's exactly one cause, and
            # it's reported once.
            self._avvisa(
                "REGISTRO_ILLEGGIBILE",
                "Il registro degli adattatori non si legge",
                "Nessun fornitore risulta compilabile e nessuna copia d'ordine "
                "nascerà finché il registro non si ripara. " + motivo,
                severita="error",
            )
            return
        compilabili = registro.fornitori_con_scrittura(self.configurazione.adapters_path)
        senza = [
            nome for nome in (
                str((voce or {}).get("id") or "").strip().casefold()
                for voce in (corsa.confronto.get("suppliers") or [])
                if isinstance(voce, dict)
            )
            if nome and nome not in compilabili
        ]
        for nome in sorted(dict.fromkeys(senza)):
            self._avvisa(
                "FORNITORE_NON_COMPILABILE",
                f"{nome.upper()}: nessuna copia d'ordine da mandare",
                f"Il listino {nome.upper()} è nel confronto e può vincere, ma il registro non "
                "dichiara come si scrive l'ordine dentro il suo documento: per questo fornitore "
                "non verrà creata nessuna copia da mandare, e la merce che gli assegni resta "
                "da ordinare a mano.",
                fornitore=nome,
            )
        # Second family: suppliers the registry declares compilable but
        # whose write-back this week's document doesn't actually enable — a
        # changed header, a missing sheet, rows outside the expected range.
        # The launcher knows the specific cause; without this pass it would
        # only surface inside a message the server discards once writing
        # succeeds, and the user would discover it only at compilation time
        # with no explanation.
        #
        # Two functions, not one, because they look at different sources:
        # `fornitori_senza_copia` starts from the comparison's DOCUMENTS,
        # `fornitori_ordinati_senza_copia` starts from the SUPPLIERS the
        # comparison actually uses. The two lists diverge as soon as a price
        # list is deleted or replaced — in that case the first stays silent,
        # since its warnings come from the loop over resolved documents, and
        # zero resolved documents means zero warnings. Reporting "no copy
        # for this supplier" reliably needs both sources checked.
        from launcher import (  # deferred import, matching the server's own pattern
            fornitori_del_confronto,
            fornitori_ordinati_senza_copia,
            fornitori_senza_copia,
        )

        avvisati = set(senza)
        non_attivabili: dict[str, str] = {}
        dai_documenti = fornitori_senza_copia(
            corsa.confronto, self.configurazione.adapters_path
        )
        dalle_offerte = fornitori_ordinati_senza_copia(
            corsa.confronto,
            fornitori_del_confronto(corsa.confronto),
            self.configurazione.adapters_path,
        )
        for nome, frase in sorted({**dalle_offerte, **dai_documenti}.items()):
            if nome in avvisati:
                continue
            non_attivabili[nome] = frase
            self._avvisa(
                "FORNITORE_NON_COMPILABILE",
                f"{nome.upper()}: nessuna copia d'ordine da mandare",
                frase.rstrip(".")
                + ". Per questo fornitore non verrà creata nessuna copia da mandare, "
                "e la merce che gli assegni resta da ordinare a mano.",
                fornitore=nome,
            )
        tutti = sorted(avvisati | set(non_attivabili))
        if tutti:
            self._numeri(fornitoriSenzaCompilazione=tutti)

    # -- the check that warns and never blocks -------------------------------

    def _post_check(self, corsa: "_Corsa") -> None:
        """Compares against the previous run; anything it finds is a warning.

        When a price list's row count or prices shift a lot compared to the
        previous run, the run still completes and reports the difference.
        No threshold blocks the run: a wrong threshold would stop a good run
        every single week, and this program is meant to run unattended.
        """

        precedente = self._esecuzione_precedente(corsa.cartella)
        if precedente is None:
            self._numeri(confrontoConLaVoltaPrima="prima esecuzione")
            return
        vecchio = leggi_json(precedente / "dati" / "audit.json", None)
        if not isinstance(vecchio, dict):
            self._numeri(confrontoConLaVoltaPrima="audit precedente non leggibile")
            return

        vecchie_fonti = vecchio.get("sources") or {}
        nuove_fonti = corsa.audit.get("sources") or {}
        for nome in sorted(set(vecchie_fonti) | set(nuove_fonti)):
            prima = int((vecchie_fonti.get(nome) or {}).get("rows") or 0)
            adesso = int((nuove_fonti.get(nome) or {}).get("rows") or 0)
            if prima and not adesso:
                self._avvisa(
                    "FORNITORE_SPARITO",
                    f"{nome.upper()} non è più nel confronto",
                    f"La volta prima aveva {prima} righe e oggi non c'è.",
                )
                continue
            if adesso and not prima:
                self._avvisa(
                    "FORNITORE_NUOVO",
                    f"{nome.upper()} entra nel confronto",
                    f"Non c'era nella volta prima e oggi porta {adesso} righe.",
                )
                continue
            if prima and abs(adesso - prima) / prima > SCARTO_RIGHE_DA_SEGNALARE:
                self._avvisa(
                    "RIGHE_CAMBIATE_MOLTO",
                    f"Il listino {nome.upper()} è cambiato molto",
                    f"Da {prima} a {adesso} righe ordinabili "
                    f"({(adesso - prima) * 100 / prima:+.0f}%). Il confronto è pronto lo stesso.",
                )

        vecchi_prezzi = vecchio.get("price_summary") or {}
        nuovi_prezzi = corsa.audit.get("price_summary") or {}
        for nome in sorted(set(vecchi_prezzi) & set(nuovi_prezzi)):
            prima = _numero((vecchi_prezzi.get(nome) or {}).get("median"))
            adesso = _numero((nuovi_prezzi.get(nome) or {}).get("median"))
            # A collapse to nothing — a missing or zero median — isn't
            # reported here: `_controlla_i_prezzi` already covers that, and
            # doesn't need a previous run to compare against. This check is
            # for scale changes between two runs, where a zero as the
            # baseline wouldn't say anything meaningful.
            if not prima or not adesso:
                continue
            if abs(adesso - prima) / prima > SCARTO_PREZZO_DA_SEGNALARE:
                self._avvisa(
                    "PREZZI_CAMBIATI_IN_BLOCCO",
                    f"I prezzi {nome.upper()} sono cambiati in blocco",
                    f"Il prezzo mediano al pezzo passa da {prima:.4f} a {adesso:.4f} "
                    f"({(adesso - prima) * 100 / prima:+.0f}%). Vale la pena guardarlo prima "
                    "di mandare l'ordine.",
                )

        # The previous run's model and prompt versions: this is where a
        # provenance mismatch actually means something. An order built with
        # a different prompt than a week ago isn't wrong, but it's the only
        # explanation for a comparison changing while the price lists
        # themselves haven't.
        vecchio_rapporto = leggi_json(precedente / "dati" / "ai_rapporto.json", None)
        if isinstance(vecchio_rapporto, dict):
            cambiate = [
                f"{etichetta}: da «{vecchio_rapporto.get(chiave)}» a «{corsa.rapporto_ai.get(chiave)}»"
                for chiave, etichetta in (
                    ("model", "modello"),
                    ("versione_prompt", "prompt"),
                    ("versione_avversario", "verifica avversariale"),
                )
                if vecchio_rapporto.get(chiave) not in (None, corsa.rapporto_ai.get(chiave))
            ]
            if cambiate:
                self._avvisa(
                    "CONFIGURAZIONE_AI_CAMBIATA",
                    "La valutazione automatica gira con una configurazione diversa",
                    "Rispetto alla volta prima è cambiato " + "; ".join(cambiate)
                    + ". Le corrispondenze proposte possono essere diverse anche a listini "
                    "identici.",
                )
        self._numeri(confrontoConLaVoltaPrima=precedente.name)

    def _ripulisci_le_esecuzioni(self, corsa: Path) -> int:
        """Deletes the oldest run folders and returns how many were removed.

        Two folders are never touched, and not because they're recent: the
        run about to start, and the one behind the live comparison. The
        latter is what `colonne_dei_documenti` reopens to report which
        columns it read per document, and what the guided-mapping flow
        reopens when the pipeline has stopped; deleting it would leave the
        page reporting "La cartella di quel confronto non c'è più." (that
        comparison's folder is gone) for a comparison still being looked at.

        Never raises: freeing disk space is a convenience, and a folder that
        can't be deleted (antivirus, open handle, permissions) must not be
        able to block the recompute the user just asked for.
        """

        radice = self.configurazione.esecuzioni_dir
        intoccabili = {corsa.name}
        vivo = leggi_json(self.configurazione.review_path, None)
        run_vivo = (vivo if isinstance(vivo, dict) else {}).get("run")
        dichiarata = str((run_vivo if isinstance(run_vivo, dict) else {}).get("pipelineRunId") or "")
        if dichiarata:
            intoccabili.add(dichiarata)
        try:
            with os.scandir(radice) as scansione:
                nomi = sorted(
                    voce.name for voce in scansione
                    if voce.is_dir() and not voce.name.startswith(".")
                )
        except OSError:
            return 0
        # Folder names carry the date, so alphabetical order is already
        # chronological order.
        da_tenere = set(nomi[-ESECUZIONI_DA_TENERE:]) | intoccabili
        tolte = 0
        for nome in nomi:
            if nome in da_tenere:
                continue
            cartella = consegna.cartella_sicura(radice, nome)
            if cartella is None:
                continue
            try:
                shutil.rmtree(cartella)
            except OSError:
                continue
            tolte += 1
        return tolte

    def _esecuzione_precedente(self, corsa_corrente: Path) -> Path | None:
        """The last fully completed run before this one, or `None`.

        Scans the disk rather than keeping an index: a separate index is one
        more thing that can drift out of sync with what's actually there.

        "Completed" is the key requirement. `dati/audit.json` is written
        mid-pipeline by the parsing phase; the run's own audit record is
        written by the `finally` block at the very end. A run killed in
        between — the machine loses power, the process is killed — leaves
        the first file but not the second, and using it as the comparison
        baseline would compare against a comparison that was never actually
        activated, silently muting warnings like a supplier disappearing.
        So this looks at the run's own audit record and requires it to
        declare a run that reached the end: a run stopped halfway updated
        nothing, and the true previous run is the one before that.

        Before scanning, the live comparison is checked first:
        `review_data.json` declares which run activated it
        (`run.pipelineRunId`), and that folder is the previous run by
        definition — even if the run died moments after the atomic swap,
        before it had time to mark its own record complete. Requiring the
        completed-record check on the live comparison too would make it
        disappear from the baseline and silently mute the same warnings.
        Scanning the disk remains a fallback, for comparisons activated
        before this field existed.
        """

        radice = self.configurazione.esecuzioni_dir

        vivo = leggi_json(self.configurazione.review_path, None)
        run_vivo = (vivo if isinstance(vivo, dict) else {}).get("run")
        dichiarata = str((run_vivo if isinstance(run_vivo, dict) else {}).get("pipelineRunId") or "")
        if dichiarata and dichiarata != corsa_corrente.name:
            attivata = consegna.cartella_sicura(radice, dichiarata)
            if attivata is not None and (attivata / "dati" / "audit.json").is_file():
                return attivata

        candidate: list[tuple[float, Path]] = []
        try:
            with os.scandir(radice) as scansione:
                nomi = [voce.name for voce in scansione if voce.is_dir() and not voce.name.startswith(".")]
        except OSError:
            return None
        for nome in nomi:
            cartella = consegna.cartella_sicura(radice, nome)
            if cartella is None or cartella == corsa_corrente:
                continue
            if not (cartella / "dati" / "audit.json").is_file():
                continue
            audit = leggi_json(cartella / NOME_AUDIT_ESECUZIONE, None)
            if not isinstance(audit, dict) or audit.get("stato") != COMPLETATO:
                continue
            # The timestamp comes from the run's own audit record and
            # nowhere else: a file's mtime is its last-write time, which a
            # copy, an antivirus scan or a backup can push forward at will.
            # A record with no declared start time isn't a usable baseline
            # and is skipped, like any other run that can't be read.
            if not isinstance(audit.get("iniziatoIl"), str):
                continue
            try:
                istante = datetime.fromisoformat(audit["iniziatoIl"])
            except ValueError:
                continue
            if istante.tzinfo is None:
                # `avvia()` always writes the timestamp with a timezone; one
                # without was written by something else, and `timestamp()`
                # on a naive datetime assumes local time — mixing the two
                # conventions can invert the ordering of runs by hours.
                continue
            try:
                quando = istante.timestamp()
            except (OSError, OverflowError):
                continue
            candidate.append((quando, cartella))
        if not candidate:
            return None
        candidate.sort(key=lambda voce: (voce[0], voce[1].name))
        return candidate[-1][1]

    # -- 9. ATTIVAZIONE ----------------------------------------------------

    def _fase_attivazione(self, corsa: "_Corsa") -> None:
        """The only step that touches the live comparison, and it has preconditions.

        The checks are numeric, not impressionistic: a comparison with no
        products or no suppliers isn't a comparison, and is exactly the
        shape an unnoticed upstream failure would take. A run that fails
        them activates nothing and says so, and the previous comparison
        stays intact.
        """

        fase = "ATTIVAZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        confronto = corsa.confronto
        prodotti = confronto.get("products") or []
        fornitori = confronto.get("suppliers") or []
        motivi: list[str] = []
        if not prodotti:
            motivi.append("il confronto non contiene nessun prodotto")
        if not fornitori:
            motivi.append("il confronto non contiene nessun fornitore")
        ricevuti = corsa.rapporto_ai.get("casi_ricevuti")
        if isinstance(ricevuti, int) and ricevuti != corsa.numero_shortlist:
            motivi.append(
                f"la fase AI dichiara {ricevuti} casi ricevuti e i candidati prodotti sono "
                f"{corsa.numero_shortlist}"
            )
        senza_shortlist = (corsa.riepiloghi.get("risoluzione") or {}).get("coppie_senza_shortlist")
        if isinstance(senza_shortlist, int) and senza_shortlist:
            motivi.append(f"{senza_shortlist} coppie semantiche non hanno mai avuto candidati")
        if motivi:
            raise Fermata(
                codice="ATTIVAZIONE_RIFIUTATA",
                messaggio=(
                    "Il nuovo confronto non è stato attivato perché "
                    + "; ".join(motivi)
                    + ". Quello di prima è rimasto al suo posto."
                ),
            )

        # Everything that must accompany the new comparison happens before
        # the new comparison becomes the live one, not after. Doing it after
        # had two measured consequences: warnings raised past that point
        # never made it into the document (only the snapshot taken below
        # does), and a process dying in the window between the atomic swap
        # and reconfiguration would leave the new comparison live with last
        # week's order-writing configuration — last week's price list
        # against this week's row numbers. In this order that window closes
        # on the safe side: dying here still leaves the previous comparison
        # live.
        if self.su_confronto_attivato is not None:
            try:
                self.su_confronto_attivato(deepcopy(confronto))
            except Exception as exc:
                # Order compilation is reconfigured before activation; if
                # that fails the comparison is still good and gets activated
                # anyway, but it has to be reported — otherwise compilation
                # would silently keep using last week's price list. Not the
                # only safeguard: at compile time the configuration's run id
                # must match the active comparison's, and a mismatch stops it.
                self._avvisa(
                    "COMPILAZIONE_DA_RICONFIGURARE",
                    "Le copie dei listini non sono state riconfigurate",
                    "Il confronto è aggiornato. La compilazione va ricontrollata prima di "
                    f"creare le copie. Dettaglio: {type(exc).__name__}: {exc}",
                )

        # From here to the swap of the live comparison, the routes lock is
        # held: this is the only stretch where the pipeline writes data the
        # HTTP service is actively serving. Without it, a page save landing
        # in the middle would write `state.json` while `_ripulisci_stato`
        # was rewriting it, leaving the page with the old state and the new
        # comparison mismatched. The stretch is kept short on purpose: order
        # compilation's own reconfiguration, which shells out to Node, stays
        # outside it.
        with self.lucchetto_dati:
            # The state's raw bytes, saved before cleanup. `_ripulisci_stato`
            # below rewrites `state.json` — clearing stale selections,
            # unlinking decisions, refreshing quantities from the reorder
            # list — and the new comparison only goes live at the very end;
            # if that final swap fails, the page would be left with the old
            # comparison but the already-cleared decisions, i.e. quantities
            # reset to zero with nothing visible to explain why. This is a
            # real failure mode on Windows, where `os.replace` can fail
            # while an antivirus, a backup tool or a sync client holds
            # `review_data.json` open. Either both files change or neither does.
            try:
                stato_prima = self.configurazione.state_path.read_bytes()
            except OSError:
                # If it can't even be read, there's nothing to restore:
                # proceed as if this safeguard weren't here.
                stato_prima = None
            try:
                rimossi, conferme_scadute, riprese, scollegate = self._ripulisci_stato(confronto)
                # Quantities refreshed from the reorder list do NOT raise a
                # warning, deliberately: quantity is always driven by the
                # management-software export, and a warning firing on every
                # single recompute just to say the program worked would be
                # noise. The count stays in the run's own numbers for anyone
                # who wants it.
                self._numeri(quantitaRiprese=riprese)
                if scollegate:
                    plurale = scollegate != 1
                    self._avvisa(
                        "DECISIONI_SCOLLEGATE",
                        "Alcune righe portano ora un altro prodotto",
                        f"{scollegate} {'righe dell’elenco portano' if plurale else 'riga dell’elenco porta'} "
                        f"un articolo diverso da prima: fornitore scelto, conferma ed esclusione di "
                        f"{'quelle righe' if plurale else 'quella riga'} non valevano più e sono stati rifatti "
                        "da capo con il confronto nuovo.",
                    )
                if rimossi:
                    self._avvisa(
                        "SCELTE_NON_PIU_VALIDE",
                        "Alcune scelte precedenti sono state azzerate",
                        f"{rimossi} prodotti avevano una quantità su un'offerta che il listino nuovo "
                        "non ha più: la quantità è tornata a zero.",
                    )
                if conferme_scadute:
                    plurale = conferme_scadute != 1
                    self._avvisa(
                        "CONFERME_SCADUTE",
                        "Alcune conferme vanno rifatte",
                        f"{conferme_scadute} {'prodotti sono stati abbinati' if plurale else 'prodotto è stato abbinato'} "
                        "a una riga diversa del listino: la conferma che avevi dato valeva per "
                        "l'articolo di prima. Rileggi nome e codice e riconferma nel passo 2.",
                    )

                # The run's warnings are carried into the document: two
                # minutes later nobody is watching the progress bar anymore,
                # and a warning that lives only in the job's state is a
                # warning nobody reads.
                avvisi_run = [dict(voce) for voce in (self.stato().get("avvisi") or [])]
                if avvisi_run:
                    confronto.setdefault("warnings", [])
                    confronto["warnings"] = [*avvisi_run, *(confronto.get("warnings") or [])]
                confronto.setdefault("run", {})["pipelineRunId"] = corsa.cartella.name
                scrivi_json(corsa.review_path, confronto)
                corsa.registro_artefatti[self._chiave_artefatto(corsa, corsa.review_path)] = {
                    "fase": fase,
                    "sha256": impronta_file(corsa.review_path),
                    "byte": corsa.review_path.stat().st_size,
                    "scritto_il": utc_ora(),
                }

                vivo = self.configurazione.review_path
                # This swap also goes through `scrittura_sicura`, on the
                # largest and most important file the program writes — a
                # power loss right after a naive replace would leave it
                # present but truncated, exactly the failure mode this
                # helper exists to close off.
                scrittura_sicura.scrivi_bytes(vivo, corsa.review_path.read_bytes())
            except Exception:
                if stato_prima is not None:
                    scrittura_sicura.scrivi_bytes(self.configurazione.state_path, stato_prima)
                raise

        corsa.messaggio_finale = (
            f"Confronto aggiornato: {len(prodotti)} prodotti, {len(fornitori)} fornitori."
        )
        self._segna_fase(
            fase, COMPLETATO, corsa.messaggio_finale, time.monotonic() - inizio
        )

    def _rifiuti_in_vigore(self) -> dict[tuple[str, str], dict[str, Any]]:
        """The user's rejections ("not the same item"), or none if unreadable.

        Failing to read them is safe by construction: the offer counts as
        available again, the quantity resets to zero, and the row waits for
        a choice — i.e. the user is asked again rather than an order being
        placed on their behalf.
        """

        if self.rifiuti_dichiarati is None:
            return {}
        try:
            return self.rifiuti_dichiarati() or {}
        except Exception as exc:  # noqa: BLE001 - a missing cache must not stop a recompute
            self._numeri(rifiutiNonLetti=f"{type(exc).__name__}: {exc}")
            return {}

    @staticmethod
    def _e_rifiutata(
        rifiuti: dict[tuple[str, str], dict[str, Any]],
        articolo: str,
        fornitore: str,
        offerta: dict[str, Any],
    ) -> bool:
        """Same rule as `ReviewStore.spegni_le_offerte_rifiutate`, applied row by row."""

        if not rifiuti or not articolo:
            return False
        voce = rifiuti.get((str(fornitore or "").strip().casefold(), articolo))
        return voce is not None and str(voce.get("offerta") or "") == impronta_articolo(offerta)

    def _ripulisci_stato(self, confronto: dict[str, Any]) -> tuple[int, int, int, int]:
        """Resets any saved selection the new comparison can no longer support.

        A product that no longer exists, or an offer that's gone, would
        otherwise leave a quantity assigned to a supplier who can't deliver
        it: on the next save the user would see everything rejected as
        invalid, with no indication of which row.

        The check isn't "is the saved supplier still there", it's "can
        anyone still supply this":

        * at least one supplier still has a usable offer — deciding which
          one gets the quantity is a choice the program doesn't make for the
          user: the quantity resets to zero as always and the row waits for
          them to choose;
        * no supplier has one at all — there's nothing left to choose.
          Zeroing the quantity here would erase the one thing known about
          that row, how many are needed, and `validate_snapshot` accepts a
          quantity with no usable offer from anyone. The quantity stays and
          the supplier field is cleared: this is an item to source, and it
          enters the "items to source" list at compilation.

        Returns (quantities reset, confirmations expired, quantities
        refreshed from the management export, decisions unlinked). Items to
        source aren't counted here and raise no warning: nothing was lost,
        and a warning that fires just to say the program worked would be
        noise.

        Expired confirmations cover a separate case: the supplier still has
        an offer, so nothing gets zeroed, but that offer is now a different
        row of the price list — different barcode, different description.
        The "confirm same item" checkbox would otherwise stay checked
        against the wrong item, and compilation would proceed silently.

        The remaining two counters exist because the comparison's own
        product identifiers can shift between runs. A newly built
        comparison can be correct — the management export's new quantities
        are already in `review_data.json` — while `ReviewStore.review()`
        still overlays every product with its saved decision, and that
        decision came from the previous product list. Two separate rules
        answer two different questions:

        * a decision applies to the item it was made on. Identifiers are row
          numbers from the management export: with a new export, row 3 can
          be a different product, and the supplier choice, confirmation and
          exclusion made on the old row 3 no longer apply to it. When the
          item has changed, the decision is unlinked and the row starts
          fresh from what the new comparison says;
        * a quantity sourced from the management export is not a decision:
          it's a copy of what the export says, re-read on every recompute
          unconditionally. Quantities the user typed themselves
          (`quantitySource: "utente"`) are left untouched — those are the
          user's own — except for a zero that was never really user-entered
          but is recognized as coming from that export (handled in the loop
          below). One deliberate consequence: "reset default quantities"
          only holds until the next recompute, because what it resets isn't
          a saved choice but a copy of the export.
        """

        stato = leggi_json(self.configurazione.state_path, None)
        if not isinstance(stato, dict) or not isinstance(stato.get("products"), list):
            return 0, 0, 0, 0
        # Manually added products aren't part of the comparison — they come
        # from the state on every read — but their decisions are decisions
        # like any other. Without this line they'd disappear on every
        # recompute, and the quantity on a manually added product would
        # reset to zero with the wrong explanation ("the new price list no
        # longer has the offer").
        prodotti_del_confronto = [
            *(confronto.get("products") or []),
            *(voce for voce in stato.get("manualProducts") or [] if isinstance(voce, dict)),
        ]
        offerte_valide: dict[str, set[str]] = {}
        offerte_per_fornitore: dict[tuple[str, str], dict[str, Any]] = {}
        quantita_del_confronto: dict[str, int] = {}
        articolo_adesso: dict[str, str] = {}
        # Offers the user has rejected don't count as "someone can still
        # supply this". The comparison read here is the raw one — the layer
        # that hides a rejected offer lives on the service's own read path,
        # not here — so the same check is repeated: matching supplier,
        # matching item, matching fingerprint of the row the rejection was
        # made on.
        rifiuti = self._rifiuti_in_vigore()
        for prodotto in prodotti_del_confronto:
            identificativo_prodotto = str(prodotto.get("id") or "")
            disponibili = set()
            articolo_del_gestionale = impronta_prodotto(prodotto) if rifiuti else ""
            for offerta in prodotto.get("offers") or []:
                fornitore_offerta = offer_supplier_id(offerta)
                if offer_is_available(offerta) and not self._e_rifiutata(
                    rifiuti, articolo_del_gestionale, fornitore_offerta, offerta,
                ):
                    disponibili.add(fornitore_offerta)
                offerte_per_fornitore[(identificativo_prodotto, fornitore_offerta)] = offerta
            offerte_valide[identificativo_prodotto] = disponibili
            quantita_del_confronto[identificativo_prodotto] = int(_numero(prodotto.get("quantity")) or 0)
            articolo_adesso[identificativo_prodotto] = articolo_della_riga(prodotto)

        # The live comparison on disk is still the previous one: this looks
        # at what each of its rows used to hold. It's only trusted when the
        # state actually belongs to that run; otherwise nothing is known
        # about those rows, and a decision is never unlinked on a mere guess.
        precedente = leggi_json(self.configurazione.review_path, None)
        if not isinstance(precedente, dict) or str(stato.get("runId") or "") != str(
            (precedente.get("run") or {}).get("id") or ""
        ):
            precedente = None
        articolo_prima: dict[str, str] = {}
        # How many units that row's export asked for at the time: 0 if the
        # export said zero, `None` if the column was blank. This is the only
        # place that distinction is still recorded, and it's needed below.
        suggerito_prima: dict[str, Any] = {}
        if precedente is not None:
            for prodotto in precedente.get("products") or []:
                if isinstance(prodotto, dict):
                    identificativo_prima = str(prodotto.get("id") or "")
                    articolo_prima[identificativo_prima] = articolo_della_riga(prodotto)
                    suggerito_prima[identificativo_prima] = prodotto.get("suggestedQuantity")

        azzerati = 0
        conferme_scadute = 0
        riprese_dal_gestionale = 0
        scollegate = 0
        # Not returned, and never raises a warning on its own: only used to
        # force a state rewrite when the only thing that changed is the
        # supplier field being cleared on a product nobody carries anymore.
        da_reperire = 0
        # Same purpose: forces a state rewrite when the only thing that
        # changed is the quantity's source label, with the number itself
        # staying zero.
        marchi_corretti = 0
        tenute: list[dict[str, Any]] = []
        for decisione in stato.get("products") or []:
            if not isinstance(decisione, dict):
                continue
            identificativo = str(decisione.get("id") or "")
            disponibili = offerte_valide.get(identificativo)
            if disponibili is None:
                # The product no longer exists: its decision has nowhere
                # left to attach and is dropped with it.
                if int(_numero(decisione.get("quantity")) or 0) > 0:
                    azzerati += 1
                continue
            prima = articolo_prima.get(identificativo, "")
            adesso_articolo = articolo_adesso.get(identificativo, "")
            if prima and adesso_articolo and prima != adesso_articolo:
                # Same row id, different item: the decision is unlinked and
                # the row starts fresh from what the new comparison says.
                scollegate += 1
                continue
            # A zero already saved on disk can carry the wrong source label:
            # an older `state.json` may hold a zero from the export labeled
            # "utente", and the label persists until the user starts over
            # from a clean slate (which wipes `state.json`): the new export
            # asks for 4, the row stays at 0, and nothing reports it. Relabeling
            # every such zero would be worse than the bug it fixes: a zero
            # labeled "utente" can also be a deliberate "don't order this"
            # written over a suggested quantity, and re-reading it from the
            # export would order stock the user had intentionally removed.
            # The safe signal is what that row's export actually said: if it
            # asked for zero — zero, not a blank column — that zero really
            # is the export's own, and gets its label corrected. From there
            # the branch below re-reads it from the current export, and the
            # state heals itself on the next recompute. The item identity
            # was already checked above, so a row now carrying a different
            # product has already been unlinked by this point.
            if (
                str(decisione.get("quantitySource") or "") == "utente"
                and int(_numero(decisione.get("quantity")) or 0) == 0
                and _numero(suggerito_prima.get(identificativo)) == 0
            ):
                decisione = {**decisione, "quantitySource": "gestionale"}
                marchi_corretti += 1
            if str(decisione.get("quantitySource") or "") == "gestionale":
                dal_confronto = quantita_del_confronto.get(identificativo, 0)
                if dal_confronto != int(_numero(decisione.get("quantity")) or 0):
                    decisione = {**decisione, "quantity": dal_confronto}
                    riprese_dal_gestionale += 1
            quantita = int(_numero(decisione.get("quantity")) or 0)
            fornitore = str(decisione.get("selectedSupplierId") or "")
            if quantita > 0 and (not fornitore or fornitore not in disponibili):
                if disponibili:
                    # Someone can still supply it, just not the one chosen:
                    # the quantity resets to zero because deciding which
                    # offer to use is a choice the program doesn't make for
                    # the user.
                    decisione = {**decisione, "quantity": 0, "selectedSupplierId": "", "confirmed": False}
                    decisione.pop("confirmedArticle", None)
                    azzerati += 1
                else:
                    # No supplier has it: there's nothing left to choose,
                    # and zeroing the quantity would erase the one thing
                    # known about this row — how many are needed. The
                    # quantity stays, the supplier field is cleared: this is
                    # an item to source, not an error.
                    ripulita = {**decisione, "selectedSupplierId": "", "confirmed": False}
                    ripulita.pop("confirmedArticle", None)
                    if ripulita != decisione:
                        # Without this counter the state wouldn't be
                        # rewritten, and the vanished supplier would stay on
                        # disk.
                        da_reperire += 1
                    decisione = ripulita
            elif decisione.get("confirmed"):
                # The confirmation applies to the item the user actually
                # looked at. If the state doesn't record which one — saved
                # before this rule existed — it's still expired: not knowing
                # what was confirmed is not a confirmation.
                offerta = offerte_per_fornitore.get((identificativo, fornitore))
                confermato = str(decisione.get("confirmedArticle") or "")
                adesso = impronta_articolo(offerta) if offerta else ""
                if not confermato or not adesso or confermato != adesso:
                    decisione = {**decisione, "confirmed": False}
                    decisione.pop("confirmedArticle", None)
                    conferme_scadute += 1
            tenute.append(decisione)
        if (
            azzerati
            or conferme_scadute
            or riprese_dal_gestionale
            or scollegate
            or da_reperire
            or marchi_corretti
            or len(tenute) != len(stato.get("products") or [])
        ):
            stato["products"] = tenute
            stato["runId"] = str((confronto.get("run") or {}).get("id") or stato.get("runId") or "")
            stato["updatedAt"] = utc_ora()
            scrivi_json(self.configurazione.state_path, stato)
        elif str(stato.get("runId") or "") != str((confronto.get("run") or {}).get("id") or ""):
            # The run id changes on every recompute, and the state has to
            # follow it: `PUT /api/state` rejects a snapshot from another run.
            stato["runId"] = str((confronto.get("run") or {}).get("id") or "")
            stato["updatedAt"] = utc_ora()
            scrivi_json(self.configurazione.state_path, stato)
        return azzerati, conferme_scadute, riprese_dal_gestionale, scollegate


@dataclass
class _Corsa:
    """Scratchpad for one run: paths, numbers and summaries."""

    cartella: Path
    registro_artefatti: dict[str, dict[str, Any]]
    profili_path: Path = Path()
    profili_usati_path: Path = Path()
    decisioni_path: Path = Path()
    manifest_path: Path = Path()
    validazione_path: Path = Path()
    dati_dir: Path = Path()
    shortlists_path: Path = Path()
    decisioni_ai_path: Path = Path()
    rapporto_ai_path: Path = Path()
    risolti_path: Path = Path()
    review_path: Path = Path()
    profili: list[dict[str, Any]] = field(default_factory=list)
    decisioni_da_imparare: list[str] = field(default_factory=list)
    fornitori_attesi: list[str] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    rapporto_ai: dict[str, Any] = field(default_factory=dict)
    confronto: dict[str, Any] = field(default_factory=dict)
    riepiloghi: dict[str, Any] = field(default_factory=dict)
    provenienza: dict[str, Any] = field(default_factory=dict)
    comandi: list[dict[str, Any]] = field(default_factory=list)
    numero_shortlist: int = 0
    messaggio_finale: str = ""
