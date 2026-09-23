#!/usr/bin/env python3
"""Local single-user server for the interactive supplier comparison UI.

The server binds to 127.0.0.1, stores state locally, never overwrites uploaded
files, and creates an audited JSON order plan.  Supplier XLSX copies remain the
responsibility of the deterministic writer in the skill pipeline.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
SKILL_ROOT = APP_DIR.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import ai_client  # noqa: E402
# La sostituzione della chiave dentro un testo sta in un posto solo: il suo
# modulo. Ricopiarla qui vorrebbe dire due espressioni regolari da tenere
# allineate, e quella che si dimentica e' sempre quella che perde la chiave.
from ai_client import oscura as senza_la_chiave  # noqa: E402
from catalog_search import SupplierCatalog  # noqa: E402
# La colonna in cui si scrivono le quantita' ordinate si puo' spostare dalla
# pagina: le regole di che cosa diventa la dichiarazione stanno nel suo modulo,
# e la prova che quella colonna sia scrivibile resta `launcher.source_rule`.
import colonna_ordine  # noqa: E402
# Le conferme dell'utente vivono in un file SQLite che sopravvive al ricalcolo
# settimanale: l'identita' e' l'articolo (codice a barre + nome normalizzato),
# mai la riga e mai il prezzo. Qui dentro non si riscrive nessuna delle sue
# regole — chi decide che cosa e' lo stesso articolo e' quel modulo, e lo
# decide in un posto solo.
from conferme import (  # noqa: E402
    MagazzinoConferme,
    MagazzinoNonUtilizzabile,
    codice_confrontabile,
    impronta_prodotto,
)
# La cartella datata, l'audit, l'elenco delle compilazioni e le difese sul
# percorso stanno tutte in `consegna`: qui dentro non si ricostruisce nessuna
# di quelle regole a mano, perche' due copie della stessa difesa divergono.
import consegna  # noqa: E402
# L'elenco dei prodotti che nessun fornitore porta: il foglio, il nome e il
# motivo stanno nel loro modulo. Qui resta il solo filtro di chi ci entra,
# perche' e' `find_offer`/`offer_is_available` a saperlo dire.
import da_reperire as da_reperire_modulo  # noqa: E402
from build_review_data import impronta_articolo  # noqa: E402
# Che cosa e' un'offerta — di chi e', si puo' ordinare, quanto costa al pezzo —
# lo dice il suo modulo, e lo dice per tutti: fino al 20 agosto 2026 «si puo'
# ordinare» aveva un'autorita' qui e due copie scritte a mano, una delle quali
# dentro la funzione che azzera le quantita' dopo un ricalcolo.
from offerta import (  # noqa: E402
    STATO_RIFIUTATO_UTENTE,
    find_offer,
    numero as number,
    offer_is_available,
    offer_pricing,
    offer_supplier_id,
)
from inspect_sources import profile_file  # noqa: E402
import order_history  # noqa: E402
import registro  # noqa: E402
# Scrivere un file senza poterlo trovare a meta', nemmeno dopo un black-out:
# lo schema stava in quattro copie e nessuna forzava i byte sul disco.
import scrittura_sicura  # noqa: E402
import versione_del_codice  # noqa: E402
from pipeline_jobs import (  # noqa: E402
    STATI_IN_CORSO,
    ConfigurazionePipeline,
    LavoroGiaInCorso,
    PipelineJobManager,
)
from promotion_bridge import PromotionService  # noqa: E402


MAX_JSON_BYTES = 150 * 1024 * 1024
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
# Il .xls c'e' perche' Noce manda un Excel 97-2003 e non ha il .xlsx: era
# l'unico formato che l'utente non riusciva a caricare.  L'estensione resta
# solo un primo filtro sul nome; che cosa sia davvero il documento lo dicono i
# suoi primi byte, in profile_file.
ALLOWED_UPLOAD_SUFFIXES = {".xlsx", ".xls", ".csv"}
FORMATO_DELL_ESTENSIONE = {".xlsx": "xlsx", ".xls": "xls", ".csv": "csv"}
NOME_DEL_FORMATO = {"xlsx": "Excel (.xlsx)", "xls": "Excel 97-2003 (.xls)", "csv": "CSV"}
SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._() -]+")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    """Scrittura atomica **e** arrivata sul disco: la fa `scrittura_sicura`.

    Era una delle quattro copie dello stesso schema, e nessuna delle quattro
    forzava i byte sul disco prima di sostituire: `os.replace` e' atomico
    rispetto ai metadati, non ai dati, e una mancanza di corrente nell'istante
    sbagliato lasciava uno `state.json` presente ma vuoto.
    """

    scrittura_sicura.scrivi_json(path, value)


def _centesimi_come_in_pagina(value: float) -> float:
    """L'arrotondamento con cui la pagina MOSTRA un importo, replicato qui.

    `round()` di Python arrotonda il double binario e a metà va al pari;
    `Intl.NumberFormat` del browser arrotonda la rappresentazione decimale
    più corta e a metà va per eccesso: su 14,665 il primo dà 14,66 e lo
    schermo mostra 14,67.  Ogni numero che la pagina disegna e che il
    servizio dichiara (i totali del Riepilogo, la somma delle righe) deve
    passare di qui, o la frase «le righe sommano X» nomina un numero che
    sullo schermo non esiste (revisione avversariale R4).  `Decimal(str(x))`
    usa la stessa rappresentazione decimale più corta del browser.
    """

    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def safe_upload_name(value: str) -> str:
    name = Path(str(value or "")).name.strip()
    name = SAFE_FILE_RE.sub("_", name).strip(" .")
    # Il taglio a 180 caratteri viene PRIMA del controllo sull'estensione, non
    # dopo: tagliare un nome già approvato (come si faceva prima) può staccare
    # il punto e l'estensione da un nome lunghissimo, e il file arriva sul
    # disco senza estensione pur avendo superato il controllo. Non è un errore
    # visibile: il caricamento riesce lo stesso — il lettore sceglie dai primi
    # byte, non dal nome — e il listino sparisce in silenzio dal confronto,
    # perché `candidate_files` di `scripts/inspect_sources.py` filtra per
    # suffisso e un file senza estensione non entra.
    suffix = Path(name).suffix.casefold()
    if len(name) > 180:
        name = name[:180 - len(suffix)] + suffix
    # Il controllo resta DOPO il taglio: un'aritmetica sbagliata (un suffisso
    # più lungo di 180 caratteri) potrebbe produrre un nome vuoto, e deve
    # cadere qui come qualunque altro nome non valido.
    if not name or name in {".", ".."}:
        raise ValueError("Nome del documento non valido")
    suffix = Path(name).suffix.casefold()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise ValueError(
            f"Formato non accettato: {suffix or 'senza estensione'}. "
            "Si caricano fogli di calcolo Excel (.xlsx o .xls) e file CSV."
        )
    return name


def upload_role(value: Any) -> str:
    """Ruolo scelto nel box di importazione, nel vocabolario della pipeline."""

    role = str(value or "").strip().casefold()
    if role in {"management", "master"}:
        return "master"
    if role in {"suppliers", "supplier"}:
        return "supplier"
    raise ValueError("Scegli se il documento è l'elenco del gestionale o un listino fornitore")


def profiled_upload_role(profile: Any) -> str:
    if not isinstance(profile, dict):
        return ""
    role = str(profile.get("upload_role") or (profile.get("ai_preflight") or {}).get("role") or "").strip().casefold()
    if role in {"master", "supplier"}:
        return role
    adattatore_id = str((profile.get("deterministic_hint") or {}).get("adapter_id") or "")
    if adattatore_id:
        try:
            adattatore = registro.adattatore(adattatore_id)
        except (KeyError, ValueError):
            return ""
        if not adattatore:
            return ""
        return "master" if adattatore.get("kind") == "master" else "supplier"
    return ""


def unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 10000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError(f"Troppe versioni del documento {name}")


def supplier_label(supplier_id: str) -> str:
    """Come si chiama questo fornitore per chi legge.

    Il nome viene dal registro degli adattatori — la stessa fonte da cui il
    fornitore nasce — e non da un elenco scritto qui. Con quattro nomi cablati,
    un fornitore imparato compariva come «NUOVO_FORNITORE», underscore
    compreso, nei messaggi e nello storico, mentre il registro ne portava gia'
    il `display_name` (revisione del 14 agosto 2026).
    """

    return registro.nome_del_fornitore(supplier_id)


def frase(value: Any) -> str:
    """Chiude una frase con il punto.

    I motivi tecnici arrivano dalle librerie senza punteggiatura, e concatenarli
    con la frase successiva produce testi che non si chiudono mai.
    """

    testo = str(value or "").strip()
    if not testo:
        return ""
    return testo if testo[-1] in ".!?" else f"{testo}."


def normalize_header(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split()).upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Regole condivise su offerte, prezzi e soglie.
#
# Stanno qui, fuori dalle singole funzioni, perche' la convalida dello stato,
# la compilazione e il preventivo di spostamento devono leggere i prezzi nello
# stesso identico modo. Due copie della stessa regola prima o poi divergono e
# l'utente si ritrova un preventivo che non corrisponde all'ordine compilato.
# ---------------------------------------------------------------------------


def nessuna_offerta_utilizzabile(product: Any) -> bool:
    """Vero quando nessun fornitore puo' servire questo prodotto.

    Non e' «l'utente non ha ancora scelto»: e' «non c'e' niente da scegliere».
    Un match ancora da verificare NON entra qui — un'offerta che chiede una
    conferma resta un'offerta utilizzabile, e il prodotto resta un caso da
    verificare, non un prodotto introvabile (decisione di Daniele del 16 agosto
    2026).  E' la condizione che distingue una quantita' senza fornitore — uno
    stato valido, che finisce nell'elenco dei prodotti da reperire — da una
    quantita' su un'offerta che non si puo' ordinare, che resta un errore.
    """

    if not isinstance(product, dict):
        return False
    return not any(offer_is_available(offer) for offer in product.get("offers") or [])


def offer_needs_confirmation(product: Any, offer: Any) -> bool:
    """Vero quando l'utente deve confermare a mano prima di poter ordinare.

    Somma le due provenienze: il prodotto (abbinamento incerto rilevato dalla
    pipeline) e la singola offerta (match non esatto, espositore non ad alta
    confidenza). E' la stessa condizione che fa scattare CONFERMA_MANCANTE nel
    salvataggio dello stato, quindi il preventivo di spostamento puo' avvisare
    in anticipo invece di far fallire il salvataggio successivo.
    """

    requires = bool(isinstance(product, dict) and (product.get("requiresConfirmation") or product.get("requires_confirmation")))
    if isinstance(offer, dict):
        requires = requires or bool(offer.get("requiresConfirmation") or offer.get("requires_confirmation"))
    return requires


def supplier_threshold(definition: Any) -> float:
    if not isinstance(definition, dict):
        return 0.0
    return number(definition.get("minimumOrder") or definition.get("minimum_order") or definition.get("thresholdNet")) or 0.0


def meets_threshold(total: float, threshold: float) -> bool:
    """Regola della compilazione: chi non ordina nulla non ha soglia da raggiungere."""

    return not (0 < total < threshold)


# Le due strade per compilare un ordine. La copia `.xlsx` la scrive il writer
# Node; il documento che va cambiato **in posizione** — quattro byte per cella,
# tutto il resto identico — lo scrive `app/xls_writer.py`, qui in Python.
PATCH_IN_POSIZIONE = "patch_xls_in_posizione"


def procedura_di_scrittura(regola: Any) -> str:
    """Come si compila il listino di questo fornitore: **lo dice la regola**.

    ⚠ Fino al 17 agosto 2026 la scelta fra le due strade era `supplier ==
    "noce"`, scritta in due punti di questo file. Il registro invece la
    dichiara da sempre (`order_write.mode`), e `launcher.source_rule` la porta
    dentro la regola di scrittura come `compilazione`: due verita' sullo stesso
    dato, e quella cablata vinceva.

    Costava in tutte e due le direzioni. Un fornitore **nuovo** che manda un
    `.xls` — il caso che il programma promette di saper imparare dalla pagina —
    finiva nel ramo del writer Node e veniva fermato con «il listino non e'
    disponibile in formato XLSX», cioe' accusando il documento del fornitore
    invece della configurazione, che e' l'unica cosa che si puo' correggere. E
    il giorno in cui Noce mandasse un `.xlsx` come tutti, il nome cablato
    lo avrebbe mandato lo stesso alla patch in posizione.

    Chi non dichiara niente passa dal writer Node: e' la strada normale, e
    resta quella di tutti i listini `.xlsx`.
    """

    if not isinstance(regola, dict):
        return ""
    return str(regola.get("compilazione") or "").strip()


# Le chiavi che `state.json` riceve da una rotta parziale — una risposta a un
# candidato, una riga abbinata a mano, uno sconto di testata, un prodotto
# aggiunto — e che percio' `validate_snapshot` deve **ricopiare dal disco**
# invece di ricostruire dallo snapshot della scheda: la scheda non le manda, e
# ricostruire lo stato senza di loro le cancella.
#
# Il 19 agosto 2026 qui mancava `manualMatches`, e il primo autosalvataggio
# dopo un abbinamento a mano lo cancellava in silenzio. Il valore e' quello che
# la chiave deve avere quando sul disco non c'e' niente.
#
# ⚠ Se aggiungi una rotta che scrive una chiave sua dentro `state`, aggiungila
# qui: `tests/test_abbinamento_a_mano.py` legge le rotte e confronta i due
# elenchi, e se ne dimentichi una la prova diventa rossa.
CHIAVI_DI_STATO_RICOPIATE: dict[str, Any] = {
    "manualProducts": [],
    "matchOverrides": [],
    "supplierDiscounts": {},
    "manualMatches": [],
}


class ReviewStore:
    def __init__(self, review_path: Path, state_path: Path, upload_dir: Path, output_dir: Path, writer_config: Path | None = None, history_path: Path | None = None, orders_dir: Path | None = None, conferme_path: Path | None = None) -> None:
        self.review_path = review_path.resolve()
        self.state_path = state_path.resolve()
        self.upload_dir = upload_dir.resolve()
        self.output_dir = output_dir.resolve()
        # La radice delle compilazioni sta accanto a `outputs`, non dentro.
        # Dallo smontaggio del carrello Noce il programma in `outputs` non
        # scrive piu' niente: cartella e rotta `/outputs/` restano solo per gli
        # artefatti delle run passate, e toglierle e' una decisione a se'.
        self.orders_dir = orders_dir.resolve() if orders_dir else self.output_dir.parent / "ordini"
        self.writer_config = writer_config.resolve() if writer_config else None
        # Lo storico vive fuori dalla cartella della run in corso: deve sopravvivere
        # al ricalcolo settimanale del confronto.
        self.history_path = history_path.resolve() if history_path else self.state_path.parent.parent / "history" / "orders.json"
        # Le conferme stanno accanto allo storico, e per la stessa ragione: una
        # risposta data vale per l'articolo, non per la settimana in cui e'
        # stata data. Dentro la cartella della run sparirebbe al primo ricalcolo,
        # che e' esattamente il difetto che questo magazzino chiude.
        self.conferme_path = conferme_path.resolve() if conferme_path else self.history_path.parent / "conferme.db"
        # Aperto alla prima domanda e non qui: un file che non si apre non deve
        # impedire al programma di partire. Il guasto si dice una volta, in
        # pagina, e il resto continua a funzionare senza memoria delle conferme.
        self._conferme: MagazzinoConferme | None = None
        self._conferme_guasto = ""
        self.lock = threading.RLock()
        self.catalog = SupplierCatalog()
        self.promotion_service = PromotionService()
        self.upload_profiles_path = self.upload_dir / "upload_profiles.json"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.orders_dir.mkdir(parents=True, exist_ok=True)
        # Un lavoro per volta, chiunque sia: il lucchetto e' uno solo e lo
        # prende chi sostituisce `review_data.json`.
        self.lucchetto_lavori = threading.Lock()
        self.pipeline_jobs = PipelineJobManager(
            ConfigurazionePipeline(
                data_dir=self.state_path.parent,
                uploads_dir=self.upload_dir,
                review_path=self.review_path,
                state_path=self.state_path,
            ),
            lucchetto_lavori=self.lucchetto_lavori,
            lucchetto_dati=self.lock,
            su_confronto_attivato=self.riconfigura_compilazione,
            uguaglianze_dichiarate=self.uguaglianze_in_vigore,
            rifiuti_dichiarati=self.rifiuti_in_vigore,
        )

    # ------------------------------------------------------- le conferme date
    #
    # ⚠ Perche' esiste un magazzino a parte, e non basta `state.json`. Un
    # prodotto in pagina si chiama `product:539`, cioe' e' il suo **numero di
    # riga** nell'export del gestionale: misurato il 15 agosto 2026 sui due
    # export veri, dei 457 identificativi presenti in tutti e due **449 portano
    # un articolo diverso**. Una conferma legata a quell'identificativo o si
    # perde al ricalcolo, o — peggio — si riapplica a merce che l'utente non ha
    # mai visto. Qui la conferma e' legata all'articolo, e sopravvive al listino
    # della settimana dopo.

    def magazzino_conferme(self) -> MagazzinoConferme | None:
        """Il magazzino, aperto alla prima domanda. `None` se non si apre.

        L'eccezione del modulo si intercetta **qui e in un posto solo**, come
        chiede il suo docstring: rispondere «nessuna conferma» a un file
        illeggibile farebbe tornare tutte le domande senza dire perche', e
        l'utente riconfermerebbe a mano credendo che il programma non avesse
        mai saputo niente. Il motivo resta in `_conferme_guasto` e finisce fra
        gli avvisi del confronto.
        """

        if self._conferme is not None or self._conferme_guasto:
            return self._conferme
        try:
            self._conferme = MagazzinoConferme(self.conferme_path)
        except MagazzinoNonUtilizzabile as exc:
            self._conferme_guasto = frase(exc) or "Il file delle conferme non si apre."
            self._conferme = None
        return self._conferme

    def _magazzino_solo_se_c_e(self) -> MagazzinoConferme | None:
        """Il magazzino, ma **senza crearlo** se non esiste ancora.

        ⚠ Serve a tenere la regola che il magazzino ha gia' pagato una volta:
        *un programma senza conferme non crea nessun `conferme.db`*. SQLite
        tiene il file aperto finche' la connessione vive, su Windows un file
        aperto blocca la cartella che lo contiene, e la volta scorsa ventinove
        prove morirono alla pulizia della cartella temporanea — con i test
        mirati tutti verdi. Le uguaglianze si leggono a **ogni** lettura del
        confronto: aprirle sempre riporterebbe quel guasto identico. Se nessuno
        ha mai dichiarato niente, non c'e' niente da leggere.
        """

        if self._conferme is None and not self.conferme_path.exists():
            return None
        return self.magazzino_conferme()

    def esporta_le_conferme(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Tutto quello che l'utente ha confermato, storico compreso.

        Due elenchi perche' il magazzino ha due tabelle e tutt'e due sono
        memoria «per sempre»: le conferme («si', e' lo stesso articolo») e le
        uguaglianze fra codici a barre. Portarne via una sola vorrebbe dire
        chiamare «copia» meta' del file.

        ⚠ `conferme.db` e' la memoria «per sempre» del programma — un
        abbinamento confermato vale anche per il listino della settimana
        prossima — e da quando `app/data/` e' ignorato per intero da git non ha
        piu' nessuna copia di sicurezza. `MagazzinoConferme.esporta` esisteva
        dal primo giorno, col suo perche' scritto nel docstring («un `.db` non
        si legge a occhio»), e non la chiamava nessuno fuori dai collaudi:
        questa e' la via d'uscita che rende quel perche' vero, e insieme il solo
        modo di farsi una copia senza copiare a mano un file SQLite aperto.

        Il magazzino **non si crea** se non c'e': un programma senza conferme
        non deve trovarsi un `conferme.db` vuoto per aver aperto Impostazioni.
        E se non si apre si risponde con l'elenco vuoto, come per le
        uguaglianze: il motivo sta gia' fra gli avvisi del confronto.
        """

        magazzino = self._magazzino_solo_se_c_e()
        if magazzino is None:
            return [], []
        try:
            return magazzino.esporta(), magazzino.esporta_uguaglianze()
        except MagazzinoNonUtilizzabile as exc:
            self._conferme_guasto = frase(exc) or "Le conferme date non si leggono."
            return [], []

    def uguaglianze_dichiarate(self) -> list[dict[str, Any]]:
        """Le dichiarazioni in vigore, una per coppia, per la pagina.

        Le **coppie** e non i gruppi: si toglie quello che qualcuno ha detto.
        Se il magazzino non si apre si risponde con l'elenco vuoto — il motivo è
        già fra gli avvisi del confronto, e una pagina che non si disegna per
        una memoria in meno sarebbe sproporzionata.
        """

        magazzino = self._magazzino_solo_se_c_e()
        if magazzino is None:
            return []
        try:
            return magazzino.uguaglianze()
        except MagazzinoNonUtilizzabile as exc:
            self._conferme_guasto = frase(exc) or "Le uguaglianze fra codici non si leggono."
            return []

    def elenco_delle_uguaglianze(self, query: str = "") -> dict[str, Any]:
        """Le dichiarazioni «questi due codici sono lo stesso articolo», leggibili.

        ⚠ **Perche' non basta la coppia di codici.** Fino al 18 agosto 2026
        l'elenco diceva `4009428623194 = 8729721830575` e basta: due numeri di
        tredici cifre, cioe' esattamente l'informazione che non permette di
        giudicare se la dichiarazione e' giusta. Daniele: «vorrei vedere anche
        il nome del prodotto da gestionale e quello da listino, altrimenti non
        posso valutare la correttezza».

        I nomi ci sono gia' e non serve nessuna colonna nuova: il magazzino
        salva l'**impronta** dei due articoli (`impronta_prodotto` per il
        gestionale, `impronta_articolo` per la riga di listino), e dentro
        l'impronta il nome normalizzato c'e'. Qui si aprono quelle due impronte
        nei pezzi che le compongono. Sono nomi **normalizzati** — maiuscoli e
        senza punteggiatura — e va detto: sono quelli con cui il programma
        confronta, non quelli che il fornitore stampa.

        La ricerca guarda tutto quello che si vede: codici, nomi, fornitore e
        motivo. Un elenco che cresce di una riga a settimana dopo un anno ne ha
        cinquanta, e cercarle a occhio e' il modo di non rileggerle mai.
        """

        cercato = " ".join(str(query or "").split()).casefold()
        dichiarate = self.uguaglianze_dichiarate()
        voci = []
        for riga in dichiarate:
            voce = self._uguaglianza_leggibile(riga)
            # `cercabile` e' il testo su cui si cerca, non un campo della
            # dichiarazione: esce di qui e non arriva alla pagina.
            testo = voce.pop("cercabile")
            if cercato and cercato not in testo:
                continue
            voci.append(voce)
        return {
            "ok": True,
            "query": " ".join(str(query or "").split()),
            "totale": len(dichiarate),
            "uguaglianze": voci,
            "motivo": self._conferme_guasto or "",
        }

    @staticmethod
    def _uguaglianza_leggibile(riga: Any) -> dict[str, Any]:
        """Una dichiarazione con dentro i nomi, non solo i due numeri.

        Le due impronte hanno forma diversa perche' rispondono a due domande
        diverse: quella del gestionale e' `codice|nome`, quella della riga di
        listino e' `fornitore|codice|codice fornitore|nome`. Si aprono per
        posizione, e quello che manca resta vuoto: un'impronta di una versione
        piu' vecchia non deve far sparire la riga dall'elenco.
        """

        riga = riga if isinstance(riga, dict) else {}
        codici = [str(voce) for voce in (riga.get("codici") or [])]
        pezzi_articolo = str(riga.get("articolo") or "").split("|")
        pezzi_offerta = str(riga.get("offerta") or "").split("|")
        gestionale = {
            "codice": pezzi_articolo[0] if len(pezzi_articolo) > 0 else "",
            "nome": pezzi_articolo[1] if len(pezzi_articolo) > 1 else "",
        }
        fornitore_id = pezzi_offerta[0] if len(pezzi_offerta) > 0 else ""
        listino = {
            "fornitoreId": fornitore_id,
            "fornitore": supplier_label(fornitore_id) if fornitore_id else "",
            "codice": pezzi_offerta[1] if len(pezzi_offerta) > 1 else "",
            "codiceFornitore": pezzi_offerta[2] if len(pezzi_offerta) > 2 else "",
            "nome": pezzi_offerta[3] if len(pezzi_offerta) > 3 else "",
        }
        motivo = str(riga.get("motivo") or "")
        cercabile = " ".join([
            *codici, gestionale["codice"], gestionale["nome"],
            listino["fornitore"], listino["fornitoreId"], listino["codice"],
            listino["codiceFornitore"], listino["nome"], motivo,
        ]).casefold()
        return {
            "codici": codici,
            "gestionale": gestionale,
            "listino": listino,
            "motivo": motivo,
            "dal": str(riga.get("valida_dal") or ""),
            "cercabile": cercabile,
        }

    def uguaglianze_in_vigore(self) -> list[list[str]]:
        """I gruppi di codici a barre dichiarati uguali, per la catena.

        La chiama `pipeline_jobs` all'inizio della lettura dei listini e ne
        scrive il risultato nella cartella della run. Se il magazzino non si
        apre si risponde «nessuna»: il motivo e' gia' in `_conferme_guasto` e
        finisce fra gli avvisi del confronto, e fermare un ricalcolo per una
        memoria che e' un di piu' sarebbe sproporzionato.
        """

        magazzino = self._magazzino_solo_se_c_e()
        if magazzino is None:
            return []
        try:
            return magazzino.classi()
        except MagazzinoNonUtilizzabile as exc:
            self._conferme_guasto = frase(exc) or "Le uguaglianze fra codici non si leggono."
            return []

    def rifiuti_in_vigore(self) -> dict[tuple[str, str], dict[str, Any]]:
        """I «no» dati, indicizzati per (fornitore, articolo del gestionale).

        Una query sola e non una per prodotto: questa funzione gira a ogni
        lettura del confronto, e la pagina si autosalva 450 ms dopo ogni
        modifica.

        ⚠ Passa da `_magazzino_solo_se_c_e` e non da `magazzino_conferme`, per
        la stessa ragione delle uguaglianze: un programma senza conferme non
        deve trovarsi un `conferme.db` vuoto per aver aperto una pagina, e su
        Windows un file SQLite aperto tiene in ostaggio la cartella che lo
        contiene.

        Se il magazzino non si apre si risponde «nessuno»: il motivo e' gia' in
        `_conferme_guasto` e finisce fra gli avvisi del confronto. ⚠ La
        direzione della degradazione e' quella prudente per costruzione — senza
        memoria dei rifiuti l'offerta torna visibile e la domanda torna a essere
        posta, cioe' si torna a chiedere invece di ordinare per conto proprio.
        """

        magazzino = self._magazzino_solo_se_c_e()
        if magazzino is None:
            return {}
        try:
            voci = magazzino.rifiuti()
        except MagazzinoNonUtilizzabile as exc:
            self._conferme_guasto = frase(exc) or "Gli abbinamenti rifiutati non si leggono."
            return {}
        return {
            (str(voce.get("fornitore") or ""), str(voce.get("articolo") or "")): voce
            for voce in voci
        }

    def spegni_le_offerte_rifiutate(self, review: dict[str, Any]) -> None:
        """«Non e' lo stesso articolo»: l'offerta esce dal confronto.

        E' il gemello negativo di `dichiara_la_conferma`, e produce l'unico
        effetto che serve: l'offerta smette di essere utilizzabile. Da li' in poi
        non si tocca nient'altro, perche' `offerta.offer_is_available` e' l'unica
        risposta a «si puo' ordinare» e la chiamano gia' tutti —
        `nessuna_offerta_utilizzabile`, `validate_snapshot`, il ciclo
        `da_reperire` di `compile`, `pipeline_jobs._ripulisci_stato`.

        ⚠ **Solo se l'impronta della riga di oggi combacia con quella su cui il
        no e' stato detto.** Stessa disciplina di `conferma_in_vigore`: il
        listino della settimana prossima puo' abbinare quel prodotto a un'altra
        riga, e su quella nessuno ha ancora detto niente — sarebbe una risposta
        applicata a merce mai vista, che e' il difetto che questo magazzino
        esiste per chiudere.

        ⚠ **Qui e non in `review()`.** `dichiara_la_conferma` sta in `review()`
        apposta, perche' un si' rimesso dal magazzino durante il salvataggio
        cancellerebbe la revoca. Il no non ha quel problema: non esiste nessuna
        casella nello snapshot che lo dica, si da' e si toglie solo dalla sua
        rotta, quindi il magazzino e' l'unica autorita' in lettura come in
        scrittura. E deve stare sulla porta comune, altrimenti
        `validate_snapshot` vedrebbe l'offerta ancora viva e rifiuterebbe con
        `OFFERTA_NON_VALIDA` la sola risposta rimasta all'utente.

        Non e' distruttiva: `base_review()` rilegge `review_data.json` dal disco
        a ogni chiamata, quindi togliere il no fa tornare l'offerta esattamente
        com'era — stato, prezzi, richiesta di conferma.
        """

        rifiuti = self.rifiuti_in_vigore()
        if not rifiuti:
            return
        for product in review.get("products") or []:
            if not isinstance(product, dict):
                continue
            articolo = impronta_prodotto(product)
            # Un articolo che non si identifica non ha nessun no da ritrovare.
            if not articolo:
                continue
            spente: list[dict[str, Any]] = []
            for offer in product.get("offers") or []:
                if not isinstance(offer, dict) or not offer_is_available(offer):
                    continue
                voce = rifiuti.get((offer_supplier_id(offer).strip().casefold(), articolo))
                if voce is None or str(voce.get("offerta") or "") != impronta_articolo(offer):
                    continue
                spente.append(offer)
                offer["available"] = False
                offer["status"] = STATO_RIFIUTATO_UTENTE
                offer["matchStatus"] = STATO_RIFIUTATO_UTENTE
                # La domanda non e' piu' aperta: chiederla ancora su un'offerta
                # che non si puo' scegliere terrebbe in piedi il bloccante
                # «Conferma richiesta» su un prodotto senza niente da
                # confermare, che e' esattamente il vicolo cieco di partenza.
                offer["requiresConfirmation"] = False
                offer["confirmed"] = False
                # Quello che la pagina deve poter dire: che cosa e' stato
                # rifiutato e quando. Senza, la griglia delle offerte
                # dichiarerebbe di quel fornitore «non ce l'hanno nel listino di
                # adesso» — falso: la riga ce l'hanno.
                offer["rifiutata"] = {
                    "since": str(voce.get("valida_dal") or ""),
                    "description": str(offer.get("description") or ""),
                }
            self._riscegli_dopo_il_no(product, spente)

    def _riscegli_dopo_il_no(self, product: dict[str, Any], spente: list[dict[str, Any]]) -> None:
        """Dopo un no: le righe con lo stesso codice chiedono conferma, e se il
        no ha spento l'offerta **scelta** la scelta si rifà.

        Il sospetto sta sulle **offerte**, non sul prodotto. Se la riga
        rifiutata non e' l'articolo, una riga con lo stesso codice a barre
        presso un altro fornitore e' sospetta — e deve chiederlo qualunque sia
        la strada per cui finisce scelta: il ripiego qui, uno sconto di testata
        che `review()` applica dopo, uno spostamento a mano, il preventivo di
        spostamento. Tutti passano da `offer_needs_confirmation`, che somma
        prodotto e offerta. Messo sul prodotto — la prima versione, 21 settembre
        2026 — la domanda restava sul ripiego calcolato qui senza sconti, e la
        riga NOCE col codice appena rifiutato passava in ordine senza
        conferma (verifica avversariale dello stesso giorno).

        La scelta si rifà perche' il confronto l'ha scritta la catena, che i no
        non li conosce: la settimana dopo un «non è lo stesso articolo» il
        prodotto nasceva ancora sulla riga spenta, `dichiara_la_conferma`
        cercava il si' sul fornitore sbagliato, e un si' dato sul nuovo non si
        riapplicava mai. La domanda di prodotto, che era della riga spenta, si
        toglie: da li' in poi decidono le offerte.
        """

        codici = {
            codice: str(offer.get("supplierName") or offer_supplier_id(offer)).upper()
            for offer in spente
            if (codice := codice_confrontabile(offer.get("ean")))
        }
        for offer in product.get("offers") or []:
            if not isinstance(offer, dict) or not offer_is_available(offer):
                continue
            codice = codice_confrontabile(offer.get("ean"))
            if codice not in codici:
                continue
            offer["requiresConfirmation"] = True
            offer["confirmed"] = False
            offer["confirmationMessage"] = (
                f"Hai detto che la riga di {codici[codice]} non è questo articolo, e questa ha lo "
                f"stesso codice a barre ({codice}): controlla che sia davvero lo stesso articolo."
            )
        scelta = str(product.get("selectedSupplierId") or "")
        if not any(offer_supplier_id(offer) == scelta for offer in spente):
            return
        migliore = self._offerta_piu_conveniente(product)
        product["selectedSupplierId"] = offer_supplier_id(migliore) if migliore else ""
        product["confirmed"] = False
        product["requiresConfirmation"] = False
        product["confirmationMessage"] = ""

    def chiudi(self) -> None:
        """Restituisce il file delle conferme.

        ⚠ Serve perche' SQLite tiene il file **aperto** finche' la connessione
        vive, e su Windows un file aperto non si cancella e non si rinomina —
        compresa la cartella che lo contiene. Un negozio che nessuno usa piu' e
        non l'ha restituito lascia il suo `conferme.db` in ostaggio: l'ha
        scoperto la suite intera, dove ventisei negozi nascono in cartelle
        temporanee e alla pulizia ventinove prove morivano con «Il file è
        utilizzato da un altro processo». I test mirati non lo vedevano.

        Chiamarlo due volte non fa danno, e dopo il negozio riapre da capo alla
        prima domanda.
        """

        if self._conferme is not None:
            self._conferme.chiudi()
            self._conferme = None

    def conferma_in_vigore(self, product: Any, offer: Any) -> dict[str, Any] | None:
        """La conferma che copre **questa** offerta di **questo** articolo.

        Il magazzino risponde anche quando l'articolo del fornitore di oggi non
        e' piu' quello confermato — e' una sua scelta dichiarata, che serve a
        distinguere «hanno cambiato il formato del listino» da «gli hanno
        cambiato l'articolo sotto il naso». Il confronto lo fa quindi chi
        chiama, cioe' qui: si applica solo la conferma la cui impronta combacia
        con la riga di listino di adesso.

        Una conferma **negata** (`accettata` falsa) non e' una conferma: si
        risponde `None`, perche' il posto in cui un «no» conta e' un'altra
        domanda e non questa.
        """

        if not isinstance(offer, dict):
            return None
        fornitore = offer_supplier_id(offer)
        articolo = impronta_prodotto(product)
        impronta = impronta_articolo(offer)
        # ⚠ Le impronte **prima** di aprire il file: un articolo che non si
        # identifica non ha niente da chiedere, e aprire il magazzino per
        # scoprirlo creerebbe un `conferme.db` a un programma che di conferme
        # non ne ha nessuna.
        if not fornitore or not articolo or not impronta:
            return None
        magazzino = self.magazzino_conferme()
        if magazzino is None:
            return None
        try:
            voce = magazzino.cerca(fornitore, articolo)
        except MagazzinoNonUtilizzabile as exc:
            self._conferme_guasto = frase(exc) or "Il file delle conferme non si legge più."
            return None
        if voce is None or not voce.get("accettata") or str(voce.get("offerta") or "") != impronta:
            return None
        return voce

    def dichiara_la_conferma(self, product: Any) -> None:
        """Sul prodotto: se una conferma c'e' gia', **quale** e da quando.

        Sono i due dati che la pagina dovra' saper dire — che una risposta e'
        gia' stata data e quando — e nascono qui perche' il posto in cui vive
        una conferma e' il magazzino, non lo stato della settimana.

        Il campo `confirmation` e' informativo e puo' mancare; `confirmed`
        invece diventa vero anche senza una decisione salvata, ed e' il punto
        di tutto il magazzino: la settimana dopo l'export del gestionale e'
        un altro file, il prodotto ha un altro numero di riga e la sua
        decisione non c'e' piu' — ma l'articolo e' lo stesso, e la risposta
        gia' data resta valida.
        """

        if not isinstance(product, dict):
            return
        offerta = find_offer(product, str(product.get("selectedSupplierId") or ""))
        # Solo dove una conferma serviva davvero: un abbinamento certo non e'
        # mai stato una domanda, e dirgli «confermato il 12 agosto» sarebbe una
        # notizia inventata su cinquecento righe.
        if offerta is None or not offer_needs_confirmation(product, offerta):
            return
        voce = self.conferma_in_vigore(product, offerta)
        if voce is None:
            return
        product["confirmed"] = True
        product["confirmation"] = {
            "supplierId": offer_supplier_id(offerta),
            "since": voce.get("valida_dal") or "",
            # L'identita' su cui la conferma vale, in chiaro: e' quella che
            # spiega perche' sopravvive a un listino nuovo, e senza dirla la
            # pagina non potrebbe spiegarlo a nessuno.
            "article": voce.get("articolo") or "",
        }

    def ricorda_le_conferme(
        self, clean: dict[str, Any], review: dict[str, Any], precedente: dict[str, Any]
    ) -> None:
        """Scrive nel magazzino quello che l'utente ha appena risposto.

        ⚠ **La revoca si riconosce dal confronto con lo stato di prima, non da
        `confirmed: false`.** La pagina manda `false` in due situazioni che non
        c'entrano niente fra loro: l'utente ha tolto la spunta, oppure il
        ricalcolo ha fatto scadere la conferma perche' la riga di listino porta
        un altro articolo. Trattarle allo stesso modo svuoterebbe il magazzino
        proprio nella settimana in cui deve servire. Quindi si dimentica solo
        quando **prima** c'era una conferma sullo **stesso** articolo del
        fornitore: quello e' l'utente che ha cambiato idea, e non c'e' altro
        modo di leggerlo.

        Un guasto del magazzino **non ferma il salvataggio**: si annota e si
        dice in pagina. Un `PUT /api/state` che fallisce non perde una
        risposta, le perde tutte quelle che vengono dopo — e' successo il
        15 agosto 2026 e non si rifa'.
        """

        prodotti = {str(item.get("id")): item for item in review.get("products") or []}
        prima = {
            str(item.get("id")): item
            for item in (precedente.get("products") or [])
            if isinstance(item, dict)
        }
        quando = str(clean.get("updatedAt") or "")
        if not quando:
            return

        # Prima si guarda **se** c'e' qualcosa da ricordare, e solo dopo si apre
        # il magazzino: aprirlo comunque creerebbe un `conferme.db` a un
        # programma che di conferme non ne ha mai avuta nessuna, e su Windows un
        # file aperto tiene in ostaggio la cartella che lo contiene.
        da_scrivere: list[tuple[bool, str, str, str, str]] = []
        for decisione in clean.get("products") or []:
            identificativo = str(decisione.get("id") or "")
            prodotto = prodotti.get(identificativo)
            fornitore = str(decisione.get("selectedSupplierId") or "")
            if prodotto is None or not fornitore:
                continue
            offerta = find_offer(prodotto, fornitore)
            # ⚠ Si ricorda solo dove una conferma era **richiesta**. Il
            # confronto marca `confirmed` anche sugli abbinamenti certi
            # (`server.py`, riassegnazione dello sconto: `confirmed = not
            # requiresConfirmation`), quindi senza questo filtro il magazzino si
            # riempirebbe di quattrocento risposte che nessuno ha mai dato — e
            # le poche vere non si troverebbero piu' in mezzo. Una memoria che
            # l'utente non puo' rileggere non e' una memoria.
            if offerta is None or not offer_needs_confirmation(prodotto, offerta):
                continue
            articolo = impronta_prodotto(prodotto)
            impronta = impronta_articolo(offerta)
            if not articolo or not impronta:
                continue
            if decisione.get("confirmed"):
                da_scrivere.append((True, fornitore, articolo, impronta, str(offerta.get("method") or "")))
                continue
            vecchia = prima.get(identificativo) or {}
            if vecchia.get("confirmed") and str(vecchia.get("confirmedArticle") or "") == impronta:
                da_scrivere.append((False, fornitore, articolo, impronta, ""))
        if not da_scrivere:
            return

        magazzino = self.magazzino_conferme()
        if magazzino is None:
            return
        try:
            for accettata, fornitore, articolo, impronta, motivo in da_scrivere:
                if accettata:
                    magazzino.ricorda(
                        fornitore=fornitore,
                        articolo=articolo,
                        offerta=impronta,
                        accettata=True,
                        motivo=motivo,
                        quando=quando,
                    )
                else:
                    # ⚠ Solo se quello in vigore e' un si'. `dimentica` chiude la
                    # riga in vigore qualunque essa sia, e da quando esiste il
                    # «non è lo stesso articolo» quella riga puo' essere un NO:
                    # un autosalvataggio che passasse di qui lo cancellerebbe, e
                    # la domanda tornerebbe la settimana dopo su un abbinamento
                    # che l'utente aveva gia' scartato. Cintura sopra le bretelle
                    # — la rotta del rifiuto svuota anche il fornitore scelto, e
                    # senza fornitore questo ciclo salta il prodotto — ma e' la
                    # difesa che costa meno di tutte.
                    voce_in_vigore = magazzino.cerca(fornitore, articolo)
                    if voce_in_vigore is None or voce_in_vigore.get("accettata"):
                        magazzino.dimentica(fornitore, articolo, quando=quando)
        except (MagazzinoNonUtilizzabile, ValueError) as exc:
            self._conferme_guasto = frase(exc) or "Le conferme non sono state salvate."

    def riconfigura_compilazione(self, review: dict[str, Any]) -> None:
        """Dopo un ricalcolo i listini sono altri file: la scrittura si rifa'.

        Senza questo passaggio la compilazione userebbe la configurazione della
        settimana scorsa — cioe' **il listino della settimana scorsa** — con i
        numeri di riga del confronto di adesso.  Il writer se ne accorgerebbe
        solo quando il file punta ancora allo stesso percorso, perche' li'
        confronta l'impronta; con un listino caricato con un nome nuovo no.
        """

        if self.writer_config is None:
            return
        from launcher import prepare_writer_config  # import tardivo: il server parte anche senza

        setup = prepare_writer_config(review, write=True, destinazione=self.writer_config)
        if setup.config_path is None:
            # ⚠ `prepare_writer_config` torna senza scrivere e SENZA sollevare
            # quando manca un requisito (Node sparito, script mancante): senza
            # questa riga il ramo piu' probabile falliva in silenzio e l'avviso
            # COMPILAZIONE_DA_RICONFIGURARE non nasceva (revisione del 13
            # agosto 2026).  La guardia del run_id terrebbe comunque chiusa la
            # compilazione, ma la promessa e' che lo si dica subito.
            raise RuntimeError(setup.message)

    def avvia_pipeline(self) -> dict[str, Any]:
        return self.pipeline_jobs.avvia()

    def stato_pipeline(self) -> dict[str, Any]:
        return self.pipeline_jobs.stato()

    def schemi_pendenti(self) -> dict[str, Any]:
        return self.pipeline_jobs.schemi_pendenti()

    def colonne_dei_documenti(self) -> dict[str, Any]:
        return self.pipeline_jobs.colonne_dei_documenti()

    def valida_schemi(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.pipeline_jobs.valida_schemi(payload)

    def conferma_schemi(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.pipeline_jobs.conferma_schemi(payload)

    # Il selettore delle colonne aperto a mano su un listino gia' caricato.
    def colonne_del_documento(self, nome: Any) -> dict[str, Any]:
        return self.pipeline_jobs.colonne_del_documento(nome)

    def prova_colonne(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.pipeline_jobs.prova_colonne_del_documento(payload)

    def salva_colonne(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.pipeline_jobs.salva_colonne_del_documento(payload)

    def colonne_d_ordine(self, supplier_id: str) -> dict[str, Any]:
        """Fra quali colonne si puo' scegliere quella dell'ordine, per un fornitore.

        Si mostrano **tutte** le colonne del foglio, anche quelle che il
        programma legge: nasconderle farebbe sembrare che il documento ne abbia
        meno di quante ne ha, e chi cerca «quella dopo il prezzo» conta quelle
        che vede. Quelle occupate arrivano marcate, non tolte.
        """

        documento = self.pipeline_jobs.documento_del_fornitore(supplier_id)
        effettiva = documento["effettiva"]
        chiave = str(supplier_id or "").strip().casefold()
        return {
            "ok": True,
            "supplierId": chiave,
            "supplierName": supplier_label(chiave),
            "fileName": effettiva.get("fileName"),
            "sheet": effettiva.get("sheet"),
            "headerRow": effettiva.get("headerRow"),
            "dataStartRow": effettiva.get("dataStartRow"),
            "attuale": effettiva.get("orderColumn"),
            "colonne": colonna_ordine.colonne_per_la_scelta(effettiva, documento["colonne"]),
        }

    def cambia_colonna_d_ordine(self, payload: Any) -> dict[str, Any]:
        """Sposta la colonna in cui si scrivono le quantita' ordinate.

        ⚠ **La prova che decide non e' scritta qui**: e' `launcher.source_rule`,
        cioe' la stessa funzione che attiva la compilazione. Si costruisce la
        dichiarazione che si vorrebbe scrivere, la si prova sul documento vero,
        e si tocca il registro **solo se passa**. Se qui nascesse una seconda
        regola, la settimana prossima direbbe una cosa diversa da quella che
        compila — ed e' esattamente l'errore che il 14 agosto 2026 ha bloccato
        LARICE dal vivo.

        Si scrive in quattro posti, e sono quattro perche' quattro sono le
        autorita' che devono dire la stessa cosa:

        1. il **registro**, che vale per tutte le settimane a venire;
        2. la **mappatura confermata** del registro, per i fornitori che
           dichiarano `from_field_mapping`: `source_rule` si rifiuta se le due
           divergono, e cambiarne una sola lascerebbe il fornitore muto;
        3. la **scheda del confronto in uso**, altrimenti la colonna nuova
           varrebbe solo dal prossimo ricalcolo — cioe' non adesso;
        4. la **decisione scritta a mano**, quando ce n'e' una per quel
           documento: e' sovrana sul registro, e lasciarla indietro farebbe
           tornare la colonna vecchia al primo ricalcolo, in silenzio.
        """

        from launcher import resolve_source_path, source_rule  # noqa: PLC0415

        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida")
        supplier_id = str(payload.get("supplierId") or "").strip().casefold()
        if not supplier_id:
            raise ValueError("Indicare il fornitore di cui cambiare la colonna d'ordine")
        indice = colonna_ordine.indice_scelto(payload.get("colonna"))
        if indice is None:
            raise ValueError("Indicare la colonna in cui scrivere l'ordine")
        if self.pipeline_jobs.in_corso():
            raise LavoroGiaInCorso(
                "Un confronto è in corso: la colonna d'ordine si cambia quando ha finito."
            )
        lettera = colonna_ordine.lettera_di_indice(indice)
        nome = supplier_label(supplier_id)

        with self.lock:
            documento = self.pipeline_jobs.documento_del_fornitore(supplier_id)
            effettiva = documento["effettiva"]
            colonne = {
                int(voce["colonna"]): voce
                for voce in documento["colonne"]
                if isinstance(voce, dict) and voce.get("colonna")
            }
            voce = colonne.get(indice)
            if voce is None:
                raise ValueError(
                    f"Il foglio «{effettiva.get('sheet') or ''}» di {nome} non arriva alla colonna {lettera}."
                )
            occupata = colonna_ordine.campo_che_occupa(effettiva, indice)
            if occupata:
                raise ValueError(
                    f"La colonna {lettera} è quella che leggo come {occupata}: "
                    "l'ordine la azzererebbe e la riscriverebbe, e quel dato andrebbe perso. "
                    "Scegline una libera."
                )
            formule = int(voce.get("formule") or 0)
            if formule:
                raise ValueError(
                    f"La colonna {lettera} di {nome} contiene {formule} "
                    f"{'formula' if formule == 1 else 'formule'}: scrivere l'ordine le cancella. "
                    "Scegline una senza calcoli dentro."
                )

            adattatore = documento["adattatore"]
            if not isinstance(adattatore, dict) or not adattatore:
                raise ValueError(
                    f"Il confronto in uso non dice con quale adattatore ha letto il listino {nome}."
                )
            scrittura = adattatore.get("order_write")
            if not isinstance(scrittura, dict) or not scrittura:
                raise ValueError(
                    f"Il registro non dichiara come si scrive l'ordine dentro il listino {nome}: "
                    "la colonna si sceglie quando ti chiedo le colonne del documento."
                )
            intestazione = voce.get("intestazione")
            # ⚠ Un listino puo' non avere **nessuna** riga di intestazione —
            # LARICE e' cosi' — e allora sopra la colonna non c'e' nessuna cella
            # da confermare vuota. Misurato prima di scriverlo: dichiararla lo
            # bloccherebbe con una frase che accusa il registro.
            c_e_intestazione = bool(effettiva.get("headerRow"))
            candidato = dict(adattatore)
            # ⚠ Con l'id cosi' com'e' questa mossa scriveva una **fotocopia
            # completa** della voce spedita nel registro imparato, e da quel
            # momento nessun aggiornamento di quell'adattatore arrivava piu':
            # e' la stessa malattia di `offerte_v1`, dove l'impronta stretta
            # appena scritta e' rimasta spenta sotto una copia imparata. Adesso
            # la voce nuova prende un id suo — `betulla_v1__locale` — con la
            # stessa regola della mappatura guidata, che sta in un posto solo.
            candidato["id"] = registro.identificativo_da_scrivere(
                adattatore.get("id"), self.pipeline_jobs.configurazione.adapters_path
            )
            if candidato["id"] != adattatore.get("id"):
                # Scritto nella voce e non solo nel nome, come fa
                # `impara_adattatore`: chi la rilegge fra un mese deve poter
                # dire da dove viene senza conoscere la convenzione del suffisso.
                candidato["derivato_da"] = adattatore.get("id")
            candidato["order_write"] = colonna_ordine.dichiarazione_aggiornata(
                scrittura, lettera, intestazione, c_e_intestazione=c_e_intestazione
            )
            da_mappatura = bool(candidato["order_write"].get("from_field_mapping"))
            if da_mappatura:
                candidato["field_mapping"] = colonna_ordine.mappatura_aggiornata(
                    adattatore.get("field_mapping"), lettera, intestazione,
                    c_e_intestazione=c_e_intestazione,
                )

            review = load_json(self.review_path, {})
            scheda = self._scheda_del_fornitore(review, supplier_id)
            if scheda is None:
                raise ValueError(f"Nel confronto in uso non c'è nessun listino di {nome}.")
            sorgente = resolve_source_path(
                scheda.get("sourcePath")
                or scheda.get("source_path")
                or scheda.get("originalPath")
                or scheda.get("path")
            )
            if sorgente is None or not sorgente.is_file():
                raise ValueError(f"Il listino di {nome} non è più al suo posto sul disco.")
            prova = dict(scheda)
            if da_mappatura:
                prova["fieldMapping"] = colonna_ordine.mappatura_aggiornata(
                    scheda.get("fieldMapping") or scheda.get("field_mapping"),
                    lettera,
                    intestazione,
                    c_e_intestazione=c_e_intestazione,
                )
            regola, motivo = source_rule(supplier_id, sorgente, prova, candidato)
            if regola is None:
                # La frase e' quella della verifica vera, non una riscritta qui:
                # e' la stessa che comparirebbe al prossimo avvio, e chi la legge
                # deve poterla ritrovare identica.
                raise ValueError(motivo or f"{nome}: la colonna {lettera} non è scrivibile.")

            registro.scrivi_adattatore(candidato, self.pipeline_jobs.configurazione.adapters_path)
            if da_mappatura:
                scheda["fieldMapping"] = prova["fieldMapping"]
                atomic_json(self.review_path, review)
            self._aggiorna_decisione_a_mano(
                scheda,
                prova.get("fieldMapping") if da_mappatura else None,
                lettera,
                intestazione,
                c_e_intestazione=c_e_intestazione,
            )
            # ⚠ Da qui in poi la colonna **e' gia' cambiata**: la dichiarazione
            # e' scritta e ha passato la verifica sul documento. Se la
            # configurazione di scrittura non si rifa' — Node sparito, cartella
            # non scrivibile — lasciare uscire l'eccezione direbbe all'utente che
            # non e' successo niente, mentre e' successo quasi tutto. E' la
            # stessa mezza verita' che il 17 agosto mostrava il riquadro verde
            # per una compilazione senza nessuna copia: si dice che cosa e'
            # cambiato, che cosa no, e come si rimedia.
            avviso = ""
            try:
                self.riconfigura_compilazione(review)
            except Exception as exc:  # noqa: BLE001 - qualunque motivo, va detto
                avviso = (
                    f"La colonna è cambiata, ma la configurazione di scrittura non si è "
                    f"rifatta: {frase(exc)} Si rifà da sola al prossimo confronto, oppure "
                    "chiudendo e riaprendo il programma."
                )

        titolo = " ".join(str(intestazione or "").split())
        return {
            "ok": True,
            "supplierId": supplier_id,
            "supplierName": nome,
            "colonna": lettera,
            "intestazione": titolo,
            "regola": regola,
            "avviso": avviso,
            "message": (
                f"L'ordine di {nome} si scrive nella colonna {lettera}"
                + (f" («{titolo}»)" if titolo else ", che non ha intestazione")
                + (". Vale da subito, anche per la compilazione di adesso." if not avviso
                   else ". " + avviso)
            ),
        }

    @staticmethod
    def _scheda_del_fornitore(review: Any, supplier_id: str) -> dict[str, Any] | None:
        """La scheda-documento del listino di questo fornitore nel confronto."""

        cercato = str(supplier_id or "").strip().casefold()
        for voce in (review or {}).get("files") or []:
            if not isinstance(voce, dict):
                continue
            chiave, _nome = ReviewStore._fornitore_della_voce(voce)
            if chiave == cercato:
                return voce
        return None

    def _aggiorna_decisione_a_mano(
        self,
        scheda: dict[str, Any],
        mappatura: dict[str, Any] | None,
        lettera: str,
        intestazione: Any,
        *,
        c_e_intestazione: bool = True,
    ) -> None:
        """Se per quel documento c'e' una decisione scritta a mano, la si allinea.

        Una decisione manuale e' **sovrana** sul registro
        (`pipeline_jobs`: «una decisione manuale e' sovrana e copre anche un
        documento che il registro declasserebbe»). Lasciarla indietro vorrebbe
        dire vedere la colonna nuova oggi e ritrovarsi quella vecchia al primo
        ricalcolo, senza una parola.
        """

        percorso = self.pipeline_jobs.configurazione.decisioni_manuali_path
        documento = load_json(percorso, None)
        decisioni = documento.get("decisions") if isinstance(documento, dict) else documento
        if not isinstance(decisioni, list) or not decisioni:
            return
        nome = str(scheda.get("name") or "").strip().casefold()
        impronta = str(scheda.get("sourceSha256") or scheda.get("source_sha256") or "").strip().casefold()
        cambiate = False
        for decisione in decisioni:
            if not isinstance(decisione, dict):
                continue
            suo_nome = str(decisione.get("file_name") or "").strip().casefold()
            sua_impronta = str(decisione.get("file_sha256") or "").strip().casefold()
            if suo_nome != nome and not (impronta and sua_impronta == impronta):
                continue
            decisione["field_mapping"] = colonna_ordine.mappatura_aggiornata(
                mappatura if mappatura is not None else decisione.get("field_mapping"),
                lettera,
                intestazione,
                c_e_intestazione=c_e_intestazione,
            )
            cambiate = True
        if cambiate:
            atomic_json(percorso, {"decisions": decisioni})

    def review_with_manual_products(self, state: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """Il confronto decorato. Con `state` si decora su uno stato in mano.

        ⚠ Lo stato lo rilegge dal disco, e va bene per tutti tranne uno: chi
        deve vedere il confronto come sara' DOPO una decisione che non ha
        ancora scritto. `set_supplier_discount` e' quel caso — riassegna il
        fornitore sui prezzi dello sconto appena messo — e prima di passare di
        qui si ricostruiva il confronto per conto suo, saltando gli
        abbinamenti a mano e le risposte alle proposte (revisione del 6
        settembre 2026).
        """

        review = self.base_review()
        if state is None:
            state = load_json(self.state_path, {})
        known_ids = {str(item.get("id") or "") for item in review.get("products") or []}
        for product in state.get("manualProducts") or []:
            product_id = str(product.get("id") or "")
            if not product_id or product_id in known_ids:
                continue
            review.setdefault("products", []).append(deepcopy(product))
            known_ids.add(product_id)
        for product in review.get("products") or []:
            is_display = str(product.get("itemType") or product.get("kind") or "").casefold() == "display"
            product["quantityLabel"] = "espositori" if is_display else "colli"
            product["orderUnitLabel"] = "espositori" if is_display else "colli"
        self.apply_match_overrides(review, state)
        # Le righe scelte a mano nel visualizzatore dei listini: subito dopo le
        # risposte all'analisi automatica, perche' sono la stessa famiglia di
        # decisione — «questa riga e' il mio prodotto» — e prima degli sconti,
        # che devono valere anche su un'offerta appena entrata.
        self._applica_abbinamenti_manuali(review, state)
        # I «no» dati: dopo le due risposte positive — la proposta accettata e la
        # riga scelta a mano — perche' e' l'ultima parola su quell'offerta. Non
        # c'e' conflitto con l'abbinamento manuale: quello riscrive EAN e
        # descrizione della riga, quindi l'impronta cambia e un vecchio no non la
        # copre piu'. Se invece l'utente sceglie a mano PROPRIO la riga che aveva
        # rifiutato, il no viene chiuso da `abbina_riga_di_listino`: due risposte
        # umane sullo stesso prodotto, vince la piu' recente.
        self.spegni_le_offerte_rifiutate(review)
        # ⚠ Qui, e solo qui.  Questa e' la porta da cui passa **tutto** quello
        # che tocca un prezzo: la pagina, i totali del riepilogo, l'anteprima
        # dello spostamento, il piano d'ordine.  Scontare piu' avanti — per
        # esempio dentro `offer_pricing` — vorrebbe dire prezzi scontati nei
        # conti del servizio e prezzi pieni sullo schermo, cioe' due verita'
        # sulla stessa riga.  Scontare piu' indietro non funziona: dentro
        # `base_review` il catalogo riscrive le offerte con i prezzi freschi
        # (`catalog_search.enrich_review`) e cancellerebbe lo sconto sui soli
        # prodotti che il catalogo conosce — a macchia, e in silenzio.
        self.applica_sconti_fornitore(review, self.sconti_del_confronto(state, review))
        return review, state

    # Lo sconto di testata che il fornitore fa su tutto il listino: «BETULLA
    # potrebbe scontare tutto del 6%».  Non sta nel registro degli adattatori —
    # quello dice come si LEGGE un documento — ma nello stato, accanto alle
    # altre decisioni di questa settimana.
    @staticmethod
    def sconti_del_confronto(state: Any, review: Any) -> dict[str, float]:
        """Gli sconti che valgono per QUESTO confronto, gia' controllati.

        ⚠ Scadono da soli al ricalcolo, senza dire niente a nessuno (decisione
        di Daniele del 15 agosto 2026: lo sconto lo mette lui subito dopo aver
        caricato i listini, e non vuole avvisi).  La scadenza e' qui e non in
        una pulizia periodica: uno sconto legato a un'altra run semplicemente
        non si applica, e non c'e' finestra in cui possa applicarsi per sbaglio.
        """

        run = str(((review or {}).get("run") or {}).get("id") or "")
        voci = (state or {}).get("supplierDiscounts")
        if not run or not isinstance(voci, dict):
            return {}
        validi: dict[str, float] = {}
        for chiave, voce in voci.items():
            if not isinstance(voce, dict) or str(voce.get("runId") or "") != run:
                continue
            rate = number(voce.get("rate"))
            if rate is None or not 0 < rate < 1:
                continue
            validi[str(chiave).strip().casefold()] = float(rate)
        return validi

    @staticmethod
    def applica_sconti_fornitore(review: dict[str, Any], sconti: dict[str, float]) -> None:
        """Toglie la percentuale a ogni prezzo delle offerte di quel fornitore."""

        if not sconti:
            return
        for product in review.get("products") or []:
            if not isinstance(product, dict):
                continue
            for offer in product.get("offers") or []:
                if not isinstance(offer, dict):
                    continue
                rate = sconti.get(str(offer.get("supplierId") or "").strip().casefold())
                if not rate:
                    continue
                for campo in ("unitPriceNet", "orderUnitPriceNet", "price", "pricePerPiece", "unitPricePreDiscount"):
                    valore = number(offer.get(campo))
                    if valore is not None:
                        offer[campo] = round(valore * (1 - rate), 4)
                # ⚠ «▼ 0,12 € rispetto all'ultimo pagato» era calcolato a monte
                # sul prezzo pieno: lasciarlo sarebbe una freccia che indica un
                # numero che sullo schermo non c'e' piu'.  Tolto, la pagina lo
                # ricalcola da sola dal prezzo che ha in mano.
                offer.pop("lastPriceDifference", None)
                offer.pop("lastPriceDifferencePct", None)
                offer["supplierDiscountRate"] = rate

    @staticmethod
    def _candidate_matches_warning(warning: dict[str, Any], supplier_id: str, candidate_key: str) -> bool:
        return (
            str(warning.get("code") or "") == "RIFIUTO_CON_CANDIDATO_FORTE"
            and str(warning.get("supplierId") or "") == supplier_id
            and str(warning.get("candidateKey") or "") == candidate_key
        )

    def apply_match_overrides(self, review: dict[str, Any], state: dict[str, Any]) -> None:
        """Applica le correzioni umane ai rifiuti sospetti della run corrente.

        La decisione vale soltanto se coincidono run, prodotto, fornitore e
        impronta della riga proposta. Il listino della settimana dopo può
        riusare la stessa riga numerica per un altro articolo: in quel caso
        l'impronta cambia e la vecchia risposta resta inerte.
        """

        review_run = str((review.get("run") or {}).get("id") or "")
        if str(state.get("runId") or "") != review_run:
            return
        overrides = {
            (str(item.get("productId") or ""), str(item.get("supplierId") or "")): item
            for item in state.get("matchOverrides") or []
            if isinstance(item, dict) and str(item.get("runId") or "") == review_run
        }
        for product in review.get("products") or []:
            product_id = str(product.get("id") or "")
            for offer in product.get("offers") or []:
                supplier_id = str(offer.get("supplierId") or "")
                candidate = offer.get("rejectedCandidate")
                if not isinstance(candidate, dict):
                    continue
                candidate_key = str(candidate.get("candidateKey") or "")
                override = overrides.get((product_id, supplier_id))
                if not candidate_key or not override or str(override.get("candidateKey") or "") != candidate_key:
                    continue
                accepted = override.get("accepted") is True
                offer["candidateDecision"] = "accepted" if accepted else "rejected"
                if accepted and candidate.get("available") is True:
                    preserved = deepcopy(candidate)
                    offer.update(deepcopy(candidate))
                    offer["rejectedCandidate"] = preserved
                    offer["available"] = True
                    offer["status"] = "SEMANTICO_CONFERMATO_UTENTE"
                    offer["matchStatus"] = "SEMANTICO_CONFERMATO_UTENTE"
                    offer["method"] = "CORREZIONE_UTENTE"
                    offer["confidence"] = "UTENTE"
                    offer["requiresConfirmation"] = False
                    offer["confirmed"] = True
                product["warnings"] = [
                    warning for warning in product.get("warnings") or []
                    if not (
                        isinstance(warning, dict)
                        and self._candidate_matches_warning(warning, supplier_id, candidate_key)
                    )
                ]

        # Il conteggio generale scritto dal builder non può conoscere le
        # risposte arrivate dopo. Si ricostruisce sui soli casi ancora aperti.
        review["warnings"] = [
            warning for warning in review.get("warnings") or []
            if not (isinstance(warning, dict) and warning.get("code") == "RIFIUTI_CON_CANDIDATO_FORTE")
        ]
        aperti = [
            product for product in review.get("products") or []
            if any(
                isinstance(warning, dict) and warning.get("code") == "RIFIUTO_CON_CANDIDATO_FORTE"
                for warning in product.get("warnings") or []
            )
        ]
        if aperti:
            review.setdefault("warnings", []).append({
                "code": "RIFIUTI_CON_CANDIDATO_FORTE",
                "severity": "warning",
                "blocking": False,
                "title": "Proposte da controllare",
                "message": (
                    (
                        "1 prodotto ha una proposta di un fornitore da confermare o rifiutare. "
                        if len(aperti) == 1
                        else f"{len(aperti)} prodotti hanno una proposta di un fornitore da confermare o rifiutare. "
                    )
                    + "Li trovi con il filtro «Da confermare»."
                ),
                "count": len(aperti),
            })

    def _profilo_ha_ancora_la_sua_copia(self, profile: Any) -> bool:
        """Il profilo descrive un documento che negli upload c'e' ancora davvero.

        ⚠ Un profilo non e' il documento: e' la sua scheda.  Se la copia viene
        cancellata dalla cartella — con Esplora risorse, non dall'app — la
        scheda resta, e resta a dire il falso.  Questa e' l'unica domanda che
        distingue una scheda vera da un fantasma, e si fa in un posto solo
        perche' la sbagliano tutti allo stesso modo (difetto del 15 agosto
        2026: `upload` rifiutava come «gia' presente» un listino che sul disco
        non c'era piu', e la pagina — che gia' filtrava i fantasmi — non
        mostrava niente da eliminare per uscirne).
        """

        if not isinstance(profile, dict):
            return False
        name = str(profile.get("file_name") or "")
        path = consegna.file_sicuro(self.upload_dir, name) if name else None
        if path is None:
            return False
        try:
            return Path(str(profile.get("path") or "")).resolve() == path.resolve()
        except OSError:
            return False

    def _profili_veri_e_fantasmi(
        self, profiles_doc: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """I profili con una copia sul disco, e quelli che la copia non ce l'hanno."""

        veri: list[dict[str, Any]] = []
        fantasmi: list[dict[str, Any]] = []
        for profile in profiles_doc.get("profiles") or []:
            if not isinstance(profile, dict):
                continue
            (veri if self._profilo_ha_ancora_la_sua_copia(profile) else fantasmi).append(profile)
        return veri, fantasmi

    def _safe_upload_profiles(self, profiles_doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Profili il cui percorso coincide davvero con una copia negli upload."""

        veri, _ = self._profili_veri_e_fantasmi(profiles_doc)
        return {str(profile.get("file_name") or ""): profile for profile in veri}

    @staticmethod
    def _fornitore_della_voce(voce: dict[str, Any]) -> tuple[str, str]:
        """Chiave e nome del fornitore di una scheda-documento, o vuoto.

        Le schede non hanno tutte la stessa forma: quelle nate da un ricalcolo
        portano `supplierId`, quelle di un confronto piu' vecchio che punta
        ancora all'originale fuori dai caricamenti portano solo `supplier`, il
        nome per esteso.  Leggerne una sola delle due vuol dire non accorgersi
        della meta' dei casi, quindi la regola sta qui, scritta una volta.
        """

        if str(voce.get("role") or "").strip().casefold() == "master":
            return "", ""
        etichetta = str(voce.get("supplier") or "").strip()
        chiave = str(voce.get("supplierId") or "").strip().casefold()
        if not chiave and etichetta.casefold() not in {
            "", "gestionale", "da confermare", "da riconoscere",
        }:
            chiave = etichetta.casefold()
        if not chiave:
            return "", ""
        return chiave, (etichetta or chiave.upper())

    @staticmethod
    def _spegni_i_fornitori_senza_documento(
        review: dict[str, Any], rimossi: dict[str, str]
    ) -> None:
        """Un fornitore il cui listino e' stato tolto smette di essere ordinabile.

        ⚠ Il difetto che ha aperto la revisione del 14 agosto 2026, con le
        parole di chi lo ha subito: «eliminando un listino questo continuava a
        comparire nel confronto».  Era cosi' per costruzione: `delete_upload`
        toglieva la scheda del file, `base_review` filtrava `review["files"]`, e
        nessuna delle due nominava `suppliers` o `products`.  Il fornitore
        restava fra le offerte di ogni prodotto, restava quello scelto, e
        restava nel totale in euro del riepilogo.

        Non si cancella niente: si spegne `available`, che e' gia' la leva con
        cui il resto del programma dice «da qui non si ordina».  La pagina
        riassegna da sola al piu' conveniente fra i disponibili e lo dichiara,
        la convalida rifiuta chi prova a ordinare lo stesso, e se il listino
        torna al suo posto il confronto ricompare intatto: e' una derivazione,
        non una potatura.

        `rimossi` contiene **solo** i fornitori che un documento ce l'avevano e
        non ce l'hanno piu'.  Chi nell'elenco non e' mai comparso resta
        ordinabile: di lui non si sa niente, e spegnerlo sarebbe indovinare.
        """

        if not rimossi:
            return

        toccati: set[str] = set()
        for prodotto in review.get("products") or []:
            if not isinstance(prodotto, dict):
                continue
            for offerta in prodotto.get("offers") or []:
                if not isinstance(offerta, dict):
                    continue
                nome = str(offerta.get("supplierId") or offerta.get("supplier_id") or "").strip().casefold()
                if nome not in rimossi:
                    continue
                offerta["available"] = False
                offerta["warning"] = (
                    f"Il listino {rimossi[nome]} è stato eliminato: questa offerta non è "
                    "più ordinabile finché non ricarichi il listino e rifai il confronto."
                )
                toccati.add(nome)

        for voce in review.get("suppliers") or []:
            if isinstance(voce, dict) and str(voce.get("id") or "").strip().casefold() in rimossi:
                voce["documentMissing"] = True

        if not toccati:
            return
        nomi = sorted(rimossi[nome] for nome in toccati)
        elenco = ", ".join(nomi)
        review.setdefault("warnings", []).append({
            "code": "LISTINO_ELIMINATO",
            "severity": "warning",
            "blocking": False,
            "suppliers": sorted(toccati),
            "title": (
                f"{elenco}: listino eliminato" if len(nomi) == 1
                else f"{len(nomi)} listini eliminati"
            ),
            "message": (
                f"Hai eliminato il listino di {elenco} dopo l'ultimo confronto. Le sue offerte "
                "restano visibili ma non si possono più ordinare, e i prodotti che gli erano "
                "assegnati sono passati a un altro fornitore. Ricarica il listino e rifai il "
                "confronto per rimetterlo in gioco."
            ),
        })

    def base_review(self) -> dict[str, Any]:
        review = load_json(self.review_path, {
            "run": {"id": "", "status": "awaiting_files", "createdAt": datetime.now(tz=timezone.utc).isoformat(), "label": "Nuova run"},
            "files": [],
            "suppliers": [],
            "products": [],
            "warnings": [],
        })
        if not isinstance(review, dict):
            raise ValueError("review_data.json non contiene un oggetto")
        profiles = load_json(self.upload_profiles_path, {"profiles": []})
        safe_profiles = self._safe_upload_profiles(profiles)
        visible_files = []
        fornitori_senza_documento: dict[str, str] = {}
        for file_entry in review.get("files") or []:
            if not isinstance(file_entry, dict):
                continue
            name = str(file_entry.get("name") or "")
            profile = safe_profiles.get(name)
            ruolo_scelto = profiled_upload_role(profile)
            if ruolo_scelto:
                file_entry["role"] = ruolo_scelto
                file_entry["kind"] = "Gestionale" if ruolo_scelto == "master" else "Listino"
                file_entry["supplier"] = "Gestionale" if ruolo_scelto == "master" else str(file_entry.get("supplier") or "Da confermare")
            source = str(file_entry.get("sourcePath") or "")
            inside_uploads = False
            if source:
                try:
                    inside_uploads = Path(source).resolve().parent == self.upload_dir
                except OSError:
                    inside_uploads = False
            if inside_uploads:
                # Il confronto vivo resta valido, ma la pagina Importa descrive
                # le copie che sono ancora disponibili per il prossimo
                # ricalcolo. Una copia eliminata non deve ricomparire dal JSON
                # della run precedente.
                path = consegna.file_sicuro(self.upload_dir, name)
                if path is None:
                    # ⚠ Qui si sa una cosa che nessuno usava: questo fornitore
                    # AVEVA un documento e adesso non ce l'ha piu'.  E' la
                    # differenza fra «non lo so» e «e' stato tolto», e solo la
                    # seconda autorizza a togliergli le offerte.  Un fornitore
                    # che nell'elenco non c'e' mai stato non finisce qui, e
                    # resta ordinabile: di lui non sappiamo niente.
                    chiave, etichetta = self._fornitore_della_voce(file_entry)
                    if chiave:
                        fornitori_senza_documento[chiave] = etichetta
                    continue
                if profile is not None:
                    file_entry["deletable"] = True
                    file_entry["uploadName"] = name
            # I confronti creati prima dell'importazione guidata possono
            # puntare ancora all'originale scelto dall'utente, fuori dalla
            # cartella degli upload. La pagina deve poterlo rimuovere dalla
            # propria lista senza cancellare quel file dal disco.
            if str(file_entry.get("role") or "").casefold() in {"supplier", "master"}:
                file_entry["deletable"] = True
                file_entry["uploadName"] = str(file_entry.get("name") or "")
            visible_files.append(file_entry)
        review["files"] = visible_files
        self._spegni_i_fornitori_senza_documento(review, fornitori_senza_documento)
        existing_names = {str(item.get("name") or "").casefold() for item in review.get("files") or []}
        for name, profile in safe_profiles.items():
            if name.casefold() in existing_names:
                continue
            hint = profile.get("deterministic_hint") or {}
            details = profile.get("details") or {}
            ruolo_scelto = profiled_upload_role(profile)
            gestionale = ruolo_scelto == "master"
            rows = details.get("active_range", {}).get("nonempty_rows")
            if details.get("format") == "xlsx":
                rows = sum(sheet.get("active_range", {}).get("nonempty_rows", 0) for sheet in details.get("sheets") or [])
            review.setdefault("files", []).append({
                "id": profile.get("profile_id"),
                "name": name,
                "kind": "Gestionale" if gestionale else "Documento acquisito",
                "supplier": "Gestionale" if gestionale else "Da confermare",
                "role": ruolo_scelto or None,
                "status": "review",
                "schemaState": hint.get("state") or "AMBIGUO",
                "rows": rows or 0,
                # ⚠ Qui c'era scritto «serve la verifica preliminare di Codex».
                # Oltre a nominare uno strumento che chi usa il programma non
                # conosce, dal cantiere R6 e' anche **falsa**: le colonne le
                # riconosce il ricalcolo, e quando non ci riesce le chiede in
                # pagina.  Diceva all'utente di aspettare un permesso che
                # nessuno deve piu' dare.
                "message": "Letto. Le colonne verranno riconosciute al prossimo confronto; se non le riconosce, te le chiede.",
                "sourcePath": str((self.upload_dir / name).resolve()),
                "deletable": True,
                "uploadName": name,
            })
        try:
            self.catalog.enrich_review(review)
        except Exception as exc:
            review.setdefault("warnings", []).append({
                "code": "CATALOGO_NON_AGGIORNATO",
                "severity": "warning",
                "blocking": False,
                "title": "La ricerca nei listini può non trovare tutto",
                "message": (
                    "Il confronto dei prezzi resta buono. Quello che può mancare è "
                    "«+ Aggiungi un prodotto», che cerca nei listini la merce che non è "
                    f"nell'elenco del gestionale. Il motivo, per chi ripara: {exc}"
                ),
            })
        # Un fornitore che non si e' lasciato leggere non ferma piu' gli altri:
        # proprio per questo va detto, altrimenti sparisce dalla ricerca
        # prodotti senza che niente lo segnali.
        for failure in self.catalog.load_errors:
            review.setdefault("warnings", []).append({
                "code": "CATALOGO_FORNITORE_NON_LETTO",
                "supplier": failure.get("supplier"),
                "severity": "warning",
                "blocking": False,
                "title": f"Listino {failure.get('supplierName') or 'fornitore'} non letto",
                "message": (
                    f"{frase(failure.get('message')) or 'Lettura non riuscita.'} "
                    "Gli altri fornitori restano nel confronto; questo non compare nella ricerca "
                    "prodotti. Per rimetterlo dentro, ricarica il listino dal passo 1 «Importa i dati»."
                ),
            })
        return review

    def review(self) -> dict[str, Any]:
        with self.lock:
            review, state = self.review_with_manual_products()
            decisions = {str(item.get("id")): item for item in state.get("products") or [] if item.get("id") is not None}
            riallineati: list[str] = []
            for product in review.get("products") or []:
                decision = decisions.get(str(product.get("id")))
                if not decision:
                    product["excluded"] = False
                else:
                    product["quantity"] = decision.get("quantity", product.get("quantity", 0))
                    # ⚠ Il fornitore lo rimette la decisione salvata **solo se
                    # l'ha scelto l'utente**. Se invece era il default — il più
                    # conveniente del confronto di allora — vince quello che il
                    # confronto ha appena calcolato, cioè il più conveniente di
                    # ADESSO.
                    #
                    # Senza questa distinzione un default vecchio si travestiva
                    # da scelta dell'utente e sopravviveva a un ricalcolo che lo
                    # rendeva sbagliato: si aggiungeva un listino a metà lavoro,
                    # il fornitore nuovo costava meno, la pagina lo mostrava, e
                    # l'ordine continuava ad andare all'altro senza che niente
                    # lo dicesse. Il solo modo di rimetterlo a posto era mettere
                    # uno sconto e toglierlo, perché `set_supplier_discount` era
                    # l'unico punto che rifaceva la scelta.
                    #
                    # ⚠ Le decisioni salvate prima del 26 agosto 2026 non
                    # portano il marchio: valgono automatiche, e quelle che
                    # cambiano si contano qui sotto perché la pagina possa dirlo.
                    salvato = str(decision.get("selectedSupplierId") or "")
                    # ⚠ Un solo `if/else`, e nessuna uscita anticipata: la
                    # prima versione usciva di qui con un `continue` e saltava
                    # il ripristino di `quantitySource` qui sotto. Un prodotto
                    # con fornitore scelto a mano **e** quantità scritta a mano
                    # tornava a dichiarare «valore dal gestionale», e il comando
                    # che azzera le sole quantità predefinite se la portava via.
                    if str(decision.get("selectedSupplierSource") or "") == "utente":
                        product["selectedSupplierId"] = decision.get("selectedSupplierId", product.get("selectedSupplierId"))
                        product["confirmed"] = bool(decision.get("confirmed"))
                    else:
                        # ⚠ Si RICALCOLA qui, e non si prende `selectedSupplierId`
                        # dal confronto: quello lo ha scritto la catena, che gli
                        # sconti di testata non li conosce. `review_with_manual_products`
                        # li ha appena applicati ai prezzi, quindi il più conveniente
                        # è quello che si vede in pagina — ed è l'unico numero su cui
                        # ha senso rifare la scelta. Prendendo il valore della catena
                        # si buttava via la riassegnazione dello sconto.
                        migliore = self._offerta_piu_conveniente(product)
                        product["selectedSupplierId"] = str(migliore.get("supplierId") or "") if migliore else ""
                        if str(product.get("selectedSupplierId") or "") == salvato:
                            product["confirmed"] = bool(decision.get("confirmed"))
                        else:
                            # Cambiando fornitore cambia l'articolo: la conferma
                            # di prima non copre questo, come dopo uno spostamento.
                            product["confirmed"] = False
                            riallineati.append(str(product.get("name") or product.get("id") or ""))
                    product["excluded"] = bool(decision.get("excluded"))
                    # Senza questo ripristino un prodotto già modificato dall'utente tornerebbe
                    # "valore dal gestionale" dopo un ricaricamento, e verrebbe azzerato per errore
                    # dal comando che riporta a zero le sole quantità predefinite.
                    if decision.get("quantitySource") in {"gestionale", "utente"}:
                        product["quantitySource"] = decision["quantitySource"]
                self.dichiara_la_conferma(product)
            # ⚠ Quando il posto cambia sotto gli occhi, la frase che lo spiega
            # deve arrivare col cambiamento. Qui l'ordine di alcuni prodotti si
            # sposta da solo su un altro fornitore, ed e' giusto — costa meno —
            # ma va detto: se fra quelli c'e' un fornitore che l'utente teneva
            # per una ragione sua, questo e' l'unico momento in cui puo'
            # accorgersene e rimetterlo.
            # ⚠ Solo se le decisioni parlano di QUESTO confronto. Con una run
            # nuova sono di un'altra settimana: lì il fornitore non "è passato"
            # a nessuno — il confronto è un altro — e l'avviso sarebbe rumore
            # su una cosa che il programma ha fatto giusta.
            stessa_run = str(state.get("runId") or "") == str((review.get("run") or {}).get("id") or "")
            if riallineati and stessa_run:
                quanti = len(riallineati)
                esempi = ", ".join(f"«{nome}»" for nome in riallineati[:3] if nome)
                coda = f" ({esempi}{', e altri' if quanti > 3 else ''})" if esempi else ""
                review.setdefault("warnings", []).append({
                    "code": "FORNITORE_PIU_CONVENIENTE_RIPRESO",
                    "severity": "warning",
                    "blocking": False,
                    "title": (
                        f"{quanti} prodotti sono passati al fornitore più conveniente"
                        if quanti > 1 else
                        "Un prodotto è passato al fornitore più conveniente"
                    ),
                    "message": (
                        f"Il confronto è cambiato da quando li avevi salvati, e su {'questi' if quanti > 1 else 'questo'} "
                        f"adesso costa meno un altro fornitore{coda}. Il fornitore che avevi scelto tu a mano "
                        "non è stato toccato: si sono spostati solo quelli che il programma aveva scelto da sé. "
                        "Se uno di questi lo tenevi apposta, riscegli il fornitore dalla riga del prodotto."
                    ),
                    "count": quanti,
                })
            try:
                review = self.promotion_service.decorate(review, decisions)
            except Exception as exc:
                # Le promozioni sono un di piu': se il loro motore si rompe,
                # l'utente deve poter vedere lo stesso prezzi, quantita' e
                # fornitori. Prima bastava un listino Larice su un percorso
                # non raggiungibile perche' la pagina non si aprisse.
                print(f"[AVVISO] promozioni non calcolate — {type(exc).__name__}: {exc}")
                review.setdefault("warnings", []).append({
                    "code": "PROMOZIONI_NON_CALCOLATE",
                    "severity": "warning",
                    "blocking": False,
                    "title": "Non ho letto le offerte dei fornitori",
                    "message": (
                        "Prezzi, quantità e confronto restano buoni. Quello che manca è "
                        "quanto devi comprare per avere gli omaggi: quel conto non c'è. "
                        f"{frase(exc) or 'Il guasto non si è descritto.'}"
                    ),
                })
            # Un fornitore gia' segnalato dal catalogo non prende un secondo
            # avviso: e' lo stesso file e lo stesso guasto. Ma il primo avviso
            # deve dire anche questa perdita, altrimenti le soglie con omaggio
            # spariscono senza che una sola parola lo dica.
            per_fornitore = {
                str(item.get("supplier") or ""): item
                for item in review.get("warnings") or []
                if item.get("supplier")
            }
            for failure in self.promotion_service.load_errors:
                esistente = per_fornitore.get(str(failure.get("supplier") or ""))
                if esistente is not None:
                    esistente["message"] = frase(esistente.get("message")) + " Mancano anche le sue soglie con omaggio."
                    continue
                review.setdefault("warnings", []).append({
                    "code": "CONDIZIONI_COMMERCIALI_NON_LETTE",
                    "supplier": failure.get("supplier"),
                    "severity": "warning",
                    "blocking": False,
                    "title": f"Offerte {failure.get('supplierName') or 'fornitore'} non lette",
                    "message": (
                        f"{frase(failure.get('message')) or 'Lettura non riuscita.'} "
                        "Prezzi e confronto restano validi; mancano le soglie con omaggio di questo fornitore."
                    ),
                })
            # ⚠ Il magazzino delle conferme che non si apre non si tace: senza
            # una parola le domande gia' risposte tornerebbero tutte, e l'utente
            # le rifarebbe a mano credendo che il programma non avesse mai
            # saputo niente. Il resto del confronto resta valido.
            if self._conferme_guasto:
                review.setdefault("warnings", []).append({
                    "code": "CONFERME_NON_DISPONIBILI",
                    "severity": "warning",
                    "blocking": False,
                    "title": "Le conferme già date non sono disponibili",
                    "message": (
                        f"{self._conferme_guasto} Prezzi, quantità e fornitori restano validi: "
                        "quello che manca è la memoria delle corrispondenze già confermate, "
                        "quindi le domande possono tornare anche se avevi già risposto."
                    ),
                })
            self.decorate_pending_orders(review)
            # Il Riepilogo ha i suoi totali gia' alla prima apertura, senza
            # aspettare un salvataggio: il browser non li ricalcola per conto
            # suo e lo scarto di arrotondamento e' dichiarato subito.
            review["orderSummary"] = self.order_summary_from_state(review, state)
            review["state"] = {
                "currentStep": max(1, min(3, int(number(state.get("currentStep")) or 1))),
                "acceptBelowThreshold": bool(state.get("acceptBelowThreshold")),
                "summaryGrouping": state.get("summaryGrouping") if state.get("summaryGrouping") in {"supplier", "product"} else "supplier",
                # La versione da cui questa scheda parte: la rimanda a ogni
                # salvataggio, ed e' cosi' che il servizio si accorge che nel
                # frattempo ha salvato un'altra scheda.
                "stateVersion": int(number(state.get("stateVersion")) or 0),
                # Gli sconti di testata, in percentuale, solo per mostrarli nel
                # campo: i prezzi che la pagina ha in mano sono gia' scontati e
                # non deve rifare il conto.
                "supplierDiscounts": {
                    chiave: round(valore * 100, 4)
                    for chiave, valore in self.sconti_del_confronto(state, review).items()
                },
            }
            # ⚠ Le uguaglianze fra codici **non** viaggiano piu' col confronto.
            # Ci stavano perche' dovevano essere visibili senza cercarle, ma il
            # posto era la finestra «Sfoglia i listini», cioe' dentro un
            # prodotto: per rileggerle bisognava aprire un prodotto qualunque,
            # e l'elenco diceva due numeri di tredici cifre senza i nomi —
            # l'informazione che serve a giudicare se la dichiarazione e'
            # giusta non c'era. Adesso stanno in Impostazioni, con i nomi e una
            # ricerca, e hanno la loro rotta (`GET /api/matches/uguaglianze`):
            # quella pagina si apre anche quando un confronto non c'e'.
            return review

    def read_order_history(self) -> dict[str, Any]:
        """Legge lo storico applicando la scadenza e risalvandolo se è cambiato."""

        with self.lock:
            history, changed = order_history.read_history(self.history_path)
            if changed:
                try:
                    order_history.save_history(self.history_path, history)
                except OSError as exc:
                    # La scadenza è solo pulizia: se non si riesce a salvare,
                    # l'elenco resta corretto per questa lettura.
                    print(f"[AVVISO] Storico ordini non aggiornato su disco: {exc}")
            return history

    @staticmethod
    def avviso_ordini_scaduti(scaduti: list[dict[str, Any]]) -> dict[str, Any]:
        """La frase che dice quali ordini sono scaduti, senza mentire su chi ha risposto.

        Sessanta giorni dopo l'ultima interazione la domanda non viene più
        fatta: se lo si tace, la merce non arrivata esce di scena senza che
        nessuno se ne accorga.  «Senza risposta» si dice SOLO di chi non ha
        mai risposto: un ordine risposto «non ancora arrivata» otto volte non
        va trattato come uno ignorato (revisione avversariale R4).
        """

        frasi = []
        for voce in scaduti:
            momento = order_history.parse_moment(voce.get("createdAt"))
            # M-5: senza la data di creazione non si dichiara un conteggio mai
            # fatto — lo si dice.
            quando = (f" del {consegna.data_leggibile(momento.astimezone())}" if momento
                      else " (la voce non porta la data di creazione)")
            risposta = order_history.parse_moment(voce.get("answeredAt"))
            esito = (f", l'ultima risposta è del {consegna.data_leggibile(risposta.astimezone())}"
                     if risposta else ", mai risposto")
            frasi.append(f"l'ordine {voce.get('supplierName') or voce.get('supplier')}{quando}{esito}")
        elenco = "; ".join(frasi)
        return {
            "code": "ORDINI_SCADUTI_SENZA_RISPOSTA",
            "severity": "warning",
            "blocking": False,
            "title": "Ordini di cui non chiedo più notizie",
            "message": (
                f"Di {elenco} non ti chiedo più se la merce è arrivata: sono passati "
                f"{order_history.EXPIRY_DAYS} giorni da quando ne abbiamo parlato "
                "l'ultima volta. Se non è mai arrivata, va riordinata."
            ),
            "count": len(scaduti),
            "orders": scaduti,
        }

    def decorate_pending_orders(self, review: dict[str, Any]) -> None:
        """Segnala su ogni prodotto gli ordini non ancora ricevuti.

        L'abbinamento avviene per identità dell'articolo: l'EAN quando c'è,
        altrimenti l'identificativo stabile del prodotto (gli espositori, che
        un EAN non ce l'hanno).  Gli identificativi product:<riga> restano
        fuori: dipendono dalla posizione nell'esportazione settimanale e
        confrontarli produrrebbe corrispondenze sbagliate senza avvisare.
        """

        products = review.get("products") or []
        try:
            history = self.read_order_history()
        except Exception as exc:
            for product in products:
                product["pendingOrders"] = []
            review.setdefault("warnings", []).append({
                "code": "STORICO_ORDINI_NON_LEGGIBILE",
                "severity": "warning",
                "blocking": False,
                "title": "Storico degli ordini non disponibile",
                "message": f"Il confronto resta utilizzabile, ma non posso ricordare la merce ordinata e non ancora ricevuta. Dettaglio: {exc}",
            })
            return
        order_history.attach_pending_orders(
            products,
            history,
            exclude_run_id=str((review.get("run") or {}).get("id") or ""),
        )
        scaduti = order_history.expired_orders(history)
        if scaduti:
            review.setdefault("warnings", []).append(self.avviso_ordini_scaduti(scaduti))

    def current_run_id(self) -> str:
        """Identificativo della run aperta: i suoi ordini non si autosegnalano."""

        try:
            review = load_json(self.review_path, {})
        except Exception:
            return ""
        return str(((review or {}).get("run") or {}).get("id") or "")

    def history_pending(self) -> dict[str, Any]:
        with self.lock:
            history = self.read_order_history()
            return {
                "ok": True,
                "pending": order_history.pending_summary(history, exclude_run_id=self.current_run_id()),
            }

    def answer_history_order(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Risposta non valida")
        order_id = str(payload.get("orderId") or "").strip()
        if not order_id:
            raise ValueError("Ordine non indicato")
        # «Non arriverà più» chiude la domanda per sempre: e' la terza risposta,
        # quella che serve quando la merce non arrivera' mai e segnarla
        # ricevuta sarebbe scrivere il falso.
        closed = payload.get("closed") is True
        received = payload.get("received")
        if not closed and not isinstance(received, bool):
            raise ValueError("Indicare se la merce è stata ricevuta: sì oppure no")
        with self.lock:
            history = self.read_order_history()
            registrata = (
                order_history.close_order(history, order_id)
                if closed
                else order_history.answer_order(history, order_id, bool(received))
            )
            if registrata is None:
                raise ValueError("Ordine non presente nello storico")
            order_history.save_history(self.history_path, history)
            return {
                "ok": True,
                "pending": order_history.pending_summary(history, exclude_run_id=self.current_run_id()),
            }

    def answer_rejected_candidate(self, payload: Any) -> dict[str, Any]:
        """Accetta o rifiuta una riga proposta, soltanto nella run che l'ha generata."""

        if not isinstance(payload, dict):
            raise ValueError("Risposta non valida")
        run_id = str(payload.get("runId") or "").strip()
        product_id = str(payload.get("productId") or "").strip()
        supplier_id = str(payload.get("supplierId") or "").strip()
        candidate_key = str(payload.get("candidateKey") or "").strip()
        accepted = payload.get("accepted")
        if not all((run_id, product_id, supplier_id, candidate_key)) or not isinstance(accepted, bool):
            raise ValueError("Indicare la proposta e scegliere sì oppure no")

        with self.lock:
            review, state = self.review_with_manual_products()
            expected_run = str((review.get("run") or {}).get("id") or "")
            if run_id != expected_run:
                raise ValueError("Il confronto è cambiato: ricarica la pagina prima di rispondere")
            saved_run = str(state.get("runId") or "")
            if saved_run and saved_run != run_id:
                raise ValueError("Le scelte salvate appartengono a un altro confronto: ricarica la pagina")

            product = next(
                (item for item in review.get("products") or [] if str(item.get("id") or "") == product_id),
                None,
            )
            offer = next(
                (
                    item for item in (product or {}).get("offers") or []
                    if str(item.get("supplierId") or "") == supplier_id
                ),
                None,
            )
            candidate = (offer or {}).get("rejectedCandidate")
            if not isinstance(candidate, dict) or str(candidate.get("candidateKey") or "") != candidate_key:
                raise ValueError("Questa proposta non è più disponibile: ricarica il confronto")
            if accepted and candidate.get("available") is not True:
                raise ValueError("La riga proposta non ha prezzo e confezione utilizzabili")

            overrides = [
                item for item in state.get("matchOverrides") or []
                if isinstance(item, dict)
                and not (
                    str(item.get("productId") or "") == product_id
                    and str(item.get("supplierId") or "") == supplier_id
                )
            ]
            overrides.append({
                "runId": run_id,
                "productId": product_id,
                "supplierId": supplier_id,
                "candidateKey": candidate_key,
                "accepted": accepted,
                "answeredAt": datetime.now(tz=timezone.utc).isoformat(),
            })
            state["schemaVersion"] = int(number(state.get("schemaVersion")) or 1)
            state["runId"] = run_id
            state["matchOverrides"] = overrides
            state["updatedAt"] = datetime.now(tz=timezone.utc).isoformat()
            atomic_json(self.state_path, state)
            return {
                "ok": True,
                "message": "Prodotto collegato al fornitore." if accepted else "Proposta esclusa.",
            }

    def _avanza_la_versione(self, state: dict[str, Any], origine: str) -> int:
        """Scrive lo stato con la versione avanzata di uno, e la restituisce.

        ⚠ Il numero **va restituito a chi ha chiesto la modifica**, e non è una
        cortesia: la scheda dichiara a ogni salvataggio la versione da cui è
        partita, e se sul disco ce n'è una più nuova il salvataggio viene
        rifiutato.  Chi avanza la versione qui dentro e non la manda indietro
        lascia la scheda indietro di uno, e il salvataggio successivo — fatto
        dalla STESSA scheda — si sente rispondere «un'altra scheda ha salvato
        dopo di te».  È il difetto del 26 agosto 2026: rifiutare una conferma
        di corrispondenza ogni due o tre, mandando a cercare una scheda che non
        esiste.  Tre risposte su cinque se n'erano dimenticate.
        """

        versione = int(number(state.get("stateVersion")) or 0) + 1
        state["stateVersion"] = versione
        state["stateVersionOrigin"] = origine
        atomic_json(self.state_path, state)
        return versione

    def abbina_riga_di_listino(self, payload: Any) -> dict[str, Any]:
        """«Questa riga del listino è il mio prodotto»: un clic, due effetti.

        **Subito**: l'offerta entra nel confronto di adesso, come quando si
        accetta la proposta dell'analisi automatica.

        **Per sempre**: si registra che i due codici a barre sono lo stesso
        articolo. Ricordare «riga 4794 di NOCE» varrebbe una settimana — al
        listino nuovo la riga si sposta; l'uguaglianza fra codici vale sempre e
        vale **per tutti i fornitori insieme**, e trasforma il prodotto in un
        `EAN_ESATTO` nativo al prossimo ricalcolo.

        ⚠ Il secondo effetto non è sempre possibile: 6 prodotti su 457 non hanno
        EAN nel gestionale, e gli espositori LARICE non ce l'hanno a listino.
        Lì l'abbinamento vale per questo confronto e basta, e la risposta lo
        dice — lasciarlo sembrare uguale sarebbe una promessa che salta al primo
        ricalcolo.
        """

        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida")
        product_id = str(payload.get("productId") or "").strip()
        supplier_id = str(payload.get("supplierId") or "").strip().casefold()
        source_row = payload.get("sourceRow")
        if not product_id or not supplier_id or source_row in (None, ""):
            raise ValueError("Indicare il prodotto, il fornitore e la riga del listino")

        with self.lock:
            review, state = self.review_with_manual_products()
            run_id = str((review.get("run") or {}).get("id") or "")
            # ⚠ `productId` e `sourceRow` sono posizionali: da una scheda ferma
            # al confronto della settimana prima indicano, sul confronto nuovo,
            # due articoli qualsiasi — e li dichiarano uguali per sempre e per
            # tutti i fornitori. Stesso controllo e stessa frase del rifiuto:
            # le due rotte rispondono alla stessa domanda (revisione del 6
            # settembre 2026). Senza `runId` si passa come prima: una pagina
            # vecchia rimasta in cache non deve rompersi.
            dichiarata = str(payload.get("runId") or "").strip()
            if dichiarata and run_id and dichiarata != run_id:
                raise ValueError("Il confronto è cambiato: ricarica la pagina prima di rispondere")
            product = next(
                (item for item in review.get("products") or [] if str(item.get("id") or "") == product_id),
                None,
            )
            if product is None:
                raise ValueError("Questo prodotto non è nel confronto: ricarica la pagina")
            if not any(str(voce.get("id") or "").casefold() == supplier_id for voce in review.get("suppliers") or []):
                raise ValueError("Questo fornitore non è nel confronto")
            record, offerta = self.catalog.offerta_dalla_riga(review, supplier_id, source_row)

            abbinamenti = [
                voce for voce in state.get("manualMatches") or []
                if isinstance(voce, dict)
                and not (
                    str(voce.get("productId") or "") == product_id
                    and str(voce.get("supplierId") or "") == supplier_id
                )
            ]
            abbinamenti.append({
                "runId": run_id,
                "productId": product_id,
                "supplierId": supplier_id,
                "sourceRow": record.get("source_row"),
                "chosenAt": datetime.now(tz=timezone.utc).isoformat(),
            })
            state["manualMatches"] = abbinamenti
            # ⚠ Un «no» dato prima all'analisi automatica su questo fornitore
            # non deve tornare a spegnere l'offerta appena scelta: sono due
            # risposte umane sullo stesso prodotto, e vince la più recente.
            # Si toglie, invece di lasciarle convivere e scoprire dopo quale
            # delle due ha vinto.
            rifiuti_tolti = 0
            rimasti = []
            for voce in state.get("matchOverrides") or []:
                if (
                    isinstance(voce, dict)
                    and str(voce.get("productId") or "") == product_id
                    and str(voce.get("supplierId") or "") == supplier_id
                ):
                    rifiuti_tolti += 1
                    continue
                rimasti.append(voce)
            state["matchOverrides"] = rimasti

            state["runId"] = run_id
            state["schemaVersion"] = int(number(state.get("schemaVersion")) or 1)
            state["updatedAt"] = datetime.now(tz=timezone.utc).isoformat()
            versione = self._avanza_la_versione(state, "abbinamento")

            # ⚠ Anche un «non è lo stesso articolo» detto prima su questo
            # fornitore va tolto: sono due risposte umane sullo stesso prodotto,
            # e vince la piu' recente. Serve nel caso preciso in cui l'utente
            # sfoglia il listino e sceglie a mano PROPRIO la riga che aveva
            # rifiutato: l'impronta e' la stessa, e senza questa riga il no la
            # rispegnerebbe subito, in silenzio.
            #
            # Solo se quello in vigore e' un no: `dimentica` non distingue, e
            # chiuderebbe anche un si' che nessuno ha revocato.
            articolo_del_prodotto = impronta_prodotto(product)
            magazzino_dei_no = self.magazzino_conferme()
            if magazzino_dei_no is not None and articolo_del_prodotto:
                try:
                    in_vigore = magazzino_dei_no.cerca(supplier_id, articolo_del_prodotto)
                    if in_vigore is not None and not in_vigore.get("accettata"):
                        magazzino_dei_no.dimentica(
                            supplier_id, articolo_del_prodotto,
                            quando=datetime.now(tz=timezone.utc).isoformat(),
                        )
                except MagazzinoNonUtilizzabile as exc:
                    # L'abbinamento di oggi resta valido: si annota e lo dice il
                    # confronto, come per l'uguaglianza qui sotto.
                    self._conferme_guasto = frase(exc) or "Il rifiuto precedente non è stato tolto."

            ricordata, motivo = self._ricorda_l_uguaglianza(product, record, supplier_id)
            return {
                "ok": True,
                "stateVersion": versione,
                "productId": product_id,
                "supplierId": supplier_id,
                "sourceRow": record.get("source_row"),
                "offerta": offerta,
                "uguaglianzaRicordata": ricordata,
                "message": motivo,
                "rifiutiTolti": rifiuti_tolti,
            }

    def _ricorda_l_uguaglianza(
        self, product: Any, record: Any, supplier_id: str
    ) -> tuple[bool, str]:
        """Registra che i due codici a barre sono lo stesso articolo.

        Restituisce `(ricordata, frase da mostrare)`. Non solleva mai: se il
        magazzino non si apre l'abbinamento di oggi resta valido lo stesso, e
        quello che si perde — che valga anche la settimana prossima — va detto,
        non nascosto dietro un successo.
        """

        codice_prodotto = codice_confrontabile((product or {}).get("ean"))
        codice_riga = codice_confrontabile((record or {}).get("ean"))
        nome = str((record or {}).get("description") or "").strip()
        if not codice_prodotto or not codice_riga:
            manca = "il prodotto" if not codice_prodotto else "la riga del listino"
            return False, (
                f"Abbinato per questo confronto. Non vale per i prossimi: {manca} non ha un "
                "codice a barre, e senza non c'è niente da dichiarare uguale."
            )
        if codice_prodotto == codice_riga:
            return False, "Abbinato: i due codici a barre erano già lo stesso."
        magazzino = self.magazzino_conferme()
        if magazzino is None:
            return False, (
                "Abbinato per questo confronto. Non vale per i prossimi: "
                + (self._conferme_guasto or "il file delle conferme non si apre.")
            )
        try:
            magazzino.unisci(
                codice_prodotto,
                codice_riga,
                articolo=impronta_prodotto(product),
                offerta=impronta_articolo({**(record or {}), "supplierId": supplier_id}),
                motivo=f"scelta a mano dal listino {supplier_label(supplier_id)}, riga {record.get('source_row')}",
                quando=datetime.now(tz=timezone.utc).isoformat(),
            )
        except (MagazzinoNonUtilizzabile, ValueError) as exc:
            return False, f"Abbinato per questo confronto. Non vale per i prossimi: {frase(exc)}"
        return True, (
            f"Abbinato a «{nome}». D'ora in poi {codice_prodotto} e {codice_riga} valgono come lo "
            "stesso articolo, per tutti i fornitori."
        )

    def rifiuta_l_abbinamento(self, payload: Any) -> dict[str, Any]:
        """«Non e' lo stesso articolo», e il ritorno indietro.

        La risposta che mancava. Finche' non c'era, chi non poteva confermare un
        abbinamento proposto non aveva **nessuna** uscita: confermare ordina
        l'articolo sbagliato, non confermare lascia la compilazione ferma su
        «Conferma richiesta · bloccante», e «Escludi dall'ordine» azzera la
        quantita' — quindi il prodotto sparisce anche dall'elenco «Prodotti da
        reperire», che salta chi ha quantita' zero. Misurato il 21 agosto 2026
        su `conferme.db`: venti conferme, di cui **zero** negative, e undici
        scritte in trenta secondi.

        Si scrive nel magazzino delle conferme, come riga con `accettata` falsa:
        `MagazzinoConferme.ricorda` la sa scrivere dal primo giorno. **Non nasce
        nessuna memoria nuova**, e nessuna chiave nuova in `state.json` — che sta
        sotto `current/` e muore col ricalcolo, mentre questa risposta deve
        sopravvivergli: la proposta semantica torna ogni settimana, e un no che
        scade ogni lunedi' rimette l'utente nello stesso vicolo cieco sette
        giorni dopo.

        Il no vale per la coppia (fornitore, articolo del gestionale), ancorato
        all'impronta della riga di listino su cui e' stato detto. **Non** e'
        un'uguaglianza negata: quella varrebbe per tutti i fornitori e per
        sempre, e il fatto che la riga di LARICE non sia il mio articolo non dice
        niente su che cosa hanno gli altri.

        Dello stato si tocca solo la decisione di quel prodotto: il fornitore
        scelto si svuota e la conferma cade, **la quantita' no**. E' il numero
        che serve a reperirlo altrove.
        """

        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida")
        product_id = str(payload.get("productId") or "").strip()
        supplier_id = str(payload.get("supplierId") or "").strip().casefold()
        rifiutata = payload.get("rifiutata")
        if not product_id or not supplier_id or not isinstance(rifiutata, bool):
            raise ValueError("Indicare il prodotto, il fornitore e la risposta")

        with self.lock:
            review, state = self.review_with_manual_products()
            run_id = str((review.get("run") or {}).get("id") or "")
            dichiarata = str(payload.get("runId") or "").strip()
            if dichiarata and run_id and dichiarata != run_id:
                raise ValueError("Il confronto è cambiato: ricarica la pagina prima di rispondere")
            product = next(
                (item for item in review.get("products") or [] if str(item.get("id") or "") == product_id),
                None,
            )
            if product is None:
                raise ValueError("Questo prodotto non è nel confronto: ricarica la pagina")
            # ⚠ `find_offer` sul confronto GIA' decorato: se il no c'e' gia',
            # l'offerta e' qui con `available` falsa e la si ritrova lo stesso.
            # E' il modo in cui il ritorno indietro funziona.
            offerta = find_offer(product, supplier_id)
            if offerta is None:
                raise ValueError("Questo fornitore non ha nessuna riga per questo prodotto")
            articolo = impronta_prodotto(product)
            impronta = impronta_articolo(offerta)
            # Le impronte PRIMA di aprire il file, come in `conferma_in_vigore`:
            # una riga che non si identifica non si puo' ricordare, e aprire il
            # magazzino per scoprirlo creerebbe un `conferme.db` a chi non ne ha
            # nessuno.
            if not articolo or not impronta:
                raise ValueError(
                    "Questa riga non si può rifiutare: prodotto o riga del listino non hanno "
                    "né codice a barre né nome, e una risposta agganciata al vuoto tornerebbe "
                    "su merce a caso"
                )
            magazzino = self.magazzino_conferme()
            if magazzino is None:
                raise ValueError(self._conferme_guasto or "Il file delle conferme non si apre.")
            quando = datetime.now(tz=timezone.utc).isoformat()
            nome_fornitore = supplier_label(supplier_id)
            try:
                if rifiutata:
                    magazzino.ricorda(
                        fornitore=supplier_id,
                        articolo=articolo,
                        offerta=impronta,
                        accettata=False,
                        motivo="non è lo stesso articolo",
                        quando=quando,
                    )
                    # ⚠ Questo messaggio e' un avviso che sparisce in 3,6
                    # secondi (`showToast`), e diceva per intero la regola di
                    # quanto dura un no — 172 caratteri, la stessa frase che sta
                    # gia' scritta e ferma nel riquadro del fornitore rifiutato,
                    # e che stava anche sotto il pulsante prima di premerlo: tre
                    # copie della stessa cosa, e nessuna diceva quello che serve
                    # subito. Un avviso che passa conferma il gesto; la regola
                    # resta dov'e' scritta e non scappa.
                    messaggio = f"{nome_fornitore} esce da questo prodotto. La quantità resta."
                else:
                    # ⚠ Si toglie solo se quello in vigore e' un NO. `dimentica`
                    # non distingue: chiamata alla cieca chiuderebbe anche un si'
                    # che nessuno ha revocato.
                    voce = magazzino.cerca(supplier_id, articolo)
                    if voce is not None and not voce.get("accettata"):
                        magazzino.dimentica(supplier_id, articolo, quando=quando)
                    messaggio = (
                        f"{nome_fornitore} torna fra i fornitori di questo prodotto: "
                        "la domanda è di nuovo aperta."
                    )
            except (MagazzinoNonUtilizzabile, ValueError) as exc:
                # ⚠ Qui si SOLLEVA, al contrario di `ricorda_le_conferme` che
                # annota e prosegue. La' il salvataggio delle quantita' non deve
                # morire per una memoria; qui la memoria E' la risposta, e un no
                # che si crede dato e non lo e' rimanda l'utente esattamente nel
                # vicolo cieco da cui stava uscendo, senza dirglielo.
                raise ValueError(f"La risposta non è stata registrata: {frase(exc)}") from exc

            # Lo stato: solo la decisione di questo prodotto, e solo su chiavi
            # che `validate_snapshot` ricostruisce comunque dallo snapshot della
            # scheda. ⚠ Niente chiavi nuove, quindi `CHIAVI_DI_STATO_RICOPIATE`
            # non si tocca.
            voci: list[Any] = []
            cambiato = False
            for voce in state.get("products") or []:
                if (
                    rifiutata
                    and isinstance(voce, dict)
                    and str(voce.get("id") or "") == product_id
                    and str(voce.get("selectedSupplierId") or "").strip().casefold() == supplier_id
                ):
                    ripulita = {**voce, "selectedSupplierId": "", "confirmed": False}
                    ripulita.pop("confirmedArticle", None)
                    cambiato = True
                    voci.append(ripulita)
                    continue
                voci.append(voce)
            # ⚠ La versione si restituisce anche quando non si e' scritto
            # niente: la scheda va allineata comunque, e una risposta che tace
            # la lascia a indovinare.
            versione = int(number(state.get("stateVersion")) or 0)
            if cambiato:
                state["products"] = voci
                state["runId"] = run_id
                state["schemaVersion"] = int(number(state.get("schemaVersion")) or 1)
                state["updatedAt"] = quando
                versione = self._avanza_la_versione(state, "rifiuto")
            return {
                "ok": True,
                "stateVersion": versione,
                "productId": product_id,
                "supplierId": supplier_id,
                "rifiutata": bool(rifiutata),
                "message": messaggio,
            }

    def togli_uguaglianza(self, payload: Any) -> dict[str, Any]:
        """Toglie una dichiarazione «questi due codici sono lo stesso articolo».

        Deve costare un clic: finché non è stata tolta, quella dichiarazione
        entra in **ogni** confronto futuro e su **tutti** i fornitori. La riga
        non si cancella — se ha già prodotto un ordine sbagliato è l'unica
        traccia che lo spiega — si chiude.
        """

        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida")
        codici = payload.get("codici")
        if not isinstance(codici, (list, tuple)) or len(codici) != 2:
            raise ValueError("Indicare i due codici da separare")
        magazzino = self.magazzino_conferme()
        if magazzino is None:
            raise ValueError(self._conferme_guasto or "Il file delle conferme non si apre.")
        tolta = magazzino.separa(
            codici[0], codici[1], quando=datetime.now(tz=timezone.utc).isoformat(),
        )
        return {
            "ok": True,
            "tolta": tolta,
            # L'elenco aggiornato torna gia' leggibile: la pagina che l'ha
            # chiesto e' quella che lo mostra, e farle fare una seconda chiamata
            # per riavere le stesse righe sarebbe un giro a vuoto.
            "uguaglianze": self.elenco_delle_uguaglianze()["uguaglianze"],
            "message": (
                "Dichiarazione tolta: dal prossimo confronto quei due codici tornano a essere "
                "due articoli diversi."
                if tolta
                else "Quella dichiarazione non era in vigore."
            ),
        }

    def _applica_abbinamenti_manuali(self, review: dict[str, Any], state: dict[str, Any]) -> None:
        """Le righe scelte a mano diventano offerte, in questo confronto.

        Vale solo per la run in cui la scelta è stata fatta: al ricalcolo il
        numero di riga non vuol più dire niente, e a tenere in piedi
        l'abbinamento c'è l'uguaglianza fra codici, che è un'altra cosa e sta
        nel magazzino delle conferme.
        """

        review_run = str((review.get("run") or {}).get("id") or "")
        scelte = {
            (str(voce.get("productId") or ""), str(voce.get("supplierId") or "")): voce
            for voce in state.get("manualMatches") or []
            if isinstance(voce, dict) and str(voce.get("runId") or "") == review_run
        }
        if not scelte:
            return
        for product in review.get("products") or []:
            product_id = str(product.get("id") or "")
            for offer in product.get("offers") or []:
                voce = scelte.get((product_id, str(offer.get("supplierId") or "").casefold()))
                if voce is None:
                    continue
                try:
                    _record, offerta = self.catalog.offerta_dalla_riga(
                        review, str(offer.get("supplierId") or ""), voce.get("sourceRow"),
                    )
                except ValueError:
                    # Il listino è cambiato sotto: l'offerta non si rimette, e il
                    # prodotto torna com'era. Non è un errore da fermare — la
                    # scelta resta scritta e tornerà utile al ricalcolo, che la
                    # rifà per codice invece che per numero di riga.
                    continue
                offer.update(deepcopy(offerta))
                offer["available"] = True
                offer["status"] = "SCELTO_A_MANO"
                offer["matchStatus"] = "SCELTO_A_MANO"
                offer["method"] = "SCELTA_UTENTE"
                offer["confidence"] = "UTENTE"
                offer["requiresConfirmation"] = False
                offer["confirmed"] = True
                offer["sceltaManuale"] = True

    def set_supplier_discount(self, payload: Any) -> dict[str, Any]:
        """Sconta tutte le offerte di un fornitore e riassegna al piu' conveniente.

        Con le parole di chi lo usa: «BETULLA potrebbe scontare tutto del 6%, devo
        poter scontare TUTTE LE OFFERTE di un fornitore… lo sconto lo mettero'
        dopo aver caricato i listini, subito, e verra' riassegnato al piu'
        conveniente senza dovermelo dire».  Quindi: si riassegna in silenzio, e
        si riassegna QUI — una volta, quando lo sconto cambia — e non a ogni
        lettura del confronto, che rifarebbe la scelta anche il giorno dopo,
        sopra le decisioni prese nel frattempo.
        """

        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida")
        supplier_id = str(payload.get("supplierId") or "").strip().casefold()
        if not supplier_id:
            raise ValueError("Indicare il fornitore")
        percento = number(payload.get("percent"))
        if percento is None or percento < 0 or percento >= 100:
            raise ValueError("Lo sconto è una percentuale fra 0 e 99")

        with self.lock:
            review, state = self.review_with_manual_products()
            run_id = str((review.get("run") or {}).get("id") or "")
            if not run_id:
                raise ValueError("Non c'è nessun confronto su cui applicare uno sconto")
            fornitori = {str(voce.get("id") or "").strip().casefold() for voce in review.get("suppliers") or []}
            if supplier_id not in fornitori:
                raise ValueError("Questo fornitore non è nel confronto")

            sconti = dict(state.get("supplierDiscounts") or {})
            if percento <= 0:
                sconti.pop(supplier_id, None)
            else:
                sconti[supplier_id] = {
                    "rate": round(float(percento) / 100, 6),
                    "runId": run_id,
                    "setAt": datetime.now(tz=timezone.utc).isoformat(),
                }
            state["supplierDiscounts"] = sconti
            state["schemaVersion"] = int(number(state.get("schemaVersion")) or 1)
            state["runId"] = run_id

            # I prezzi nuovi: si rilegge il confronto **con** lo sconto appena
            # deciso, perche' la riassegnazione va fatta sui prezzi che l'utente
            # avra' davanti, non su quelli di un attimo fa.
            #
            # ⚠ E lo si rilegge dalla porta di tutti, passandole lo stato che
            # abbiamo in mano — sul disco lo sconto non c'e' ancora, lo scrive
            # `_avanza_la_versione` in fondo. Prima qui il confronto si
            # ricostruiva a mano da `base_review()`: mancavano le risposte alle
            # proposte e le righe scelte a mano, e la riassegnazione avveniva su
            # offerte che in pagina non esistevano — l'ordine andava al
            # fornitore con la riga sbagliata (revisione del 6 settembre 2026).
            scontato, _ = self.review_with_manual_products(state)

            decisioni = {str(item.get("id")): item for item in state.get("products") or [] if isinstance(item, dict)}
            spostati = 0
            for product in scontato.get("products") or []:
                migliore = self._offerta_piu_conveniente(product)
                if migliore is None:
                    continue
                product_id = str(product.get("id") or "")
                decisione = decisioni.get(product_id)
                if decisione is None:
                    # Un prodotto senza decisione salvata veniva saltato in
                    # silenzio. Oggi il campo dello sconto non e' raggiungibile
                    # senza passare da un salvataggio che scrive una decisione
                    # per ogni prodotto del confronto, quindi il caso non si
                    # vede — ma e' un'invariante che nessuno impone: basta che
                    # un salvataggio fallisca perche' lo sconto riassegni a
                    # meta', senza una parola. La decisione si crea invece di
                    # saltarla.
                    #
                    # ⚠ `quantitySource` va copiato dal confronto, non
                    # inventato: e' lui a dire se la quantita' viene dal
                    # gestionale, e senza il ricalcolo successivo smette di
                    # rileggere i colli e la quantita' resta congelata
                    # (`pipeline_jobs._ripulisci_stato`).
                    origine = str(product.get("quantitySource") or "")
                    decisione = {
                        "id": product_id,
                        "quantity": int(number(product.get("quantity")) or 0),
                        "quantitySource": origine if origine in {"gestionale", "utente"} else "gestionale",
                        "excluded": bool(product.get("excluded")),
                        "confirmed": False,
                        "selectedSupplierId": str(product.get("selectedSupplierId") or ""),
                    }
                    decisioni[product_id] = decisione
                    state.setdefault("products", []).append(decisione)
                # Il fornitore scelto a mano non si tocca: lo sconto cambia i
                # prezzi, non le decisioni prese dall'utente. E' lo stesso
                # marchio che rispetta `review()` dal 26 agosto 2026 — qui non
                # veniva guardato, e uno sconto portava via un fornitore
                # scelto apposta (revisione del 6 settembre 2026).
                if str(decisione.get("selectedSupplierSource") or "") == "utente":
                    continue
                if str(decisione.get("selectedSupplierId") or "") == migliore["supplierId"]:
                    continue
                decisione["selectedSupplierId"] = migliore["supplierId"]
                # La conferma vale per l'articolo guardato: cambiando fornitore
                # cambia l'articolo, e quella di prima non copre questo.
                decisione["confirmed"] = not bool(migliore.get("requiresConfirmation"))
                decisione.pop("confirmedArticle", None)
                spostati += 1

            state["updatedAt"] = datetime.now(tz=timezone.utc).isoformat()
            versione = self._avanza_la_versione(state, "sconto")
            return {
                "ok": True,
                "stateVersion": versione,
                "supplierId": supplier_id,
                "percent": round(float(percento), 4),
                "reassigned": spostati,
            }

    @staticmethod
    def _offerta_piu_conveniente(product: Any) -> dict[str, Any] | None:
        """L'offerta disponibile col prezzo al pezzo piu' basso.

        Sul prezzo al pezzo, come tutto il resto del programma: e' la decisione
        commerciale da cui nasce il confronto.
        """

        migliori = [
            offer for offer in (product or {}).get("offers") or []
            if offer_is_available(offer)
            and number(offer.get("unitPriceNet")) is not None
        ]
        if not migliori:
            return None
        return min(migliori, key=lambda offer: number(offer.get("unitPriceNet")) or 0.0)

    def delete_compilation(self, payload: Any) -> dict[str, Any]:
        """Elimina una compilazione e tutte le sue voci nello storico ordini."""

        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida")
        name = str(payload.get("cartella") or "").strip()
        if not name:
            raise ValueError("Compilazione non indicata")

        with self.lock:
            visible = consegna.elenco(self.orders_dir, etichetta_fornitore=supplier_label)
            if name not in {str(item.get("cartella") or "") for item in visible}:
                raise ValueError("Compilazione non trovata")
            folder = consegna.cartella_sicura(self.orders_dir, name)
            if folder is None:
                raise ValueError("Compilazione non trovata")

            history_existed = self.history_path.exists()
            original_history = self.history_path.read_bytes() if history_existed else None
            history, _changed = order_history.read_history(self.history_path)
            removed = order_history.remove_compilation(history, name)
            quarantine = self.orders_dir / f".eliminazione-{uuid4().hex}"
            folder.rename(quarantine)
            history_saved = False
            try:
                order_history.save_history(self.history_path, history)
                history_saved = True
                shutil.rmtree(quarantine)
            except Exception:
                # Le due parti devono restare allineate: se una fallisce,
                # cartella e promemoria tornano entrambi com'erano.
                try:
                    if history_saved:
                        if original_history is None:
                            self.history_path.unlink(missing_ok=True)
                        else:
                            # Il ramo che deve funzionare quando qualcosa e'
                            # gia' andato storto: passa dallo stesso aiutante
                            # degli altri, temporaneo con un nome suo e byte
                            # sul disco prima di sostituire.
                            scrittura_sicura.scrivi_bytes(self.history_path, original_history)
                    if quarantine.exists() and not folder.exists():
                        quarantine.rename(folder)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "Eliminazione interrotta e ripristino non riuscito; non usare lo storico finché non viene controllato"
                    ) from rollback_error
                raise

            return {
                "ok": True,
                "message": "Compilazione eliminata.",
                "promemoriaRimossi": len(removed),
                "compilazioni": consegna.elenco(self.orders_dir, etichetta_fornitore=supplier_label),
            }

    def delete_upload(self, payload: Any) -> dict[str, Any]:
        """Rimuove un listino dall'app; cancella solo le copie gestite dall'app."""

        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida")
        name = str(payload.get("name") or "").strip()
        try:
            nome_sicuro = safe_upload_name(name)
        except ValueError:
            raise ValueError("Listino non trovato") from None
        if nome_sicuro != name or name == self.upload_profiles_path.name:
            raise ValueError("Listino non trovato")

        with self.lock:
            # Una copia negli upload si elimina davvero; un riferimento a un
            # originale esterno si stacca soltanto dalla pagina.
            path = consegna.file_sicuro(self.upload_dir, name)
            profiles_doc = load_json(
                self.upload_profiles_path,
                {"schema_version": 1, "profiles": [], "errors": []},
            )
            profiles = profiles_doc.get("profiles") or []
            profile = next(
                (item for item in profiles if isinstance(item, dict) and str(item.get("file_name") or "") == name),
                None,
            )
            active_review = load_json(self.review_path, {})
            file_attivi = active_review.get("files") if isinstance(active_review, dict) else []
            if not isinstance(file_attivi, list):
                file_attivi = []
            riferimenti_documento = [
                voce for voce in file_attivi
                if isinstance(voce, dict)
                and str(voce.get("name") or "") == name
                and str(voce.get("role") or "").casefold() in {"supplier", "master"}
            ]
            ruolo_documento = profiled_upload_role(profile) or next((
                str(voce.get("role") or "").casefold() for voce in riferimenti_documento
                if str(voce.get("role") or "").casefold() in {"master", "supplier"}
            ), "supplier")
            if profile is not None:
                if path is None:
                    raise ValueError("Il file caricato non è più disponibile")
                try:
                    declared_path = Path(str(profile.get("path") or "")).resolve()
                except OSError as exc:
                    raise ValueError("Il profilo del listino non è valido") from exc
                if declared_path != path.resolve():
                    raise ValueError("Il profilo del listino non corrisponde al file caricato")
            elif riferimenti_documento:
                path = None
            else:
                raise ValueError("Listino non trovato")

            original_profiles = deepcopy(profiles_doc)
            original_review = deepcopy(active_review)
            review_changed = bool(riferimenti_documento)
            if review_changed:
                active_review["files"] = [voce for voce in file_attivi if voce not in riferimenti_documento]
                # ⚠ Togliere la voce dall'elenco cancella anche la prova che
                # quel fornitore un documento ce l'aveva: `base_review` non ha
                # piu' niente da cui dedurre che e' stato tolto.  Quindi la
                # regola si applica qui, sulla stessa funzione, mentre la prova
                # c'e' ancora.  (`base_review` continua a servire per i file
                # spariti fuori dall'applicazione.)
                fornitori_tolti: dict[str, str] = {}
                gestionale_tolto = False
                for voce in riferimenti_documento:
                    if str(voce.get("role") or "").strip().casefold() == "master":
                        gestionale_tolto = True
                        continue
                    chiave, etichetta = self._fornitore_della_voce(voce)
                    if chiave:
                        fornitori_tolti[chiave] = etichetta
                self._spegni_i_fornitori_senza_documento(active_review, fornitori_tolti)
                if gestionale_tolto and active_review.get("products"):
                    # Eliminare l'elenco del gestionale e' una cosa che si puo'
                    # fare apposta — serve quando se ne carica uno sbagliato —
                    # ma i prodotti del confronto vengono da li'.  Restano
                    # visibili, e va detto da dove arrivano adesso.
                    active_review.setdefault("warnings", []).append({
                        "code": "GESTIONALE_ELIMINATO",
                        "severity": "warning",
                        "blocking": False,
                        "title": "Elenco dei prodotti eliminato",
                        "message": (
                            f"Hai tolto l'elenco del gestionale. I {len(active_review['products'])} "
                            "prodotti che vedi vengono ancora dall'ultimo confronto: carica il nuovo "
                            "elenco e rifai il confronto prima di preparare gli ordini."
                        ),
                    })
            if profile is not None:
                profiles_doc["profiles"] = [item for item in profiles if item is not profile]
                profiles_doc["errors"] = [
                    item for item in profiles_doc.get("errors") or []
                    if not isinstance(item, dict)
                    or str(item.get("file_name") or item.get("name") or "") != name
                ]
                profiles_doc["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
            quarantine = self.upload_dir / f".eliminazione-{uuid4().hex}{path.suffix}" if path else None
            if path is not None and quarantine is not None:
                path.rename(quarantine)
            profiles_saved = False
            review_saved = False
            try:
                if profile is not None:
                    atomic_json(self.upload_profiles_path, profiles_doc)
                    profiles_saved = True
                if review_changed:
                    atomic_json(self.review_path, active_review)
                    review_saved = True
                if quarantine is not None:
                    quarantine.unlink()
            except Exception:
                try:
                    if review_saved:
                        atomic_json(self.review_path, original_review)
                    if profiles_saved:
                        atomic_json(self.upload_profiles_path, original_profiles)
                    if quarantine is not None and path is not None and quarantine.exists() and not path.exists():
                        quarantine.rename(path)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "Eliminazione interrotta e ripristino non riuscito; non ricalcolare finché il listino non viene controllato"
                    ) from rollback_error
                raise

            # Che cosa e' cambiato, non solo che qualcosa e' cambiato: e' quello
            # che permette alla pagina di dire «i prezzi che vedi sono ancora
            # quelli di lunedi' 10 agosto» nominando il listino tolto.
            stato_pipeline = self.pipeline_jobs.input_modificato(
                "eliminato",
                documenti=[name],
                fornitori=sorted(fornitori_tolti.values()) if review_changed else [],
            )
            return {
                "ok": True,
                "message": (
                    f"{'Elenco' if ruolo_documento == 'master' else 'Listino'} rimosso dall'app. Il file originale non è stato cancellato."
                    if profile is None
                    else f"{'Elenco' if ruolo_documento == 'master' else 'Listino'} eliminato. Il confronto attuale resta visibile fino al prossimo confronto."
                ),
                "pipeline": stato_pipeline,
            }

    def nuova_comparazione(self, payload: Any = None) -> dict[str, Any]:
        """Svuota documenti e confronto per ricominciare da capo.

        Chiesto da Daniele il 22 agosto 2026. Il giro di ogni lunedì era:
        togliere l'elenco del gestionale, togliere i listini uno per uno — con
        cinque fornitori sono sei cancellazioni — e solo allora caricare i
        nuovi. Chi non lo faceva non se ne accorgeva subito: due listini dello
        stesso fornitore nel confronto ne fanno entrare **uno solo**, scelto
        sulla data di modifica del file (`renderDocumentoConteso` in pagina lo
        dice, ma va letto).

        ⚠ **Che cosa NON tocca, ed è il punto della funzione.** Le conferme e
        le uguaglianze (`conferme.db`), gli schemi dei fornitori imparati qui
        (`adattatori_imparati.json`), gli ordini di cui si aspetta la merce
        (`orders.json`) e le compilazioni già fatte. Le prime due sono le
        memorie che **nessun ricalcolo sa rifare** — le conferme non hanno
        nemmeno una copia fuori da questo disco — e la
        terza, cancellata, farebbe riordinare merce che sta già arrivando:
        l'unico modo in cui un comando di pulizia produce un ordine sbagliato.
        Chi aggiunge qui una riga che tocca `history/` sta cambiando quello,
        non sta facendo pulizia.

        Quello che sparisce è **solo copie**: i documenti in ingresso sono di
        sola lettura e il programma se ne fa una copia sua (regola 1), quindi i
        file di chi ordina restano dove sono.
        """

        del payload  # la richiesta non porta niente: si svuota tutto o niente
        with self.lock:
            if self.pipeline_jobs.in_corso():
                raise LavoroGiaInCorso(
                    "Un confronto è in corso: la comparazione nuova si comincia quando ha finito."
                )

            profiles_doc = load_json(
                self.upload_profiles_path,
                {"schema_version": 1, "profiles": [], "errors": []},
            )
            profili = [voce for voce in (profiles_doc.get("profiles") or []) if isinstance(voce, dict)]
            review = load_json(self.review_path, {})
            voci_file = review.get("files") if isinstance(review, dict) else []
            if not isinstance(voci_file, list):
                voci_file = []

            # I nomi da togliere vengono da tutte e due le parti: un documento
            # può stare nei profili e non nel confronto (caricato dopo l'ultimo
            # ricalcolo) o viceversa (il confronto è di prima).
            nomi: list[str] = []
            for voce in profili:
                nome = str(voce.get("file_name") or "").strip()
                if nome and nome not in nomi:
                    nomi.append(nome)
            fornitori: dict[str, str] = {}
            elenchi = 0
            for voce in voci_file:
                if not isinstance(voce, dict):
                    continue
                ruolo = str(voce.get("role") or "").strip().casefold()
                if ruolo not in {"master", "supplier"}:
                    continue
                nome = str(voce.get("name") or "").strip()
                if nome and nome not in nomi:
                    nomi.append(nome)
                if ruolo == "master":
                    elenchi += 1
                    continue
                chiave, etichetta = self._fornitore_della_voce(voce)
                if chiave:
                    fornitori[chiave] = etichetta

            # Solo le copie dentro `uploads/`: un documento che l'utente ha
            # collegato dal suo disco si stacca dalla pagina e basta, come fa
            # `delete_upload`, e il suo originale non si tocca mai.
            copie: list[Path] = []
            for nome in nomi:
                if nome == self.upload_profiles_path.name:
                    continue
                percorso = consegna.file_sicuro(self.upload_dir, nome)
                if percorso is not None and percorso.is_file():
                    copie.append(percorso)

            profili_originali = deepcopy(profiles_doc)
            vuoto = {
                "schema_version": profiles_doc.get("schema_version", 1),
                "profiles": [],
                "errors": [],
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            }

            # Prima si sposta tutto in quarantena, poi si scrive, e solo alla
            # fine si cancella davvero: è la disciplina di `delete_upload`, e
            # qui serve di più, perché i file sono tanti e a metà strada si
            # resterebbe con un confronto che descrive documenti che non ci
            # sono più.
            quarantena: list[tuple[Path, Path]] = []
            profili_scritti = False
            try:
                for percorso in copie:
                    riparo = self.upload_dir / f".nuova-comparazione-{uuid4().hex}{percorso.suffix}"
                    percorso.rename(riparo)
                    quarantena.append((percorso, riparo))
                for percorso in (self.review_path, self.state_path):
                    if percorso.is_file():
                        riparo = percorso.with_name(f".nuova-comparazione-{uuid4().hex}.json")
                        percorso.rename(riparo)
                        quarantena.append((percorso, riparo))
                atomic_json(self.upload_profiles_path, vuoto)
                profili_scritti = True
            except Exception:
                try:
                    if profili_scritti:
                        atomic_json(self.upload_profiles_path, profili_originali)
                    for percorso, riparo in reversed(quarantena):
                        if riparo.exists() and not percorso.exists():
                            riparo.rename(percorso)
                except Exception as rimessa_fallita:
                    raise RuntimeError(
                        "Pulizia interrotta e ripristino non riuscito; controlla i documenti caricati prima di ricalcolare"
                    ) from rimessa_fallita
                raise

            for _, riparo in quarantena:
                try:
                    riparo.unlink()
                except OSError:
                    # Il lavoro è fatto: `uploads/` non lo elenca più e il
                    # confronto non c'è. Un file di quarantena rimasto è
                    # sporcizia, non un guasto, e dirlo qui non aiuterebbe.
                    pass

            # Niente `cambiamento`: quella fascia dice «i prezzi che vedi nelle
            # pagine 2 e 3 sono ancora quelli di lunedì 10 agosto», e adesso
            # non ci sono più né prezzi né pagine 2 e 3 da guardare. Lo stato
            # torna in attesa e basta.
            stato_pipeline = self.pipeline_jobs.input_modificato()

            # E le domande «è arrivata la merce?» tornano adesso, non fra sette
            # giorni. Chi risponde «no, non ancora» si sente rinviare la
            # domanda di una settimana, e va bene finché a scandire il tempo è
            # un timer; ma cominciare una comparazione nuova è un segnale più
            # forte del timer — è il momento in cui ci si chiede davvero se la
            # merce della settimana scorsa è arrivata. Misurato il 22 agosto
            # 2026: quattro ordini risposti «non ancora» il 19 erano rimandati
            # al 26, quindi il comando non avrebbe chiesto niente.
            domande = 0
            storico_illeggibile = ""
            try:
                storico, _scaduti = order_history.read_history(self.history_path)
                domande = order_history.riapri_le_domande(storico)
                if domande:
                    order_history.save_history(self.history_path, storico)
            except Exception as exc:  # noqa: BLE001 - la pulizia è già fatta
                # ⚠ Non si inghiotte: la comparazione nuova è cominciata
                # davvero e non si annulla per questo, ma «non ho potuto
                # rimettere le domande» è un'informazione, e uno storico che
                # non si legge è la stessa cosa che non sapere quale merce sta
                # arrivando. Lo dice la risposta, invece di sparire.
                storico_illeggibile = frase(exc) or "Lo storico degli ordini non si è lasciato leggere."

            listini = max(0, len(nomi) - elenchi)
            return {
                "ok": True,
                "message": "Comparazione nuova: carica l'elenco del gestionale e i listini di questa settimana.",
                "tolti": {"elenco": elenchi, "listini": listini, "documenti": len(nomi)},
                "fornitori": sorted(fornitori.values()),
                "domandeRiaperte": domande,
                "storicoIlleggibile": storico_illeggibile,
                "pipeline": stato_pipeline,
            }

    def record_order_history(
        self,
        plan: dict[str, Any],
        *,
        order_key: str = "",
        delivered: set[str] | None = None,
    ) -> list[str]:
        """Registra il piano appena compilato come ordine in attesa di consegna.

        Si chiama SOLO a compilazione riuscita davvero — le copie dei listini
        scritte sul disco — e riceve i fornitori di cui la copia esiste: un
        ordine che nessuno ha potuto mandare non deve diventare la domanda
        «è arrivata?» della settimana dopo.

        Le copie ci sono già: un problema qui non deve annullare una
        compilazione riuscita, quindi viene solo segnalato.
        """

        try:
            history, _changed = order_history.read_history(self.history_path)
            order_history.record_plan(
                history,
                plan,
                supplier_name=supplier_label,
                order_key=order_key,
                delivered=delivered,
            )
            order_history.save_history(self.history_path, history)
        except Exception as exc:
            message = f"L'ordine non è stato registrato nello storico delle consegne: {exc}"
            print(f"[AVVISO] {message}")
            return [message]
        return []

    def search_products(self, query: str, limit: int = 20) -> dict[str, Any]:
        with self.lock:
            review, _state = self.review_with_manual_products()
            results = self.catalog.search(review, query, limit)
            return {"ok": True, "query": query, "results": results, "count": len(results)}

    def sfoglia_listino(
        self,
        fornitore: str,
        *,
        query: str = "",
        da: int = 0,
        quante: int = 50,
        riga: Any = None,
    ) -> dict[str, Any]:
        """Una pagina del listino di un fornitore, come il programma l'ha letto.

        Serve a rispondere a una domanda che nessun punteggio puo' risolvere:
        «questo prodotto ce l'ha anche lui, sotto un altro nome?». Il caso vero
        e' `LUXA SAPONE LIQ. EROG.250ML`, che il gestionale chiama cosi' e
        NOCE chiama `LUXA SAPONE EROGATORE ORIGINAL ML.250` sotto un altro
        codice a barre: dal testo non e' deducibile, con il listino davanti si'.

        ⚠ Un listino che non si lascia leggere NON fa fallire la richiesta.
        Prima si': `sfoglia()` alzava, la rotta rispondeva 400, e l'elenco dei
        fornitori sfogliabili — che sta nella riga sotto — non veniva mai
        calcolato. In pagina la tendina «Fornitore» restava vuota e l'unica
        cosa che si poteva fare era chiudere, anche quando gli altri listini
        stavano benissimo. Adesso la risposta porta SEMPRE l'elenco, e il
        fornitore che non c'e' lo dice in `problema`, con il motivo vero.
        """

        with self.lock:
            review, _state = self.review_with_manual_products()
            # PRIMA l'elenco, poi la pagina: e' l'inversione che toglie il
            # vicolo cieco. Calcolarlo dentro la stessa espressione del
            # risultato voleva dire non calcolarlo mai quando `sfoglia` alzava.
            fornitori = self.catalog.fornitori_sfogliabili(review)
            chiesto = str(fornitore or "").strip().casefold()
            # Nessun fornitore chiesto vuol dire «scegli tu»: il pulsante
            # «Sfoglia i listini» di un prodotto senza nessuna offerta non ne
            # porta uno (app.js, renderApriIlListino), e quel caso finiva su
            # «Nessun listino caricato per «»».
            # ⚠ Una scelta ESPLICITA non si sostituisce mai: le righe portano
            # «E' questo», che abbina al fornitore della finestra. Aprire
            # LARICE a chi ha chiesto BETULLA vorrebbe dire un abbinamento a mano
            # sul listino sbagliato, cioe' un ordine sbagliato.
            if not chiesto and fornitori:
                chiesto = str(fornitori[0].get("id") or "")
            try:
                pagina = self.catalog.sfoglia(
                    review, chiesto, query=query, da=da, quante=quante, riga=riga,
                )
            except ValueError:
                pagina = self._listino_non_sfogliabile(chiesto, fornitori, quante=quante)
            return {"ok": True, "fornitori": fornitori, **pagina}

    def _listino_non_sfogliabile(
        self, chiesto: str, fornitori: list[dict[str, Any]], *, quante: int
    ) -> dict[str, Any]:
        """La pagina vuota di un listino che non c'e', con il motivo vero.

        `SupplierCatalog.load_errors` sa perche' quel fornitore e' uscito —
        file spostato, schema non riconosciuto, colonne non dichiarate, nessuna
        riga ordinabile — e lo dice in italiano. «Nessun listino caricato per
        «betulla»» non diceva ne' che cosa fosse successo ne' che cosa fare.
        """

        motivo = next(
            (
                frase(voce.get("message"))
                for voce in self.catalog.load_errors
                if str(voce.get("supplier") or "").casefold() == chiesto
            ),
            "",
        )
        if not motivo:
            motivo = (
                f"Il listino {supplier_label(chiesto)} non fa parte di questo confronto."
                if chiesto
                else "Questo confronto non ha nessun listino da sfogliare."
            )
        seguito = (
            "Gli altri listini si sfogliano dalla tendina qui sopra."
            if fornitori
            else "Ricarica i listini dal passo 1 «Importa i dati» e rifai il confronto."
        )
        return {
            "supplier": chiesto,
            "supplierName": supplier_label(chiesto) if chiesto else "",
            "righe": [],
            "da": 0,
            # Lo stesso tetto che mette `SupplierCatalog.sfoglia`: la pagina
            # legge `quante` per contare le pagine, e due limiti diversi prima
            # o poi divergono.
            "quante": max(1, min(int(quante or 50), 200)),
            "trovate": 0,
            "totale": 0,
            "scartate": 0,
            "rigaCercata": None,
            "problema": {"fornitore": chiesto, "messaggio": f"{motivo} {seguito}".strip()},
        }

    def add_manual_product(self, payload: Any) -> dict[str, Any]:
        catalog_id = str(payload.get("catalogId") or "") if isinstance(payload, dict) else ""
        if not catalog_id:
            raise ValueError("Prodotto non indicato")
        with self.lock:
            review, state = self.review_with_manual_products()
            product = self.catalog.get(review, catalog_id)
            product_id = str(product.get("id") or "")
            current_ids = {str(item.get("id") or "") for item in review.get("products") or []}
            if product_id in current_ids:
                raise ValueError("Il prodotto è già presente nell'elenco")
            ean = str(product.get("ean") or "")
            if ean and any(str(item.get("ean") or "") == ean for item in review.get("products") or []):
                raise ValueError("Un prodotto con lo stesso codice è già presente nell'elenco")
            product.pop("catalogId", None)
            product["addedManually"] = True
            product["quantity"] = 0
            product["excluded"] = False
            state.setdefault("manualProducts", []).append(product)
            state["updatedAt"] = datetime.now(tz=timezone.utc).isoformat()
            atomic_json(self.state_path, state)
            return {"ok": True, "message": "Prodotto aggiunto all'elenco.", "product": product}

    def validate_snapshot(
        self, snapshot: Any, *, for_compile: bool
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        """Lo stato pulito, gli errori, **e il confronto su cui li ha decisi**.

        ⚠ Il terzo valore non e' una comodita': senza, chi chiama ricostruiva
        il confronto una seconda volta subito dopo, e `base_review` rilegge e
        ripassa da capo `review_data.json` — il file piu' grosso del programma,
        2,4 MB sul confronto vero. La pagina si autosalva 450 ms dopo ogni
        modifica, quindi quel lavoro si faceva due volte per ogni quantita'
        toccata, e il primo dei due risultati si buttava via.

        Restituire quello gia' costruito e' lecito, e la ragione va detta
        perche' e' l'unica cosa che rende sicura questa scorciatoia: fra le due
        chiamate non cambia niente da cui il confronto dipenda. `review_data.json`
        e' fermo — la catena adesso prende lo stesso lucchetto delle rotte per
        sostituirlo — e le quattro chiavi di stato che entrano nel confronto
        (`manualProducts`, `matchOverrides`, `manualMatches`, `supplierDiscounts`)
        `clean` le ricopia identiche da quello letto qui: sono esattamente
        `CHIAVI_DI_STATO_RICOPIATE`. Non e' una cache e non ha nessuna finestra
        di staleness — il confronto non viene tenuto da parte fra una richiesta
        e l'altra, si passa di mano dentro la stessa.
        """

        if not isinstance(snapshot, dict):
            raise ValueError("Snapshot non valido")
        review, existing_state = self.review_with_manual_products()
        run_id = str(snapshot.get("runId") or "")
        expected_run = str((review.get("run") or {}).get("id") or "")
        if expected_run and run_id != expected_run:
            raise ValueError("La run dello snapshot non coincide con la run caricata")

        # Due schede aperte sullo stesso confronto si cancellavano il lavoro a
        # vicenda: `save_state` riscrive TUTTO lo stato, e il controllo della run
        # non le distingue — e' la stessa run. La scheda dichiara la versione da
        # cui e' partita; se sul disco ce n'e' una piu' nuova, qualcun altro ha
        # gia' salvato, e va detto invece di far sparire le sue quantita' (e le
        # proprie, alla ricarica). Uno snapshot senza versione non viene
        # rifiutato: e' una pagina vecchia rimasta aperta, e toglierle il
        # salvataggio sarebbe peggio del rischio che corre.
        versione_sul_disco = int(number(existing_state.get("stateVersion")) or 0)
        versione_dichiarata = number(snapshot.get("stateVersion"))
        if versione_dichiarata is not None and int(versione_dichiarata) != versione_sul_disco:
            # ⚠ Chi ha scritto quella versione conta: una compilazione riscrive
            # lo stato prima di produrre le copie, e se poi qualcosa va storto la
            # scheda resta indietro di un numero senza che nessuna altra scheda
            # esista. Dirle «un'altra scheda ha salvato dopo di te» la manda a
            # cercare un collega che non c'e', e il pulsante «riprova» non puo'
            # funzionare perche' rifa' il salvataggio per primo (revisione di
            # regressione del 14 agosto 2026).
            # ⚠ Tre origini, non due.  La terza: sul disco `state.json` non
            # c'e' affatto, e la scheda ne dichiara una versione — vuol dire
            # che nel frattempo qualcuno ha premuto «Inizia nuova
            # comparazione».  Dirle «un'altra scheda ha salvato dopo di te» la
            # manda a cercare un collega che non esiste, ed e' lo stesso
            # errore gia' corretto una volta per la compilazione.
            svuotato = versione_sul_disco == 0 and not self.state_path.is_file()
            return {}, [{
                "code": "STATO_SOVRASCRITTO",
                "stateVersion": versione_sul_disco,
                "origin": "svuotato" if svuotato else str(existing_state.get("stateVersionOrigin") or "scheda"),
            }], review

        products_by_id = {str(product.get("id")): product for product in review.get("products") or []}
        decisions = snapshot.get("products")
        if not isinstance(decisions, list):
            raise ValueError("products deve essere una lista")
        seen = set()
        normalized = []
        errors = []
        for decision in decisions:
            product_id = str(decision.get("id") or "")
            if not product_id or product_id in seen:
                errors.append({"code": "ID_PRODOTTO_NON_VALIDO", "productId": product_id})
                continue
            seen.add(product_id)
            product = products_by_id.get(product_id)
            if product is None:
                errors.append({"code": "PRODOTTO_SCONOSCIUTO", "productId": product_id})
                continue
            quantity_value = number(decision.get("quantity"))
            if quantity_value is None or quantity_value < 0 or int(quantity_value) != quantity_value or quantity_value > 100000:
                errors.append({
                    "code": "QUANTITA_NON_VALIDA",
                    "productId": product_id,
                    # Il nome viaggia con l'errore: la pagina elenca i prodotti
                    # fermi uno per uno, e un identificativo non si cerca a mano
                    # fra cinquecento righe.
                    "productName": str(product.get("name") or product.get("description") or product_id),
                })
                continue
            quantity = int(quantity_value)
            supplier_id = str(decision.get("selectedSupplierId") or "")
            confirmed = bool(decision.get("confirmed"))
            excluded = bool(decision.get("excluded"))
            if excluded:
                quantity = 0
            offer = find_offer(product, supplier_id)
            product_name = str(product.get("name") or product.get("description") or product_id)
            # ⚠ Dal 16 agosto 2026 una quantità positiva senza nessun fornitore
            # disponibile è uno stato valido: il prodotto non entra nel piano né
            # nei listini dei fornitori, ma nell'elenco «Prodotti da reperire»
            # che nasce alla compilazione. La regola del 12 agosto — «una riga
            # d'ordine senza offerta utilizzabile è un ordine che non si può
            # mandare» — resta intera per gli altri due casi: un fornitore
            # scelto la cui offerta non si può ordinare, e nessun fornitore
            # scelto quando invece qualcuno ce l'ha. Lì c'è una decisione da
            # prendere, e lasciarla passare in silenzio manderebbe un ordine
            # sbagliato.
            da_reperire = not supplier_id and nessuna_offerta_utilizzabile(product)
            offerta_da_sistemare = (
                (not offer_is_available(offer)) if supplier_id else not da_reperire
            )
            if quantity > 0 and offerta_da_sistemare:
                # Il rifiuto dice DI QUALE prodotto parla: prima la pagina
                # mostrava «Controlli snapshot non superati» e su un elenco di
                # cinquecento righe non c'era modo di sapere quale toccare.
                #
                # ⚠ Due codici e non uno, dal 21 agosto 2026. «Il fornitore
                # scelto ha un'offerta che non si puo' ordinare» e' un errore da
                # sistemare subito; «non ho ancora scelto fra quelli che ce
                # l'hanno» e' una decisione ancora da prendere, e succede
                # normalmente dopo un «Non e' lo stesso articolo» quando un
                # altro fornitore l'articolo ce l'ha. Con un codice solo quel
                # rifiuto spegneva l'autosalvataggio di TUTTI i 459 prodotti —
                # e' il guasto del 15 agosto rifatto — e per giunta bloccava il
                # pulsante che serviva a tornare indietro, che salva prima di
                # rispondere. Ferma la compilazione, non il salvataggio.
                errors.append({
                    "code": "OFFERTA_NON_VALIDA" if supplier_id else "FORNITORE_DA_SCEGLIERE",
                    "productId": product_id,
                    "productName": product_name,
                    "supplierId": supplier_id,
                    "supplierName": supplier_label(supplier_id) if supplier_id else "",
                })
            # Senza un fornitore scelto non c'è niente da confermare: pretendere
            # una conferma su un prodotto che nessuno ha a listino sarebbe una
            # casella che l'utente non può spuntare, e fermerebbe la
            # compilazione per sempre.
            requires = bool(supplier_id) and offer_needs_confirmation(product, offer)
            # ⚠ Qui il magazzino delle conferme **non** si guarda, ed e' una
            # scelta. Chi semina la risposta gia' data e' `review()`, che la
            # mette su `product.confirmed`; la pagina la rimanda indietro nello
            # snapshot senza toccarla, ed e' quella che arriva fin qui. Se
            # invece il magazzino potesse dire di si' anche qui, una spunta
            # tolta dall'utente verrebbe rimessa dal ricordo di ieri nello
            # stesso salvataggio in cui l'ha tolta: la revoca non esisterebbe.
            # Una sola autorita' per volta — il magazzino quando si legge, lo
            # snapshot quando si salva.
            if quantity > 0 and requires and not confirmed:
                errors.append({
                    "code": "CONFERMA_MANCANTE",
                    "productId": product_id,
                    "productName": product_name,
                    "supplierId": supplier_id,
                    "supplierName": supplier_label(supplier_id) if supplier_id else "",
                })
            quantity_source = str(decision.get("quantitySource") or "")
            if quantity_source not in {"gestionale", "utente"}:
                quantity_source = "gestionale"
            # Chi ha scelto questo fornitore: il programma o l'utente.
            #
            # Non si chiede alla pagina, si deduce — e la deduzione è esatta,
            # perché il più conveniente il programma **l'ha già selezionato da
            # sé**: sceglierlo a mano non cambierebbe niente, quindi una
            # decisione che coincide col migliore non può essere altro che il
            # default. Chi ha voluto un fornitore diverso dal migliore, invece,
            # ha fatto una scelta, e quella si difende.
            #
            # ⚠ Serve a `review()`, e senza il difetto è questo: il confronto
            # ricalcolato sceglie di nuovo il più conveniente, poi la decisione
            # salvata gli passa sopra. Un default calcolato la settimana scorsa
            # sopravviveva a un ricalcolo che lo rendeva sbagliato travestito da
            # scelta dell'utente, e NOCE restava fuori dall'ordine con il
            # prezzo più basso in pagina (difetto trovato il 26 agosto 2026, in
            # piedi dal primo commit).
            migliore = self._offerta_piu_conveniente(product)
            scelta_dell_utente = bool(supplier_id) and (
                migliore is None or supplier_id != str(migliore.get("supplierId") or "")
            )
            voce = {
                "id": product_id,
                "quantity": quantity,
                "selectedSupplierId": supplier_id,
                "selectedSupplierSource": "utente" if scelta_dell_utente else "automatico",
                "confirmed": confirmed,
                "excluded": excluded,
                "quantitySource": quantity_source,
            }
            # Che cosa e' stato confermato, non solo che qualcosa lo e' stato.
            # Senza questo, dopo il ricalcolo della settimana dopo la casella
            # restava spuntata anche quando il prodotto era stato abbinato a
            # un'altra riga del listino, con altro EAN e altra descrizione: la
            # conferma dell'utente copriva un articolo che non aveva mai visto.
            if confirmed and offer:
                voce["confirmedArticle"] = impronta_articolo(offer)
            normalized.append(voce)

        # ⚠ Una compilazione fatta di soli prodotti da reperire non è un ordine
        # vuoto da rifiutare: è una settimana in cui il gestionale chiede merce
        # che nessun fornitore porta, e l'unica uscita giusta è l'elenco di
        # quella merce (decisione di Daniele del 16 agosto 2026). `ORDINE_VUOTO`
        # resta per il caso in cui non c'è né un ordine né un elenco.
        if for_compile and not any(item["quantity"] > 0 for item in normalized):
            errors.append({"code": "ORDINE_VUOTO"})
        clean = {
            "schemaVersion": 1,
            # Cresce a ogni riscrittura completa dello stato (`save_state` e
            # `compile`): e' il numero con cui la scheda successiva si accorge
            # di non essere la sola. Chi tocca solo una parte dello stato —
            # una risposta a un candidato, un prodotto aggiunto a mano — non lo
            # muove, perche' quelle scritture `validate_snapshot` le ricopia dal
            # disco e non le puo' perdere.
            "stateVersion": versione_sul_disco + 1,
            # Chi l'ha scritta: `compile` lo rimette a «compilazione» dopo aver
            # chiamato questa funzione. Serve alla scheda che si trova indietro
            # di un numero per sapere se l'ha superata un collega o se stessa.
            "stateVersionOrigin": "scheda",
            "runId": run_id,
            "currentStep": max(1, min(3, int(number(snapshot.get("currentStep")) or 1))),
            "acceptBelowThreshold": bool(snapshot.get("acceptBelowThreshold")),
            "summaryGrouping": snapshot.get("summaryGrouping") if snapshot.get("summaryGrouping") in {"supplier", "product"} else "supplier",
            "updatedAt": datetime.now(tz=timezone.utc).isoformat(),
            "products": normalized,
        }
        # Le decisioni sui candidati sono legate alla run e alla loro impronta;
        # lo sconto di testata lo scrive la sua rotta; le righe abbinate a mano
        # pure. Nessuna delle tre viaggia nello snapshot della scheda, quindi
        # l'autosalvataggio delle quantità non le deve toccare: si ricopiano dal
        # disco, una per una, dall'elenco che le tiene tutte.
        for chiave, quando_manca in CHIAVI_DI_STATO_RICOPIATE.items():
            clean[chiave] = existing_state.get(chiave) or deepcopy(quando_manca)
        return clean, errors, review

    def order_summary(
        self,
        review: dict[str, Any],
        selections: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """I totali del Riepilogo, contati qui: testata, somma delle righe, scarto.

        ⚠ La testata e la somma delle righe non coincidono sempre.  Il totale
        di un ordine si fa sui prezzi interi (le promozioni e i listini portano
        fino a sei decimali), mentre ogni riga in pagina si mostra arrotondata
        al centesimo: sommando le righe mostrate escono anche quattro centesimi
        di differenza — misurati su BETULLA il 12 agosto 2026, 830,16 € contro
        830,12 €.  Nessuno dei due numeri è sbagliato; sbagliato è mostrarne uno
        senza dire dell'altro, perché chi controlla a mano trova un buco e non
        sa di che cosa sia fatto.
        """

        products_by_id = {str(item.get("id")): item for item in review.get("products") or []}
        supplier_defs = {str(item.get("id")): item for item in review.get("suppliers") or []}
        righe: dict[str, dict[str, Any]] = {}
        for decision in selections.values():
            quantity = int(number(decision.get("quantity")) or 0)
            if quantity <= 0 or decision.get("excluded"):
                continue
            product = products_by_id.get(str(decision.get("id") or ""))
            if product is None:
                continue
            supplier = str(decision.get("selectedSupplierId") or "")
            pricing = offer_pricing(find_offer(product, supplier))
            if pricing is None:
                continue
            line_net = round(quantity * pricing["orderUnitPriceNet"], 4)
            voce = righe.setdefault(supplier, {
                "supplierId": supplier,
                "supplierName": supplier_label(supplier),
                "lineCount": 0,
                "_intero": 0.0,
                "_arrotondato": 0.0,
            })
            voce["lineCount"] += 1
            voce["_intero"] = round(voce["_intero"] + line_net, 4)
            # ⚠ La somma delle righe deve contare COME CONTA LO SCHERMO.
            # `round()` di Python arrotonda il double e a metà va al pari;
            # `Intl.NumberFormat` del browser arrotonda la rappresentazione
            # decimale e a metà va per eccesso: su 14,665 il primo dice 14,66 e
            # la pagina mostra 14,67.  La frase del Riepilogo confronta la
            # testata con «quello che le righe mostrano»: contato diversamente,
            # dichiarava un numero che sullo schermo non c'era (BLOCCANTE della
            # revisione avversariale R4).
            voce["_arrotondato"] = round(voce["_arrotondato"] + _centesimi_come_in_pagina(line_net), 2)

        suppliers = []
        for supplier in sorted(righe, key=lambda item: (supplier_label(item), item)):
            voce = righe[supplier]
            totale = _centesimi_come_in_pagina(voce.pop("_intero"))
            somma_righe = round(voce.pop("_arrotondato"), 2)
            voce["totalNet"] = totale
            voce["linesTotalNet"] = somma_righe
            voce["roundingDifference"] = round(totale - somma_righe, 2)
            voce["threshold"] = round(supplier_threshold(supplier_defs.get(supplier, {})), 2)
            suppliers.append(voce)

        totale_generale = _centesimi_come_in_pagina(sum(item["totalNet"] for item in suppliers))
        somma_generale = _centesimi_come_in_pagina(sum(item["linesTotalNet"] for item in suppliers))
        return {
            "suppliers": suppliers,
            "totalNet": totale_generale,
            "linesTotalNet": somma_generale,
            "roundingDifference": round(totale_generale - somma_generale, 2),
        }

    def order_summary_from_state(self, review: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Lo stesso riepilogo, partendo dalle scelte salvate sul disco."""

        selections = {
            str(item.get("id") or ""): item
            for item in (state or {}).get("products") or []
            if isinstance(item, dict)
        }
        return self.order_summary(review, selections)

    def save_state(self, snapshot: Any) -> dict[str, Any]:
        with self.lock:
            # Lo stato di prima serve a distinguere una revoca da una scadenza:
            # va letto adesso, perche' fra un attimo viene sostituito.
            precedente = load_json(self.state_path, {}) if self.state_path.is_file() else {}
            clean, errors, review = self.validate_snapshot(snapshot, for_compile=False)
            # ⚠ Una conferma che manca non impedisce di SALVARE, impedisce di
            # COMPILARE. Finche' bloccava anche il salvataggio, il passo 2 si
            # apriva con un riquadro rosso «Salvataggio non riuscito» e da quel
            # momento **niente** si salvava piu': ne' le quantita' che cambiavi,
            # ne' i fornitori che sceglievi, finche' non avevi risposto a tutte
            # le proposte in sospeso. Misurato il 15 agosto 2026 sul confronto
            # vero: 8 prodotti con la quantita' del gestionale e la
            # corrispondenza da confermare bastavano a spegnere il
            # salvataggio di 459.
            #
            # Il cancello resta dov'e' utile: `compile` valida con
            # `for_compile=True` e li' `CONFERMA_MANCANTE` ferma tutto, con la
            # frase che dice quale prodotto e quale fornitore. E chi deve
            # ancora rispondere lo sa gia' dalla pagina, che dichiara le
            # proposte in sospeso e il filtro «Da confermare»: ripeterlo qui
            # sarebbe la seconda notifica per la stessa cosa.
            # ⚠ `FORNITORE_DA_SCEGLIERE` sta accanto a `CONFERMA_MANCANTE` per
            # la stessa ragione: e' una decisione ancora da prendere, non un
            # ordine sbagliato, e non deve spegnere il salvataggio degli altri
            # quattrocentocinquanta prodotti. Con `for_compile=True` ferma
            # eccome.
            non_bloccanti = {"CONFERMA_MANCANTE", "FORNITORE_DA_SCEGLIERE"}
            bloccanti = [errore for errore in errors if errore.get("code") not in non_bloccanti]
            if bloccanti:
                raise SnapshotError(bloccanti)
            atomic_json(self.state_path, clean)
            if str((precedente or {}).get("runId") or "") != str(clean.get("runId") or ""):
                # ⚠ L'unico caso in cui il confronto costruito da
                # `validate_snapshot` NON e' quello che si otterrebbe adesso.
                # Quasi tutto quello che entra nel confronto viene ricopiato
                # identico dallo stato di prima, ma `apply_match_overrides` — le
                # risposte date ai candidati — si applica **solo** se lo stato
                # dichiara la stessa run del confronto: con uno stato che ne
                # dichiarava un'altra (un'installazione nuova, una catena morta
                # fra la ripulitura dello stato e la sostituzione del confronto)
                # quello di prima le ha saltate e quello scritto adesso no. E'
                # raro e sarebbe silenzioso: si ricostruisce e basta.
                review, _stato = self.review_with_manual_products()
            self.ricorda_le_conferme(clean, review, precedente if isinstance(precedente, dict) else {})
            selections = {str(item["id"]): item for item in clean["products"]}
            decorated = self.promotion_service.decorate(review, selections)
            promotion_states = {
                str(item.get("id")): item.get("state") or {}
                for item in decorated.get("promotions") or []
                if item.get("id")
            }
            return {
                "ok": True,
                "savedAt": clean["updatedAt"],
                "stateVersion": clean["stateVersion"],
                "promotionSummary": decorated.get("promotionSummary") or {"counts": {}},
                "promotionStates": promotion_states,
                # I totali del Riepilogo li conta il servizio, e con loro lo
                # scarto di arrotondamento fra testata e righe.
                "orderSummary": self.order_summary(review, selections),
            }

    def omaggi_per_fornitore(
        self,
        review: dict[str, Any],
        selections: dict[str, dict[str, Any]],
    ) -> dict[str, int]:
        """Quanti omaggi da soglia si portano a casa con queste scelte.

        Il NUMERO si calcola con le stesse regole delle promozioni (soglie per
        singolo ordine e ripetibili); il VALORE dell'omaggio no, per decisione
        commerciale: l'omaggio informa e basta.
        """

        from promotions import (  # import tardivo: il modulo vive in `scripts`
            KIND_THRESHOLD_GIFT,
            STATUS_EARNED,
            calculate_promotion_state,
        )

        conteggi: dict[str, int] = {}
        with self.promotion_service.lock:
            promozioni = self.promotion_service.detect(review)
        for promozione in promozioni:
            if str(promozione.get("kind") or "") != KIND_THRESHOLD_GIFT:
                continue
            stato = calculate_promotion_state(promozione, review, selections=selections)
            if stato.get("status") != STATUS_EARNED:
                continue
            fornitore = str(promozione.get("supplier") or "")
            conteggi[fornitore] = conteggi.get(fornitore, 0) + max(1, int(stato.get("reward_count") or 1))
        return conteggi

    def move_preview(self, payload: Any) -> dict[str, Any]:
        """Preventivo dello spostamento di tutti i prodotti di un fornitore.

        Non scrive niente: ne' lo stato, ne' lo storico, ne' i documenti finali.
        Mostra soltanto quanto costerebbe cambiare fornitore, cosi' l'utente
        decide guardando i numeri; lo spostamento vero avviene poi con il
        normale salvataggio dello stato. Il conto lo fa il server e non il
        browser perche' prezzi e totali li decide sempre e solo il server.

        Il numero di colli (o di espositori) non cambia mai: cambia il
        fornitore, quindi il prezzo dell'unita' d'ordine e i pezzi consegnati.
        """

        if not isinstance(payload, dict):
            raise ValueError("Richiesta non valida")
        from_supplier = str(payload.get("from") or "").strip()
        if not from_supplier:
            raise ValueError("Fornitore di partenza non indicato")

        with self.lock:
            # Stesso controllo degli altri endpoint: run sbagliata, prodotto
            # sconosciuto o quantita' impossibile vengono rifiutati qui.
            # CONFERMA_MANCANTE no: un preventivo non scrive niente, e una
            # conferma che manca su un prodotto di UN ALTRO fornitore non deve
            # impedire di vedere i numeri di questo. Le conferme che serviranno
            # dopo lo spostamento vengono comunque dichiarate una per una nel
            # campo needsConfirmation di ogni assegnazione.
            clean, errors, review = self.validate_snapshot(payload, for_compile=False)
            blocking = [error for error in errors if error.get("code") != "CONFERMA_MANCANTE"]
            if blocking:
                raise SnapshotError(blocking)
            products_by_id = {str(item.get("id")): item for item in review.get("products") or []}
            supplier_defs = {str(item.get("id")): item for item in review.get("suppliers") or []}
            # Le scelte di adesso, nella forma che le promozioni sanno leggere:
            # servono a contare gli omaggi prima e dopo lo spostamento.
            selezioni_prima = {str(item["id"]): dict(item) for item in clean["products"]}
            omaggi_prima = self.omaggi_per_fornitore(review, selezioni_prima)

            # Prima passata: totale netto dell'ordine intero riga per riga, con
            # la stessa aritmetica della compilazione. I prodotti a quantita'
            # zero e quelli esclusi non pesano (la convalida azzera gli esclusi).
            totals_before: dict[str, float] = {}
            current_total = 0.0
            movable: list[dict[str, Any]] = []
            for decision in clean["products"]:
                if decision["quantity"] <= 0:
                    continue
                product = products_by_id.get(decision["id"])
                if product is None:
                    continue
                supplier = decision["selectedSupplierId"]
                pricing = offer_pricing(find_offer(product, supplier))
                line_net = round(decision["quantity"] * pricing["orderUnitPriceNet"], 4) if pricing else 0.0
                totals_before[supplier] = round(totals_before.get(supplier, 0.0) + line_net, 4)
                current_total = round(current_total + line_net, 4)
                if supplier != from_supplier:
                    continue
                alternatives: dict[str, dict[str, Any]] = {}
                for offer in product.get("offers") or []:
                    destination = offer_supplier_id(offer)
                    if not destination or destination == from_supplier or destination in alternatives:
                        continue
                    if not offer_is_available(offer):
                        continue
                    destination_pricing = offer_pricing(offer)
                    if destination_pricing is None:
                        continue
                    alternatives[destination] = {
                        "supplierId": destination,
                        "pricing": destination_pricing,
                        "needsConfirmation": offer_needs_confirmation(product, offer),
                    }
                movable.append({
                    "id": decision["id"],
                    "name": str(product.get("name") or product.get("description") or decision["id"]),
                    "product": product,
                    "quantity": decision["quantity"],
                    "lineNet": line_net,
                    "factor": pricing["factor"] if pricing else 1.0,
                    "alternatives": alternatives,
                })

            def left_behind_reason(item: dict[str, Any], supplier_id: str | None) -> str:
                """Perché il prodotto resta dov'è. Non viene azzerato né tolto."""

                if supplier_id:
                    examined = [find_offer(item["product"], supplier_id)]
                else:
                    examined = [
                        offer for offer in item["product"].get("offers") or []
                        if offer_supplier_id(offer) not in {"", from_supplier}
                    ]
                if any(offer_is_available(offer) for offer in examined):
                    return "PREZZO_NON_DISPONIBILE"
                return "NESSUNA_OFFERTA"

            def build_option(option_id: str, kind: str, label: str, chosen: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
                assignments: list[dict[str, Any]] = []
                left_behind: list[dict[str, Any]] = []
                totals_after = dict(totals_before)
                delta = 0.0
                # A parita' di colli i pezzi consegnati cambiano, perche' i colli
                # di fornitori diversi non contengono la stessa merce. Senza
                # questi conteggi la differenza di spesa da sola e' ingannevole:
                # un'opzione che "fa risparmiare" puo' consegnare meta' roba.
                pieces_before = 0.0
                pieces_after = 0.0
                moved_net_before = 0.0
                moved_net_after = 0.0
                for item in movable:
                    choice = chosen.get(item["id"])
                    if choice is None:
                        left_behind.append({
                            "productId": item["id"],
                            "productName": item["name"],
                            "reason": left_behind_reason(item, option_id if kind == "supplier" else None),
                        })
                        continue
                    destination = choice["supplierId"]
                    new_line = round(item["quantity"] * choice["pricing"]["orderUnitPriceNet"], 4)
                    totals_after[from_supplier] = round(totals_after.get(from_supplier, 0.0) - item["lineNet"], 4)
                    totals_after[destination] = round(totals_after.get(destination, 0.0) + new_line, 4)
                    delta = round(delta + new_line - item["lineNet"], 4)
                    new_factor = choice["pricing"]["factor"]
                    previous_pieces = item["quantity"] * item["factor"]
                    new_pieces = item["quantity"] * new_factor
                    pieces_before += previous_pieces
                    pieces_after += new_pieces
                    moved_net_before = round(moved_net_before + item["lineNet"], 4)
                    moved_net_after = round(moved_net_after + new_line, 4)
                    assignments.append({
                        "productId": item["id"],
                        "productName": item["name"],
                        "toSupplierId": destination,
                        "previousFactor": item["factor"],
                        "newFactor": new_factor,
                        "factorChanged": new_factor != item["factor"],
                        "previousPieces": int(round(previous_pieces)),
                        "newPieces": int(round(new_pieces)),
                        "needsConfirmation": bool(choice["needsConfirmation"]),
                        "previousLineNet": round(item["lineNet"], 2),
                        "newLineNet": round(new_line, 2),
                    })
                if not assignments:
                    # Un'opzione che non sposta niente non si propone: sarebbe
                    # una scelta che non cambia nulla in mezzo alle altre.
                    return None
                # Le soglie con omaggio si raggiungono con l'ordine di UN
                # fornitore: spostando la merce altrove si perdono, e finora il
                # preventivo non lo diceva. Il conto lo rifa' il servizio con le
                # scelte di dopo, con le stesse regole delle promozioni.
                selezioni_dopo = {key: dict(value) for key, value in selezioni_prima.items()}
                for assignment in assignments:
                    voce = selezioni_dopo.get(assignment["productId"])
                    if voce is not None:
                        voce["selectedSupplierId"] = assignment["toSupplierId"]
                omaggi_dopo = self.omaggi_per_fornitore(review, selezioni_dopo)
                totale_omaggi_prima = sum(omaggi_prima.values())
                totale_omaggi_dopo = sum(omaggi_dopo.values())
                # Persi e guadagnati si contano PER FORNITORE e poi si sommano:
                # perdere 2 omaggi da LARICE e guadagnarne 2 da BETULLA non e'
                # «zero» — sono merci diverse di fornitori diversi, e la
                # differenza dei totali generali le compensava in silenzio
                # (revisione avversariale R4).
                fornitori_omaggi = set(omaggi_prima) | set(omaggi_dopo)
                omaggi_persi = sum(
                    max(0, omaggi_prima.get(nome, 0) - omaggi_dopo.get(nome, 0))
                    for nome in fornitori_omaggi
                )
                omaggi_guadagnati = sum(
                    max(0, omaggi_dopo.get(nome, 0) - omaggi_prima.get(nome, 0))
                    for nome in fornitori_omaggi
                )
                totals_rows = []
                for supplier in [from_supplier] + sorted({item["toSupplierId"] for item in assignments}):
                    threshold = supplier_threshold(supplier_defs.get(supplier, {}))
                    before = totals_before.get(supplier, 0.0)
                    after = totals_after.get(supplier, 0.0)
                    totals_rows.append({
                        "supplierId": supplier,
                        "supplierName": supplier_label(supplier),
                        "netTotalBefore": round(before, 2),
                        "netTotalAfter": round(after, 2),
                        "giftsBefore": int(omaggi_prima.get(supplier, 0)),
                        "giftsAfter": int(omaggi_dopo.get(supplier, 0)),
                        "threshold": round(threshold, 2),
                        # meets_threshold segue la regola della compilazione, per
                        # cui chi non ordina niente non e' "sotto soglia". Senza
                        # hadOrderBefore chi legge scambia quel vero per "la
                        # soglia era raggiunta" e racconta che un fornitore
                        # partito da zero e' sceso.
                        "hadOrderBefore": before > 0,
                        "meetsThresholdBefore": meets_threshold(before, threshold),
                        "meetsThresholdAfter": meets_threshold(after, threshold),
                    })
                cost_before = round(moved_net_before / pieces_before, 4) if pieces_before else 0.0
                cost_after = round(moved_net_after / pieces_after, 4) if pieces_after else 0.0
                return {
                    "id": option_id,
                    "kind": kind,
                    "label": label,
                    "movedCount": len(assignments),
                    "movableCount": len(movable),
                    "deltaNet": round(delta, 2),
                    "deliveredPiecesBefore": int(round(pieces_before)),
                    "deliveredPiecesAfter": int(round(pieces_after)),
                    "deltaPieces": int(round(pieces_after - pieces_before)),
                    "costPerPieceBefore": cost_before,
                    "costPerPieceAfter": cost_after,
                    "deltaCostPerPiece": round(cost_after - cost_before, 4),
                    # Il numero degli omaggi, mai il loro valore: quanto vale un
                    # omaggio non lo decide questo programma.
                    "giftsBefore": totale_omaggi_prima,
                    "giftsAfter": totale_omaggi_dopo,
                    "giftsLost": omaggi_persi,
                    "giftsGained": omaggi_guadagnati,
                    "assignments": assignments,
                    "leftBehind": left_behind,
                    "supplierTotalsAfter": totals_rows,
                }

            options = []
            # "Migliore alternativa": si sceglie sul prezzo AL PEZZO, mai sul
            # prezzo del collo. Colli di fornitori diversi contengono quantita'
            # diverse, quindi il collo piu' economico puo' benissimo essere
            # quello che fa pagare di piu' ogni singolo pezzo. A parita' di
            # prezzo al pezzo vince il fornitore in ordine alfabetico, cosi' la
            # stessa domanda ottiene sempre la stessa risposta.
            best_choice = {
                item["id"]: min(
                    item["alternatives"].values(),
                    key=lambda alternative: (alternative["pricing"]["unitPriceNet"], alternative["supplierId"]),
                )
                for item in movable if item["alternatives"]
            }
            best_option = build_option("best", "best", "Migliore alternativa per ciascun prodotto", best_choice)
            if best_option is not None:
                options.append(best_option)

            supplier_options = []
            for destination in sorted({key for item in movable for key in item["alternatives"]}):
                chosen = {
                    item["id"]: item["alternatives"][destination]
                    for item in movable if destination in item["alternatives"]
                }
                option = build_option(destination, "supplier", supplier_label(destination), chosen)
                if option is not None:
                    supplier_options.append(option)
            # L'ordine di lettura NON puo' essere il totale speso: ordini che
            # contengono quantita' di merce diverse non sono confrontabili sul
            # totale, ed e' la stessa trappola del confronto fra offerte. Prima
            # le opzioni che svuotano di piu' il fornitore (e' lo scopo del
            # comando), poi quelle che fanno pagare meno OGNI PEZZO.
            supplier_options.sort(key=lambda option: (
                -option["movedCount"],
                option["deltaCostPerPiece"],
                option["id"],
            ))
            options.extend(supplier_options)

            return {
                "ok": True,
                "from": from_supplier,
                "fromName": supplier_label(from_supplier),
                "movableCount": len(movable),
                "currentNetTotal": round(current_total, 2),
                "options": options,
            }

    def upload(self, payload: Any) -> dict[str, Any]:
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list) or not files:
            raise ValueError("Nessun documento ricevuto")
        saved = []
        letti_per_il_contenuto: list[str] = []
        rinominati: list[str] = []
        # Nomi ed estensioni si controllano tutti prima di scrivere anche solo
        # un byte: se il secondo documento del gruppo e' inaccettabile, il
        # primo non deve restare sul disco come copia che nessuno conosce.
        da_scrivere: list[tuple[str, bytes, str]] = []
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("Documento non valido")
            name = safe_upload_name(str(item.get("name") or ""))
            ruolo_scelto = upload_role(item.get("role") or "suppliers")
            encoded = item.get("data")
            if not isinstance(encoded, str):
                raise ValueError(f"Contenuto mancante per {name}")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"Base64 non valido per {name}") from exc
            if not content:
                raise ValueError(f"«{name}» è vuoto: non contiene nessun dato.")
            if len(content) > MAX_UPLOAD_BYTES:
                raise ValueError(f"«{name}» supera i {MAX_UPLOAD_BYTES // (1024 * 1024)} MB consentiti.")
            da_scrivere.append((name, content, ruolo_scelto))

        with self.lock:
            profiles_doc = load_json(self.upload_profiles_path, {"schema_version": 1, "profiles": [], "errors": []})
            # ⚠ «Gia' presente» vuol dire «la copia c'e'», non «me lo ricordo».
            # I profili senza piu' una copia sul disco escono dal registro qui:
            # finche' restavano, il documento cancellato a mano dalla cartella
            # non si poteva ne' vedere (la pagina li filtra) ne' ricaricare (il
            # controllo sull'impronta lo dichiarava duplicato e non scriveva
            # niente), e il confronto non ripartiva piu'.
            profili_veri, fantasmi = self._profili_veri_e_fantasmi(profiles_doc)
            if fantasmi:
                profiles_doc["profiles"] = profili_veri
                nomi_fantasma = ", ".join(sorted(str(item.get("file_name") or "?") for item in fantasmi))
                print(
                    f"[AVVISO] {len(fantasmi)} documenti non sono più nella cartella dei "
                    f"caricamenti e sono stati tolti dal registro: {nomi_fantasma}"
                )
            profili_per_hash = {
                item.get("sha256"): item for item in profili_veri
                if item.get("sha256")
            }
            known_hashes = set(profili_per_hash)
            scritti: list[Path] = []
            try:
                for name, content, ruolo_scelto in da_scrivere:
                    destination = unique_destination(self.upload_dir, name)
                    destination.write_bytes(content)
                    scritti.append(destination)
                    try:
                        profile = profile_file(destination)
                    except Exception as exc:
                        # Il documento non arriva a destinazione, quindi il
                        # motivo va detto qui: senza questo messaggio l'utente
                        # leggerebbe l'errore del lettore, in inglese e senza
                        # indicazioni. Il tipo dell'eccezione resta nel
                        # dettaglio tecnico e sulla console: e' l'unica cosa
                        # che distingue un documento malformato — che e' colpa
                        # del file — da un guasto del programma, che non lo e'.
                        dettaglio = f"{type(exc).__name__}: {exc}" if str(exc).strip() else type(exc).__name__
                        print(f"[AVVISO] «{name}» non profilato — {dettaglio}")
                        raise ValueError(
                            f"«{name}» non è stato riconosciuto: non è un foglio di calcolo Excel "
                            "né un CSV leggibile. Noce manda un .xls, gli altri fornitori un "
                            f".xlsx o un .csv. Dettaglio tecnico: {dettaglio}"
                        ) from exc
                    profile["upload_role"] = ruolo_scelto
                    profile.setdefault("ai_preflight", {})["role"] = ruolo_scelto
                    if profile.get("sha256") in known_hashes:
                        destination.unlink(missing_ok=True)
                        scritti.pop()
                        esistente = profili_per_hash.get(profile.get("sha256"))
                        if isinstance(esistente, dict):
                            esistente["upload_role"] = ruolo_scelto
                            esistente.setdefault("ai_preflight", {})["role"] = ruolo_scelto
                        saved.append({
                            "name": str((esistente or {}).get("file_name") or name),
                            "status": "duplicate",
                            "role": ruolo_scelto,
                            "message": "Documento già acquisito; aggiornata la sua destinazione nell'app.",
                        })
                        continue
                    profiles_doc.setdefault("profiles", []).append(profile)
                    known_hashes.add(profile.get("sha256"))
                    profili_per_hash[profile.get("sha256")] = profile
                    if destination.name != name:
                        # Un listino corretto e ricaricato non sostituisce il
                        # vecchio: convivono, e senza questa riga l'utente non
                        # saprebbe quale dei due sta confermando.
                        rinominati.append(f"«{name}» era già presente: salvato come «{destination.name}»")
                    formato = str(profile.get("content_format") or "")
                    atteso = FORMATO_DELL_ESTENSIONE.get(Path(destination.name).suffix.casefold())
                    if formato and atteso and formato != atteso:
                        # Un listino rinominato si legge lo stesso, ma dirlo evita
                        # che l'utente cerchi per mezz'ora perche' le colonne non
                        # sono quelle che si aspettava.
                        letti_per_il_contenuto.append(
                            f"«{destination.name}» dentro è un {NOME_DEL_FORMATO.get(formato, formato)}"
                        )
                    saved.append({
                        "name": destination.name,
                        "status": "profiled",
                        "contentFormat": formato,
                        "schemaState": (profile.get("deterministic_hint") or {}).get("state"),
                        "adapterId": (profile.get("deterministic_hint") or {}).get("adapter_id"),
                        "role": ruolo_scelto,
                    })
            except Exception:
                for copia in scritti:
                    copia.unlink(missing_ok=True)
                raise
            profiles_doc["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
            atomic_json(self.upload_profiles_path, profiles_doc)
        acquisiti = sum(1 for voce in saved if voce.get("status") == "profiled")
        duplicati = len(saved) - acquisiti
        if acquisiti:
            # ⚠ Seguiva «Codex deve confermarne la struttura prima del
            # ricalcolo»: un nome che l'utente non conosce, per un permesso che
            # dal cantiere R6 non serve piu'.  Adesso la frase dice l'unica cosa
            # che tocca a lui: quando ha finito di caricare, preme il pulsante.
            message = "1 documento caricato e letto." if acquisiti == 1 else f"{acquisiti} documenti caricati e letti."
            message += " Premi «Confronta i listini» quando hai finito di caricare."
        else:
            message = (
                "1 documento era già presente: nessuna nuova copia creata."
                if duplicati == 1
                else f"{duplicati} documenti erano già presenti: nessuna nuova copia creata."
            )
        if duplicati and acquisiti:
            message += f" Altri {duplicati} erano già presenti e non sono stati ricopiati." if duplicati > 1 else " Un altro era già presente e non è stato ricopiato."
        if rinominati:
            message += " " + "; ".join(rinominati) + "."
        if letti_per_il_contenuto:
            coda = "letto per quello che è" if len(letti_per_il_contenuto) == 1 else "letti per quello che sono"
            message += " " + "; ".join(letti_per_il_contenuto) + f": {coda}, non per il nome."
        # I nomi dei documenti appena entrati e, quando il registro li
        # riconosce, i fornitori: e' quello che la fascia in pagina nomina —
        # «CIPRESSO» dice piu' di «Listino3_34.xlsx».
        nomi_caricati = [
            str(voce.get("name") or "") for voce in saved
            if str(voce.get("status") or "") == "profiled"
        ]
        fornitori_caricati: list[str] = []
        for voce in saved:
            adattatore_id = str(voce.get("adapterId") or "")
            if str(voce.get("status") or "") != "profiled" or not adattatore_id:
                continue
            try:
                adattatore = registro.adattatore(adattatore_id)
            except (KeyError, ValueError):
                continue
            # ⚠ Solo i fornitori veri. L'elenco del gestionale ha un
            # adattatore come tutti (`gestionale_v1`) ma non ha un
            # `supplier_id`, e `supplier_label("")` risponde «FORNITORE» —
            # il ripiego generico del registro. Caricando elenco e listino
            # insieme, la fascia diceva «Hai caricato i listini BETULLA e
            # FORNITORE dopo l'ultimo confronto»: visto in pagina il 23 agosto
            # 2026, con il servizio vero. Il `kind` del registro sa gia' chi e'
            # chi, e la guardia `if etichetta` non poteva bastare, perche'
            # «FORNITORE» e' una stringa piena.
            supplier_id = str((adattatore or {}).get("supplier_id") or "")
            if not supplier_id or (adattatore or {}).get("kind") == "master":
                continue
            etichetta = supplier_label(supplier_id)
            if etichetta and etichetta not in fornitori_caricati:
                fornitori_caricati.append(etichetta)
        stato_pipeline = self.pipeline_jobs.input_modificato(
            "caricato", documenti=nomi_caricati, fornitori=sorted(fornitori_caricati),
        )
        return {
            "ok": True,
            "status": "PROFILED_AI_PENDING",
            "files": saved,
            "message": message,
            "pipeline": stato_pipeline,
        }

    # Le chiavi che fanno di una dichiarazione del registro una regola di
    # scrittura utilizzabile da sola, senza la mappatura confermata dall'utente.
    CHIAVI_REGOLA_DI_SCRITTURA = ("sheet", "header_row", "data_start_row", "order_column", "expected_header")

    @staticmethod
    def default_write_rule(supplier: str, percorso: Path | None = None) -> dict[str, Any] | None:
        """La regola di scrittura dichiarata dal registro per questo fornitore.

        Il registro dichiara gia' BETULLA in colonna C e LARICE in D: averlo anche
        scritto qui significava **due verita' sullo stesso dato**, e soprattutto
        un fornitore imparato non ereditava niente — restava per sempre senza
        regola di ripiego (revisione del 14 agosto 2026).

        Torna `None` quando il registro dice `from_field_mapping`: li' foglio e
        righe vengono dalla mappatura confermata dall'utente, quindi non esiste
        una regola *predefinita* e la vera dev'essere in configurazione. E'
        quello che succedeva prima per CIPRESSO e Noce, e non cambia.
        """

        chiave = str(supplier or "").strip().casefold()
        if not chiave:
            return None
        for voce in registro.adattatori(percorso):
            if str(voce.get("supplier_id") or "").strip().casefold() != chiave:
                continue
            regola = registro.scrittura_ordine(voce)
            if not regola or regola.get("from_field_mapping"):
                continue
            essenziale = {
                nome: regola[nome]
                for nome in ReviewStore.CHIAVI_REGOLA_DI_SCRITTURA
                if regola.get(nome) not in (None, "")
            }
            if essenziale.get("order_column") and essenziale.get("sheet"):
                essenziale.setdefault("data_start_row", 2)
                return essenziale
        return None

    def configurazione_di_un_altra_run(self, config: dict[str, Any], plan: dict[str, Any]) -> str:
        """La difesa decisiva: si scrive solo se la configurazione e' del piano che si compila.

        ⚠ Dopo un ricalcolo i listini sono altri file, con altri nomi e altre
        righe.  Se `writer_config.json` non viene riscritto — il processo muore
        nel mezzo, il riavvio non c'e' stato, la riconfigurazione fallisce — la
        compilazione prende il listino della settimana scorsa e ci mette dentro
        i numeri di riga di adesso: la quantita' di un prodotto finisce sulla
        riga di un altro, e non se ne accorge nessuno.  L'impronta non basta:
        controlla che il file non sia cambiato **da quando e' stato verificato**,
        ed e' vera anche quando il file e' quello sbagliato ma coerente con se
        stesso; e comunque la si guarda solo per due fornitori su quattro.

        ⚠ Il confronto si fa con il **piano**, non con il confronto vivo riletto
        dal disco.  La prima versione rileggeva `review_data.json`, e la
        revisione avversariale del 13 agosto 2026 ha aperto la finestra: se un
        ricalcolo finisce fra la costruzione del piano e questa guardia,
        configurazione e confronto passano insieme alla run nuova mentre il
        piano — cioe' le righe che stanno per essere scritte — e' della run
        vecchia, e la guardia direbbe di si' proprio a cio' che deve fermare
        (i due lucchetti, `store.lock` e `lucchetto_lavori`, non si escludono).
        Il piano la sua run ce l'ha scritta dentro, nessuno puo' cambiarla
        sotto i piedi, ed e' la cosa che questa guardia protegge; la coerenza
        fra piano e confronto la garantisce `validate_snapshot` al momento in
        cui il piano nasce.

        Il controllo e' un confronto fra due stringhe, e vale per tutti i
        fornitori compilabili.  Fallisce **chiuso**: una configurazione che non
        dichiara nessuna run (scritta prima che il campo esistesse) e un piano
        senza run (il confronto attivo non dichiara nessun ricalcolo) sono due
        modi di non poter rispondere alla domanda, e a una domanda senza
        risposta non si scrive dentro il listino di un fornitore.
        """

        attesa = str(plan.get("run_id") or "")
        dichiarata = str(config.get("run_id") or "")
        if attesa and dichiarata and attesa == dichiarata:
            return ""
        rimedio = (
            "Premi «Ricalcola il confronto» nella pagina di importazione: la configurazione "
            "viene riscritta alla fine del confronto. Anche riavviare il programma la rifà."
        )
        if not attesa:
            return (
                "Il confronto attivo non dichiara da dove viene: non creo copie dei listini, "
                "perché non posso sapere a quale listino appartengono le righe del piano. "
                + rimedio
            )
        if not dichiarata:
            return (
                "La configurazione per creare le copie dei listini non dice a quale confronto "
                "appartiene: non creo copie, perché potrebbe puntare ai listini di una volta "
                "precedente e le righe finirebbero sbagliate. " + rimedio
            )
        # Senza tener traccia dell'ordine non si sa quale dei due sia rimasto
        # indietro: la frase non lo pretende (la prima versione diceva «rimasta
        # a un ricalcolo precedente» anche quando era il contrario).
        return (
            "La configurazione per creare le copie dei listini non appartiene allo stesso "
            "confronto di questo piano ordini: non creo copie, perché scriverebbe le quantità "
            "nelle righe dei listini di un'altra run. " + rimedio
        )

    def writer_configuration_issues(self, plan: dict[str, Any]) -> list[str]:
        """Validate every selected supplier before the writer can create a copy.

        The JSON plan is intentionally still useful when a new supplier has not
        completed the separate XLSX-writing verification.  In particular this
        prevents a BETULLA/Larice partial result when CIPRESSO is selected too.
        """

        if self.writer_config is None:
            return []
        if not self.writer_config.is_file():
            return ["La configurazione per creare le copie dei listini non è disponibile."]
        try:
            config = load_json(self.writer_config, {})
        except (OSError, json.JSONDecodeError) as exc:
            return [f"La configurazione per creare le copie non è leggibile: {exc}."]
        if not isinstance(config, dict):
            return ["La configurazione per creare le copie non è valida."]

        selected = {
            str(item.get("supplier") or "").casefold()
            for item in plan.get("orders") or []
            if isinstance(item, dict) and item.get("supplier")
        }
        if not selected:
            return ["Il piano non contiene fornitori da compilare."]

        problema_di_run = self.configurazione_di_un_altra_run(config, plan)
        if problema_di_run:
            # Si torna subito, senza gli altri controlli: se la configurazione
            # e' di un'altra run, tutto quello che c'e' scritto dentro parla di
            # altri file, e un elenco di problemi seppellirebbe l'unico che
            # conta.
            return [problema_di_run]

        files_value = config.get("supplier_files")
        rules_value = config.get("supplier_write_rules")
        if not isinstance(files_value, dict):
            return ["Manca l'elenco dei listini da compilare nella configurazione."]
        if rules_value is None:
            rules_value = {}
        if not isinstance(rules_value, dict):
            return ["Le regole di scrittura dei listini non sono valide."]
        supplier_files = {str(key).casefold(): value for key, value in files_value.items()}
        supplier_rules = {str(key).casefold(): value for key, value in rules_value.items()}
        issues: list[str] = []

        # Chi si compila in posizione non passa da Node: la sua copia e' una
        # patch sul documento del fornitore, in Python.  Un ordine fatto di soli
        # fornitori cosi' si compila anche senza il writer, e chiedere Node li'
        # vorrebbe dire rifiutare una compilazione che si puo' fare.  Chi lo
        # dichiara e' la regola di scrittura, non il nome del fornitore.
        in_posizione = {
            supplier for supplier in selected
            if procedura_di_scrittura(supplier_rules.get(supplier)) == PATCH_IN_POSIZIONE
        }
        requires_writer = bool(set(selected) - in_posizione)
        if requires_writer:
            node_value = config.get("node_executable")
            script_value = config.get("writer_script")
            if not node_value or not Path(str(node_value)).is_file():
                issues.append("Sul computer manca quello che serve per scrivere i fogli di calcolo: non posso creare le copie dei listini.")
            if not script_value or not Path(str(script_value)).is_file():
                issues.append("Non è disponibile lo strumento di compilazione dei listini.")

        for supplier in sorted(selected):
            if supplier in in_posizione:
                issues.extend(self.problemi_in_posizione(supplier, supplier_files, supplier_rules))
                continue
            source_value = supplier_files.get(supplier)
            if not isinstance(source_value, str) or not source_value.strip():
                issues.append(f"{supplier_label(supplier)} è selezionato, ma manca il suo listino configurato per la compilazione.")
                continue
            source = Path(source_value.strip()).expanduser()
            if not source.is_absolute():
                source = self.writer_config.parent / source
            source = source.resolve()
            if not source.is_file():
                issues.append(f"Il listino configurato per {supplier_label(supplier)} non è disponibile.")
                continue
            if source.suffix.casefold() != ".xlsx":
                # ⚠ Il documento non è sbagliato: è la configurazione a non dire
                # come si compila. Accusare il listino manderebbe l'utente a
                # chiedere un altro file al fornitore, che è l'unica cosa che
                # non risolve niente — mentre la procedura si dichiara dalla
                # pagina, come fa Noce con il suo `.xls`.
                if not isinstance(supplier_rules.get(supplier), dict):
                    # Nessuna regola affatto: la stessa frase che riceve un
                    # `.xlsx` nella stessa condizione, qualche riga più sotto.
                    issues.append(
                        f"{supplier_label(supplier)} è selezionato, ma manca la regola di "
                        "scrittura verificata del suo listino."
                    )
                else:
                    # ⚠ Il documento non e' sbagliato e la configurazione
                    # nemmeno per forza: le due cose vanno dette tutt'e due.
                    # Prima qui c'era «va riconfigurato dalla pagina Importa i
                    # dati», che non risolve niente — la pagina non ha nessun
                    # comando che trasformi un `.xls` in un `.xlsx`, e chi ci
                    # andava tornava indietro come prima.  Il rimedio che
                    # l'utente puo' davvero mettere in pratica e' una riga di
                    # Excel, ed e' lo stesso che dice il lanciatore quando la
                    # regola non nasce nemmeno.
                    issues.append(
                        f"Il listino di {supplier_label(supplier)} non è un .xlsx e la sua "
                        "configurazione non dichiara come compilarlo: se il fornitore lo manda "
                        "in Excel 97-2003, aprilo con Excel e salvalo come «Cartella di lavoro "
                        "di Excel (.xlsx)», poi ricaricalo."
                    )
                continue

            rule = supplier_rules.get(supplier)
            if rule is None:
                rule = self.default_write_rule(supplier)
            if not isinstance(rule, dict):
                issues.append(f"{supplier_label(supplier)} è selezionato, ma manca la regola di scrittura verificata.")
                continue
            order_column = str(rule.get("order_column") or rule.get("orderColumn") or "").strip().upper()
            sheet_name = str(rule.get("sheet_name") or rule.get("sheet") or "").strip()
            data_start_value = rule.get("data_start_row")
            if data_start_value is None:
                data_start_value = rule.get("dataStartRow")
            if data_start_value is None:
                data_start_value = 2
            data_start = number(data_start_value)
            if not re.fullmatch(r"[A-Z]{1,3}", order_column) or not sheet_name:
                issues.append(f"La regola di scrittura per {supplier_label(supplier)} è incompleta.")
                continue
            if data_start is None or data_start < 1 or int(data_start) != data_start:
                issues.append(f"La riga iniziale della regola di scrittura per {supplier_label(supplier)} non è valida.")
                continue

            # ⚠ Fin qui il controllo valeva per un fornitore solo.  Da qui in
            # giu' c'era `if supplier != "cipresso": continue`, e con lui
            # restavano fuori l'impronta del file e la verifica del documento
            # aperto — cioe' le uniche difese contro «ho eliminato il listino
            # sbagliato, ho ricaricato quello giusto con lo stesso nome e ho
            # compilato senza rifare il confronto», che scrive le quantita'
            # nelle righe del listino nuovo con i numeri di riga del confronto
            # vecchio (revisione del 14 agosto 2026).
            #
            # Adesso si verifica **quello che la regola dichiara**, per
            # chiunque: il registro decide, il codice esegue.  Un fornitore la
            # cui regola non dichiara niente non viene controllato piu' di
            # prima; uno che dichiara impronta e intestazione viene controllato
            # come Cipresso, senza che il suo nome compaia nel codice.
            etichetta_fornitore = supplier_label(supplier)
            header_row = number(rule.get("header_row") or rule.get("headerRow"))
            expected_header = normalize_header(
                rule.get("expected_header")
                or rule.get("expectedHeader")
                or rule.get("order_header")
                or rule.get("orderHeader")
            )
            expected_hash = str(rule.get("source_sha256") or rule.get("sourceSha256") or "").strip().casefold()

            # L'impronta: dichiarata da `source_rule` per **ogni** fornitore.
            if expected_hash:
                if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
                    issues.append(f"L'impronta dichiarata del listino {etichetta_fornitore} non è valida.")
                    continue
                if sha256_file(source) != expected_hash:
                    issues.append(
                        f"Il listino {etichetta_fornitore} è cambiato dopo l'ultimo confronto: "
                        "non creo copie finché non rifai il confronto."
                    )
                    continue

            # L'intestazione verificata: solo chi la dichiara.
            if expected_header:
                if sheet_name.casefold() == "first":
                    issues.append(f"Per {etichetta_fornitore} manca il nome esatto del foglio verificato.")
                    continue
                if header_row is None or header_row < 1 or int(header_row) != header_row:
                    issues.append(f"Per {etichetta_fornitore} manca la riga dell'intestazione verificata.")
                    continue

            if not expected_header and sheet_name.casefold() == "first":
                # Senza nome di foglio e senza intestazione dichiarata non c'e'
                # niente da riaprire: si resta al controllo leggero di prima.
                continue

            try:
                from openpyxl import load_workbook
                from openpyxl.utils import column_index_from_string

                workbook = load_workbook(source, read_only=True, data_only=False)
                try:
                    if sheet_name not in workbook.sheetnames:
                        issues.append(f"Il foglio verificato del listino {etichetta_fornitore} non è presente.")
                        continue
                    sheet = workbook[sheet_name]
                    if expected_header:
                        indice = column_index_from_string(order_column)
                        letto = normalize_header(sheet.cell(int(header_row), indice).value)
                        if letto != expected_header:
                            issues.append(
                                f"L'intestazione {order_column} del listino {etichetta_fornitore} "
                                f"non coincide più con {expected_header}."
                            )
                            continue
                    for order in plan.get("orders") or []:
                        if str(order.get("supplier") or "").casefold() != supplier:
                            continue
                        source_row = number(order.get("supplier_source_row"))
                        if (
                            source_row is None
                            or source_row < data_start
                            or int(source_row) != source_row
                            or source_row > sheet.max_row
                        ):
                            issues.append(
                                f"Una riga selezionata per {etichetta_fornitore} non appartiene "
                                "al listino verificato."
                            )
                            break
                finally:
                    workbook.close()
            except Exception as exc:
                issues.append(
                    f"Non riesco a verificare il foglio {etichetta_fornitore} prima della compilazione: {exc}."
                )
        return issues

    def problemi_in_posizione(
        self, supplier: str, supplier_files: dict[str, Any], supplier_rules: dict[str, Any]
    ) -> list[str]:
        """Che cosa impedisce di compilare in posizione, detto prima di scrivere.

        Il controllo pesante — che la colonna d'ordine sia ancora tutta fatta di
        numeri a lunghezza fissa — si fa qui e non solo al momento di scrivere:
        e' la condizione da cui dipende tutta la patch, e va rifatta **a ogni
        file**, perche' basta che una settimana il fornitore ci metta una
        formula perche' non valga piu'.

        Vale per chiunque dichiari `patch_xls_in_posizione`, non per un nome
        cablato: oggi e' Noce, domani e' il fornitore che l'utente ha
        configurato dalla pagina.
        """

        from xls_writer import XlsError as ErroreXls, controlla_colonna_ordine

        nome = supplier_label(supplier)
        problemi: list[str] = []
        raw = supplier_files.get(supplier)
        if not isinstance(raw, str) or not raw.strip():
            return [f"{nome} è selezionato, ma manca il suo listino configurato per la compilazione."]
        source = Path(raw.strip()).expanduser()
        if not source.is_absolute() and self.writer_config is not None:
            source = self.writer_config.parent / source
        source = source.resolve()
        if not source.is_file():
            return [f"Il listino configurato per {nome} non è disponibile."]
        regola = supplier_rules.get(supplier)
        if not isinstance(regola, dict):
            return [f"{nome} è selezionato, ma manca la regola di scrittura verificata del suo listino."]
        colonna = str(regola.get("order_column") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{1,3}", colonna):
            problemi.append(f"La colonna d'ordine verificata di {nome} non è dichiarata nel registro.")
        impronta = str(regola.get("source_sha256") or "").strip().casefold()
        if impronta and sha256_file(source) != impronta:
            problemi.append(f"Il listino {nome} è cambiato dopo la verifica: va ricontrollato.")
        if problemi:
            return problemi
        try:
            esito = controlla_colonna_ordine(
                source,
                colonna_ordine=colonna,
                foglio=str(regola.get("sheet") or "") or None,
                prima_riga=int(number(regola.get("data_start_row")) or 1),
            )
        except ErroreXls as exc:
            return [f"Il listino {nome} non si lascia leggere per la compilazione: {frase(exc)}"]
        if not esito.get("compilabile"):
            quante = len(esito.get("celle_di_altro_tipo") or {})
            problemi.append(
                f"La colonna d'ordine {colonna} del listino {nome} ha {quante} celle che non "
                "sono numeri a lunghezza fissa: il loro documento non si può compilare in "
                f"posizione e l'ordine {nome} va preparato a mano."
            )
        return problemi

    @staticmethod
    def righe_di_listino_contese(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Due righe del piano che finiscono nella **stessa cella** del listino.

        ⚠ I due compilatori — il writer Node e `xls_writer` per Noce —
        sommano le quantita' di due righe del piano che puntano alla stessa riga
        del listino, e per due articoli del gestionale che sono lo stesso
        articolo del fornitore quella somma e' giusta.

        Non lo e' quando le due righe ordinano **unita' diverse**, ed e' un caso
        che il programma sa costruire da solo: l'offerta di un espositore porta
        il numero di riga del suo **collo padre** (`display_offer`), e la riga
        padre resta ordinabile per conto suo.  Chi ordina 4 colli e 6 espositori
        dello stesso prodotto si vedrebbe scrivere `10` in una cella sola, e il
        fornitore leggerebbe dieci di qualcosa che nessuno ha ordinato.

        Sommare non si puo' e indovinare nemmeno: qui ci si ferma e si dice
        quali due prodotti se la contendono.  Il numero da scrivere lo decide
        l'utente, spostando una delle due righe o azzerandola.
        """

        per_riga: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for order in orders:
            riga = number(order.get("supplier_source_row"))
            fornitore = str(order.get("supplier") or "").strip().casefold()
            if riga is None or not fornitore:
                continue
            per_riga.setdefault((fornitore, int(riga)), []).append(order)
        errori: list[dict[str, Any]] = []
        for (fornitore, riga), voci in sorted(per_riga.items()):
            if len(voci) < 2:
                continue
            unita = {str(voce.get("desired_quantity_unit") or "") for voce in voci}
            fattori = {round(float(number(voce.get("quantity_factor")) or 0), 6) for voce in voci}
            if len(unita) == 1 and len(fattori) == 1:
                # Lo stesso articolo del fornitore comprato per due articoli del
                # gestionale: la somma e' quello che si vuole.
                continue
            altri = ", ".join(
                f"«{str(voce.get('description') or voce.get('product_id'))}»" for voce in voci[1:]
            )
            errori.append({
                "code": "RIGA_LISTINO_CONTESA",
                "productId": str(voci[0].get("product_id") or ""),
                "productName": str(voci[0].get("description") or voci[0].get("product_id") or ""),
                "supplierId": fornitore,
                "supplierName": supplier_label(fornitore),
                "sourceRow": riga,
                "otherProducts": altri,
            })
        return errori

    def compile(self, snapshot: Any) -> dict[str, Any]:
        with self.lock:
            # ⚠ Non mentre la catena lavora.  `compile` prende il lucchetto dei
            # dati, non quello dei lavori: finche' la fase 9 non sostituisce il
            # confronto vivo, compilare adesso e' legittimo e produce i listini
            # del confronto **precedente** — con i prezzi della settimana
            # scorsa, mentre in pagina 1 una barra dice che si sta aggiornando.
            # Chi preme crede di compilare quello che sta nascendo.  Aspettare
            # non costa niente: la catena finisce da sola e il pulsante torna.
            if self.pipeline_jobs.in_corso():
                raise LavoroGiaInCorso(
                    "Il confronto si sta aggiornando: i listini si compilano quando ha finito, "
                    "altrimenti sarebbero quelli di prima."
                )
            clean, errors, review = self.validate_snapshot(snapshot, for_compile=True)
            if errors:
                raise SnapshotError(errors)
            selections = {str(item["id"]): item for item in clean["products"]}
            review = self.promotion_service.decorate(review, selections)
            products_by_id = {str(product.get("id")): product for product in review.get("products") or []}
            supplier_defs = {str(item.get("id")): item for item in review.get("suppliers") or []}
            totals: dict[str, float] = {supplier: 0.0 for supplier in supplier_defs}
            orders = []
            # I prodotti che il gestionale chiede e che nessun fornitore porta.
            # Non entrano nel piano né nei listini dei fornitori — non c'è
            # nessuna riga su cui scrivere — ma non si perdono: escono in un
            # foglio a parte, che è l'unica cosa che si può fare con loro.
            da_reperire: list[dict[str, Any]] = []
            for decision in clean["products"]:
                if decision["quantity"] <= 0:
                    continue
                product = products_by_id[decision["id"]]
                supplier = decision["selectedSupplierId"]
                if not supplier and nessuna_offerta_utilizzabile(product):
                    da_reperire.append({
                        "ean": str(product.get("ean") or ""),
                        "descrizione": str(product.get("name") or product.get("description") or ""),
                        "quantita": decision["quantity"],
                        "unita": "espositori" if str(
                            product.get("itemType") or product.get("kind") or ""
                        ).casefold() == "display" else "colli",
                        "ultimo_prezzo": product.get("lastUnitPrice"),
                        "motivo": da_reperire_modulo.motivo(product.get("offers") or []),
                    })
                    continue
                offer = find_offer(product, supplier)
                pricing = offer_pricing(offer)
                if pricing is None:
                    errors.append({
                        "code": "PREZZO_NON_VALIDO",
                        "productId": decision["id"],
                        "productName": str(product.get("name") or product.get("description") or decision["id"]),
                        "supplierId": supplier,
                        "supplierName": supplier_label(supplier) if supplier else "",
                    })
                    continue
                offer = offer or {}
                factor = pricing["factor"]
                unit_price = pricing["unitPriceNet"]
                order_price = pricing["orderUnitPriceNet"]
                is_display = str(product.get("itemType") or product.get("kind") or "").casefold() == "display"
                desired_quantity = decision["quantity"]
                # L'utente inserisce direttamente i colli (o gli espositori): nessun
                # arrotondamento pezzi->colli, per nessun tipo di articolo o fornitore.
                order_quantity = desired_quantity
                delivered_pieces = int(round(order_quantity * factor))
                excess_pieces = 0
                line_total = round(order_quantity * order_price, 4)
                totals[supplier] = round(totals.get(supplier, 0.0) + line_total, 4)
                orders.append({
                    "product_id": decision["id"],
                    "item_type": str(product.get("itemType") or product.get("kind") or "product").lower(),
                    "gestionale_source_row": product.get("sourceRow") or product.get("source_row"),
                    "ean": product.get("ean") or "",
                    "description": product.get("name") or product.get("description") or "",
                    "supplier": supplier,
                    "supplier_source_row": offer.get("sourceRow") or offer.get("source_row"),
                    "supplier_ean": offer.get("ean") or "",
                    "supplier_description": offer.get("description") or product.get("name") or "",
                    "quantity": order_quantity,
                    "desired_quantity": desired_quantity,
                    "desired_quantity_unit": "espositori" if is_display else "colli",
                    "delivered_pieces": delivered_pieces,
                    "excess_pieces": excess_pieces,
                    "unit_price_net": round(unit_price, 6),
                    "quantity_factor": factor,
                    "order_unit_price_net": round(order_price, 6),
                    "line_total_net": line_total,
                    "match_method": offer.get("method") or offer.get("matchStatus") or offer.get("match_status"),
                    "match_confidence": offer.get("confidence"),
                    "confirmed": decision["confirmed"],
                    "promotions": offer.get("promotions") or [],
                })
            errors.extend(self.righe_di_listino_contese(orders))
            if errors:
                raise SnapshotError(errors)

            below = []
            for supplier, total in totals.items():
                threshold = supplier_threshold(supplier_defs.get(supplier, {}))
                if not meets_threshold(total, threshold):
                    below.append({"supplier": supplier, "total_net": round(total, 2), "threshold_net": round(threshold, 2)})
            if below and not clean["acceptBelowThreshold"]:
                raise SnapshotError([{"code": "SOGLIA_NON_CONFERMATA", **item} for item in below])


            plan = {
                "schema_version": 1,
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "source_review": str(self.review_path),
                "source_state": str(self.state_path),
                # ⚠ La run del piano e' quella del confronto che ha risolto le
                # offerte QUI DENTRO, non quella dichiarata dallo snapshot del
                # browser: quando il confronto non dichiara nessuna run le due
                # cose divergono (validate_snapshot non ha niente contro cui
                # verificare), e il piano non deve poter "appartenere" a una run
                # per sola parola del client.
                "run_id": str((review.get("run") or {}).get("id") or ""),
                "threshold_override_confirmed": clean["acceptBelowThreshold"],
                "below_threshold": below,
                "totals_net": {supplier: round(total, 2) for supplier, total in totals.items()},
                "promotion_summary": review.get("promotionSummary") or {},
                "promotions": review.get("promotions") or [],
                "orders": orders,
            }
            # Lo stato lo riscrive la compilazione, non la scheda: se da qui in
            # poi qualcosa va storto, chi si trova indietro di una versione deve
            # sapere che a superarla e' stata la sua stessa compilazione.
            clean["stateVersionOrigin"] = "compilazione"
            atomic_json(self.state_path, clean)
            # Da qui in poi si scrive.  La cartella nasce **dopo** l'ultima
            # convalida — soglia compresa — perche' una compilazione rifiutata
            # non deve lasciare in giro una cartella vuota che l'elenco delle
            # compilazioni mostrerebbe come una consegna avvenuta.
            momento = datetime.now().astimezone()
            cartella = consegna.crea_cartella(self.orders_dir, momento)
            # La cartella esiste solo se la consegna arriva in fondo.  L'audit
            # si scrive per ultimo, e finche' non c'e' la cartella e' un lavoro
            # a meta': l'elenco delle compilazioni la mostrerebbe come una
            # consegna avvenuta, con lo zip scaricabile e le copie mai passate
            # dal controllo di fedelta'.  Prima qui c'era solo `except ValueError`
            # attorno alla scrittura, quindi bastava un guasto di un'altra
            # famiglia — un `.xls` illeggibile, il timeout del writer, il disco —
            # per lasciarla li' (revisione del 14 agosto 2026).
            consegna_completata = False
            # ⚠ Lo storico si tocca **prima** dell'audit, e l'audit puo'
            # fermarsi: disco pieno, antivirus, file bloccato. Fin qui il
            # `finally` cancellava la cartella e lo storico restava com'era
            # stato riscritto, cioe' con l'ordine nuovo — mai consegnato — e
            # senza quello vero, che `record_plan` toglie perche' sostituibile.
            # La settimana dopo il programma chiedeva «e' arrivata?» di merce
            # che nessuno aveva ordinato: il sintomo chiuso il 13 agosto 2026.
            # Le due parti restano allineate, come nell'eliminazione di una
            # compilazione (revisione del 6 settembre 2026).
            storico_di_prima = self.history_path.read_bytes() if self.history_path.exists() else None
            try:
                plan_path = cartella / consegna.NOME_PIANO
                atomic_json(plan_path, plan)
                # Se non c'è niente da reperire il file non nasce, e la pagina
                # non ha niente di nuovo da mostrare: una settimana in cui tutto
                # si può ordinare è il caso normale, non una notizia.
                if da_reperire:
                    da_reperire_modulo.scrivi(cartella, da_reperire, momento)
                history_issues: list[str] = []
                writer_issues: list[str] = []
                # Le due famiglie restano separate anche dopo essere state unite
                # in `writer_issues`: parlano di cose opposte — una copia che non
                # e' stata consegnata e una copia che e' stata consegnata con
                # una riga da ricontrollare — e il messaggio le tratta in modo
                # diverso.
                avvisi_writer: list[str] = []
                infedeli: list[str] = []
                copie: list[tuple[str, Path, Path]] = []
                problemi_consegna: list[str] = []
                status = "PLAN_READY"
                # Perche' le copie non ci sono, quando non ci sono. Vuoto vuol
                # dire «nessuno ci ha nemmeno provato»: la frase la compone
                # `messaggio_della_compilazione`, che e' l'unico posto in cui
                # si decide che cosa l'utente legge.
                copie_non_create: list[str] = []
                if self.writer_config:
                    writer_issues = self.writer_configuration_issues(plan)
                    if writer_issues:
                        copie_non_create = list(writer_issues)
                    else:
                        try:
                            _generated, prodotte, avvisi_writer = self.run_writer(plan_path, cartella)
                        except ValueError as exc:
                            # Il writer verifica tutto prima di rendere visibili le
                            # copie. Il piano resta quindi consegnabile anche quando
                            # una verifica finale del listino fallisce.
                            #
                            # ⚠ Il motivo va in `writer_issues`, non solo nel
                            # `message`: e' lo stesso campo che riempie il ramo
                            # qui sopra quando a mancare e' la configurazione, ed
                            # e' l'unico che la pagina legge per capire che la
                            # compilazione **non** e' riuscita per intero. Finche'
                            # stava solo nella frase, una compilazione senza
                            # nessuna copia si presentava col riquadro verde e il
                            # pulsante primario, come una consegna completa
                            # (17 agosto 2026). Il codice `FORNITORE_SENZA_COPIA`
                            # che la pagina traduceva era il posto previsto per
                            # questa notizia e non e' mai stato riempito: il
                            # canale vero e' questo, e ha gia' l'intestazione
                            # giusta («Listini non preparati o da ricontrollare»).
                            writer_issues = [frase(exc) or "Le copie dei listini non sono state create."]
                            copie_non_create = list(writer_issues)
                        else:
                            # ⚠ Prima di consegnare qualsiasi cosa: la copia e' il
                            # listino del fornitore o e' un'altra cosa?  Il writer
                            # guarda solo le celle che voleva scrivere; questa
                            # guardia le guarda tutte.  Una copia che non regge non
                            # esce di qui.
                            prodotte, infedeli = self.scarta_copie_infedeli(plan, prodotte)
                            # Gli avvisi del writer stanno **davanti**: parlano
                            # delle copie che si consegnano, e chi legge deve
                            # trovarli anche quando nessuna copia e' stata
                            # scartata.  Fino a oggi morivano sulla console.
                            writer_issues = [*avvisi_writer, *infedeli]
                            if prodotte:
                                # La rinomina non puo' far fallire una compilazione
                                # riuscita: le copie ci sono e sono giuste, e un nome
                                # brutto si dice, non si trasforma in un guasto.
                                copie, problemi_consegna = self.rinomina_listini(prodotte, momento)
                                status = "FILES_READY"
                            else:
                                # ⚠ Qui il motivo sono le copie **scartate**, non
                                # gli avvisi del writer: quelli dicono «la
                                # quantita' e' stata scritta lo stesso», e messi
                                # dentro una frase che dichiara che non e' stato
                                # creato niente si contraddicono a vicenda.
                                copie_non_create = list(infedeli)
                # ⚠ La registrazione nello storico sta QUI, dopo la scrittura, e non
                # prima: fino al 13 agosto 2026 stava subito dopo il piano, e una
                # compilazione fermata dai `writer_issues` — o dal writer che non
                # parte — registrava lo stesso l'ordine.  La settimana dopo il
                # programma chiedeva «è arrivata?» di merce che nessuno aveva mai
                # potuto ordinare, e segnava quei prodotti come «già ordinati».
                # La regola è una sola: entra nello storico solo il fornitore di cui
                # esiste la copia del listino sul disco.
                if status == "FILES_READY":
                    history_issues = self.record_order_history(
                        plan,
                        order_key=cartella.name,
                        # Casefold: `record_plan` raggruppa il piano casefoldato, e
                        # un fornitore con una maiuscola non si incontrava mai con
                        # la sua copia — ordine sul disco, storico muto (revisione
                        # avversariale R4).
                        delivered={str(fornitore).strip().casefold()
                                   for fornitore, _sorgente, _copia in copie},
                    )
                elif status == "PLAN_READY":
                    # Senza writer non nasce nessuna copia, quindi nessun ordine:
                    # e' la regola. Ma spegnere TUTTO lo storico — domande, avvisi,
                    # scadenze — senza una parola trasformava una modalita'
                    # ordinaria (manca Node, manca un listino) in una perdita
                    # silenziosa (revisione avversariale R4).
                    history_issues = [
                        "Questa compilazione non entra fra gli ordini da controllare: senza le "
                        "copie dei listini non c'è nessun ordine da mandare, quindi nessuna "
                        "domanda «è arrivata?» nascerà per lei. Il piano ordini JSON resta "
                        "scaricabile."
                    ]
                # L'elenco dei file si legge dal disco, non dalla lista che abbiamo
                # in mano: se il writer ha lasciato qualcosa che nessuno aspettava,
                # l'audit lo deve dire invece di descrivere una cartella immaginaria.
                file_audit = self.descrivi_cartella(cartella, copie)
                message = self.messaggio_della_compilazione(
                    status=status,
                    writer_configurato=bool(self.writer_config),
                    file_audit=file_audit,
                    copie_non_create=copie_non_create,
                    infedeli=infedeli,
                    avvisi_writer=avvisi_writer,
                    problemi_consegna=problemi_consegna,
                    history_issues=history_issues,
                )
                fornitori_ordinati = sorted({str(item["supplier"]) for item in orders})
                totali_netti = {supplier: round(totals[supplier], 2) for supplier in fornitori_ordinati}
                # L'audit si scrive **per ultimo**: quando c'e', dice che tutto il
                # resto della cartella era gia' al suo posto.
                consegna.scrivi_audit(cartella, {
                    "schema_audit": consegna.SCHEMA_AUDIT,
                    "cartella": cartella.name,
                    "run_id": clean["runId"],
                    "creato_il": momento.isoformat(),
                    "stato": status,
                    "messaggio": message,
                    "fornitori": fornitori_ordinati,
                    "totali_netti": totali_netti,
                    "totale_netto": round(sum(totali_netti.values()), 2),
                    "righe": len(orders),
                    "sotto_soglia": below,
                    "avvisi": [*writer_issues, *problemi_consegna, *history_issues],
                    "file": file_audit,
                })
                # Da qui la cartella e' una consegna vera e resta sul disco.
                consegna_completata = True
                # La voce si rilegge dall'audit appena scritto: cosi' la risposta
                # della compilazione e la riga di `GET /api/ordini` non possono
                # raccontare due cose diverse della stessa cartella.
                voce = consegna.voce(cartella, etichetta_fornitore=supplier_label)
                return {
                    "ok": True,
                    "status": status,
                    "message": message,
                    # Anche la compilazione riscrive tutto lo stato: senza
                    # rimandare la versione nuova, la scheda che ha compilato si
                    # troverebbe rifiutato il salvataggio successivo.
                    "stateVersion": clean["stateVersion"],
                    "cartella": cartella.name,
                    "outputs": [
                        {"name": item["nome"], "tipo": item["tipo"], "url": item["url"]}
                        for item in voce["file"]
                    ],
                    "zipUrl": voce["zipUrl"],
                    "zipNome": voce["zipNome"],
                    "totalsNet": plan["totals_net"],
                    "belowThreshold": below,
                    "writerIssues": writer_issues,
                    # ⚠ La consegna aveva i suoi avvisi solo dentro `message`:
                    # un nome rimasto brutto perche' `os.rename` e' fallito non
                    # colorava niente, il riquadro restava verde con il pulsante
                    # primario, e al ricaricamento la notizia spariva. Adesso
                    # sono un campo, e l'audit li conserva (revisione del 14
                    # agosto 2026).
                    "deliveryIssues": problemi_consegna,
                    "historyIssues": history_issues,
                }
            finally:
                if not consegna_completata:
                    shutil.rmtree(cartella, ignore_errors=True)
                    if storico_di_prima is None:
                        self.history_path.unlink(missing_ok=True)
                    else:
                        # Lo stesso aiutante degli altri: temporaneo con un nome
                        # suo e byte sul disco prima di sostituire, perche' e' il
                        # ramo che deve funzionare quando qualcosa e' gia'
                        # andato storto.
                        scrittura_sicura.scrivi_bytes(self.history_path, storico_di_prima)

    @staticmethod
    def spiegazione_del_writer(stderr: str | None, stdout: str | None) -> str:
        """Che cosa legge l'utente quando il writer si ferma.

        ⚠ Prima qui si ricopiavano gli ultimi quattromila caratteri di `stderr`.
        Quando il listino cambiava dopo la verifica — il caso piu' frequente e
        il piu' innocuo, basta ricontrollarlo — l'utente si trovava in pagina la
        traccia di Node con i percorsi assoluti del computer, mentre il writer
        la frase in italiano ce l'aveva gia' pronta e la stava dicendo.

        Il writer marca la sua frase con `ERRORE_COMPILAZIONE:` su una riga
        sola.  Qui si prende quella.  Se non c'e' — un guasto che il writer non
        ha previsto — si dice che si e' fermato, **senza** riversare in pagina
        la traccia: i dettagli tecnici restano sulla console del programma, che
        e' il posto dove servono.
        """

        marca = "ERRORE_COMPILAZIONE:"
        righe = str(stderr or "").splitlines()
        for riga in reversed(righe):
            testo = riga.strip()
            if testo.startswith(marca):
                spiegazione = testo[len(marca):].strip()
                if spiegazione:
                    # Quello che accompagna la frase marcata — la traccia, il
                    # `cause` di un guasto imprevisto — e' dettaglio tecnico:
                    # resta sulla console del programma, non in pagina.
                    tecnico = "\n".join(
                        r for r in righe if not r.strip().startswith(marca)
                    ).strip()
                    if tecnico:
                        print(f"[WRITER] {tecnico[-4000:]}")
                    return spiegazione
        tecnico = (str(stderr or "") or str(stdout or "")).strip()
        if tecnico:
            print(f"[WRITER] {tecnico[-4000:]}")
        return (
            "lo strumento che crea le copie dei listini si è fermato senza spiegare perché. "
            "Il piano ordini è completo e si può scaricare."
        )

    def scarta_copie_infedeli(
        self, plan: dict[str, Any], prodotte: list[tuple[str, Path, Path]]
    ) -> tuple[list[tuple[str, Path, Path]], list[str]]:
        """Riapre ogni copia e la confronta **cella per cella** con il suo listino.

        ⚠ E' la difesa che mancava il 12 agosto 2026, quando la copia LARICE
        consegnabile aveva 34 celle EAN con scritto «1235» al posto del nulla e
        aveva perso 641 titoli di sezione — e il programma diceva che era andato
        tutto bene, perche' guardava soltanto le celle che aveva scritto.

        Le uniche differenze ammesse sono nella colonna d'ordine: le quantita'
        del piano e l'azzeramento delle quantita' che c'erano gia'.  Qualunque
        altra differenza fa fallire la compilazione di **quel** fornitore: la
        copia viene cancellata e il motivo si dice, invece di consegnare un
        documento che somiglia al listino senza esserlo.

        Restituisce le copie che si possono consegnare e le frasi da mostrare.
        """

        from copia_fedele import confronta_copia, frase_di_rifiuto

        config = load_json(self.writer_config, {}) if self.writer_config else {}
        regole_value = config.get("supplier_write_rules") if isinstance(config, dict) else None
        regole = regole_value if isinstance(regole_value, dict) else {}
        buone: list[tuple[str, Path, Path]] = []
        problemi: list[str] = []
        for fornitore, sorgente, copia in prodotte:
            # A Noce si manda il **loro** `.xls`, compilato in posizione:
            # li' la fedelta' non e' un confronto, sono i byte che non si sono
            # mossi, e `app/xls_writer.py` la difende gia' da solo.
            if copia.suffix.casefold() != ".xlsx":
                buone.append((fornitore, sorgente, copia))
                continue
            regola = regole.get(fornitore)
            if not isinstance(regola, dict):
                regola = self.default_write_rule(fornitore) or {}
            colonna = str(regola.get("order_column") or regola.get("orderColumn") or "").strip()
            prima_riga = number(regola.get("data_start_row") or regola.get("dataStartRow")) or 2
            quantita: dict[int, int] = {}
            for order in plan.get("orders") or []:
                if str(order.get("supplier") or "").casefold() != fornitore:
                    continue
                riga = number(order.get("supplier_source_row"))
                colli = number(order.get("quantity"))
                if riga is None or colli is None:
                    continue
                quantita[int(riga)] = quantita.get(int(riga), 0) + int(colli)
            try:
                esito = confronta_copia(
                    sorgente,
                    copia,
                    colonna_ordine=colonna,
                    prima_riga=int(prima_riga),
                    quantita=quantita,
                    foglio_ordine=str(regola.get("sheet") or regola.get("sheet_name") or "") or None,
                )
            except Exception as errore:  # noqa: BLE001 - `ConfrontoImpossibile` compreso
                # Una copia che non si riesce a verificare non si consegna: e'
                # la stessa regola del resto del programma, dove il dubbio si
                # dichiara e non si spedisce.
                copia.unlink(missing_ok=True)
                problemi.append(
                    f"La copia per {supplier_label(fornitore)} non è stata verificata e quindi "
                    f"non viene consegnata: {frase(errore)}"
                )
                continue
            if esito.fedele:
                buone.append((fornitore, sorgente, copia))
                continue
            copia.unlink(missing_ok=True)
            problemi.append(frase_di_rifiuto(supplier_label(fornitore), esito))
        return buone, problemi

    @staticmethod
    def messaggio_dei_file(file_audit: list[dict[str, Any]]) -> str:
        """Il riepilogo di che cosa è stato generato, contato sul disco vero."""

        workbook_count = sum(1 for item in file_audit if item["tipo"] == "listino")
        parts = ["piano ordini JSON"]
        if workbook_count:
            parts.append(f"{workbook_count} copia del listino" if workbook_count == 1 else f"{workbook_count} copie dei listini")
        summary = " e ".join(parts)
        summary = summary[:1].upper() + summary[1:]
        verb = "generato" if len(parts) == 1 else "generati"
        return f"{summary} {verb}. Gli originali sono rimasti invariati; nessun ordine è stato inviato."

    @staticmethod
    def messaggio_della_compilazione(
        *,
        status: str,
        writer_configurato: bool,
        file_audit: list[dict[str, Any]],
        copie_non_create: list[str],
        infedeli: list[str],
        avvisi_writer: list[str],
        problemi_consegna: list[str],
        history_issues: list[str],
    ) -> str:
        """La frase che l'utente legge a compilazione finita, e che va nell'audit.

        Era una sessantina di righe di `message = f"{message} …"` innestate in
        sei rami dentro `compile`, che e' anche la funzione piu' lunga del
        programma: ogni caso nuovo andava infilato in un `if` dentro un `try`
        dentro un `with`, e sbagliare l'ordine di due `message` e' gia'
        successo.  Qui dentro non c'e' niente da orchestrare — nessuno stato,
        nessun disco — quindi i sei casi si provano con una tabella, che prima
        si potevano guardare solo compilando davvero.

        I sei casi, nell'ordine in cui si aggiungono:

        1. **senza writer configurato**: nessuna copia, e si dice che cosa
           manca per averle;
        2. **copie non create**: la configurazione c'e' ma qualcosa l'ha
           fermata, oppure sono state tutte scartate.  `copie_non_create`
           porta il motivo — ⚠ e nel caso delle copie scartate sono gli
           `infedeli`, non gli avvisi del writer: quelli dicono «la quantita'
           e' stata scritta lo stesso» e dentro una frase che dichiara che non
           e' stato creato niente si contraddicono;

        ⚠ A separare il primo caso dal secondo e' `writer_configurato`, non il
        fatto che `copie_non_create` sia vuoto.  Sono due cose diverse — «non
        ci ha provato nessuno» e «ci ha provato e non ne e' uscito niente» — e
        confonderle fa leggere «completa la configurazione di scrittura» a chi
        la configurazione ce l'ha gia' completa.  La prima estrazione le
        confondeva: trovato dalla verifica avversariale del 20 agosto 2026, che
        ha fatto girare 11.664 combinazioni contro il codice di prima e ne ha
        trovate 162 divergenti, tutte questo caso.  Oggi non e' raggiungibile —
        `run_writer` o solleva o produce una voce per fornitore, e una copia
        scartata mette sempre una frase negli `infedeli` — ma la frase finisce
        nell'audit della cartella, dove resta.
        3. **copie create**: il riepilogo di che cosa c'e' nella cartella;
        4. **qualche copia scartata e le altre no**: chi legge deve sapere
           quale listino manca, non contare i file;
        5. **avvisi del writer**: solo il numero. Il testo lo elenca gia' la
           pagina da `writerIssues`, e ripeterlo qui lo farebbe comparire due
           volte sotto un titolo che lo smentisce; il numero resta perche' lo
           storico delle compilazioni ha il messaggio e non l'elenco;
        6. **rinomina e storico**: un nome brutto e un ordine che non entra fra
           quelli da controllare non fanno fallire niente, ma vanno detti.
        """

        if status == "FILES_READY":
            message = ReviewStore.messaggio_dei_file(file_audit)
            if infedeli:
                message = f"{message} Attenzione: " + " ".join(infedeli)
            if avvisi_writer:
                message = (
                    f"{message} Su un listino preparato c'è una segnalazione da leggere."
                    if len(avvisi_writer) == 1 else
                    f"{message} Sui listini preparati ci sono {len(avvisi_writer)} "
                    "segnalazioni da leggere."
                )
        elif writer_configurato:
            message = (
                "Piano ordini convalidato. Non sono state create copie dei listini: "
                + " ".join(copie_non_create)
            )
        else:
            message = (
                "Piano ordini convalidato. Per creare le copie dei listini occorre "
                "completare la configurazione di scrittura."
            )
        if problemi_consegna:
            message = f"{message} Attenzione: " + " ".join(problemi_consegna)
        if history_issues:
            # Senza questo avviso la compilazione sembrerebbe perfettamente
            # riuscita e la settimana prossima mancherebbe il promemoria della
            # merce non consegnata: proprio il caso che questa funzione evita.
            message = (
                f"{message} Attenzione: questo ordine non è stato registrato fra quelli "
                "da controllare la prossima settimana. "
                + " ".join(history_issues)
            )
        return message

    @staticmethod
    def rinomina_listini(
        prodotte: list[tuple[str, Path, Path]], momento: datetime
    ) -> tuple[list[tuple[str, Path, Path]], list[str]]:
        """Da `ORDINE_LARICE_listino.xlsx` a `Ordine LARICE — 12 agosto 2026.xlsx`.

        Restituisce le copie con il nome che hanno **davvero** preso, e l'elenco
        di quelle che il nome leggibile non l'hanno potuto prendere.

        ⚠ Un nome brutto non e' un motivo per buttare via un ordine giusto.
        `os.rename` su Windows alza `PermissionError` (WinError 32) se qualcuno
        tiene aperto il file: l'antivirus che lo sta scansionando, OneDrive che
        lo sincronizza, l'utente che l'ha aperto in Excel per controllarlo.
        Sollevare li' faceva uscire la compilazione con un 500, lasciava una
        cartella senza audit e la pagina diceva «non riuscita» mentre i listini
        erano li', giusti e scaricabili.  Quindi si tiene la copia com'e' e si
        dice che cosa non ha funzionato.

        La destinazione che esiste gia' non si sovrascrive **mai**: e' il
        difetto che la 6d chiude, e rifarlo dentro la cartella nuova sarebbe
        peggio, perche' li' l'utente si aspetta che niente si tocchi piu'.
        """

        rinominate: list[tuple[str, Path, Path]] = []
        problemi: list[str] = []
        for fornitore, sorgente, prodotta in prodotte:
            # L'estensione la porta la copia, non la si sceglie qui: il
            # documento Noce e' un `.xls` e resta un `.xls`.
            # ⚠ Il nome che arriva al fornitore e' quello del **registro**, non
            # l'identificativo tecnico: `consegna.nome_listino` maiuscola quello
            # che riceve, e passandogli l'identificativo il documento partiva
            # come «Ordine NUOVO_FORNITORE_1 — 14 agosto 2026.xlsx», underscore
            # compresi. Il writer il nome giusto lo usava gia' per la copia
            # intermedia: si perdeva un passo dopo (revisione del 14 agosto).
            destinazione = prodotta.parent / consegna.nome_listino(
                supplier_label(fornitore), momento, prodotta.suffix
            )
            if destinazione == prodotta:
                rinominate.append((fornitore, sorgente, prodotta))
                continue
            if destinazione.exists():
                problemi.append(
                    f"«{prodotta.name}» è rimasto con questo nome: nella cartella "
                    f"c'era già un documento chiamato «{destinazione.name}»."
                )
                rinominate.append((fornitore, sorgente, prodotta))
                continue
            try:
                os.rename(prodotta, destinazione)
            except OSError as errore:
                problemi.append(
                    f"«{prodotta.name}» è rimasto con questo nome invece di diventare "
                    f"«{destinazione.name}»: {errore.strerror or errore}. "
                    "Il documento è completo e si può scaricare lo stesso."
                )
                rinominate.append((fornitore, sorgente, prodotta))
                continue
            rinominate.append((fornitore, sorgente, destinazione))
        return rinominate, problemi

    @staticmethod
    def descrivi_cartella(cartella: Path, copie: list[tuple[str, Path, Path]]) -> list[dict[str, Any]]:
        """L'elenco `file` dell'audit, costruito scandendo la cartella.

        Si guarda il disco e non la lista dei file che abbiamo appena scritto:
        un audit costruito dalle proprie intenzioni descrive quello che doveva
        succedere, non quello che e' successo.  Un `.xlsx` che non abbiamo
        rinominato noi resta `altro` — non sappiamo di chi sia ne' da dove
        venga, quindi non finisce fra i listini da consegnare.
        """

        listini = {prodotta.name: (fornitore, sorgente) for fornitore, sorgente, prodotta in copie}
        with os.scandir(cartella) as scansione:
            nomi = sorted(
                trovato.name for trovato in scansione
                if trovato.is_file() and consegna.e_documento(trovato.name)
            )
        righe: list[dict[str, Any]] = []
        for nome in nomi:
            try:
                byte: int | None = (cartella / nome).stat().st_size
            except OSError:
                byte = None
            if nome in listini:
                fornitore, sorgente = listini[nome]
                righe.append({
                    "nome": nome,
                    "tipo": "listino",
                    "fornitore": fornitore,
                    "origine": str(sorgente),
                    "byte": byte,
                })
                continue
            tipo = consegna.tipo_file(nome)
            righe.append({"nome": nome, "tipo": tipo if tipo != "listino" else "altro", "byte": byte})
        return righe

    @staticmethod
    def riepilogo_del_writer(stdout: str | None) -> dict[str, Any]:
        """Il riepilogo che il writer Node dichiara alla fine del lavoro.

        Non si legge tutto `stdout` come JSON: la libreria dei fogli di calcolo
        ci scrive righe sue. Il writer marca la sua con `RIEPILOGO_COMPILAZIONE:`
        apposta. Un writer piu' vecchio non la scrive: in quel caso non si sa
        niente, e non sapere non e' un guasto.
        """

        marca = "RIEPILOGO_COMPILAZIONE:"
        for riga in reversed(str(stdout or "").splitlines()):
            pulita = riga.strip()
            if not pulita.startswith(marca):
                continue
            try:
                letto = json.loads(pulita[len(marca):])
            except ValueError:
                return {}
            return letto if isinstance(letto, dict) else {}
        return {}

    @staticmethod
    def copie_dichiarate_dal_writer(
        riepilogo: dict[str, Any], destinazione: Path
    ) -> tuple[dict[str, Path], list[str]]:
        """Che cosa il writer dice di aver prodotto, e che cosa non ha potuto verificare.

        Due cose escono di qui, e la prima e' la piu' importante: **il nome
        della copia lo dichiara chi l'ha scritta**.  Il writer costruisce
        `ORDINE_<nome>_<listino>.xlsx` dal `display_name` che la regola porta
        dal registro, quindi per un fornitore imparato — identificativo
        `nuovo_fornitore_1`, nome «Sapori & Co.» — il file si chiama
        `ORDINE_SAPORI_CO_...`, e il servizio che ricalcolava il nome
        dall'identificativo cercava un documento che non esiste: compilazione
        fallita con la copia giusta li' accanto.

        La seconda: le righe che il writer ha scritto **senza poter controllare**
        che fossero la riga giusta.  Le sue frasi finivano sulla console del
        programma, cioe' in nessun posto che l'utente guardi.

        ⚠ Una copia dichiarata fuori dalla cartella della compilazione non si
        prende: il servizio consegna quello che c'e' li' dentro, e un percorso
        che punta altrove porterebbe nello zip un file che nessuno ha
        verificato.
        """

        copie: dict[str, Path] = {}
        avvisi: list[str] = []
        voci = riepilogo.get("supplier_copies")
        for voce in voci if isinstance(voci, list) else []:
            if not isinstance(voce, dict):
                continue
            fornitore = str(voce.get("supplier") or "").strip().casefold()
            if not fornitore:
                continue
            # Il nome leggibile lo dichiara il writer, che l'ha preso dal
            # registro: rifarselo qui vorrebbe dire avere due tabelle di nomi.
            etichetta = str(voce.get("supplier_name") or "").strip() or supplier_label(fornitore)
            dichiarata = voce.get("destination")
            if voce.get("skipped") or not isinstance(dichiarata, str) or not dichiarata.strip():
                # Un fornitore configurato ma non ordinato: il writer lo elenca
                # per completezza, e non c'e' nessuna copia da consegnare.
                continue
            percorso = Path(dichiarata.strip()).expanduser()
            if not percorso.is_absolute():
                percorso = destinazione / percorso
            percorso = percorso.resolve()
            if percorso.parent != destinazione.resolve():
                raise ValueError(
                    f"Il writer dichiara la copia di {etichetta} fuori dalla cartella della "
                    f"compilazione ({percorso}): non viene consegnata."
                )
            copie[fornitore] = percorso
            dette = 0
            for testo in voce.get("warnings") if isinstance(voce.get("warnings"), list) else []:
                pulita = str(testo or "").strip()
                if pulita:
                    avvisi.append(pulita)
                    dette += 1
            # Le righe non verificabili che una frase non ce l'hanno: succede
            # quando la regola di scrittura non dichiara nessuna colonna da
            # controllare, e allora **nessuna** riga di quel fornitore e' stata
            # verificata.  Senza questa frase la pagina direbbe soltanto
            # «pronto», che e' la stessa immagine di una verifica riuscita.
            non_verificabili = number(voce.get("unverifiable_rows")) or 0
            verificate = number(voce.get("verified_rows")) or 0
            silenziose = int(non_verificabili) - dette
            if silenziose > 0:
                totale = int(verificate) + int(non_verificabili)
                avvisi.append(
                    f"Di {etichetta} una riga su {totale} è stata scritta senza poter verificare "
                    "che fosse la riga giusta: controllala prima di mandare l'ordine."
                    if silenziose == 1 else
                    f"Di {etichetta} {silenziose} righe su {totale} sono state scritte senza poter "
                    "verificare che fossero le righe giuste: controllale prima di mandare l'ordine."
                )
        return copie, avvisi

    def run_writer(
        self, plan_path: Path, destinazione: Path
    ) -> tuple[list[Path], list[tuple[str, Path, Path]], list[str]]:
        """I documenti attesi, le copie prodotte e gli avvisi del writer.

        ⚠ Gli avvisi sono il terzo valore e non un campo dentro le copie: chi
        chiama deve **vederli** per forza, perche' una copia consegnata con
        righe non verificate non e' una compilazione riuscita a meta' — e' una
        compilazione riuscita di cui va detta una cosa.
        """

        if self.writer_config is None:
            raise ValueError("Configurazione di scrittura non disponibile")
        config = load_json(self.writer_config, {})
        if not isinstance(config, dict):
            raise ValueError("Configurazione di scrittura non valida")
        supplier_files_value = config.get("supplier_files")
        if not isinstance(supplier_files_value, dict):
            raise ValueError("Manca l'elenco dei listini da compilare")
        supplier_files = {str(key).casefold(): value for key, value in supplier_files_value.items()}
        rules_value = config.get("supplier_write_rules")
        supplier_rules = (
            {str(key).casefold(): value for key, value in rules_value.items()}
            if isinstance(rules_value, dict) else {}
        )
        plan = load_json(plan_path, {"orders": []})
        ordered_suppliers = {
            str(item.get("supplier") or "").casefold()
            for item in plan.get("orders") or []
            if isinstance(item, dict) and item.get("supplier")
        }
        # Chi si compila in posizione lo dichiara la sua regola di scrittura;
        # tutti gli altri passano dal writer Node.  Era `!= "noce"`.
        in_posizione = {
            supplier for supplier in ordered_suppliers
            if procedura_di_scrittura(supplier_rules.get(supplier)) == PATCH_IN_POSIZIONE
        }
        ordered_workbooks = ordered_suppliers - in_posizione
        missing_sources = [supplier_label(supplier) for supplier in sorted(ordered_workbooks) if not supplier_files.get(supplier)]
        if missing_sources:
            raise ValueError("Manca il listino configurato per: " + ", ".join(missing_sources))
        source_paths: dict[str, Path] = {}
        for supplier in ordered_workbooks:
            raw_source = Path(str(supplier_files[supplier])).expanduser()
            if not raw_source.is_absolute():
                raw_source = self.writer_config.parent / raw_source
            source_paths[supplier] = raw_source.resolve()
        expected = [plan_path]
        # Fornitore, listino di partenza e copia prodotta viaggiano insieme:
        # servono tutti e tre dopo, per la rinomina al nome leggibile e per il
        # campo `origine` dell'audit, e ricostruirli da soli i nomi dei file
        # vorrebbe dire indovinare da quale listino viene una copia.
        prodotte: list[tuple[str, Path, Path]] = []
        avvisi: list[str] = []
        # Node serve per le copie `.xlsx`, non per Noce: il loro documento
        # si compila in posizione, qui in Python.  Un ordine di solo Noce
        # non deve pretendere un writer che non gli serve.
        if ordered_workbooks:
            node_value = config.get("node_executable")
            script_value = config.get("writer_script")
            if not node_value or not script_value:
                raise ValueError("Configurazione di scrittura incompleta")
            node = Path(str(node_value)).resolve()
            script = Path(str(script_value)).resolve()
            working = Path(config.get("working_directory") or script.parent).resolve()
            missing = [str(path) for path in (node, script, working, *source_paths.values()) if not path.exists()]
            if missing:
                raise ValueError("Configurazione di scrittura incompleta; percorsi assenti: " + ", ".join(missing))
            command = [
                str(node),
                str(script),
                "--plan", str(plan_path),
                "--config", str(self.writer_config),
                # Il writer scrive nella cartella della compilazione, non piu' in
                # `outputs`: e' li' che i suoi file devono nascere, altrimenti fra
                # la scrittura e lo spostamento ci sarebbe un istante in cui la
                # compilazione precedente e' gia' stata sovrascritta.
                "--output-dir", str(destinazione),
            ]
            writer_environment = os.environ.copy()
            result = subprocess.run(
                command,
                cwd=working,
                env=writer_environment,
                capture_output=True,
                text=True,
                # ⚠ Node scrive in UTF-8.  Senza dirlo, su Windows Python
                # decodifica con la codifica della console (cp1252) e la frase
                # italiana del writer arriva all'utente con i caratteri
                # sfigurati: «Il listino LARICE Ã¨ cambiato...».
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
            if result.returncode != 0:
                raise ValueError(self.spiegazione_del_writer(result.stderr, result.stdout))
            riepilogo = self.riepilogo_del_writer(result.stdout)
            dichiarate, avvisi = self.copie_dichiarate_dal_writer(riepilogo, destinazione)
            for supplier in sorted(ordered_workbooks):
                source = source_paths[supplier]
                if riepilogo:
                    destination = dichiarate.get(supplier)
                    if destination is None:
                        # Il writer ha parlato e questo fornitore non l'ha
                        # nominato: qualunque file col nome che ci aspettiamo e'
                        # di qualcun altro o di un'altra volta.
                        raise ValueError(
                            f"Il writer non dichiara nessuna copia per {supplier_label(supplier)}, "
                            "che il piano ordina."
                        )
                else:
                    # Un writer che il riepilogo non lo scrive: resta il nome
                    # che questo servizio ha sempre ricalcolato.  Non sapere non
                    # e' un guasto, ma e' anche l'unico caso in cui il nome lo
                    # indoviniamo noi.
                    destination = destinazione / f"ORDINE_{supplier.upper()}_{source.stem}.xlsx"
                if not destination.is_file():
                    raise ValueError(f"Il writer non ha creato la copia prevista per {supplier_label(supplier)}")
                expected.append(destination)
                prodotte.append((supplier, source, destination))
        for supplier in sorted(in_posizione):
            fornitore, sorgente, copia = self.compila_in_posizione(supplier, plan, config, destinazione)
            expected.append(copia)
            prodotte.append((fornitore, sorgente, copia))
        return sorted(expected, key=lambda path: path.name.casefold()), prodotte, avvisi

    def compila_in_posizione(
        self, supplier: str, plan: dict[str, Any], config: dict[str, Any], destinazione: Path
    ) -> tuple[str, Path, Path]:
        """Al fornitore si rimanda il **suo** documento, con la sola colonna d'ordine.

        Deciso da Daniele il 12 agosto 2026 per Noce, e vale per chiunque
        dichiari `patch_xls_in_posizione` nella regola di scrittura.  Non passa
        dal writer Node, che importa ed esporta `.xlsx` e riscriverebbe il file
        da capo: qui si cambiano quattro byte per cella dentro una copia del
        `.xls`, e tutto il resto resta identico byte per byte.

        Se la patch non si puo' fare, **la compilazione di quel fornitore
        fallisce e lo dice**: non ripiega su un formato che non accetta.
        """

        from xls_writer import CompilazioneXlsError, compila_ordine  # import tardivo

        nome = supplier_label(supplier)
        regole = config.get("supplier_write_rules")
        regola = (regole or {}).get(supplier) if isinstance(regole, dict) else None
        if not isinstance(regola, dict):
            raise ValueError(
                f"{nome} è selezionato, ma manca la regola di scrittura verificata del suo listino."
            )
        sorgente = self.percorso_listino(config, supplier)
        impronta = str(regola.get("source_sha256") or "").strip().casefold()
        if impronta and sha256_file(sorgente) != impronta:
            raise ValueError(
                f"Il listino {nome} è cambiato dopo la verifica: non creo copie finché non "
                "viene ricontrollato."
            )
        righe: dict[int, int] = {}
        ean_attesi: dict[int, str] = {}
        for order in plan.get("orders") or []:
            if str(order.get("supplier") or "").casefold() != supplier:
                continue
            riga = number(order.get("supplier_source_row"))
            quantita = number(order.get("quantity"))
            if riga is None or int(riga) != riga or riga < 1 or quantita is None or int(quantita) != quantita or quantita < 1:
                raise ValueError(
                    f"Riga o quantità {nome} non valide: riga {order.get('supplier_source_row')!r}, "
                    f"quantità {order.get('quantity')!r}."
                )
            # Due righe del piano sullo stesso prodotto si sommano, come fa il
            # writer Node per gli altri fornitori.
            righe[int(riga)] = righe.get(int(riga), 0) + int(quantita)
            ean_attesi[int(riga)] = str(order.get("supplier_ean") or "")
        if not righe:
            raise ValueError(f"Il piano dichiara {nome} ma non ha nessuna riga da compilare.")

        colonna_ean = self.colonna_ean_dichiarata(sorgente, regola)
        copia = destinazione / f"ORDINE_{supplier.upper()}_{sorgente.stem}{sorgente.suffix}"
        try:
            esito = compila_ordine(
                sorgente,
                copia,
                righe,
                colonna_ordine=str(regola.get("order_column") or "I"),
                foglio=str(regola.get("sheet") or "") or None,
                ean_attesi=ean_attesi,
                colonna_ean=colonna_ean,
                prima_riga=int(number(regola.get("data_start_row")) or 1),
            )
        except CompilazioneXlsError as exc:
            copia.unlink(missing_ok=True)
            raise ValueError(str(exc)) from exc
        print(f"[{supplier.upper()}] {json.dumps(esito, ensure_ascii=False)}")
        if not copia.is_file():
            raise ValueError(f"La copia del listino {nome} non è stata creata.")
        return supplier, sorgente, copia

    def percorso_listino(self, config: dict[str, Any], supplier: str) -> Path:
        files_value = config.get("supplier_files")
        raw = (files_value or {}).get(supplier) if isinstance(files_value, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(
                f"{supplier_label(supplier)} è selezionato, ma manca il suo listino configurato "
                "per la compilazione."
            )
        source = Path(raw.strip()).expanduser()
        if not source.is_absolute() and self.writer_config is not None:
            source = self.writer_config.parent / source
        source = source.resolve()
        if not source.is_file():
            raise ValueError(f"Il listino configurato per {supplier_label(supplier)} non è disponibile.")
        return source

    @staticmethod
    def colonna_ean_dichiarata(sorgente: Path, regola: dict[str, Any]) -> int | str | None:
        """Quale colonna porta l'EAN, risolta **dalle intestazioni del file**.

        Nel registro la colonna e' dichiarata per nome (`codice_a_barre`), non
        per lettera, ed e' giusto cosi': `cat` e `Iva` sono nomi di colonna veri
        di questo listino e insieme riferimenti Excel validi, quindi il ripiego
        sulle lettere leggerebbe in silenzio la colonna sbagliata.  La regola
        che traduce nome -> numero e' quella del lettore, in un posto solo.
        """

        nome = regola.get("ean_column_name") or regola.get("ean_column")
        if nome in (None, ""):
            return None
        riga_intestazione = number(regola.get("header_row"))
        if riga_intestazione is None or riga_intestazione < 1:
            return nome if isinstance(nome, str) else None
        from prepare_manifest_sources import column_number
        from xls_reader import read_workbook

        fogli = read_workbook(sorgente)
        atteso = str(regola.get("sheet") or "")
        griglia = next((f.rows for f in fogli if f.name == atteso), fogli[0].rows if fogli else [])
        indice = int(riga_intestazione) - 1
        if not 0 <= indice < len(griglia):
            raise ValueError(
                f"Il listino NOCE non arriva alla riga {int(riga_intestazione)}, dove "
                "dovrebbero esserci le intestazioni."
            )
        intestazione = [valore for valore, _grassetto in griglia[indice]]
        return column_number(nome, intestazione)


def frase_di_un_errore(errore: dict[str, Any]) -> str:
    """La riga in italiano di un controllo non superato, col nome del prodotto.

    Il codice da solo non basta a nessuno: la pagina mostra `message`, e un
    «Controlli snapshot non superati» davanti a cinquecento righe non dice
    quale riga guardare.
    """

    codice = str(errore.get("code") or "")
    prodotto = str(errore.get("productName") or errore.get("productId") or "").strip()
    fornitore = str(errore.get("supplierName") or errore.get("supplierId") or "").strip()
    if codice == "FORNITORE_DA_SCEGLIERE":
        return (
            f"«{prodotto}»: non è stato scelto nessun fornitore, e qualcuno ce l'ha. "
            "Scegline uno, oppure metti la quantità a zero se non ti serve."
        )
    if codice == "OFFERTA_NON_VALIDA":
        if not fornitore:
            return (
                f"«{prodotto}»: non è stato scelto nessun fornitore. Scegline uno "
                "oppure metti la quantità a zero."
            )
        return (
            f"«{prodotto}»: l'offerta di {fornitore} non è utilizzabile (il listino non "
            "la riporta più disponibile). Scegli un altro fornitore per questo prodotto "
            "oppure metti la quantità a zero."
        )
    if codice == "CONFERMA_MANCANTE":
        coda = f" di {fornitore}" if fornitore else ""
        return f"«{prodotto}»: va confermata la corrispondenza con l'offerta{coda} prima di procedere."
    if codice == "SOGLIA_NON_CONFERMATA":
        return (
            f"{supplier_label(str(errore.get('supplier') or ''))}: l'ordine resta sotto la "
            f"soglia netta di {errore.get('threshold_net')} €. Serve la conferma esplicita."
        )
    if codice == "PREZZO_NON_VALIDO":
        coda = f" di {fornitore}" if fornitore else ""
        return f"«{prodotto}»: il prezzo dell'offerta{coda} non è utilizzabile per calcolare l'ordine."
    if codice == "RIGA_LISTINO_CONTESA":
        altri = str(errore.get("otherProducts") or "").strip()
        coda = f" e {altri}" if altri else ""
        return (
            f"«{prodotto}»{coda}: sono due ordini diversi sulla stessa riga "
            f"{errore.get('sourceRow')} del listino {fornitore}, e in quella cella ci va un "
            "numero solo. Succede fra un espositore e il collo da cui è ricavato. Tieni "
            "l'ordine che ti serve e metti l'altro a zero."
        )
    if codice == "PRODOTTO_SCONOSCIUTO":
        return f"«{prodotto}» non fa parte del confronto caricato: ricarica la pagina."
    if codice == "QUANTITA_NON_VALIDA":
        return f"«{prodotto}»: la quantità non è un numero intero di colli."
    if codice == "ID_PRODOTTO_NON_VALIDO":
        return "Una riga arriva senza identificativo o ripetuta: ricarica la pagina."
    if codice == "STATO_SOVRASCRITTO":
        if str(errore.get("origin") or "") == "svuotato":
            return (
                "Le tue scelte non sono state salvate, ed è giusto così: è cominciata una "
                "comparazione nuova, e parlavano del confronto di prima. Non c'è niente da "
                "recuperare — carica i documenti di questa settimana e ricomincia da lì."
            )
        if str(errore.get("origin") or "") == "compilazione":
            return (
                "Le tue scelte non sono state salvate: le ha già riscritte l'ultima "
                "compilazione, che salva lo stato prima di preparare i listini. Ricarica "
                "questa pagina — le scelte con cui hai compilato sono quelle salvate — e "
                "riprova da lì."
            )
        return (
            "Le tue scelte non sono state salvate: un'altra scheda del comparatore ha "
            "salvato dopo di te, e salvare adesso cancellerebbe quello che ha scritto. "
            "Ricarica questa pagina per vedere le scelte aggiornate, poi rifai le tue modifiche."
        )
    if codice == "ORDINE_VUOTO":
        return "Nessun prodotto ha una quantità maggiore di zero: non c'è niente da compilare."
    return f"Controllo non superato: {codice or 'senza codice'}."


def frase_degli_errori(errori: list[dict[str, Any]], *, massimo: int = 3) -> str:
    """Le prime frasi, e quante altre ce ne sono: un elenco infinito non si legge."""

    frasi: list[str] = []
    for errore in errori or []:
        if not isinstance(errore, dict):
            continue
        frase_singola = frase_di_un_errore(errore)
        if frase_singola not in frasi:
            frasi.append(frase_singola)
    if not frasi:
        return "Controlli non superati."
    mostrate = frasi[:massimo]
    restanti = len(frasi) - len(mostrate)
    testo = " ".join(mostrate)
    if restanti > 0:
        testo = f"{testo} E altri {restanti} controlli non superati."
    return testo


class SnapshotError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(frase_degli_errori(errors))
        self.errors = errors


# ----------------------------------------------------------------------------
# La pagina Impostazioni
# ----------------------------------------------------------------------------

# Le sole voci che si cambiano da questa pagina.  Restano fuori di proposito
# `versione_prompt` e `versione_avversario` — sono componenti del programma,
# scelti misurando quanti `ALTA` sbagliati producono, non preferenze — e con
# loro `max_tokens`, `temperature` e `base_url`, che fanno parte della stessa
# misura o dell'indirizzo del servizio.  Una manopola in piu' qui vorrebbe dire
# che l'utente puo' rendere falsa quella misura senza che niente glielo dica.
VOCI_IMPOSTAZIONI = ("model", "tetto_spesa_usd", "tetto_chiamate", "parallelismo", "timeout_secondi")

# `GET /api/v1/models` di OpenRouter risponde **anche senza chiave e anche con
# una chiave scaduta**: un menu' che si popola non prova niente sulla chiave.
# La frase viaggia con l'elenco perche' e' la pagina a doverla dire, e perche'
# chi un domani riusa questa rotta la trova insieme ai dati.
AVVISO_ELENCO_PUBBLICO = (
    "L’elenco dei modelli è pubblico: si carica anche senza chiave e anche con una chiave "
    "scaduta. Che la chiave funzioni lo dice soltanto «Prova la connessione»."
)

# Che cosa dire all'utente per ogni stato che `prova_connessione` puo' tornare.
# Il tono e' solo il colore dell'avviso: `ok` in risposta e' vero **soltanto**
# per `OK`, perche' e' l'unico caso in cui chiave, identificativo del modello e
# formato della risposta hanno funzionato tutti e tre insieme.  Dire «chiave
# valida» su un 429 sarebbe una deduzione, e questa pagina non ne fa.
MESSAGGI_DELLA_PROVA: dict[str, tuple[str, str]] = {
    ai_client.STATO_OK: ("success", "La chiave funziona: il modello ha risposto nel formato previsto."),
    ai_client.STATO_SENZA_CHIAVE: ("danger", "Non c’è nessuna chiave da provare: incollala nel campo qui sopra."),
    "HTTP_401": ("danger", "Chiave non accettata (401): è sbagliata, scaduta o revocata."),
    "HTTP_403": ("danger", "Chiave rifiutata (403): esiste, ma non è abilitata a questo modello."),
    "HTTP_402": ("warning", "Il credito su OpenRouter è esaurito (402): la chiave non è stata respinta, ma finché non lo ricarichi ogni chiamata fallisce."),
    "HTTP_400": ("warning", "Richiesta rifiutata (400): quasi sempre l’identificativo del modello non esiste. La chiave non è stata respinta."),
    "HTTP_404": ("warning", "Questo modello non esiste (404): controlla l’identificativo. La chiave non è stata respinta."),
    "HTTP_429": ("warning", "Il servizio ha chiesto di rallentare (429): riprova fra un minuto. La chiave non è stata respinta."),
    "HTTP_5xx": ("warning", "OpenRouter ha risposto con un guasto suo (5xx): riprova fra un minuto. La chiave non è stata respinta."),
    ai_client.STATO_ERRORE_RETE: ("danger", "OpenRouter non è stato raggiunto: la prova non dice niente sulla chiave. Controlla la connessione."),
    ai_client.STATO_ERRORE_TRASPORTO: ("danger", "La chiamata si è rotta prima di partire: la prova non dice niente sulla chiave."),
    ai_client.STATO_TETTO_SPESA: ("danger", "Il tetto di spesa impostato non lascia partire nemmeno la chiamata di prova: alzalo e riprova."),
    ai_client.STATO_TETTO_CHIAMATE: ("danger", "Il tetto di chiamate impostato non lascia partire nemmeno la chiamata di prova: alzalo e riprova."),
}

# Qui la chiamata e' partita, il modello ha risposto e la risposta e' arrivata
# storta.  Non e' un guasto della chiave, ed e' comunque una ragione per non
# usare quel modello: con una risposta cosi' la fase AI non deciderebbe niente.
STATI_RISPOSTA_INUTILIZZABILE = frozenset({
    ai_client.STATO_TRONCATA,
    ai_client.STATO_CONTENUTO_VUOTO,
    ai_client.STATO_NON_E_JSON,
    ai_client.STATO_SCHEMA_NON_CONFORME,
    ai_client.STATO_RIGA_FUORI_SHORTLIST,
    ai_client.STATO_JSON_RICHIESTO,
})


def chiave_dal_corpo(payload: Any) -> str:
    """La chiave incollata nel corpo della richiesta, o stringa vuota.

    Sta in una funzione sola perche' il valore che torna di qui e' anche quello
    che il gestore mette da parte per ripulire i messaggi d'errore: se i due
    punti leggessero il corpo in due modi diversi, la chiave che sfugge alla
    lettura sfugge anche alla ripulitura.
    """

    if not isinstance(payload, dict):
        return ""
    return str(payload.get("chiave") or "").strip()


class ServizioImpostazioni:
    """Chiave, modello e tetti: tutto quello che la pagina Impostazioni tocca.

    Sta fuori da `ReviewStore` di proposito.  `GET /api/review` e' la risposta
    piu' grande e piu' letta del programma, e la configurazione AI non deve
    poterci finire dentro: tenerla in un altro oggetto rende la cosa vera per
    costruzione, invece che per attenzione di chi scrivera' il prossimo campo.

    `leggi_modelli` e `crea_client` sono i due punti in cui si tocca la rete, e
    sono iniettabili per la stessa ragione per cui lo e' `ClientAI(trasporto=…)`:
    nessun test di questa suite deve chiamare OpenRouter.
    """

    def __init__(
        self,
        *,
        percorso_secrets: Path | None = None,
        percorso_impostazioni: Path | None = None,
        leggi_modelli: Any = None,
        crea_client: Any = None,
    ) -> None:
        self.percorso_secrets = Path(percorso_secrets) if percorso_secrets else ai_client.PERCORSO_SECRETS
        self.percorso_impostazioni = (
            Path(percorso_impostazioni) if percorso_impostazioni else ai_client.PERCORSO_IMPOSTAZIONI
        )
        self._leggi_modelli = leggi_modelli or ai_client.elenco_modelli
        self._crea_client = crea_client or (
            lambda configurazione, chiave: ai_client.ClientAI(configurazione, chiave=chiave)
        )

    # -- lettura -------------------------------------------------------------

    def configurazione(self) -> dict[str, Any]:
        return ai_client.carica_configurazione(self.percorso_impostazioni)

    def chiave_salvata(self) -> str:
        """La chiave che il servizio userebbe se il corpo non ne porta una.

        Non esce mai verso il browser: serve solo a `_chiave_in_volo`, cioe' a
        sapere che cosa nascondere se qualcosa va storto mentre la si usa.
        """

        return ai_client.leggi_chiave(self.percorso_secrets) or ""

    def stato(self) -> dict[str, Any]:
        """Com'e' configurato il programma adesso. **Senza la chiave.**

        `stato_chiave` torna soltanto presenza, origine e coda di quattro
        caratteri: e' l'unica cosa che di una chiave puo' arrivare al browser.
        """

        configurazione = self.configurazione()
        return {
            "ok": True,
            "chiave": ai_client.stato_chiave(self.percorso_secrets),
            "impostazioni": {nome: configurazione[nome] for nome in VOCI_IMPOSTAZIONI},
            "predefinite": {nome: ai_client.CONFIGURAZIONE_PREDEFINITA[nome] for nome in VOCI_IMPOSTAZIONI},
            "percorsoChiave": str(self.percorso_secrets),
        }

    def modelli(self) -> dict[str, Any]:
        """L'elenco per il menu'.  Un guasto qui non e' un guasto della pagina.

        Il menu' e' una comodita': l'identificativo si puo' sempre scrivere a
        mano, ed e' l'unico modo di usare un modello uscito ieri.  Quindi una
        rete che non risponde torna `ok: false` con il motivo scritto, non un
        500 che spegne la pagina.
        """

        configurazione = self.configurazione()
        try:
            elenco = self._leggi_modelli(configurazione["base_url"])
        except ai_client.GUASTI_DI_RETE as errore:
            return {
                "ok": False,
                "modelli": [],
                "avviso": AVVISO_ELENCO_PUBBLICO,
                "messaggio": (
                    f"Elenco dei modelli non disponibile ({type(errore).__name__}): "
                    "scrivi l’identificativo del modello a mano."
                ),
            }
        return {"ok": True, "modelli": elenco, "avviso": AVVISO_ELENCO_PUBBLICO}

    # -- scrittura -----------------------------------------------------------

    def salva_chiave(self, payload: Any) -> dict[str, Any]:
        """Scrive la chiave e risponde con il solo stato: il valore non torna."""

        ai_client.salva_chiave(chiave_dal_corpo(payload), self.percorso_secrets)
        return {
            "ok": True,
            "messaggio": "Chiave salvata sul computer.",
            "chiave": ai_client.stato_chiave(self.percorso_secrets),
        }

    def salva(self, payload: Any) -> dict[str, Any]:
        """Salva le voci esposte.  Un valore rifiutato e' un errore, non un ripiego.

        `salva_impostazioni` solleva `ValueError` con un messaggio gia' in
        italiano e gia' leggibile — «attenzione al separatore decimale…» — e
        quel messaggio arriva all'utente cosi' com'e': riscriverlo qui vorrebbe
        dire due testi da tenere allineati.
        """

        valori = payload.get("impostazioni") if isinstance(payload, dict) else None
        if not isinstance(valori, dict) or not valori:
            raise ValueError("Non è arrivata nessuna impostazione da salvare.")
        fuori = sorted(set(valori) - set(VOCI_IMPOSTAZIONI))
        if fuori:
            raise ValueError(
                "Queste voci non si cambiano dalla pagina Impostazioni: " + ", ".join(fuori)
                + ". Da qui si cambiano soltanto: " + ", ".join(VOCI_IMPOSTAZIONI) + "."
            )
        configurazione = ai_client.salva_impostazioni(valori, self.percorso_impostazioni)
        return {
            "ok": True,
            "messaggio": "Impostazioni salvate.",
            "impostazioni": {nome: configurazione[nome] for nome in VOCI_IMPOSTAZIONI},
        }

    # -- la prova ------------------------------------------------------------

    def prova(self, payload: Any) -> dict[str, Any]:
        """Una chiamata vera al modello, con la chiave che l'utente sta provando.

        La chiave arriva nel corpo e **non viene salvata**: cosi' si prova prima
        di salvare, e una chiave sbagliata non sostituisce quella che funziona.
        Se il corpo non la porta si prova quella gia' configurata.
        """

        chiave = chiave_dal_corpo(payload)
        if not chiave:
            # Si legge **dal percorso di questo servizio**, non lasciando cercare
            # il client: `ClientAI(chiave=None)` guarderebbe il percorso
            # predefinito del modulo, che in produzione e' lo stesso ma altrove
            # no. La prova finirebbe per dire com'e' una chiave diversa da
            # quella che la pagina mostra due riquadri piu' su.
            chiave = ai_client.leggi_chiave(self.percorso_secrets) or ""

        configurazione = self.configurazione()
        modello = str((payload or {}).get("model") or "").strip() if isinstance(payload, dict) else ""
        if modello:
            # Si prova l'identificativo che l'utente ha appena scritto, prima di
            # salvarlo: un modello che non esiste risponde 400 e si scopre qui.
            configurazione = {**configurazione, "model": modello}

        # `chiave=""` dichiara al client che la chiave non c'e', ed e' quello che
        # fa tornare `SENZA_CHIAVE` invece di una ricerca a sorpresa.
        client = self._crea_client(configurazione, chiave)
        esito = client.prova_connessione()

        if esito.stato in MESSAGGI_DELLA_PROVA:
            tono, messaggio = MESSAGGI_DELLA_PROVA[esito.stato]
        elif esito.stato in STATI_RISPOSTA_INUTILIZZABILE:
            tono = "warning"
            messaggio = (
                "Il modello ha risposto, ma la risposta non è utilizzabile "
                f"({esito.stato}). La chiave non è stata respinta: è questo modello a non "
                "rispettare il formato richiesto."
            )
        else:
            tono = "danger"
            messaggio = f"Prova non riuscita ({esito.stato}). Il dettaglio qui sotto dice che cosa è successo."

        # `ClientAI` toglie gia' la chiave da ogni campo dell'esito.  Si rifa'
        # qui perche' questa risposta esce verso il browser e la difesa che
        # conta e' quella che sta nel punto d'uscita, non quella a monte.
        return {
            "ok": esito.stato == ai_client.STATO_OK,
            "tono": tono,
            "stato": senza_la_chiave(esito.stato, chiave),
            "messaggio": messaggio,
            "dettaglio": senza_la_chiave(esito.dettaglio, chiave),
            "modello": senza_la_chiave(esito.modello, chiave),
            "costoUsd": esito.costo_usd,
            "durataS": esito.durata_s,
        }


class AppHandler(BaseHTTPRequestHandler):
    server_version = "ComparaOrdini/0.1"

    # La chiave che sta passando in **questa** richiesta, e solo per la sua
    # durata.  Serve a `senza_segreti`: il corpo di un 500 rimanda `str(exc)` al
    # browser — e dal 20 agosto 2026 il traceback finisce anche su `stderr` —
    # e un'eccezione sollevata mentre si prova una chiave se la porterebbe
    # dietro, dalla libreria HTTP, da urllib, da un `KeyError` su un dizionario
    # di intestazioni.  E' la chiave appena incollata **oppure quella salvata**,
    # perche' e' quest'ultima che viene usata quando il corpo non ne porta
    # nessuna, cioe' quasi sempre.  Sta sull'istanza e non e' un dato di modulo
    # perche' ogni connessione ha la sua istanza e il suo thread.
    _chiave_in_volo: str = ""

    @property
    def store(self) -> ReviewStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def impostazioni(self) -> ServizioImpostazioni:
        servizio = getattr(self.server, "impostazioni", None)
        if servizio is None:
            servizio = ServizioImpostazioni()
            self.server.impostazioni = servizio  # type: ignore[attr-defined]
        return servizio

    def senza_segreti(self, testo: Any) -> str:
        """Il testo che sta per uscire verso il browser, senza la chiave dentro."""

        return senza_la_chiave(str(testo), self._chiave_in_volo)

    def log_message(self, format_string: str, *args: Any) -> None:
        # La riga di richiesta finisce qui a ogni chiamata: e' la ragione per cui
        # la chiave viaggia soltanto nel corpo di una POST e mai in una query.
        print(f"{self.address_string()} - {self.senza_segreti(format_string % args)}")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def json_response(self, value: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stampa_il_guasto(self) -> None:
        """Il traceback di un guasto imprevisto, sulla finestra del programma.

        ⚠ Fino al 20 agosto 2026 non finiva da nessuna parte. La frase tecnica
        arrivava in pagina dentro `DETTAGLIO_TECNICO` — «KeyError: 'offers'» —
        e li' si fermava: `traceback` e `logging` non erano importati in nessun
        file di `app/`, e un file di registro non esiste. In negozio, davanti a
        quel messaggio, non c'era modo di sapere da quale delle 5.200 righe
        venisse: restava farsi raccontare i passi al telefono.

        Non cambia nessuna risposta HTTP e l'utente non lo vede: esce da
        `stderr`, che e' la finestra di PowerShell del lanciatore, e serve a chi
        sta guardando mentre succede.

        Passa da `senza_segreti` come tutto il resto: la chiave puo' comparire
        in un traceback di `urllib`, ed e' esattamente la ragione per cui
        `_chiave_in_volo` esiste.
        """

        tipo, valore, _traccia = sys.exc_info()
        if tipo is None:
            # Un 500 dichiarato a mano, senza nessuna eccezione in volo:
            # `format_exc()` scriverebbe la riga inutile «NoneType: None».
            return
        if isinstance(valore, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            # ⚠ Non e' un guasto del programma: e' il browser che ha staccato a
            # meta' — l'utente che annulla uno scaricamento, la scheda che si
            # chiude su un file grosso. Succede per davvero, e un traceback per
            # ognuno riempirebbe di rumore proprio la finestra in cui il giorno
            # del guasto vero bisogna saper guardare. Trovato dalla verifica
            # avversariale del 20 agosto 2026, riproducendolo su un file da
            # 60 MB interrotto a meta'.
            return
        dove = self.senza_segreti(f"{self.command} {self.path}")
        print(
            f"[GUASTO] {dove}\n{self.senza_segreti(traceback.format_exc()).rstrip()}",
            file=sys.stderr,
            # Senza, il testo resta nel tampone: se il programma muore subito
            # dopo, il traceback muore con lui — cioe' proprio nel caso in cui
            # serviva.
            flush=True,
        )

    def error_response(self, status: int, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        # Ogni messaggio d'errore passa da qui, compreso il `str(exc)` dei 500:
        # e' il solo punto in cui basta ripulire una volta per coprirli tutti.
        pulito = self.senza_segreti(message)
        voci = list(errors or [])
        if status == HTTPStatus.INTERNAL_SERVER_ERROR:
            self._stampa_il_guasto()
            # ⚠ Un guasto imprevisto arrivava in pagina cosi' com'era: «[Errno
            # 13] Permission denied: 'C:\\Users\\HP\\...\\review_data.json'».
            # Inglese, percorsi del computer, e nessuna indicazione di cosa
            # fare.  La frase tecnica non si butta — serve a chi deve capire —
            # ma va di lato, non al posto della risposta.
            voci = [{"code": "DETTAGLIO_TECNICO", "message": pulito}, *voci]
            pulito = (
                "Ho avuto un problema con questa operazione. Riprova; se succede "
                "ancora, chiudi e riapri il comparatore."
            )
        self.json_response({"ok": False, "message": pulito, "errors": voci}, status)

    def read_json(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        if not raw_length or not raw_length.isdigit():
            raise ValueError("Content-Length mancante o non valido")
        length = int(raw_length)
        if length <= 0 or length > MAX_JSON_BYTES:
            raise ValueError("Dimensione richiesta non valida")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _origini_della_pagina(self) -> set[str]:
        """Gli unici indirizzi da cui la pagina del comparatore puo' arrivare.

        La porta non e' cablata e non va passata a mano: il lanciatore ripiega
        sulla 8766, 8767 e via cosi' quando la 8765 e' occupata, e una guardia
        con la porta scritta dentro rifiuterebbe proprio la pagina vera.
        `self.server.server_address[1]` e' quella su cui questo servizio sta
        rispondendo, sempre.
        """

        porta = self.server.server_address[1]
        return {f"http://{macchina}:{porta}" for macchina in ("127.0.0.1", "localhost", "[::1]")}

    def richiesta_dalla_nostra_pagina(self) -> bool:
        """Vero se questa richiesta con effetti arriva dalla pagina del comparatore.

        ⚠ Il metodo POST non e' una difesa, e per un po' qui si e' creduto che
        lo fosse.  Una POST con `Content-Type: text/plain` e' una «simple
        request» per il browser: parte senza preflight, e l'effetto avviene
        anche se chi l'ha mandata non legge la risposta.  Finche' il
        comparatore e' acceso, qualunque pagina aperta in quel browser poteva
        cosi' caricare un listino, avviare la catena — che spende credito
        OpenRouter — compilare gli ordini, cancellare i caricamenti,
        sovrascrivere la chiave e spegnere il programma.

        Si guarda quello che c'e', non si pretende che ci sia: il lanciatore
        chiama `/api/spegni` con urllib, che `Origin` non lo manda affatto, e
        un browser vecchio puo' non mandare `Sec-Fetch-Site`.  Le `fetch` della
        nostra pagina, servita da questo stesso servizio, mandano sempre tutti
        e due e sono sempre same-origin.
        """

        sito = str(self.headers.get("Sec-Fetch-Site") or "").strip()
        if sito and sito != "same-origin":
            return False
        origine = str(self.headers.get("Origin") or "").strip()
        if origine and origine not in self._origini_della_pagina():
            return False
        return True

    def rifiuta_l_origine(self) -> None:
        self.error_response(
            HTTPStatus.FORBIDDEN,
            "Questa richiesta non arriva dalla pagina del comparatore: è stata rifiutata.",
        )

    def _host_della_pagina(self) -> set[str]:
        """Gli unici valori di `Host` che possono venire dalla pagina del comparatore.

        Con la porta di questo servizio e senza: alcuni client mandano
        `Host: 127.0.0.1` senza porta, e la regola deve accettarli comunque.
        La porta si legge da `self.server.server_address[1]`, mai cablata.
        """

        porta = self.server.server_address[1]
        macchine = ("127.0.0.1", "localhost", "[::1]")
        return set(macchine) | {f"{macchina}:{porta}" for macchina in macchine}

    def richiesta_con_host_valido(self) -> bool:
        """Vero se l'intestazione `Host` punta a questo servizio, o manca del tutto.

        ⚠ Un sito che l'utente ha aperto puo' far scadere il proprio DNS e
        ripuntare il proprio dominio su 127.0.0.1 (DNS rebinding): da quel
        momento il browser manda le richieste a
        `http://dominio-cattivo.example:<porta>/api/...`, che per lui restano
        same-origin — quindi la pagina cattiva LEGGE la risposta, e in una
        GET l'`Origin` spesso non c'e' nemmeno, quindi la guardia anti-CSRF da
        sola non basta. L'intestazione `Host` pero' arriva sempre com'era nella
        barra dell'indirizzo del sito cattivo, mai come l'IP a cui il DNS ha
        ripuntato: controllarla chiude il buco.

        HTTP/1.0 puo' non mandare `Host` affatto, e alcuni client la mandano
        senza porta: i due casi passano, altrimenti la pagina vera smetterebbe
        di aprirsi.
        """

        host = str(self.headers.get("Host") or "").strip()
        if not host:
            return True
        return host in self._host_della_pagina()

    def rifiuta_l_host(self) -> None:
        self.error_response(
            HTTPStatus.FORBIDDEN,
            "Questo indirizzo non è quello del comparatore: la richiesta è stata rifiutata.",
        )

    def serve_file(self, path: Path, download: bool = False) -> None:
        if not path.is_file():
            self.error_response(HTTPStatus.NOT_FOUND, "Documento non trovato")
            return
        content = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        if download:
            # Misurato: `filename="Ordine LARICE — 12 agosto 2026.xlsx"` non si
            # codifica in latin-1, e le intestazioni di
            # BaseHTTPRequestHandler sono latin-1: la risposta morirebbe con
            # UnicodeEncodeError a corpo gia' promesso.  La forma RFC 5987 la
            # costruisce `consegna`, in un posto solo per tutte le rotte.
            self.send_header("Content-Disposition", consegna.intestazione_allegato(path.name))
        self.end_headers()
        self.wfile.write(content)

    def scarica_le_conferme(self) -> None:
        """`GET /api/conferme/esporta`: le conferme date, come file da salvare.

        Non passa da `json_response` perche' questa risposta non e' per la
        pagina: e' un file che l'utente salva. L'intestazione la costruisce
        `consegna`, come per tutti gli altri scaricamenti — il nome porta la
        data in italiano e in latin-1 non si codifica.
        """

        conferme, uguaglianze = self.store.esporta_le_conferme()
        momento = datetime.now().astimezone()
        nome = f"Conferme — {consegna.data_leggibile(momento)}.json"
        corpo = json.dumps(
            {
                "esportate_il": momento.isoformat(),
                "quante": len(conferme),
                "conferme": conferme,
                # ⚠ Anche le uguaglianze, e non e' un extra: stanno nello stesso
                # `conferme.db`, sono memoria «per sempre» come le conferme, e
                # questo file e' presentato in pagina come la copia che ci si
                # puo' portare via. Senza, sarebbe meta' del magazzino.
                "quante_uguaglianze": len(uguaglianze),
                "uguaglianze": uguaglianze,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Content-Disposition", consegna.intestazione_allegato(nome))
        self.end_headers()
        self.wfile.write(corpo)

    def servi_consegna(self, resto: str) -> None:
        """`/ordini/<cartella>/<nome>` e `/ordini/<cartella>/zip`.

        Il percorso arriva gia' decodificato (`unquote` in `do_GET`), quindi
        `..%2f..%2fsecrets.json` a questo punto e' `../../secrets.json`: qui si
        contano i segmenti **dopo** la decodifica, e ogni segmento passa dalle
        difese di `consegna`.  I messaggi non ripetono mai il percorso chiesto:
        direbbero a chi prova che cosa ha provato.
        """

        pezzi = resto.split("/")
        if len(pezzi) != 2 or not all(pezzi):
            self.error_response(HTTPStatus.NOT_FOUND, "Percorso non trovato")
            return
        nome_cartella, nome = pezzi
        cartella = consegna.cartella_sicura(self.store.orders_dir, nome_cartella)
        if cartella is None:
            self.error_response(HTTPStatus.NOT_FOUND, "Compilazione non trovata")
            return
        if nome == "zip":
            self.servi_zip(cartella)
            return
        percorso = consegna.file_sicuro(cartella, nome)
        if percorso is None:
            self.error_response(HTTPStatus.NOT_FOUND, "Documento non trovato")
            return
        self.serve_file(percorso, download=True)

    def servi_zip(self, cartella: Path) -> None:
        """Lo zip di quello che si consegna, costruito al momento e mai su disco.

        Sono i listini compilati e, quando c'è, l'elenco dei prodotti che nessun
        fornitore porta: chi scarica lo zip sta preparando la settimana, e
        quell'elenco è parte del lavoro di quella settimana quanto un ordine.
        """

        voce = consegna.voce(cartella)
        # I tipi da consegnare stanno in `consegna`, in un posto solo: due
        # elenchi da tenere allineati sono un elenco che si dimentica.
        nomi = [item["nome"] for item in voce["file"]
                if item["tipo"] in consegna.TIPI_DA_CONSEGNARE]
        if not nomi:
            self.error_response(HTTPStatus.NOT_FOUND, "In questa compilazione non ci sono listini da scaricare")
            return
        try:
            contenuto = consegna.zip_in_memoria(cartella, nomi)
        except ValueError:
            # L'audit nomina un listino che sul disco non c'e' piu': la cartella
            # e' stata toccata a mano.  E' un 404, non un guasto del programma.
            self.error_response(HTTPStatus.NOT_FOUND, "In questa compilazione non ci sono listini da scaricare")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(contenuto)))
        self.send_header("Content-Disposition", consegna.intestazione_allegato(voce["zipNome"]))
        self.end_headers()
        self.wfile.write(contenuto)

    def do_GET(self) -> None:  # noqa: N802
        if not self.richiesta_con_host_valido():
            self.rifiuta_l_host()
            return
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        try:
            if route == "/api/review":
                self.json_response(self.store.review())
                return
            if route == "/api/products/search":
                parameters = parse_qs(parsed.query)
                query = str((parameters.get("q") or [""])[0]).strip()
                limit = int(number((parameters.get("limit") or [20])[0]) or 20)
                self.json_response(self.store.search_products(query, limit))
                return
            if route == "/api/listino":
                parameters = parse_qs(parsed.query)
                primo = lambda nome: str((parameters.get(nome) or [""])[0]).strip()  # noqa: E731
                self.json_response(self.store.sfoglia_listino(
                    primo("fornitore"),
                    query=primo("q"),
                    da=int(number(primo("da")) or 0),
                    quante=int(number(primo("quante")) or 50),
                    riga=primo("riga") or None,
                ))
                return
            if route == "/api/matches/uguaglianze":
                parameters = parse_qs(parsed.query)
                self.json_response(self.store.elenco_delle_uguaglianze(
                    str((parameters.get("q") or [""])[0])
                ))
                return
            if route == "/api/conferme/esporta":
                self.scarica_le_conferme()
                return
            if route == "/api/schemas/order-column":
                parameters = parse_qs(parsed.query)
                self.json_response(self.store.colonne_d_ordine(
                    str((parameters.get("fornitore") or [""])[0]).strip()
                ))
                return
            if route == "/api/history/pending":
                self.json_response(self.store.history_pending())
                return
            if route == "/api/pipeline/stato":
                self.json_response(self.store.stato_pipeline())
                return
            if route == "/api/schemas/pending":
                self.json_response(self.store.schemi_pendenti())
                return
            if route == "/api/schemas/documento":
                # Il nome del documento non e' un percorso: e' una voce
                # dell'elenco che il servizio ha appena dato alla pagina, e
                # `colonne_del_documento` lo ricontrolla sulla cartella.
                parameters = parse_qs(parsed.query)
                nome = str((parameters.get("nome") or [""])[0]).strip()
                self.json_response(self.store.colonne_del_documento(nome))
                return
            if route == "/api/schemas/columns":
                self.json_response(self.store.colonne_dei_documenti())
                return
            if route == "/api/impostazioni":
                self.json_response(self.impostazioni.stato())
                return
            if route == "/api/impostazioni/modelli":
                # L'elenco e' pubblico e non porta nessun segreto: e' l'unica
                # rotta delle impostazioni che puo' essere una GET.
                self.json_response(self.impostazioni.modelli())
                return
            if route == "/api/ordini":
                self.json_response({
                    "ok": True,
                    "compilazioni": consegna.elenco(self.store.orders_dir, etichetta_fornitore=supplier_label),
                })
                return
            if route == "/api/health":
                # `firmaDelCodice` dice **quale** programma sta rispondendo, non
                # solo che qualcuno risponde.  Senza, il lanciatore trovava un
                # server sano e lo riusava anche quando i sorgenti erano cambiati
                # sotto: si riapriva il `.cmd` credendo di riavviare e si tornava
                # sul programma di due giorni prima.
                self.json_response({
                    "ok": True,
                    "status": "ready",
                    "firmaDelCodice": versione_del_codice.firma(),
                    # La data della versione pubblicata che sta girando: la
                    # pagina la mostra in alto, perche' l'allineamento a GitHub
                    # puo' fallire in silenzio e nessuno se ne accorgerebbe.
                    "versionePubblicata": versione_del_codice.pubblicata(),
                })
                return
            if route.startswith(consegna.PREFISSO_URL + "/"):
                self.servi_consegna(route.removeprefix(consegna.PREFISSO_URL + "/"))
                return
            if route.startswith("/outputs/"):
                # Stessa difesa della rotta gemella `/ordini/<cartella>/<nome>`:
                # `.name` da solo blocca il traversal, ma non un collegamento
                # simbolico dentro `outputs/` — quello lo ferma solo il
                # controllo di contenimento dopo `resolve()` che `file_sicuro`
                # fa gia' per l'altra rotta.
                percorso = consegna.file_sicuro(
                    self.store.output_dir, Path(route.removeprefix("/outputs/")).name
                )
                if percorso is None:
                    self.error_response(HTTPStatus.NOT_FOUND, "Documento non trovato")
                    return
                self.serve_file(percorso, download=True)
                return
            if route in {"/", "/index.html"}:
                self.serve_file(STATIC_DIR / "index.html")
                return
            if route.startswith("/static/"):
                relative = Path(route.removeprefix("/static/"))
                if relative.name != str(relative) or relative.name not in {"index.html", "styles.css", "app.js"}:
                    self.error_response(HTTPStatus.NOT_FOUND, "Risorsa non trovata")
                    return
                self.serve_file(STATIC_DIR / relative.name)
                return
            self.error_response(HTTPStatus.NOT_FOUND, "Percorso non trovato")
        except ValueError as exc:
            self.error_response(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self.error_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_PUT(self) -> None:  # noqa: N802
        # Per simmetria con `do_POST`.  Qui non e' dove si gioca la partita —
        # `PUT` non e' un metodo «semplice», quindi il browser fa comunque il
        # preflight e `/api/state` e' gia' fuori dalla portata di un altro sito
        # — ma una guardia che vale per un metodo con effetti e non per l'altro
        # e' una guardia che qualcuno prima o poi legge al contrario.
        if not self.richiesta_con_host_valido():
            self.rifiuta_l_host()
            return
        if not self.richiesta_dalla_nostra_pagina():
            self.rifiuta_l_origine()
            return
        if urlparse(self.path).path != "/api/state":
            self.error_response(HTTPStatus.NOT_FOUND, "Percorso non trovato")
            return
        try:
            self.json_response(self.store.save_state(self.read_json()))
        except SnapshotError as exc:
            self.error_response(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc), exc.errors)
        except (ValueError, json.JSONDecodeError) as exc:
            self.error_response(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self.error_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _spegni(self) -> dict[str, Any]:
        """Ferma il servizio, ma **non** mentre la catena sta lavorando.

        Una run uccisa a meta' lascia una cartella datata orfana e il lavoro
        gia' pagato all'AI da rifare: chi chiede di spegnere deve sapere che
        c'e' qualcosa in corso e decidere lui.  Chi chiama e' il lanciatore,
        che in quel caso riusa il server invece di riavviarlo.

        Lo spegnimento vero parte **dopo** la risposta: `shutdown()` aspetta che
        il ciclo del servizio si fermi, e chiamarlo da dentro un handler
        bloccherebbe il servizio contro se stesso.
        """

        stato = self.store.stato_pipeline()
        if str(stato.get("stato") or "") in STATI_IN_CORSO:
            return {
                "ok": False,
                "spento": False,
                "motivo": "RUN_IN_CORSO",
                "messaggio": (
                    "C'è un confronto in corso: il programma non si spegne adesso. "
                    "Aspetta che finisca e riprova."
                ),
            }

        # Il file delle conferme si restituisce **prima** di fermarsi: SQLite lo
        # tiene aperto finche' la connessione vive, e su Windows un file aperto
        # non si rinomina e non si cancella. Chi spegne perche' sta per
        # riavviare col codice nuovo (`/api/health` → firma diversa) deve
        # trovare la cartella libera.
        self.store.chiudi()
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        return {"ok": True, "spento": True}

    def do_POST(self) -> None:  # noqa: N802
        # Prima di leggere il corpo: nessuna delle diciannove rotte qui sotto
        # deve poter partire da una pagina che non e' la nostra.
        if not self.richiesta_con_host_valido():
            self.rifiuta_l_host()
            return
        if not self.richiesta_dalla_nostra_pagina():
            self.rifiuta_l_origine()
            return
        route = urlparse(self.path).path
        try:
            payload = self.read_json()
            if route.startswith("/api/impostazioni"):
                # Da qui in poi la chiave puo' essere nel corpo: si mette da
                # parte **prima** di toccarla, cosi' qualunque eccezione fra
                # questa riga e la risposta esce gia' ripulita.
                #
                # ⚠ E se nel corpo non c'e', quella che sta per essere usata e'
                # la chiave **salvata**: e' il caso piu' comune di tutti — chi
                # preme «Prova la connessione» senza reincollare niente manda un
                # corpo senza `chiave`, e il servizio ripiega su quella del
                # file. Finche' qui si guardava solo il corpo, in quel caso
                # `_chiave_in_volo` restava vuota e la sola difesa era la forma
                # `sk-...`, che una chiave presa da `OPENROUTER_API_KEY` — mai
                # controllata da nessuno — non e' tenuta ad avere. Trovato dalla
                # verifica avversariale del 20 agosto 2026.
                self._chiave_in_volo = chiave_dal_corpo(payload) or self.impostazioni.chiave_salvata()
            if route == "/api/impostazioni":
                self.json_response(self.impostazioni.salva(payload))
            elif route == "/api/impostazioni/chiave":
                self.json_response(self.impostazioni.salva_chiave(payload))
            elif route == "/api/impostazioni/prova":
                self.json_response(self.impostazioni.prova(payload))
            elif route == "/api/upload":
                self.json_response(self.store.upload(payload), HTTPStatus.CREATED)
            elif route == "/api/products/add":
                self.json_response(self.store.add_manual_product(payload), HTTPStatus.CREATED)
            elif route == "/api/history/answer":
                self.json_response(self.store.answer_history_order(payload))
            elif route == "/api/matches/answer":
                self.json_response(self.store.answer_rejected_candidate(payload))
            elif route == "/api/matches/abbina":
                self.json_response(self.store.abbina_riga_di_listino(payload))
            elif route == "/api/matches/rifiuta":
                self.json_response(self.store.rifiuta_l_abbinamento(payload))
            elif route == "/api/matches/uguaglianze/togli":
                self.json_response(self.store.togli_uguaglianza(payload))
            elif route == "/api/ordini/elimina":
                self.json_response(self.store.delete_compilation(payload))
            elif route == "/api/uploads/elimina":
                self.json_response(self.store.delete_upload(payload))
            elif route == "/api/comparazione/nuova":
                self.json_response(self.store.nuova_comparazione(payload))
            elif route == "/api/suppliers/discount":
                self.json_response(self.store.set_supplier_discount(payload))
            elif route == "/api/suppliers/move-preview":
                # Sola lettura: e' un preventivo, non applica niente.
                self.json_response(self.store.move_preview(payload))
            elif route == "/api/pipeline/avvia":
                # Non aspetta la catena: torna subito con lo stato iniziale, e
                # la pagina lo interroga finche' non finisce.
                self.json_response(self.store.avvia_pipeline(), HTTPStatus.ACCEPTED)
            elif route == "/api/schemas/validate":
                self.json_response(self.store.valida_schemi(payload))
            elif route == "/api/schemas/order-column":
                self.json_response(self.store.cambia_colonna_d_ordine(payload))
            elif route == "/api/schemas/confirm":
                risposta = self.store.conferma_schemi(payload)
                self.json_response(risposta, HTTPStatus.ACCEPTED)
            elif route == "/api/schemas/documento/prova":
                self.json_response(self.store.prova_colonne(payload))
            elif route == "/api/schemas/documento/salva":
                self.json_response(self.store.salva_colonne(payload))
            elif route == "/api/compile":
                self.json_response(self.store.compile(payload))
            elif route == "/api/spegni":
                # Lo chiede il lanciatore quando i sorgenti sono cambiati: e'
                # lui che riapre subito dopo.  POST e non GET perche' un
                # `<img src=...>` non deve poter spegnere il programma — ma il
                # POST da solo non basta e non e' mai bastato: a fermare le
                # pagine estranee e' la guardia in cima a `do_POST`.
                self.json_response(self._spegni())
            else:
                self.error_response(HTTPStatus.NOT_FOUND, "Percorso non trovato")
        except SnapshotError as exc:
            self.error_response(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc), exc.errors)
        except LavoroGiaInCorso as exc:
            self.error_response(HTTPStatus.CONFLICT, str(exc))
        except (ValueError, json.JSONDecodeError) as exc:
            self.error_response(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self.error_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        finally:
            # Una connessione tenuta aperta serve piu' richieste con la stessa
            # istanza: la chiave della richiesta precedente non deve restare a
            # disposizione della successiva.
            self._chiave_in_volo = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, required=True, help="review_data.json generato dalla pipeline")
    parser.add_argument("--state", type=Path, required=True, help="File JSON locale per autosalvataggio")
    parser.add_argument("--uploads", type=Path, required=True, help="Cartella read-only della run per le copie caricate")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--orders-dir", type=Path, help="Radice delle compilazioni datate (predefinito: accanto a output-dir, cartella «ordini»)")
    parser.add_argument("--writer-config", type=Path, help="Configurazione opzionale per creare subito le copie XLSX")
    parser.add_argument("--history", type=Path, help="Archivio degli ordini in attesa di consegna (predefinito: data/history/orders.json)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Per sicurezza l'app locale può ascoltare soltanto sull'interfaccia loopback")
    store = ReviewStore(args.review, args.state, args.uploads, args.output_dir, args.writer_config, args.history, args.orders_dir)
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    server.store = store  # type: ignore[attr-defined]
    # La configurazione AI non passa dallo store: vedi ServizioImpostazioni.
    server.impostazioni = ServizioImpostazioni()  # type: ignore[attr-defined]
    print(f"Comparatore locale: http://{args.host}:{args.port}")
    print("Premere Ctrl+C per fermare il server. Gli originali non vengono modificati.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
