#!/usr/bin/env python3
"""Store of the user's confirmations: what was answered, and when.

The management-software export is a new file every week, and a product's
identifier on the page is its row number (`product:539`). Row numbers are
not stable across exports: the same identifier can point at a different item
next week. A confirmation keyed on the row number would not just get lost —
it would silently reapply to merchandise the user never actually reviewed.

A confirmation is keyed on what the item is, not on where it is written:

* the management-software item identity is `impronta_prodotto` — barcode
  and name stripped to the essentials, no row and no price;
* the supplier item identity is `impronta_articolo` from
  `scripts/build_review_data.py`, reused rather than redefined here: two
  definitions of the same identity would be two sources of truth for the same
  data, and the day one of them changes the store stops matching silently.

This module makes no business decisions. It doesn't know what a good deal
is, doesn't apply any confirmation and doesn't expire anything on its own: it
remembers what it's told and answers what it's asked. All commercial rules
live outside this module.

A confirmation that never expires is a mistake that repeats forever: if the
user confirms a wrong match, it would resurface every week unreviewed. So every
row carries when, which exact offer, and why; nothing is ever
truly deleted (`dimentica` closes a row, it doesn't drop it), and `esporta`
returns everything in the open — a `.db` file isn't human-readable, and the
user needs to be able to see what was confirmed.

The caller chooses the file path; nothing is hardcoded here.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

# `normalized_name` and `impronta_articolo` come from the module that already
# owns them: duplicating them here would mean two identity rules to keep in
# sync. Same import path `app/server.py` already uses.
_CARTELLA_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_CARTELLA_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CARTELLA_SCRIPTS))

from build_review_data import impronta_articolo, normalized_name  # noqa: E402,F401

__all__ = [
    "MagazzinoConferme",
    "MagazzinoNonUtilizzabile",
    "codice_confrontabile",
    "impronta_articolo",
    "impronta_prodotto",
    "VERSIONE_SCHEMA",
]

# 2: adds the barcode-equality tables. Bumped for any schema change, including
# an additive one: `_prepara` rejects a file written by a newer schema rather
# than guessing which changes are safe to skip. Downgrading means reopening
# the file with the version that wrote it.
VERSIONE_SCHEMA = 2

# How long one write waits for another to finish before the file counts as
# busy. The service is multi-threaded (the page poller and the pipeline
# thread write from the same process, and nothing rules out a second
# process), and without this wait the second write would fail with
# "database is locked" instead of queuing for a few milliseconds.
ATTESA_BLOCCO_S = 10.0


class MagazzinoNonUtilizzabile(RuntimeError):
    """The confirmations file can't be opened, or has become unreadable.

    A single exception to catch, deliberately not a silent fallback:
    answering "no confirmation" for an unreadable file would resurface every
    question without explanation, and the user would reconfirm by hand
    believing the program never knew anything. The caller must catch this
    at open time, in one place, and decide there whether to proceed
    without memory and say so in the UI — that decision belongs to the
    caller, not to this module.
    """


def impronta_prodotto(prodotto: Any) -> str:
    """Identity fingerprint of the management-software item, not its row.

    Same criterion as `impronta_articolo` on the supplier side: identity and
    nothing else — barcode and normalized name. Deliberately excluded:

    * the row number, the reason this module exists (row numbers are not
      stable across weekly exports, see the module docstring);
    * price and suggested quantity, which change weekly on the same item:
      including them would mean re-asking the confirmation on every price-list
      update, i.e. training the user to click through without reading.

    The barcode alone isn't enough, hence the name. In this export EANs are
    sometimes truncated to eight digits and hand-typed: a barcode mistyped
    onto the wrong item would silently attach yesterday's confirmation to
    different merchandise, which is the one failure mode this module must
    rule out. With the name included, that case expires the confirmation
    instead — losing a confirmation costs a click, applying a wrong one costs
    an order.

    Returns `""` when there is neither a code nor a name: an item that can't
    be identified can't be remembered (see `MagazzinoConferme.ricorda`).

    Note for callers: two rows in the same export with the same code and name
    give the same fingerprint on purpose — they are the same item, and a
    confirmation on one applies to the other. The row-level suffix that tells
    the two rows apart on the page is the page's concern, not this module's.
    """

    if not isinstance(prodotto, dict):
        return ""
    ean = str(prodotto.get("ean") or prodotto.get("gtin") or prodotto.get("barcode") or "").strip()
    nome = normalized_name(prodotto.get("description") or prodotto.get("name"))
    if not ean and not nome:
        return ""
    return f"{ean}|{nome}"


def codice_confrontabile(valore: Any) -> str:
    """Reduce a barcode to what actually gets compared: digits only.

    In the management-software export EANs are hand-typed and arrive in
    several shapes: `'4009428623194'`, `4009428623194.0` when the sheet read
    them as numbers, with leading spaces, with a leading apostrophe. Two
    spellings of the same code must produce the same key, or an equality
    declared one week won't be found the next — without raising any error.

    Length is not normalized: an EAN-8 and an EAN-13 remain different codes,
    which is correct — whether they're the same item is exactly what an
    equality declaration (see `unisci`) is for.
    """

    testo = str(valore or "").strip()
    if testo.endswith(".0") and testo[:-2].isdigit():
        testo = testo[:-2]
    return "".join(carattere for carattere in testo if carattere.isdigit())


def _identifica_qualcosa(impronta: str) -> bool:
    """A fingerprint made only of separators identifies nothing.

    `impronta_articolo({})` returns `"|||"`, which is a string and looks like
    a fingerprint; storing it would attach the confirmation to the next
    equally empty row. When the fingerprint has several parts, the first
    alone doesn't count: `"noce|||"` is a supplier, not an item.
    """

    pezzi = impronta.split("|")
    if len(pezzi) > 1:
        return any(pezzo.strip() for pezzo in pezzi[1:])
    return bool(impronta.strip())


class MagazzinoConferme:
    """The user's confirmations, in a SQLite file the caller chooses.

    A single table, never rewritten: append a row, close the previous
    one. The alternative — one table for current confirmations, one for
    history — was rejected because the same fact would live in two places:
    every `ricorda` would have to update both, and the day one path is missed
    the store answers "what did you decide before" from a table that row
    never reached. Here the rows are the history: `valida_dal` is when
    the answer took effect, `valida_fino_a` when it was replaced or removed,
    and `NULL` means "in effect now". No `UPDATE` ever removes content; the
    only one there is sets `valida_fino_a`, i.e. it adds a fact.

    Two indexes, each protecting one thing:

    * `conferme_in_vigore`, a unique partial index on `(fornitore, articolo)`
      where `valida_fino_a IS NULL`: both the lookup path for `cerca` and the
      invariant "at most one active confirmation per pair", enforced by the
      database rather than by this code. If a transaction here ever goes
      wrong, the file doesn't end up with two simultaneous answers — the
      write fails instead.
    * `conferme_storia` on `(fornitore, articolo, valida_dal)`: answers "what
      did I decide before, and when" in time order.

    Concurrency: the service is multi-threaded (the page poller and the
    pipeline thread). One connection, serialized by an internal lock;
    `PRAGMA journal_mode=WAL` plus the busy-timeout handle the rest — a
    second store opened on the same file from another thread or process.
    Every write opens its transaction with `BEGIN IMMEDIATE`, taking the
    lock up front instead of discovering at `COMMIT` that another write got
    there first, which is the usual way two SQLite writers deadlock each
    other with no way to retry.
    """

    def __init__(self, percorso: Path) -> None:
        self.percorso = Path(percorso)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        # Which journal mode the file actually accepted. On a network drive
        # WAL can silently fail to activate and SQLite stays in `delete`
        # mode: the store keeps working, but anyone debugging a slowdown or
        # a lock needs to be able to read this instead of guessing.
        self.giornale = ""
        try:
            self.percorso.parent.mkdir(parents=True, exist_ok=True)
        except OSError as errore:
            raise MagazzinoNonUtilizzabile(
                f"Non riesco a creare la cartella delle conferme {self.percorso.parent}: {errore}"
            ) from errore
        try:
            self._conn = sqlite3.connect(
                str(self.percorso),
                timeout=ATTESA_BLOCCO_S,
                isolation_level=None,  # transactions are opened explicitly, with BEGIN IMMEDIATE
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self.giornale = str(self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            # `timeout=` above is the busy-wait; no separate `PRAGMA
            # busy_timeout` is needed on top of it.
            # `synchronous` is left at SQLite's default on purpose: only a
            # few rows are written per week (the user's answers), so trading
            # durability for a few saved milliseconds isn't worth it.
            self._prepara()
        except sqlite3.Error as errore:
            self._chiudi_di_forza()
            raise MagazzinoNonUtilizzabile(
                f"Il file delle conferme non si apre: {self.percorso} — {errore}. "
                "Se il file è danneggiato, rinominarlo lo fa ripartire vuoto: si perdono "
                "le conferme già date, non il resto del programma."
            ) from errore

    # ------------------------------------------------------------------ interno

    def _prepara(self) -> None:
        conn = self._connessione()
        versione = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if versione > VERSIONE_SCHEMA:
            raise MagazzinoNonUtilizzabile(
                f"Il file delle conferme {self.percorso} è stato scritto da una versione più "
                f"recente del programma (schema {versione}, qui si conosce il {VERSIONE_SCHEMA}). "
                "Non lo tocco: riaprirlo con la versione che l'ha scritto, oppure metterlo da parte."
            )
        with self._transazione():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conferme (
                    id INTEGER PRIMARY KEY,
                    fornitore TEXT NOT NULL,
                    articolo TEXT NOT NULL,
                    offerta TEXT NOT NULL,
                    accettata INTEGER NOT NULL,
                    motivo TEXT NOT NULL DEFAULT '',
                    valida_dal TEXT NOT NULL,
                    valida_fino_a TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS conferme_in_vigore
                ON conferme (fornitore, articolo) WHERE valida_fino_a IS NULL
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS conferme_storia
                ON conferme (fornitore, articolo, valida_dal)
                """
            )
            # Barcode equalities. Same discipline as the table above — append
            # and close, nothing ever deleted — because the risk is bigger
            # here: a wrong confirmation affects one item at one supplier, a
            # wrong equality affects every supplier, every week. `articolo`
            # and `offerta` are the two fingerprints the declaration was made
            # on; they play no part in the lookup, they record what evidence
            # led someone to declare the two codes the same item.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS uguaglianze (
                    id INTEGER PRIMARY KEY,
                    codice_a TEXT NOT NULL,
                    codice_b TEXT NOT NULL,
                    articolo TEXT NOT NULL DEFAULT '',
                    offerta TEXT NOT NULL DEFAULT '',
                    motivo TEXT NOT NULL DEFAULT '',
                    valida_dal TEXT NOT NULL,
                    valida_fino_a TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uguaglianze_in_vigore
                ON uguaglianze (codice_a, codice_b) WHERE valida_fino_a IS NULL
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS uguaglianze_storia
                ON uguaglianze (codice_a, codice_b, valida_dal)
                """
            )
            conn.execute(f"PRAGMA user_version={VERSIONE_SCHEMA}")

    def _connessione(self) -> sqlite3.Connection:
        if self._conn is None:
            raise MagazzinoNonUtilizzabile(
                f"Il magazzino delle conferme {self.percorso} è già stato chiuso: "
                "chi lo usa dopo `chiudi()` sta lavorando su un oggetto morto."
            )
        return self._conn

    def _chiudi_di_forza(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - closing must never raise
                pass
            self._conn = None

    class _Transazione:
        """`BEGIN IMMEDIATE` ... `COMMIT`, or `ROLLBACK` if something goes wrong."""

        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def __enter__(self) -> sqlite3.Connection:
            self._conn.execute("BEGIN IMMEDIATE")
            return self._conn

        def __exit__(self, tipo: Any, valore: Any, traccia: Any) -> bool:
            if tipo is None:
                self._conn.execute("COMMIT")
            else:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover
                    pass
            return False

    def _transazione(self) -> "MagazzinoConferme._Transazione":
        return MagazzinoConferme._Transazione(self._connessione())

    @staticmethod
    def _chiave_fornitore(fornitore: str) -> str:
        """Suppliers compare case-insensitively.

        `NOCE` and `noce` are the same price list; writing one and looking up
        the other would silently drop the confirmation. The offer fingerprint,
        by contrast, is kept as-is: it's compared against one computed the
        same way, and changing its case would make it incomparable.
        """

        return str(fornitore or "").strip().casefold()

    @staticmethod
    def _riga(riga: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(riga["id"]),
            "fornitore": riga["fornitore"],
            "articolo": riga["articolo"],
            "offerta": riga["offerta"],
            "accettata": bool(riga["accettata"]),
            "motivo": riga["motivo"],
            "valida_dal": riga["valida_dal"],
            "valida_fino_a": riga["valida_fino_a"],
            "in_vigore": riga["valida_fino_a"] is None,
        }

    def _esegui(self, sql: str, parametri: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            return list(self._connessione().execute(sql, parametri))
        except sqlite3.Error as errore:
            raise MagazzinoNonUtilizzabile(
                f"Il file delle conferme {self.percorso} non si legge più: {errore}"
            ) from errore

    # ------------------------------------------------------------------ pubblico

    def ricorda(
        self,
        *,
        fornitore: str,
        articolo: str,
        offerta: str,
        accettata: bool,
        motivo: str = "",
        quando: str,
    ) -> None:
        """Record the user's answer for a (supplier, item) pair.

        Replaces the previous answer while keeping history: the active
        row is closed with `valida_fino_a = quando` and the new one starts at
        `valida_dal = quando`. Closing and opening at the same instant isn't
        incidental: a pair's history has no gaps and no overlaps, so "what was
        in effect on a given date" always has one answer.

        `accettata=False` is a memory like any other, and the one that saves
        the most repeated work: a "no, this isn't my item" only needs to be
        said once.

        `quando` is an ISO string supplied by the caller; this method
        never reads the clock itself, so a stored answer's timestamp can
        always be verified independently.

        Reconfirming an identical answer writes nothing — same offer,
        same decision, same reason — and `valida_dal` keeps its original
        value, since that's when the answer first took effect. This guards
        against the caller: the page autosaves every 450 ms, and without this
        check one confirmation would turn into hundreds of history rows for
        decisions never actually made.

        Raises `ValueError` for a fingerprint that identifies nothing: a
        confirmation keyed on `"|||"` would match the next equally empty row.
        """

        chiave_fornitore = self._chiave_fornitore(fornitore)
        articolo = str(articolo or "").strip()
        offerta = str(offerta or "").strip()
        quando = str(quando or "").strip()
        if not chiave_fornitore:
            raise ValueError("Una conferma senza fornitore non si può ritrovare: `fornitore` è vuoto.")
        if not _identifica_qualcosa(articolo):
            raise ValueError(
                f"L'articolo del gestionale non è identificabile ({articolo!r}): una conferma "
                "agganciata a un'impronta vuota tornerebbe su merce a caso."
            )
        if not _identifica_qualcosa(offerta):
            raise ValueError(
                f"L'articolo del fornitore non è identificabile ({offerta!r}): senza sapere "
                "quale riga è stata confermata, la conferma non è verificabile da nessuno."
            )
        if not quando:
            raise ValueError(
                "`quando` è vuoto: una conferma senza data non si può controllare a posteriori, "
                "ed è il minimo che serve per capire un ordine sbagliato."
            )
        motivo = str(motivo or "")

        try:
            with self._lock:
                conn = self._connessione()
                with self._transazione():
                    riga = conn.execute(
                        "SELECT offerta, accettata, motivo FROM conferme "
                        "WHERE fornitore=? AND articolo=? AND valida_fino_a IS NULL",
                        (chiave_fornitore, articolo),
                    ).fetchone()
                    if riga is not None and (
                        riga["offerta"] == offerta
                        and bool(riga["accettata"]) == bool(accettata)
                        and riga["motivo"] == motivo
                    ):
                        return
                    if riga is not None:
                        conn.execute(
                            "UPDATE conferme SET valida_fino_a=? "
                            "WHERE fornitore=? AND articolo=? AND valida_fino_a IS NULL",
                            (quando, chiave_fornitore, articolo),
                        )
                    conn.execute(
                        "INSERT INTO conferme (fornitore, articolo, offerta, accettata, motivo, "
                        "valida_dal, valida_fino_a) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        (chiave_fornitore, articolo, offerta, 1 if accettata else 0, motivo, quando),
                    )
        except sqlite3.Error as errore:
            raise MagazzinoNonUtilizzabile(
                f"La conferma non è stata scritta in {self.percorso}: {errore}. "
                "Chi ha risposto deve saperlo: una risposta che si crede salvata e non lo è "
                "tornerà a essere chiesta senza spiegazioni."
            ) from errore

    def cerca(self, fornitore: str, articolo: str) -> dict | None:
        """The active confirmation for that pair, or `None` if there isn't one.

        `None` means "no memory of this", not "no": the user's "no" is a row
        with `accettata` false, and the two lead to different screens.

        The confirmation is always returned, even when today's supplier item
        stops matching the confirmed one — the caller checks that by
        comparing `offerta` against the current row's fingerprint. This isn't
        an oversight: a supplier's price-list format or item codes can change
        between exports while the item itself stays the same. If `cerca`
        required today's fingerprint to match and answered "expired"
        otherwise, that kind of format change would wipe the memory for
        identical items — exactly the failure this store exists to prevent.

        By always returning the row, the caller can do the right thing in
        both cases: apply the confirmation when the fingerprint matches, or,
        when it doesn't, show the user both fingerprints and ask for a single
        re-confirmation instead of starting over. The store itself can't
        decide between "the price-list format changed" and "the supplier
        swapped the item underneath us" — that distinction needs the current
        week's data, which isn't available here.
        """

        chiave_fornitore = self._chiave_fornitore(fornitore)
        articolo = str(articolo or "").strip()
        if not chiave_fornitore or not articolo:
            return None
        with self._lock:
            righe = self._esegui(
                "SELECT * FROM conferme WHERE fornitore=? AND articolo=? AND valida_fino_a IS NULL",
                (chiave_fornitore, articolo),
            )
        return self._riga(righe[0]) if righe else None

    def dimentica(self, fornitore: str, articolo: str, *, quando: str = "") -> bool:
        """Remove the active confirmation. `True` if there was one.

        This is the only way out of a wrong confirmation, and it needs to be
        cheap: until it's called, that confirmation keeps applying every week.

        The row is closed, not deleted. If a wrong confirmation already
        produced a wrong order, deleting the row would remove the only trace
        that explains it; `esporta` keeps showing it, with its date and reason.

        `quando` is optional so that `dimentica(fornitore, articolo)` keeps
        working as-is, but callers that have a timestamp should pass it:
        without it the closed row has no record of when it was removed
        (`valida_fino_a` empty). The clock isn't read here, for the same
        reason as in `ricorda`.
        """

        chiave_fornitore = self._chiave_fornitore(fornitore)
        articolo = str(articolo or "").strip()
        if not chiave_fornitore or not articolo:
            return False
        try:
            with self._lock:
                conn = self._connessione()
                with self._transazione():
                    cursore = conn.execute(
                        "UPDATE conferme SET valida_fino_a=? "
                        "WHERE fornitore=? AND articolo=? AND valida_fino_a IS NULL",
                        (str(quando or "").strip(), chiave_fornitore, articolo),
                    )
                    return cursore.rowcount > 0
        except sqlite3.Error as errore:
            raise MagazzinoNonUtilizzabile(
                f"La conferma non è stata tolta da {self.percorso}: {errore}. "
                "Chi l'ha tolta deve saperlo: resterebbe in vigore la settimana prossima."
            ) from errore

    def tutte(self) -> list[dict]:
        """Confirmations active now, ordered by supplier then item.

        What the weekly comparison and the page that lists them (to remove
        them) need. No history here — that's `esporta`.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM conferme WHERE valida_fino_a IS NULL "
                "ORDER BY fornitore, articolo"
            )
        return [self._riga(riga) for riga in righe]

    def rifiuti(self) -> list[dict]:
        """Active "no" answers: "this row from this supplier is not my item".

        The other half of `tutte()`. Kept here rather than duplicated by the
        caller because the rule that tells a "no" apart — a row with
        `accettata` false — belongs to this store.

        Same ordering as `tutte()`, so the two can be compared side by side.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM conferme WHERE valida_fino_a IS NULL AND accettata=0 "
                "ORDER BY fornitore, articolo"
            )
        return [self._riga(riga) for riga in righe]

    def esporta(self) -> list[dict]:
        """Everything the store knows, history included, as JSON.

        A `.db` file isn't human-readable; without this, "what did I
        confirm" would need a tool the user doesn't have. Closed rows —
        replaced or removed — are included too, since they're the only
        answer to "what did I decide before, and when", and a confirmation
        that produced a wrong order needs to be findable after it's fixed.

        Ordering is stable and values are plain JSON types (strings, bools,
        ints, `None`): two exports of the same content produce the same
        document, so they can be diffed week to week.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM conferme ORDER BY fornitore, articolo, valida_dal, id"
            )
        return [self._riga(riga) for riga in righe]

    # -------------------------------------------------- uguaglianze fra codici

    @staticmethod
    def _coppia(codice_a: Any, codice_b: Any) -> tuple[str, str]:
        """The two codes in a fixed order: equality has no direction.

        Without this ordering, `unisci(A, B)` and `unisci(B, A)` would be two
        different rows, the unique index wouldn't catch the second one, and
        `separa(B, A)` wouldn't find the row stored as `(A, B)` — an equality
        that can't be removed.
        """

        primo = codice_confrontabile(codice_a)
        secondo = codice_confrontabile(codice_b)
        return (primo, secondo) if primo <= secondo else (secondo, primo)

    @staticmethod
    def _riga_uguaglianza(riga: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(riga["id"]),
            "codici": [riga["codice_a"], riga["codice_b"]],
            "articolo": riga["articolo"],
            "offerta": riga["offerta"],
            "motivo": riga["motivo"],
            "valida_dal": riga["valida_dal"],
            "valida_fino_a": riga["valida_fino_a"],
            "in_vigore": riga["valida_fino_a"] is None,
        }

    def unisci(
        self,
        codice_a: Any,
        codice_b: Any,
        *,
        articolo: str = "",
        offerta: str = "",
        motivo: str = "",
        quando: str,
    ) -> bool:
        """Declare that two barcodes are the same item.

        Returns `True` if it wrote a row, `False` if that equality was
        already active — redeclaring it leaves `valida_dal` untouched, since
        that's when it first took effect. Guards against the caller, as in
        `ricorda`.

        This stores one edge of the graph: that A and B are the same
        item. That C, already equal to B, is therefore equal to A too is
        derived by `classe`, not stored anywhere. This is deliberate: the
        stored rows are the human declarations, and only those — otherwise
        removing A≡B would leave an A≡C nobody actually declared, with no
        record of where it came from.

        Rejects two equal codes (nothing to declare) and an empty code
        (identifies nothing).
        """

        primo, secondo = self._coppia(codice_a, codice_b)
        quando = str(quando or "").strip()
        if not primo or not secondo:
            raise ValueError(
                "Un'uguaglianza vuole due codici a barre: senza, non si può ritrovare "
                "e non si può togliere."
            )
        if primo == secondo:
            raise ValueError(f"«{primo}» è uguale a se stesso: non c'è niente da dichiarare.")
        if not quando:
            raise ValueError(
                "`quando` è vuoto: un'uguaglianza senza data non si può controllare a "
                "posteriori, e questa entra in ogni confronto futuro."
            )
        try:
            with self._lock:
                conn = self._connessione()
                with self._transazione():
                    gia = conn.execute(
                        "SELECT id FROM uguaglianze WHERE codice_a=? AND codice_b=? "
                        "AND valida_fino_a IS NULL",
                        (primo, secondo),
                    ).fetchone()
                    if gia is not None:
                        return False
                    conn.execute(
                        "INSERT INTO uguaglianze (codice_a, codice_b, articolo, offerta, motivo, "
                        "valida_dal, valida_fino_a) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        (primo, secondo, str(articolo or ""), str(offerta or ""), str(motivo or ""), quando),
                    )
                    return True
        except sqlite3.Error as errore:
            raise MagazzinoNonUtilizzabile(
                f"L'uguaglianza non è stata scritta in {self.percorso}: {errore}. "
                "Chi l'ha dichiarata deve saperlo: al prossimo confronto quei due codici "
                "resterebbero due articoli diversi."
            ) from errore

    def separa(self, codice_a: Any, codice_b: Any, *, quando: str = "") -> bool:
        """Remove an equality. `True` if one was active.

        The only way out, and it needs to be cheap: until it's called, the
        declaration applies to every future comparison and every
        supplier. The row is closed, not deleted, for the same reason as in
        `dimentica`.
        """

        primo, secondo = self._coppia(codice_a, codice_b)
        if not primo or not secondo:
            return False
        try:
            with self._lock:
                conn = self._connessione()
                with self._transazione():
                    cursore = conn.execute(
                        "UPDATE uguaglianze SET valida_fino_a=? "
                        "WHERE codice_a=? AND codice_b=? AND valida_fino_a IS NULL",
                        (str(quando or "").strip(), primo, secondo),
                    )
                    return cursore.rowcount > 0
        except sqlite3.Error as errore:
            raise MagazzinoNonUtilizzabile(
                f"L'uguaglianza non è stata tolta da {self.percorso}: {errore}. "
                "Chi l'ha tolta deve saperlo: varrebbe ancora al prossimo confronto."
            ) from errore

    def uguaglianze(self) -> list[dict]:
        """Declarations active now, one row per declared pair.

        What the page lists in order to remove them: pairs, not classes,
        since what gets removed is what someone actually declared.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM uguaglianze WHERE valida_fino_a IS NULL "
                "ORDER BY codice_a, codice_b"
            )
        return [self._riga_uguaglianza(riga) for riga in righe]

    def classi(self) -> list[list[str]]:
        """Groups of codes that count as the same item.

        The connected components of the declared-pairs graph: A≡B and B≡C
        form one group of three, and looking up A also finds C. Each group is
        sorted, and the groups themselves are sorted, so two reads of the same
        content produce the same document — this is what the pipeline
        consumes and diffs week to week.

        A group of one doesn't exist: a code with no equalities isn't a
        group, it's just itself, and including it would add a row for every
        item that says nothing.
        """

        vicini: dict[str, set[str]] = {}
        for riga in self.uguaglianze():
            primo, secondo = riga["codici"]
            vicini.setdefault(primo, set()).add(secondo)
            vicini.setdefault(secondo, set()).add(primo)
        gruppi: list[list[str]] = []
        visti: set[str] = set()
        for codice in sorted(vicini):
            if codice in visti:
                continue
            gruppo: set[str] = set()
            da_guardare = [codice]
            while da_guardare:
                corrente = da_guardare.pop()
                if corrente in gruppo:
                    continue
                gruppo.add(corrente)
                da_guardare.extend(vicini.get(corrente, ()))
            visti |= gruppo
            gruppi.append(sorted(gruppo))
        gruppi.sort()
        return gruppi

    def classe(self, codice: Any) -> list[str]:
        """All codes that count as this one, including this one.

        A code with no equalities returns itself: callers indexing by EAN
        don't need a special case for it. Returns an empty list only for an
        empty code.
        """

        chiave = codice_confrontabile(codice)
        if not chiave:
            return []
        for gruppo in self.classi():
            if chiave in gruppo:
                return gruppo
        return [chiave]

    def esporta_uguaglianze(self) -> list[dict]:
        """Everything the store knows about equalities, history included.

        Same reason as `esporta`: a `.db` file isn't human-readable, and a
        removed declaration needs to stay findable after it's fixed, since
        it's what explains earlier orders.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM uguaglianze ORDER BY codice_a, codice_b, valida_dal, id"
            )
        return [self._riga_uguaglianza(riga) for riga in righe]

    def chiudi(self) -> None:
        """Close the file. Safe to call twice."""

        with self._lock:
            self._chiudi_di_forza()
