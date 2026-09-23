#!/usr/bin/env python3
"""Build the compact, supplier-agnostic view model used by the local web app."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# La soglia sta in un posto solo, ed e' quello che scrive il punteggio. Qui si
# applica: un rifiuto dell'AI con un candidato molto simile diventa un avviso
# sul prodotto, e finisce da solo nel filtro "Da confermare" della pagina.
try:
    from merge_match_decisions import SOGLIA_RIFIUTO_SOSPETTO
except ImportError:  # importato da fuori dalla cartella scripts/
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from merge_match_decisions import SOGLIA_RIFIUTO_SOSPETTO

# Il nome leggibile di un fornitore lo dichiara il registro, non un elenco
# scritto qui: e' la stessa fonte da cui il fornitore nasce.
import registro  # noqa: E402


def load(path: Path | None, default: Any) -> Any:
    if path is None or not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


USCITA_INGRESSO_NON_UTILIZZABILE = 2


def load_obbligatorio(path: Path) -> Any:
    """Un ingresso che manca — o che c'e' e non contiene niente — e' un guasto.

    `--resolved` passava da `load(..., [])`: se il file non c'era, il confronto
    si costruiva lo stesso — misurato, otto prodotti su 527 e due fornitori su
    quattro — usciva **0** e la pagina si apriva quasi vuota senza che niente
    dicesse perche'. Su un programma che gira da solo e i cui log nessuno legge,
    e' il modo peggiore di fallire.

    **Anche una lista vuota** produce quel sintomo identico, quindi la porta si
    chiude da tutti e due i lati. `merge_match_decisions.py` non scrive mai una
    lista vuota se non quando il gestionale e' vuoto, e `prepare_sources.py`
    quel caso lo impedisce gia': un `[]` qui vuol dire che qualcosa a monte non
    ha fatto il suo lavoro.

    L'uscita e' **2**, lo stesso numero che il resto della fase usa per
    «ingresso non utilizzabile»: un guasto dichiarato, non un `SystemExit` con
    una stringa che uscirebbe 1."""

    def fermati(motivo: str) -> None:
        print(f"[ERRORE] {motivo}", file=sys.stderr)
        raise SystemExit(USCITA_INGRESSO_NON_UTILIZZABILE)

    if not path.exists():
        fermati(
            f"Il file dei match risolti non esiste: {path}. Il passo precedente "
            "della catena non l'ha prodotto: non si costruisce un confronto senza."
        )
    try:
        contenuto = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as errore:
        fermati(f"Il file dei match risolti non si legge: {path} — {errore}")
    if not isinstance(contenuto, list) or not contenuto:
        fermati(
            f"Il file dei match risolti non contiene nessun prodotto: {path}. "
            "Un confronto vuoto marcato «pronto» è peggio di nessun confronto."
        )
    return contenuto


def load_dichiarato(path: Path | None, che_cosa: str) -> Any:
    """Un ingresso facoltativo: se non lo si chiede va bene, se manca no.

    `--displays` era rimasto sul ripiego silenzioso: chi lancia la fase lo passa
    apposta — l'orchestratore lo dichiara obbligatorio — ma se il file non
    c'era, `load(..., [])` faceva sparire **tutti** gli espositori dal confronto
    senza una parola. Non chiederlo affatto resta legittimo: vuol dire che per
    questa run non ci sono espositori. Un file **vuoto** resta legittimo allo
    stesso modo: vuol dire che non ne sono stati trovati.
    """

    if path is None:
        return []
    if not path.exists():
        print(
            f"[ERRORE] Il file {che_cosa} non esiste: {path}. È stato chiesto, quindi "
            "il passo precedente doveva produrlo: senza, quella merce sparirebbe dal "
            "confronto senza che niente lo dica.",
            file=sys.stderr,
        )
        raise SystemExit(USCITA_INGRESSO_NON_UTILIZZABILE)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as errore:
        print(f"[ERRORE] Il file {che_cosa} non si legge: {path} — {errore}", file=sys.stderr)
        raise SystemExit(USCITA_INGRESSO_NON_UTILIZZABILE)


def number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def money(value: Any) -> float | None:
    parsed = number(value)
    return round(parsed, 4) if parsed is not None else None


def suggested_quantity(value: Any) -> int | None:
    """Colli suggeriti dalla colonna "Colli" del gestionale: intero >= 0 oppure None."""
    parsed = number(value)
    if parsed is None or parsed < 0:
        return None
    return int(round(parsed))


def supplier_name(supplier_id: str) -> str:
    """Il nome leggibile del fornitore, dal registro degli adattatori.

    Stessa fonte del servizio e del writer: erano tre elenchi diversi, tutti
    fermi ai quattro fornitori del 2026, e un fornitore imparato compariva col
    suo identificativo tecnico.
    """

    return registro.nome_del_fornitore(supplier_id)


def normalized_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").upper())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", text).split())


def impronta_articolo(offerta: Any) -> str:
    """Che cosa identifica l'ARTICOLO di un'offerta, non le sue condizioni.

    Serve a far scadere la conferma («confermo che è lo stesso articolo») quando
    il ricalcolo della settimana dopo abbina quel prodotto a una riga diversa
    del listino: la casella restava spuntata su un articolo che l'utente non
    aveva mai visto, e la compilazione passava (revisione del 14 agosto 2026).

    Dentro ci va soltanto l'identità: fornitore, EAN, codice articolo, nome
    normalizzato. Il prezzo e la confezione NO — cambiano ogni settimana sullo
    stesso articolo, e rifare la domanda a ogni ritocco di listino insegnerebbe
    a spuntare senza leggere. Chi vuole l'impronta commerciale completa usa
    `rejected_candidate_key`, che risponde a un'altra domanda.
    """

    if not isinstance(offerta, dict):
        return ""
    pezzi = [
        str(offerta.get("supplierId") or offerta.get("supplier_id") or offerta.get("supplier") or ""),
        str(offerta.get("ean") or offerta.get("gtin") or "").strip(),
        str(offerta.get("supplierCode") or offerta.get("supplier_code") or "").strip(),
        normalized_name(offerta.get("description") or offerta.get("name")),
    ]
    return "|".join(pezzi)


def rejected_candidate_key(supplier_id: str, candidate: dict[str, Any]) -> str:
    """Impronta della riga proposta all'utente dopo un rifiuto sospetto.

    La decisione umana deve smettere di valere appena cambia una qualunque
    caratteristica commerciale della riga: posizione, nome, EAN, codice,
    prezzo o confezione. La run viene verificata separatamente dal servizio.
    """

    payload = {
        "supplier": str(supplier_id or ""),
        "source_row": candidate.get("source_row"),
        "description": str(candidate.get("description") or ""),
        "ean": str(candidate.get("ean") or ""),
        "supplier_code": str(candidate.get("supplier_code") or ""),
        "unit_price_net": number(candidate.get("unit_price_net")),
        "order_multiplier": number(candidate.get("order_multiplier")),
        "pieces_per_carton": number(candidate.get("pieces_per_carton")),
        "packaging": str(candidate.get("packaging") or ""),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def supplier_ids(resolved: list[dict[str, Any]], display_offers: list[dict[str, Any]]) -> list[str]:
    found = set()
    for product in resolved:
        found.update((product.get("suppliers") or {}).keys())
    found.update(str(item.get("supplier") or item.get("supplier_id") or "") for item in display_offers)
    found.discard("")
    preferred = [supplier for supplier in ("betulla", "larice", "noce", "cipresso") if supplier in found]
    return preferred + sorted(found - set(preferred))


def offer_from_match(supplier_id: str, result: dict[str, Any], last_price: float | None) -> dict[str, Any]:
    selected = result.get("selected") or None
    alternatives = result.get("alternatives") or []
    if not selected:
        offer = {
            "supplierId": supplier_id,
            "supplierName": supplier_name(supplier_id),
            "available": False,
            "status": result.get("status") or "NON_TROVATO",
            "method": result.get("method") or "",
            "confidence": result.get("confidence") or "",
            "requiresConfirmation": bool(result.get("requires_user_confirmation")),
            "confirmed": False,
            "rationale": result.get("rationale") or "",
            "alternatives": alternatives[:3],
            "price": 0,
            "unitsPerOrderUnit": 1,
            "pricePerPiece": 0,
            "matchStatus": result.get("status") or "NON_TROVATO",
            # Quanto somigliava il candidato migliore che l'AI ha scartato.
            # `None` quando il rifiuto non e' dell'AI o la shortlist non aveva
            # punteggi: la pagina distingue «non lo so» da «somigliava poco».
            "rejectBestScore": number(result.get("ai_reject_best_score")),
        }
        # Un rifiuto sospetto non deve terminare in un avviso senza uscita. La
        # prima riga della shortlist è la proposta che l'utente può accettare o
        # rifiutare; diventa un'offerta vera soltanto dopo la sua decisione.
        best = alternatives[0] if alternatives and isinstance(alternatives[0], dict) else None
        score = offer["rejectBestScore"]
        if result.get("method") == "AI_RIFIUTATO" and best and score is not None and score >= SOGLIA_RIFIUTO_SOSPETTO:
            candidate_offer = offer_from_match(
                supplier_id,
                {
                    "selected": best,
                    "status": "SEMANTICO_PROPOSTO",
                    "method": "CORREZIONE_UTENTE",
                    "confidence": "UTENTE",
                    "requires_user_confirmation": False,
                    "rationale": result.get("rationale") or "",
                    "alternatives": [],
                },
                last_price,
            )
            candidate_offer["candidateKey"] = rejected_candidate_key(supplier_id, best)
            candidate_offer["score"] = score
            offer["rejectedCandidate"] = candidate_offer
        return offer

    unit_price = money(selected.get("unit_price_net"))
    factor = number(selected.get("order_multiplier"))
    factor_kind = "unità"
    if factor is None:
        factor = number(selected.get("pieces_per_carton"))
        factor_kind = "pezzi/collo"
    order_price = money(unit_price * factor) if unit_price is not None and factor is not None else None
    difference = money(unit_price - last_price) if unit_price is not None and last_price is not None else None
    difference_pct = round((difference / last_price) * 100, 2) if difference is not None and last_price else None
    details = [selected.get("packaging"), selected.get("availability"), selected.get("unit")]
    if selected.get("pallet") not in (None, ""):
        details.append(f"Pedana: {selected['pallet']}")
    requires_confirmation = bool(result.get("requires_user_confirmation"))
    return {
        "supplierId": supplier_id,
        "supplierName": supplier_name(supplier_id),
        # ⚠ `> 0`, non `is not None`.  Un prezzo che si legge come 0,00 passava
        # tutti i filtri e poi **vinceva** il confronto, perche' l'ordinamento
        # mette il piu' basso davanti: il prodotto finiva assegnato al fornitore
        # la cui cella non si era lasciata leggere, a totale zero, e la soglia
        # minima d'ordine non scattava perche' zero e' sotto qualunque soglia.
        # La difesa che c'era (PREZZI_A_ZERO) ragiona sulla mediana dell'intero
        # listino: prende la colonna sbagliata su tutto un file, non la riga
        # singola dentro un listino sano (revisione del 14 agosto 2026).
        "available": (
            unit_price is not None and unit_price > 0
            and factor is not None and factor > 0
            and bool(selected.get("usable", True))
        ),
        "status": result.get("status") or "",
        "method": result.get("method") or "",
        "confidence": result.get("confidence") or "",
        "requiresConfirmation": requires_confirmation,
        "confirmed": not requires_confirmation,
        "rationale": result.get("rationale") or "",
        "description": selected.get("description") or "",
        "ean": selected.get("ean") or "",
        "supplierCode": selected.get("supplier_code"),
        "sourceRow": selected.get("source_row"),
        "unitPriceNet": unit_price,
        "quantityFactor": factor,
        "quantityFactorLabel": factor_kind,
        "orderUnitPriceNet": order_price,
        "price": order_price,
        "unitsPerOrderUnit": factor,
        "pricePerPiece": unit_price,
        "matchStatus": result.get("status") or "",
        "lastPriceDifference": difference,
        "lastPriceDifferencePct": difference_pct,
        "details": " | ".join(str(item) for item in details if item not in (None, "")),
        "alternatives": alternatives[:3],
    }


def suspect_reject_warnings(product_id: str, name: str, offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gli avvisi per i rifiuti dell'AI che avevano un candidato molto simile.

    E' la sola difesa contro il rifiuto sbagliato, che altrimenti non lascia
    traccia: la verifica avversariale della Fase 5b protegge dagli `ACCEPT`
    sbagliati, non dai `REJECT`. Non blocca niente — un rifiuto giusto e' il
    caso normale, e la meta' di questi avvisi lo sara' — ma il prodotto entra
    nel filtro "Da confermare" invece di sparire in silenzio."""
    avvisi = []
    for offer in offers:
        punteggio = offer.get("rejectBestScore")
        if offer.get("method") != "AI_RIFIUTATO" or punteggio is None:
            continue
        if punteggio < SOGLIA_RIFIUTO_SOSPETTO:
            continue
        candidate = offer.get("rejectedCandidate") if isinstance(offer.get("rejectedCandidate"), dict) else None
        candidate_name = str((candidate or {}).get("description") or "").strip()
        supplier = str(offer.get("supplierName") or offer.get("supplierId") or "Fornitore")
        avvisi.append({
            "id": f"{product_id}-rifiuto-{offer['supplierId']}",
            "code": "RIFIUTO_CON_CANDIDATO_FORTE",
            "severity": "warning",
            "blocking": False,
            "productId": product_id,
            "supplierId": offer.get("supplierId"),
            "candidateKey": (candidate or {}).get("candidateKey") or "",
            "candidate": candidate,
            "title": f"Possibile prodotto {supplier}",
            "message": (
                f"{supplier} propone «{candidate_name}». Indica se è lo stesso articolo."
                if candidate_name
                else f"{supplier} potrebbe avere questo prodotto, ma il nome della riga proposta non è disponibile."
            ),
            "technicalMessage": (
                f"Prodotto gestionale: {name}. Il candidato migliore della selezione è stato "
                f"rifiutato dall'analisi automatica con somiglianza {punteggio:.2f} su 1."
            ),
        })
    return avvisi


