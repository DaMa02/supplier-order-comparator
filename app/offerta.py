"""Che cosa è un'offerta, e chi lo decide.

Un'offerta è la riga con cui un fornitore dice a che prezzo porta un articolo.
Tre domande su di lei tornano dappertutto nel programma — **di chi è**,
**si può ordinare** e **quanto costa al pezzo** — e fino al 20 agosto 2026 la
seconda aveva un'autorità qui e **due copie scritte a mano**: una in
`server._offerta_piu_conveniente`, una in `pipeline_jobs._ripulisci_stato`,
cioè proprio la funzione che decide quali quantità azzerare dopo un ricalcolo.

Il docstring di `_ripulisci_stato` dichiara che deve «seguire la stessa riga di
confine» di `validate_snapshot`: un accordo tenuto in piedi a mano fra copie.
Il giorno in cui si aggiunge una condizione — per esempio: un'offerta senza
`sourceRow` non è utilizzabile — chi la scrive nel servizio e la dimentica
nella catena ottiene un ricalcolo che NON azzera una quantità su un'offerta che
la compilazione poi rifiuta, e il sintomo si vede una settimana dopo e altrove.

Da qui questo modulo, che è la strada già battuta da `registro`, `consegna` e
`conferme`: **una** autorità, importata da chi la usa. Non importa niente da
`server` né da `pipeline_jobs` — sono loro a importare lui — così non può
nascere un anello.

⚠ I nomi restano quelli che avevano in `server.py`. Sono in inglese e il
progetto scrive in italiano: qui però non è stato scritto niente di nuovo, è
uno spostamento, e ribattezzare quattro funzioni nello stesso commit avrebbe
nascosto nel rumore l'unica cosa da leggere — che il corpo non è cambiato.
"""

from __future__ import annotations

from typing import Any


def numero(value: Any) -> float | None:
    """Un numero, o `None` se quel valore non lo è.

    Sta qui e non accanto a chi la usa perché `offer_pricing` non può leggere
    un prezzo senza di lei: `server.py` la riespone come `number`, quindi il
    parser dei numeri del servizio e quello dell'offerta sono lo stesso, non
    due che si somigliano.  `True` non è 1: un campo booleano finito dove ci
    vuole un prezzo è un dato sbagliato, non il numero uno.
    """

    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def offer_supplier_id(offer: Any) -> str:
    if not isinstance(offer, dict):
        return ""
    return str(offer.get("supplierId") or offer.get("supplier_id") or "")


def find_offer(product: Any, supplier_id: str) -> dict[str, Any] | None:
    """Offerta di un fornitore su un prodotto, la prima se il listino la ripete."""

    if not isinstance(product, dict) or not supplier_id:
        return None
    for offer in product.get("offers") or []:
        if offer_supplier_id(offer) == supplier_id:
            return offer
    return None


def offer_is_available(offer: Any) -> bool:
    """Un'offerta assente o dichiarata non disponibile non e' ordinabile.

    Il campo mancante vale disponibile: i listini piu' vecchi non lo scrivono.

    ⚠ È l'unica risposta a «si può ordinare», e va chiamata anche dove la riga
    sarebbe di sette caratteri: il costo di riscriverla non è la riga, è che il
    giorno in cui la regola cresce una delle copie resta indietro in silenzio.
    """

    return isinstance(offer, dict) and offer.get("available") is not False


def offer_pricing(offer: Any) -> dict[str, float] | None:
    """Fattore e prezzi netti di un'offerta, oppure None se non sono utilizzabili.

    factor sono i pezzi contenuti nell'unita' d'ordine; per un espositore sono i
    pezzi che l'espositore contiene, non 1 — l'unita' d'ordine e' l'espositore,
    ma la merce consegnata sono i pezzi. unitPriceNet e' il prezzo al pezzo,
    orderUnitPriceNet il prezzo del collo (o dell'espositore intero).

    E' lo stesso contratto per ogni tipo di articolo, ed e' quello che rende
    confrontabili le offerte: chi sceglie la «Migliore alternativa» ordina su
    unitPriceNet, e su un espositore quel numero dev'essere il prezzo del pezzo.
    Il produttore di questi campi e' `display_offer` in
    `scripts/build_review_data.py`, che fino al 14 agosto 2026 scriveva `1` e il
    prezzo dell'espositore intero.

    ⚠ Le tre catene con `or` qui sotto trattano uno `0` lecito come un campo
    assente. Oggi non morde perche' `catalog_search._offer` scarta gia' i
    prezzi `<= 0` prima di arrivare qui, cioe' la difesa e' a monte e non nel
    punto che legge; i nomi buoni e quelli tollerati stanno in
    `references/offerta.md`.
    """

    if not isinstance(offer, dict):
        return None
    factor = numero(offer.get("quantityFactor") or offer.get("unitsPerOrderUnit") or offer.get("units_per_order_unit")) or 1.0
    unit_price = numero(offer.get("unitPriceNet") or offer.get("pricePerPiece") or offer.get("price_per_piece"))
    order_price = numero(offer.get("orderUnitPriceNet") or offer.get("price") or offer.get("netPrice"))
    if order_price is None and unit_price is not None:
        order_price = unit_price * factor
    if unit_price is None and order_price is not None:
        unit_price = order_price / factor
    if order_price is None or unit_price is None or order_price < 0:
        return None
    return {"factor": factor, "unitPriceNet": unit_price, "orderUnitPriceNet": order_price}


# Lo stato di un'offerta che chi ordina ha rifiutato: «non e' lo stesso
# articolo».  Sta qui, con le altre risposte a «si puo' ordinare», e non in
# `server.py`: lo SCRIVE il servizio e lo LEGGE `da_reperire.motivo()`, e due
# copie della stessa stringa vogliono dire che il giorno in cui una cambia il
# motivo scritto accanto al prodotto torna a dire «non ce l'ha nessuno» su una
# riga che invece e' stata rifiutata da chi ordina — cioe' la sola cosa che
# quella colonna doveva saper distinguere.
#
# La forma e' quella degli altri stati del confronto (`EAN_ESATTO`,
# `SEMANTICO_PROPOSTO`, `SEMANTICO_CONFERMATO_UTENTE`): il negativo si legge
# accanto al suo positivo.
STATO_RIFIUTATO_UTENTE = "RIFIUTATO_UTENTE"
