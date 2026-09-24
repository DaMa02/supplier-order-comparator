#!/usr/bin/env python3
"""Fill the order-quantity column into a copy of a supplier's `.xls` file.

The delivery to this supplier is their own file back, with only the order
column filled in and everything else byte-identical — not a separate sheet,
not a converted `.xlsx`.

This is possible without writing a full BIFF writer because, on the real
price list, the order column is 17,145 cells, zero formulas, all RK-encoded
(fixed 4-byte numbers). Replacing a stored zero with an integer quantity
moves no other byte: no offsets to recompute, no records to shift, the rest
of the file stays identical.

That shortcut depends on the column staying that shape, so the check is
redone for every file: if a cell ever arrived blank, as a formula, or as an
8-byte float, the fixed-length patch would no longer apply. The check runs
before any byte is touched, and if it fails the fill for this supplier
raises rather than falling back to a format the supplier won't accept.

Two extra checks this supplier's file gets, at no added read cost since the
file is parsed anyway:

1. The row's EAN must match the plan's. Other suppliers trust the row
   number once the file's fingerprint and header are verified; here every
   row is checked and the fill stops at the first mismatch, guarding
   against a shifted or reordered price list slipping a wrong product's
   quantity into the wrong row.
2. The total the spreadsheet itself computes must match the plan's total.
   The `Importo` column's formula is `quantity x price x pieces_per_carton`,
   the same model the comparator uses; if the totals disagree, nothing is
   delivered.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import scrittura_sicura
from xls_reader import XlsError, apri_contenitore, posizioni_fogli, read_workbook


_BOF = 0x0809
_EOF = 0x000A
_RK = 0x027E
_RK_ALTERNATIVO = 0x007E
_MULRK = 0x00BD
_NUMBER = 0x0203
_BLANK = 0x0201
_MULBLANK = 0x00BE
_FORMULA = 0x0006
_FORMULA_ALTERNATIVO = 0x0406
_LABELSST = 0x00FD
_LABEL = 0x0204
_RSTRING = 0x00D6
_BOOLERR = 0x0205
_RECALCID = 0x01C1
_TIPO_FOGLIO_DATI = 0x00

# The largest value a signed 30-bit RK integer can hold.
MASSIMA_QUANTITA_RK = (1 << 29) - 1

NOME_TIPO_CELLA = {
    _NUMBER: "un numero a virgola mobile",
    _BLANK: "una cella vuota",
    _MULBLANK: "una cella vuota",
    _FORMULA: "una formula",
    _FORMULA_ALTERNATIVO: "una formula",
    _LABELSST: "del testo",
    _LABEL: "del testo",
    _RSTRING: "del testo",
    _BOOLERR: "un valore logico o un errore",
}


class CompilazioneXlsError(XlsError):
    """The order fill can't be done; the message says why."""


@dataclass
class _CellaOrdine:
    """A cell of the order column and where its four bytes live."""

    riga: int          # row number as the user sees it, 1-based
    posizione: int     # offset of the RK value within the workbook stream
    valore: Any        # what's currently stored there


@dataclass
class _Scansione:
    celle: dict[int, _CellaOrdine] = field(default_factory=dict)
    non_rk: dict[int, str] = field(default_factory=dict)


def codifica_rk_intero(valore: int) -> int:
    """Pack an integer into the four bytes of an RK value.

    Two trailing bits: the low one means "divide by 100" and stays off, the
    one above means "integer" and is set. The high 30 bits carry the number.
    Exact inverse of the reader's `_decodifica_rk`; the round trip is
    covered by a test.
    """

    if isinstance(valore, bool) or not isinstance(valore, int):
        raise CompilazioneXlsError(f"La quantità {valore!r} non è un numero intero.")
    if not 0 <= valore <= MASSIMA_QUANTITA_RK:
        raise CompilazioneXlsError(
            f"La quantità {valore} non sta nei quattro byte della cella d'ordine."
        )
    return (valore << 2) | 0x02


