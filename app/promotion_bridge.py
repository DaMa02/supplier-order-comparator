"""Connect supplier source annotations to the conservative promotion engine."""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from offerta import offer_is_available, offer_supplier_id
import registro
import xls_reader
from promotions import (
    CERTAINTY_REVIEW,
    KIND_AMBIGUOUS,
    decorate_review_data,
    detect_numeric_discount,
    detect_promotions,
    detect_threshold_gift,
    looks_like_reward,
    looks_like_threshold_heading,
    make_promotion,
)


# Che cosa sia un'intestazione di soglia lo decide il motore, non questo
# ponte: la copia che stava qui riconosceva «ACQUISTANDO» ma non «ACQUISTANO»,
# e nel listino del 3-6 agosto quella sola lettera mancante faceva sparire per
# intero la condizione «ACQUISTANO 5 CT TRA ... IN OMAGGIO 1 CT DI DENT.
# SENSODENT 15 ML», righe di merce comprese.
e_intestazione_di_soglia = looks_like_threshold_heading
e_riga_premio = looks_like_reward


# ---------------------------------------------------------------------------
# Il vocabolario del registro: dove un fornitore scrive le sue condizioni
# ---------------------------------------------------------------------------
#
# ⚠ Fino al 15 agosto 2026 questo lettore conosceva un fornitore solo.  Andava
# a prendere `larice_v1` per nome, scriveva «LARICE» nei messaggi e leggeva il
# documento di `paths["larice"]`: le condizioni di chiunque altro non le
# guardava nessuno, e non c'era modo di dichiararle senza rimettere mano al
# codice.  Adesso la domanda che il ponte fa al registro e' un'altra — «quali
# fornitori dichiarano dove tengono le loro condizioni commerciali?» — e la
# risposta la scrive `references/adapters.json`, non questo file.
#
# Misura del 15 agosto 2026 sui quattro listini veri della settimana, che e' il
# motivo per cui oggi lo dichiara **soltanto** LARICE: LARICE 13 intestazioni
# di soglia e 13 righe premio in colonna G; BETULLA 12 testi con una parola
# promozionale dentro la descrizione, di cui 7 sono la parola «Ogni» di «Ogni
# Superficie»; CIPRESSO 1, ed e' un nome di prodotto che contiene «offerta»;
# NOCE la colonna `descrizione_offerta` vuota su tutte e 18.074 le righe e
# la colonna `offerta` che dice `NO` su tutte e 17.148 quelle compilate.
# Dichiararle per gli altri tre vorrebbe dire inventare offerte che non
# esistono, che e' peggio del difetto.

# I ruoli che un lettore di condizioni sa usare, e la parola con cui li chiama
# chi usa il programma: la seconda serve a dire quale manca senza nominare una
# colonna di foglio elettronico.  L'ordine e' quello in cui compaiono nel
# messaggio.
_RUOLI_DEL_LETTORE = {
    "text": "le descrizioni",
    "reward": "il nome dell'articolo in omaggio",
    "ean": "il codice a barre",
    "row_code": "i codici delle righe",
}

# Le due forme in cui i listini veri scrivono una condizione commerciale.
#
# `blocchi`: la condizione occupa piu' righe — l'intestazione con la quantita'
# da acquistare, poi la merce ammessa, poi la riga con l'omaggio.  E' come
# scrive LARICE, ed e' l'unica forma misurata su un listino vero.
#
# `riga`: una riga porta per intero la sua condizione, in una colonna sola.
# E' come scriverebbero BETULLA («LINDA SETA ... 11+1 Gratis Pz», dentro la
# descrizione) e NOCE (la colonna `descrizione_offerta`, oggi vuota): non
# la dichiara nessuno, ma senza questa forma quei due non si potrebbero
# accendere con una riga di registro il giorno che scrivono qualcosa — e
# «funziona senza toccare il codice» sarebbe falso proprio per loro.
LAYOUT_BLOCCHI = "blocchi"
LAYOUT_RIGA = "riga"

# Che cosa pretende ciascuna forma.  Una soglia a blocchi ha bisogno di tutte
# e quattro le colonne: senza il nome del premio la condizione si ricompone lo
# stesso ma esce «da verificare» invece che confermata, e l'utente perde
# l'omaggio in un altro modo.  Una condizione scritta per intero dentro una
# riga sola ha bisogno soltanto della colonna dove sta scritta.
_COLONNE_RICHIESTE = {
    LAYOUT_BLOCCHI: ("text", "reward", "ean", "row_code"),
    LAYOUT_RIGA: ("text",),
}


def colonne_richieste_dal_layout(forma: Any) -> tuple[str, ...] | None:
    """Che cosa pretende una forma di scrittura, o `None` se non la conosciamo.

    Serve a chi raccoglie la dichiarazione **prima** che arrivi qui: la
    mappatura guidata deve poter rifiutare subito una forma senza le sue
    colonne, invece di lasciar scrivere nel registro una dichiarazione che poi
    ogni settimana produce «non so piu' dove il listino tiene …».  La tabella
    resta una sola: due elenchi da tenere allineati divergono, e quello che si
    dimentica e' sempre quello che fa sparire le soglie con omaggio.
    """

    return _COLONNE_RICHIESTE.get(str(forma or "").strip().casefold())


