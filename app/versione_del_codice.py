#!/usr/bin/env python3
"""Che cosa c'era sul disco quando il programma è partito.

**Perché esiste.** Il 14 agosto 2026 il server è stato avviato alle 09:42 dal
commit `33827ee`; alle 16:33 e alle 18:24 il codice sul disco è cambiato due
volte. Alle 20:18 l'utente ha premuto «Rifai il confronto» e la catena si è
fermata con questa frase:

    AttributeError: module 'registro' has no attribute 'nome_del_fornitore'

Non era un difetto del codice — sul disco quella funzione c'era ed era giusta.
Era un processo **ibrido**: Python carica ogni modulo una volta sola, quindi
`registro`, importato all'avvio, era rimasto quello delle 09:42 (dove
`nome_del_fornitore` non esisteva ancora), mentre `launcher` — che
`pipeline_jobs` importa **dentro la funzione**, e che in quel processo nessuno
aveva mai importato — è stato letto dal disco alle 20:19, nella versione delle
18:09, che quella funzione la chiama. Metà vecchio e metà nuovo nello stesso
processo.

Il difetto vero non è l'incidente: è che l'utente si è trovato davanti a una
riga di Python in inglese, senza **una sola cosa da fare** per uscirne. Da qui
la frase di questo modulo: la causa in italiano e il rimedio, che è alla sua
portata e non richiede nessun intervento sul codice.

⚠ Le impronte si prendono **all'import**, che avviene all'avvio del server
(`server.py` importa `pipeline_jobs` in cima, che importa questo). Un import
tardivo fotograferebbe il disco di mezz'ora dopo e non vedrebbe niente.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
SKILL_ROOT = APP_DIR.parent
# Solo il codice Python che il processo tiene in memoria. `static/` no: la
# pagina e il suo JavaScript il browser se li rilegge a ogni caricamento, e un
# server vecchio serve comunque il file nuovo. `references/` e `app/data/` sono
# dati, e cambiano di mestiere: segnalarli sarebbe rumore a ogni run.
CARTELLE_DEL_CODICE = (APP_DIR, SKILL_ROOT / "scripts")
# `tests/` non è sotto queste cartelle, quindi non serve escluderlo; `vendor` e
# `__pycache__` sì, il primo perché è libreria di terzi che nessuno modifica a
# caldo, il secondo perché è generato e cambia da sé.
CARTELLE_ESCLUSE = frozenset({"__pycache__", "vendor", "node_modules", "data"})

FRASE_DEL_RIMEDIO = (
    "Il programma è stato aggiornato dopo che l'hai aperto: in memoria sta "
    "ancora girando la versione caricata all'avvio, e le due metà non vanno "
    "d'accordo. Chiudi il comparatore e riaprilo con AVVIA_COMPARATORE.cmd, "
    "poi rifai il confronto. Non perdi niente: i listini caricati, il confronto "
    "attivo e le tue scelte restano dove sono."
)


def _impronta(percorso: Path) -> str | None:
    """L'impronta del file, `None` se in questo momento non si legge.

    Un file illeggibile non è una prova di niente — su Windows capita che un
    editor o un antivirus lo tengano per un istante — e questa funzione decide
    se una run parte: un falso positivo qui fermerebbe un ricalcolo buono.
    """

    try:
        return hashlib.sha256(percorso.read_bytes()).hexdigest()
    except OSError:
        return None


def _e_da_guardare(percorso: Path, radice: Path = SKILL_ROOT) -> bool:
    """Se questo file conta come «codice del programma».

    Oggi in `vendor`, in `__pycache__` e in `app/data` non c'è un solo `.py`,
    quindi il filtro non toglie niente: esiste per il giorno in cui ci sarà —
    una libreria copiata dentro il progetto si aggiorna, e non è il programma
    che è cambiato sotto l'utente.
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
    """Nome relativo → impronta, per ogni sorgente Python del programma."""

    return {
        str(percorso.relative_to(SKILL_ROOT)).replace("\\", "/"): _impronta(percorso)
        for percorso in _file_del_codice()
    }


ALL_AVVIO: dict[str, str | None] = fotografia()


def file_cambiati(riferimento: dict[str, str | None] | None = None) -> list[str]:
    """I file di codice diversi da com'erano all'avvio.

    Un file **comparso** o **sparito** conta: sono cambiamenti veri. Un file
    che non si è riusciti a leggere — né allora né adesso — **non conta**: non
    sapere non è la stessa cosa che sapere che è cambiato, ed è la regola che
    questo progetto ha già imparato altrove a sue spese.
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
    """La frase da mostrare, oppure `None` se il programma è quello dell'avvio."""

    cambiati = file_cambiati(riferimento)
    if not cambiati:
        return None
    elenco = ", ".join(cambiati[:5])
    if len(cambiati) > 5:
        elenco += f" e altri {len(cambiati) - 5}"
    return f"{FRASE_DEL_RIMEDIO} (file cambiati dopo l'avvio: {elenco})"


