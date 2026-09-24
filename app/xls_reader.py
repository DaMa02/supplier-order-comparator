"""Reader for legacy Excel 97-2003 (.xls) files, stdlib only.

Some suppliers only provide `.xls`, never `.xlsx`, so this reader exists to
keep them in the comparison without adding a third-party dependency (the
tool has to run as-is on the user's machine, without Excel installed).

A `.xls` file is two nested layers:
1. An OLE2 container (Compound File Binary): a small file system with a
   header, a sector map (FAT), a directory of streams, and a mini-FAT for
   streams below a size threshold. The workbook lives in the "Workbook"
   stream.
2. BIFF8 records inside that stream: a sequence of (code, length, data)
   blocks describing sheets, shared strings, formats and cells.

This reader returns the value Excel already computed and stored (not the
formula), plus whether the cell font is bold. Bold is kept because on some
supplier price lists it is how a promotional price is marked.

The source file is only ever read, never rewritten or moved.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["XlsError", "XlsSheet", "read_workbook", "read_sheet_values"]


class XlsError(Exception):
    """The file can't be read; the message explains why."""


@dataclass
class XlsSheet:
    """A sheet of the workbook.

    ``rows[r][c]`` is always a ``(value, bold)`` pair: rows are dense and
    missing cells are ``(None, False)``, so callers never need to guard
    against an IndexError.
    """

    name: str
    rows: list[list[tuple[object, bool]]] = field(default_factory=list)


# A single shared, immutable empty-cell object: safe to reuse across the
# whole grid without allocating one per missing cell on large sheets.
_CELLA_VUOTA: tuple[object, bool] = (None, False)


# ---------------------------------------------------------------------------
# Layer 1: the OLE2 container
# ---------------------------------------------------------------------------

_FIRMA_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FIRMA_ZIP = b"PK\x03\x04"

# Sector-map values above this threshold aren't real sectors: they're
# markers (end of chain, free sector, reserved).
_MASSIMO_SETTORE_REGOLARE = 0xFFFFFFFA

_VOCE_FLUSSO = 2
_VOCE_RADICE = 5


