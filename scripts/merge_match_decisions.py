#!/usr/bin/env python3
"""Merge deterministic exact matches and bounded AI decisions into one audited result.

Due decisioni della Fase 5b vivono qui, e non nel client AI.

**Gli `ALTA` non si fanno confermare a schermo.** Deciso da Daniele l'11 agosto
2026: un revisore umano guarda comunque gli ordini prima di mandarli, quindi il
lavoro va speso nel rendere `ALTA` degno di fiducia — prompt misurato e verifica
avversariale — non in centinaia di conferme. Misurato sul banco con
`openai/gpt-5.6-luna` e il prompt `v3`, su tre passate indipendenti da 150 casi:
`ALTA` sbagliati **zero** ogni volta. Chi non e' `ALTA` la conferma la chiede
ancora.

**Un rifiuto sbagliato resta silenzioso**, e questa e' la difesa che si puo'
permettere. La verifica avversariale protegge dagli `ACCEPT` sbagliati, non dai
`REJECT`: un prodotto rifiutato sparisce dal confronto presso quel fornitore e
niente lo dice. Correggerlo costerebbe altre chiamate; contarlo no. Ogni rifiuto
porta con se' il punteggio del suo miglior candidato, e chi legge questi dati
decide che cosa segnalare.

Poi la Fase 6a, che chiude le porte da chiudere prima di accendere davvero il
passo AI. Tutte e quattro nascono dallo stesso principio: **un fallimento non
deve poter somigliare a un successo.**

**1. La riga accettata si verifica contro cio' che il modello ha visto davvero,
cioe' la shortlist.** Il numero di riga non identifica un prodotto: identifica
una posizione. Se il listino viene riletto dopo la costruzione della shortlist e
ha una riga in piu' in testa, la riga 441 e' un altro articolo, e la decisione
`ALTA` — che non chiede conferma a nessuno — metterebbe in ordine quell'altro.
Il record completo si prende ancora da `normalized_sources.json`, perche' la
shortlist non porta il moltiplicatore d'ordine, ma deve essere lo stesso
prodotto: **stesso EAN, stessa descrizione, stesso prezzo**.

⚠ Il confronto si fa contro il candidato della **shortlist**, mai contro quello
di `matching_result.json`. La prima versione di questa guardia sbagliava
esattamente qui, e la revisione avversariale l'ha smontata: `prepare_sources.py`
scrive `matching_result.json` e `normalized_sources.json` nella stessa
esecuzione, dagli stessi oggetti, quindi confrontare l'uno con l'altro e'
confrontare il listino con se stesso. L'unico artefatto che puo' essere vecchio
e' la shortlist — cioe' l'unico che il modello ha visto.

**2. Il modello non puo' nominare una riga che non gli e' stata mostrata.** Le
righe ammesse sono quelle della shortlist, e basta. Prima erano ammesse anche
quelle dei candidati EAN ambigui, che pero' al modello non arrivano: il passo
AI costruisce i suoi casi dalle sole shortlist.

**3. L'assenza di decisioni non e' un successo.** `--decisions-attese` e'
**obbligatorio**: chi chiama dichiara quante decisioni la fase AI ha prodotto e
il numero deve tornare. Facoltativo non serviva a niente — bastava dimenticarlo
per riaprire la porta. Zero e' un valore legittimo e attua la decisione
commerciale «se OpenRouter non risponde il programma tira dritto».

**4. Contare non dimostra appartenenza.** Un file di decisioni di un'altra run
riconcilia benissimo — dichiarate 942, trovate 942 — e non se ne applica
nemmeno una. Quindi si contano anche le decisioni che **non hanno trovato la
loro coppia**, e se non sono zero la catena si ferma.

**Il file si scrive sempre**, quando si arriva a costruirlo, con le coppie
guaste degradate a `DA_VERIFICARE`. Non scriverlo sembrava piu' prudente e non
lo era: resterebbe sul disco il `resolved_matches.json` della run precedente, e
`build_review_data.py` lo leggerebbe come se fosse di oggi. Un artefatto fresco
e degradato e' onesto; uno vecchio e scambiato per nuovo no. Il segnale sta nel
**codice d'uscita** e nel riepilogo.

⚠ Il riepilogo completo si stampa quando si arriva a costruirlo. I guasti che
si scoprono prima — ingresso illeggibile, decisioni non riconciliate — stampano
il loro messaggio e basta, perche' a quel punto non c'e' ancora niente da
riassumere. Il commento precedente diceva «si stampa sempre» e non era vero.

**Ogni coppia degradata porta `ai_decisione_scartata`**, ed e' il campo con cui
`build_review_data.py` conta questi casi in cima alla pagina. Senza, il vincolo
del progetto — «cio' che viene scartato va contato in un riepilogo visibile» —
sarebbe soddisfatto solo su `stdout`, che nessuno legge.

Poi la Fase 6b, che ne chiude altre due.

**5. Una decisione deve dichiarare il caso su cui e' stata presa.** I quattro
controlli qui sopra confrontano artefatti della **run corrente** — la shortlist
con il listino, la riga accettata con la shortlist — e rispetto a un file di
decisioni di un'altra run sono ciechi per costruzione: il gestionale e' lo
stesso file di settimana in settimana, quindi le coppie `(riga, fornitore)` si
sovrappongono quasi tutte e la riconciliazione dei numeri torna. Da qui
l'impronta: `valuta_shortlist.py` scrive su ogni decisione `ai_impronta_caso`,
calcolata su cio' che il modello ha davvero visto, e qui la si ricalcola dalla
shortlist di oggi. Se non corrisponde, la decisione non e' di questa run e la
coppia torna al revisore.

L'impronta e' **obbligatoria**: una decisione che non ce l'ha non e' vecchia, e'
di un produttore che non la scrive — e ammetterla renderebbe la guardia
aggirabile dimenticandosi un campo, che e' esattamente il difetto che
`--decisions-attese` ha appena chiuso. Le due cose restano pero' **distinte**
nella causa e nel messaggio, perche' mandano a cercare il guasto in due posti
diversi.

⚠ Che cosa l'impronta prova, e che cosa no. Prova che il caso e' **identico** a
quello di oggi. Se il listino di un fornitore non cambia da una settimana
all'altra, il caso e' davvero lo stesso e la decisione della settimana scorsa
passa — ed e' giusto, perche' e' quello che fa apposta la memoria delle
risposte. Quello che l'impronta **non** dice e' con quale modello e con quale
prompt quella decisione e' stata presa: per quello ogni riga porta anche
`ai_modello`, `ai_versione_prompt` e `ai_versione_avversario`, che qui si
leggono e non si giudicano — il confronto con la configurazione viva e' lavoro
dell'orchestratore, che la configurazione ce l'ha.

**6. Una coppia semantica senza shortlist e' lavoro che nessuno ha tentato.**
Il numero di `--decisions-attese` viene dal rapporto della fase AI, ma quel
rapporto conta i casi **del file che ha ricevuto**: se le shortlist sono 500
per 948 coppie, la fase ne valuta 500, ne dichiara 500, qui se ne trovano 500 e
tutto riconcilia — perche' le due parti stanno contando la stessa cosa mancante.
Quattrocentoquarantotto prodotti sparivano dalla valutazione con esito 0
dappertutto. `coda_semantica` e `coda_valutabile` erano gia' tutti e due qui,
stampati uno accanto all'altro e mai confrontati.

Poi, dal 21 settembre 2026, una regola che non chiude una porta ma ne apre una.

**7. Un abbinamento accettato porta il suo codice a barre agli altri
fornitori.** Il caso vero: il gestionale chiama `LINDA SETA ULTRA LUNGO ALI
18PZ` 8009405394204, NOCE e LARICE lo chiamano tutti e due 8009496220932.
Nessun aggancio per EAN; l'AI ha accettato la riga NOCE (2,31) e rifiutato
quella LARICE (2,25), scritta abbreviata, e NOCE ha vinto da solo. Ma la
riga accettata **dice** un codice, e presso LARICE quel codice c'era.

Dopo le decisioni, per ogni prodotto: se presso un fornitore A l'AI ha accettato
una riga con EAN X diverso da quello del prodotto, presso ogni altro fornitore S
**senza** abbinamento, e senza nessuna riga col codice del prodotto, l'unica
riga utilizzabile con EAN X entra come proposta. Sempre **da confermare**,
qualunque fosse la confidenza su A: la prova la da' il modello, non una
persona, e un «no» su S resta un no (lo ricorda il servizio, per impronta
della riga). Non scavalca mai una riga gia' scelta, una decisione scartata o un
EAN ambiguo; scavalca invece un rifiuto dell'AI presso S, perche' e' proprio il
caso da cui nasce. Le fonti si fotografano prima di cambiare qualcosa, quindi
niente catene e niente dipendenza dall'ordine dei fornitori.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


CARTELLA_SCRIPT = Path(__file__).resolve().parent
if str(CARTELLA_SCRIPT) not in sys.path:
    sys.path.insert(0, str(CARTELLA_SCRIPT))

# L'impronta la definisce chi scrive le shortlist. Si importa, non si ricopia:
# due implementazioni della stessa impronta vorrebbe dire che quella
# dimenticata e' quella che accetta un file di un'altra run.
from build_semantic_shortlists import impronta_caso  # noqa: E402


def uscita_in_utf8() -> None:
    """Stdout e stderr in UTF-8, qualunque sia la tabella codici della console.

    Su Windows un processo Python che scrive su una pipe usa la codifica di
    sistema (cp1252 qui). Il riepilogo porta le **descrizioni vere dei
    prodotti**, e basta un `CAFFÈ` perche' chi legge la pipe si trovi davanti a
    byte che non sono UTF-8. L'orchestratore della 6c leggera' proprio questa
    uscita: e' meglio che sia sempre la stessa, qualunque console ci sia sotto.
    Scoperto da un test, non da una run."""

    for flusso in (sys.stdout, sys.stderr):
        try:
            flusso.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover - flussi rimpiazzati
            pass


# La confidenza che vale una riga d'ordine senza domande a schermo.
CONFIDENZA_SENZA_CONFERMA = "ALTA"

# Il metodo delle righe entrate perche' presso un altro fornitore l'AI ha
# accettato una riga con lo stesso codice a barre (regola 7 in testa).
METODO_STESSO_CODICE = "EAN_DA_ALTRO_FORNITORE"
# Le lunghezze di un EAN/GTIN vero. Un codice interno corto o fatto di zeri non
# e' la prova di niente, e non deve trascinare righe da un listino all'altro.
LUNGHEZZE_EAN = frozenset({8, 12, 13, 14})

# Sopra questo punteggio un rifiuto merita di essere guardato. Misurato sul
# banco (150 casi, `openai/gpt-5.6-luna`, prompt `v3` con verifica): a 0,65
# segnala 5 dei 13 rifiuti sbagliati e 5 degli 87 giusti; a 0,60 ne segnala gli
# stessi 5 e il rumore sale a 12; a 0,80 restano 3 su 13. E' il punto in cui il
# segnale e' ancora meta' di quello che si mostra.
SOGLIA_RIFIUTO_SOSPETTO = 0.65

# I codici d'uscita, perche' l'orchestratore deve distinguere «ho fatto» da «non
# ho potuto» senza leggere la prosa. Sono un contratto: stanno in `SKILL.md` con
# questi numeri e un test li inchioda.
USCITA_OK = 0
USCITA_INGRESSO_NON_UTILIZZABILE = 2
USCITA_DECISIONI_NON_RICONCILIATE = 3
USCITA_LISTINO_DISALLINEATO = 4
USCITA_RIGA_INVENTATA = 5


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def coppia(riga: Any, fornitore: Any) -> tuple[Any, str]:
    """La chiave con cui matching, shortlist e decisioni si legano fra loro.

    `"12"` e `12` sono la stessa riga. Le decisioni la normalizzavano gia', gli
    altri due file no: bastava che il gestionale portasse la riga come testo
    perche' ogni decisione diventasse «senza riscontro», il riepilogo dicesse
    «i due file non parlano della stessa run» e l'orchestratore mandasse a
    rifare — cioe' a ripagare — la fase AI su una run sana. Trovato dalla
    revisione della 6b, eseguendolo.

    Un valore che non e' un intero resta com'e': meglio una coppia che non si
    lega — e si vede — di due righe diverse che collassano su una."""

    if not isinstance(riga, bool):
        try:
            numero = float(str(riga).strip())
            if numero == int(numero):
                riga = int(numero)
        except (TypeError, ValueError, OverflowError):
            pass
    return (riga, str(fornitore))


def impronta_attesa(shortlist: dict[str, Any]) -> str:
    """L'impronta che una decisione di **questa** run deve portare.

    Si ricalcola dalla shortlist di oggi con la stessa funzione che l'ha scritta
    (`build_semantic_shortlists.impronta_caso`), passandole i tre soli campi che
    il modello legge di ogni candidato."""

    return impronta_caso(
        shortlist.get("gestionale_source_row"),
        shortlist.get("supplier"),
        shortlist.get("description"),
        [
            (candidato.get("source_row"), candidato.get("description"), candidato.get("score"))
            for candidato in shortlist.get("candidates") or []
            if isinstance(candidato, dict)
        ],
    )


def prezzo_confrontabile(valore: Any) -> float | None:
    """Il prezzo come numero, da qualunque forma arrivi. `None` se non lo e'.

    ⚠ **`prepare_sources.py` scrive i prezzi come stringhe** (`json_decimal`
    serializza con `format(..., "f")`, quindi `"2.1000"`). La prima versione di
    questa funzione accettava solo `int` e `float`: sui dati veri **0 righe su
    25.093** avevano un prezzo, e il prezzo dentro l'identita' non confrontava
    niente. La revisione avversariale l'ha misurato.

    E il verso opposto conta quanto questo: se un artefatto porta `1.0` e
    l'altro `"1.0"` — un adattatore imparato, una normalizzazione futura — due
    scritture dello stesso numero non devono diventare due prodotti diversi."""

    if isinstance(valore, bool) or valore is None:
        return None
    if isinstance(valore, (int, float)):
        return round(float(valore), 6)
    try:
        return round(float(str(valore).strip().replace(",", ".")), 6)
    except (TypeError, ValueError):
        return None


def identita(record: dict[str, Any]) -> tuple[str, str, float | None]:
    """Che cosa prova che due righe sono lo stesso prodotto.

    Non il numero di riga, che e' una posizione e cambia quando il fornitore
    aggiunge una riga in testa. EAN, descrizione e prezzo: sono i tre campi che
    la shortlist e il listino normalizzato portano tutti e due.

    Il prezzo c'e' perche' la riga che finisce in ordine e' quella da cui si
    prende il prezzo, e quella dev'essere la stessa riga. Il modello il prezzo
    non lo vede, e giustamente: non c'entra con l'identita' del prodotto.
    ⚠ Nota di misura, perche' la prima stesura di questo commento diceva un
    numero sbagliato: fra le righe **utilizzabili** — le uniche che possono
    entrare in una shortlist o in un ordine — i listini veri di oggi hanno **3
    identita' duplicate in tutto, nessuna con prezzo diverso**. Le «24 di
    Larice» si contano solo includendo righe che non sono ordinabili. Quindi il
    campo non serve contro un problema di oggi: serve perche' costa una riga e
    perche' un fornitore che domani mette due lotti dello stesso articolo a
    prezzo diverso non deve poterli scambiare.

    `or ""` e non `get(campo, "")`: nei listini veri un EAN mancante arriva
    dal JSON come `null`, non come chiave assente, e `null` contro `""` sarebbe
    un disallineamento inventato che ferma una run buona."""

    return (
        str(record.get("ean") or "").strip(),
        " ".join(str(record.get("description") or "").split()).upper(),
        prezzo_confrontabile(record.get("unit_price_net")),
    )


def leggibile(chi: tuple[str, str, float | None] | None) -> str | None:
    """L'identita' come la leggerebbe una persona, per il riepilogo."""

    if chi is None:
        return None
    return chi[1] or chi[0] or "(riga senza descrizione né EAN)"


def miglior_punteggio(shortlist: dict[str, Any]) -> float | None:
    """Il punteggio del candidato migliore fra quelli mostrati all'AI.

    Non si legge `candidates[0]`: l'ordinamento e' una proprieta' di chi ha
    scritto la shortlist, e questa funzione non deve dipenderne."""
    punteggi = [
        candidato.get("score")
        for candidato in shortlist.get("candidates") or []
        if isinstance(candidato.get("score"), (int, float)) and not isinstance(candidato.get("score"), bool)
    ]
    return float(max(punteggi)) if punteggi else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matching", type=Path, required=True)
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--shortlists", type=Path, required=True)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument(
        "--decisions-attese",
        type=int,
        required=True,
        help=(
            "Quante decisioni la fase AI dichiara di aver prodotto. Obbligatorio: "
            "facoltativo bastava dimenticarlo per far passare una fase AI mai "
            "eseguita. Zero è legittimo e vuol dire «la fase AI è degradata e "
            "lo sa»."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def carica_decisioni(args: argparse.Namespace) -> tuple[list[Any] | None, int, str]:
    """Le decisioni, o il motivo per cui non si può proseguire."""

    if args.decisions is None:
        return [], USCITA_OK, ""
    if not args.decisions.exists():
        # Un percorso indicato e non trovato e' un guasto, non un file vuoto:
        # prima passava per «l'AI non ha deciso niente».
        return None, USCITA_DECISIONI_NON_RICONCILIATE, f"File delle decisioni non trovato: {args.decisions}"
    try:
        decisioni = load(args.decisions)
    except (OSError, ValueError) as errore:
        return None, USCITA_INGRESSO_NON_UTILIZZABILE, f"File delle decisioni illeggibile: {errore}"
    if not isinstance(decisioni, list):
        return None, USCITA_INGRESSO_NON_UTILIZZABILE, (
            "Il file delle decisioni deve essere una lista, non "
            f"{type(decisioni).__name__}."
        )
    return decisioni, USCITA_OK, ""


def indicizza_decisioni(decisioni: list[Any]) -> tuple[dict[tuple[int, str], dict[str, Any]] | None, int, str]:
    indice: dict[tuple[int, str], dict[str, Any]] = {}
    for decisione in decisioni:
        if not isinstance(decisione, dict):
            return None, USCITA_INGRESSO_NON_UTILIZZABILE, "Una decisione non è un oggetto."
        try:
            chiave = coppia(int(decisione["gestionale_source_row"]), decisione["supplier"])
        except (KeyError, TypeError, ValueError) as errore:
            return None, USCITA_INGRESSO_NON_UTILIZZABILE, f"Decisione senza coppia utilizzabile: {errore}"
        if chiave in indice:
            # Vince l'ultima riga del file, e un ACCEPT puo' diventare un
            # REJECT: il prodotto sparirebbe dal confronto per l'ordine delle
            # righe in un file, non per una decisione di qualcuno.
            return None, USCITA_DECISIONI_NON_RICONCILIATE, f"Decisione duplicata: {chiave}"
        if decisione.get("action") not in {"ACCEPT", "REJECT", "UNRESOLVED"}:
            return None, USCITA_INGRESSO_NON_UTILIZZABILE, f"Azione AI non valida: {decisione.get('action')}"
        if decisione["action"] == "ACCEPT":
            # Un `ACCEPT` senza riga e' una decisione malformata, non un
            # modello che ha inventato una riga: lo schema del client ammette
            # `source_row: null`, e raccontarlo come allucinazione manderebbe
            # chi legge a cercare il guasto dalla parte sbagliata. La riga si
            # normalizza a intero come si fa con la coppia: `"50"` scritto da
            # un altro programma non e' un guasto.
            try:
                decisione = {**decisione, "source_row": int(decisione["source_row"])}
            except (KeyError, TypeError, ValueError):
                return None, USCITA_INGRESSO_NON_UTILIZZABILE, (
                    f"ACCEPT senza una riga utilizzabile per {chiave}: "
                    f"{decisione.get('source_row')!r}"
                )
        indice[chiave] = decisione
    return indice, USCITA_OK, ""


def main() -> int:
    uscita_in_utf8()
    args = parse_args()
    try:
        matching = load(args.matching)
        normalized = load(args.normalized)
        shortlists = load(args.shortlists)
    except (OSError, ValueError) as errore:
        print(f"Ingresso non utilizzabile: {errore}")
        return USCITA_INGRESSO_NON_UTILIZZABILE

    decisions, codice, messaggio = carica_decisioni(args)
    if decisions is None:
        print(messaggio)
        return codice
    decision_index, codice, messaggio = indicizza_decisioni(decisions)
    if decision_index is None:
        print(messaggio)
        return codice

    shortlist_index = {
        coppia(item["gestionale_source_row"], item["supplier"]): item for item in shortlists
    }

    # La coda semantica e' il lavoro che la fase AI aveva davanti: ogni coppia
    # prodotto-fornitore che l'EAN non ha gia' risolto da solo. `valutabile`
    # toglie i casi senza nessun candidato, che il client rifiuta di mandare al
    # modello: sono sei su 948 nella run vera, e senza questo numero un
    # riepilogo onesto sembra dire «ne mancano sei».
    coda_semantica = sum(
        1
        for product in matching
        for match in product["suppliers"].values()
        if match["status"] != "EAN_ESATTO"
    )
    coda_valutabile = sum(1 for item in shortlists if item.get("candidates"))

    if len(decision_index) != args.decisions_attese:
        print(json.dumps({
            "errore": "decisioni non riconciliate",
            "dichiarate": args.decisions_attese,
            "trovate": len(decision_index),
            "coda_semantica": coda_semantica,
            "coda_valutabile": coda_valutabile,
        }, ensure_ascii=False, indent=2))
        return USCITA_DECISIONI_NON_RICONCILIATE

    output = []
    normalized_index = {
        supplier: {record.get("source_row"): record for record in records}
        for supplier, records in normalized.items()
        if supplier != "gestionale" and isinstance(records, list)
    }
    righe_per_codice = indice_per_codice(normalized)
    propagati: list[dict[str, Any]] = []
    stesso_codice_ambiguo: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    # Due conteggi che non sono stati: quanti match l'AI ha messo in un ordine
    # senza chiedere niente, e quanti rifiuti avevano un candidato che
    # somigliava molto. Il primo dice quanto pesa la fiducia data ad `ALTA`, il
    # secondo e' la sola traccia che resta di un rifiuto sbagliato.
    accettati_senza_conferma = 0
    rifiuti_sospetti = 0
    # Ogni riga accettata che nel listino non e' piu' il prodotto mostrato al
    # modello, e ogni riga che al modello non e' mai stata mostrata.
    disallineamenti: list[dict[str, Any]] = []
    righe_inventate: list[dict[str, Any]] = []
    ean_traditi: list[dict[str, Any]] = []
    # Le decisioni prese su un caso che oggi non e' piu' quello: un file di
    # un'altra run, o una shortlist ricostruita dopo la fase AI.
    di_un_altra_run: list[dict[str, Any]] = []
    # Le coppie semantiche per cui una shortlist non e' mai stata scritta: non
    # sono state valutate e nessuno le ha nemmeno provate.
    senza_shortlist: list[dict[str, Any]] = []
    # Le decisioni che hanno trovato la loro coppia. Quelle che non la trovano
    # sono il segno che i due file non parlano della stessa run.
    con_riscontro: set[tuple[int, str]] = set()
    # Quelle la cui coppia esiste ma nel frattempo l'EAN l'ha risolta da solo.
    # Non sono un guasto: succede ogni volta che si rifanno i passi
    # deterministici dopo la fase AI, e il risultato e' **migliore** di quello
    # che l'AI proponeva. Contarle fra le «senza riscontro» fermerebbe una run
    # sana e manderebbe a rifare — cioe' a pagare — la fase AI.
    superate_dall_ean: set[tuple[int, str]] = set()
    for product in matching:
        row = product["gestionale"]["source_row"]
        resolved = {"gestionale": product["gestionale"], "suppliers": {}}
        for supplier, match in product["suppliers"].items():
            key = coppia(row, supplier)
            if match["status"] == "EAN_ESATTO":
                if key in decision_index:
                    superate_dall_ean.add(key)
                selected = match["usable_candidates"][0]
                result = {
                    "status": "EAN_ESATTO",
                    "method": "EAN",
                    "selected": selected,
                    "confidence": "CERTA",
                    "requires_user_confirmation": False,
                    "rationale": "EAN gestionale presente in una sola riga utilizzabile.",
                    "alternatives": [],
                }
            else:
                shortlist = shortlist_index.get(key)
                # Una coppia semantica **senza shortlist** e' lavoro che nessuno
                # ha nemmeno tentato. Prima passava per «l'AI non ha deciso» e
                # non incideva su nessun conteggio: con 500 shortlist su 948
                # coppie, 448 prodotti sparivano dalla valutazione e tutta la
                # catena usciva 0, perche' il rapporto dichiarava le decisioni
                # contando lo **stesso** file troncato. La riconciliazione
                # tornava perche' le due parti contavano la stessa cosa
                # mancante. Trovato da due revisori su tre, eseguendolo.
                if shortlist is None:
                    senza_shortlist.append({"gestionale_source_row": row, "supplier": supplier})
                    shortlist = {"candidates": []}
                mostrati = {
                    candidato.get("source_row"): candidato
                    for candidato in shortlist.get("candidates", [])
                }
                decision = decision_index.get(key)
                # Prima di guardare che cosa dice, si guarda **su che cosa l'ha
                # detto**: una decisione presa su un altro caso non merita
                # nessuno degli altri controlli, e chiamarla «riga inventata»
                # manderebbe a cercare il guasto nel modello invece che nei
                # file.
                stantia = False
                senza_impronta = False
                if decision is not None:
                    con_riscontro.add(key)
                    attesa = impronta_attesa(shortlist)
                    dichiarata = decision.get("ai_impronta_caso")
                    stantia = dichiarata != attesa
                    senza_impronta = dichiarata is None
                    if stantia:
                        di_un_altra_run.append({
                            "gestionale_source_row": row,
                            "supplier": supplier,
                            "articolo": shortlist.get("description"),
                            # Solo se dice qualcosa: nel caso che questa guardia
                            # intercetta l'articolo cercato e' lo stesso e sono
                            # cambiati i candidati, quindi due campi sempre
                            # uguali sembrerebbero un difetto invece di
                            # un'informazione.
                            **(
                                {"articolo_della_decisione": decision.get("ai_articolo_mostrato")}
                                if decision.get("ai_articolo_mostrato") != shortlist.get("description")
                                else {}
                            ),
                            "impronta_dichiarata": dichiarata,
                            "impronta_attesa": attesa,
                        })
                if stantia:
                    # Due cause distinte, perche' mandano a cercare il guasto in
                    # due posti diversi: un'impronta che non c'e' e' un
                    # produttore che non la scrive, una che non corrisponde e'
                    # un file di un'altra run. Il messaggio in pagina le
                    # confondeva.
                    result = scartata(
                        shortlist,
                        "Decisione AI scartata: non dichiara su quale caso è stata presa."
                        if senza_impronta
                        else "Decisione AI scartata: è stata presa su un caso diverso da "
                        "quello di questa run.",
                        "DECISIONE_SENZA_IMPRONTA" if senza_impronta else "DECISIONE_DI_UNA_ALTRA_RUN",
                    )
                elif decision and decision["action"] == "ACCEPT":
                    source_row = decision.get("source_row")
                    candidate = mostrati.get(source_row)
                    # Su un `EAN_AMBIGUO` la riga giusta non e' un'opinione: il
                    # fornitore ha piu' righe con l'EAN del gestionale, e la
                    # scelta sta fra quelle. Il modello pero' vede la shortlist,
                    # che l'EAN non lo guarda mai (`build_semantic_shortlists`
                    # ordina per token della descrizione): senza questo
                    # controllo puo' scegliere una riga con un EAN che non
                    # c'entra, e con `ALTA` finirebbe in ordine senza conferma.
                    # I dati per verificarlo sono gia' qui.
                    ean_ammessi = {
                        c.get("source_row") for c in match.get("usable_candidates", [])
                    } if match["status"] == "EAN_AMBIGUO" else None
                    if ean_ammessi is not None and source_row not in ean_ammessi:
                        ean_traditi.append({
                            "gestionale_source_row": row,
                            "supplier": supplier,
                            "source_row": source_row,
                            "righe_con_l_ean_del_gestionale": sorted(r for r in ean_ammessi if r is not None),
                        })
                        result = scartata(
                            shortlist,
                            f"Decisione AI scartata: la riga {source_row} non ha l'EAN del "
                            "prodotto, e per questa coppia l'EAN lo decide.",
                            "EAN_NON_RISPETTATO",
                        )
                    elif candidate is None:
                        # Il modello ha nominato una riga che non gli e' stata
                        # mostrata. E' la regola piu' vecchia del modulo, e
                        # adesso ha un esito suo invece di uno schianto: chi la
                        # riceve deve poterla distinguere da un listino
                        # slittato, perche' la cosa da fare e' diversa.
                        righe_inventate.append({
                            "gestionale_source_row": row,
                            "supplier": supplier,
                            "source_row": source_row,
                            "righe_mostrate": sorted(r for r in mostrati if r is not None),
                        })
                        result = scartata(
                            shortlist,
                            f"Decisione AI scartata: la riga {source_row} non è fra quelle "
                            "mostrate al modello.",
                            "RIGA_NON_MOSTRATA",
                        )
                    else:
                        # Il record completo sta nel listino normalizzato — la
                        # shortlist non porta il moltiplicatore d'ordine — ma
                        # deve essere lo stesso prodotto che il modello ha visto.
                        selected = normalized_index.get(supplier, {}).get(source_row)
                        atteso = identita(candidate)
                        trovato = identita(selected) if selected is not None else None
                        if trovato != atteso:
                            disallineamenti.append({
                                "gestionale_source_row": row,
                                "supplier": supplier,
                                "source_row": source_row,
                                "mostrato_al_modello": leggibile(atteso),
                                "trovato_nel_listino": leggibile(trovato),
                            })
                            result = scartata(
                                shortlist,
                                f"Decisione AI scartata: la riga {source_row} del listino "
                                "non è più il prodotto mostrato al modello.",
                                "LISTINO_DISALLINEATO",
                            )
                        else:
                            confidenza = decision.get("confidence", "BASSA")
                            result = {
                                "status": "SEMANTICO_PROPOSTO" if match["status"] != "EAN_AMBIGUO" else "EAN_AMBIGUO_RISOLTO_AI",
                                "method": "SEMANTICO_AI" if match["status"] != "EAN_AMBIGUO" else "EAN_AI",
                                "selected": selected,
                                "confidence": confidenza,
                                # Qui, e non nel client: e' la riga che attua la
                                # decisione dell'11 agosto. Vedi il commento in testa.
                                "requires_user_confirmation": confidenza != CONFIDENZA_SENZA_CONFERMA,
                                "rationale": decision.get("rationale", ""),
                                "alternatives": shortlist.get("candidates", []),
                            }
                            if not result["requires_user_confirmation"]:
                                accettati_senza_conferma += 1
                elif decision and decision["action"] == "REJECT":
                    punteggio = miglior_punteggio(shortlist)
                    result = {
                        "status": "NON_TROVATO",
                        "method": "AI_RIFIUTATO",
                        "selected": None,
                        "confidence": decision.get("confidence", ""),
                        "requires_user_confirmation": False,
                        "rationale": decision.get("rationale", "Nessun candidato equivalente."),
                        "alternatives": shortlist.get("candidates", []),
                        # Il punteggio viaggia sempre, anche quando e' basso:
                        # la soglia la applica chi mostra i dati, in un posto
                        # solo. `None` vuol dire «shortlist senza punteggi»,
                        # che non e' la stessa cosa di «punteggio zero».
                        "ai_reject_best_score": punteggio,
                    }
                else:
                    result = {
                        "status": "DA_VERIFICARE",
                        "method": "REVISIONE",
                        "selected": None,
                        "confidence": decision.get("confidence", "") if decision else "",
                        "requires_user_confirmation": True,
                        "rationale": (
                            decision.get("rationale", "Decisione AI non disponibile o non conclusiva.")
                            if decision
                            else "Nessuna shortlist per questa coppia: i candidati non sono mai stati prodotti."
                            if not shortlist.get("candidates")
                            else "Decisione AI da effettuare."
                        ),
                        "alternatives": shortlist.get("candidates", []),
                    }
            resolved["suppliers"][supplier] = result
        # Dopo le decisioni di tutti i fornitori del prodotto, non durante: la
        # fonte puo' essere un fornitore che viene dopo nell'elenco.
        nuovi, ambigui = propaga_lo_stesso_codice(product, resolved, righe_per_codice)
        propagati.extend(nuovi)
        stesso_codice_ambiguo.extend(ambigui)
        for result in resolved["suppliers"].values():
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            # Contato qui e non alla decisione: un rifiuto che la regola 7 ha
            # sostituito con una proposta non e' piu' un rifiuto da guardare.
            punteggio = result.get("ai_reject_best_score")
            if (
                result.get("method") == "AI_RIFIUTATO"
                and punteggio is not None
                and punteggio >= SOGLIA_RIFIUTO_SOSPETTO
            ):
                rifiuti_sospetti += 1
        output.append(resolved)

    senza_riscontro = sorted(set(decision_index) - con_riscontro - superate_dall_ean)
    riepilogo = {
        "products": len(output),
        "supplier_results": counts,
        "coda_semantica": coda_semantica,
        "coda_valutabile": coda_valutabile,
        "decisioni_lette": len(decision_index),
        "decisioni_con_riscontro": len(con_riscontro),
        "decisioni_superate_dall_ean": len(superate_dall_ean),
        "decisioni_senza_riscontro": len(senza_riscontro),
        "accettati_senza_conferma": accettati_senza_conferma,
        "rifiuti_con_candidato_forte": rifiuti_sospetti,
        "soglia_rifiuto_sospetto": SOGLIA_RIFIUTO_SOSPETTO,
        "decisioni_scartate_per_disallineamento": len(disallineamenti),
        "decisioni_scartate_per_riga_inventata": len(righe_inventate),
        "decisioni_scartate_per_ean_non_rispettato": len(ean_traditi),
        "decisioni_scartate_perche_di_un_altra_run": len(di_un_altra_run),
        "coppie_senza_shortlist": len(senza_shortlist),
        "abbinamenti_per_stesso_codice": len(propagati),
        "stesso_codice_ambiguo": len(stesso_codice_ambiguo),
    }
    # Gli esempi si troncano, i conteggi no: dieci righe bastano a capire che
    # cosa e' successo, e il numero intero e' quello che conta.
    if disallineamenti:
        riepilogo["disallineamenti"] = disallineamenti[:10]
    if righe_inventate:
        riepilogo["righe_inventate"] = righe_inventate[:10]
    if ean_traditi:
        riepilogo["ean_non_rispettato"] = ean_traditi[:10]
    if di_un_altra_run:
        riepilogo["di_un_altra_run"] = di_un_altra_run[:10]
    if senza_shortlist:
        riepilogo["senza_shortlist"] = senza_shortlist[:10]
    if propagati:
        riepilogo["per_stesso_codice"] = propagati[:10]
    if stesso_codice_ambiguo:
        riepilogo["stesso_codice_ambiguo_esempi"] = stesso_codice_ambiguo[:10]
    if senza_riscontro:
        riepilogo["coppie_senza_riscontro"] = [list(chiave) for chiave in senza_riscontro[:10]]

    # Il file si scrive sempre: le coppie guaste sono gia' degradate a
    # `DA_VERIFICARE`, quindi e' un artefatto onesto. Vedi il commento in testa.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(riepilogo, ensure_ascii=False, indent=2))

    # Dal piu' grave: una riga fuori dal recinto o senza l'EAN dovuto dice che
    # il modello ha risposto male, un listino disallineato che gli artefatti
    # vengono da letture diverse, decisioni senza riscontro che i file non
    # parlano della stessa run.
    if righe_inventate or ean_traditi:
        return USCITA_RIGA_INVENTATA
    if disallineamenti:
        return USCITA_LISTINO_DISALLINEATO
    # Una decisione presa su un altro caso, una senza riscontro e una coppia
    # che una shortlist non ce l'ha mai avuta dicono la stessa cosa — gli
    # artefatti non parlano dello stesso lavoro — e meritano lo stesso codice:
    # chi lo riceve deve rifare i candidati e la fase AI, non rileggere il
    # listino.
    if di_un_altra_run or senza_riscontro or senza_shortlist:
        return USCITA_DECISIONI_NON_RICONCILIATE
    return USCITA_OK


def codice_ean(valore: Any) -> str:
    """Il codice a barre come si confronta qui: grezzo, come fa `build_matching`
    per la strada nativa, e solo se ha la forma di un EAN vero. Vuoto se no."""

    codice = str(valore or "").strip()
    if codice.isdigit() and len(codice) in LUNGHEZZE_EAN and codice.strip("0"):
        return codice
    return ""


def indice_per_codice(normalized: Any) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Per fornitore, le righe **utilizzabili** di ogni codice a barre.

    Utilizzabile con lo stesso predicato di `build_matching`: un omaggio, un
    componente d'espositore o una riga senza prezzo non si propone a nessuno.
    """

    indice: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if not isinstance(normalized, dict):
        return indice
    for fornitore, righe in normalized.items():
        if fornitore == "gestionale" or not isinstance(righe, list):
            continue
        per_codice: dict[str, list[dict[str, Any]]] = {}
        for riga in righe:
            if not isinstance(riga, dict) or not riga.get("usable", True):
                continue
            codice = codice_ean(riga.get("ean"))
            if codice:
                per_codice.setdefault(codice, []).append(riga)
        indice[fornitore] = per_codice
    return indice


