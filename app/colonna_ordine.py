"""La colonna in cui il programma scrive le quantità ordinate, cambiabile.

Fino al 18 agosto 2026 quella colonna si sceglieva **una volta sola**, dentro
la mappatura guidata — e quella si apre soltanto quando il programma non
riconosce le colonne di un documento. Per i quattro fornitori conosciuti la
colonna stava nel registro (BETULLA C, LARICE D, CIPRESSO G, NOCE I) e la
pagina la mostrava e basta: per spostarla bisognava aprire un file JSON.

⚠ **Qui non si verifica niente di nuovo.** La prova che la colonna scelta sia
davvero scrivibile è la **stessa** che il programma fa prima di attivare la
compilazione: `launcher.source_rule`. Si costruisce la dichiarazione che si
vorrebbe scrivere, la si dà in pasto a quella funzione sul documento vero, e si
scrive nel registro **solo se passa**. Una regola inventata qui sarebbe una
seconda autorità sulla stessa domanda, ed è già costata una compilazione
bloccata dal vivo (LARICE, 14 agosto 2026: un cablato sostituito con una regola
inventata, trovata dal browser e non dai test).

Le due famiglie di fornitori, che qui vanno trattate diversamente perché il
registro le tratta diversamente:

* **lettore dedicato** (BETULLA, LARICE): `order_write` dichiara da solo foglio,
  righe e colonna. Cambiare la colonna vuol dire cambiare quella riga lì.
* **`from_field_mapping`** (CIPRESSO, NOCE): `order_write` e la mappatura
  confermata devono dire **la stessa** colonna, e `source_rule` si rifiuta se
  divergono. Cambiarne una sola lascerebbe il fornitore non compilabile con una
  frase che accusa la mappatura: si cambiano tutte e due, insieme.
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
    """1 → «A», 27 → «AA». Zero e i negativi non sono colonne: danno «»."""

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
    """«A» → 1, «AA» → 27. Qualunque altra cosa è `None`, non uno zero.

    Uno zero silenzioso finirebbe in `foglio.cell(riga, 0)`, che openpyxl
    rifiuta con un'eccezione lontana dal punto in cui la lettera era sbagliata.
    """

    testo = str(lettera or "").strip().upper()
    if not COLONNA_VALIDA.fullmatch(testo):
        return None
    numero = 0
    for carattere in testo:
        numero = numero * 26 + ord(carattere) - ord("A") + 1
    return numero


def indice_scelto(valore: Any) -> int | None:
    """Quale colonna intendeva chi ha risposto: «C», «3» e `3` sono la stessa.

    La pagina manda il numero (il `<select>` ha per valore l'indice, come nella
    mappatura guidata), ma la stessa domanda arriva anche a voce e per lettera —
    ed e' per lettera che il registro la scrive. Tradurre in un posto solo
    evita che i due modi divergano su «AA» contro 27.
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
    """Il nome leggibile del campo che quella colonna serve già a **leggere**.

    È l'unico rifiuto che questo modulo dà di suo, e non è una regola inventata:
    è la stessa che la mappatura guidata applica da sempre («la colonna ordine
    non puo' essere anche la colonna prezzo», `schema_mapping`). Vale la pena
    dire perché è severa: la colonna d'ordine viene **azzerata e riscritta**
    sulla copia, quindi indicarla sulla colonna del prezzo non darebbe un errore
    — darebbe un listino compilato con i prezzi cancellati, e il controllo di
    fedeltà non se ne accorgerebbe, perché quella cella il piano ha il diritto
    di toccarla.
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
    """Le colonne del documento, dicendo qual è l'attuale e quali sono occupate.

    Le occupate si mostrano lo stesso, spente: nasconderle farebbe sembrare che
    il documento abbia meno colonne di quelle che ha, e chi cerca «quella dopo
    il prezzo» conta quelle che vede.
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
    """`order_write` con la colonna nuova, e l'intestazione che il documento ha.

    Non si copia l'intestazione vecchia sulla colonna nuova: `expected_header` è
    una **misura** del documento, non una preferenza. Se la colonna scelta ha un
    titolo, quello diventa la cosa da verificare; se è vuota, si dichiara che è
    vuota — e allora `source_rule` pretende che sia ancora vuota davvero, e in
    più che sotto non ci siano testo o formule.

    ⚠ `c_e_intestazione` **falso** è il caso di LARICE, e va trattato a parte:
    quel listino non ha nessuna riga di intestazione, quindi sopra la colonna
    non c'è nessuna cella da guardare. Dichiarare lì «cella vuota confermata»
    non renderebbe il controllo più severo — lo renderebbe **impossibile**:
    `source_rule` pretende una riga di intestazione per poterlo fare e si
    fermerebbe con «il registro non dichiara le righe da cui parte l'ordine»,
    cioè accusando il registro di una cosa che il documento non ha. Misurato sul
    listino vero il 18 agosto 2026, prima di scriverlo.

    ⚠ `mode`, `from_field_mapping` e `required_columns` restano dov'erano: sono
    la procedura di scrittura di quel fornitore (il `.xls` di Noce si scrive
    in posizione) e non hanno niente a che vedere con quale colonna si riempie.
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
        # ⚠ La conferma della cella vuota, per un fornitore con un lettore
        # dedicato (BETULLA), non ha una mappatura in cui stare: senza questa
        # chiave `source_rule` non troverebbe nessuna conferma, `expected_header`
        # sarebbe assente, e la colonna verrebbe scritta **senza nessun
        # controllo**. Cioè: scegliere una colonna senza titolo spegnerebbe la
        # difesa invece di accenderla.
        nuova["order_header_blank_confirmed"] = True
    return nuova


def mappatura_aggiornata(
    mappatura: Any, lettera: Any, intestazione: Any, *, c_e_intestazione: bool = True
) -> dict[str, Any]:
    """La mappatura confermata che dice la stessa cosa di `order_write`.

    Serve ai fornitori `from_field_mapping`. Le chiavi sono quelle che la
    mappatura guidata scrive da sempre (`order_column`, `order_header_expected`,
    `order_header_blank_confirmed`), perché è quella forma che
    `impara_adattatore.verifica_intestazione_ordine` sa rileggere: inventarne
    altre qui vorrebbe dire che la prossima volta che quel documento viene
    imparato la colonna tornerebbe indietro.
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