def nomi_dei_ruoli() -> dict[str, str]:
    """I ruoli del lettore col nome che ne ha l'utente, per chi deve chiederli."""

    return dict(_RUOLI_DEL_LETTORE)

# Oltre questa distanza dall'intestazione un blocco non e' piu' credibile: le
# righe che seguono appartengono ad altro. Il numero e' una difesa contro un
# listino malformato, non una regola commerciale, e il registro puo' dirne un
# altro con `max_block_rows`.
_RIGHE_MASSIME_DI_UN_BLOCCO = 500

# I primi otto byte di un documento Excel 97-2003.  Il formato lo decidono i
# byte e non l'estensione — lo dichiara anche il registro, nella nota
# dell'adattatore Noce — e chi sa leggerli e' `app/xls_reader.py`.
_FIRMA_XLS = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _codici_non_ordinabili(adattatore: dict[str, Any]) -> set[str]:
    """I codici che il registro dichiara non acquistabili, per quel listino."""

    codici = registro.codici_di_riga(adattatore)
    return {
        str(chiave).strip().upper()
        for chiave, dichiarato in (codici.get("codes") or {}).items()
        if isinstance(dichiarato, dict) and dichiarato.get("orderable") is False
    }


def _fornitori_con_promozione_inclusa() -> set[str]:
    """I fornitori che dichiarano il di piu' gia' compreso nel prezzo di listino.

    Da BETULLA «11+1» non e' uno sconto da applicare: il prezzo che manda lo
    contiene gia'.  E' una convenzione del rapporto con quel fornitore, non
    una proprieta' del programma, e finche' stava scritta qui come
    `supplier == "betulla"` il giorno che un altro fornitore avesse fatto lo
    stesso patto non c'era modo di dichiararlo senza rimettere mano al codice
    — e chi ci avesse provato dal registro non avrebbe visto succedere niente.

    Il fornitore si cerca per `supplier_id`: andare a prendere l'adattatore
    per nome («betulla_v1») sarebbe lo stesso confronto con un altro vestito.
    Uno stesso fornitore puo' avere piu' di un adattatore — Noce ne ha uno
    per il .xls e uno per il CSV — e basta che uno lo dichiari.
    """

    fornitori: set[str] = set()
    for voce in registro.adattatori():
        regole = voce.get("commercial_rules")
        if not isinstance(regole, dict) or regole.get("promotion_included_in_product") is not True:
            continue
        fornitore = str(voce.get("supplier_id") or "").strip().casefold()
        if fornitore:
            fornitori.add(fornitore)
    return fornitori


def fornitori_con_condizioni_dichiarate() -> dict[str, list[dict[str, Any]]]:
    """Per ogni fornitore, gli adattatori che dicono dove tiene le condizioni.

    Un fornitore che non lo dichiara non viene letto e non produce nessun
    avviso: e' la differenza fra «non ha condizioni commerciali» e «non so
    leggerle», e confonderle riempirebbe la pagina di righe che non chiedono
    di fare niente.

    Lo stesso fornitore puo' avere piu' di un adattatore, uno per formato di
    documento: si tengono tutti, e quale usare lo decide il documento vero.
    """

    dichiarati: dict[str, list[dict[str, Any]]] = {}
    for voce in registro.adattatori():
        if not isinstance(voce.get("commercial_conditions"), dict):
            continue
        fornitore = str(voce.get("supplier_id") or "").strip().casefold()
        if fornitore:
            dichiarati.setdefault(fornitore, []).append(voce)
    return dichiarati


def _adattatore_per_il_documento(
    voci: list[dict[str, Any]], path: Path | None, adapter_id: str | None = None
) -> dict[str, Any]:
    """Fra gli adattatori di un fornitore, quello del documento che si ha in mano.

    Noce ne ha due, uno per il `.xls` e uno per il CSV: leggere le
    condizioni con la dichiarazione dell'altro formato vorrebbe dire cercare
    una colonna dove non c'e'.

    ⚠ **L'estensione da sola non basta piu'.**  Dal 4 settembre 2026 LARICE ha
    due adattatori dello **stesso** formato — il canvass vecchio senza
    intestazioni e quello nuovo con l'intestazione alla riga 11 — e la vecchia
    regola avrebbe restituito sempre il primo dei due, cioe' avrebbe letto le
    soglie del listino nuovo nelle colonne del vecchio.  Non sarebbe uscito un
    errore: sarebbero uscite zero condizioni, in silenzio.  Chi ha riconosciuto
    il documento lo dice la review (`adapterId`), e si passa da
    `registro.adattatore_base` perche' quello imparato porta lo stesso id con
    `__locale` in coda.

    Se l'identificativo c'e' ma non e' fra questi, e i candidati sono piu' di
    uno, non si indovina: meglio nessuna condizione che le condizioni di un
    altro documento.
    """

    if adapter_id:
        base = registro.adattatore_base(adapter_id)
        for voce in voci:
            if str(voce.get("id") or "") in {adapter_id, base}:
                return voce
        if len(voci) > 1:
            return {}
    if path is not None:
        suffisso = path.suffix.casefold()
        for voce in voci:
            tipi = [str(tipo).casefold() for tipo in (voce.get("file_types") or [])]
            if suffisso in tipi:
                return voce
    return voci[0] if voci else {}


