"""Mappatura guidata dei documenti che il registro non riconosce.

Il servizio espone soltanto dati gia' presenti nel profilo della run fermata:
nessun percorso arriva dal browser e nessun file arbitrario puo' essere letto.
La conferma viene poi provata con lo stesso lettore usato dalla pipeline, cosi'
un'anteprima apparentemente buona non puo' trasformarsi in un listino vuoto al
ricalcolo successivo.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from openpyxl.utils import column_index_from_string, get_column_letter


APP_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = APP_DIR.parent / "scripts"
for cartella in (APP_DIR, SCRIPTS_DIR):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

from inspect_sources import container_format, file_hash  # noqa: E402
from prepare_manifest_sources import (  # noqa: E402
    read_mapped_csv_supplier,
    read_mapped_master,
    read_mapped_xlsx_supplier,
)
from validate_input_manifest import (  # noqa: E402
    errori_del_marcatore,
    incomplete_mapping,
    parole_della_pagina,
)


CAMPI_FORNITORE = {
    "ean",
    "supplier_code",
    "description",
    "pieces_per_carton",
    "unit_price_net",
    "vat",
    "availability",
    "unit",
    # ⚠ I tre qui sotto servono alle **condizioni commerciali**, ed erano il
    # buco misurato su QUERCIA il 17 agosto 2026: un fornitore nuovo si legge e si
    # compila dalla pagina, le sue offerte no.  `commercial_conditions` si
    # scriveva a mano nel registro — solo LARICE ce l'aveva — perche' non
    # c'era nessun modo di dichiarare da dove vengono i suoi testi.
    #
    # `promotion_text` e' la colonna in cui il fornitore scrive le sue offerte.
    # Non e' un campo del prodotto: nessun lettore la mette nei record, e sta
    # qui perche' e' l'unico modo di dire **dove** guardare.  Puo' coincidere
    # con una colonna gia' assegnata — su BETULLA l'offerta sta dentro la
    # descrizione, su QUERCIA nella colonna del codice articolo — ed e' l'unica
    # esente dalla regola «una colonna, un campo».
    "reward_description",
    "discount",
    "promotion_text",
}
# Le colonne che possono stare sopra a un'altra: guardano lo stesso testo con
# un'altra domanda, non lo leggono due volte come campi diversi.
CAMPI_SOVRAPPONIBILI = {"promotion_text"}
CAMPI_MASTER = {
    "ean",
    "description",
    "last_unit_price",
    "suggested_colli",
    "unit",
    "vat",
}

# Quota minima di righe confrontabili perche' un gestionale passi la prova.
#
# Un export vero non e' mai pulito al 100%: ci sono intestazioni ripetute a
# meta' elenco, totali di reparto, righe di separazione. Sono una manciata su
# centinaia, quindi le righe confrontabili restano fra il 90 e il 100%. Una
# colonna puntata sulla casella sbagliata invece non ne lascia nessuna, o
# quasi. Fra i due casi c'e' un abisso, e la soglia sta in mezzo ma dalla parte
# della tolleranza: il documento deve essere dieci volte piu' rotto di uno
# semplicemente sporco prima che la prova si fermi. Sotto una riga su dieci
# non e' un documento con qualche riga di troppo: e' la colonna sbagliata.
QUOTA_MINIMA_MASTER = 0.10

# Sotto quale somiglianza non si propone piu' nessun fornitore.
#
# `miglior_adattatore` restituisce **sempre** un candidato: se nessuno prende
# punti ripiega sul primo del ruolo giusto con copertura 0.0. La pagina lo
# metteva gia' selezionato nella tendina «Fornitore», e chi conferma sostituisce
# il listino vero di quel fornitore. E' successo in negozio il 21 agosto 2026.
#
# Misurato sugli undici documenti di `listini-storici/` piu' i tre della run:
#
#   proposta giusta     BETULLA 1.00 (due file) · CIPRESSO 1.00 (due file) ·
#                       NOCE .xls 1.00 · gestionale 1.00
#   proposta sbagliata  LARICE->noce 0.00 (quattro file) ·
#                       OFFERTE AGOSTO 4->betulla 0.20 (una intestazione su
#                       cinque, e quell'una e' «ORDINE») ·
#                       QUERCIA->gestionale 0.29 · GINEPRO->gestionale 0.29 ·
#                       ACERO->cipresso 0.33
#
# Fra 0.33 e 1.00 non c'e' nessun documento: la soglia sta in quell'abisso, e
# 0.60 sta dalla parte giusta. E' quasi il doppio della proposta sbagliata piu'
# alta, e lascia passare il caso per cui la proposta esiste — il fornitore che
# rinomina una colonna: su BETULLA, cinque obbligatorie, una rinominata fa 0.80 e
# due fanno 0.60, e si propone ancora; tre fanno 0.40 e non si propone piu'.
# Con due intestazioni su cinque non e' un riconoscimento, e' una coincidenza.
SOGLIA_DELLA_PROPOSTA = 0.60
# ...e mai su una intestazione sola, qualunque sia la percentuale: un adattatore
# con due obbligatorie farebbe 1.00 trovandone due, ma uno con una sola farebbe
# 1.00 con una parola. Oggi la firma piu' povera ne ha cinque (`betulla_v1`),
# quindi questa non morde: sta qui perche' il registro si scrive dalla pagina.
INTESTAZIONI_MINIME_DELLA_PROPOSTA = 2

# Ripiego per uno schema nuovo. Le intestazioni gia' note non stanno qui: si
# prendono da ``header_aliases`` del registro, che resta l'unica fonte per i
# fornitori conosciuti.
ALIAS_GENERICI: dict[str, tuple[str, ...]] = {
    "ean": ("ean", "cod ean", "codice ean", "codice a barre", "barcode", "gtin"),
    "supplier_code": ("cod art", "codice articolo", "codice", "sku", "product code"),
    "description": (
        "descrizione",
        "des articolo",
        "descr commerciale",
        "descrizione articolo",
        "prodotto",
        "product",
    ),
    "pieces_per_carton": (
        "pz ct",
        "pezzi per cartone",
        "pezzi x cartone",
        "pezzi collo",
        "qt",
        "packaging",
        "pack",
    ),
    "unit_price_net": ("cessione", "listino", "prezzo", "prezzo netto", "unit price", "price"),
    "last_unit_price": ("prezzo", "ultimo prezzo", "prezzo unitario"),
    "suggested_colli": ("colli", "cartoni", "quantita ordine", "quantita"),
    "vat": ("iva", "vat"),
    "availability": ("disponibilita", "disponibile", "availability"),
    "unit": ("um", "unita misura", "unit"),
    "order_quantity": ("ordine", "quantita ordine", "order quantity"),
}


def normalizza(valore: Any) -> str:
    testo = unicodedata.normalize("NFKD", str(valore or ""))
    testo = "".join(carattere for carattere in testo if not unicodedata.combining(carattere))
    return re.sub(r"[^a-z0-9]+", "", testo.casefold())


def slug_fornitore(nome: str) -> str:
    testo = unicodedata.normalize("NFKD", nome)
    testo = "".join(carattere for carattere in testo if not unicodedata.combining(carattere))
    return re.sub(r"[^a-z0-9]+", "_", testo.casefold()).strip("_")


def carica_adattatori(percorso: Path) -> list[dict[str, Any]]:
    """Il registro effettivo: quello spedito piu' quello imparato in negozio.

    ⚠ Il fornitore imparato la settimana scorsa deve comparire anche qui,
    altrimenti la mappatura guidata lo propone di nuovo come sconosciuto e
    l'utente rimappa a mano uno schema che il programma conosce gia'.

    Si solleva quando non si e' letto niente: chi mappa uno schema deve sapere
    che il registro e' rotto, invece di vedersi proporre un elenco vuoto come
    se il programma non conoscesse nessun fornitore. Un imparato rotto sopra
    uno spedito buono non ferma: si lavora con lo spedito, e il motivo lo
    dice `registro.motivo_registro_illeggibile`.
    """

    from registro import adattatori_effettivi  # noqa: PLC0415 - import tardivo come gli altri

    voci, motivo = adattatori_effettivi(percorso)
    if motivo is not None and not voci:
        raise ValueError("Il registro dei fornitori non si legge.")
    return voci


def nome_dichiarato(supplier_id: str, adattatori: list[dict[str, Any]]) -> str:
    """Come si chiama questo fornitore, secondo gli adattatori gia' in mano.

    ⚠ Qui c'era `supplier_id.upper()`: chi aveva dichiarato «Sapori & Co.» si
    vedeva rispondere `SAPORI_E_CO`, underscore compresi (revisione di
    regressione del 14 agosto 2026).  La regola — a parita' di `supplier_id`
    vince il nome piu' corto, perche' il piu' lungo descrive il documento e non
    il fornitore — e' quella di `scripts/registro.py`, e la si chiama invece di
    riscriverla: il registro e' gia' aperto e passa di qui come parametro.
    """

    from registro import nome_del_fornitore_fra  # noqa: PLC0415 - import tardivo come gli altri

    return nome_del_fornitore_fra(supplier_id, adattatori)


def fogli_del_profilo(profilo: dict[str, Any]) -> list[dict[str, Any]]:
    dettagli = profilo.get("details") or {}
    fogli = dettagli.get("sheets")
    if isinstance(fogli, list):
        return [foglio for foglio in fogli if isinstance(foglio, dict)]
    # Un CSV e' un unico foglio logico. Tenere la stessa forma semplifica sia
    # la pagina sia la validazione, senza inventargli un nome che non ha.
    return [{
        "name": "",
        "active_range": dettagli.get("active_range") or {},
        "header_candidates": dettagli.get("header_candidates") or [],
        "header_rows": dettagli.get("header_rows") or [],
        "section_breaks": dettagli.get("section_breaks") or [],
        "section_rows": dettagli.get("section_rows") or [],
        "samples": dettagli.get("samples") or {},
        "columns": dettagli.get("columns") or [],
    }]


# Quante righe l'anteprima puo' portare. Erano 36, cioe' esattamente quante ne
# mandava il profilo: le righe attorno ai separatori di sezione — le uniche che
# fanno vedere dove comincia davvero il listino — sarebbero entrate solo
# buttando fuori le ultime righe del documento. Il tetto resta perche' serve a
# non gonfiare la risposta, ma sta sopra al massimo che il profilo produce
# (20 di testa + 24 di sezione + 5 centrali + 12 finali).
RIGHE_DELL_ANTEPRIMA = 64


def righe_visibili(foglio: dict[str, Any]) -> list[dict[str, Any]]:
    per_numero: dict[int, dict[str, Any]] = {}
    gruppi: list[Iterable[Any]] = [
        foglio.get("header_rows") or [],
        foglio.get("header_candidates") or [],
        # Le righe attorno a un separatore di sezione: senza di loro chi mappa
        # QUERCIA scriveva 69 in «Prima riga dei prodotti» e continuava a vedere
        # le righe 3-20, cioe' il blocco promozionale.
        foglio.get("section_rows") or [],
    ]
    campioni = foglio.get("samples") or {}
    for nome in ("initial", "middle", "final"):
        gruppi.append(campioni.get(nome) or [])
    for gruppo in gruppi:
        for voce in gruppo:
            if not isinstance(voce, dict):
                continue
            try:
                numero = int(voce.get("row"))
            except (TypeError, ValueError):
                continue
            valori = voce.get("values")
            if isinstance(valori, list):
                per_numero[numero] = {"row": numero, "values": valori}
    # Le righe iniziali sono la parte davvero utile dell'anteprima. I campioni
    # centrali e finali restano disponibili, ma il limite impedisce a un
    # profilo anomalo di gonfiare la risposta senza misura.
    return [per_numero[numero] for numero in sorted(per_numero)[:RIGHE_DELL_ANTEPRIMA]]


def valori_riga(foglio: dict[str, Any], numero: int) -> list[Any]:
    for voce in righe_visibili(foglio):
        if voce["row"] == numero:
            return list(voce["values"])
    return []


def righe_candidate(foglio: dict[str, Any]) -> list[int]:
    numeri: list[int] = []
    for voce in [*(foglio.get("header_candidates") or []), *(foglio.get("header_rows") or [])]:
        if not isinstance(voce, dict):
            continue
        try:
            numero = int(voce.get("row"))
        except (TypeError, ValueError):
            continue
        if numero >= 1 and numero not in numeri:
            numeri.append(numero)
    return numeri or [1]


def compatibile_formato(adattatore: dict[str, Any], profilo: dict[str, Any]) -> bool:
    tipi = {str(voce).casefold() for voce in (adattatore.get("file_types") or [])}
    suffisso = str(profilo.get("declared_suffix") or "").casefold()
    return not tipi or suffisso in tipi


def punteggio_adattatore(
    profilo: dict[str, Any], adattatore: dict[str, Any]
) -> tuple[float, int, int, str] | None:
    firma = adattatore.get("header_signature") or {}
    richieste = {normalizza(voce) for voce in (firma.get("required") or []) if normalizza(voce)}
    if not richieste:
        return None
    migliore: tuple[float, int, int, str] | None = None
    for foglio in fogli_del_profilo(profilo):
        for riga in righe_candidate(foglio):
            osservate = {normalizza(voce) for voce in valori_riga(foglio, riga) if normalizza(voce)}
            trovate = len(richieste & osservate)
            copertura = trovate / len(richieste)
            # Il formato vale solo come spareggio. Il riconoscimento resta
            # fondato sul contenuto: un .xlsx rinominato non cambia fornitore.
            candidato = (copertura, trovate, int(compatibile_formato(adattatore, profilo)), str(foglio.get("name") or ""))
            if migliore is None or candidato > migliore:
                migliore = candidato[:3] + (f"{candidato[3]}\n{riga}",)
    return migliore


def miglior_adattatore(
    profilo: dict[str, Any], adattatori: list[dict[str, Any]], *, ruolo: str | None = None,
    supplier_id: str | None = None,
) -> tuple[dict[str, Any] | None, str, int, float]:
    candidati: list[tuple[float, int, int, dict[str, Any], str, int]] = []
    for adattatore in adattatori:
        tipo = "master" if adattatore.get("kind") == "master" else "supplier"
        if ruolo and tipo != ruolo:
            continue
        if supplier_id and str(adattatore.get("supplier_id") or "").casefold() != supplier_id.casefold():
            continue
        punteggio = punteggio_adattatore(profilo, adattatore)
        if punteggio is None:
            continue
        copertura, trovate, formato, posizione = punteggio
        foglio, riga = posizione.rsplit("\n", 1)
        candidati.append((copertura, trovate, formato, adattatore, foglio, int(riga)))
    if not candidati:
        ripiego = next((voce for voce in adattatori if (
            (not ruolo or ("master" if voce.get("kind") == "master" else "supplier") == ruolo)
            and (not supplier_id or str(voce.get("supplier_id") or "").casefold() == supplier_id.casefold())
        )), None)
        foglio = str((fogli_del_profilo(profilo)[0] or {}).get("name") or "")
        return ripiego, foglio, righe_candidate(fogli_del_profilo(profilo)[0])[0], 0.0
    candidati.sort(key=lambda voce: (voce[0], voce[1], voce[2], str(voce[3].get("id") or "")), reverse=True)
    copertura, _trovate, _formato, adattatore, foglio, riga = candidati[0]
    return adattatore, foglio, riga, copertura


def proposta_credibile(
    profilo: dict[str, Any], adattatore: dict[str, Any] | None, copertura: float
) -> bool:
    """Se questo candidato merita di comparire **gia' scelto** nella tendina.

    ⚠ Non si applica dentro `miglior_adattatore`, e non e' una svista: di li'
    passa anche `adattatore_per_scelta`, che cerca l'adattatore del fornitore
    che l'utente ha **appena scelto a mano**. Li' una copertura bassa e'
    normale — il documento non somiglia a niente, e' per questo che siamo nella
    mappatura guidata — e rifiutarla vorrebbe dire non poter piu' assegnare un
    listino nuovo a un fornitore che esiste gia'. La soglia riguarda solo
    quello che il programma **propone da solo**.
    """

    if adattatore is None or copertura < SOGLIA_DELLA_PROPOSTA:
        return False
    punteggio = punteggio_adattatore(profilo, adattatore)
    return punteggio is not None and punteggio[1] >= INTESTAZIONI_MINIME_DELLA_PROPOSTA


def alias_per(adattatore: dict[str, Any] | None, ruolo: str) -> dict[str, set[str]]:
    risultato = {campo: {normalizza(alias) for alias in aliases} for campo, aliases in ALIAS_GENERICI.items()}
    if isinstance(adattatore, dict):
        for campo, aliases in (adattatore.get("header_aliases") or {}).items():
            destinazione = str(campo)
            if destinazione in {"order_quantity_ignored", "order_quantity"}:
                destinazione = "order_quantity"
            if destinazione not in risultato:
                risultato[destinazione] = set()
            if isinstance(aliases, list):
                risultato[destinazione].update(normalizza(alias) for alias in aliases)
    ammessi = CAMPI_MASTER | {"order_quantity"} if ruolo == "master" else CAMPI_FORNITORE | {"order_quantity"}
    return {campo: aliases - {""} for campo, aliases in risultato.items() if campo in ammessi}


def suggerisci_colonne(
    foglio: dict[str, Any], riga: int, adattatore: dict[str, Any] | None, ruolo: str
) -> tuple[dict[str, int], int | None]:
    valori = valori_riga(foglio, riga)
    normali = [normalizza(valore) for valore in valori]
    risultato: dict[str, int] = {}
    for campo, aliases in alias_per(adattatore, ruolo).items():
        indici = [indice for indice, nome in enumerate(normali, start=1) if nome and nome in aliases]
        if len(indici) == 1:
            if campo == "order_quantity":
                continue
            risultato[campo] = indici[0]
    ordine = next((indice for indice, nome in enumerate(normali, start=1)
                   if nome and nome in alias_per(adattatore, ruolo).get("order_quantity", set())), None)
    if ordine is None and isinstance(adattatore, dict):
        dichiarato = str((adattatore.get("order_write") or {}).get("order_column") or "").strip().upper()
        if re.fullmatch(r"[A-Z]{1,3}", dichiarato):
            numero = 0
            for lettera in dichiarato:
                numero = numero * 26 + ord(lettera) - ord("A") + 1
            ordine = numero
    return risultato, ordine


def foglio_per_nome(profilo: dict[str, Any], nome: str) -> dict[str, Any]:
    fogli = fogli_del_profilo(profilo)
    if len(fogli) == 1 and str(fogli[0].get("name") or "") == str(nome or ""):
        return fogli[0]
    for foglio in fogli:
        if str(foglio.get("name") or "") == str(nome or ""):
            return foglio
    raise ValueError(f"Foglio non trovato nel profilo: {nome or 'primo foglio'}")


def colonna_massima(foglio: dict[str, Any]) -> int:
    intervallo = foglio.get("active_range") or {}
    dichiarata = int(intervallo.get("max_column") or 0)
    profilate = max((int(voce.get("index") or 0) for voce in (foglio.get("columns") or []) if isinstance(voce, dict)), default=0)
    visibili = max((len(voce.get("values") or []) for voce in righe_visibili(foglio)), default=0)
    return max(dichiarata, profilate, visibili, 1)


def serializza_foglio(foglio: dict[str, Any]) -> dict[str, Any]:
    attivo = foglio.get("active_range") or {}
    return {
        "name": str(foglio.get("name") or ""),
        "maxRow": int(attivo.get("max_row") or 0),
        "maxColumn": colonna_massima(foglio),
        "headerRows": righe_candidate(foglio),
        "rows": righe_visibili(foglio),
        # Le righe che separano una sezione dall'altra, con che cosa c'e'
        # scritto e da quale riga ripartono i dati: e' quello che la pagina
        # propone al posto di far indovinare un numero.
        "sectionBreaks": deepcopy(foglio.get("section_breaks") or []),
        "columns": deepcopy(foglio.get("columns") or []),
    }


def fornitori_disponibili(adattatori: list[dict[str, Any]]) -> list[dict[str, str]]:
    per_id: dict[str, str] = {}
    for voce in adattatori:
        identificativo = str(voce.get("supplier_id") or "").strip().casefold()
        if not identificativo:
            continue
        etichetta = str(voce.get("display_name") or identificativo).strip()
        # Due adattatori dello stesso fornitore (per esempio XLS e CSV) non
        # diventano due scelte uguali nella pagina.
        per_id.setdefault(identificativo, etichetta.split(" listino ", 1)[0])
    return [{"id": chiave, "name": per_id[chiave]} for chiave in sorted(per_id)]


# Che cosa e' cambiato in un documento che il registro conosce, detto con le
# parole di chi guarda il file.  ⚠ I nomi delle verifiche in pagina non ci
# vanno: «riga_intestazione» e «posizioni_intestazioni» sono nomi di codice.
COSA_E_CAMBIATO = {
    "foglio": "il nome del foglio",
    "riga_intestazione": "la riga delle intestazioni",
    "colonne_attese": "una colonna dichiarata non c'è più",
    "posizioni_intestazioni": "l'ordine delle colonne",
    "tipi_plausibili": "il tipo di dato di una colonna",
    "righe_dati": "le righe di prodotto",
}


def cambiamenti_del_documento(indizio: dict[str, Any]) -> list[str]:
    """Le verifiche non superate, in italiano e senza doppioni."""

    fuori: list[str] = []
    for verifica in (indizio.get("checks") or []):
        if not isinstance(verifica, dict) or verifica.get("ok") is not False:
            continue
        frase = COSA_E_CAMBIATO.get(str(verifica.get("name") or ""))
        if frase and frase not in fuori:
            fuori.append(frase)
    return fuori


def _adattatore_per_id(identificativo: str, adattatori: list[dict[str, Any]]) -> dict[str, Any]:
    for voce in adattatori:
        if str(voce.get("id") or "") == identificativo:
            return voce
    return {}


def ruoli_gia_occupati(
    profili: list[dict[str, Any]], richiesti: set[str], adattatori: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Chi, in questa run, ha gia' un documento che NON passa dalla mappatura.

    Serve a dire **prima** della conferma quello che oggi si scopre dopo: se il
    documento viene assegnato a un fornitore che ha gia' un listino,
    `PipelineJobManager._piu_recente_per_ruolo` ne tiene uno solo — quello con
    `modified_at` piu' recente — e l'altro esce dal confronto senza che nessuno
    l'abbia chiesto. Misurato il 21 agosto 2026 con i profili veri: assegnando
    un foglio di offerte a BETULLA, «LISTINO BETULLA VALIDO FINO AL 01-09-26.xlsx»
    resta fuori.

    La chiave e' la stessa di la': «master», oppure «supplier:<supplier_id>»
    preso dall'adattatore che il registro ha riconosciuto. Un documento che il
    registro non riconosce non occupa niente: e' fra quelli che stanno passando
    di qui.
    """

    per_id = {str(voce.get("id") or ""): voce for voce in adattatori}
    occupati: list[dict[str, str]] = []
    for profilo in profili:
        nome = str(profilo.get("file_name") or "")
        if nome.casefold() in richiesti:
            continue
        indizio = profilo.get("deterministic_hint") or {}
        if str(indizio.get("state") or "") != "SCHEMA_NOTO":
            continue
        adattatore = per_id.get(str(indizio.get("adapter_id") or ""))
        if adattatore is None:
            continue
        quando = str(profilo.get("modified_at") or "")
        if adattatore.get("kind") == "master":
            occupati.append({
                "role": "master", "supplierId": "",
                "supplierName": "l'elenco del gestionale",
                "fileName": nome, "modifiedAt": quando,
            })
            continue
        supplier_id = str(adattatore.get("supplier_id") or "").strip().casefold()
        if not supplier_id:
            continue
        occupati.append({
            "role": "supplier", "supplierId": supplier_id,
            "supplierName": nome_dichiarato(supplier_id, adattatori),
            "fileName": nome, "modifiedAt": quando,
        })
    # ⚠ Una voce per chiave, e dev'essere quella che vince davvero: la stessa
    # regola di `_piu_recente_per_ruolo`, cioe' il file modificato piu' di
    # recente. Con due listini dello stesso fornitore gia' caricati la pagina
    # nominava il primo in ordine di nome e prometteva che il documento appena
    # configurato entrava — mentre a restare fuori era proprio lui.
    per_chiave: dict[str, dict[str, str]] = {}
    for voce in occupati:
        chiave = voce["role"] if voce["role"] == "master" else f"supplier:{voce['supplierId']}"
        vincente = per_chiave.get(chiave)
        if vincente is None or str(voce["modifiedAt"]) >= str(vincente["modifiedAt"]):
            per_chiave[chiave] = voce
    return sorted(per_chiave.values(), key=lambda voce: str(voce.get("fileName") or "").casefold())


