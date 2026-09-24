#!/usr/bin/env python3
"""Products no supplier can provide: the list for calling around to source them.

A product the user wants to order that no supplier carries — neither on any
price list nor as an available offer — would otherwise leave the compilation
without a trace: its quantity zeroed, the row gone from the order, and no
record that it still needs to be bought somewhere else. This module writes
the sheet that stays in that person's hands: one row per product, and an
empty `Note` column they fill in while calling around.

Filtering which products belong on this list is not this module's job.
Whether an offer is usable is decided by `app/server.py`, which already owns
`find_offer` and `offer_is_available`; this module receives already-selected
rows and writes them out. It doesn't import the server — that would be a
circular dependency — and doesn't redefine that predicate, since two
definitions of "not available" are two truths about the same data.

The file is never read back. No code path in the app imports it, compares
against it, or carries it into the next compilation: it's a sheet for a
person, not an exchange format.

`openpyxl` is already a required dependency (`app/launcher.py:128-130` blocks
startup without it). Elsewhere in the app it's used only for reading; this
is the write path, kept minimal: no formulas, no elaborate styling, bold
headers and columns wide enough to read a description without resizing them
by hand.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

import consegna
# The status for an offer the user rejected is defined in `offerta.py`,
# which already owns "can this be ordered": this module reads that string
# rather than redefining it, so the two never drift apart and start
# disagreeing about the same row.
from offerta import STATO_RIFIUTATO_UTENTE


# The `tipo` this file is registered under in the folder inventory, the
# audit and the zip. Owned by `consegna`, the module that must recognize the
# file even when there's no audit; a second constant here would be a second
# source of truth that could drift out of sync without anything noticing.
TIPO = consegna.TIPO_DA_REPERIRE

# The only two possible sentences for the `Motivo` column. Two, not one,
# because they tell the caller different things: "not on anyone's price
# list" means look for a new supplier, "nobody has it available" means call
# the same ones back in a few days.
MOTIVO_NON_A_LISTINO = "Nessun fornitore lo ha a listino"
MOTIVO_NON_DISPONIBILE = "Nessun fornitore lo ha disponibile"
# A third reason, distinct from the other two: a supplier does carry a
# similar row, but the user rejected it as the wrong article. This changes
# the action for the caller — that supplier shouldn't be called back later,
# since they don't have the right item; it needs to be sourced elsewhere.
MOTIVO_RIFIUTATO_DA_TE = "Rifiutato da te: la riga del fornitore era un altro articolo"

# Status for an offer the comparison never found at all.
STATO_NON_TROVATO = "NON_TROVATO"

# Column headers, in this order. There's no column for the management
# software's internal item code, because that code plays no role here: the
# export works with EAN and alternate EANs, and alternate EANs aren't
# currently part of it. If alternate EANs are exported one day, they're
# additional EANs for the same product, and where they'd go is a decision
# for that point — not necessarily a single column.
INTESTAZIONI = (
    "EAN",
    "Descrizione",
    "Colli richiesti",
    "Ultimo prezzo noto",
    "Motivo",
    "Note",
)

# Column widths in characters, matching the header order. Description is
# the wide column because it's the one people read; `Note` is wide because
# it's the one they write in.
LARGHEZZE = (16, 52, 14, 18, 34, 40)

# The unit that's never written into the cell: the header already says
# "Colli" (cartons).
UNITA_IMPLICITA = "colli"

NOME_FOGLIO = "Da reperire"

# Two-decimal price format. Not a style choice — it's what keeps
# `3.9000000000000004` from showing up to a reader.
FORMATO_PREZZO = "#,##0.00"


def nome_file(momento: datetime) -> str:
    """`Prodotti da reperire — 17 agosto 2026.xlsx`, with a spaced em dash (U+2014).

    The date comes from `consegna.data_leggibile`, the same function that
    names compiled price lists: month names come from a lookup table rather
    than `strftime("%B")`, which depends on locale and would say `August` on
    this machine.

    The em dash matches `consegna.nome_listino` and survives the same way:
    the zip's UTF-8 flag (0x800), and the download header, handled by
    `consegna.intestazione_allegato`.
    """
    return f"{consegna.PREFISSO_DA_REPERIRE}— {consegna.data_leggibile(momento)}.xlsx"


def motivo(offerte: Sequence[Any] | None) -> str:
    """Return, as one sentence, why this product can't be ordered.

    A pure function over the product's `offers` list: it knows nothing about
    the server, opens no files, and doesn't decide which products belong on
    this list — the caller has already decided that.

    A single offer the user rejected is enough to select the third sentence:
    that product is here because of a human decision, not a gap in a price
    list.

    If every offer is `NON_TROVATO`, no supplier carries it on any price
    list at all. A single offer with a different status — the supplier has
    the article, but the offer isn't usable — selects the other sentence.

    A product with no offers at all (no supplier said anything about it) is
    the limiting case of "nobody has it on their price list", which is why
    `all()` over an empty list evaluating to true is the right behavior here.
    """
    stati = []
    for offerta in offerte or ():
        if not isinstance(offerta, dict):
            # Not a readable offer: it can't be asserted that this isn't on
            # a price list, so the cautious sentence is the other one.
            stati.append("")
            continue
        stato = offerta.get("status") or offerta.get("matchStatus") or ""
        stati.append(str(stato).strip().upper())
    # A single rejection is enough to pick this sentence, and it's checked
    # before the other two: it's the one piece of information that explains
    # why a product is on this list despite a supplier having a matching
    # row. Without it, the sheet would say "nobody has it available" —
    # blaming the supplier for a decision the person reading the sheet made.
    if any(stato == STATO_RIFIUTATO_UTENTE for stato in stati):
        return MOTIVO_RIFIUTATO_DA_TE
    if all(stato == STATO_NON_TROVATO for stato in stati):
        return MOTIVO_NON_A_LISTINO
    return MOTIVO_NON_DISPONIBILE


def _quantita_leggibile(quantita: Any, unita: Any) -> Any:
    """Build column 4's cell value: a plain number, or `3 espositori`.

    The header stays fixed as "Colli richiesti" because the list mixes
    products and displays in a single table: when the unit isn't "colli"
    (cartons) it's written inside the cell itself, otherwise three displays
    would read as three cartons.
    """
    etichetta = str(unita or "").strip()
    if not etichetta or etichetta.casefold() == UNITA_IMPLICITA:
        return quantita
    return f"{quantita} {etichetta}"


def scrivi(cartella: Path, righe: list[dict], momento: datetime) -> str:
    """Write the file into the folder and return its name.

    Each row: `{"ean", "descrizione", "quantita", "unita", "ultimo_prezzo",
    "motivo"}`. Extra keys are ignored. `ultimo_prezzo` may be `None`, and
    then the cell stays empty: a zero would claim the product cost nothing,
    which is a different fact than "we don't know".

    An empty `righe` is a caller error, not an empty file: a headers-only
    sheet would still show up in the compilation listing and the zip,
    telling whoever opens it there's something to source when there isn't.

    The EAN is written as text: a thirteen-digit barcode treated as a number
    becomes `8.0059e+12` in scientific notation, and copying that to search
    for it returns nothing.

    Written in two steps — a temp file alongside plus `os.replace` — because
    an `.xlsx` truncated mid-save is a broken zip Excel won't open, and would
    sit in the folder indistinguishable from a good file. The temp file gets
    a `.tmp` suffix, which `consegna.e_documento` doesn't treat as a
    document: even if it were left behind, it wouldn't be listed or
    delivered.
    """
    righe = list(righe or [])
    if not righe:
        raise ValueError("Non ci sono prodotti da reperire da scrivere")

    cartella = Path(cartella)
    nome = nome_file(momento)

    libro = Workbook()
    foglio = libro.active
    foglio.title = NOME_FOGLIO

    foglio.append(list(INTESTAZIONI))
    grassetto = Font(bold=True)
    for colonna in range(1, len(INTESTAZIONI) + 1):
        foglio.cell(row=1, column=colonna).font = grassetto
        foglio.column_dimensions[get_column_letter(colonna)].width = LARGHEZZE[colonna - 1]
    # Headers stay visible while scrolling through the list on the phone.
    foglio.freeze_panes = "A2"

    for indice, riga in enumerate(righe, start=2):
        ean = riga.get("ean")
        # EAN and description come from supplier price lists: text we don't
        # control. openpyxl writes a string starting with "=" as a formula —
        # an EAN or product name that happens to start that way would stop
        # being text and become a calculation (or a `#NAME?` error).
        # `data_type = "s"` forces the cell to stay literal text no matter
        # what it contains; don't remove it as redundant with `str(...)`,
        # which protects against the wrong type but not against the leading
        # character.
        cella_ean = foglio.cell(row=indice, column=1, value="" if ean is None else str(ean))
        cella_ean.data_type = "s"
        cella_descrizione = foglio.cell(row=indice, column=2, value=str(riga.get("descrizione") or ""))
        cella_descrizione.data_type = "s"
        foglio.cell(
            row=indice,
            column=3,
            value=_quantita_leggibile(riga.get("quantita"), riga.get("unita")),
        )
        prezzo = riga.get("ultimo_prezzo")
        if prezzo is not None:
            cella = foglio.cell(row=indice, column=4, value=prezzo)
            if isinstance(prezzo, (int, float)) and not isinstance(prezzo, bool):
                cella.number_format = FORMATO_PREZZO
        # Column 5 (`Motivo`) never receives arbitrary text — `motivo()`
        # returns one of the constants defined above — but it gets the same
        # protection for consistency with the two columns that do receive
        # untrusted text: belt and suspenders, not a gap being closed.
        cella_motivo = foglio.cell(row=indice, column=5, value=str(riga.get("motivo") or ""))
        cella_motivo.data_type = "s"
        # Column 6 (`Note`) is left as-is: it's the one the user fills in.

    percorso = cartella / nome
    temporaneo = percorso.with_name(percorso.name + ".tmp")
    libro.save(temporaneo)
    os.replace(temporaneo, percorso)
    return nome