def _indice_di_colonna(dichiarata: Any) -> int | None:
    """La colonna dichiarata dal registro, come numero a partire da 1.

    Il listino Larice non ha nessuna riga di intestazione: li' una colonna si
    puo' indicare solo per lettera («R») o per numero (18).  Una stringa di
    cifre non vale come numero, ed e' la stessa regola che applica il registro:
    altrove quella e' il nome di un'intestazione, e rispondere «colonna 9» a
    una dichiarazione che nessun altro lettore interpreta cosi' vorrebbe dire
    leggere la colonna sbagliata in silenzio.
    """

    if isinstance(dichiarata, bool):
        return None
    if isinstance(dichiarata, int):
        return dichiarata if dichiarata >= 1 else None
    lettere = str(dichiarata or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", lettere):
        return None
    indice = 0
    for lettera in lettere:
        indice = indice * 26 + (ord(lettera) - ord("A") + 1)
    return indice


def _dove_sta_la_colonna(adattatore: dict[str, Any], campo: Any) -> int | None:
    """Dove l'adattatore dice che sta la colonna che chiama cosi'.

    Si guarda nelle stesse dichiarazioni che segue il lettore dei prezzi —
    `column_map` per chi indica le colonne per lettera, le posizioni della
    firma per chi le indica per nome di intestazione.  Due idee diverse di
    dove sta una colonna vorrebbero dire prezzi giusti e condizioni sparite,
    senza niente che le colleghi; e quando l'utente conferma una variazione di
    schema il registro riscrive quelle, non altro, quindi le condizioni
    seguono i prezzi invece di restare indietro di una settimana.
    """

    nome = str(campo or "").strip()
    if not nome:
        return None
    mappa = adattatore.get("column_map")
    if isinstance(mappa, dict) and nome in mappa:
        return _indice_di_colonna(mappa.get(nome))
    firma = adattatore.get("header_signature")
    colonne = firma.get("columns") if isinstance(firma, dict) else None
    if isinstance(colonne, dict):
        cercato = registro.normalizza(nome)
        for intestazione, posizione in colonne.items():
            if registro.normalizza(intestazione) == cercato:
                return _indice_di_colonna(posizione)
    return None


def _colonne_delle_condizioni(
    adattatore: dict[str, Any], dichiarazione: dict[str, Any]
) -> tuple[str, dict[str, int], list[str]]:
    """La forma di scrittura, le colonne che servono e quelle che mancano.

    Prima queste posizioni stavano scritte nel codice.  Il giorno che il
    fornitore ne sposta una — o che l'utente conferma una variazione di
    schema, e il registro impara la mappatura nuova — il lettore continuava a
    guardare la colonna di prima: nessun errore, nessun blocco, e le soglie
    con omaggio sparivano dal riepilogo senza una parola.  Chi ordina 4
    cartoni invece di 5 perde il cartone in omaggio e non lo sa.

    Una colonna che il registro non dichiara non si indovina: finisce
    nell'elenco che torna di fianco, e chi legge si ferma e lo dice.
    """

    forma = str(dichiarazione.get("layout") or "").strip().casefold()
    campi = dichiarazione.get("fields")
    campi = campi if isinstance(campi, dict) else {}
    if forma not in _COLONNE_RICHIESTE:
        return forma, {}, ["in che modo scrive le sue condizioni"]

    colonne: dict[str, int] = {}
    mancanti: list[str] = []
    for ruolo, nome_per_l_utente in _RUOLI_DEL_LETTORE.items():
        indice = _dove_sta_la_colonna(adattatore, campi.get(ruolo))
        if indice is not None:
            colonne[ruolo] = indice
        elif ruolo in _COLONNE_RICHIESTE[forma]:
            mancanti.append(nome_per_l_utente)
    return forma, colonne, mancanti


def _firma_del_registro() -> int:
    """Quando il registro e' cambiato l'ultima volta.

    Entra nella firma della lettura perche' adesso le colonne vengono di li':
    se l'utente conferma una variazione di schema e il listino resta lo stesso
    file, senza questo il servizio continuerebbe a mostrare fino al riavvio i
    blocchi letti con la mappatura vecchia.
    """

    try:
        return registro.REGISTRO.stat().st_mtime_ns
    except OSError:
        return -1


def _cella(row: tuple[Any, ...], colonna: int | None) -> str:
    """Il testo di una cella, indicata come la indica il registro (1 = A).

    Il lettore del `.xls` consegna ogni cella come coppia `(valore,
    grassetto)`: il grassetto qui non serve, e senza toglierlo il testo di una
    condizione diventerebbe la stringa «('ACQUISTANDO 5 CT TRA', False)», che
    nessun rilevatore riconosce.
    """

    if colonna is None or len(row) < colonna:
        return ""
    valore = row[colonna - 1]
    if isinstance(valore, tuple) and len(valore) == 2 and isinstance(valore[1], bool):
        valore = valore[0]
    return str(valore or "").strip()


def _elenco_in_italiano(voci: list[str]) -> str:
    """«le descrizioni e il codice a barre», non «['description', 'ean']»."""

    if len(voci) < 2:
        return voci[0] if voci else ""
    return ", ".join(voci[:-1]) + " e " + voci[-1]


def _errore_di_lettura(fornitore: str, motivo: str) -> dict[str, str]:
    """Il modo in cui l'utente viene a sapere che le condizioni mancano.

    La pagina ne fa un avviso «Offerte non lette» e ci aggiunge da se' che
    prezzi e confronto restano validi: qui va detto soltanto che cosa e'
    successo, in una riga, senza nominare colonne, file o funzioni.

    Come si chiama il fornitore lo dice il registro.  Quando stava scritto qui
    («LARICE», maiuscolo, accanto a `supplier="larice"`) un fornitore
    imparato sarebbe comparso nei messaggi con il suo identificativo tecnico.
    """

    nome = registro.nome_del_fornitore(fornitore)
    return {
        "supplier": fornitore,
        "supplierName": nome,
        "message": f"Non riesco a leggere le condizioni commerciali di {nome}: {motivo}.",
    }


def _errore_di_documento(fornitore: str, path: Path, motivo: str) -> dict[str, str]:
    """Il documento c'è ma non si è riusciti a leggerlo, o non fino in fondo."""

    nome = registro.nome_del_fornitore(fornitore)
    return {
        "supplier": fornitore,
        "supplierName": nome,
        "message": (
            f"Le condizioni commerciali del listino {nome} «{path.name}» non sono "
            f"state lette: {motivo}"
        ),
    }


def _source_paths(review: dict[str, Any]) -> tuple[dict[str, Path], set[str], dict[str, str]]:
    """I listini dichiarati dalla review, e quelli che non si aprono piu'.

    Un fornitore dichiarato dalla review il cui documento non si risolve non
    e' la stessa cosa di un fornitore che in questa run non c'e': il primo
    aveva delle condizioni commerciali e adesso non le ha piu', e va detto.
    Senza questa distinzione «il listino non c'e' piu'» e «questo fornitore
    non fa offerte» arrivavano all'utente nello stesso modo, cioe' in silenzio.

    Il fornitore che la review non dichiara affatto resta fuori: avvisare che
    mancano le soglie di chi non e' nel confronto vorrebbe dire un avviso a
    ogni ricalcolo, e un avviso che c'e' sempre non lo legge piu' nessuno.
    """

    result: dict[str, Path] = {}
    senza_documento: set[str] = set()
    # Quale adattatore ha riconosciuto quel documento: serve quando un
    # fornitore ne ha piu' d'uno dello stesso formato, e la sola estensione non
    # basta piu' a dire quale delle due dichiarazioni vale.
    adattatori: dict[str, str] = {}
    for item in review.get("files") or []:
        supplier = str(item.get("supplierId") or "").casefold()
        if not supplier:
            continue
        raw = item.get("sourcePath") or item.get("originalPath") or item.get("path")
        path = Path(str(raw)).resolve() if raw else None
        if path is not None and path.is_file():
            result[supplier] = path
            riconosciuto = str(item.get("adapterId") or item.get("adapter_id") or "").strip()
            if riconosciuto:
                adattatori[supplier] = riconosciuto
        else:
            senza_documento.add(supplier)
    # Lo stesso fornitore puo' avere piu' di una voce: se almeno un documento
    # si apre, non manca niente.
    return result, senza_documento - set(result), adattatori


def _row_ref(supplier: str, offer: dict[str, Any]) -> str:
    return f"{supplier}:riga:{offer.get('sourceRow') or offer.get('source_row') or '?'}"


def _foglio_scelto(nomi: list[str], dichiarato: Any) -> str | None:
    """Quale foglio, fra quelli del documento, dice di leggere il registro.

    «FIRST» e' la parola che il registro usa gia' altrove per dire «il primo
    foglio»; un nome vero si confronta come lo confronta il registro, cioe'
    senza badare a punti, spazi e maiuscole.
    """

    if not nomi:
        return None
    voluto = str(dichiarato or "FIRST").strip()
    if not voluto or voluto.upper() == "FIRST":
        return nomi[0]
    cercato = registro.normalizza(voluto)
    for nome in nomi:
        if registro.normalizza(nome) == cercato:
            return nome
    return None


@contextmanager
def _righe_del_documento(
    path: Path, foglio_dichiarato: Any
) -> Iterator[tuple[str, Iterator[tuple[int, tuple[Any, ...]]]]]:
    """Il foglio dichiarato dal registro, riga per riga, qualunque sia il formato.

    Un fornitore che manda un Excel 97-2003 non e' meno leggibile di uno che
    manda un `.xlsx`: Noce manda **solo** quel formato, e un motore che
    dicesse «dichiara pure dove tieni le condizioni, tanto il tuo documento non
    lo apro» sarebbe un altro cablaggio, solo meno visibile.
    """

    with path.open("rb") as documento:
        firma = documento.read(8)
    if firma == _FIRMA_XLS:
        fogli = xls_reader.read_workbook(path)
        nome = _foglio_scelto([foglio.name for foglio in fogli], foglio_dichiarato)
        if nome is None:
            raise LookupError(f"il foglio «{foglio_dichiarato}» non c'è")
        foglio = next(item for item in fogli if item.name == nome)
        yield nome, enumerate((tuple(riga) for riga in foglio.rows), start=1)
        return

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        nome = _foglio_scelto(list(workbook.sheetnames), foglio_dichiarato)
        if nome is None:
            raise LookupError(f"il foglio «{foglio_dichiarato}» non c'è")
        yield nome, enumerate(workbook[nome].iter_rows(values_only=True), start=1)
    finally:
        workbook.close()


def _riferimento(
    colonne: dict[str, int], foglio: str, prima_riga: int, ultima_riga: int
) -> str:
    """Dove sta il blocco dentro il listino, con le lettere di colonna vere.

    E' l'unico appiglio che ha l'utente per ritrovare la condizione nel file
    del fornitore, ed entra anche nell'identificativo della promozione: se le
    colonne si spostano deve spostarsi anche lui, altrimenti manda a guardare
    una colonna dove non c'e' niente.
    """

    prima = get_column_letter(colonne["text"])
    ultima = get_column_letter(colonne.get("reward") or colonne["text"])
    if prima_riga == ultima_riga and prima == ultima:
        return f"{foglio}!{prima}{prima_riga}"
    return f"{foglio}!{prima}{prima_riga}:{ultima}{ultima_riga}"


def _condizione_da_leggere(
    fornitore: str,
    riferimento: str,
    testo: str,
    righe_ammesse: list[int],
    motivo: str,
) -> dict[str, Any]:
    """Una condizione dichiarata dal fornitore che il programma non ricompone.

    Non e' un errore di lettura del file: le righe ci sono e la merce si
    ordina, ma il patto commerciale non diventa un calcolo. Diventa allora una
    «offerta ambigua», cioe' l'unico esito che la pagina mostra senza usarlo
    per nessun conto: cosi' l'utente la vede e la conta, invece di non sapere
    che c'era.
    """

    return make_promotion(
        supplier=fornitore,
        source_reference=riferimento,
        source_text=f"{testo} [condizione non ricomposta: {motivo}]",
        kind=KIND_AMBIGUOUS,
        eligible={"source_rows": list(righe_ammesse), "mix_allowed": True},
        certainty=CERTAINTY_REVIEW,
        confirmed=False,
        economic_effect={
            "type": "none",
            "deterministic": False,
            "active": False,
            "affects_total": False,
            "affects_supplier_choice": False,
        },
    )


class PromotionService:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        # Lo stato della lettura, per fornitore.  Prima erano quattro campi con
        # «larice» nel nome: bastava quello per raccontare che il programma
        # sapeva leggere le condizioni di un fornitore solo.
        self.firme_dei_listini: dict[str, tuple[str, int, int]] = {}
        self.condizioni_lette: dict[str, list[dict[str, Any]]] = {}
        self.errori_dei_listini: dict[str, dict[str, str]] = {}
        # Quante condizioni l'ultima lettura ha dichiarato senza riuscire a
        # ricomporle, fornitore per fornitore: sono nella pagina come «da
        # verificare», e questo numero permette di accorgersene senza contarle
        # a mano.
        self.condizioni_da_verificare: dict[str, int] = {}
        # Come per il catalogo: i fornitori rimasti fuori dall'ultima lettura,
        # con il motivo in italiano.  Il server li mostra all'utente.
        self.load_errors: list[dict[str, str]] = []
        # Quanti sconti gia' compresi nel prezzo l'ultima lettura ha tolto
        # dall'elenco: non si mostrano (sono rumore, uno per riga di listino) ma
        # il numero resta a disposizione di chi voglia dirlo in una riga sola.
        self.sconti_gia_nel_prezzo: int = 0

    # -- la lettura di un fornitore ----------------------------------------

    def _condizioni_del_fornitore(
        self,
        fornitore: str,
        voci: list[dict[str, Any]],
        path: Path | None,
        documento_sparito: bool = False,
        adapter_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if path is None:
            if documento_sparito:
                # «Nessuna promozione» e «il listino non c'e' piu'» erano la
                # stessa identica cosa: le soglie sparivano dal riepilogo e
                # nessun avviso lo diceva.  Chi ordina 4 cartoni invece di 5
                # perde il cartone in omaggio senza che niente glielo dica.
                self.load_errors.append(
                    _errore_di_lettura(fornitore, "il listino non è fra i documenti caricati")
                )
            return []
        try:
            if not path.is_file():
                self.load_errors.append(
                    _errore_di_lettura(fornitore, "il listino non è fra i documenti caricati")
                )
                return []
            firma = (str(path), path.stat().st_mtime_ns, _firma_del_registro())
        except OSError as exc:
            # Un percorso di rete caduto o i permessi negati fanno fallire gia'
            # `stat`: anche quello e' un avviso, non una pagina che non si apre.
            nome = registro.nome_del_fornitore(fornitore)
            self.load_errors.append({
                "supplier": fornitore,
                "supplierName": nome,
                "message": f"Il listino {nome} «{path.name}» non è raggiungibile: {exc}",
            })
            return []
        if firma != self.firme_dei_listini.get(fornitore):
            self.firme_dei_listini[fornitore] = firma
            lette, errore = self._leggi_condizioni(
                fornitore, _adattatore_per_il_documento(voci, path, adapter_id), path
            )
            self.condizioni_lette[fornitore] = lette
            self.errori_dei_listini[fornitore] = errore or {}
        errore = self.errori_dei_listini.get(fornitore) or {}
        if errore:
            self.load_errors.append(dict(errore))
        condizioni = self.condizioni_lette.get(fornitore) or []
        self.condizioni_da_verificare[fornitore] = sum(
            1 for promotion in condizioni if promotion.get("kind") == KIND_AMBIGUOUS
        )
        return deepcopy(condizioni)

    def condizioni_di(
        self, fornitore: str, path: Path, adapter_id: str | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        """Le condizioni commerciali di un fornitore, lette dal suo documento."""

        voci = fornitori_con_condizioni_dichiarate().get(str(fornitore).casefold()) or []
        return self._leggi_condizioni(
            fornitore, _adattatore_per_il_documento(voci, path, adapter_id), path
        )

    def _leggi_condizioni(
        self, fornitore: str, adattatore: dict[str, Any], path: Path
    ) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        """Le condizioni commerciali di un listino, o il motivo per cui mancano.

        Un listino illeggibile costa le sue condizioni commerciali, non la
        pagina intera: prima di questa guardia un file rovinato faceva fallire
        `GET /api/review`, e all'utente non si apriva piu' niente.

        Dove stanno le colonne e come sono disposte lo dice il registro degli
        adattatori, che e' la stessa dichiarazione seguita dal lettore dei
        prezzi: due idee diverse di dove sta la colonna dello sconto
        vorrebbero dire prezzi giusti e soglie sparite, senza niente che le
        colleghi.
        """

        dichiarazione = adattatore.get("commercial_conditions")
        if not isinstance(dichiarazione, dict):
            return [], None

        forma, colonne, mancanti = _colonne_delle_condizioni(adattatore, dichiarazione)
        if mancanti:
            return [], _errore_di_lettura(
                fornitore, f"non so più dove il listino tiene {_elenco_in_italiano(mancanti)}"
            )

        try:
            with _righe_del_documento(path, dichiarazione.get("sheet")) as (titolo, righe):
                if forma == LAYOUT_BLOCCHI:
                    promozioni = self._blocchi(
                        fornitore, adattatore, dichiarazione, colonne, titolo, righe
                    )
                else:
                    promozioni = self._righe(
                        fornitore, adattatore, dichiarazione, colonne, titolo, righe
                    )
        except Exception as exc:  # noqa: BLE001 - il motivo lo legge l'utente
            return [], _errore_di_documento(fornitore, path, str(exc))
        return promozioni, None

    # -- le due forme di scrittura -----------------------------------------

    def _righe(
        self,
        fornitore: str,
        adattatore: dict[str, Any],
        dichiarazione: dict[str, Any],
        colonne: dict[str, int],
        titolo: str,
        righe: Iterator[tuple[int, tuple[Any, ...]]],
    ) -> list[dict[str, Any]]:
        """Una riga, una condizione scritta per intero in una colonna sola."""

        prima_riga = _numero_o(dichiarazione.get("data_start_row"), 1)
        non_ordinabili = _codici_non_ordinabili(adattatore)
        gia_compreso = fornitore in _fornitori_con_promozione_inclusa()
        promozioni: list[dict[str, Any]] = []
        for numero, riga in righe:
            if numero < prima_riga:
                continue
            testo = _cella(riga, colonne["text"])
            if not testo:
                continue
            if _cella(riga, colonne.get("row_code")).upper() in non_ordinabili:
                continue
            ean = _cella(riga, colonne.get("ean"))
            promozioni.extend(
                detect_promotions(
                    supplier=fornitore,
                    source_reference=_riferimento(colonne, titolo, numero, numero),
                    source_text=testo,
                    eligible={"source_rows": [numero], "eans": [ean] if ean else []},
                    included_in_product=gia_compreso,
                    numeric_discount_already_applied=True,
                )
            )
        return promozioni

    def _blocchi(
        self,
        fornitore: str,
        adattatore: dict[str, Any],
        dichiarazione: dict[str, Any],
        colonne: dict[str, int],
        titolo: str,
        righe: Iterator[tuple[int, tuple[Any, ...]]],
    ) -> list[dict[str, Any]]:
        """Una condizione su più righe: intestazione, merce ammessa, omaggio."""

        prima_riga = _numero_o(dichiarazione.get("data_start_row"), 1)
        massimo = _numero_o(dichiarazione.get("max_block_rows"), _RIGHE_MASSIME_DI_UN_BLOCCO)
        # Quali codici marchino una riga non acquistabile lo dice il registro
        # degli adattatori: qui serve per non contare il premio fra i prodotti
        # che fanno raggiungere la soglia.
        non_ordinabili = _codici_non_ordinabili(adattatore)

        promozioni: list[dict[str, Any]] = []
        heading_row: int | None = None
        heading_text = ""
        eligible_rows: list[int] = []
        ultima_riga = prima_riga
        for row_number, row in righe:
            if row_number < prima_riga:
                continue
            ultima_riga = row_number
            descrizione = _cella(row, colonne["text"])
            # ⚠ Quando il fornitore scrive il nome del premio dentro la riga
            # stessa, il registro dichiara la stessa colonna per i due ruoli, e
            # leggerla due volte la fa uscire scritta due volte: sul canvass
            # nuovo di LARICE — testo e premio tutt'e due in colonna E — la
            # frase diventava «SH. A/ERBAR. 250ML LAVANDA IN OMAGGIO 1CT SH.
            # A/ERBAR. 250ML LAVANDA».  La soglia usciva giusta lo stesso: e' la
            # frase che l'utente legge a sporcarsi, che e' la malattia del §17.
            # Dove le colonne sono due (LARICE vecchio, G e J) non cambia nulla.
            nome_del_premio = (
                "" if colonne["reward"] == colonne["text"] else _cella(row, colonne["reward"])
            )
            ean = _cella(row, colonne["ean"])
            discount_code = _cella(row, colonne["row_code"]).upper()

            if e_intestazione_di_soglia(descrizione):
                if heading_row is not None:
                    # Una seconda intestazione senza riga premio in mezzo:
                    # la prima condizione non si ricompone piu'.
                    promozioni.append(_condizione_da_leggere(
                        fornitore,
                        _riferimento(colonne, titolo, heading_row, row_number - 1),
                        heading_text, eligible_rows,
                        "la riga con l'omaggio non è arrivata prima dell'intestazione successiva",
                    ))
                heading_row = row_number
                heading_text = descrizione
                eligible_rows = []
                continue
            if heading_row is None:
                if e_riga_premio(descrizione):
                    # Un premio senza intestazione: la soglia che lo
                    # governa non e' stata riconosciuta, ma la merce c'e'.
                    promozioni.append(_condizione_da_leggere(
                        fornitore,
                        _riferimento(colonne, titolo, row_number, row_number),
                        " ".join(item for item in (descrizione, nome_del_premio) if item), [],
                        "manca l'intestazione con la quantità da acquistare",
                    ))
                continue
            if e_riga_premio(descrizione):
                source_text = " ".join(
                    item for item in (heading_text, descrizione, nome_del_premio) if item
                )
                promotion = detect_threshold_gift(
                    supplier=fornitore,
                    source_reference=_riferimento(colonne, titolo, heading_row, row_number),
                    source_text=source_text,
                    eligible={"source_rows": eligible_rows, "mix_allowed": True},
                    # L'EAN della riga premio e' gia' qui: senza, il
                    # contratto della promozione esce con reward.ean nullo
                    # e nessuno puo' risalire all'articolo regalato.
                    reward_ean=ean or None,
                )
                if promotion:
                    promozioni.append(promotion)
                else:
                    # Il blocco e' completo ma il testo non si calcola:
                    # resta comunque una condizione commerciale su merce
                    # ordinabile, e sparire in silenzio non e' un'opzione.
                    promozioni.append(_condizione_da_leggere(
                        fornitore,
                        _riferimento(colonne, titolo, heading_row, row_number),
                        source_text, eligible_rows,
                        "il testo del blocco non dice una soglia calcolabile",
                    ))
                heading_row = None
                heading_text = ""
                eligible_rows = []
                continue
            if ean and discount_code not in non_ordinabili:
                eligible_rows.append(row_number)
            elif row_number - heading_row > massimo:
                promozioni.append(_condizione_da_leggere(
                    fornitore,
                    _riferimento(colonne, titolo, heading_row, row_number),
                    heading_text, eligible_rows,
                    f"nessuna riga con l'omaggio entro {massimo} righe",
                ))
                heading_row = None
                heading_text = ""
                eligible_rows = []
        if heading_row is not None:
            # Il foglio finisce con un blocco aperto: stessa perdita, in
            # un punto dove nessun controllo passava.
            promozioni.append(_condizione_da_leggere(
                fornitore,
                _riferimento(colonne, titolo, heading_row, max(ultima_riga, heading_row)),
                heading_text, eligible_rows,
                "il foglio finisce prima della riga con l'omaggio",
            ))
        return promozioni

    # -- l'ingresso -------------------------------------------------------

    def detect(self, review: dict[str, Any]) -> list[dict[str, Any]]:
        self.load_errors = []
        self.condizioni_da_verificare = {}
        grouped_text: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
        promotions: list[dict[str, Any]] = []

        for product in review.get("products") or []:
            for offer in product.get("offers") or []:
                supplier = offer_supplier_id(offer).casefold()
                # ⚠ La regola «si puo' ordinare» la dice `offerta`, non questa
                # riga: qui era riscritta a mano nella forma negata, che e' lo
                # stesso accordo fra copie ma sotto un'altra faccia — tanto che
                # la prova che scandaglia le copie non la vedeva.  Questa
                # funzione decide quali offerte concorrono alle soglie delle
                # promozioni, quindi una regola che cresce altrove e non qui
                # sbaglia un omaggio.
                if not supplier or not offer_is_available(offer):
                    continue
                text = str(offer.get("promotionText") or "").strip()
                if text:
                    grouped_text[(supplier, " ".join(text.casefold().split()))].append((product, offer))

                discount = offer.get("discountRate")
                if discount not in (None, "", 0, 0.0):
                    numeric = detect_numeric_discount(
                        supplier=supplier,
                        source_reference=_row_ref(supplier, offer),
                        source_text=f"Sconto numerico {float(discount) * 100:g}%",
                        discount_value=discount,
                        eligible={
                            "products": [product.get("id")],
                            "eans": [product.get("ean")],
                            "source_rows": [offer.get("sourceRow")],
                        },
                        already_applied=True,
                    )
                    if numeric:
                        promotions.append(numeric)

        # Il registro si legge una volta sola: aprirlo per ogni gruppo di
        # offerte vorrebbe dire rileggere lo stesso file decine di volte per
        # ricevere sempre la stessa risposta.
        gia_compreso_nel_prezzo = _fornitori_con_promozione_inclusa()
        for (supplier, _normalized), holders in grouped_text.items():
            products = [product for product, _offer in holders]
            offers = [offer for _product, offer in holders]
            source_rows = [offer.get("sourceRow") for offer in offers if offer.get("sourceRow") is not None]
            source_reference = (
                f"{supplier}:righe:{min(source_rows)}-{max(source_rows)}"
                if source_rows
                else f"{supplier}:offerta"
            )
            promotions.extend(
                detect_promotions(
                    supplier=supplier,
                    source_reference=source_reference,
                    source_text=str(offers[0].get("promotionText") or ""),
                    eligible={
                        "products": [product.get("id") for product in products],
                        "eans": [product.get("ean") for product in products],
                        "source_rows": source_rows,
                        "mix_allowed": True if len(products) > 1 else False,
                    },
                    eligible_group=f"{supplier}:{source_reference}",
                    included_in_product=supplier in gia_compreso_nel_prezzo,
                    numeric_discount_already_applied=True,
                )
            )

        paths, senza_documento, adattatori_riconosciuti = _source_paths(review)
        # Chi legge le condizioni dal proprio listino sono i fornitori che
        # dichiarano dove le tengono: l'elenco lo fa il registro, e l'ordine
        # e' quello alfabetico perche' due letture della stessa run devono
        # dare lo stesso riepilogo.
        for fornitore, voci in sorted(fornitori_con_condizioni_dichiarate().items()):
            promotions.extend(self._condizioni_del_fornitore(
                fornitore, voci, paths.get(fornitore), fornitore in senza_documento,
                adattatori_riconosciuti.get(fornitore),
            ))
        deduplicated = {str(item.get("id")): item for item in promotions if item.get("id")}
        return self._solo_quelle_che_cambiano_qualcosa(list(deduplicated.values()))

    def _solo_quelle_che_cambiano_qualcosa(
        self, promotions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Uno sconto gia' dentro il prezzo non e' una condizione da leggere.

        ⚠ Deciso il 15 agosto 2026, con le parole di chi le leggeva: «LARICE fa
        una INFINITA di sconti; mantieni solo offerte come quella del tostapane,
        della bistecchiera, del sale lavastoviglie e simili — non mi interessa
        vedere gli sconti che mi ha fatto».  Misurato sul listino vero della
        settimana: **165 condizioni su 166 erano «Sconto numerico 10%»**, una per
        riga di listino, tutte gia' comprese nel prezzo.  Sepolte in mezzo a
        quelle, le tre soglie con omaggio non si vedevano — e una condizione che
        non si legge vale come una che non c'e'.

        La riga che resta e' quella che cambia una decisione: un premio in merce
        (`soglia_omaggio`), oppure uno sconto che il prezzo NON contiene ancora e
        che quindi sposta il totale.  Non si butta niente di calcolato: uno
        sconto `already_applied` non ha mai prodotto un prezzo — lo rifiuta
        `calculate_effective_price` — quindi qui cambia solo che cosa si legge.
        """

        tenute: list[dict[str, Any]] = []
        nel_prezzo = 0
        for promotion in promotions:
            effetto = promotion.get("economic_effect") or {}
            if effetto.get("type") == "price_discount" and effetto.get("already_applied"):
                nel_prezzo += 1
                continue
            tenute.append(promotion)
        self.sconti_gia_nel_prezzo = nel_prezzo
        return tenute

    def decorate(
        self,
        review: dict[str, Any],
        selections: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            promotions = self.detect(review)
            return decorate_review_data(review, promotions, selections=selections or {})


def _numero_o(dichiarato: Any, ripiego: int) -> int:
    """Un numero dichiarato dal registro, o il ripiego se non e' un numero.

    Un registro imparato male non deve poter fermare la lettura: se
    `data_start_row` arriva come testo, si legge dalla prima riga — cioe' un po'
    piu' di quanto serve — invece di non leggere niente.
    """

    if isinstance(dichiarato, bool) or not isinstance(dichiarato, int):
        return ripiego
    return dichiarato if dichiarato >= 1 else ripiego
