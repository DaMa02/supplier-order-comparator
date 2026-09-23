#!/usr/bin/env python3
"""I prodotti che nessun fornitore può dare: l'elenco per telefonare in giro.

Punto 5.  Fino a ieri un prodotto che l'utente voleva ordinare e che nessun
fornitore aveva — né a listino né disponibile — usciva dalla compilazione senza
lasciare traccia: la quantità veniva azzerata, la riga spariva dall'ordine e chi
compilava non sapeva più che quel prodotto lo doveva comprare comunque, da
qualche altra parte.  Questo modulo scrive il foglio che resta in mano a quella
persona: sette colonne, una riga per prodotto, e una colonna `Note` vuota che
riempie lei mentre telefona.

**Il filtro di chi entra nell'elenco non sta qui.**  Chi ha un'offerta
utilizzabile e chi no lo sa `app/server.py`, che possiede già `find_offer` e
`offer_is_available`; questo modulo riceve righe già scelte e le scrive.  Non
importa il server — sarebbe una dipendenza circolare — e non riscrive quel
predicato, perché due definizioni dello stesso «non disponibile» sono due verità
sullo stesso dato.

**Il file non si rilegge mai.**  Nessun percorso del programma lo importa, lo
confronta o lo trascina alla compilazione successiva: è un foglio per una
persona, non un formato di scambio.

`openpyxl` è già una dipendenza obbligatoria (`app/launcher.py:128-130` ferma
l'avvio se manca) ma finora il codice di produzione l'aveva usato **solo per
leggere**.  Qui si scrive, e si scrive il minimo: nessuna formula, nessuno
stile elaborato, intestazioni in grassetto e colonne larghe abbastanza da
leggere una descrizione senza allargarle a mano.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

import consegna
# Lo stato di un'offerta rifiutata da chi ordina lo dichiara `offerta.py`, che è
# già l'autorità su «si può ordinare»: qui si LEGGE quella stringa, e riscriverla
# vorrebbe dire che il giorno in cui cambia questo foglio torna a dire «non ce
# l'ha nessuno» su una riga che invece è stata scartata da chi telefona.
from offerta import STATO_RIFIUTATO_UTENTE


# Il `tipo` con cui questo file compare nell'inventario della cartella,
# nell'audit e nello zip.  La stringa vive in `consegna`, che è il modulo che
# deve riconoscere il file anche quando l'audit non c'è: una seconda costante
# qui sarebbe una seconda verità, e il giorno in cui una delle due cambiasse il
# file smetterebbe di essere consegnato senza che niente lo dica.
TIPO = consegna.TIPO_DA_REPERIRE

# Le due sole frasi possibili della colonna `Motivo`.  Sono due e non una
# perché dicono due cose diverse a chi telefona: «a listino non ce l'ha
# nessuno» vuol dire cercare un fornitore nuovo, «nessuno ce l'ha disponibile»
# vuol dire richiamare fra qualche giorno gli stessi.
MOTIVO_NON_A_LISTINO = "Nessun fornitore lo ha a listino"
MOTIVO_NON_DISPONIBILE = "Nessun fornitore lo ha disponibile"
# La terza, dal 21 agosto 2026. Dice una cosa che le altre due non possono dire:
# che il fornitore una riga simile ce l'ha, e che a scartarla è stato chi ordina.
# A chi telefona serve, perché cambia l'azione: quel fornitore non va richiamato
# fra qualche giorno — l'articolo giusto non ce l'ha — va cercato altrove.
MOTIVO_RIFIUTATO_DA_TE = "Rifiutato da te: la riga del fornitore era un altro articolo"

# Lo stato di un'offerta che il confronto non ha proprio trovato.
STATO_NON_TROVATO = "NON_TROVATO"

# Le intestazioni, in quest'ordine.  Non c'è nessuna colonna per il codice del
# gestionale: quel codice non esiste.  Il gestionale lavora con l'EAN e con gli
# EAN alternativi, e gli alternativi oggi non escono dall'esportazione — decisione
# di Daniele del 17 agosto 2026, dopo che la colonna era nata vuota.  Se un giorno
# gli EAN alternativi usciranno, sono più EAN dello stesso prodotto: il posto dove
# metterli si decide allora, e non è detto sia una colonna sola.
INTESTAZIONI = (
    "EAN",
    "Descrizione",
    "Colli richiesti",
    "Ultimo prezzo noto",
    "Motivo",
    "Note",
)

# Larghezze in caratteri, nell'ordine delle intestazioni.  La descrizione è la
# colonna larga perché è quella che si legge; `Note` è larga perché è quella che
# si scrive.
LARGHEZZE = (16, 52, 14, 18, 34, 40)

# L'unità che non si scrive dentro la cella: l'intestazione dice già «Colli».
UNITA_IMPLICITA = "colli"

NOME_FOGLIO = "Da reperire"

# Il formato numerico del prezzo: due decimali.  Non è uno stile, è il modo di
# non mostrare `3.9000000000000004` a chi legge.
FORMATO_PREZZO = "#,##0.00"


def nome_file(momento: datetime) -> str:
    """`Prodotti da reperire — 17 agosto 2026.xlsx`, con l'em dash U+2014 spaziato.

    La data la rende `consegna.data_leggibile`, la stessa che dà il nome ai
    listini compilati: i mesi vengono da una tabella e non da `strftime("%B")`,
    che dipende dalla localizzazione e su questa macchina direbbe `August`.

    L'em dash è voluto ed è lo stesso di `consegna.nome_listino`: regge nello
    zip (bandiera 0x800) e nell'intestazione di scaricamento, dove ci pensa
    `consegna.intestazione_allegato`.
    """
    return f"{consegna.PREFISSO_DA_REPERIRE}— {consegna.data_leggibile(momento)}.xlsx"


def motivo(offerte: Sequence[Any] | None) -> str:
    """Perché non si può ordinare, in una frase.

    Funzione pura sulla lista `offers` del prodotto: non conosce il server, non
    apre file e non decide chi entra nell'elenco — quello lo ha già deciso chi
    chiama.

    Basta **un'offerta rifiutata da chi ordina** perché la frase sia la terza:
    quel prodotto è qui per una decisione umana, non per un buco del listino.

    Se **ogni** offerta è `NON_TROVATO` nessun fornitore lo ha nemmeno a
    listino.  Basta un'offerta con un altro stato — il fornitore l'articolo ce
    l'ha, ma l'offerta non è utilizzabile — perché la frase diventi l'altra.

    Un prodotto senza nessuna offerta (nessun fornitore ha risposto niente su di
    lui) è il caso limite di «nessuno lo ha a listino», e infatti `all()` su una
    lista vuota è vero.
    """
    stati = []
    for offerta in offerte or ():
        if not isinstance(offerta, dict):
            # Non è un'offerta leggibile: non si può affermare che non sia a
            # listino, e la frase prudente è l'altra.
            stati.append("")
            continue
        stato = offerta.get("status") or offerta.get("matchStatus") or ""
        stati.append(str(stato).strip().upper())
    # Basta UN rifiuto perché la frase diventi questa, e viene prima delle altre
    # due: è l'informazione che spiega perché un prodotto è in questo elenco pur
    # avendo un fornitore con una riga, e senza di lei il foglio direbbe «nessun
    # fornitore lo ha disponibile» — cioè darebbe la colpa al fornitore di una
    # decisione presa da chi legge il foglio.
    if any(stato == STATO_RIFIUTATO_UTENTE for stato in stati):
        return MOTIVO_RIFIUTATO_DA_TE
    if all(stato == STATO_NON_TROVATO for stato in stati):
        return MOTIVO_NON_A_LISTINO
    return MOTIVO_NON_DISPONIBILE


def _quantita_leggibile(quantita: Any, unita: Any) -> Any:
    """La cella della colonna 4: un numero, oppure `3 espositori`.

    L'intestazione resta fissa «Colli richiesti» perché l'elenco mescola
    prodotti ed espositori in una tabella sola: quando l'unità non è «colli» la
    si scrive **dentro** la cella, altrimenti tre espositori si leggerebbero
    come tre colli.
    """
    etichetta = str(unita or "").strip()
    if not etichetta or etichetta.casefold() == UNITA_IMPLICITA:
        return quantita
    return f"{quantita} {etichetta}"


def scrivi(cartella: Path, righe: list[dict], momento: datetime) -> str:
    """Scrive il file nella cartella e ne restituisce il **nome**.

    Ogni riga: `{"ean", "descrizione", "quantita", "unita", "ultimo_prezzo",
    "motivo"}`.  Una chiave in più viene ignorata.  `ultimo_prezzo` può essere `None` e
    allora la cella resta vuota: uno zero direbbe che quel prodotto costava
    zero, che è una cosa diversa da «non lo sappiamo».

    `righe` vuoto è un errore del chiamante, non un file vuoto: un foglio con le
    sole intestazioni finirebbe nell'elenco della compilazione e nello zip, e
    direbbe a chi lo apre che c'è qualcosa da reperire quando non c'è niente.

    L'EAN si scrive come **testo**: un codice a barre di tredici cifre trattato
    da numero diventa `8.0059e+12` in notazione scientifica, e chi lo copia per
    cercarlo trova zero risultati.

    Scrittura in due tempi — file temporaneo accanto e `os.replace` — perché un
    `.xlsx` troncato a metà salvataggio è uno zip rotto che Excel non apre, e in
    cartella sarebbe indistinguibile da un documento buono.  Il temporaneo
    finisce in `.tmp`, che `consegna.e_documento` non considera un documento:
    anche se restasse lì non verrebbe né elencato né consegnato.
    """
    righe = list(righe or [])
    if not righe:
        raise ValueError("Non ci sono prodotti da reperire da scrivere")

    cartella = Path(cartella)
    nome = nome_file(momento)

    libro = Workbook()
    foglio = libro.active
    foglio.title = NOME_FOGLIO

    foglio.append(list(INTESTAZIONI))
    grassetto = Font(bold=True)
    for colonna in range(1, len(INTESTAZIONI) + 1):
        foglio.cell(row=1, column=colonna).font = grassetto
        foglio.column_dimensions[get_column_letter(colonna)].width = LARGHEZZE[colonna - 1]
    # Le intestazioni restano in vista mentre si scorre l'elenco al telefono.
    foglio.freeze_panes = "A2"

    for indice, riga in enumerate(righe, start=2):
        ean = riga.get("ean")
        # EAN e descrizione arrivano dai listini dei fornitori: testo che non
        # controlliamo.  openpyxl scrive come FORMULA una stringa che comincia
        # per «=» — un EAN o un nome prodotto messo così da un fornitore
        # smetterebbe di essere testo e diventerebbe un calcolo (o un
        # `#NAME?`).  `data_type = "s"` forza la cella a restare testo
        # letterale qualunque cosa contenga: non toglierla credendola
        # ridondante con `str(...)`, che protegge dal tipo ma non dal segno.
        cella_ean = foglio.cell(row=indice, column=1, value="" if ean is None else str(ean))
        cella_ean.data_type = "s"
        cella_descrizione = foglio.cell(row=indice, column=2, value=str(riga.get("descrizione") or ""))
        cella_descrizione.data_type = "s"
        foglio.cell(
            row=indice,
            column=3,
            value=_quantita_leggibile(riga.get("quantita"), riga.get("unita")),
        )
        prezzo = riga.get("ultimo_prezzo")
        if prezzo is not None:
            cella = foglio.cell(row=indice, column=4, value=prezzo)
            if isinstance(prezzo, (int, float)) and not isinstance(prezzo, bool):
                cella.number_format = FORMATO_PREZZO
        # La colonna 5 (`Motivo`) non riceve mai testo altrui — `motivo()`
        # restituisce una delle due costanti sopra — ma la si blinda uguale
        # per uniformità con le due colonne che invece lo ricevono: cintura
        # sopra le bretelle, non un buco da chiudere.
        cella_motivo = foglio.cell(row=indice, column=5, value=str(riga.get("motivo") or ""))
        cella_motivo.data_type = "s"
        # La colonna 6 (`Note`) si lascia com'è: è quella che riempie l'utente.

    percorso = cartella / nome
    temporaneo = percorso.with_name(percorso.name + ".tmp")
    libro.save(temporaneo)
    os.replace(temporaneo, percorso)
    return nome
