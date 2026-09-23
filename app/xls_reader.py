"""Lettore dei file Excel 97-2003 (.xls) scritto con la sola libreria standard.

PERCHE' ESISTE QUESTO MODULO
Noce non manda piu' i dati dal sito: manda un .xls e basta, e non ha il
.xlsx.  Se questo lettore non esistesse, quel fornitore resterebbe fuori dal
confronto.  Per lo stesso motivo qui dentro non entra nessuna dipendenza
esterna e non si chiama Excel: il programma deve funzionare sul computer
dell'utente cosi' com'e'.

COSA C'E' DENTRO UN .xls: DUE STRATI, UNO DENTRO L'ALTRO
1. Il contenitore OLE2 (Compound File Binary).  E' un piccolo file system:
   intestazione, mappa dei settori (FAT), elenco dei flussi (il direttorio),
   piu' una seconda mappa in miniatura (mini-FAT) per i flussi piccoli.  Il
   libro di lavoro sta nel flusso chiamato "Workbook".
2. I record BIFF8 dentro quel flusso: una sequenza di blocchi
   (codice, lunghezza, dati) che descrivono fogli, testi condivisi, formati e
   celle.

COSA RESTITUISCE
Il valore gia' calcolato che Excel ha memorizzato nel file (non la formula) e
il grassetto del carattere.  Il grassetto non e' un vezzo estetico: sui listini
Noce il prezzo in offerta e' segnalato anche cosi' ("i prezzi offerta sono
in grassetto"), quindi buttarlo via significherebbe perdere l'informazione.

Il file di partenza viene soltanto letto: mai riscritto, mai spostato.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["XlsError", "XlsSheet", "read_workbook", "read_sheet_values"]


class XlsError(Exception):
    """Il file non si riesce a leggere, con la spiegazione del perché."""


@dataclass
class XlsSheet:
    """Un foglio del libro di lavoro.

    ``rows[r][c]`` e' sempre una coppia ``(valore, grassetto)``: le righe sono
    dense e le celle mancanti valgono ``(None, False)``, cosi' chi legge non
    deve difendersi da un IndexError a ogni accesso.
    """

    name: str
    rows: list[list[tuple[object, bool]]] = field(default_factory=list)


# La cella vuota e' un oggetto solo, condiviso da tutta la griglia: e'
# immutabile, quindi si puo' riusare senza rischi e senza sprecare memoria su
# un foglio da diciassettemila righe.
_CELLA_VUOTA: tuple[object, bool] = (None, False)


# ---------------------------------------------------------------------------
# Strato 1: il contenitore OLE2
# ---------------------------------------------------------------------------

_FIRMA_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FIRMA_ZIP = b"PK\x03\x04"

# Nella mappa dei settori i valori oltre questa soglia non sono settori veri:
# sono marcatori (fine catena, settore libero, settore di servizio).
_MASSIMO_SETTORE_REGOLARE = 0xFFFFFFFA

_VOCE_FLUSSO = 2
_VOCE_RADICE = 5


class _ContenitoreOle2:
    """Lo strato esterno del .xls: un piccolo file system dentro un file.

    Senza questo strato non si legge nemmeno un record: i dati del libro di
    lavoro non stanno in fondo al file in ordine, stanno sparsi in settori che
    solo la FAT sa rimettere in fila.
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
        # Il mini-stream e la mini-FAT servono solo per i flussi sotto la
        # soglia: il "Workbook" di un listino vero e' grande e sta nei settori
        # normali, ma i flussi piccoli esistono lo stesso e vanno letti.
        self._mini_fat = self._costruisci_mini_fat(primo_mini_fat, numero_mini_fat)
        self._mini_stream = self._costruisci_mini_stream()

    # -- lettura grezza dei settori ----------------------------------------

    def _settore(self, numero: int) -> bytes:
        inizio = (numero + 1) * self._dimensione_settore
        if inizio >= len(self._dati):
            raise XlsError("Il file indica un settore oltre la sua fine: è incompleto o rovinato.")
        blocco = self._dati[inizio : inizio + self._dimensione_settore]
        if len(blocco) < self._dimensione_settore:
            # L'ultimo settore puo' essere tagliato: si completa con zeri
            # invece di far fallire tutta la lettura per pochi byte di coda.
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

    # -- le due mappe -------------------------------------------------------

    def _costruisci_fat(self, numero_fat: int, primo_difat: int) -> list[int]:
        # I primi 109 riferimenti stanno nell'intestazione; se non bastano si
        # continua nella catena dei settori DIFAT, dove l'ultimo intero di ogni
        # settore e' il puntatore al successivo.
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

    # -- lettura dei flussi -------------------------------------------------

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
            # La lunghezza e' in byte e comprende lo zero finale del testo.
            fine_nome = max(0, min(64, lunghezza_nome - 2))
            nome = blocco[:fine_nome].decode("utf-16-le", "replace")
            (settore, dimensione) = struct.unpack_from("<IQ", blocco, 116)
            if versione_maggiore < 4:
                # Nei file versione 3 la meta' alta della dimensione non e'
                # affidabile: Excel non la azzera sempre.
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

    # -- dove stanno i byte, dentro il file --------------------------------

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
        # Il mini-stream vive dentro i settori grandi della voce radice: un
        # mini settore sta sempre **tutto** dentro un settore grande, perche' la
        # dimensione grande e' un multiplo di quella mini.
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
        """Dove stanno, dentro il file, i byte del flusso: `(posizione, quanti)`.

        Serve per scrivere **in posizione**: i byte del libro di lavoro non
        stanno in fila in fondo al file, stanno sparsi in settori, e senza
        questa mappa una modifica finirebbe nel posto sbagliato.  La somma delle
        lunghezze e' esattamente la dimensione dichiarata del flusso, quindi chi
        la usa puo' controllare di aver capito bene prima di toccare un byte.
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
# Strato 2: i record BIFF8
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

# I codici d'errore che Excel memorizza in un byte, tradotti nel testo che
# l'utente vede nella cella.
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
    """Scorre i record del flusso: (codice, lunghezza, dati) uno dopo l'altro."""

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
    """Riporta a intero i numeri che sono interi.

    I codici, i pezzi per cartone e le quantita' d'ordine sono numeri interi e
    devono restare tali; e' anche quello che restituisce il lettore dei .xlsx,
    quindi le due strade di lettura danno lo stesso tipo di dato e i confronti
    non si sporcano di ".0".
    """
    if not math.isfinite(valore):
        return valore
    intero = int(valore)
    return intero if intero == valore else valore


