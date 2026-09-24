#!/usr/bin/env python3
"""What was on disk when the program started.

Python loads each module once. If the code on disk changes while the server
keeps running, a process can end up hybrid: some modules are still whatever
was loaded at startup, others get read from disk later, the first time
something imports them lazily. A module imported late can see a newer
version of a function that an early-imported module already expects to have
a different shape, and the result is a confusing traceback with no fix the
user can act on.

This module fingerprints the program's source files at import time, so it
can tell later whether anything on disk has changed and point the user to
the one thing that fixes it: restart the app.

Fingerprints are taken at import, which happens at server startup
(`server.py` imports `pipeline_jobs` at the top, which imports this module).
A late import would fingerprint the disk long after startup and miss
whatever changed before it ran.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
SKILL_ROOT = APP_DIR.parent
# Only the Python code the process keeps in memory. Not `static/`: the
# browser re-fetches the page and its JS on every load, so an old server
# still serves the current file. `references/` and `app/data/` are data and
# change routinely; flagging them would be noise on every run.
CARTELLE_DEL_CODICE = (APP_DIR, SKILL_ROOT / "scripts")
# `tests/` isn't under these folders, so it needs no exclusion here. `vendor`
# and `__pycache__` are excluded: the first is third-party code nobody
# hand-edits, the second is generated and changes on its own.
CARTELLE_ESCLUSE = frozenset({"__pycache__", "vendor", "node_modules", "data"})

FRASE_DEL_RIMEDIO = (
    "Il programma è stato aggiornato dopo che l'hai aperto: in memoria sta "
    "ancora girando la versione caricata all'avvio, e le due metà non vanno "
    "d'accordo. Chiudi il comparatore e riaprilo con AVVIA_COMPARATORE.cmd, "
    "poi rifai il confronto. Non perdi niente: i listini caricati, il confronto "
    "attivo e le tue scelte restano dove sono."
)


def _impronta(percorso: Path) -> str | None:
    """Fingerprint of the file, or `None` if it can't be read right now.

    An unreadable file isn't proof of anything — on Windows an editor or
    antivirus can hold it briefly — and this function's answer decides
    whether a run is allowed to start: a false positive here would block a
    legitimate recompute.
    """

    try:
        return hashlib.sha256(percorso.read_bytes()).hexdigest()
    except OSError:
        return None


def _e_da_guardare(percorso: Path, radice: Path = SKILL_ROOT) -> bool:
    """Whether this file counts as "program code".

    Today `vendor`, `__pycache__` and `app/data` hold no `.py` files, so the
    filter is a no-op in practice — it exists for when a vendored library
    gets updated in place, which shouldn't be reported as the program itself
    changing under the user.
    """

    parti = percorso.relative_to(radice).parts[:-1]
    return not any(parte in CARTELLE_ESCLUSE for parte in parti)


def _file_del_codice() -> list[Path]:
    trovati: list[Path] = []
    for cartella in CARTELLE_DEL_CODICE:
        if not cartella.is_dir():
            continue
        trovati.extend(
            percorso for percorso in sorted(cartella.rglob("*.py"))
            if _e_da_guardare(percorso)
        )
    return trovati


def fotografia() -> dict[str, str | None]:
    """Relative path → fingerprint, for every Python source of the program."""

    return {
        str(percorso.relative_to(SKILL_ROOT)).replace("\\", "/"): _impronta(percorso)
        for percorso in _file_del_codice()
    }


ALL_AVVIO: dict[str, str | None] = fotografia()


def file_cambiati(riferimento: dict[str, str | None] | None = None) -> list[str]:
    """Code files that differ from how they were at startup.

    A file that appeared or disappeared counts as changed. A file that
    couldn't be read — then or now — doesn't: not knowing isn't the same as
    knowing it changed.
    """

    prima = ALL_AVVIO if riferimento is None else riferimento
    adesso = fotografia()
    cambiati: list[str] = []
    for nome in sorted(set(prima) | set(adesso)):
        if nome not in prima or nome not in adesso:
            cambiati.append(nome)
            continue
        vecchia, nuova = prima[nome], adesso[nome]
        if vecchia is None or nuova is None:
            continue
        if vecchia != nuova:
            cambiati.append(nome)
    return cambiati


def avviso_del_codice_cambiato(riferimento: dict[str, str | None] | None = None) -> str | None:
    """The message to show, or `None` if the program is still the one from startup."""

    cambiati = file_cambiati(riferimento)
    if not cambiati:
        return None
    elenco = ", ".join(cambiati[:5])
    if len(cambiati) > 5:
        elenco += f" e altri {len(cambiati) - 5}"
    return f"{FRASE_DEL_RIMEDIO} (file cambiati dopo l'avvio: {elenco})"


def firma(riferimento: dict[str, str | None] | None = None) -> str:
    """Reduce every source fingerprint to one comparable string.

    Lets two sides answer "is this the same build?" without exchanging the
    full file list: the service reports its own signature at `/api/health`
    and the launcher compares it against the files on disk.

    With no arguments this is the signature of what's currently running
    (`ALL_AVVIO`, the snapshot taken at import); passed `fotografia()`, it's
    the signature of the disk right now.

    A file that couldn't be read stays distinct from both a missing file and
    a readable one: two different builds must not be able to produce the
    same signature because of a read error. That's why each line carries a
    presence flag (`1`/`0`) before the value, rather than a placeholder like
    `?` — a placeholder is a value like any other, and a file whose content
    happened to equal that placeholder would collapse onto "unreadable".
    """

    fonte = ALL_AVVIO if riferimento is None else riferimento
    materiale = "\n".join(
        f"{nome}\t{'1' if impronta is not None else '0'}\t{impronta or ''}"
        for nome, impronta in sorted(fonte.items())
    )
    return hashlib.sha256(materiale.encode("utf-8")).hexdigest()


def firma_del_disco() -> str:
    """The signature of the sources as they are right now, re-read from disk."""

    return firma(fotografia())


def stato() -> dict[str, Any]:
    """The summary, shaped for a JSON response."""

    cambiati = file_cambiati()
    return {
        "codiceCambiatoDopoLAvvio": bool(cambiati),
        "fileCambiati": cambiati,
        "messaggio": avviso_del_codice_cambiato() or "",
    }


# Which published version is running.
# Read once: syncing to GitHub happens before the server starts, so the
# answer can't change while the process is up, and it isn't worth spawning a
# `git` process on every request.
_data_pubblicata: str | None = None
_data_gia_cercata = False

# PATH isn't reliable on the store PC: even a bare `python` there can be a
# broken Microsoft Store stub. For the same reason, `AVVIA_COMPARATORE.ps1:49`
# doesn't look up a bare "git": it tries this absolute path first and falls
# back to `Get-Command` only if that's missing.
_GIT_ASSOLUTO_WINDOWS = Path(r"C:\Program Files\Git\cmd\git.exe")


def _eseguibile_git() -> str:
    """Which `git` to run: same criterion as `AVVIA_COMPARATORE.ps1`.

    Tries the standard Windows install path first, then
    `shutil.which("git")`, which resolves PATH once and returns an absolute
    path rather than a bare name. Falls back to the string "git" if neither
    exists — the same command this function replaces, so the fallback can't
    make things worse than before: it raises the same `OSError`, and
    `pubblicata()` returns `None` as it already did.
    """

    if _GIT_ASSOLUTO_WINDOWS.is_file():
        return str(_GIT_ASSOLUTO_WINDOWS)
    return shutil.which("git") or "git"


def pubblicata() -> str | None:
    """ISO 8601 date of the commit this disk is running.

    Why this exists: this PC syncs the program to GitHub on every startup,
    and when the sync fails — expired credentials, no network — by design it
    does not block startup: it logs a line to the console and starts with
    what it has. Nobody reads that line. Without a date shown in the page, a
    build stopped for months and one updated this morning look identical;
    the only way to notice otherwise is that some expected feature is
    missing, by which point it's too late.

    Returns `None` if git isn't available or this folder isn't a repository.
    The page then says it doesn't know, which is itself useful information,
    unlike staying silent.
    """

    global _data_pubblicata, _data_gia_cercata
    if _data_gia_cercata:
        return _data_pubblicata
    _data_gia_cercata = True

    try:
        esito = subprocess.run(
            [_eseguibile_git(), "-C", str(SKILL_ROOT), "log", "-1", "--format=%cI"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # No git, or it didn't respond: proceed without it. Not a program
        # error, and it shouldn't become one for the user.
        return None

    if esito.returncode != 0:
        return None
    _data_pubblicata = esito.stdout.strip() or None
    return _data_pubblicata
