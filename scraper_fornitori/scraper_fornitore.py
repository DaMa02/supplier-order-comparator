#!/usr/bin/env python3
"""Scheletro di scraper per il sito di un fornitore.

Questo file **non scarica niente da solo**: e' la meta' generica del lavoro —
sessione HTTP, accesso, ripresa dopo un'interruzione, scrittura incrementale
del CSV e metadati dell'estrazione. La meta' specifica di un sito (come si fa
il login, dove sta l'elenco, quali celle sono quali colonne) si scrive in una
sottoclasse di `SitoFornitore`: il modello da copiare e'
`modello_fornitore.py`, la ricetta sta nel `README.md`.

Due regole che questo modulo fa rispettare e che non vanno aggirate:

1. **le credenziali non passano mai dalla riga di comando** — si leggono
   dall'ambiente o si chiedono a schermo con la password nascosta, e non
   finiscono ne' negli output ne' nei log;
2. **il file scaricato non e' un listino buono finche' qualcuno non lo ha
   controllato** — `esegui()` scrive `metadata.json` con i conteggi
   dichiarati dal sito prima e dopo, e la decisione di accettare o bloccare il
   risultato spetta a `valida_catalogo.py`, non al solo campo `completo`.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


class ScraperError(RuntimeError):
    """Il sito ha risposto una cosa che non ci si aspettava."""


CallbackAvanzamento = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class Credenziali:
    utente: str
    password: str


@dataclass(frozen=True)
class Panoramica:
    """Quello che il sito dichiara di se' prima e dopo l'estrazione.

    `pagine` serve a sapere quando fermarsi; `articoli_dichiarati` e'
    facoltativo — molti siti non lo espongono — e serve solo al validatore per
    accorgersi che il catalogo e' cambiato mentre lo si scaricava.
    """

    pagine: int
    articoli_dichiarati: int | None = None


# ---------------------------------------------------------------------------
# La sessione HTTP: uguale per qualunque sito
# ---------------------------------------------------------------------------


class ClienteHttp:
    """Una sola sessione autenticata, con i cookie e i tentativi ripetuti.

    Volutamente sulla libreria standard: questa cartella non aggiunge
    dipendenze al progetto. Se un giorno un sito richiedesse un browser vero,
    quella e' una scelta da discutere prima, non da introdurre di nascosto.
    """

    def __init__(
        self,
        timeout_secondi: int = 30,
        tentativi: int = 3,
        *,
        user_agent: str = "ScraperListiniFornitori/1.0 (+confronto locale)",
        referer: str | None = None,
    ) -> None:
        self.timeout_secondi = timeout_secondi
        self.tentativi = tentativi
        self.user_agent = user_agent
        self.referer = referer
        self.cookie = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie)
        )

    def _richiesta(self, url: str, dati: dict[str, str] | None = None) -> str:
        corpo = urllib.parse.urlencode(dati).encode("utf-8") if dati else None
        intestazioni = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        if self.referer:
            intestazioni["Referer"] = self.referer
        richiesta = urllib.request.Request(url, data=corpo, headers=intestazioni)
        ultimo_errore: Exception | None = None
        for tentativo in range(self.tentativi + 1):
            try:
                with self.opener.open(richiesta, timeout=self.timeout_secondi) as risposta:
                    codifica = risposta.headers.get_content_charset() or "utf-8"
                    return risposta.read().decode(codifica, errors="replace")
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as errore:
                ultimo_errore = errore
                if tentativo >= self.tentativi:
                    break
                # Attesa crescente: un sito che sta rispondendo male non si
                # aiuta martellandolo.
                time.sleep(1.5 * (tentativo + 1))
        raise ScraperError(f"la richiesta a {url} e' fallita dopo i tentativi: {ultimo_errore}")

    def leggi(self, url: str) -> str:
        return self._richiesta(url)

    def invia(self, url: str, valori: dict[str, str]) -> str:
        return self._richiesta(url, valori)


# ---------------------------------------------------------------------------
# Due aiutanti per leggere l'HTML: servono quasi sempre
# ---------------------------------------------------------------------------


class AnalizzatoreForm(HTMLParser):
    """Estrae i campi nascosti di un form e il valore scelto nei `select`.

    Serve per i login dei gestionali web: quelli costruiti su ASP.NET
    WebForms rifiutano l'invio se non si rimandano indietro i campi nascosti
    (`__VIEWSTATE` e compagni) esattamente come sono arrivati.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.campi: list[dict[str, str]] = []
        self.select: dict[str, dict[str, Any]] = {}
        self._select_corrente: str | None = None
        self._opzione_corrente: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributi = {chiave.lower(): valore or "" for chiave, valore in attrs}
        if tag == "input":
            self.campi.append(attributi)
        elif tag == "select":
            identificativo = attributi.get("id") or attributi.get("name")
            if identificativo:
                self.select[identificativo] = {"attrs": attributi, "opzioni": []}
                self._select_corrente = identificativo
        elif tag == "option" and self._select_corrente:
            self._opzione_corrente = {
                "value": attributi.get("value", ""),
                "selected": "selected" in attributi,
                "text": "",
            }

    def handle_data(self, dati: str) -> None:
        if self._opzione_corrente is not None:
            self._opzione_corrente["text"] += dati

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._select_corrente and self._opzione_corrente is not None:
            self._opzione_corrente["text"] = " ".join(self._opzione_corrente["text"].split())
            self.select[self._select_corrente]["opzioni"].append(self._opzione_corrente)
            self._opzione_corrente = None
        elif tag == "select":
            self._select_corrente = None
            self._opzione_corrente = None

    def valori_nascosti(self) -> dict[str, str]:
        return {
            campo["name"]: campo.get("value", "")
            for campo in self.campi
            if campo.get("type", "").lower() == "hidden" and campo.get("name")
        }

    def trova_campo(self, frammento: str) -> dict[str, str] | None:
        """Cerca un `input` per pezzo di `id` o di `name`.

        Si cerca per frammento e non per nome esatto perche' i gestionali web
        antepongono ai nomi il percorso del controllo, e quel prefisso cambia
        appena qualcuno sposta un pannello nella pagina.
        """

        frammento = frammento.lower()
        for campo in self.campi:
            if frammento in campo.get("id", "").lower() or frammento in campo.get("name", "").lower():
                return campo
        return None

    def valore_select(self, identificativo: str) -> str | None:
        select = self.select.get(identificativo)
        if not select:
            return None
        for opzione in select["opzioni"]:
            if opzione["selected"]:
                return opzione["value"]
        return select["opzioni"][0]["value"] if select["opzioni"] else None


