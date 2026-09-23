#!/usr/bin/env python3
"""La consegna: una cartella datata per ogni compilazione, e come si scarica.

Fase 6d.  Fino a ieri ogni compilazione scriveva in `app/data/current/outputs`
con un nome fisso: la compilazione successiva sovrascriveva in silenzio i
listini pronti della volta prima, e chi ricompilava prima di aver inviato
l'ordine perdeva i file senza che niente glielo dicesse.  Qui dentro c'e' tutto
quello che serve perche' questo non succeda piu': il nome della cartella, la
sua creazione atomica, l'audit che la descrive, l'elenco delle compilazioni
ricavato dalle cartelle vere e le due difese che impediscono a un percorso
costruito a mano di uscire dalla cartella degli ordini.

Il modulo e' autonomo: **non importa `server`** e non sa che cosa sia una rotta
HTTP.  Le uniche stringhe che sembrano rotte sono i campi `url` e `zipUrl`
della voce d'elenco (§4 del contratto), che il servizio rimanda tali e quali
alla pagina: sono dati della voce, non conoscenza del protocollo.

Sola libreria standard.
"""

from __future__ import annotations

import io
import json
import os
import re
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote


SCHEMA_AUDIT = 1
NOME_AUDIT = "compilazione.json"
NOME_PIANO = "final_order_plan.json"
MESI = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre")

# Il prefisso delle rotte di consegna.  Sta qui e non in `server.py` perche' la
# voce dell'elenco (§4) porta gia' gli URL costruiti: la pagina non deve
# incollare pezzi di percorso a mano.
PREFISSO_URL = "/ordini"

# Massimo suffisso provato quando due compilazioni cadono nello stesso minuto.
MAX_SUFFISSO = 99

# I caratteri che Windows non accetta in un nome di file.  I due punti sono in
# questo elenco: e' il motivo per cui l'ora si scrive `1435` e non `14:35`.
VIETATI_WINDOWS = '<>:"/\\|?*'

# `AAAA-MM-GG_HHMM`, con l'eventuale suffisso progressivo `_2`, `_3`, ...
NOME_CARTELLA_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(?:_(\d+))?$")

# L'elenco dei prodotti che nessun fornitore può dare (punto 5).  Il nome e il
# `tipo` stanno qui e non in `app/da_reperire.py` perché è **questo** modulo che
# deve riconoscere il file dal solo nome, quando la cartella si rilegge senza
# audit; `da_reperire` importa questo, non viceversa, e le due costanti restano
# una sola verità invece di due che possono allontanarsi in silenzio.
PREFISSO_DA_REPERIRE = "Prodotti da reperire "
TIPO_DA_REPERIRE = "da_reperire"

# I tipi che si consegnano: quelli che si contano per decidere se il pulsante
# «Scarica» compare e che finiscono nello zip.  L'elenco dei prodotti da
# reperire è merce da consegnare quanto un listino: una compilazione fatta di
# **soli** prodotti da reperire — il caso in cui l'utente non può ordinare
# niente da nessuno — altrimenti non avrebbe nessun collegamento per scaricarli.
TIPI_DA_CONSEGNARE = (
    "listino",
    TIPO_DA_REPERIRE,
)


# --------------------------------------------------------------------------
# Nomi
# --------------------------------------------------------------------------

def nome_cartella(momento: datetime) -> str:
    """Il nome della cartella di una compilazione: `2026-08-12_1435`."""
    return momento.strftime("%Y-%m-%d_%H%M")


def data_leggibile(momento: datetime) -> str:
    """`12 agosto 2026`.

    I nomi dei mesi vengono da `MESI` e non da `strftime("%B")`: quello dipende
    dalla localizzazione del sistema e su questa macchina darebbe `August`.
    """
    return f"{momento.day} {MESI[momento.month - 1]} {momento.year}"


def _ripulisci_nome_file(valore: str) -> str:
    """Rende `valore` usabile come pezzo di nome di file su Windows.

    Punti e spazi in coda diventano `_`: Windows li toglierebbe in silenzio, e
    due fornitori che differiscono solo per quelli finirebbero nello stesso
    file senza che nessuno se ne accorga.
    """
    ripulito = "".join(
        "_" if carattere in VIETATI_WINDOWS or ord(carattere) < 32 or ord(carattere) == 127 else carattere
        for carattere in str(valore)
    )
    coda = len(ripulito) - len(ripulito.rstrip(". "))
    if coda:
        ripulito = ripulito[:-coda] + "_" * coda
    return ripulito


