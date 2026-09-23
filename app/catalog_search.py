"""Searchable, read-only catalogue assembled from the active supplier files."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from detect_displays import analyse_workbook
from inspect_sources import container_format
# I lettori dedicati non si nominano piu' qui: chi legge che cosa lo dice
# `prepare_manifest_sources.lettore_dedicato`, che e' la stessa funzione su cui
# decide la catena del ricalcolo. Resta l'integrazione degli espositori Larice,
# che anche nella catena sta accanto al lettore e non dentro.
from prepare_sources import integrate_larice_displays
from prepare_manifest_sources import (
    colonne_corrette,
    lettore_dedicato,
    mapped_standalone_displays,
    read_mapped_csv_supplier,
    read_mapped_xlsx_supplier,
)
# ⚠ Si importa la funzione e non il modulo: qui dentro `registro` e' gia' il
# nome di una variabile locale (`_signature`), e un modulo che sparisce sotto
# un'assegnazione e' un guasto che si scopre a runtime.
from registro import (
    adattatore_base,
    adattatori_effettivi,
    mappatura_spedita,
    nome_del_fornitore,
    percorso_imparato,
    voce_in_uso,
)


PROMOTION_WORDS = re.compile(r"\b(omaggi|omaggio|gratis|offerta|promoz|acquista|ogni\s+\d+)\b", re.IGNORECASE)
ADAPTERS_PATH = Path(__file__).resolve().parents[1] / "references" / "adapters.json"


_NOMI_DEL_CATALOGO: dict[str, str] = {}


def _supplier_name(supplier: str) -> str:
    """Come si chiama questo fornitore, secondo il registro.

    ⚠ Il nome si tiene in memoria finche' il registro non cambia. `_offer` lo
    chiede **per ogni riga di listino** — decine di migliaia per catalogo — e
    `registro.nome_del_fornitore` a ogni chiamata fa una `stat` sul file e una
    copia della mappa dei nomi: misurato, 40.000 chiamate costano qualche
    secondo buono (revisione del 14 agosto 2026). La firma del file la si
    guarda una volta per costruzione del catalogo, non una per riga.

    ⚠ Qui c'era una `SUPPLIER_NAMES` con i quattro fornitori scritti a mano,
    la terza copia della stessa tabella.  Un fornitore imparato non ci stava
    dentro e compariva nella ricerca prodotti come `NUOVO_FORNITORE_1`,
    underscore compreso, mentre il registro il suo nome ce l'aveva gia'.

    Il percorso si passa sempre: `nome_del_fornitore` senza percorso legge il
    registro predefinito, e in questo modulo il registro e' quello dichiarato
    da `ADAPTERS_PATH`.
    """

    memorizzato = _NOMI_DEL_CATALOGO.get(supplier)
    if memorizzato is not None:
        return memorizzato
    nome = nome_del_fornitore(supplier, ADAPTERS_PATH)
    _NOMI_DEL_CATALOGO[supplier] = nome
    return nome


def _adapter_mapping(adapter_id: str) -> dict[str, Any]:
    """La mappatura dichiarata nel registro degli adattatori.

    Serve quando la review non porta con se' una field_mapping: il listino
    Noce in Excel ha uno schema noto e registrato, e ripescarlo di li' e'
    meglio che tenerne una seconda copia dentro questo modulo, che prima o poi
    divergerebbe da quella vera.

    Le tre cose che possono andare storte — registro assente, registro
    illeggibile, adattatore non censito — hanno tre messaggi diversi: sono
    guasti dell'applicazione, non della review, e chi legge l'avviso deve
    sapere dove andare a guardare.

    Il registro e' quello **fuso**: spedito piu' imparato. Leggendo il solo
    `references/adapters.json` un adattatore reimparato con lo stesso `id`
    restava invisibile a questo modulo e visibile alla catena.
    """

    if not adapter_id:
        raise ValueError(
            "la review non dice con quale schema questo listino è stato letto: "
            "rifai il confronto dalla pagina Importa"
        )
    if not ADAPTERS_PATH.is_file():
        raise ValueError(f"manca il registro degli adattatori ({ADAPTERS_PATH.name})")
    # ⚠ I registri sono due, e non si trattano allo stesso modo. Lo **spedito**
    # sta sotto git e se non si legge e' un guasto dell'applicazione. L'
    # **imparato** lo scrive il programma, e un PC spento a meta' scrittura lo
    # tronca: `registro._leggi_registro` restituisce comunque le voci spedite
    # insieme al motivo, e tutto il resto del codice continua con quelle.
    # Trattare qualunque motivo come un rifiuto faceva sparire un fornitore dal
    # visualizzatore per un file che non e' quello nominato dal messaggio.
    voci, motivo = adattatori_effettivi(ADAPTERS_PATH)
    voce = next((item for item in voci if str(item.get("id") or "") == adapter_id), {})
    if not voce:
        if motivo:
            raise ValueError(f"{_quale_registro(motivo)} non è leggibile: {motivo}")
        raise ValueError(f"l'adattatore {adapter_id} non c'è nel registro degli adattatori")
    mapping = voce.get("field_mapping")
    if isinstance(mapping, dict) and mapping:
        return mapping
    raise ValueError(f"l'adattatore {adapter_id} non dichiara la mappatura delle colonne")


def _quale_registro(motivo: str) -> str:
    """Il nome del file che non si e' letto, per non mandare a guardare l'altro.

    «il registro degli adattatori (adapters.json) non e' leggibile: Registro
    degli adattatori imparati qui non interpretabile…» si contraddice da sola, e
    manda a guardare un file che sta sotto git e sta benissimo.
    """

    imparato = percorso_imparato(ADAPTERS_PATH)
    if "imparat" in motivo.casefold():
        return (
            f"il registro degli adattatori imparati su questo computer ({imparato.name})"
            "; cancellarlo fa ricominciare l'apprendimento da zero e non rompe nient'altro"
        )
    return f"il registro degli adattatori ({ADAPTERS_PATH.name})"


def _mappatura_spedita(adapter_id: str) -> dict[str, Any]:
    """La mappatura come sta nel registro **spedito**, o `{}` se non c'e'.

    ⚠ Spedito e non effettivo, ed e' la differenza che conta: l'imparato lo
    scrive il programma, e la mappatura confermata dalla pagina si costruisce da
    zero — non porta con se' `exclude_rows`. Confrontandosi con l'imparato, la
    regola che tiene fuori le righe alimentari di Noce si spegneva da sola
    la settimana dopo che qualcuno rifaceva la mappatura di quel fornitore:
    entrambe le parti del confronto avevano perso la regola, quindi il
    controllo non scattava piu' (misurato il 21 agosto 2026: 11 righe lette
    invece di 9, due delle quali FOOD). Qui si guarda quello che il programma ha
    **ricevuto**, che nessuno ha deciso di cambiare.

    ⚠ La lettura la fa `registro`, non un `json.load` di qui: il registro sono
    due file, e chi ne apre uno per conto suo prima o poi ne apre quello
    sbagliato (regola 4, ultima lettura rimasta fuori — 6 settembre 2026).
    """

    return mappatura_spedita(adapter_id, ADAPTERS_PATH)


def _mappatura_attiva(adapter_id: str, mapping: dict[str, Any]) -> dict[str, Any]:
    """Con quali colonne si legge, e la garanzia di non perdere le esclusioni.

    Una mappatura confermata nella review ha la precedenza su quella del
    registro — e' la risposta piu' recente, e l'ha data qualcuno che aveva il
    documento davanti — ma puo' arrivare senza le regole di esclusione che il
    registro dichiara. Nel listino Noce quella regola scarta migliaia di
    righe FOOD, che l'utente non tratta: qui ci si rifiuta di leggere, invece
    di farle entrare in silenzio.

    La regola non e' scritta qui e non nomina Noce: e' quella del registro,
    qualunque fornitore la dichiari.
    """

    attiva = mapping or _adapter_mapping(adapter_id)
    di_serie = _mappatura_spedita(adapter_id)
    if _esclude_gli_alimentari(di_serie) and not _esclude_gli_alimentari(attiva):
        raise ValueError(
            "la mappatura delle colonne non scarta le righe alimentari: senza quella "
            "regola entrerebbero nel confronto migliaia di articoli FOOD, che l'utente "
            "non tratta"
        )
    return attiva


def _motivo(exc: BaseException) -> str:
    """Il motivo di un guasto, mai vuoto.

    `str(MemoryError())` e' la stringa vuota, e un avviso che finisce con i due
    punti e il nulla non aiuta nessuno: in quel caso si mostra almeno il tipo.
    """

    return str(exc).strip() or type(exc).__name__


def _esclude_gli_alimentari(mapping: dict[str, Any]) -> bool:
    """Vero quando la mappatura scarta davvero le righe alimentari.

    L'utente non tratta il FOOD, e nel listino Noce sono 8.292 righe su
    17.143. La regola sta nella mappatura, quindi una mappatura confermata
    nella review — che ha la precedenza su quella di serie — puo' arrivare
    senza: il controllo e' qui perche' quelle righe non entrino comunque.
    """

    for rule in mapping.get("exclude_rows") or []:
        if str(rule.get("field") or "") != "category":
            continue
        if str(rule.get("equals") or "").strip().casefold() == "food":
            return True
        if rule.get("regex") and re.search(str(rule["regex"]), "FOOD", re.IGNORECASE):
            return True
    return False


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _search_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^A-Za-z0-9]+", " ", text).casefold().split())


def _catalog_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _promotion_text(record: dict[str, Any]) -> str:
    availability = str(record.get("availability") or "").strip()
    if availability.casefold().startswith("disponibile"):
        availability = availability[len("Disponibile") :].strip()
    description = str(record.get("description") or "").strip()
    if availability:
        return availability
    return description if PROMOTION_WORDS.search(description) else ""


def _offer(supplier: str, record: dict[str, Any]) -> dict[str, Any] | None:
    unit_price = _number(record.get("unit_price_net"))
    factor = _number(record.get("order_multiplier"))
    factor_label = "pezzi/collo"
    if factor is None:
        factor = _number(record.get("pieces_per_carton"))
    if factor is None or factor <= 0:
        factor = 1.0
        factor_label = "pezzo"
    # ⚠ `> 0`, non `>= 0`. E' la stessa difesa che `build_review_data` ha dal 14
    # agosto 2026, e qui mancava: un prezzo che si legge come 0,00 passa tutti i
    # filtri e poi **vince** il confronto, perche' l'ordinamento mette il piu'
    # basso davanti — il prodotto finisce assegnato al fornitore la cui cella non
    # si e' lasciata leggere, a totale zero, e il minimo d'ordine non scatta
    # perche' zero e' sotto qualunque soglia. Da qui passano l'aggiunta dal
    # catalogo, il rinfresco dei prezzi e l'abbinamento a mano: tre strade per lo
    # stesso guasto. Trovato da una prova del visualizzatore, il 17 agosto 2026.
    if unit_price is None or unit_price <= 0 or not record.get("usable", True):
        return None
    order_price = round(unit_price * factor, 6)
    discount_rate = _number(record.get("discount_rate")) or 0.0
    return {
        "supplierId": supplier,
        "supplierName": _supplier_name(supplier),
        "available": True,
        "status": "CATALOGO_FORNITORE",
        "method": "CATALOGO",
        "confidence": "ALTA",
        "requiresConfirmation": False,
        "confirmed": True,
        "rationale": "Articolo aggiunto dal catalogo del fornitore.",
        "description": str(record.get("description") or "").strip(),
        "ean": str(record.get("ean") or "").strip(),
        "supplierCode": record.get("supplier_code"),
        "sourceRow": record.get("source_row"),
        "unitPriceNet": round(unit_price, 6),
        "quantityFactor": round(factor, 6),
        "quantityFactorLabel": factor_label,
        "orderUnitPriceNet": order_price,
        "price": order_price,
        "unitsPerOrderUnit": round(factor, 6),
        "pricePerPiece": round(unit_price, 6),
        "matchStatus": "CATALOGO_FORNITORE",
        "details": " | ".join(
            str(item)
            for item in (record.get("packaging"), record.get("availability"), record.get("unit"))
            if item not in (None, "")
        ),
        "promotionText": _promotion_text(record),
        "discountRate": round(discount_rate, 6),
        "sourceReference": {"supplier": supplier, "row": record.get("source_row")},
        "alternatives": [],
    }


@dataclass(frozen=True)
class _Sorgente:
    """Un listino della review, con quello che serve per sceglierne il lettore.

    `adapter_id` e `state` sono gli stessi due campi su cui decide la catena
    (`prepare_manifest_sources.lettore_dedicato`): la review li porta gia' —
    li scrive `build_review_data.manifest_files` come `adapterId` e
    `schemaState` — e finche' non arrivavano fin qui il catalogo doveva
    indovinare dal nome del fornitore. Indovinava male.
    """

    path: Path
    mapping: dict[str, Any]
    adapter_id: str
    state: str


class SupplierCatalog:
    """Load supplier sources once and expose conservative search results."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.signature: tuple[tuple[str, str, int], ...] = ()
        self.entries: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.unique_offers: dict[str, dict[str, dict[str, Any]]] = {}
        # Le righe di ogni listino nell'ordine in cui stanno nel documento, per
        # il visualizzatore: la ricerca del catalogo tiene le sole ordinabili,
        # qui servono tutte.
        self.righe_per_fornitore: dict[str, list[dict[str, Any]]] = {}
        # I fornitori esclusi dall'ultima lettura, con il motivo in italiano:
        # il server li trasforma in avvisi della review, perche' un catalogo
        # che si assottiglia in silenzio e' peggio di uno che non si carica.
        self.load_errors: list[dict[str, str]] = []

    @staticmethod
    def _source_paths(review: dict[str, Any]) -> tuple[dict[str, "_Sorgente"], list[dict[str, str]]]:
        """I listini dichiarati dalla review, e quelli che non ci sono piu'.

        Il modo piu' comune in cui un fornitore esce dal confronto non e' un
        file rovinato: e' un file spostato o rinominato sul Desktop. Prima
        veniva scartato qui in silenzio, e il fornitore spariva dalla ricerca
        prodotti senza che niente lo dicesse.
        """

        paths: dict[str, _Sorgente] = {}
        mancanti: list[dict[str, str]] = []
        for item in review.get("files") or []:
            stato = str(item.get("schemaState") or "")
            # Gli stessi due stati che salta la catena
            # (`prepare_manifest_sources.main`): quei documenti nel confronto
            # non sono mai entrati, e la pagina Importa li mostra gia' con il
            # loro motivo. Qui sarebbero lo stesso avviso una seconda volta.
            if stato in {"FILE_NON_PERTINENTE", "AMBIGUO"}:
                continue
            # Il gestionale non e' un listino da sfogliare. Finora restava
            # fuori per un incidente — `supplierId` nullo, chiave vuota,
            # mappatura vuota, scartato in silenzio da `_read_sources` — e
            # quell'incidente era la stessa riga che faceva sparire i
            # fornitori veri. Adesso e' una regola, e si legge.
            if str(item.get("role") or "") == "master":
                continue
            supplier = str(item.get("supplierId") or "").casefold()
            raw_path = item.get("sourcePath") or item.get("originalPath") or item.get("path")
            mapping = item.get("fieldMapping") or item.get("field_mapping") or {}
            if not raw_path or not supplier:
                continue
            path = Path(str(raw_path)).resolve()
            if path.is_file():
                paths[supplier] = _Sorgente(
                    path=path,
                    mapping=mapping if isinstance(mapping, dict) else {},
                    adapter_id=str(item.get("adapterId") or item.get("adapter_id") or ""),
                    state=stato,
                )
            elif supplier:
                mancanti.append({
                    "supplier": supplier,
                    "supplierName": _supplier_name(supplier),
                    "message": (
                        f"Il listino {_supplier_name(supplier)} non è stato trovato dove la run "
                        f"lo cerca: {path}. Se è stato spostato o rinominato, va ricaricato."
                    ),
                })
        return paths, mancanti

    @staticmethod
    def _signature(
        paths: dict[str, "_Sorgente"],
        mancanti: list[dict[str, str]] | None = None,
    ) -> tuple[Any, ...]:
        """Che cosa fa ricaricare il catalogo.

        Oltre ai listini c'e' il registro degli adattatori: da li' arriva la
        mappatura del .xls Noce, quindi se il registro manca o viene
        rimesso a posto il catalogo deve accorgersene, altrimenti resterebbe
        attaccato all'errore fino al riavvio.  E ci sono i listini dichiarati
        ma non trovati: senza di loro nella firma, un avviso resterebbe in
        pagina anche dopo che quel fornitore e' uscito dalla run.

        Il registro sono **due** file, spedito e imparato: senza l'imparato
        nella firma, un adattatore reimparato non faceva ricostruire il
        catalogo e il visualizzatore restava attaccato allo schema vecchio fino
        al riavvio.  E ci sono `adapter_id` e `state`, che adesso decidono il
        lettore: se cambiano e la firma non se ne accorge, il catalogo resta
        con la lettura di prima.
        """

        registro = tuple(
            (str(documento), documento.stat().st_mtime_ns if documento.is_file() else -1)
            for documento in (ADAPTERS_PATH, percorso_imparato(ADAPTERS_PATH))
        )
        assenti = tuple(sorted(str(voce.get("supplier") or "") for voce in mancanti or []))
        return (registro, assenti, *sorted(
            (
                supplier,
                str(sorgente.path),
                sorgente.path.stat().st_mtime_ns,
                sorgente.adapter_id,
                sorgente.state,
                json.dumps(sorgente.mapping, ensure_ascii=False, sort_keys=True, default=str),
            )
            for supplier, sorgente in paths.items()
        ))

    @staticmethod
    def _read_supplier(supplier: str, sorgente: "_Sorgente") -> list[dict[str, Any]]:
        """Legge un listino con lo STESSO criterio della catena del ricalcolo.

        Il lettore lo sceglie la **decisione** — stato dello schema e
        adattatore — non il nome del fornitore, e a dirlo e'
        `prepare_manifest_sources.lettore_dedicato`, che e' l'unica autorita'
        su quale lettore apre quale documento.

        Fino al 21 agosto 2026 qui c'era `if supplier == "betulla": return
        read_betulla(path)`. Un listino promozionale dichiarato «BETULLA» nella
        mappatura guidata la catena lo leggeva col lettore generico — 621
        righe, stato `SCHEMA_VARIATO` — e il catalogo lo mandava a
        `read_betulla`, che alzava «Schema BETULLA non riconosciuto». Risultato:
        BETULLA restava nel confronto e spariva dal visualizzatore e dalla
        ricerca prodotti.
        """

        lettore = lettore_dedicato(sorgente.state, sorgente.adapter_id)
        if lettore is not None:
            # Le colonne corrette a mano entrano **dentro** il lettore
            # dedicato, come nella catena: mandare il documento al generico per
            # applicarle perderebbe gli espositori di Larice e le sue soglie
            # con omaggio.
            #
            # ⚠ L'adattatore si passa, non si finge `{}`: una mappatura
            # imparata indica le colonne per nome, e le posizioni per rileggerle
            # stanno nella sua impronta. Senza, la catena leggeva il listino e
            # il catalogo si fermava — cioe' il fornitore nel confronto e
            # assente dal visualizzatore, il difetto del 21 agosto 2026 rifatto
            # da un'altra parte (6 settembre 2026).
            voce = voce_in_uso(sorgente.adapter_id, adattatori_effettivi(ADAPTERS_PATH)[0])
            extra = colonne_corrette({"field_mapping": sorgente.mapping}, voce, sorgente.adapter_id)
            # `adattatore_base`: un `larice_v1__locale` — la mappatura imparata
            # qui sopra quella spedita — resta Larice, espositori compresi.
            if adattatore_base(sorgente.adapter_id) == "larice_v1":
                records, _warnings = lettore(sorgente.path, **extra)
                records, _displays, _summary = integrate_larice_displays(
                    records, analyse_workbook(sorgente.path),
                )
                return records
            risultato = lettore(sorgente.path, **extra)
            return risultato[0] if isinstance(risultato, tuple) else risultato

        attiva = _mappatura_attiva(sorgente.adapter_id, sorgente.mapping)
        # Il formato lo dicono i primi byte, non l'estensione: dall'agosto 2026
        # Noce manda un Excel 97-2003 al posto del CSV estratto dal sito, e
        # un .xls mandato al lettore CSV e' un UnicodeDecodeError su un file
        # valido. Il .xls lo legge `app/xls_reader.py`, dentro il lettore
        # generico: passare di qui non perde nessun lettore scritto a mano.
        if container_format(sorgente.path) == "csv":
            records, _warnings = read_mapped_csv_supplier(sorgente.path, supplier, attiva)
        else:
            records, _warnings = read_mapped_xlsx_supplier(sorgente.path, supplier, attiva)
        records, _displays, _audit = mapped_standalone_displays(records, supplier, attiva)
        return records

    @classmethod
    def _read_sources(
        cls,
        paths: dict[str, "_Sorgente"],
    ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
        """Legge i listini un fornitore alla volta, isolando chi fallisce.

        Un listino illeggibile toglie di mezzo il suo fornitore e nient'altro:
        prima di questa guardia bastava un .xls mandato al lettore CSV per far
        cadere l'intero catalogo, e all'utente arrivava un messaggio tecnico in
        inglese al posto degli altri tre fornitori.
        """

        sources: dict[str, list[dict[str, Any]]] = {}
        errors: list[dict[str, str]] = []
        # L'ordine e' quello di prima (betulla, larice, noce, poi gli altri):
        # a parita' esatta di prezzo al pezzo decide quale offerta viene
        # accodata per prima, e non e' una cosa da cambiare per distrazione.
        ordine = {"betulla": 0, "larice": 1, "noce": 2}
        # ⚠ Qui c'era `if supplier not in ordine and not mapping: continue`, e
        # scartava un fornitore senza aggiungere niente a `errors`: una
        # sparizione silenziosa esattamente nel punto in cui il modulo
        # prometteva che «il motivo si sa dire». Adesso chi entra in `paths` o
        # si legge o si spiega, e chi non deve entrarci resta fuori da
        # `_source_paths`.
        for supplier, sorgente in sorted(paths.items(), key=lambda voce: (ordine.get(voce[0], 3), voce[0])):
            try:
                sources[supplier] = cls._read_supplier(supplier, sorgente)
            except Exception as exc:  # noqa: BLE001 - un fornitore rotto non ne ferma altri
                errors.append({
                    "supplier": supplier,
                    "supplierName": _supplier_name(supplier),
                    "message": (
                        f"Il listino {_supplier_name(supplier)} «{sorgente.path.name}» "
                        f"non è stato letto: {_motivo(exc)}"
                    ),
                })
        return sources, errors

    def _ensure_loaded(self, review: dict[str, Any]) -> None:
        # I nomi dei fornitori si rileggono dal registro a ogni giro di questa
        # funzione, non a ogni riga di listino: `_offer` ne chiede uno per riga,
        # e sono decine di migliaia. Il registro entra comunque in `signature`,
        # quindi se cambia il catalogo si ricostruisce e i nomi con lui.
        _NOMI_DEL_CATALOGO.clear()
        paths, mancanti = self._source_paths(review)
        signature = self._signature(paths, mancanti)
        # Anche una lettura finita male e' una lettura fatta: senza ricordarlo,
        # un listino illeggibile verrebbe riaperto a ogni tasto digitato nella
        # ricerca prodotti.
        if signature == self.signature and (self.entries or self.load_errors):
            return
        sources, errors = self._read_sources(paths)
        usable = {
            supplier: [record for record in records if record.get("usable", True) and _offer(supplier, record)]
            for supplier, records in sources.items()
        }
        # Un listino che si apre ma non ha righe ordinabili toglie il fornitore
        # dal confronto esattamente come uno che non si apre: se solo il
        # secondo caso si segnala, il primo diventa una sparizione silenziosa.
        for supplier, records in usable.items():
            if records:
                continue
            errors.append({
                "supplier": supplier,
                "supplierName": _supplier_name(supplier),
                "message": (
                    f"Il listino {_supplier_name(supplier)} «{paths[supplier].path.name}» è stato "
                    "letto ma non contiene nessuna riga ordinabile."
                ),
            })
        self.load_errors = mancanti + errors
        # ⚠ Le righe **tutte**, ordinabili o no, tenute per fornitore: e' quello
        # che il visualizzatore mostra. Il catalogo di ricerca lavora sulle sole
        # ordinabili — giusto, perche' propone merce da comprare — ma chi apre
        # un listino per capire perche' un prodotto non e' stato abbinato deve
        # poter vedere **anche** la riga che il programma ha scartato, con il
        # motivo. Non raddoppia la memoria: `usable` tiene riferimenti agli
        # stessi dizionari, qui si aggiungono solo le righe scartate.
        self.righe_per_fornitore = {
            supplier: list(records) for supplier, records in sources.items()
        }
        counts = {
            supplier: Counter(str(record.get("ean") or "") for record in records if record.get("ean"))
            for supplier, records in usable.items()
        }
        unique_by_ean: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        ambiguous: list[tuple[str, dict[str, Any]]] = []
        for supplier, records in usable.items():
            for record in records:
                ean = str(record.get("ean") or "").strip()
                if ean and counts[supplier][ean] == 1:
                    unique_by_ean[ean][supplier] = record
                else:
                    ambiguous.append((supplier, record))

        entries: list[dict[str, Any]] = []
        for ean, records in unique_by_ean.items():
            preferred_supplier = next((item for item in ("betulla", "larice", "noce", "cipresso") if item in records), next(iter(records)))
            preferred = records[preferred_supplier]
            offers = [candidate for supplier, record in records.items() if (candidate := _offer(supplier, record))]
            entries.append(self._entry(f"ean:{ean}", preferred, offers, requires_confirmation=False))
        for supplier, record in ambiguous:
            offer = _offer(supplier, record)
            if offer:
                key = f"row:{supplier}:{record.get('source_row')}:{record.get('ean') or ''}"
                entries.append(self._entry(key, record, [offer], requires_confirmation=bool(record.get("ean"))))

        entries.sort(key=lambda item: (_search_text(item["name"]), item["catalogId"]))
        self.entries = entries
        self.by_id = {item["catalogId"]: item for item in entries}
        self.unique_offers = {
            supplier: {
                ean: candidate
                for ean, records in unique_by_ean.items()
                if (record := records.get(supplier)) is not None
                and (candidate := _offer(supplier, record)) is not None
            }
            for supplier in sources
        }
        self.signature = signature

    def enrich_review(self, review: dict[str, Any]) -> dict[str, Any]:
        """Refresh exact supplier offers and attach current promotion text."""

        with self.lock:
            self._ensure_loaded(review)
            for product in review.get("products") or []:
                if str(product.get("itemType") or product.get("kind") or "").casefold() == "display":
                    continue
                ean = str(product.get("ean") or "").strip()
                if not ean:
                    continue
                offers = product.setdefault("offers", [])
                positions = {
                    str(item.get("supplierId") or item.get("supplier_id") or ""): index
                    for index, item in enumerate(offers)
                }
                for supplier, by_ean in self.unique_offers.items():
                    current = by_ean.get(ean)
                    if not current:
                        continue
                    refreshed = deepcopy(current)
                    refreshed.update({
                        "status": "EAN_ESATTO",
                        "method": "EAN",
                        "confidence": "CERTA",
                        "requiresConfirmation": False,
                        "confirmed": True,
                        "rationale": "Codice esatto presente in una sola riga ordinabile del listino aggiornato.",
                        "matchStatus": "EAN_ESATTO",
                    })
                    if supplier in positions:
                        old = offers[positions[supplier]]
                        if old.get("lastPriceDifference") is not None:
                            refreshed["lastPriceDifference"] = old.get("lastPriceDifference")
                        if old.get("lastPriceDifferencePct") is not None:
                            refreshed["lastPriceDifferencePct"] = old.get("lastPriceDifferencePct")
                        offers[positions[supplier]] = refreshed
                    else:
                        offers.append(refreshed)
            return review

    @staticmethod
    def _entry(
        key: str,
        preferred: dict[str, Any],
        offers: list[dict[str, Any]],
        *,
        requires_confirmation: bool,
    ) -> dict[str, Any]:
        catalog_id = _catalog_id(key)
        available = sorted(offers, key=lambda item: (item.get("unitPriceNet") or float("inf"), item["supplierId"]))
        selected = available[0]["supplierId"] if available else None
        name = str(preferred.get("description") or "Articolo senza descrizione").strip()
        ean = str(preferred.get("ean") or "").strip()
        return {
            "catalogId": catalog_id,
            "id": f"manual:{catalog_id}",
            "kind": "PRODUCT",
            "itemType": "product",
            "sourceRow": None,
            "ean": ean,
            "description": name,
            "name": name,
            "lastUnitPrice": None,
            "quantity": 0,
            "quantityLabel": "colli",
            "orderUnitLabel": "colli",
            "selectedSupplierId": selected,
            "confirmed": bool(selected) and not requires_confirmation,
            "requiresConfirmation": requires_confirmation,
            "confirmationMessage": "Codice ripetuto nel catalogo: controllare la variante." if requires_confirmation else "",
            "components": [],
            "warnings": ["Codice ripetuto: controllare la variante."] if requires_confirmation else [],
            "notes": "Aggiunto manualmente",
            "addedManually": True,
            "offers": available,
            "_search": _search_text(" ".join([name, ean, *(str(item.get("supplierCode") or "") for item in offers)])),
        }

    def search(self, review: dict[str, Any], query: str, limit: int = 20) -> list[dict[str, Any]]:
        normalized = _search_text(query)
        if len(normalized) < 2:
            return []
        with self.lock:
            self._ensure_loaded(review)
            existing_eans = {str(item.get("ean") or "") for item in review.get("products") or [] if item.get("ean")}
            tokens = normalized.split()
            results = [item for item in self.entries if all(token in item["_search"] for token in tokens)]
            results.sort(
                key=lambda item: (
                    0 if str(item.get("ean") or "") == query.strip() else 1,
                    0 if item["_search"].startswith(normalized) else 1,
                    min((offer.get("unitPriceNet") or float("inf")) for offer in item["offers"]),
                    item["name"],
                )
            )
            visible = []
            for item in results[: max(1, min(int(limit), 50))]:
                clean = {key: deepcopy(value) for key, value in item.items() if key != "_search"}
                clean["alreadyPresent"] = bool(clean.get("ean") and clean["ean"] in existing_eans)
                visible.append(clean)
            return visible

    # ------------------------------------------------ il listino come si legge

    @staticmethod
    def _riga_di_listino(supplier: str, record: dict[str, Any]) -> dict[str, Any]:
        """Una riga di listino con le colonne su cui poi si ordina.

        Sono le stesse che decidono un abbinamento — codice, descrizione, pezzi
        per collo, prezzo — e non le colonne del foglio: qui si guarda **quello
        che il programma ha letto**, che è l'informazione che serve quando un
        prodotto non è stato abbinato e non si capisce perché. Se una colonna è
        letta storta, qui si vede storta.
        """

        offerta = _offer(supplier, record)
        return {
            "sourceRow": record.get("source_row"),
            "ean": str(record.get("ean") or "").strip(),
            "supplierCode": str(record.get("supplier_code") or "").strip(),
            "description": str(record.get("description") or "").strip(),
            "piecesPerCarton": _number(record.get("pieces_per_carton")),
            "orderMultiplier": _number(record.get("order_multiplier")),
            "unitPriceNet": _number(record.get("unit_price_net")),
            # `ordinabile` è la stessa domanda che il confronto si fa: una riga
            # che si vede ma non si può ordinare deve dirlo, e dire perché.
            "ordinabile": offerta is not None,
            "motivo": str(record.get("unusable_reason") or record.get("row_type") or ""),
            "orderUnitPriceNet": (offerta or {}).get("orderUnitPriceNet"),
        }

    def fornitori_sfogliabili(self, review: dict[str, Any]) -> list[dict[str, Any]]:
        """Quali listini si possono aprire, con quante righe ciascuno."""

        with self.lock:
            self._ensure_loaded(review)
            return [
                {
                    "id": supplier,
                    "name": _supplier_name(supplier),
                    "righe": len(records),
                    "ordinabili": sum(1 for record in records if _offer(supplier, record)),
                }
                for supplier, records in sorted(self.righe_per_fornitore.items())
            ]

    def sfoglia(
        self,
        review: dict[str, Any],
        supplier: str,
        *,
        query: str = "",
        da: int = 0,
        quante: int = 50,
        riga: Any = None,
    ) -> dict[str, Any]:
        """Una pagina del listino di un fornitore, come il programma l'ha letto.

        `riga` è il numero di riga su cui mettere il fuoco — quella già abbinata
        al prodotto da cui si è aperto il visualizzatore. Quando c'è, `da` viene
        ignorato e la pagina è quella che la contiene: aprire il listino di
        ottomila righe all'inizio, quando si sa già dove guardare, sarebbe far
        cercare a mano una cosa che il programma sa.

        Le righe scartate si contano sempre, anche quando la pagina non ne
        mostra nessuna: un listino di 8.881 righe che ne mostra 8.849 senza
        dirlo sarebbe un visualizzatore che nasconde.
        """

        chiave = str(supplier or "").strip().casefold()
        with self.lock:
            self._ensure_loaded(review)
            if chiave not in self.righe_per_fornitore:
                raise ValueError(f"Nessun listino caricato per «{supplier}»")
            tutte = self.righe_per_fornitore[chiave]
            normalizzata = _search_text(query)
            if normalizzata:
                parole = normalizzata.split()
                trovate = [
                    record for record in tutte
                    if all(
                        parola in _search_text(
                            f"{record.get('description') or ''} {record.get('ean') or ''} "
                            f"{record.get('supplier_code') or ''}"
                        )
                        for parola in parole
                    )
                ]
            else:
                trovate = list(tutte)
            quante = max(1, min(int(quante or 50), 200))
            posizione = None
            if riga is not None:
                cercata = str(riga)
                posizione = next(
                    (indice for indice, record in enumerate(trovate) if str(record.get("source_row")) == cercata),
                    None,
                )
            da = (posizione // quante) * quante if posizione is not None else max(0, int(da or 0))
            if da >= len(trovate):
                da = max(0, (len(trovate) - 1) // quante * quante) if trovate else 0
            pagina = trovate[da : da + quante]
            return {
                "supplier": chiave,
                "supplierName": _supplier_name(chiave),
                "righe": [self._riga_di_listino(chiave, record) for record in pagina],
                "da": da,
                "quante": quante,
                "trovate": len(trovate),
                "totale": len(tutte),
                "scartate": sum(1 for record in tutte if not _offer(chiave, record)),
                # Dove sta la riga su cui si voleva il fuoco: `None` quando non
                # è stata chiesta, oppure quando la ricerca l'ha esclusa — e in
                # quel caso la pagina deve dirlo invece di far cercare a vuoto.
                "rigaCercata": None if riga is None else (
                    str(riga) if posizione is not None else None
                ),
            }

    def offerta_dalla_riga(
        self, review: dict[str, Any], supplier: str, source_row: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """La riga scelta a mano e l'offerta che ne nasce.

        Restituisce `(record, offerta)`. Solleva se la riga non c'è o se non è
        ordinabile: abbinare un prodotto a una riga senza prezzo o senza pezzi
        per collo vorrebbe dire mettere in ordine una quantità che non si sa
        calcolare, ed è lo stesso rifiuto che il servizio oppone alle proposte
        dell'analisi automatica.
        """

        chiave = str(supplier or "").strip().casefold()
        cercata = str(source_row)
        with self.lock:
            self._ensure_loaded(review)
            if chiave not in self.righe_per_fornitore:
                raise ValueError(f"Nessun listino caricato per «{supplier}»")
            record = next(
                (
                    voce for voce in self.righe_per_fornitore[chiave]
                    if str(voce.get("source_row")) == cercata
                ),
                None,
            )
            if record is None:
                raise ValueError(
                    f"Nel listino {_supplier_name(chiave)} non c'è nessuna riga {source_row}: "
                    "il documento è cambiato, riapri il listino."
                )
            offerta = _offer(chiave, record)
            if offerta is None:
                motivo = str(record.get("unusable_reason") or record.get("row_type") or "")
                raise ValueError(
                    f"La riga {source_row} di {_supplier_name(chiave)} non è ordinabile"
                    + (f": {motivo}" if motivo else " (manca il prezzo o i pezzi per collo).")
                )
            return deepcopy(record), offerta

    def get(self, review: dict[str, Any], catalog_id: str) -> dict[str, Any]:
        with self.lock:
            self._ensure_loaded(review)
            item = self.by_id.get(str(catalog_id))
            if not item:
                raise ValueError("Prodotto non trovato nel catalogo")
            return {key: deepcopy(value) for key, value in item.items() if key != "_search"}

    def invalidate(self) -> None:
        with self.lock:
            self.signature = ()
            self.entries = []
            self.by_id = {}
            self.unique_offers = {}
            self.load_errors = []