class AnalizzatoreTabella(HTMLParser):
    """Legge le righe di una tabella HTML come liste di celle testuali.

    `id_tabella` e' il frammento di `id` che identifica la tabella dei
    prodotti; le tabelle annidate dentro quella vengono seguite, cosi' una
    cella che contiene a sua volta una tabellina non spezza la riga.
    """

    def __init__(self, id_tabella: str) -> None:
        super().__init__(convert_charrefs=True)
        self.id_tabella = id_tabella
        self.righe: list[list[str]] = []
        self._pila_tabelle: list[str] = []
        self._dentro = False
        self._riga_corrente: list[str] | None = None
        self._cella_corrente: list[str] | None = None

    def _ricalcola_dentro(self) -> None:
        self._dentro = any(self.id_tabella in identificativo for identificativo in self._pila_tabelle)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributi = {chiave.lower(): valore or "" for chiave, valore in attrs}
        if tag == "table":
            self._pila_tabelle.append(attributi.get("id", ""))
            self._ricalcola_dentro()
        elif tag == "tr" and self._dentro and self._riga_corrente is None:
            self._riga_corrente = []
        elif tag in {"td", "th"} and self._riga_corrente is not None:
            self._cella_corrente = []

    def handle_data(self, dati: str) -> None:
        if self._cella_corrente is not None:
            self._cella_corrente.append(dati)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cella_corrente is not None and self._riga_corrente is not None:
            self._riga_corrente.append(" ".join("".join(self._cella_corrente).split()))
            self._cella_corrente = None
        elif tag == "tr" and self._riga_corrente is not None:
            self.righe.append(self._riga_corrente)
            self._riga_corrente = None
            self._cella_corrente = None
        elif tag == "table" and self._pila_tabelle:
            self._pila_tabelle.pop()
            self._ricalcola_dentro()


def analizza_form(html: str) -> AnalizzatoreForm:
    analizzatore = AnalizzatoreForm()
    analizzatore.feed(html)
    analizzatore.close()
    return analizzatore


def righe_di_tabella(html: str, id_tabella: str) -> list[list[str]]:
    analizzatore = AnalizzatoreTabella(id_tabella)
    analizzatore.feed(html)
    analizzatore.close()
    return analizzatore.righe


# ---------------------------------------------------------------------------
# La parte da scrivere per ogni sito
# ---------------------------------------------------------------------------