class _ContenitoreOle2:
    """The outer layer of a .xls file: a small file system inside a file.

    Workbook data isn't laid out sequentially; it's scattered across sectors
    that only the FAT can reassemble in order.
    """

    def __init__(self, dati: bytes) -> None:
        self._dati = dati
        if dati[:4] == _FIRMA_ZIP:
            raise XlsError(
                "Questo file è in realtà un .xlsx (il formato nuovo di Excel): "
                "va letto con il lettore dei .xlsx, non con questo."
            )
        if len(dati) < 512 or not dati.startswith(_FIRMA_OLE2):
            raise XlsError("Il file non è un documento Excel 97-2003 (.xls).")

        (versione_maggiore, ordine_byte) = struct.unpack_from("<HH", dati, 26)
        if ordine_byte != 0xFFFE:
            raise XlsError("Il file dichiara un ordine dei byte che Excel non usa.")

        (esponente, esponente_mini) = struct.unpack_from("<HH", dati, 30)
        if not 7 <= esponente <= 20 or not 2 <= esponente_mini <= esponente:
            raise XlsError("Le dimensioni dei settori dichiarate nel file non sono valide.")
        self._dimensione_settore = 1 << esponente
        self._dimensione_mini = 1 << esponente_mini

        (numero_fat, primo_direttorio) = struct.unpack_from("<II", dati, 44)
        (self._soglia_mini,) = struct.unpack_from("<I", dati, 56)
        (primo_mini_fat, numero_mini_fat, primo_difat) = struct.unpack_from("<III", dati, 60)

        self._fat = self._costruisci_fat(numero_fat, primo_difat)
        self._voci = self._leggi_direttorio(primo_direttorio, versione_maggiore)
        # The mini-stream and mini-FAT only matter for streams below the
        # threshold; a real workbook stream is large and lives in normal
        # sectors, but small streams still exist and must be readable.
        self._mini_fat = self._costruisci_mini_fat(primo_mini_fat, numero_mini_fat)
        self._mini_stream = self._costruisci_mini_stream()

    # -- raw sector access ---------------------------------------------------

    def _settore(self, numero: int) -> bytes:
        inizio = (numero + 1) * self._dimensione_settore
        if inizio >= len(self._dati):
            raise XlsError("Il file indica un settore oltre la sua fine: è incompleto o rovinato.")
        blocco = self._dati[inizio : inizio + self._dimensione_settore]
        if len(blocco) < self._dimensione_settore:
            # The last sector can be truncated; pad with zeros instead of
            # failing the whole read for a few trailing bytes.
            blocco += b"\x00" * (self._dimensione_settore - len(blocco))
        return blocco

    def _interi_del_settore(self, numero: int) -> tuple[int, ...]:
        quanti = self._dimensione_settore // 4
        return struct.unpack("<%dI" % quanti, self._settore(numero))

    def _catena(self, primo: int, mappa: list[int], dove: str) -> list[int]:
        catena: list[int] = []
        visti: set[int] = set()
        corrente = primo
        while corrente <= _MASSIMO_SETTORE_REGOLARE:
            if corrente >= len(mappa):
                raise XlsError(f"La mappa {dove} rimanda a un settore che non esiste: file rovinato.")
            if corrente in visti:
                raise XlsError(f"La mappa {dove} si ripiega su se stessa: file rovinato.")
            visti.add(corrente)
            catena.append(corrente)
            corrente = mappa[corrente]
        return catena

    # -- the two maps ---------------------------------------------------------

    def _costruisci_fat(self, numero_fat: int, primo_difat: int) -> list[int]:
        # The first 109 references live in the header; if that's not enough,
        # continue through the DIFAT sector chain, where the last integer of
        # each sector points to the next one.
        elenco = [n for n in struct.unpack_from("<109I", self._dati, 76) if n <= _MASSIMO_SETTORE_REGOLARE]
        visti: set[int] = set()
        settore = primo_difat
        while settore <= _MASSIMO_SETTORE_REGOLARE:
            if settore in visti:
                raise XlsError("La catena DIFAT si ripiega su se stessa: file rovinato.")
            visti.add(settore)
            valori = self._interi_del_settore(settore)
            elenco.extend(n for n in valori[:-1] if n <= _MASSIMO_SETTORE_REGOLARE)
            settore = valori[-1]

        if 0 < numero_fat <= len(elenco):
            elenco = elenco[:numero_fat]
        fat: list[int] = []
        for numero in elenco:
            fat.extend(self._interi_del_settore(numero))
        if not fat:
            raise XlsError("Il file non ha la mappa dei settori: è rovinato.")
        return fat

    def _costruisci_mini_fat(self, primo: int, numero: int) -> list[int]:
        if primo > _MASSIMO_SETTORE_REGOLARE or numero == 0:
            return []
        mini_fat: list[int] = []
        for settore in self._catena(primo, self._fat, "dei settori"):
            mini_fat.extend(self._interi_del_settore(settore))
        return mini_fat

    def _costruisci_mini_stream(self) -> bytes:
        for _nome, tipo, settore, dimensione in self._voci:
            if tipo == _VOCE_RADICE:
                if dimensione == 0 or settore > _MASSIMO_SETTORE_REGOLARE:
                    return b""
                return self._flusso_grande(settore, dimensione)
        return b""

    # -- stream reading --------------------------------------------------------

    def _flusso_grande(self, primo: int, dimensione: int) -> bytes:
        pezzi = [self._settore(numero) for numero in self._catena(primo, self._fat, "dei settori")]
        crudo = b"".join(pezzi)
        return crudo[:dimensione] if dimensione else crudo

    def _flusso_mini(self, primo: int, dimensione: int) -> bytes:
        pezzi = []
        for numero in self._catena(primo, self._mini_fat, "dei mini settori"):
            inizio = numero * self._dimensione_mini
            pezzi.append(self._mini_stream[inizio : inizio + self._dimensione_mini])
        crudo = b"".join(pezzi)
        return crudo[:dimensione] if dimensione else crudo

    def _leggi_direttorio(self, primo: int, versione_maggiore: int) -> list[tuple[str, int, int, int]]:
        crudo = self._flusso_grande(primo, 0)
        voci: list[tuple[str, int, int, int]] = []
        for inizio in range(0, len(crudo) - 127, 128):
            blocco = crudo[inizio : inizio + 128]
            tipo = blocco[66]
            if tipo not in (1, _VOCE_FLUSSO, _VOCE_RADICE):
                continue
            (lunghezza_nome,) = struct.unpack_from("<H", blocco, 64)
            # Length is in bytes and includes the trailing null terminator.
            fine_nome = max(0, min(64, lunghezza_nome - 2))
            nome = blocco[:fine_nome].decode("utf-16-le", "replace")
            (settore, dimensione) = struct.unpack_from("<IQ", blocco, 116)
            if versione_maggiore < 4:
                # In version-3 files the high half of the size isn't
                # reliable; Excel doesn't always zero it.
                dimensione &= 0xFFFFFFFF
            voci.append((nome, tipo, settore, dimensione))
        if not voci:
            raise XlsError("Il file non ha l'elenco dei suoi contenuti: è rovinato.")
        return voci

    def flusso(self, *nomi_ammessi: str) -> bytes:
        cercati = {nome.casefold() for nome in nomi_ammessi}
        for nome, tipo, settore, dimensione in self._voci:
            if tipo != _VOCE_FLUSSO or nome.casefold() not in cercati:
                continue
            if dimensione == 0:
                return b""
            if dimensione < self._soglia_mini:
                return self._flusso_mini(settore, dimensione)
            return self._flusso_grande(settore, dimensione)
        raise XlsError(
            "Dentro il file non c'è il libro di lavoro di Excel: "
            "forse è un documento di un altro programma."
        )

    # -- byte offsets within the file -----------------------------------------

    def _segmenti_grandi(self, primo: int, dimensione: int) -> list[tuple[int, int]]:
        segmenti: list[tuple[int, int]] = []
        restanti = dimensione
        for numero in self._catena(primo, self._fat, "dei settori"):
            if restanti <= 0:
                break
            quanti = min(self._dimensione_settore, restanti)
            segmenti.append(((numero + 1) * self._dimensione_settore, quanti))
            restanti -= quanti
        return segmenti

    def _segmenti_mini(self, primo: int, dimensione: int) -> list[tuple[int, int]]:
        # The mini-stream lives inside the root entry's large sectors; a
        # mini sector always fits entirely within one large sector because
        # the large sector size is a multiple of the mini one.
        radice = next(
            (voce for voce in self._voci if voce[1] == _VOCE_RADICE),
            None,
        )
        if radice is None:
            raise XlsError("Il file non ha la voce radice: non si può modificare.")
        grandi = self._segmenti_grandi(radice[2], radice[3])
        segmenti: list[tuple[int, int]] = []
        restanti = dimensione
        for numero in self._catena(primo, self._mini_fat, "dei mini settori"):
            if restanti <= 0:
                break
            quanti = min(self._dimensione_mini, restanti)
            posizione = numero * self._dimensione_mini
            for inizio, lunghezza in grandi:
                if posizione < lunghezza:
                    segmenti.append((inizio + posizione, quanti))
                    break
                posizione -= lunghezza
            else:
                raise XlsError("Il mini flusso indica una posizione che non esiste nel file.")
            restanti -= quanti
        return segmenti

    def segmenti(self, *nomi_ammessi: str) -> list[tuple[int, int]]:
        """Byte offsets of the stream's data within the file: `(offset, length)`.

        Needed to write in place: workbook bytes aren't contiguous, they're
        scattered across sectors, so an in-place edit needs this map to land
        in the right spot. The lengths sum to the stream's declared size,
        which callers can use to sanity-check the map before writing.
        """

        cercati = {nome.casefold() for nome in nomi_ammessi}
        for nome, tipo, settore, dimensione in self._voci:
            if tipo != _VOCE_FLUSSO or nome.casefold() not in cercati:
                continue
            if dimensione == 0:
                return []
            if dimensione < self._soglia_mini:
                return self._segmenti_mini(settore, dimensione)
            return self._segmenti_grandi(settore, dimensione)
        raise XlsError(
            "Dentro il file non c'è il libro di lavoro di Excel: "
            "forse è un documento di un altro programma."
        )