def senza_offerta_warnings(
    product_id: str,
    ordinabile: bool,
    colli_chiesti: int | None,
    offers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Un prodotto che nessun listino sa servire lo dice sulla sua scheda.

    Dal 16 agosto 2026 la quantita' del gestionale resta applicata anche qui:
    la riga mostra i colli che servono e nessun fornitore accanto: senza questa
    frase sembrerebbe una scelta ancora da fare, mentre non c'e' niente da
    scegliere. E' l'unico posto che dice, sulla riga, perche' quella quantita'
    non trovera' un ordine: alla compilazione finisce nell'elenco «Prodotti da
    reperire».
    """

    if ordinabile:
        return []
    fornitori = len(offers)
    dove = (
        f"Nessuno dei {fornitori} listini caricati ha una riga utilizzabile per questo prodotto."
        if fornitori
        else "Nessun listino caricato porta questo prodotto."
    )
    chiesti = (
        f" Il gestionale ne chiedeva {colli_chiesti} "
        + ("collo" if colli_chiesti == 1 else "colli")
        + ": va ordinato altrove, oppure serve un listino che lo porti."
        if colli_chiesti
        else ""
    )
    # Le stringhe nuove usano gli accenti; quelle vecchie del progetto restano
    # con l'apostrofo, ed e' una convenzione, non un difetto.
    return [{
        "id": f"{product_id}-senza-offerta",
        "code": "SENZA_OFFERTA_UTILIZZABILE",
        "severity": "warning",
        "blocking": False,
        "productId": product_id,
        # ⚠ Diceva «Nessuna offerta utilizzabile». In questo programma
        # «offerta» vuol dire *proposta di un fornitore*, ma per chi lavora in
        # un negozio «offerta» vuol dire **sconto**: chi legge capisce
        # «nessuno me lo fa in offerta» invece di «nessun fornitore ce l'ha».
        # Daniele l'ha letto cosi' il 20 agosto 2026 usando il programma. Qui
        # e nelle frasi che lo riassumono la parola non si usa piu' per
        # dire «proposta»: resta agli sconti veri, dove l'utente la aspetta.
        "title": "Nessun fornitore ce l’ha",
        "message": dove + chiesti,
    }]


def senza_offerta_summary(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Quanti sono, in cima alla pagina, e come si trovano.

    Gli avvisi di prodotto la pagina li raccoglie solo per i prodotti con una
    quantita' da ordinare, e lo stesso vale per il filtro «Nessuno ce l’ha», che
    nasconde chi sta a zero. Dal 16 agosto 2026 i prodotti che il gestionale
    chiede la quantita' ce l'hanno, quindi in quel filtro si trovano davvero;
    gli altri — quelli che nessuno chiede — restano fuori da tutto, e questa
    riga e' il solo posto che li conta. I due numeri si dichiarano entrambi:
    promettere un filtro che non li mostra tutti sarebbe un avviso che mente.
    """

    senza = [
        product for product in products
        if any(
            (warning or {}).get("code") == "SENZA_OFFERTA_UTILIZZABILE"
            for warning in product.get("warnings") or []
        )
    ]
    if not senza:
        return []
    # Si guarda `quantity`, non `suggestedQuantity`: e' `quantity` che decide
    # se il filtro «Nessuno ce l’ha» mostra la riga (nasconde chi sta a zero), e
    # questa frase promette proprio quel filtro. Oggi i due numeri coincidono;
    # se un domani divergessero, la promessa resterebbe vera lo stesso.
    chiesti = [
        product for product in senza
        if (product.get("quantity") or 0) > 0
    ]
    # ⚠ Diceva «I 80 che il gestionale chiede», e con un prodotto solo «I 1»:
    # l'articolo non regge davanti a un numero qualunque. Qui il numero sta
    # dopo il verbo, e la frase resta giusta da uno a mille.
    uno = len(chiesti) == 1
    coda = (
        f" Di questi il gestionale ne chiede {len(chiesti)}: "
        f"{'lo trovi' if uno else 'li trovi'} con il filtro «Nessuno ce l’ha», "
        f"con la {'sua' if uno else 'loro'} quantità. "
        f"{'Va' if uno else 'Vanno'} reperit{'o' if uno else 'i'} altrove, e alla compilazione "
        f"{'finisce' if uno else 'finiscono'} nell'elenco «Prodotti da reperire»."
        if chiesti
        else " Nessuno di questi ha una quantità da ordinare, quindi nel filtro "
        "«Nessuno ce l’ha» non compaiono."
    )
    return [{
        "code": "PRODOTTI_SENZA_OFFERTA",
        "severity": "warning",
        "blocking": False,
        "title": "Prodotti che nessun fornitore ha",
        "message": (
            f"{len(senza)} prodotti dell'elenco non li ha nessuno dei fornitori: nei listini "
            "caricati non c'è una riga utilizzabile per loro." + coda
        ),
        "count": len(senza),
    }]


# ⚠ L'avviso diceva «3 anomalie in LARICE; i valori restano visibili
# nell'audit», e Daniele leggendolo il 20 agosto 2026 ha scritto: «quindi? Che
# vuol dire?». Aveva ragione due volte. «Anomalia» non e' una parola del suo
# mestiere, e «restano visibili nell'audit» non dice dove andare a guardare —
# ma soprattutto la frase taceva l'unica cosa che conta davanti a un ordine:
# **se quel prezzo puo' essere sbagliato**.
#
# Queste tre voci portano la conseguenza, non il nome del difetto. La chiave e'
# l'inizio del testo che scrive chi legge il listino: un testo nuovo non rompe
# niente, finisce nel ramo generico qui sotto e si aggiunge qui il giorno in
# cui vale la pena spiegarlo.
SPIEGAZIONI_ANOMALIE: tuple[tuple[str, str, str, bool], ...] = (
    (
        "Codice sconto testuale inatteso",
        "nella colonna dello sconto una sigla che il programma non conosce",
        "il prezzo è stato preso senza sconto, quindi può risultare più alto del vero",
        True,
    ),
    (
        "Scadenza scritta male",
        "la data di scadenza scritta in un modo che non si riesce a leggere",
        "il prezzo non cambia — è la scadenza che va letta sul listino del fornitore",
        False,
    ),
    (
        "Scadenza fuori dal credibile",
        "una data di scadenza troppo lontana per essere vera",
        "il prezzo non cambia — è la scadenza che va letta sul listino del fornitore",
        False,
    ),
)


def _righe_citate(righe: list[Any]) -> str:
    """Le righe del file del fornitore, poche e per esteso, poi il conto.

    Chi controlla apre il listino e cerca la riga: sei numeri si copiano a
    mano, sessanta sono un muro e il resto della frase non si legge piu'.
    """

    numeri = [str(riga) for riga in righe if riga not in (None, "")]
    if not numeri:
        return ""
    if len(numeri) == 1:
        return f" È la riga {numeri[0]} del suo file."
    if len(numeri) <= 6:
        return f" Sono le righe {', '.join(numeri[:-1])} e {numeri[-1]} del suo file."
    return f" Le prime sono le righe {', '.join(numeri[:6])} del suo file, e ce ne sono altre {len(numeri) - 6}."


def anomalie_listino_summary(source_warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Che cosa e' successo, su quale listino, e se tocca un prezzo.

    Un avviso che dice «3 anomalie» chiede a chi legge di indovinare il resto.
    Questo dice il fornitore, quante righe, che cosa avevano di strano e la
    conseguenza — e il titolo risponde alla domanda che ci si fa davanti a un
    ordine: quel prezzo lo posso guardare o lo devo controllare?
    """

    if not source_warnings:
        return []

    # Si raggruppa per (fornitore, tipo di anomalia): e' la coppia che decide
    # sia la frase sia le righe da citare. `dict` normale, che conserva
    # l'ordine di inserimento: l'avviso esce nell'ordine in cui i listini sono
    # stati letti, non in uno alfabetico che nessuno riconosce.
    gruppi: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for voce in source_warnings:
        if not isinstance(voce, dict):
            continue
        testo = str(voce.get("warning") or "")
        indice = next(
            (n for n, (prefisso, *_) in enumerate(SPIEGAZIONI_ANOMALIE) if testo.startswith(prefisso)),
            len(SPIEGAZIONI_ANOMALIE),
        )
        fornitore = str(voce.get("source") or "fonte").upper()
        gruppi.setdefault((fornitore, indice), []).append(voce)

    frasi: list[str] = []
    righe_sul_prezzo = 0
    righe_senza_spiegazione = 0
    for (fornitore, indice), voci in gruppi.items():
        una = len(voci) == 1
        quante = "1 riga" if una else f"{len(voci)} righe"
        righe = _righe_citate([voce.get("source_row") for voce in voci])
        if indice < len(SPIEGAZIONI_ANOMALIE):
            _, che_cosa, conseguenza, sul_prezzo = SPIEGAZIONI_ANOMALIE[indice]
            frasi.append(
                f"{quante} del listino {fornitore} {'ha' if una else 'hanno'} {che_cosa}: "
                f"{conseguenza}.{righe}"
            )
            if sul_prezzo:
                righe_sul_prezzo += len(voci)
        else:
            # Un motivo che questo elenco non conosce: si riporta com'e'
            # scritto, senza inventargli una conseguenza che non sappiamo.
            motivi = sorted({str(voce.get("warning") or "").strip() for voce in voci if voce.get("warning")})
            dettaglio = f" Il motivo scritto in lettura: {'; '.join(motivi)}." if motivi else ""
            frasi.append(
                f"{quante} del listino {fornitore} "
                f"{'è stata letta' if una else 'sono state lette'} con una riserva.{dettaglio}{righe}"
            )
            righe_senza_spiegazione += len(voci)

    # Il titolo risponde alla domanda che costa: se anche un solo gruppo tocca
    # il prezzo, il titolo lo dice, e il dettaglio resta nel messaggio.
    fornitori = sorted({fornitore for fornitore, _ in gruppi})
    chi = f"{fornitori[0]}: " if len(fornitori) == 1 else ""
    coda_listino = "" if len(fornitori) == 1 else " di listino"
    totali = len(source_warnings)
    una_sola = totali == 1
    quante_tutte = "1 riga" if una_sola else f"{totali} righe"
    if righe_sul_prezzo:
        quante = "1 riga" if righe_sul_prezzo == 1 else f"{righe_sul_prezzo} righe"
        titolo = f"{chi}su {quante}{coda_listino} il prezzo può essere più alto del vero"
    elif righe_senza_spiegazione:
        letta = "letta" if una_sola else "lette"
        titolo = f"{chi}{quante_tutte}{coda_listino} {letta} con una riserva"
    else:
        titolo = f"{chi}{quante_tutte}{coda_listino} con un dato incerto, che non tocca il prezzo"
    # Senza il nome del fornitore davanti, il titolo comincia con la frase: la
    # maiuscola la mette qui, in un posto solo.
    if not chi:
        titolo = titolo[:1].upper() + titolo[1:]

    return [{
        "code": "ANOMALIE_LISTINO",
        "severity": "warning",
        "blocking": False,
        "title": titolo,
        "message": " ".join(frasi) + " Il resto dei listini non è toccato.",
        "count": len(source_warnings),
    }]


def suspect_reject_summary(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lo stesso conteggio, ma in cima alla pagina.

    Serve perche' la pagina raccoglie gli avvisi di prodotto **solo** per i
    prodotti con una quantita' da ordinare: senza questa riga, un prodotto
    scartato a torto e lasciato a zero non comparirebbe da nessuna parte. I due
    numeri si dichiarano entrambi — promettere che si trovano tutti con un
    filtro che non li mostra tutti sarebbe un avviso che mente."""
    con_avviso = [
        product
        for product in products
        if any(
            (warning or {}).get("code") == "RIFIUTO_CON_CANDIDATO_FORTE"
            for warning in product.get("warnings") or []
        )
    ]
    if not con_avviso:
        return []
    da_ordinare = sum(1 for product in con_avviso if (product.get("quantity") or 0) > 0)
    return [{
        "code": "RIFIUTI_CON_CANDIDATO_FORTE",
        "severity": "warning",
        "blocking": False,
        "title": "Prodotti scartati che somigliavano molto",
        "message": (
            f"Su {len(con_avviso)} prodotti l'analisi automatica non ha trovato corrispondenza "
            "presso un fornitore, ma la riga più simile del suo listino somigliava molto. "
            + (
                f"I {da_ordinare} con una quantità da ordinare li trovi con il filtro «Da confermare»."
                if da_ordinare
                else "Nessuno di questi ha una quantità da ordinare, quindi non compaiono in quel filtro."
            )
        ),
        "count": len(con_avviso),
    }]


CAUSE_DI_SCARTO = {
    "LISTINO_DISALLINEATO": "il listino non era più quello su cui il modello aveva deciso",
    "RIGA_NON_MOSTRATA": "il modello ha indicato una riga che non gli era stata mostrata",
    "EAN_NON_RISPETTATO": "il modello ha scelto una riga senza il codice a barre del prodotto",
    "DECISIONE_DI_UNA_ALTRA_RUN": "la proposta era stata presa su un elenco di candidati diverso da quello di oggi",
    "DECISIONE_SENZA_IMPRONTA": "la proposta non dice su quali candidati è stata presa",
}


def decisioni_ai_scartate(resolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """L'avviso in cima alla pagina per le decisioni AI che sono state buttate.

    Il conteggio esisteva solo su `stdout` di `merge_match_decisions.py`, e il
    programma finito gira da solo: nessuno legge quello schermo. In elenco una
    coppia degradata e' indistinguibile da una che l'AI non ha mai valutato —
    l'unica differenza e' il testo della motivazione, che si vede solo aprendo
    quel prodotto presso quel fornitore. Senza questo avviso il vincolo «cio'
    che viene scartato va contato in un riepilogo visibile» sarebbe soddisfatto
    a parole."""

    per_causa: dict[str, int] = {}
    for item in resolved:
        for match in (item.get("suppliers") or {}).values():
            causa = (match or {}).get("ai_decisione_scartata")
            if causa:
                per_causa[causa] = per_causa.get(causa, 0) + 1
    if not per_causa:
        return []
    totale = sum(per_causa.values())
    motivi = "; ".join(
        f"{quanti} perché {CAUSE_DI_SCARTO.get(causa, causa)}"
        for causa, quanti in sorted(per_causa.items(), key=lambda voce: -voce[1])
    )
    return [{
        "code": "DECISIONI_AI_SCARTATE",
        "severity": "warning",
        "blocking": False,
        "title": "Abbinamenti proposti dall'analisi e non usati",
        "message": (
            f"{totale} abbinamenti proposti dall'analisi automatica sono stati scartati "
            f"prima di entrare nel confronto: {motivi}. Quei prodotti sono da verificare a "
            "mano presso quel fornitore: non è che non esista una corrispondenza, è che "
            "questa non era affidabile."
        ),
        "count": totale,
        "byCause": per_causa,
    }]


def build_products(resolved: list[dict[str, Any]], suppliers: list[str]) -> list[dict[str, Any]]:
    products = []
    for item in resolved:
        master = item.get("gestionale") or {}
        last_price = money(master.get("last_unit_price"))
        offers = [offer_from_match(supplier, (item.get("suppliers") or {}).get(supplier, {}), last_price) for supplier in suppliers]
        selectable = [offer for offer in offers if offer.get("available")]
        selectable.sort(key=lambda offer: (offer.get("unitPriceNet") is None, offer.get("unitPriceNet") or 0, suppliers.index(offer["supplierId"])))
        selected_supplier = selectable[0]["supplierId"] if selectable else None
        selected_offer = next((offer for offer in offers if offer.get("supplierId") == selected_supplier), None)
        requires_confirmation = bool(selected_offer and selected_offer.get("requiresConfirmation"))
        # `source_colli_ignored` è il nome usato prima del passaggio all'ordine in colli:
        # accettarlo permette di rigenerare il confronto da artefatti di run precedenti.
        default_quantity = suggested_quantity(
            master.get("suggested_colli", master.get("source_colli_ignored"))
        )
        # I colli del gestionale si applicano SEMPRE, anche quando nessun
        # listino porta il prodotto: è la quantità normale del prodotto, non una
        # scelta, e azzerarla perdeva l'unica cosa che il gestionale aveva detto
        # su quella riga (decisione di Daniele del 16 agosto 2026).
        #
        # ⚠ Fino al 16 agosto 2026 qui si azzerava, e la ragione era vera: il 14
        # agosto tre prodotti nuovi del gestionale, che nessun listino porta,
        # erano nati con `quantity: 1` e nessun fornitore, il passo 2 si apriva
        # con un errore bloccante e l'autosalvataggio moriva a ogni battuta
        # perché `validate_snapshot` rifiutava una quantità senza offerta
        # utilizzabile. Quel rifiuto è stato allentato nello stesso lavoro:
        # quantità > 0 senza NESSUNA offerta utilizzabile da nessun fornitore è
        # oggi uno stato valido, e quei prodotti non restano orfani — alla
        # compilazione finiscono nell'elenco «Prodotti da reperire», che è chi
        # adesso li raccoglie e li porta fuori dal programma.
        #
        # Il prodotto **resta nel confronto** e dice che offerte non ne ha:
        # nasconderlo vorrebbe dire perdere una riga che il gestionale chiede.
        ordinabile = selected_supplier is not None
        quantita = default_quantity if default_quantity is not None else 0
        product_id = f"product:{master.get('source_row')}"
        products.append({
            "id": product_id,
            "kind": "PRODUCT",
            "itemType": "product",
            "sourceRow": master.get("source_row"),
            "ean": master.get("ean") or "",
            "description": master.get("description") or "",
            "name": master.get("description") or "",
            "lastUnitPrice": last_price,
            "quantity": quantita,
            "suggestedQuantity": default_quantity,
            # La sorgente segue i colli del gestionale, non piu' la presenza di
            # un fornitore: adesso che la quantita' si applica comunque, dire
            # «utente» su un numero che l'utente non ha scritto lo renderebbe
            # intoccabile al ricalcolo successivo (`_ripulisci_stato` rilegge
            # dall'elenco solo cio' che e' marcato «gestionale»).
            #
            # ⚠ `is not None`, non la verita' del numero: fino al 6 settembre
            # 2026 uno zero del gestionale nasceva «utente», con la spiegazione
            # «non c'e' niente da rileggere» — vera la settimana dello zero e
            # falsa quella dopo.  L'elenco chiedeva 0 colli, la pagina salvava
            # da sola, la settimana dopo ne chiedeva 4 e in pagina restava 0:
            # l'articolo fuori dall'ordine, senza un avviso.  Zero e' un numero
            # che il gestionale ha detto, e si rilegge da li' come gli altri;
            # «utente» resta solo la colonna vuota, dove non c'e' davvero
            # niente da rileggere.
            "quantitySource": "gestionale" if default_quantity is not None else "utente",
            "quantityLabel": "colli",
            "orderUnitLabel": "colli",
            "selectedSupplierId": selected_supplier,
            "confirmed": selected_supplier is not None and not requires_confirmation,
            "requiresConfirmation": requires_confirmation,
            "confirmationMessage": selected_offer.get("rationale") if selected_offer else "",
            "components": [],
            "warnings": (
                suspect_reject_warnings(product_id, master.get("description") or "", offers)
                + senza_offerta_warnings(product_id, ordinabile, default_quantity, offers)
            ),
            "notes": "",
            "offers": offers,
        })
    return products


def flatten_displays(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("display_offers"), list):
        return value["display_offers"]
    if isinstance(value.get("offers"), list):
        return value["offers"]
    flattened = []
    for supplier, offers in value.items():
        if isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    flattened.append({"supplier": supplier, **offer})
    return flattened


def component_fingerprint(offer: dict[str, Any]) -> str:
    explicit = offer.get("composition_fingerprint") or offer.get("fingerprint")
    if explicit:
        return str(explicit)
    components = offer.get("components") or []
    pairs = []
    for component in components:
        ean = str(component.get("ean") or component.get("gtin") or "").strip()
        quantity = number(component.get("quantity") or component.get("units") or component.get("units_per_display"))
        if ean and quantity is not None:
            pairs.append(f"{ean}:{quantity:g}")
    if pairs:
        return "composition:" + "|".join(sorted(pairs))
    description = offer.get("description") or offer.get("display_name") or offer.get("normalized_name")
    declared = offer.get("declared_units") or offer.get("declared_quantity") or offer.get("total_units")
    supplier = offer.get("supplier") or offer.get("supplier_id") or "unknown"
    return f"unmatched:{supplier}:{normalized_name(description)}:{declared}"


def display_offer(offer: dict[str, Any]) -> dict[str, Any]:
    supplier = str(offer.get("supplier") or offer.get("supplier_id") or "")
    pre_price = money(offer.get("list_price_per_display") or offer.get("unit_price_pre_discount") or offer.get("parent_price_pre_discount"))
    net_price = money(offer.get("net_price_per_display") or offer.get("unit_price_net") or offer.get("parent_price_net") or offer.get("parent_price_post_discount"))
    if net_price is None:
        discount = number(offer.get("discount_rate"))
        if pre_price is not None:
            net_price = money(pre_price * (1 - (discount or 0)))
    declared = number(offer.get("declared_units") or offer.get("declared_quantity") or offer.get("total_units") or offer.get("sum_component_units"))
    confidence = str(offer.get("confidence") or offer.get("detection_confidence") or "").upper()
    requires_confirmation = confidence not in {"ALTA", "HIGH", "CERTA"} and not bool(offer.get("auto_confirmed"))
    components = []
    for item in offer.get("components") or []:
        components.append({
            "ean": item.get("ean") or item.get("gtin") or "",
            "description": item.get("description") or item.get("normalized_product") or "",
            "quantity": number(item.get("quantity") or item.get("units") or item.get("units_per_display")),
            "unitPricePreDiscount": money(item.get("unit_price_pre_discount") or item.get("component_unit_price")),
            "sourceRow": item.get("source_row"),
        })
    # Un espositore e' un'unita' d'ordine come il collo: `quantityFactor` sono i
    # pezzi che contiene e `unitPriceNet` il prezzo del singolo pezzo, esattamente
    # come per un prodotto normale (`offer_record`, piu' su). Qui c'era `1` con
    # dentro `unitPriceNet` il prezzo dell'espositore INTERO, e la conseguenza non
    # era estetica: il piano d'ordine e lo storico scrivevano `delivered_pieces` =
    # numero di espositori e `unit_price_net` = prezzo dell'espositore, cioe' una
    # merce diversa da quella che la pagina mostrava (fuori di un fattore
    # `declaredUnits`), e «Sposta tutto su un altro fornitore» sceglieva la
    # migliore alternativa sul prezzo dell'espositore intero invece che sul prezzo
    # al pezzo — contro la regola dichiarata a server.py:1677-1682, e contro quello
    # che la pagina consiglia sullo stesso schermo (revisione del 14 agosto 2026).
    # Se i pezzi dichiarati non ci sono l'espositore vale un pezzo: e' il ripiego
    # prudente, perche' fa sembrare l'offerta piu' cara e non piu' conveniente.
    pieces = declared if declared and declared > 0 else 1
    price_per_piece = money(net_price / pieces) if net_price is not None else None
    # ⚠ «Identico» è il risultato di una verifica fatta, non l'assenza di una
    # smentita. `quantity_reconciled` e `price_reconciled` valgono `None` quando
    # i dati per riconciliare non c'erano, e `None is not False` è vero: la
    # pagina scriveva «Espositore identico» sopra un controllo che nessuno
    # aveva eseguito (revisione del 14 agosto 2026).
    composition_status = (
        "identical"
        if offer.get("quantity_reconciled") is True and offer.get("price_reconciled") is True
        else "comparable"
    )
    raw_evidence = offer.get("evidence") or []
    if isinstance(raw_evidence, list):
        evidence = "; ".join(
            str(item.get("detail") or item.get("code") or item) if isinstance(item, dict) else str(item)
            for item in raw_evidence
        )
    else:
        evidence = str(raw_evidence)
    return {
        "supplierId": supplier,
        "supplierName": supplier_name(supplier),
        # ⚠ `usable` vale anche per un espositore. Qui si guardava soltanto il
        # prezzo, quindi un espositore che il lettore aveva gia' dichiarato non
        # ordinabile — per esempio perche' i pezzi del collo padre non si
        # leggono — sarebbe rientrato dalla porta di servizio con un prezzo
        # ricavato da un fattore che nessuno conosce.
        "available": (
            net_price is not None and net_price > 0
            and bool(offer.get("usable", True))
        ),
        "status": "ESPOSITORE_RILEVATO",
        "method": "COMPOSIZIONE",
        "confidence": confidence or "DA_VERIFICARE",
        "requiresConfirmation": requires_confirmation,
        "confirmed": not requires_confirmation,
        "rationale": evidence,
        "description": offer.get("description") or offer.get("display_name") or "",
        "supplierCode": offer.get("supplier_code"),
        "sourceRow": offer.get("source_row") or offer.get("parent_source_row"),
        "unitPricePreDiscount": pre_price,
        "unitPriceNet": price_per_piece,
        "quantityFactor": pieces,
        "quantityFactorLabel": "espositore",
        "orderUnitPriceNet": net_price,
        "pricePerPieceNet": price_per_piece,
        "price": net_price,
        "unitsPerOrderUnit": pieces,
        "pricePerPiece": price_per_piece,
        "matchStatus": "Espositore identico" if composition_status == "identical" else "Composizione da verificare",
        "compositionStatus": composition_status,
        "declaredUnits": declared,
        "components": components,
        "quantityReconciled": offer.get("quantity_reconciled"),
        "priceReconciled": offer.get("price_reconciled"),
    }


def build_display_products(raw_offers: list[dict[str, Any]], suppliers: list[str]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for offer in raw_offers:
        groups[component_fingerprint(offer)].append(offer)
    products = []
    for fingerprint, offers_raw in sorted(groups.items(), key=lambda item: normalized_name(item[1][0].get("description") or item[1][0].get("display_name"))):
        offers = [display_offer(item) for item in offers_raw]
        first = offers_raw[0]
        description = first.get("description") or first.get("display_name") or "Espositore"
        components = offers[0].get("components") or []
        declared = offers[0].get("declaredUnits")
        selectable = [offer for offer in offers if offer.get("available")]
        selectable.sort(key=lambda offer: (offer.get("unitPriceNet") is None, offer.get("unitPriceNet") or 0))
        selected_supplier = selectable[0]["supplierId"] if selectable else None
        selected_offer = selectable[0] if selectable else None
        requires_confirmation = bool(selected_offer and selected_offer.get("requiresConfirmation"))
        products.append({
            "id": f"display:{fingerprint}",
            "kind": "DISPLAY",
            "itemType": "display",
            "ean": "",
            "description": description,
            "name": description,
            "lastUnitPrice": None,
            # ⚠ Zero, e la decisione del 16 agosto 2026 sulla quantita' non lo
            # tocca: un espositore non esiste nel gestionale — nasce dai listini
            # dei fornitori — quindi non ha colli da cui prendere una quantita'
            # e non c'e' niente da conservare. Quanti espositori ordinare lo
            # scrive l'utente, come prima.
            "quantity": 0,
            "quantityLabel": "espositori",
            "orderUnitLabel": "espositori",
            "selectedSupplierId": selected_supplier,
            "confirmed": bool(selected_supplier) and not requires_confirmation,
            "requiresConfirmation": requires_confirmation,
            "confirmationMessage": selected_offer.get("rationale") if selected_offer else "Conferma composizione e offerta dell'espositore.",
            "notes": "",
            "offers": offers,
            "components": components,
            "warnings": [],
            "display": {
                "fingerprint": fingerprint,
                "declaredUnits": declared,
                "componentCount": len(components),
                "components": components,
                "comparison": "IDENTICO" if len({offer["supplierId"] for offer in offers}) > 1 and not fingerprint.startswith("unmatched:") else "SINGOLA_OFFERTA",
            },
        })
    return products


def manifest_files(manifest: dict[str, Any], audit: dict[str, Any]) -> list[dict[str, Any]]:
    audit_by_name = {
        Path(item.get("path") or "").name.casefold(): item
        for item in audit.get("inputs") or []
    }
    result = []
    for item in manifest.get("files") or []:
        ai = item.get("ai_preflight") or {}
        file_name = item.get("file_name") or ""
        state = ai.get("state") or "AMBIGUO"
        status = "ready" if state == "SCHEMA_NOTO" else "warning" if state in {"SCHEMA_VARIATO", "NUOVO_FORNITORE"} else "error"
        role = ai.get("role")
        supplier_id = ai.get("supplier_id")
        audited = audit_by_name.get(file_name.casefold(), {})
        result.append({
            "id": item.get("profile_id"),
            "name": file_name,
            "sourcePath": item.get("path"),
            "role": ai.get("role"),
            "supplierId": ai.get("supplier_id"),
            "adapterId": ai.get("adapter_id"),
            "status": status,
            "confidence": ai.get("confidence"),
            "kind": "Gestionale" if role == "master" else "Listino" if role == "supplier" else "Ignorato",
            "supplier": "Gestionale" if role == "master" else supplier_name(supplier_id) if supplier_id else "Da riconoscere",
            "schemaState": state,
            "rows": audited.get("records") or 0,
            "message": ai.get("rationale") or "",
            "fieldMapping": ai.get("field_mapping") if isinstance(ai.get("field_mapping"), dict) else None,
            "sourceSha256": item.get("sha256") or None,
        })
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--displays", type=Path)
    parser.add_argument("--threshold", type=float, default=1000.0)
    parser.add_argument("--run-id")
    return parser.parse_args()


def main() -> int:
    # Stessa ragione di `merge_match_decisions.py`: su Windows un processo che
    # scrive su una pipe usa cp1252, e i messaggi portano le accentate. Chi
    # legge questa uscita deve trovare sempre UTF-8.
    for flusso in (sys.stdout, sys.stderr):
        try:
            flusso.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    args = parse_args()
    resolved = load_obbligatorio(args.resolved)
    manifest = load(args.manifest, {})
    audit = load(args.audit, {})
    raw_displays = flatten_displays(load_dichiarato(args.displays, "degli espositori"))
    suppliers = supplier_ids(resolved, raw_displays)
    products = build_products(resolved, suppliers)
    products.extend(build_display_products(raw_displays, suppliers))
    warnings = []
    warnings.extend(anomalie_listino_summary(audit.get("warnings") or []))

    warnings.extend(decisioni_ai_scartate(resolved))
    warnings.extend(suspect_reject_summary(products))
    warnings.extend(senza_offerta_summary(products))

    now = datetime.now(tz=timezone.utc).isoformat()
    review = {
        "schemaVersion": 1,
        "run": {
            "id": args.run_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            "status": "ready",
            "generatedAt": now,
            "createdAt": now,
            "label": "Confronto ordini corrente",
            "thresholdNet": args.threshold,
            "productCount": len(products),
            "standardProductCount": len(resolved),
            "displayCount": len(products) - len(resolved),
        },
        "files": manifest_files(manifest, audit),
        "suppliers": [{"id": item, "name": supplier_name(item), "thresholdNet": args.threshold, "minimumOrder": args.threshold} for item in suppliers],
        "products": products,
        "warnings": warnings,
        "auditSummary": audit,
        "ui": {"currentStep": 1, "currency": "EUR", "locale": "it-IT"},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "products": len(products), "displays": review["run"]["displayCount"], "suppliers": suppliers}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
