"""The column the app writes ordered quantities into, made changeable by the user.

Before this module, that column could be set only once, through the guided
mapping flow — which only opens when the app fails to recognize a
document's columns. For the four known suppliers the column lives in the
adapter registry (BETULLA C, LARICE D, CIPRESSO G, NOCE I) and the page only
displayed it; moving it meant editing a JSON file by hand.

This module validates nothing new. The proof that a chosen column is
actually writable is the same check the app runs before activating a
compilation: `launcher.source_rule`. The candidate declaration is built, fed
to that same function against the real document, and only written to the
registry if it passes. A validation rule invented here would be a second
authority on the same question — a real production compilation was blocked
once by exactly that kind of drift, when a hard-coded rule was replaced with
an invented one that the browser exercised but the tests hadn't.

Two supplier families, handled differently because the adapter registry
treats them differently:

* Dedicated reader (BETULLA, LARICE): `order_write` declares sheet, rows and
  column on its own. Changing the column means changing that one entry.
* `from_field_mapping` (CIPRESSO, NOCE): `order_write` and the confirmed
  field mapping must agree on the same column, and `source_rule` rejects
  them if they diverge. Changing only one would leave the supplier
  uncompilable with an error that blames the mapping; both are changed
  together.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "COLONNA_VALIDA",
    "campo_che_occupa",
    "colonne_per_la_scelta",
    "dichiarazione_aggiornata",
    "indice_di_lettera",
    "indice_scelto",
    "lettera_di_indice",
    "mappatura_aggiornata",
]

COLONNA_VALIDA = re.compile(r"[A-Z]{1,3}")


def lettera_di_indice(indice: Any) -> str:
    """1 -> "A", 27 -> "AA". Zero and negatives aren't columns and return ""."""

    try:
        numero = int(indice)
    except (TypeError, ValueError):
        return ""
    if numero < 1:
        return ""
    risultato = ""
    while numero > 0:
        numero, resto = divmod(numero - 1, 26)
        risultato = chr(ord("A") + resto) + risultato
    return risultato


def indice_di_lettera(lettera: Any) -> int | None:
    """"A" -> 1, "AA" -> 27. Anything else is `None`, not a zero.

    A silent zero would end up in `foglio.cell(riga, 0)`, which openpyxl
    rejects with an exception far from where the letter was actually wrong.
    """

    testo = str(lettera or "").strip().upper()
    if not COLONNA_VALIDA.fullmatch(testo):
        return None
    numero = 0
    for carattere in testo:
        numero = numero * 26 + ord(carattere) - ord("A") + 1
    return numero


def indice_scelto(valore: Any) -> int | None:
    """Resolve which column was meant: "C", "3" and `3` are the same column.

    The page sends the numeric index (the `<select>`'s value, same as in the
    guided mapping flow), but the same input can also arrive as a letter,
    which is the form the registry stores it in. Translating in one place
    avoids the two representations disagreeing over "AA" versus 27.
    """

    if isinstance(valore, bool) or valore in (None, ""):
        return None
    if isinstance(valore, int):
        return valore if valore >= 1 else None
    testo = str(valore).strip()
    if testo.isdigit():
        numero = int(testo)
        return numero if numero >= 1 else None
    return indice_di_lettera(testo)


def campo_che_occupa(effettiva: Any, indice: Any) -> str:
    """Return the readable name of the field that column is currently mapped to.

    This is the only rejection this module makes on its own, and it isn't
    an invented rule: it's the same one the guided mapping flow has always
    applied ("the order column can't also be the price column",
    `schema_mapping`). Worth stating why it's strict: the order column gets
    overwritten on the copy, so pointing it at the price column wouldn't
    raise an error — it would produce a compiled price list with its prices
    wiped, and the fidelity check wouldn't catch it, since the order plan is
    allowed to touch that cell.
    """

    if not isinstance(effettiva, dict) or not indice:
        return ""
    for voce in effettiva.get("columns") or []:
        if not isinstance(voce, dict):
            continue
        if voce.get("colonna") == indice:
            return str(voce.get("etichetta") or voce.get("campo") or "")
    return ""