def _scorri_record(flusso: bytes, posizione: int) -> Iterable[tuple[int, int, bytes]]:
    """`(code, header offset, data)` for every record from here onward."""

    while posizione + 4 <= len(flusso):
        (codice, lunghezza) = struct.unpack_from("<HH", flusso, posizione)
        inizio = posizione + 4
        fine = inizio + lunghezza
        if fine > len(flusso):
            raise CompilazioneXlsError("Il file finisce a metà di un record: è rovinato.")
        yield codice, posizione, flusso[inizio:fine]
        posizione = fine


def _scansiona_colonna(flusso: bytes, inizio_foglio: int, colonna: int) -> _Scansione:
    """Locate every cell of the order column and its type.

    Scans the whole sheet, not just the rows to fill: the "is the column
    still all RK" check only means something if every row was seen.
    """

    scansione = _Scansione()
    dentro = False
    for codice, posizione, dati in _scorri_record(flusso, inizio_foglio):
        if codice == _BOF:
            if dentro:
                break
            dentro = True
            continue
        if not dentro:
            continue
        if codice == _EOF:
            break
        if codice in (_RK, _RK_ALTERNATIVO):
            (riga, colonna_cella, _xf, grezzo) = struct.unpack_from("<HHHI", dati, 0)
            if colonna_cella == colonna:
                scansione.celle[riga + 1] = _CellaOrdine(
                    riga=riga + 1, posizione=posizione + 4 + 6, valore=grezzo
                )
        elif codice == _MULRK:
            (riga, prima_colonna) = struct.unpack_from("<HH", dati, 0)
            quante = (len(dati) - 6) // 6
            if prima_colonna <= colonna < prima_colonna + quante:
                passo = colonna - prima_colonna
                (_xf, grezzo) = struct.unpack_from("<HI", dati, 4 + 6 * passo)
                scansione.celle[riga + 1] = _CellaOrdine(
                    riga=riga + 1,
                    posizione=posizione + 4 + 4 + 6 * passo + 2,
                    valore=grezzo,
                )
        elif codice in NOME_TIPO_CELLA:
            if codice == _MULBLANK:
                (riga, prima_colonna) = struct.unpack_from("<HH", dati, 0)
                quante = (len(dati) - 6) // 2
                if prima_colonna <= colonna < prima_colonna + quante:
                    scansione.non_rk[riga + 1] = NOME_TIPO_CELLA[codice]
            else:
                (riga, colonna_cella, _xf) = struct.unpack_from("<HHH", dati, 0)
                if colonna_cella == colonna:
                    scansione.non_rk[riga + 1] = NOME_TIPO_CELLA[codice]
    return scansione


def _posizione_recalcid(flusso: bytes) -> int | None:
    """Find the calculation-engine id in the workbook globals.

    Overwriting a cell's stored value does not dirty the formulas that
    reference it: Excel shows whatever result it last found written in the
    file, so patching quantities without this step would leave the
    dependent totals stuck at their old (usually zero) value.

    `RECALCID` carries the id of the engine that last recalculated. Zeroing
    it tells Excel "an older engine computed this", which forces a full
    recalculation on open. It's four bytes, and like the rest of the patch
    it overwrites in place without moving anything.
    """

    posizione = 0
    while posizione + 4 <= len(flusso):
        (codice, lunghezza) = struct.unpack_from("<HH", flusso, posizione)
        if codice == _RECALCID and lunghezza >= 8:
            return posizione + 4 + 4
        if codice == _EOF:
            # Globals end here; sheets follow after this point.
            return None
        posizione += 4 + lunghezza
    return None


