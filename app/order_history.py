#!/usr/bin/env python3
"""Storico degli ordini compilati, per ricordare la merce non ancora ricevuta.

Il negozio ordina una settimana, il fornitore non consegna, la merce non entra
nel gestionale: la settimana dopo lo stesso articolo verrebbe riordinato senza
accorgersene.  Questo modulo conserva una voce per fornitore ad ogni
compilazione riuscita — riuscita vuol dire con le copie dei listini sul disco,
il resto non e' un ordine — e permette di rispondere una volta sola, per
l'intero ordine.

Le risposte sono tre e riguardano sempre l'ordine intero, perche' le consegne
parziali non esistono: «ricevuta» chiude, «non ancora» rimanda la domanda alla
settimana dopo, «non arrivera' piu'» la chiude senza dire che la merce e'
arrivata.  Una domanda mai risposta scade dopo 60 giorni, e la scadenza si
dichiara (`expired_orders`) invece di spegnersi in silenzio.

Il modulo e' autonomo: non conosce ne' il server HTTP ne' ReviewStore, cosi'
resta verificabile da solo.  L'archivio vive fuori dalla cartella della run in
corso (app/data/history/orders.json) per sopravvivere al ricalcolo settimanale.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import scrittura_sicura  # noqa: E402


SCHEMA_VERSION = 1
STATUS_PENDING = "in_attesa"
STATUS_RECEIVED = "ricevuto"
STATUS_EXPIRED = "scaduto"
# «Non arrivera' piu'»: l'unico modo di chiudere una domanda senza mentire
# dicendo che la merce e' arrivata.  Senza questa risposta un ordine mai
# consegnato tornerebbe a chiedere di se' ogni settimana per sessanta giorni.
STATUS_CLOSED = "non_arrivera"
STATI_NOTI = {STATUS_PENDING, STATUS_RECEIVED, STATUS_EXPIRED, STATUS_CLOSED}
EXPIRY_DAYS = 60
# Dopo un «non ancora arrivata» la domanda torna la settimana dopo: il negozio
# ordina una volta a settimana, quindi e' quello il momento in cui ha senso
# richiedere.  Prima era il browser a nascondere la domanda per la sola
# giornata in corso, e la stessa domanda tornava il giorno dopo.
REASK_DAYS = 7
# Per quanti giorni la scadenza di un ordine resta scritta in pagina.  Un mese:
# chi lavora una volta a settimana la vede almeno quattro volte, e poi smette
# di occupare la pagina con una notizia vecchia.
EXPIRY_NOTICE_DAYS = 30


def empty_history() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "orders": []}


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def to_iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).astimezone(timezone.utc).isoformat()


def parse_moment(value: Any) -> datetime | None:
    """Legge un ISO8601; restituisce None quando la data non è interpretabile."""

    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_ean(value: Any) -> str:
    """Il codice a barre ripulito dagli spazi esterni: la chiave preferita."""

    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


# Un identificativo di prodotto vale come identita' solo quando lo compone il
# CONTENUTO e non la posizione nel foglio.  Dei due nomi degli espositori
# (`scripts/build_review_data.py`, build_display_products) solo
# `display:composition:<ean>:<pezzi>|...` nasce dalla composizione, quindi la
# settimana dopo indica lo stesso articolo; `display:unmatched:...` porta
# dentro il fornitore scelto e la descrizione, e cambia al cambio di
# fornitore — abbinarlo mancherebbe l'avviso senza dirlo (revisione
# avversariale R4).  `product:<riga>` mai: dipende dalla riga
# dell'esportazione settimanale, e product:198 di oggi non e' l'articolo che
# stava su product:198 la settimana scorsa.
PREFISSI_IDENTITA_STABILE = ("display:composition:",)


def stable_product_id(value: Any) -> str:
    """L'identificativo, se è di quelli confrontabili fra due settimane."""

    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    return text if text.startswith(PREFISSI_IDENTITA_STABILE) else ""