def colonne_per_la_scelta(
    effettiva: Any, colonne_foglio: Any
) -> list[dict[str, Any]]:
    """List the document's columns, marking which is current and which are taken.

    Taken columns are still shown, just disabled: hiding them would make the
    document look like it has fewer columns than it does, and someone
    looking for "the one after the price column" counts what they can see.
    """

    ordine = (effettiva or {}).get("orderColumn") if isinstance(effettiva, dict) else None
    ordine = ordine if isinstance(ordine, dict) else {}
    attuale = ordine.get("colonna")
    scelte: list[dict[str, Any]] = []
    for voce in colonne_foglio or []:
        if not isinstance(voce, dict):
            continue
        indice = voce.get("colonna")
        occupata = campo_che_occupa(effettiva, indice)
        scelte.append({
            **voce,
            "attuale": bool(attuale) and indice == attuale,
            "occupataDa": occupata,
            "scegliibile": not occupata,
        })
    return scelte


def dichiarazione_aggiornata(
    order_write: Any, lettera: Any, intestazione: Any, *, c_e_intestazione: bool = True
) -> dict[str, Any]:
    """Build `order_write` with the new column and the document's real header text.

    The old header text is never copied onto the new column: `expected_header`
    is a measurement of the document, not a preference. If the chosen column
    has a title, that becomes what gets verified; if it's blank, the
    declaration says so, and then `source_rule` requires it to genuinely
    still be blank, with no text or formulas underneath either.

    `c_e_intestazione=False` is for LARICE, whose price list has no header
    row at all, so there's no cell above the column to look at.
    Declaring "confirmed blank cell" there wouldn't make the check stricter,
    it would make it impossible: `source_rule` needs a header row to perform
    that check and would fail with an error blaming the registry for
    something the document itself doesn't have.

    `mode`, `from_field_mapping` and `required_columns` are left untouched:
    they're that supplier's write procedure (one supplier's `.xls` is
    patched in place) and have nothing to do with which column gets filled.
    """

    nuova = dict(order_write) if isinstance(order_write, dict) else {}
    nuova["order_column"] = str(lettera or "").strip().upper()
    titolo = " ".join(str(intestazione or "").split())
    if titolo:
        nuova["expected_header"] = titolo
        nuova.pop("allow_blank_header_if_confirmed", None)
        nuova.pop("order_header_blank_confirmed", None)
    elif not c_e_intestazione:
        nuova.pop("expected_header", None)
        nuova.pop("allow_blank_header_if_confirmed", None)
        nuova.pop("order_header_blank_confirmed", None)
    else:
        nuova.pop("expected_header", None)
        nuova["allow_blank_header_if_confirmed"] = True
        # For a supplier with a dedicated reader (BETULLA), there's no field
        # mapping for the blank-cell confirmation to live in. Without this
        # key, `source_rule` would find no confirmation, `expected_header`
        # would be absent, and the column would be written with no check at
        # all — choosing a header-less column would turn the safeguard off
        # instead of on.
        nuova["order_header_blank_confirmed"] = True
    return nuova


def mappatura_aggiornata(
    mappatura: Any, lettera: Any, intestazione: Any, *, c_e_intestazione: bool = True
) -> dict[str, Any]:
    """Build the confirmed field mapping that agrees with `order_write`.

    Used for `from_field_mapping` suppliers. The keys match what the guided
    mapping flow has always written (`order_column`, `order_header_expected`,
    `order_header_blank_confirmed`), because that's the shape
    `impara_adattatore.verifica_intestazione_ordine` knows how to read back;
    inventing different keys here would mean the column reverts the next
    time that document gets relearned.
    """

    nuova = dict(mappatura) if isinstance(mappatura, dict) else {}
    nuova["order_column"] = str(lettera or "").strip().upper()
    titolo = " ".join(str(intestazione or "").split())
    if titolo:
        nuova["order_header_expected"] = titolo
        nuova.pop("order_header_blank_confirmed", None)
    elif not c_e_intestazione:
        nuova.pop("order_header_expected", None)
        nuova.pop("order_header_blank_confirmed", None)
    else:
        nuova.pop("order_header_expected", None)
        nuova["order_header_blank_confirmed"] = True
    return nuova