class SitoFornitore:
    """Il contratto fra lo scheletro e il sito di un fornitore.

    Si eredita da questa classe e si riempiono i quattro attributi e i tre
    metodi. Tutto il resto — ripresa, CSV, metadati, avanzamento — e' gia'
    scritto qui sotto e non va ricopiato.
    """

    #: Identificativo breve del fornitore, in minuscolo. Finisce nel nome del
    #: file CSV e nel prefisso delle variabili d'ambiente.
    nome: str = ""

    #: Le colonne del CSV, nell'ordine in cui si vogliono nel file. Sono anche
    #: le chiavi che `pagina()` deve restituire per ogni riga.
    campi_csv: Sequence[str] = ()

    #: Quante righe ci si aspetta per pagina. Serve solo ai messaggi: nessun
    #: controllo si appoggia a questo numero.
    righe_per_pagina: int | None = None

    #: Prefisso delle variabili d'ambiente delle credenziali. Se resta vuoto
    #: si usa `nome` in maiuscolo.
    prefisso_variabili: str = ""

    # -- da riscrivere ------------------------------------------------------

    def accedi(self, cliente: ClienteHttp, credenziali: Credenziali) -> None:
        """Esegue il login e lascia il `cliente` con la sessione aperta.

        Deve sollevare `ScraperError` quando l'accesso non e' riuscito:
        un login fallito che prosegue in silenzio produce un catalogo di
        pagine d'errore, e nessun controllo a valle se ne accorge.
        """

        raise NotImplementedError(self._da_scrivere("accedi"))

    def panoramica(self, cliente: ClienteHttp) -> Panoramica:
        """Legge dal sito quante pagine ci sono, e se lo dice quanti articoli.

        Viene chiamata due volte: prima di cominciare e alla fine. Le due
        risposte finiscono nei metadati e servono ad accorgersi che il
        catalogo e' cambiato mentre lo si scaricava.
        """

        raise NotImplementedError(self._da_scrivere("panoramica"))

    def pagina(self, cliente: ClienteHttp, numero: int) -> list[dict[str, Any]]:
        """Restituisce le righe di una pagina, gia' nelle chiavi di `campi_csv`.

        Una pagina che non contiene nessuna riga e' un guasto, non un elenco
        vuoto: lo scheletro si ferma da solo in quel caso.
        """

        raise NotImplementedError(self._da_scrivere("pagina"))

    # -- pronti all'uso -----------------------------------------------------

    def crea_cliente(self, timeout_secondi: int, tentativi: int) -> ClienteHttp:
        """Costruisce la sessione. Si riscrive solo per cambiare user agent."""

        return ClienteHttp(timeout_secondi=timeout_secondi, tentativi=tentativi)

    def nome_file_csv(self) -> str:
        return f"catalogo_{self.nome or 'fornitore'}.csv"

    def variabili_credenziali(self) -> tuple[str, str]:
        prefisso = (self.prefisso_variabili or self.nome or "FORNITORE").upper()
        return f"{prefisso}_UTENTE", f"{prefisso}_PASSWORD"

    def _da_scrivere(self, metodo: str) -> str:
        etichetta = type(self).__name__
        return (
            f"{etichetta}.{metodo}() non e' stato scritto: questa parte dipende dal sito "
            "del fornitore. Copia modello_fornitore.py e leggi il README."
        )


# ---------------------------------------------------------------------------
# Credenziali: mai negli argomenti, mai negli output
# ---------------------------------------------------------------------------


def leggi_credenziali(sito: SitoFornitore, utente: str | None = None) -> Credenziali:
    """Prende le credenziali dall'ambiente, o le chiede a schermo.

    La password non e' un argomento della riga di comando in nessun caso: la
    riga di comando finisce nella cronologia della shell, nei log del
    pianificatore e nell'elenco dei processi.
    """

    variabile_utente, variabile_password = sito.variabili_credenziali()
    etichetta = sito.nome or "fornitore"
    utente = utente or os.getenv(variabile_utente) or input(f"Utente {etichetta}: ")
    password = os.getenv(variabile_password) or getpass.getpass(f"Password {etichetta}: ")
    if not utente or not password:
        raise ScraperError(
            f"servono utente e password: passali in {variabile_utente} e {variabile_password}, "
            "o lasciali chiedere a schermo"
        )
    return Credenziali(utente=utente, password=password)


# ---------------------------------------------------------------------------
# Ripresa, CSV, metadati
# ---------------------------------------------------------------------------


def carica_ripresa(percorso: Path) -> dict[str, Any]:
    if not percorso.exists():
        return {"ultima_pagina_completata": 0, "righe_scritte": 0}
    return json.loads(percorso.read_text(encoding="utf-8"))