def match_key(ean: Any, product_id: Any = "") -> str:
    """La chiave con cui una riga d'ordine e un prodotto del confronto si abbinano.

    Prima l'EAN, che e' l'articolo e basta.  Quando l'EAN non c'e' — e' il caso
    degli espositori, che nel confronto non ne hanno mai uno — subentra
    l'identita' del prodotto, ma soltanto se e' di quelle stabili: senza questo
    ripiego un espositore ordinato restava invisibile la settimana dopo.
    """

    normalized = normalize_ean(ean)
    if normalized:
        return f"ean:{normalized}"
    stable = stable_product_id(product_id)
    return f"prodotto:{stable}" if stable else ""


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _quantity(value: Any) -> float:
    parsed = _number(value) or 0.0
    return int(parsed) if float(parsed).is_integer() else parsed


def _default_supplier_name(supplier_id: str) -> str:
    return str(supplier_id or "fornitore").upper()


def _normalized_entry(raw: dict[str, Any]) -> dict[str, Any]:
    entry = dict(raw)
    entry["orderId"] = str(entry.get("orderId") or "")
    entry["supplier"] = str(entry.get("supplier") or "")
    entry["supplierName"] = str(entry.get("supplierName") or _default_supplier_name(entry["supplier"]))
    entry["runId"] = str(entry.get("runId") or "")
    entry["createdAt"] = str(entry.get("createdAt") or "")
    status = str(entry.get("status") or "")
    entry["status"] = status if status in STATI_NOTI else STATUS_PENDING
    answered = entry.get("answeredAt")
    entry["answeredAt"] = str(answered) if answered else None
    # Quando la scadenza e' scattata: serve a dirlo in pagina.  Gli archivi
    # scritti prima non ce l'hanno e restano leggibili senza.
    expired = entry.get("expiredAt")
    entry["expiredAt"] = str(expired) if expired else None
    entry["totalNet"] = round(_number(entry.get("totalNet")) or 0.0, 2)
    lines = entry.get("lines")
    entry["lines"] = [dict(line) for line in lines if isinstance(line, dict)] if isinstance(lines, list) else []
    return entry


def load_history(path: Path) -> dict[str, Any]:
    """Legge l'archivio; se il file manca restituisce uno storico vuoto."""

    file_path = Path(path)
    if not file_path.exists():
        return empty_history()
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Lo storico degli ordini non è leggibile: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Lo storico degli ordini non contiene un oggetto")
    orders = raw.get("orders")
    if orders is not None and not isinstance(orders, list):
        raise ValueError("Lo storico degli ordini non contiene un elenco di ordini")
    history = dict(raw)
    history["schema_version"] = int(_number(raw.get("schema_version")) or SCHEMA_VERSION)
    history["orders"] = [_normalized_entry(item) for item in orders or [] if isinstance(item, dict)]
    return history


def save_history(path: Path, history: dict[str, Any]) -> None:
    """Scrittura atomica e arrivata sul disco: la fa `scrittura_sicura`.

    ⚠ Questa e' la memoria che dice **che cosa e' stato ordinato e non e'
    ancora arrivato**: trovarla vuota dopo una mancanza di corrente vuol dire
    che nessuno chiedera' piu' «e' arrivata?» per quella merce.
    """

    scrittura_sicura.scrivi_json(Path(path), history)