def _indice_di_colonna(lettera: Any) -> int:
    """Resolve a column reference: `I` and `9` both become 8.

    An int is accepted because the EAN column is declared by header name in
    the adapter registry, and the caller resolves it by reading the file's
    header row before calling this; what arrives here is already a 1-based
    column number, as in Excel.
    """

    if isinstance(lettera, bool):
        raise CompilazioneXlsError(f"Colonna non valida: {lettera!r}")
    if isinstance(lettera, int):
        if lettera < 1:
            raise CompilazioneXlsError(f"Colonna non valida: {lettera!r}")
        return lettera - 1
    testo = str(lettera or "").strip().upper()
    if not testo or not testo.isalpha():
        raise CompilazioneXlsError(f"Colonna non valida: {lettera!r}")
    indice = 0
    for carattere in testo:
        indice = indice * 26 + (ord(carattere) - ord("A") + 1)
    return indice - 1


def _foglio_scelto(flusso: bytes, nome_atteso: str | None) -> tuple[str, int]:
    fogli = [voce for voce in posizioni_fogli(flusso) if voce[2] == _TIPO_FOGLIO_DATI]
    if not fogli:
        raise CompilazioneXlsError("Il file non contiene nessun foglio di dati.")
    if nome_atteso in (None, "", "FIRST"):
        nome, posizione, _tipo = fogli[0]
        return nome, posizione
    for nome, posizione, _tipo in fogli:
        if nome == nome_atteso:
            return nome, posizione
    raise CompilazioneXlsError(
        f"Il foglio «{nome_atteso}» non c'è in questo file: la compilazione si ferma."
    )


def _mappa_posizioni(segmenti: list[tuple[int, int]], quante: int) -> list[tuple[int, int, int]]:
    """`(stream offset, file offset, length)` triples, to translate offsets."""

    mappa: list[tuple[int, int, int]] = []
    scorso = 0
    for inizio_file, lunghezza in segmenti:
        mappa.append((scorso, inizio_file, lunghezza))
        scorso += lunghezza
    if scorso != quante:
        raise CompilazioneXlsError(
            "La mappa dei settori non copre tutto il libro di lavoro: il file non è "
            "quello che dichiara di essere e non viene modificato."
        )
    return mappa


def _posizione_nel_file(mappa: list[tuple[int, int, int]], posizione: int) -> int:
    for inizio_flusso, inizio_file, lunghezza in mappa:
        if inizio_flusso <= posizione < inizio_flusso + lunghezza:
            return inizio_file + (posizione - inizio_flusso)
    raise CompilazioneXlsError("Una cella da compilare sta fuori dal libro di lavoro.")


def _leggi_dal_flusso(dati: bytes, mappa: list[tuple[int, int, int]],
                      posizione: int, quanti: int) -> bytes:
    """Read `quanti` bytes that are contiguous in the STREAM, from their file offsets.

    Stream bytes are contiguous; file bytes are not. A `.xls` is an OLE
    container: the stream is split into sectors (512 bytes here) that sit
    out of order in the file. An RK cell straddling a sector boundary has
    its four bytes in two distant sectors, so reading (or writing) them as
    one run starting from the first byte's file offset would read — or
    worse, overwrite — bytes belonging to another sector. On the real price
    list this affects roughly a hundred cells per column.
    """

    return bytes(dati[_posizione_nel_file(mappa, posizione + scarto)] for scarto in range(quanti))


def _scrivi_nel_flusso(dati: bytearray, mappa: list[tuple[int, int, int]],
                       posizione: int, contenuto: bytes) -> None:
    """Write `contenuto` byte by byte, at each byte's real file offset.

    The write-side twin of `_leggi_dal_flusso`, for the same reason: writing
    four bytes as one run from a single translated offset corrupts a cell
    that straddles a sector boundary, since the offset is only correct for
    the first byte. The other bytes then land over unrelated sector data
    and the file Excel receives fails to open.
    """

    for scarto, byte in enumerate(contenuto):
        dati[_posizione_nel_file(mappa, posizione + scarto)] = byte