def scrivi_json(percorso: Path, valore: dict[str, Any]) -> None:
    """Scrive sostituendo: un'interruzione non lascia un file mezzo scritto."""

    temporaneo = percorso.with_suffix(percorso.suffix + ".tmp")
    temporaneo.write_text(json.dumps(valore, indent=2, ensure_ascii=False), encoding="utf-8")
    temporaneo.replace(percorso)


def aggiungi_righe(percorso: Path, campi: Sequence[str], righe: Iterable[dict[str, Any]]) -> int:
    righe = list(righe)
    if not righe:
        return 0
    nuovo = not percorso.exists() or percorso.stat().st_size == 0
    # `utf-8-sig`: senza la firma in testa Excel apre il CSV con la codifica
    # sbagliata e le accentate diventano illeggibili.
    with percorso.open("a", newline="", encoding="utf-8-sig") as flusso:
        scrittore = csv.DictWriter(flusso, fieldnames=list(campi))
        if nuovo:
            scrittore.writeheader()
        scrittore.writerows(righe)
    return len(righe)


def _avanzamento(callback: CallbackAvanzamento | None, **evento: Any) -> None:
    """Manda un evento di avanzamento, senza mai una credenziale dentro."""

    if callback is not None:
        callback(evento)


def _adesso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Il giro completo
# ---------------------------------------------------------------------------


def esegui(
    sito: SitoFornitore,
    argomenti: argparse.Namespace,
    *,
    credenziali: Credenziali | None = None,
    callback_avanzamento: CallbackAvanzamento | None = None,
) -> int:
    """Scarica il catalogo pagina per pagina e scrive CSV, ripresa e metadati."""

    if not sito.campi_csv:
        raise ScraperError("il sito non dichiara `campi_csv`: senza colonne non si scrive un CSV")

    cartella = argomenti.output.resolve()
    cartella.mkdir(parents=True, exist_ok=True)
    percorso_csv = cartella / sito.nome_file_csv()
    percorso_ripresa = cartella / "ripresa.json"
    percorso_metadati = cartella / "metadata.json"

    if argomenti.reset:
        for percorso in (percorso_csv, percorso_ripresa, percorso_metadati):
            if percorso.exists():
                percorso.unlink()
    elif percorso_csv.exists() and not argomenti.riprendi:
        raise ScraperError(
            f"{percorso_csv} esiste gia'. Usa --riprendi per continuare o --reset per rifarlo."
        )

    ripresa = (
        carica_ripresa(percorso_ripresa)
        if argomenti.riprendi
        else {"ultima_pagina_completata": 0, "righe_scritte": 0}
    )

    credenziali = credenziali or leggi_credenziali(sito, getattr(argomenti, "utente", None))
    cliente = sito.crea_cliente(argomenti.timeout, argomenti.tentativi)

    _avanzamento(callback_avanzamento, fase="accesso", messaggio=f"Accesso a {sito.nome}")
    sito.accedi(cliente, credenziali)

    _avanzamento(callback_avanzamento, fase="panoramica", messaggio="Lettura del catalogo")
    panoramica_iniziale = sito.panoramica(cliente)
    pagine_iniziali = panoramica_iniziale.pagine
    if pagine_iniziali < 1:
        raise ScraperError("il sito dichiara zero pagine: non c'e' niente da scaricare")

    ultima_pagina = min(argomenti.max_pagine or pagine_iniziali, pagine_iniziali)
    prima_pagina = int(ripresa["ultima_pagina_completata"]) + 1
    if prima_pagina > ultima_pagina:
        print("Nessuna pagina da scaricare: il catalogo e' gia' stato preso per intero.")
        _avanzamento(
            callback_avanzamento,
            fase="scaricamento_completato",
            pagina_corrente=ultima_pagina,
            pagine_totali=ultima_pagina,
            righe_scritte=int(ripresa.get("righe_scritte") or 0),
            messaggio="Catalogo gia' acquisito",
        )
        return 0

    iniziato = time.monotonic()
    for numero in range(prima_pagina, ultima_pagina + 1):
        righe = sito.pagina(cliente, numero)
        if not righe:
            # Zero righe quasi sempre vuol dire che la sessione e' scaduta o
            # che la pagina e' cambiata: fermarsi lascia il CSV valido fin
            # dove e' arrivato e la ripresa al punto giusto.
            raise ScraperError(f"nessuna riga di prodotto nella pagina {numero}")
        scritte = aggiungi_righe(percorso_csv, sito.campi_csv, righe)
        ripresa = {
            "ultima_pagina_completata": numero,
            "righe_scritte": int(ripresa["righe_scritte"]) + scritte,
            "pagine_totali_inizio": pagine_iniziali,
            "articoli_dichiarati_inizio": panoramica_iniziale.articoli_dichiarati,
            "fornitore": sito.nome,
            "aggiornata_il": _adesso(),
        }
        scrivi_json(percorso_ripresa, ripresa)
        passati = time.monotonic() - iniziato
        print(
            f"Pagina {numero}/{ultima_pagina}: {scritte} righe "
            f"({ripresa['righe_scritte']} in tutto, {passati:.1f}s)"
        )
        _avanzamento(
            callback_avanzamento,
            fase="scaricamento",
            pagina_corrente=numero,
            pagine_totali=ultima_pagina,
            righe_scritte=ripresa["righe_scritte"],
            secondi_passati=round(passati, 1),
            messaggio=f"Lettura del catalogo: pagina {numero} di {ultima_pagina}",
        )
        time.sleep(argomenti.pausa)

    _avanzamento(
        callback_avanzamento,
        fase="panoramica_finale",
        pagina_corrente=ultima_pagina,
        pagine_totali=ultima_pagina,
        righe_scritte=ripresa["righe_scritte"],
        messaggio="Verifica finale del catalogo",
    )
    pagine_finali: int | None = None
    articoli_finali: int | None = None
    errore_finale: str | None = None
    try:
        panoramica_finale = sito.panoramica(cliente)
        pagine_finali = panoramica_finale.pagine
        articoli_finali = panoramica_finale.articoli_dichiarati
    except ScraperError as errore:
        # Un guasto passeggero dopo che tutte le pagine sono state scaricate
        # non butta via il lavoro, ma resta scritto: sara' il validatore a
        # decidere se il conteggio iniziale basta.
        errore_finale = str(errore)

    conteggi = [
        valore
        for valore in (panoramica_iniziale.articoli_dichiarati, articoli_finali)
        if valore is not None
    ]
    metadati = {
        **ripresa,
        "pagine_totali_inizio": pagine_iniziali,
        "pagine_totali_fine": pagine_finali,
        "articoli_dichiarati_inizio": panoramica_iniziale.articoli_dichiarati,
        "articoli_dichiarati_fine": articoli_finali,
        "verifica_finale_riuscita": errore_finale is None,
        "errore_verifica_finale": errore_finale,
        "scarto_conteggi": abs(conteggi[0] - conteggi[1]) if len(conteggi) == 2 else None,
        "completo": ultima_pagina == pagine_iniziali,
        "csv": str(percorso_csv),
        "scaricato_il": _adesso(),
    }
    scrivi_json(percorso_metadati, metadati)
    print(f"Scritto {percorso_csv}")
    _avanzamento(
        callback_avanzamento,
        fase="finito",
        pagina_corrente=ultima_pagina,
        pagine_totali=ultima_pagina,
        righe_scritte=ripresa["righe_scritte"],
        messaggio="Estrazione completata",
    )
    return 0


