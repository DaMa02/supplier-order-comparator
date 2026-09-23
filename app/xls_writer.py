#!/usr/bin/env python3
"""Compila la colonna d'ordine dentro una copia del `.xls` di Noce.

**A Noce si rimanda il LORO documento** (deciso da Daniele il 12 agosto
2026): la consegna e' il loro file con la sola colonna d'ordine riempita e
tutto il resto identico.  Non un foglio a parte, non un `.xlsx` convertito.

Questo si puo' fare senza scrivere un writer BIFF, e la ragione e' una misura:
nel listino vero la colonna d'ordine ha **17.145 celle, zero formule, tutte
codificate RK** — cioe' a lunghezza fissa di quattro byte.  Mettere una
quantita' intera al posto dello zero **non sposta un solo byte**: niente offset
da ricalcolare, nessun record da spostare, il resto del file resta identico
byte per byte.

⚠ Quella facilita' dipende da *quel* file, e il controllo va rifatto **a ogni
file**: se una settimana la colonna arrivasse con una cella vuota, una formula
o un numero a virgola mobile lungo otto byte, la patch a lunghezza fissa non
basterebbe.  Qui il controllo si fa prima di toccare un byte, e se non passa la
compilazione Noce **fallisce e lo dice** — non ripiega su un formato che il
fornitore non accetta.

Due difese che gli altri tre fornitori non hanno, e che qui costano zero perche'
il file va letto comunque:

1. **L'EAN della riga dev'essere quello del piano.**  Gli altri si fidano del
   numero di riga dopo aver verificato impronta e intestazione; per Noce si
   controlla riga per riga, e al primo disallineamento ci si ferma.  E' l'unico
   controllo che avrebbe intercettato il difetto trovato il 12 agosto — la riga
   2600 che era olio Carapelli invece del prodotto atteso.
2. **Il totale che il file calcola da solo dev'essere quello del piano.**  La
   formula della colonna `Importo` e' `quantita x prezzo x pezzi_per_cartone`,
   cioe' esattamente il modello del comparatore: se il totale non torna, non si
   consegna.
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

# Il valore piu' grande che un RK intero puo' portare: trenta bit con segno.
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
    """La compilazione Noce non si può fare, e il motivo è dichiarato."""


@dataclass
class _CellaOrdine:
    """Una cella della colonna d'ordine e dove stanno i suoi quattro byte."""

    riga: int          # numero di riga come lo vede l'utente, a partire da 1
    posizione: int     # dove sta il valore RK dentro il flusso del libro
    valore: Any        # che cosa c'e' scritto adesso


@dataclass
class _Scansione:
    celle: dict[int, _CellaOrdine] = field(default_factory=dict)
    non_rk: dict[int, str] = field(default_factory=dict)


def codifica_rk_intero(valore: int) -> int:
    """Un intero nei quattro byte di un RK.

    Due bit di coda: quello basso dice «dividi per cento» e resta a zero, quello
    sopra dice «e' un intero» e si accende.  I trenta bit alti portano il
    numero.  E' l'inverso esatto di `_decodifica_rk` del lettore, e il test lo
    verifica facendo il giro completo.
    """

    if isinstance(valore, bool) or not isinstance(valore, int):
        raise CompilazioneXlsError(f"La quantità {valore!r} non è un numero intero.")
    if not 0 <= valore <= MASSIMA_QUANTITA_RK:
        raise CompilazioneXlsError(
            f"La quantità {valore} non sta nei quattro byte della cella d'ordine."
        )
    return (valore << 2) | 0x02


def _scorri_record(flusso: bytes, posizione: int) -> Iterable[tuple[int, int, bytes]]:
    """`(codice, posizione dell'intestazione, dati)` di ogni record, da qui in poi."""

    while posizione + 4 <= len(flusso):
        (codice, lunghezza) = struct.unpack_from("<HH", flusso, posizione)
        inizio = posizione + 4
        fine = inizio + lunghezza
        if fine > len(flusso):
            raise CompilazioneXlsError("Il file finisce a metà di un record: è rovinato.")
        yield codice, posizione, flusso[inizio:fine]
        posizione = fine


