#!/usr/bin/env python3
"""Il passo che accende la Fase 5: la shortlist diventa una decisione.

`app/ai_client.py` esiste dalla 5a, e' misurato, ha i suoi test — e **non ha
nessun chiamante di produzione**. La prova sta nell'ultima run vera: zero
`SEMANTICO_PROPOSTO`, tutti e 948 i casi semantici arrivati al revisore non
decisi. Questo file e' il chiamante che mancava.

Una CLI come tutte le altre della catena, perche' l'orchestratore della 6c le
esegue come sottoprocessi e perche' cosi' si prova da sola:

    scripts/valuta_shortlist.py --shortlists <run>/dati/semantic_shortlists.json
                                --output     <run>/dati/ai_decisions.json
                                --rapporto   <run>/dati/ai_rapporto.json

Quattro proprieta' non negoziabili, decise altrove e qui solo applicate.

**1. Gli stati senza decisione si omettono dal file, non si inventano.**
`merge_match_decisions.py` tratta gia' una coppia senza decisione come
`DA_VERIFICARE`. Scrivere una `UNRESOLVED` finta al posto di un guasto di rete
farebbe sparire la differenza fra «il modello non ha saputo» e «non ho potuto
chiedere», e la seconda e' quella che si rimedia rilanciando.

**2. Il rapporto e' obbligatorio quanto il file delle decisioni.** E' la stessa
lezione della 6a: `--decisions` facoltativo rendeva una fase AI mai eseguita
indistinguibile da una che non aveva deciso niente. Un `--rapporto` facoltativo
la riaprirebbe da un'altra porta, perche' il numero che
`merge_match_decisions.py --decisions-attese` pretende deve venire da qui — dalla
contabilita' di chi ha chiamato il modello — e non dal conteggio del file che si
sta riconciliando, altrimenti il confronto e' una tautologia.

**3. Il degrado e' un valore di ritorno, mai un'eccezione.** E' il contratto del
client e vale anche per la CLI: nessuna chiave, tetto di spesa superato, rete
giu' escono con **i due file scritti**, un rapporto onesto e un codice d'uscita
che distingue «non ho potuto» da «ho deciso tutto». La catena prosegue lo
stesso: «se OpenRouter non risponde il programma tira dritto» e' una decisione
presa, e senza il file delle decisioni `build_review_data.py` produrrebbe un
confronto monco.

**4. Nessun test tocca la rete.** Il client accetta un trasporto iniettato, e
`main()` accetta `crea_client` per la stessa ragione.

⚠ **Ogni decisione porta l'impronta del caso su cui e' stata presa**, e la
verifica `merge_match_decisions.py`. Le guardie della 6a confrontano shortlist e
listino, **entrambi della run corrente**: rispetto a un file di decisioni preso
da un'altra run sono cieche per costruzione, perche' il gestionale e' lo stesso
file di settimana in settimana e le coppie `(riga, fornitore)` si sovrappongono
quasi tutte.

**Che cosa l'impronta prova, e che cosa no** — la prima stesura di questo
commento prometteva piu' di quanto mantiene, e la revisione l'ha smontata.
Prova che il caso valutato e' **identico** a quello di oggi: stesso articolo,
stessi candidati, stesso ordine, stessi punteggi. Se il listino di un fornitore
non cambia da una settimana all'altra, il caso e' davvero lo stesso e la
decisione della settimana scorsa passa — ed e' giusto che passi, perche' e'
esattamente quello che fa apposta la memoria delle risposte. Quello che
l'impronta **non** dice e' con che cosa quella decisione e' stata presa: per
quello ogni riga porta anche `ai_modello`, `ai_versione_prompt` e
`ai_versione_avversario`, e il confronto con la configurazione viva lo fara'
l'orchestratore della 6c.

Codici d'uscita, che sono un contratto con l'orchestratore:

| Codice | Significato |
|---|---|
| 0 | valutato tutto quello che c'era da valutare |
| 2 | errore d'uso: ingresso illeggibile, malformato, o uscite non scrivibili |
| 5 | degradato: i due file ci sono, ma una parte dei casi non e' stata valutata |

Il 5 **non e' un errore**: e' il modo di dire all'utente che la run e' degradata
senza fermarla.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Sequence


CARTELLA_SCRIPT = Path(__file__).resolve().parent
RADICE_SKILL = CARTELLA_SCRIPT.parent
for _cartella in (RADICE_SKILL / "app", CARTELLA_SCRIPT):
    if str(_cartella) not in sys.path:
        sys.path.insert(0, str(_cartella))

from ai_client import (  # noqa: E402
    PERCORSO_MEMORIA,
    Candidato,
    CasoValutazione,
    ClientAI,
    carica_configurazione,
    leggi_chiave,
    oscura,
)

# L'impronta la definisce chi scrive le shortlist, e la usano in due: questo
# script che la stampa su ogni decisione e `merge_match_decisions.py` che la
# verifica. Si importa, non si ricopia — due implementazioni di un'impronta
# vorrebbe dire che quella dimenticata e' quella che non riconosce un file
# vecchio.
from build_semantic_shortlists import impronta_caso  # noqa: E402


USCITA_OK = 0
USCITA_INGRESSO_NON_UTILIZZABILE = 2
USCITA_DEGRADATO = 5


def uscita_in_utf8() -> None:
    """Stdout e stderr in UTF-8, qualunque sia la tabella codici della console.

    Stessa ragione di `merge_match_decisions.py`: su Windows un processo che
    scrive su una pipe usa la codifica di sistema (cp1252 qui), e qui passano le
    descrizioni vere dei prodotti — basta un `CAFFÈ` perche' l'orchestratore
    della 6c si trovi davanti byte che non sono UTF-8."""

    for flusso in (sys.stdout, sys.stderr):
        try:
            flusso.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover - flussi rimpiazzati
            pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shortlists", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--rapporto",
        type=Path,
        required=True,
        help=(
            "Dove scrivere la contabilità della fase AI. Obbligatorio: è la "
            "fonte indipendente da cui l'orchestratore prende il numero per "
            "«merge_match_decisions.py --decisions-attese»."
        ),
    )
    parser.add_argument(
        "--memoria",
        type=Path,
        default=PERCORSO_MEMORIA,
        help=(
            "La memoria delle risposte già ottenute, indirizzata dal contenuto: "
            "un caso identico non si paga due volte. Predefinita: "
            "app/data/memoria_ai.json."
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


# ----------------------------------------------------------------------------
# Dall'ingresso ai casi
# ----------------------------------------------------------------------------


def numero_di_riga(valore: Any, dove: str) -> int:
    """Un numero di riga intero. Accetta `"441"`, rifiuta `441.5` e `True`.

    Non e' pedanteria: `int(441.9)` fa 441 in silenzio, e una riga sbagliata di
    uno e' esattamente il difetto che la 6a ha passato tre giri a chiudere."""

    if isinstance(valore, bool) or valore is None:
        raise ValueError(f"{dove}: {valore!r} non è un numero di riga")
    if isinstance(valore, int):
        return valore
    try:
        numero = float(str(valore).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{dove}: {valore!r} non è un numero di riga") from None
    # `Infinity` e `NaN` sono JSON validi per Python, e `int()` su un infinito
    # solleva `OverflowError`, che non e' fra quelle che `main` cattura: il
    # risultato era un traceback, esito 1 e nessuno dei due file scritto.
    # Trovato dalla revisione della 6b.
    if not math.isfinite(numero):
        raise ValueError(f"{dove}: {valore!r} non è un numero di riga")
    if numero != int(numero):
        raise ValueError(f"{dove}: {valore!r} non è un numero di riga intero")
    return int(numero)


def testo_non_vuoto(valore: Any, dove: str) -> str:
    """Il testo com'e', purche' non sia vuoto.

    Si restituisce **senza normalizzarlo**: e' quello che il modello leggera',
    ed e' quello su cui si calcola l'impronta. Toglierci gli spazi qui e non in
    `merge_match_decisions.py` farebbe fallire il confronto delle impronte su
    ogni caso, cioe' butterebbe una run intera."""

    if not isinstance(valore, str) or not valore.strip():
        raise ValueError(f"{dove}: {valore!r} non è un testo utilizzabile")
    return valore


def punteggio(valore: Any, dove: str) -> float:
    if isinstance(valore, bool) or valore is None:
        raise ValueError(f"{dove}: punteggio {valore!r} non utilizzabile")
    try:
        return float(valore)
    except (TypeError, ValueError):
        raise ValueError(f"{dove}: punteggio {valore!r} non utilizzabile") from None


def caso_dalla_voce(voce: Any, posizione: int) -> CasoValutazione:
    """Una voce di `semantic_shortlists.json` diventa un caso per il client.

    Un ingresso malformato e' un guasto, non un caso da saltare: la shortlist la
    scrive `build_semantic_shortlists.py`, quindi una voce storta vuol dire che
    qualcosa a monte si e' rotto, e proseguire in silenzio toglierebbe prodotti
    dal confronto senza dirlo.

    ⚠ `Candidato` non porta EAN ne' prezzo, ed e' voluto: l'EAN e' la verita' di
    riferimento del banco di prova e mostrarlo renderebbe falsa ogni misura; il
    prezzo non c'entra con l'identita' del prodotto. Non aggiungerli."""

    dove = f"voce {posizione}"
    if not isinstance(voce, dict):
        raise ValueError(f"{dove}: non è un oggetto ma {type(voce).__name__}")
    riga = numero_di_riga(voce.get("gestionale_source_row"), dove)
    fornitore = testo_non_vuoto(voce.get("supplier"), f"{dove}: supplier")
    descrizione = testo_non_vuoto(voce.get("description"), f"{dove}: description")

    grezzi = voce.get("candidates")
    if grezzi is None:
        grezzi = []
    if not isinstance(grezzi, list):
        raise ValueError(f"{dove}: «candidates» non è una lista ma {type(grezzi).__name__}")

    candidati = []
    for indice, grezzo in enumerate(grezzi):
        qui = f"{dove}, candidato {indice}"
        if not isinstance(grezzo, dict):
            raise ValueError(f"{qui}: non è un oggetto ma {type(grezzo).__name__}")
        candidati.append(
            Candidato(
                source_row=numero_di_riga(grezzo.get("source_row"), qui),
                description=testo_non_vuoto(grezzo.get("description"), f"{qui}: description"),
                score=punteggio(grezzo.get("score"), qui),
            )
        )

    return CasoValutazione(
        gestionale_source_row=riga,
        supplier=fornitore,
        descrizione=descrizione,
        candidati=tuple(candidati),
    )


def casi_dalle_shortlist(
    shortlists: Any,
) -> tuple[list[CasoValutazione], list[CasoValutazione]]:
    """I casi da valutare e quelli senza nessun candidato, separati.

    I secondi esistono davvero — sei su 948 nella run del 10 agosto — e non si
    mandano al modello: chiedergli di scegliere fra niente e' un uso sbagliato
    dell'API, e infatti `ClientAI` solleva. Si contano nel rapporto e finiscono
    a `DA_VERIFICARE` come tutte le coppie senza decisione.

    Le coppie duplicate si fermano qui. Piu' avanti diventerebbero due decisioni
    con la stessa coppia, e `merge_match_decisions.py` uscirebbe 3 dicendo
    «decisione duplicata» — cioe' mandando a cercare il guasto nel file
    sbagliato."""

    if not isinstance(shortlists, list):
        raise ValueError(f"il file delle shortlist deve essere una lista, non {type(shortlists).__name__}")

    casi: list[CasoValutazione] = []
    senza_candidati: list[CasoValutazione] = []
    viste: set[tuple[int, str]] = set()
    for posizione, voce in enumerate(shortlists):
        caso = caso_dalla_voce(voce, posizione)
        coppia = (caso.gestionale_source_row, caso.supplier)
        if coppia in viste:
            raise ValueError(
                f"voce {posizione}: la coppia riga {coppia[0]} / {coppia[1]} compare più di una volta"
            )
        viste.add(coppia)
        (casi if caso.candidati else senza_candidati).append(caso)
    return casi, senza_candidati


# ----------------------------------------------------------------------------
# Dagli esiti alle decisioni
# ----------------------------------------------------------------------------


def impronta_del_caso(caso: CasoValutazione) -> str:
    """L'impronta calcolata su cio' che e' stato davvero mandato al modello.

    Si passa il **caso**, non la voce da cui e' nato: se un giorno questo script
    cambiasse qualcosa fra le due — un candidato scartato, una descrizione
    accorciata — l'impronta deve raccontare quello che il modello ha visto, non
    quello che c'era nel file."""

    return impronta_caso(
        caso.gestionale_source_row,
        caso.supplier,
        caso.descrizione,
        [(c.source_row, c.description, c.score) for c in caso.candidati],
    )


def righe_decisioni(
    casi: Sequence[CasoValutazione],
    esiti: Sequence[Any],
    configurazione: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Le decisioni nel formato di `references/ai-decision-format.md`.

    `valuta_molti_con_verifica` restituisce gli esiti **nell'ordine dei casi in
    ingresso**, ed e' su questo che si appoggia l'accoppiamento. Se quella
    proprieta' saltasse, ogni decisione finirebbe sull'articolo sbagliato con la
    sua brava confidenza `ALTA`: qui si verifica invece di fidarsi, e un
    disaccordo ferma tutto — meglio zero decisioni che 948 attribuite a caso."""

    if len(esiti) != len(casi):
        raise RuntimeError(
            f"il client ha restituito {len(esiti)} esiti per {len(casi)} casi: "
            "non si può accoppiarli"
        )
    righe: list[dict[str, Any]] = []
    for caso, esito in zip(casi, esiti):
        decisione = esito.decisione
        if not decisione:
            continue
        sua = (decisione.get("gestionale_source_row"), decisione.get("supplier"))
        if sua != (caso.gestionale_source_row, caso.supplier):
            raise RuntimeError(
                f"esito fuori posto: decisione per {sua} sul caso "
                f"{(caso.gestionale_source_row, caso.supplier)}"
            )
        righe.append({
            **decisione,
            # Che cosa il modello ha visto: l'impronta la verifica il merge,
            # la descrizione serve a chi legge un rapporto o un file a mano.
            "ai_impronta_caso": impronta_del_caso(caso),
            "ai_articolo_mostrato": caso.descrizione,
            # **Chi** l'ha vista. L'impronta dice che il caso e' lo stesso, e
            # quando il listino di un fornitore non cambia da una settimana
            # all'altra il caso **e'** davvero lo stesso: l'impronta combacia e
            # una decisione vecchia passa. Riapplicarla a un caso identico non
            # e' sbagliato — e' quello che fa apposta la memoria — ma una presa
            # con il prompt `v1`, che sbagliava 5 `ALTA` e non aveva la verifica
            # avversariale, non deve poter entrare in un ordine di oggi senza
            # che nessuno se ne accorga. Il confronto con la configurazione viva
            # lo fa l'orchestratore della 6c: qui il dato si scrive, perche' e'
            # l'unico momento in cui esiste.
            **provenienza(configurazione),
        })
    return righe


def provenienza(configurazione: dict[str, Any] | None) -> dict[str, Any]:
    """Con quale modello e con quale prompt è stata presa una decisione."""

    configurazione = configurazione or {}
    return {
        "ai_modello": configurazione.get("model"),
        "ai_versione_prompt": configurazione.get("versione_prompt"),
        "ai_versione_avversario": configurazione.get("versione_avversario"),
    }


def costruisci_rapporto(
    *,
    casi: Sequence[CasoValutazione],
    senza_candidati: Sequence[CasoValutazione],
    esiti: Sequence[Any],
    righe: Sequence[dict[str, Any]],
    contabilita: dict[str, Any],
    configurazione: dict[str, Any],
    durata: float,
    guasto: str,
) -> dict[str, Any]:
    """La contabilita' della fase, che e' l'unica cosa che qualcuno leggera'.

    Il programma finito gira da solo: cio' che non e' stato valutato va contato
    qui, perche' i log non li legge nessuno.

    ⚠ Due conteggi per stato, e non e' una svista. `per_stato` ha una voce per
    caso; `per_stato_incluse_verifiche` viene dalla contabilita' del client e
    comprende anche il secondo giro avversariale, che parte su ogni `ACCEPT`:
    la sua somma e' maggiore del numero dei casi, ed e' giusto cosi'."""

    per_stato = Counter(esito.stato for esito in esiti)
    mancanti = Counter(esito.stato for esito in esiti if not esito.decisione)
    per_azione = Counter(str(riga.get("action")) for riga in righe)
    non_decisi = len(casi) - len(righe)
    # C'era del lavoro davanti e non se n'e' potuto tentare nemmeno un pezzo.
    # Non e' un caso di scuola: basta che un adattatore cambi e un listino esca
    # senza prezzi perche' `build_semantic_shortlists.py` scriva centinaia di
    # voci con `candidates: []` — e senza questa riga il rapporto diceva
    # «valutato tutto quello che c'era da valutare» ed usciva 0. Trovato dalla
    # revisione della 6b, eseguendolo.
    niente_da_fare = len(casi) == 0 and len(senza_candidati) > 0

    if guasto:
        motivo = f"guasto imprevisto della fase AI: {guasto}"
    elif niente_da_fare:
        motivo = (
            f"nessuno dei {len(senza_candidati)} casi ricevuti aveva un candidato da "
            "valutare: il passo deterministico non ha prodotto nessuna shortlist "
            "utilizzabile, e di solito vuol dire che un listino non si è caricato."
        )
    elif non_decisi > 0:
        dettaglio = ", ".join(f"{stato} {quanti}" for stato, quanti in mancanti.most_common())
        motivo = (
            f"{non_decisi} casi su {len(casi)} senza decisione"
            + (f": {dettaglio}" if dettaglio else ".")
        )
    else:
        motivo = ""

    rapporto = {
        "generato_il": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "casi_ricevuti": len(casi) + len(senza_candidati),
        "casi_valutabili": len(casi),
        "casi_senza_candidati": len(senza_candidati),
        # E' il numero che l'orchestratore passera' a
        # `merge_match_decisions.py --decisions-attese`.
        "casi_decisi": len(righe),
        "casi_senza_decisione": non_decisi,
        "per_stato": dict(sorted(per_stato.items())),
        "per_stato_incluse_verifiche": dict(sorted(contabilita.get("per_stato", {}).items())),
        "per_azione": dict(sorted(per_azione.items())),
        "chiamate": contabilita.get("chiamate", 0),
        "costo_usd": contabilita.get("costo_usd", 0.0),
        "dalla_memoria": contabilita.get("dalla_memoria", 0),
        "durata_s": round(float(durata), 3),
        "parallelismo": configurazione.get("parallelismo"),
        "model": configurazione.get("model"),
        "versione_prompt": configurazione.get("versione_prompt"),
        "versione_avversario": configurazione.get("versione_avversario"),
        "degradato": bool(guasto) or non_decisi > 0 or niente_da_fare,
        "motivo_degrado": motivo,
    }
    if senza_candidati:
        rapporto["coppie_senza_candidati"] = [
            [caso.gestionale_source_row, caso.supplier] for caso in senza_candidati[:10]
        ]
    return rapporto


def scrivi_json(percorso: Path, documento: Any) -> None:
    """In binario, perché su Windows `write_text` trasforma gli a capo in CRLF."""

    percorso.parent.mkdir(parents=True, exist_ok=True)
    testo = json.dumps(documento, ensure_ascii=False, indent=2) + "\n"
    percorso.write_bytes(testo.encode("utf-8"))


# ----------------------------------------------------------------------------
# La CLI
# ----------------------------------------------------------------------------


PREFISSO_AVANZAMENTO = "AVANZAMENTO "
# Una riga ogni dieci casi, piu' la prima e l'ultima: su 948 casi sono un
# centinaio di righe, abbastanza per una barra che si muove e poche abbastanza
# da non trasformare `stderr` in un file di log.
CASI_FRA_DUE_ANNUNCI = 10


def _annuncia_avanzamento(client: Any) -> Callable[[int, int], None]:
    """Stampa su `stderr` a che punto e' l'infornata, in forma leggibile a macchina.

    Il formato e' una riga sola — `AVANZAMENTO {json}` — perche' chi la legge e'
    un altro programma che scorre il flusso mentre arriva, e un JSON su piu'
    righe non si sa quando finisce.
    """

    def annuncia(fatti: int, totali: int) -> None:
        if fatti != totali and fatti % CASI_FRA_DUE_ANNUNCI:
            return
        contabilita = getattr(client, "contabilita", {}) or {}
        evento = {
            "fatti": fatti,
            "totali": totali,
            "chiamate": contabilita.get("chiamate"),
            "costo_usd": contabilita.get("costo_usd"),
        }
        print(
            PREFISSO_AVANZAMENTO + json.dumps(evento, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )

    return annuncia


def main(
    argv: Sequence[str] | None = None,
    *,
    crea_client: Callable[[dict[str, Any], Path], Any] | None = None,
) -> int:
    uscita_in_utf8()
    args = parse_args(argv)

    try:
        shortlists = json.loads(args.shortlists.read_text(encoding="utf-8"))
        casi, senza_candidati = casi_dalle_shortlist(shortlists)
    except (OSError, ValueError) as errore:
        print(f"Ingresso non utilizzabile: {errore}")
        return USCITA_INGRESSO_NON_UTILIZZABILE

    configurazione = carica_configurazione()
    print(
        f"{len(casi)} casi da valutare"
        + (f" ({len(senza_candidati)} senza candidati, esclusi)" if senza_candidati else "")
        + f" — modello {configurazione['model']},"
        f" parallelismo {configurazione['parallelismo']}.",
        flush=True,
    )

    # Da qui in avanti **si scrivono sempre i due file**. Un guasto imprevisto
    # non deve lasciare in mano all'orchestratore un traceback e nessun
    # artefatto: il `resolved_matches.json` della run precedente resterebbe sul
    # disco a farsi leggere come fresco. E' la stessa scelta della 6a.
    client = None
    esiti: list[Any] = []
    righe: list[dict[str, Any]] = []
    guasto = ""
    inizio = time.monotonic()
    try:
        client = (
            crea_client(configurazione, args.memoria)
            if crea_client is not None
            else ClientAI(configurazione, memoria=args.memoria)
        )
        # L'avanzamento esce su `stderr` e non su `stdout`: `stdout` porta il
        # rapporto finale, che l'orchestratore legge come JSON, e mescolarci
        # dentro delle righe di stato lo renderebbe illeggibile.
        try:
            client.avanzamento = _annuncia_avanzamento(client)
        except AttributeError:  # pragma: no cover - un client finto senza attributo
            pass
        esiti = list(client.valuta_molti_con_verifica(casi))
        righe = righe_decisioni(casi, esiti, configurazione)
    except Exception as errore:  # noqa: BLE001 — il degrado e' un valore di ritorno
        # `oscura` anche qui: un messaggio d'errore che riporta l'URL chiamato
        # o un'intestazione puo' portarsi dietro la chiave. Le si passa la
        # chiave vera invece di `None`: la sostituzione esatta e' la difesa
        # forte, l'espressione regolare `sk-…` e' solo la rete sotto.
        guasto = f"{type(errore).__name__}: {oscura(str(errore), leggi_chiave())}"
        righe = []
    durata = time.monotonic() - inizio

    contabilita = client.contabilita if client is not None else {}
    rapporto = costruisci_rapporto(
        casi=casi,
        senza_candidati=senza_candidati,
        esiti=esiti,
        righe=righe,
        contabilita=contabilita,
        configurazione=configurazione,
        durata=durata,
        guasto=guasto,
    )

    try:
        # Il rapporto della run precedente si toglie **prima**: se la scrittura
        # si ferma a meta', chi legge deve trovare un rapporto che non c'e' — e
        # accorgersene — non quello di ieri accanto alle decisioni di oggi.
        args.rapporto.unlink(missing_ok=True)
        # Prima le decisioni, poi il rapporto: un rapporto presente vuol dire
        # che le decisioni sono state scritte per intero.
        scrivi_json(args.output, righe)
        scrivi_json(args.rapporto, rapporto)
    except OSError as errore:
        print(f"Uscite non scrivibili: {errore}")
        return USCITA_INGRESSO_NON_UTILIZZABILE

    print(json.dumps(rapporto, ensure_ascii=False, indent=2))
    return USCITA_DEGRADATO if rapporto["degradato"] else USCITA_OK


if __name__ == "__main__":
    raise SystemExit(main())