def analizza_argomenti(descrizione: str | None = None, argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Gli argomenti comuni a qualunque scraper costruito su questo scheletro."""

    analizzatore = argparse.ArgumentParser(description=descrizione or __doc__)
    analizzatore.add_argument(
        "--utente",
        help="Nome utente. La password non si passa mai come argomento.",
    )
    analizzatore.add_argument("--output", type=Path, default=Path("output"), help="Cartella di destinazione.")
    analizzatore.add_argument("--riprendi", action="store_true", help="Continua da ripresa.json.")
    analizzatore.add_argument("--reset", action="store_true", help="Rifa' da capo i file in --output.")
    analizzatore.add_argument("--max-pagine", type=int, help="Limita le pagine, per una prova breve.")
    analizzatore.add_argument("--timeout", type=int, default=30, help="Secondi di attesa per richiesta.")
    analizzatore.add_argument("--tentativi", type=int, default=3, help="Tentativi ripetuti per richiesta fallita.")
    analizzatore.add_argument("--pausa", type=float, default=0.15, help="Pausa fra una pagina e l'altra, in secondi.")
    return analizzatore.parse_args(argv)


def avvia(sito: SitoFornitore, argv: Sequence[str] | None = None) -> int:
    """Il `main` di uno scraper concreto: `raise SystemExit(avvia(IlMioSito()))`."""

    try:
        return esegui(sito, analizza_argomenti(argv=argv))
    except (ScraperError, KeyboardInterrupt) as errore:
        print(f"ERRORE: {errore}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    print(
        "Questo file e' lo scheletro, non uno scraper: non e' configurato su nessun sito.\n"
        "Copia modello_fornitore.py, riempi le tre parti che dipendono dal sito e avvia quello.\n"
        "La ricetta sta nel README di questa cartella.",
        file=sys.stderr,
    )
    raise SystemExit(2)