def _scansiona_colonna(flusso: bytes, inizio_foglio: int, colonna: int) -> _Scansione:
    """Dove sta ogni cella della colonna d'ordine, e di che tipo e'.

    Si guarda **tutto** il foglio, non le sole righe da compilare: il controllo
    che la colonna sia ancora tutta RK ha senso solo se le ha viste tutte.
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
    """Dove sta il numero del motore di calcolo, nelle globali del libro.

    ⚠ Serve, e non e' un dettaglio: **cambiare il valore memorizzato di una
    cella non sporca le formule che la usano.**  Misurato il 12 agosto 2026 su
    questo file: dopo la patch, la quantita' in `I` era giusta e l'`Importo` in
    `K` era ancora zero, come il totale in `K4` — perche' Excel mostra il
    risultato che ha trovato scritto nel file.  Noce avrebbe ricevuto un
    ordine con le quantita' giuste e i totali a zero.

    `RECALCID` porta il numero del motore che ha calcolato l'ultima volta:
    azzerarlo dice a Excel «l'ha calcolato qualcosa di piu' vecchio di te», e
    Excel rifa' tutti i conti aprendo il file.  Sono quattro byte, e non
    spostano niente come il resto della patch.
    """

    posizione = 0
    while posizione + 4 <= len(flusso):
        (codice, lunghezza) = struct.unpack_from("<HH", flusso, posizione)
        if codice == _RECALCID and lunghezza >= 8:
            return posizione + 4 + 4
        if codice == _EOF:
            # Le globali finiscono qui: piu' avanti ci sono i fogli.
            return None
        posizione += 4 + lunghezza
    return None


def _indice_di_colonna(lettera: Any) -> int:
    """`I` diventa 8, e `9` diventa 8.  La colonna arriva dal registro.

    Il numero e' ammesso perche' la colonna dell'EAN nel registro e' dichiarata
    per **nome** di intestazione, e chi chiama la risolve leggendo la riga di
    intestazione del file: quello che arriva qui e' gia' un numero di colonna,
    a partire da uno come in Excel.
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
    """`(inizio nel flusso, inizio nel file, quanti)`, per tradurre gli offset."""

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
    """I `quanti` byte che nel FLUSSO stanno di fila, presi dove stanno nel file.

    ⚠ Nel flusso sono contigui; **nel file no**. Un `.xls` e' un contenitore
    OLE: il flusso e' spezzato in settori (qui da 512 byte) che nel file stanno
    in ordine sparso. Una cella RK a cavallo di un confine ha i suoi quattro
    byte in due settori lontani, e leggerli (o scriverli) di fila a partire dal
    primo significa prendere — o peggio, coprire — i byte di un altro settore.
    Sul listino Noce vero: 10.652 segmenti e circa cento celle a cavallo per
    ogni colonna.
    """

    return bytes(dati[_posizione_nel_file(mappa, posizione + scarto)] for scarto in range(quanti))