def controlla_colonna_ordine(
    origine: Path,
    *,
    colonna_ordine: str,
    foglio: str | None = None,
    prima_riga: int = 1,
    ultima_riga: int | None = None,
) -> dict[str, Any]:
    """Inspect the order column before writing to it, and report what's there.

    Reports how many cells are RK vs. some other type. If they aren't all
    RK the fixed-length patch doesn't apply, and the caller must stop
    instead of writing something that only looks like a valid order.
    """

    dati = Path(origine).read_bytes()
    contenitore = apri_contenitore(dati)
    flusso = contenitore.flusso("Workbook", "Book")
    _nome, inizio = _foglio_scelto(flusso, foglio)
    scansione = _scansiona_colonna(flusso, inizio, _indice_di_colonna(colonna_ordine))
    fine = ultima_riga if ultima_riga is not None else max(
        [*scansione.celle, *scansione.non_rk] or [prima_riga - 1]
    )
    rk = sorted(riga for riga in scansione.celle if prima_riga <= riga <= fine)
    altre = {riga: tipo for riga, tipo in scansione.non_rk.items() if prima_riga <= riga <= fine}
    return {
        "foglio": _nome,
        "prima_riga": prima_riga,
        "ultima_riga": fine,
        "celle_rk": len(rk),
        "celle_di_altro_tipo": altre,
        # An order column with zero writable cells isn't fillable: it's a
        # missing column. `not altre` alone would say "fillable" for that
        # case too, deferring the failure to a less clear error later.
        "compilabile": not altre and bool(rk),
    }