def prepara_pendenti(
    profili: list[dict[str, Any]], nomi: list[str], adattatori: list[dict[str, Any]], run_id: str,
    motivi: dict[str, Any] | None = None,
) -> dict[str, Any]:
    richiesti = {nome.casefold() for nome in nomi}
    per_motivo = {str(nome).casefold(): str(valore or "") for nome, valore in (motivi or {}).items()}
    documenti = []
    for profilo in profili:
        nome = str(profilo.get("file_name") or "")
        if nome.casefold() not in richiesti:
            continue
        ruolo_caricato = str(
            profilo.get("upload_role") or (profilo.get("ai_preflight") or {}).get("role") or ""
        ).casefold()
        if ruolo_caricato in {"master", "supplier"}:
            candidati = [
                voce for voce in adattatori
                if ("master" if voce.get("kind") == "master" else "supplier") == ruolo_caricato
            ]
        else:
            candidati = adattatori
        adattatore, nome_foglio, riga, copertura = miglior_adattatore(profilo, candidati)
        # Il foglio e la riga restano: sono la posizione in cui le poche
        # intestazioni riconosciute stanno **davvero**, ed e' una proposta
        # migliore della riga 1. Quello che cade e' l'**identita'** del
        # fornitore, che con una copertura cosi' bassa non e' un
        # riconoscimento. Con l'adattatore cade anche il suo `kind`, quindi il
        # documento arriva in pagina come «Listino fornitore» invece che come
        # «Elenco del gestionale»: misurato, QUERCIA e GINEPRO arrivavano proposti
        # gestionale con copertura 0.29.
        if not proposta_credibile(profilo, adattatore, copertura):
            adattatore = None
        ruolo = ruolo_caricato if ruolo_caricato in {"master", "supplier"} else (
            "master" if (adattatore or {}).get("kind") == "master" else "supplier"
        )
        foglio = foglio_per_nome(profilo, nome_foglio)
        colonne, ordine = suggerisci_colonne(foglio, riga, adattatore, ruolo)
        if ruolo == "supplier" and "description" not in colonne:
            # Uno schema non somigliante al gestionale e senza proposta viene
            # comunque presentato come listino: e' il caso piu' comune e
            # lascia all'utente una sola scelta da correggere, non due.
            ruolo = "supplier"
        indizio = profilo.get("deterministic_hint") or {}
        # ⚠ Due notizie diverse, e finora il payload ne portava una sola.
        # «SCONOSCIUTO» = il registro non lo conosce, c'e' un fornitore da
        # configurare.  «VARIATO» = lo conosce, ed e' il documento a essere
        # cambiato: le tendine arrivano gia' compilate con quello che il
        # registro dichiara, e quasi sempre basta guardarle.
        # ⚠ «NOTO» esiste per una strada sola: le colonne riviste a mano da un
        # listino che il programma riconosce benissimo. Li' non c'e' nessuna
        # fermata che detti il motivo, e senza questo ramo un documento
        # riconosciuto arrivava in pagina marcato «SCONOSCIUTO» — un dato falso
        # che oggi non produce nessuna frase, e che la produrrebbe il giorno in
        # cui qualcuno si fida di quel campo. Sulla mappatura guidata il motivo
        # arriva sempre dalla fermata, quindi questo ramo non la tocca.
        stato_documento = per_motivo.get(nome.casefold()) or (
            "VARIATO" if str(indizio.get("state") or "") == "SCHEMA_VARIATO"
            else "QUASI" if indizio.get("quasi_adapter_id")
            else "NOTO" if str(indizio.get("state") or "") == "SCHEMA_NOTO"
            else "SCONOSCIUTO"
        )
        gia_noto = (
            _adattatore_per_id(str(indizio.get("adapter_id") or ""), adattatori)
            if stato_documento in {"VARIATO", "RUOLO_SBAGLIATO", "NOTO"} else {}
        )
        # ⚠ «QUASI» = il registro sa di quale fornitore e' il documento e sa
        # anche che cosa gli manca per leggerlo. Non e' uno sconosciuto: dirgli
        # «configura un fornitore nuovo» e' quello che il 21 agosto 2026 ha
        # fatto scrivere un adattatore imparato sopra quello spedito di BETULLA,
        # per una cella svuotata in Excel.  I dati li produce `registro`, uno
        # solo: qui si portano in pagina, non si ricalcolano.
        quasi = {
            "supplierName": str(indizio.get("quasi_supplier_name") or ""),
            "missing": [
                {"header": str(voce.get("header") or ""), "column": str(voce.get("column") or "")}
                for voce in (indizio.get("quasi_missing") or [])
                if isinstance(voce, dict)
            ],
            "present": int(indizio.get("quasi_present") or 0),
        } if stato_documento == "QUASI" else {}
        documenti.append({
            "profileId": str(profilo.get("profile_id") or ""),
            "fileName": nome,
            "format": str(profilo.get("content_format") or (profilo.get("details") or {}).get("format") or ""),
            "sizeBytes": int(profilo.get("size_bytes") or 0),
            "modifiedAt": str(profilo.get("modified_at") or ""),
            "sheets": [serializza_foglio(voce) for voce in fogli_del_profilo(profilo)],
            "reason": {
                "state": stato_documento,
                "supplierName": str(gia_noto.get("display_name") or quasi.get("supplierName") or ""),
                "changed": cambiamenti_del_documento(indizio) if stato_documento == "VARIATO" else [],
                "missing": quasi.get("missing") or [],
                "present": quasi.get("present") or 0,
            },
            "suggestion": {
                "role": ruolo,
                "supplierId": str((adattatore or {}).get("supplier_id") or ""),
                "supplierName": str((adattatore or {}).get("display_name") or ""),
                "sheet": nome_foglio,
                "headerRow": riga,
                "dataStartRow": riga + 1,
                "columns": colonne,
                "orderColumn": ordine,
                "match": round(copertura, 3),
            },
        })
    mancanti = richiesti - {str(voce.get("fileName") or "").casefold() for voce in documenti}
    if mancanti:
        raise ValueError("I profili della run non contengono tutti i documenti da configurare.")
    return {
        "ok": True,
        "required": bool(documenti),
        "runId": run_id,
        "suppliers": fornitori_disponibili(adattatori),
        # Chi ha gia' un documento in questa run. La pagina lo usa per dire, al
        # momento della scelta, quale listino resterebbe fuori.
        "occupied": ruoli_gia_occupati(profili, richiesti, adattatori),
        "documents": documenti,
    }


