#!/usr/bin/env python3
"""Il magazzino delle conferme: quello che l'utente ha già risposto, e quando.

Il problema che risolve, con i numeri veri di questo progetto. Ogni settimana
l'export del gestionale è un file nuovo, e l'identificativo di un prodotto in
pagina è il suo **numero di riga** (`product:539`). Misurato il 15 agosto 2026
sui due export veri sul disco: dei 457 identificativi presenti in tutti e due,
**449 portano un articolo diverso** — `product:3` era un appendiabiti e adesso
è una lametta Gillardo. Una conferma legata a quell'identificativo non si
perde soltanto: si riapplica in silenzio a merce che l'utente non ha mai visto.

Qui la conferma è legata a **che cosa è l'articolo**, non a dove sta scritto:

* l'articolo del **gestionale** è `impronta_prodotto` — codice a barre e nome
  ridotto all'osso, niente riga e niente prezzo;
* l'articolo del **fornitore** è `impronta_articolo` di
  `scripts/build_review_data.py`, che esiste già e non si riscrive qui: due
  definizioni della stessa identità sono due verità sullo stesso dato, e il
  giorno in cui una cambia il magazzino smette di combaciare in silenzio.

**Questo modulo non decide niente.** Non sa che cos'è un'offerta conveniente,
non applica nessuna conferma e non scade niente da solo: ricorda quello che gli
si dice e risponde a chi chiede. Ogni regola commerciale sta fuori di qui.

⚠ **Una conferma che dura per sempre è un modo di sbagliare per sempre.** A
valle di questa memoria c'è un ordine vero: se l'utente conferma una
corrispondenza sbagliata, quella tornerebbe ogni lunedì senza che nessuno la
riguardi. Per questo ogni riga porta con sé **quando**, **quale offerta esatta**
e **perché**, niente si cancella mai davvero (`dimentica` chiude una riga, non
la butta) e `esporta` restituisce tutto in chiaro: un `.db` non si legge a
occhio, e l'utente deve poter guardare che cosa ha confermato.

Il file lo sceglie il chiamante: qui dentro non c'è nessun percorso cablato.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

# `normalized_name` e `impronta_articolo` vengono dal modulo che li ha già:
# ricopiarli qui vorrebbe dire due regole di identità da tenere allineate, e
# quella che si dimentica è sempre quella che fa perdere le conferme. È lo
# stesso aggancio che `app/server.py` usa da prima (`from build_review_data
# import impronta_articolo`).
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

# 2: le uguaglianze fra codici a barre (17 agosto 2026).
# ⚠ Il numero sale **anche per una tabella in più**, e il guardiano di
# `_prepara` continua a rifiutare un file scritto da una versione più recente.
# Sembra severo — una tabella nuova non rompe la lettura di quella vecchia — ma
# l'eccezione «le aggiunte non contano» è una regola che dovrebbe indovinare da
# sola quali cambiamenti sono innocui, e il primo che non lo è passerebbe in
# silenzio. Il prezzo, tornando indietro con la versione, è riaprire il file con
# la versione che l'ha scritto: dichiarato, e reversibile.
VERSIONE_SCHEMA = 2

# Quanto una scrittura aspetta che l'altra finisca prima di dichiarare il file
# occupato. Il servizio è multi-thread — il polling della pagina e il filo della
# pipeline scrivono dallo stesso processo, e niente vieta due processi — e senza
# questa attesa la seconda scrittura morirebbe con «database is locked» invece
# di mettersi in coda per qualche millisecondo.
ATTESA_BLOCCO_S = 10.0


class MagazzinoNonUtilizzabile(RuntimeError):
    """Il file delle conferme non si apre, o non si legge più.

    È **un'eccezione sola da intercettare**, e non un guasto silenzioso, per una
    ragione che vale la pena scrivere: rispondere «nessuna conferma» a un file
    illeggibile farebbe tornare tutte le domande senza dire perché, e l'utente
    riconfermerebbe a mano credendo che il programma non avesse mai saputo
    niente. Chi aggancia questo magazzino al servizio deve intercettarla
    **all'apertura**, in un posto solo, e decidere lì se proseguire senza
    memoria dicendolo in pagina: la decisione è sua, non di questo modulo.
    """


def impronta_prodotto(prodotto: Any) -> str:
    """Che cosa identifica l'articolo del **gestionale**, non la sua riga.

    Stesso criterio di `impronta_articolo` per il lato fornitore: dentro ci va
    l'identità e nient'altro — **codice a barre e nome normalizzato**. Fuori
    restano di proposito:

    * il **numero di riga**, che è il difetto da cui nasce tutto questo modulo
      (449 identificativi su 457 cambiano articolo fra i due export veri);
    * il **prezzo** e la quantità suggerita, che cambiano ogni settimana sullo
      stesso articolo: farli entrare vorrebbe dire rifare la domanda a ogni
      ritocco di listino, cioè insegnare a spuntare senza leggere.

    ⚠ **Il codice a barre da solo non basta**, e il nome ci sta apposta. In
    questo export gli EAN si accorciano fino a otto cifre (`59016458`) e sono
    scritti a mano: un codice ribattuto sull'articolo sbagliato aggancerebbe la
    conferma di ieri a merce diversa **in silenzio**, che è l'unico modo di
    sbagliare che questo modulo deve rendere impossibile. Con il nome dentro,
    quel caso fa scadere la conferma e la domanda torna — perdere una conferma
    costa un clic, applicarne una sbagliata costa un ordine. E costa zero:
    misurato sui due export veri, dei 90 codici presenti in tutti e due
    **nessuno** ha cambiato descrizione.

    Restituisce `""` quando non c'è né codice né nome: un articolo che non si
    identifica non si ricorda (vedi `MagazzinoConferme.ricorda`).

    ⚠ Nota per chi aggancia: due righe dello stesso export con lo stesso codice
    e lo stesso nome danno la **stessa** impronta — nell'export del 15 agosto
    succede davvero, `DIXOR POLVERE CLASSICO 40MISURINI` compare due volte. È
    voluto: sono lo stesso articolo, e la conferma data sull'una vale sull'altra.
    Il suffisso che distingue le due righe in pagina è una faccenda della pagina.
    """

    if not isinstance(prodotto, dict):
        return ""
    ean = str(prodotto.get("ean") or prodotto.get("gtin") or prodotto.get("barcode") or "").strip()
    nome = normalized_name(prodotto.get("description") or prodotto.get("name"))
    if not ean and not nome:
        return ""
    return f"{ean}|{nome}"


def codice_confrontabile(valore: Any) -> str:
    """Un codice a barre ridotto a quello che si confronta: cifre e basta.

    Nell'export del gestionale gli EAN sono scritti a mano e arrivano in tutti i
    modi: `'4009428623194'`, `4009428623194.0` quando il foglio li ha letti come
    numeri, con spazi davanti, con un apostrofo iniziale. Due scritture dello
    stesso codice devono dare la stessa chiave, altrimenti un'uguaglianza
    dichiarata lunedì non si ritrova martedì **senza dare nessun errore**.

    Non si normalizza la lunghezza: un EAN-8 e un EAN-13 restano codici diversi,
    ed è giusto — se sono lo stesso articolo lo dice un'uguaglianza, che è
    esattamente lo strumento che questo modulo offre.
    """

    testo = str(valore or "").strip()
    if testo.endswith(".0") and testo[:-2].isdigit():
        testo = testo[:-2]
    return "".join(carattere for carattere in testo if carattere.isdigit())


def _identifica_qualcosa(impronta: str) -> bool:
    """Un'impronta fatta di soli separatori non identifica niente.

    `impronta_articolo({})` restituisce `"|||"`, che è una stringa e sembra
    un'impronta: registrarla vorrebbe dire agganciare quella conferma alla prima
    riga che domani nascerà altrettanto vuota. Quando l'impronta ha più pezzi, il
    **primo** non conta da solo: `"noce|||"` è un fornitore, non un articolo.
    """

    pezzi = impronta.split("|")
    if len(pezzi) > 1:
        return any(pezzo.strip() for pezzo in pezzi[1:])
    return bool(impronta.strip())


class MagazzinoConferme:
    """Le conferme dell'utente, in un file SQLite che il chiamante sceglie.

    **Una tabella sola, mai riscritta: si aggiunge una riga e si chiude quella
    di prima.** L'alternativa — una tabella per le conferme in vigore e una per
    lo storico — è stata scartata perché lo stesso fatto starebbe scritto in due
    posti: ogni `ricorda` dovrebbe aggiornarli tutti e due, e il giorno in cui
    uno dei due percorsi sbaglia il magazzino risponde «cosa avevi deciso prima»
    da una tabella in cui quella riga non è mai arrivata. Qui la storia **sono**
    le righe: `valida_dal` è l'istante in cui la risposta è entrata in vigore,
    `valida_fino_a` quello in cui è stata sostituita o tolta, e `NULL` vuol dire
    «vale adesso». Nessun `UPDATE` toglie mai contenuto: l'unico che esiste
    scrive `valida_fino_a`, cioè **aggiunge** un fatto.

    Gli indici sono due, e ognuno difende una cosa:

    * `conferme_in_vigore`, unico e parziale su `(fornitore, articolo)` dove
      `valida_fino_a IS NULL`: è insieme la strada di `cerca` e **l'invariante**
      «al massimo una conferma in vigore per coppia», tenuta dal database e non
      dal mio codice. Se un giorno una transazione qui dentro sbaglia, il file
      non si riempie di due risposte contemporanee: la scrittura fallisce.
    * `conferme_storia` su `(fornitore, articolo, valida_dal)`: la domanda
      «che cosa avevo deciso prima, e quando» in ordine di tempo.

    Concorrenza: il servizio è multi-thread (il polling della pagina e il filo
    della pipeline). La connessione è una sola e un lucchetto interno serializza
    le operazioni di questo oggetto; `PRAGMA journal_mode=WAL` e l'attesa sul
    blocco tengono il resto, cioè un secondo magazzino aperto sullo stesso file
    da un altro thread o da un altro processo. Ogni scrittura apre la
    transazione con `BEGIN IMMEDIATE`: prende il blocco subito invece di
    scoprire al `COMMIT` che un'altra l'ha preceduta, che è il modo classico in
    cui due scritture SQLite si uccidono a vicenda senza poter più riprovare.
    """

    def __init__(self, percorso: Path) -> None:
        self.percorso = Path(percorso)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        # Che giornale il file ha davvero accettato. Su un disco di rete WAL non
        # si può attivare e SQLite resta in `delete` **senza** dare errore: il
        # magazzino continua a funzionare, ma chi indaga su una lentezza o su un
        # blocco deve poterlo leggere invece di dedurlo.
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
                isolation_level=None,  # le transazioni le apro io, con BEGIN IMMEDIATE
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self.giornale = str(self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            # L'attesa sul blocco la mette `timeout=` qui sopra, e basta quello:
            # un `PRAGMA busy_timeout` in più c'era, ed è stato tolto perché una
            # controprova l'ha trovato **inerte** — spegnerlo non faceva fallire
            # niente. Una riga che sembra una difesa e non lo è è peggio di una
            # difesa che manca, perché la prossima persona la conta.
            # `synchronous` resta il predefinito di SQLite e non si abbassa
            # apposta: qui si scrivono poche righe alla settimana, e sono le
            # risposte dell'utente. Guadagnare millisecondi rischiando di
            # perderne una all'ultima scrittura sarebbe un pessimo affare.
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
            # Le uguaglianze fra codici a barre. Stessa disciplina della tabella
            # accanto — si aggiunge una riga e si chiude quella di prima, niente
            # si cancella mai — perché il rischio è dello stesso genere e più
            # grande: una conferma sbagliata rovina un prodotto presso un
            # fornitore, un'uguaglianza sbagliata li rovina **tutti**, e ogni
            # settimana. `articolo` e `offerta` sono le due impronte su cui la
            # dichiarazione è stata fatta: non servono a ritrovarla, servono a
            # sapere **guardando che cosa** qualcuno ha detto che i due codici
            # erano lo stesso articolo.
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
            except sqlite3.Error:  # pragma: no cover - chiudere non deve mai far danno
                pass
            self._conn = None

    class _Transazione:
        """`BEGIN IMMEDIATE` … `COMMIT`, oppure `ROLLBACK` se qualcosa va storto."""

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
        """Il fornitore si confronta senza maiuscole.

        `NOCE` e `noce` sono lo stesso listino, e scrivere l'uno per poi
        cercare l'altro farebbe sparire la conferma **senza un errore**: è il
        genere di guasto che nessuno trova, perché somiglia a «non l'avevo mai
        confermato». L'impronta dell'offerta invece resta com'è arrivata: chi la
        rilegge la confronta con una calcolata allo stesso modo, e cambiarla
        anche solo di maiuscole la renderebbe incomparabile.
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
        """Registra la risposta dell'utente su una coppia (fornitore, articolo).

        Sostituisce quella di prima **conservando lo storico**: la riga in vigore
        viene chiusa con `valida_fino_a = quando` e la nuova nasce con
        `valida_dal = quando`. Chiudere e aprire nello stesso istante non è un
        dettaglio: la storia di una coppia non ha buchi e non ha sovrapposizioni,
        quindi «che cosa valeva il 12 agosto» ha una risposta sola.

        `accettata=False` è una memoria come le altre, e anzi è quella che fa
        risparmiare più fatica: «no, il piatto frutta di NOCE non è il mio
        piatto dessert» detto una volta non deve tornare ogni lunedì.

        `quando` è una stringa ISO che **passa il chiamante**. Qui dentro non si
        chiama nessun orologio, e non è un vezzo: una memoria che si data da sola
        non si può provare, e questo progetto ha già pagato quell'errore.

        **Riconfermare una risposta identica non scrive niente** — stessa
        offerta, stessa decisione, stesso motivo — e `valida_dal` resta quello
        della prima volta, perché è da allora che quella risposta vale. Serve
        contro il chiamante: la pagina si autosalva ogni 450 ms, e senza questa
        regola una sola conferma diventerebbe centinaia di righe di storico che
        raccontano decisioni mai prese.

        Rifiuta con `ValueError` un'impronta che non identifica niente: una
        conferma agganciata a `"|||"` combacerebbe domani con la prima riga
        altrettanto vuota, cioè con merce a caso.
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
        """La conferma **in vigore** per quella coppia, o `None` se non c'è.

        `None` vuol dire «di questo non ho memoria», non «no»: il no dell'utente
        è una riga con `accettata` falsa, e le due cose portano a schermate
        diverse.

        ⚠ **La conferma si risponde sempre, anche quando l'articolo del
        fornitore di oggi non è più quello confermato**, e la verifica la fa il
        chiamante confrontando `offerta` con l'impronta della riga di adesso. Non
        è pigrizia, è la scelta che protegge il caso vero: fra i due export sul
        disco, **51 articoli Noce identici** — stesso codice a barre, stessa
        descrizione — hanno cambiato impronta perché il listino è passato dal CSV
        del sito al loro `.xls` e il codice articolo è passato da vuoto a
        `0000000004795`. Se `cerca` avesse preteso l'impronta di oggi e avesse
        risposto «scaduta», quella settimana la memoria si sarebbe svuotata in
        blocco proprio mentre l'articolo era rimasto lo stesso: cioè il difetto
        che questo magazzino esiste per chiudere.

        Rispondendo sempre, il chiamante può fare la cosa giusta in tutti e due i
        casi: se l'impronta combacia applica la conferma; se non combacia ha in
        mano **entrambe** le impronte e può dire all'utente che cosa è cambiato e
        chiedergli una riconferma sola invece di ricominciare da zero. Il
        magazzino non può decidere fra i due, perché la differenza fra «hanno
        cambiato il formato del listino» e «gli hanno cambiato l'articolo sotto
        il naso» sta nei dati della settimana, che qui dentro non ci sono.
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
        """Toglie la conferma in vigore. `True` se ce n'era una.

        Una mossa sola, ed è la mossa che deve costare meno di tutte: è l'unica
        via d'uscita da una conferma sbagliata, e finché non è stata fatta quella
        conferma continua a valere ogni settimana.

        **La riga non si cancella, si chiude.** Se una conferma sbagliata ha già
        prodotto un ordine sbagliato, buttare via la riga cancellerebbe l'unica
        traccia che spiega quell'ordine: `esporta` continua a mostrarla, con la
        sua data e il suo motivo.

        `quando` è in più rispetto alla firma minima e resta facoltativo perché
        `dimentica(fornitore, articolo)` deve funzionare così com'è, ma **chi ha
        un orologio dovrebbe passarlo**: senza, la riga chiusa resta senza
        l'istante in cui è stata tolta (`valida_fino_a` vuoto, cioè «tolta, non
        so dire quando»), e un audit con un buco è un audit che va spiegato a
        voce. Qui dentro l'orologio non si chiama, per la stessa ragione di
        `ricorda`.
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
        """Le conferme che valgono **adesso**, in ordine di fornitore e articolo.

        È quello che serve al confronto della settimana e alla pagina che le
        elenca per poterle togliere. Lo storico non c'è: sta in `esporta`.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM conferme WHERE valida_fino_a IS NULL "
                "ORDER BY fornitore, articolo"
            )
        return [self._riga(riga) for riga in righe]

    def rifiuti(self) -> list[dict]:
        """I «no» in vigore: «questa riga di questo fornitore non e' il mio articolo».

        E' l'altra meta' di `tutte()`, e sta qui e non nel servizio perche' la
        regola che distingue un no da un si' e' di questo magazzino — una riga
        con `accettata` falsa — e chi la ricopia fuori si porta dietro il dovere
        di tenerla allineata.

        ⚠ `ricorda` sa scrivere questa riga dal primo giorno, e il suo docstring
        la nomina per nome. Fino al 21 agosto 2026 non la scriveva nessuno:
        `conferme.db` aveva venti righe e `sum(accettata=0)` faceva zero. Il
        programma sapeva ricordare solo i si', e chi doveva dire di no non aveva
        nessun posto dove dirlo — quindi confermava.

        L'ordine e' quello di `tutte()`: chi le legge insieme le confronta.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM conferme WHERE valida_fino_a IS NULL AND accettata=0 "
                "ORDER BY fornitore, articolo"
            )
        return [self._riga(riga) for riga in righe]

    def esporta(self) -> list[dict]:
        """**Tutto** quello che il magazzino sa, storico compreso, in JSON.

        Un `.db` non si legge a occhio: senza questa via d'uscita, «che cosa ho
        confermato» sarebbe una domanda a cui si risponde solo con uno strumento
        che l'utente non ha. Qui ci sono anche le righe chiuse — sostituite o
        tolte — perché sono la sola risposta a «che cosa avevo deciso prima, e
        quando», e perché la conferma che ha prodotto un ordine sbagliato va
        ritrovata **dopo** che è stata corretta.

        L'ordine è stabile e i valori sono tutti tipi JSON (stringhe, booleani,
        interi, `None`): due esportazioni dello stesso contenuto danno lo stesso
        documento, e si possono confrontare fra una settimana e l'altra.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM conferme ORDER BY fornitore, articolo, valida_dal, id"
            )
        return [self._riga(riga) for riga in righe]

    # -------------------------------------------------- uguaglianze fra codici

    @staticmethod
    def _coppia(codice_a: Any, codice_b: Any) -> tuple[str, str]:
        """I due codici in ordine fisso: l'uguaglianza non ha un verso.

        Senza quest'ordine `unisci(A, B)` e `unisci(B, A)` sarebbero due righe
        diverse, l'indice unico non fermerebbe la seconda, e `separa(B, A)` non
        troverebbe quella scritta come `(A, B)` — cioè un'uguaglianza sbagliata
        che non si riesce a togliere.
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
        """Dichiara che due codici a barre sono lo stesso articolo.

        Restituisce `True` se ha scritto qualcosa, `False` se quell'uguaglianza
        era già in vigore: ridichiararla non tocca `valida_dal`, perché è da
        allora che vale. Serve contro il chiamante, come in `ricorda`.

        ⚠ Qui si registra **un lato solo del grafo**: che A e B siano lo stesso
        articolo. Che allora anche C, già uguale a B, sia uguale ad A lo deduce
        `classe`, e non si scrive da nessuna parte. È voluto: le righe scritte
        sono le dichiarazioni umane, e devono restare quelle e solo quelle —
        altrimenti togliendo A≡B resterebbe in giro un A≡C che nessuno ha mai
        detto, e che nessuno saprebbe da dove è arrivato.

        Rifiuta due codici uguali (non dice niente) e un codice vuoto (non
        identifica niente).
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
        """Toglie un'uguaglianza. `True` se ce n'era una in vigore.

        È la via d'uscita, e deve costare un clic: finché non è stata fatta,
        quella dichiarazione entra in **ogni** confronto futuro e su **tutti** i
        fornitori. La riga non si cancella, si chiude: se ha già prodotto un
        ordine sbagliato, buttarla via cancellerebbe l'unica traccia che lo
        spiega.
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
        """Le dichiarazioni che valgono adesso, una riga per coppia dichiarata.

        È quello che la pagina elenca per poterle togliere: le **coppie**, non le
        classi, perché si toglie quello che qualcuno ha detto.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM uguaglianze WHERE valida_fino_a IS NULL "
                "ORDER BY codice_a, codice_b"
            )
        return [self._riga_uguaglianza(riga) for riga in righe]

    def classi(self) -> list[list[str]]:
        """I gruppi di codici che valgono come lo stesso articolo.

        Le componenti connesse del grafo delle coppie dichiarate: A≡B e B≡C
        fanno un gruppo di tre, e chi cerca A trova anche C. Ogni gruppo è
        ordinato e i gruppi fra loro pure, così due letture dello stesso
        contenuto danno lo stesso documento — è l'artefatto che la catena riceve
        e confronta da una settimana all'altra.

        Un gruppo di uno non esiste: un codice senza uguaglianze non è un gruppo,
        è se stesso, e scriverlo vorrebbe dire mettere nell'artefatto 451 righe
        che non dicono niente.
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
        """Tutti i codici che valgono come questo, **compreso questo**.

        Un codice senza uguaglianze restituisce se stesso: chi interroga
        l'indice per EAN non deve avere un caso in meno da trattare.
        Restituisce la lista vuota solo per un codice vuoto.
        """

        chiave = codice_confrontabile(codice)
        if not chiave:
            return []
        for gruppo in self.classi():
            if chiave in gruppo:
                return gruppo
        return [chiave]

    def esporta_uguaglianze(self) -> list[dict]:
        """**Tutto** quello che il magazzino sa sulle uguaglianze, storico compreso.

        Stessa ragione di `esporta`: un `.db` non si legge a occhio, e una
        dichiarazione tolta va ritrovata **dopo** che è stata corretta, perché è
        quella che spiega gli ordini di prima.
        """

        with self._lock:
            righe = self._esegui(
                "SELECT * FROM uguaglianze ORDER BY codice_a, codice_b, valida_dal, id"
            )
        return [self._riga_uguaglianza(riga) for riga in righe]

    def chiudi(self) -> None:
        """Chiude il file. Si può chiamare due volte senza conseguenze."""

        with self._lock:
            self._chiudi_di_forza()