def propaga_lo_stesso_codice(
    prodotto: dict[str, Any],
    risolto: dict[str, Any],
    righe_per_codice: dict[str, dict[str, list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """La regola 7: il codice di una riga accettata dall'AI vale anche altrove.

    Modifica `risolto` sul posto e restituisce le coppie cambiate e quelle
    saltate perche' ambigue, per il riepilogo. Non solleva mai: il file dei
    risultati si scrive sempre.
    """

    codice_prodotto = str((prodotto.get("gestionale") or {}).get("ean") or "").strip()
    stati_ean = prodotto.get("suppliers") or {}
    esiti = risolto.get("suppliers") or {}

    # Le fonti, fotografate prima di toccare qualcosa: una riga propagata non
    # diventa fonte, e l'ordine dei fornitori non cambia il risultato.
    fonti: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for fornitore, esito in esiti.items():
        selezionata = (esito or {}).get("selected")
        if (esito or {}).get("method") != "SEMANTICO_AI" or not isinstance(selezionata, dict):
            continue
        codice = codice_ean(selezionata.get("ean"))
        if codice and codice != codice_prodotto:
            fonti.setdefault(codice, []).append((fornitore, selezionata))
    if not fonti:
        return [], []

    propagati: list[dict[str, Any]] = []
    ambigui: list[dict[str, Any]] = []
    riga_gestionale = (prodotto.get("gestionale") or {}).get("source_row")
    for fornitore, esito in esiti.items():
        esito = esito or {}
        if (
            esito.get("selected")
            or esito.get("ai_decisione_scartata")
            or (stati_ean.get(fornitore) or {}).get("status") != "EAN_ASSENTE"
        ):
            continue
        trovate = [
            (codice, riga)
            for codice in sorted(fonti)
            for riga in (righe_per_codice.get(fornitore) or {}).get(codice, [])
        ]
        if not trovate:
            continue
        if len(trovate) > 1:
            ambigui.append({
                "gestionale_source_row": riga_gestionale,
                "supplier": fornitore,
                "righe": sorted(str(riga.get("source_row")) for _, riga in trovate),
            })
            continue
        codice, riga = trovate[0]
        da_chi = sorted(chi for chi, _ in fonti[codice])
        # La descrizione del primo in ordine alfabetico, non del primo trovato:
        # la frase non deve dipendere dall'ordine dei fornitori nel file.
        come_la_scrive = dict(fonti[codice])[da_chi[0]].get("description") or ""
        esiti[fornitore] = {
            "status": "SEMANTICO_PROPOSTO",
            "method": METODO_STESSO_CODICE,
            "selected": riga,
            # Mai `ALTA`: la prova la da' il modello su un altro listino.
            "confidence": "MEDIA",
            "requires_user_confirmation": True,
            "rationale": (
                f"{' e '.join(chi.upper() for chi in da_chi)} {'ha' if len(da_chi) == 1 else 'hanno'} "
                f"questo prodotto con il codice a "
                f"barre {codice} («{come_la_scrive}»), abbinato dall'analisi automatica. Anche "
                f"{fornitore.upper()} ha una riga con quel codice: controlla che sia lo stesso articolo."
            ),
            "alternatives": esito.get("alternatives", []),
            "propagato_da": da_chi,
            "codice_propagato": codice,
            # Quello che c'era prima, per risalire a chi aveva deciso che cosa.
            "prima": {
                chiave: esito.get(chiave)
                for chiave in ("status", "method", "confidence", "rationale")
            },
        }
        propagati.append({
            "gestionale_source_row": riga_gestionale,
            "supplier": fornitore,
            "source_row": riga.get("source_row"),
            "codice": codice,
            "da": da_chi,
        })
    return propagati, ambigui


def scartata(shortlist: dict[str, Any], motivo: str, causa: str) -> dict[str, Any]:
    """Una coppia la cui decisione AI e' stata buttata: torna al revisore.

    Non `NON_TROVATO`, che vuol dire «l'AI ha guardato e ha detto di no»: qui
    l'AI ha detto di si' e non ci si e' potuti fidare, il che e' un caso da
    guardare, non un prodotto da togliere dal confronto.

    `ai_decisione_scartata` non e' decorazione: e' il campo con cui
    `build_review_data.py` conta questi casi in cima alla pagina. Senza, un
    prodotto degradato e' indistinguibile in elenco da uno che l'AI non ha mai
    valutato, e il vincolo del progetto — «cio' che viene scartato va contato
    in un riepilogo visibile» — resterebbe soddisfatto solo su `stdout`, che
    nessuno legge."""

    return {
        "status": "DA_VERIFICARE",
        "method": "REVISIONE",
        "selected": None,
        "confidence": "",
        "requires_user_confirmation": True,
        "rationale": motivo,
        "alternatives": shortlist.get("candidates", []),
        "ai_decisione_scartata": causa,
    }


if __name__ == "__main__":
    raise SystemExit(main())
