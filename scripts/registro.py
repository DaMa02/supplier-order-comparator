#!/usr/bin/env python3
"""Le convenzioni dei fornitori stanno nel registro, non dentro il codice.

Il programma finito gira da solo: quando un fornitore cambia una convenzione si
aggiorna `references/adapters.json` e basta.  Una regola scritta a mano dentro
una funzione vale per il listino di quella settimana e muore alla prima
variazione, per giunta in silenzio.

Questo modulo e' l'unico che legge — e da questa fase anche scrive — quel
registro per conto degli altri.  Fa due cose:

1. applica i codici di riga dichiarati da un adattatore;
2. riconosce lo schema di un documento confrontando l'impronta osservata con
   le impronte dichiarate, e impara un adattatore nuovo senza perdere quello
   di prima.

La forma dichiarata dei codici di riga e' questa:

    "row_markers": {
      "field": "discount_raw",
      "codes": {
        "TP": {"means": "...", "orderable": true},
        "SM": {"means": "...", "orderable": false, "row_type": "OMAGGIO"}
      },
      "reward_rows": {"means": "...", "orderable": false, "row_type": "OMAGGIO"}
    }

`field` e' il campo del record normalizzato in cui il codice compare, non una
colonna del foglio: cosi' la stessa regola vale per un lettore dedicato e per
uno guidato da una mappatura.

`reward_rows` e' per i fornitori che la riga premio **non la marcano**: la si
riconosce solo dal testo, e a riconoscerlo e' gia' il motore delle promozioni
(`promotions.looks_like_reward`), lo stesso giudizio con cui il ponte chiude un
blocco.  Vale la pena averla perche' e' il caso che il commento di
`applica_codici_di_riga` aveva previsto: sul canvass nuovo di LARICE le sei
righe «IN OMAGGIO ...» hanno un prezzo vero e nessun codice, e cinque su sei
ripetono l'EAN di un articolo gia' a listino a prezzo pieno.  Senza questa
dichiarazione il confronto sceglie il prezzo del regalo.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# Il registro **spedito**: sta sotto git, lo scrive Daniele, e a ogni avvio il
# PC del negozio lo riporta a com'e' su GitHub con un `reset --hard`.
REGISTRO = Path(__file__).resolve().parents[1] / "references" / "adapters.json"

# Il registro **imparato**: quello che il programma si scrive da solo quando
# riconosce un fornitore nuovo o quando qualcuno conferma uno schema variato.
#
# Perche' e' un file a parte. Fino al 19 agosto 2026 l'imparato finiva dentro
# `references/adapters.json`, cioe' dentro git, e il `reset --hard` dell'avvio
# se lo mangiava: chi aveva appena insegnato un fornitore al programma se lo
# ritrovava sconosciuto al doppio clic successivo, senza una parola. Qui invece
# sta in `app/data/`, che git non traccia e il reset non tocca — insieme alle
# altre memorie del programma, ed e' anche il motivo per cui la copia di
# sicurezza che le salva salva anche questo.
NOME_REGISTRO_IMPARATO = "adattatori_imparati.json"
REGISTRO_IMPARATO = Path(__file__).resolve().parents[1] / "app" / "data" / NOME_REGISTRO_IMPARATO


# Il suffisso di un adattatore imparato SOPRA uno che il programma spedisce.
#
# ⚠ Fino al 22 agosto 2026 una mappatura confermata su un fornitore gia'
# spedito si scriveva con lo STESSO id, e a parita' di id vince l'imparato:
# quello spedito spariva sotto, con tutto quello che porta e che la mappatura
# guidata non chiede — le condizioni commerciali, gli alias delle intestazioni,
# l'intestazione attesa sulla colonna dell'ordine, le posizioni delle colonne.
# Non si poteva tornare indietro dal programma: bisognava aprire
# `app/data/adattatori_imparati.json` e cancellare la voce a mano, e sul PC del
# negozio e' successo davvero — tre voci scritte il 21 agosto alle 17:16,
# `cipresso_v1`, `betulla_v1` e `offerte_v1`, tutte e tre sopra una spedita.
#
# Adesso l'imparato prende un id suo, che porta dentro quello da cui deriva:
# `betulla_v1__locale`. I due convivono, il documento se lo prende quello che lo
# legge davvero (`riconosci`), e lo spedito resta li' per il giorno in cui
# l'imparato non serve piu'.
SUFFISSO_LOCALE = "__locale"


def id_locale(identificativo: str) -> str:
    """L'id con cui si scrive quello che si impara sopra un adattatore spedito."""

    return f"{identificativo}{SUFFISSO_LOCALE}"


def adattatore_base(identificativo: Any) -> str:
    """L'id spedito da cui un adattatore imparato qui deriva, o l'id stesso.

    ⚠ Serve a tutti i punti del programma che decidono **per identificativo**:
    i quattro lettori dedicati (`lettore_dedicato`), le colonne che si possono
    correggere a mano (`colonne_corrette`) e gli espositori di Larice. Senza,
    un `larice_v1__locale` perderebbe il lettore di Larice e con lui espositori
    e soglie con omaggio — cioe' proprio il danno che questa separazione
    esiste per evitare.
    """

    testo = str(identificativo or "")
    if testo.endswith(SUFFISSO_LOCALE):
        return testo[: -len(SUFFISSO_LOCALE)]
    return testo


def identificativo_da_scrivere(dichiarato: str, percorso: Path | None = None) -> str:
    """Con quale id una voce imparata entra davvero nel registro.

    ⚠ Fino al 22 agosto 2026 una mappatura confermata si scriveva con l'id
    dichiarato, e a parita' di id vince l'imparato: confermare la mappatura
    guidata su un fornitore che il programma **spedisce** cancellava di fatto
    la voce spedita, con tutto quello che porta e che la mappatura guidata non
    chiede — le condizioni commerciali, gli alias, l'intestazione attesa sulla
    colonna dell'ordine, le posizioni delle colonne.

    Un fornitore imparato da zero, che nello spedito non c'e', continua ad
    aggiornare la sua voce come sempre: li' non c'e' niente sotto da salvare.

    ⚠ Sta qui e non in `impara_adattatore` perche' la mappatura guidata non e'
    l'unica strada che scrive nel registro: ci scrive anche chi sposta la
    colonna d'ordine dalla pagina, e fino al 22 agosto 2026 quella strada
    derivava l'id per conto suo — cioe' non lo derivava affatto, e si portava a
    casa una fotocopia completa della voce spedita.
    """

    identificativo = str(dichiarato or "").strip()
    if identificativo in identificativi_spediti(percorso):
        return id_locale(identificativo)
    return identificativo