def firma(riferimento: dict[str, str | None] | None = None) -> str:
    """Le impronte di tutti i sorgenti ridotte a **una** stringa confrontabile.

    Serve a chi deve rispondere «è lo stesso programma?» senza spedirsi dietro
    l'elenco intero: il servizio dichiara la propria in `/api/health` e il
    lanciatore la confronta con quella dei file sul disco.

    Senza argomenti è la firma di **quello che sta girando** (`ALL_AVVIO`, la
    fotografia presa all'import); con `fotografia()` è quella del disco adesso.

    Un file che non si è riusciti a leggere resta distinto sia da un file
    assente sia da uno leggibile: due programmi diversi non devono poter
    produrre la stessa firma per un errore di lettura.  Per questo la riga porta
    **prima** se l'impronta c'è (`1`/`0`) e poi il valore, invece di un
    segnaposto tipo `?`: un segnaposto è un valore come gli altri, e un file il
    cui contenuto desse proprio quella stringa collasserebbe sull'illeggibile.
    """

    fonte = ALL_AVVIO if riferimento is None else riferimento
    materiale = "\n".join(
        f"{nome}\t{'1' if impronta is not None else '0'}\t{impronta or ''}"
        for nome, impronta in sorted(fonte.items())
    )
    return hashlib.sha256(materiale.encode("utf-8")).hexdigest()


def firma_del_disco() -> str:
    """La firma dei sorgenti **come sono adesso**, riletti dal disco."""

    return firma(fotografia())


def stato() -> dict[str, Any]:
    """Il riassunto per chi lo vuole in una risposta JSON."""

    cambiati = file_cambiati()
    return {
        "codiceCambiatoDopoLAvvio": bool(cambiati),
        "fileCambiati": cambiati,
        "messaggio": avviso_del_codice_cambiato() or "",
    }


# ---------------------------------------------------------------------------
# Quale versione pubblicata sta girando.
# ---------------------------------------------------------------------------
# Letta una volta sola: l'allineamento a GitHub avviene PRIMA che il server
# parta, quindi la risposta non cambia finche' il programma e' acceso, e non ha
# senso pagare un processo `git` a ogni richiesta.
_data_pubblicata: str | None = None
_data_gia_cercata = False

# Sul PC del negozio il PATH non e' affidabile: anche `python` nudo li' e' un
# segnaposto rotto del Microsoft Store.
# `AVVIA_COMPARATORE.ps1:49` per questo non cerca "git" nudo: prova prima
# questo percorso assoluto, e ripiega su `Get-Command` solo se manca.
_GIT_ASSOLUTO_WINDOWS = Path(r"C:\Program Files\Git\cmd\git.exe")


def _eseguibile_git() -> str:
    """Quale `git` lanciare: stesso criterio di `AVVIA_COMPARATORE.ps1`.

    Prima il percorso assoluto dell'installazione standard su Windows, poi
    `shutil.which("git")`, che risolve il PATH una volta sola e restituisce
    un percorso assoluto (non il nome nudo). Se nessuno dei due esiste si
    ripiega comunque sulla stringa "git": e' lo stesso identico comando che
    girava prima di questa funzione, quindi il ripiego non puo' rendere le
    cose peggiori di oggi — solleva lo stesso `OSError` e `pubblicata()`
    torna `None` come faceva gia'.
    """

    if _GIT_ASSOLUTO_WINDOWS.is_file():
        return str(_GIT_ASSOLUTO_WINDOWS)
    return shutil.which("git") or "git"


def pubblicata() -> str | None:
    """La data del commit che questo disco sta eseguendo, in ISO 8601.

    **Perche' esiste.** Su questo PC il programma si allinea a GitHub da solo a
    ogni avvio, e quando l'allineamento non riesce — credenziali scadute, rete
    che non risponde — per scelta NON blocca l'avvio: lo scrive in una riga di
    console e parte con quello che ha. Quella riga non la legge nessuno. Senza
    una data in pagina, un programma fermo da mesi e uno aggiornato stamattina
    si somigliano: l'unico modo di accorgersene e' che manchi una funzione che
    era stata chiesta, e a quel punto e' tardi.

    Torna `None` se git non c'e' o questa cartella non e' un repository. La
    pagina in quel caso dice che non lo sa — che e' gia' un'informazione utile,
    diversa dal tacere.
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
        # Niente git, o non risponde: si va avanti senza. Non e' un errore del
        # programma e non deve diventarlo per chi lo usa.
        return None

    if esito.returncode != 0:
        return None
    _data_pubblicata = esito.stdout.strip() or None
    return _data_pubblicata