def _decodifica_rk(rk: int) -> float | int:
    """I quattro modi in cui Excel comprime un numero in quattro byte.

    Due bit di coda dicono tutto: uno sceglie fra intero e virgola mobile,
    l'altro dice se il numero va diviso per cento.  Sbagliarli non fa saltare
    la lettura, fa comparire prezzi cento volte piu' grandi: per questo i
    quattro casi sono scritti tutti, uno per uno.
    """
    if rk & 0x02:
        # Intero con segno su 30 bit.
        valore: float = rk >> 2
        if valore >= 1 << 29:
            valore -= 1 << 30
    else:
        # I 30 bit alti di un numero IEEE a doppia precisione; i restanti
        # 34 bit bassi Excel li butta via perche' sono zero.
        (valore,) = struct.unpack("<d", struct.pack("<Q", (rk & 0xFFFFFFFC) << 32))
    if rk & 0x01:
        valore = valore / 100.0
    return _numero(valore)


def _testo_da_caratteri(dati: bytes, offset: int, quanti: int, bandiere: int) -> tuple[str, int]:
    """Legge i caratteri di una stringa BIFF8 e dice dove finisce.

    La bandiera dice se il testo e' compresso a un byte per carattere (il byte
    basso di UTF-16, cioe' latin-1) oppure disteso in UTF-16 a due byte.
    """
    if bandiere & 0x08:  # testo con formattazioni interne
        (numero_tratti,) = struct.unpack_from("<H", dati, offset)
        offset += 2
    else:
        numero_tratti = 0
    if bandiere & 0x04:  # coda fonetica (giapponese), si salta
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
    """Stringa con il contatore su un byte: la usano i nomi dei fogli."""
    quanti = dati[offset]
    bandiere = dati[offset + 1]
    testo, _fine = _testo_da_caratteri(dati, offset + 2, quanti, bandiere)
    return testo


def _stringa_lunga(dati: bytes, offset: int) -> str:
    """Stringa con il contatore su due byte: LABEL, STRING e simili."""
    (quanti,) = struct.unpack_from("<H", dati, offset)
    bandiere = dati[offset + 2]
    testo, _fine = _testo_da_caratteri(dati, offset + 3, quanti, bandiere)
    return testo


