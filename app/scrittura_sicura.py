"""Scrivere un file senza poterlo trovare a metà, nemmeno dopo un black-out.

Il programma ha cinque memorie che il ricalcolo settimanale non butta via, e
tutte e cinque sono file JSON scritti con lo stesso schema: si scrive un
temporaneo, poi `os.replace`.  Lo schema era in quattro copie —
`server.atomic_json`, `pipeline_jobs.scrivi_json`, `order_history.save_history`,
`ai_client._salva_memoria` — e nessuna delle quattro forzava i byte sul disco
prima di sostituire.

⚠ **`os.replace` è atomico rispetto ai metadati, non rispetto ai dati.**
Garantisce che nessuno veda il file a metà fra il vecchio e il nuovo; non
garantisce che i byte del nuovo siano già sul disco.  Se il computer del
negozio va giù per una mancanza di corrente nell'istante sbagliato, al riavvio
si può trovare uno `state.json` o un `orders.json` **presente ma vuoto o
tronco** — e da quando `app/data/` è ignorato per intero da git, dentro il
repository di quelle memorie non resta niente.

`conferme.db` non passa di qui e non è una dimenticanza: SQLite in WAL ha il
suo `synchronous`, tenuto al valore predefinito apposta.

⚠ **In binario e a fine riga LF**, come tutto il resto del progetto: su Windows
`write_text` trasformerebbe ogni `\n` in `\r\n`, e ci sono cinque collaudi che
pretendono il contrario sui file che il programma si riscrive.

⚠ **Quanto costa un `fsync`, e come si riapre la decisione.** Su un disco lento
— o su una cartella sincronizzata da OneDrive — può costare decine di
millisecondi, e la pagina si autosalva 450 ms dopo ogni modifica.  Misurato sul
Mac: nessuna differenza apprezzabile su un file da 155 KB, **ma quel numero non
vale per Windows**, perché su macOS `os.fsync` non è un flush completo del
disco (quello è `F_FULLFSYNC`) mentre su Windows è `FlushFileBuffers`, che lo è.
La scelta di tenerlo acceso ovunque poggia quindi su un altro conto, che vale su
tutt'e due i sistemi: un salvataggio della pagina sul confronto vero costa già
123 ms misurati, non blocca niente di quello che l'utente sta facendo, e il
peggio che un `FlushFileBuffers` aggiunge su un disco a piatti è dello stesso
ordine.  Se in negozio il salvataggio diventasse visibilmente più lento, la
strada è `forza_su_disco=False` sul solo `save_state` — l'autosalvataggio è
l'unica scrittura frequente — e non spegnerlo dappertutto.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


def scrivi_bytes(percorso: Path, contenuto: bytes, *, forza_su_disco: bool = True) -> None:
    """Scrive `contenuto` in `percorso`, o non lo scrive affatto.

    Il temporaneo porta processo e filo: con un nome fisso due scritture in
    volo sullo stesso file si contendono lo stesso temporaneo, la prima
    `os.replace` pubblica il contenuto della seconda e la seconda trova il
    temporaneo già sparito.  Su `state.json` succede per davvero — lo scrivono
    la pagina e la catena — ed è il difetto chiuso il 20 agosto 2026.
    """

    percorso = Path(percorso)
    percorso.parent.mkdir(parents=True, exist_ok=True)
    temporaneo = percorso.with_name(f"{percorso.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(temporaneo, "wb") as flusso:
            flusso.write(contenuto)
            if forza_su_disco:
                flusso.flush()
                os.fsync(flusso.fileno())
        os.replace(temporaneo, percorso)
    except Exception:
        # Un temporaneo lasciato in giro va tolto: chi scrive dopo non lo
        # riuserà — processo e filo sono altri — quindi resterebbe lì.
        # ⚠ «Non si ripete mai» sarebbe falso e non è la ragione: il sistema
        # ricicla sia i PID sia gli identificativi dei fili. Quello che conta è
        # un'altra cosa, ed è vera: due scritture **contemporanee** hanno per
        # forza coppie diverse, e quelle non contemporanee non si incrociano.
        # Quello che nessuno può togliere è il temporaneo di un processo morto
        # di morte violenta — mancanza di corrente, `taskkill` — e resta lì
        # senza far danno: `consegna.e_documento` non lo conta fra i documenti
        # e `inspect_sources` filtra per estensione.
        try:
            temporaneo.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def scrivi_json(
    percorso: Path,
    valore: Any,
    *,
    forza_su_disco: bool = True,
    ordina_le_chiavi: bool = False,
    a_capo_finale: bool = False,
) -> None:
    """Lo stesso, per un documento JSON. `indent=2` come tutti gli altri.

    I due interruttori esistono perché i quattro chiamanti scrivevano documenti
    leggermente diversi, e cambiarglieli sotto sarebbe stata una modifica che
    nessuno ha chiesto: `ai_client` ordina le chiavi (le sue impronte si
    confrontano fra una settimana e l'altra) e chiude con un a capo, la catena
    chiude con un a capo, il servizio no.
    """

    testo = json.dumps(valore, ensure_ascii=False, indent=2, sort_keys=ordina_le_chiavi)
    if a_capo_finale:
        testo += "\n"
    scrivi_bytes(percorso, testo.encode("utf-8"), forza_su_disco=forza_su_disco)