# ---------------------------------------------------------------------------
# Layer 2: BIFF8 records
# ---------------------------------------------------------------------------

_BOF = 0x0809
_EOF = 0x000A
_CONTINUE = 0x003C
_FILEPASS = 0x002F
_BOUNDSHEET = 0x0085
_SST = 0x00FC
_FONT = 0x0031
_XF = 0x00E0

_LABELSST = 0x00FD
_LABEL = 0x0204
_RSTRING = 0x00D6
_NUMBER = 0x0203
_RK = 0x027E
_RK_ALTERNATIVO = 0x007E
_MULRK = 0x00BD
_BLANK = 0x0201
_MULBLANK = 0x00BE
_BOOLERR = 0x0205
_FORMULA = 0x0006
_FORMULA_ALTERNATIVO = 0x0406
_STRING = 0x0207

_TIPO_FOGLIO_DATI = 0x00

# Error codes Excel stores as a single byte, mapped to the text the user
# sees in the cell.
_ERRORI = {
    0x00: "#NULL!",
    0x07: "#DIV/0!",
    0x0F: "#VALUE!",
    0x17: "#REF!",
    0x1D: "#NAME?",
    0x24: "#NUM!",
    0x2A: "#N/A",
}


class _ScorriRecord:
    """Walks the stream's records: (code, length, data) one after another."""

    def __init__(self, flusso: bytes, posizione: int = 0) -> None:
        self._flusso = flusso
        self.posizione = posizione

    def prossimo(self) -> tuple[int, bytes] | None:
        if self.posizione + 4 > len(self._flusso):
            return None
        (codice, lunghezza) = struct.unpack_from("<HH", self._flusso, self.posizione)
        inizio = self.posizione + 4
        fine = inizio + lunghezza
        if fine > len(self._flusso):
            raise XlsError("Il file finisce a metà di un record: è incompleto o rovinato.")
        self.posizione = fine
        return codice, self._flusso[inizio:fine]

    def codice_in_vista(self) -> int | None:
        if self.posizione + 4 > len(self._flusso):
            return None
        return struct.unpack_from("<H", self._flusso, self.posizione)[0]