def compila_ordine(
    origine: Path,
    destinazione: Path,
    righe: Mapping[int, int],
    *,
    colonna_ordine: str,
    foglio: str | None = None,
    ean_attesi: Mapping[int, str] | None = None,
    colonna_ean: str | int | None = None,
    prima_riga: int = 1,
) -> dict[str, Any]:
    """Write quantities into the order column of a copy of the file.

    The original is never touched: it's read, patched in memory, and
    written to a copy. Returns what was done, so the fill can be audited
    without reopening the file.
    """

    origine = Path(origine)
    destinazione = Path(destinazione)
    if not righe:
        raise CompilazioneXlsError("Non c'è nessuna riga da compilare.")

    dati = bytearray(Path(origine).read_bytes())
    contenitore = apri_contenitore(bytes(dati))
    flusso = contenitore.flusso("Workbook", "Book")
    segmenti = contenitore.segmenti("Workbook", "Book")
    mappa = _mappa_posizioni(segmenti, len(flusso))
    nome_foglio, inizio = _foglio_scelto(flusso, foglio)
    colonna = _indice_di_colonna(colonna_ordine)
    scansione = _scansiona_colonna(flusso, inizio, colonna)

    # 1. The column must still be all RK, from the first data row down.
    #    Re-checked for every file: this must scan every row the fill can
    #    touch, not just the rows the plan mentions, or a formula further
    #    down would be skipped by the zeroing step (it isn't a fixed-length
    #    number and has no quantity to zero) and survive untouched into the
    #    copy, which mirrors what `controlla_colonna_ordine` already checks.
    fuori = {riga: tipo for riga, tipo in scansione.non_rk.items() if riga >= prima_riga}
    if fuori:
        esempi = ", ".join(f"riga {riga} ({tipo})" for riga, tipo in sorted(fuori.items())[:5])
        raise CompilazioneXlsError(
            f"La colonna d'ordine {colonna_ordine.upper()} di «{origine.name}» non è più "
            f"tutta fatta di numeri a lunghezza fissa: {len(fuori)} celle sono di altro tipo "
            f"({esempi}). L'ordine Noce non viene compilato."
        )

    # 2. Every row the plan mentions must be a real order cell.
    mancanti = sorted(riga for riga in righe if riga not in scansione.celle)
    if mancanti:
        raise CompilazioneXlsError(
            f"Il piano indica {len(mancanti)} righe che nel listino non hanno una cella "
            f"d'ordine (per esempio la {mancanti[0]}). L'ordine Noce non viene compilato."
        )

    # 3. The row's EAN must match the plan's. Costs one extra read, but the
    #    file is parsed anyway, and this is the only check that catches a
    #    row shifted to the wrong product.
    controllati = 0
    if ean_attesi and colonna_ean:
        indice_ean = _indice_di_colonna(colonna_ean)
        fogli = read_workbook(origine)
        griglia = next((f.rows for f in fogli if f.name == nome_foglio), fogli[0].rows)
        for riga, atteso in sorted(ean_attesi.items()):
            valori = griglia[riga - 1] if 0 < riga <= len(griglia) else []
            trovato = str(valori[indice_ean][0] or "").strip() if indice_ean < len(valori) else ""
            atteso_pulito = str(atteso or "").strip()
            if not atteso_pulito:
                # The plan carries no EAN for this row. If the price list
                # does have one there, the row was picked without the data
                # that identifies it, and only the row number is left to
                # rely on — the exact failure mode this check exists to
                # catch. If the price list has no EAN there either (a
                # display, a service row), there's nothing to compare and
                # the row is skipped.
                if trovato:
                    raise CompilazioneXlsError(
                        f"Il piano non dice quale prodotto sia la riga {riga} di «{origine.name}», "
                        f"ma il listino lì porta l'EAN {trovato}: non si può controllare che sia "
                        "la riga giusta. L'ordine Noce non viene compilato."
                    )
                continue
            if trovato != atteso_pulito:
                raise CompilazioneXlsError(
                    f"La riga {riga} di «{origine.name}» porta l'EAN {trovato or 'vuoto'} e il "
                    f"piano si aspetta {atteso}: il listino non è più quello su cui è stato "
                    "costruito l'ordine. L'ordine Noce non viene compilato."
                )
            controllati += 1

    # 4. Zero out whatever was already in the order column, then write the
    #    plan. Matches `write_supplier_orders.mjs`, which also zeroes
    #    pre-existing quantities: the source file for a run is often last
    #    week's filled copy, and without this step its stale quantities
    #    would ship alongside the new plan. Only RK cells in the order
    #    column from the first data row down are touched; a cell that isn't
    #    a fixed-length number has no quantity to zero.
    zero = struct.pack("<I", codifica_rk_intero(0))
    azzerate = 0
    for riga, cella in sorted(scansione.celle.items()):
        if riga < prima_riga or riga in righe:
            continue
        if _leggi_dal_flusso(dati, mappa, cella.posizione, 4) == zero:
            continue
        _scrivi_nel_flusso(dati, mappa, cella.posizione, zero)
        azzerate += 1
    for riga, quantita in sorted(righe.items()):
        cella = scansione.celle[riga]
        codificato = codifica_rk_intero(int(quantita))
        _scrivi_nel_flusso(dati, mappa, cella.posizione, struct.pack("<I", codificato))

    # 5. Tell Excel to recompute on open; without this the quantities would
    #    be correct but the dependent totals would stay at zero.
    posizione_recalcid = _posizione_recalcid(flusso)
    ricalcolo_forzato = posizione_recalcid is not None
    if ricalcolo_forzato:
        _scrivi_nel_flusso(dati, mappa, posizione_recalcid, struct.pack("<I", 0))

    # Written via a temp file, `fsync` and `os.replace` rather than a
    # direct `write_bytes`, so a full disk or a killed process can't leave
    # a truncated `.xls` in place of a good previous copy.
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    scrittura_sicura.scrivi_bytes(destinazione, bytes(dati))
    return {
        "foglio": nome_foglio,
        "colonna_ordine": colonna_ordine.upper(),
        "righe_scritte": len(righe),
        "quantita_azzerate": azzerate,
        "colli_totali": int(sum(int(valore) for valore in righe.values())),
        "ean_controllati": controllati,
        "celle_rk_nella_colonna": len(scansione.celle),
        # When `RECALCID` is absent entirely, Excel recalculates anyway on
        # open: the missing field itself tells it there's no known engine.
        "ricalcolo_forzato": ricalcolo_forzato,
        "byte_origine": len(dati),
        "byte_copia": destinazione.stat().st_size,
    }
