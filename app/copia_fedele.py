#!/usr/bin/env python3
"""La copia che si consegna al fornitore dev'essere il suo listino, non un'altra cosa.

PERCHE' ESISTE QUESTO MODULO
Il 12 agosto 2026 la copia LARICE consegnabile aveva **34 celle della colonna
EAN** con scritto il testo «1235» al posto del nulla, e aveva perso **641 titoli
di sezione** dalla colonna d'ordine.  Nessuno se n'era accorto: il writer aveva
detto che era andato tutto bene, perche' guardava soltanto le celle che voleva
scrivere.

La lezione non e' «correggi quei due difetti».  E' che **la fedelta' della copia
non puo' dipendere dalla buona condotta della libreria** che la scrive: la
libreria importa il documento, se lo ricostruisce dentro e lo riscrive da capo,
e qualunque cosa perda per strada esce lo stesso dalla porta.  Serve una prova,
sempre, su ogni compilazione: si riaprono i due file e si confrontano **cella
per cella**.

COME SI LEGGONO I DUE FILE
Per intero, mai in `read_only`.  La modalita' veloce di openpyxl si fida
dell'elemento `<dimension>` e dell'ordine delle righe nel file: un listino
lecito con una `<dimension>` prudente, o con le righe scritte fuori ordine,
farebbe rifiutare una copia perfetta accusando celle innocenti (misurato dalla
revisione avversariale del 13 agosto 2026).  Il carico completo ignora la
dichiarazione e mette ogni cella al suo numero di riga vero.  Costo misurato
sui listini veri: meno di un secondo a libro.

LE UNICHE DIFFERENZE AMMESSE
Solo dentro la colonna d'ordine, dalla prima riga di dati in giu', e solo di
due specie:

1. **la quantita' del piano** nelle righe che il piano tocca;
2. **l'azzeramento di una quantita' che c'era gia'** — senza, si spedirebbero
   righe fantasma ordinate la settimana scorsa.

Tutto il resto no: nemmeno una cella.  Un titolo di sezione cancellato, un EAN
che cambia, un prezzo che si sposta, un foglio che sparisce → la copia di
**quel** fornitore non si consegna, e il programma dice perche'.

E QUELLO CHE IL PIANO CHIEDE DEV'ESSERCI (il difetto misurato il 14/8/2026)
Guardare solo le differenze risponde a «c'e' qualcosa di troppo?» e lascia
scoperta la domanda gemella: «c'e' tutto quello che serve?».  Una riga d'ordine
che il writer **non scrive** non e' una differenza — la cella della copia resta
identica all'originale — quindi nessun controllo la incontrava e il verdetto era
«fedele».  La copia partiva, con un ordine in meno dentro:

    copia completa            fedele=True  ammesse=3  rifiutate=0  (piano: 3 righe)
    copia senza la riga 3     fedele=True  ammesse=2  rifiutate=0  (piano: 3 righe)

Il piano e' il contratto: ogni riga che `quantita` chiede dev'essere **nella
copia**, con il numero che il piano dice.  Non «dev'essere cambiata»: se il
listino di partenza portava gia' quel numero — l'ordine della settimana scorsa,
identico a quello di questa — la copia e' giusta e non c'e' nessuna differenza
da contare.  Quello che conta e' il valore che il fornitore legge.

Sono due guasti diversi e il programma li dice diversi: «2 celle cambiate fuori
dalla colonna d'ordine» e «manca 1 delle 3 righe d'ordine richieste» mandano a
cercare in due posti opposti.

⚠ Una quantita' e' un numero.  E' la stessa regola che `app/xls_writer.py`
applica al `.xls` di Noce, ed e' quella che distingue una quantita' da
azzerare (`12`) da un titolo di sezione da lasciare stare
(«DENT. E SPAZZ. AQUAFRISK OPPORTUNITA'»).

IL FORMATO NUMERICO ENTRA NEL CONFRONTO, DOVE LA COPIA MOSTRA UN VALORE
Il fornitore non legge la memoria del file: legge il numero **mostrato**, e il
formato decide come si mostra.  Un prezzo `1,75` con formato `0` si legge «2»;
una quantita' con formato `;;;` si legge vuota.  La revisione avversariale del
13 agosto 2026 ha costruito entrambe le manomissioni e la prima versione della
guardia le prometteva al fornitore senza dire niente.  Percio': dove la copia
porta un valore visibile, il suo formato numerico dev'essere quello del listino
di partenza.  Dove la copia e' vuota il formato non mostra nulla, e una
differenza li' non ferma un ordine giusto.

CHE COSA QUESTO CONFRONTO **NON** GUARDA (dichiarato, misurato il 13/8/2026)
L'aspetto che non cambia i valori mostrati: colori e caratteri, larghezze di
colonna, celle unite, immagini, collegamenti ipertestuali, filtri automatici,
impostazioni di stampa, proprieta' del documento, fogli nascosti.  E i
**risultati in cache delle formule**: le formule si confrontano come testo
(`data_only=False`), e la libreria i risultati li ricalcola comunque
all'esportazione (verificato sul BETULLA vero).  La libreria rimaneggia queste
parti — la copia LARICE pesa 447 KB contro i 521 KB dell'originale — e
distinguere una perdita vera da una normalizzazione innocente richiederebbe di
leggere l'OpenXML a mano.  Qui si difende quello che il fornitore legge:
i numeri, i testi e la loro veste numerica.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# Quante differenze si nominano nel messaggio prima di limitarsi a contarle: un
# avviso lungo una pagina non lo legge nessuno.
ESEMPI_NEL_MESSAGGIO = 3

# Il formato numerico delle celle che non ne dichiarano uno.
FORMATO_PREDEFINITO = "General"


class ConfrontoImpossibile(Exception):
    """I due documenti non si riescono nemmeno ad aprire per confrontarli."""


@dataclass
class EsitoFedelta:
    """Che cosa e' stato confrontato e che cosa non torna.

    Due misure separate, perche' sono due guasti separati: `quante_rifiutate`
    conta quello che nella copia c'e' **di troppo** (celle cambiate dove non si
    poteva), `righe_mancanti` elenca quello che **manca** (righe che il piano
    chiedeva e che la copia non porta).  Una copia si consegna solo se sono
    vuote tutte e due.
    """

    celle_confrontate: int = 0
    differenze_ammesse: int = 0
    quante_rifiutate: int = 0
    esempi_rifiutati: list[str] = field(default_factory=list)
    # Quante righe il piano chiedeva sul foglio dell'ordine, e quali di quelle
    # la copia non porta.  Numeri di riga come li vede l'utente.
    righe_richieste: int = 0
    righe_mancanti: list[int] = field(default_factory=list)

    @property
    def fedele(self) -> bool:
        return self.quante_rifiutate == 0 and not self.righe_mancanti


def _vuoto(valore: Any) -> bool:
    """Niente, stringa vuota e soli spazi sono la stessa cosa: una cella vuota.

    Non e' una scorciatoia: nel listino di partenza una cella puo' portare una
    *stringa condivisa vuota*, che in Excel si vede identica a una cella senza
    niente dentro, e la copia la riscrive come cella senza niente.  Chiamarla
    differenza vorrebbe dire fermare una compilazione giusta per una cosa che
    nessuno puo' vedere.  Sul listino LARICE vero sono 35 celle a compilazione:
    senza questa riga il fornitore piu' grosso non riceverebbe mai un ordine.
    """

    if valore is None:
        return True
    return isinstance(valore, str) and not valore.strip()


def _numero(valore: Any) -> float | None:
    """Il valore come numero, se lo è.  Un booleano non è una quantità."""

    if isinstance(valore, bool) or valore is None:
        return None
    if isinstance(valore, (int, float)):
        return float(valore)
    if isinstance(valore, str):
        testo = valore.strip().replace(",", ".")
        if not testo:
            return None
        try:
            return float(testo)
        except ValueError:
            return None
    return None


def lettera_di_colonna(numero: int) -> str:
    lettere = ""
    while numero > 0:
        numero, resto = divmod(numero - 1, 26)
        lettere = chr(ord("A") + resto) + lettere
    return lettere


def numero_di_colonna(lettera: str) -> int:
    testo = str(lettera or "").strip().upper()
    if not testo or not testo.isalpha():
        raise ConfrontoImpossibile(f"Colonna d'ordine non valida: {lettera!r}.")
    numero = 0
    for carattere in testo:
        numero = numero * 26 + (ord(carattere) - ord("A") + 1)
    return numero


def _mostra(valore: Any) -> str:
    if _vuoto(valore):
        return "vuota"
    testo = str(valore)
    return f"«{testo}»" if len(testo) <= 40 else f"«{testo[:40]}…»"


def confronta_copia(
    originale: Path,
    copia: Path,
    *,
    colonna_ordine: str,
    prima_riga: int,
    quantita: Mapping[int, int],
    foglio_ordine: str | None = None,
) -> EsitoFedelta:
    """Confronta la copia con il listino di partenza, cella per cella.

    `quantita` sono le righe che il piano ha chiesto di scrivere (numero di riga
    come lo vede l'utente -> colli).  E' insieme la **tolleranza** (li' una
    differenza e' ammessa) e il **contratto** (li' la quantita' dev'esserci):
    vedi `_righe_del_piano_mancanti`.  `foglio_ordine` e' il nome del foglio
    dove sta la colonna d'ordine: negli **altri** fogli non e' ammessa nessuna
    differenza, nemmeno nella colonna con la stessa lettera.
    """

    from openpyxl import load_workbook

    esito = EsitoFedelta()
    colonna = numero_di_colonna(colonna_ordine)

    try:
        libro_originale = load_workbook(originale, read_only=False, data_only=False)
    except Exception as errore:  # noqa: BLE001 - qualunque motivo, il messaggio e' quello
        raise ConfrontoImpossibile(
            f"non riesco a riaprire il listino di partenza «{Path(originale).name}» "
            f"per confrontarlo con la copia: {errore}"
        ) from errore
    try:
        try:
            libro_copia = load_workbook(copia, read_only=False, data_only=False)
        except Exception as errore:  # noqa: BLE001
            raise ConfrontoImpossibile(
                f"non riesco a riaprire la copia «{Path(copia).name}» per confrontarla "
                f"con il listino di partenza: {errore}"
            ) from errore
        try:
            nomi_originale = list(libro_originale.sheetnames)
            nomi_copia = list(libro_copia.sheetnames)
            if nomi_originale != nomi_copia:
                esito.quante_rifiutate = 1
                esito.esempi_rifiutati.append(
                    "i fogli della copia non sono quelli del listino di partenza "
                    f"({', '.join(nomi_copia) or 'nessuno'} invece di {', '.join(nomi_originale)})"
                )
                return esito

            # Il foglio dell'ordine e' quello dichiarato dalla regola; `FIRST` e
            # il nome assente vogliono dire «il primo», come per il writer.
            atteso = str(foglio_ordine or "").strip()
            if not atteso or atteso.upper() == "FIRST" or atteso not in nomi_originale:
                atteso = nomi_originale[0] if nomi_originale else ""

            for nome in nomi_originale:
                _confronta_foglio(
                    libro_originale[nome],
                    libro_copia[nome],
                    nome=nome if len(nomi_originale) > 1 else "",
                    colonna_ordine=colonna if nome == atteso else None,
                    prima_riga=prima_riga,
                    quantita=quantita,
                    esito=esito,
                )
        finally:
            libro_copia.close()
    finally:
        libro_originale.close()
    return esito


def _celle_significative(foglio: Any) -> dict[tuple[int, int], tuple[Any, str]]:
    """`{(riga, colonna): (valore, formato)}` per le celle che portano qualcosa.

    E' il carico completo a rimettere in ordine un file con le righe scritte
    fuori posto (il parser archivia ogni cella alla sua coordinata vera, e
    `iter_rows` riparte sempre dalla riga 1: misurato il 13 agosto 2026).  La
    chiave usa comunque `cell.row`, il numero dichiarato dalla cella, cosi' il
    confronto non dipende dall'ordine di lettura nemmeno se un giorno il modo
    di leggere cambia.  Una cella senza valore e con il formato predefinito non
    porta niente e non entra nella mappa.
    """

    mappa: dict[tuple[int, int], tuple[Any, str]] = {}
    for riga in foglio.iter_rows():
        for cella in riga:
            valore = cella.value
            formato = cella.number_format
            if valore is None and formato == FORMATO_PREDEFINITO:
                continue
            mappa[(cella.row, cella.column)] = (valore, formato)
    return mappa


def _confronta_foglio(
    foglio_originale: Any,
    foglio_copia: Any,
    *,
    nome: str,
    colonna_ordine: int | None,
    prima_riga: int,
    quantita: Mapping[int, int],
    esito: EsitoFedelta,
) -> None:
    originale = _celle_significative(foglio_originale)
    copia = _celle_significative(foglio_copia)
    vuota = (None, FORMATO_PREDEFINITO)
    for chiave in sorted(originale.keys() | copia.keys()):
        riga, colonna = chiave
        valore_originale, formato_originale = originale.get(chiave, vuota)
        valore_copia, formato_copia = copia.get(chiave, vuota)
        esito.celle_confrontate += 1
        if valore_originale != valore_copia and not (
            _vuoto(valore_originale) and _vuoto(valore_copia)
        ):
            if (
                colonna == colonna_ordine
                and riga >= prima_riga
                and _differenza_ammessa(valore_originale, valore_copia, quantita.get(riga))
            ):
                esito.differenze_ammesse += 1
            else:
                _rifiuta(esito, nome, colonna, riga, valore_originale, valore_copia)
                continue
        # Il formato numerico conta solo dove la copia mostra un valore: su una
        # cella vuota nessun formato mostra niente, e fermarsi li' vorrebbe dire
        # rifiutare un ordine giusto per una veste che non si vede.
        if formato_originale != formato_copia and not _vuoto(valore_copia):
            _rifiuta_formato(esito, nome, colonna, riga, formato_originale, formato_copia)

    # Fin qui si e' guardato quello che nella copia c'e' di troppo.  Adesso
    # quello che ci deve essere: il ciclo delle differenze non puo' accorgersi
    # di una quantita' **non scritta**, perche' una cella non scritta non e'
    # diversa da com'era.
    if colonna_ordine is not None:
        esito.righe_richieste += len(quantita)
        esito.righe_mancanti.extend(
            _righe_del_piano_mancanti(copia, colonna_ordine=colonna_ordine, quantita=quantita)
        )


def _quantita_del_piano_nella_cella(valore: Any, attesa: int) -> bool:
    """La cella mostra i colli che il piano chiede per quella riga.

    E' l'unica definizione di «il piano e' rispettato qui», e la usano tutti e
    due i controlli: quello che ammette la differenza mentre scorre le celle e
    quello che, alla fine, verifica che ogni riga del piano ci sia davvero.
    Una sola regola, valida per ogni fornitore.
    """

    return _numero(valore) == float(attesa)


def _righe_del_piano_mancanti(
    copia: Mapping[tuple[int, int], tuple[Any, str]],
    *,
    colonna_ordine: int,
    quantita: Mapping[int, int],
) -> list[int]:
    """Le righe che il piano chiedeva e che nella copia non ci sono.

    Si guarda il **valore che la copia porta**, non la differenza rispetto al
    listino: se il listino di partenza aveva gia' quel numero — la stessa riga
    ordinata la settimana scorsa, con gli stessi colli — la copia e' giusta pur
    non essendo cambiata, e pretendere una differenza rifiuterebbe un ordine
    corretto.

    ⚠ Quantita' zero: `quantita` arriva dal piano, e chi lo costruisce scarta
    gia' le righe con zero colli (`server.py`, «if decision["quantity"] <= 0»);
    il writer rifiuta pure lui qualunque quantita' minore di 1.  Se una riga a
    zero ci arrivasse lo stesso, qui vale la regola generale — la copia deve
    mostrare quello che il piano chiede, cioe' il numero 0 — e la copia si
    ferma invece di partire con una cella che non corrisponde al piano.  E' la
    stessa lettura che `_differenza_ammessa` da' gia' a `attesa == 0`: una
    regola sola, non due comportamenti che si contraddicono.

    L'azzeramento non c'entra e non entra nel conto: svuotare una cella che
    portava un numero e' ammesso proprio **quando il piano non chiede niente**
    per quella riga, quindi quelle righe in `quantita` non ci sono.
    """

    mancanti: list[int] = []
    for riga in sorted(quantita):
        valore, _formato = copia.get((riga, colonna_ordine), (None, FORMATO_PREDEFINITO))
        if not _quantita_del_piano_nella_cella(valore, quantita[riga]):
            mancanti.append(riga)
    return mancanti


def _differenza_ammessa(valore_originale: Any, valore_copia: Any, attesa: int | None) -> bool:
    """La quantità del piano, o l'azzeramento di una quantità che c'era già."""

    if attesa is not None:
        return _quantita_del_piano_nella_cella(valore_copia, attesa)
    # Nessuna quantita' chiesta per questa riga: allora la copia puo' solo aver
    # svuotato una cella che portava un numero.  Un titolo di sezione no.
    return _vuoto(valore_copia) and _numero(valore_originale) is not None


def _dove(nome_foglio: str, colonna: int, riga: int) -> str:
    dove = f"{lettera_di_colonna(colonna)}{riga}"
    if nome_foglio:
        dove += f" del foglio «{nome_foglio}»"
    return dove


def _rifiuta(
    esito: EsitoFedelta,
    nome_foglio: str,
    colonna: int,
    riga: int,
    valore_originale: Any,
    valore_copia: Any,
) -> None:
    esito.quante_rifiutate += 1
    if len(esito.esempi_rifiutati) >= ESEMPI_NEL_MESSAGGIO:
        # Oltre i primi esempi si continua a contare, senza scrivere altro: a
        # chi legge servono i primi casi e il totale, non l'elenco intero.
        return
    esito.esempi_rifiutati.append(
        f"{_dove(nome_foglio, colonna, riga)} nel listino di partenza è "
        f"{_mostra(valore_originale)} e nella copia è {_mostra(valore_copia)}"
    )


def _rifiuta_formato(
    esito: EsitoFedelta,
    nome_foglio: str,
    colonna: int,
    riga: int,
    formato_originale: str,
    formato_copia: str,
) -> None:
    esito.quante_rifiutate += 1
    if len(esito.esempi_rifiutati) >= ESEMPI_NEL_MESSAGGIO:
        return
    esito.esempi_rifiutati.append(
        f"{_dove(nome_foglio, colonna, riga)} ha cambiato formato numerico "
        f"(«{formato_originale}» nel listino di partenza, «{formato_copia}» nella copia): "
        "il numero mostrato non sarebbe più quello"
    )


def _elenco_di_righe(righe: list[int]) -> str:
    """«riga 7», «righe 7 e 9», «righe 7, 9 e 12, e altre 4»."""

    mostrate = [str(riga) for riga in righe[:ESEMPI_NEL_MESSAGGIO]]
    if len(mostrate) == 1:
        elenco = f"riga {mostrate[0]}"
    else:
        elenco = "righe " + ", ".join(mostrate[:-1]) + f" e {mostrate[-1]}"
    restano = len(righe) - len(mostrate)
    if not restano:
        return elenco
    return elenco + (", e un'altra" if restano == 1 else f", e altre {restano}")


def _frase_delle_righe_mancanti(fornitore: str, esito: EsitoFedelta, *, insieme: bool) -> str:
    """Il guasto opposto: non celle di troppo, ma righe d'ordine che non ci sono.

    `insieme` e' vero quando la copia ha **anche** celle cambiate fuori posto:
    allora questa frase si aggancia alla prima invece di ricominciare dal nome
    del fornitore.
    """

    quante = len(esito.righe_mancanti)
    if esito.righe_richieste == 1:
        conto = "manca l'unica riga d'ordine richiesta"
    else:
        verbo = "manca" if quante == 1 else "mancano"
        conto = f"{verbo} {quante} delle {esito.righe_richieste} righe d'ordine richieste"
    apertura = "Inoltre alla copia" if insieme else f"Alla copia per {fornitore}"
    return (
        f"{apertura} {conto}: la quantità del piano non è arrivata nella sua cella "
        f"({_elenco_di_righe(esito.righe_mancanti)})"
    )


def frase_di_rifiuto(fornitore: str, esito: EsitoFedelta) -> str:
    """Che cosa legge l'utente quando la copia non e' fedele.

    I due guasti si dicono con due frasi diverse, perche' mandano a cercare in
    due posti opposti: una cella cambiata fuori dalla colonna d'ordine e' la
    libreria che ha rovinato il listino, una riga d'ordine che non e' arrivata
    e' un ordine incompleto.  Una copia che li ha tutti e due li dice tutti e
    due.
    """

    parti: list[str] = []
    # La frase di sempre, parola per parola: la copia con celle di troppo si
    # spiega oggi come si spiegava prima.  Regge anche il caso senza guasti
    # nominati (i fogli che non coincidono contano una differenza sola).
    if esito.quante_rifiutate or not esito.righe_mancanti:
        quante = esito.quante_rifiutate
        celle = "cella è diversa" if quante == 1 else "celle sono diverse"
        testa = (
            f"La copia per {fornitore} non è fedele al listino di partenza: {quante} {celle} "
            "fuori dalle celle dell'ordine"
        )
        if esito.esempi_rifiutati:
            testa += " (" + "; ".join(esito.esempi_rifiutati) + ")"
        parti.append(testa)
    if esito.righe_mancanti:
        parti.append(_frase_delle_righe_mancanti(fornitore, esito, insieme=bool(parti)))
    return ". ".join(parti) + ". La copia non viene consegnata; il listino di partenza è rimasto invariato."