def _scrivi_nel_flusso(dati: bytearray, mappa: list[tuple[int, int, int]],
                       posizione: int, contenuto: bytes) -> None:
    """Scrive `contenuto` dove quei byte stanno **nel file**, uno per uno.

    Il gemello di `_leggi_dal_flusso`, e la ragione e' la stessa: qui c'era
    `dati[posizione:posizione + 4] = ...`, che scrive quattro byte di fila a
    partire da un offset tradotto per il PRIMO. Per una cella a cavallo di due
    settori quei quattro byte finivano sopra dati di un altro settore, e il
    documento che si manda al fornitore usciva rotto — Excel si rifiutava di
    aprirlo (difetto del 26 agosto 2026).
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
    """Guarda la colonna d'ordine **prima** di scriverci, e dice che cosa ha visto.

    E' il controllo che il piano chiede a ogni file: quante celle, quante RK,
    quante di altro tipo.  Se non sono tutte RK la patch a lunghezza fissa non
    e' applicabile, e chi chiama deve fermarsi invece di scrivere qualcosa che
    somiglia a un ordine.
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
        # ⚠ Una colonna d'ordine con ZERO celle scrivibili non e' compilabile:
        # e' una colonna che non c'e'. `not altre` diceva «compilabile» anche
        # li', il pre-volo non trovava niente da segnalare, e la compilazione
        # falliva piu' avanti con «Il piano indica N righe che nel listino non
        # hanno una cella d'ordine» — un ripiego che sembrava un dato vero
        # (revisione del 14 agosto 2026).
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
    """Scrive le quantita' nella colonna d'ordine di una **copia** del file.

    L'originale non si tocca mai: si legge, si modificano i byte in memoria e si
    scrive la copia.  Restituisce che cosa e' stato fatto, perche' l'audit della
    compilazione deve poterlo raccontare senza riaprire il file.
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

    # 1. La colonna dev'essere ancora tutta RK, dalla prima riga di dati in
    #    giu'.  Il controllo si rifa' a ogni file.
    #
    #    ⚠ Fino al 6 settembre 2026 ci si fermava all'ultima riga che il piano
    #    tocca, e piu' sotto poteva restare una formula: l'azzeramento la salta
    #    — non e' un numero a lunghezza fissa e non ha una quantita' da
    #    azzerare — e finisce intatta nella copia, che Excel ricalcola
    #    all'apertura.  E' R7, lo stesso buco dell'altro scrittore.  Il
    #    controllo preventivo (`controlla_colonna_ordine`) tutta la colonna la
    #    guardava gia': adesso i due dicono la stessa cosa.
    fuori = {riga: tipo for riga, tipo in scansione.non_rk.items() if riga >= prima_riga}
    if fuori:
        esempi = ", ".join(f"riga {riga} ({tipo})" for riga, tipo in sorted(fuori.items())[:5])
        raise CompilazioneXlsError(
            f"La colonna d'ordine {colonna_ordine.upper()} di «{origine.name}» non è più "
            f"tutta fatta di numeri a lunghezza fissa: {len(fuori)} celle sono di altro tipo "
            f"({esempi}). L'ordine Noce non viene compilato."
        )

    # 2. Ogni riga del piano dev'essere una cella d'ordine vera.
    mancanti = sorted(riga for riga in righe if riga not in scansione.celle)
    if mancanti:
        raise CompilazioneXlsError(
            f"Il piano indica {len(mancanti)} righe che nel listino non hanno una cella "
            f"d'ordine (per esempio la {mancanti[0]}). L'ordine Noce non viene compilato."
        )

    # 3. L'EAN della riga dev'essere quello del piano.  Costa una lettura, e il
    #    file va letto comunque: e' l'unico controllo che avrebbe intercettato
    #    il difetto del 12 agosto.
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
                # ⚠ Il piano non porta l'EAN per questa riga.  Se **nel listino
                # l'EAN c'e'**, la riga e' stata scelta senza il dato che la
                # identifica: resterebbe in piedi il solo numero di riga, cioe'
                # esattamente quello che il 12 agosto 2026 ha mandato l'ordine
                # sulla riga 2600 (olio Carapelli invece del prodotto atteso).
                # Se invece nemmeno il listino ha l'EAN li' — un espositore, una
                # riga di servizio — non c'e' niente da confrontare e si passa.
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

    # 4. Prima si azzera quello che c'era gia' nella colonna d'ordine, e poi si
    #    scrive il piano.  ⚠ Questo pezzo mancava, e i due scrittori facevano
    #    due cose diverse: `write_supplier_orders.mjs` azzera le quantita'
    #    preesistenti — «senza, si spedirebbero righe fantasma» — e qui si
    #    scrivevano solo le righe del piano, lasciando tutto il resto com'era.
    #    Il caso che morde e' quello che capita: come origine finisce la copia
    #    compilata della settimana prima, e Noce riceve anche le sue righe
    #    mentre gli altri fornitori no.  Si toccano soltanto le celle RK della
    #    colonna d'ordine dalla prima riga di dati in giu': una cella che non e'
    #    un numero a lunghezza fissa non ha una quantita' da azzerare, ed e' la
    #    stessa regola dell'altro scrittore — **una quantita' e' un numero**.
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

    # 5. E si dice a Excel di rifare i conti aprendo il file: senza, le
    #    quantita' sarebbero giuste e i totali fermi a zero.
    posizione_recalcid = _posizione_recalcid(flusso)
    ricalcolo_forzato = posizione_recalcid is not None
    if ricalcolo_forzato:
        _scrivi_nel_flusso(dati, mappa, posizione_recalcid, struct.pack("<I", 0))

    # ⚠ Temporaneo, `fsync` e `os.replace`, come le cinque memorie: qui c'era un
    # `write_bytes` diretto, cioe' l'unico posto del programma che pubblica un
    # documento aprendo e troncando il file finale.  Disco pieno o processo
    # ucciso a meta' lasciavano al suo posto un `.xls` monco — e se la
    # destinazione esisteva gia', al posto della copia buona di prima.  Il
    # documento che va a Noce merita la stessa disciplina di `state.json`.
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
        # Quando `RECALCID` non c'e' affatto, Excel ricalcola comunque
        # all'apertura: e' l'assenza del numero a dirgli che non sa chi ha
        # fatto i conti l'ultima volta.
        "ricalcolo_forzato": ricalcolo_forzato,
        "byte_origine": len(dati),
        "byte_copia": destinazione.stat().st_size,
    }