def nome_listino(fornitore: str, momento: datetime, estensione: str = ".xlsx") -> str:
    """`Ordine LARICE — 12 agosto 2026.xlsx`, con l'em dash U+2014 spaziato.

    L'estensione la decide il documento di partenza e non questa funzione: a
    Noce si rimanda il **loro** file, che e' un `.xls`, e rinominarlo
    `.xlsx` vorrebbe dire consegnare un documento che dichiara di essere quello
    che non e'.

    L'em dash regge: `zipfile` alza la bandiera 0x800 e Esplora risorse rilegge
    il nome carattere per carattere (misurato prima della 6d).  Quello che non
    regge e' l'intestazione HTTP, e se ne occupa `intestazione_allegato`.
    """
    coda = _ripulisci_nome_file(str(estensione or ".xlsx"))
    if not coda.startswith("."):
        coda = "." + coda
    return f"Ordine {_ripulisci_nome_file(fornitore).upper()} — {data_leggibile(momento)}{coda}"


def _pezzi_nome_cartella(nome: str) -> tuple[datetime, int | None] | None:
    """Il momento e il suffisso letti dal nome della cartella, o `None`."""
    corrispondenza = NOME_CARTELLA_RE.match(str(nome or ""))
    if corrispondenza is None:
        return None
    anno, mese, giorno, ora, minuto, suffisso = corrispondenza.groups()
    try:
        momento = datetime(int(anno), int(mese), int(giorno), int(ora), int(minuto))
    except ValueError:
        # `2026-02-30_1435` ha la forma giusta e non e' una data.
        return None
    return momento, (int(suffisso) if suffisso is not None else None)


def etichetta(nome_cartella: str) -> str:
    """`12 agosto 2026, 14:35`, oppure `... (2)` quando c'e' il suffisso.

    Un nome che non si legge come data torna com'e': una cartella creata a mano
    dentro `ordini` non deve far esplodere l'elenco.
    """
    pezzi = _pezzi_nome_cartella(nome_cartella)
    if pezzi is None:
        return str(nome_cartella)
    momento, suffisso = pezzi
    testo = f"{data_leggibile(momento)}, {momento.hour:02d}:{momento.minute:02d}"
    return f"{testo} ({suffisso})" if suffisso is not None else testo


def nome_zip(nome_cartella: str) -> str:
    """`Listini pronti per invio 2026-08-12 14-35.zip`.

    I due punti dell'ora diventano un trattino perche' il nome finisce in un
    file che l'utente salva su disco.
    """
    pezzi = _pezzi_nome_cartella(nome_cartella)
    if pezzi is None:
        return f"Listini pronti per invio {_ripulisci_nome_file(nome_cartella)}.zip"
    momento, suffisso = pezzi
    testo = momento.strftime("%Y-%m-%d %H-%M")
    if suffisso is not None:
        testo = f"{testo} ({suffisso})"
    return f"Listini pronti per invio {testo}.zip"


# --------------------------------------------------------------------------
# La cartella
# --------------------------------------------------------------------------

def crea_cartella(radice: Path, momento: datetime) -> Path:
    """Crea la cartella della compilazione e la restituisce.

    `mkdir()` **senza `exist_ok`**, catturando `FileExistsError`: e' l'unico
    modo atomico di dire «l'ho creata io».  Un `if not exists(): mkdir()` e' una
    corsa, e due compilazioni nello stesso minuto finirebbero nella stessa
    cartella sovrascrivendosi -- cioe' esattamente il difetto che la 6d chiude.
    """
    radice = Path(radice)
    radice.mkdir(parents=True, exist_ok=True)
    base = nome_cartella(momento)
    for tentativo in range(1, MAX_SUFFISSO + 1):
        candidata = radice / (base if tentativo == 1 else f"{base}_{tentativo}")
        try:
            candidata.mkdir()
        except FileExistsError:
            continue
        return candidata
    raise ValueError("Troppe compilazioni nello stesso minuto")


def ultima(radice: Path) -> Path | None:
    """La cartella della compilazione piu' recente **che ha un piano**, o `None`.

    Non basta «la cartella piu' recente»: il riquadro nuovo insegna all'utente
    che `ordini` e' il suo archivio, e appena ci crea dentro una sottocartella
    sua — o Windows gli lascia una «Nuova cartella» per un clic sbagliato —
    quella diventerebbe l'ultima compilazione, e chi ha appena compilato si
    sentirebbe rispondere che compilazioni non ce ne sono.  Una cartella senza
    piano non e' una compilazione.
    """
    radice = Path(radice)
    for voce_elenco in elenco(radice):
        candidata = radice / voce_elenco["cartella"]
        if (candidata / NOME_PIANO).is_file():
            return candidata
    return None