def _numero(valore: float) -> float | int:
    """Collapse whole-number floats down to int.

    Codes, pieces-per-carton and order quantities are integers and must stay
    that way; the `.xlsx` reader returns the same type, so values from both
    readers compare cleanly without a stray ".0".
    """
    if not math.isfinite(valore):
        return valore
    intero = int(valore)
    return intero if intero == valore else valore


def _decodifica_rk(rk: int) -> float | int:
    """Decode Excel's 4-byte packed number (the four RK cases).

    Two trailing bits decide everything: one picks integer vs. float, the
    other whether to divide by 100. Getting a case wrong doesn't crash the
    read, it silently produces a price 100x too large, so all four cases
    are handled explicitly.
    """
    if rk & 0x02:
        # 30-bit signed integer.
        valore: float = rk >> 2
        if valore >= 1 << 29:
            valore -= 1 << 30
    else:
        # High 30 bits of an IEEE double; Excel drops the low 34 bits
        # because they're zero.
        (valore,) = struct.unpack("<d", struct.pack("<Q", (rk & 0xFFFFFFFC) << 32))
    if rk & 0x01:
        valore = valore / 100.0
    return _numero(valore)


def _testo_da_caratteri(dati: bytes, offset: int, quanti: int, bandiere: int) -> tuple[str, int]:
    """Read a BIFF8 string's characters and return where it ends.

    The flag byte says whether the text is packed one byte per character
    (the low byte of UTF-16, i.e. latin-1) or full two-byte UTF-16.
    """
    if bandiere & 0x08:  # rich-text run formatting
        (numero_tratti,) = struct.unpack_from("<H", dati, offset)
        offset += 2
    else:
        numero_tratti = 0
    if bandiere & 0x04:  # phonetic (Asian) text, skipped
        (lunghezza_fonetica,) = struct.unpack_from("<I", dati, offset)
        offset += 4
    else:
        lunghezza_fonetica = 0

    if bandiere & 0x01:
        grezzo = dati[offset : offset + 2 * quanti]
        offset += 2 * quanti
        testo = grezzo.decode("utf-16-le", "replace")
    else:
        grezzo = dati[offset : offset + quanti]
        offset += quanti
        testo = grezzo.decode("latin-1")
    offset += 4 * numero_tratti + lunghezza_fonetica
    return testo, offset