def _leggi_sst(blocchi: list[bytes]) -> list[str]:
    """Ricompone la tabella dei testi condivisi spezzata sui record CONTINUE.

    QUI SBAGLIANO QUASI TUTTI.  La tabella non entra in un record solo, quindi
    Excel la taglia; il taglio puo' cadere in mezzo a una parola e ogni pezzo
    successivo RICOMINCIA con un byte di bandiere che puo' cambiare la
    codifica, da un byte a due byte, a meta' della stessa stringa.  Chi lo
    ignora non se ne accorge subito: il testo diventa spazzatura solo in fondo
    al file, dove nessuno guarda.
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
        # Quando un blocco e' esaurito si passa al successivo: qui non c'e'
        # nessun byte di bandiere da consumare, perche' comincia una stringa
        # nuova con la sua intestazione.
        nonlocal indice_blocco, posizione
        while indice_blocco < len(blocchi) - 1 and posizione >= len(blocchi[indice_blocco]):
            indice_blocco += 1
            posizione = 0

    def leggi(quanti: int) -> bytes:
        # Lettura che attraversa i blocchi: serve per le intestazioni e per le
        # code (formattazioni, testo fonetico), che non portano bandiere.
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
        # leggi() salta da solo i blocchi esauriti: qui comincia una stringa
        # nuova con la sua intestazione, senza byte di bandiere davanti.
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
            # Si passa al pezzo successivo della stringa: il primo byte non e'
            # testo, e' la bandiera che dice come sono scritti i caratteri che
            # restano.  Puo' essere diversa da quella di partenza.
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
    """Dalla cella al grassetto: cella -> indice XF -> record XF -> font."""

    def __init__(self) -> None:
        self._grassetto_font: list[bool] = []
        self._font_di_xf: list[int] = []

    def aggiungi_font(self, dati: bytes) -> None:
        # Il grassetto sta nel peso del carattere: 400 e' normale, 700 e'
        # grassetto.  Nei BIFF vecchi c'era anche un bit dedicato dentro le
        # opzioni, e nel listino Noce i due segnali coincidono (i font in
        # grassetto hanno peso 700 e anche quel bit acceso), ma dal BIFF5 in poi
        # il bit e' dichiarato inutilizzato e gli altri programmi che scrivono
        # .xls possono lasciarlo spento: fa fede il peso.
        peso = struct.unpack_from("<H", dati, 6)[0] if len(dati) >= 8 else 400
        if len(self._grassetto_font) == 4:
            # Excel non usa mai il font numero 4: salta da 3 a 5.  Si mette un
            # segnaposto, altrimenti tutti i font successivi risulterebbero
            # spostati di uno e il grassetto finirebbe sulle celle sbagliate.
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
            # Un record solo che descrive piu' celle affiancate della stessa
            # riga: ignorarlo vuol dire perdere righe intere di numeri.
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
                # Il risultato non e' un numero: il primo byte dice che cos'e'.
                specie = memorizzato[0]
                if specie == 0:
                    # Testo: sta nel record STRING che segue subito dopo.
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
    """Legge un .xls e restituisce i suoi fogli, valori e grassetto.

    Il file viene soltanto aperto in lettura: non si riscrive e non si sposta,
    perche' l'originale del fornitore deve restare com'e' arrivato.
    """
    percorso = Path(path)
    try:
        dati = percorso.read_bytes()
    except OSError as errore:
        raise XlsError(f"Non riesco ad aprire il file {percorso.name}: {errore}") from errore

    contenitore = _ContenitoreOle2(dati)
    flusso = contenitore.flusso("Workbook", "Book")

    # Un record tagliato a meta' fa saltare struct: l'utente deve leggere che
    # il file e' rovinato, non un messaggio in inglese sul numero di byte.
    try:
        globali = _leggi_globali(_ScorriRecord(flusso))

        fogli: list[XlsSheet] = []
        for nome, posizione, tipo in globali.fogli:
            if tipo != _TIPO_FOGLIO_DATI:
                # Grafici e fogli macro non hanno una griglia di celle da leggere.
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
    """Il contenitore OLE2 di un `.xls` gia' letto in memoria.

    Esiste per `app/xls_writer.py`, che deve scrivere dentro lo stesso file
    che questo modulo legge: il piccolo file system del `.xls` e' scritto qui
    una volta sola, e riscriverlo altrove vorrebbe dire due copie della stessa
    regola che prima o poi divergono.
    """

    return _ContenitoreOle2(dati)


def posizioni_fogli(flusso: bytes) -> list[tuple[str, int, int]]:
    """Nome, posizione nel flusso e tipo di ogni foglio, senza leggere le celle."""

    return list(_leggi_globali(_ScorriRecord(flusso)).fogli)


def read_sheet_values(path: Path, sheet_index: int = 0) -> list[list[object]]:
    """Come read_workbook, ma di ogni cella tiene soltanto il valore."""
    fogli = read_workbook(path)
    if not fogli:
        raise XlsError("Il file non contiene nessun foglio di dati.")
    if not -len(fogli) <= sheet_index < len(fogli):
        raise XlsError(
            f"Il file ha {len(fogli)} fogli: il foglio numero {sheet_index} non esiste."
        )
    return [[valore for valore, _grassetto in riga] for riga in fogli[sheet_index].rows]