# --------------------------------------------------------------------------
# L'audit
# --------------------------------------------------------------------------

def scrivi_audit(cartella: Path, audit: dict) -> Path:
    """Scrive `compilazione.json` in binario, in modo atomico.

    In binario perche' su Windows `write_text` trasformerebbe ogni `\\n` in
    `\\r\\n`, e questo file lo rileggono anche gli strumenti a riga di comando.
    Temporaneo accanto + `os.replace`: un audit troncato a meta' scrittura
    direbbe il falso sulla compilazione.
    """
    percorso = Path(cartella) / NOME_AUDIT
    testo = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    temporaneo = percorso.with_name(percorso.name + ".tmp")
    temporaneo.write_bytes(testo.encode("utf-8"))
    os.replace(temporaneo, percorso)
    return percorso


def leggi_audit(cartella: Path) -> dict | None:
    """Il dizionario dell'audit, oppure `None`.  **Non solleva mai.**

    Un audit rotto e' un problema di quella compilazione, non dell'elenco di
    tutte le altre.
    """
    try:
        dati = json.loads((Path(cartella) / NOME_AUDIT).read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return dati if isinstance(dati, dict) else None


def tipo_file(nome: str) -> str:
    """Il `tipo` dedotto dal solo nome, per le cartelle senza audit leggibile.

    Un nome che qui non e' previsto torna `"altro"`, e nell'elenco compare come
    «altro documento».  E' il ripiego che regge le cartelle d'ordine vecchie:
    dentro ci possono essere artefatti di servizio che il programma non produce
    piu' — e una cartella non deve ne' sparire dall'elenco ne' far esplodere la
    voce solo perche' contiene un file di un'epoca precedente.
    """
    if nome == NOME_PIANO:
        return "piano"
    # ⚠ Prima del ripiego sull'estensione: l'elenco dei prodotti da reperire è
    # un `.xlsx` e senza questo ramo verrebbe scambiato per un listino, cioè
    # finirebbe nello zip diretto al fornitore — un foglio che dice quello che
    # quel fornitore *non* ci ha venduto.
    ripulito = str(nome or "").casefold()
    if ripulito.startswith(PREFISSO_DA_REPERIRE.casefold()) and ripulito.endswith(".xlsx"):
        return TIPO_DA_REPERIRE
    # Il `.xls` c'e' perche' a Noce si rimanda il loro documento, che e' un
    # Excel 97-2003: senza, la copia compilata sarebbe finita fra gli «altri» e
    # la pagina non l'avrebbe contata come un listino da spedire.
    if nome.casefold().endswith((".xlsx", ".xls")):
        return "listino"
    return "altro"


def e_documento(nome: str) -> bool:
    """Se quel nome e' un documento della compilazione o roba di servizio.

    Restano fuori quattro famiglie, e ognuna per un motivo suo:

    - i nomi che cominciano per `.`, perche' il writer lavora in
      `.ordine-temporaneo-*`;
    - `~$...`, che su Windows e' il file di proprieta' che Excel deposita
      accanto a un documento aperto.  Senza questo filtro basta aprire una copia
      per controllarla perche' finisca nello zip diretto al fornitore, e nella
      cartella senza audit verrebbe anche contato come un listino;
    - i `.tmp`, che sono le scritture atomiche colte a meta';
    - l'audit stesso, che descrive la cartella e non e' merce da consegnare.
    """
    nome = str(nome or "")
    if not nome or nome.startswith(".") or nome.startswith("~$"):
        return False
    if nome == NOME_AUDIT or nome.casefold().endswith(".tmp"):
        return False
    return True


# --------------------------------------------------------------------------
# L'elenco
# --------------------------------------------------------------------------

def _numero(valore: Any) -> float | None:
    if isinstance(valore, bool) or not isinstance(valore, (int, float)):
        return None
    return float(valore)


def _url_file(nome_della_cartella: str, nome: str) -> str:
    return f"{PREFISSO_URL}/{quote(nome_della_cartella, safe='')}/{quote(nome, safe='')}"


def _tipi_dall_audit(audit: dict) -> dict[str, str]:
    """Nome del documento -> `tipo`, letto dall'audit."""
    tipi: dict[str, str] = {}
    for riga in audit.get("file") or []:
        if not isinstance(riga, dict):
            continue
        nome = riga.get("nome")
        if not isinstance(nome, str) or not nome:
            continue
        tipo = riga.get("tipo")
        tipi[nome] = tipo if isinstance(tipo, str) and tipo else tipo_file(nome)
    return tipi


def _nomi_sul_disco(cartella: Path) -> list[str]:
    """I documenti che ci sono **adesso** nella cartella, in ordine di nome."""
    try:
        with os.scandir(cartella) as scansione:
            return sorted(
                trovato.name for trovato in scansione
                if trovato.is_file() and e_documento(trovato.name)
            )
    except OSError:
        return []


def voce(cartella: Path, *, etichetta_fornitore: Callable[[str], str] | None = None) -> dict:
    """La voce dell'elenco (§4 del contratto), in camelCase per `app.js`.

    Una cartella che c'e' compare **sempre**: se l'audit manca o non si legge la
    voce esiste lo stesso con `completa: false`, perche' sparire dall'elenco
    sarebbe il modo peggiore di segnalare un audit rotto.

    ⚠ L'elenco dei documenti si legge **dal disco**, non dall'audit, e l'audit
    dice soltanto che tipo sia ciascuno.  L'audit descrive la cartella
    nell'istante in cui e' stato scritto; da li' in poi l'utente vive in quella
    cartella — allega un listino a una mail, lo sposta, lo rinomina.  Un elenco
    costruito dall'audit continuerebbe a contare un documento che non c'e' piu'
    e offrirebbe un collegamento che risponde 404; e lo zip, che chiede ogni
    nome dichiarato, si rifiuterebbe di consegnare anche i listini rimasti.
    Quello che l'audit nomina e il disco non ha finisce in `mancanti`, perche'
    sparire in silenzio e' proprio il difetto che questa fase chiude.
    """
    cartella = Path(cartella)
    nome_della_cartella = cartella.name
    audit = leggi_audit(cartella)
    nominatore = etichetta_fornitore if callable(etichetta_fornitore) else (lambda identificativo: identificativo.upper())
    sul_disco = _nomi_sul_disco(cartella)

    if audit is None:
        file_voci = [
            {"nome": nome, "tipo": tipo_file(nome), "url": _url_file(nome_della_cartella, nome)}
            for nome in sul_disco
        ]
        mancanti: list[str] = []
        completa = False
        creato_il: Any = None
        stato = "SCONOSCIUTO"
        fornitori: list[dict] = []
        totale_netto: float | None = None
        righe: Any = None
        avvisi: list[str] = []
    else:
        tipi = _tipi_dall_audit(audit)
        file_voci = [
            {
                # Un documento comparso dopo la scrittura dell'audit e' `altro`:
                # non sappiamo di chi sia ne' da dove venga, e quindi non entra
                # fra i listini da consegnare al fornitore.
                "nome": nome,
                "tipo": tipi.get(nome, "altro"),
                "url": _url_file(nome_della_cartella, nome),
            }
            for nome in sul_disco
        ]
        mancanti = [nome for nome in sorted(tipi) if nome not in set(sul_disco)]
        completa = True
        creato_il = audit.get("creato_il") if isinstance(audit.get("creato_il"), str) else None
        stato = audit.get("stato") if isinstance(audit.get("stato"), str) and audit.get("stato") else "SCONOSCIUTO"
        totali = audit.get("totali_netti")
        totali = totali if isinstance(totali, dict) else {}
        fornitori = [
            {
                "id": identificativo,
                "nome": nominatore(identificativo),
                "totaleNetto": _numero(totali.get(identificativo)),
            }
            for identificativo in (audit.get("fornitori") or [])
            if isinstance(identificativo, str) and identificativo
        ]
        totale_netto = _numero(audit.get("totale_netto"))
        righe = audit.get("righe") if isinstance(audit.get("righe"), int) and not isinstance(audit.get("righe"), bool) else None
        # Gli avvisi che l'audit ha registrato: una copia scartata, un nome
        # rimasto brutto, un ordine non entrato fra quelli da controllare. Fino
        # a oggi restavano dentro `compilazione.json` e la voce dell'elenco non
        # li nominava: la notizia viveva quanto la pagina che l'aveva vista, e
        # ricaricando spariva (revisione di regressione del 14 agosto 2026).
        avvisi = [str(voce).strip() for voce in (audit.get("avvisi") or []) if str(voce).strip()]

    # I listini si contano su quello che c'e', non su quello che l'audit
    # dichiarava: e' il numero che la pagina mostra, e dev'essere vero adesso.
    #
    # ⚠ Sono due domande diverse e vogliono due numeri diversi. La pagina scrive
    # «2 listini», e l'elenco dei prodotti da reperire un listino non e': contarlo
    # li' direbbe una cosa falsa. Il pulsante «Scarica» invece chiede «c'e'
    # qualcosa da portare via?», e li' quell'elenco conta eccome — una
    # compilazione in cui non si e' potuto ordinare niente da nessuno produce
    # **solo** lui, e con il conteggio dei soli listini resterebbe senza nessun
    # collegamento per scaricarlo.
    listini = sum(1 for riga in file_voci if riga["tipo"] == "listino")
    da_consegnare = sum(1 for riga in file_voci if riga["tipo"] in TIPI_DA_CONSEGNARE)

    return {
        "cartella": nome_della_cartella,
        "etichetta": etichetta(nome_della_cartella),
        "creatoIl": creato_il,
        "stato": stato,
        "fornitori": fornitori,
        "totaleNetto": totale_netto,
        "righe": righe,
        "listini": listini,
        "file": file_voci,
        # Quello che l'audit nomina e sul disco non c'e' piu'.  Non e' un
        # errore del programma — e' l'utente che ha spostato un documento — ma
        # va detto, perche' chi cerca un listino di due settimane fa deve sapere
        # che l'ha spostato lui e non che il programma l'ha perso.
        "mancanti": mancanti,
        "avvisi": avvisi,
        # Senza niente da consegnare il pulsante non deve comparire: e' `null`,
        # non un URL che risponderebbe 404.
        "zipUrl": (f"{PREFISSO_URL}/{quote(nome_della_cartella, safe='')}/zip" if da_consegnare else None),
        "zipNome": nome_zip(nome_della_cartella),
        "completa": completa,
    }


def _chiave_ordine(cartella: Path) -> float:
    """Quando è stata fatta la compilazione: l'audit se si legge, altrimenti il disco."""
    audit = leggi_audit(cartella)
    if isinstance(audit, dict) and isinstance(audit.get("creato_il"), str):
        try:
            return datetime.fromisoformat(audit["creato_il"]).timestamp()
        except (ValueError, OSError, OverflowError):
            pass
    try:
        return cartella.stat().st_mtime
    except OSError:
        return 0.0


def elenco(radice: Path, *, etichetta_fornitore: Callable[[str], str] | None = None) -> list[dict]:
    """Tutte le compilazioni, dalla piu' recente.

    Si scandiscono le cartelle vere: un indice a parte sarebbe una cosa in piu'
    che puo' disallinearsi dal disco.

    Si elenca solo cio' che `cartella_sicura` accetterebbe, cioe' esattamente
    quello che le rotte sanno servire.  Una giunzione NTFS dentro `ordini` — un
    `mklink /J` verso una cartella di rete, un backup ripristinato male — si
    risolve fuori e viene rifiutata: senza questo filtro comparirebbe come una
    compilazione, con dentro i nomi dei file di fuori e collegamenti che
    rispondono 404.
    """
    radice = Path(radice)
    try:
        with os.scandir(radice) as scansione:
            nomi = [
                trovata.name for trovata in scansione
                if trovata.is_dir() and not trovata.name.startswith(".")
            ]
    except OSError:
        return []
    cartelle = [c for c in (cartella_sicura(radice, nome) for nome in nomi) if c is not None]
    ordinate = sorted(cartelle, key=lambda c: (_chiave_ordine(c), c.name), reverse=True)
    return [voce(c, etichetta_fornitore=etichetta_fornitore) for c in ordinate]


# --------------------------------------------------------------------------
# Le difese sul percorso
# --------------------------------------------------------------------------

def _dentro(figlio: Path, genitore: Path) -> bool:
    """`figlio` sta dentro `genitore`?  Su percorsi già risolti."""
    try:
        figlio.relative_to(genitore)
        return True
    except ValueError:
        return False


def _percorso_sicuro(genitore: Path, nome: str, *, vuole_cartella: bool) -> Path | None:
    """La difesa a lista bianca condivisa da `cartella_sicura` e `file_sicuro`.

    Non si filtrano i caratteri cattivi -- filtrare e' una lista nera e le liste
    nere si dimenticano sempre qualcosa.  Si chiede al disco che cosa esiste
    davvero e si accetta solo un nome che compare **identico** in quella
    scansione: i flussi alternativi NTFS (`compilazione.json:$DATA`) e i nomi
    riservati non compaiono mai in una scansione, quindi cadono qui senza che
    nessuno debba averli previsti.  Poi si verifica comunque il contenimento,
    perche' un collegamento simbolico compare nella scansione ma si risolve
    dove gli pare.

    Chi chiama arriva da `unquote(...)`: `..%2f..%2fsecrets.json` a questo punto
    e' gia' `../../secrets.json`, quindi la difesa lavora sul nome decodificato.
    """
    if not isinstance(nome, str) or not nome:
        return None
    if nome in (".", ".."):
        return None
    if "/" in nome or "\\" in nome or "\x00" in nome:
        return None

    genitore = Path(genitore)
    try:
        with os.scandir(genitore) as scansione:
            esiste = any(
                trovata.name == nome and (trovata.is_dir() if vuole_cartella else trovata.is_file())
                for trovata in scansione
            )
    except OSError:
        return None
    if not esiste:
        return None

    percorso = genitore / nome
    try:
        risolto = percorso.resolve()
        radice_risolta = genitore.resolve()
    except OSError:
        return None
    if risolto == radice_risolta or not _dentro(risolto, radice_risolta):
        return None
    return percorso


def cartella_sicura(radice: Path, nome: str) -> Path | None:
    """La cartella di quella compilazione, o `None` se il nome non è legittimo."""
    return _percorso_sicuro(radice, nome, vuole_cartella=True)


def file_sicuro(cartella: Path, nome: str) -> Path | None:
    """Il file dentro quella compilazione, o `None` se il nome non è legittimo."""
    return _percorso_sicuro(cartella, nome, vuole_cartella=False)


# --------------------------------------------------------------------------
# La consegna vera e propria
# --------------------------------------------------------------------------

def zip_in_memoria(cartella: Path, nomi: Sequence[str]) -> bytes:
    """I byte di uno zip costruito al momento in memoria.

    Non si scrive mai su disco: sarebbe l'ennesimo artefatto da tenere
    allineato con la cartella che dovrebbe descrivere.  Ogni nome ripassa da
    `file_sicuro` anche se chi chiama lo ha appena letto dall'audit: l'audit e'
    un file di testo e questa e' l'ultima porta prima di leggere il disco.
    """
    nomi = list(nomi or [])
    if not nomi:
        raise ValueError("Non ci sono listini da mettere nello zip")

    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", zipfile.ZIP_DEFLATED) as archivio:
        for nome in nomi:
            percorso = file_sicuro(cartella, nome)
            if percorso is None:
                # Non si ripete il nome chiesto: finirebbe in un messaggio
                # all'utente e direbbe a chi prova che cosa ha provato.
                raise ValueError("Uno dei documenti chiesti non è in questa compilazione")
            # `arcname=nome` e non il percorso: dentro lo zip niente cartelle.
            archivio.write(percorso, arcname=nome)
    return memoria.getvalue()


def _ripiego_ascii(nome: str) -> str:
    """Il nome ridotto ad ASCII per la parte `filename=` dell'intestazione."""
    scomposto = unicodedata.normalize("NFKD", str(nome))
    pezzi = []
    for carattere in scomposto:
        if unicodedata.combining(carattere):
            continue  # l'accento della `e` accentata, gia' staccato da NFKD
        if carattere in '"\\':
            continue  # chiuderebbe le virgolette dell'intestazione
        codice = ord(carattere)
        if codice < 32 or codice == 127:
            continue  # un `\r\n` qui dentro sarebbe un'iniezione di intestazioni
        pezzi.append(carattere if codice < 128 else "-")
    ripiego = "".join(pezzi).strip()
    return ripiego or "allegato"


def intestazione_allegato(nome: str) -> str:
    """Il valore di `Content-Disposition` per uno scaricamento, in RFC 5987.

    Misurato prima della 6d: `attachment; filename="Ordine LARICE — 12 agosto
    2026.xlsx"` **non si codifica in latin-1**, e `BaseHTTPRequestHandler`
    scrive le intestazioni in latin-1: la risposta morirebbe con
    `UnicodeEncodeError` mentre scrive l'intestazione, cioe' a corpo gia'
    promesso.  Quindi ripiego ASCII fra virgolette per i lettori vecchi, piu'
    `filename*=UTF-8''` per tutti gli altri, che e' quello che i browser di oggi
    preferiscono.
    """
    return f"attachment; filename=\"{_ripiego_ascii(nome)}\"; filename*=UTF-8''{quote(str(nome), safe='')}"