def specifica_colonna(foglio: dict[str, Any], riga: int, indice: int) -> str | int:
    valori = valori_riga(foglio, riga)
    valore = valori[indice - 1] if indice <= len(valori) else None
    token = normalizza(valore)
    if token and sum(1 for voce in valori if normalizza(voce) == token) == 1:
        return str(valore).strip()
    return indice


def intero_positivo(valore: Any, etichetta: str, *, zero: bool = False) -> int:
    if isinstance(valore, bool):
        raise ValueError(f"{etichetta}: serve un numero intero.")
    try:
        numero = int(valore)
    except (TypeError, ValueError):
        raise ValueError(f"{etichetta}: serve un numero intero.") from None
    minimo = 0 if zero else 1
    if numero < minimo:
        raise ValueError(f"{etichetta}: il numero deve essere almeno {minimo}.")
    return numero


def marcatore_dei_dati(grezzo: Any, foglio: dict[str, Any], riga_dati: int, nome: str) -> dict[str, Any] | None:
    """La regola «i prodotti cominciano dopo la riga che dice X», se e' stata scelta.

    Serve perche' un numero di riga non sopravvive a una settimana: su QUERCIA le
    righe 7-67 sono un blocco promozionale — prezzi che sono valorizzazioni di
    omaggi, non prezzi d'acquisto — e il listino vero comincia alla 69, dopo
    l'unico `A68 = 'LISTINO'` del file. La settimana prossima quel blocco sara'
    di lunghezza diversa; il 69 congelato taglierebbe l'elenco nel punto
    sbagliato **senza dire niente**.

    Qui la regola non si crede sulla parola: la riga dichiarata dev'essere fra
    quelle che l'anteprima porta, e li' dentro deve esserci davvero quel testo,
    in quella colonna.  Una regola confermata alla cieca sarebbe peggio del
    numero che sostituisce.

    Contratto della pagina (`dataStartMarker`)::

        {"column": 1, "match": "equals", "text": "LISTINO", "offset": 1}

    `column` e' il numero 1-based della colonna (la lettera e' ammessa),
    `match` vale «equals» o «contains», `offset` quante righe piu' in basso
    cominciano i prodotti (1 se non c'e').
    """

    if grezzo in (None, "", {}):
        return None
    if not isinstance(grezzo, dict):
        raise ValueError(f"{nome}: la regola dell'inizio dei prodotti non è leggibile.")
    testo = " ".join(str(grezzo.get("text") or "").split())
    if not testo:
        raise ValueError(f"{nome}: scrivi che cosa c'è scritto nella riga che separa i prodotti.")
    confronto = str(grezzo.get("match") or "equals").strip().casefold()
    if confronto not in {"equals", "contains"}:
        raise ValueError(f"{nome}: la regola dell'inizio dei prodotti può essere «equals» o «contains».")
    scarto = intero_positivo(grezzo.get("offset", 1), f"{nome}, righe dopo il separatore", zero=True)
    grezza_colonna = grezzo.get("column")
    if isinstance(grezza_colonna, str) and re.fullmatch(r"[A-Za-z]{1,3}", grezza_colonna.strip()):
        indice = column_index_from_string(grezza_colonna.strip().upper())
    else:
        indice = intero_positivo(grezza_colonna, f"{nome}, colonna del separatore")
    if indice > colonna_massima(foglio):
        raise ValueError(f"{nome}: la colonna {get_column_letter(indice)} non contiene dati da leggere.")

    riga_marcatore = riga_dati - scarto
    if riga_marcatore < 1:
        raise ValueError(f"{nome}: la riga che separa i prodotti finirebbe sopra l'inizio del foglio.")
    valori = valori_riga(foglio, riga_marcatore)
    if not valori:
        raise ValueError(
            f"{nome}: la riga {riga_marcatore} non è fra quelle dell'anteprima, quindi non posso "
            "verificare che contenga il testo indicato. Scegli una riga che si veda nell'anteprima."
        )
    grezza_cella = valori[indice - 1] if indice <= len(valori) else ""
    letto = " ".join(str(grezza_cella if grezza_cella is not None else "").split())
    trovato = letto.casefold() == testo.casefold() if confronto == "equals" else testo.casefold() in letto.casefold()
    if not trovato:
        quale = "contiene" if confronto == "contains" else "è"
        raise ValueError(
            f"{nome}: nella cella {get_column_letter(indice)}{riga_marcatore} c'è "
            f"«{letto}», non una scritta che {quale} «{testo}». Controlla la riga che separa "
            "i prodotti dal resto."
        )
    marcatore = {"column": get_column_letter(indice), confronto: testo, "offset": scarto}
    problemi = errori_del_marcatore(marcatore)
    if problemi:
        # Non e' raggiungibile oggi — il marcatore lo costruisce la riga qui
        # sopra, in forma valida — ma se un giorno lo diventa non deve uscire
        # «data_start_marker.offset deve essere un numero intero da 0 in su».
        raise ValueError(f"{nome}: controlla {parole_della_pagina(problemi)}.")
    return marcatore