def voce_in_uso(identificativo: Any, voci: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """La voce che vale davvero per una decisione presa con quell'identificativo.

    Una decisione registrata la settimana scorsa dice `betulla_v1`; da allora
    qualcuno puo' aver spostato la colonna d'ordine dalla pagina, e quella
    mossa scrive `betulla_v1__locale`. Chi cerca l'id cosi' com'e' trova ancora
    la voce spedita, con la colonna di prima — e scrive l'ordine dove non lo
    vuole piu' nessuno.

    E' la stessa regola di `_leggi_registro` sulla fusione dei due file e di
    `riconosci` a parita' piena: fra le due versioni della stessa cosa vale la
    piu' recente, quella che ha dato qualcuno che aveva il documento davanti.

    Restituisce `{}` quando non c'e' niente: un registro che non conosce quella
    decisione non e' un guasto da sollevare qui.
    """

    cercato = str(identificativo or "").strip()
    if not cercato:
        return {}
    per_id = {str(voce.get("id") or ""): voce for voce in voci if isinstance(voce, dict)}
    locale = per_id.get(id_locale(adattatore_base(cercato)))
    if locale is not None:
        return locale
    return per_id.get(cercato) or {}


def impronta_della_voce(voce: dict[str, Any]) -> str:
    """L'impronta di una voce del registro cosi' com'e' scritta nel file.

    Serve a una domanda sola: «la voce spedita e' ancora quella su cui questa
    voce imparata e' nata?».  Si calcola sul contenuto intero, note comprese:
    distinguere una modifica «di sostanza» da una «di forma» vorrebbe dire
    tenere un elenco di chiavi che nessuno aggiornerebbe.
    """

    canonico = json.dumps(voce, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()[:16]


def sopra_spedito_di(spedita: dict[str, Any]) -> dict[str, Any]:
    """Il timbro che una voce imparata porta per dire su quale spedita e' nata."""

    return {"id": str(spedita.get("id") or ""), "impronta": impronta_della_voce(spedita)}


def _motivo_del_superamento(imparata: dict[str, Any],
                            spedite_per_id: dict[str, dict[str, Any]]) -> str | None:
    """Perche' una voce imparata non vale piu', o `None` se vale ancora.

    ⚠ Dal 5 settembre 2026 **lo spedito piu' recente vince**.  Fino ad allora a
    parita' di id vinceva l'imparato, sempre: un adattatore imparato male non
    si correggeva spedendone uno nuovo, perche' l'imparato gli restava sopra, e
    per toglierlo bisognava aprire `app/data/adattatori_imparati.json` sul PC
    del negozio.  E' successo tre volte in due settimane — BETULLA e le offerte
    CIPRESSO il 21 agosto, il canvass nuovo di LARICE il 4 settembre — e ogni
    volta la correzione spedita e' rimasta inerte finche' qualcuno non e'
    andato a cancellare la voce a mano.

    La regola: una voce imparata **sopra** una spedita (stesso id, o
    `__locale`) vale finche' la spedita e' quella su cui e' stata imparata.  Se
    la spedita cambia — cioe' Daniele l'ha corretta e l'aggiornamento l'ha
    portata al negozio — vince la spedita e l'imparata si mette da parte.  Chi
    la conferma di nuovo dalla mappatura guidata la rimette in gioco, con il
    timbro della spedita di adesso.

    Una voce imparata **da zero**, che nello spedito non ha nessuna base, non
    e' toccata da questa regola: li' non c'e' niente di piu' recente.

    Una voce sopra una spedita che il timbro non ce l'ha — sono tutte quelle
    imparate prima del 5 settembre 2026 — e' superata: e' esattamente l'elenco
    che `documenti/PROMPT_PC_NEGOZIO_ADATTATORI.md` chiedeva di cancellare a
    mano, e da qui in poi lo fa il programma.
    """

    base = adattatore_base(imparata.get("id"))
    spedita = spedite_per_id.get(base)
    if spedita is None:
        return None
    timbro = imparata.get("sopra_spedito")
    if not isinstance(timbro, dict) or not str(timbro.get("impronta") or ""):
        return f"la voce non dice su quale versione di «{base}» era stata imparata"
    if str(timbro.get("impronta")) == impronta_della_voce(spedita):
        return None
    return f"«{base}» è cambiato dopo che questa voce era stata imparata"


def _spedite_per_id(spedite: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(voce.get("id") or ""): voce for voce in spedite if str(voce.get("id") or "")}


def _scheda_della_superata(voce: dict[str, Any], motivo: str) -> dict[str, Any]:
    return {
        "id": str(voce.get("id") or ""),
        "base": adattatore_base(voce.get("id")),
        "supplier_id": voce.get("supplier_id"),
        "display_name": voce.get("display_name"),
        "motivo": motivo,
    }


def adattatori_superati(percorso: Path | None = None) -> list[dict[str, Any]]:
    """Le voci imparate che lo spedito ha superato, senza toccare niente.

    `[]` anche quando uno dei due file non si legge: il motivo lo dice gia'
    `motivo_registro_illeggibile`, e qui si risponde a un'altra domanda.
    """

    spedito_percorso = Path(percorso or REGISTRO)
    spedite, errore = _voci_di_un_documento(spedito_percorso, quale="Registro degli adattatori")
    if errore is not None:
        return []
    imparato_percorso = percorso_imparato(spedito_percorso)
    if not imparato_percorso.is_file():
        return []
    imparate, errore = _voci_di_un_documento(imparato_percorso, quale="Registro degli adattatori imparati qui")
    if errore is not None:
        return []
    per_id = _spedite_per_id(spedite)
    esito: list[dict[str, Any]] = []
    for voce in imparate:
        motivo = _motivo_del_superamento(voce, per_id)
        if motivo is not None:
            esito.append(_scheda_della_superata(voce, motivo))
    return esito


def percorso_imparato(registro_spedito: Path | None = None) -> Path:
    """Dov'e' l'imparato, dato dove sta lo spedito.

    Nell'installazione vera lo spedito sta in `references/` e l'imparato in
    `app/data/`, con le altre memorie. Un registro che sta altrove — la copia
    di lavoro di `impara_adattatore`, una prova in cartella temporanea — tiene
    il suo imparato **accanto a se'**: una prova che scrivesse dentro
    l'installazione si porterebbe dietro gli adattatori da un'esecuzione
    all'altra, ed e' esattamente il difetto che ha reso rosse nove prove il 14
    agosto 2026.
    """

    base = Path(registro_spedito or REGISTRO)
    if base.parent.name == "references":
        return base.parent.parent / "app" / "data" / NOME_REGISTRO_IMPARATO
    return base.with_name(NOME_REGISTRO_IMPARATO)


def identificativi_spediti(percorso: Path | None = None) -> set[str]:
    """Gli id che stanno nel registro **spedito**, senza l'imparato.

    E' la domanda che serve a `impara_adattatore` per sapere se sta per
    scrivere sopra qualcosa che il programma porta con se': quelli si imparano
    accanto (`id_locale`), non addosso.
    """

    voci, errore = _voci_di_un_documento(Path(percorso or REGISTRO), quale="Registro degli adattatori")
    if errore is not None:
        return set()
    return {str(voce.get("id") or "") for voce in voci if str(voce.get("id") or "")}


def adattatore(adapter_id: str, percorso: Path | None = None) -> dict[str, Any]:
    """La voce del registro con quell'identificativo, o {} se non c'e'.

    Un registro assente o illeggibile non ferma la lettura di un listino: le
    regole opzionali semplicemente non vengono applicate, e chi ne dipende
    davvero (il .xls Noce) lo dice per conto suo.
    """

    voci, _errore = _leggi_registro(percorso)
    for voce in voci:
        if voce.get("id") == adapter_id:
            return voce
    return {}


def mappatura_spedita(adapter_id: str, percorso: Path | None = None) -> dict[str, Any]:
    """La `field_mapping` della voce **spedita** con quell'id, o `{}` se non c'e'.

    ⚠ Spedita e non effettiva, ed e' la differenza che conta: l'imparato lo
    scrive il programma, e la mappatura confermata dalla pagina si costruisce
    da zero — non porta con se' `exclude_rows`. Chi confronta le due per
    accorgersi che una regola si e' persa deve guardare quello che il programma
    ha **ricevuto**, altrimenti perde la regola da tutt'e due le parti del
    confronto e il controllo non scatta piu': misurato il 21 agosto 2026 sul
    listino Noce, 11 righe lette invece di 9, due delle quali FOOD.

    Sta qui e non nel catalogo perche' ogni lettura del registro passa da
    `registro` (regola 4): il catalogo se ne apriva uno per conto suo, ed era
    l'ultimo `json.load` rimasto fuori (6 settembre 2026).

    Un registro assente o illeggibile risponde `{}`, come `adattatore`: chi
    chiede questa mappatura la usa per un controllo in piu', e un file che non
    si apre non deve spegnere il visualizzatore.
    """

    if not str(adapter_id or ""):
        return {}
    voci, errore = _voci_di_un_documento(
        Path(percorso or REGISTRO), quale="Registro degli adattatori",
    )
    if errore is not None:
        return {}
    for voce in voci:
        if str(voce.get("id") or "") == str(adapter_id):
            mappatura = voce.get("field_mapping")
            return mappatura if isinstance(mappatura, dict) else {}
    return {}


def codici_di_riga(fonte: dict[str, Any]) -> dict[str, Any]:
    """I `row_markers` dichiarati da un adattatore o da una mappatura."""

    codici = (fonte or {}).get("row_markers")
    if not isinstance(codici, dict):
        return {}
    # Un fornitore puo' dichiarare i codici, le righe premio, o tutt'e due:
    # pretendere `codes` buttava via una dichiarazione fatta di sole righe
    # premio senza dire niente a nessuno.
    if not isinstance(codici.get("codes"), dict) and not isinstance(codici.get("reward_rows"), dict):
        return {}
    return codici


def codici_ammessi(codici: dict[str, Any]) -> set[str]:
    """I codici che il fornitore usa davvero: il resto è da segnalare."""

    return {str(chiave).strip().upper() for chiave in (codici.get("codes") or {})}


def codice_della_riga(record: dict[str, Any], codici: dict[str, Any]) -> dict[str, Any] | None:
    """Il codice dichiarato che marca questa riga, se ce n'e' uno."""

    campo = str(codici.get("field") or "discount_raw")
    valore = record.get(campo)
    if isinstance(valore, bool) or isinstance(valore, (int, float)):
        return None
    testo = str(valore or "").strip().upper()
    if not testo:
        return None
    for chiave, dichiarato in (codici.get("codes") or {}).items():
        if str(chiave).strip().upper() == testo and isinstance(dichiarato, dict):
            return dichiarato
    return None


def _riga_premio_dal_testo(record: dict[str, Any]) -> bool:
    """Se la descrizione dice da sola che questa riga e' un premio.

    Il giudizio non si riscrive qui: e' `promotions.looks_like_reward`, cioe' lo
    stesso con cui `promotion_bridge._blocchi` decide che un blocco e' finito.
    Due definizioni della stessa cosa divergono, e il giorno che divergono una
    riga sarebbe premio per il motore delle offerte e merce per il confronto.
    """

    try:
        from promotions import looks_like_reward  # noqa: PLC0415 - import tardivo voluto
    except Exception:  # noqa: BLE001 - senza il motore si resta ai codici
        return False
    return looks_like_reward(record.get("description"))


def applica_codici_di_riga(records: list[dict[str, Any]], codici: dict[str, Any]) -> Counter[str]:
    """Declassa le righe che il registro dichiara non ordinabili.

    La riga non si butta: e' informazione — il premio di una soglia con
    omaggio ha codice, EAN e descrizione veri — ma non e' merce acquistabile,
    e non deve poter entrare in un ordine ne' pesare sulla scelta del
    fornitore.

    ⚠ «Il giorno in cui il fornitore ci scrive il valore dell'omaggio, senza
    questa regola diventerebbero ordinabili»: qui c'era scritto cosi', ed e'
    successo il 4 settembre 2026.  Sul canvass nuovo di LARICE le sei righe
    «IN OMAGGIO ...» hanno un prezzo vero (0,65 · 0,50 · 2,00 · 6,00 · 0,60) e
    **nessun codice**, e cinque su sei ripetono l'EAN di un articolo gia' a
    listino a prezzo pieno — l'EAN 8019580330416 sta a 0,68 come merce e a 0,65
    come regalo.  Chi non ha un codice si dichiara con `reward_rows`, e la riga
    la riconosce il motore delle promozioni dal testo.
    """

    conteggio: Counter[str] = Counter()
    if not codici:
        return conteggio
    premio = codici.get("reward_rows")
    if not isinstance(premio, dict):
        premio = None
    for record in records:
        dichiarato = codice_della_riga(record, codici)
        if (
            premio is not None
            # Un codice che gia' declassa la riga resta com'e': dice di piu' di
            # quanto sappia il testo, e portarlo via sarebbe una perdita.
            and (dichiarato is None or dichiarato.get("orderable") is not False)
            and _riga_premio_dal_testo(record)
        ):
            # Fra le due risposte vince «non ordinabile», e la direzione non e'
            # simmetrica: scambiare un prodotto per un premio si vede subito —
            # manca dal confronto — mentre scambiare un premio per un prodotto
            # mette a listino un prezzo che non esiste, e si scopre dal
            # fornitore.
            dichiarato = premio
        if not dichiarato or dichiarato.get("orderable") is not False:
            continue
        tipo = str(dichiarato.get("row_type") or "NON_ORDINABILE")
        record["usable"] = False
        record["row_type"] = tipo
        record["not_orderable_reason"] = str(dichiarato.get("means") or "")
        conteggio[tipo] += 1
    return conteggio


# ---------------------------------------------------------------------------
# La memoria degli schemi: impronte, riconoscimento, scrittura versionata.
# ---------------------------------------------------------------------------


# Le tre verifiche che nessun'altra parte del programma puo' fare al posto
# nostro: se il prezzo e' diventato testo il confronto fra fornitori si
# svuota in silenzio, ed e' la variazione che fa piu' danno.
CAMPI_NUMERICI = ("unit_price_net", "unit_price_pre_discount", "pieces_per_carton")

# Un adattatore con un lettore dedicato (BETULLA, Larice, il gestionale) non
# dichiara una mappatura: pretendere lo stesso le verifiche che la riguardano
# lo farebbe declassare a SCHEMA_VARIATO ogni settimana, e ogni settimana
# costerebbe una chiamata all'AI per niente.
NON_APPLICABILE = "l'adattatore non dichiara una mappatura: verifica non applicabile"


def normalizza(valore: Any) -> str:
    """Riduce un'intestazione alla sua sostanza, per poterla confrontare.

    Lo stesso fornitore scrive «Cod.Art.», «COD ART» e «Cod. Art.» in tre
    settimane diverse senza avvisare nessuno: confrontare le intestazioni
    cosi' come sono vorrebbe dire non riconoscere piu' un listino per via di
    un punto in piu'.
    """

    testo = unicodedata.normalize("NFKD", str(valore or ""))
    testo = "".join(carattere for carattere in testo if not unicodedata.combining(carattere))
    return re.sub(r"[^a-z0-9]+", "", testo.casefold())


def impronta_intestazioni(valori: Iterable[Any]) -> list[str]:
    """I token normalizzati di una riga: senza vuoti, senza doppioni, ordinati.

    L'ordine delle colonne non e' identita': un fornitore che sposta una
    colonna manda lo stesso listino.  Ordinare rende anche l'impronta stabile
    da una lettura all'altra, che e' quello che serve per confrontarla.
    """

    return sorted({normalizza(valore) for valore in valori} - {""})


def posizioni_delle_intestazioni(intestazioni: Iterable[Any]) -> dict[str, int]:
    """Dove sta ogni intestazione, nella forma che `header_signature.columns` usa.

    E' il pezzo che rende verificabile una firma: l'insieme dei nomi dice che
    il documento e' di quel fornitore, le posizioni dicono che il lettore
    trovera' le colonne dove le va a prendere.  Serve a chi **scrive** una
    firma — `impara_adattatore` — perche' la scriva nella stessa forma in cui
    `_verifica_posizioni` la rilegge; averle scritte in due modi diversi
    vorrebbe dire un adattatore imparato e poi mai piu' riconosciuto.

    Un nome ripetuto vale l'ultima posizione, esattamente come nella verifica:
    su ACERO «COSTO IMPON.» compare in colonna 9 e in colonna 15, e le due
    parti devono rispondere la stessa cosa.
    """

    posizioni: dict[str, int] = {}
    for posizione, valore in enumerate(intestazioni, start=1):
        token = normalizza(valore)
        if token:
            posizioni[token] = posizione
    return posizioni


def impronta(sheet: str | None, header_row: int | None,
             data_start_row: int | None, intestazioni: Iterable[Any]) -> dict[str, Any]:
    """L'impronta osservata di uno schema, con il suo `hash` per l'audit.

    L'`hash` serve a dire in un messaggio «e' cambiato qualcosa» e a ritrovare
    due letture identiche; **non** e' il criterio di identita', perche' una
    colonna decorativa in piu' lo cambia e non cambia lo schema.  L'identita'
    la decide l'insieme `required` in sottoinsieme.
    """

    corpo = {
        "sheet": sheet,
        "header_row": header_row,
        "data_start_row": data_start_row,
        "headers": impronta_intestazioni(intestazioni),
    }
    canonico = json.dumps(corpo, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {**corpo, "hash": hashlib.sha256(canonico.encode("utf-8")).hexdigest()}


def adattatori(percorso: Path | None = None) -> list[dict[str, Any]]:
    """Tutte le voci del registro, `[]` se il registro manca o non si legge."""

    voci, _errore = _leggi_registro(percorso)
    return voci


def adattatori_effettivi(percorso: Path | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Le voci del registro effettivo **e** il motivo, in una lettura sola.

    Serve a chi deve fare due cose diverse a seconda che il registro sia
    vuoto o rotto, senza aprire i file due volte. Chi vuole solo l'elenco usa
    `adattatori()`; chi vuole solo il motivo usa
    `motivo_registro_illeggibile()`.
    """

    return _leggi_registro(percorso)


_NOMI_IN_MEMORIA: dict[str, tuple[tuple[Any, ...] | None, dict[str, str]]] = {}


def _firma_dei_due_registri(spedito: Path) -> tuple[Any, ...] | None:
    """Quanto basta per accorgersi che uno dei due file e' cambiato.

    `None` vuol dire «non lo so»: chi la usa per tenere qualcosa in memoria
    deve rileggere invece di fidarsi.
    """

    firma: list[Any] = []
    for documento in (spedito, percorso_imparato(spedito)):
        try:
            stato = documento.stat()
        except OSError:
            if documento == spedito:
                return None
            # L'imparato che non c'e' e' un caso normale, e va distinto da un
            # imparato che c'e' ed e' vuoto.
            firma.append(None)
            continue
        firma.append((stato.st_mtime_ns, stato.st_size))
    return tuple(firma)


def nomi_dichiarati_fra(adattatori_letti: Iterable[dict[str, Any]]) -> dict[str, str]:
    """La stessa mappa, ma su adattatori gia' letti da qualcun altro.

    Serve a chi il registro ce l'ha gia' aperto in mano — la mappatura guidata —
    e non deve rileggerlo dal disco solo per sapere come si chiama un fornitore.
    La regola del nome piu' corto sta scritta **qui**, in un posto solo: era
    proprio la sua terza copia il difetto che il cantiere R8 ha chiuso.
    """

    nomi: dict[str, str] = {}
    for voce in adattatori_letti or []:
        if not isinstance(voce, dict):
            continue
        fornitore = str(voce.get("supplier_id") or "").strip().casefold()
        nome = str(voce.get("display_name") or "").strip()
        if not fornitore or not nome:
            continue
        precedente = nomi.get(fornitore)
        if precedente is None or len(nome) < len(precedente):
            nomi[fornitore] = nome
    return nomi


def nome_del_fornitore_fra(supplier_id: Any, adattatori_letti: Iterable[dict[str, Any]]) -> str:
    """`nome_del_fornitore`, ma senza tornare sul disco: stesso ripiego."""

    identificativo = str(supplier_id or "").strip()
    if not identificativo:
        return "FORNITORE"
    nome = nomi_dichiarati_fra(adattatori_letti).get(identificativo.casefold())
    if nome:
        return nome
    return re.sub(r"[-_]+", " ", identificativo).strip().upper() or "FORNITORE"


def nomi_dei_fornitori(percorso: Path | None = None) -> dict[str, str]:
    """Come si chiama ogni fornitore, secondo il registro.

    Il nome leggibile stava scritto in **tre** punti del codice — il servizio,
    il writer Node e la costruzione del confronto — e ne conosceva quattro: un
    fornitore imparato compariva come «NUOVO_FORNITORE», con l'underscore, nei
    messaggi, nei nomi dei file d'ordine e nello storico, mentre il registro ne
    portava gia' il `display_name`. Qui la fonte e' una sola, ed e' la stessa da
    cui il fornitore nasce.

    ⚠ Quando piu' adattatori dichiarano lo stesso `supplier_id` — Noce ha il
    CSV e l'Excel — vince il nome **piu' corto**: il piu' lungo descrive il
    documento («NOCE listino Excel 97-2003»), non il fornitore.

    Il risultato si tiene in memoria finche' il file non cambia: questa mappa la
    chiede una frase per prodotto, e sono centinaia per pagina.
    """

    documento = Path(percorso or REGISTRO)
    chiave = str(documento)
    # ⚠ La firma copre TUTTI E DUE i file: con la sola firma dello spedito, un
    # fornitore appena imparato continuava a chiamarsi come prima finche' non
    # si toccava un file che non c'entrava niente.
    firma = _firma_dei_due_registri(documento)
    if firma is not None:
        memorizzato = _NOMI_IN_MEMORIA.get(chiave)
        if memorizzato is not None and memorizzato[0] == firma:
            return dict(memorizzato[1])
    nomi = nomi_dichiarati_fra(adattatori(documento))
    if firma is not None:
        _NOMI_IN_MEMORIA[chiave] = (firma, dict(nomi))
    return nomi


def nome_del_fornitore(supplier_id: Any, percorso: Path | None = None) -> str:
    """Il nome leggibile di un fornitore, con il ripiego quando non si sa.

    Il ripiego non e' l'identificativo tal quale: `nuovo_fornitore` diventa
    «NUOVO FORNITORE», perche' l'underscore in mezzo a una frase si legge come
    un errore del programma.
    """

    identificativo = str(supplier_id or "").strip()
    if not identificativo:
        return "FORNITORE"
    nome = nomi_dei_fornitori(percorso).get(identificativo.casefold())
    if nome:
        return nome
    return re.sub(r"[-_]+", " ", identificativo).strip().upper() or "FORNITORE"


def motivo_registro_illeggibile(percorso: Path | None = None) -> str | None:
    """La frase da dire quando il registro non si apre, `None` se si apre.

    «Il registro non dichiara come si scrive l'ordine» e «il registro non si
    legge» mandano l'utente in due posti diversi: la prima a dichiarare
    `order_write`, la seconda ad aggiustare un JSON rotto.  Con `adattatori()`
    che risponde `[]` in tutti e due i casi, chi avvisa diceva sempre la prima
    frase — anche davanti a un file che non si apre (revisione avversariale
    del 13 agosto 2026).
    """

    _voci, errore = _leggi_registro(percorso)
    return errore


def _voci_di_un_documento(documento_percorso: Path, *, quale: str) -> tuple[list[dict[str, Any]], str | None]:
    """Le voci di UN file di registro, e il motivo se non si è potuto aprire."""

    try:
        documento = json.loads(documento_percorso.read_text(encoding="utf-8"))
    except OSError as errore:
        return [], f"{quale} non leggibile: {errore}"
    except ValueError as errore:
        return [], f"{quale} non interpretabile: {errore}"
    voci = documento.get("adapters") if isinstance(documento, dict) else None
    if not isinstance(voci, list):
        return [], f"{quale} senza l'elenco «adapters»"
    return [voce for voce in voci if isinstance(voce, dict)], None


def _leggi_registro(percorso: Path | None) -> tuple[list[dict[str, Any]], str | None]:
    """Le voci del registro e, quando non si e' potuto leggerlo, il motivo.

    Il registro effettivo e' fatto di due file: quello **spedito**
    (`references/adapters.json`, sotto git) e quello **imparato**
    (`app/data/adattatori_imparati.json`, fuori da git). A parita' di `id`
    vince l'imparato, perche' e' la risposta piu' recente e l'ha data qualcuno
    che aveva il documento davanti — **finche' la spedita e' quella su cui
    l'imparato e' nato**.  Se la spedita e' cambiata dopo, vince la spedita e
    l'imparata non entra: vedi `_motivo_del_superamento`.  Chi vuole sapere
    quali sono chiede `adattatori_superati`; chi vuole toglierle dal file
    chiama `metti_da_parte_le_superate`, che e' quello che fa la catena
    all'inizio di ogni confronto.

    L'imparato che manca e' la normalita' — un'installazione nuova non ha
    imparato niente. L'imparato **rotto** invece si dice, ma non ferma: si
    riconosce con il solo spedito e il motivo esce di qui, perche' un
    riconoscimento fallito per un file illeggibile non deve somigliare a un
    documento sconosciuto.
    """

    spedito_percorso = Path(percorso or REGISTRO)
    spedite, errore = _voci_di_un_documento(spedito_percorso, quale="Registro degli adattatori")
    if errore is not None:
        return [], errore

    imparato_percorso = percorso_imparato(spedito_percorso)
    if not imparato_percorso.is_file():
        return spedite, None
    imparate, errore_imparato = _voci_di_un_documento(
        imparato_percorso, quale="Registro degli adattatori imparati qui",
    )
    if errore_imparato is not None:
        return spedite, errore_imparato

    spedite_per_id = _spedite_per_id(spedite)
    attive = [voce for voce in imparate if _motivo_del_superamento(voce, spedite_per_id) is None]
    per_id = {str(voce.get("id") or ""): voce for voce in attive if str(voce.get("id") or "")}
    if not per_id:
        return spedite, None
    fuse = [per_id.pop(str(voce.get("id") or ""), voce) for voce in spedite]
    # Quelle imparate da zero, che nello spedito non ci sono: in coda, nello
    # stesso ordine in cui sono state imparate.
    fuse.extend(voce for voce in attive if str(voce.get("id") or "") in per_id)
    return fuse, None


def scrittura_ordine(adattatore: dict[str, Any]) -> dict[str, Any]:
    """Come si scrive l'ordine dentro il listino di questo fornitore, se si sa.

    `{}` vuol dire «non si sa»: il fornitore entra nel confronto e ci resta,
    ma per lui non nascera' nessuna copia da mandare, e questo va detto invece
    di lasciarlo scoprire alla fine.
    """

    dichiarazione = (adattatore or {}).get("order_write")
    return dichiarazione if isinstance(dichiarazione, dict) and dichiarazione else {}


def fornitori_con_scrittura(percorso: Path | None = None) -> set[str]:
    """I fornitori che il registro dichiara compilabili.

    E' l'unica definizione di «compilabile» del programma: la usano il
    lanciatore, per costruire la configurazione di scrittura, e
    l'orchestratore, per avvisare quando un fornitore del confronto non c'e'
    dentro.  Due elenchi che si allontanano vorrebbero dire un avviso che non
    corrisponde a quello che poi succede davvero.
    """

    return {
        str(voce.get("supplier_id") or "").strip().casefold()
        for voce in adattatori(percorso)
        if scrittura_ordine(voce) and str(voce.get("supplier_id") or "").strip()
    }


def fogli_del_profilo(profilo: dict[str, Any]) -> list[dict[str, Any]]:
    """I fogli da esaminare, che un CSV non ha.

    Il profilo di un CSV e' piatto — niente `sheets` — mentre quello di un
    foglio di calcolo ne porta uno per scheda.  Trattare il CSV come un foglio
    solo evita di avere due motori di riconoscimento che possono divergere.
    """

    fogli = (profilo or {}).get("sheets")
    if isinstance(fogli, list):
        return [foglio for foglio in fogli if isinstance(foglio, dict)]
    return [profilo] if isinstance(profilo, dict) else []


def righe_di_intestazione(foglio: dict[str, Any]) -> list[dict[str, Any]]:
    """Le righe del foglio che possono essere un'intestazione, come le vede il profilo.

    Si legge il profilo cosi' com'e': lo produce gia' l'inspector, e una
    seconda lettura del file qui dentro sarebbe una seconda verita' sullo
    stesso documento.

    `header_candidates` sopravvive solo alle righe che contengono almeno una
    parola di un elenco scritto nell'inspector: un fornitore che intitola le
    colonne in modo tutto suo non arriverebbe mai fin qui.  Percio' se il
    profilo porta `header_rows` — le prime righe non vuote cosi' come sono —
    si usano quelle, che non dipendono da nessun elenco.
    """

    righe: list[dict[str, Any]] = []
    for candidato in foglio.get("header_rows") or foglio.get("header_candidates") or []:
        if not isinstance(candidato, dict):
            continue
        valori = list(candidato.get("values") or [])
        token = impronta_intestazioni(valori)
        if token:
            righe.append({"row": candidato.get("row"), "headers": token, "values": valori})
    return righe


def _quota(colonna: dict[str, Any], tipo: str, scarto: int = 0) -> float:
    """La frazione di celle non vuote di quel tipo, **come sono scritte**.

    `scarto` toglie dal conto le celle che non sono dati — in pratica quella
    dell'intestazione, che e' testo e sta nella stessa colonna dei numeri.

    Qui una formula conta come formula, e non e' un dettaglio: questa quota
    serve alle impronte per forma delle colonne, cioe' a dire **di chi e'** un
    documento, e com'e' scritta una colonna e' un tratto d'identita' come un
    altro.  Il listino LARICE non ha una sola formula; ACERO ne ha 18.644 e
    le stesse colonne calcolate.  Contando le formule per il loro risultato,
    ACERO prendeva 0,76 sull'impronta di LARICE e si presentava come una
    sua variazione — un fornitore vero scambiato per un altro fornitore vero.
    Un'impronta che volesse accettarle lo dichiara da se': `any_of` legge il
    nome del tipo, e «formula» e' un tipo come gli altri.
    """

    non_vuote = max(1, int(colonna.get("nonempty") or 0) - max(0, scarto))
    return float((colonna.get("types") or {}).get(tipo, 0)) / non_vuote


def _quota_leggibile(colonna: dict[str, Any], tipo: str, scarto: int = 0) -> float:
    """La frazione di celle che **valgono** quel tipo, formule comprese.

    ⚠ Una cella scritta `=SUM(E4*(1-5%))` in una colonna di prezzi vale 1,52,
    ed e' quello che legge chi apre il documento con `data_only=True`.  Il
    profilo lo apre con `data_only=False` e ci trova il testo della formula:
    finche' la colonna dei prezzi di GINEPRO contava «formula» su 4132 celle,
    `tipi_plausibili` la dichiarava **0% numerica** e quel fornitore sarebbe
    tornato SCHEMA_VARIATO ogni settimana — una mappatura a mano per sempre.
    Due parti dello stesso programma guardavano la stessa cella e ne dicevano
    due cose diverse.

    Il valore in cache lo censisce `inspect_sources.censisci_valori_delle_formule`
    in `formula_values`.  Una formula che restituisce **testo** resta testo: la
    verifica continua a bocciare una colonna di prezzi diventata testo, che e'
    la ragione per cui esiste.
    """

    non_vuote = max(1, int(colonna.get("nonempty") or 0) - max(0, scarto))
    conteggio = float((colonna.get("types") or {}).get(tipo, 0))
    conteggio += float((colonna.get("formula_values") or {}).get(tipo, 0))
    return conteggio / non_vuote


def _colonne_per_indice(foglio: dict[str, Any]) -> dict[int, dict[str, Any]]:
    colonne = {}
    for colonna in foglio.get("columns") or []:
        if isinstance(colonna, dict) and isinstance(colonna.get("index"), int):
            colonne[colonna["index"]] = colonna
    return colonne


def _punteggio_forma(firma: dict[str, Any], foglio: dict[str, Any]) -> tuple[float, list[str]]:
    """Il punteggio di un'impronta per forma delle colonne.

    Larice non ha nessuna riga di intestazione: le sue colonne si riconoscono
    da come sono popolate.  Senza questa strada la regola di Larice resterebbe
    scritta nel codice, che e' esattamente cio' che questa fase toglie.
    """

    intervallo = foglio.get("active_range") or {}
    colonne = _colonne_per_indice(foglio)
    minime = firma.get("min_columns")
    if isinstance(minime, int) and int(intervallo.get("max_column") or 0) < minime:
        return 0.0, []
    richieste = [indice for indice in (firma.get("required_columns") or []) if isinstance(indice, int)]
    if any(indice not in colonne for indice in richieste):
        return 0.0, []
    # ⚠ Le colonne che devono essere **vuote**. Sono la meta' che mancava a
    # un'impronta per forma: «ci sono prezzi, pezzi e codici a barre» lo dicono
    # quasi tutti i listini del mondo, e senza un tratto negativo il foglio
    # delle offerte — che ha A-F piene, G e H vuote e la sola parola ORDINE
    # sopra — si prendeva il listino di un altro fornitore (misurato il 21
    # agosto 2026 su due fogli costruiti apposta). Una colonna vuota non
    # compare fra quelle del profilo: e' cosi' che si dice «non c'e' niente».
    assenti = [indice for indice in (firma.get("absent_columns") or []) if isinstance(indice, int)]
    if any(indice in colonne for indice in assenti):
        return 0.0, []

    punteggio = 0.0
    prove: list[str] = []
    for verifica in firma.get("checks") or []:
        if not isinstance(verifica, dict):
            continue
        colonna = colonne.get(verifica.get("column"))
        if colonna is None:
            continue
        minimo = verifica.get("min_nonempty")
        if isinstance(minimo, int) and int(colonna.get("nonempty") or 0) < minimo:
            continue
        # Il tetto: serve dove a identificare la colonna e' il fatto che porti
        # UNA cosa sola. Nel foglio delle offerte la colonna H ha la sola
        # parola ORDINE, e se ne compare una seconda l'inizio dei prodotti non
        # e' piu' deducibile — il lettore alzerebbe, e alzerebbe dentro la
        # catena, fermando il ricalcolo di tutti i fornitori invece di
        # riportare questo documento davanti a chi lo puo' guardare.
        massimo = verifica.get("max_nonempty")
        if isinstance(massimo, int) and int(colonna.get("nonempty") or 0) > massimo:
            continue
        rapporto = verifica.get("type_ratio")
        if isinstance(rapporto, dict):
            somma = sum(_quota(colonna, str(tipo)) for tipo in (rapporto.get("any_of") or []))
            # Il confronto e' `>` e non `>=`: era cosi' prima che la regola
            # uscisse dal codice e cambiarlo qui sposterebbe di nascosto la
            # soglia di un riconoscimento gia' collaudato sui listini veri.
            if not somma > float(rapporto.get("min_exclusive", 0.0)):
                continue
        attesi = verifica.get("examples_include")
        if attesi:
            esempi = {str(valore).strip().upper() for valore in (colonna.get("examples") or [])}
            if not {str(valore).strip().upper() for valore in attesi} <= esempi:
                continue
        punteggio += float(verifica.get("weight") or 0.0)
        if verifica.get("evidence"):
            prove.append(str(verifica["evidence"]))
    return punteggio, prove


def _candidati(voci: list[dict[str, Any]], fogli: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gli adattatori che potrebbero essere questo documento, con quanto ci somigliano."""

    trovati: list[dict[str, Any]] = []
    for ordine, voce in enumerate(voci):
        firma = voce.get("header_signature")
        if isinstance(firma, dict) and str(firma.get("kind") or "headers") == "headers":
            richieste = sorted({normalizza(token) for token in (firma.get("required") or [])} - {""})
            for foglio in fogli if richieste else []:
                riga = next(
                    (riga for riga in righe_di_intestazione(foglio)
                     if set(richieste) <= set(riga["headers"])),
                    None,
                )
                if riga is None:
                    continue
                trovati.append({
                    "voce": voce, "ordine": ordine, "kind": "headers", "foglio": foglio,
                    "confidence": 0.99, "obbligatorie": len(richieste), "prove": [],
                    "impronta": impronta(
                        foglio.get("name"), riga["row"],
                        (riga["row"] + 1) if isinstance(riga["row"], int) else None,
                        riga["values"],
                    ),
                })
                break
        forma = voce.get("column_shape_signature")
        if isinstance(forma, dict):
            soglia = float(forma.get("min_score") or 0.0)
            massimo = float(forma.get("max_confidence") or 1.0)
            for foglio in fogli:
                punteggio, prove = _punteggio_forma(forma, foglio)
                if punteggio < soglia:
                    continue
                trovati.append({
                    "voce": voce, "ordine": ordine, "kind": "shape", "foglio": foglio,
                    "confidence": round(min(massimo, punteggio), 2),
                    "obbligatorie": len(forma.get("required_columns") or []), "prove": prove,
                    # Senza intestazioni l'impronta osservata resta quella del
                    # foglio: dire «nessuna riga di intestazione» e' un dato,
                    # inventarne una sarebbe una bugia.
                    "impronta": impronta(foglio.get("name"), None, None, []),
                })
                break
    return trovati


# Quante intestazioni obbligatorie possono mancare a un adattatore perche' il
# documento resti «il suo, meno questa cosa qui» invece di un documento
# sconosciuto. Due: una cella svuotata per sbaglio, o una colonna che il
# fornitore ha smesso di intitolare. Da tre in su non si sta piu' riconoscendo
# niente, si sta indovinando.
MASSIMO_OBBLIGATORIE_MANCANTI = 2

# E quante devono restarci comunque. Dire «e' il listino di BETULLA» avendone
# viste due su cinque sarebbe una bugia detta con sicurezza, che e' peggio di
# «non lo riconosco».
MINIMO_OBBLIGATORIE_PRESENTI = 3


def lettera_di_colonna(numero: Any) -> str:
    """Da numero 1-based a lettera, come la mostra un foglio di calcolo.

    Serve per parlare a chi ha il documento aperto davanti: «la colonna C» si
    trova, «la colonna 3» si conta.
    """

    try:
        indice = int(numero)
    except (TypeError, ValueError):
        return ""
    if indice < 1:
        return ""
    lettere = ""
    while indice > 0:
        indice, resto = divmod(indice - 1, 26)
        lettere = chr(ord("A") + resto) + lettere
    return lettere


def _nome_leggibile(voce: dict[str, Any], token: str) -> str:
    """Come si chiama quell'intestazione nel documento, non nell'impronta.

    L'impronta porta i token normalizzati — «ordine», «codart» — perche' e' con
    quelli che si confronta. Ma a chi deve cercare la cella nel foglio va detto
    il nome che ci vedra' scritto, e quello sta altrove nella stessa voce: nella
    mappatura dei campi o nell'intestazione attesa sulla colonna dell'ordine.
    Quando non c'e' da nessuna parte si mostra il token in maiuscolo: brutto,
    ma vero.
    """

    fonti: list[Any] = []
    mappatura = voce.get("field_mapping")
    if isinstance(mappatura, dict):
        colonne = mappatura.get("columns")
        if isinstance(colonne, dict):
            fonti.extend(colonne.values())
    scrittura = voce.get("order_write")
    if isinstance(scrittura, dict):
        fonti.append(scrittura.get("expected_header"))
    for fonte in fonti:
        if isinstance(fonte, str) and normalizza(fonte) == token:
            return " ".join(fonte.split())
    return token.upper()


def _mancato_per_un_pelo(voci: list[dict[str, Any]],
                         fogli: list[dict[str, Any]]) -> dict[str, Any] | None:
    """L'adattatore a cui manca **poco** per essere questo documento, se c'e'.

    ⚠ Non e' un candidato e non lo diventa: lo stato resta AMBIGUO e nessun
    listino viene letto con un adattatore a cui manca un pezzo. Cambia solo
    quello che il programma dice di sapere.

    Il caso vero, il 22 agosto 2026 sul PC del negozio: il listino BETULLA e'
    uscito AMBIGUO perche' qualcuno l'aveva aperto in Excel e risalvato con la
    cella C1 svuotata — la parola ORDINE non c'era piu'. Le altre quattro
    obbligatorie c'erano tutte, e tutte al loro posto. Il programma aveva tutto
    per dirlo e ha detto «Nessuna firma nota sufficiente»; chi l'ha letto ha
    confermato a mano la mappatura guidata e si e' portato a casa un adattatore
    imparato sopra quello spedito.

    La difesa contro il falso riconoscimento e' la **posizione**: le
    obbligatorie che ci sono devono stare dove il registro dice che stiano.
    Un documento di un altro fornitore che per caso condivide tre nomi di
    colonna non li ha quasi mai anche negli stessi posti.
    """

    migliore: dict[str, Any] | None = None
    for voce in voci:
        firma = voce.get("header_signature")
        if not isinstance(firma, dict) or str(firma.get("kind") or "headers") != "headers":
            continue
        richieste = sorted({normalizza(token) for token in (firma.get("required") or [])} - {""})
        if len(richieste) < MINIMO_OBBLIGATORIE_PRESENTI:
            continue
        attese = firma.get("columns") if isinstance(firma.get("columns"), dict) else {}
        for foglio in fogli:
            for riga in righe_di_intestazione(foglio):
                osservate = set(riga["headers"])
                mancanti = [token for token in richieste if token not in osservate]
                presenti = [token for token in richieste if token in osservate]
                if not mancanti or len(mancanti) > MASSIMO_OBBLIGATORIE_MANCANTI:
                    continue
                if len(presenti) < MINIMO_OBBLIGATORIE_PRESENTI:
                    continue
                posizioni = posizioni_delle_intestazioni(riga["values"])
                fuori_posto = [
                    token for token in presenti
                    if token in attese and posizioni.get(token) != attese.get(token)
                ]
                if fuori_posto:
                    continue
                trovato = {
                    "voce": voce, "foglio": foglio, "riga": riga,
                    "mancanti": mancanti, "presenti": presenti, "attese": attese,
                }
                if migliore is None or (len(mancanti), -len(presenti)) < (
                    len(migliore["mancanti"]), -len(migliore["presenti"])
                ):
                    migliore = trovato
                break
    return migliore


def dettagli_del_mancato_per_un_pelo(trovato: dict[str, Any]) -> list[dict[str, str]]:
    """Che cosa manca, in una forma che si puo' mostrare senza rifare il conto.

    La frase qui sotto la usano la catena e i registri; la pagina compone la
    sua, con gli accenti veri che questo file non usa. Perche' le due non
    divergano, i **dati** sono uno solo e stanno qui.
    """

    voce = trovato["voce"]
    return [
        {
            "header": _nome_leggibile(voce, token),
            "column": lettera_di_colonna(trovato["attese"].get(token)),
        }
        for token in trovato["mancanti"]
    ]


def _frase_del_mancato_per_un_pelo(trovato: dict[str, Any]) -> str:
    """«È il suo, meno questa cosa qui» — detto a chi ha il file davanti."""

    voce = trovato["voce"]
    nome = str(voce.get("display_name") or voce.get("supplier_id") or voce.get("id") or "").strip()
    pezzi = []
    for dettaglio in dettagli_del_mancato_per_un_pelo(trovato):
        titolo, lettera = dettaglio["header"], dettaglio["column"]
        pezzi.append(f"«{titolo}» nella colonna {lettera}" if lettera else f"«{titolo}»")
    quali = " e ".join(pezzi)
    quante = len(trovato["presenti"])
    manca = "manca l'intestazione" if len(pezzi) == 1 else "mancano le intestazioni"
    altre = (f"l'altra obbligatoria c'è, ed è al posto giusto"
             if quante == 1 else
             f"le altre {quante} obbligatorie ci sono tutte, e tutte al posto giusto")
    celle = "quella cella può essere stata svuotata" if len(pezzi) == 1 else "quelle celle possono essere state svuotate"
    return (
        f"Sembra il listino di {nome}, ma {manca} {quali}: {altre}. "
        f"Se il file è stato aperto in Excel e risalvato, {celle} senza volerlo: "
        f"ricarica l'originale del fornitore."
    )


def _rango_completo(candidato: dict[str, Any]) -> tuple[Any, ...]:
    """Quanto bene un candidato descrive il documento, in ordine di importanza."""

    return (candidato["confidence"], candidato["senza_guasti"],
            candidato["obbligatorie"], -candidato["ordine"])


def _versione_imparata_a_parita(scelto: dict[str, Any],
                                candidati: list[dict[str, Any]]) -> dict[str, Any]:
    """Fra lo spedito e quello imparato QUI SOPRA, a parita' piena vince l'imparato.

    E' il principio scritto del registro — «a parita' di `id` vince l'imparato,
    perche' e' la risposta piu' recente e l'ha data qualcuno che aveva il
    documento davanti» — che fino al 22 agosto 2026 valeva per la fusione dei
    due file ma non per questa scelta: qui vinceva chi veniva prima
    nell'elenco, cioe' sempre lo spedito.

    ⚠ La conseguenza non era teorica: teneva chiusa la strada della colonna
    d'ordine. Chi la sposta dalla pagina si porta a casa una voce che legge il
    documento esattamente come la spedita e cambia solo dove si scrive
    l'ordine, quindi le due pareggiano su tutto — misurato sul listino BETULLA
    vero, confidenza 0,99 e cinque obbligatorie tutte e due — e la voce nuova
    restava inerte.

    Vale **solo dentro la stessa famiglia**, cioe' fra `betulla_v1` e il suo
    `betulla_v1__locale`. Fra due adattatori di fornitori diversi che pareggiano
    non c'e' un piu' recente e un meno recente: c'e' solo l'ordine del
    registro, e quello resta com'era. Il caso non si presenta su nessuno dei
    tredici listini del repo — contato il 22 agosto 2026: un candidato per
    documento, zero parita' — e allargare qui una regola per un caso che
    nessuno ha misurato costerebbe piu' di quanto renda.

    Che cosa si perde, e va saputo: una voce imparata vecchia continua a
    vincere anche quando quella spedita migliora, se le due tornano a
    pareggiare. Non a ogni miglioramento — solo a uno che non cambia l'esito
    delle verifiche — e si rimedia cancellando la voce da
    `app/data/adattatori_imparati.json`, che da quando lo spedito non sparisce
    piu' e' un'operazione che non perde niente.
    """

    identificativo = str(scelto["voce"].get("id") or "")
    if identificativo.endswith(SUFFISSO_LOCALE):
        return scelto
    famiglia = adattatore_base(identificativo)
    rango = _rango_completo(scelto)[:3]
    for candidato in candidati:
        suo_id = str(candidato["voce"].get("id") or "")
        if not suo_id.endswith(SUFFISSO_LOCALE):
            continue
        if adattatore_base(suo_id) != famiglia:
            continue
        if _rango_completo(candidato)[:3] == rango:
            return candidato
    return scelto


def riconosci(profilo: dict[str, Any], percorso: Path | None = None) -> dict[str, Any]:
    """Dice a quale adattatore corrisponde un documento, e perche'.

    Il nome del file non entra mai in questa decisione: Noce manda
    documenti chiamati `formattato_104233.xls`, che non dicono niente a
    nessuno, e un fornitore che rinomina il proprio listino non deve diventare
    un fornitore nuovo.

    Una colonna in piu' rispetto a quelle dichiarate, di per se', non declassa
    niente: finisce in `unknown_headers` e in una frase di evidenza.  Un
    fornitore che aggiunge una colonna decorativa non deve costare una chiamata
    AI ogni settimana.  A declassare e' semmai una delle verifiche
    deterministiche: per chi legge per posizione una colonna in piu' sposta
    tutto il resto, e infatti se ne accorge `posizioni_intestazioni`.
    """

    voci, errore = _leggi_registro(percorso)
    if errore:
        return _nessun_candidato(errore)
    fogli = fogli_del_profilo(profilo)
    candidati = _candidati(voci, fogli)
    if not candidati:
        # ⚠ «Nessuna firma nota sufficiente» e' vero e non e' tutto quello che
        # il programma sa: a un adattatore possono mancare una o due
        # intestazioni obbligatorie su cinque, e le altre stare esattamente
        # dove lui dice. Quello non e' un documento sconosciuto, e' «il suo,
        # meno questa cosa qui» — e chi legge la frase deve poterlo capire
        # senza aprire il file e contare le colonne.
        pelo = _mancato_per_un_pelo(voci, fogli)
        if pelo is None:
            return _nessun_candidato("Nessuna firma nota sufficiente")
        esito = _nessun_candidato(_frase_del_mancato_per_un_pelo(pelo))
        # Lo stato resta AMBIGUO: si dice quello che manca, non si legge il
        # listino con un adattatore a cui manca un pezzo.
        quasi = pelo["voce"]
        esito["quasi_adapter_id"] = quasi.get("id")
        esito["quasi_supplier_name"] = str(
            quasi.get("display_name") or quasi.get("supplier_id") or quasi.get("id") or ""
        ).strip()
        esito["quasi_missing"] = dettagli_del_mancato_per_un_pelo(pelo)
        esito["quasi_present"] = len(pelo["presenti"])
        esito["missing_headers"] = list(pelo["mancanti"])
        return esito

    # ⚠ Le verifiche si fanno su TUTTI i candidati, non solo sul vincitore, e
    # il perche' e' la ragione per cui esistono i `__locale`. Da quando una
    # mappatura confermata su un fornitore spedito si scrive accanto invece che
    # addosso, lo stesso documento puo' avere due adattatori che gli somigliano:
    # quello spedito e quello imparato qui. Fra i due deve prendersi il
    # documento **quello che lo legge davvero** — il foglio giusto, la riga
    # giusta, le colonne al loro posto — non quello che viene prima
    # nell'elenco. Sono al massimo una manciata di candidati: il conto si paga.
    for candidato in candidati:
        candidato["verifiche"] = verifica_deterministica(
            candidato["voce"], candidato["foglio"], candidato["impronta"], fogli,
        )
        candidato["senza_guasti"] = all(verifica["ok"] for verifica in candidato["verifiche"])
    # A parita' di confidenza vince chi passa le verifiche; poi chi dichiara
    # piu' intestazioni obbligatorie, che e' lo schema piu' specifico, quindi
    # quello che descrive meglio il documento.  A parita' ancora, l'ordine del
    # registro: fra due adattatori di fornitori DIVERSI che pareggiano su tutto
    # non c'e' niente di meglio da guardare, e quello scritto prima e' quello
    # che il programma sa di aver spedito.
    scelto = max(candidati, key=_rango_completo)
    # ⚠ Fra le due versioni della STESSA cosa la regola e' un'altra: vedi qui
    # sotto.  Va applicata dopo, perche' guarda le altre voci in gara e non si
    # puo' dire di un candidato da solo.
    scelto = _versione_imparata_a_parita(scelto, candidati)
    voce = scelto["voce"]
    foglio = scelto["foglio"]
    osservata = scelto["impronta"]
    firma = voce.get("header_signature") if isinstance(voce.get("header_signature"), dict) else {}

    osservate = set(osservata["headers"])
    richieste = {normalizza(token) for token in (firma.get("required") or [])} - {""}
    dichiarate = richieste | ({normalizza(token) for token in (firma.get("known") or [])} - {""})
    sconosciute = sorted(osservate - dichiarate)
    mancanti = sorted(richieste - osservate)

    verifiche = scelto["verifiche"]
    fallite = [verifica for verifica in verifiche if not verifica["ok"]]

    prove: list[str] = []
    if scelto["kind"] == "headers":
        prove.append(
            f"Impronta per intestazioni di «{voce.get('id')}»: le {len(richieste)} intestazioni "
            f"obbligatorie sono tutte nel foglio «{foglio.get('name')}», riga {osservata['header_row']}"
        )
        if osservata["data_start_row"] is not None:
            prove.append(f"Primi dati alla riga {osservata['data_start_row']}")
    else:
        prove.append(
            f"Impronta per forma delle colonne di «{voce.get('id')}» nel foglio "
            f"«{foglio.get('name')}»: punteggio {scelto['confidence']}"
        )
        prove.extend(scelto["prove"])
    if sconosciute:
        prove.append("Intestazioni non dichiarate, che non declassano lo schema: " + ", ".join(sconosciute))
    if mancanti:
        prove.append("Intestazioni dichiarate obbligatorie e non trovate: " + ", ".join(mancanti))
    for verifica in fallite:
        prove.append(f"Verifica «{verifica['name']}» non superata: {verifica['detail']}")

    if scelto["kind"] == "headers":
        confidenza = 0.99 if not fallite else 0.98
    else:
        confidenza = scelto["confidence"]
    return {
        "state": "SCHEMA_VARIATO" if fallite else "SCHEMA_NOTO",
        "adapter_id": voce.get("id"),
        "confidence": round(confidenza, 2),
        "evidence": prove,
        "signature": osservata,
        "checks": verifiche,
        "unknown_headers": sconosciute,
        "missing_headers": mancanti,
    }


def _nessun_candidato(motivo: str) -> dict[str, Any]:
    """Il documento resta da interpretare: lo dice, invece di scegliere a caso."""

    return {
        "state": "AMBIGUO",
        "adapter_id": None,
        "confidence": 0.0,
        "evidence": [motivo],
        "signature": None,
        "checks": [],
        "unknown_headers": [],
        "missing_headers": [],
    }


def verifica_deterministica(adattatore: dict[str, Any], foglio: dict[str, Any],
                            impronta_osservata: dict[str, Any],
                            fogli: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Le verifiche che sostituiscono la chiamata AI sul percorso veloce.

    Un'impronta che combacia dice che il documento e' di quel fornitore, non
    che il lettore ci puo' lavorare: la colonna del prezzo puo' essere
    diventata testo, il foglio puo' essersi spostato, le colonne possono
    essersi scambiate di posto, il listino puo' essere vuoto.  Sono le cose
    che, senza questo controllo, entrerebbero in silenzio.

    `fogli` serve alla sola verifica del foglio: `sheet: "FIRST"` vuol dire
    «il primo foglio del documento», e per saperlo bisogna vederli tutti.
    """

    mappatura = adattatore.get("field_mapping") if isinstance(adattatore.get("field_mapping"), dict) else {}
    firma = adattatore.get("header_signature") if isinstance(adattatore.get("header_signature"), dict) else {}
    if mappatura:
        verifiche = [
            _verifica_colonne_attese(mappatura, foglio, impronta_osservata),
            _verifica_riga_intestazione(mappatura, impronta_osservata),
            _verifica_foglio(mappatura, foglio, fogli),
        ]
    else:
        verifiche = [{"name": nome, "ok": True, "detail": NON_APPLICABILE}
                     for nome in ("colonne_attese", "riga_intestazione", "foglio")]
    # Un listino vuoto riguarda anche gli adattatori con un lettore dedicato:
    # e' l'unico modo in cui un fornitore sparisce dal confronto senza che
    # niente vada storto.
    verifiche.append(_verifica_righe_dati(mappatura, firma, foglio, impronta_osservata))
    # I tipi si verificano anche senza mappatura: dove stanno i campi numerici
    # lo dicono comunque `header_aliases` e `column_map`.  Saltare la verifica
    # per chi ha un lettore dedicato la toglieva proprio ai tre fornitori piu'
    # grossi del confronto, cioe' dove un prezzo diventato testo fa piu' danno.
    verifiche.append(_verifica_tipi(adattatore, mappatura, foglio, impronta_osservata))
    verifiche.append(_verifica_posizioni(firma, foglio, impronta_osservata))
    return verifiche


def _valori_intestazione(foglio: dict[str, Any], impronta_osservata: dict[str, Any]) -> list[Any]:
    """La riga di intestazione riconosciuta, nell'ordine in cui sta nel foglio.

    Serve la posizione, non il solo insieme dei token: una colonna dichiarata
    per nome si ritrova nel profilo solo sapendo in che colonna sta.
    """

    riga = impronta_osservata.get("header_row")
    if riga is None:
        return []
    for candidato in righe_di_intestazione(foglio):
        if candidato["row"] == riga:
            return candidato["values"]
    return []


def _indice_di_colonna(valori: list[Any], nome: Any) -> int | None:
    """La colonna, 1-based, che porta quel nome — o quel numero, o quella lettera.

    Il numero viene per primo perche' e' gia' la risposta: una mappatura
    dichiara una colonna **per numero** quando quella colonna un nome non ce
    l'ha o ce l'ha uguale a un'altra — su ACERO «COSTO IMPON.» compare due
    volte, in colonna 9 e in colonna 15, e per nome il prezzo non si puo'
    indicare affatto.  E' la stessa regola che applica chi legge davvero
    (`prepare_manifest_sources.column_number`: un intero >= 1 e' il numero di
    colonna e basta).  Finche' questo ramo tornava `None`, `tipi_plausibili`
    non trovava la colonna, si dichiarava non applicabile, e un prezzo
    diventato testo passava come SCHEMA_NOTO 0.99 con zero offerte.

    ⚠ Vale per gli interi VERI: una stringa di cifre («9», «09») per chi
    legge davvero e' un nome di colonna come un altro, e qui deve valere lo
    stesso — rispondere «colonna 9» a una domanda che il lettore rifiutera'
    vorrebbe dire dichiarare verificato un documento che non verra' mai letto
    (revisione avversariale del 13 agosto 2026).

    Il nome viene prima della lettera di proposito: «UM» e «QT» sono
    intestazioni vere di CIPRESSO e sarebbero anche lettere di colonna
    plausibili.  Cercare prima fra le intestazioni evita di leggere la colonna
    567 al posto della terza.
    """

    if isinstance(nome, bool):
        return None
    if isinstance(nome, int):
        return nome if nome >= 1 else None

    cercato = normalizza(nome)
    if not cercato:
        return None
    for posizione, valore in enumerate(valori, start=1):
        if normalizza(valore) == cercato:
            return posizione
    lettere = str(nome or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", lettere):
        return None
    indice = 0
    for lettera in lettere:
        indice = indice * 26 + (ord(lettera) - ord("A") + 1)
    return indice


def _verifica_colonne_attese(mappatura: dict[str, Any], foglio: dict[str, Any],
                             impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    nome = "colonne_attese"
    colonne = mappatura.get("columns")
    if not isinstance(colonne, dict) or not colonne:
        return {"name": nome, "ok": True, "detail": "la mappatura non dichiara colonne: verifica non applicabile"}
    osservate = set(impronta_osservata.get("headers") or [])
    # Una mappatura puo' dichiarare una colonna per nome oppure, quando quella
    # colonna un nome non ce l'ha, per numero: e' la forma che documenta
    # `prepare_manifest_sources`, ed e' il caso di quasi tutte le colonne
    # d'ordine, che arrivano vuote.  Cercare «6» fra le intestazioni non la
    # troverebbe mai, e il fornitore resterebbe da interpretare per sempre.
    attese: set[str] = set()
    per_numero: list[int] = []
    for valore in colonne.values():
        if isinstance(valore, bool) or valore in (None, ""):
            continue
        if isinstance(valore, int) or str(valore).strip().isdigit():
            per_numero.append(int(str(valore).strip()))
            continue
        token = normalizza(valore)
        if token:
            attese.add(token)
    if attese and not osservate:
        return {"name": nome, "ok": False,
                "detail": "nessuna intestazione osservata: le colonne dichiarate per nome non si possono verificare"}
    mancanti = sorted(attese - osservate)
    if mancanti:
        return {"name": nome, "ok": False, "detail": "colonne dichiarate e non trovate: " + ", ".join(mancanti)}
    larghezza = int((foglio.get("active_range") or {}).get("max_column") or 0)
    presenti = set(_colonne_per_indice(foglio)) | set(range(1, larghezza + 1))
    fuori = sorted(indice for indice in per_numero if indice not in presenti)
    if fuori:
        return {"name": nome, "ok": False,
                "detail": "colonne dichiarate per numero e fuori dal documento, largo "
                          f"{larghezza} colonne: " + ", ".join(str(indice) for indice in fuori)}
    conto = len(attese) + len(per_numero)
    coda = f", di cui {len(per_numero)} per numero" if per_numero else ""
    return {"name": nome, "ok": True, "detail": f"tutte le {conto} colonne dichiarate sono nel documento{coda}"}


def _verifica_riga_intestazione(mappatura: dict[str, Any], impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    nome = "riga_intestazione"
    attesa = mappatura.get("header_row")
    if attesa is None:
        return {"name": nome, "ok": True,
                "detail": "la mappatura non dichiara la riga di intestazione: verifica non applicabile"}
    osservata = impronta_osservata.get("header_row")
    return {
        "name": nome,
        "ok": osservata == attesa,
        "detail": f"intestazioni trovate alla riga {osservata}, la mappatura dichiara la riga {attesa}",
    }


def _verifica_foglio(mappatura: dict[str, Any], foglio: dict[str, Any],
                     fogli: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    nome = "foglio"
    atteso = mappatura.get("sheet")
    riconosciuto = foglio.get("name")
    if atteso is None:
        return {"name": nome, "ok": True, "detail": "la mappatura non dichiara un foglio: verifica non applicabile"}
    if str(atteso).strip().upper() == "FIRST":
        # «FIRST» non vuol dire «il primo che combacia»: chi legge davvero il
        # documento — `prepare_manifest_sources.selected_sheet` — prende il
        # foglio numero uno e basta.  Se lo schema e' stato riconosciuto in un
        # foglio piu' in la', per esempio perche' il fornitore ha aggiunto una
        # copertina, il lettore aprirebbe la copertina e non il listino.
        if not fogli:
            return {"name": nome, "ok": True,
                    "detail": "la mappatura vuole il primo foglio del documento, che non è stato osservato"}
        primo = fogli[0].get("name")
        return {
            "name": nome,
            "ok": normalizza(riconosciuto) == normalizza(primo),
            "detail": f"foglio riconosciuto «{riconosciuto}», la mappatura vuole il primo del documento, che è «{primo}»",
        }
    return {
        "name": nome,
        "ok": normalizza(riconosciuto) == normalizza(atteso),
        "detail": f"foglio riconosciuto «{riconosciuto}», la mappatura dichiara «{atteso}»",
    }


def _verifica_righe_dati(mappatura: dict[str, Any], firma: dict[str, Any], foglio: dict[str, Any],
                         impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    nome = "righe_dati"
    intervallo = foglio.get("active_range") or {}
    marcatore = mappatura.get("data_start_marker") or firma.get("data_start_marker")
    if marcatore not in (None, "", {}):
        return _verifica_marcatore_dei_dati(marcatore, foglio)
    inizio = mappatura.get("data_start_row")
    if inizio is None:
        inizio = firma.get("data_start_row")
    if inizio is None:
        inizio = impronta_osservata.get("data_start_row")
    non_vuote = int(intervallo.get("nonempty_rows") or 0)
    if inizio is None:
        return {"name": nome, "ok": non_vuote > 0,
                "detail": f"nessuna riga di dati dichiarata; il foglio ha {non_vuote} righe non vuote"}
    ultima = intervallo.get("max_row")
    ok = isinstance(ultima, int) and ultima >= int(inizio)
    return {"name": nome, "ok": ok,
            "detail": f"ultima riga con contenuto: {ultima}, i dati cominciano alla riga {inizio}"}


def _verifica_marcatore_dei_dati(marcatore: Any, foglio: dict[str, Any]) -> dict[str, Any]:
    """Il separatore che dichiara l'inizio dei dati e' ancora dov'era?

    Il profilo non porta tutte le righe del documento, quindi «non l'ho
    trovato» qui non vuol dire «non c'e'»: chi lo cerca davvero e' il lettore,
    su tutto il file, e li' un marcatore mancante ferma la lettura.  Quello che
    si puo' dire da qui si dice, e il resto si dichiara invece di far credere
    una verifica che non e' stata fatta.

    Un caso pero' e' decidibile subito, ed e' quello che conta: se nelle righe
    profilate il testo compare **piu' di una volta**, l'inizio dei dati e'
    gia' ambiguo e il documento non va letto senza guardarlo.
    """

    nome = "righe_dati"
    if not isinstance(marcatore, dict):
        return {"name": nome, "ok": False,
                "detail": "la regola dell'inizio dei dati non è un oggetto leggibile"}
    indice = marcatore.get("column")
    if isinstance(indice, str) and re.fullmatch(r"[A-Z]{1,3}", indice.strip().upper()):
        numero = 0
        for lettera in indice.strip().upper():
            numero = numero * 26 + (ord(lettera) - ord("A") + 1)
        indice = numero
    if isinstance(indice, bool) or not isinstance(indice, int) or indice < 1:
        return {"name": nome, "ok": False,
                "detail": f"la regola dell'inizio dei dati non dice in quale colonna cercare: «{marcatore.get('column')}»"}
    esatto = str(marcatore.get("equals") or "").strip()
    contenuto = str(marcatore.get("contains") or "").strip()
    cercato = esatto or contenuto
    if not cercato:
        return {"name": nome, "ok": False,
                "detail": "la regola dell'inizio dei dati non dice che cosa cercare"}
    atteso = " ".join(cercato.split()).casefold()
    trovate: list[int] = []
    for riga in righe_visibili_del_foglio(foglio):
        valori = riga.get("values") or []
        if indice > len(valori):
            continue
        letto = " ".join(str(valori[indice - 1] if valori[indice - 1] is not None else "").split()).casefold()
        if (letto == atteso) if esatto else (atteso in letto):
            trovate.append(int(riga.get("row") or 0))
    dove = f"colonna {indice}, testo «{cercato}»"
    if len(trovate) > 1:
        return {"name": nome, "ok": False,
                "detail": f"l'inizio dei dati è ambiguo: {dove} compare alle righe "
                          + ", ".join(str(numero) for numero in trovate)}
    if not trovate:
        return {"name": nome, "ok": True,
                "detail": f"i dati cominciano dopo la riga con {dove}: non è fra le righe "
                          "profilate, si cerca sul documento intero quando si legge"}
    return {"name": nome, "ok": True,
            "detail": f"i dati cominciano dopo la riga {trovate[0]} ({dove})"}


def righe_visibili_del_foglio(foglio: dict[str, Any]) -> list[dict[str, Any]]:
    """Tutte le righe che il profilo porta di questo foglio, senza doppioni.

    Non sono tutte le righe del documento e non pretendono di esserlo: sono
    quelle su cui una verifica fatta sul profilo puo' dire qualcosa.
    """

    per_numero: dict[int, dict[str, Any]] = {}
    gruppi = [foglio.get("header_rows") or [], foglio.get("header_candidates") or [],
              foglio.get("section_rows") or []]
    campioni = foglio.get("samples") or {}
    for chiave in ("initial", "middle", "final"):
        gruppi.append(campioni.get(chiave) or [])
    for gruppo in gruppi:
        for voce in gruppo:
            if not isinstance(voce, dict) or not isinstance(voce.get("values"), list):
                continue
            try:
                numero = int(voce.get("row"))
            except (TypeError, ValueError):
                continue
            per_numero[numero] = {"row": numero, "values": voce["values"]}
    return [per_numero[numero] for numero in sorted(per_numero)]


def posizione_del_campo(adattatore: dict[str, Any], mappatura: dict[str, Any], campo: str) -> Any:
    """Dove il registro dichiara che sta un campo, o `None` se non lo dice.

    Il valore torna com'e' scritto: una lettera di colonna («R»), un numero
    1-based, o il nome dell'intestazione («Descr.Commerciale»). Chi lo usa lo
    risolve sul documento che ha in mano — sono tre modi di dire la stessa cosa,
    e il registro li usa tutti e tre a seconda del fornitore.

    E' la versione pubblica di `_dove_sta_il_campo`, che serviva solo alla
    verifica dei tipi: la stessa dichiarazione dice anche al writer dove
    controllare che la riga di destinazione porti il prodotto giusto.
    """

    return _dove_sta_il_campo(adattatore or {}, mappatura or {}, campo)


def indice_della_colonna(valori: list[Any], dichiarata: Any) -> int | None:
    """La colonna, 1-based, che quella dichiarazione indica su questo foglio.

    Versione pubblica di `_indice_di_colonna`, con le stesse regole: un intero
    vero e' gia' il numero di colonna, un nome si cerca fra le intestazioni, e
    solo per ultima si prova la lettera.  Serve a chi deve **mostrare** dove il
    programma andra' a leggere — la pagina Importa — e deve rispondere la
    stessa cosa che risponde chi legge davvero, non una seconda regola scritta
    altrove.
    """

    return _indice_di_colonna(list(valori or []), dichiarata)


def _dove_sta_il_campo(adattatore: dict[str, Any], mappatura: dict[str, Any], campo: str) -> Any:
    """Dove il registro dice che sta un campo, comunque lo dichiari.

    Tre fornitori su sei non hanno una `field_mapping` perche' hanno un lettore
    dedicato, ma dove stanno le loro colonne il registro lo dice lo stesso:
    BETULLA con `header_aliases` («Cessione»), Larice con `column_map` («O»).
    Sono dichiarazioni gia' scritte, e usarle e' l'unico modo perche' la
    verifica dei tipi valga anche per loro.
    """

    colonne = mappatura.get("columns")
    if isinstance(colonne, dict) and colonne.get(campo):
        return colonne[campo]
    alias = adattatore.get("header_aliases")
    if isinstance(alias, dict):
        for nome in alias.get(campo) or []:
            if nome:
                return nome
    mappa = adattatore.get("column_map")
    if isinstance(mappa, dict) and mappa.get(campo):
        return mappa[campo]
    return None


def _verifica_tipi(adattatore: dict[str, Any], mappatura: dict[str, Any], foglio: dict[str, Any],
                   impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    nome = "tipi_plausibili"
    valori = _valori_intestazione(foglio, impronta_osservata)
    colonne = _colonne_per_indice(foglio)
    misure: list[tuple[str, float]] = []
    for campo in CAMPI_NUMERICI:
        dichiarata = _dove_sta_il_campo(adattatore, mappatura, campo)
        if not dichiarata:
            continue
        indice = _indice_di_colonna(valori, dichiarata)
        colonna = colonne.get(indice) if indice is not None else None
        # Una colonna che non si trova la segnala gia' `colonne_attese`: dirlo
        # due volte non aggiunge niente a chi legge l'esito.
        if colonna is None:
            continue
        # La cella dell'intestazione sta nella stessa colonna dei prezzi ed e'
        # testo: contarla fa scendere la quota dei numeri, e su un listino di
        # poche righe la fa scendere sotto meta'.  Un listino corto non e' un
        # listino sbagliato.
        intestata = indice <= len(valori) and str(valori[indice - 1] or "").strip() != ""
        # Una colonna dichiarata per numero si legge male scritta cosi' com'e'
        # («unit_price_net (9)»): chi legge l'esito deve capire che il 9 e' una
        # colonna e non un valore.
        per_numero = isinstance(dichiarata, int) and not isinstance(dichiarata, bool)
        dove = f"colonna {dichiarata}" if per_numero or str(dichiarata).strip().isdigit() else str(dichiarata)
        misure.append((f"{campo} ({dove})", _quota_leggibile(colonna, "number", int(intestata))))
    if not misure:
        return {"name": nome, "ok": True,
                "detail": "nessuna colonna numerica dichiarata e ritrovata: verifica non applicabile"}
    cattive = [(campo, quota) for campo, quota in misure if not quota > 0.5]
    if cattive:
        return {"name": nome, "ok": False,
                "detail": "colonne dichiarate numeriche ma in maggioranza non numeriche: "
                          + ", ".join(f"{campo} {quota:.0%}" for campo, quota in cattive)}
    return {"name": nome, "ok": True,
            "detail": "colonne numeriche in maggioranza: " + ", ".join(f"{campo} {quota:.0%}" for campo, quota in misure)}


def _verifica_posizioni(firma: dict[str, Any], foglio: dict[str, Any],
                        impronta_osservata: dict[str, Any]) -> dict[str, Any]:
    """Le intestazioni stanno ancora dove il lettore le va a prendere?

    Tre adattatori — BETULLA, il gestionale, Larice — hanno un lettore dedicato
    che legge **per posizione**: `read_betulla` prende il prezzo da `row[5]`, non
    dalla colonna intitolata «Cessione».  L'insieme delle intestazioni non
    basta a difenderli, ed e' stato misurato: con una colonna in piu' in testa
    al listino vero il percorso veloce dichiarava SCHEMA_NOTO 0.99 e il lettore
    restituiva 12,00 euro al posto di 3,98; con due colonne scambiate — stesso
    insieme di nomi, nessuna intestazione nuova — restituiva 6,00 al posto di
    1,25.  Nessun avviso, per una settimana intera di ordini.

    Percio' la firma di quegli adattatori dichiara anche **dove** sta ogni
    intestazione, e qui si confronta.  Un adattatore nativo la cui mappatura
    risolve tutto per nome puo' non dichiarare niente, e la verifica esce non
    applicabile.  Gli adattatori **imparati** invece la dichiarano sempre
    (`impara_adattatore` la scrive d'ufficio), anche quando i campi si
    risolvono per nome: la colonna dove si scrive l'ordine resta posizionale
    — per QUERCIA e' la lettera «N» — e una colonna aggiunta in testa la
    sposterebbe senza cambiare nessun nome.  Il prezzo e' un declassamento a
    SCHEMA_VARIATO (cioe' una conferma in piu') quando il fornitore aggiunge
    una colonna innocua: e' il costo scelto per chiudere la trappola BETULLA.
    """

    nome = "posizioni_intestazioni"
    dichiarate = firma.get("columns")
    if not isinstance(dichiarate, dict) or not dichiarate:
        return {"name": nome, "ok": True,
                "detail": "la firma non dichiara dove stanno le intestazioni: verifica non applicabile"}
    valori = _valori_intestazione(foglio, impronta_osservata)
    if not valori:
        return {"name": nome, "ok": False,
                "detail": "nessuna riga di intestazione osservata: le posizioni dichiarate non si possono verificare"}
    # La stessa funzione che usa chi scrive la firma: se le due parti contassero
    # le posizioni in due modi diversi, un adattatore imparato risulterebbe
    # fuori posto sul documento da cui e' stato imparato.
    osservate = posizioni_delle_intestazioni(valori)
    fuori_posto: list[str] = []
    malformate: list[str] = []
    for token, attesa in dichiarate.items():
        atteso = normalizza(token)
        if not atteso:
            continue
        if isinstance(attesa, bool) or not isinstance(attesa, int):
            # Una posizione che non e' un numero intero non si puo' verificare,
            # e saltarla direbbe «tutto al suo posto» di una dichiarazione mai
            # guardata.  `True` passava proprio cosi', perche' in Python e' un
            # intero (revisione avversariale del 13 agosto 2026).
            malformate.append(f"«{token}» dichiara una posizione che non è un numero intero")
            continue
        trovata = osservate.get(atteso)
        if trovata is None:
            fuori_posto.append(f"«{token}» attesa in colonna {attesa}, non c'è più")
        elif trovata != attesa:
            fuori_posto.append(f"«{token}» attesa in colonna {attesa}, trovata nella {trovata}")
    if malformate:
        return {"name": nome, "ok": False,
                "detail": "la firma dichiara posizioni che non sono numeri interi: "
                          + "; ".join(malformate + fuori_posto)}
    if fuori_posto:
        return {"name": nome, "ok": False,
                "detail": "il lettore di questo fornitore legge per posizione: " + "; ".join(fuori_posto)}
    return {"name": nome, "ok": True,
            "detail": f"tutte le {len(dichiarate)} intestazioni dichiarate sono nella colonna prevista"}


# Quanto si aspetta il proprio turno prima di scrivere nel registro, e dopo
# quanto un turno che nessuno ha restituito si considera abbandonato. Il secondo
# numero e' quello che conta: un processo ucciso a meta' — l'antivirus, la
# chiusura della finestra, il PC spento — lascerebbe il registro chiuso per
# sempre, e un programma che non impara piu' e non lo dice e' peggio del difetto
# che questo lucchetto esiste per chiudere.
ATTESA_DEL_TURNO = 5.0
TURNO_ABBANDONATO = 20.0


@contextlib.contextmanager
def _turno_di_scrittura(documento_percorso: Path):
    """Un processo per volta dentro il giro leggi-modifica-riscrivi.

    ⚠ La scrittura del file e' atomica da sempre — temporaneo con `os.replace`,
    quindi chi legge trova il registro vecchio o quello nuovo, mai uno monco.
    Quello che non era protetto e' il **giro intero**: due scritture che si
    accavallano leggono lo stesso documento di partenza, e la seconda a
    riscrivere cancella la voce della prima. Misurato il 22 agosto 2026: dieci
    scritture insieme, e nel registro ne restava **una**.

    Non e' un caso di laboratorio. Il servizio e' un `ThreadingHTTPServer`,
    `impara_adattatore` gira come processo a se' durante il ricalcolo, e ogni
    comparatore avviato dalla stessa cartella scrive lo **stesso**
    `app/data/adattatori_imparati.json` — i percorsi dei dati si scelgono
    all'avvio, quello del registro no.

    Il lucchetto e' un file creato con `O_EXCL`, che e' l'unica primitiva che
    funziona uguale su Windows e su macOS. Porta dentro chi lo tiene e da
    quando, cosi' un turno abbandonato si riconosce e si toglie invece di
    bloccare tutto per sempre. Se dopo `ATTESA_DEL_TURNO` non si e' ottenuto e
    non risulta abbandonato, **si scrive lo stesso**: il caso peggiore torna a
    essere quello di prima, non uno peggiore, e un adattatore che l'utente ha
    appena confermato non deve andare perso perche' un altro processo e' lento.
    """

    lucchetto = documento_percorso.with_name(documento_percorso.name + ".lock")
    lucchetto.parent.mkdir(parents=True, exist_ok=True)
    preso = False
    scadenza = time.monotonic() + ATTESA_DEL_TURNO
    while True:
        try:
            descrittore = os.open(str(lucchetto), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                eta = time.time() - lucchetto.stat().st_mtime
            except OSError:
                continue  # e' appena sparito: si riprova subito a prenderlo
            if eta > TURNO_ABBANDONATO:
                # Chi lo teneva non c'e' piu'. `missing_ok`: se nel frattempo
                # l'ha tolto lui, va bene lo stesso.
                lucchetto.unlink(missing_ok=True)
                continue
            if time.monotonic() >= scadenza:
                break
            time.sleep(0.02)
            continue
        except OSError:
            # Cartella non scrivibile, disco pieno: non e' questo il posto in
            # cui fermare l'apprendimento di un adattatore.
            break
        with os.fdopen(descrittore, "w", encoding="utf-8") as flusso:
            flusso.write(f"{os.getpid()} {time.time()}\n")
        preso = True
        break
    try:
        yield preso
    finally:
        if preso:
            lucchetto.unlink(missing_ok=True)


def scrivi_adattatore(voce: dict[str, Any], percorso: Path | None = None) -> dict[str, Any]:
    """Scrive una voce nel registro senza perdere quella di prima.

    Il programma finito gira da solo: quando l'utente conferma uno schema
    nuovo, quello vecchio non deve sparire.  Se la settimana dopo il
    riconoscimento peggiora, l'unico modo per capire che cosa e' cambiato e'
    avere ancora sotto gli occhi la versione precedente.

    La scrittura passa da un file temporaneo nella stessa cartella e da
    `os.replace`: un'interruzione a meta' lascerebbe il registro monco, e con
    il registro monco il programma non riconosce piu' nessun fornitore.

    ⚠ Si scrive **sempre e solo** nel registro imparato
    (`app/data/adattatori_imparati.json`), mai in quello spedito: quello sta
    sotto git e l'avvio del PC del negozio lo riporta indietro a ogni doppio
    clic. La versione e la storia (`schema_version`, `previous_versions`) si
    contano pero' sul registro **effettivo**, spedito compreso: il primo
    schema imparato sopra un adattatore spedito e' la sua seconda versione,
    non la prima, e chi legge la storia deve trovarci dentro anche quella da
    cui si e' partiti.
    """

    if not isinstance(voce, dict):
        raise ValueError("La voce da scrivere nel registro deve essere un dizionario")
    identificativo = str(voce.get("id") or "").strip()
    if not identificativo:
        raise ValueError("La voce da scrivere nel registro non ha un «id»")

    documento_percorso = percorso_imparato(percorso)
    # ⚠ Il `with` copre il giro INTERO — leggi, modifica, riscrivi — non la sola
    # scrittura del file, che era gia' atomica. Vedi `_turno_di_scrittura`.
    with _turno_di_scrittura(documento_percorso):
        if documento_percorso.exists():
            try:
                documento = json.loads(documento_percorso.read_text(encoding="utf-8"))
            except (OSError, ValueError) as errore:
                # Riscrivere da zero un registro che non si riesce a leggere
                # vorrebbe dire buttare via tutti gli altri adattatori.
                raise ValueError(f"Il registro degli adattatori non è leggibile: {errore}") from errore
            if not isinstance(documento, dict) or not isinstance(documento.get("adapters"), list):
                raise ValueError("Il registro degli adattatori non ha l'elenco «adapters»")
        else:
            documento = {"schema_version": 1, "adapters": []}

        voci = documento["adapters"]
        posizione = next(
            (indice for indice, esistente in enumerate(voci)
             if isinstance(esistente, dict) and esistente.get("id") == identificativo),
            None,
        )
        # La spedita da cui questa voce deriva, se c'e'. Serve due volte: come
        # versione di partenza quando l'imparato non ha ancora niente, e come
        # timbro — «sono nata sopra questa» — che decide fin quando la voce
        # vale (vedi `_motivo_del_superamento`). Uno spedito che c'e' ma non si
        # legge ferma la scrittura invece di far ripartire la versione da uno:
        # ricominciare da capo vorrebbe dire buttare via la storia di un
        # adattatore senza dirlo a nessuno.
        #
        # ⚠ `adattatore_base`: la prima versione di un `betulla_v1__locale` e' la
        # spedita `betulla_v1`. Cercare l'id intero la farebbe nascere come
        # versione uno e senza storia, e chi rileggesse la voce fra un mese non
        # troverebbe piu' da dove si era partiti — che e' l'unica cosa che
        # permette di capire che cosa e' cambiato.
        spedita_base: dict[str, Any] = {}
        spedito_percorso = Path(percorso or REGISTRO)
        if spedito_percorso.is_file():
            spedite, errore_spedito = _voci_di_un_documento(
                spedito_percorso, quale="Il registro degli adattatori",
            )
            if errore_spedito is not None:
                raise ValueError(f"Il registro degli adattatori non è leggibile: {errore_spedito}")
            cercato = adattatore_base(identificativo)
            spedita_base = next(
                (esistente for esistente in spedite if esistente.get("id") == cercato), {},
            )
        if posizione is not None:
            precedente = dict(voci[posizione])
        else:
            precedente = dict(spedita_base)

        nuova = dict(voce)
        if spedita_base:
            nuova["sopra_spedito"] = sopra_spedito_di(spedita_base)
        else:
            nuova.pop("sopra_spedito", None)
        if not precedente:
            nuova["schema_version"] = 1
            nuova.pop("previous_versions", None)
            storia: list[Any] = []
        else:
            if precedente.get("supplier_id") != voce.get("supplier_id"):
                raise ValueError(
                    f"L'adattatore «{identificativo}» è del fornitore "
                    f"«{precedente.get('supplier_id')}»: non lo si può riscrivere per "
                    f"«{voce.get('supplier_id')}»"
                )
            storia = list(precedente.pop("previous_versions", None) or [])
            storia.append(precedente)
            nuova["schema_version"] = int(precedente.get("schema_version") or 1) + 1
            nuova["previous_versions"] = storia

        if posizione is None:
            voci.append(nuova)
        else:
            voci[posizione] = nuova

        _scrivi_documento(documento_percorso, documento)
    return {
        "id": identificativo,
        "schema_version": nuova["schema_version"],
        "created": not precedente,
        "previous_versions": len(storia),
    }


def metti_da_parte_le_superate(percorso: Path | None = None,
                               quando: str | None = None) -> list[dict[str, Any]]:
    """Sposta le voci imparate che lo spedito ha superato fuori da `adapters`.

    Non si cancellano: finiscono in `adapters_messi_da_parte` nello stesso
    file, con la data e il motivo, perche' quello che il titolare aveva mappato a
    mano e' informazione — dice che cosa vedeva nel listino — e va potuto
    rileggere.  Da `adapters` invece devono uscire, altrimenti ogni confronto
    ripeterebbe lo stesso avviso per sempre.

    Torna le schede delle voci spostate, `[]` se non c'era niente da spostare
    o se uno dei due file non si legge: qui si sposta, non si diagnostica.
    """

    spedito_percorso = Path(percorso or REGISTRO)
    documento_percorso = percorso_imparato(spedito_percorso)
    if not documento_percorso.is_file():
        return []
    spedite, errore = _voci_di_un_documento(spedito_percorso, quale="Registro degli adattatori")
    if errore is not None:
        return []
    per_id = _spedite_per_id(spedite)
    with _turno_di_scrittura(documento_percorso):
        try:
            documento = json.loads(documento_percorso.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(documento, dict) or not isinstance(documento.get("adapters"), list):
            return []
        restano: list[Any] = []
        messe: list[dict[str, Any]] = []
        schede: list[dict[str, Any]] = []
        for voce in documento["adapters"]:
            motivo = _motivo_del_superamento(voce, per_id) if isinstance(voce, dict) else None
            if motivo is None:
                restano.append(voce)
                continue
            messe.append({
                **voce,
                "messo_da_parte_il": quando or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "messo_da_parte_perche": motivo,
            })
            schede.append(_scheda_della_superata(voce, motivo))
        if not messe:
            return []
        documento["adapters"] = restano
        documento["adapters_messi_da_parte"] = [
            *(documento.get("adapters_messi_da_parte") or []), *messe,
        ]
        _scrivi_documento(documento_percorso, documento)
    return schede


def _scrivi_documento(documento_percorso: Path, documento: dict[str, Any]) -> None:
    """Sostituisce il registro in un colpo solo, a fine riga LF.

    Il registro sta sotto git con fine riga LF: riscriverlo in CRLF farebbe
    apparire modificato l'intero file, e il diff che una persona deve leggere
    prima di accettare uno schema nuovo diventerebbe illeggibile.
    """

    documento_percorso.parent.mkdir(parents=True, exist_ok=True)
    descrittore, temporaneo = tempfile.mkstemp(
        prefix=documento_percorso.name + ".", suffix=".tmp", dir=str(documento_percorso.parent)
    )
    try:
        with os.fdopen(descrittore, "w", encoding="utf-8", newline="\n") as flusso:
            json.dump(documento, flusso, ensure_ascii=False, indent=2)
            flusso.write("\n")
        os.replace(temporaneo, documento_percorso)
    except BaseException:
        Path(temporaneo).unlink(missing_ok=True)
        raise