def _stringa_breve(dati: bytes, offset: int) -> str:
    """String with a 1-byte length counter, used for sheet names."""
    quanti = dati[offset]
    bandiere = dati[offset + 1]
    testo, _fine = _testo_da_caratteri(dati, offset + 2, quanti, bandiere)
    return testo


def _stringa_lunga(dati: bytes, offset: int) -> str:
    """String with a 2-byte length counter, used by LABEL, STRING, etc."""
    (quanti,) = struct.unpack_from("<H", dati, offset)
    bandiere = dati[offset + 2]
    testo, _fine = _testo_da_caratteri(dati, offset + 3, quanti, bandiere)
    return testo


def _leggi_sst(blocchi: list[bytes]) -> list[str]:
    """Reassemble the shared-string table split across CONTINUE records.

    The table rarely fits in one record, so Excel splits it, possibly mid
    string; each continuation restarts with its own flag byte, which can
    switch the encoding (1-byte vs. 2-byte) partway through the same
    string. Getting this wrong doesn't fail loudly: the text turns to
    garbage only near the end of the table, where it's easy to miss.
    """
    if not blocchi:
        return []
    primo = blocchi[0]
    if len(primo) < 8:
        raise XlsError("La tabella dei testi del file è troncata.")
    (_totali, unici) = struct.unpack_from("<Ii", primo, 0)
    if unici < 0:
        raise XlsError("La tabella dei testi del file dichiara un numero di voci impossibile.")

    indice_blocco = 0
    posizione = 8

    def blocco_corrente() -> bytes:
        return blocchi[indice_blocco]

    def avanza_se_finito() -> None:
        # Move to the next block once the current one is exhausted; no flag
        # byte to consume here, a new string starts with its own header.
        nonlocal indice_blocco, posizione
        while indice_blocco < len(blocchi) - 1 and posizione >= len(blocchi[indice_blocco]):
            indice_blocco += 1
            posizione = 0

    def leggi(quanti: int) -> bytes:
        # Cross-block read, used for headers and trailing data (rich-text
        # runs, phonetic text), which carry no flag byte of their own.
        nonlocal indice_blocco, posizione
        raccolti = bytearray()
        while quanti > 0:
            avanza_se_finito()
            corrente = blocco_corrente()
            disponibili = len(corrente) - posizione
            if disponibili <= 0:
                break
            presi = min(disponibili, quanti)
            raccolti += corrente[posizione : posizione + presi]
            posizione += presi
            quanti -= presi
        return bytes(raccolti)

    testi: list[str] = []
    for _indice in range(unici):
        # leggi() skips exhausted blocks on its own; a new string starts
        # here with its own header, no flag byte in front.
        intestazione = leggi(3)
        if len(intestazione) < 3:
            break
        (quanti,) = struct.unpack_from("<H", intestazione, 0)
        bandiere = intestazione[2]
        numero_tratti = 0
        lunghezza_fonetica = 0
        if bandiere & 0x08:
            coda = leggi(2)
            (numero_tratti,) = struct.unpack("<H", coda) if len(coda) == 2 else (0,)
        if bandiere & 0x04:
            coda = leggi(4)
            (lunghezza_fonetica,) = struct.unpack("<I", coda) if len(coda) == 4 else (0,)

        pezzi: list[str] = []
        mancanti = quanti
        alto = bool(bandiere & 0x01)
        while mancanti > 0:
            corrente = blocco_corrente()
            disponibili = len(corrente) - posizione
            if disponibili > 0:
                if alto:
                    prendibili = min(disponibili // 2, mancanti)
                    if prendibili:
                        fine = posizione + 2 * prendibili
                        pezzi.append(corrente[posizione:fine].decode("utf-16-le", "replace"))
                        posizione = fine
                else:
                    prendibili = min(disponibili, mancanti)
                    if prendibili:
                        fine = posizione + prendibili
                        pezzi.append(corrente[posizione:fine].decode("latin-1"))
                        posizione = fine
                mancanti -= prendibili
                if mancanti == 0:
                    break
            if indice_blocco >= len(blocchi) - 1:
                break
            # Moving to the string's next chunk: the first byte isn't text,
            # it's the flag for how the remaining characters are encoded,
            # which can differ from the string's initial flag.
            indice_blocco += 1
            bandiere_nuove = blocchi[indice_blocco][:1]
            if not bandiere_nuove:
                posizione = 0
                break
            alto = bool(bandiere_nuove[0] & 0x01)
            posizione = 1

        if numero_tratti:
            leggi(4 * numero_tratti)
        if lunghezza_fonetica:
            leggi(lunghezza_fonetica)
        testi.append("".join(pezzi))
    return testi


class _Formati:
    """Maps a cell to bold: cell -> XF index -> XF record -> font."""

    def __init__(self) -> None:
        self._grassetto_font: list[bool] = []
        self._font_di_xf: list[int] = []

    def aggiungi_font(self, dati: bytes) -> None:
        # Bold is carried by font weight: 400 is regular, 700 is bold.
        # Older BIFF also had a dedicated bold bit in the options field,
        # but since BIFF5 it's declared unused and other .xls writers may
        # leave it off, so weight is authoritative.
        peso = struct.unpack_from("<H", dati, 6)[0] if len(dati) >= 8 else 400
        if len(self._grassetto_font) == 4:
            # Excel never uses font index 4, it jumps from 3 to 5. Insert a
            # placeholder, otherwise every later font would be off by one
            # and bold would land on the wrong cells.
            self._grassetto_font.append(False)
        self._grassetto_font.append(peso >= 700)

    def aggiungi_xf(self, dati: bytes) -> None:
        indice_font = struct.unpack_from("<H", dati, 0)[0] if len(dati) >= 2 else 0
        self._font_di_xf.append(indice_font)

    def grassetto(self, indice_xf: int) -> bool:
        if not 0 <= indice_xf < len(self._font_di_xf):
            return False
        indice_font = self._font_di_xf[indice_xf]
        if not 0 <= indice_font < len(self._grassetto_font):
            return False
        return self._grassetto_font[indice_font]


@dataclass
class _Globali:
    fogli: list[tuple[str, int, int]] = field(default_factory=list)
    testi: list[str] = field(default_factory=list)
    formati: _Formati = field(default_factory=_Formati)


def _leggi_globali(scorri: _ScorriRecord) -> _Globali:
    globali = _Globali()
    primo = scorri.prossimo()
    if primo is None or primo[0] != _BOF:
        raise XlsError("Il libro di lavoro non comincia come dovrebbe: file rovinato.")
    (versione,) = struct.unpack_from("<H", primo[1], 0) if len(primo[1]) >= 2 else (0,)
    if versione and versione < 0x0600:
        raise XlsError(
            "Il file è in un formato Excel più vecchio del 97: "
            "va riaperto e salvato in formato Excel 97-2003."
        )

    while True:
        record = scorri.prossimo()
        if record is None:
            break
        codice, dati = record
        if codice == _EOF:
            break
        if codice == _FILEPASS:
            raise XlsError("Il file è protetto da password: va salvato senza protezione.")
        if codice == _BOUNDSHEET:
            (posizione,) = struct.unpack_from("<I", dati, 0)
            tipo = dati[5]
            globali.fogli.append((_stringa_breve(dati, 6), posizione, tipo))
        elif codice == _FONT:
            globali.formati.aggiungi_font(dati)
        elif codice == _XF:
            globali.formati.aggiungi_xf(dati)
        elif codice == _SST:
            blocchi = [dati]
            while scorri.codice_in_vista() == _CONTINUE:
                successivo = scorri.prossimo()
                assert successivo is not None
                blocchi.append(successivo[1])
            globali.testi = _leggi_sst(blocchi)
    return globali


def _leggi_foglio(scorri: _ScorriRecord, globali: _Globali, nome: str) -> XlsSheet:
    celle: dict[tuple[int, int], tuple[object, bool]] = {}
    ultima_riga = -1
    ultima_colonna = -1
    formula_in_attesa: tuple[int, int, bool] | None = None
    formati = globali.formati
    testi = globali.testi

    def segna(riga: int, colonna: int, valore: object, indice_xf: int) -> None:
        nonlocal ultima_riga, ultima_colonna
        celle[(riga, colonna)] = (valore, formati.grassetto(indice_xf))
        if riga > ultima_riga:
            ultima_riga = riga
        if colonna > ultima_colonna:
            ultima_colonna = colonna

    while True:
        record = scorri.prossimo()
        if record is None:
            break
        codice, dati = record
        if codice == _EOF:
            break

        if codice == _LABELSST:
            (riga, colonna, indice_xf, indice_testo) = struct.unpack_from("<HHHI", dati, 0)
            testo = testi[indice_testo] if 0 <= indice_testo < len(testi) else ""
            segna(riga, colonna, testo, indice_xf)
        elif codice in (_LABEL, _RSTRING):
            (riga, colonna, indice_xf) = struct.unpack_from("<HHH", dati, 0)
            segna(riga, colonna, _stringa_lunga(dati, 6), indice_xf)
        elif codice == _NUMBER:
            (riga, colonna, indice_xf, valore) = struct.unpack_from("<HHHd", dati, 0)
            segna(riga, colonna, _numero(valore), indice_xf)
        elif codice in (_RK, _RK_ALTERNATIVO):
            (riga, colonna, indice_xf, grezzo) = struct.unpack_from("<HHHI", dati, 0)
            segna(riga, colonna, _decodifica_rk(grezzo), indice_xf)
        elif codice == _MULRK:
            # A single record describing several adjacent cells in the same
            # row; skipping it loses whole rows of numbers.
            (riga, prima_colonna) = struct.unpack_from("<HH", dati, 0)
            quante = (len(dati) - 6) // 6
            for passo in range(quante):
                (indice_xf, grezzo) = struct.unpack_from("<HI", dati, 4 + 6 * passo)
                segna(riga, prima_colonna + passo, _decodifica_rk(grezzo), indice_xf)
        elif codice == _BLANK:
            (riga, colonna, indice_xf) = struct.unpack_from("<HHH", dati, 0)
            segna(riga, colonna, None, indice_xf)
        elif codice == _MULBLANK:
            (riga, prima_colonna) = struct.unpack_from("<HH", dati, 0)
            quante = (len(dati) - 6) // 2
            for passo in range(quante):
                (indice_xf,) = struct.unpack_from("<H", dati, 4 + 2 * passo)
                segna(riga, prima_colonna + passo, None, indice_xf)
        elif codice == _BOOLERR:
            (riga, colonna, indice_xf, valore, e_errore) = struct.unpack_from("<HHHBB", dati, 0)
            contenuto: object = _ERRORI.get(valore, "#N/D") if e_errore else bool(valore)
            segna(riga, colonna, contenuto, indice_xf)
        elif codice in (_FORMULA, _FORMULA_ALTERNATIVO):
            (riga, colonna, indice_xf) = struct.unpack_from("<HHH", dati, 0)
            memorizzato = dati[6:14]
            if len(memorizzato) == 8 and memorizzato[6:8] == b"\xff\xff":
                # Result isn't a number; the first byte says what it is.
                specie = memorizzato[0]
                if specie == 0:
                    # Text: carried in the STRING record right after this one.
                    formula_in_attesa = (riga, colonna, formati.grassetto(indice_xf))
                    segna(riga, colonna, "", indice_xf)
                elif specie == 1:
                    segna(riga, colonna, bool(memorizzato[2]), indice_xf)
                elif specie == 2:
                    segna(riga, colonna, _ERRORI.get(memorizzato[2], "#N/D"), indice_xf)
                else:
                    segna(riga, colonna, None, indice_xf)
            else:
                (valore,) = struct.unpack_from("<d", dati, 6)
                segna(riga, colonna, _numero(valore), indice_xf)
        elif codice == _STRING:
            if formula_in_attesa is not None:
                (riga, colonna, grassetto) = formula_in_attesa
                celle[(riga, colonna)] = (_stringa_lunga(dati, 0), grassetto)
                formula_in_attesa = None

    righe: list[list[tuple[object, bool]]] = []
    for numero_riga in range(ultima_riga + 1):
        righe.append([_CELLA_VUOTA] * (ultima_colonna + 1))
    for (riga, colonna), contenuto in celle.items():
        righe[riga][colonna] = contenuto
    return XlsSheet(name=nome, rows=righe)


def read_workbook(path: Path) -> list[XlsSheet]:
    """Read a .xls file and return its sheets, values and bold flags.

    The file is opened read-only: never rewritten or moved, since the
    original file as received from the supplier must stay untouched.
    """
    percorso = Path(path)
    try:
        dati = percorso.read_bytes()
    except OSError as errore:
        raise XlsError(f"Non riesco ad aprire il file {percorso.name}: {errore}") from errore

    contenitore = _ContenitoreOle2(dati)
    flusso = contenitore.flusso("Workbook", "Book")

    # A record cut in half makes struct raise; surface it as a corrupted
    # file rather than a raw byte-offset error.
    try:
        globali = _leggi_globali(_ScorriRecord(flusso))

        fogli: list[XlsSheet] = []
        for nome, posizione, tipo in globali.fogli:
            if tipo != _TIPO_FOGLIO_DATI:
                # Charts and macro sheets have no cell grid to read.
                continue
            if not 0 <= posizione < len(flusso):
                raise XlsError(f"Il foglio «{nome}» dichiara una posizione che non esiste nel file.")
            scorri = _ScorriRecord(flusso, posizione)
            apertura = scorri.prossimo()
            if apertura is None or apertura[0] != _BOF:
                raise XlsError(f"Il foglio «{nome}» non comincia come dovrebbe: file rovinato.")
            fogli.append(_leggi_foglio(scorri, globali, nome))
    except (struct.error, IndexError) as errore:
        raise XlsError(
            f"Il contenuto di {percorso.name} non è leggibile: il file è incompleto "
            f"o rovinato ({errore})."
        ) from errore
    return fogli


def apri_contenitore(dati: bytes) -> _ContenitoreOle2:
    """Open the OLE2 container of a `.xls` file already read into memory.

    Exposed for `app/xls_writer.py`, which writes into the same file this
    module reads: the `.xls` file-system logic lives here once, instead of
    a second copy that could drift out of sync.
    """

    return _ContenitoreOle2(dati)


def posizioni_fogli(flusso: bytes) -> list[tuple[str, int, int]]:
    """Name, stream position and type of each sheet, without reading cells."""

    return list(_leggi_globali(_ScorriRecord(flusso)).fogli)


def read_sheet_values(path: Path, sheet_index: int = 0) -> list[list[object]]:
    """Like read_workbook, but keeps only each cell's value."""
    fogli = read_workbook(path)
    if not fogli:
        raise XlsError("Il file non contiene nessun foglio di dati.")
    if not -len(fogli) <= sheet_index < len(fogli):
        raise XlsError(
            f"Il file ha {len(fogli)} fogli: il foglio numero {sheet_index} non esiste."
        )
    return [[valore for valore, _grassetto in riga] for riga in fogli[sheet_index].rows]
