"""Write files so a reader never finds one half-written, even after a crash.

The program keeps five pieces of state across the weekly recompute, all JSON
files written the same way: write a temp file, then `os.replace`. This
module centralizes that pattern for `server.atomic_json`,
`pipeline_jobs.scrivi_json`, `order_history.save_history` and
`ai_client._salva_memoria`, syncing bytes to disk before replacing.

`os.replace` is atomic for metadata, not for data: it guarantees no reader
ever sees a mix of old and new content, not that the new bytes are already on
disk. On power loss at the wrong instant, `state.json` or `orders.json` can
come back present but empty or truncated — and since `app/data/` is entirely
gitignored, the repository keeps no copy of that state to recover from.

`conferme.db` isn't written through this module, on purpose: it's SQLite in
WAL mode, which has its own `synchronous` setting, left at its default.

Always writes binary with LF endings, like the rest of the project:
`write_text` on Windows would turn every `\n` into `\r\n`, and several tests
expect LF on the files the program rewrites.

Cost of `fsync`: on a slow disk, or a folder synced by OneDrive, it can add
tens of milliseconds, and the page autosaves 450 ms after every change.
Measured on macOS: no noticeable difference on a 155 KB file — though that
number doesn't transfer to Windows, since `os.fsync` on macOS isn't a full
disk flush (that's `F_FULLFSYNC`) while on Windows it is `FlushFileBuffers`.
The actual case for keeping it on everywhere holds on both platforms: saving
the real comparison page already costs 123 ms measured and doesn't block the
UI, and whatever a `FlushFileBuffers` adds on a spinning disk is the same
order of magnitude. If saving ever becomes visibly slower in practice, the
fix is `forza_su_disco=False` on `save_state` alone — the autosave is the
only frequent write — not disabling it everywhere.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


def scrivi_bytes(percorso: Path, contenuto: bytes, *, forza_su_disco: bool = True) -> None:
    """Write `contenuto` to `percorso` atomically, or not at all.

    The temp filename carries both the pid and thread id: with a fixed name,
    two writes in flight to the same file would race on the same temp file —
    the first `os.replace` would publish the second write's content, and the
    second would find its temp file already gone. This is a real race on
    `state.json`, written by both the page and the pipeline.
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
        # Clean up a leftover temp file: the next writer won't reuse it
        # (different pid/thread), so it would otherwise stay orphaned. The
        # pid/thread pair isn't guaranteed unique forever — both get
        # recycled — but two *concurrent* writes always get different pairs,
        # and non-concurrent ones don't collide. What this can't remove is
        # the temp file of a process killed outright (power loss,
        # `taskkill`); that's harmless left behind: `consegna.e_documento`
        # doesn't count it among documents, and `inspect_sources` filters by
        # extension.
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
    """Same as `scrivi_bytes`, for a JSON document. `indent=2` like everywhere else.

    The two flags exist because the four original callers wrote slightly
    different documents, and normalizing that on the way in wasn't asked for:
    `ai_client` sorts keys (its fingerprints get compared week over week) and
    adds a trailing newline, the pipeline adds a trailing newline too, the
    web service doesn't.
    """

    testo = json.dumps(valore, ensure_ascii=False, indent=2, sort_keys=ordina_le_chiavi)
    if a_capo_finale:
        testo += "\n"
    scrivi_bytes(percorso, testo.encode("utf-8"), forza_su_disco=forza_su_disco)
