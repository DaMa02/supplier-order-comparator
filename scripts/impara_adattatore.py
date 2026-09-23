#!/usr/bin/env python3
"""Manda nel registro gli schemi che l'utente ha confermato.

Senza questo passaggio uno schema confermato resta scritto nel manifest della
run e basta: la settimana dopo lo stesso listino torna sconosciuto, l'AI viene
richiamata e all'utente viene chiesto di confermare di nuovo la stessa cosa.
Il programma finito gira da solo e non ci sara' nessuno a copiare a mano una
voce dentro ``references/adapters.json``.

Due regole decidono la forma di tutto il resto:

- **Nel registro entra solo cio' che l'utente ha approvato.**  L'AI propone,
  l'utente conferma: una voce senza ``user_confirmation.status == "CONFIRMED"``
  non entra per nessuna ragione, nemmeno quando la mappatura sembra perfetta.
- **Un rifiuto non e' mai silenzioso.**  Ogni voce non imparata esce nel
  rapporto con il suo motivo scritto in italiano, e se una voce che doveva
  essere imparata viene rifiutata l'uscita e' ``2``.  Un fallimento zitto qui
  vorrebbe dire un fornitore che torna sconosciuto la settimana prossima senza
  che nessuno sappia perche'.

Uso::

    python scripts/impara_adattatore.py --manifest <input_manifest.json> \\
        --adapters references\\adapters.json [--output <rapporto.json>] [--prova]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Il motore del registro e il validatore del manifest stanno qui accanto.  Le
# verifiche sulla mappatura sono quelle del validatore e non una seconda copia:
# due elenchi di controlli che si allontanano di un campo vorrebbero dire una
# mappatura accettata qui e rifiutata la' — o peggio il contrario — senza che
# nessuno se ne accorga.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import registro  # noqa: E402
from validate_input_manifest import incomplete_mapping  # noqa: E402


# I due stati in cui l'AI ha proposto qualcosa che il registro non sa ancora.
# SCHEMA_NOTO non ha niente da imparare; AMBIGUO e FILE_NON_PERTINENTE non
# hanno nemmeno una mappatura da scrivere.
STATI_DA_IMPARARE = ("SCHEMA_VARIATO", "NUOVO_FORNITORE")

CONFERMATO = "CONFIRMED"

# Chi ha approvato lo schema.  E' una costante e non un campo perche' questo
# script scrive solo cio' che l'utente ha confermato: se un giorno esistesse
# una conferma automatica, sarebbe un valore diverso e si vedrebbe nel registro.
CONFERMATO_DA = "utente"


class Rifiuto(Exception):
    """Una voce confermata dall'utente che non si e' potuta imparare.

    E' un'eccezione e non un valore di ritorno perche' un rifiuto interrompe
    davvero la lavorazione di quella voce: proseguire con meta' adattatore
    vorrebbe dire scrivere nel registro una voce che non descrive niente.
    """


def voci_del_manifest(manifest: Any) -> list[dict[str, Any]]:
    """I documenti elencati dal manifest, con i due nomi che il progetto usa.

    ``apply_preflight_decisions`` scrive ``files``, l'inspector ``profiles``:
    leggerli entrambi evita di far dipendere l'apprendimento da quale dei due
    file gli e' stato passato.
    """

    if not isinstance(manifest, dict):
        raise ValueError("Il manifest deve essere un oggetto JSON")
    voci = manifest.get("files") or manifest.get("profiles") or []
    if not isinstance(voci, list):
        raise ValueError("Il manifest non ha un elenco di file")
    return [voce for voce in voci if isinstance(voce, dict)]


def etichetta(voce: dict[str, Any]) -> str:
    """Come si chiama questo documento nel rapporto."""

    return str(voce.get("file_name") or Path(str(voce.get("path") or "")).name or "file-senza-nome")


def motivo_per_saltare(voce: dict[str, Any]) -> str | None:
    """Perche' questa voce non si impara, quando non e' un errore di nessuno.

    Sono i due casi normali: uno schema che il registro conosce gia' non ha
    niente da insegnare, e uno schema che l'utente non ha confermato non deve
    entrare — l'AI propone, l'utente approva.  Restano scritti lo stesso, con
    il loro motivo: nessuno leggera' i log al posto suo.
    """

    decisione = voce.get("ai_preflight") or {}
    stato = decisione.get("state")
    if stato not in STATI_DA_IMPARARE:
        return (f"lo stato «{stato}» non chiede di imparare niente: entrano nel registro solo "
                f"gli schemi che l'AI ha proposto come {' o '.join(STATI_DA_IMPARARE)}")
    conferma = (voce.get("user_confirmation") or {}).get("status")
    if conferma != CONFERMATO:
        return (f"l'utente non ha confermato lo schema (user_confirmation.status = «{conferma}»): "
                f"l'AI propone, l'utente approva, e ciò che non è stato approvato non entra")
    return None


def foglio_dichiarato(profilo: dict[str, Any], mappatura: dict[str, Any]) -> dict[str, Any]:
    """Il foglio del profilo su cui la mappatura confermata dice di lavorare.

    La scelta segue le stesse regole del lettore (``selected_sheet`` in
    ``prepare_manifest_sources``), nome esatto compreso: imparare l'impronta da
    un foglio che il lettore non aprirebbe vorrebbe dire un adattatore
    riconosciuto e poi illeggibile.
    """

    fogli = registro.fogli_del_profilo(profilo)
    if not fogli:
        raise Rifiuto("il profilo nel manifest non contiene nessun foglio")
    scelto = mappatura.get("sheet")
    if scelto in (None, "", "FIRST"):
        return fogli[0]
    if isinstance(scelto, int) and not isinstance(scelto, bool):
        if 0 <= scelto < len(fogli):
            return fogli[scelto]
        raise Rifiuto(f"la mappatura dichiara il foglio numero {scelto}, il documento ne ha {len(fogli)}")
    for foglio in fogli:
        if foglio.get("name") == str(scelto):
            return foglio
    nomi = ", ".join(f"«{foglio.get('name')}»" for foglio in fogli) or "nessuno"
    raise Rifiuto(f"il foglio «{scelto}» dichiarato dalla mappatura non è nel documento: ci sono {nomi}")


def riga_dichiarata(profilo: dict[str, Any], mappatura: dict[str, Any]) -> int:
    """La riga dove la mappatura confermata dice che stanno le intestazioni.

    Il valore mancante vale 1 per un CSV e 0 per un foglio di calcolo: sono i
    ripieghi che usano gia' i due lettori di ``prepare_manifest_sources``, e
    dedurne qui di diversi vorrebbe dire imparare un'impronta che descrive una
    riga che il lettore non guardera' mai.
    """

    grezzo = mappatura.get("header_row")
    if grezzo in (None, ""):
        return 1 if not isinstance(profilo.get("sheets"), list) else 0
    try:
        return int(grezzo)
    except (TypeError, ValueError):
        raise Rifiuto(f"la mappatura dichiara una riga di intestazione illeggibile: {grezzo!r}") from None


def intestazioni_osservate(foglio: dict[str, Any], riga: int) -> list[Any]:
    """I valori della riga di intestazione, come stanno nel profilo del manifest.

    Il file non si riapre: il manifest e' il documento auditabile, ed e' quello
    che l'utente ha avuto davanti quando ha confermato.  Rileggere il file
    vorrebbe dire imparare da un contenuto che nessuno ha approvato.
    """

    if riga < 1:
        raise Rifiuto(
            "la mappatura dichiara che il documento non ha una riga di intestazione "
            "(header_row 0): un'impronta per forma delle colonne si scrive a mano nel "
            "registro, non si impara da un profilo"
        )
    for candidato in registro.righe_di_intestazione(foglio):
        if candidato["row"] == riga:
            return list(candidato["values"])
    raise Rifiuto(
        f"la riga di intestazione {riga} non è fra quelle che il profilo riporta: "
        f"il manifest non la contiene e il documento non si riapre"
    )


def obbligatorie_della_mappatura(mappatura: dict[str, Any], osservate: list[str],
                                 riga: int) -> list[str]:
    """Le intestazioni senza le quali il lettore non puo' lavorare.

    Sono le colonne che la mappatura confermata nomina davvero: se una di
    queste sparisce il documento non e' piu' leggibile con questo adattatore,
    ed e' esattamente la variazione che deve tornare all'AI invece di entrare
    in silenzio.  Le colonne dichiarate per numero restano fuori: non hanno un
    nome da cercare fra le intestazioni.
    """

    colonne = mappatura.get("columns") or mappatura.get("field_mapping") or {}
    per_nome = [valore for valore in colonne.values()
                if valore not in (None, "") and not isinstance(valore, (int, bool))]
    richieste = sorted({registro.normalizza(valore) for valore in per_nome} - {""})
    if not richieste:
        raise Rifiuto(
            "la mappatura non dichiara nessuna colonna per nome: un'impronta per "
            "intestazioni ha bisogno di almeno un nome da ritrovare nel documento"
        )
    mancanti = sorted(set(richieste) - set(osservate))
    if mancanti:
        raise Rifiuto(
            f"la mappatura dichiara colonne che non sono nella riga di intestazione {riga}: "
            + ", ".join(f"«{nome}»" for nome in mancanti)
        )
    return richieste


def verifica_intestazione_ordine(mappatura: dict[str, Any], valori: list[Any], riga: int) -> None:
    """La conferma della colonna vuota o nominata deve descrivere il profilo."""

    dichiarata = str(mappatura.get("order_column") or "").strip().upper()
    if not dichiarata:
        return
    if not re.fullmatch(r"[A-Z]{1,3}", dichiarata):
        raise Rifiuto(f"la colonna ordine «{dichiarata}» non è una colonna Excel valida")
    indice = 0
    for lettera in dichiarata:
        indice = indice * 26 + ord(lettera) - ord("A") + 1
    osservata = valori[indice - 1] if indice <= len(valori) else None
    attesa = str(mappatura.get("order_header_expected") or "").strip()
    vuota = mappatura.get("order_header_blank_confirmed") is True
    if attesa and vuota:
        raise Rifiuto("la colonna ordine non può avere insieme un'intestazione attesa e una conferma di cella vuota")
    if attesa and " ".join(str(osservata or "").replace(" ", " ").split()).casefold() != " ".join(attesa.split()).casefold():
        raise Rifiuto(
            f"la cella {dichiarata}{riga} contiene «{osservata}» invece dell'intestazione confermata «{attesa}»"
        )
    if vuota and str(osservata or "").strip():
        raise Rifiuto(
            f"la cella {dichiarata}{riga} contiene «{osservata}» ma la mappatura la conferma vuota"
        )


def identificativo_dell_adattatore(decisione: dict[str, Any]) -> str:
    """Con quale identificativo l'adattatore entra nel registro.

    Una variazione porta l'identificativo dell'adattatore che sta cambiando, e
    quello e' anche il modo in cui la versione di prima non si perde.  Un
    fornitore nuovo non ne ha uno: si costruisce dal suo `supplier_id`, che e'
    la chiave con cui il resto del programma lo chiama gia'.
    """

    dichiarato = str(decisione.get("adapter_id") or "").strip()
    if dichiarato:
        return dichiarato
    fornitore = str(decisione.get("supplier_id") or "").strip()
    if fornitore:
        # Non `registro.normalizza`: quella incolla le parole per confrontare
        # intestazioni («nuovo_fornitore» diventerebbe «nuovofornitore»), e qui
        # il risultato e' un nome che una persona legge nel registro accanto a
        # `noce_csv_v1`.
        pulito = re.sub(r"[^a-z0-9]+", "_", fornitore.casefold()).strip("_")
        return f"{pulito or fornitore}_v1"
    raise Rifiuto(
        "non si sa con quale identificativo scrivere l'adattatore: la decisione non "
        "dichiara né «adapter_id» né «supplier_id»"
    )


# ⚠ `identificativo_da_scrivere` stava qui e adesso sta in `registro`: la
# mappatura guidata non e' l'unica strada che scrive nel registro — ci scrive
# anche chi sposta la colonna d'ordine dalla pagina — e finche' la regola e'
# vissuta in questo file quell'altra strada non l'ha applicata, portandosi a
# casa una fotocopia completa della voce spedita a ogni spostamento.
identificativo_da_scrivere = registro.identificativo_da_scrivere


def _colonne_per_lettera(mappatura: dict[str, Any],
                         posizioni: dict[str, int] | None) -> dict[str, str]:
    """Dove sta ogni campo della mappatura confermata, espresso in lettere.

    `column_map` parla di posizioni, non di nomi: e' l'unica forma che serve a
    chi legge un listino senza riga di intestazione. La mappatura confermata
    dichiara le colonne a volte per numero e a volte per nome, e il nome si
    risolve con le intestazioni misurate su questo documento.
    """

    colonne = mappatura.get("columns")
    if not isinstance(colonne, dict):
        return {}
    # Tardivo: questo script si lancia anche da solo, e la lettera di colonna
    # serve solo a chi arriva fin qui.
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    posizioni = posizioni or {}
    per_lettera: dict[str, str] = {}
    for campo, dichiarata in colonne.items():
        nome = str(campo or "").strip()
        if not nome or dichiarata in (None, ""):
            continue
        if isinstance(dichiarata, bool):
            continue
        indice: int | None = None
        if isinstance(dichiarata, (int, float)) and float(dichiarata).is_integer():
            indice = int(dichiarata)
        else:
            testo = str(dichiarata).strip()
            if re.fullmatch(r"\d+", testo):
                indice = int(testo)
            else:
                indice = posizioni.get(registro.normalizza(testo))
        if indice and indice >= 1:
            per_lettera[nome] = get_column_letter(indice)
    return per_lettera


def voce_da_scrivere(voce: dict[str, Any], decisione: dict[str, Any], mappatura: dict[str, Any],
                     precedente: dict[str, Any], identificativo: str, foglio: dict[str, Any],
                     riga: int, richieste: list[str], osservate: list[str],
                     quando: str, posizioni: dict[str, int] | None = None) -> dict[str, Any]:
    """L'adattatore come verra' scritto, partendo da quello che c'e' gia'.

    Si parte dalla voce esistente di proposito: un adattatore porta anche
    regole che non stanno nella mappatura — i codici di riga di Larice dicono
    che `SM` non e' merce acquistabile — e riscriverlo da zero le farebbe
    sparire senza che nessuno lo veda.  La variazione confermata cambia lo
    schema, non le convenzioni commerciali del fornitore.
    """

    ruolo = str(decisione.get("role") or "")
    scartate = {"schema_version", "previous_versions"} | ({"supplier_id"} if ruolo == "master" else set())
    base = {chiave: valore for chiave, valore in precedente.items() if chiave not in scartate}

    tipi = list(base.get("file_types") or [])
    suffisso = str(voce.get("declared_suffix") or Path(etichetta(voce)).suffix).casefold()
    if suffisso and suffisso not in tipi:
        tipi.append(suffisso)

    # Il foglio misurato sta in `learned_from` perche' la mappatura puo' dire
    # «FIRST»: senza, non si potrebbe piu' ricostruire da dove viene l'impronta.
    provenienza = {"file_name": etichetta(voce), "sha256": voce.get("sha256")}
    if foglio.get("name"):
        provenienza["sheet"] = foglio.get("name")

    nuova: dict[str, Any] = {"id": identificativo, "kind": "master" if ruolo == "master" else "supplier"}
    if ruolo != "master":
        nuova["supplier_id"] = decisione.get("supplier_id")
    nuova["display_name"] = str(decisione.get("display_name") or base.get("display_name")
                                or decisione.get("supplier_id") or identificativo)
    if tipi:
        nuova["file_types"] = tipi
    nuova["header_signature"] = {
        "kind": "headers",
        "sheet": mappatura.get("sheet"),
        "header_row": riga,
        "data_start_row": mappatura.get("data_start_row"),
        "note": (f"Impronta imparata dal profilo di «{etichetta(voce)}» e confermata "
                 f"dall'utente. Le obbligatorie sono le colonne che la mappatura usa "
                 f"davvero: sono quelle senza cui il lettore non può lavorare."),
        # ⚠ Le posizioni non sono un di piu': un adattatore imparato legge per
        # posizione ogni volta che la mappatura dichiara una colonna per numero
        # (obbligatorio dove un'intestazione compare due volte) e scrive
        # l'ordine in una colonna indicata per lettera.  Una colonna in piu' in
        # testa lascia identico l'insieme dei nomi e sposta tutto il resto: e'
        # la trappola misurata su BETULLA — 12,00 euro al posto di 3,98 con
        # SCHEMA_NOTO 0.99 — e senza questa riga gli adattatori imparati non ne
        # avevano nessuna difesa.
        "columns_note": ("Dove stava ogni intestazione nel documento da cui questo schema è "
                         "stato imparato. Una colonna che si sposta declassa a SCHEMA_VARIATO: "
                         "la mappatura indica colonne anche per numero e per lettera, e quelle "
                         "seguono la posizione, non il nome."),
        "columns": dict(posizioni or {}),
        "required": richieste,
        "known": osservate,
    }
    # ⚠ La regola dell'inizio dei dati va nell'impronta insieme al numero, e
    # **davanti** a lui in ogni lettura: `data_start_row` da solo e' il numero
    # di questa settimana, e il blocco promozionale che precede il listino
    # cambia lunghezza.  Chi rilegge l'adattatore deve vedere che quel numero
    # non e' la regola, e' cio' che la regola valeva il giorno in cui e' stata
    # confermata.
    marcatore = mappatura.get("data_start_marker")
    if marcatore not in (None, "", {}):
        nuova["header_signature"]["data_start_marker"] = marcatore
        nuova["header_signature"]["data_start_note"] = (
            "I dati cominciano dove dice «data_start_marker», ricalcolato a ogni lettura: "
            "«data_start_row» è la riga a cui quel marcatore si è risolto quando l'utente "
            "ha confermato lo schema, e serve a chi scrive la copia dell'ordine."
        )
    nuova["field_mapping"] = mappatura
    # ⚠ `column_map` dice DOVE stanno le colonne, e ci sono lettori che leggono
    # solo quello: il lettore delle soglie con omaggio di Larice, per esempio,
    # perche' quel listino non ha nessuna riga di intestazione e non c'e' un
    # nome da risolvere.  Imparando una variazione confermata si riscriveva
    # `field_mapping` e si lasciava `column_map` a quello della settimana
    # prima: da li' in poi due parti dello stesso programma leggevano due
    # colonne diverse dello stesso listino, e nessuna delle due lo diceva
    # (segnalato dalla revisione del 14 agosto 2026).
    #
    # Si aggiorna solo cio' che la mappatura confermata dichiara: le altre voci
    # restano, perche' `column_map` ne porta anche di non mappate — su Larice
    # l'etichetta del gruppo, il nome dell'articolo in omaggio — e cancellarle
    # spegnerebbe chi le usa.
    aggiornate = _colonne_per_lettera(mappatura, posizioni)
    if aggiornate:
        nuova["column_map"] = {**(base.get("column_map") or {}), **aggiornate}
    # ⚠ Le condizioni commerciali entrano nel registro come le colonne, e per
    # la stessa ragione: fino al 17 agosto 2026 `commercial_conditions` si
    # scriveva a mano dentro `references/adapters.json` e ce l'aveva solo
    # LARICE, quindi un fornitore imparato dalla pagina si leggeva e si
    # compilava ma le sue offerte non le leggeva nessuno. La dichiarazione
    # nomina chiavi di `column_map`, che la riga qui sopra ha appena
    # aggiornato: le condizioni seguono i prezzi invece di restare indietro di
    # una settimana.
    #
    # Se la mappatura confermata non la dichiara, quella di prima resta dov'e'
    # (`setdefault` in coda): una variazione di schema cambia dove stanno le
    # colonne, non il fatto che quel fornitore faccia offerte.
    condizioni = mappatura.get("commercial_conditions")
    if ruolo == "supplier" and isinstance(condizioni, dict) and condizioni:
        nuova["commercial_conditions"] = condizioni
    if (
        ruolo == "supplier"
        and mappatura.get("order_column")
        and (
            base.get("order_write")
            or mappatura.get("order_header_expected")
            or mappatura.get("order_header_blank_confirmed") is True
        )
    ):
        # La colonna ordine e' stata scelta nella stessa anteprima delle
        # colonne di lettura. Per uno schema nuovo questa dichiarazione lo
        # rende compilabile; per una variazione aggiorna anche l'intestazione
        # che il fornitore ha cambiato. Le procedure speciali (il .xls di
        # Noce) conservano comunque modalita' e guardie della versione
        # precedente.
        scrittura = dict(base.get("order_write") or {})
        scrittura["from_field_mapping"] = True
        scrittura["order_column"] = str(mappatura["order_column"]).strip().upper()
        attesa = str(mappatura.get("order_header_expected") or "").strip()
        if attesa:
            scrittura["expected_header"] = attesa
            scrittura.pop("allow_blank_header_if_confirmed", None)
        elif mappatura.get("order_header_blank_confirmed") is True:
            scrittura.pop("expected_header", None)
            scrittura["allow_blank_header_if_confirmed"] = True
        if not scrittura.get("required_columns"):
            colonne_mappate = set((mappatura.get("columns") or {}).keys())
            scrittura["required_columns"] = sorted(
                colonne_mappate
                & {"ean", "supplier_code", "description", "pieces_per_carton",
                   "order_multiplier", "unit_price_net", "unit_price_pre_discount"}
            )
        nuova["order_write"] = scrittura
    nuova["learned_at"] = quando
    nuova["learned_from"] = provenienza
    nuova["confirmed_by"] = CONFERMATO_DA
    # Tutto il resto della voce precedente resta dov'era: `setdefault` non
    # sovrascrive niente di quello che si e' appena deciso.
    for chiave, valore in base.items():
        nuova.setdefault(chiave, valore)
    return nuova


def registro_di_lavoro(percorso: Path, cartella: Path) -> Path:
    """Una copia del registro su cui provare la scrittura prima di farla davvero.

    Serve a una cosa sola, ed e' la ragione per cui questo script esiste: un
    adattatore che, una volta scritto, non basta a far riconoscere il documento
    da cui e' stato imparato e' peggio di niente — la settimana prossima il
    fornitore torna sconosciuto e nel registro c'e' una voce in piu' che nessuno
    sa a che cosa serva.  Scrivere su una copia e rileggerla con lo stesso
    motore che decidera' davvero e' l'unico modo per accorgersene prima.
    """

    copia = cartella / "adapters.json"
    if percorso.exists():
        # In binario: il registro sta sotto git a fine riga LF e una copia
        # riscritta in CRLF non sarebbe piu' lo stesso documento.
        copia.write_bytes(percorso.read_bytes())
    # ⚠ Anche l'imparato, o la riprova girerebbe contro un registro che non e'
    # quello vero: un adattatore imparato in negozio potrebbe vincere il
    # riconoscimento al posto di quello appena scritto, e qui non si vedrebbe.
    imparato = registro.percorso_imparato(percorso)
    if imparato.exists():
        registro.percorso_imparato(copia).write_bytes(imparato.read_bytes())
    return copia


def impara(voce: dict[str, Any], copia: Path, adapters: Path, presi: dict[str, str],
           prova: bool, quando: str) -> dict[str, Any]:
    """Impara una voce del manifest, o dice perché non si è potuto."""

    decisione = voce.get("ai_preflight") or {}
    ruolo = str(decisione.get("role") or "")
    if ruolo not in ("master", "supplier"):
        raise Rifiuto(f"il ruolo «{ruolo or None}» non permette di scrivere un adattatore: "
                      f"serve «master» oppure «supplier»")

    profilo = voce.get("details")
    if not isinstance(profilo, dict):
        raise Rifiuto("la voce del manifest non porta il profilo del documento («details»): "
                      "l'impronta si calcola da lì e il documento non si riapre")

    if ruolo == "supplier" and not str(decisione.get("supplier_id") or "").strip():
        raise Rifiuto("la decisione non dichiara il fornitore («supplier_id»): un adattatore "
                      "senza fornitore non lo cercherebbe nessuno")

    mappatura = decisione.get("field_mapping")
    mancanti = incomplete_mapping(mappatura, ruolo, Path(str(voce.get("path") or etichetta(voce))))
    if mancanti:
        raise Rifiuto("la mappatura confermata è incompleta, mancano: " + "; ".join(mancanti))

    dichiarato = identificativo_dell_adattatore(decisione)
    identificativo = identificativo_da_scrivere(dichiarato, copia)
    if identificativo in presi:
        raise Rifiuto(f"l'identificativo «{identificativo}» è già stato imparato in questa "
                      f"esecuzione da «{presi[identificativo]}»: due schemi diversi con lo stesso "
                      f"nome si sovrascriverebbero a vicenda")

    foglio = foglio_dichiarato(profilo, mappatura)
    riga = riga_dichiarata(profilo, mappatura)
    valori = intestazioni_osservate(foglio, riga)
    verifica_intestazione_ordine(mappatura, valori, riga)
    impronta = registro.impronta(foglio.get("name"), riga, riga + 1, valori)
    richieste = obbligatorie_della_mappatura(mappatura, impronta["headers"], riga)

    posizioni = registro.posizioni_delle_intestazioni(valori)
    # ⚠ La prima volta che si impara accanto a uno spedito, l'id nuovo nel
    # registro non c'e' ancora: la base e' la voce SPEDITA, altrimenti la voce
    # imparata nascerebbe nuda — senza condizioni commerciali, senza alias,
    # senza le regole di riga che il fornitore ha e la mappatura non nomina. Da
    # li' in poi la base e' la voce locale, che quelle cose se l'e' portate
    # dietro.
    precedente = registro.adattatore(identificativo, copia)
    if not precedente and identificativo != dichiarato:
        spedito = registro.adattatore(dichiarato, copia)
        # ⚠ La guardia che teneva `scrivi_adattatore` — «questo adattatore e'
        # del fornitore X, non lo si riscrive per Y» — scattava perche' l'id
        # era lo stesso. Adesso l'id nuovo non collide piu' con niente, e senza
        # questa riga un fornitore erediterebbe in silenzio le regole di un
        # altro: la base di partenza e' la voce spedita, e quella porta le sue
        # condizioni commerciali e i suoi codici di riga.
        atteso = str(spedito.get("supplier_id") or "").strip()
        chiesto = str(decisione.get("supplier_id") or "").strip()
        if spedito and ruolo == "supplier" and atteso and atteso != chiesto:
            raise Rifiuto(
                f"l'adattatore «{dichiarato}» è del fornitore «{atteso}»: non lo si può "
                f"imparare per «{chiesto}», nemmeno accanto — la voce nuova nascerebbe con le "
                f"regole di «{atteso}» addosso"
            )
        precedente = spedito
    nuova = voce_da_scrivere(voce, decisione, mappatura, precedente, identificativo,
                             foglio, riga, richieste, impronta["headers"], quando, posizioni)
    if identificativo != dichiarato:
        # Scritto nella voce, non solo nel nome: chi la rilegge deve poter dire
        # da dove viene senza conoscere la convenzione del suffisso.
        nuova["derivato_da"] = dichiarato

    try:
        esito = registro.scrivi_adattatore(nuova, copia)
    except ValueError as errore:
        raise Rifiuto(f"il registro non ha accettato la voce: {errore}") from errore

    # La riprova che conta: con il registro appena scritto, lo stesso motore che
    # decidera' la settimana prossima deve riconoscere proprio questo documento
    # e proprio con questo adattatore.  Se vince un altro adattatore o se una
    # verifica non passa, quello che si e' scritto non serve a nessuno.
    riletto = registro.riconosci(profilo, copia)
    # ⚠ Il documento se lo puo' riprendere quello SPEDITO, se lo legge senza
    # guasti: e' un esito buono, non un errore — vuol dire che non c'era niente
    # da imparare. Si dice, e non si scrive niente.
    if riletto["state"] == "SCHEMA_NOTO" and riletto["adapter_id"] == dichiarato != identificativo:
        raise Rifiuto(
            f"con questo documento l'adattatore spedito «{dichiarato}» funziona: il programma lo "
            f"legge già senza bisogno di imparare niente, e una voce in più nel registro "
            f"sarebbe solo una cosa da capire fra un mese"
        )
    if riletto["state"] != "SCHEMA_NOTO" or riletto["adapter_id"] != identificativo:
        raise Rifiuto(
            f"con l'adattatore appena scritto il documento risulta {riletto['state']} "
            f"«{riletto['adapter_id']}» invece di SCHEMA_NOTO «{identificativo}»: "
            + (riletto["evidence"][-1] if riletto["evidence"] else "nessuna spiegazione")
        )

    if not prova:
        esito = registro.scrivi_adattatore(nuova, adapters)
    presi[identificativo] = etichetta(voce)
    return {
        "file": etichetta(voce),
        "adapter_id": identificativo,
        "supplier_id": nuova.get("supplier_id"),
        "schema_version": esito["schema_version"],
        "created": esito["created"],
        "previous_versions": esito["previous_versions"],
        "sheet": foglio.get("name"),
        "header_row": riga,
        "required": richieste,
        "hash": impronta["hash"],
        "learned_from": nuova["learned_from"],
        "scritto": not prova,
    }


def scrivi_rapporto(percorso: Path, rapporto: dict[str, Any]) -> None:
    """Salva il rapporto a fine riga LF, come tutto il resto del progetto."""

    percorso.parent.mkdir(parents=True, exist_ok=True)
    with percorso.open("w", encoding="utf-8", newline="\n") as flusso:
        json.dump(rapporto, flusso, ensure_ascii=False, indent=2)
        flusso.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="Il manifest prodotto da apply_preflight_decisions.py")
    # Il registro si dichiara sempre: e' il file che questo script riscrive, e
    # un valore predefinito lo farebbe modificare a chi non se lo aspetta.
    parser.add_argument("--adapters", type=Path, required=True,
                        help="Il registro degli adattatori da aggiornare")
    parser.add_argument("--output", type=Path, help="Dove salvare il rapporto JSON")
    parser.add_argument("--prova", action="store_true",
                        help="Calcola e stampa tutto senza scrivere niente nel registro")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    quando = datetime.now(tz=timezone.utc).isoformat()

    imparati: list[dict[str, Any]] = []
    saltati: list[dict[str, Any]] = []
    presi: dict[str, str] = {}
    rifiutate = 0

    try:
        voci = voci_del_manifest(json.loads(args.manifest.read_text(encoding="utf-8")))
    except (OSError, ValueError) as errore:
        # Un manifest illeggibile e' un fallimento come gli altri: esce con il
        # suo motivo e con uscita 2, invece di una traccia di errore che chi
        # legge il rapporto non troverebbe mai.
        voci = []
        saltati.append({"file": args.manifest.name,
                        "motivo": f"Rifiutato: il manifest non si legge: {errore}"})
        rifiutate = 1

    with tempfile.TemporaryDirectory(prefix="registro_di_prova_") as temporanea:
        copia = registro_di_lavoro(args.adapters, Path(temporanea))
        for voce in voci:
            nome = etichetta(voce)
            motivo = motivo_per_saltare(voce)
            if motivo:
                saltati.append({"file": nome, "motivo": motivo})
                continue
            try:
                imparati.append(impara(voce, copia, args.adapters, presi, args.prova, quando))
            except Rifiuto as rifiuto:
                # «Rifiutato» in testa al motivo perche' e' la differenza che
                # decide l'uscita: una voce saltata e' un caso normale, una
                # rifiutata e' un fornitore che la settimana prossima torna
                # sconosciuto.
                saltati.append({"file": nome, "motivo": f"Rifiutato: {rifiuto}"})
                rifiutate += 1

    rapporto = {"imparati": imparati, "saltati": saltati, "registro": str(args.adapters.resolve())}
    if args.output:
        scrivi_rapporto(args.output, rapporto)
    print(json.dumps(rapporto, ensure_ascii=False, indent=2))
    return 2 if rifiutate else 0


if __name__ == "__main__":
    raise SystemExit(main())
