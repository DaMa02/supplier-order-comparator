#!/usr/bin/env python3
"""L'orchestratore della catena: dal pulsante al confronto pronto.

E' il pezzo che toglie di mezzo l'assistente di sviluppo.  Fino a ieri i nove
passi si lanciavano a mano, uno per uno, leggendo l'uscita di ognuno per
decidere se il seguente si poteva fare; qui quella lettura la fa il programma,
e la fa in modo che **un fallimento non possa somigliare a un successo**.

Tre regole governano tutto il resto.

1. **Ogni esecuzione ha la sua cartella datata** sotto `<dati>/esecuzioni/`, e
   nasce vuota.  Non e' una comodita' d'archivio: e' il modo con cui «un passo
   saltato lascia in giro il file di ieri» smette di essere possibile.  Un
   artefatto che un passo doveva produrre e che non c'e' e' un guasto
   dichiarato, non un file vecchio riletto come fresco.
2. **Il confronto vivo si sostituisce alla fine, in un colpo solo.**  Se una
   fase fallisce, `review_data.json` resta esattamente quello di prima: chi ha
   premuto il pulsante perde la run, non il confronto su cui stava lavorando.
3. **Il controllo sui numeri avvisa e non ferma** (decisione di Daniele del 12
   agosto 2026).  Le fermate sono tre, e sono quelle in cui il programma non
   ha l'autorita' per decidere perche' l'ingresso non si puo' usare: uno
   schema che il registro non conosce, un documento che non si legge affatto,
   e un listino da cui non esce nemmeno una riga ordinabile
   (`FORNITORE_SENZA_RIGHE`, classificata cosi' il 13 agosto 2026 su delega
   di Daniele: un fornitore caricato apposta che sparisse dal confronto con
   un semplice avviso farebbe compilare gli ordini senza di lui, a prezzi
   potenzialmente peggiori, mentre la fermata costa solo un rilancio — il
   confronto vivo resta quello di prima).

⚠ Quello che questo modulo **non** fa: non manda ordini e non tocca gli
originali dei fornitori. Il confronto vivo cambia soltanto all'attivazione;
l'unica altra scrittura persistente e' nel registro degli adattatori, dopo che
una mappatura confermata ha gia' superato manifest, parsing e costruzione.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


APP_DIR = Path(__file__).resolve().parent
SKILL_ROOT = APP_DIR.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
REFERENCES_DIR = SKILL_ROOT / "references"

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import consegna  # noqa: E402
# «Questa offerta si puo' ordinare» ha una risposta sola e sta nel suo modulo.
# Qui era riscritta a mano, dentro la funzione che azzera le quantita' dopo un
# ricalcolo: il giorno in cui la regola cresce nel servizio e non qui, il
# ricalcolo non azzera una quantita' che la compilazione poi rifiuta, e il
# sintomo si vede una settimana dopo e altrove.
from offerta import offer_is_available, offer_supplier_id  # noqa: E402
import registro  # noqa: E402
import scrittura_sicura  # noqa: E402
import schema_mapping  # noqa: E402
import versione_del_codice  # noqa: E402
from build_review_data import impronta_articolo  # noqa: E402
from conferme import impronta_prodotto  # noqa: E402


# --------------------------------------------------------------------------
# Il contratto verso la pagina
# --------------------------------------------------------------------------

FASI: tuple[str, ...] = (
    "PROFILAZIONE",
    "RICONOSCIMENTO",
    "VALIDAZIONE",
    "PARSING",
    "SHORTLIST",
    "VALUTAZIONE_AI",
    "RISOLUZIONE",
    "COSTRUZIONE",
    "ATTIVAZIONE",
)

TITOLI_FASI: dict[str, str] = {
    "PROFILAZIONE": "Lettura dei documenti",
    # I titoli si leggono in pagina, e si leggono soprattutto quando qualcosa
    # non va: «schema» e «manifest» sono strutture interne, e sapere che si e'
    # fermato il «controllo del manifest» non dice niente a chi deve decidere
    # se rilanciare o guardare i documenti.
    "RICONOSCIMENTO": "Riconoscimento delle colonne",
    "VALIDAZIONE": "Controllo dell'elenco dei documenti",
    "PARSING": "Lettura dei listini",
    "SHORTLIST": "Candidati per i prodotti senza EAN",
    "VALUTAZIONE_AI": "Valutazione dei candidati",
    "RISOLUZIONE": "Unione delle decisioni",
    "COSTRUZIONE": "Costruzione del confronto",
    "ATTIVAZIONE": "Attivazione del confronto",
}

# Quanto puo' stare zitto un passo prima che si dichiari impiantato, in
# secondi.  ⚠ Sono tetti, non attese: nessuna run lecita ci arriva vicino — la
# fase AI, la piu' lunga, e' stata misurata in 105 secondi — e sono larghi
# apposta perche' il computer del negozio e' piu' lento di quello su cui si
# misura.  Il tetto serve a un caso solo: un passo che non risponde piu'.
#
# ⚠ E adesso misura davvero il **silenzio**, non la durata: fino al 22 agosto
# 2026 questa riga diceva «quanto puo' stare zitto» e il codice faceva
# `wait(timeout=...)`, cioe' contava dall'inizio.  Una fase AI sana e lunga —
# tanti casi, un computer lento, un modello che risponde piano — veniva uccisa
# a 900 secondi e raccontata come «non ha risposto», buttando via le chiamate
# gia' pagate.  Il tetto si riarma a ogni riga che il passo scrive; per i passi
# che non stampano niente le due misure coincidono e non cambia nulla.
# Senza, quel passo teneva `lucchetto_lavori` per sempre, ogni «Ricalcola»
# successivo rispondeva 409, `POST /api/spegni` si rifiutava di spegnere e il
# lanciatore riusava il server vecchio — cioe' si inchiodava anche la sola
# strada con cui una correzione arriva in negozio.  All'utente restava
# chiudere la finestra a forza.
TETTO_DELLE_FASI: dict[str, float] = {"VALUTAZIONE_AI": 900.0}
TETTO_PREDEFINITO_DI_FASE = 300.0


def tetto_della_fase(fase: str) -> float:
    """I secondi oltre i quali quel passo si considera piantato."""

    return TETTO_DELLE_FASI.get(fase, TETTO_PREDEFINITO_DI_FASE)


IN_ATTESA = "IN_ATTESA"
IN_CORSO = "IN_CORSO"
COMPLETATO = "COMPLETATO"
ERRORE = "ERRORE"
INTERROTTO = "INTERROTTO"

STATI_IN_CORSO = {IN_CORSO}

NOME_STATO = "pipeline_status.json"
NOME_AUDIT_ESECUZIONE = "esecuzione.json"
NOME_DECISIONI_MANUALI = "decisioni_schemi.json"

# La cartella di una run non e' una compilazione: le due radici sono diverse
# apposta, cosi' l'archivio dell'utente («ordini») non si mescola agli
# artefatti di lavoro, che a nessuno servono dopo qualche settimana.
NOME_RADICE_ESECUZIONI = "esecuzioni"

# Quante cartelle di lavoro si tengono.  ⚠ Non ne cancellava nessuna: ogni
# ricalcolo, riuscito o fallito, ne lascia una — misurata a 13 MB sul confronto
# vero — e sul PC del negozio faceva circa settecento megabyte l'anno con un
# ricalcolo a settimana, molti di piu' contando i tentativi.  Non e' lo spazio
# in se': a disco pieno si aprono in fila i guasti che questo file passa la vita
# a evitare — lo stato della catena che non si scrive, il lucchetto che resta
# preso, la copia di sicurezza che salta in silenzio.  Cinque sono piu' di un
# mese di lavoro normale, e le due cartelle che servono ancora non si contano:
# quella della run di adesso e quella del confronto vivo.
ESECUZIONI_DA_TENERE = 5

# Quanto puo' cambiare il numero di righe di un listino rispetto alla volta
# prima senza che valga la pena dirlo.  Non e' una soglia che blocca: e' la
# soglia oltre la quale l'avviso compare in cima alla pagina.
SCARTO_RIGHE_DA_SEGNALARE = 0.15
# Sul prezzo mediano si e' piu' sensibili: un listino che cambia tutti i prezzi
# del 10% e' una cosa che si vuole sapere prima di mandare un ordine.
SCARTO_PREZZO_DA_SEGNALARE = 0.10

MASSIMO_AVANZAMENTO_STDERR = 4000

PREFISSO_AVANZAMENTO = "AVANZAMENTO "

# Il programma sul disco non è più quello che sta girando in memoria. Non è un
# guasto del codice ed è l'utente a poterlo risolvere, quindi ha un codice suo:
# «riprova» sarebbe un consiglio sbagliato, qui si chiude e si riapre.
CODICE_CAMBIATO_DOPO_L_AVVIO = "CODICE_CAMBIATO_DOPO_L_AVVIO"


class LavoroGiaInCorso(RuntimeError):
    """Un secondo ricalcolo mentre il primo sta ancora lavorando."""


@dataclass
class Fermata(Exception):
    """La catena si ferma, e il motivo e' una cosa che deve fare l'utente.

    Non e' un guasto del programma: e' uno dei due casi in cui il programma non
    ha l'autorita' per decidere da solo.  Porta con se' il codice, la frase da
    mostrare e l'elenco dei documenti coinvolti, perche' «non riesco a
    continuare» senza i nomi non e' un messaggio, e' un ostacolo.
    """

    codice: str
    messaggio: str
    documenti: list[str] = field(default_factory=list)
    dettaglio: str = ""
    # Per ogni documento coinvolto, PERCHE' ci finisce: "SCONOSCIUTO" (il
    # registro non lo conosce) oppure "VARIATO" (lo conosce, ed e' il documento
    # che non combacia piu' con la firma).  ⚠ `documenti` resta la lista piatta:
    # e' quella con cui il servizio sa QUALI file configurare, e la ragione e'
    # un'altra domanda.  Tenerle nello stesso campo e' esattamente l'errore che
    # faceva scrivere «non riconosco le colonne» sul listino di CIPRESSO.
    motivi: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - solo per i traceback
        return self.messaggio


class ComandoTroppoLungo(Exception):
    """Il passo non ha risposto entro il suo tetto ed e' stato ucciso.

    Non porta il nome della fase perche' chi esegue il comando non lo sa: la
    fase la mette `_esegui`, che e' anche l'unico posto in cui si sa come si
    chiama in italiano.
    """

    def __init__(self, secondi: float, stderr: str = "") -> None:
        super().__init__(f"nessuna risposta entro {secondi:g} secondi")
        self.secondi = secondi
        self.stderr = stderr


@dataclass(frozen=True)
class RisultatoComando:
    """Che cosa ha fatto un passo della catena: uscita, uscite di testo, JSON."""

    uscita: int
    stdout: str
    stderr: str

    @property
    def riepilogo(self) -> dict[str, Any]:
        """L'ultimo oggetto JSON stampato dal comando, o `{}`.

        Ogni script della catena chiude stampando il proprio riepilogo: e' da
        li' che arrivano i numeri che la pagina mostra.  Un comando che stampa
        altro non e' un guasto — il numero semplicemente non c'e'.
        """

        testo = self.stdout.strip()
        if not testo:
            return {}
        inizio = testo.find("{")
        while inizio != -1:
            try:
                valore = json.loads(testo[inizio:])
            except json.JSONDecodeError:
                inizio = testo.find("{", inizio + 1)
                continue
            return valore if isinstance(valore, dict) else {}
        return {}


AscoltaAvanzamento = Callable[[dict[str, Any]], None]
# ⚠ `...` e non la firma per esteso: l'esecutore riceve anche `timeout_secondi`
# come argomento con nome, e `Callable` non sa dichiarare gli argomenti con nome.
Esecutore = Callable[..., RisultatoComando]


def esegui_comando(
    comando: Sequence[str],
    cartella: Path,
    avanzamento: AscoltaAvanzamento,
    *,
    timeout_secondi: float | None = None,
) -> RisultatoComando:
    """Esegue un passo della catena come sottoprocesso, leggendo l'avanzamento.

    `stdout` si raccoglie tutto insieme perche' e' il riepilogo finale;
    `stderr` si legge **riga per riga mentre il comando lavora**, perche' e' da
    li' che passa l'avanzamento della fase AI, che da sola dura due minuti.  Un
    programma che sta zitto per due minuti sembra rotto.

    `PYTHONIOENCODING` non e' un vezzo: senza, su Windows il figlio scrive nel
    tubo con la codifica del sistema e la prima virgoletta all'italiana di un
    messaggio lo fa morire di `UnicodeEncodeError` a lavoro finito.
    """

    ambiente = dict(os.environ)
    ambiente["PYTHONIOENCODING"] = "utf-8"
    processo = subprocess.Popen(
        list(comando),
        cwd=str(cartella),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=ambiente,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    pezzi_errore: list[str] = []
    # L'ultimo segno di vita del figlio. ⚠ Serve a far misurare al tetto quello
    # che il suo nome dice — «quanto puo' stare zitto un passo» — e non la
    # durata totale: `wait(timeout=...)` da solo uccideva una fase sana ma lunga
    # e la raccontava come «non ha risposto». Per i passi che non stampano
    # niente le due misure coincidono, e per loro non cambia niente; cambia per
    # la fase AI, che manda una riga di avanzamento a ogni caso.
    ultimo_segno = [time.monotonic()]

    def leggi_errore() -> None:
        assert processo.stderr is not None
        for riga in processo.stderr:
            ultimo_segno[0] = time.monotonic()
            pezzi_errore.append(riga)
            testo = riga.strip()
            if not testo.startswith(PREFISSO_AVANZAMENTO):
                continue
            try:
                evento = json.loads(testo[len(PREFISSO_AVANZAMENTO):])
            except json.JSONDecodeError:
                continue
            if isinstance(evento, dict):
                try:
                    avanzamento(evento)
                except Exception:  # pragma: no cover - l'avanzamento non ferma nulla
                    pass

    pezzi_uscita: list[str] = []

    def leggi_uscita() -> None:
        if processo.stdout is not None:
            pezzi_uscita.append(processo.stdout.read())

    lettore = threading.Thread(target=leggi_errore, name="pipeline-stderr", daemon=True)
    lettore.start()
    # ⚠ Anche `stdout` si legge in un filo, e non e' un vezzo: `read()` torna
    # all'EOF, cioe' quando il figlio muore o chiude il tubo.  Leggendolo qui
    # un passo impiantato bloccava PRIMA di arrivare alla `wait`, e un tetto
    # sulla sola `wait` non sarebbe scattato mai — che e' il modo in cui una
    # difesa contro i piantamenti si scrive e non funziona.
    lettore_uscita = threading.Thread(target=leggi_uscita, name="pipeline-stdout", daemon=True)
    lettore_uscita.start()

    def chiudi() -> None:
        """⚠ Un tubo si chiude **solo** se chi lo leggeva ha finito.

        `close()` su un `BufferedReader` pretende il lucchetto interno del
        lettore, e se un filo e' ancora dentro `read()` si resta li' per
        sempre — con `lucchetto_lavori` in mano, cioe' esattamente la fermata
        che il tetto esiste per evitare.  Succede per davvero: `kill()` uccide
        il figlio diretto, e se quel figlio aveva lasciato un discendente che
        ha ereditato il tubo l'EOF non arriva mai.  In quel caso i due
        descrittori restano aperti fino alla morte del processo, ed e' il
        prezzo giusto: sono due, e l'alternativa e' il programma piantato.
        """

        # Cinque secondi in tutto, non cinque per filo: e' il tempo che si
        # concede alla pulizia, e raddoppiarlo perche' i lettori sono due
        # raddoppierebbe anche l'attesa dell'utente davanti a un passo morto.
        scadenza = time.monotonic() + 5
        lettore.join(timeout=max(0.0, scadenza - time.monotonic()))
        lettore_uscita.join(timeout=max(0.0, scadenza - time.monotonic()))
        if processo.stdout is not None and not lettore_uscita.is_alive():
            processo.stdout.close()
        if processo.stderr is not None and not lettore.is_alive():
            processo.stderr.close()

    def aspetta_finche_da_segni() -> int:
        """Aspetta il figlio, e lo uccide solo se sta zitto da troppo.

        Il tetto si riarma a ogni riga che arriva: un passo che lavora e lo
        dice puo' durare quanto gli serve, un passo che non risponde piu' viene
        chiuso dopo `timeout_secondi` di silenzio. Senza tetto — che e' il caso
        delle prove — si aspetta e basta.
        """

        if timeout_secondi is None:
            return processo.wait()
        while True:
            rimasto = timeout_secondi - (time.monotonic() - ultimo_segno[0])
            if rimasto <= 0:
                raise subprocess.TimeoutExpired(processo.args, timeout_secondi)
            try:
                # Non piu' di un secondo per volta: e' il passo con cui si
                # riguarda l'orologio, non un'attesa in piu'.
                return processo.wait(timeout=min(rimasto, 1.0))
            except subprocess.TimeoutExpired:
                continue

    try:
        try:
            codice = aspetta_finche_da_segni()
        except subprocess.TimeoutExpired:
            processo.kill()
            # ⚠ Col tetto anche qui: SIGKILL non si ignora, ma un figlio fermo
            # in un'attesa di I/O non interrompibile pianterebbe la `wait`, e
            # sarebbe di nuovo il programma inchiodato per non aver saputo
            # aspettare.
            try:
                processo.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise ComandoTroppoLungo(
                secondi=float(timeout_secondi or 0.0), stderr="".join(pezzi_errore)[-4000:]
            ) from None
    finally:
        # Nel `finally` e non nei due rami: qualunque cosa succeda qui dentro
        # — anche un `KeyboardInterrupt` — i tubi e i fili si chiudono.
        chiudi()
    return RisultatoComando(
        uscita=codice, stdout="".join(pezzi_uscita), stderr="".join(pezzi_errore)
    )


# --------------------------------------------------------------------------
# La configurazione
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigurazionePipeline:
    """Dove stanno le cose.  Nessun percorso è cablato dentro il codice."""

    data_dir: Path
    uploads_dir: Path
    review_path: Path
    state_path: Path
    esecuzioni_dir: Path | None = None
    adapters_path: Path = REFERENCES_DIR / "adapters.json"
    scripts_dir: Path = SCRIPTS_DIR
    python_executable: str = sys.executable
    top_k: int = 5
    soglia_ordine: float = 1000.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        object.__setattr__(self, "uploads_dir", Path(self.uploads_dir).resolve())
        object.__setattr__(self, "review_path", Path(self.review_path).resolve())
        object.__setattr__(self, "state_path", Path(self.state_path).resolve())
        object.__setattr__(self, "adapters_path", Path(self.adapters_path).resolve())
        object.__setattr__(self, "scripts_dir", Path(self.scripts_dir).resolve())
        radice = self.esecuzioni_dir or (self.data_dir / NOME_RADICE_ESECUZIONI)
        object.__setattr__(self, "esecuzioni_dir", Path(radice).resolve())

    @property
    def decisioni_manuali_path(self) -> Path:
        """Le decisioni scritte a mano per gli schemi che il registro non conosce."""

        return self.data_dir / NOME_DECISIONI_MANUALI


def _frase_dell_errore(voce: Any) -> str:
    """Una voce d'errore del manifest, detta a chi fa gli ordini.

    ⚠ Qui c'era `str(voce)`, e `voce` e' un **dizionario Python**: in pagina
    usciva «{'file': 'OFFERTE AGOSTO 4.xlsx', 'code': 'MAPPATURA_INCOMPLETA',
    'message': '…', 'missing': [...]}», parentesi graffe e apostrofi compresi.
    Il dizionario intero resta in `dettaglio`, che e' dove serve.
    """

    if not isinstance(voce, dict):
        return str(voce)
    testo = str(voce.get("message") or voce.get("code") or "").strip()
    nome = str(voce.get("file") or "").strip()
    if nome and testo:
        return f"«{nome}»: {testo}"
    return testo or nome or "errore senza descrizione"


def utc_ora() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def json_sicuro(valore: Any) -> Any:
    if isinstance(valore, Path):
        return str(valore)
    if isinstance(valore, Mapping):
        return {str(chiave): json_sicuro(voce) for chiave, voce in valore.items()}
    if isinstance(valore, (list, tuple)):
        return [json_sicuro(voce) for voce in valore]
    if valore is None or isinstance(valore, (str, int, float, bool)):
        return valore
    return str(valore)


def scrivi_json(percorso: Path, valore: Any) -> None:
    """In binario e a fine riga LF, come tutto il resto del progetto.

    Lo schema — temporaneo col nome di chi lo scrive, `fsync`, `os.replace` —
    sta in `scrittura_sicura`: era in quattro copie, e le copie di una difesa
    divergono. `json_sicuro` resta qui perche' e' l'unico chiamante che deve
    scrivere anche oggetti che JSON non conosce.
    """

    scrittura_sicura.scrivi_json(percorso, json_sicuro(valore), a_capo_finale=True)

def leggi_json(percorso: Path, predefinito: Any = None) -> Any:
    try:
        return json.loads(percorso.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return deepcopy(predefinito)


def impronta_file(percorso: Path) -> str:
    digest = hashlib.sha256()
    with percorso.open("rb") as flusso:
        for pezzo in iter(lambda: flusso.read(1024 * 1024), b""):
            digest.update(pezzo)
    return digest.hexdigest()


def _numero(valore: Any) -> float | None:
    if isinstance(valore, bool) or valore is None:
        return None
    if isinstance(valore, (int, float)):
        return float(valore)
    testo = str(valore).strip().replace(",", ".")
    try:
        return float(testo)
    except ValueError:
        return None


def articolo_della_riga(prodotto: Any) -> str:
    """Che cosa c'e' su questa riga dell'elenco, indipendentemente dal numero di riga.

    L'identificativo di un prodotto e' il **numero di riga** del gestionale
    (`product:3` e' la riga 3).  Con un elenco nuovo la riga 3 e' un altro
    articolo, e una decisione presa sulla riga 3 di prima non parla di lui.
    Questa e' la firma che dice se e' rimasto lo stesso: il codice a barre
    quando c'e', altrimenti il nome ridotto all'osso.
    """

    if not isinstance(prodotto, dict):
        return ""
    ean = str(prodotto.get("ean") or prodotto.get("barcode") or "").strip()
    if ean:
        return f"ean:{ean}"
    testo = str(prodotto.get("description") or prodotto.get("name") or "").strip()
    if not testo:
        return ""
    return "nome:" + " ".join(testo.split()).casefold()


# --------------------------------------------------------------------------
# L'orchestratore
# --------------------------------------------------------------------------

class PipelineJobManager:
    """Possiede una sola esecuzione della catena e ne espone lo stato in JSON.

    Non sa che cosa sia una rotta HTTP: `avvia()` e `stato()` sono due funzioni
    che restituiscono dizionari, ed e' il server a metterle sotto un URL.
    """

    def __init__(
        self,
        configurazione: ConfigurazionePipeline,
        *,
        lucchetto_lavori: threading.Lock | None = None,
        lucchetto_dati: threading.RLock | None = None,
        esecutore: Esecutore = esegui_comando,
        su_confronto_attivato: Callable[[dict[str, Any]], None] | None = None,
        uguaglianze_dichiarate: Callable[[], list[list[str]]] | None = None,
        rifiuti_dichiarati: Callable[[], dict[tuple[str, str], dict[str, Any]]] | None = None,
    ) -> None:
        self.configurazione = configurazione
        self.esecutore = esecutore
        self.su_confronto_attivato = su_confronto_attivato
        # Chi sa quali codici a barre l'utente ha dichiarato uguali. E' una
        # funzione e non un percorso perche' il magazzino delle conferme ha un
        # padrone solo — il servizio — e due oggetti aperti sullo stesso file
        # SQLite sono il modo classico di scoprire troppo tardi che uno dei due
        # lo teneva bloccato. Qui la risposta si chiede e si scrive nella
        # cartella della run, dove resta a dire quali dichiarazioni valevano.
        self.uguaglianze_dichiarate = uguaglianze_dichiarate
        # Chi sa quali offerte l'utente ha rifiutato con «Non e' lo stesso
        # articolo». Stessa forma delle uguaglianze, e per la stessa ragione:
        # il magazzino delle conferme ha un padrone solo. ⚠ Serve **qui** e non
        # solo in lettura: senza, `_ripulisci_stato` vede l'offerta rifiutata
        # ancora utilizzabile, conclude «qualcuno lo serve» e azzera la
        # quantita' — e il prodotto sparisce da «Prodotti da reperire», che e'
        # esattamente il vicolo cieco che quel rifiuto doveva chiudere.
        self.rifiuti_dichiarati = rifiuti_dichiarati
        # Il lucchetto dei lavori e' **uno solo** per tutti i job che toccano
        # `review_data.json`: due che lo sostituiscono insieme lascerebbero un
        # confronto meta' di uno e meta' dell'altro.
        self.lucchetto_lavori = lucchetto_lavori or threading.Lock()
        # ⚠ E' il **medesimo** lucchetto delle rotte (`ReviewStore.lock`), non
        # un secondo: serve a tenere fuori il salvataggio della pagina
        # dall'unico istante in cui la catena tocca i dati che le rotte
        # servono — lo stato ripulito e la sostituzione del confronto vivo.
        # L'ordine e' sempre `lucchetto_lavori` prima e questo dopo, mai il
        # contrario: nessuna rotta prende il lucchetto dei lavori, quindi
        # l'abbraccio mortale non ha da dove nascere.  Un `RLock` perche' e'
        # quello che il servizio usa, e la catena lo prende una volta sola.
        self.lucchetto_dati = lucchetto_dati or threading.RLock()
        self._lucchetto = threading.RLock()
        self._filo: threading.Thread | None = None
        # Lo si dice una volta sola: lo stato si riscrive a ogni fase e a ogni
        # avanzamento, e una console piena della stessa riga non la legge nessuno.
        self._stato_non_si_scrive = False
        self._percorso_stato = configurazione.data_dir / NOME_STATO
        configurazione.esecuzioni_dir.mkdir(parents=True, exist_ok=True)

        stato = leggi_json(self._percorso_stato, None)
        self._stato = stato if isinstance(stato, dict) else self._stato_in_attesa()
        if self._stato.get("stato") in STATI_IN_CORSO:
            # Il server e' stato chiuso mentre la catena lavorava: quella run
            # non riprende da sola, e dirlo e' meglio che lasciare in pagina una
            # barra che non avanzera' mai piu'.
            self._stato.update({
                "ok": False,
                "stato": INTERROTTO,
                "messaggio": (
                    "Il confronto precedente è stato interrotto: quello attivo è "
                    "rimasto quello di prima. Si può rifare."
                ),
                "aggiornatoIl": utc_ora(),
            })
            self._salva_stato()
        elif (
            self._stato.get("stato") == ERRORE
            and (self._stato.get("fermata") or {}).get("code") == "SCHEMA_SCONOSCIUTO"
        ):
            richiesti = [
                str(nome) for nome in ((self._stato.get("fermata") or {}).get("documenti") or [])
                if str(nome).strip()
            ]
            if richiesti and any(
                consegna.file_sicuro(self.configurazione.uploads_dir, nome) is None
                for nome in richiesti
            ):
                self._stato = self._stato_in_attesa()
                self._stato["messaggio"] = "I documenti sono cambiati. Ricalcola il confronto quando sono pronti."
                self._salva_stato()

    # -- lo stato ----------------------------------------------------------

    def _stato_in_attesa(self) -> dict[str, Any]:
        return {
            "ok": True,
            "stato": IN_ATTESA,
            "fase": None,
            "messaggio": "Nessun confronto in corso.",
            "runId": None,
            "cartella": None,
            "iniziatoIl": None,
            "aggiornatoIl": utc_ora(),
            "completatoIl": None,
            "fasi": [self._fase_vuota(nome) for nome in FASI],
            "avanzamento": {"fatte": 0, "totali": len(FASI), "percento": 0.0},
            "numeri": {},
            "avvisi": [],
            "fermata": None,
        }

    @staticmethod
    def _fase_vuota(nome: str) -> dict[str, Any]:
        return {
            "nome": nome,
            "titolo": TITOLI_FASI.get(nome, nome),
            "stato": IN_ATTESA,
            "dettaglio": "",
            "durataSecondi": None,
        }

    def _salva_stato(self) -> None:
        """Lo stato sul disco, e un guasto qui non ferma la catena.

        ⚠ Prima sollevava, e il guasto peggiore era il piu' silenzioso: se
        `pipeline_status.json` non si scrive — disco pieno, cartella in sola
        lettura, antivirus sul temporaneo — l'eccezione partiva da dentro il
        gestore d'errore di `_lavora`, che chiama `_segna_fase` e riprova la
        stessa scrittura fallita. La seconda eccezione usciva **prima** che lo
        stato diventasse `ERRORE`: il filo moriva, il lucchetto si liberava, e
        in pagina restava una barra ferma su «in corso» che non sarebbe
        avanzata mai piu', con il ricalcolo e i caricamenti spenti.

        Lo stato che conta per chi guarda la pagina e' quello in memoria —
        `stato()` legge quello — e il file serve a due cose sole: sopravvivere
        a un riavvio del servizio, e dire «interrotto» a chi riapre. Perderlo
        e' un peggioramento, non un guasto: si dice una volta e si va avanti.
        """

        try:
            scrivi_json(self._percorso_stato, self._stato)
        except OSError as guasto:
            if not self._stato_non_si_scrive:
                self._stato_non_si_scrive = True
                print(
                    f"[AVVISO] Non riesco a scrivere «{self._percorso_stato.name}» "
                    f"({type(guasto).__name__}: {guasto}). Il confronto va avanti lo stesso; "
                    "se il programma viene chiuso adesso, alla riapertura non sapra' dire "
                    "a che punto era."
                )

    def _aggiorna(self, **cambiamenti: Any) -> None:
        with self._lucchetto:
            self._stato.update(json_sicuro(cambiamenti))
            self._stato["aggiornatoIl"] = utc_ora()
            self._salva_stato()

    def _segna_fase(self, nome: str, stato: str, dettaglio: str = "", durata: float | None = None) -> None:
        """`durata` arriva da `time.monotonic()`, non dall'orologio di parete.

        ⚠ Le nove fasi si misuravano con `datetime.now()`, che il PC del
        negozio fa saltare: Windows Time lo corregge quando vuole, e due volte
        l'anno cambia l'ora legale.  Una correzione presa in mezzo a un
        ricalcolo scriveva qui — e nell'audit della cartella — un'ora di troppo
        o una durata negativa, ed e' il numero che poi si usa per dire «era
        lento».  `time.monotonic()` non torna indietro e non ha fuso: e' un
        conta-secondi, non una data.  I timbri restano dove sono: `utc_ora()`
        e' gia' in UTC esplicito, e il `datetime.now().astimezone()` che da'
        il nome alla cartella della run deve restare l'ora locale.
        """

        with self._lucchetto:
            for voce in self._stato.get("fasi") or []:
                if voce.get("nome") != nome:
                    continue
                voce["stato"] = stato
                if dettaglio:
                    voce["dettaglio"] = dettaglio
                if durata is not None:
                    voce["durataSecondi"] = round(durata, 2)
            fatte = sum(1 for voce in self._stato.get("fasi") or [] if voce.get("stato") == COMPLETATO)
            self._stato["avanzamento"] = {
                "fatte": fatte,
                "totali": len(FASI),
                "percento": round(fatte * 100 / len(FASI), 1),
            }
            if stato == IN_CORSO:
                self._stato["fase"] = nome
                self._stato["messaggio"] = TITOLI_FASI.get(nome, nome) + "…"
            self._stato["aggiornatoIl"] = utc_ora()
            self._salva_stato()

    def _numeri(self, **valori: Any) -> None:
        with self._lucchetto:
            numeri = dict(self._stato.get("numeri") or {})
            numeri.update(json_sicuro(valori))
            self._stato["numeri"] = numeri
            self._stato["aggiornatoIl"] = utc_ora()
            self._salva_stato()

    def _metti_da_parte_gli_adattatori_superati(self) -> None:
        """Prima di riconoscere qualunque documento: vale lo spedito piu' recente.

        E' la regola di `registro._motivo_del_superamento`, applicata al file
        una volta per confronto, cosi' l'avviso esce una volta sola e non a
        ogni lettura del registro.  Sta prima della profilazione perche' e'
        li' — `inspect_sources` chiama `registro.riconosci` — che una voce
        imparata vecchia si prenderebbe il documento al posto di quella
        spedita.  Un registro che non si legge non ferma qui: lo dice gia' la
        fase che ne ha bisogno.
        """

        try:
            messe = registro.metti_da_parte_le_superate(self.configurazione.adapters_path)
        except Exception as exc:  # noqa: BLE001 - confine difensivo: si va avanti con il registro com'e'
            print(f"[AVVISO] adattatori superati non messi da parte — {type(exc).__name__}: {exc}")
            return
        for scheda in messe:
            nome = str(scheda.get("display_name") or scheda.get("supplier_id") or scheda.get("base") or "").strip()
            self._avvisa(
                "ADATTATORE_MESSO_DA_PARTE",
                f"{nome.upper()}: vale la versione aggiornata del suo listino",
                f"La disposizione delle colonne di {nome.upper()} imparata su questo computer è "
                "stata messa da parte: con l'aggiornamento del programma ne è arrivata una più "
                "recente, e da adesso vale quella. Se il suo listino non viene riconosciuto, "
                "controlla le colonne nell'anteprima e conferma: verrà imparato di nuovo.",
                adattatore=scheda.get("id"),
                dettaglio=scheda.get("motivo"),
            )

    def _avvisa(self, codice: str, titolo: str, messaggio: str,
                *, severita: str = "warning", **extra: Any) -> None:
        """Un avviso non ferma niente: e' il contratto della fase 6c.

        Finisce sia nello stato del job — che la pagina mostra mentre lavora —
        sia, all'attivazione, dentro `review_data.json`, perche' due minuti dopo
        nessuno guardera' piu' la barra di avanzamento.

        `severita` esiste per i pochi avvisi che non si possono leggere di
        sfuggita: `"error"` li fa uscire in rosso in cima alla pagina e in
        rosso nel documento, e `blocking` resta `False` — la compilazione non
        si chiude, perche' «il controllo sui numeri avvisa e non ferma».  Non
        c'e' nessun modo di spegnerli: un avviso che si puo' zittire, prima o
        poi, viene zittito.
        """

        with self._lucchetto:
            avvisi = list(self._stato.get("avvisi") or [])
            avvisi.append(json_sicuro({
                "code": codice,
                "severity": severita,
                "blocking": False,
                "title": titolo,
                "message": messaggio,
                **extra,
            }))
            self._stato["avvisi"] = avvisi
            self._stato["aggiornatoIl"] = utc_ora()
            self._salva_stato()

    def stato(self) -> dict[str, Any]:
        with self._lucchetto:
            return deepcopy(self._stato)

    def input_modificato(
        self,
        tipo: str = "",
        documenti: list[str] | None = None,
        fornitori: list[str] | None = None,
    ) -> dict[str, Any]:
        """Scarta l'esito di una run che descrive documenti ormai diversi.

        Le cartelle delle run restano come audit, ma la pagina non deve tentare
        di configurare un file che nel frattempo è stato eliminato o sostituito.
        Una run viva conserva invece il proprio stato: se i file cambiano
        durante il lavoro, saranno le sue verifiche a fermarla in sicurezza.

        ⚠ `cambiamento` è il campo che accende in pagina la fascia «Hai
        caricato il listino LARICE dopo l'ultimo confronto: i prezzi che vedi
        nelle pagine 2 e 3 sono ancora quelli di lunedì 10 agosto». La pagina
        sa leggerlo dal 15 agosto 2026 e ha cinque prove che lo dimostrano, ma
        **nessuno lo scriveva**: chi cancellava un listino e ne caricava un
        altro si ritrovava i prezzi della settimana prima senza una parola.
        Verificato sul `pipeline_status.json` vero del 22 agosto 2026: quel
        campo non c'era. È la forma più insidiosa di prova verde — prova che
        chi legge funziona, e nessuno prova che qualcuno lo alimenti.

        Quello che qui non si dichiara resta com'era — lo stato torna in attesa
        e basta — perché una fascia che non sa dire *che cosa* è cambiato non
        aiuta nessuno.
        """

        with self._lucchetto:
            if self.in_corso():
                return deepcopy(self._stato)
            self._stato = self._stato_in_attesa()
            self._stato["messaggio"] = "I documenti sono cambiati. Ricalcola il confronto quando sono pronti."
            nomi = [str(voce).strip() for voce in (documenti or []) if str(voce).strip()]
            etichette = [str(voce).strip() for voce in (fornitori or []) if str(voce).strip()]
            if tipo in {"eliminato", "caricato", "colonne"} and (nomi or etichette):
                self._stato["cambiamento"] = {
                    "tipo": tipo,
                    "documenti": nomi,
                    "fornitori": etichette,
                }
            self._salva_stato()
            return deepcopy(self._stato)

    def _contesto_mappatura(self) -> tuple[dict[str, Any], Path, list[dict[str, Any]], list[str]]:
        """Restituisce soltanto la run fermata che la pagina può configurare."""

        stato = deepcopy(self._stato)
        fermata = stato.get("fermata") or {}
        if stato.get("stato") != ERRORE or fermata.get("code") != "SCHEMA_SCONOSCIUTO":
            raise ValueError("Non ci sono documenti in attesa di configurazione.")
        run_id = str(stato.get("runId") or "")
        cartella = consegna.cartella_sicura(self.configurazione.esecuzioni_dir, run_id)
        if cartella is None:
            raise ValueError("La cartella del confronto da configurare non esiste più.")
        dichiarata = Path(str(stato.get("cartella") or "")).resolve()
        if dichiarata != cartella.resolve():
            raise ValueError("Lo stato del confronto non coincide con la sua cartella.")
        documento = leggi_json(cartella / "input_profiles.json", None)
        profili = documento.get("profiles") if isinstance(documento, dict) else None
        if not isinstance(profili, list):
            raise ValueError("L'anteprima dei documenti non è più disponibile.")
        nomi = [str(nome) for nome in (fermata.get("documenti") or []) if str(nome).strip()]
        if not nomi:
            raise ValueError("Il confronto non indica quali documenti configurare.")
        radice_upload = self.configurazione.uploads_dir.resolve()
        for profilo in profili:
            if not isinstance(profilo, dict):
                raise ValueError("L'anteprima dei documenti non è valida.")
            percorso = Path(str(profilo.get("path") or "")).resolve()
            try:
                percorso.relative_to(radice_upload)
            except ValueError:
                raise ValueError("Un documento dell'anteprima non appartiene ai caricamenti.") from None
        return stato, cartella, profili, nomi

    # --- il selettore delle colonne aperto a mano --------------------------
    #
    # Il percorso della mappatura guidata apre il selettore soltanto quando la
    # catena si ferma su uno schema che il registro non conosce.  Con i
    # fornitori riconosciuti non si ferma mai, e non c'era nessun modo di
    # rivedere le colonne di un listino gia' noto — a partire dalla colonna
    # d'ordine, che e' quella che decide dove finiscono le quantita' nella copia
    # da mandare.  Richiesta di Daniele, 15 agosto 2026: «vorrei un pulsante che
    # mi permetta di selezionare la colonna di compilazione quantita' sui
    # listini».
    #
    # Il macchinario e' lo stesso della mappatura guidata (`schema_mapping`):
    # cambia soltanto da dove arrivano i profili — la cartella dei caricamenti
    # invece della cartella di una run ferma — e che alla fine **non riparte
    # niente**.  La mappatura confermata diventa una decisione scritta, e la usa
    # il prossimo ricalcolo, che lo lancia l'utente quando vuole: dieci minuti
    # non si prendono senza che li abbia chiesti.  Ma la pagina lo dice, con la
    # stessa fascia di un documento cambiato, perche' e' la stessa cosa — i
    # prezzi che si vedono adesso sono stati letti con le colonne di prima.
    CHIAVE_COLONNE_A_MANO = "colonne-a-mano"

    def _profili_dei_caricamenti(self) -> list[dict[str, Any]]:
        documento = leggi_json(self.configurazione.uploads_dir / "upload_profiles.json", None)
        profili = documento.get("profiles") if isinstance(documento, dict) else None
        if not isinstance(profili, list):
            return []
        return [voce for voce in profili if isinstance(voce, dict)]

    def _documento_caricato(self, nome: Any) -> str:
        """Il nome, controllato contro la cartella dei caricamenti.

        Il browser non sceglie percorsi: sceglie fra i nomi che il servizio gli
        ha dato, e questo lo verifica sul disco prima di aprire qualsiasi cosa.

        ⚠ Le due domande sono diverse e servono tutte e due. Il registro dei
        profili ferma i nomi inventati; il **file tolto dalla cartella con
        Esplora risorse**, che nel registro c'e' ancora, lo ferma soltanto il
        controllo sul disco.
        """

        nome = str(nome or "").strip()
        if not nome:
            raise ValueError("Indica di quale documento vuoi rivedere le colonne.")
        if consegna.file_sicuro(self.configurazione.uploads_dir, nome) is None:
            raise ValueError("Questo documento non è fra i caricamenti.")
        if not any(
            str(profilo.get("file_name") or "").casefold() == nome.casefold()
            for profilo in self._profili_dei_caricamenti()
        ):
            raise ValueError("Di questo documento non c'è l'anteprima: ricaricalo e riprova.")
        return nome

    def colonne_del_documento(self, nome: Any) -> dict[str, Any]:
        with self._lucchetto:
            if self.in_corso():
                raise ValueError("Il confronto è in corso: le colonne si rivedono appena ha finito.")
            nome = self._documento_caricato(nome)
        adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
        return schema_mapping.prepara_pendenti(
            self._profili_dei_caricamenti(), [nome], adattatori, self.CHIAVE_COLONNE_A_MANO,
        )

    def prova_colonne_del_documento(self, payload: dict[str, Any]) -> dict[str, Any]:
        nome, profili, adattatori = self._contesto_colonne(payload)
        esito, _decisioni = schema_mapping.valida_mappature(
            profili, [nome], adattatori, payload, self.CHIAVE_COLONNE_A_MANO,
        )
        return esito

    def salva_colonne_del_documento(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Scrive la mappatura e dichiara che il confronto e' da rifare.

        Non lancia il ricalcolo: dieci minuti non si prendono senza che l'utente
        li abbia chiesti.  Le colonne nuove le usa il prossimo confronto, e la
        pagina lo dice con la stessa fascia di un documento cambiato — perche'
        e' la stessa cosa: i prezzi che si vedono adesso sono stati letti con le
        colonne di prima.
        """

        nome, profili, adattatori = self._contesto_colonne(payload)
        esito, decisioni = schema_mapping.valida_mappature(
            profili, [nome], adattatori, payload, self.CHIAVE_COLONNE_A_MANO,
        )
        with self._lucchetto:
            if self.in_corso():
                raise ValueError("Il confronto è partito nel frattempo: riprova quando ha finito.")
            self._salva_decisioni_confermate(decisioni)
        # Il fornitore, quando la mappatura lo dichiara: «BETULLA» dice piu' del
        # nome del file, ed e' quello che la fascia mette in prima riga.
        fornitori = [
            schema_mapping.nome_dichiarato(str(voce.get("supplier_id") or ""), adattatori)
            for voce in decisioni
            if str(voce.get("supplier_id") or "")
        ]
        stato = self.input_modificato(
            "colonne", documenti=[nome], fornitori=[voce for voce in fornitori if voce],
        )
        return {"ok": True, "validation": esito, "pipeline": stato}

    def _contesto_colonne(self, payload: Any) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida.")
        with self._lucchetto:
            if self.in_corso():
                raise ValueError("Il confronto è in corso: le colonne si rivedono appena ha finito.")
            nome = self._documento_caricato(payload.get("fileName"))
        return nome, self._profili_dei_caricamenti(), schema_mapping.carica_adattatori(
            self.configurazione.adapters_path
        )

    def schemi_pendenti(self) -> dict[str, Any]:
        with self._lucchetto:
            stato, _cartella, profili, nomi = self._contesto_mappatura()
            # La ragione per documento la dichiara la fermata, non la si
            # rideduce dal profilo: e' la stessa autorita' che ha deciso di
            # fermarsi.
            motivi = (stato.get("fermata") or {}).get("motivi") or {}
            adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
            return schema_mapping.prepara_pendenti(
                profili, nomi, adattatori, str(stato.get("runId") or ""), motivi=motivi,
            )

    def colonne_dei_documenti(self) -> dict[str, Any]:
        """Quali colonne il confronto vivo ha usato, documento per documento.

        Non guarda lo stato della catena — guarda la **run che ha prodotto il
        confronto che si sta usando**, che e' un'altra cosa: dopo un ricalcolo
        andato male lo stato racconta l'ultimo tentativo, mentre i prezzi in
        pagina vengono ancora dalla run di prima, ed e' di quella che bisogna
        poter vedere le colonne.  L'identificativo lo dichiara il confronto
        stesso (`run.pipelineRunId`), come fa la guardia della compilazione.

        Non solleva quando non c'e' niente da mostrare: un programma appena
        installato, una cartella di run ripulita a mano e un confronto vecchio
        di tre versioni sono tre modi normali di non avere la risposta, e in
        pagina valgono tutti «di questo documento non so dirti le colonne».
        """

        run_id, motivo, voci = self._documenti_della_run()
        if motivo:
            return {"ok": True, "runId": run_id, "documents": [], "motivo": motivo}
        documenti = [
            schema_mapping.mappatura_effettiva(profilo, adattatore, decisione)
            for profilo, adattatore, decisione in voci
        ]
        return {"ok": True, "runId": run_id, "documents": documenti, "motivo": ""}

    def _documenti_della_run(
        self,
    ) -> tuple[str, str, list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]]]:
        """I documenti della run che ha prodotto il confronto in uso.

        Restituisce `(runId, motivo, voci)`: se `motivo` non e' vuoto le voci
        sono zero e quella frase e' la ragione, che in pagina vale «di questo
        documento non so dirti le colonne».  Ogni voce e' la terna
        `(profilo, adattatore, decisione)`, cioe' i tre pezzi che servono a
        chiunque debba dire qualcosa su un documento del confronto: il profilo
        porta le righe, l'adattatore quello che il registro dichiara, la
        decisione quello che l'utente ha confermato.
        """

        review = leggi_json(self.configurazione.review_path, {})
        run_id = ""
        if isinstance(review, dict):
            run_id = str((review.get("run") or {}).get("pipelineRunId") or "")
        if not run_id:
            return "", "Il confronto in uso non dice da quale elaborazione viene.", []
        cartella = consegna.cartella_sicura(self.configurazione.esecuzioni_dir, run_id)
        if cartella is None:
            return run_id, "La cartella di quel confronto non c'è più.", []
        documento = leggi_json(cartella / "input_profiles.json", None)
        profili = documento.get("profiles") if isinstance(documento, dict) else None
        manifest = leggi_json(cartella / "input_manifest.json", None)
        voci = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(profili, list) or not isinstance(voci, list):
            return run_id, "I file di quel confronto non sono più leggibili.", []
        # La decisione (fornitore, adattatore, mappatura confermata) sta nel
        # manifest; il profilo porta le righe da mostrare come esempio. Si
        # incrociano sul `profile_id`, che e' l'identificativo che entrambi
        # dichiarano, e non sul nome del file.
        per_profilo = {
            str(voce.get("profile_id") or ""): (voce.get("ai_preflight") or {})
            for voce in voci
            if isinstance(voce, dict)
        }
        adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
        risultato = []
        for profilo in profili:
            if not isinstance(profilo, dict):
                continue
            decisione = per_profilo.get(str(profilo.get("profile_id") or "")) or {}
            # ⚠ `voce_in_uso` e non l'id cosi' com'e': questa decisione e' stata
            # presa al ricalcolo, e da allora qualcuno puo' aver spostato la
            # colonna d'ordine dalla pagina — mossa che scrive una voce
            # `__locale`. Cercando `betulla_v1` si ritroverebbe quella spedita,
            # con la colonna di prima, e la pagina mostrerebbe una colonna che
            # non e' piu' quella che si usa.
            adattatore = registro.voce_in_uso(decisione.get("adapter_id"), adattatori) or None
            risultato.append((profilo, adattatore, decisione))
        return run_id, "", risultato

    def documento_del_fornitore(self, supplier_id: str) -> dict[str, Any]:
        """Il documento di questo fornitore nel confronto in uso, e le sue colonne.

        Serve a chi deve **cambiare la colonna d'ordine**: sono gli stessi dati
        che la scheda del documento mostra in pagina 1, piu' l'elenco completo
        delle colonne del foglio, che li' non serve.

        ⚠ Il fornitore lo dichiara la **decisione della run**, non il nome del
        file: due settimane di fila lo stesso fornitore manda file con nomi
        diversi, e legare la scelta al nome vorrebbe dire perderla ogni lunedi'.
        """

        cercato = str(supplier_id or "").strip().casefold()
        run_id, motivo, voci = self._documenti_della_run()
        if motivo:
            raise ValueError(motivo)
        for profilo, adattatore, decisione in voci:
            if str(decisione.get("supplier_id") or "").strip().casefold() != cercato or not cercato:
                continue
            effettiva = schema_mapping.mappatura_effettiva(profilo, adattatore, decisione)
            return {
                "runId": run_id,
                "profilo": profilo,
                "adattatore": adattatore or {},
                "decisione": decisione,
                "effettiva": effettiva,
                "colonne": schema_mapping.colonne_del_foglio(
                    profilo,
                    effettiva.get("sheet"),
                    effettiva.get("headerRow"),
                    effettiva.get("dataStartRow"),
                    # La colonna d'ordine di oggi puo' stare **oltre** l'ultima
                    # colonna con qualcosa dentro: su CIPRESSO la G e' vuota su
                    # tutte le righe e nel profilo non compare. Senza questo,
                    # l'unica colonna nuova che si potrebbe scegliere sarebbe
                    # quella accanto a quella in uso.
                    fino_a=(effettiva.get("orderColumn") or {}).get("colonna"),
                ),
            }
        raise ValueError(
            "Nel confronto in uso non c'è nessun listino di questo fornitore."
        )

    def valida_schemi(self, payload: dict[str, Any]) -> dict[str, Any]:
        # La prova puo' leggere migliaia di righe: non tiene bloccato lo stato.
        # Si verifica di nuovo il runId prima di consegnare il risultato.
        with self._lucchetto:
            stato, _cartella, profili, nomi = self._contesto_mappatura()
            run_id = str(stato.get("runId") or "")
        adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
        esito, _decisioni = schema_mapping.valida_mappature(
            profili, nomi, adattatori, payload, run_id
        )
        with self._lucchetto:
            if str(self._stato.get("runId") or "") != run_id:
                raise ValueError("Nel frattempo è partito un altro confronto. Riapri la configurazione.")
        return esito

    def conferma_schemi(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lucchetto:
            stato, _cartella, profili, nomi = self._contesto_mappatura()
            run_id = str(stato.get("runId") or "")
        adattatori = schema_mapping.carica_adattatori(self.configurazione.adapters_path)
        esito, decisioni = schema_mapping.valida_mappature(
            profili, nomi, adattatori, payload, run_id
        )
        with self._lucchetto:
            if str(self._stato.get("runId") or "") != run_id:
                raise ValueError("Nel frattempo è partito un altro confronto. Riapri la configurazione.")
            self._salva_decisioni_confermate(decisioni)
            nuovo_stato = self.avvia()
        return {"ok": True, "validation": esito, "pipeline": nuovo_stato}

    def _salva_decisioni_confermate(self, nuove: list[dict[str, Any]]) -> None:
        """Sostituisce solo le decisioni dei documenti appena confermati."""

        percorso = self.configurazione.decisioni_manuali_path
        documento = leggi_json(percorso, {})
        vecchie = documento.get("decisions") if isinstance(documento, dict) else documento
        if not isinstance(vecchie, list):
            vecchie = []
        nomi = {str(voce.get("file_name") or "").casefold() for voce in nuove}
        profili = {str(voce.get("profile_id") or "") for voce in nuove}
        tenute = [
            voce for voce in vecchie
            if isinstance(voce, dict)
            and str(voce.get("file_name") or "").casefold() not in nomi
            and str(voce.get("profile_id") or "") not in profili
        ]
        scrivi_json(percorso, {"decisions": [*tenute, *nuove]})

    def in_corso(self) -> bool:
        return self._filo is not None and self._filo.is_alive()

    # -- l'avvio -----------------------------------------------------------

    def avvia(self) -> dict[str, Any]:
        """Parte e torna subito: chi ha premuto il pulsante non aspetta la rete."""

        with self._lucchetto:
            if self.in_corso():
                raise LavoroGiaInCorso("Un confronto è già in corso")
            if not self.lucchetto_lavori.acquire(blocking=False):
                raise LavoroGiaInCorso("Un altro lavoro sta già aggiornando il confronto")
            # ⚠ Da qui alla partenza del filo il lucchetto e' in mano a nessuno:
            # se qualcosa va storto lo tiene per sempre.  Prima il ripiego
            # copriva la sola creazione della cartella, e bastava un
            # `pipeline_status.json` non scrivibile — disco pieno, cartella in
            # sola lettura, antivirus sul temporaneo — per lasciarlo preso: da
            # li' in poi ogni «Ricalcola» rispondeva 409 e `POST /api/spegni` si
            # rifiutava di spegnere, cioe' il programma inchiodato senza niente
            # da premere in pagina.  Adesso il `finally` lo restituisce a
            # QUALUNQUE guasto, e lo lascia in mano al filo solo quando il filo
            # esiste davvero: e' lui che lo rilascera' alla fine del lavoro.
            partito = False
            try:
                momento = datetime.now().astimezone()
                cartella = consegna.crea_cartella(self.configurazione.esecuzioni_dir, momento)
                tolte = self._ripulisci_le_esecuzioni(cartella)
                if tolte:
                    # Detto e non taciuto: una pulizia silenziosa e' una pulizia
                    # di cui nessuno si accorge finche' non cerca una cartella
                    # che non c'e' piu'.
                    print(f"[PULIZIA] Cartelle di lavoro vecchie rimosse: {tolte} "
                          f"(se ne tengono {ESECUZIONI_DA_TENERE}).")
                self._stato = {
                    "ok": True,
                    "stato": IN_CORSO,
                    "fase": FASI[0],
                    "messaggio": "Confronto avviato.",
                    "runId": cartella.name,
                    "cartella": str(cartella),
                    "iniziatoIl": utc_ora(),
                    "aggiornatoIl": utc_ora(),
                    "completatoIl": None,
                    "fasi": [self._fase_vuota(nome) for nome in FASI],
                    "avanzamento": {"fatte": 0, "totali": len(FASI), "percento": 0.0},
                    "numeri": {},
                    "avvisi": [],
                    "fermata": None,
                }
                self._salva_stato()
                self._filo = threading.Thread(
                    target=self._lavora,
                    args=(cartella,),
                    name=f"pipeline-{cartella.name}",
                    daemon=True,
                )
                self._filo.start()
                partito = True
                return deepcopy(self._stato)
            finally:
                if not partito:
                    try:
                        self.lucchetto_lavori.release()
                    except RuntimeError:  # pragma: no cover - non rilasciarlo due volte
                        pass

    def attendi(self, timeout: float | None = None) -> dict[str, Any]:
        """Comodità per le prove e per i lanciatori; la pagina interroga `stato()`."""

        filo = self._filo
        if filo is not None:
            filo.join(timeout=timeout)
        return self.stato()

    # -- il lavoro ---------------------------------------------------------

    def _verifica_il_codice_in_memoria(self) -> None:
        """Una run non parte se il programma non è più quello che sta girando.

        Il 14 agosto 2026 il ricalcolo si è fermato a due terzi con
        `AttributeError: module 'registro' has no attribute
        'nome_del_fornitore'`: il server era acceso dalle 09:42 e il codice sul
        disco era cambiato due volte nel pomeriggio. Python i moduli li carica
        una volta sola, ma quelli importati **dentro una funzione** li legge dal
        disco la prima volta che quella riga passa — e quel giorno è passata
        alle 20:19. Vecchio e nuovo insieme, e all'utente una riga di Python in
        inglese che dal browser non si poteva risolvere in nessun modo.

        Si **ferma**, non avvisa: una run mezza vecchia e mezza nuova può
        arrivare in fondo e scrivere un confronto sbagliato in silenzio, che è
        il modo peggiore di fallire per un programma che gira da solo. Fermarsi
        non costa niente — il confronto attivo resta dov'è — e il rimedio ce
        l'ha in mano l'utente: chiudere e riaprire.
        """

        avviso = versione_del_codice.avviso_del_codice_cambiato()
        if avviso:
            raise Fermata(codice=CODICE_CAMBIATO_DOPO_L_AVVIO, messaggio=avviso)

    def _lavora(self, cartella: Path) -> None:
        registro_artefatti: dict[str, dict[str, Any]] = {}
        try:
            self._verifica_il_codice_in_memoria()
            self._metti_da_parte_gli_adattatori_superati()
            corsa = _Corsa(cartella=cartella, registro_artefatti=registro_artefatti)
            self._fase_profilazione(corsa)
            self._fase_riconoscimento(corsa)
            self._fase_validazione(corsa)
            self._fase_parsing(corsa)
            self._fase_shortlist(corsa)
            self._fase_valutazione_ai(corsa)
            self._fase_risoluzione(corsa)
            self._fase_costruzione(corsa)
            self._fase_attivazione(corsa)
            self._aggiorna(
                ok=True,
                stato=COMPLETATO,
                fase=None,
                messaggio=corsa.messaggio_finale or "Confronto aggiornato.",
                completatoIl=utc_ora(),
            )
        except Fermata as fermata:
            self._segna_fase(self._stato.get("fase") or FASI[0], ERRORE, fermata.messaggio)
            self._aggiorna(
                ok=False,
                stato=ERRORE,
                messaggio=fermata.messaggio,
                fermata={
                    "code": fermata.codice,
                    "message": fermata.messaggio,
                    "documenti": fermata.documenti,
                    "dettaglio": fermata.dettaglio,
                    # Perche' ognuno di quei documenti e' li'. Chi legge questo
                    # stato — la pagina, e /api/schemas/pending — non ha altro
                    # modo di saperlo: il profilo da solo non basta, perche' il
                    # documento con il ruolo che non combacia col registro esce
                    # SCHEMA_NOTO nel profilo e VARIATO qui.
                    "motivi": fermata.motivi,
                },
                completatoIl=utc_ora(),
            )
        except Exception as exc:  # confine difensivo di un filo di sfondo
            dettaglio = f"{type(exc).__name__}: {exc}"
            self._segna_fase(self._stato.get("fase") or FASI[0], ERRORE, dettaglio)
            # La stessa domanda della guardia d'ingresso, rifatta qui: il codice
            # puo' essere cambiato **mentre** la run camminava, e in quel caso il
            # guasto non e' un difetto del programma ma un processo da riavviare.
            # Senza questo, chi aggiorna il codice a run avviata si ritrova
            # ancora la riga di Python e nessuna cosa da fare.
            cambiato = versione_del_codice.avviso_del_codice_cambiato()
            codice = CODICE_CAMBIATO_DOPO_L_AVVIO if cambiato else "GUASTO_INATTESO"
            frase = cambiato or (
                "Il confronto non è riuscito. Riprova; se succede ancora, chiudi e riapri "
                "il comparatore."
            )
            self._aggiorna(
                ok=False,
                stato=ERRORE,
                messaggio=(
                    f"{frase} Il confronto precedente è rimasto attivo e non è stato toccato."
                ),
                fermata={
                    "code": codice,
                    "message": frase,
                    "documenti": [],
                    "dettaglio": dettaglio[:2000],
                },
                completatoIl=utc_ora(),
            )
        finally:
            # ⚠ L'audit resta prima del rilascio — scritto col lucchetto in
            # mano, altrimenti una run nuova potrebbe partire nel mezzo e
            # `self.stato()` racconterebbe LEI dentro la cartella di questa —
            # ma il rilascio adesso sta in un `finally` suo:
            # `_scrivi_audit_esecuzione` intercetta il solo `OSError`, e
            # qualunque altra eccezione lasciava il lucchetto in mano per
            # sempre. 409 a ogni «Ricalcola», niente spegnimento, niente
            # aggiornamento del codice: il rilascio non deve dipendere da
            # nient'altro.
            try:
                self._scrivi_audit_esecuzione(cartella, registro_artefatti)
            except Exception as errore:  # noqa: BLE001 - l'audit non uccide il filo
                # Lo stato della run e' gia' definitivo qui sopra: l'audit che
                # non riesce non cambia niente per chi guarda la pagina. Quello
                # che cambia e' come lo si scopre — un traceback grezzo su
                # `stderr` da un filo, in negozio, non lo legge nessuno, mentre
                # una riga `[AVVISO]` sta accanto a tutte le altre.
                print(f"[AVVISO] audit della run non scritto: {type(errore).__name__}: {errore}")
            finally:
                try:
                    self.lucchetto_lavori.release()
                except RuntimeError:  # pragma: no cover - non rilasciarlo due volte
                    pass

    def _scrivi_audit_esecuzione(
        self, cartella: Path, registro_artefatti: dict[str, dict[str, Any]]
    ) -> None:
        """L'audit della run, scritto **sempre**, anche quando la run fallisce.

        E' quello che la run successiva confronta con i propri numeri, ed e'
        l'unico posto in cui resta scritto quali artefatti sono stati prodotti
        davvero e con quale impronta.
        """

        try:
            scrivi_json(cartella / NOME_AUDIT_ESECUZIONE, {
                **self.stato(),
                "artefatti": registro_artefatti,
            })
        except OSError:  # pragma: no cover - il disco pieno non e' un errore di catena
            pass

    # -- gli attrezzi delle fasi -------------------------------------------

    def _esegui(
        self,
        corsa: "_Corsa",
        fase: str,
        argomenti: Sequence[str],
        *,
        script: str,
        uscite_ammesse: Sequence[int] = (0,),
    ) -> RisultatoComando:
        comando = [self.configurazione.python_executable, str(self.configurazione.scripts_dir / script), *argomenti]
        tetto = tetto_della_fase(fase)
        try:
            risultato = self.esecutore(
                comando,
                self.configurazione.scripts_dir,
                self._avanzamento_fase(fase),
                timeout_secondi=tetto,
            )
        except ComandoTroppoLungo as troppo:
            # Il figlio e' gia' stato ucciso da chi ha misurato il tetto. Qui si
            # da' un nome alla fermata, e il nome e' quello che l'utente legge in
            # pagina: il canale c'e' gia' e la pagina sa mostrarlo.
            corsa.comandi.append({
                "fase": fase,
                "script": script,
                "uscita": None,
                "tettoSecondi": tetto,
                "stderr": troppo.stderr[-4000:],
            })
            minuti = troppo.secondi / 60
            raise Fermata(
                codice=f"{fase}_TROPPO_LUNGA",
                messaggio=(
                    f"Il passo «{TITOLI_FASI.get(fase, fase).lower()}» non ha risposto entro "
                    f"{minuti:g} minuti: la catena si è fermata e il confronto di prima è "
                    "intatto. Riprova; se si ferma di nuovo nello stesso punto, il problema è "
                    "in uno dei documenti caricati."
                ),
                dettaglio=(
                    f"Passo «{script}», ucciso dopo {troppo.secondi:g} secondi.\n"
                    + troppo.stderr[-2000:]
                ),
            ) from None
        corsa.comandi.append({
            "fase": fase,
            "script": script,
            "uscita": risultato.uscita,
            # `stderr` si tiene tagliato: serve a spiegare un guasto, non a
            # diventare un secondo file di log.
            "stderr": risultato.stderr[-4000:],
        })
        if risultato.uscita not in uscite_ammesse:
            # ⚠ La fermata piu' frequente, e quella che l'utente legge di piu'.
            # Prima diceva «il passo "prepare_manifest_sources.py" si e' fermato
            # con esito 1»: un nome di file sorgente e un codice di uscita a
            # qualcuno che deve solo decidere cosa fare adesso.  Il nome dello
            # script resta nel `dettaglio`, che sta nel pieghevole tecnico.
            raise Fermata(
                codice=f"{fase}_NON_RIUSCITA",
                messaggio=(
                    f"Il confronto si è fermato durante: {TITOLI_FASI.get(fase, fase).lower()}. "
                    "Il confronto di prima è intatto. Riprova; se si ferma di nuovo nello stesso "
                    "punto, il problema è in uno dei documenti caricati."
                ),
                dettaglio=(
                    f"Passo «{script}», esito {risultato.uscita}.\n"
                    + (risultato.stderr or risultato.stdout)[-2000:]
                ),
            )
        return risultato

    def _avanzamento_fase(self, fase: str) -> AscoltaAvanzamento:
        def ascolta(evento: dict[str, Any]) -> None:
            fatti = evento.get("fatti")
            totali = evento.get("totali")
            dettaglio = ""
            if isinstance(fatti, int) and isinstance(totali, int) and totali:
                dettaglio = f"{fatti} di {totali}"
                costo = _numero(evento.get("costo_usd"))
                if costo:
                    dettaglio += f" · {costo:.3f} $"
            self._segna_fase(fase, IN_CORSO, dettaglio)
            numeri: dict[str, Any] = {}
            if isinstance(fatti, int):
                numeri["casiValutati"] = fatti
            if isinstance(totali, int):
                numeri["casiDaValutare"] = totali
            if evento.get("chiamate") is not None:
                numeri["chiamateAi"] = evento.get("chiamate")
            if evento.get("costo_usd") is not None:
                numeri["spesaUsd"] = evento.get("costo_usd")
            if numeri:
                self._numeri(**numeri)

        return ascolta

    def _prima_del_passo(self, corsa: "_Corsa", attesi: Sequence[Path]) -> None:
        """Nessuno degli artefatti che il passo deve produrre puo' esistere gia'.

        In una cartella nata vuota non ci puo' essere il file di ieri; questo
        controllo vale per il caso che resta — un passo rilanciato a mano, o due
        run che finiscono nella stessa cartella — e costa una `stat`.
        """

        for percorso in attesi:
            if percorso.exists():
                raise Fermata(
                    codice="ARTEFATTO_PREESISTENTE",
                    messaggio=(
                        f"«{percorso.name}» esisteva già prima che il passo lo scrivesse: "
                        "questa esecuzione non è pulita e non prosegue."
                    ),
                    documenti=[percorso.name],
                )

    def _dopo_il_passo(self, corsa: "_Corsa", fase: str, attesi: Sequence[Path]) -> None:
        """Ogni artefatto dichiarato dev'esserci, e la sua impronta si registra.

        E' il modo con cui «l'ho scritto» smette di essere una promessa: chi
        legge un artefatto piu' avanti ne ricontrolla l'impronta, quindi un file
        sostituito nel frattempo si vede.
        """

        for percorso in attesi:
            if not percorso.is_file():
                raise Fermata(
                    codice="ARTEFATTO_MANCANTE",
                    messaggio=(
                        f"{TITOLI_FASI.get(fase, fase)}: il passo dice di essere riuscito ma "
                        f"«{percorso.name}» non c'è."
                    ),
                    documenti=[percorso.name],
                )
            corsa.registro_artefatti[self._chiave_artefatto(corsa, percorso)] = {
                "fase": fase,
                "sha256": impronta_file(percorso),
                "byte": percorso.stat().st_size,
                "scritto_il": utc_ora(),
            }

    def _chiave_artefatto(self, corsa: "_Corsa", percorso: Path) -> str:
        try:
            return percorso.relative_to(corsa.cartella).as_posix()
        except ValueError:  # pragma: no cover - tutti gli artefatti stanno nella run
            return percorso.name

    def _leggi_artefatto(self, corsa: "_Corsa", percorso: Path) -> Any:
        """Rilegge un artefatto **dopo** aver ricontrollato la sua impronta."""

        chiave = self._chiave_artefatto(corsa, percorso)
        registrato = corsa.registro_artefatti.get(chiave)
        if registrato is None:
            raise Fermata(
                codice="ARTEFATTO_NON_REGISTRATO",
                messaggio=f"«{percorso.name}» non è stato prodotto da questa esecuzione.",
                documenti=[percorso.name],
            )
        if impronta_file(percorso) != registrato.get("sha256"):
            raise Fermata(
                codice="ARTEFATTO_CAMBIATO",
                messaggio=(
                    f"«{percorso.name}» è cambiato dopo essere stato scritto: qualcun altro "
                    "sta lavorando sugli stessi file e questa esecuzione si ferma."
                ),
                documenti=[percorso.name],
            )
        return leggi_json(percorso, None)

    # -- 1. PROFILAZIONE ---------------------------------------------------

    def _fase_profilazione(self, corsa: "_Corsa") -> None:
        fase = "PROFILAZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        uploads = self.configurazione.uploads_dir
        documenti = sorted(
            percorso for percorso in uploads.glob("*")
            if percorso.is_file() and percorso.suffix.casefold() in {".xlsx", ".xls", ".csv"}
        )
        if not documenti:
            raise Fermata(
                codice="NESSUN_DOCUMENTO",
                messaggio=(
                    "Non c'è nessun documento da confrontare: carica prima l'elenco del "
                    "gestionale e i listini dei fornitori."
                ),
            )
        corsa.profili_path = corsa.cartella / "input_profiles.json"
        self._prima_del_passo(corsa, [corsa.profili_path])
        risultato = self._esegui(
            corsa,
            fase,
            [str(uploads), "--output", str(corsa.profili_path), "--recursive"],
            script="inspect_sources.py",
            # L'esito 2 dell'inventario vuol dire «qualcuno di questi documenti
            # non si e' lasciato leggere»: il file si scrive lo stesso, e la
            # fermata la decide questa funzione dopo averlo letto, cosi' il
            # messaggio puo' dire **quali**.
            uscite_ammesse=(0, 2),
        )
        self._dopo_il_passo(corsa, fase, [corsa.profili_path])
        documento = self._leggi_artefatto(corsa, corsa.profili_path)
        if not isinstance(documento, dict):
            raise Fermata(
                codice="PROFILI_ILLEGGIBILI",
                messaggio="L'inventario dei documenti non è leggibile.",
            )
        errori = documento.get("errors") or []
        if errori:
            nomi = [Path(str(voce.get("path") or "")).name for voce in errori]
            raise Fermata(
                codice="DOCUMENTO_NON_LETTO",
                messaggio=(
                    f"{len(nomi)} documento non si è lasciato leggere e il confronto non "
                    "può proseguire senza sapere che cosa contiene: "
                    if len(nomi) == 1
                    else f"{len(nomi)} documenti non si sono lasciati leggere: "
                ) + ", ".join(f"«{nome}»" for nome in nomi)
                + ". Toglili dai documenti caricati oppure sostituiscili.",
                documenti=nomi,
                dettaglio=str(risultato.riepilogo.get("errors") or ""),
            )
        corsa.profili = list(documento.get("profiles") or [])
        self._numeri(documenti=len(corsa.profili))
        self._segna_fase(
            fase, COMPLETATO,
            f"{len(corsa.profili)} documenti letti",
            time.monotonic() - inizio,
        )

    # -- 2. RICONOSCIMENTO -------------------------------------------------

    def _fase_riconoscimento(self, corsa: "_Corsa") -> None:
        """Il percorso veloce della Fase 4, e la prima delle tre fermate.

        Nel caso normale — i listini di ogni settimana — il registro riconosce
        tutto e qui non si chiama nessuno: la decisione che
        `apply_preflight_decisions.py` pretende scritta a mano la genera
        l'orchestratore, perche' il riconoscimento l'ha gia' presa.

        Resta scritto a mano soltanto cio' che il registro **non** conosce, ed
        e' esattamente la fermata dichiarata: un fornitore nuovo si impara una
        volta, poi passa dal percorso veloce come gli altri.
        """

        fase = "RICONOSCIMENTO"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        manuali = self._decisioni_di_questi_documenti(self._decisioni_manuali(), corsa.profili)
        decisioni: list[dict[str, Any]] = []
        sconosciuti: list[str] = []
        variati: list[tuple[str, dict[str, Any]]] = []
        # Il documento e' riconosciuto benissimo: e' stato caricato con il tipo
        # sbagliato. Non e' un listino cambiato, e raccontarlo cosi' mandava a
        # cercare una variazione che non c'era.
        ruolo_sbagliato: list[tuple[str, str]] = []
        # ⚠ E il terzo caso, che fino al 22 agosto 2026 finiva fra gli
        # sconosciuti: un documento a cui manca **una** intestazione
        # obbligatoria e ha tutte le altre al posto giusto. «Non riconosco
        # ancora la disposizione delle colonne» manda a configurare un
        # fornitore nuovo, e sul PC del negozio e' successo davvero — il
        # listino BETULLA con la cella C1 svuotata in Excel.
        per_un_pelo: list[tuple[str, dict[str, Any]]] = []
        scelti, scartati = self._piu_recente_per_ruolo(corsa.profili, manuali)

        for profilo in scelti:
            nome = str(profilo.get("file_name") or "")
            a_mano = manuali.get(nome.casefold())
            if a_mano is not None:
                decisione = dict(a_mano)
                decisione.setdefault("file_name", nome)
                # `validate_input_manifest.py` pretende una motivazione da ogni
                # decisione, anche da quelle che dicono «questo file non
                # c'entra»: senza, una decisione scritta a mano bloccherebbe la
                # catena con `MOTIVAZIONE_AI_MANCANTE`, che non spiega niente.
                if not str(decisione.get("rationale") or "").strip():
                    decisione["rationale"] = (
                        f"Decisione scritta a mano in «{NOME_DECISIONI_MANUALI}»."
                    )
                # Una decisione manuale e' sovrana e copre anche un documento
                # che il registro declasserebbe: e' il primo passo del rimedio.
                # Ma spegnere le verifiche della firma senza dirlo lascerebbe
                # il file delle decisioni attivo per sempre, con la difesa
                # nuova che non partecipa piu' (revisione avversariale R4).
                stato_a_mano = str((profilo.get("deterministic_hint") or {}).get("state") or "")
                if stato_a_mano != "SCHEMA_NOTO":
                    self._avvisa(
                        "DECISIONE_MANUALE_ATTIVA",
                        f"«{nome}» entra per decisione manuale",
                        f"«{nome}» entra nel confronto per le colonne che hai indicato tu, "
                        f"scritte in «{NOME_DECISIONI_MANUALI}»: su questo documento le "
                        "verifiche della firma del registro non si applicano. Quando il "
                        "programma riesce a memorizzare quelle colonne, questa nota sparisce "
                        "da sola. Non c'è niente da cancellare a mano.",
                    )
                decisioni.append(decisione)
                conferma = decisione.get("user_confirmation") or {}
                if (
                    decisione.get("state") in {"SCHEMA_VARIATO", "NUOVO_FORNITORE"}
                    and conferma.get("status") == "CONFIRMED"
                ):
                    corsa.decisioni_da_imparare.append(nome)
                continue
            indizio = profilo.get("deterministic_hint") or {}
            stato_indizio = str(indizio.get("state") or "")
            ruolo_caricato = str(
                profilo.get("upload_role") or (profilo.get("ai_preflight") or {}).get("role") or ""
            ).casefold()
            adattatore_id = str(indizio.get("adapter_id") or "")
            if stato_indizio == "SCHEMA_NOTO" and ruolo_caricato in {"master", "supplier"} and adattatore_id:
                voce_registro = registro.adattatore(adattatore_id)
                ruolo_registro = "master" if voce_registro.get("kind") == "master" else "supplier"
                if ruolo_registro != ruolo_caricato:
                    ruolo_sbagliato.append((nome, str(voce_registro.get("display_name") or adattatore_id)))
                    continue
            if stato_indizio == "SCHEMA_VARIATO":
                # Un VARIATO non e' uno sconosciuto: il registro l'ha
                # riconosciuto, e' il documento che non combacia piu' con la
                # firma (una colonna in piu', due scambiate).  Dire «non
                # riconosce» mandava a cercare un fornitore nuovo quando la
                # notizia vera e' «il fornitore ha cambiato il listino»
                # (minore della verifica del 12 agosto 2026, chiuso in R4).
                variati.append((nome, indizio))
                continue
            if stato_indizio != "SCHEMA_NOTO":
                if indizio.get("quasi_adapter_id"):
                    per_un_pelo.append((nome, indizio))
                else:
                    sconosciuti.append(nome)
                continue
            decisioni.append(self._decisione_dal_registro(profilo, indizio))

        if sconosciuti or variati or ruolo_sbagliato or per_un_pelo:
            coinvolti = (
                sconosciuti
                + [nome for nome, _indizio in variati]
                + [nome for nome, _chi in ruolo_sbagliato]
                + [nome for nome, _indizio in per_un_pelo]
            )
            # ⚠ Fin qui le due liste sono separate, e il commento qui sopra dice
            # perche'. Fino al 21 agosto 2026 da questa riga in giu' la
            # differenza spariva: un `+` e una frase sola, «Non riconosco ancora
            # la disposizione delle colonne in ...», detta anche a un listino che
            # il registro riconosce con confidenza 0,98 (misurato su
            # «3listino_Cipresso.xlsx»: cambia il nome del foglio, che porta la
            # data, non le colonne). La ragione adesso viaggia con la fermata.
            motivi = {nome: "SCONOSCIUTO" for nome in sconosciuti}
            motivi.update({nome: "VARIATO" for nome, _indizio in variati})
            motivi.update({nome: "RUOLO_SBAGLIATO" for nome, _chi in ruolo_sbagliato})
            motivi.update({nome: "QUASI" for nome, _indizio in per_un_pelo})
            frasi: list[str] = []
            if sconosciuti:
                frasi.append(
                    "Non riconosco ancora la disposizione delle colonne in "
                    + ", ".join(f"«{nome}»" for nome in sconosciuti)
                    + "."
                )
            if variati:
                nomi_dei_fornitori = []
                for nome, indizio in variati:
                    voce = registro.adattatore(str(indizio.get("adapter_id") or ""))
                    etichetta = str(voce.get("display_name") or "").strip()
                    nomi_dei_fornitori.append(
                        f"{etichetta} («{nome}»)" if etichetta else f"«{nome}»"
                    )
                frasi.append(
                    "È cambiato il listino di "
                    + ", ".join(nomi_dei_fornitori)
                    + ": il fornitore lo conosco, ma il documento non è più come era e non"
                    " voglio leggere i prezzi dal posto sbagliato."
                )
            if per_un_pelo:
                # La frase la scrive il registro, che e' l'unico posto che sa
                # quale intestazione manca e in quale colonna stava: riscriverla
                # qui vorrebbe dire due versioni della stessa notizia, e la
                # settimana prossima ne direbbero due diverse.
                frasi.extend(
                    f"«{nome}»: " + str((indizio.get("evidence") or ["non si sa perché"])[0])
                    for nome, indizio in per_un_pelo
                )
            if ruolo_sbagliato:
                frasi.append(
                    "; ".join(
                        f"«{nome}» è il listino di {chi}, ma è stato caricato come elenco del "
                        "gestionale (o viceversa)"
                        for nome, chi in ruolo_sbagliato
                    )
                    + ": cambia il tipo di documento qui sotto."
                )
            frasi.append(
                "Controlla le colonne nell'anteprima qui sotto: controllo i dati prima di"
                " usarli e, dopo la conferma, il confronto riparte da solo."
            )
            raise Fermata(
                codice="SCHEMA_SCONOSCIUTO",
                messaggio=" ".join(frasi),
                documenti=coinvolti,
                motivi=motivi,
            )

        for tenuto, lasciato_fuori in scartati:
            # ⚠ Il messaggio dice tre cose e nessuna in piu': chi e' stato
            # tenuto, chi e' rimasto fuori, e su che cosa e' stata fatta la
            # scelta.  Prima diceva «il confronto usa il piu' recente» senza
            # nominare il vincitore, e il lettore capiva «il listino piu'
            # recente»: la scelta invece guarda la data del **file**, che con la
            # validita' del listino non c'entra niente.  Un listino scaduto
            # ricopiato oggi vince, e finche' non lo si dice nessuno lo sa.
            messaggio = (
                f"«{lasciato_fuori.get('file_name')}» resta fuori dal confronto: dello stesso "
                f"fornitore c'è anche «{tenuto.get('file_name')}», ed è quello che viene usato. "
                "La scelta è fatta sulla data di modifica dei file nella cartella dei documenti "
                "caricati, non sulla validità scritta dentro i listini: se il listino buono è "
                f"«{lasciato_fuori.get('file_name')}», togli «{tenuto.get('file_name')}» dalla "
                "cartella e rifai il confronto."
            )
            if str(tenuto.get("modified_at") or "") == str(lasciato_fuori.get("modified_at") or ""):
                # A parita' di data la data non ha deciso niente, e dirlo evita
                # di attribuire la scelta a un criterio che non l'ha fatta: una
                # cartella copiata con robocopy conserva i tempi, e capita.
                messaggio += (
                    " I due file risultano modificati nello stesso momento: a parità di data "
                    "resta l'ultimo in ordine di elenco."
                )
            self._avvisa(
                "DOCUMENTO_PIU_VECCHIO_LASCIATO_FUORI",
                "Due documenti dello stesso fornitore",
                messaggio,
                tenuto=str(tenuto.get("file_name") or ""),
                lasciatoFuori=str(lasciato_fuori.get("file_name") or ""),
            )

        corsa.decisioni_path = corsa.cartella / "preflight_decisions.json"
        corsa.profili_usati_path = corsa.cartella / "input_profiles_usati.json"
        corsa.manifest_path = corsa.cartella / "input_manifest.json"
        self._prima_del_passo(corsa, [corsa.decisioni_path, corsa.profili_usati_path, corsa.manifest_path])
        # L'inventario completo resta in `input_profiles.json`, che e' il
        # verbale di quello che c'era.  Al manifest arrivano solo i documenti
        # che entrano nel confronto: `apply_preflight_decisions` pretende una
        # decisione per ogni profilo che gli si passa, e un listino superato da
        # uno piu' recente comparirebbe in pagina come un documento «da
        # correggere» — che non e'.
        scrivi_json(corsa.profili_usati_path, {"schema_version": 1, "profiles": scelti})
        scrivi_json(corsa.decisioni_path, {"decisions": decisioni})
        for percorso in (corsa.profili_usati_path, corsa.decisioni_path):
            corsa.registro_artefatti[self._chiave_artefatto(corsa, percorso)] = {
                "fase": fase,
                "sha256": impronta_file(percorso),
                "byte": percorso.stat().st_size,
                "scritto_il": utc_ora(),
            }
        self._esegui(
            corsa,
            fase,
            [
                "--profiles", str(corsa.profili_usati_path),
                "--decisions", str(corsa.decisioni_path),
                "--output", str(corsa.manifest_path),
            ],
            script="apply_preflight_decisions.py",
        )
        self._dopo_il_passo(corsa, fase, [corsa.manifest_path])
        fornitori = sorted({
            str(voce.get("supplier_id"))
            for voce in decisioni
            if voce.get("role") == "supplier" and voce.get("supplier_id")
        })
        corsa.fornitori_attesi = fornitori
        self._numeri(fornitori=len(fornitori), fornitoriElenco=fornitori)
        self._segna_fase(
            fase, COMPLETATO,
            f"{len(fornitori)} fornitori riconosciuti, nessuna chiamata AI"
            if not manuali else f"{len(fornitori)} fornitori riconosciuti",
            time.monotonic() - inizio,
        )

    def _decisioni_di_questi_documenti(
        self,
        manuali: dict[str, dict[str, Any]],
        profili: Sequence[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Tiene le decisioni che parlano dei documenti caricati adesso.

        Una decisione confermata era indicizzata per NOME del file, e
        l'eliminazione di un listino non la toccava: si eliminava un listino
        configurato male, si ricaricava il file giusto con lo stesso nome, e il
        ricalcolo riapplicava la mappatura vecchia a un documento diverso —
        cioè metteva le quantità leggendo le colonne sbagliate. Ora la decisione
        porta l'impronta del documento su cui è stata data, e quando non
        combacia si mette da parte **dicendolo**: uno scarto silenzioso qui
        sarebbe la stessa malattia, dall'altro lato.
        """

        if not manuali:
            return {}
        per_nome = {str(profilo.get("file_name") or "").casefold(): profilo for profilo in profili}
        tenute: dict[str, dict[str, Any]] = {}
        for nome, decisione in manuali.items():
            profilo = per_nome.get(nome)
            if profilo is None or self._decisione_e_di_questo_documento(decisione, profilo):
                tenute[nome] = decisione
                continue
            etichetta = str(profilo.get("file_name") or nome)
            self._avvisa(
                "DECISIONE_MANUALE_SCARTATA",
                f"«{etichetta}» non è il documento configurato",
                f"Le colonne confermate per «{etichetta}» erano di un altro documento con lo "
                "stesso nome. Questo file viene riconosciuto da capo: se il confronto si ferma "
                "di nuovo sulle colonne, indicamele un'altra volta.",
            )
        return tenute

    @staticmethod
    def _decisione_e_di_questo_documento(
        decisione: Mapping[str, Any],
        profilo: Mapping[str, Any],
    ) -> bool:
        """La decisione confermata riguarda proprio questo file?

        Il confronto è sull'impronta del contenuto, non sul nome: due settimane
        di seguito il listino si chiama sempre «betulla.xlsx». Una decisione senza
        impronta è quella scritta a mano in «decisioni_schemi.json» — la via
        d'uscita documentata quando il riconoscimento non basta — e continua a
        valere per nome: toglierle il permesso vorrebbe dire togliere il rimedio.
        """

        confermata = str(decisione.get("file_sha256") or "").strip().casefold()
        if not confermata:
            return True
        adesso = str(profilo.get("sha256") or "").strip().casefold()
        # Un profilo senza impronta non e' una prova che il documento sia
        # cambiato: non lo si scarta per un dato che non c'e'.
        return not adesso or adesso == confermata

    def _decisioni_manuali(self) -> dict[str, dict[str, Any]]:
        documento = leggi_json(self.configurazione.decisioni_manuali_path, None)
        if isinstance(documento, dict):
            voci = documento.get("decisions")
        else:
            voci = documento
        if not isinstance(voci, list):
            return {}
        risultato: dict[str, dict[str, Any]] = {}
        for voce in voci:
            if not isinstance(voce, dict):
                continue
            nome = str(voce.get("file_name") or "").strip()
            if nome:
                risultato[nome.casefold()] = voce
        return risultato

    @staticmethod
    def _piu_recente_per_ruolo(
        profili: Sequence[dict[str, Any]],
        manuali: Mapping[str, dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
        """Un documento per fornitore: quello modificato per ultimo.

        La cartella dei documenti caricati si accumula settimana dopo settimana,
        e due listini dello stesso fornitore farebbero fallire il parser con
        «Fornitore duplicato» — cioe' con un messaggio che non dice a nessuno
        che cosa fare.  Si tiene uno solo e **si dice** quale e' rimasto fuori:
        uno scarto silenzioso e' peggio di un errore.

        ⚠ «Piu' recente» qui vuol dire `modified_at`, cioe' quando il file e'
        stato messo nella cartella: non e' la validita' dichiarata dentro il
        listino, e le due cose possono dire il contrario l'una dell'altra — un
        listino scaduto ricopiato oggi vince su quello valido di ieri.  Il
        criterio resta questo perche' e' l'unico segnale che c'e' su **tutti** i
        fornitori; quello che cambia e' che adesso l'avviso lo dichiara, invece
        di far credere che sia il listino piu' recente.  Per questo si tiene la
        coppia (tenuto, scartato) e non il solo scartato.

        I documenti che il registro non riconosce non si toccano: la loro sorte
        la decide la fermata, non questa regola.
        """

        manuali = manuali or {}
        per_chiave: dict[str, dict[str, Any]] = {}
        persi_per_chiave: dict[str, list[dict[str, Any]]] = {}
        sconosciuti: list[dict[str, Any]] = []
        for profilo in profili:
            indizio = profilo.get("deterministic_hint") or {}
            adattatore_id = str(indizio.get("adapter_id") or "")
            manuale = manuali.get(str(profilo.get("file_name") or "").casefold())
            ruolo_caricato = str(
                profilo.get("upload_role") or (profilo.get("ai_preflight") or {}).get("role") or ""
            ).casefold()
            if isinstance(manuale, dict) and manuale.get("role") in {"master", "supplier"}:
                chiave = (
                    "master" if manuale.get("role") == "master"
                    else f"supplier:{manuale.get('supplier_id') or manuale.get('adapter_id') or profilo.get('profile_id')}"
                )
            elif ruolo_caricato in {"master", "supplier"}:
                if ruolo_caricato == "master":
                    chiave = "master"
                else:
                    voce = registro.adattatore(adattatore_id) if adattatore_id else {}
                    chiave = f"supplier:{voce.get('supplier_id') or adattatore_id or profilo.get('profile_id')}"
            else:
                if str(indizio.get("state") or "") != "SCHEMA_NOTO" or not adattatore_id:
                    sconosciuti.append(profilo)
                    continue
                voce = registro.adattatore(adattatore_id)
                chiave = (
                    "master" if voce.get("kind") == "master"
                    else f"supplier:{voce.get('supplier_id') or adattatore_id}"
                )
            precedente = per_chiave.get(chiave)
            if precedente is None:
                per_chiave[chiave] = profilo
                continue
            if str(profilo.get("modified_at") or "") >= str(precedente.get("modified_at") or ""):
                per_chiave[chiave] = profilo
                persi_per_chiave.setdefault(chiave, []).append(precedente)
            else:
                persi_per_chiave.setdefault(chiave, []).append(profilo)
        # Le coppie si formano solo alla fine: con tre documenti dello stesso
        # fornitore, chi vinceva a meta' giro non e' detto che vinca, e un
        # avviso che nomina un vincitore intermedio direbbe il falso.
        scartati = sorted(
            ((per_chiave[chiave], perso) for chiave, persi in persi_per_chiave.items() for perso in persi),
            key=lambda coppia: str(coppia[1].get("file_name") or "").casefold(),
        )
        scelti = sorted(
            [*per_chiave.values(), *sconosciuti],
            key=lambda voce: str(voce.get("file_name") or "").casefold(),
        )
        return scelti, scartati

    @staticmethod
    def _decisione_dal_registro(profilo: dict[str, Any], indizio: dict[str, Any]) -> dict[str, Any]:
        """La decisione che l'utente avrebbe scritto a mano, presa dal registro.

        La `field_mapping` si porta dietro **sempre** quando il registro ce
        l'ha: senza, il manifest non direbbe dov'e' la colonna d'ordine di
        Cipresso, e la compilazione lo lascerebbe fuori dicendo «manca la
        mappatura confermata» a chi non ha tolto niente.
        """

        adattatore_id = str(indizio.get("adapter_id") or "")
        voce = registro.adattatore(adattatore_id)
        ruolo = "master" if voce.get("kind") == "master" else "supplier"
        decisione: dict[str, Any] = {
            "file_name": profilo.get("file_name"),
            "profile_id": profilo.get("profile_id"),
            "state": "SCHEMA_NOTO",
            "role": ruolo,
            "adapter_id": adattatore_id,
            "confidence": "ALTA",
            "rationale": (
                f"Schema riconosciuto dal registro ({adattatore_id}, "
                f"confidenza {indizio.get('confidence')}): nessuna chiamata AI."
            ),
            "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
        }
        if ruolo == "supplier":
            decisione["supplier_id"] = voce.get("supplier_id")
        mappatura = voce.get("field_mapping")
        if isinstance(mappatura, dict):
            decisione["field_mapping"] = deepcopy(mappatura)
        return decisione

    # -- 3. VALIDAZIONE ----------------------------------------------------

    def _fase_validazione(self, corsa: "_Corsa") -> None:
        """`prepare_manifest_sources` non legge la validazione: la legge qui.

        Lo script del parser gira anche su un manifest bocciato, e senza questa
        fermata un master mancante o due fornitori con lo stesso identificativo
        diventerebbero un confronto sbagliato invece di un messaggio.
        """

        fase = "VALIDAZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.validazione_path = corsa.cartella / "manifest_validation.json"
        self._prima_del_passo(corsa, [corsa.validazione_path])
        self._esegui(
            corsa,
            fase,
            [
                "--manifest", str(corsa.manifest_path),
                "--adapters", str(self.configurazione.adapters_path),
                "--output", str(corsa.validazione_path),
            ],
            script="validate_input_manifest.py",
            uscite_ammesse=(0, 2),
        )
        self._dopo_il_passo(corsa, fase, [corsa.validazione_path])
        validazione = self._leggi_artefatto(corsa, corsa.validazione_path)
        errori = (validazione or {}).get("errors") or []
        if errori:
            raise Fermata(
                codice="MANIFEST_NON_VALIDO",
                messaggio=(
                    "I documenti caricati non formano un confronto valido: "
                    + "; ".join(_frase_dell_errore(voce) for voce in errori[:5])
                ),
                dettaglio=json.dumps(errori[:20], ensure_ascii=False),
            )
        avvisi = (validazione or {}).get("warnings") or []
        for avviso in avvisi[:10]:
            # ⚠ `str(avviso)` stampava il dizionario Python cosi' com'e' —
            # «{'code': '...', 'message': '...'}» — in faccia a chi usa il
            # programma. La frase in italiano e' gia' li' dentro:
            # `validate_input_manifest.py` la scrive apposta. Il dizionario
            # resta solo come ripiego, se un giorno un avviso arrivasse in
            # un'altra forma.
            #
            # (`NESSUN_FORNITORE`, che era l'esempio di questo commento, dal 22
            # agosto 2026 e' un errore e non un avviso: era l'unico avviso che
            # la fase dopo trasformava in un guasto, e la frase che dice cosa
            # fare la sapeva gia' questa fase.)
            testo = avviso.get("message") if isinstance(avviso, dict) else None
            self._avvisa("MANIFEST_AVVISO", "Avviso sui documenti", str(testo or avviso))
        self._segna_fase(
            fase, COMPLETATO, "manifest valido", time.monotonic() - inizio
        )

    # -- 4. PARSING --------------------------------------------------------

    def _scrivi_le_uguaglianze(self, corsa: "_Corsa") -> Path | None:
        """Le uguaglianze fra codici in vigore, dentro la cartella della run.

        Restituisce il percorso del file, oppure `None` quando non ce n'e'
        nessuna: chiedere il file al passo successivo e non avercelo sarebbe una
        fermata, e una run senza dichiarazioni e' il caso normale.

        ⚠ **Si scrive anche quando il magazzino non si apre**, con la lista
        vuota? No: in quel caso non si scrive niente e il passo lavora come
        prima. La differenza fra «non ce ne sono» e «non riesco a leggerle» la
        dice `self.uguaglianze_dichiarate`, che è del servizio; qui una
        eccezione fermerebbe il ricalcolo per una memoria che è un di piu',
        quindi si prosegue e si scrive nei numeri della run quante ne valevano.
        """

        if self.uguaglianze_dichiarate is None:
            return None
        try:
            classi = [list(gruppo) for gruppo in self.uguaglianze_dichiarate() or [] if len(gruppo) > 1]
        except Exception as exc:  # noqa: BLE001 - una memoria in meno non ferma un ricalcolo
            self._numeri(uguaglianzeDichiarate=0, uguaglianzeNonLette=f"{type(exc).__name__}: {exc}")
            return None
        self._numeri(uguaglianzeDichiarate=len(classi))
        if not classi:
            return None
        corsa.dati_dir.mkdir(parents=True, exist_ok=True)
        percorso = corsa.dati_dir / "uguaglianze.json"
        # `write_text` su Windows farebbe CRLF, e questo file lo rilegge un
        # passo della catena: in binario e a fine riga LF come tutti gli altri.
        scrittura_sicura.scrivi_json(percorso, {"classi": classi})
        return percorso

    def _fase_parsing(self, corsa: "_Corsa") -> None:
        fase = "PARSING"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.dati_dir = corsa.cartella / "dati"
        attesi = [
            corsa.dati_dir / nome for nome in (
                "normalized_sources.json", "display_offers.json",
                "matching_result.json", "semantic_queue.json", "audit.json",
            )
        ]
        self._prima_del_passo(corsa, attesi)
        argomenti = [
            "--manifest", str(corsa.manifest_path),
            "--adapters", str(self.configurazione.adapters_path),
            "--output", str(corsa.dati_dir),
        ]
        uguaglianze = self._scrivi_le_uguaglianze(corsa)
        if uguaglianze is not None:
            argomenti += ["--equivalenze", str(uguaglianze)]
        self._esegui(corsa, fase, argomenti, script="prepare_manifest_sources.py")
        self._dopo_il_passo(corsa, fase, attesi)
        audit = self._leggi_artefatto(corsa, corsa.dati_dir / "audit.json") or {}
        corsa.audit = audit if isinstance(audit, dict) else {}
        fonti = corsa.audit.get("sources") or {}
        letti = {str(nome): int((valori or {}).get("rows") or 0) for nome, valori in fonti.items()}
        # Un fornitore che il manifest dichiarava e che dal parser esce con zero
        # righe non e' un dettaglio: e' un fornitore sparito dal confronto.
        mancanti = [nome for nome in corsa.fornitori_attesi if letti.get(nome, 0) <= 0]
        if mancanti:
            # E' la terza fermata, della stessa famiglia della seconda: un
            # listino caricato apposta da cui non esce nemmeno una riga e' un
            # documento che nella sostanza non si e' letto.  Un avviso non
            # bloccante farebbe sparire il fornitore dal confronto e gli
            # ordini si farebbero senza di lui (classificata il 13 agosto
            # 2026 su delega di Daniele).
            raise Fermata(
                codice="FORNITORE_SENZA_RIGHE",
                messaggio=(
                    "Il listino di " + ", ".join(nome.upper() for nome in mancanti)
                    + " non ha prodotto nessuna riga ordinabile: il confronto direbbe che quel "
                    "fornitore non ha niente, e non è vero. Il confronto precedente è rimasto "
                    "attivo. Si rimedia così: guarda il suo documento fra i caricamenti — se è "
                    "il file sbagliato o un listino vuoto, sostituiscilo (o togli il fornitore "
                    "dai caricamenti) e rifai il confronto; se invece il fornitore ha cambiato "
                    "la forma del listino, la strada è quella della fermata degli schemi, in "
                    "«references/schema-routing.md»."
                ),
                documenti=mancanti,
            )
        self._controlla_i_prezzi(corsa, letti)
        scartati = {}
        for voce in corsa.audit.get("inputs") or []:
            non_ordinabili = voce.get("rows_not_orderable") or {}
            lettura = voce.get("reading") or {}
            # Il produttore scrive `rows_excluded` (`prepare_manifest_sources`);
            # `excluded` resta come ripiego per gli audit piu' vecchi.  Con la
            # sola chiave sbagliata le escluse non entravano mai nei numeri
            # della run (rilievo preesistente, chiuso in R4).
            esclusi = lettura.get("rows_excluded") or lettura.get("excluded") or {}
            if non_ordinabili or esclusi:
                scartati[str(voce.get("supplier_id") or voce.get("role") or "?")] = {
                    "nonOrdinabili": non_ordinabili,
                    "esclusi": esclusi,
                }
        self._numeri(
            righeLette=letti,
            prodottiGestionale=int((corsa.audit.get("master") or {}).get("rows") or 0),
            scartati=scartati,
        )
        self._segna_fase(
            fase, COMPLETATO,
            ", ".join(f"{nome.upper()} {quante}" for nome, quante in sorted(letti.items())),
            time.monotonic() - inizio,
        )

    def _controlla_i_prezzi(self, corsa: "_Corsa", letti: dict[str, int]) -> None:
        """Un listino con righe e senza prezzi si dice subito, gia' alla prima run.

        `_post_check` confronta con la volta prima: alla prima run non guarda
        niente, e dalla seconda salta proprio il caso peggiore, perche' una
        mediana assente — cioe' `usable: 0` nell'audit — usciva da
        `if not prima or adesso is None: continue` senza una parola.

        Misurato il 12 agosto 2026 sui listini veri: con il prezzo puntato
        sulla colonna del totale di riga, 242 prodotti a 0,00 € con confidenza
        CERTA e un piano d'ordine da 0,00 €, mentre `dati/audit.json` scriveva
        gia' `usable: 0` per quel fornitore.  Il dato c'era: non lo guardava
        nessuno.

        Qui il paragone non e' con la settimana scorsa, e' con lo zero, che non
        ha bisogno di un termine di paragone: un listino letto per intero in
        cui nessuna riga ha un prezzo maggiore di zero non e' un listino
        conveniente, e' una colonna sbagliata.  E un fornitore a zero non
        sparisce dal confronto — **vince**, ogni riga, e trascina con se'
        l'intero ordine.

        Resta un avviso e non una fermata: le fermate sono quelle in cui il
        programma non ha l'autorita' per decidere perche' l'ingresso non si
        puo' usare (uno schema che il registro non conosce, un documento che
        non si legge, un listino senza righe ordinabili).  Qui il
        programma sa benissimo che cosa e' successo e lo puo' dire; il
        controllo sui numeri avvisa e non ferma, per decisione del 12 agosto
        2026.  Ma e' un avviso in rosso, che entra nel documento oltre che in
        pagina, e che non si puo' spegnere.
        """

        prezzi = corsa.audit.get("price_summary")
        if not isinstance(prezzi, dict):
            # «Non misurato» non e' «misurato zero»: un audit senza il
            # riepilogo dei prezzi (formato piu' vecchio di questa correzione)
            # accendeva PREZZI_A_ZERO su ogni fornitore della run, e un avviso
            # che parte sempre e' un avviso che nessuno legge piu' (revisione
            # avversariale del 13 agosto 2026).
            self._avvisa(
                "PREZZI_NON_MISURATI",
                "Il controllo dei prezzi non si è potuto fare",
                "L'audit di questa run non porta il riepilogo dei prezzi "
                "(price_summary), quindi il controllo che scopre una colonna "
                "dei prezzi sbagliata non ha niente da guardare. Il confronto "
                "di oggi lo scrive sempre: se questo avviso esce, l'audit "
                "viene da un formato più vecchio e conviene rifare il "
                "confronto.",
            )
            return
        senza: list[str] = []
        for nome, righe in sorted(letti.items()):
            if righe <= 0:
                continue
            voce = prezzi.get(nome)
            voce = voce if isinstance(voce, dict) else {}
            mediana = _numero(voce.get("median"))
            if mediana is not None and mediana > 0:
                continue
            usabili = int(_numero(voce.get("usable")) or 0)
            # La frase dice solo cio' che l'audit prova: «4660 prezzi maggiori
            # di zero e mediana zero» erano due meta' che non stavano insieme.
            if mediana is None and usabili <= 0:
                diagnosi = "nessuna delle quali porta un prezzo al pezzo utilizzabile"
            elif mediana is None:
                diagnosi = (f"un riepilogo dei prezzi incoerente ({usabili} prezzi "
                            "dichiarati utilizzabili ma nessun prezzo mediano)")
            else:
                diagnosi = (f"un prezzo mediano dichiarato di {mediana:g} su "
                            f"{usabili} prezzi dichiarati utilizzabili")
            senza.append(nome)
            self._avvisa(
                "PREZZI_A_ZERO",
                f"{nome.upper()}: nessun prezzo utilizzabile",
                f"Il listino {nome.upper()} porta {righe} righe nel confronto e "
                + diagnosi
                + ". Così quel fornitore comparirebbe a 0,00 € e vincerebbe ogni "
                "riga del confronto. Quasi sempre vuol dire che la colonna del prezzo "
                "dichiarata per questo listino non è quella dei prezzi: controllala "
                "prima di mandare l'ordine.",
                severita="error",
                fornitore=nome,
                righe=righe,
                prezziUsabili=usabili,
            )
        if senza:
            self._numeri(fornitoriSenzaPrezzi=senza)

    # -- 5. SHORTLIST ------------------------------------------------------

    def _fase_shortlist(self, corsa: "_Corsa") -> None:
        fase = "SHORTLIST"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.shortlists_path = corsa.dati_dir / "semantic_shortlists.json"
        self._prima_del_passo(corsa, [corsa.shortlists_path])
        risultato = self._esegui(
            corsa,
            fase,
            [
                "--normalized", str(corsa.dati_dir / "normalized_sources.json"),
                "--queue", str(corsa.dati_dir / "semantic_queue.json"),
                "--output", str(corsa.shortlists_path),
                "--top-k", str(self.configurazione.top_k),
            ],
            script="build_semantic_shortlists.py",
        )
        self._dopo_il_passo(corsa, fase, [corsa.shortlists_path])
        shortlists = self._leggi_artefatto(corsa, corsa.shortlists_path)
        corsa.numero_shortlist = len(shortlists) if isinstance(shortlists, list) else 0
        self._numeri(casiSemantici=corsa.numero_shortlist)
        self._segna_fase(
            fase, COMPLETATO,
            f"{corsa.numero_shortlist} casi da valutare",
            time.monotonic() - inizio,
        )
        corsa.riepiloghi["shortlist"] = risultato.riepilogo

    # -- 6. VALUTAZIONE_AI -------------------------------------------------

    def _fase_valutazione_ai(self, corsa: "_Corsa") -> None:
        """Il passo che costa due minuti e 0,13 $.  Un degrado non ferma niente.

        «Se OpenRouter non risponde il programma tira dritto» e' una decisione
        presa: i casi che l'AI avrebbe interpretato restano `DA_VERIFICARE` e
        arrivano al revisore, che li guarda con l'ordine davanti.
        """

        fase = "VALUTAZIONE_AI"
        # ⚠ Il dettaglio si scrive QUI, all'inizio, e non solo alla fine come
        # fanno le altre fasi.  È il passo che costa due minuti: la barra sale a
        # scatti fino al 55,6 % e poi resta ferma per il tempo più lungo
        # dell'intero ricalcolo.  Senza questo, in pagina la riga della fase
        # diceva «in corso» e basta, e «fermo al 56 % da due minuti» per una
        # persona non tecnica vuol dire «si è piantato» — con la reazione
        # naturale di chiudere il programma proprio mentre la catena lavora.
        quanti = corsa.numero_shortlist
        self._segna_fase(fase, IN_CORSO, f"{quanti} {'caso' if quanti == 1 else 'casi'} da valutare")
        inizio = time.monotonic()
        corsa.decisioni_ai_path = corsa.dati_dir / "ai_decisions.json"
        corsa.rapporto_ai_path = corsa.dati_dir / "ai_rapporto.json"
        self._prima_del_passo(corsa, [corsa.decisioni_ai_path, corsa.rapporto_ai_path])
        self._esegui(
            corsa,
            fase,
            [
                "--shortlists", str(corsa.shortlists_path),
                "--output", str(corsa.decisioni_ai_path),
                "--rapporto", str(corsa.rapporto_ai_path),
            ],
            script="valuta_shortlist.py",
            # 5 e' il degrado dichiarato: i due file ci sono, una parte dei casi
            # non e' stata valutata.  La catena prosegue e lo dice.
            uscite_ammesse=(0, 5),
        )
        self._dopo_il_passo(corsa, fase, [corsa.decisioni_ai_path, corsa.rapporto_ai_path])
        rapporto = self._leggi_artefatto(corsa, corsa.rapporto_ai_path)
        if not isinstance(rapporto, dict):
            raise Fermata(
                codice="RAPPORTO_AI_ILLEGGIBILE",
                messaggio="La contabilità della fase AI non è leggibile.",
            )
        corsa.rapporto_ai = rapporto
        self._numeri(
            casiRicevuti=rapporto.get("casi_ricevuti"),
            casiValutabili=rapporto.get("casi_valutabili"),
            casiDecisi=rapporto.get("casi_decisi"),
            chiamateAi=rapporto.get("chiamate"),
            spesaUsd=rapporto.get("costo_usd"),
            modello=rapporto.get("model"),
        )
        if rapporto.get("degradato"):
            self._avvisa(
                "FASE_AI_DEGRADATA",
                "Valutazione automatica incompleta",
                "Una parte dei casi non è stata valutata e resta da verificare a mano. "
                + str(rapporto.get("motivo_degrado") or ""),
            )
        self._segna_fase(
            fase, COMPLETATO,
            f"{rapporto.get('casi_decisi')} decisi su {rapporto.get('casi_valutabili')}"
            f" · {_numero(rapporto.get('costo_usd')) or 0:.3f} $",
            time.monotonic() - inizio,
        )

    # -- 7. RISOLUZIONE ----------------------------------------------------

    def _fase_risoluzione(self, corsa: "_Corsa") -> None:
        fase = "RISOLUZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.risolti_path = corsa.dati_dir / "resolved_matches.json"
        self._prima_del_passo(corsa, [corsa.risolti_path])
        attese = corsa.rapporto_ai.get("casi_decisi")
        if not isinstance(attese, int) or attese < 0:
            raise Fermata(
                codice="RAPPORTO_AI_SENZA_CONTEGGIO",
                messaggio="La fase AI non dichiara quanti casi ha deciso: la catena si ferma.",
            )
        risultato = self._esegui(
            corsa,
            fase,
            [
                "--matching", str(corsa.dati_dir / "matching_result.json"),
                "--normalized", str(corsa.dati_dir / "normalized_sources.json"),
                "--shortlists", str(corsa.shortlists_path),
                "--decisions", str(corsa.decisioni_ai_path),
                "--decisions-attese", str(attese),
                "--output", str(corsa.risolti_path),
            ],
            script="merge_match_decisions.py",
            # 4 e 5 scrivono il file con le coppie guaste gia' degradate a
            # `DA_VERIFICARE`: e' un artefatto onesto e la catena prosegue
            # dicendolo.  2 e 3 no: li' gli artefatti non parlano dello stesso
            # lavoro e il confronto sarebbe una bugia.
            uscite_ammesse=(0, 4, 5),
        )
        self._dopo_il_passo(corsa, fase, [corsa.risolti_path])
        riepilogo = risultato.riepilogo
        corsa.riepiloghi["risoluzione"] = riepilogo
        if risultato.uscita in (4, 5):
            self._avvisa(
                "DECISIONI_AI_SCARTATE",
                "Alcune decisioni automatiche sono state scartate",
                "Le coppie interessate tornano da verificare a mano: "
                f"{riepilogo.get('decisioni_scartate_per_disallineamento', 0)} per listino "
                f"disallineato, {riepilogo.get('decisioni_scartate_per_riga_inventata', 0)} per "
                f"riga fuori dai candidati, "
                f"{riepilogo.get('decisioni_scartate_per_ean_non_rispettato', 0)} per EAN non "
                "rispettato.",
            )
        self._giudica_provenienza(corsa)
        self._numeri(
            accettatiSenzaConferma=riepilogo.get("accettati_senza_conferma"),
            rifiutiConCandidatoForte=riepilogo.get("rifiuti_con_candidato_forte"),
            # Quante righe sono entrate per la regola 7 del merge: viaggia nel
            # diario del negozio con `pipeline_status.json`.
            abbinamentiPerStessoCodice=riepilogo.get("abbinamenti_per_stesso_codice"),
        )
        self._segna_fase(
            fase, COMPLETATO,
            f"{riepilogo.get('decisioni_con_riscontro', 0)} decisioni entrate",
            time.monotonic() - inizio,
        )

    def _giudica_provenienza(self, corsa: "_Corsa") -> None:
        """La 6b scriveva la provenienza e non la giudicava: qui si giudica.

        Ogni decisione porta il modello e le due versioni di prompt con cui e'
        stata presa; `merge_match_decisions.py` le legge e non le confronta con
        niente, perche' la configurazione viva non ce l'ha.

        ⚠ Che cosa questo controllo prova e che cosa no.  Dentro una run
        orchestrata le decisioni le scrive la fase AI di questa stessa run, con
        la configurazione di oggi: qui il confronto **non puo'** trovare
        differenze, ed e' giusto che sia cosi'.  Serve per il caso che resta —
        un `ai_decisions.json` che arriva da fuori, perche' `--decisions` accetta
        qualunque percorso — e per il confronto con la volta prima, che sta
        invece in `_post_check` ed e' quello che segnala davvero qualcosa.
        """

        decisioni = self._leggi_artefatto(corsa, corsa.decisioni_ai_path)
        righe = decisioni.get("decisions") if isinstance(decisioni, dict) else decisioni
        if not isinstance(righe, list):
            return
        atteso = {
            "ai_modello": corsa.rapporto_ai.get("model"),
            "ai_versione_prompt": corsa.rapporto_ai.get("versione_prompt"),
            "ai_versione_avversario": corsa.rapporto_ai.get("versione_avversario"),
        }
        corsa.provenienza = {chiave: valore for chiave, valore in atteso.items() if valore is not None}
        diverse: dict[str, int] = {}
        for riga in righe:
            if not isinstance(riga, dict):
                continue
            for campo, valore in atteso.items():
                if valore is None or riga.get(campo) == valore:
                    continue
                diverse[campo] = diverse.get(campo, 0) + 1
        if diverse:
            self._avvisa(
                "DECISIONI_DI_UNA_CONFIGURAZIONE_DIVERSA",
                "Decisioni prese con una configurazione diversa",
                "Alcune corrispondenze entrate nel confronto non dichiarano il modello o il "
                "prompt con cui la fase AI di oggi ha lavorato: "
                + ", ".join(f"{campo} {quante}" for campo, quante in sorted(diverse.items()))
                + ". Vanno guardate prima di mandare l'ordine.",
            )

    # -- 8. COSTRUZIONE ----------------------------------------------------

    def _fase_costruzione(self, corsa: "_Corsa") -> None:
        fase = "COSTRUZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        corsa.review_path = corsa.cartella / "review_data.json"
        self._prima_del_passo(corsa, [corsa.review_path])
        # `--displays`, `--audit` e `--manifest` restano facoltativi nello
        # script; qui non lo sono: la loro assenza toglierebbe prodotti dalla
        # pagina senza che niente lo dica, e l'orchestratore li ha tutti e tre.
        self._esegui(
            corsa,
            fase,
            [
                "--resolved", str(corsa.risolti_path),
                "--manifest", str(corsa.manifest_path),
                "--audit", str(corsa.dati_dir / "audit.json"),
                "--displays", str(corsa.dati_dir / "display_offers.json"),
                "--threshold", str(self.configurazione.soglia_ordine),
                "--run-id", corsa.cartella.name,
                "--output", str(corsa.review_path),
            ],
            script="build_review_data.py",
        )
        self._dopo_il_passo(corsa, fase, [corsa.review_path])
        confronto = self._leggi_artefatto(corsa, corsa.review_path)
        if not isinstance(confronto, dict):
            raise Fermata(
                codice="CONFRONTO_ILLEGGIBILE",
                messaggio="Il confronto costruito non è leggibile.",
            )
        corsa.confronto = confronto
        self._impara_schemi_confermati(corsa, fase)
        self._numeri(
            prodotti=len(confronto.get("products") or []),
            fornitoriNelConfronto=len(confronto.get("suppliers") or []),
        )
        self._avvisa_chi_non_si_compila(corsa)
        self._post_check(corsa)
        self._segna_fase(
            fase, COMPLETATO,
            f"{len(confronto.get('products') or [])} prodotti",
            time.monotonic() - inizio,
        )

    def _impara_schemi_confermati(self, corsa: "_Corsa", fase: str) -> None:
        """Memorizza le mappature approvate e toglie il ponte temporaneo.

        Si arriva qui solo dopo che manifest, parser e confronto hanno usato
        davvero le colonne scelte. Il registro non impara quindi da una
        semplice anteprima, ma da una run che ha gia' superato tutti i
        controlli precedenti all'attivazione.

        ⚠ **Un apprendimento che non riesce non annulla il confronto.** Fino al
        21 agosto 2026 lo annullava, e costava una giornata: su un listino
        senza riga di intestazione la mappatura guidata dichiara le colonne per
        NUMERO (`schema_mapping.specifica_colonna`), `impara_adattatore`
        rifiuta di ricavarne un'impronta per intestazioni, e la fermata buttava
        via un confronto gia' costruito e gia' buono. Riprovare non serviva a
        niente — la decisione a mano resta al suo posto e la run rifa' la
        stessa strada — e «riapri la configurazione» era un consiglio
        impossibile: la pagina offre la mappatura guidata solo dopo
        `SCHEMA_SCONOSCIUTO` (`app.js::schemaMappingRequired`). Quello che il
        registro non impara riguarda la **prossima** settimana, non il
        confronto di adesso: si dice e si va avanti.
        """

        if not corsa.decisioni_da_imparare:
            return
        # ⚠ Nessun guasto di questo passo puo' buttare via il confronto. Il
        # rifiuto pulito — `impara_adattatore` che esce 2 dicendo perche' — era
        # gia' un avviso; tutto il resto usciva 1 e faceva alzare a `_esegui`
        # una Fermata, cioe' esattamente la cosa che questo commit toglie. Ed e'
        # roba che capita su un PC vero: la cartella `app/data/` in sola
        # lettura, il registro tenuto aperto da un antivirus o da OneDrive
        # mentre lo si legge. Il confronto e' gia' costruito e gia' valido: se
        # ne esce un guasto, si dice e si va avanti.
        #
        # L'unica eccezione e' `REGISTRO_ADATTATORI_ROVINATO`, che non riguarda
        # il confronto ma i fornitori imparati su questo computer, e passa.
        try:
            self._prova_a_imparare(corsa, fase)
        except Fermata:
            raise
        except Exception as exc:  # noqa: BLE001 - una memoria non annulla un confronto
            motivo = registro.motivo_registro_illeggibile(self.configurazione.adapters_path)
            if motivo:
                raise Fermata(
                    codice="REGISTRO_ADATTATORI_ROVINATO",
                    messaggio=(
                        "Il registro degli adattatori imparati su questo computer non si legge "
                        "più: da adesso valgono solo gli adattatori spediti col programma, "
                        "quindi i fornitori imparati qui possono tornare sconosciuti e per loro "
                        "non nascerebbe nessuna copia d'ordine. Il confronto precedente è "
                        "rimasto attivo e non è stato toccato. Chiudi e riapri il comparatore; "
                        "se si ferma di nuovo qui, "
                        f"«{registro.percorso_imparato(self.configurazione.adapters_path).name}» "
                        "va riparato o eliminato — eliminarlo fa ricominciare l'apprendimento "
                        "da zero e non rompe nient'altro."
                    ),
                    documenti=sorted(corsa.decisioni_da_imparare),
                    dettaglio=motivo,
                ) from exc
            self._avvisa_schemi_non_memorizzati(
                corsa,
                {nome.casefold() for nome in corsa.decisioni_da_imparare},
                {nome.casefold(): f"{type(exc).__name__}: {exc}"
                 for nome in corsa.decisioni_da_imparare},
            )

    def _prova_a_imparare(self, corsa: "_Corsa", fase: str) -> None:
        """I due passi di `impara_adattatore`: la simulazione e la scrittura."""

        prova = corsa.cartella / "adattatori_da_imparare.json"
        rapporto = corsa.cartella / "adattatori_imparati.json"
        self._prima_del_passo(corsa, [prova, rapporto])
        # Il primo giro e' una simulazione, e non e' una formalita': scrive
        # dentro una cartella temporanea sua e il registro vero non lo tocca
        # nemmeno quando riesce. Fallire qui non puo' aver rovinato niente, ed
        # e' per questo che qui non si ferma niente.
        self._esegui(
            corsa,
            fase,
            [
                "--manifest", str(corsa.manifest_path),
                "--adapters", str(self.configurazione.adapters_path),
                "--output", str(prova),
                "--prova",
            ],
            script="impara_adattatore.py",
            uscite_ammesse=(0, 2),
        )
        self._dopo_il_passo(corsa, fase, [prova])
        documento_prova = self._leggi_artefatto(corsa, prova) or {}
        attesi = {nome.casefold() for nome in corsa.decisioni_da_imparare}
        imparabili = self._nomi_del_rapporto(documento_prova, "imparati") & attesi
        non_memorizzabili = attesi - imparabili
        if non_memorizzabili:
            self._avvisa_schemi_non_memorizzati(
                corsa,
                non_memorizzabili,
                self._motivi_del_rapporto(documento_prova, non_memorizzabili),
            )
        if not imparabili:
            # Nessuno da scrivere: il passo vero non parte, e il registro non
            # viene aperto in scrittura per niente.
            return

        # ⚠ Il secondo giro scrive davvero, e l'unica cosa che puo' lasciare
        # peggio di prima e' il registro. Si guarda prima e dopo, invece di
        # dedurlo dal codice d'uscita.
        motivo_prima = registro.motivo_registro_illeggibile(self.configurazione.adapters_path)
        self._esegui(
            corsa,
            fase,
            [
                "--manifest", str(corsa.manifest_path),
                "--adapters", str(self.configurazione.adapters_path),
                "--output", str(rapporto),
            ],
            script="impara_adattatore.py",
            uscite_ammesse=(0, 2),
        )
        self._dopo_il_passo(corsa, fase, [rapporto])
        documento = self._leggi_artefatto(corsa, rapporto) or {}
        imparati = self._nomi_del_rapporto(documento, "imparati") & attesi
        motivo_dopo = registro.motivo_registro_illeggibile(self.configurazione.adapters_path)
        if motivo_dopo and not motivo_prima:
            imparato = registro.percorso_imparato(self.configurazione.adapters_path)
            raise Fermata(
                codice="REGISTRO_ADATTATORI_ROVINATO",
                messaggio=(
                    "Ho memorizzato le colonne, e subito dopo il registro degli adattatori "
                    "imparati su questo computer non si è più letto: da adesso valgono solo "
                    "gli adattatori spediti col programma, quindi i fornitori imparati qui "
                    "possono tornare sconosciuti e per loro non nascerebbe nessuna copia "
                    "d'ordine. Il confronto precedente è rimasto attivo e non è stato "
                    "toccato. Chiudi e riapri il comparatore; se si ferma di nuovo qui, "
                    f"«{imparato.name}» va riparato o eliminato — eliminarlo fa ricominciare "
                    "l'apprendimento da zero e non rompe nient'altro."
                ),
                documenti=sorted(attesi),
                dettaglio=motivo_dopo,
            )
        rifiutati = imparabili - imparati
        if rifiutati:
            self._avvisa_schemi_non_memorizzati(
                corsa, rifiutati, self._motivi_del_rapporto(documento, rifiutati),
            )
        # ⚠ Il ponte a mano si toglie **solo** per i documenti che il registro
        # ha imparato davvero. Per gli altri quella decisione e' l'unica cosa
        # che li rende ancora leggibili, e toglierla qui vorrebbe dire un
        # listino che la settimana prossima non si apre piu'.
        self._rimuovi_decisioni_imparate(imparati)

    @staticmethod
    def _nomi_del_rapporto(documento: Mapping[str, Any], chiave: str) -> set[str]:
        """I nomi di file elencati sotto una chiave del rapporto, ripiegati."""

        return {
            str(voce.get("file") or "").casefold()
            for voce in (documento.get(chiave) or [])
            if isinstance(voce, dict)
        }

    @staticmethod
    def _motivi_del_rapporto(documento: Mapping[str, Any], nomi: set[str]) -> dict[str, str]:
        """Il motivo che `impara_adattatore` ha scritto, per nome di file.

        E' gia' una frase in italiano scritta per essere letta: qui non si
        riscrive, si porta all'utente.
        """

        motivi: dict[str, str] = {}
        for voce in (documento.get("saltati") or []):
            if not isinstance(voce, dict):
                continue
            nome = str(voce.get("file") or "").casefold()
            if nome in nomi and nome not in motivi:
                motivi[nome] = str(voce.get("motivo") or "").strip()
        return motivi

    def _avvisa_schemi_non_memorizzati(
        self,
        corsa: "_Corsa",
        nomi: set[str],
        motivi: Mapping[str, str],
    ) -> None:
        """Un documento che il registro non ha imparato si dice, uno per uno.

        Uno per documento e non un avviso solo con l'elenco dentro: il nome del
        file e' l'unica cosa che lega la frase alla scheda che l'utente ha
        appena configurato. E la prima cosa che dice e' che il confronto c'e',
        perche' e' la domanda che si fa chi legge: «ho perso il lavoro?».
        """

        per_nome = {nome.casefold(): nome for nome in corsa.decisioni_da_imparare}
        for chiave in sorted(nomi):
            nome = per_nome.get(chiave, chiave)
            motivo = (motivi.get(chiave) or "").removeprefix("Rifiutato:").strip()
            altro = self._fornitore_che_se_lo_prende(motivo)
            if altro:
                # ⚠ Questo rifiuto non vuol dire «non l'ho memorizzato». Vuol
                # dire che con il registro di adesso quel documento **e' di un
                # altro fornitore**: la settimana prossima il ricalcolo non si
                # fermera' e non chiedera' niente, lo leggera' come listino di
                # quell'altro. Dirgli «non c'e' niente da fare» era la frase
                # peggiore possibile, perche' non c'e' niente da fare **oggi** e
                # tutto da fare prima del prossimo lunedi'.
                self._avvisa(
                    "SCHEMA_NON_MEMORIZZATO",
                    f"«{nome}»: da adesso lo leggo come il listino di un altro fornitore",
                    f"Il confronto di adesso è giusto e «{nome}» ci sta dentro con le colonne "
                    f"che hai indicato. Ma il registro non ha potuto tenerlo separato: con lo "
                    f"schema di oggi quel documento combacia con «{altro}», e al prossimo "
                    "confronto verrebbe letto come il suo listino, senza chiedere niente. "
                    "I due sono troppo simili perché li distingua da solo: "
                    "prima del prossimo confronto vanno tenuti separati a mano — carica un "
                    "documento per volta, oppure chiedi al fornitore un listino riconoscibile."
                    + (f" Dettaglio: {motivo}" if motivo else ""),
                    severita="error",
                    documento=nome,
                    motivo=motivo,
                )
                continue
            self._avvisa(
                "SCHEMA_NON_MEMORIZZATO",
                f"«{nome}»: colonne usate adesso, non memorizzate",
                f"Il confronto è aggiornato e «{nome}» ci sta dentro con le colonne che hai "
                "indicato: quel lavoro non è andato perso. Quello che non ho potuto fare è "
                "ricordarmele per i prossimi listini di questo fornitore, quindi la prossima "
                "volta il confronto si fermerà di nuovo sulle colonne e te le richiederà. "
                "Adesso non c'è niente da fare."
                + (f" Motivo: {motivo}" if motivo else ""),
                documento=nome,
                motivo=motivo,
            )

    @staticmethod
    def _fornitore_che_se_lo_prende(motivo: str) -> str:
        """L'adattatore che si e' preso il documento, quando il rifiuto lo dice.

        `impara_adattatore` rifiuta con «con l'adattatore appena scritto il
        documento risulta SCHEMA_NOTO «X» invece di SCHEMA_NOTO «Y»» quando due
        fornitori esportano con lo stesso modello. E' l'unico rifiuto che
        cambia che cosa succede la **settimana prossima**, e va detto in un
        altro modo.
        """

        trovato = re.search(
            r"il documento risulta \S+ «([^»]+)» invece di", str(motivo or ""),
        )
        if not trovato:
            return ""
        identificativo = trovato.group(1).strip()
        if not identificativo or identificativo.casefold() == "none":
            return ""
        return registro.nome_del_fornitore(
            str(registro.adattatore(identificativo).get("supplier_id") or identificativo),
        ) or identificativo

    def _rimuovi_decisioni_imparate(self, nomi: set[str]) -> None:
        percorso = self.configurazione.decisioni_manuali_path
        documento = leggi_json(percorso, None)
        voci = documento.get("decisions") if isinstance(documento, dict) else documento
        if not isinstance(voci, list):
            return
        tenute = [
            voce for voce in voci
            if not isinstance(voce, dict)
            or str(voce.get("file_name") or "").casefold() not in nomi
        ]
        if tenute:
            scrivi_json(percorso, {"decisions": tenute})
            return
        try:
            percorso.unlink()
        except FileNotFoundError:
            pass

    def _avvisa_chi_non_si_compila(self, corsa: "_Corsa") -> None:
        """Un fornitore del confronto per cui non nascera' nessuna copia si dice.

        La compilabilita' e' dichiarata dal registro (`order_write`
        nell'adattatore).  Un fornitore che non ce l'ha resta nel confronto,
        puo' vincere e puo' prendersi meta' ordine — ma alla fine, dove ci si
        aspetta il suo listino compilato, non c'e' niente.  Misurato il 12
        agosto 2026: 102 prodotti assegnati a ACERO e avvertimenti vuoti.

        L'avviso arriva qui e non alla compilazione perche' qui e' ancora
        possibile cambiare idea: dopo, la merce e' gia' stata assegnata.
        """

        motivo = registro.motivo_registro_illeggibile(self.configurazione.adapters_path)
        if motivo:
            # Con il registro rotto OGNI fornitore risulterebbe «non
            # dichiarato», e l'avviso manderebbe a cercare una dichiarazione
            # dentro un file che non si apre.  La causa e' una sola e si dice
            # una volta (revisione avversariale del 13 agosto 2026).
            self._avvisa(
                "REGISTRO_ILLEGGIBILE",
                "Il registro degli adattatori non si legge",
                "Nessun fornitore risulta compilabile e nessuna copia d'ordine "
                "nascerà finché il registro non si ripara. " + motivo,
                severita="error",
            )
            return
        compilabili = registro.fornitori_con_scrittura(self.configurazione.adapters_path)
        senza = [
            nome for nome in (
                str((voce or {}).get("id") or "").strip().casefold()
                for voce in (corsa.confronto.get("suppliers") or [])
                if isinstance(voce, dict)
            )
            if nome and nome not in compilabili
        ]
        for nome in sorted(dict.fromkeys(senza)):
            self._avvisa(
                "FORNITORE_NON_COMPILABILE",
                f"{nome.upper()}: nessuna copia d'ordine da mandare",
                f"Il listino {nome.upper()} è nel confronto e può vincere, ma il registro non "
                "dichiara come si scrive l'ordine dentro il suo documento: per questo fornitore "
                "non verrà creata nessuna copia da mandare, e la merce che gli assegni resta "
                "da ordinare a mano.",
                fornitore=nome,
            )
        # La seconda famiglia: fornitori DICHIARATI dal registro che il
        # documento di questa settimana non attiva — un'intestazione cambiata,
        # un foglio sparito, righe fuori dal documento.  La causa la sa il
        # lanciatore; senza questo giro moriva dentro il messaggio di
        # `prepare_writer_config`, che il server butta via quando la scrittura
        # riesce, e la si scopriva alla compilazione con una frase che non
        # diceva la causa (revisione avversariale del 13 agosto 2026).
        # ⚠ Due funzioni, non una, e la seconda esiste per un buco misurato il
        # 14 agosto 2026.  `fornitori_senza_copia` parte dall'elenco dei
        # DOCUMENTI del confronto; `fornitori_ordinati_senza_copia` parte dai
        # FORNITORI che il confronto usa davvero.  Le due liste divergono
        # appena un listino viene eliminato o sostituito, e in quel caso la
        # prima tace: i suoi avvisi nascono dentro il ciclo sui documenti
        # risolti, quindi zero documenti risolti significa zero avvisi.  Chi
        # deve dire «per questo fornitore non nascera' nessuna copia» deve
        # guardare da tutte e due le parti.
        from launcher import (  # import tardivo, come fa il server
            fornitori_del_confronto,
            fornitori_ordinati_senza_copia,
            fornitori_senza_copia,
        )

        avvisati = set(senza)
        non_attivabili: dict[str, str] = {}
        dai_documenti = fornitori_senza_copia(
            corsa.confronto, self.configurazione.adapters_path
        )
        dalle_offerte = fornitori_ordinati_senza_copia(
            corsa.confronto,
            fornitori_del_confronto(corsa.confronto),
            self.configurazione.adapters_path,
        )
        for nome, frase in sorted({**dalle_offerte, **dai_documenti}.items()):
            if nome in avvisati:
                continue
            non_attivabili[nome] = frase
            self._avvisa(
                "FORNITORE_NON_COMPILABILE",
                f"{nome.upper()}: nessuna copia d'ordine da mandare",
                frase.rstrip(".")
                + ". Per questo fornitore non verrà creata nessuna copia da mandare, "
                "e la merce che gli assegni resta da ordinare a mano.",
                fornitore=nome,
            )
        tutti = sorted(avvisati | set(non_attivabili))
        if tutti:
            self._numeri(fornitoriSenzaCompilazione=tutti)

    # -- il controllo che avvisa e non ferma -------------------------------

    def _post_check(self, corsa: "_Corsa") -> None:
        """Il confronto con la run precedente.  La sua uscita e' un avviso.

        Deciso da Daniele il 12 agosto 2026: quando i conteggi di un listino
        cambiano molto rispetto alla volta prima la run **va fino in fondo** e
        lo dice.  Niente soglie che bloccano: una soglia sbagliata fermerebbe
        ogni settimana una run buona, e questo programma gira da solo.
        """

        precedente = self._esecuzione_precedente(corsa.cartella)
        if precedente is None:
            self._numeri(confrontoConLaVoltaPrima="prima esecuzione")
            return
        vecchio = leggi_json(precedente / "dati" / "audit.json", None)
        if not isinstance(vecchio, dict):
            self._numeri(confrontoConLaVoltaPrima="audit precedente non leggibile")
            return

        vecchie_fonti = vecchio.get("sources") or {}
        nuove_fonti = corsa.audit.get("sources") or {}
        for nome in sorted(set(vecchie_fonti) | set(nuove_fonti)):
            prima = int((vecchie_fonti.get(nome) or {}).get("rows") or 0)
            adesso = int((nuove_fonti.get(nome) or {}).get("rows") or 0)
            if prima and not adesso:
                self._avvisa(
                    "FORNITORE_SPARITO",
                    f"{nome.upper()} non è più nel confronto",
                    f"La volta prima aveva {prima} righe e oggi non c'è.",
                )
                continue
            if adesso and not prima:
                self._avvisa(
                    "FORNITORE_NUOVO",
                    f"{nome.upper()} entra nel confronto",
                    f"Non c'era nella volta prima e oggi porta {adesso} righe.",
                )
                continue
            if prima and abs(adesso - prima) / prima > SCARTO_RIGHE_DA_SEGNALARE:
                self._avvisa(
                    "RIGHE_CAMBIATE_MOLTO",
                    f"Il listino {nome.upper()} è cambiato molto",
                    f"Da {prima} a {adesso} righe ordinabili "
                    f"({(adesso - prima) * 100 / prima:+.0f}%). Il confronto è pronto lo stesso.",
                )

        vecchi_prezzi = vecchio.get("price_summary") or {}
        nuovi_prezzi = corsa.audit.get("price_summary") or {}
        for nome in sorted(set(vecchi_prezzi) & set(nuovi_prezzi)):
            prima = _numero((vecchi_prezzi.get(nome) or {}).get("median"))
            adesso = _numero((nuovi_prezzi.get(nome) or {}).get("median"))
            # Il crollo a nulla — mediana assente o zero — non passa di qui:
            # lo dice gia' `_controlla_i_prezzi`, che non ha bisogno della
            # volta prima e vale anche alla prima run.  Questo confronto serve
            # ai cambiamenti di scala fra due settimane, e per quelli uno zero
            # come termine di paragone non direbbe niente.
            if not prima or not adesso:
                continue
            if abs(adesso - prima) / prima > SCARTO_PREZZO_DA_SEGNALARE:
                self._avvisa(
                    "PREZZI_CAMBIATI_IN_BLOCCO",
                    f"I prezzi {nome.upper()} sono cambiati in blocco",
                    f"Il prezzo mediano al pezzo passa da {prima:.4f} a {adesso:.4f} "
                    f"({(adesso - prima) * 100 / prima:+.0f}%). Vale la pena guardarlo prima "
                    "di mandare l'ordine.",
                )

        # Il modello e il prompt della volta prima: e' qui che il confronto
        # sulla provenienza trova davvero qualcosa.  Un ordine costruito con un
        # prompt diverso da quello di sette giorni fa non e' sbagliato, ma e'
        # l'unica spiegazione possibile di un confronto che cambia senza che i
        # listini siano cambiati — e senza questa riga la si cercherebbe altrove.
        vecchio_rapporto = leggi_json(precedente / "dati" / "ai_rapporto.json", None)
        if isinstance(vecchio_rapporto, dict):
            cambiate = [
                f"{etichetta}: da «{vecchio_rapporto.get(chiave)}» a «{corsa.rapporto_ai.get(chiave)}»"
                for chiave, etichetta in (
                    ("model", "modello"),
                    ("versione_prompt", "prompt"),
                    ("versione_avversario", "verifica avversariale"),
                )
                if vecchio_rapporto.get(chiave) not in (None, corsa.rapporto_ai.get(chiave))
            ]
            if cambiate:
                self._avvisa(
                    "CONFIGURAZIONE_AI_CAMBIATA",
                    "La valutazione automatica gira con una configurazione diversa",
                    "Rispetto alla volta prima è cambiato " + "; ".join(cambiate)
                    + ". Le corrispondenze proposte possono essere diverse anche a listini "
                    "identici.",
                )
        self._numeri(confrontoConLaVoltaPrima=precedente.name)

    def _ripulisci_le_esecuzioni(self, corsa: Path) -> int:
        """Toglie le cartelle di lavoro piu' vecchie, e dice quante ne ha tolte.

        ⚠ Due cartelle non si toccano mai, e non perche' sono recenti: quella
        della run che sta partendo, e quella del **confronto vivo**.  La seconda
        e' quella che `colonne_dei_documenti` riapre per dire quali colonne ha
        letto in ogni documento, e che la mappatura guidata riapre quando la
        catena si e' fermata: cancellarla lascerebbe la pagina a rispondere «la
        cartella di quel confronto non c'e' piu'» su un confronto che si sta
        guardando in quel momento.

        ⚠ E non solleva mai.  Fare spazio e' una comodita': una cartella che non
        si cancella — antivirus, file aperto, permessi — non deve poter fermare
        il ricalcolo che l'utente ha appena chiesto.
        """

        radice = self.configurazione.esecuzioni_dir
        intoccabili = {corsa.name}
        vivo = leggi_json(self.configurazione.review_path, None)
        run_vivo = (vivo if isinstance(vivo, dict) else {}).get("run")
        dichiarata = str((run_vivo if isinstance(run_vivo, dict) else {}).get("pipelineRunId") or "")
        if dichiarata:
            intoccabili.add(dichiarata)
        try:
            with os.scandir(radice) as scansione:
                nomi = sorted(
                    voce.name for voce in scansione
                    if voce.is_dir() and not voce.name.startswith(".")
                )
        except OSError:
            return 0
        # I nomi portano la data, quindi l'ordine alfabetico e' gia' l'ordine
        # del tempo: e' la stessa regola delle copie delle memorie.
        da_tenere = set(nomi[-ESECUZIONI_DA_TENERE:]) | intoccabili
        tolte = 0
        for nome in nomi:
            if nome in da_tenere:
                continue
            cartella = consegna.cartella_sicura(radice, nome)
            if cartella is None:
                continue
            try:
                shutil.rmtree(cartella)
            except OSError:
                continue
            tolte += 1
        return tolte

    def _esecuzione_precedente(self, corsa_corrente: Path) -> Path | None:
        """L'ultima run **completa** prima di questa, o `None`.

        Si scandisce il disco invece di tenere un indice: un indice a parte e'
        una cosa in piu' che puo' disallinearsi da quello che c'e' davvero.

        ⚠ «Completa» e' il punto.  `dati/audit.json` lo scrive la fase di
        preparazione, a meta' catena; `esecuzione.json` lo scrive il `finally`
        della run, alla fine.  Una run uccisa in mezzo — il computer si spegne,
        il processo viene chiuso — lascia il primo e non il secondo, e prenderla
        come termine di paragone significa confrontarsi con un confronto che non
        e' mai stato attivato: misurato, bastava a spegnere `FORNITORE_SPARITO`
        e a far uscire un fornitore intero dal confronto senza una parola.  Si
        guarda quindi il verbale, e si pretende che dichiari una run arrivata in
        fondo: una run ferma a meta' non ha aggiornato niente, e la volta prima
        vera resta quella di prima ancora.

        ⚠ Ma prima del verbale parla il confronto vivo: `review_data.json`
        dichiara chi l'ha attivato (`run.pipelineRunId`), e quella cartella E'
        la volta prima per definizione — anche quando la run e' morta un attimo
        dopo `os.replace`, senza fare in tempo a scrivere `COMPLETATO` nel
        proprio verbale.  Pretendere il verbale anche da lei significava farla
        sparire dal paragone e rispedire il confronto alla «prima esecuzione»,
        con `FORNITORE_SPARITO` di nuovo muto (revisione avversariale del 13
        agosto 2026).  La scansione del disco resta come ripiego, per i
        confronti attivati prima che il campo esistesse.
        """

        radice = self.configurazione.esecuzioni_dir

        vivo = leggi_json(self.configurazione.review_path, None)
        run_vivo = (vivo if isinstance(vivo, dict) else {}).get("run")
        dichiarata = str((run_vivo if isinstance(run_vivo, dict) else {}).get("pipelineRunId") or "")
        if dichiarata and dichiarata != corsa_corrente.name:
            attivata = consegna.cartella_sicura(radice, dichiarata)
            if attivata is not None and (attivata / "dati" / "audit.json").is_file():
                return attivata

        candidate: list[tuple[float, Path]] = []
        try:
            with os.scandir(radice) as scansione:
                nomi = [voce.name for voce in scansione if voce.is_dir() and not voce.name.startswith(".")]
        except OSError:
            return None
        for nome in nomi:
            cartella = consegna.cartella_sicura(radice, nome)
            if cartella is None or cartella == corsa_corrente:
                continue
            if not (cartella / "dati" / "audit.json").is_file():
                continue
            audit = leggi_json(cartella / NOME_AUDIT_ESECUZIONE, None)
            if not isinstance(audit, dict) or audit.get("stato") != COMPLETATO:
                continue
            # Il momento si prende dal verbale e da nessun'altra parte: l'ora
            # del file e' l'ora dell'ultima scrittura, che una copia, un
            # antivirus o un backup spostano in avanti quanto vogliono.  Un
            # verbale che non sa dire quando e' cominciato non e' un termine di
            # paragone: si salta, come le altre run che non si lasciano leggere.
            if not isinstance(audit.get("iniziatoIl"), str):
                continue
            try:
                istante = datetime.fromisoformat(audit["iniziatoIl"])
            except ValueError:
                continue
            if istante.tzinfo is None:
                # `avvia()` scrive sempre l'ora con il fuso: un verbale senza
                # e' stato scritto da qualcun altro, e `timestamp()` su un'ora
                # nuda usa l'ora locale — due convenzioni mischiate possono
                # invertire l'ordine delle run di ore intere.
                continue
            try:
                quando = istante.timestamp()
            except (OSError, OverflowError):
                continue
            candidate.append((quando, cartella))
        if not candidate:
            return None
        candidate.sort(key=lambda voce: (voce[0], voce[1].name))
        return candidate[-1][1]

    # -- 9. ATTIVAZIONE ----------------------------------------------------

    def _fase_attivazione(self, corsa: "_Corsa") -> None:
        """L'unico passo che tocca il confronto vivo, e ha delle precondizioni.

        Sono numeri, non impressioni: un confronto senza prodotti o senza
        fornitori non e' un confronto, ed e' esattamente la forma che prende un
        guasto a monte quando nessuno lo guarda.  Una run che non le rispetta
        non attiva niente e lo dice, e il confronto di prima resta intatto.
        """

        fase = "ATTIVAZIONE"
        self._segna_fase(fase, IN_CORSO)
        inizio = time.monotonic()
        confronto = corsa.confronto
        prodotti = confronto.get("products") or []
        fornitori = confronto.get("suppliers") or []
        motivi: list[str] = []
        if not prodotti:
            motivi.append("il confronto non contiene nessun prodotto")
        if not fornitori:
            motivi.append("il confronto non contiene nessun fornitore")
        ricevuti = corsa.rapporto_ai.get("casi_ricevuti")
        if isinstance(ricevuti, int) and ricevuti != corsa.numero_shortlist:
            motivi.append(
                f"la fase AI dichiara {ricevuti} casi ricevuti e i candidati prodotti sono "
                f"{corsa.numero_shortlist}"
            )
        senza_shortlist = (corsa.riepiloghi.get("risoluzione") or {}).get("coppie_senza_shortlist")
        if isinstance(senza_shortlist, int) and senza_shortlist:
            motivi.append(f"{senza_shortlist} coppie semantiche non hanno mai avuto candidati")
        if motivi:
            raise Fermata(
                codice="ATTIVAZIONE_RIFIUTATA",
                messaggio=(
                    "Il nuovo confronto non è stato attivato perché "
                    + "; ".join(motivi)
                    + ". Quello di prima è rimasto al suo posto."
                ),
            )

        # ⚠ Tutto quello che deve accompagnare il confronto nuovo si fa **prima**
        # che il confronto nuovo diventi quello vivo.  Prima era il contrario, e
        # quell'ordine aveva due conseguenze misurate: gli avvisi nati dopo la
        # sostituzione non entravano mai nel documento (ci entra la fotografia
        # scattata qui sotto), e un processo che moriva nella finestra fra
        # `os.replace` e la riconfigurazione lasciava vivo il confronto di adesso
        # con la configurazione di scrittura della settimana scorsa — cioe' con
        # **il listino della settimana scorsa** e i numeri di riga di adesso.
        # Nell'ordine giusto quella finestra si chiude dalla parte sicura: se si
        # muore qui, il confronto vivo e' ancora quello di prima.
        if self.su_confronto_attivato is not None:
            try:
                self.su_confronto_attivato(deepcopy(confronto))
            except Exception as exc:
                # La compilazione si riconfigura prima dell'attivazione: se non
                # ci riesce il confronto e' buono lo stesso e viene attivato, ma
                # va detto — altrimenti la compilazione userebbe in silenzio il
                # listino della volta prima.  Non e' l'ultima difesa: alla
                # compilazione il `run_id` della configurazione deve combaciare
                # con quello del confronto attivo, e se non combacia si ferma.
                self._avvisa(
                    "COMPILAZIONE_DA_RICONFIGURARE",
                    "Le copie dei listini non sono state riconfigurate",
                    "Il confronto è aggiornato. La compilazione va ricontrollata prima di "
                    f"creare le copie. Dettaglio: {type(exc).__name__}: {exc}",
                )

        # ⚠ Da qui alla sostituzione del confronto vivo si tiene il lucchetto
        # delle rotte: e' l'unico tratto in cui la catena scrive dati che il
        # servizio sta servendo.  Senza, un salvataggio della pagina arrivato
        # in mezzo scriveva `state.json` mentre `_ripulisci_stato` lo stava
        # riscrivendo — e la pagina si ritrovava con lo stato di prima e il
        # confronto nuovo.  Il tratto e' breve di proposito: la
        # riconfigurazione della scrittura, che chiama Node, resta fuori.
        with self.lucchetto_dati:
            # ⚠ I byte dello stato **prima** della ripulitura.  Qui sotto
            # `_ripulisci_stato` scrive `state.json` — azzera, scollega,
            # riprende le quantita' dall'elenco — e il confronto nuovo diventa
            # vivo solo in fondo: se quella sostituzione non riesce, restano in
            # pagina il confronto di prima e le decisioni gia' tolte, cioe'
            # quantita' azzerate che nulla di quel che si vede spiega.  Su
            # Windows succede per davvero: `os.replace` fallisce mentre un
            # antivirus, un backup o OneDrive tengono aperto `review_data.json`.
            # O cambiano tutti e due, o non cambia nessuno dei due (6 settembre
            # 2026).
            try:
                stato_prima = self.configurazione.state_path.read_bytes()
            except OSError:
                # Se non si riesce nemmeno a leggerlo non c'e' niente da
                # rimettere: si prosegue come prima di questa difesa.
                stato_prima = None
            try:
                rimossi, conferme_scadute, riprese, scollegate = self._ripulisci_stato(confronto)
                # ⚠ Le quantita' riprese dall'elenco NON producono un avviso, e non e'
                # una dimenticanza: e' la regola normale del programma — la quantita'
                # la decide il gestionale — e un avviso che compare a ogni ricalcolo
                # per dire che il programma ha funzionato e' rumore.  Deciso il 15
                # agosto 2026, con le sue parole: «non riempire tutto di notifiche,
                # popup, e roba da paranoici logorroici».  Il numero resta nel
                # riepilogo della run per chi lo cerca.
                self._numeri(quantitaRiprese=riprese)
                if scollegate:
                    plurale = scollegate != 1
                    self._avvisa(
                        "DECISIONI_SCOLLEGATE",
                        "Alcune righe portano ora un altro prodotto",
                        f"{scollegate} {'righe dell’elenco portano' if plurale else 'riga dell’elenco porta'} "
                        f"un articolo diverso da prima: fornitore scelto, conferma ed esclusione di "
                        f"{'quelle righe' if plurale else 'quella riga'} non valevano più e sono stati rifatti "
                        "da capo con il confronto nuovo.",
                    )
                if rimossi:
                    self._avvisa(
                        "SCELTE_NON_PIU_VALIDE",
                        "Alcune scelte precedenti sono state azzerate",
                        f"{rimossi} prodotti avevano una quantità su un'offerta che il listino nuovo "
                        "non ha più: la quantità è tornata a zero.",
                    )
                if conferme_scadute:
                    plurale = conferme_scadute != 1
                    self._avvisa(
                        "CONFERME_SCADUTE",
                        "Alcune conferme vanno rifatte",
                        f"{conferme_scadute} {'prodotti sono stati abbinati' if plurale else 'prodotto è stato abbinato'} "
                        "a una riga diversa del listino: la conferma che avevi dato valeva per "
                        "l'articolo di prima. Rileggi nome e codice e riconferma nel passo 2.",
                    )

                # Gli avvisi della run entrano nel documento: due minuti dopo nessuno
                # guardera' piu' la barra di avanzamento, e un avviso che vive solo
                # nello stato del job e' un avviso che nessuno legge.
                avvisi_run = [dict(voce) for voce in (self.stato().get("avvisi") or [])]
                if avvisi_run:
                    confronto.setdefault("warnings", [])
                    confronto["warnings"] = [*avvisi_run, *(confronto.get("warnings") or [])]
                confronto.setdefault("run", {})["pipelineRunId"] = corsa.cartella.name
                scrivi_json(corsa.review_path, confronto)
                corsa.registro_artefatti[self._chiave_artefatto(corsa, corsa.review_path)] = {
                    "fase": fase,
                    "sha256": impronta_file(corsa.review_path),
                    "byte": corsa.review_path.stat().st_size,
                    "scritto_il": utc_ora(),
                }

                vivo = self.configurazione.review_path
                # ⚠ Anche questa passa da `scrittura_sicura`, e non era cosi': era
                # l'unica sostituzione rimasta col temporaneo dal nome fisso e
                # senza `fsync`, proprio sul file piu' grosso e piu' importante del
                # programma — 2,4 MB sul confronto vero. Una mancanza di corrente
                # subito dopo lo lasciava presente e troncato, cioe' alla lettera
                # il difetto che `scrittura_sicura` esiste per chiudere.
                scrittura_sicura.scrivi_bytes(vivo, corsa.review_path.read_bytes())
            except Exception:
                if stato_prima is not None:
                    scrittura_sicura.scrivi_bytes(self.configurazione.state_path, stato_prima)
                raise

        corsa.messaggio_finale = (
            f"Confronto aggiornato: {len(prodotti)} prodotti, {len(fornitori)} fornitori."
        )
        self._segna_fase(
            fase, COMPLETATO, corsa.messaggio_finale, time.monotonic() - inizio
        )

    def _rifiuti_in_vigore(self) -> dict[tuple[str, str], dict[str, Any]]:
        """I «no» dati dall'utente, o niente se non si riescono a leggere.

        Non leggerli e' prudente per costruzione: l'offerta torna a contare come
        disponibile, la quantita' torna a zero e la riga aspetta una scelta —
        cioe' si torna a chiedere, non si ordina per conto proprio.
        """

        if self.rifiuti_dichiarati is None:
            return {}
        try:
            return self.rifiuti_dichiarati() or {}
        except Exception as exc:  # noqa: BLE001 - una memoria non ferma un ricalcolo
            self._numeri(rifiutiNonLetti=f"{type(exc).__name__}: {exc}")
            return {}

    @staticmethod
    def _e_rifiutata(
        rifiuti: dict[tuple[str, str], dict[str, Any]],
        articolo: str,
        fornitore: str,
        offerta: dict[str, Any],
    ) -> bool:
        """Stessa regola di `ReviewStore.spegni_le_offerte_rifiutate`, riga per riga."""

        if not rifiuti or not articolo:
            return False
        voce = rifiuti.get((str(fornitore or "").strip().casefold(), articolo))
        return voce is not None and str(voce.get("offerta") or "") == impronta_articolo(offerta)

    def _ripulisci_stato(self, confronto: dict[str, Any]) -> tuple[int, int, int, int]:
        """Le scelte che il confronto nuovo non regge piu' tornano a zero.

        Un prodotto sparito o un'offerta che non c'e' piu' lascerebbero nello
        stato una quantita' su un fornitore che non puo' consegnarla: al
        salvataggio successivo l'utente si vedrebbe rifiutare tutto con
        `OFFERTA_NON_VALIDA` senza capire quale riga sia.

        ⚠ Dal 16 agosto 2026 quel rifiuto non copre piu' tutti i casi, e questa
        funzione segue la stessa riga di confine. La domanda non e' «il
        fornitore salvato c'e' ancora», e' «qualcuno puo' servirlo»:

        * **almeno un fornitore ha ancora un'offerta utilizzabile** — su quale
          metterci la quantita' e' una scelta, e il programma non la fa al posto
          dell'utente: la quantita' torna a zero come sempre e la riga aspetta
          che lui scelga;
        * **nessun fornitore ce l'ha** — non c'e' niente da scegliere. Azzerare
          qui cancellava l'unica cosa nota di quella riga, quanti ne servono, e
          non evitava nessun errore: `validate_snapshot` accetta una quantita'
          senza nessuna offerta utilizzabile da nessuno. La quantita' resta e il
          fornitore si svuota: e' un prodotto da reperire, e alla compilazione
          entra nell'elenco «Prodotti da reperire».

        Restituisce (quantita' azzerate, conferme scadute, quantita' riprese
        dal gestionale, decisioni scollegate). ⚠ I prodotti da reperire non
        stanno in questo conto e non producono nessun avviso: non e' stato
        perso niente, e un avviso che dice che il programma ha funzionato e'
        rumore. Ci finivano dentro prima, ed era il conteggio a mentire —
        `SCELTE_NON_PIU_VALIDE` dice «la quantita' e' tornata a zero».

        Le conferme scadute erano il caso che mancava: il fornitore ha ancora
        un'offerta — quindi niente veniva azzerato — ma quell'offerta e' una
        RIGA DIVERSA del listino, con altro EAN e altra descrizione. La casella
        «confermo che e' lo stesso articolo» restava spuntata sull'articolo
        sbagliato, e la compilazione passava senza una parola (revisione del 14
        agosto 2026).

        ⚠ Le altre due misure nascono dal difetto del 15 agosto 2026, con le
        parole di chi lo ha subito: «ordine2 non veniva usato davvero: nonostante
        alcuni prodotti avessero quantita' diverse, mostrava tutti 0».  Il
        confronto nuovo era giusto — i colli del gestionale nuovo erano dentro
        `review_data.json` — ma `ReviewStore.review()` copre ogni prodotto con la
        decisione salvata, e la decisione salvata veniva dall'elenco di prima.
        Due regole, e sono due perche' rispondono a due domande diverse:

        * **una decisione vale per l'articolo su cui e' stata presa.**  Gli
          identificativi sono numeri di riga del gestionale: con un elenco nuovo
          la riga 3 e' un altro prodotto, e la scelta del fornitore, la conferma
          e l'esclusione della riga 3 di prima non parlano di lui.  Se l'articolo
          e' cambiato, la decisione si scollega e la riga riparte da quello che
          dice il confronto nuovo;
        * **una quantita' che viene dal gestionale non e' una decisione**: e' una
          copia di quello che c'e' scritto nell'elenco, e a ogni ricalcolo si
          rilegge da li'.  Senza condizioni — decisione di Daniele del 15 agosto
          2026: «la quantita' deve prenderla dal gestionale e stop».  Le
          quantita' scritte dall'utente (`quantitySource: "utente"`) restano
          intatte: quelle sono sue — tranne lo zero che «utente» non era mai
          stato, riconosciuto dall'elenco di allora (7 settembre 2026, nel
          ciclo qui sotto).  ⚠ Conseguenza voluta: «Azzera le quantita'
          predefinite» vale fino al ricalcolo successivo, perche' quello che
          azzera non e' una scelta salvata ma una copia dell'elenco.
        """

        stato = leggi_json(self.configurazione.state_path, None)
        if not isinstance(stato, dict) or not isinstance(stato.get("products"), list):
            return 0, 0, 0, 0
        # I prodotti aggiunti a mano non stanno nel confronto — ci arrivano
        # dallo stato, a ogni lettura — ma le loro decisioni sono decisioni come
        # tutte le altre. Senza questa riga sparivano a ogni ricalcolo, e la
        # quantita' scritta su un prodotto aggiunto a mano tornava a zero con la
        # spiegazione sbagliata («il listino nuovo non ha piu' l'offerta»).
        prodotti_del_confronto = [
            *(confronto.get("products") or []),
            *(voce for voce in stato.get("manualProducts") or [] if isinstance(voce, dict)),
        ]
        offerte_valide: dict[str, set[str]] = {}
        offerte_per_fornitore: dict[tuple[str, str], dict[str, Any]] = {}
        quantita_del_confronto: dict[str, int] = {}
        articolo_adesso: dict[str, str] = {}
        # ⚠ Le offerte che l'utente ha rifiutato non contano come «qualcuno lo
        # serve ancora». Il confronto qui e' quello NUDO — la decorazione che
        # spegne un'offerta rifiutata sta sulla porta di lettura del servizio, e
        # di qui non passa — quindi la domanda si rifa' con la stessa regola:
        # combacia il fornitore, combacia l'articolo del gestionale, e combacia
        # l'impronta della riga su cui il no e' stato detto.
        rifiuti = self._rifiuti_in_vigore()
        for prodotto in prodotti_del_confronto:
            identificativo_prodotto = str(prodotto.get("id") or "")
            disponibili = set()
            articolo_del_gestionale = impronta_prodotto(prodotto) if rifiuti else ""
            for offerta in prodotto.get("offers") or []:
                fornitore_offerta = offer_supplier_id(offerta)
                if offer_is_available(offerta) and not self._e_rifiutata(
                    rifiuti, articolo_del_gestionale, fornitore_offerta, offerta,
                ):
                    disponibili.add(fornitore_offerta)
                offerte_per_fornitore[(identificativo_prodotto, fornitore_offerta)] = offerta
            offerte_valide[identificativo_prodotto] = disponibili
            quantita_del_confronto[identificativo_prodotto] = int(_numero(prodotto.get("quantity")) or 0)
            articolo_adesso[identificativo_prodotto] = articolo_della_riga(prodotto)

        # Il confronto vivo sul disco e' ancora quello di prima: qui si guarda
        # che cosa c'era su ogni sua riga. Vale solo se lo stato appartiene
        # davvero a quella run: altrimenti di quelle righe non si sa niente, e
        # una decisione non si scollega per un sospetto.
        precedente = leggi_json(self.configurazione.review_path, None)
        if not isinstance(precedente, dict) or str(stato.get("runId") or "") != str(
            (precedente.get("run") or {}).get("id") or ""
        ):
            precedente = None
        articolo_prima: dict[str, str] = {}
        # Quanti colli chiedeva l'elenco di allora su quella riga: 0 se il
        # gestionale ha detto zero, `None` se la colonna era vuota. E' il solo
        # posto in cui quella differenza e' ancora scritta, e serve qui sotto.
        suggerito_prima: dict[str, Any] = {}
        if precedente is not None:
            for prodotto in precedente.get("products") or []:
                if isinstance(prodotto, dict):
                    identificativo_prima = str(prodotto.get("id") or "")
                    articolo_prima[identificativo_prima] = articolo_della_riga(prodotto)
                    suggerito_prima[identificativo_prima] = prodotto.get("suggestedQuantity")

        azzerati = 0
        conferme_scadute = 0
        riprese_dal_gestionale = 0
        scollegate = 0
        # Non esce di qui e non produce nessun avviso: serve solo a far
        # riscrivere lo stato quando l'unica cosa cambiata e' il fornitore
        # svuotato su un prodotto che nessuno ha piu'.
        da_reperire = 0
        # Nemmeno questo esce di qui: fa riscrivere lo stato quando l'unica cosa
        # cambiata e' il marchio della quantita' e il numero resta zero.
        marchi_corretti = 0
        tenute: list[dict[str, Any]] = []
        for decisione in stato.get("products") or []:
            if not isinstance(decisione, dict):
                continue
            identificativo = str(decisione.get("id") or "")
            disponibili = offerte_valide.get(identificativo)
            if disponibili is None:
                # Il prodotto non c'e' piu': la sua decisione non ha piu' un
                # posto dove attaccarsi e sparisce con lui.
                if int(_numero(decisione.get("quantity")) or 0) > 0:
                    azzerati += 1
                continue
            prima = articolo_prima.get(identificativo, "")
            adesso_articolo = articolo_adesso.get(identificativo, "")
            if prima and adesso_articolo and prima != adesso_articolo:
                # Stessa riga, altro articolo: la decisione si scollega e la riga
                # riparte da quello che dice il confronto nuovo.
                scollegate += 1
                continue
            # ⚠ 7 settembre 2026: le decisioni gia' sul disco con l'etichetta
            # sbagliata.  Fino al 6 settembre uno zero dell'elenco nasceva
            # «utente», e chi non riparte da «Inizia nuova comparazione» — che
            # cancella `state.json` — se lo porta dietro: l'elenco nuovo ne
            # chiede 4, la riga resta a 0 e nessuno lo dice.  Rietichettarli
            # tutti sarebbe peggio del difetto: uno zero «utente» puo' anche
            # essere il «non ordinarne» scritto a mano sopra un suggerimento, e
            # rileggerlo dall'elenco ordinerebbe merce che l'utente aveva
            # tolto.  A dirlo e' l'elenco di allora: se su quella riga il
            # gestionale aveva detto **zero** — zero, non colonna vuota — quello
            # zero e' suo, e torna a portare il suo marchio.  Da qui in poi lo
            # tratta il ramo di sotto, che lo rilegge dall'elenco nuovo, e al
            # primo ricalcolo lo stato si sana da se'.  L'articolo e' gia'
            # stato verificato qui sopra: se la riga porta un altro prodotto,
            # la decisione e' uscita prima.
            if (
                str(decisione.get("quantitySource") or "") == "utente"
                and int(_numero(decisione.get("quantity")) or 0) == 0
                and _numero(suggerito_prima.get(identificativo)) == 0
            ):
                decisione = {**decisione, "quantitySource": "gestionale"}
                marchi_corretti += 1
            if str(decisione.get("quantitySource") or "") == "gestionale":
                dal_confronto = quantita_del_confronto.get(identificativo, 0)
                if dal_confronto != int(_numero(decisione.get("quantity")) or 0):
                    decisione = {**decisione, "quantity": dal_confronto}
                    riprese_dal_gestionale += 1
            quantita = int(_numero(decisione.get("quantity")) or 0)
            fornitore = str(decisione.get("selectedSupplierId") or "")
            if quantita > 0 and (not fornitore or fornitore not in disponibili):
                if disponibili:
                    # Qualcuno lo serve ancora, ma non quello scelto: la
                    # quantita' torna a zero perche' su quale offerta metterla
                    # e' una scelta, e non la fa il programma al posto suo.
                    decisione = {**decisione, "quantity": 0, "selectedSupplierId": "", "confirmed": False}
                    decisione.pop("confirmedArticle", None)
                    azzerati += 1
                else:
                    # Nessun fornitore lo ha: non c'e' niente da scegliere, e
                    # azzerare cancellerebbe l'unica cosa che si sa di quella
                    # riga — quanti ne servono. La quantita' resta, il fornitore
                    # si svuota: e' un prodotto da reperire, non un errore.
                    ripulita = {**decisione, "selectedSupplierId": "", "confirmed": False}
                    ripulita.pop("confirmedArticle", None)
                    if ripulita != decisione:
                        # Senza questo conteggio lo stato non verrebbe riscritto
                        # e il fornitore sparito resterebbe sul disco.
                        da_reperire += 1
                    decisione = ripulita
            elif decisione.get("confirmed"):
                # La conferma vale per l'articolo che l'utente ha guardato. Se
                # nello stato non c'e' scritto QUALE — stato salvato prima di
                # questa regola — la si fa scadere lo stesso: non sapere che
                # cosa si e' confermato non e' una conferma.
                offerta = offerte_per_fornitore.get((identificativo, fornitore))
                confermato = str(decisione.get("confirmedArticle") or "")
                adesso = impronta_articolo(offerta) if offerta else ""
                if not confermato or not adesso or confermato != adesso:
                    decisione = {**decisione, "confirmed": False}
                    decisione.pop("confirmedArticle", None)
                    conferme_scadute += 1
            tenute.append(decisione)
        if (
            azzerati
            or conferme_scadute
            or riprese_dal_gestionale
            or scollegate
            or da_reperire
            or marchi_corretti
            or len(tenute) != len(stato.get("products") or [])
        ):
            stato["products"] = tenute
            stato["runId"] = str((confronto.get("run") or {}).get("id") or stato.get("runId") or "")
            stato["updatedAt"] = utc_ora()
            scrivi_json(self.configurazione.state_path, stato)
        elif str(stato.get("runId") or "") != str((confronto.get("run") or {}).get("id") or ""):
            # La run cambia identificativo a ogni ricalcolo, e lo stato deve
            # seguirla: `PUT /api/state` rifiuta uno snapshot di un'altra run.
            stato["runId"] = str((confronto.get("run") or {}).get("id") or "")
            stato["updatedAt"] = utc_ora()
            scrivi_json(self.configurazione.state_path, stato)
        return azzerati, conferme_scadute, riprese_dal_gestionale, scollegate


@dataclass
class _Corsa:
    """Lo scratchpad di una esecuzione: percorsi, numeri e riepiloghi."""

    cartella: Path
    registro_artefatti: dict[str, dict[str, Any]]
    profili_path: Path = Path()
    profili_usati_path: Path = Path()
    decisioni_path: Path = Path()
    manifest_path: Path = Path()
    validazione_path: Path = Path()
    dati_dir: Path = Path()
    shortlists_path: Path = Path()
    decisioni_ai_path: Path = Path()
    rapporto_ai_path: Path = Path()
    risolti_path: Path = Path()
    review_path: Path = Path()
    profili: list[dict[str, Any]] = field(default_factory=list)
    decisioni_da_imparare: list[str] = field(default_factory=list)
    fornitori_attesi: list[str] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    rapporto_ai: dict[str, Any] = field(default_factory=dict)
    confronto: dict[str, Any] = field(default_factory=dict)
    riepiloghi: dict[str, Any] = field(default_factory=dict)
    provenienza: dict[str, Any] = field(default_factory=dict)
    comandi: list[dict[str, Any]] = field(default_factory=list)
    numero_shortlist: int = 0
    messaggio_finale: str = ""