def adattatore_per_scelta(
    profilo: dict[str, Any], adattatori: list[dict[str, Any]], ruolo: str, supplier_id: str
) -> dict[str, Any] | None:
    adattatore, _foglio, _riga, _copertura = miglior_adattatore(
        profilo, adattatori, ruolo=ruolo, supplier_id=supplier_id or None
    )
    return adattatore


# Il campo della mappatura che porta ogni ruolo del lettore delle condizioni.
# I nomi a destra sono chiavi di `column_map`: e' cosi' che LARICE dichiara le
# sue («text» → «description»), ed e' l'unica forma che `promotion_bridge` sa
# risolvere, perche' e' la stessa che segue il lettore dei prezzi.
CAMPI_DELLE_CONDIZIONI = {
    "text": "promotion_text",
    "reward": "reward_description",
    "ean": "ean",
    "row_code": "discount",
}


def condizioni_commerciali(
    grezza: dict[str, Any], colonne: dict[str, str | int], foglio: dict[str, Any], nome: str
) -> dict[str, Any] | None:
    """Dove il fornitore scrive le sue offerte, dichiarato dalla mappatura.

    ⚠ Il buco che chiude, misurato su QUERCIA il 17 agosto 2026: **oggi un
    fornitore nuovo si legge e si compila, le sue offerte no.**
    `commercial_conditions` si scrive a mano dentro `references/adapters.json`
    e ce l'ha solo LARICE; la mappatura guidata non aveva un campo per
    dichiararla e `impara_adattatore` non la scriveva. QUERCIA ha **quindici**
    testi promozionali — sei in colonna A e nove in colonna Q — e nessuno di
    loro sarebbe mai diventato una regola applicabile.

    Qui non si indovina niente. Se la mappatura non dichiara la colonna dei
    testi non nasce nessuna dichiarazione, e le offerte di quel fornitore
    restano quello che erano: non lette. Se invece la dichiara, si scrive la
    forma e i campi, e chi legge e' lo stesso motore di LARICE.

    La forma sta nella mappatura e non si deduce: `riga` — una riga porta per
    intero la sua condizione — e' il predefinito perche' e' come scrivono
    quasi tutti, ma `blocchi` resta dichiarabile e **pretende le sue colonne**.
    Accettarla senza sarebbe scrivere nel registro una dichiarazione che poi
    ogni settimana produce «non so piu' dove il listino tiene …».
    """

    from promotion_bridge import LAYOUT_RIGA, colonne_richieste_dal_layout, nomi_dei_ruoli

    grezze = grezza.get("commercialConditions")
    if not isinstance(grezze, dict):
        return None
    if "promotion_text" not in colonne:
        # Dichiarare la forma senza dire dove sono i testi non e' una
        # dichiarazione: e' una casella spuntata a vuoto.
        if str(grezze.get("layout") or "").strip():
            raise ValueError(
                f"{nome}: per leggere le offerte di questo fornitore serve la colonna in cui le "
                "scrive. Indicala, oppure lascia le offerte fuori."
            )
        return None

    forma = str(grezze.get("layout") or LAYOUT_RIGA).strip().casefold()
    richieste = colonne_richieste_dal_layout(forma)
    if richieste is None:
        raise ValueError(
            f"{nome}: «{grezze.get('layout')}» non è un modo di scrivere le condizioni che "
            "io sappia leggere."
        )
    etichette = nomi_dei_ruoli()
    campi: dict[str, str] = {}
    mancanti: list[str] = []
    for ruolo, campo in CAMPI_DELLE_CONDIZIONI.items():
        if campo in colonne:
            campi[ruolo] = campo
        elif ruolo in richieste:
            mancanti.append(etichette.get(ruolo, ruolo))
    if mancanti:
        raise ValueError(
            f"{nome}: per leggere le offerte scritte a «{forma}» serve anche "
            + ", ".join(mancanti)
            + ". Indica quelle colonne, oppure scegli un altro modo."
        )
    return {
        "layout": forma,
        "sheet": str(foglio.get("name") or "FIRST"),
        # Dalla prima riga: le condizioni commerciali stanno spesso **sopra**
        # l'elenco dei prodotti — su QUERCIA nel blocco promozionale delle righe
        # 7-68 — e partire dalla prima riga dei dati le taglierebbe fuori tutte.
        "data_start_row": 1,
        "fields": campi,
    }


