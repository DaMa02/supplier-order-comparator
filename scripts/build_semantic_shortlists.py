#!/usr/bin/env python3
"""Build deterministic semantic candidate shortlists without deciding matches.

**Le righe con l'EAN del prodotto entrano sempre.** Il punteggio guarda solo i
token della descrizione, e i due testi li scrivono due persone diverse: misurato
sulle 1160 coppie `EAN_ESATTO` della run vera — dove la riga giusta e' certa
perche' l'EAN la identifica — la shortlist per sola descrizione l'avrebbe
mostrata **1014 volte su 1160**. Il 12,6% delle volte no, e non per poco:
`CHANTE BRILL ANTICALCARE ACETO 625ML` contro `CHANTEBR. A/CALCARE 625
EXTRARAPIDO` non entra nemmeno nel gruppo dei candidati.

Per un `EAN_ASSENTE` non c'e' rimedio, ed e' il caso normale. Ma quando lo stato
e' `EAN_AMBIGUO` — il fornitore ha piu' righe utilizzabili con quell'EAN e
bisogna scegliere fra loro — la riga giusta e' **una di quelle**, e mostrarne
altre al posto loro e' un errore che si puo' evitare: le si forza in shortlist.

Serve anche a valle: `merge_match_decisions.py` pretende che su un
`EAN_AMBIGUO` la riga accettata abbia l'EAN del prodotto. Senza questa forzatura
quella regola sarebbe insoddisfabile, e ogni ambiguo si fermerebbe."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


STOPWORDS = {
    "A", "AL", "ALLA", "CON", "DA", "DAL", "DE", "DEL", "DELLA", "DI", "E",
    "IL", "IN", "LA", "LE", "LO", "NEW", "PER", "PIU", "THE", "UN", "UNA",
}
ALIASES = {
    "DEODORANTE": "DEO",
    "CANDEG": "CANDEGGINA",
    "CANDEGG": "CANDEGGINA",
    "RIC": "RICARICA",
    "RICAR": "RICARICA",
    "PZ": "PEZZI",
    "PZZ": "PEZZI",
    "LT": "L",
    "LITRO": "L",
    "LITRI": "L",
    "GR": "G",
    "GRAMMI": "G",
}


def _confrontabile(valore: Any) -> Any:
    """Un numero come numero, tutto il resto come testo. Non solleva mai.

    Serve solo dentro l'impronta, e le due parti che la calcolano leggono dati
    di provenienza diversa: `valuta_shortlist.py` passa quello che ha davvero
    mandato al modello (interi e float), `merge_match_decisions.py` passa quello
    che trova nel file. `441` e `"441"` sono lo stesso numero di riga e non
    devono produrre due impronte diverse — e un valore storto non deve far
    schiantare un confronto che serve proprio a dire «questo file non va bene».
    """

    if isinstance(valore, bool) or valore is None:
        return str(valore)
    try:
        return round(float(valore if isinstance(valore, (int, float)) else str(valore).strip()), 6)
    except (TypeError, ValueError, OverflowError):
        # `OverflowError` non e' teorico: un intero JSON da 401 cifre — un file
        # corrotto, o scritto da un altro programma — faceva sollevare questa
        # funzione, e con lei `merge_match_decisions.py`, **prima** che
        # scrivesse `resolved_matches.json`. Restava sul disco quello della run
        # precedente, da farsi leggere come fresco: cioe' proprio il guasto che
        # il modulo dice di voler evitare. Chi serve a dire «questo file non va
        # bene» non puo' schiantarsi mentre lo dice.
        return str(valore)


def impronta_caso(
    gestionale_source_row: Any,
    supplier: Any,
    description: Any,
    candidati: Iterable[tuple[Any, Any, Any]],
) -> str:
    """Sedici cifre che dicono **che cosa il modello ha visto** per una coppia.

    Le guardie della Fase 6a confrontano la riga accettata con la shortlist e la
    shortlist con il listino, ma entrambi gli artefatti sono quelli della run
    corrente: rispetto a un file di decisioni prodotto la settimana scorsa sono
    cieche per costruzione, perche' il gestionale e' lo stesso file e le coppie
    `(riga, fornitore)` si sovrappongono quasi tutte. Una decisione vecchia
    verrebbe applicata a una shortlist nuova, e un `ACCEPT` con confidenza
    `ALTA` entra in ordine senza che nessuno lo guardi.

    L'impronta chiude quella porta: `valuta_shortlist.py` la scrive su ogni
    decisione, `merge_match_decisions.py` la ricalcola dalla shortlist di oggi e
    butta le decisioni che non corrispondono.

    Comprende **tutti** i candidati, non solo quello accettato, perche' anche un
    `REJECT` vecchio fa danno: dice «nessuno di questi va bene» a proposito di
    un elenco che oggi e' un altro, e il prodotto sparisce dal confronto presso
    quel fornitore senza che niente lo segnali.

    `candidati` e' una sequenza di terne `(source_row, description, score)`:
    esattamente i tre campi che il modello legge (`Candidato` non porta EAN ne'
    prezzo). L'ordine conta, perche' e' l'ordine in cui li ha visti."""

    canonico = json.dumps(
        {
            "riga": _confrontabile(gestionale_source_row),
            "fornitore": str(supplier),
            "articolo": str(description),
            "candidati": [
                [_confrontabile(riga), str(descrizione), _confrontabile(punteggio)]
                for riga, descrizione, punteggio in candidati
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()[:16]


def normalize_text(value: Any, *, tieni_il_piu: bool = False) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").upper())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^A-Z0-9.,+]+" if tieni_il_piu else r"[^A-Z0-9.,]+", " ", text)
    return " ".join(text.split())


def tokens(value: Any) -> set[str]:
    result = set()
    for token in normalize_text(value).split():
        normalized = ALIASES.get(token, token)
        if normalized not in STOPWORDS and len(normalized) > 1:
            result.add(normalized)
    return result


# Le quantita' scritte nel nome, per confrontare due formati: «18PZ» contro
# «X 9» deve dire «pacchi diversi», e un conflitto toglie 0,35 al punteggio.
#
# ⚠ Dal 21 settembre 2026 si leggono anche le forme con l'unita' **davanti**,
# che prima erano invisibili: `X 18`, `PZ.18`, `ML.500`, `LT.3`, `KG 4`. Il
# caso vero: il gestionale scrive `LINDA SETA ULTRA LUNGO ALI 18PZ`, LARICE
# `ASS. LINDA SETAMORBI X 18 LUNGO`, e in testa alla shortlist c'era
# `SETAMORBI X 9 LUNGO ALI` — un altro prodotto — perche' nessuna delle due
# righe LARICE dichiarava una quantita' leggibile. La riga giusta non e' stata
# accettata, e il prodotto e' andato a NOCE a 2,31 invece che a LARICE a
# 2,25. Prima non si leggevano nemmeno `3LT` e `250GR`: il `\b` dopo `L` o `G`
# falliva dentro `LT` e `GR`.
#
# Misurato sul gestionale del 18 settembre contro LARICE 39 e NOCE (355 e
# 308 prodotti di cui la riga giusta e' certa perche' l'EAN coincide; si
# nasconde l'EAN e si ordina per descrizione): riga giusta al primo posto
# 199 -> 215 e 220 -> 240, fra le prime cinque 292 -> 302 e 283 -> 284.
#
# Il principio e': **meglio non leggere un numero che leggerlo sbagliato**. Un
# falso conflitto toglie 0,35 proprio alla riga giusta; un numero non letto
# lascia il punteggio com'era.
#
# ⚠ Il punteggio entra nel testo mandato al modello e nella chiave della
# memoria AI (`ai_client._caso_serializzato`): cambiare questa lettura fa
# rifare le domande sui casi il cui punteggio cambia, non su tutti.
_UNITA_LETTE = {
    "ML": "ML", "L": "L", "LT": "L", "LITRI": "L", "LITRO": "L",
    "KG": "KG", "G": "G", "GR": "G",
    "PZ": "PEZZI", "PEZZI": "PEZZI",
    "ROT": "ROTOLI", "ROTOLI": "ROTOLI",
    "LAV": "LAVAGGI", "LAVAGGI": "LAVAGGI", "MIS": "LAVAGGI", "MISURINI": "LAVAGGI",
    "NOTTI": "NOTTI",
}
_NUMERO = r"\d+(?:\.\d+)?"
_UNITA_DOPO = re.compile(
    rf"({_NUMERO}) ?(ML|LT|LITRI|LITRO|L|KG|GR|G|PEZZI|PZ|ROTOLI|ROT|LAVAGGI|LAV|MISURINI|MIS|NOTTI)\b"
)
# L'unita' davanti: «X 18», «PZ.18», «ML.500», «LT.3», «KG4». Il punto prima e'
# ammesso («SALVACAM.ML.150», «VER.KG.1,5»), una lettera o una cifra no.
# Il numero non e' attaccato a lettere («SH. 250 ML 2IN1» non sono 2 ml), tranne
# la X di un moltiplicatore («GR.50X6»).
_UNITA_DAVANTI = re.compile(rf"(?<![A-Z0-9])(X|PZ|PEZZI|ML|LT|KG|GR)\.? ?({_NUMERO})(?![\dA-WYZ])")
# Dopo la X, un'unita' dice che la X e' un «per»: «2 X 250ML», «X 10 PZ». Non
# se quell'unita' ha un numero suo: in «X 2 GR.90» la X conta le saponette e i
# grammi sono 90.
_UNITA_DOPO_LA_X = re.compile(
    r" ?(ML|LT|L|KG|GR|G|CL|CM|MM|MT|M|PZ|PEZZI|LAV|LAVAGGI|MIS|MISURINI|NOTTI|ROT|ROTOLI|ANNI|MESI)\b(?!\.? ?\d)"
)
# Un'unita' subito dopo un numero: dice che quel numero ha gia' la sua.
_UNITA_SUBITO_DOPO = re.compile(
    r" ?(ML|LT|LITRI|LITRO|L|KG|GR|G|PEZZI|PZ|ROTOLI|ROT|LAVAGGI|LAV|MISURINI|MIS|NOTTI)\b"
)
# Solo il «+» attaccato ai due numeri: «70+8 LAV», «PZ.8+2», «500+250ML». Sei
# cifre bastano a qualunque quantita' vera, e un numero di migliaia di cifre
# farebbe sollevare `int()` e fermare tutta la shortlist.
_SOMMA = re.compile(r"(?<!\d)(?<!\d\.)(\d{1,6})((?:\+\d{1,6})+)(?!\d)(?!\.\d)")
# «45+» (eta' della crema), «FP50+», «6+ ANNI»: non sono quantita'.
_ETA = re.compile(r"(?<![\d.])\d+\+(?!\d)")
# Misure che non sono la quantita' della confezione, tolte dal testo grezzo
# prima di leggere (la normalizzazione trasformerebbe «-», «/», «°» e «=» in
# spazi, e i numeri resterebbero sciolti): le fasce («7-18 KG» del bambino,
# «KG. 11/25», «0-6» anni), le taglie («5°MIS.») e le formule («2X13=26»).
# Verifica avversariale del 21 settembre 2026: 56 pannolini BETULLA, CIPRESSO e
# ACERO leggevano un peso del bambino diverso per listino.
# La fascia di peso si porta via il suo KG, prima o dopo: resterebbe sciolto e
# si attaccherebbe al numero vicino («11-25KG 14 P» sarebbero 14 kg). Solo KG:
# le fasce vere dei listini sono pesi di bambini e animali, e in «60 ML 0-6»
# o «2/1 ML 250» il ML e' del numero accanto, non della fascia.
_FASCIA = re.compile(
    r"(?:(?<![A-Z])KG\.?\s*)?"
    r"\d+(?:[.,]\d+)?\s*[-/]\s*\d+(?:[.,]\d+)?"
    r"(?:\s*(?:KG|ANNI|MESI)(?![A-Z])\.?)?"
)
_ORDINALE = re.compile(r"\d+\s*[°ºª]")
_FORMULA = re.compile(r"\d+\s*X\s*\d+\s*=\s*\d+")


def _unita_davanti_girata(trovato: re.Match[str]) -> str:
    """«X 18» -> «18 PZ», «ML.500» -> «500 ML»; lascia stare le misure.

    Un numero subito prima vuol dire che non e' un'unita' davanti ma una misura
    o un codice: «30 X 40 CM», «10 PZ 1276», «25 LT 3». Una X seguita da
    un'unita' e' un «per»: «X 250ML» resta 250 ML, non 250 pezzi.

    ⚠ Tranne l'unita' col punto attaccato, che e' sempre la forma «unita'
    davanti»: in «X 2 GR.90» e «4 IN 1 GR.900» i grammi sono 90 e 900, non 2 e
    1. Lasciata com'era, `_UNITA_DOPO` incollava l'unita' al numero di prima e
    la riga giusta prendeva un falso conflitto (18 saponette ACERO, CALGOR
    e tre WHISKAT di NOCE; revisione del 21 settembre 2026). La barra la
    separa dal numero di prima.
    """

    unita, numero = trovato.group(1), trovato.group(2)
    parole_prima = trovato.string[: trovato.start()].split()
    prima = parole_prima[-1] if parole_prima else ""
    if re.fullmatch(_NUMERO, prima):
        # Con un numero subito prima l'unita' puo' essere sua o del numero che
        # segue. Tre forme, misurate sui listini veri:
        #   «GR.90» (punto attaccato)  -> sempre del numero che segue;
        #   «GR. 500» (punto e spazio) -> del numero che segue solo se quello
        #       prima conta dei pezzi («X 2 GR. 500»); in «ADDITIVO 500 GR. 100
        #       PIU'» i grammi sono 500;
        #   «ML 200» (solo lo spazio)  -> la forma di ACERO, «PH 3.5 ML 200»:
        #       del numero che segue se e' un volume o un peso e quel numero non
        #       ha un'unita' sua («50 LT 10 PZ» sono 50 litri). Per i pezzi no:
        #       dopo «PZ» viene spesso un codice articolo («10 PZ 1276»).
        dopo_l_unita = trovato.group(0)[len(unita):]
        pezzi_prima = len(parole_prima) > 1 and parole_prima[-2] == "X"
        if unita == "X":
            separa = False
        elif re.match(r"\.\d", dopo_l_unita):
            separa = True
        elif dopo_l_unita.startswith("."):
            separa = pezzi_prima
        else:
            # ...e solo se il numero dopo e' piu' grande: «PH 3.5 ML 200» si',
            # «CHICCA BIBERON 330 ML 3 FORI» no (sono 330 ml e tre fori).
            separa = (
                unita in {"ML", "LT", "KG", "GR"}
                and not _UNITA_SUBITO_DOPO.match(trovato.string, trovato.end())
                and (pezzi_prima or float(numero) > float(prima))
            )
        if separa:
            return f" | {numero} {unita} "
        return trovato.group(0)
    if unita == "X" and _UNITA_DOPO_LA_X.match(trovato.string, trovato.end()):
        return trovato.group(0)
    # «54 DOSI X 12=648 GR» (BETULLA): dopo il numero della X ne viene un altro
    # con la sua unita', e 12 sono i grammi di una dose, non dodici pezzi. Un
    # numero senza unita' no: in «TEMPE BOX X 80 4VELI» sono 80 fazzoletti.
    if unita == "X":
        dopo = re.match(rf" ?({_NUMERO})", trovato.string[trovato.end():])
        if dopo and _UNITA_SUBITO_DOPO.match(trovato.string, trovato.end() + dopo.end()):
            return trovato.group(0)
    return f"{numero} {'PZ' if unita == 'X' else unita} "


def attributes(value: Any) -> dict[str, list[float]]:
    # Il testo grezzo, non quello di `normalize_text`: il «+» delle somme
    # sparirebbe prima di arrivare qui.
    grezzo = str(value or "").upper()
    for misura in (_FORMULA, _FASCIA, _ORDINALE):
        grezzo = misura.sub(" ", grezzo)
    text = normalize_text(grezzo, tieni_il_piu=True).replace(",", ".")
    text = _ETA.sub(" ", text)
    letture = [text]
    if _SOMMA.search(text):
        # La somma vale due volte, il totale e la base: lo stesso articolo sta
        # come «8PZ» nel gestionale e «PZ.8+2» nel listino, e come «78
        # MISURINI» contro «70+8 LAV». Un numero in piu' non crea conflitti:
        # basta che una coppia combaci.
        letture = [
            _SOMMA.sub(lambda somma: str(sum(int(parte) for parte in somma.group(0).split("+"))), text),
            _SOMMA.sub(lambda somma: somma.group(1), text),
        ]
    parsed: dict[str, set[float]] = defaultdict(set)
    for lettura in letture:
        lettura = _UNITA_DAVANTI.sub(_unita_davanti_girata, " ".join(lettura.replace("+", " ").split()))
        for raw_number, raw_unit in _UNITA_DOPO.findall(lettura):
            number = float(raw_number)
            unit = _UNITA_LETTE[raw_unit]
            if raw_unit == "L" and "." not in raw_number and number >= 10:
                # «COCCOLONE 952ML 45L», «AMMORB LT2 40L»: una L sola dopo un
                # intero da 10 in su sono lavaggi. `LT` e `LITRI` restano litri.
                unit = "LAVAGGI"
            if unit == "L":
                unit, number = "ML", number * 1000
            elif unit == "KG":
                unit, number = "G", number * 1000
            parsed[unit].add(number)
    # Pezzi e peso (o volume) nello stesso nome: vale anche il totale, perche'
    # lo stesso articolo sta come «GR.250 X 2» e come «X 2 GR.500». Come per
    # le somme, un valore in piu' toglie conflitti e non ne crea.
    for pezzi in [valore for valore in parsed.get("PEZZI", ()) if 1 < valore <= 100]:
        for unit in ("ML", "G"):
            parsed[unit].update({valore * pezzi for valore in list(parsed.get(unit, ()))})
    return {key: sorted(values) for key, values in parsed.items() if values}


def attribute_comparison(left: dict[str, list[float]], right: dict[str, list[float]]) -> tuple[float, list[str]]:
    common_units = set(left) & set(right)
    if not common_units:
        return 0.5, []
    matches = 0
    conflicts = []
    for unit in sorted(common_units):
        compatible = any(abs(a - b) <= max(0.02 * max(a, b), 0.01) for a in left[unit] for b in right[unit])
        if compatible:
            matches += 1
        else:
            conflicts.append(unit)
    return matches / len(common_units), conflicts


def score_pair(query: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    query_text = normalize_text(query.get("description"))
    candidate_text = normalize_text(candidate.get("description"))
    query_tokens = tokens(query_text)
    candidate_tokens = tokens(candidate_text)
    union = query_tokens | candidate_tokens
    shared = query_tokens & candidate_tokens
    jaccard = len(shared) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, query_text, candidate_text, autojunk=False).ratio()
    query_attributes = attributes(query.get("description"))
    candidate_attributes = attributes(candidate.get("description"))
    attribute_score, conflicts = attribute_comparison(query_attributes, candidate_attributes)
    score = 0.45 * jaccard + 0.35 * sequence + 0.20 * attribute_score
    if conflicts:
        score -= 0.35
    return {
        "score": round(max(0.0, score), 6),
        "shared_tokens": sorted(shared),
        "attribute_conflicts": conflicts,
        "query_attributes": query_attributes,
        "candidate_attributes": candidate_attributes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    normalized = json.loads(args.normalized.read_text(encoding="utf-8"))
    queue = json.loads(args.queue.read_text(encoding="utf-8"))

    token_indexes: dict[str, dict[str, set[int]]] = {}
    ean_indexes: dict[str, dict[str, set[int]]] = {}
    for supplier in {item["supplier"] for item in queue}:
        index: dict[str, set[int]] = defaultdict(set)
        per_ean: dict[str, set[int]] = defaultdict(set)
        for record_index, record in enumerate(normalized[supplier]):
            if (
                not record.get("description")
                or not record.get("unit_price_net")
                or not record.get("usable", True)
                or record.get("row_type") == "DISPLAY_COMPONENT"
            ):
                continue
            for token in tokens(record["description"]):
                index[token].add(record_index)
            ean = str(record.get("ean") or "").strip()
            if ean:
                per_ean[ean].add(record_index)
        token_indexes[supplier] = index
        ean_indexes[supplier] = per_ean

    results = []
    forzati_totali = 0
    for item in queue:
        supplier = item["supplier"]
        query_tokens = tokens(item["description"])
        pool: set[int] = set()
        for token in query_tokens:
            pool.update(token_indexes[supplier].get(token, set()))

        # Le righe con lo stesso EAN entrano comunque, e prima di tutte: quando
        # lo stato e' `EAN_AMBIGUO` la riga giusta e' una di quelle, e lasciarla
        # fuori vuol dire far scegliere il modello fra le sbagliate.
        ean = str(item.get("ean") or "").strip()
        forzati = ean_indexes[supplier].get(ean, set()) if ean else set()
        pool.update(forzati)

        ranked = []
        for record_index in pool:
            candidate = normalized[supplier][record_index]
            scoring = score_pair(item, candidate)
            ranked.append(
                {
                    "source_row": candidate.get("source_row"),
                    "ean": candidate.get("ean"),
                    "description": candidate.get("description"),
                    "unit_price_net": candidate.get("unit_price_net"),
                    # ⚠ La riga del candidato viaggia **intera** nella sua parte
                    # commerciale, e non e' un di piu': quando il rifiuto e'
                    # sospetto la prima riga di questa lista diventa la proposta
                    # che l'utente puo' accettare, e `offer_from_match` la
                    # promuove a offerta vera. Con le sole quattro colonne di
                    # prima nasceva senza pezzi per collo, quindi `available:
                    # false`, e il «Si» rispondeva «la riga proposta non ha
                    # prezzo e confezione utilizzabili» — misurato sul confronto
                    # del 17 agosto 2026: **48 proposte, zero accettabili**.
                    # Il modello non le vede: `valuta_shortlist.py` costruisce i
                    # suoi `Candidato` con `source_row`, `description` e `score`
                    # e basta, quindi il prompt — e la chiave della memoria che
                    # ci si calcola sopra — non cambiano.
                    "supplier_code": candidate.get("supplier_code"),
                    "pieces_per_carton": candidate.get("pieces_per_carton"),
                    "order_multiplier": candidate.get("order_multiplier"),
                    "packaging": candidate.get("packaging"),
                    "availability": candidate.get("availability"),
                    "unit": candidate.get("unit"),
                    "pallet": candidate.get("pallet"),
                    "usable": candidate.get("usable", True),
                    "stesso_ean": record_index in forzati,
                    **scoring,
                }
            )
        # Ordine: prima l'EAN, poi il punteggio. L'EAN e' una prova, il
        # punteggio una somiglianza.
        ranked.sort(key=lambda value: (not value["stesso_ean"], -value["score"], value["source_row"] or 0))
        # Il taglio non puo' buttare via una riga con l'EAN giusto: se sono piu'
        # di `top_k` si mostrano tutte, perche' la scelta sta li' dentro.
        quanti = max(args.top_k, sum(1 for value in ranked if value["stesso_ean"]))
        forzati_totali += sum(1 for value in ranked[:quanti] if value["stesso_ean"])
        results.append({**item, "candidates": ranked[:quanti]})

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "queue_items": len(queue),
        "shortlists": len(results),
        "top_k": args.top_k,
        "candidati_con_lo_stesso_ean": forzati_totali,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