def expire_pending(
    history: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_days: int = EXPIRY_DAYS,
) -> bool:
    """Manda in scadenza le attese troppo vecchie; True se qualcosa è cambiato."""

    limit = (now or utc_now()) - timedelta(days=max_age_days)
    changed = False
    for entry in history.get("orders") or []:
        if entry.get("status") != STATUS_PENDING:
            continue
        created = parse_moment(entry.get("createdAt"))
        # I sessanta giorni contano dall'ULTIMA interazione, non dalla nascita:
        # con la domanda che torna ogni sette giorni, un ordine risposto «non
        # ancora arrivata» ieri sarebbe scaduto oggi come se l'utente non
        # avesse mai risposto — e la frase avrebbe detto «senza risposta» di
        # chi rispondeva con diligenza (revisione avversariale R4).
        answered = parse_moment(entry.get("answeredAt"))
        ultima = max(momento for momento in (created, answered) if momento is not None) \
            if (created or answered) else None
        # Una data assente o illeggibile va trattata come scaduta: senza data la
        # domanda non è collocabile nel tempo e resterebbe in attesa per sempre.
        if ultima is not None and ultima >= limit:
            continue
        entry["status"] = STATUS_EXPIRED
        # Il momento della scadenza si scrive: e' quello che permette di dirlo
        # in pagina invece di spegnere la domanda in silenzio.
        entry["expiredAt"] = to_iso(now)
        changed = True
    return changed


def expired_orders(
    history: dict[str, Any],
    *,
    now: datetime | None = None,
    window_days: int = EXPIRY_NOTICE_DAYS,
) -> list[dict[str, Any]]:
    """Gli ordini scaduti di recente, per dirlo in pagina.

    Un ordine che scade e' una domanda che il programma smette di fare: va
    detto, altrimenti la merce non arrivata sparisce senza che nessuno lo
    sappia.  Restano fuori le scadenze piu' vecchie del limite e quelle degli
    archivi scritti prima di `expiredAt`, che nessuno potrebbe piu' collocare
    nel tempo.
    """

    limit = (now or utc_now()) - timedelta(days=window_days)
    notices = []
    for entry in history.get("orders") or []:
        if not isinstance(entry, dict) or entry.get("status") != STATUS_EXPIRED:
            continue
        expired = parse_moment(entry.get("expiredAt"))
        if expired is None or expired < limit:
            continue
        notices.append({
            "orderId": str(entry.get("orderId") or ""),
            "supplier": str(entry.get("supplier") or ""),
            "supplierName": str(entry.get("supplierName") or _default_supplier_name(str(entry.get("supplier") or ""))),
            "createdAt": str(entry.get("createdAt") or ""),
            # Chi scrive la frase deve poter dire la verita': «senza risposta»
            # e «l'ultima risposta e' del …» sono due notizie diverse.
            "answeredAt": str(entry.get("answeredAt") or "") or None,
            "expiredAt": str(entry.get("expiredAt") or ""),
            "lineCount": len(entry.get("lines") or []),
            "totalNet": round(_number(entry.get("totalNet")) or 0.0, 2),
        })
    notices.sort(key=lambda item: item["expiredAt"])
    return notices