def valori_di_disponibilita(grezzi: Any, nome: str) -> list[str]:
    """Quali valori della colonna «Disponibilita» vogliono dire «disponibile».

    ⚠ Senza questa lista il lettore parte da `available = True` e la cella non
    la guarda nemmeno (`prepare_manifest_sources`, `available_values`): la
    colonna si poteva mappare, e le righe che dicevano NO restavano ordinabili
    e potevano vincere il confronto — a prezzo piu' basso, per il motivo per
    cui erano piu' basse (6 settembre 2026).

    L'elenco lo dichiara chi ha il listino davanti, e non si inventa qui: «SI»,
    «S», «disponibile», «X» sono tutti veri per qualcuno, e un elenco cablato
    sarebbe una convenzione di fornitore scritta nel codice, che questo
    progetto non vuole (regola 4). Se la colonna e' mappata e la lista e'
    vuota, la conferma si rifiuta: leggere quella colonna senza sapere che cosa
    significa e' peggio che non leggerla.

    Contratto della pagina (`availableValues`): un testo con i valori separati
    da virgola o a capo, per esempio «SI, S, disponibile».
    """

    valori: list[str] = []
    for pezzo in re.split(r"[,\r\n]", str(grezzi or "")):
        valore = pezzo.strip()
        if valore and valore not in valori:
            valori.append(valore)
    if not valori:
        raise ValueError(
            f"{nome}: indica quali valori della colonna «Disponibilità» significano "
            "disponibile (per esempio: SI), separati da virgola."
        )
    return valori


def decisione_da_mappatura(
    profilo: dict[str, Any], grezza: dict[str, Any], adattatori: list[dict[str, Any]]
) -> dict[str, Any]:
    nome = str(profilo.get("file_name") or "documento")
    ruolo = str(grezza.get("role") or "supplier")
    if ruolo not in {"supplier", "master"}:
        raise ValueError(f"{nome}: scegli se il documento è un listino o l'elenco del gestionale.")
    foglio = foglio_per_nome(profilo, str(grezza.get("sheet") or ""))
    max_colonna = colonna_massima(foglio)
    max_riga = int((foglio.get("active_range") or {}).get("max_row") or 0)
    riga_header = intero_positivo(grezza.get("headerRow"), f"{nome}, riga intestazioni")
    riga_dati = intero_positivo(grezza.get("dataStartRow"), f"{nome}, prima riga dati")
    if max_riga and riga_header > max_riga:
        raise ValueError(f"{nome}: la riga delle intestazioni è oltre la fine del foglio.")
    if riga_dati <= riga_header:
        raise ValueError(f"{nome}: la prima riga dati deve stare sotto le intestazioni.")
    if max_riga and riga_dati > max_riga:
        raise ValueError(f"{nome}: la prima riga dati è oltre la fine del foglio.")

    ammessi = CAMPI_MASTER if ruolo == "master" else CAMPI_FORNITORE
    colonne_grezze = grezza.get("columns")
    if not isinstance(colonne_grezze, dict):
        raise ValueError(f"{nome}: la scelta delle colonne non è completa.")
    colonne: dict[str, str | int] = {}
    indici_usati: dict[int, str] = {}
    for campo, valore in colonne_grezze.items():
        if campo not in ammessi or valore in (None, ""):
            continue
        indice = intero_positivo(valore, f"{nome}, colonna {campo}")
        if indice > max_colonna:
            raise ValueError(f"{nome}: la colonna {get_column_letter(indice)} non contiene dati da leggere.")
        precedente = indici_usati.get(indice)
        if precedente and campo not in CAMPI_SOVRAPPONIBILI and precedente not in CAMPI_SOVRAPPONIBILI:
            raise ValueError(
                f"{nome}: la colonna {get_column_letter(indice)} è assegnata sia a {precedente} sia a {campo}."
            )
        # La colonna resta comunque **occupata** — serve al controllo della
        # colonna d'ordine qui sotto — ma il nome che si ricorda e' quello del
        # campo vero: «assegnata sia a promotion_text sia a description» non
        # direbbe niente a nessuno.
        if precedente is None or campo not in CAMPI_SOVRAPPONIBILI:
            indici_usati[indice] = campo
        colonne[campo] = specifica_colonna(foglio, riga_header, indice)

    mappatura: dict[str, Any] = {
        "header_row": riga_header,
        "data_start_row": riga_dati,
        "columns": colonne,
        "italian_numbers": True,
    }
    marcatore = marcatore_dei_dati(grezza.get("dataStartMarker"), foglio, riga_dati, nome)
    if marcatore:
        mappatura["data_start_marker"] = marcatore
    if str(profilo.get("content_format") or "") != "csv":
        mappatura["sheet"] = str(foglio.get("name") or "FIRST")
    else:
        dettagli = profilo.get("details") or {}
        mappatura["encoding"] = dettagli.get("encoding") or "utf-8-sig"
        mappatura["delimiter"] = dettagli.get("delimiter") or ";"

    if ruolo == "supplier":
        condizioni = condizioni_commerciali(grezza, colonne, foglio, nome)
        if condizioni:
            mappatura["commercial_conditions"] = condizioni
        if "pieces_per_carton" not in colonne and grezza.get("piecesPerCartonDefault") not in (None, ""):
            try:
                fisso = float(str(grezza["piecesPerCartonDefault"]).replace(",", "."))
            except ValueError:
                raise ValueError(f"{nome}: il valore fisso dei pezzi per collo non è un numero.") from None
            if fisso <= 0:
                raise ValueError(f"{nome}: il valore fisso dei pezzi per collo deve essere maggiore di zero.")
            mappatura["pieces_per_carton_default"] = fisso
        if "ean" not in colonne:
            mappatura["ean_unavailable"] = True
        if "supplier_code" not in colonne:
            mappatura["supplier_code_unavailable"] = True
        if "availability" in colonne:
            mappatura["available_values"] = valori_di_disponibilita(grezza.get("availableValues"), nome)
        else:
            mappatura["assume_available"] = True
        if "vat" not in colonne:
            mappatura["vat_unavailable"] = True
        if "supplier_code" in colonne:
            mappatura["text_columns"] = ["supplier_code"]
        if str(profilo.get("content_format") or "") != "csv":
            ordine = intero_positivo(grezza.get("orderColumn"), f"{nome}, colonna ordine")
            if ordine > max_colonna + 1:
                raise ValueError(
                    f"{nome}: la colonna ordine può essere al massimo {get_column_letter(max_colonna + 1)}."
                )
            if ordine in indici_usati:
                raise ValueError(
                    f"{nome}: la colonna ordine non può essere anche la colonna {indici_usati[ordine]}."
                )
            mappatura["order_column"] = get_column_letter(ordine)
            intestazioni = valori_riga(foglio, riga_header)
            intestazione_ordine = intestazioni[ordine - 1] if ordine <= len(intestazioni) else None
            if str(intestazione_ordine or "").strip():
                mappatura["order_header_expected"] = str(intestazione_ordine).strip()
            else:
                # E' un consenso circoscritto a questi byte e a questa
                # posizione. Il writer ricontrolla poi hash, cella vuota e
                # contenuto della colonna prima di scrivere sulla copia.
                mappatura["order_header_blank_confirmed"] = True

    supplier_id = ""
    display_name = ""
    adattatore: dict[str, Any] | None = None
    stato = "SCHEMA_VARIATO"
    if ruolo == "master":
        adattatore = adattatore_per_scelta(profilo, adattatori, ruolo, "")
        if adattatore is None:
            raise ValueError(f"{nome}: nel registro non esiste ancora l'elenco gestionale da aggiornare.")
    else:
        supplier_id = str(grezza.get("supplierId") or "").strip().casefold()
        if supplier_id:
            adattatore = adattatore_per_scelta(profilo, adattatori, ruolo, supplier_id)
            if adattatore is None:
                raise ValueError(f"{nome}: il fornitore scelto non esiste nel registro.")
            display_name = str(adattatore.get("display_name") or supplier_id)
        else:
            display_name = " ".join(str(grezza.get("supplierName") or "").split())
            if len(display_name) < 2 or len(display_name) > 80:
                # ⚠ Da quando la tendina puo' restare su «Scegli il fornitore…»
                # questo messaggio copre due situazioni diverse, e dirne una
                # sola manda a cercare un campo che non c'e' in pagina.
                raise ValueError(
                    f"{nome}: scegli il fornitore, oppure «Nuovo fornitore…» e scrivi il nome."
                )
            supplier_id = slug_fornitore(display_name)
            esistenti = {str(voce.get("supplier_id") or "").casefold() for voce in adattatori}
            if not supplier_id:
                raise ValueError(f"{nome}: il nome del nuovo fornitore non contiene lettere o numeri.")
            if supplier_id in esistenti:
                raise ValueError(f"{nome}: questo fornitore esiste già; selezionalo dall'elenco.")
            stato = "NUOVO_FORNITORE"

    # ⚠ `incomplete_mapping` decide «e' un CSV?» dal **suffisso del nome**. Un
    # CSV che arriva chiamato .xlsx — succede, e il programma lo accetta perche'
    # sceglie il lettore dai byte — si vedeva chiedere «completa foglio da
    # leggere, colonna ordine»: due cose che quel documento non ha e che nessun
    # campo della pagina puo' dare. Il formato lo sa gia' il profilo: glielo si
    # dice.
    percorso_dichiarato = Path(str(profilo.get("path") or nome))
    if str(profilo.get("content_format") or "") == "csv":
        percorso_dichiarato = percorso_dichiarato.with_suffix(".csv")
    mancanti = incomplete_mapping(mappatura, ruolo, percorso_dichiarato)
    if mancanti:
        raise ValueError(f"{nome}: completa {parole_della_pagina(mancanti)}.")

    decisione: dict[str, Any] = {
        "file_name": nome,
        # La decisione vale per QUESTO documento, non per il suo nome. Senza
        # l'impronta bastava eliminare un listino configurato male e ricaricarne
        # un altro con lo stesso nome perche' il ricalcolo gli riapplicasse la
        # mappatura vecchia, colonne comprese (revisione del 14 agosto 2026).
        "file_sha256": profilo.get("sha256"),
        "profile_id": profilo.get("profile_id"),
        "state": stato,
        "role": ruolo,
        "confidence": "ALTA",
        "rationale": "Colonne controllate e confermate nella pagina Importa.",
        "field_mapping": mappatura,
        "user_confirmation": {"required": True, "status": "CONFIRMED"},
    }
    if adattatore is not None:
        decisione["adapter_id"] = adattatore.get("id")
    if ruolo == "supplier":
        decisione["supplier_id"] = supplier_id
        decisione["display_name"] = display_name
    return decisione


def righe_master_confrontabili(righe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Le righe del gestionale che possono davvero entrare nel confronto.

    Il gestionale non ha ne' prezzo netto ne' pezzi per collo, quindi
    «utilizzabile» qui vuol dire un'altra cosa che per un listino, e la dice il
    consumatore vero di questi dati:

    * il **codice** e' l'unica chiave su cui si costruisce il confronto.
      ``build_matching`` cerca le offerte con ``index.get(product["ean"])``
      (``scripts/prepare_sources.py``): senza codice il prodotto esce
      ``EAN_ASSENTE`` da ogni fornitore e nessuna offerta gli si attacca.
    * il **nome** e' quello che l'utente legge nel confronto ed e' l'unica
      chiave del ripiego semantico; una riga che ne e' priva e finisce in coda
      semantica fa fallire ``valuta_shortlist.py``, che pretende una
      descrizione non vuota, e con lei l'intero ricalcolo.

    L'ultimo prezzo non entra nel criterio: serve al risparmio mostrato in
    pagina, non alla partecipazione al confronto, e un articolo mai comprato
    prima non ce l'ha per definizione.
    """
    return [
        voce for voce in righe
        if str(voce.get("ean") or "").strip() and str(voce.get("description") or "").strip()
    ]


def colonne_master_da_rivedere(righe: list[dict[str, Any]]) -> str:
    """Quale colonna guardare, detta con le stesse parole della pagina."""
    senza_codice = any(not str(voce.get("ean") or "").strip() for voce in righe)
    senza_nome = any(not str(voce.get("description") or "").strip() for voce in righe)
    nomi = [
        etichetta for etichetta, mancante in
        (("Codice EAN", senza_codice), ("Nome prodotto", senza_nome)) if mancante
    ]
    if not nomi:
        nomi = ["Codice EAN", "Nome prodotto"]
    return " e ".join(f"la colonna {etichetta}" for etichetta in nomi)


def prova_decisione(profilo: dict[str, Any], decisione: dict[str, Any]) -> dict[str, Any]:
    percorso = Path(str(profilo.get("path") or ""))
    mappatura = decisione["field_mapping"]
    ruolo = decisione["role"]
    if ruolo == "master":
        righe = read_mapped_master(percorso, mappatura)
        utilizzabili = righe_master_confrontabili(righe)
        if len(utilizzabili) < max(1, len(righe) * QUOTA_MINIMA_MASTER):
            quante = (
                "non risulta nessun prodotto confrontabile" if not utilizzabili
                else f"restano solo {len(utilizzabili)} righe confrontabili su {len(righe)} lette"
            )
            raise ValueError(
                f"{profilo.get('file_name')}: con queste colonne {quante}. "
                f"Controlla soprattutto {colonne_master_da_rivedere(righe)}."
            )
        campione = [{
            "row": voce.get("source_row"),
            "ean": voce.get("ean"),
            "description": voce.get("description"),
            "price": voce.get("last_unit_price"),
        } for voce in utilizzabili[:8]]
        return {
            "ok": True,
            "rowsRead": len(righe),
            "rowsUsable": len(utilizzabili),
            "rowsDiscarded": max(0, len(righe) - len(utilizzabili)),
            "sample": campione,
        }

    rapporto: dict[str, Any] = {}
    if container_format(percorso) == "csv":
        righe, avvisi = read_mapped_csv_supplier(
            percorso, decisione["supplier_id"], mappatura, report=rapporto
        )
    else:
        righe, avvisi = read_mapped_xlsx_supplier(
            percorso, decisione["supplier_id"], mappatura, report=rapporto
        )
    utilizzabili = [voce for voce in righe if voce.get("usable") is True]
    if not utilizzabili:
        raise ValueError(
            f"{profilo.get('file_name')}: con queste colonne non risulta nessun prodotto ordinabile. "
            "Controlla soprattutto prezzo e pezzi per collo."
        )
    campione = [{
        "row": voce.get("source_row"),
        "ean": voce.get("ean"),
        "supplierCode": voce.get("supplier_code"),
        "description": voce.get("description"),
        "piecesPerCarton": voce.get("pieces_per_carton") or voce.get("order_multiplier"),
        "price": voce.get("unit_price_net"),
    } for voce in utilizzabili[:8]]
    return {
        "ok": True,
        "rowsRead": len(righe),
        "rowsUsable": len(utilizzabili),
        "rowsDiscarded": max(0, len(righe) - len(utilizzabili)),
        "warnings": len(avvisi),
        "reading": rapporto,
        "sample": campione,
    }


def valida_mappature(
    profili: list[dict[str, Any]], nomi: list[str], adattatori: list[dict[str, Any]],
    payload: dict[str, Any], run_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if str(payload.get("runId") or "") != run_id:
        raise ValueError("Questa anteprima appartiene a un confronto precedente. Riapri la configurazione.")
    grezze = payload.get("mappings")
    if not isinstance(grezze, list):
        raise ValueError("Le mappature dei documenti non sono presenti.")
    per_id: dict[str, dict[str, Any]] = {}
    for voce in grezze:
        if not isinstance(voce, dict):
            continue
        identificativo = str(voce.get("profileId") or "")
        if not identificativo or identificativo in per_id:
            raise ValueError("Ogni documento deve comparire una sola volta.")
        per_id[identificativo] = voce

    richiesti = {nome.casefold() for nome in nomi}
    profili_richiesti = [voce for voce in profili if str(voce.get("file_name") or "").casefold() in richiesti]
    if set(per_id) != {str(voce.get("profile_id") or "") for voce in profili_richiesti}:
        raise ValueError("Configura tutti e soltanto i documenti indicati dal confronto.")

    decisioni: list[dict[str, Any]] = []
    risultati: list[dict[str, Any]] = []
    fornitori: set[str] = set()
    for profilo in profili_richiesti:
        percorso = Path(str(profilo.get("path") or ""))
        if not percorso.is_file():
            raise ValueError(f"{profilo.get('file_name')}: il documento non è più fra i caricamenti.")
        # La run ha profilato proprio questi byte. Se il file e' stato
        # sostituito nel frattempo, la pagina non deve confermare la mappatura
        # di un documento e applicarla a un altro.
        digest = file_hash(percorso)
        if profilo.get("sha256") and digest != profilo.get("sha256"):
            raise ValueError(f"{profilo.get('file_name')}: il documento è cambiato dopo l'anteprima.")
        decisione = decisione_da_mappatura(profilo, per_id[str(profilo.get("profile_id") or "")], adattatori)
        supplier_id = str(decisione.get("supplier_id") or "")
        if supplier_id and supplier_id in fornitori:
            # Il nome dal registro, come in ogni altra frase: `supplier_id.upper()`
            # faceva leggere `NUOVO_FORNITORE_1` a chi aveva dichiarato «Sapori &
            # Co.» (revisione di regressione del 14 agosto 2026).
            raise ValueError(
                "Due documenti sono stati assegnati allo stesso fornitore: "
                f"{nome_dichiarato(supplier_id, adattatori)}."
            )
        fornitori.add(supplier_id)
        risultato = prova_decisione(profilo, decisione)
        risultati.append({
            "profileId": profilo.get("profile_id"),
            "fileName": profilo.get("file_name"),
            **risultato,
        })
        decisioni.append(decisione)
    return {"ok": True, "runId": run_id, "documents": risultati}, decisioni


# ---------------------------------------------------------------------------
# Che cosa il programma legge, colonna per colonna
# ---------------------------------------------------------------------------
# Fino al 15 agosto 2026 l'assegnazione delle colonne si poteva vedere soltanto
# quando il programma NON riconosceva un documento: la mappatura guidata qui
# sopra compare solo dopo `SCHEMA_SCONOSCIUTO`.  Sui documenti riconosciuti —
# cioe' sempre, nella settimana normale — la pagina diceva «Schema riconosciuto
# dal registro (betulla_v1, confidenza 0.99)» e nient'altro: quale colonna
# diventasse il prezzo non era visibile da nessuna parte, e la sola verifica
# possibile era aprire il listino e contare le colonne a mano.
#
# ⚠ Queste posizioni NON si ricalcolano indovinando dalle intestazioni.
# `suggerisci_colonne` e' il proponitore per uno schema nuovo, e su un documento
# gia' riconosciuto risponde il falso: sul listino LARICE vero (che non ha
# nessuna riga di intestazione) propone «fornitore NOCE, nessuna colonna»,
# e sul gestionale lascia vuoti i colli perche' «Colli» e «Quantita» sono
# entrambe alias dello stesso campo e la proposta si ferma sull'ambiguita'.
# La verita' e' una sola ed e' gia' scritta: la dichiarazione del registro
# (`header_aliases`, `column_map`) oppure la mappatura confermata della
# decisione, risolte sul documento dalle stesse due funzioni che usano il
# verificatore e il writer.

# I campi che i lettori mettono davvero nei record, con il nome che ha senso
# per chi fa gli ordini.  Un campo dichiarato dal registro ma che nessun lettore
# consuma (`historical_total`, `line_total`, `department`) non entra: dire che
# il programma «legge» una colonna che poi butta e' un'informazione falsa.
ETICHETTE_DEI_CAMPI: dict[str, str] = {
    "ean": "Codice a barre (EAN)",
    "supplier_code": "Codice articolo del fornitore",
    "description": "Nome del prodotto",
    "unit": "Unità di misura",
    "pieces_per_carton": "Pezzi per collo",
    "unit_price_net": "Prezzo netto",
    "unit_price_pre_discount": "Prezzo prima dello sconto",
    "discount": "Sconto",
    "vat": "IVA",
    "availability": "Disponibilità",
    "pallet": "Pedana",
    "reward_description": "Premio dell'offerta",
    "promotion_text": "Dove il fornitore scrive le sue offerte",
    "last_unit_price": "Ultimo prezzo pagato",
    "suggested_colli": "Colli chiesti dal gestionale",
    "source_quantity_ignored": "Quantità del gestionale (non usata: contano i colli)",
    "source_discount": "Sconto del gestionale",
    "offer_flag": "Segnalazione di offerta",
    "category": "Categoria",
}

CAMPI_LETTI_FORNITORE: tuple[str, ...] = (
    "ean",
    "supplier_code",
    "description",
    "unit",
    "pieces_per_carton",
    "unit_price_net",
    "unit_price_pre_discount",
    "discount",
    "vat",
    "availability",
    "pallet",
    "reward_description",
    "offer_flag",
    "category",
)
CAMPI_LETTI_MASTER: tuple[str, ...] = (
    "ean",
    "description",
    "unit",
    "suggested_colli",
    "last_unit_price",
    "source_quantity_ignored",
    "source_discount",
    "vat",
)


def _etichetta_del_campo(campo: str) -> str:
    return ETICHETTE_DEI_CAMPI.get(campo, campo.replace("_", " "))


def _riga_del_foglio(foglio: dict[str, Any], numero: Any) -> list[Any]:
    try:
        cercata = int(numero)
    except (TypeError, ValueError):
        return []
    for riga in righe_visibili(foglio):
        if int(riga.get("row") or 0) == cercata:
            return list(riga.get("values") or [])
    return []


def _primo_esempio(foglio: dict[str, Any], indice: int, dalla_riga: Any) -> str:
    """Un valore vero di quella colonna, preso dalle righe dei prodotti.

    Serve a rispondere all'unica domanda che conta guardando la tabella: «e'
    davvero questa la colonna del prezzo?».  Un'intestazione puo' mentire, un
    valore no.
    """

    try:
        inizio = int(dalla_riga)
    except (TypeError, ValueError):
        inizio = 0
    for riga in righe_visibili(foglio):
        if int(riga.get("row") or 0) < inizio:
            continue
        valori = riga.get("values") or []
        if indice <= len(valori):
            testo = str(valori[indice - 1] if valori[indice - 1] is not None else "").strip()
            if testo:
                return testo[:60]
    return ""


def colonne_del_foglio(
    profilo: dict[str, Any],
    nome_foglio: Any,
    riga_intestazioni: Any,
    riga_dati: Any,
    fino_a: Any = None,
) -> list[dict[str, Any]]:
    """**Tutte** le colonne di un foglio, con titolo, esempio e che cosa contiene.

    `mappatura_effettiva` dice quali colonne il programma **usa**; questa dice
    quali **esistono**, ed e' quello che serve a chi deve sceglierne una. Il
    conto dei tipi non e' un campione: `inspect_sources` attraversa ogni riga di
    ogni foglio, quindi `formule` e' il numero vero — ed e' l'informazione che
    decide, perche' scrivere l'ordine sopra una colonna di formule le cancella.

    Si arriva a **una colonna oltre** l'ultima scritta: la colonna d'ordine puo'
    essere la prima libera in fondo, ed e' il caso di un listino che non ne ha
    ancora una.  `fino_a` allunga il conto — serve quando la colonna d'ordine di
    oggi e' gia' oltre l'ultima colonna con qualcosa dentro, come su CIPRESSO,
    dove la G e' vuota su tutte e 3372 le righe e nel profilo non compare
    proprio: senza, l'unica colonna che non si potrebbe scegliere sarebbe quella
    accanto a quella in uso.
    """

    fogli = fogli_del_profilo(profilo)
    foglio = next(
        (voce for voce in fogli if str(voce.get("name") or "") == str(nome_foglio or "")),
        fogli[0] if fogli else {},
    )
    intestazioni = _riga_del_foglio(foglio, riga_intestazioni)
    per_indice: dict[int, dict[str, Any]] = {}
    for voce in foglio.get("columns") or []:
        if not isinstance(voce, dict):
            continue
        try:
            per_indice[int(voce.get("index"))] = voce
        except (TypeError, ValueError):
            continue
    try:
        richiesta = int(fino_a)
    except (TypeError, ValueError):
        richiesta = 1
    ultima = max([*per_indice, len(intestazioni), richiesta, 1])
    prima_riga_dati = riga_dati or ((riga_intestazioni or 0) + 1)
    colonne: list[dict[str, Any]] = []
    for indice in range(1, ultima + 2):
        statistiche = per_indice.get(indice) or {}
        tipi = statistiche.get("types") if isinstance(statistiche.get("types"), dict) else {}
        intestazione = ""
        if indice <= len(intestazioni) and intestazioni[indice - 1] is not None:
            intestazione = str(intestazioni[indice - 1]).strip()
        colonne.append({
            "colonna": indice,
            "lettera": get_column_letter(indice),
            "intestazione": intestazione,
            "esempio": _primo_esempio(foglio, indice, prima_riga_dati),
            "valori": int(statistiche.get("nonempty") or 0),
            "formule": int(tipi.get("formula") or 0),
            "testo": int(tipi.get("text") or 0),
            "numeri": int(tipi.get("number") or 0),
        })
    return colonne


def mappatura_effettiva(
    profilo: dict[str, Any],
    adattatore: dict[str, Any] | None,
    decisione: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Le colonne che il programma usa davvero per leggere QUESTO documento.

    Non e' una proposta e non e' una ricostruzione: e' la dichiarazione che i
    lettori seguono, risolta sul documento con `registro.posizione_del_campo` e
    `registro.indice_della_colonna` — le stesse due funzioni che usa la verifica
    delle posizioni durante il riconoscimento e il writer prima di scrivere una
    quantita'.  Se un giorno il registro e il lettore non dicessero piu' la
    stessa cosa, questa tabella lo mostrerebbe invece di nasconderlo.

    Restituisce sempre qualcosa: un documento di cui non si sa niente esce con
    `colonne: []` e `riconosciuto: False`, che in pagina e' l'informazione utile.
    """

    from registro import indice_della_colonna, posizione_del_campo  # noqa: PLC0415

    decisione = decisione if isinstance(decisione, dict) else {}
    adattatore = adattatore if isinstance(adattatore, dict) else {}
    mappatura = decisione.get("field_mapping")
    mappatura = mappatura if isinstance(mappatura, dict) else (adattatore.get("field_mapping") or {})
    mappatura = mappatura if isinstance(mappatura, dict) else {}
    firma_registro = adattatore.get("header_signature")
    firma_registro = firma_registro if isinstance(firma_registro, dict) else {}
    firma_osservata = (profilo.get("deterministic_hint") or {}).get("signature")
    firma_osservata = firma_osservata if isinstance(firma_osservata, dict) else {}

    ruolo = str(decisione.get("role") or ("master" if adattatore.get("kind") == "master" else "supplier"))
    # La riga delle intestazioni: quella confermata vince su quella osservata dal
    # riconoscimento, che vince sul ripiego dichiarato nel registro. Il ripiego
    # per ultimo di proposito — `gestionale_v1` lo dichiara «1» sapendo che
    # nell'export vero e' la 2.
    riga_intestazioni = _primo_numero(
        mappatura.get("header_row"), firma_osservata.get("header_row"), firma_registro.get("header_row")
    )
    riga_dati = _primo_numero(
        mappatura.get("data_start_row"),
        firma_osservata.get("data_start_row"),
        firma_registro.get("data_start_row"),
        (adattatore.get("order_write") or {}).get("data_start_row"),
    )
    nome_foglio = str(
        mappatura.get("sheet")
        or firma_osservata.get("sheet")
        or ""
    )
    fogli = fogli_del_profilo(profilo)
    foglio = next(
        (voce for voce in fogli if str(voce.get("name") or "") == nome_foglio),
        fogli[0] if fogli else {},
    )
    if not nome_foglio:
        nome_foglio = str(foglio.get("name") or "")
    intestazioni = _riga_del_foglio(foglio, riga_intestazioni)

    campi = CAMPI_LETTI_MASTER if ruolo == "master" else CAMPI_LETTI_FORNITORE
    colonne: list[dict[str, Any]] = []
    for campo in campi:
        dichiarata = posizione_del_campo(adattatore, mappatura, campo)
        if dichiarata in (None, ""):
            continue
        indice = indice_della_colonna(intestazioni, dichiarata)
        if indice is None:
            # Dichiarata e non trovata: e' esattamente il caso che l'utente deve
            # poter vedere, non uno da saltare in silenzio.
            colonne.append({
                "campo": campo,
                "etichetta": _etichetta_del_campo(campo),
                "colonna": None,
                "lettera": "",
                "intestazione": "",
                "esempio": "",
                "dichiarata": str(dichiarata),
                "trovata": False,
            })
            continue
        intestazione = ""
        if indice <= len(intestazioni) and intestazioni[indice - 1] is not None:
            intestazione = str(intestazioni[indice - 1]).strip()
        colonne.append({
            "campo": campo,
            "etichetta": _etichetta_del_campo(campo),
            "colonna": indice,
            "lettera": get_column_letter(indice),
            "intestazione": intestazione,
            "esempio": _primo_esempio(foglio, indice, riga_dati or ((riga_intestazioni or 0) + 1)),
            "dichiarata": str(dichiarata),
            "trovata": True,
        })
    colonne.sort(key=lambda voce: (voce["colonna"] is None, voce["colonna"] or 0))

    ordine = _colonna_d_ordine(adattatore, mappatura, intestazioni)
    return {
        "profileId": str(profilo.get("profile_id") or ""),
        "fileName": str(profilo.get("file_name") or ""),
        "role": ruolo,
        "adapterId": str(decisione.get("adapter_id") or adattatore.get("id") or ""),
        "sheet": nome_foglio,
        "headerRow": riga_intestazioni,
        "dataStartRow": riga_dati,
        "orderColumn": ordine,
        "columns": colonne,
        # Da dove vengono le offerte di questo fornitore, se qualcuno l'ha
        # dichiarato. `None` vuol dire che non le legge nessuno — che oggi e' il
        # caso di tutti tranne LARICE — ed e' un'informazione, non un vuoto:
        # e' la differenza fra «questo fornitore non fa offerte» e «le fa e non
        # gliele stiamo leggendo».
        "commercialConditions": _condizioni_dichiarate(adattatore, mappatura, intestazioni),
        "origin": _origine_della_mappatura(decisione, adattatore),
        "recognised": bool(colonne),
    }


def _condizioni_dichiarate(
    adattatore: dict[str, Any], mappatura: dict[str, Any], intestazioni: list[Any]
) -> dict[str, Any] | None:
    """La dichiarazione delle condizioni commerciali, risolta sul documento."""

    from registro import indice_della_colonna  # noqa: PLC0415

    dichiarazione = mappatura.get("commercial_conditions")
    if not isinstance(dichiarazione, dict) or not dichiarazione:
        dichiarazione = adattatore.get("commercial_conditions")
    if not isinstance(dichiarazione, dict) or not dichiarazione:
        return None
    campi = dichiarazione.get("fields")
    campi = campi if isinstance(campi, dict) else {}
    mappa = adattatore.get("column_map")
    mappa = mappa if isinstance(mappa, dict) else {}
    ruoli: dict[str, Any] = {}
    for ruolo, campo in campi.items():
        nome = str(campo or "")
        dichiarata = mappa.get(nome, (mappatura.get("columns") or {}).get(nome))
        indice = indice_della_colonna(intestazioni, dichiarata) if dichiarata not in (None, "") else None
        ruoli[str(ruolo)] = {
            "campo": nome,
            "colonna": indice,
            "lettera": get_column_letter(indice) if indice else "",
        }
    return {
        "layout": str(dichiarazione.get("layout") or ""),
        "sheet": str(dichiarazione.get("sheet") or ""),
        "fields": ruoli,
    }


def _primo_numero(*candidati: Any) -> int | None:
    for valore in candidati:
        if isinstance(valore, bool) or not isinstance(valore, int):
            continue
        if valore >= 1:
            return valore
    return None


def _colonna_d_ordine(
    adattatore: dict[str, Any], mappatura: dict[str, Any], intestazioni: list[Any]
) -> dict[str, Any] | None:
    """La colonna in cui il writer scrivera' le quantita', se ce n'e' una.

    Sta a fianco delle altre e non dentro l'elenco: non e' una colonna che si
    legge, e' l'unica che il programma **scrive** sulla copia del listino.
    """

    dichiarata = mappatura.get("order_column") or (adattatore.get("order_write") or {}).get("order_column")
    if not dichiarata:
        return None
    from registro import indice_della_colonna  # noqa: PLC0415

    indice = indice_della_colonna(intestazioni, dichiarata)
    if indice is None:
        return {"lettera": str(dichiarata), "colonna": None, "intestazione": "", "trovata": False}
    intestazione = ""
    if indice <= len(intestazioni) and intestazioni[indice - 1] is not None:
        intestazione = str(intestazioni[indice - 1]).strip()
    return {
        "lettera": get_column_letter(indice),
        "colonna": indice,
        "intestazione": intestazione,
        "trovata": True,
    }


def _origine_della_mappatura(decisione: dict[str, Any], adattatore: dict[str, Any]) -> str:
    """Da dove viene questa assegnazione.

    Due valori soli, perche' due sono le risposte che cambiano qualcosa per chi
    guarda: **confermata** (l'ha indicata l'utente in pagina, e vale solo per
    questo documento) oppure **registro** (sta in `references/adapters.json`,
    che sia nativa o imparata la settimana scorsa).  Nativa contro imparata e'
    una distinzione vera ma inerte: non cambia ne' quello che si vede ne'
    quello che si puo' fare.
    """

    conferma = decisione.get("user_confirmation")
    if isinstance(conferma, dict) and str(conferma.get("status") or "") == "CONFIRMED":
        return "confermata"
    if (
        adattatore.get("column_map")
        or adattatore.get("field_mapping")
        or adattatore.get("header_aliases")
        or (isinstance(decisione.get("field_mapping"), dict) and decisione["field_mapping"])
    ):
        return "registro"
    return "sconosciuta"