def read_history(path: Path, *, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Carica l'archivio applicando la scadenza a 60 giorni.

    Restituisce (storico, cambiato): quando è cambiato conviene risalvarlo.
    """

    history = load_history(path)
    changed = expire_pending(history, now=now)
    return history, changed


def order_lines_from_plan(plan_lines: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = []
    for line in plan_lines:
        if not isinstance(line, dict):
            continue
        lines.append({
            "ean": normalize_ean(line.get("ean")),
            "description": str(line.get("description") or ""),
            "quantity": _quantity(line.get("quantity")),
            # L'unità con cui si era ordinato: lo stesso codice può essere un
            # espositore una settimana e un prodotto normale quella dopo, e
            # l'avviso deve dire "espositori" se di espositori si trattava.
            "unit": str(line.get("desired_quantity_unit") or "colli"),
            "orderUnitPriceNet": round(_number(line.get("order_unit_price_net")) or 0.0, 6),
            # Identita' del prodotto nel confronto: per gli espositori, che un
            # EAN non ce l'hanno, e' l'unico modo di ritrovarli la settimana
            # dopo (vedi match_key).
            "productId": str(line.get("product_id") or ""),
        })
    return lines


def _run_key(run_id: Any, created_at: Any) -> str:
    """La settimana a cui appartiene un ordine.

    Senza runId (confronto senza identificativo) si ripiega sulla data, per non
    accorpare ordini di giornate diverse sotto la stessa chiave.
    """

    run = str(run_id or "").strip()
    return run or f"senza-run-{str(created_at or '')[:10]}"


def _entry_run_key(entry: dict[str, Any]) -> str:
    return _run_key(entry.get("runId"), entry.get("createdAt"))


def _is_replaceable(entry: dict[str, Any]) -> bool:
    """Una domanda ancora aperta: la RICONSEGNA dello stesso fornitore la sostituisce.

    Aperta vuol dire `in_attesa`, anche se l'utente ha gia' risposto «non
    ancora arrivata»: quella risposta parlava di una compilazione che una
    riconsegna ha superato, e tenerla in piedi accanto alla nuova produrrebbe
    due domande per la stessa settimana e colli sommati due volte (revisione
    avversariale R4).  «Ricevuta», «non arrivera' piu'» e «scaduto» sono
    storia chiusa: non si toccano e non si ripropongono.
    """

    return entry.get("status") == STATUS_PENDING


def record_plan(
    history: dict[str, Any],
    plan: dict[str, Any],
    *,
    now: datetime | None = None,
    supplier_name: Callable[[str], str] | None = None,
    order_key: str = "",
    delivered: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Registra una voce "in_attesa" per ogni fornitore consegnato dal piano.

    IDENTIFICATIVO: "<order_key>:<fornitore>", dove `order_key` è la cartella
    datata della compilazione.  Due compilazioni della stessa run sono due
    ordini diversi — l'utente puo' aver gia' mandato la prima — e la seconda
    non deve cancellarla scrivendoci sopra.

    SOSTITUZIONE: della stessa settimana si tiene solo l'ultima compilazione,
    ma soltanto fra le domande ancora SENZA RISPOSTA.  Una risposta gia' data
    resta dov'è: la sua domanda non torna e non si duplica.

    `delivered` sono i fornitori di cui la compilazione ha davvero creato la
    copia: un fornitore rimasto senza copia non diventa un ordine, e nemmeno
    cancella quello che c'era prima, perché una compilazione fallita non
    cambia niente di quello che è già stato mandato.
    """

    label = supplier_name or _default_supplier_name
    created_at = to_iso(now)
    run_id = str((plan or {}).get("run_id") or "").strip()
    run_key = _run_key(run_id, created_at)
    order_prefix = str(order_key or "").strip() or run_key

    grouped: dict[str, list[dict[str, Any]]] = {}
    totals: dict[str, float] = {}
    for line in (plan or {}).get("orders") or []:
        if not isinstance(line, dict):
            continue
        # Casefold da tutte e due le parti: `run_writer` consegna identificativi
        # casefoldati, e un fornitore con una maiuscola nel piano non si
        # incontrava mai con la sua copia — ordine prodotto, storico muto
        # (revisione avversariale R4).
        supplier = str(line.get("supplier") or "").strip().casefold()
        if not supplier:
            continue
        grouped.setdefault(supplier, []).append(line)
        total = _number(line.get("line_total_net"))
        if total is None:
            total = (_number(line.get("quantity")) or 0.0) * (_number(line.get("order_unit_price_net")) or 0.0)
        totals[supplier] = totals.get(supplier, 0.0) + total

    consegnati = ({str(item).strip().casefold() for item in delivered}
                  if delivered is not None else set(grouped))

    orders = history.setdefault("orders", [])
    history.setdefault("schema_version", SCHEMA_VERSION)
    existing = {str(item.get("orderId") or ""): item for item in orders if isinstance(item, dict)}

    recorded = []
    for supplier, plan_lines in grouped.items():
        if supplier not in consegnati:
            continue
        order_id = f"{order_prefix}:{supplier}"
        payload = {
            "orderId": order_id,
            "createdAt": created_at,
            "supplier": supplier,
            "supplierName": str(label(supplier)),
            "runId": run_id,
            "status": STATUS_PENDING,
            "answeredAt": None,
            "totalNet": round(totals.get(supplier, 0.0), 2),
            "lines": order_lines_from_plan(plan_lines),
        }
        entry = existing.get(order_id)
        if entry is None:
            orders.append(payload)
            existing[order_id] = payload
            recorded.append(payload)
            continue
        # Stessa compilazione registrata due volte: si aggiorna, non si duplica.
        payload["createdAt"] = str(entry.get("createdAt") or created_at)
        if entry.get("answeredAt") or entry.get("status") != STATUS_PENDING:
            # Una risposta già data alla STESSA compilazione non regredisce
            # perché la si sta ri-registrando: qui non c'è nessuna riconsegna,
            # è la stessa voce scritta due volte.
            payload["status"] = str(entry.get("status"))
            payload["answeredAt"] = entry.get("answeredAt")
        entry.update(payload)
        recorded.append(entry)

    # Della stessa settimana resta in piedi solo l'ultima compilazione, e la
    # sostituzione tocca SOLTANTO i fornitori riconsegnati adesso: la domanda
    # di prima parlava di una compilazione superata (anche se l'utente le
    # aveva risposto «non ancora arrivata»).  ⚠ Un fornitore ASSENTE dal piano
    # non si cancella piu': dopo D1 ogni voce dello storico e' un documento
    # consegnato davvero — cancellarla perche' una compilazione successiva non
    # lo conteneva significava perdere l'ordine LARICE gia' mandato e
    # riordinarne la merce (BLOCCANTE della revisione avversariale R4; la
    # vecchia regola nasceva quando la voce si scriveva al momento del piano,
    # cioe' quando era un'intenzione e non un documento).
    registrati = {str(item.get("orderId") or "") for item in recorded}
    superflue = [
        entry for entry in orders
        if isinstance(entry, dict)
        and _entry_run_key(entry) == run_key
        and str(entry.get("orderId") or "") not in registrati
        and _is_replaceable(entry)
        and str(entry.get("supplier") or "").strip().casefold() in consegnati
    ]
    for entry in superflue:
        orders.remove(entry)
    return recorded


def pending_entries(
    history: dict[str, Any],
    *,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Ordini ancora in attesa, esclusa la run attualmente aperta.

    Senza l'esclusione, subito dopo aver compilato l'utente si vedrebbe chiedere
    se ha ricevuto la merce ordinata trenta secondi prima, e ogni articolo appena
    ordinato comparirebbe segnalato come "già ordinato".
    """

    current = str(exclude_run_id or "").strip()
    entries = [
        entry for entry in history.get("orders") or []
        if isinstance(entry, dict)
        and entry.get("status") == STATUS_PENDING
        and not (current and str(entry.get("runId") or "") == current)
    ]
    entries.sort(key=lambda entry: str(entry.get("createdAt") or ""))
    return entries


def remove_compilation(history: dict[str, Any], order_key: str) -> list[dict[str, Any]]:
    """Elimina tutte le voci nate da una compilazione cancellata.

    La chiave delle voci nuove è ``<cartella>:<fornitore>``. Si confronta il
    prefisso completo, con i due punti finali, così cancellare ``..._2`` non
    tocca ``..._20``. Ricevuti, chiusi, scaduti e ancora in attesa vengono
    eliminati insieme: una compilazione di prova non deve lasciare alcun
    promemoria o traccia commerciale nel pannello delle consegne.
    """

    key = str(order_key or "").strip()
    if not key:
        return []
    orders = history.get("orders")
    if not isinstance(orders, list):
        history["orders"] = []
        return []
    prefix = f"{key}:"
    removed = [
        entry for entry in orders
        if isinstance(entry, dict) and str(entry.get("orderId") or "").startswith(prefix)
    ]
    if removed:
        history["orders"] = [entry for entry in orders if entry not in removed]
    return removed


def ask_again_at(entry: dict[str, Any]) -> str:
    """Quando la domanda torna dopo un «non ancora arrivata»; "" se è dovuta ora.

    La decide il servizio e non il browser, come ogni altra regola: il browser
    si limita a confrontarla con l'orologio. ⚠ Non sta sul disco: si ricalcola
    da `answeredAt` a ogni richiesta, quindi non è un valore che si possa
    correggere a mano dentro `orders.json`.
    """

    answered = parse_moment(entry.get("answeredAt"))
    if answered is None:
        return ""
    # ⚠ Chi comincia una comparazione nuova rimette in piedi la domanda subito,
    # e questo segno vince sui sette giorni: aprire la settimana è un segnale
    # più forte di un timer, ed è il momento in cui ci si chiede davvero se la
    # merce della settimana scorsa è arrivata (Daniele, 22 agosto 2026).
    # Si confronta con `answeredAt` e non con l'orologio: una risposta data
    # DOPO la riapertura rimette il rinvio, altrimenti la domanda tornerebbe
    # per sempre a ogni ricaricamento.
    riaperta = parse_moment(entry.get("reaskedAt"))
    if riaperta is not None and riaperta >= answered:
        return ""
    return to_iso(answered + timedelta(days=REASK_DAYS))


def riapri_le_domande(history: dict[str, Any]) -> int:
    """Toglie il rinvio a tutte le domande «è arrivata?» ancora in attesa.

    Non tocca `answeredAt`: quella è la memoria di **che cosa** è stato
    risposto e **quando**, ed è la stessa cosa che permette di rispondere a
    «che cosa avevo deciso prima». Il rinvio è una conseguenza di quella data,
    non un dato suo, quindi si annulla con un segno a parte.

    Restituisce quante domande sono tornate in piedi: serve a non dire
    «rimesse 4 domande» quando non ce n'era nessuna da rimettere.
    """

    adesso = to_iso()
    quante = 0
    for entry in pending_entries(history):
        if not entry.get("answeredAt"):
            # Mai risposta: la domanda è già dovuta, non c'è rinvio da togliere.
            continue
        entry["reaskedAt"] = adesso
        quante += 1
    return quante


def pending_summary(
    history: dict[str, Any],
    *,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Elenco compatto per la domanda in cima alla pagina 2."""

    return [
        {
            "orderId": str(entry.get("orderId") or ""),
            "supplier": str(entry.get("supplier") or ""),
            "supplierName": str(entry.get("supplierName") or _default_supplier_name(str(entry.get("supplier") or ""))),
            "createdAt": str(entry.get("createdAt") or ""),
            # Serve al programma per non riproporre la stessa domanda a ogni
            # ricaricamento della pagina dopo un "non ancora arrivata".
            "answeredAt": entry.get("answeredAt") or None,
            # ...e questo dice quando invece va riproposta: una settimana dopo,
            # perché la merce può arrivare nel frattempo e nessuno lo direbbe.
            "askAgainAt": ask_again_at(entry),
            "lineCount": len(entry.get("lines") or []),
            "totalNet": round(_number(entry.get("totalNet")) or 0.0, 2),
        }
        for entry in pending_entries(history, exclude_run_id=exclude_run_id)
    ]


def answer_order(
    history: dict[str, Any],
    order_id: str,
    received: bool,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Risposta unica per l'intero ordine: le consegne parziali non esistono.

    "Sì" chiude l'ordine; "No" lo lascia in attesa e segna la risposta, così la
    domanda tace per una settimana e poi torna: finché la merce non è arrivata
    la domanda ha ancora senso, e sparire sarebbe come dire che è arrivata.
    """

    target = str(order_id or "").strip()
    if not target:
        return None
    for entry in history.get("orders") or []:
        if not isinstance(entry, dict) or str(entry.get("orderId") or "") != target:
            continue
        entry["answeredAt"] = to_iso(now)
        if received:
            entry["status"] = STATUS_RECEIVED
        elif entry.get("status") == STATUS_EXPIRED:
            # Un «non ancora arrivata» su un ordine GIA' scaduto non lo
            # resuscita: tornerebbe in attesa, riscadrebbe alla lettura dopo
            # con un `expiredAt` di oggi, e l'avviso «ordini scaduti»
            # rinascerebbe ogni volta, per sempre (revisione avversariale R4).
            # La risposta si registra, lo stato resta quello che era.
            pass
        else:
            entry["status"] = STATUS_PENDING
        return entry
    return None


def close_order(
    history: dict[str, Any],
    order_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """«Non arriverà più»: chiude la domanda senza dire che la merce è arrivata.

    Serve perché un «non ancora arrivata» torna a chiedere ogni settimana: un
    ordine che il fornitore non consegnerà mai deve poter uscire di scena, e
    l'unica alternativa sarebbe segnarlo ricevuto, cioè scrivere il falso.
    """

    target = str(order_id or "").strip()
    if not target:
        return None
    for entry in history.get("orders") or []:
        if not isinstance(entry, dict) or str(entry.get("orderId") or "") != target:
            continue
        entry["answeredAt"] = to_iso(now)
        entry["status"] = STATUS_CLOSED
        return entry
    return None


def pending_by_identity(
    history: dict[str, Any],
    *,
    exclude_run_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Indice identità -> ordini in attesa, con i colli complessivi dell'articolo.

    L'identità è quella di `match_key`: l'EAN quando c'è, altrimenti
    l'identificativo stabile del prodotto (gli espositori).
    """

    index: dict[str, list[dict[str, Any]]] = {}
    for entry in pending_entries(history, exclude_run_id=exclude_run_id):
        quantities: dict[str, float] = {}
        units: dict[str, str] = {}
        for line in entry.get("lines") or []:
            if not isinstance(line, dict):
                continue
            key = match_key(line.get("ean"), line.get("productId"))
            if not key:
                # Né EAN né identità stabile: non abbina mai nulla, perché un
                # avviso sbagliato è peggio di un avviso mancante.
                continue
            quantities[key] = quantities.get(key, 0.0) + (_number(line.get("quantity")) or 0.0)
            units.setdefault(key, str(line.get("unit") or "colli"))
        for key, quantity in quantities.items():
            index.setdefault(key, []).append({
                "orderId": str(entry.get("orderId") or ""),
                "supplier": str(entry.get("supplier") or ""),
                "supplierName": str(entry.get("supplierName") or _default_supplier_name(str(entry.get("supplier") or ""))),
                "orderedAt": str(entry.get("createdAt") or ""),
                "quantity": _quantity(quantity),
                # Unità del momento in cui si era ordinato, non di questa settimana.
                "unit": units.get(key, "colli"),
            })
    return index


def attach_pending_orders(
    products: Iterable[dict[str, Any]],
    history: dict[str, Any],
    *,
    exclude_run_id: str | None = None,
) -> None:
    """Aggiunge "pendingOrders" ad ogni prodotto abbinando per identità.

    ⚠ Un EAN puo' essere condiviso da piu' prodotti del confronto (Noce ne
    ha 46 ripetuti su 99 righe): attribuire la quantita' ordinata a OGNI
    prodotto che porta quel codice regalava «20 colli gia' ordinati» anche
    all'articolo che nessuno aveva ordinato (revisione avversariale R4).  Lo
    storico non sa distinguere le righe che condividono il codice: quando
    succede, la voce lo dichiara (`sharedWith`) invece di inventare
    un'attribuzione, e la pagina deve dirlo insieme al numero.
    """

    elenco = [product for product in products or [] if isinstance(product, dict)]
    condivisi: dict[str, int] = {}
    for product in elenco:
        key = match_key(product.get("ean"), product.get("id"))
        if key:
            condivisi[key] = condivisi.get(key, 0) + 1
    index = pending_by_identity(history, exclude_run_id=exclude_run_id)
    for product in elenco:
        key = match_key(product.get("ean"), product.get("id"))
        matches = index.get(key) or [] if key else []
        quanti = condivisi.get(key, 1)
        product["pendingOrders"] = [
            {**item, **({"sharedWith": quanti} if quanti > 1 else {})}
            for item in matches
        ]
